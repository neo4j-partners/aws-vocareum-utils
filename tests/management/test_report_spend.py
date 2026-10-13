"""Tests for the cohort spend report, driven entirely by recorded payloads.

This command only ever sends ``GET``, so the failures worth testing are about
the total, not about acting on the wrong session: one unreadable student must
not silently understate the cohort's spend, a dropped student's spend still
counts, and the roster/session shapes are the same ones ``test_endall.py``
recorded against the live course.
"""

from __future__ import annotations

from typing import Any

import pytest

from vocareum_tools import endall, report_spend
from vocareum_tools.client import ApiError
from vocareum_tools.endall import Config
from vocareum_tools.endpoints import DEFAULT_API_BASE

CONFIG = Config(
    api_base=DEFAULT_API_BASE,
    course_id="100002",
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
        "courseid": "100002",
        "role": "student",
        "name": name or f"Test Student {user_id}",
        "email": f"student{user_id}@example.com",
        "lti_user_id": None,
        "dropped": dropped,
    }


def session_payload(spend: str = "0.01", allowed: str = "200") -> dict[str, Any]:
    """Build a resources payload holding a readable spend figure."""
    return {
        "status": "success",
        "resources": [
            {
                "state": "Ready",
                "sessionend": "2026-08-06T12:35:26-0700",
                "cloud": [
                    {
                        "status": "active",
                        "vendor": "aws",
                        "aws_data": {
                            "account_id": "111111111111",
                            "totalspend": spend,
                            "allowedspend": allowed,
                            "aws_access_key_id": "ASIAEXAMPLE",
                            "aws_linked_accounts": [],
                        },
                    }
                ],
            }
        ],
    }


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
        return answer


def resource_url(user_id: str) -> str:
    """Return the session URL for a user id under CONFIG."""
    return f"{CONFIG.part_url()}/resources/{user_id}"


def patch_api(monkeypatch: pytest.MonkeyPatch, api: FakeApi) -> None:
    """Patch every module that holds its own bound ``request_json`` name.

    ``report_spend.fetch_facts`` calls the name in its own module, while
    ``fetch_roster`` is defined in, and calls the name bound in, ``endall``.
    Both have to point at the same fake or the roster read and the per-student
    reads would hit different backends.
    """
    monkeypatch.setattr(endall, "request_json", api)
    monkeypatch.setattr(report_spend, "request_json", api)


# ---------------------------------------------------------------------------
# Summing spend
# ---------------------------------------------------------------------------


def test_run_sums_spend_across_every_student(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two readable students add up, and the exit code is clean."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1"), student_row("2")], 2
            ),
            ("GET", resource_url("1")): session_payload("1.50"),
            ("GET", resource_url("2")): session_payload("2.25"),
        }
    )
    patch_api(monkeypatch, api)
    assert report_spend.run(CONFIG) == 0
    out = capsys.readouterr().out
    assert "Total spend across 2 readable student(s): $3.75" in out


def test_dropped_student_spend_still_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dropped roster row is annotated, not excluded from the total."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1", dropped=True)], 1
            ),
            ("GET", resource_url("1")): session_payload("5.00"),
        }
    )
    patch_api(monkeypatch, api)
    assert report_spend.run(CONFIG) == 0
    out = capsys.readouterr().out
    assert "(dropped)" in out
    assert "Total spend across 1 readable student(s): $5.00" in out


def test_an_unreadable_student_makes_the_total_a_lower_bound(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed read must not silently understate the cohort's spend."""
    api = FakeApi(
        {
            ("GET", CONFIG.roster_url(include_dropped=True)): roster_page(
                [student_row("1"), student_row("2")], 2
            ),
            ("GET", resource_url("1")): session_payload("1.00"),
            ("GET", resource_url("2")): ApiError("HTTP 500 from GET"),
        }
    )
    patch_api(monkeypatch, api)
    assert report_spend.run(CONFIG) == 1
    out = capsys.readouterr().out
    assert "unreadable" in out
    assert "lower bound" in out
    assert "Total spend across 1 readable student(s): $1.00" in out


def test_an_empty_roster_reports_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No students on the course is not an error."""
    api = FakeApi(
        {("GET", CONFIG.roster_url(include_dropped=True)): roster_page([], 0)}
    )
    patch_api(monkeypatch, api)
    assert report_spend.run(CONFIG) == 0
    assert "Nobody to report on" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_main_reports_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing .env exits 2 without reading the roster."""
    from vocareum_tools.endall import VOCAREUM_PART_VARS

    for name in VOCAREUM_PART_VARS:
        monkeypatch.delenv(name, raising=False)
    assert report_spend.main(["--env-file", str(tmp_path / "absent.env")]) == 2
    assert "Configuration error" in capsys.readouterr().out
