#!/usr/bin/env -S uv run
"""Remove every enrolled student from one Vocareum course.

One-off cleanup for a course whose session has already ended (there is no
bulk-delete endpoint: ``DELETE /courses/{id}/users`` with no ``userid``
answers "Invalid Request - userid missing", confirmed live on
2026-08-27). This pages the roster, then issues one
``DELETE /courses/{id}/users/{user_id}`` per student.

Defaults to listing who would be removed. Pass ``--yes`` to actually delete.
Instructor/TA rows are left alone; only ``role == "student"`` rows are
touched, and a row already ``dropped: true`` is skipped since the API answers
a second delete with "not enrolled" rather than succeeding twice.

**Sweep and end every live session before running this with ``--yes``, never
after.** Unenrolling is a one-way door: confirmed live on
2026-08-27, every removed student's ``GET .../resources/{userId}`` then
answers ``HTTP 400 "The user is not enrolled as a student in the course"``,
even though ``?include_dropped=true`` still lists them on the roster. That
endpoint is what both ``endall.py`` (to end a session) and ``sweep.py`` (to
read the session's temporary AWS credentials) depend on, so once a student is
removed here, their AWS account is no longer reachable through this API at
all. Whatever it still holds runs to Vocareum's own session_length and is
reclaimed by the pool on its own; nothing in this repository can inspect or
stop it after the fact. Run ``endall.py`` (and sweep any live account it
reports) first, and only remove students once that confirms the cohort down.

    uv run scripts/remove_course_students.py --course-id COURSE_ID
    uv run scripts/remove_course_students.py --course-id COURSE_ID --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vocareum_tools.client import ApiError, request_json
from vocareum_tools.config import Environment, repository_root
from vocareum_tools.endpoints import PartEndpoints
from vocareum_tools.security import redact_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-id", required=True, help="Vocareum course id")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this flag, only lists who would be removed.",
    )
    return parser.parse_args()


def fetch_roster(endpoints: PartEndpoints, api_key: str) -> list[dict]:
    """Return every roster row, paging until ``total_records`` is covered."""
    users: list[dict] = []
    page = 0
    total_records = None
    while total_records is None or len(users) < total_records:
        payload = request_json("GET", endpoints.roster_url(page=page), api_key)
        total_records = payload.get("total_records", 0)
        page_users = payload.get("users", [])
        if not page_users:
            break
        users.extend(page_users)
        page += 1
    return users


def main() -> int:
    args = parse_args()
    root = repository_root()
    env = Environment.from_file(root / ".env")
    env.require(("VOCAREUM_API_KEY",))
    api_key = env.get("VOCAREUM_API_KEY")
    api_base = env.get("VOCAREUM_API_BASE") or "https://api.vocareum.com/api/v2"

    endpoints = PartEndpoints(
        api_base=api_base,
        course_id=args.course_id,
        assignment_id="0",
        part_id="0",
    )

    roster = fetch_roster(endpoints, api_key)
    students = [row for row in roster if row.get("role") == "student"]
    already_dropped = [row for row in students if row.get("dropped")]
    to_remove = [row for row in students if not row.get("dropped")]

    print(
        f"Course {args.course_id}: {len(roster)} roster rows, {len(students)} students."
    )
    if already_dropped:
        print(f"Already dropped, skipping: {len(already_dropped)}")
    print(f"To remove: {len(to_remove)}")
    for row in to_remove:
        print(f"  id={row.get('id')}  name={row.get('name')}  email={row.get('email')}")

    if not to_remove:
        print("Nothing to do.")
        return 0

    if not args.yes:
        print("\nDry run only. Re-run with --yes to delete the students listed above.")
        return 0

    failures = 0
    for row in to_remove:
        user_id = row.get("id")
        url = f"{endpoints.roster_url()}/{user_id}"
        try:
            request_json("DELETE", url, api_key)
            print(f"Removed id={user_id} name={row.get('name')}")
        except ApiError as error:
            failures += 1
            print(
                f"FAILED id={user_id} name={row.get('name')}: {redact_text(str(error))}"
            )

    print(f"\nDone. Removed {len(to_remove) - failures}/{len(to_remove)}.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
