import json
from pathlib import Path
from typing import Any

import pytest

from vocareum_tools import release, upload
from vocareum_tools.config import ConfigError


def make_config(root: Path) -> upload.Config:
    return upload.Config(
        api_base="https://example.test/api/v2",
        course_id="100002",
        assignment_id="100003",
        part_id="100004",
        api_key="api-key",
        course="graphrag",
        env_path=root / ".env",
    )


def write_template(root: Path, revision: str = "a" * 40) -> Path:
    path = root / "courses" / "graphrag" / "lab.template"
    path.parent.mkdir(parents=True)
    path.write_text(
        "Parameters:\n"
        "  RepoCommit:\n"
        "    Description: pinned source\n"
        f"    Default: {revision}\n"
        "Resources: {}\n",
        encoding="utf-8",
    )
    return path


def write_course_env(root: Path) -> None:
    path = root / "courses" / "graphrag.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "VOCAREUM_COURSE_ID=100002\n"
        "VOCAREUM_ASSIGNMENT_ID=100003\n"
        "VOCAREUM_PART_ID=100004\n",
        encoding="utf-8",
    )


def local_release(root: Path) -> release.LocalRelease:
    template = write_template(root)
    starter_files = {
        ".env.example": b"NEO4J_URI=replace-me\n",
        "notebooks/01/lesson.ipynb": b'{"cells": []}\n',
    }
    starter_path = root / ".vocareum" / "build" / "graphrag" / "startercode.zip"
    inventory_path = starter_path.with_name("startercode.inventory.json")
    starter_path.parent.mkdir(parents=True)
    starter_payload = release.tree_archive(starter_files)
    inventory_payload = b'{"file_count": 2}\n'
    starter_path.write_bytes(starter_payload)
    inventory_path.write_bytes(inventory_payload)
    return release.LocalRelease(
        template_path=template,
        template_payload=template.read_bytes(),
        source_revision="a" * 40,
        starter_path=starter_path,
        starter_payload=starter_payload,
        inventory_path=inventory_path,
        inventory_payload=inventory_payload,
        starter_files=starter_files,
    )


def prepared_release(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    previous_template: bytes = b"old template",
    previous_starter: dict[str, bytes] | None = None,
) -> tuple[release.ReleasePlan, release.LocalRelease]:
    local = local_release(root)
    previous_starter = previous_starter or {"old.txt": b"old starter"}
    monkeypatch.setattr(release, "read_remote_file", lambda *args: previous_template)
    monkeypatch.setattr(
        release, "read_remote_tree", lambda *args: previous_starter.copy()
    )
    plan = release.prepare_release(root, make_config(root), local)
    return plan, local


def test_source_revision_reads_only_a_pinned_commit(tmp_path: Path) -> None:
    source = write_template(tmp_path, "f" * 40)

    assert release.source_revision(source) == "f" * 40

    source.write_text(
        "Parameters:\n  RepoCommit:\n    Default: main\n", encoding="utf-8"
    )
    with pytest.raises(upload.ConfigError, match="40-character commit"):
        release.source_revision(source)


def test_source_revision_reads_no_infrastructure_metadata(tmp_path: Path) -> None:
    source = tmp_path / "lab.template"
    source.write_text(
        "Metadata:\n"
        "  SourceRepository: https://example.test/course.git\n"
        f"  SourceCommit: {'e' * 40}\n"
        "Resources: {}\n",
        encoding="utf-8",
    )

    assert release.source_revision(source) == "e" * 40


def test_default_mode_is_local_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    write_course_env(tmp_path)
    config = make_config(tmp_path)
    local = local_release(tmp_path)
    monkeypatch.setattr(release, "load_config", lambda *args: config)
    monkeypatch.setattr(release, "build_local_release", lambda *args: local)
    monkeypatch.setattr(
        release,
        "read_remote_file",
        lambda *args: pytest.fail("preview must not call Vocareum"),
    )
    monkeypatch.setattr(
        release,
        "read_remote_tree",
        lambda *args: pytest.fail("preview must not call Vocareum"),
    )

    result = release.run(release.parse_args(["--course", "graphrag"]), tmp_path)
    output = capsys.readouterr().out

    assert result == release.EXIT_OK
    assert local.template_path.as_posix() in output
    assert local.starter_path.as_posix() in output
    assert "No Vocareum API call was made" in output


def test_release_refuses_course_id_shell_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_course_env(tmp_path)
    config = make_config(tmp_path)
    monkeypatch.setenv("VOCAREUM_PART_ID", "wrong-part")
    changed = upload.Config(
        api_base=config.api_base,
        course_id=config.course_id,
        assignment_id=config.assignment_id,
        part_id="wrong-part",
        api_key=config.api_key,
        course=config.course,
        env_path=config.env_path,
    )

    with pytest.raises(upload.ConfigError, match="Unset shell overrides"):
        release.verify_course_destination(changed)


def test_prepare_saves_both_prior_artifacts_without_uploading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous_starter = {
        "README.md": b"old",
        "notebooks/01/old.ipynb": b"old notebook",
    }
    monkeypatch.setattr(
        release,
        "upload",
        lambda *args: pytest.fail("prepare must not upload"),
    )
    monkeypatch.setattr(
        release,
        "upload_archive",
        lambda *args, **kwargs: pytest.fail("prepare must not upload"),
    )

    plan, _ = prepared_release(
        tmp_path,
        monkeypatch,
        previous_template=b"prior",
        previous_starter=previous_starter,
    )
    directory = release.release_directory(tmp_path, "graphrag", plan.release_id)
    _, rollback_files = release.read_zip_files(directory / "rollback-startercode.zip")

    assert (directory / "rollback-lab.template").read_bytes() == b"prior"
    assert rollback_files == previous_starter
    recorded = json.loads((directory / "plan.json").read_text())
    assert recorded["previous_digest"] == upload.digest(b"prior")
    assert recorded["starter_previous_file_count"] == 2
    assert recorded["starter_file_count"] == 2


def test_read_remote_tree_preserves_nested_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directories = {
        "/voc/startercode": ["README.md", "notebooks"],
        "/voc/startercode/notebooks": ["01"],
        "/voc/startercode/notebooks/01": ["lesson.ipynb"],
    }
    files = {
        "startercode/README.md": b"readme",
        "startercode/notebooks/01/lesson.ipynb": b"notebook",
    }
    monkeypatch.setattr(
        release,
        "list_remote_directory",
        lambda config, path: directories.get(path),
    )

    def read_file(config: upload.Config, path: str) -> bytes:
        if path not in files:
            pytest.fail(f"directory must not be downloaded as a file: {path}")
        return files[path]

    monkeypatch.setattr(
        release,
        "read_remote_file",
        read_file,
    )

    assert release.read_remote_tree(make_config(tmp_path), "startercode") == {
        "README.md": b"readme",
        "notebooks/01/lesson.ipynb": b"notebook",
    }


def test_upload_starter_tree_waits_for_the_remote_tree_to_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {
        "README.md": b"readme",
        "notebooks/01/lesson.ipynb": b"notebook",
    }
    observations: list[dict[str, bytes] | Exception] = [
        release.RemoteTreeChangedError("listed file disappeared"),
        {"README.md": b"readme", "notebooks": b"temporary directory response"},
        expected,
    ]
    sleeps: list[int] = []

    def read_tree(*args: Any) -> dict[str, bytes]:
        observation = observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return observation

    monkeypatch.setattr(release, "upload_archive", lambda *args, **kwargs: "tx-1")
    monkeypatch.setattr(release, "poll_transaction", lambda *args: "success")
    monkeypatch.setattr(release, "read_remote_tree", read_tree)
    monkeypatch.setattr(release.time, "sleep", sleeps.append)

    result, transaction_id, actual = release.upload_starter_tree(
        make_config(tmp_path), b"archive", expected
    )

    assert result == release.EXIT_OK
    assert transaction_id == "tx-1"
    assert actual == expected
    assert sleeps == [
        release.REMOTE_TREE_VERIFY_POLL_SECONDS,
        release.REMOTE_TREE_VERIFY_POLL_SECONDS,
    ]


def test_apply_refuses_starter_drift_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, local = prepared_release(tmp_path, monkeypatch)
    monkeypatch.setattr(
        release, "remote_template_digest", lambda *args: plan.previous_digest
    )
    monkeypatch.setattr(
        release, "read_remote_tree", lambda *args: {"changed.txt": b"unrelated"}
    )
    monkeypatch.setattr(
        release,
        "upload_starter_tree",
        lambda *args: pytest.fail("drift must stop before upload"),
    )

    with pytest.raises(release.ReleaseError, match="starter code changed"):
        release.apply_release(tmp_path, make_config(tmp_path), plan.release_id, local)


def test_apply_uploads_verifies_and_records_both_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous_starter = {"old.txt": b"old"}
    plan, local = prepared_release(
        tmp_path, monkeypatch, previous_starter=previous_starter
    )
    template_state = {"digest": plan.previous_digest}
    starter_state = {"files": previous_starter}
    monkeypatch.setattr(
        release, "remote_template_digest", lambda *args: template_state["digest"]
    )
    monkeypatch.setattr(
        release, "read_remote_tree", lambda *args: starter_state["files"].copy()
    )

    def put_starter(*args: Any) -> tuple[int, str, dict[str, bytes]]:
        starter_state["files"] = local.starter_files
        return release.EXIT_OK, "starter-1", local.starter_files

    def put_template(*args: Any) -> tuple[int, str]:
        template_state["digest"] = upload.digest(local.template_payload)
        return release.EXIT_OK, "template-1"

    monkeypatch.setattr(release, "upload_starter_tree", put_starter)
    monkeypatch.setattr(release, "upload_template", put_template)

    result = release.apply_release(
        tmp_path, make_config(tmp_path), plan.release_id, local
    )
    directory = release.release_directory(tmp_path, "graphrag", plan.release_id)
    record = json.loads((directory / "deployment.json").read_text())

    assert result == release.EXIT_OK
    assert record["result"] == "success"
    assert record["remote_digest"] == upload.digest(local.template_payload)
    assert record["starter_remote_digest"] == upload.digest(local.starter_payload)
    assert record["transaction_id"] == "template-1"
    assert record["starter_transaction_id"] == "starter-1"


def test_restore_refuses_unrelated_remote_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = prepared_release(tmp_path, monkeypatch)
    monkeypatch.setattr(release, "remote_template_digest", lambda *args: "unrelated")
    monkeypatch.setattr(release, "read_remote_tree", lambda *args: {})
    monkeypatch.setattr(
        release,
        "upload_template",
        lambda *args: pytest.fail("unrelated content must not be overwritten"),
    )

    with pytest.raises(release.ReleaseError, match="template is unrelated"):
        release.restore_release(tmp_path, make_config(tmp_path), plan.release_id)


def test_restore_uploads_both_saved_prior_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous_starter = {"prior.txt": b"prior starter"}
    plan, local = prepared_release(
        tmp_path,
        monkeypatch,
        previous_template=b"prior template",
        previous_starter=previous_starter,
    )
    template_state = {"digest": plan.source_digest}
    starter_state = {"files": local.starter_files}
    monkeypatch.setattr(
        release, "remote_template_digest", lambda *args: template_state["digest"]
    )
    monkeypatch.setattr(
        release, "read_remote_tree", lambda *args: starter_state["files"].copy()
    )

    def put_starter(
        config: upload.Config, archive: bytes, files: dict[str, bytes]
    ) -> tuple[int, str, dict[str, bytes]]:
        starter_state["files"] = files
        return release.EXIT_OK, "restore-starter", files

    def put_template(config: upload.Config, payload: bytes) -> tuple[int, str]:
        template_state["digest"] = upload.digest(payload)
        return release.EXIT_OK, "restore-template"

    monkeypatch.setattr(release, "upload_starter_tree", put_starter)
    monkeypatch.setattr(release, "upload_template", put_template)

    result = release.restore_release(tmp_path, make_config(tmp_path), plan.release_id)
    directory = release.release_directory(tmp_path, "graphrag", plan.release_id)
    record = json.loads((directory / "restore.json").read_text())

    assert result == release.EXIT_OK
    assert template_state["digest"] == upload.digest(b"prior template")
    assert starter_state["files"] == previous_starter
    assert record["result"] == "success"
    assert record["transaction_id"] == "restore-template"
    assert record["starter_transaction_id"] == "restore-starter"


def test_apply_and_restore_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        release.parse_args(["--course", "graphrag", "--apply", "x", "--restore", "y"])


def test_publish_is_mutually_exclusive_with_split_release_modes() -> None:
    with pytest.raises(SystemExit):
        release.parse_args(["--course", "graphrag", "--publish", "--prepare"])


def test_publish_prepares_then_applies_the_generated_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = local_release(tmp_path)
    config = make_config(tmp_path)
    plan = release.ReleasePlan(
        schema_version=release.RELEASE_SCHEMA,
        release_id="20260825T143000123456Z-abc123def456",
        created_at="2026-08-25T14:30:00.123456Z",
        course="graphrag",
        course_id=config.course_id,
        assignment_id=config.assignment_id,
        part_id=config.part_id,
        source_file="courses/graphrag/lab.template",
        source_revision=local.source_revision,
        source_size=len(local.template_payload),
        source_digest=upload.digest(local.template_payload),
        remote_path=release.TEMPLATE_REMOTE_PATH,
        previous_present=True,
        previous_digest=upload.digest(b"old template"),
        starter_source_file=str(local.starter_path),
        starter_source_size=len(local.starter_payload),
        starter_source_digest=upload.digest(local.starter_payload),
        starter_inventory_file=str(local.inventory_path),
        starter_inventory_digest=upload.digest(local.inventory_payload),
        starter_file_count=len(local.starter_files),
        starter_remote_path=release.STARTER_TARGET,
        starter_previous_digest=upload.digest(b"old starter"),
        starter_previous_file_count=1,
    )
    calls: list[tuple[str, str | None]] = []

    def prepare(*args: Any) -> release.ReleasePlan:
        calls.append(("prepare", None))
        return plan

    def apply(*args: Any) -> int:
        calls.append(("apply", args[2]))
        assert args[3] is local
        return release.EXIT_OK

    monkeypatch.setattr(release, "prepare_release", prepare)
    monkeypatch.setattr(release, "apply_release", apply)

    result = release.publish_release(tmp_path, config, local)

    assert result == release.EXIT_OK
    assert calls == [("prepare", None), ("apply", plan.release_id)]


def write_inventory(root: Path, files: dict[str, bytes]) -> Path:
    """Write a real starter-code inventory for the files --verify-live expects."""
    path = (
        root / ".vocareum" / "build" / "graphrag" / release.startercode.INVENTORY_NAME
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "course": "graphrag",
                "target": "startercode",
                "file_count": len(files),
                "files": [
                    {
                        "path": name,
                        "size": len(payload),
                        "sha256": release.digest(payload),
                    }
                    for name, payload in sorted(files.items())
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def run_verify_live(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    published: dict[str, bytes],
    live: dict[str, bytes],
    live_template: bytes | None = None,
) -> int:
    template = write_template(root)
    write_inventory(root, published)
    monkeypatch.setattr(
        release,
        "remote_template_digest",
        lambda *args: (
            release.digest(live_template)
            if live_template is not None
            else release.digest(template.read_bytes())
        ),
    )
    monkeypatch.setattr(release, "read_remote_tree", lambda *args: live.copy())
    return release.verify_live(root, make_config(root))


def test_verify_live_passes_when_the_live_tree_is_the_published_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {"README.md": b"hello\n", "notebooks/01/lesson.ipynb": b'{"cells": []}\n'}
    assert (
        run_verify_live(tmp_path, monkeypatch, published=files, live=dict(files))
        == release.EXIT_OK
    )


def test_verify_live_catches_a_wholesale_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The 2026-08-25 failure: the tree replaced by its pre-release snapshot."""
    published = {"README.md": b"hello\n", "notebooks/01/lesson.ipynb": b"{}\n"}
    result = run_verify_live(
        tmp_path,
        monkeypatch,
        published=published,
        live={"lab-01-verify-environment.ipynb": b"the other course\n"},
    )
    assert result == release.EXIT_MISMATCH
    output = capsys.readouterr().out
    assert "DRIFTED" in output
    assert "lab-01-verify-environment.ipynb" in output


def test_verify_live_reports_a_changed_file_separately_from_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = {"a.txt": b"one\n", "b.txt": b"two\n", "c.txt": b"three\n"}
    live = {"a.txt": b"one\n", "b.txt": b"EDITED\n", "extra.txt": b"leftover\n"}
    drift = release.compare_live(
        {
            name: (len(payload), release.digest(payload))
            for name, payload in published.items()
        },
        live,
        "expected-template",
        "expected-template",
    )
    assert drift.missing == ("c.txt",)
    assert drift.changed == ("b.txt",)
    assert drift.unexpected == ("extra.txt",)
    assert not drift.starter_matches
    assert drift.template_matches
    assert not drift.matches


def test_verify_live_catches_a_reverted_template_with_an_intact_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {"README.md": b"hello\n"}
    assert (
        run_verify_live(
            tmp_path,
            monkeypatch,
            published=files,
            live=dict(files),
            live_template=b"some other template",
        )
        == release.EXIT_MISMATCH
    )


def test_verify_live_refuses_without_a_recorded_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_template(tmp_path)
    with pytest.raises(release.ReleaseError, match="Could not read the starter-code"):
        release.verify_live(tmp_path, make_config(tmp_path))


def test_verify_live_needs_no_prepared_release_or_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the mode: it must run when nothing else can."""
    files = {"README.md": b"hello\n"}
    write_course_env(tmp_path)
    config = make_config(tmp_path)
    template = write_template(tmp_path)
    write_inventory(tmp_path, files)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("--verify-live must not build the local release.")

    monkeypatch.setattr(release, "load_config", lambda *args: config)
    monkeypatch.setattr(release, "build_local_release", refuse)
    monkeypatch.setattr(
        release,
        "remote_template_digest",
        lambda *args: release.digest(template.read_bytes()),
    )
    monkeypatch.setattr(release, "read_remote_tree", lambda *args: dict(files))
    args = release.parse_args(["--verify-live", "--course", "graphrag"])
    assert release.run(args, tmp_path) == release.EXIT_OK
    assert not (tmp_path / ".vocareum" / "releases").exists()


def test_display_path_names_a_template_outside_this_repository(
    tmp_path: Path,
) -> None:
    """A release record has to be able to name an external course's template.

    ``Path.relative_to`` raises for a path outside the root, and a course that
    lives in its own repository is exactly that. The absolute path is stable
    across the prepare read and the apply read, which is the only property the
    plan comparison needs.
    """
    root = tmp_path / "aws-vocareum"
    root.mkdir()
    inside = root / "courses" / "local" / "lab.template"
    outside = tmp_path / "workshop" / "vocareum" / "lab.template"

    assert release.display_path(inside, root) == "courses/local/lab.template"
    assert release.display_path(outside, root) == str(outside)


def test_a_course_without_a_startercode_block_cannot_be_released(
    tmp_path: Path,
) -> None:
    base = tmp_path / ".env"
    base.write_text("", encoding="utf-8")
    directory = tmp_path / "courses" / "plain"
    directory.mkdir(parents=True)
    (directory / "lab.template").write_text("Resources: {}\n", encoding="utf-8")
    (directory / "expected.json").write_text("{}\n", encoding="utf-8")
    (directory / "course.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "course_key": "plain",
                "assets": {
                    "template": "lab.template",
                    "manifest": "expected.json",
                    "notebook": "",
                },
                "vocareum": {},
                "cleanup": {},
            }
        ),
        encoding="utf-8",
    )
    config = upload.Config(
        api_base="https://api.example.test",
        api_key="k",
        course="plain",
        env_path=base,
        course_id="1",
        assignment_id="2",
        part_id="3",
    )

    with pytest.raises(ConfigError, match="no startercode block"):
        release.starter_code(config)
