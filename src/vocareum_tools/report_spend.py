"""Report AWS spend across every student in a course, from the roster.

``vocareum-diagnose`` reads ``totalspend``/``allowedspend`` for exactly one
student, whichever ``VOCAREUM_USER_ID`` names in ``.env``. That answers "what
did I spend" for one diagnostic session. It cannot answer "what did the
cohort spend," and an instructor's own id is not even a valid argument to
try: ``/resources/{userId}`` answers HTTP 400 "The user is not enrolled as a
student in the course" for an id that never enrolled as one.

There is no endpoint that answers the cohort figure directly. The one
Vocareum route that spans more than one user, ``GET /orgs/{orgid}/aws_usage``
(see ``docs/vocareum-api.md``), needs Organization Administrator rights this
package has never exercised. The only measured path to a cohort total is the
same one ``endall.py`` already walks to end sessions: read the roster, then
read each student's own session record, which is where ``totalspend`` lives.

This module is that walk with no ``DELETE`` at the end. It reuses
``endall.fetch_roster`` and ``endlab.read_facts`` rather than re-deriving
either, for the reason ``endall.py``'s docstring already gives: the
terminated-session shape, ``aws_data`` surviving as a populated dict full of
empty strings, is a trap worth hitting once, not once per new tool.

**A total after an unreadable student is a lower bound, not the cohort's
spend.** One failed read must not silently understate the rest, so the exit
code says so and the printed total is labeled accordingly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from .client import ApiError, request_json
from .config import ConfigError, add_course_argument, repository_root
from .diagnose import parse_amount
from .endall import Config, Student, fetch_roster, load_config, session_config
from .endlab import SessionFacts, read_facts
from .output import section


@dataclass
class Row:
    """One student's spend, or the reason it could not be read."""

    student: Student
    facts: SessionFacts | None = None
    error: str = ""

    @property
    def spend(self) -> float | None:
        """Return this student's total spend, or ``None`` if unreadable."""
        return parse_amount(self.facts.total_spend) if self.facts else None

    @property
    def allowed(self) -> float | None:
        """Return this student's spend ceiling, or ``None`` if unreadable."""
        return parse_amount(self.facts.allowed_spend) if self.facts else None


def fetch_facts(config: Config, student: Student) -> SessionFacts:
    """Read one student's session record."""
    url = session_config(config, student).resource_url()
    return read_facts(request_json("GET", url, config.api_key))


def survey(config: Config, students: list[Student]) -> list[Row]:
    """Read every student's spend, recording failures instead of raising."""
    rows = []
    for student in students:
        row = Row(student=student)
        try:
            row.facts = fetch_facts(config, student)
        except ApiError as error:
            row.error = str(error).splitlines()[0]
        rows.append(row)
    return rows


def print_rows(rows: list[Row]) -> None:
    """Print one line per student: spend, ceiling, and dropped/error markers."""
    for row in rows:
        label = row.student.label
        if row.error:
            print(f"  {label:<40} unreadable  {row.error}")
            continue
        spend_text = f"${row.spend:.2f}" if row.spend is not None else "?"
        allowed_text = f"${row.allowed:.2f}" if row.allowed is not None else "?"
        marker = "  (dropped)" if row.student.dropped else ""
        print(f"  {label:<40} {spend_text:>10} of {allowed_text}{marker}")


def summarize(rows: list[Row]) -> tuple[float, int]:
    """Return the total spend across readable rows, and how many were not."""
    total = 0.0
    unreadable = 0
    for row in rows:
        if row.spend is None:
            unreadable += 1
        else:
            total += row.spend
    return total, unreadable


def run(config: Config) -> int:
    """Read the roster, then read every student's spend."""
    section("STEP 1: Read the course roster")
    students = fetch_roster(config)
    print(f"\n{len(students)} students on the course.")
    if not students:
        print("Nobody to report on.")
        return 0

    section("STEP 2: Read every student's spend")
    rows = survey(config, students)
    print_rows(rows)

    total, unreadable = summarize(rows)
    readable = len(rows) - unreadable

    section("RESULT")
    print(f"Total spend across {readable} readable student(s): ${total:.2f}")
    if unreadable:
        print(
            f"\n{unreadable} student session(s) could not be read, so this "
            "total is a lower bound, not the cohort's true spend."
        )
        return 1

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Report AWS spend for every student on a Vocareum course part, "
            "read from the course roster and each student's own session "
            "record. Read-only: starts and ends nothing."
        )
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
    """Entry point for ``vocareum-report-spend``.

    Exit codes:
        0  Every student's spend was read.
        1  At least one student's session could not be read, so the printed
           total is a lower bound.
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
        return run(config)
    except ApiError as error:
        print(f"\nAPI error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
