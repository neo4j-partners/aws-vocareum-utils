"""Upload a file into a Vocareum part workspace and prove it arrived intact.

Vocareum reads ``lab.template`` from ``/voc/private/lab.template`` in the
instructor workspace when a student starts the lab. The usual way to put it
there is Configure Workspace in the browser, which is manual, silent about what
it actually wrote, and impossible to run from a pipeline.

The API can do it, though the v2 reference does not say how. All three
documented ``files`` operations are ``GET``. The only write path for workspace
content is the part update, ``PUT .../parts/{partId}``, whose ``content`` block
is documented in a single line naming ``target``, ``url``/``zipcontent``, and
``reset`` with no example. The undocumented part that costs an afternoon to find
is that **``content`` must be a JSON array**. Passing the object it looks like it
should be produces ``HTTP 500`` with a zero-length body and no error text, which
reads as a server fault rather than as the request-shape problem it is.

Three things make this worth a script rather than a curl invocation:

1. The write is asynchronous. It answers ``202`` with a ``transactionid``, and
   the file is not in place when the response arrives.
2. Neither the ``202`` nor the directory listing can tell you what content
   landed. The listing returns names only, with no size, hash, or modified time.
   Only downloading the file back and comparing hashes proves anything.
3. Getting ``target`` or the archive layout wrong fails the same silent way as
   getting the JSON shape wrong.

So the default run uploads, waits for the transaction, downloads the file back,
and compares SHA-256 against what was sent. Nothing is reported as uploaded
until those hashes match.

This command is part of the repository's uv project::

    uv run vocareum-upload --dry-run --course graphrag
    uv run vocareum-upload --course graphrag

Configuration comes from a ``.env`` file in the repository root, or from real
environment variables, which always win. See ``.env.example``. Only the API key,
course, assignment, and part are needed. ``VOCAREUM_USER_ID`` is not, since a
workspace belongs to the part rather than to a student.

Run ``cfn-lint`` on a CloudFormation template before uploading it. A template
that cannot parse uploads just as successfully as one that can, and the failure
then surfaces minutes later inside a student session.

Exit codes:
    0  The file was uploaded and verified byte for byte.
    1  Configuration error.
    2  The Vocareum API calls could not complete.
    3  The upload was accepted but the file that came back does not match.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import (
    TERMINAL_SUCCESS_STATES,
    ApiError,
    download_bytes,
    find_transaction_id,
    request_json,
)
from .client import (
    poll_transaction as wait_for_transaction,
)
from .config import (
    VOCAREUM_PART_VARS,
    ConfigError,
    Environment,
    add_course_argument,
    repository_root,
)
from .course import template_path
from .endpoints import DEFAULT_API_BASE, PartEndpoints
from .output import section
from .security import redact_payload, redact_text

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_API = 2
EXIT_MISMATCH = 3

DEFAULT_TARGET = "private"

# Deliberately faster than client.TRANSACTION_POLL_SECONDS. This poll waits on
# a workspace write rather than on a lab launch, and the run is interactive.
TRANSACTION_POLL_SECONDS = 3
TRANSACTION_TIMEOUT_SECONDS = 120
HTTP_TIMEOUT_SECONDS = 60

# Zip entries carry a modification time, so the same file zipped twice produces
# two different archives unless the timestamp is pinned. Pinning it makes an
# upload reproducible and lets two runs be compared. 1980-01-01 is the zero of
# the DOS timestamp the format uses, and is the only value guaranteed to encode.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# Folders seen under /voc on this course. Only "private" has been written to and
# confirmed, so an unrecognized target warns rather than blocks: the list is a
# typo catcher, not a specification. It is worth catching, because an unknown
# target is answered with the same bodyless 500 as every other bad request.
KNOWN_TARGETS = frozenset({"docs", "scripts", "startercode", "private"})


@dataclass(frozen=True)
class Config(PartEndpoints):
    """Resolved configuration for one upload."""

    api_key: str
    course: str = ""
    env_path: Path = Path(".env")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(env_path: Path, course: str | None = None) -> Config:
    """Build a :class:`Config` from real environment variables and ``.env``.

    Real environment variables take precedence over ``.env`` values so the
    script works unchanged in CI. Raises :class:`ConfigError` naming every
    missing required variable at once.
    """
    environment = Environment.from_file(env_path, course)
    try:
        environment.require(VOCAREUM_PART_VARS)
    except ConfigError as error:
        raise ConfigError(
            f"{error}\nCopy .env.example to .env and fill in the values, "
            "or export them in your shell."
        ) from error

    return Config(
        api_key=environment.get("VOCAREUM_API_KEY"),
        course_id=environment.get("VOCAREUM_COURSE_ID"),
        assignment_id=environment.get("VOCAREUM_ASSIGNMENT_ID"),
        part_id=environment.get("VOCAREUM_PART_ID"),
        api_base=(environment.get("VOCAREUM_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
        course=environment.course,
        env_path=env_path,
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def api_json(method: str, url: str, api_key: str, body: Any = None) -> Any:
    """Call the shared API client with the upload timeout."""
    return request_json(
        method,
        url,
        api_key,
        body,
        timeout=HTTP_TIMEOUT_SECONDS,
    )


def download(url: str) -> bytes:
    """Download with the upload timeout and safe URL diagnostics."""
    return download_bytes(url, timeout=HTTP_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def build_zip(arcname: str, payload: bytes) -> bytes:
    """Return a deterministic single-member zip archive holding ``payload``.

    Archive members are written into the folder named by ``target``, so a member
    named ``lab.template`` uploaded with ``target: "private"`` lands at
    ``/voc/private/lab.template``. A member carrying its own directory prefix
    was rejected, so the name stays flat.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        info = zipfile.ZipInfo(filename=arcname, date_time=ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, payload)
    return buffer.getvalue()


def digest(payload: bytes) -> str:
    """Return the SHA-256 hex digest of a payload."""
    return hashlib.sha256(payload).hexdigest()


def validate_path_segment(value: str, label: str) -> None:
    """Reject names that could escape the selected workspace directory."""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ConfigError(f"{label} must be one non-empty path segment, not {value!r}.")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def read_remote_file(config: Config, remote_path: str) -> bytes | None:
    """Download one workspace file, or return ``None`` when it is absent."""
    try:
        payload = api_json(
            "GET", config.files_url({"filename": remote_path}), config.api_key
        )
    except ApiError as error:
        # lab.template already existed at its path when this was written, so
        # only a 404 had ever been observed for "not there yet". Uploading
        # lab.policy for the first time surfaced a second shape: HTTP 400
        # with a body saying the file does not exist. Both mean the same
        # thing, so both are treated as a first upload; any other 400 still
        # raises.
        not_found = error.status_code == 404 or (
            error.status_code == 400 and "does not exist" in str(error).lower()
        )
        if not not_found:
            raise
        return None

    url = find_download_url(payload)
    if url is None:
        raise ApiError(
            "The existing-file response succeeded but carried no download_url. "
            "Refusing to treat an unknown response shape as a missing file."
        )

    return download(url)


def show_current_file(config: Config, remote_path: str) -> str | None:
    """Print the digest of what is up there now. Returns it, or ``None``.

    Runs before the upload so the transcript records what was replaced. A
    missing file is an ordinary first upload rather than an error.
    """
    section("STEP 1: Read what is there now")
    print(f"filename={remote_path}")

    payload = read_remote_file(config, remote_path)
    if payload is None:
        print("No existing file at that path.")
        print("Treating this as a first upload.")
        return None

    current = digest(payload)
    print(f"Current sha256: {current}")
    return current


def find_download_url(payload: Any) -> str | None:
    """Pull the first ``download_url`` out of a files response, at any nesting."""
    if isinstance(payload, dict):
        url = payload.get("download_url")
        if isinstance(url, str) and url.strip():
            return url
        for value in payload.values():
            found = find_download_url(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_download_url(item)
            if found is not None:
                return found
    return None


def upload_archive(
    config: Config, target: str, archive: bytes, *, reset: bool = False
) -> str | None:
    """Send a ready-to-extract zip archive to one workspace target.

    ``reset`` is reserved for whole-directory releases such as starter code.
    It asks Vocareum to replace the target tree before extracting the archive,
    which prevents stale files from surviving a release.
    """
    section("STEP 2: Upload")
    encoded = base64.b64encode(archive).decode("ascii")
    print(f"PUT {config.part_url()}")
    print(f"target={target} reset={int(reset)}")
    print(f"zip bytes={len(archive)} base64 chars={len(encoded)}")

    item: dict[str, Any] = {"target": target, "zipcontent": encoded}
    if reset:
        item["reset"] = 1
    body = {"content": [item]}
    response = api_json("PUT", config.part_url(), config.api_key, body)
    print(f"Response: {json.dumps(redact_payload(response), sort_keys=True)}")

    return find_transaction_id(response)


def upload(config: Config, target: str, arcname: str, payload: bytes) -> str | None:
    """Wrap one file in a zip and send it without resetting the target."""
    archive = build_zip(arcname, payload)
    print(f"member={arcname}")
    return upload_archive(config, target, archive)


def poll_transaction(config: Config, transaction_id: str) -> str:
    """Poll a transaction to a terminal state and return that state."""
    section("STEP 3: Wait for the write")
    url = config.transaction_url(transaction_id)
    print(f"GET {url}")

    result = wait_for_transaction(
        url,
        config.api_key,
        poll_seconds=TRANSACTION_POLL_SECONDS,
        timeout_seconds=TRANSACTION_TIMEOUT_SECONDS,
        on_state=lambda state: print(f"  state={state}"),
    )
    if result.timed_out:
        print(
            f"  still pending after {TRANSACTION_TIMEOUT_SECONDS}s, "
            "giving up on the poll"
        )
    if isinstance(result.payload, dict):
        message = str(result.payload.get("message", "(none)"))
        print(f"  message={redact_text(message)}")
    return result.state


def verify(config: Config, remote_path: str, expected: str) -> bool:
    """Download the file back and compare its digest with what was sent."""
    section("STEP 4: Verify")
    print(f"filename={remote_path}")

    payload = api_json(
        "GET", config.files_url({"filename": remote_path}), config.api_key
    )
    url = find_download_url(payload)
    if url is None:
        print("No download_url came back, so the upload cannot be verified.")
        return False

    actual = digest(download(url))
    print(f"  expected sha256: {expected}")
    print(f"  actual   sha256: {actual}")
    return actual == expected


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def warn_unknown_target(target: str) -> None:
    """Warn when ``--target`` names a folder nobody has seen on this course."""
    if target in KNOWN_TARGETS:
        return
    print(
        f"WARNING: --target {target!r} is not one of the folders seen on this "
        f"course ({', '.join(sorted(KNOWN_TARGETS))}). If it is wrong, the API "
        "answers with a bodyless HTTP 500 that names nothing."
    )


def course_template_path(env_path: Path, course: str) -> Path:
    """Return the only lab template allowed for a named course."""
    return template_path(env_path, course)


def source_path(config: Config, args: argparse.Namespace) -> Path:
    """Resolve the local source and enforce named-course template ownership."""
    if args.file:
        source = Path(args.file).expanduser()
    elif config.course:
        source = course_template_path(config.env_path, config.course)
    else:
        source = config.env_path.parent / "lab.template"

    arcname = args.name or source.name
    if config.course and arcname == "lab.template":
        expected = course_template_path(config.env_path, config.course)
        if source.resolve() != expected.resolve():
            raise ConfigError(
                f"Course {config.course!r} can upload lab.template only from "
                f"{expected}, not {source}."
            )
    return source


def run_upload(config: Config, args: argparse.Namespace) -> int:
    """Run every step in order and return the process exit code."""
    if not args.dry_run and not config.course:
        raise ConfigError(
            "A Vocareum write requires a named course. Pass --course NAME or "
            "set VOCAREUM_COURSE."
        )

    source = source_path(config, args)
    if not source.is_file():
        raise ConfigError(f"No such file: {source}")

    try:
        payload = source.read_bytes()
    except OSError as error:
        raise ConfigError(f"Could not read {source}: {error}") from error
    if not payload:
        raise ConfigError(f"{source} is empty. Refusing to upload it.")

    arcname = args.name or source.name
    validate_path_segment(args.target, "--target")
    validate_path_segment(arcname, "--name")
    warn_unknown_target(args.target)
    remote_path = f"{args.target}/{arcname}"
    expected = digest(payload)

    print(f"Vocareum API base : {config.api_base}")
    print(f"Selected course   : {config.course or 'legacy root'}")
    print(f"Course/Assignment : {config.course_id}/{config.assignment_id}")
    print(f"Part              : {config.part_id}")
    print(f"Source            : {source} ({len(payload)} bytes)")
    print(f"Destination       : /voc/{remote_path}")
    print(f"Source sha256     : {expected}")

    if args.dry_run:
        section("RESULT")
        print("--dry-run was passed. Nothing was uploaded.")
        return EXIT_OK

    current = show_current_file(config, remote_path)
    if current == expected and not args.force:
        section("RESULT")
        print("The file up there is already byte-identical to the local one.")
        print("Nothing to do. Pass --force to write it anyway.")
        return EXIT_OK

    transaction_id = upload(config, args.target, arcname, payload)
    if transaction_id is None:
        section("RESULT")
        print("No transaction ID came back, so the asynchronous write is unproven.")
        return EXIT_API

    state = poll_transaction(config, transaction_id)
    if state.lower() not in TERMINAL_SUCCESS_STATES:
        section("RESULT")
        print(f"The write ended in state {state!r} rather than success.")
        return EXIT_API

    if args.no_verify:
        section("RESULT")
        print("--no-verify was passed. The write was accepted and not checked.")
        return EXIT_OK

    if not verify(config, remote_path, expected):
        section("RESULT")
        print(
            "The upload was accepted but what came back does not match what was "
            "sent.\n"
            "Do not start a lab against this. Check that --target names the right "
            "folder and that nothing else wrote to the workspace at the same time."
        )
        return EXIT_MISMATCH

    section("RESULT")
    print(f"/voc/{remote_path} now matches {source} byte for byte.")
    if current is not None:
        print(f"It replaced content with sha256 {current}.")
    print("The next Start Lab will use it. Vocareum caches nothing.")
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Upload a file into a Vocareum part workspace and verify it by hash."
        ),
        epilog=(
            "Configuration is read from .env in the repository root. Real "
            "environment variables take precedence. See .env.example."
        ),
    )
    parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help=(
            "Local file to upload. A named course defaults to "
            "courses/NAME/lab.template. No course defaults to lab.template."
        ),
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help=(
            "Workspace folder under /voc to write into. Default: "
            f"{DEFAULT_TARGET}, which is where Vocareum reads lab.template from."
        ),
    )
    parser.add_argument(
        "--name",
        default=None,
        help=(
            "Filename to write on the Vocareum side. Defaults to the local "
            "basename. Use it to upload lab-bedrock.template as lab.template."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be uploaded and stop before writing anything.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Upload even when the remote file already matches. Useful to prove "
            "the write path still works without changing what is deployed."
        ),
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help=(
            "Skip the download-and-compare step. Only useful when the file is "
            "too large to fetch back, since the API cannot otherwise confirm "
            "what it stored."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help="Override the default .env location (repository root).",
    )
    add_course_argument(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. See the module docstring for exit code meanings."""
    args = parse_args(argv)

    default_env = repository_root() / ".env"
    env_path = Path(args.env_file).expanduser() if args.env_file else default_env

    try:
        config = load_config(env_path, args.course)
        return run_upload(config, args)
    except ConfigError as error:
        print("Configuration error.")
        print(error)
        return EXIT_CONFIG
    except ApiError as error:
        section("RESULT")
        print(f"The upload could not complete: {error}")
        return EXIT_API


if __name__ == "__main__":
    raise SystemExit(main())
