"""Restart one student's cloud session from the course roster.

This rotates the student's AWS account, but preserves their ``/voc/work``
files. Vocareum copies ``/voc/startercode`` only on a student's first access;
to replace an existing work area, use Vocareum's Reset workarea action. Ending
releases the student's AWS account, so this command is a dry run unless
``--yes`` is passed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from .client import (
    CREDENTIAL_POLL_SECONDS,
    CREDENTIAL_TIMEOUT_SECONDS,
    TERMINAL_SUCCESS_STATES,
    ApiError,
    find_transaction_id,
    poll_transaction,
    request_json,
    wait_for_payload,
)
from .config import (
    VOCAREUM_PART_VARS,
    ConfigError,
    Environment,
    add_course_argument,
    repository_root,
)
from .endall import Student, fetch_roster, session_config
from .endlab import (
    END_ACTION,
    INACTIVE_STATUS,
    TERMINATED_STATE,
    SessionFacts,
    read_facts,
)
from .endpoints import DEFAULT_API_BASE, PartEndpoints
from .output import section


class RestartError(RuntimeError):
    """A refusal that avoids changing the wrong student's session."""


READY_STATE = "ready"
ACTIVE_STATUS = "active"


@dataclass(frozen=True)
class Config(PartEndpoints):
    """The API coordinates needed to address a student session by roster ID."""

    api_key: str


def load_config(env_path: Path, course: str | None = None) -> Config:
    """Load the selected course without requiring a preconfigured student ID."""
    environment = Environment.from_file(env_path, course)
    environment.require(VOCAREUM_PART_VARS)
    return Config(
        api_key=environment.get("VOCAREUM_API_KEY"),
        course_id=environment.get("VOCAREUM_COURSE_ID"),
        assignment_id=environment.get("VOCAREUM_ASSIGNMENT_ID"),
        part_id=environment.get("VOCAREUM_PART_ID"),
        api_base=(environment.get("VOCAREUM_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
    )


def select_student(students: list[Student], email: str) -> Student:
    """Return the one active student whose roster email matches ``email``."""
    wanted = email.strip().casefold()
    matches = [
        student
        for student in students
        if student.email.strip().casefold() == wanted
        and student.role.casefold() == "student"
        and not student.dropped
    ]
    if len(matches) != 1:
        raise RestartError(
            f"--student-email matched {len(matches)} active students: {email!r}. "
            "Refusing to choose a session."
        )
    return matches[0]


def fetch_facts(config: Config, student: Student) -> SessionFacts:
    """Read a student's session without exposing the response's credentials."""
    endpoint = session_config(config, student)
    payload = request_json("GET", endpoint.resource_url(), config.api_key)
    return read_facts(payload)


def end_session(config: Config, student: Student) -> None:
    """End a live session using Vocareum's measured ``action: end`` shape."""
    endpoint = session_config(config, student)
    request_json(
        "DELETE", endpoint.resource_url(), config.api_key, body={"action": END_ACTION}
    )


def session_is_ready(facts: SessionFacts) -> bool:
    """Return true only once Vocareum has finished starting the cloud session."""
    return (
        bool(facts.account_id)
        and facts.state.casefold() == READY_STATE
        and facts.status.casefold() == ACTIVE_STATUS
    )


def wait_for_live_session(config: Config, student: Student) -> SessionFacts:
    """Wait until the new session has an allocated, ready AWS account."""
    endpoint = session_config(config, student)
    _, facts = wait_for_payload(
        lambda: request_json("GET", endpoint.resource_url(), config.api_key),
        lambda payload: (
            facts if session_is_ready(facts := read_facts(payload)) else None
        ),
        poll_seconds=CREDENTIAL_POLL_SECONDS,
        timeout_seconds=CREDENTIAL_TIMEOUT_SECONDS,
        wait=True,
        on_retry=lambda seconds: print(f"  session not ready; retrying in {seconds}s"),
    )
    if facts is None:
        raise RestartError(
            "The new session did not become active within "
            f"{CREDENTIAL_TIMEOUT_SECONDS}s."
        )
    return facts


def start_session(config: Config, student: Student) -> SessionFacts:
    """Request a new session and wait until its cloud resources are ready."""
    endpoint = session_config(config, student)
    payload = request_json("PUT", endpoint.resource_url(), config.api_key)
    transaction_id = find_transaction_id(payload)
    if transaction_id is not None:
        result = poll_transaction(
            config.transaction_url(transaction_id),
            config.api_key,
            poll_seconds=CREDENTIAL_POLL_SECONDS,
            timeout_seconds=CREDENTIAL_TIMEOUT_SECONDS,
            on_state=lambda state: print(f"  transaction state={state}"),
        )
        if result.timed_out:
            raise RestartError(
                f"Session start was still pending after {CREDENTIAL_TIMEOUT_SECONDS}s."
            )
        if result.state.lower() not in TERMINAL_SUCCESS_STATES:
            raise RestartError(f"Session start ended in state {result.state!r}.")
    return wait_for_live_session(config, student)


def print_facts(facts: SessionFacts) -> None:
    """Print the non-secret facts that identify the session being replaced."""
    print(f"  state        = {facts.state or '(absent)'}")
    print(f"  cloud status = {facts.status or '(absent)'}")
    print(f"  account_id   = {facts.account_id or '(no active account)'}")


def run(config: Config, args: argparse.Namespace) -> int:
    """Dry-run or restart one roster-matched student's cloud session."""
    section("STEP 1: Resolve the student")
    student = select_student(fetch_roster(config), args.student_email)
    print(f"Student: {student.user_id} {student.email}")

    section("STEP 2: Read the current session")
    before = fetch_facts(config, student)
    print_facts(before)

    if not args.yes:
        section("DRY RUN")
        if before.live:
            print("Nothing was changed. --yes will end this session, releasing its")
            print(
                "AWS account, then start a fresh session. It preserves /voc/work files."
            )
        else:
            print("Nothing was changed. --yes will start a fresh session and preserve")
            print("the student's /voc/work files.")
        return 0

    if before.live:
        section("STEP 3: End the current session")
        end_session(config, student)
        after_end = fetch_facts(config, student)
        if after_end.state != TERMINATED_STATE and after_end.status != INACTIVE_STATUS:
            raise RestartError(
                "The end call was accepted but the session did not report inactive. "
                "Refusing to start another session."
            )
        print("Current session ended.")
    else:
        print("Current session is already down; no end request was sent.")

    section("STEP 4: Start the fresh session")
    fresh = start_session(config, student)
    print_facts(fresh)
    print("Fresh cloud session is ready. The student's /voc/work files were preserved.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the small, deliberately explicit operator interface."""
    parser = argparse.ArgumentParser(
        description="End and restart one student's Vocareum cloud session."
    )
    parser.add_argument(
        "--student-email",
        required=True,
        help="Exact email address of one active student in the course roster.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually end and restart the session. The default is a dry run.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the base .env file.",
    )
    add_course_argument(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the operator command and report safe refusals without a traceback."""
    args = parse_args(argv)
    try:
        return run(load_config(args.env_file, args.course), args)
    except (ApiError, ConfigError, RestartError) as error:
        print(f"error: {error}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
