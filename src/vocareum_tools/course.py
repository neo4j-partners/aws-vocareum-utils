"""Course asset paths and committed course-definition validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import (
    COURSE_ASSET_DIR_VAR,
    COURSE_ENV_SUFFIX,
    COURSE_NAME_PATTERN,
    COURSES_DIR_NAME,
    ConfigError,
    course_asset_dir,
)

DEFINITION_NAME = "course.json"
MANIFEST_NAME = "expected.json"
TEMPLATE_NAME = "lab.template"
SCHEMA_VERSION = 1
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class CleanupRules:
    """Additional resource-name prefixes owned by one course."""

    resource_prefixes: tuple[str, ...] = ()
    iam_role_prefixes: tuple[str, ...] = ()
    s3_bucket_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StarterCode:
    """Where one course's student package is read from, and which files.

    Held as course data rather than as module constants because each course
    reads a different repository at a different commit. Reading a pinned commit
    rather than the working tree is what keeps a local edit out of a student
    package.
    """

    repository: str
    commit: str
    source: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class CourseDefinition:
    """Committed, non-secret facts that bind a course to its local assets."""

    key: str
    notebook: str
    vocareum: dict[str, Any]
    cleanup: CleanupRules
    startercode: StarterCode | None = None


def course_directory(base_env: Path, course: str) -> Path:
    """Return the asset directory for ``course``, here or in another repository.

    A course whose content lives in its own repository keeps its Vocareum
    assets there too, named by ``COURSE_ASSET_DIR``. A missing external
    directory is an error rather than a fall back to ``courses/<name>/``:
    falling back would upload one course's template under another course's
    name and report success.
    """
    if not COURSE_NAME_PATTERN.fullmatch(course):
        raise ConfigError(f"Course name {course!r} is not usable.")
    external = course_asset_dir(base_env, course)
    if external is None:
        return base_env.parent / COURSES_DIR_NAME / course
    if not external.is_dir():
        raise ConfigError(
            f"Course {course!r} sets {COURSE_ASSET_DIR_VAR} to a directory that "
            f"does not exist: {external}. Check out the course repository, or "
            f"remove the value to read {COURSES_DIR_NAME}/{course}/ here."
        )
    assert_directory_owns_course(external, course)
    return external


def assert_directory_owns_course(directory: Path, course: str) -> None:
    """Refuse an external directory whose definition names a different course.

    ``COURSE_ASSET_DIR`` is honored from the real environment as well as from a
    course file, and a real environment variable applies to every course rather
    than to one. Exported to override one course, it silently redirected the
    next course's lookups too, and ``upload`` and ``expect`` resolve a path
    without ever reading ``course.json``, so nothing caught it. That uploads one
    course's template to another course's part and reports success.

    Checked here rather than in each caller because this is the one function
    every asset path goes through. The key is read directly instead of through
    :func:`load_definition`, which calls :func:`definition_path`, which calls
    this.

    An absent or unreadable definition is left alone. The caller's own error
    names the file and the reason, and preempting it here would replace a
    precise message with a vaguer one.
    """
    path = directory / DEFINITION_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    key = str(payload.get("course_key", "")).strip()
    if key and key != course:
        raise ConfigError(
            f"{path} declares course_key={key!r}, but course {course!r} was "
            f"selected. Unset {COURSE_ASSET_DIR_VAR} in the environment, or "
            f"set it in {COURSES_DIR_NAME}/{course}{COURSE_ENV_SUFFIX} instead, "
            "so one course's assets cannot be published under another "
            "course's name."
        )


def course_content_root(base_env: Path, course: str) -> Path:
    """Return the repository root that owns ``course``'s asset directory.

    ``assets.notebook`` is written relative to this rather than to the asset
    directory, because the notebook is course content and sits beside the other
    content. An external course keeps its own notebook at the root of its own
    repository, one level above the ``vocareum/`` directory the pointer names.
    """
    directory = course_directory(base_env, course)
    if course_asset_dir(base_env, course) is None:
        return base_env.parent
    return directory.parent


def definition_path(base_env: Path, course: str) -> Path:
    """Return the course-definition path."""
    return course_directory(base_env, course) / DEFINITION_NAME


def manifest_path(base_env: Path, course: str) -> Path:
    """Return the course readiness-manifest path."""
    return course_directory(base_env, course) / MANIFEST_NAME


def template_path(base_env: Path, course: str) -> Path:
    """Return the course CloudFormation-template path."""
    return course_directory(base_env, course) / TEMPLATE_NAME


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field} must be a JSON array of strings.")
    values = tuple(item.strip() for item in value)
    if any(not item for item in values):
        raise ConfigError(f"{field} cannot contain an empty prefix.")
    return values


def _relative_path(value: Any, field: str) -> str:
    if value in {None, ""}:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be a repository-relative path.")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise ConfigError(f"{field} must be a safe repository-relative path.")
    return value


def _checkout_path(value: Any, field: str) -> str:
    """Validate a path naming a Git checkout, relative to the asset directory.

    ``..`` is allowed here and rejected everywhere else. The asset directory is
    already outside this repository for an external course, so walking up out of
    it is the only way to name the repository that owns it.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty relative path.")
    candidate = PurePosixPath(value.strip())
    if candidate.is_absolute():
        raise ConfigError(f"{field} must be relative to the course directory.")
    return value.strip()


def _parse_startercode(value: Any) -> StarterCode | None:
    """Reduce one optional starter-code block, or report that there is none."""
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise ConfigError("Course definition startercode must be a JSON object.")
    repository = value.get("repository")
    if not isinstance(repository, str) or not repository.strip():
        raise ConfigError("startercode.repository must be a Git remote URL.")
    commit = value.get("commit")
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit.strip()):
        raise ConfigError("startercode.commit must be a full or short Git SHA.")
    paths = _strings(value.get("paths", []), "startercode.paths")
    if not paths:
        raise ConfigError("startercode.paths must name at least one path.")
    for path in paths:
        _relative_path(path, "startercode.paths")
    return StarterCode(
        repository=repository.strip(),
        commit=commit.strip(),
        source=_checkout_path(value.get("source", ".."), "startercode.source"),
        paths=paths,
    )


def parse_definition(payload: Any, expected_key: str) -> CourseDefinition:
    """Validate and reduce one untrusted course-definition payload."""
    if not isinstance(payload, dict):
        raise ConfigError("Course definition must be a JSON object.")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"Course definition schema_version must be {SCHEMA_VERSION}.")
    key = str(payload.get("course_key", "")).strip()
    if key != expected_key:
        raise ConfigError(
            f"Course definition says course_key={key!r}, expected {expected_key!r}."
        )
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        raise ConfigError("Course definition assets must be a JSON object.")
    if (
        assets.get("template") != TEMPLATE_NAME
        or assets.get("manifest") != MANIFEST_NAME
    ):
        raise ConfigError(
            f"Course assets must be {TEMPLATE_NAME!r} and {MANIFEST_NAME!r}."
        )
    vocareum = payload.get("vocareum", {})
    if not isinstance(vocareum, dict):
        raise ConfigError("Course definition vocareum must be a JSON object.")
    cleanup = payload.get("cleanup", {})
    if not isinstance(cleanup, dict):
        raise ConfigError("Course definition cleanup must be a JSON object.")
    return CourseDefinition(
        key=key,
        notebook=_relative_path(assets.get("notebook", ""), "assets.notebook"),
        vocareum=dict(vocareum),
        startercode=_parse_startercode(payload.get("startercode")),
        cleanup=CleanupRules(
            resource_prefixes=_strings(
                cleanup.get("resource_prefixes", []), "cleanup.resource_prefixes"
            ),
            iam_role_prefixes=_strings(
                cleanup.get("iam_role_prefixes", []), "cleanup.iam_role_prefixes"
            ),
            s3_bucket_prefixes=_strings(
                cleanup.get("s3_bucket_prefixes", []), "cleanup.s3_bucket_prefixes"
            ),
        ),
    )


def load_definition(base_env: Path, course: str) -> CourseDefinition:
    """Read one committed course definition, refusing missing or invalid data."""
    path = definition_path(base_env, course)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"Course {course!r} has no definition at {path}.") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(
            f"Could not read course definition {path}: {error}"
        ) from error
    definition = parse_definition(payload, course)
    directory = course_directory(base_env, course)
    for name in (TEMPLATE_NAME, MANIFEST_NAME):
        asset = directory / name
        if not asset.is_file():
            raise ConfigError(f"Course {course!r} is missing required asset {asset}.")
    content_root = course_content_root(base_env, course)
    if definition.notebook and not (content_root / definition.notebook).is_file():
        raise ConfigError(
            f"Course {course!r} names missing notebook "
            f"{content_root / definition.notebook}."
        )
    return definition
