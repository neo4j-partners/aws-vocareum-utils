from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from vocareum_tools import course_setup
from vocareum_tools.client import ApiError
from vocareum_tools.config import ConfigError, Environment, repository_root
from vocareum_tools.course import course_directory
from vocareum_tools.endall import Student


def student(user_id: str = "u1", email: str = "student@example.com") -> Student:
    return Student(user_id, "Student", email, "student", False)


def setup() -> course_setup.RemoteSetup:
    return course_setup.RemoteSetup(
        course={"id": "c1", "name": "Demo"},
        assignment=course_setup.Item(
            "a1", "Workshop", {"id": "a1", "name": "Workshop", "publish": True}
        ),
        part=course_setup.Item("p1", "Lab", {"id": "p1", "name": "Lab"}),
        part_detail={
            "id": "p1",
            "name": "Lab",
            "labtype": "Notebook",
            "session_length": 240,
            "total_dollar": 200,
        },
        roster=(student(),),
    )


def args(**overrides: Any) -> argparse.Namespace:
    values = {
        "key": "demo",
        "course_id": "c1",
        "assignment_id": "a1",
        "assignment_name": "",
        "part_id": "p1",
        "part_name": "",
        "user_email": "",
        "copy_session_from_base": False,
        "copy_assets_from": "",
        "notebook": "",
        "cleanup_prefix": [],
        "cleanup_iam_role_prefix": [],
        "cleanup_s3_prefix": [],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_choose_item_refuses_to_guess_between_assignments() -> None:
    rows = [{"id": "1", "name": "One"}, {"id": "2", "name": "Two"}]

    with pytest.raises(ConfigError, match="matched 2"):
        course_setup.choose_item(rows, "assignment")


def test_choose_item_refuses_duplicate_names() -> None:
    rows = [{"id": "1", "name": "Lab"}, {"id": "2", "name": "Lab"}]

    with pytest.raises(ConfigError, match="matched 2"):
        course_setup.choose_item(rows, "part", name="Lab")


def test_fetch_collection_reads_zero_indexed_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def request(method: str, url: str, api_key: str, body: Any = None) -> Any:
        calls.append(url)
        page = 1 if "page=1" in url else 0
        rows = [{"id": str(page + 1), "name": f"Item {page + 1}"}]
        return {"assignments": rows, "total_records": 2}

    monkeypatch.setattr(course_setup, "request_json", request)
    rows = course_setup.fetch_collection(
        lambda page: (
            "https://api/items" if page is None else f"https://api/items?page={page}"
        ),
        "assignments",
        "token",
    )

    assert [row["id"] for row in rows] == ["1", "2"]
    assert calls == ["https://api/items", "https://api/items?page=1"]


def test_fetch_collection_refuses_a_short_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        course_setup,
        "request_json",
        lambda *unused, **kwargs: {"parts": [], "total_records": 2},
    )

    with pytest.raises(ApiError, match="incomplete"):
        course_setup.fetch_collection(lambda page: "u", "parts", "token")


def test_remote_contract_includes_part_configuration_and_budget() -> None:
    contract = course_setup.remote_contract(setup())

    assert contract == {
        "course_name": "Demo",
        "assignment_name": "Workshop",
        "part_name": "Lab",
        "lab_type": "Notebook",
        "session_length": 240,
        "budget": {"total_dollar": 200},
        "assignment_published": True,
    }


def test_contract_mismatches_reports_nested_budget_values() -> None:
    mismatches = course_setup.contract_mismatches(
        {"lab_type": "Notebook", "budget": {"total_dollar": 100}},
        {"lab_type": "Notebook", "budget": {"total_dollar": 200}},
    )

    assert mismatches == ["budget.total_dollar: saved=100, remote=200"]


def test_scaffold_writes_complete_layout_without_shared_secrets(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text("VOCAREUM_API_KEY=secret\n", encoding="utf-8")
    parsed = args()

    course_setup.write_scaffold(base, parsed, Environment.from_file(base), setup())

    directory = tmp_path / "courses" / "demo"
    assert (directory / "lab.template").is_file()
    assert json.loads((directory / "expected.json").read_text())["stacks"]
    definition = json.loads((directory / "course.json").read_text())
    assert definition["course_key"] == "demo"
    env = (tmp_path / "courses" / "demo.env").read_text()
    assert "VOCAREUM_USER_ID=u1" in env
    assert "VOCAREUM_API_KEY" not in env
    assert "secret" not in env


def test_scaffold_refuses_any_overwrite(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text("", encoding="utf-8")
    existing = tmp_path / "courses" / "demo"
    existing.mkdir(parents=True)
    (existing / "lab.template").write_text("mine", encoding="utf-8")

    with pytest.raises(ConfigError, match="Refusing to overwrite"):
        course_setup.write_scaffold(base, args(), Environment.from_file(base), setup())


@pytest.mark.parametrize(
    "removed_flag",
    ["--clone-course-from", "--publish-assignment", "--enroll"],
)
def test_setup_rejects_deferred_shared_state_operations(removed_flag: str) -> None:
    with pytest.raises(SystemExit):
        course_setup.parse_args(["demo", removed_flag, "value"])


def test_render_env_refuses_to_guess_between_students(tmp_path: Path) -> None:
    remote = setup()
    remote = course_setup.RemoteSetup(
        remote.course,
        remote.assignment,
        remote.part,
        remote.part_detail,
        (student("u1", "one@example.com"), student("u2", "two@example.com")),
    )
    base = tmp_path / ".env"
    base.write_text("", encoding="utf-8")

    rendered = course_setup.render_env(Environment.from_file(base), remote, args())

    assert "VOCAREUM_USER_ID=\n" in rendered
    assert "VOCAREUM_USER_EMAIL=\n" in rendered


def test_scaffold_can_reproduce_the_graphrag_asset_layout() -> None:
    root = repository_root()
    source = course_directory(root / ".env", "graphrag")
    if not source.is_dir():
        pytest.skip(f"graphrag course not checked out: no directory at {source}")

    template, manifest, definition = course_setup.scaffold_payloads(
        root / ".env", "copy", setup(), "graphrag"
    )

    assert template == (source / "lab.template").read_bytes()
    assert manifest == (source / "expected.json").read_bytes()
    assert definition["assets"]["template"] == "lab.template"
    assert definition["assets"]["manifest"] == "expected.json"


def test_explicit_course_id_discovers_assignment_and_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / ".env"
    base.write_text(
        "VOCAREUM_API_KEY=key\n"
        "VOCAREUM_COURSE_ID=c1\n"
        "VOCAREUM_ASSIGNMENT_ID=stale-a\n"
        "VOCAREUM_PART_ID=stale-p\n",
        encoding="utf-8",
    )
    selected: list[course_setup.ApiConfig] = []

    def inspect(
        config: course_setup.ApiConfig, _: argparse.Namespace
    ) -> course_setup.RemoteSetup:
        selected.append(config)
        return setup()

    monkeypatch.setattr(course_setup, "inspect_remote", inspect)

    status = course_setup.main(["demo", "--env-file", str(base), "--course-id", "c1"])

    assert status == course_setup.EXIT_OK
    assert (selected[0].assignment_id, selected[0].part_id) == ("", "")
    output = capsys.readouterr().out
    assert "VOCAREUM_ASSIGNMENT_ID=a1\n" in output
    assert "VOCAREUM_PART_ID=p1\n" in output
    assert "VOCAREUM_USER_ID=u1\n" in output


def test_refresh_replaces_only_the_vocareum_block(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text("VOCAREUM_API_KEY=secret\n", encoding="utf-8")
    course_setup.write_scaffold(base, args(), Environment.from_file(base), setup())
    path = tmp_path / "courses" / "demo" / "course.json"
    saved = json.loads(path.read_text())
    saved["startercode"] = {
        "repository": "https://github.com/example/workshop.git",
        "commit": "190b735de743c6bbb5484d10ca8ab4fe04b94ba2",
        "paths": ["README.md"],
    }
    saved["vocareum"]["lab_type"] = "Old"
    path.write_text(json.dumps(saved), encoding="utf-8")

    course_setup.refresh_definition(base, args(), setup())

    refreshed = json.loads(path.read_text())
    assert refreshed["vocareum"] == course_setup.remote_contract(setup())
    assert refreshed["startercode"] == saved["startercode"]
    assert {key: refreshed[key] for key in saved if key != "vocareum"} == {
        key: saved[key] for key in saved if key != "vocareum"
    }
