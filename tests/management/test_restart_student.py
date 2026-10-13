"""Tests for the email-targeted, destructive-by-opt-in session restart tool."""

from __future__ import annotations

import argparse

import pytest

from vocareum_tools import restart_student
from vocareum_tools.endall import Student
from vocareum_tools.endlab import SessionFacts
from vocareum_tools.endpoints import DEFAULT_API_BASE

CONFIG = restart_student.Config(
    api_base=DEFAULT_API_BASE,
    course_id="course",
    assignment_id="assignment",
    part_id="part",
    api_key="token",
)

LIVE = SessionFacts(
    state="Ready",
    status="active",
    account_id="111111111111",
    total_spend="0",
    allowed_spend="200",
    session_end="",
    pool=[],
)
TERMINATED = SessionFacts(
    state="Terminated",
    status="inactive",
    account_id="",
    total_spend="0",
    allowed_spend="200",
    session_end="",
    pool=[],
)
FRESH = SessionFacts(
    state="Ready",
    status="active",
    account_id="222222222222",
    total_spend="0",
    allowed_spend="200",
    session_end="",
    pool=[],
)
STARTING = SessionFacts(
    state="Starting",
    status="active",
    account_id="222222222222",
    total_spend="0",
    allowed_spend="200",
    session_end="",
    pool=[],
)


def student(email: str = "student@example.com", *, dropped: bool = False) -> Student:
    """Return one roster student suitable for a focused session test."""
    return Student("student-id", "Student", email, "student", dropped)


def args(yes: bool) -> argparse.Namespace:
    """Return the parsed arguments run() consumes."""
    return argparse.Namespace(student_email="student@example.com", yes=yes)


def test_select_student_matches_email_case_insensitively() -> None:
    """Roster email casing must not make a known student unaddressable."""
    selected = restart_student.select_student(
        [student("Student@Example.Com")], "student@example.com"
    )

    assert selected.user_id == "student-id"


@pytest.mark.parametrize(
    "students",
    [[], [student(), student()]],
)
def test_select_student_refuses_zero_or_multiple_matches(
    students: list[Student],
) -> None:
    """A destructive command must never choose among ambiguous roster rows."""
    with pytest.raises(restart_student.RestartError, match="matched"):
        restart_student.select_student(students, "student@example.com")


def test_select_student_refuses_a_dropped_student() -> None:
    """A dropped roster row must not be a restart target."""
    with pytest.raises(restart_student.RestartError, match="matched 0"):
        restart_student.select_student([student(dropped=True)], "student@example.com")


def test_session_is_ready_distinguishes_starting_from_ready() -> None:
    """An allocated account alone does not mean the student workspace is ready."""
    assert restart_student.session_is_ready(FRESH)
    assert not restart_student.session_is_ready(STARTING)


def test_dry_run_never_ends_or_starts_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default command only identifies the intended account and action."""
    monkeypatch.setattr(restart_student, "fetch_roster", lambda config: [student()])
    monkeypatch.setattr(restart_student, "fetch_facts", lambda config, target: LIVE)
    monkeypatch.setattr(
        restart_student,
        "end_session",
        lambda config, target: pytest.fail("dry run ended a session"),
    )
    monkeypatch.setattr(
        restart_student,
        "start_session",
        lambda config, target: pytest.fail("dry run started a session"),
    )

    assert restart_student.run(CONFIG, args(yes=False)) == 0


def test_yes_ends_then_starts_the_exact_roster_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live session is confirmed down before one fresh session is requested."""
    calls: list[str] = []
    target = student()
    facts = iter([LIVE, TERMINATED])

    monkeypatch.setattr(restart_student, "fetch_roster", lambda config: [target])
    monkeypatch.setattr(
        restart_student, "fetch_facts", lambda config, found: next(facts)
    )
    monkeypatch.setattr(
        restart_student,
        "end_session",
        lambda config, found: calls.append(f"end:{found.user_id}"),
    )
    monkeypatch.setattr(
        restart_student,
        "start_session",
        lambda config, found: calls.append(f"start:{found.user_id}") or FRESH,
    )

    assert restart_student.run(CONFIG, args(yes=True)) == 0
    assert calls == ["end:student-id", "start:student-id"]


def test_yes_refuses_to_start_when_end_is_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 end response is not enough to risk a second live session."""
    calls: list[str] = []
    facts = iter([LIVE, LIVE])

    monkeypatch.setattr(restart_student, "fetch_roster", lambda config: [student()])
    monkeypatch.setattr(
        restart_student, "fetch_facts", lambda config, found: next(facts)
    )
    monkeypatch.setattr(
        restart_student,
        "end_session",
        lambda config, found: calls.append("end"),
    )
    monkeypatch.setattr(
        restart_student,
        "start_session",
        lambda config, found: calls.append("start") or FRESH,
    )

    with pytest.raises(restart_student.RestartError, match="did not report inactive"):
        restart_student.run(CONFIG, args(yes=True))

    assert calls == ["end"]
