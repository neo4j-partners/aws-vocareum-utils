"""Byte-preservation and ownership checks for the named-course migration."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vocareum_tools.config import repository_root
from vocareum_tools.course import course_directory, load_definition

FIRST_COURSE = "neo4j-fundamentals"
TEMPLATE_SHA256 = "65e48ca69fc3d98a33aeb7c65e322ad9de127878000ce1f0f375416a8f1a3596"
MANIFEST_SHA256 = "894e154ad2d91a71719fe398f6b5ef8371617ca7185bb49da9b3cd2dcf5231b7"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_first_course_assets_are_the_exact_pre_migration_bytes() -> None:
    root = repository_root()
    directory = root / "courses" / FIRST_COURSE

    assert digest(directory / "lab.template") == TEMPLATE_SHA256
    assert digest(directory / "expected.json") == MANIFEST_SHA256
    assert not (root / "lab.template").exists()
    assert not (root / "expected.json").exists()


def test_first_course_definition_binds_its_cleanup_rules() -> None:
    root = repository_root()
    definition = load_definition(root / ".env", FIRST_COURSE)

    assert "verify" in definition.cleanup.resource_prefixes
    assert "workshop-" in definition.cleanup.iam_role_prefixes
    assert "bedrock-agentcore-codebuild-sources" in (
        definition.cleanup.s3_bucket_prefixes
    )


@pytest.mark.parametrize("course", [FIRST_COURSE, "graphrag"])
def test_every_named_course_has_one_complete_asset_directory(course: str) -> None:
    """Each released course owns all three assets, wherever its directory is.

    Read through ``course_directory`` rather than built from ``courses/<name>``,
    because the GraphRAG course keeps its assets in its own repository and this
    check must follow it there rather than declare it missing. A fresh clone
    has no pointer to that repository, so GraphRAG alone skips there.
    """
    directory = course_directory(repository_root() / ".env", course)
    if course != FIRST_COURSE and not directory.is_dir():
        pytest.skip(f"{course} course not checked out: no directory at {directory}")
    assert {path.name for path in directory.iterdir()} >= {
        "course.json",
        "expected.json",
        "lab.template",
    }
