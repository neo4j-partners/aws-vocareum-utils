"""Tests for the cohort end-session command, driven entirely by recorded payloads.

Ending a cohort is irreversible and gives up one AWS account per student, so the
failures worth testing are the ones where the tool acts on the wrong set: a
roster read short and reported as complete, a terminated session mistaken for a
live one, or one student's error taking the other twenty-nine down with it.
None of those needs an API call. The only inputs are a roster payload, a
resources payload per student, and one flag.

The roster shapes here are the ones measured against the live course on
2026-08-06, including the zero-indexed page trap: ``page=1`` answers with an
empty ``users`` array and a truthful ``total_records``, so an off-by-one reports
an empty course rather than failing.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from vocareum_tools import endall
from vocareum_tools.client import ApiError
from vocareum_tools.endpoints import DEFAULT_API_BASE

CONFIG = endall.Config(
    api_base=DEFAULT_API_BASE,
    course_id="100001",
    assignment_id="a1",
    part_id="p1",
    api_key="token",
)


def roster_page(rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    """Build a roster payload in the shape the live endpoint returns."""
    return {"status": "success", "users": rows, "total_records": total}


def student_row(user_id: str, name: str = "", dropped: bool = False) -> dict[str, Any]:
    """Build one roster row in the shape the live endpoint returns."""
    return {
        "id": user_id,
        "courseid": "100001",
        "role": "student",
        "name": name or f"Test Student {user_id}",
        "email": f"student{user_id}@example.com",
        "lti_user_id": None,
        "dropped": dropped,
    }


def session_payload(
    account_id: str = "111111111111",
    state: str = "Ready",
    status: str = "active",
    spend: str = "0.01",
) -> dict[str, Any]:
    """Build a resources payload. Empty account_id is how a dead session reads."""
    return {
        "status": "success",
        "resources": [
            {
                "state": state,
                "sessionend": "2026-08-06T12:35:26-0700",
                "cloud": [
                    {
                        "status": status,
                        "vendor": "aws",
                        "aws_data": {
                            "account_id": account_id,
                            "totalspend": spend,
                            "allowedspend": "200",
                            "aws_access_key_id": "ASIAEXAMPLE" if account_id else "",
                            "aws_linked_accounts": [],
                        },
                    }
                ],
            }
        ],
    }


# A terminated session keeps aws_data as a populated, truthy dict holding empty
# strings. This is the payload that makes `if aws_data:` report a dead session
# as live, and the fan-out would hit it once per student.
TERMINATED = session_payload(account_id="", state="Terminated", status="inactive")


class FakeApi:
    """Record every request and answer from a scripted routing table."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, method: str, url: str, api_key: str, body: Any = None, **kwargs: Any
    ) -> Any:
        self.calls.append((method, url))
        try:
            answer = self.routes[(method, url)]
        except KeyError:  # pragma: no cover - a routing mistake in a test
            raise AssertionError(f"unexpected call {method} {url}") from None
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer()
        return answer

    def deletes(self) -> list[str]:
        """Return the URLs this run sent a DELETE to."""
        return [url for method, url in self.calls if method == "DELETE"]


def resource_url(user_id: str) -> str:
    """Return the session URL for a user id under CONFIG."""
    return f"{CONFIG.part_url()}/resources/{user_id}"


def args(yes: bool = False, exclude: list[str] | None = None) -> argparse.Namespace:
    """Build the parsed-args object ``run`` expects."""
    return argparse.Namespace(yes=yes, exclude=exclude, env_file=None)


# ---------------------------------------------------------------------------
# Roster URL and paging
# ---------------------------------------------------------------------------


def test_roster_url_omits_the_page_parameter_for_the_first_page() -> None:
    """Page 0 is sent bare, which is the shape every measurement used."""
    assert CONFIG.roster_url() == f"{CONFIG.course_url()}/users"
    assert CONFIG.roster_url(None) == f"{CONFIG.course_url()}/users"


def test_roster_url_is_zero_indexed() -> None:
    """page=1 is the second page, not the first. See endpoints.roster_url."""
    assert CONFIG.roster_url(1) == f"{CONFIG.course_url()}/users?page=1"


def test_roster_url_include_dropped() -> None:
    """A dropped (unenrolled) student is invisible without this parameter.

    Measured 2026-08-27 against a course after unenrolling its roster:
    the default view answered total_records: 0 for a course that still held
    113 dropped students.
    """
    assert (
        CONFIG.roster_url(include_dropped=True)
        == f"{CONFIG.course_url()}/users?include_dropped=true"
    )
    assert (
        CONFIG.roster_url(1, include_dropped=True)
        == f"{CONFIG.course_url()}/users?page=1&include_dropped=true"
    )


def test_fetch_roster_always_includes_dropped_students(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_roster asks for dropped students, or an emptied course reads as clear."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1", dropped=True)], 1
            ),
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    students = endall.fetch_roster(CONFIG)
    assert [s.user_id for s in students] == ["1"]


def test_read_students_parses_the_live_shape() -> None:
    """The recorded single-student payload reduces to one Student."""
    payload = roster_page([student_row("100005", "Test Student 1")], 1)
    students, total = endall.read_students(payload)
    assert total == 1
    assert students[0].user_id == "100005"
    assert students[0].name == "Test Student 1"
    assert students[0].dropped is False


def test_read_students_drops_rows_with_no_id() -> None:
    """An empty user id would address /resources/, a different endpoint."""
    payload = roster_page([{"name": "No Id"}, {"id": "", "name": "Blank"}], 2)
    students, _ = endall.read_students(payload)
    assert students == []


def test_read_students_tolerates_a_malformed_payload() -> None:
    """A tool that raises on a missing key cannot report the state it found."""
    assert endall.read_students(None) == ([], 0)
    assert endall.read_students({}) == ([], 0)
    assert endall.read_students({"users": "nonsense"}) == ([], 0)
    assert endall.read_students({"users": [], "total_records": "?"}) == ([], 0)


def test_fetch_roster_reads_one_page_when_the_total_is_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No second call once the collected count reaches total_records."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1")], 1
            )
        },
    )
    monkeypatch.setattr(endall, "request_json", api)
    students = endall.fetch_roster(CONFIG)
    assert [s.user_id for s in students] == ["1"]
    assert len(api.calls) == 1


def test_fetch_roster_pages_from_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A roster larger than one page is collected across zero-indexed pages."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1")], 3
            ),
            ("GET", CONFIG.roster_url(1, include_dropped=True)): roster_page(
                [student_row("2")], 3
            ),
            ("GET", CONFIG.roster_url(2, include_dropped=True)): roster_page(
                [student_row("3")], 3
            ),
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    students = endall.fetch_roster(CONFIG)
    assert [s.user_id for s in students] == ["1", "2", "3"]


def test_fetch_roster_stops_on_an_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty page ends the loop rather than being paged past forever."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1")], 1
            ),
            ("GET", CONFIG.roster_url(1, include_dropped=True)): roster_page([], 1),
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    assert [s.user_id for s in endall.fetch_roster(CONFIG)] == ["1"]


def test_fetch_roster_refuses_a_short_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collecting fewer rows than total_records is an error, not a warning.

    This is the failure the whole guard exists for: ending 1 of 30 sessions and
    reporting the cohort as down.
    """
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1")], 30
            ),
            ("GET", CONFIG.roster_url(1, include_dropped=True)): roster_page([], 30),
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    with pytest.raises(ApiError, match=r"Roster is short: collected 1 .* says 30"):
        endall.fetch_roster(CONFIG)


def test_fetch_roster_deduplicates_repeated_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page that repeats a row must not make the roster look complete twice."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1")], 2
            ),
            ("GET", CONFIG.roster_url(1, include_dropped=True)): roster_page(
                [student_row("1")], 2
            ),
            ("GET", CONFIG.roster_url(2, include_dropped=True)): roster_page(
                [student_row("2")], 2
            ),
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    assert [s.user_id for s in endall.fetch_roster(CONFIG)] == ["1", "2"]


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


def test_dry_run_ends_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default sends no DELETE and prints the sweep command per account."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1")], 1
            ),
            ("GET", resource_url("1")): session_payload("999988887777"),
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    assert endall.run(CONFIG, args(yes=False)) == 0
    assert api.deletes() == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "--account 999988887777" in out


def test_dry_run_reports_no_live_sessions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A terminated session is not offered as something to end."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1")], 1
            ),
            ("GET", resource_url("1")): TERMINATED,
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    assert endall.run(CONFIG, args(yes=False)) == 0
    assert api.deletes() == []
    assert "No live sessions" in capsys.readouterr().out


def test_exclude_leaves_a_student_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """An excluded user id is never even read, let alone ended."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1"), student_row("2")], 2
            ),
            ("GET", resource_url("2")): session_payload(),
            ("DELETE", resource_url("2")): {"status": "success"},
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    assert endall.run(CONFIG, args(yes=True, exclude=["1"])) == 1
    assert api.deletes() == [resource_url("2")]


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------


def test_ends_every_live_session_and_confirms(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each live student gets a DELETE, then a re-read proving the state moved."""
    reads = {"1": [session_payload(), TERMINATED], "2": [session_payload(), TERMINATED]}
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1"), student_row("2")], 2
            ),
            ("GET", resource_url("1")): lambda: reads["1"].pop(0),
            ("GET", resource_url("2")): lambda: reads["2"].pop(0),
            ("DELETE", resource_url("1")): {"status": "success"},
            ("DELETE", resource_url("2")): {"status": "success"},
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    assert endall.run(CONFIG, args(yes=True)) == 0
    assert sorted(api.deletes()) == [resource_url("1"), resource_url("2")]
    assert "Every live session was ended" in capsys.readouterr().out


def test_a_session_that_does_not_go_down_is_not_reported_as_ended(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 200 says the call was accepted, not that the session is down."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1")], 1
            ),
            ("GET", resource_url("1")): session_payload(),
            ("DELETE", resource_url("1")): {"status": "success"},
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    assert endall.run(CONFIG, args(yes=True)) == 1
    assert "UNCONFIRMED" in capsys.readouterr().out


def test_one_students_failure_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Student 1 failing still leaves students 2 and 3 holding accounts."""
    reads = {"2": [session_payload(), TERMINATED], "3": [session_payload(), TERMINATED]}
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1"), student_row("2"), student_row("3")], 3
            ),
            ("GET", resource_url("1")): session_payload(),
            ("GET", resource_url("2")): lambda: reads["2"].pop(0),
            ("GET", resource_url("3")): lambda: reads["3"].pop(0),
            ("DELETE", resource_url("1")): ApiError("HTTP 500 from DELETE"),
            ("DELETE", resource_url("2")): {"status": "success"},
            ("DELETE", resource_url("3")): {"status": "success"},
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    assert endall.run(CONFIG, args(yes=True)) == 1
    assert sorted(api.deletes()) == [
        resource_url("1"),
        resource_url("2"),
        resource_url("3"),
    ]
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "ended" in out


def test_an_unreadable_session_does_not_pass_as_clear(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A student whose session could not be read leaves the cohort unproven."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1"), student_row("2")], 2
            ),
            ("GET", resource_url("1")): ApiError("HTTP 500 from GET"),
            ("GET", resource_url("2")): TERMINATED,
        }
    )
    monkeypatch.setattr(endall, "request_json", api)
    assert endall.run(CONFIG, args(yes=True)) == 1
    assert "unreadable" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_does_not_require_a_user_id() -> None:
    """The roster supplies user ids, so VOCAREUM_USER_ID is not part of this."""
    assert "VOCAREUM_USER_ID" not in endall.VOCAREUM_PART_VARS


def test_main_reports_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing .env exits 2 without attempting anything."""
    for name in endall.VOCAREUM_PART_VARS:
        monkeypatch.delenv(name, raising=False)
    assert endall.main(["--env-file", str(tmp_path / "absent.env")]) == 2
    assert "Configuration error" in capsys.readouterr().out
