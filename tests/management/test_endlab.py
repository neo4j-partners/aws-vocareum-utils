"""Tests for the end-session command, driven entirely by recorded payloads.

Ending a session is irreversible and gives up the AWS account, so the failures
worth testing are the ones where the tool acts when it should not, or reports a
session as down when it is not. Both are reachable without an API call, because
the only inputs are a resources payload and one flag.

The payload shapes here are the ones ``docs/vocareum-api.md`` records from live
calls, including the trap that motivated ``SessionFacts.live``: a terminated
session answers with ``aws_data`` still present and truthy, holding empty strings.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from vocareum_tools import endlab
from vocareum_tools.client import ApiError

LIVE_PAYLOAD: dict[str, Any] = {
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
                        "totalspend": "0.014604",
                        "allowedspend": "200",
                        "aws_access_key_id": "ASIAEXAMPLE",
                        "aws_linked_accounts": [
                            {
                                "account": "111111111111",
                                "inuse": "true",
                                "status": "ready",
                            },
                            {
                                "account": "222222222222",
                                "inuse": "false",
                                "status": "ready",
                            },
                        ],
                    },
                }
            ],
        }
    ],
}

# What a session looks like after the end call. Every credential field and
# account_id empty out, the spend counters survive, and aws_data stays a
# populated dict.
TERMINATED_PAYLOAD: dict[str, Any] = {
    "status": "success",
    "resources": [
        {
            "state": "Terminated",
            "sessionend": "2026-08-06T09:12:00-0700",
            "cloud": [
                {
                    "status": "inactive",
                    "vendor": "aws",
                    "aws_data": {
                        "account_id": "",
                        "aws_access_key_id": "",
                        "aws_secret_access_key": "",
                        "aws_session_token": "",
                        "totalspend": "0.014604",
                        "allowedspend": "200",
                    },
                }
            ],
        }
    ],
}


def config() -> endlab.Config:
    """Return a Config that addresses nothing real."""
    return endlab.Config(
        api_base="https://api.example.invalid/api/v2",
        course_id="1",
        assignment_id="2",
        part_id="3",
        user_id="4",
        api_key="not-a-key",
    )


def test_live_payload_is_read_in_full() -> None:
    facts = endlab.read_facts(LIVE_PAYLOAD)
    assert facts.state == "Ready"
    assert facts.status == "active"
    assert facts.account_id == "111111111111"
    assert facts.total_spend == "0.014604"
    assert facts.allowed_spend == "200"
    assert [member["account"] for member in facts.pool] == [
        "111111111111",
        "222222222222",
    ]
    assert facts.live is True


def test_terminated_payload_is_not_live_despite_a_truthy_aws_data() -> None:
    """The trap this property exists for.

    ``aws_data`` is still a populated dict after termination, so ``if aws_data:``
    reports a dead session as live. Only the fields inside it answer the question.
    """
    aws_data = TERMINATED_PAYLOAD["resources"][0]["cloud"][0]["aws_data"]
    assert aws_data, "the payload no longer reproduces the trap being tested"

    facts = endlab.read_facts(TERMINATED_PAYLOAD)
    assert facts.live is False
    assert facts.account_id == ""
    assert facts.total_spend == "0.014604", "spend outlives the session"
    assert facts.pool == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "success"},
        {"resources": []},
        {"resources": [{}]},
        {"resources": [{"cloud": []}]},
        {"resources": [{"cloud": [{}]}]},
        {"resources": [{"cloud": [{"aws_data": None}]}]},
        {"resources": [{"cloud": [{"aws_data": "not a dict"}]}]},
        {"resources": "not a list"},
    ],
)
def test_a_shape_it_does_not_expect_reads_as_not_live(payload: Any) -> None:
    """A tool that raises on a missing key cannot report a dead session at all."""
    facts = endlab.read_facts(payload)
    assert facts.live is False


def test_a_dry_run_sends_no_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default must not act. Ending a session cannot be undone."""
    calls: list[str] = []

    def fake_request(method: str, url: str, api_key: str, body: Any = None, **kw: Any):
        calls.append(method)
        return LIVE_PAYLOAD

    monkeypatch.setattr(endlab, "request_json", fake_request)
    code = endlab.run(config(), argparse.Namespace(yes=False))

    assert code == 0
    assert calls == ["GET"], "a dry run read the session and nothing else"


def test_yes_sends_exactly_one_delete_and_confirms_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    answers = [LIVE_PAYLOAD, {"status": "success"}, TERMINATED_PAYLOAD]

    def fake_request(method: str, url: str, api_key: str, body: Any = None, **kw: Any):
        calls.append((method, body))
        return answers.pop(0)

    monkeypatch.setattr(endlab, "request_json", fake_request)
    code = endlab.run(config(), argparse.Namespace(yes=True))

    assert code == 0
    assert [method for method, _ in calls] == ["GET", "DELETE", "GET"]
    assert calls[1][1] == {"action": "end"}, "only the measured action is ever sent"


def test_a_200_that_did_not_move_the_state_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 says the call was accepted, not that the session is down."""
    answers = [LIVE_PAYLOAD, {"status": "success"}, LIVE_PAYLOAD]
    monkeypatch.setattr(
        endlab,
        "request_json",
        lambda *a, **k: answers.pop(0),
    )
    assert endlab.run(config(), argparse.Namespace(yes=True)) == 1


def test_an_already_terminated_session_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_request(method: str, url: str, api_key: str, body: Any = None, **kw: Any):
        calls.append(method)
        return TERMINATED_PAYLOAD

    monkeypatch.setattr(endlab, "request_json", fake_request)
    code = endlab.run(config(), argparse.Namespace(yes=True))

    assert code == 0
    assert calls == ["GET"], "--yes must not end a session that is already down"


def test_terminate_is_not_reachable() -> None:
    """``terminate`` is undocumented beyond one word and has never been measured.

    A flag that offers it would make an unmeasured destructive action one keypress
    away, so the module carries a single action constant instead. This checks the
    command surface rather than the source text, because the docstring explains
    the omission and naming the thing is the point of that explanation.
    """
    assert endlab.END_ACTION == "end"

    parser = endlab.parse_args([])
    assert not hasattr(parser, "action")
    assert not hasattr(parser, "terminate")

    with pytest.raises(SystemExit):
        endlab.parse_args(["--action", "terminate"])
    with pytest.raises(SystemExit):
        endlab.parse_args(["--terminate"])


def test_an_api_error_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ApiError("HTTP 400 from DELETE", status_code=400)

    monkeypatch.setattr(endlab, "request_json", fail)
    monkeypatch.setattr(endlab, "load_config", lambda path, course=None: config())
    assert endlab.main(["--yes"]) == 1
