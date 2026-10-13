"""End every student's lab session on a part, from the course roster.

``endlab.py`` ends one session, the one named by ``VOCAREUM_USER_ID`` in
``.env``. That is the right tool for an operator checking their own work and the
wrong one for a workshop, because a real cohort is thirty students and there is
no End Lab button in the student's AWS view. Nobody ends those sessions. They
run to the four-hour ``session_length`` and expire on their own, each one
holding a pool account the whole time.

**There is no bulk endpoint.** Every session call in the v2 surface is
``/resources/{userId}``, so ending a cohort is a roster loop over the same
``DELETE`` that ``endlab.py`` already sends. This module is that loop and
nothing more. It reuses ``endlab.read_facts`` and ``endlab.SessionFacts`` rather
than re-reading the payload its own way, because the terminated-session trap
those encode, ``aws_data`` surviving as a populated dict full of empty strings,
is exactly the trap a fan-out would hit thirty times instead of once.

Four things here are load-bearing rather than tidy:

**The roster page parameter is zero-indexed.** ``endpoints.roster_url`` carries
the measurement. The consequence for this module is that page 0 is a real page,
so the loop starts there, and it stops on the first empty page rather than
trusting an arithmetic guess at the page count.

**``total_records`` is the completeness check, and it is checked.** The roster
answers with the true count on every page, including pages past the end. A
fan-out that ends fewer sessions than there are students, and says nothing, is
the same silence the account sweep was written against: it reads as "the cohort
is down" when half of it is still up. Collecting fewer rows than
``total_records`` is an error here, not a warning.

**One student's failure must not end the run.** A cohort where student 7 returns
a 500 still has students 8 through 30 holding accounts. Every call is caught per
student, recorded as an outcome, and the loop continues. The exit code reports
that something failed; the report says which.

**Ending a session gives up its AWS account, so anything left in it becomes
unreachable.** That is per-student here rather than once. The dry run prints the
sweep command for every live account before it prints anything else, because
after ``--yes`` there is no account left to sweep.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import ApiError, request_json
from .config import (
    VOCAREUM_PART_VARS,
    ConfigError,
    Environment,
    add_course_argument,
    repository_root,
)
from .endlab import (
    END_ACTION,
    INACTIVE_STATUS,
    TERMINATED_STATE,
    SessionFacts,
    read_facts,
)
from .endpoints import DEFAULT_API_BASE, PartEndpoints, SessionEndpoints
from .output import section

# A roster page loop needs a stop that does not depend on trusting the server's
# arithmetic. This is the backstop: at the observed page size a course would
# need tens of thousands of students to reach it, so hitting it means the
# endpoint is paging in a way this code does not model.
MAX_ROSTER_PAGES = 100


@dataclass(frozen=True)
class Config(PartEndpoints):
    """Everything this command needs, resolved from the environment.

    ``PartEndpoints`` rather than ``SessionEndpoints``: the roster supplies the
    user ids, so ``VOCAREUM_USER_ID`` is not required to end a cohort. Requiring
    it would tie a fleet operation to whichever diagnostic student happens to be
    in ``.env``.
    """

    api_key: str


@dataclass(frozen=True)
class Student:
    """One roster row, reduced to the fields that decide what to do with it."""

    user_id: str
    name: str
    email: str
    role: str
    dropped: bool

    @property
    def label(self) -> str:
        """Return a one-line identity for reports."""
        who = self.name or self.email or "(unnamed)"
        return f"{self.user_id} {who}"


@dataclass
class Outcome:
    """What happened to one student, whether or not anything was attempted."""

    student: Student
    facts: SessionFacts | None = None
    status: str = "pending"
    detail: str = ""
    after: SessionFacts | None = None

    @property
    def account_id(self) -> str:
        """Return the account recorded before the end call, if it was read."""
        return self.facts.account_id if self.facts else ""


def load_config(env_path: Path, course: str | None = None) -> Config:
    """Build a :class:`Config` from real environment variables and ``.env``."""
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
    )


def read_students(payload: Any) -> tuple[list[Student], int]:
    """Pull the students and the true total out of one roster page.

    Returns the rows on this page and ``total_records`` for the whole roster.
    Every field is optional, and a row with no ``id`` is dropped rather than
    carried as an empty user id: ``/resources/`` with nothing after it is a
    different endpoint, and sending it a ``DELETE`` is not a thing to do by
    accident.
    """
    if not isinstance(payload, dict):
        return [], 0

    rows = payload.get("users") or []
    if not isinstance(rows, list):
        rows = []

    students = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        user_id = str(row.get("id", "")).strip()
        if not user_id:
            continue
        students.append(
            Student(
                user_id=user_id,
                name=str(row.get("name", "")),
                email=str(row.get("email", "")),
                role=str(row.get("role", "")),
                dropped=bool(row.get("dropped", False)),
            )
        )

    try:
        total = int(payload.get("total_records", 0))
    except (TypeError, ValueError):
        total = 0
    return students, total


def fetch_roster(config: Config) -> list[Student]:
    """Read every student on the course, paging from zero.

    Stops on the first empty page rather than computing a page count, and then
    refuses to return a short roster. See the module docstring for why a silent
    short read is the failure worth guarding.

    Always passes ``include_dropped=true``. A student unenrolled by
    ``remove_course_students.py`` still holds a session, and the default
    roster view silently answers ``total_records: 0`` for a course that has
    been fully unenrolled, which reads as "nothing to end" for a cohort that
    is not actually down.
    """
    students: list[Student] = []
    seen: set[str] = set()
    total = 0

    for page in range(MAX_ROSTER_PAGES):
        # Page 0 is sent without the page parameter. The bare URL is the shape
        # every measurement in docs/vocareum-api.md used, so it is the one
        # known good.
        url = config.roster_url(None if page == 0 else page, include_dropped=True)
        print(f"GET {url}")
        found, total = read_students(request_json("GET", url, config.api_key))
        if not found:
            break
        for student in found:
            if student.user_id not in seen:
                seen.add(student.user_id)
                students.append(student)
        if len(students) >= total:
            break
    else:
        raise ApiError(
            f"Roster paging did not terminate within {MAX_ROSTER_PAGES} pages. "
            "The endpoint is paging in a way this tool does not model, so the "
            "roster cannot be trusted to be complete."
        )

    if len(students) < total:
        raise ApiError(
            f"Roster is short: collected {len(students)} students but "
            f"total_records says {total}. Ending only the ones that were read "
            "would leave the rest up and report the cohort as down, so nothing "
            "was attempted."
        )

    return students


def session_config(config: Config, student: Student) -> SessionEndpoints:
    """Return the per-student session endpoints for a roster row."""
    return SessionEndpoints(
        api_base=config.api_base,
        course_id=config.course_id,
        assignment_id=config.assignment_id,
        part_id=config.part_id,
        user_id=student.user_id,
    )


def fetch_facts(config: Config, student: Student) -> SessionFacts:
    """Read one student's session record."""
    url = session_config(config, student).resource_url()
    return read_facts(request_json("GET", url, config.api_key))


def end_one(config: Config, student: Student) -> Any:
    """Send the end call for one student and return the decoded response.

    No transaction poll. This endpoint is synchronous, unlike every other write
    in this package.
    """
    url = session_config(config, student).resource_url()
    return request_json("DELETE", url, config.api_key, body={"action": END_ACTION})


def survey(config: Config, students: list[Student]) -> list[Outcome]:
    """Read every student's session, recording failures instead of raising."""
    outcomes = []
    for student in students:
        outcome = Outcome(student=student)
        try:
            outcome.facts = fetch_facts(config, student)
        except ApiError as error:
            outcome.status = "unreadable"
            outcome.detail = str(error).splitlines()[0]
        else:
            outcome.status = "live" if outcome.facts.live else "already down"
        outcomes.append(outcome)
    return outcomes


def print_survey(outcomes: list[Outcome]) -> None:
    """Print one line per student, with the account each live session holds."""
    for outcome in outcomes:
        line = f"  {outcome.student.label:<40} {outcome.status}"
        if outcome.account_id:
            line += f"  account={outcome.account_id}"
        if outcome.facts and outcome.facts.total_spend:
            line += f"  spend={outcome.facts.total_spend}"
        if outcome.student.dropped:
            line += "  (dropped)"
        if outcome.detail:
            line += f"\n      {outcome.detail}"
        print(line)


def print_sweep_commands(live: list[Outcome]) -> None:
    """Print the sweep command for every account about to be given up."""
    print("Ending a session gives up its AWS account. The pool wipes the released")
    print("member and allocates a different one next time, so anything left in it")
    print("becomes unreachable rather than merely untidy. Sweep first:")
    print()
    for outcome in live:
        print(f"  uv run vocareum-sweep --no-start --account {outcome.account_id}")
    print()
    print("Each of those needs that student's credentials, which only exist while")
    print("their session is up. This tool does not sweep. It ends sessions.")


def end_all(config: Config, live: list[Outcome]) -> None:
    """End every live session, then confirm each state actually moved."""
    for outcome in live:
        label = outcome.student.label
        try:
            end_one(config, outcome.student)
        except ApiError as error:
            outcome.status = "failed"
            outcome.detail = str(error).splitlines()[0]
            print(f"  {label:<40} FAILED  {outcome.detail}")
            continue

        # A 200 says the call was accepted, not that the session is down.
        try:
            outcome.after = fetch_facts(config, outcome.student)
        except ApiError as error:
            outcome.status = "unconfirmed"
            outcome.detail = f"ended, but re-read failed: {str(error).splitlines()[0]}"
            print(f"  {label:<40} UNCONFIRMED  {outcome.detail}")
            continue

        down = (
            outcome.after.state == TERMINATED_STATE
            or outcome.after.status == INACTIVE_STATUS
        )
        if down:
            outcome.status = "ended"
            print(f"  {label:<40} ended    account {outcome.account_id} given up")
        else:
            outcome.status = "unconfirmed"
            outcome.detail = (
                f"call returned but state is still {outcome.after.state or '(absent)'}"
            )
            print(f"  {label:<40} UNCONFIRMED  {outcome.detail}")


def summarize(outcomes: list[Outcome]) -> dict[str, int]:
    """Count outcomes by status."""
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return counts


def run(config: Config, args: argparse.Namespace) -> int:
    """Read the roster, survey every session, and end the live ones."""
    section("STEP 1: Read the course roster")
    students = fetch_roster(config)
    excluded = set(args.exclude or ())
    kept = [student for student in students if student.user_id not in excluded]
    skipped = len(students) - len(kept)
    print(f"\n{len(students)} students on the course.")
    if skipped:
        print(f"{skipped} excluded by --exclude, leaving {len(kept)}.")
    if not kept:
        print("Nobody to act on.")
        return 0

    section("STEP 2: Read every session before ending anything")
    print("Recorded now because account_id and the pool empty on termination.")
    outcomes = survey(config, kept)
    print_survey(outcomes)

    live = [outcome for outcome in outcomes if outcome.status == "live"]
    unreadable = [outcome for outcome in outcomes if outcome.status == "unreadable"]

    if not live:
        section("RESULT")
        print("No live sessions. Nothing to end.")
        return 1 if unreadable else 0

    if not args.yes:
        section("DRY RUN")
        print("Nothing was ended. A dry run is the default.\n")
        print_sweep_commands(live)
        print(f"Then re-run with --yes to end {len(live)} session(s).")
        return 0

    section(f"STEP 3: End {len(live)} live session(s)")
    end_all(config, live)

    section("RESULT")
    counts = summarize(outcomes)
    for status in sorted(counts):
        print(f"  {status:<14} {counts[status]}")

    failed = counts.get("failed", 0) + counts.get("unconfirmed", 0)
    if failed:
        print(f"\n{failed} session(s) did not confirm as down. Re-read them before")
        print("assuming the cohort is clear. Their accounts may still be held.")
        return 1
    if unreadable:
        print("\nEvery session that could be read was ended, but some could not be")
        print("read at all, so the cohort is not proven clear.")
        return 1

    print("\nEvery live session was ended and confirmed down.")
    print("CloudFormation tears each stack down asynchronously over the next few")
    print("minutes, so a stack listing taken now can still show them alive.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "End every student's lab session on a Vocareum part, read from the "
            "course roster. Prints what it would give up and changes nothing "
            "unless --yes is passed."
        )
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Actually end the sessions. Without it this is a dry run and the "
            "default. Sweep each account first: once a session ends its account "
            "is released and cannot be inspected."
        ),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        metavar="USER_ID",
        help=(
            "Leave this student's session alone. Repeatable. Use it to keep "
            "your own diagnostic session up while ending a cohort."
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the .env file. Defaults to the repository root.",
    )
    add_course_argument(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``vocareum-end-all``.

    Exit codes:
        0  Nothing needed ending, or everything was ended and confirmed down.
        1  Something was left in an unknown state, so the cohort is not proven
           clear.
        2  Configuration error, so nothing was attempted.
    """
    args = parse_args(argv)
    try:
        config = load_config(args.env_file, args.course)
    except ConfigError as error:
        print(f"Configuration error: {error}")
        return 2

    print(f"Vocareum API base : {config.api_base}")
    print(f"Course/Assignment : {config.course_id}/{config.assignment_id}")
    print(f"Part              : {config.part_id}")

    try:
        return run(config, args)
    except ApiError as error:
        print(f"\nAPI error: {error}")
        return 1
