"""Preview, publish, prepare, apply, and restore a complete course release.

The GraphRAG release contains two artifacts: ``private/lab.template`` and the
complete ``startercode`` tree. The default mode performs local validation and
builds a deterministic starter-code archive. ``--prepare`` adds read-only
Vocareum calls and saves both remote artifacts for rollback. ``--publish``
prepares and immediately applies one release. Only ``--publish``, ``--apply``,
and ``--restore`` write to Vocareum. No mode starts or extends a student lab.

``--verify-live`` exists because ``--apply`` proves only what was true during
its own run. It compares the local plan's own captured "previous" digest, so a
run that finds the remote already matching prints "Nothing to upload" and exits
success, and every check it makes is against state it read seconds earlier.
That was enough until 2026-08-25, when the GraphRAG ``startercode`` tree was
published and byte-verified at 04:59Z and then found reverted to its
clone-time snapshot hours later, with the release evidence still reading
``result: success``. Vocareum can discard an API write after the fact, most
likely when an instructor Configure Workspace browser session writes its own
snapshot back over the part.

So ``--verify-live`` is deliberately the cheapest mode and shares nothing with
the others: no prepared plan, no release ID, no source checkout, no ``cfn-lint``,
no archive rebuild. It reads the live tree and compares it to the recorded
starter-code inventory and the course template on disk. That makes it safe to
re-run minutes or days after a release, which is the only way this class of
silent revert is ever caught.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import startercode
from .client import TERMINAL_SUCCESS_STATES, ApiError
from .config import (
    COURSE_NAME_PATTERN,
    ConfigError,
    add_course_argument,
    course_env_path,
    read_dotenv,
    repository_root,
)
from .course import StarterCode, course_directory, load_definition
from .output import section
from .upload import (
    EXIT_API,
    EXIT_CONFIG,
    EXIT_MISMATCH,
    EXIT_OK,
    Config,
    api_json,
    course_template_path,
    digest,
    load_config,
    poll_transaction,
    read_remote_file,
    upload,
    upload_archive,
)

EXIT_VALIDATION = 4
TEMPLATE_TARGET = "private"
TEMPLATE_NAME = "lab.template"
TEMPLATE_REMOTE_PATH = f"{TEMPLATE_TARGET}/{TEMPLATE_NAME}"
STARTER_TARGET = "startercode"
RELEASE_SCHEMA = 2
RELEASE_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}$")
MAX_REMOTE_FILES = 2_000
REMOTE_TREE_VERIFY_POLL_SECONDS = 3
REMOTE_TREE_VERIFY_TIMEOUT_SECONDS = 120

# Enough to see the shape of a drift without paging a 54-file revert
# off the screen. --verify-live prints the count unconditionally, so the cap
# never hides that there is drift, only the full list of it.
MAX_DRIFT_LINES = 20


class ReleaseError(RuntimeError):
    """Raised when a release is unsafe or incomplete."""


class RemoteTreeChangedError(ReleaseError):
    """Raised when a remote tree changes while it is being traversed."""


@dataclass(frozen=True)
class LocalRelease:
    """The two locally validated artifacts in one course release."""

    template_path: Path
    template_payload: bytes
    source_revision: str
    starter_path: Path
    starter_payload: bytes
    inventory_path: Path
    inventory_payload: bytes
    starter_files: dict[str, bytes]


@dataclass(frozen=True)
class ReleasePlan:
    """The immutable facts approved for one prepared release."""

    schema_version: int
    release_id: str
    created_at: str
    course: str
    course_id: str
    assignment_id: str
    part_id: str
    source_file: str
    source_revision: str
    source_size: int
    source_digest: str
    remote_path: str
    previous_present: bool
    previous_digest: str | None
    starter_source_file: str
    starter_source_size: int
    starter_source_digest: str
    starter_inventory_file: str
    starter_inventory_digest: str
    starter_file_count: int
    starter_remote_path: str
    starter_previous_digest: str
    starter_previous_file_count: int


def template_path(env_path: Path, course: str) -> Path:
    """Return the course-owned template path.

    Delegated to :mod:`vocareum_tools.course` rather than rebuilt here, because
    a course may keep its assets in its own repository and only that module
    knows where. An independent copy of the path rule is what let one course's
    assets get split across two directories.
    """
    return course_template_path(env_path, course)


def display_path(path: Path, root: Path) -> str:
    """Name ``path`` for a release record, whether or not it lives under ``root``.

    A release plan records the path it read so ``--apply`` can prove the input
    did not change between preparation and upload. An external course's template
    sits outside this repository, and ``Path.relative_to`` raises for such a
    path, so fall back to the absolute path. Both spellings are stable across
    the two reads, which is the only property the comparison needs.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def starter_build_directory(root: Path, course: str) -> Path:
    """Return the ignored directory holding deterministic local artifacts."""
    return root / ".vocareum" / "build" / course


def release_root(root: Path, course: str) -> Path:
    """Return the ignored directory holding one course's release evidence."""
    return root / ".vocareum" / "releases" / course


def release_directory(root: Path, course: str, release_id: str) -> Path:
    """Resolve an immutable release directory without allowing path traversal."""
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise ConfigError("Release ID must be the exact value printed by --prepare.")
    return release_root(root, course) / release_id


def source_revision(path: Path) -> str:
    """Read a pinned source commit from template metadata or a legacy parameter."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ConfigError(f"Could not read {path}: {error}") from error

    for line in lines:
        if line.startswith("  SourceCommit:"):
            value = line.partition(":")[2].strip().strip("\"'")
            if re.fullmatch(r"[0-9a-f]{40}", value):
                return value
            break

    in_parameter = False
    for line in lines:
        if line == "  RepoCommit:":
            in_parameter = True
            continue
        if in_parameter and line.startswith("  ") and not line.startswith("    "):
            break
        if in_parameter and line.startswith("    Default:"):
            value = line.partition(":")[2].strip().strip("\"'")
            if re.fullmatch(r"[0-9a-f]{40}", value):
                return value
            break
    raise ConfigError(
        f"{path} must pin Metadata.SourceCommit or "
        "Parameters.RepoCommit.Default to a 40-character commit."
    )


def run_checked(command: list[str], root: Path) -> None:
    """Run a validation command and fail on any nonzero exit."""
    result = subprocess.run(command, cwd=root, check=False)
    if result.returncode != 0:
        raise ReleaseError(
            f"Validation failed with exit code {result.returncode}: {' '.join(command)}"
        )


def validate_course(root: Path, course: str, source: Path) -> None:
    """Run the reusable course validator and lint the selected template."""
    validator = root / "scripts" / f"validate_{course}_course.py"
    if not validator.is_file():
        raise ConfigError(
            f"Course {course!r} has no reusable validator at {validator}."
        )
    section("VALIDATE")
    run_checked([sys.executable, str(validator)], root)
    run_checked(["cfn-lint", str(source)], root)


def load_template(root: Path, config: Config) -> tuple[Path, bytes, str]:
    """Read and identify the only template allowed for the selected course."""
    source = template_path(config.env_path, config.course)
    if not source.is_file():
        raise ConfigError(f"No course template at {source}.")
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise ConfigError(f"Could not read {source}: {error}") from error
    if not payload:
        raise ConfigError(f"{source} is empty. Refusing to release it.")
    return source, payload, source_revision(source)


def read_starter_archive(
    archive_path: Path, inventory_path: Path, revision: str, course: str
) -> tuple[bytes, bytes, dict[str, bytes]]:
    """Read and independently verify the built starter-code package."""
    try:
        archive = archive_path.read_bytes()
        inventory_payload = inventory_path.read_bytes()
        inventory = json.loads(inventory_payload)
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"Could not read the starter-code build: {error}") from error
    if not isinstance(inventory, dict):
        raise ReleaseError("Starter-code inventory must be a JSON object.")
    expected_identity = {
        "course": course,
        "target": STARTER_TARGET,
        "source_commit": revision,
        "archive": startercode.ARCHIVE_NAME,
        "archive_size": len(archive),
        "archive_sha256": digest(archive),
    }
    mismatches = [
        name
        for name, expected in expected_identity.items()
        if inventory.get(name) != expected
    ]
    if mismatches:
        raise ReleaseError(
            "Starter-code inventory disagrees with the selected release for: "
            + ", ".join(mismatches)
        )

    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(archive_path) as bundle:
            names = bundle.namelist()
            if names != sorted(names) or len(names) != len(set(names)):
                raise ReleaseError(
                    "Starter-code archive paths must be unique and sorted."
                )
            for name in names:
                startercode.validate_archive_path(name)
                files[name] = bundle.read(name)
    except (OSError, zipfile.BadZipFile) as error:
        raise ReleaseError(f"Starter-code archive is unreadable: {error}") from error

    entries = inventory.get("files")
    if not isinstance(entries, list) or inventory.get("file_count") != len(files):
        raise ReleaseError("Starter-code inventory file count is incorrect.")
    recorded: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReleaseError("Starter-code inventory contains an invalid entry.")
        path = entry.get("path")
        size = entry.get("size")
        sha256 = entry.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(size, int)
            or not isinstance(sha256, str)
        ):
            raise ReleaseError("Starter-code inventory contains an invalid entry.")
        if path in recorded:
            raise ReleaseError(f"Duplicate starter-code inventory path: {path}")
        recorded[path] = (size, sha256)
    actual = {name: (len(payload), digest(payload)) for name, payload in files.items()}
    if recorded != actual:
        raise ReleaseError("Starter-code inventory does not match the archive files.")
    return archive, inventory_payload, files


def starter_code(config: Config) -> StarterCode:
    """Return the selected course's starter-code block, or refuse.

    Refusing on the absent block rather than on the course name states the real
    reason. A course that ships no student package has nothing for a complete
    release to build.
    """
    definition = load_definition(config.env_path, config.course)
    if definition.startercode is None:
        raise ConfigError(
            f"Course {config.course!r} declares no startercode block, so it has "
            "no student package to release. Add one to its course.json, or "
            "release the template alone."
        )
    return definition.startercode


def starter_source(config: Config, starter: StarterCode) -> Path:
    """Resolve the Git checkout the starter package is read from."""
    directory = course_directory(config.env_path, config.course)
    source = (directory / starter.source).resolve()
    if not source.is_dir():
        raise ConfigError(
            f"Course {config.course!r} names starter-code source {source}, "
            "which does not exist. Check out the course repository."
        )
    return source


def build_local_release(root: Path, config: Config) -> LocalRelease:
    """Validate, build, and load both local artifacts without an API call."""
    template, template_payload, revision = load_template(root, config)
    validate_course(root, config.course, template)
    starter = starter_code(config)
    source = starter_source(config, starter)
    output = starter_build_directory(root, config.course)
    archive_path, inventory_path, _ = startercode.write_package(
        source,
        output,
        course=config.course,
        expected_repository=starter.repository,
        commit=starter.commit,
        selected_paths=starter.paths,
    )
    archive, inventory, files = read_starter_archive(
        archive_path, inventory_path, revision, config.course
    )
    return LocalRelease(
        template_path=template,
        template_payload=template_payload,
        source_revision=revision,
        starter_path=archive_path,
        starter_payload=archive,
        inventory_path=inventory_path,
        inventory_payload=inventory,
        starter_files=files,
    )


def verify_course_destination(config: Config) -> None:
    """Refuse a shell override that separates a name from its saved IDs."""
    course_path = course_env_path(config.env_path, config.course)
    saved = read_dotenv(course_path)
    expected = {
        "VOCAREUM_COURSE_ID": config.course_id,
        "VOCAREUM_ASSIGNMENT_ID": config.assignment_id,
        "VOCAREUM_PART_ID": config.part_id,
    }
    mismatches = [
        name for name, actual in expected.items() if saved.get(name) != actual
    ]
    if mismatches:
        raise ConfigError(
            "The selected course IDs must come from "
            f"{course_path}. Unset shell overrides for: {', '.join(mismatches)}."
        )


def print_plan(config: Config, local: LocalRelease) -> None:
    """Print every identity and digest needed to approve a release."""
    section("RELEASE PLAN")
    print(f"Selected course   : {config.course}")
    print(f"Course/Assignment : {config.course_id}/{config.assignment_id}")
    print(f"Part              : {config.part_id}")
    print(f"Source revision   : {local.source_revision}")
    print(
        f"Template          : {local.template_path} "
        f"({len(local.template_payload)} bytes)"
    )
    print(f"Destination       : /voc/{TEMPLATE_REMOTE_PATH}")
    print(f"Template sha256   : {digest(local.template_payload)}")
    print(
        f"Starter code      : {local.starter_path} "
        f"({len(local.starter_payload)} bytes, {len(local.starter_files)} files)"
    )
    print(f"Destination       : /voc/{STARTER_TARGET}/")
    print(f"Starter sha256    : {digest(local.starter_payload)}")
    print(f"Inventory sha256  : {digest(local.inventory_payload)}")


def make_release_id(local: LocalRelease) -> tuple[str, str]:
    """Return a sortable release ID based on both approved artifacts."""
    combined = digest(
        f"{digest(local.template_payload)}:{digest(local.starter_payload)}".encode()
    )
    now = datetime.now(UTC)
    created_at = now.isoformat().replace("+00:00", "Z")
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{combined[:12]}", created_at


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one human-readable evidence record."""
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def find_files(payload: Any) -> list[str] | None:
    """Find the directory-listing array in a Vocareum response."""
    if isinstance(payload, dict):
        files = payload.get("files")
        if isinstance(files, list) and all(isinstance(item, str) for item in files):
            return files
        for value in payload.values():
            found = find_files(value)
            if found is not None:
                return found
    return None


def is_missing(error: ApiError) -> bool:
    """Return whether Vocareum explicitly reported an absent path."""
    return error.status_code == 404 or (
        error.status_code == 400
        and any(
            marker in str(error).lower()
            for marker in ("does not exist", "doesn't exist")
        )
    )


def list_remote_directory(config: Config, directory: str) -> list[str] | None:
    """List one absolute /voc directory, or return None when it is absent."""
    try:
        payload = api_json(
            "GET",
            config.files_url({"dir": directory, "list": "true"}),
            config.api_key,
        )
    except ApiError as error:
        if is_missing(error):
            return None
        raise
    files = find_files(payload)
    if files is None:
        raise ReleaseError(
            f"Directory listing for {directory} carried no string files array."
        )
    return files


def validate_remote_name(name: str) -> str:
    """Require one safe child name from a remote directory listing."""
    candidate = PurePosixPath(name)
    if (
        not name
        or candidate.is_absolute()
        or len(candidate.parts) != 1
        or name in {".", ".."}
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise ReleaseError(f"Unsafe remote workspace entry: {name!r}")
    return name


def read_remote_tree(config: Config, target: str) -> dict[str, bytes]:
    """Read a complete workspace tree for rollback and structural verification."""
    collected: dict[str, bytes] = {}
    visiting: set[str] = set()

    def walk(
        directory: str,
        relative: PurePosixPath,
        names: list[str] | None = None,
    ) -> None:
        if directory in visiting:
            raise ReleaseError(f"Remote workspace directory cycle at {directory}.")
        visiting.add(directory)
        if names is None:
            names = list_remote_directory(config, directory)
        if names is None:
            visiting.remove(directory)
            return
        for raw_name in names:
            name = validate_remote_name(raw_name.rstrip("/"))
            child = relative / name
            child_directory = f"{directory}/{name}"
            child_names = list_remote_directory(config, child_directory)
            if child_names is not None:
                walk(child_directory, child, child_names)
            else:
                remote_path = f"{target}/{child.as_posix()}"
                payload = read_remote_file(config, remote_path)
                if payload is None:
                    raise RemoteTreeChangedError(
                        f"Listed workspace entry disappeared: {remote_path}"
                    )
                collected[child.as_posix()] = payload
            if len(collected) > MAX_REMOTE_FILES:
                raise ReleaseError(
                    f"Remote {target} tree exceeds the {MAX_REMOTE_FILES}-file guard."
                )
        visiting.remove(directory)

    walk(f"/voc/{target}", PurePosixPath())
    return dict(sorted(collected.items()))


def tree_archive(files: dict[str, bytes]) -> bytes:
    """Build the canonical digest and rollback archive for a remote tree."""
    return startercode.build_archive(
        [startercode.SourceFile(path, payload) for path, payload in files.items()]
    )


def read_starter_inventory(root: Path, course: str) -> dict[str, tuple[int, str]]:
    """Read the recorded starter-code inventory as a path to size-and-digest map.

    Read rather than rebuilt on purpose. ``build_local_release`` regenerates the
    archive from a sibling checkout and shells out to ``cfn-lint``, which makes
    it useless for the repeated "is it still there?" check this mode is for, and
    couples the answer to a working tree that may have moved on since the
    release. The inventory is the immutable record of what was published.
    """
    path = starter_build_directory(root, course) / startercode.INVENTORY_NAME
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(
            f"Could not read the starter-code inventory at {path}: {error}. "
            "Build the package before verifying a live release."
        ) from error
    if not isinstance(inventory, dict) or inventory.get("target") != STARTER_TARGET:
        raise ReleaseError(f"{path} is not a {STARTER_TARGET} inventory.")

    entries = inventory.get("files")
    if not isinstance(entries, list) or not entries:
        raise ReleaseError(f"{path} records no files.")
    expected: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReleaseError(f"{path} holds a non-object file entry.")
        name = entry.get("path")
        size = entry.get("size")
        sha256 = entry.get("sha256")
        if (
            not isinstance(name, str)
            or not isinstance(size, int)
            or not isinstance(sha256, str)
        ):
            raise ReleaseError(f"{path} holds an unusable file entry: {entry!r}.")
        expected[name] = (size, sha256)
    if inventory.get("file_count") != len(expected):
        raise ReleaseError(f"{path} disagrees with its own file count.")
    return expected


@dataclass(frozen=True)
class LiveDrift:
    """How the live workspace differs from the published record, in both directions."""

    template_expected: str
    template_actual: str | None
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]
    changed: tuple[str, ...]

    @property
    def template_matches(self) -> bool:
        """Return whether the live template is the one on disk for this course."""
        return self.template_actual == self.template_expected

    @property
    def starter_matches(self) -> bool:
        """Return whether the live tree is exactly the published inventory."""
        return not (self.missing or self.unexpected or self.changed)

    @property
    def matches(self) -> bool:
        """Return whether both artifacts are live and unchanged."""
        return self.template_matches and self.starter_matches


def compare_live(
    expected: dict[str, tuple[int, str]],
    actual: dict[str, bytes],
    template_expected: str,
    template_actual: str | None,
) -> LiveDrift:
    """Classify live workspace state against the published record.

    Missing, unexpected, and changed are kept apart rather than collapsed into
    one count because they point at different causes. An empty ``missing`` with
    a populated ``unexpected`` is a stale leftover; a wholesale ``missing`` with
    the pre-release file back in ``unexpected`` is the revert this mode exists
    to name.
    """
    return LiveDrift(
        template_expected=template_expected,
        template_actual=template_actual,
        missing=tuple(sorted(set(expected) - set(actual))),
        unexpected=tuple(sorted(set(actual) - set(expected))),
        changed=tuple(
            sorted(
                name
                for name, payload in actual.items()
                if name in expected
                and expected[name] != (len(payload), digest(payload))
            )
        ),
    )


def verify_live(root: Path, config: Config) -> int:
    """Read the live workspace and report whether the published release survived.

    Returns ``EXIT_MISMATCH`` on any drift, so a scheduled re-check is a plain
    exit-code test and needs no output parsing.
    """
    section("VERIFY LIVE")
    print(f"Selected course   : {config.course}")
    print(f"Course/Assignment : {config.course_id}/{config.assignment_id}")
    print(f"Part              : {config.part_id}")

    template = template_path(config.env_path, config.course)
    if not template.is_file():
        raise ConfigError(f"No course template at {template}.")
    expected = read_starter_inventory(root, config.course)
    drift = compare_live(
        expected,
        read_remote_tree(config, STARTER_TARGET),
        digest(template.read_bytes()),
        remote_template_digest(config),
    )

    print(f"/voc/{TEMPLATE_REMOTE_PATH}")
    print(f"  expected sha256 : {drift.template_expected}")
    print(f"  live sha256     : {drift.template_actual or 'absent'}")
    print(f"  verdict         : {'match' if drift.template_matches else 'DRIFTED'}")
    print(f"/voc/{STARTER_TARGET}/")
    live_count = len(expected) - len(drift.missing) + len(drift.unexpected)
    print(f"  expected files  : {len(expected)}")
    print(f"  live files      : {live_count}")
    for label, names in (
        ("missing", drift.missing),
        ("unexpected", drift.unexpected),
        ("changed", drift.changed),
    ):
        print(f"  {label:14}: {len(names)}")
        for name in names[:MAX_DRIFT_LINES]:
            print(f"      {name}")
        if len(names) > MAX_DRIFT_LINES:
            print(f"      ... and {len(names) - MAX_DRIFT_LINES} more")
    print(f"  verdict         : {'match' if drift.starter_matches else 'DRIFTED'}")

    section("RESULT")
    if drift.matches:
        print("The live workspace matches the published release.")
        return EXIT_OK
    print("The live workspace does NOT match the published release.")
    print("Nothing was changed. Re-publish with --publish.")
    return EXIT_MISMATCH


def prepare_release(root: Path, config: Config, local: LocalRelease) -> ReleasePlan:
    """Read both remote artifacts and save an immutable rollback bundle."""
    section("PREPARE ROLLBACK")
    previous_template = read_remote_file(config, TEMPLATE_REMOTE_PATH)
    previous_starter = read_remote_tree(config, STARTER_TARGET)
    previous_starter_archive = tree_archive(previous_starter)
    release_id, created_at = make_release_id(local)
    directory = release_directory(root, config.course, release_id)
    if directory.exists():
        raise ReleaseError(f"Release directory already exists: {directory}")
    directory.mkdir(parents=True)

    plan = ReleasePlan(
        schema_version=RELEASE_SCHEMA,
        release_id=release_id,
        created_at=created_at,
        course=config.course,
        course_id=config.course_id,
        assignment_id=config.assignment_id,
        part_id=config.part_id,
        source_file=display_path(local.template_path, root),
        source_revision=local.source_revision,
        source_size=len(local.template_payload),
        source_digest=digest(local.template_payload),
        remote_path=TEMPLATE_REMOTE_PATH,
        previous_present=previous_template is not None,
        previous_digest=(
            digest(previous_template) if previous_template is not None else None
        ),
        starter_source_file=display_path(local.starter_path, root),
        starter_source_size=len(local.starter_payload),
        starter_source_digest=digest(local.starter_payload),
        starter_inventory_file=display_path(local.inventory_path, root),
        starter_inventory_digest=digest(local.inventory_payload),
        starter_file_count=len(local.starter_files),
        starter_remote_path=STARTER_TARGET,
        starter_previous_digest=digest(previous_starter_archive),
        starter_previous_file_count=len(previous_starter),
    )
    if previous_template is not None:
        (directory / "rollback-lab.template").write_bytes(previous_template)
    (directory / "rollback-startercode.zip").write_bytes(previous_starter_archive)
    write_json(directory / "plan.json", asdict(plan))

    print(f"Release ID             : {release_id}")
    print(f"Evidence               : {directory}")
    print(f"Previous template      : {plan.previous_digest or 'absent'}")
    print(f"Previous starter files : {plan.starter_previous_file_count}")
    print(f"Previous starter sha256: {plan.starter_previous_digest}")
    print("Nothing was uploaded.")
    return plan


def read_plan(root: Path, config: Config, release_id: str) -> ReleasePlan:
    """Load a prepared plan and verify that it still names this destination."""
    directory = release_directory(root, config.course, release_id)
    path = directory / "plan.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        plan = ReleasePlan(**raw)
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise ReleaseError(
            f"Could not read prepared release {path}: {error}"
        ) from error

    expected_identity = (
        config.course,
        config.course_id,
        config.assignment_id,
        config.part_id,
    )
    actual_identity = (
        plan.course,
        plan.course_id,
        plan.assignment_id,
        plan.part_id,
    )
    if plan.schema_version != RELEASE_SCHEMA or actual_identity != expected_identity:
        raise ReleaseError(
            "Prepared release does not match the selected course destination."
        )
    if (
        plan.remote_path != TEMPLATE_REMOTE_PATH
        or plan.starter_remote_path != STARTER_TARGET
        or plan.release_id != release_id
    ):
        raise ReleaseError("Prepared release has inconsistent immutable fields.")
    return plan


def verify_local_plan(root: Path, plan: ReleasePlan, local: LocalRelease) -> None:
    """Refuse when either local release input changed after preparation."""
    checks = (
        (display_path(local.template_path, root), plan.source_file, "template path"),
        (
            display_path(local.starter_path, root),
            plan.starter_source_file,
            "starter-code path",
        ),
        (
            display_path(local.inventory_path, root),
            plan.starter_inventory_file,
            "inventory path",
        ),
    )
    for actual, expected, label in checks:
        if actual != expected:
            raise ReleaseError(f"The prepared {label} no longer matches.")
    if (
        len(local.template_payload) != plan.source_size
        or digest(local.template_payload) != plan.source_digest
        or local.source_revision != plan.source_revision
    ):
        raise ReleaseError("The local template changed after --prepare. Prepare again.")
    if (
        len(local.starter_payload) != plan.starter_source_size
        or digest(local.starter_payload) != plan.starter_source_digest
        or digest(local.inventory_payload) != plan.starter_inventory_digest
        or len(local.starter_files) != plan.starter_file_count
    ):
        raise ReleaseError(
            "The local starter-code build changed after --prepare. Prepare again."
        )


def remote_template_digest(config: Config) -> str | None:
    """Return the current remote template digest."""
    payload = read_remote_file(config, TEMPLATE_REMOTE_PATH)
    return digest(payload) if payload is not None else None


def remote_tree_digest(files: dict[str, bytes]) -> str:
    """Return a structure-sensitive digest for a complete workspace tree."""
    return digest(tree_archive(files))


def upload_template(config: Config, payload: bytes) -> tuple[int, str | None]:
    """Upload and verify the course template."""
    transaction_id = upload(config, TEMPLATE_TARGET, TEMPLATE_NAME, payload)
    if transaction_id is None:
        return EXIT_API, None
    state = poll_transaction(config, transaction_id)
    if state.lower() not in TERMINAL_SUCCESS_STATES:
        return EXIT_API, transaction_id
    if remote_template_digest(config) != digest(payload):
        return EXIT_MISMATCH, transaction_id
    return EXIT_OK, transaction_id


def describe_tree_difference(
    expected: dict[str, bytes], actual: dict[str, bytes]
) -> str:
    """Summarize a structural or byte mismatch without printing file content."""
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        name for name in set(expected) & set(actual) if expected[name] != actual[name]
    )
    parts = []
    if missing:
        parts.append(f"missing={missing[:5]}")
    if extra:
        parts.append(f"extra={extra[:5]}")
    if changed:
        parts.append(f"changed={changed[:5]}")
    return "; ".join(parts) or "unknown mismatch"


def wait_for_uploaded_tree(
    config: Config, target: str, expected_files: dict[str, bytes]
) -> dict[str, bytes]:
    """Wait for an accepted workspace archive to become consistently readable."""
    attempts = max(
        1,
        REMOTE_TREE_VERIFY_TIMEOUT_SECONDS // REMOTE_TREE_VERIFY_POLL_SECONDS,
    )
    last_actual: dict[str, bytes] = {}
    last_error: RemoteTreeChangedError | None = None
    saw_actual = False
    for attempt in range(attempts):
        try:
            actual = read_remote_tree(config, target)
        except RemoteTreeChangedError as error:
            last_error = error
        else:
            last_actual = actual
            last_error = None
            saw_actual = True
            if actual == expected_files:
                return actual
        if attempt == 0:
            print(
                "The remote starter-code tree is still settling; waiting up to "
                f"{REMOTE_TREE_VERIFY_TIMEOUT_SECONDS} seconds for exact verification."
            )
        if attempt + 1 < attempts:
            time.sleep(REMOTE_TREE_VERIFY_POLL_SECONDS)

    if last_error is not None and not saw_actual:
        raise ReleaseError(
            "The remote starter-code tree did not become consistently readable "
            "after the upload transaction completed."
        ) from last_error
    return last_actual


def upload_starter_tree(
    config: Config, archive: bytes, expected_files: dict[str, bytes]
) -> tuple[int, str | None, dict[str, bytes]]:
    """Replace and verify the complete starter-code tree."""
    transaction_id = upload_archive(config, STARTER_TARGET, archive, reset=True)
    if transaction_id is None:
        return EXIT_API, None, {}
    state = poll_transaction(config, transaction_id)
    if state.lower() not in TERMINAL_SUCCESS_STATES:
        return EXIT_API, transaction_id, {}
    actual = wait_for_uploaded_tree(config, STARTER_TARGET, expected_files)
    if actual != expected_files:
        print(
            "The remote starter-code tree does not match the approved archive: "
            + describe_tree_difference(expected_files, actual)
        )
        return EXIT_MISMATCH, transaction_id, actual
    return EXIT_OK, transaction_id, actual


def deployment_record(
    plan: ReleasePlan,
    *,
    result: str,
    template_digest: str | None,
    starter_digest: str,
    template_transaction: str | None,
    starter_transaction: str | None,
) -> dict[str, Any]:
    """Build a combined deployment record with no credential material."""
    return {
        **asdict(plan),
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "result": result,
        "remote_digest": template_digest,
        "starter_remote_digest": starter_digest,
        "transaction_id": template_transaction,
        "starter_transaction_id": starter_transaction,
    }


def apply_release(
    root: Path, config: Config, release_id: str, local: LocalRelease
) -> int:
    """Apply both artifacts after checking all local and remote drift."""
    plan = read_plan(root, config, release_id)
    verify_local_plan(root, plan, local)
    current_template = remote_template_digest(config)
    current_starter = read_remote_tree(config, STARTER_TARGET)
    if current_template != plan.previous_digest:
        raise ReleaseError(
            "The remote template changed after --prepare. Refusing to overwrite it."
        )
    if remote_tree_digest(current_starter) != plan.starter_previous_digest:
        raise ReleaseError(
            "Remote starter code changed after --prepare. Refusing to overwrite it."
        )

    starter_transaction = None
    if plan.starter_previous_digest == plan.starter_source_digest:
        starter_result = EXIT_OK
        final_starter = current_starter
        print("Starter code already matches this release. Nothing to upload.")
    else:
        section("APPLY STARTER CODE")
        starter_result, starter_transaction, final_starter = upload_starter_tree(
            config, local.starter_payload, local.starter_files
        )
    template_result = EXIT_API
    template_transaction = None
    if starter_result == EXIT_OK:
        if plan.previous_digest == plan.source_digest:
            template_result = EXIT_OK
            print("Template already matches this release. Nothing to upload.")
        else:
            section("APPLY TEMPLATE")
            template_result, template_transaction = upload_template(
                config, local.template_payload
            )
    result = starter_result if starter_result != EXIT_OK else template_result
    final_template = remote_template_digest(config)
    if not final_starter:
        final_starter = read_remote_tree(config, STARTER_TARGET)
    record = deployment_record(
        plan,
        result="success" if result == EXIT_OK else "failed",
        template_digest=final_template,
        starter_digest=remote_tree_digest(final_starter),
        template_transaction=template_transaction,
        starter_transaction=starter_transaction,
    )
    directory = release_directory(root, config.course, release_id)
    write_json(directory / "deployment.json", record)
    print(f"Deployment record: {directory / 'deployment.json'}")
    if result != EXIT_OK:
        print(
            "Release verification failed. Inspect the record and run "
            f"--restore {release_id}."
        )
    return result


def publish_release(root: Path, config: Config, local: LocalRelease) -> int:
    """Prepare and immediately apply one release with the same drift checks."""
    plan = prepare_release(root, config, local)
    return apply_release(root, config, plan.release_id, local)


def read_zip_files(path: Path) -> tuple[bytes, dict[str, bytes]]:
    """Read a saved rollback archive and return its exact file mapping."""
    try:
        archive = path.read_bytes()
        with zipfile.ZipFile(path) as bundle:
            files = {name: bundle.read(name) for name in bundle.namelist()}
    except (OSError, zipfile.BadZipFile) as error:
        raise ReleaseError(f"Saved rollback archive is unreadable: {error}") from error
    for name in files:
        startercode.validate_archive_path(name)
    if tree_archive(files) != archive:
        raise ReleaseError("Saved starter-code rollback archive is not canonical.")
    return archive, files


def restore_release(root: Path, config: Config, release_id: str) -> int:
    """Restore the exact template and starter-code tree saved by --prepare."""
    plan = read_plan(root, config, release_id)
    directory = release_directory(root, config.course, release_id)
    rollback_template = directory / "rollback-lab.template"
    if not plan.previous_present or not rollback_template.is_file():
        raise ReleaseError("This release has no saved prior template to restore.")
    previous_template = rollback_template.read_bytes()
    if digest(previous_template) != plan.previous_digest:
        raise ReleaseError("The saved rollback template failed its digest check.")
    previous_archive, previous_files = read_zip_files(
        directory / "rollback-startercode.zip"
    )
    if (
        digest(previous_archive) != plan.starter_previous_digest
        or len(previous_files) != plan.starter_previous_file_count
    ):
        raise ReleaseError("The saved starter-code rollback failed its digest check.")

    current_template = remote_template_digest(config)
    current_starter = read_remote_tree(config, STARTER_TARGET)
    current_starter_digest = remote_tree_digest(current_starter)
    if current_template not in {plan.previous_digest, plan.source_digest}:
        raise ReleaseError(
            "The remote template is unrelated to this release. Refusing restore."
        )
    if current_starter_digest not in {
        plan.starter_previous_digest,
        plan.starter_source_digest,
    }:
        raise ReleaseError(
            "Remote starter code is unrelated to this release. Refusing restore."
        )

    starter_transaction = None
    starter_result = EXIT_OK
    if current_starter_digest != plan.starter_previous_digest:
        section("RESTORE STARTER CODE")
        starter_result, starter_transaction, _ = upload_starter_tree(
            config, previous_archive, previous_files
        )
    template_transaction = None
    template_result = EXIT_OK
    if starter_result == EXIT_OK and current_template != plan.previous_digest:
        section("RESTORE TEMPLATE")
        template_result, template_transaction = upload_template(
            config, previous_template
        )
    result = starter_result if starter_result != EXIT_OK else template_result
    final_template = remote_template_digest(config)
    final_starter = read_remote_tree(config, STARTER_TARGET)
    write_json(
        directory / "restore.json",
        {
            "release_id": release_id,
            "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "result": "success" if result == EXIT_OK else "failed",
            "restored_digest": final_template,
            "starter_restored_digest": remote_tree_digest(final_starter),
            "transaction_id": template_transaction,
            "starter_transaction_id": starter_transaction,
        },
    )
    print(f"Restore record: {directory / 'restore.json'}")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse preview, publish, prepare, apply, and restore modes."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--publish",
        action="store_true",
        help="Prepare, upload, and verify one release in a single command.",
    )
    mode.add_argument(
        "--prepare",
        action="store_true",
        help="Read and save both current remote artifacts without changing them.",
    )
    mode.add_argument(
        "--apply",
        metavar="RELEASE_ID",
        help="Upload and verify one exact release created by --prepare.",
    )
    mode.add_argument(
        "--restore",
        metavar="RELEASE_ID",
        help="Restore both prior artifacts saved by --prepare.",
    )
    mode.add_argument(
        "--verify-live",
        action="store_true",
        help=(
            "Read the live workspace and compare it to the recorded starter-code "
            "inventory and course template. Needs no prepared release. Exits 3 on "
            "any drift, which is how a Vocareum-side revert is caught after the fact."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help="Override the default .env location.",
    )
    add_course_argument(parser)
    return parser.parse_args(argv)


def selected_course(value: str | None) -> str:
    """Require a named course for every release mode."""
    course = (value or "").strip()
    if not course:
        raise ConfigError("Pass --course NAME for every release operation.")
    if not COURSE_NAME_PATTERN.fullmatch(course):
        raise ConfigError(f"Course name {course!r} is not usable.")
    return course


def run(args: argparse.Namespace, root: Path) -> int:
    """Run the selected release mode."""
    course = selected_course(args.course)
    env_path = Path(args.env_file).expanduser() if args.env_file else root / ".env"
    config = load_config(env_path, course)
    verify_course_destination(config)

    # Before build_local_release on purpose: this mode must stay runnable when
    # the source checkout is gone and long after the release it is checking.
    if args.verify_live:
        return verify_live(root, config)

    local = build_local_release(root, config)
    print_plan(config, local)

    if args.publish:
        return publish_release(root, config, local)
    if args.prepare:
        prepare_release(root, config, local)
        return EXIT_OK
    if args.apply:
        return apply_release(root, config, args.apply, local)
    if args.restore:
        return restore_release(root, config, args.restore)

    section("RESULT")
    print("Preview complete. No Vocareum API call was made.")
    print("Run again with --publish to save rollback evidence and apply the release.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point with stable nonzero failure classes."""
    args = parse_args(argv)
    try:
        return run(args, repository_root())
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except ApiError as error:
        print(f"Vocareum API error: {error}", file=sys.stderr)
        return EXIT_API
    except (ReleaseError, startercode.StarterCodeError, OSError) as error:
        print(f"Release error: {error}", file=sys.stderr)
        return EXIT_VALIDATION


if __name__ == "__main__":
    raise SystemExit(main())
