"""Inspect and scaffold Vocareum courses without starting student sessions.

The default run is read-only. Explicit local modes create or update repository
files only. This module cannot clone courses, publish assignments, enroll
students, or call a student ``/resources/`` endpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import ApiError, request_json
from .config import (
    COURSE_NAME_PATTERN,
    ConfigError,
    Environment,
    course_env_path,
    repository_root,
)
from .course import (
    DEFINITION_NAME,
    MANIFEST_NAME,
    SCHEMA_VERSION,
    TEMPLATE_NAME,
    course_directory,
    load_definition,
)
from .endall import Config as RosterConfig
from .endall import Student, fetch_roster
from .endpoints import DEFAULT_API_BASE, PartEndpoints
from .output import section

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_API = 2
EXIT_MISMATCH = 3

MAX_PAGES = 100
PAGE_SIZE = 10
BUDGET_KEYS = ("total_dollar", "monthly_dollar", "total_time", "monthly_time")
COURSE_ENV_KEYS = (
    "VOCAREUM_COURSE_ID",
    "VOCAREUM_ASSIGNMENT_ID",
    "VOCAREUM_PART_ID",
    "VOCAREUM_USER_ID",
    "VOCAREUM_USER_EMAIL",
    "LAB_AWS_ACCOUNT_ID",
    "LAB_AWS_ACCOUNT_POOL_A",
    "LAB_AWS_ACCOUNT_POOL_B",
    "LAB_AWS_ACCOUNT_POOL_C",
    "LAB_STACK_NAME",
    "LAB_DEPLOYER_ROLE",
)

DEFAULT_TEMPLATE = b"""AWSTemplateFormatVersion: '2010-09-09'
Description: No-cost launch sentinel for a Vocareum Notebook course.
Resources:
  LaunchSentinel:
    Type: AWS::CloudFormation::WaitConditionHandle
"""
DEFAULT_MANIFEST = {"stacks": ["${LAB_STACK_NAME}"]}


@dataclass(frozen=True)
class Item:
    """One assignment or part selected from a complete API collection."""

    item_id: str
    name: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RemoteSetup:
    """The remote course state needed for validation and scaffolding."""

    course: dict[str, Any]
    assignment: Item
    part: Item
    part_detail: dict[str, Any]
    roster: tuple[Student, ...]


@dataclass(frozen=True)
class ApiConfig(PartEndpoints):
    """Selected remote coordinates and authorization."""

    api_key: str


def _rows(payload: Any, key: str) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise ApiError(f"Expected a {key!r} array in the Vocareum response.")
    rows = [row for row in payload[key] if isinstance(row, dict)]
    raw_total = payload.get("total_records")
    if raw_total is None:
        return rows, None
    try:
        return rows, int(raw_total)
    except (TypeError, ValueError) as error:
        raise ApiError(
            f"Vocareum returned an invalid total_records: {raw_total!r}."
        ) from error


def fetch_collection(url_for_page: Any, key: str, api_key: str) -> list[dict[str, Any]]:
    """Read a complete zero-indexed Vocareum collection without guessing."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_total: int | None = None
    for page in range(MAX_PAGES):
        payload = request_json(
            "GET", url_for_page(None if page == 0 else page), api_key
        )
        rows, total = _rows(payload, key)
        if total is not None:
            expected_total = total
        if not rows:
            break
        for row in rows:
            item_id = str(row.get("id", "")).strip()
            if not item_id:
                raise ApiError(f"Vocareum returned a {key} row with no id.")
            if item_id not in seen:
                seen.add(item_id)
                found.append(row)
        if expected_total is not None and len(found) >= expected_total:
            break
        if expected_total is None and len(rows) < PAGE_SIZE:
            break
    else:
        raise ApiError(f"The {key} collection exceeded {MAX_PAGES} pages.")
    if expected_total is not None and len(found) != expected_total:
        raise ApiError(
            f"The {key} collection is incomplete: read {len(found)}, "
            f"total_records says {expected_total}."
        )
    return found


def detail_object(payload: Any, key: str, item_id: str) -> dict[str, Any]:
    """Unwrap GET-detail responses accepted by the live Vocareum API."""
    if not isinstance(payload, dict):
        raise ApiError(f"Vocareum returned a non-object {key} response.")
    nested = payload.get(key)
    if isinstance(nested, dict):
        return nested
    if isinstance(nested, list):
        matches = [
            item
            for item in nested
            if isinstance(item, dict) and str(item.get("id", "")) == item_id
        ]
        if len(matches) == 1:
            return matches[0]
        raise ApiError(
            f"Vocareum returned {len(matches)} matching {key} detail rows for "
            f"id {item_id}."
        )
    return payload


def choose_item(
    rows: list[dict[str, Any]],
    kind: str,
    item_id: str = "",
    name: str = "",
) -> Item:
    """Select exactly one item, refusing absent and ambiguous choices."""
    items = [
        Item(str(row.get("id", "")).strip(), str(row.get("name", "")).strip(), row)
        for row in rows
    ]
    if item_id:
        candidates = [item for item in items if item.item_id == item_id]
    elif name:
        candidates = [item for item in items if item.name == name]
    else:
        candidates = items
    if len(candidates) == 1:
        return candidates[0]
    selector = (
        f"id={item_id!r}" if item_id else f"name={name!r}" if name else "no selector"
    )
    available = ", ".join(f"{item.item_id}:{item.name}" for item in items) or "none"
    raise ConfigError(
        f"Could not select exactly one {kind} with {selector}; matched "
        f"{len(candidates)}. Available {kind}s: {available}."
    )


def _budget(part: dict[str, Any]) -> dict[str, Any]:
    nested = part.get("budgets")
    sources = [part, nested] if isinstance(nested, dict) else [part]
    return {
        key: source[key] for key in BUDGET_KEYS for source in sources if key in source
    }


def remote_contract(setup: RemoteSetup) -> dict[str, Any]:
    """Return stable, non-secret remote properties saved in course.json."""
    part = setup.part_detail
    contract: dict[str, Any] = {
        "course_name": str(setup.course.get("name", "")),
        "assignment_name": setup.assignment.name,
        "part_name": setup.part.name,
    }
    for destination, source in (
        ("lab_type", "labtype"),
        ("session_length", "session_length"),
        ("cloud_labs", "cloud_labs"),
    ):
        if source in part:
            contract[destination] = part[source]
    budget = _budget(part)
    if budget:
        contract["budget"] = budget
    if "publish" in setup.assignment.payload:
        contract["assignment_published"] = setup.assignment.payload["publish"]
    return contract


def contract_mismatches(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Compare only explicitly saved fields, including nested budget values."""
    mismatches: list[str] = []
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if isinstance(expected_value, dict) and isinstance(actual_value, dict):
            for child, value in expected_value.items():
                if actual_value.get(child) != value:
                    remote = actual_value.get(child)
                    mismatches.append(
                        f"{key}.{child}: saved={value!r}, remote={remote!r}"
                    )
        elif actual_value != expected_value:
            mismatches.append(
                f"{key}: saved={expected_value!r}, remote={actual_value!r}"
            )
    return mismatches


def inspect_remote(config: ApiConfig, args: argparse.Namespace) -> RemoteSetup:
    """Read course, assignment, part, and roster state without mutation."""
    course_payload = request_json("GET", config.course_url(), config.api_key)
    course = detail_object(course_payload, "courses", config.course_id)
    assignments = fetch_collection(
        config.assignments_url, "assignments", config.api_key
    )
    assignment = choose_item(
        assignments, "assignment", config.assignment_id, args.assignment_name
    )
    selected = ApiConfig(
        api_base=config.api_base,
        course_id=config.course_id,
        assignment_id=assignment.item_id,
        part_id=config.part_id,
        api_key=config.api_key,
    )
    parts = fetch_collection(selected.parts_url, "parts", config.api_key)
    part = choose_item(parts, "part", config.part_id, args.part_name)
    selected = ApiConfig(
        api_base=config.api_base,
        course_id=config.course_id,
        assignment_id=assignment.item_id,
        part_id=part.item_id,
        api_key=config.api_key,
    )
    part_payload = request_json("GET", selected.part_url(), config.api_key)
    part_detail = detail_object(part_payload, "parts", part.item_id)
    roster_config = RosterConfig(
        api_base=config.api_base,
        course_id=config.course_id,
        assignment_id=assignment.item_id,
        part_id=part.item_id,
        api_key=config.api_key,
    )
    roster = tuple(fetch_roster(roster_config))
    return RemoteSetup(course, assignment, part, part_detail, roster)


def _select_student(
    roster: tuple[Student, ...], requested_email: str, saved_user_id: str
) -> Student | None:
    active = [
        student
        for student in roster
        if not student.dropped and student.role == "student"
    ]
    if requested_email:
        matches = [
            student
            for student in active
            if student.email.casefold() == requested_email.casefold()
        ]
        if len(matches) != 1:
            raise ConfigError(
                f"--user-email matched {len(matches)} active students; "
                "refusing to guess."
            )
        return matches[0]
    if saved_user_id:
        matches = [student for student in active if student.user_id == saved_user_id]
        if len(matches) == 1:
            return matches[0]
    return active[0] if len(active) == 1 else None


def definition_payload(
    key: str,
    setup: RemoteSetup,
    *,
    notebook: str = "",
    cleanup: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Build the committed definition for one inspected course."""
    return {
        "schema_version": SCHEMA_VERSION,
        "course_key": key,
        "assets": {
            "template": TEMPLATE_NAME,
            "manifest": MANIFEST_NAME,
            "notebook": notebook,
        },
        "vocareum": remote_contract(setup),
        "cleanup": cleanup
        or {
            "resource_prefixes": [],
            "iam_role_prefixes": [],
            "s3_bucket_prefixes": [],
        },
    }


def scaffold_payloads(
    base_env: Path,
    key: str,
    setup: RemoteSetup,
    source_course: str = "",
) -> tuple[bytes, bytes, dict[str, Any]]:
    """Return template, manifest, and definition bytes for a new course."""
    notebook = ""
    cleanup: dict[str, list[str]] | None = None
    if source_course:
        source = load_definition(base_env, source_course)
        directory = course_directory(base_env, source_course)
        template = (directory / TEMPLATE_NAME).read_bytes()
        manifest = (directory / MANIFEST_NAME).read_bytes()
        notebook = source.notebook
        cleanup = {
            "resource_prefixes": list(source.cleanup.resource_prefixes),
            "iam_role_prefixes": list(source.cleanup.iam_role_prefixes),
            "s3_bucket_prefixes": list(source.cleanup.s3_bucket_prefixes),
        }
    else:
        template = DEFAULT_TEMPLATE
        manifest = (json.dumps(DEFAULT_MANIFEST, indent=2) + "\n").encode()
    definition = definition_payload(key, setup, notebook=notebook, cleanup=cleanup)
    return template, manifest, definition


def _dotenv_value(value: str) -> str:
    if not value or all(
        character.isalnum() or character in "@._+-/" for character in value
    ):
        return value
    return json.dumps(value)


def course_coordinates(
    environment: Environment,
    setup: RemoteSetup,
    args: argparse.Namespace,
) -> dict[str, str]:
    """Return the course env values the inspected course determines."""
    student = _select_student(
        setup.roster, args.user_email, environment.get("VOCAREUM_USER_ID")
    )
    return {
        "VOCAREUM_COURSE_ID": args.course_id,
        "VOCAREUM_ASSIGNMENT_ID": setup.assignment.item_id,
        "VOCAREUM_PART_ID": setup.part.item_id,
        "VOCAREUM_USER_ID": student.user_id if student else "",
        "VOCAREUM_USER_EMAIL": student.email if student else "",
    }


def render_env(
    environment: Environment,
    setup: RemoteSetup,
    args: argparse.Namespace,
) -> str:
    """Render the ignored course env file without copying shared credentials."""
    values = {
        **course_coordinates(environment, setup, args),
        "LAB_AWS_ACCOUNT_ID": "",
        "LAB_STACK_NAME": "",
        "LAB_DEPLOYER_ROLE": "",
    }
    if args.copy_session_from_base:
        for key in COURSE_ENV_KEYS:
            saved = environment.get(key)
            if saved:
                values[key] = saved
        values.update(
            {
                "VOCAREUM_COURSE_ID": args.course_id,
                "VOCAREUM_ASSIGNMENT_ID": setup.assignment.item_id,
                "VOCAREUM_PART_ID": setup.part.item_id,
            }
        )
    lines = [
        f"# Local coordinates for course {args.key}. This file is git-ignored.",
        "# Generated by vocareum-course-setup; shared API credentials stay in .env.",
        "",
    ]
    lines.extend(f"{key}={_dotenv_value(value)}" for key, value in values.items())
    return "\n".join(lines) + "\n"


def write_scaffold(
    base_env: Path,
    args: argparse.Namespace,
    environment: Environment,
    setup: RemoteSetup,
) -> None:
    """Create a complete local course layout, refusing every overwrite."""
    directory = course_directory(base_env, args.key)
    env_path = course_env_path(base_env, args.key)
    paths = (
        directory / TEMPLATE_NAME,
        directory / MANIFEST_NAME,
        directory / DEFINITION_NAME,
        env_path,
    )
    existing = [path for path in paths if path.exists()]
    if existing:
        raise ConfigError(
            "Refusing to overwrite an existing course scaffold: "
            + ", ".join(str(path) for path in existing)
        )
    template, manifest, definition = scaffold_payloads(
        base_env, args.key, setup, args.copy_assets_from
    )
    directory.mkdir(parents=True, exist_ok=False)
    (directory / TEMPLATE_NAME).write_bytes(template)
    (directory / MANIFEST_NAME).write_bytes(manifest)
    (directory / DEFINITION_NAME).write_text(
        json.dumps(definition, indent=2) + "\n", encoding="utf-8"
    )
    env_path.write_text(render_env(environment, setup, args), encoding="utf-8")


def adopt_existing_assets(
    base_env: Path,
    args: argparse.Namespace,
    environment: Environment,
    setup: RemoteSetup,
) -> None:
    """Add a definition to existing assets without replacing either asset."""
    directory = course_directory(base_env, args.key)
    definition_file = directory / DEFINITION_NAME
    required = (directory / TEMPLATE_NAME, directory / MANIFEST_NAME)
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise ConfigError(
            "Cannot adopt an incomplete course layout; missing "
            + ", ".join(str(path) for path in missing)
        )
    if definition_file.exists():
        raise ConfigError(f"Refusing to overwrite existing {definition_file}.")
    definition = definition_payload(
        args.key,
        setup,
        notebook=args.notebook,
        cleanup={
            "resource_prefixes": args.cleanup_prefix,
            "iam_role_prefixes": args.cleanup_iam_role_prefix,
            "s3_bucket_prefixes": args.cleanup_s3_prefix,
        },
    )
    definition_file.write_text(
        json.dumps(definition, indent=2) + "\n", encoding="utf-8"
    )
    env_path = course_env_path(base_env, args.key)
    if not env_path.exists():
        env_path.write_text(render_env(environment, setup, args), encoding="utf-8")


def refresh_definition(
    base_env: Path, args: argparse.Namespace, setup: RemoteSetup
) -> None:
    """Refresh only saved remote facts, preserving every other key.

    The payload is the file as written, not one rebuilt from known keys. A
    rebuild silently dropped the ``startercode`` block, which left the course
    with no student package and broke every release until it was restored.
    """
    load_definition(base_env, args.key)
    path = course_directory(base_env, args.key) / DEFINITION_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["vocareum"] = remote_contract(setup)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the repeatable course-setup command."""
    parser = argparse.ArgumentParser(
        description=(
            "Inspect a Vocareum course and preview or write its local course layout. "
            "The default is read-only and never starts a student session."
        )
    )
    parser.add_argument("key", help="Stable local course key.")
    parser.add_argument("--env-file", type=Path, default=repository_root() / ".env")
    parser.add_argument("--course-id", default="")
    parser.add_argument("--assignment-id", default="")
    parser.add_argument("--assignment-name", default="")
    parser.add_argument("--part-id", default="")
    parser.add_argument("--part-name", default="")
    parser.add_argument("--user-email", default="")
    parser.add_argument("--write-local", action="store_true")
    parser.add_argument("--adopt-local", action="store_true")
    parser.add_argument("--refresh-local", action="store_true")
    parser.add_argument("--copy-assets-from", default="")
    parser.add_argument("--copy-session-from-base", action="store_true")
    parser.add_argument("--notebook", default="")
    parser.add_argument("--cleanup-prefix", action="append", default=[])
    parser.add_argument("--cleanup-iam-role-prefix", action="append", default=[])
    parser.add_argument("--cleanup-s3-prefix", action="append", default=[])
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Inspect, compare, and optionally update local course files."""
    if not COURSE_NAME_PATTERN.fullmatch(args.key):
        raise ConfigError(f"Course key {args.key!r} is not usable.")
    if args.assignment_id and args.assignment_name:
        raise ConfigError("Pass only one of --assignment-id and --assignment-name.")
    if args.part_id and args.part_name:
        raise ConfigError("Pass only one of --part-id and --part-name.")
    selected_env = course_env_path(args.env_file, args.key)
    environment = Environment.from_file(
        args.env_file, args.key if selected_env.is_file() else None
    )
    api_key = environment.get("VOCAREUM_API_KEY")
    if not api_key:
        raise ConfigError("VOCAREUM_API_KEY is required for course inspection.")
    api_base = (environment.get("VOCAREUM_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    # An explicit --course-id means discover the assignment and part rather
    # than reuse the saved ones. Retargeting a course starts by editing only
    # the course ID in its env file, so the saved assignment and part still
    # name the old course, and selecting by them refuses a correct course.
    discover = bool(args.course_id)
    course_id = args.course_id or environment.get("VOCAREUM_COURSE_ID")
    if not course_id:
        raise ConfigError("Pass --course-id or add it to the selected course env.")
    args.course_id = course_id
    config = ApiConfig(
        api_base,
        course_id,
        args.assignment_id
        or ("" if discover else environment.get("VOCAREUM_ASSIGNMENT_ID")),
        args.part_id or ("" if discover else environment.get("VOCAREUM_PART_ID")),
        api_key,
    )
    setup = inspect_remote(config, args)
    actual = remote_contract(setup)

    section("COURSE SETUP")
    print(f"local key          : {args.key}")
    print(f"course             : {course_id} {actual.get('course_name', '')}")
    print(f"assignment         : {setup.assignment.item_id} {setup.assignment.name}")
    print(f"part               : {setup.part.item_id} {setup.part.name}")
    print(f"lab type           : {actual.get('lab_type', '(not returned)')}")
    print(f"session length     : {actual.get('session_length', '(not returned)')}")
    print(
        f"budget             : {json.dumps(actual.get('budget', {}), sort_keys=True)}"
    )
    student_count = sum(student.role == "student" for student in setup.roster)
    print(f"roster students    : {student_count}")

    definition_file = course_directory(args.env_file, args.key) / DEFINITION_NAME
    mismatches: list[str] = []
    if definition_file.is_file():
        definition = load_definition(args.env_file, args.key)
        mismatches = contract_mismatches(definition.vocareum, actual)
        for mismatch in mismatches:
            print(f"MISMATCH           : {mismatch}")

    local_modes = sum((args.write_local, args.adopt_local, args.refresh_local))
    if local_modes > 1:
        raise ConfigError(
            "--write-local, --adopt-local, and --refresh-local are mutually exclusive."
        )
    if args.write_local:
        write_scaffold(args.env_file, args, environment, setup)
        print(f"local scaffold     : wrote {course_directory(args.env_file, args.key)}")
    elif args.adopt_local:
        adopt_existing_assets(args.env_file, args, environment, setup)
        print(f"local definition   : wrote {definition_file}")
    elif args.refresh_local:
        refresh_definition(args.env_file, args, setup)
        print(f"local definition   : refreshed {definition_file}")
    else:
        print("local scaffold     : preview only; pass --write-local to create it")

    section(f"COURSE ENV (courses/{args.key}.env)")
    coordinates = course_coordinates(environment, setup, args)
    for key, value in coordinates.items():
        print(f"{key}={_dotenv_value(value)}")
    if not coordinates["VOCAREUM_USER_ID"]:
        print(
            "# No single active student selected. Enroll one, then rerun, "
            "or pass --user-email."
        )

    return EXIT_MISMATCH if mismatches else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point with stable exit-code classification."""
    try:
        return run(parse_args(argv))
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except ApiError as error:
        print(f"Vocareum API error: {error}", file=sys.stderr)
        return EXIT_API


if __name__ == "__main__":
    raise SystemExit(main())
