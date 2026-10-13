from __future__ import annotations

import json
from pathlib import Path

import pytest

from vocareum_tools.config import ConfigError
from vocareum_tools.course import (
    CourseDefinition,
    StarterCode,
    course_directory,
    definition_path,
    load_definition,
    manifest_path,
    parse_definition,
    template_path,
)


def payload(key: str = "demo", notebook: str = "notebooks/lab.ipynb") -> dict:
    return {
        "schema_version": 1,
        "course_key": key,
        "assets": {
            "template": "lab.template",
            "manifest": "expected.json",
            "notebook": notebook,
        },
        "vocareum": {"lab_type": "Notebook", "session_length": 240},
        "cleanup": {
            "resource_prefixes": ["demo-"],
            "iam_role_prefixes": [],
            "s3_bucket_prefixes": [],
        },
    }


def test_parse_definition_reduces_the_valid_schema() -> None:
    definition = parse_definition(payload(), "demo")

    assert isinstance(definition, CourseDefinition)
    assert definition.notebook == "notebooks/lab.ipynb"
    assert definition.cleanup.resource_prefixes == ("demo-",)


@pytest.mark.parametrize("notebook", ["/tmp/lab.ipynb", "../lab.ipynb"])
def test_parse_definition_refuses_unsafe_notebook_paths(notebook: str) -> None:
    with pytest.raises(ConfigError):
        parse_definition(payload(notebook=notebook), "demo")


def test_parse_definition_refuses_a_cross_course_definition() -> None:
    with pytest.raises(ConfigError, match="expected 'other'"):
        parse_definition(payload(), "other")


def test_load_definition_requires_every_named_asset(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    directory = course_directory(base, "demo")
    directory.mkdir(parents=True)
    (directory / "course.json").write_text(json.dumps(payload()), encoding="utf-8")

    with pytest.raises(ConfigError, match="missing required asset"):
        load_definition(base, "demo")


def test_load_definition_checks_the_notebook_exists(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    directory = course_directory(base, "demo")
    directory.mkdir(parents=True)
    (directory / "course.json").write_text(json.dumps(payload()), encoding="utf-8")
    (directory / "lab.template").write_text("Resources: {}\n", encoding="utf-8")
    (directory / "expected.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="missing notebook"):
        load_definition(base, "demo")


def write_course(directory: Path, key: str = "demo", notebook: str = "") -> None:
    """Write one complete asset directory for ``key``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "course.json").write_text(
        json.dumps(payload(key=key, notebook=notebook)), encoding="utf-8"
    )
    (directory / "lab.template").write_text("Resources: {}\n", encoding="utf-8")
    (directory / "expected.json").write_text("{}\n", encoding="utf-8")


def test_a_course_without_the_pointer_reads_the_local_directory(
    tmp_path: Path,
) -> None:
    base = tmp_path / ".env"
    base.write_text("", encoding="utf-8")

    assert course_directory(base, "demo") == tmp_path / "courses" / "demo"


def test_the_pointer_moves_every_asset_lookup_to_the_named_directory(
    tmp_path: Path,
) -> None:
    external = tmp_path / "workshop" / "vocareum"
    write_course(external)
    base = tmp_path / ".env"
    base.write_text(f"COURSE_ASSET_DIR={external}\n", encoding="utf-8")

    assert course_directory(base, "demo") == external
    assert definition_path(base, "demo") == external / "course.json"
    assert manifest_path(base, "demo") == external / "expected.json"
    assert template_path(base, "demo") == external / "lab.template"
    assert load_definition(base, "demo").key == "demo"


def test_a_relative_pointer_resolves_against_this_repository(tmp_path: Path) -> None:
    external = tmp_path / "workshop" / "vocareum"
    write_course(external)
    base = tmp_path / ".env"
    base.write_text("COURSE_ASSET_DIR=workshop/vocareum\n", encoding="utf-8")

    assert course_directory(base, "demo") == external


def test_a_pointer_naming_a_missing_directory_refuses_without_falling_back(
    tmp_path: Path,
) -> None:
    """The refusal is the point.

    A fall back to ``courses/demo/`` would upload one course's template under
    another course's name and report success.
    """
    write_course(tmp_path / "courses" / "demo")
    base = tmp_path / ".env"
    base.write_text("COURSE_ASSET_DIR=gone/vocareum\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="COURSE_ASSET_DIR"):
        course_directory(base, "demo")


def test_a_pointer_to_another_course_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exported pointer applies to every course, so ownership is checked.

    ``upload`` and ``expect`` resolve an asset path without ever reading
    ``course.json``, so the definition's own key check never runs for them. A
    pointer exported to override one course silently redirected the next
    course's lookups too, which publishes one course's template under another
    course's name and reports success.
    """
    external = tmp_path / "workshop" / "vocareum"
    write_course(external, key="graphrag")
    base = tmp_path / ".env"
    base.write_text("", encoding="utf-8")
    monkeypatch.setenv("COURSE_ASSET_DIR", str(external))

    assert course_directory(base, "graphrag") == external
    with pytest.raises(ConfigError, match="declares course_key='graphrag'"):
        course_directory(base, "demo")


def test_an_asset_directory_missing_its_definition_defers_the_error(
    tmp_path: Path,
) -> None:
    """The ownership check stays quiet so the caller's own message survives.

    ``load_definition`` names the file and the reason. Raising here instead
    would replace a precise message with a vaguer one.
    """
    external = tmp_path / "workshop" / "vocareum"
    external.mkdir(parents=True)
    base = tmp_path / ".env"
    base.write_text(f"COURSE_ASSET_DIR={external}\n", encoding="utf-8")

    assert course_directory(base, "demo") == external
    with pytest.raises(ConfigError, match="has no definition at"):
        load_definition(base, "demo")


def test_the_course_file_pointer_beats_the_base_file(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen"
    write_course(chosen)
    base = tmp_path / ".env"
    base.write_text("COURSE_ASSET_DIR=ignored\n", encoding="utf-8")
    courses = tmp_path / "courses"
    courses.mkdir(exist_ok=True)
    (courses / "demo.env").write_text(f"COURSE_ASSET_DIR={chosen}\n", encoding="utf-8")

    assert course_directory(base, "demo") == chosen


def test_an_external_notebook_resolves_against_its_own_repository(
    tmp_path: Path,
) -> None:
    external = tmp_path / "workshop" / "vocareum"
    write_course(external, notebook="notebooks/lab.ipynb")
    notebook = tmp_path / "workshop" / "notebooks" / "lab.ipynb"
    notebook.parent.mkdir(parents=True)
    notebook.write_text("{}\n", encoding="utf-8")
    base = tmp_path / ".env"
    base.write_text(f"COURSE_ASSET_DIR={external}\n", encoding="utf-8")

    assert load_definition(base, "demo").notebook == "notebooks/lab.ipynb"


def test_a_startercode_block_parses_into_pinned_source_facts() -> None:
    body = payload()
    body["startercode"] = {
        "repository": "https://github.com/owner/name.git",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "source": "..",
        "paths": ["README.md", "notebooks"],
    }

    starter = parse_definition(body, "demo").startercode

    assert starter == StarterCode(
        repository="https://github.com/owner/name.git",
        commit="0123456789abcdef0123456789abcdef01234567",
        source="..",
        paths=("README.md", "notebooks"),
    )


def test_a_course_with_no_startercode_block_reports_none() -> None:
    assert parse_definition(payload(), "demo").startercode is None


@pytest.mark.parametrize(
    "block",
    [
        {"commit": "0123456", "paths": ["README.md"]},
        {"repository": "https://x/y.git", "paths": ["README.md"]},
        {"repository": "https://x/y.git", "commit": "not-a-sha", "paths": ["a"]},
        {"repository": "https://x/y.git", "commit": "0123456", "paths": []},
        {
            "repository": "https://x/y.git",
            "commit": "0123456",
            "source": "/etc",
            "paths": ["a"],
        },
    ],
)
def test_an_incomplete_startercode_block_is_refused(block: dict) -> None:
    body = payload()
    body["startercode"] = block

    with pytest.raises(ConfigError, match="startercode"):
        parse_definition(body, "demo")
