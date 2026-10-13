"""Tests for the shared phase-runner scaffolding.

The runners this module was lifted out of have no tests of their own, because
every one of their probes needs a live lab session. What is testable is the
part that decides how a call is recorded and whether teardown reported the
truth, and that part is now here rather than copied three ways.
"""

from datetime import date, datetime
from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ParamValidationError,
)

from vocareum_tools import phase
from vocareum_tools.probe import Verdict


def client_error(code: str, message: str = "no", operation: str = "Op") -> ClientError:
    """Return a ClientError the classifier will read."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def scp_error(action: str = "dynamodb:CreateTable") -> ClientError:
    """Return the denial shape an SCP produces, naming the action it denied.

    The action in the message matters: the classifier reads a denial of some
    other action as a dependency failure rather than as a denial of this row.
    """
    return client_error(
        "AccessDeniedException",
        f"User is not authorized to perform: {action} with an "
        "explicit deny in a service control policy",
    )


def test_json_default_serializes_the_date_types_botocore_returns() -> None:
    assert phase.json_default(date(2026, 7, 30)) == "2026-07-30"
    assert phase.json_default(datetime(2026, 7, 30, 12, 0)) == "2026-07-30T12:00:00"


def test_json_default_refuses_anything_else() -> None:
    with pytest.raises(TypeError, match="set is not JSON serializable"):
        phase.json_default({1, 2})


def test_measurement_maps_its_verdict_to_a_tracker_status() -> None:
    allowed = phase.Measurement("36", "dynamodb:CreateTable", Verdict.OK, "fine")
    denied = phase.Measurement("36", "dynamodb:CreateTable", Verdict.SCP_DENY, "no")
    unknown = phase.Measurement("36", "dynamodb:CreateTable", Verdict.THROTTLED, "wait")

    assert allowed.status == "Allowed"
    assert denied.status == "Denied (SCP)"
    # A throttle is an authorization pass that ran out of capacity, so it is
    # Allowed rather than a gap.
    assert unknown.status == "Allowed"


def test_measurement_as_dict_carries_the_status_not_just_the_verdict() -> None:
    measurement = phase.Measurement("46", "dynamodb:ListTables", Verdict.OK, "fine")

    assert measurement.as_dict() == {
        "row": "46",
        "action": "dynamodb:ListTables",
        "verdict": "OK",
        "status": "Allowed",
        "detail": "fine",
    }


def test_call_records_a_success_and_returns_the_response() -> None:
    recorder = phase.Recorder()

    verdict, response = recorder.call("46", "dynamodb:ListTables", lambda: {"n": 1})

    assert verdict is Verdict.OK
    assert response == {"n": 1}
    assert [m.status for m in recorder.measurements] == ["Allowed"]


def test_call_classifies_a_denial_and_returns_no_response() -> None:
    recorder = phase.Recorder()

    def denied() -> Any:
        raise scp_error()

    verdict, response = recorder.call("36", "dynamodb:CreateTable", denied)

    assert verdict is Verdict.SCP_DENY
    assert response is None
    assert recorder.measurements[0].status == "Denied (SCP)"


def test_call_reports_a_request_the_sdk_refused_to_send_as_untested() -> None:
    """A call that never left the SDK never reached authorization."""
    recorder = phase.Recorder()

    def bad_input() -> Any:
        raise ParamValidationError(report="TableName is required")

    verdict, _ = recorder.call("36", "dynamodb:CreateTable", bad_input)

    assert verdict is Verdict.INCONCLUSIVE_BAD_INPUT
    assert recorder.measurements[0].status == "Untested"


def test_call_reports_a_transport_failure_as_untested() -> None:
    recorder = phase.Recorder()

    def unreachable() -> Any:
        raise EndpointConnectionError(endpoint_url="https://example.test")

    verdict, _ = recorder.call("36", "dynamodb:CreateTable", unreachable)

    assert verdict is Verdict.UNCLASSIFIED
    assert recorder.measurements[0].status == "Untested"


def test_call_redacts_a_secret_out_of_the_message_it_records() -> None:
    recorder = phase.Recorder()

    def leaky() -> Any:
        raise client_error("ValidationException", '{"password": "hunter2"}')

    recorder.call("117", "secretsmanager:GetSecretValue", leaky)

    assert "hunter2" not in recorder.measurements[0].detail


def test_call_names_the_resource_so_repeats_of_one_row_are_distinguishable() -> None:
    recorder = phase.Recorder()

    recorder.call("23", "iam:CreateRole", lambda: None, context="role-a")
    recorder.call("23", "iam:CreateRole", lambda: None, context="role-b")

    assert "role-a" in recorder.measurements[0].detail
    assert "role-b" in recorder.measurements[1].detail


def test_call_in_teardown_records_the_failure_and_lets_it_through() -> None:
    """Absorbing a failed delete would report a clean account that is not."""
    recorder = phase.Recorder()

    def denied() -> Any:
        raise scp_error("dynamodb:DeleteTable")

    with pytest.raises(ClientError):
        recorder.call_in_teardown("37", "dynamodb:DeleteTable", denied, "table")

    assert recorder.measurements[0].status == "Denied (SCP)"


def test_call_in_teardown_records_a_delete_that_worked() -> None:
    recorder = phase.Recorder()

    recorder.call_in_teardown("37", "dynamodb:DeleteTable", lambda: None, "table")

    assert recorder.measurements[0].verdict is Verdict.OK


def test_note_records_something_that_was_not_a_single_call() -> None:
    recorder = phase.Recorder()

    recorder.note("36", "dynamodb:CreateTable (table state)", Verdict.UNCLASSIFIED, "x")

    assert recorder.measurements[0].status == "Untested"


def test_cleanups_run_in_reverse_and_the_list_is_cleared() -> None:
    recorder = phase.Recorder()
    order: list[str] = []
    recorder.defer("first", lambda: order.append("first"))
    recorder.defer("second", lambda: order.append("second"))

    recorder.run_cleanups()

    assert order == ["second", "first"]
    assert recorder.cleanups == []


def test_one_failed_cleanup_is_reported_and_does_not_stop_the_rest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = phase.Recorder()
    removed: list[str] = []

    def fails() -> None:
        raise client_error("AccessDeniedException", "no")

    recorder.defer("the one that stays", fails)
    recorder.defer("the one that goes", lambda: removed.append("gone"))

    recorder.run_cleanups()

    printed = capsys.readouterr().out
    assert "LEFT BEHIND the one that stays" in printed
    assert removed == ["gone"]


def test_teardown_runs_even_when_the_block_raises() -> None:
    recorder = phase.Recorder()
    removed: list[str] = []
    recorder.defer("thing", lambda: removed.append("gone"))

    with pytest.raises(RuntimeError), phase.teardown_guaranteed(recorder):
        raise RuntimeError("the probe block failed")

    assert removed == ["gone"]


def test_the_action_column_width_is_the_only_thing_it_changes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each runner keeps the output width its saved logs were written at."""
    phase.Recorder(action_width=40).call("46", "logs:X", lambda: None)
    narrow = capsys.readouterr().out
    phase.Recorder(action_width=48).call("46", "logs:X", lambda: None)
    wide = capsys.readouterr().out

    assert len(wide) - len(narrow) == 8
    assert narrow.split() == wide.split()


def test_wait_until_returns_the_state_it_settled_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phase.time, "sleep", lambda _: None)
    states = iter(["CREATING", "CREATING", "ACTIVE"])

    state = phase.wait_until(
        lambda: next(states),
        done={"ACTIVE"},
        failed={"DELETING"},
        label="table",
    )

    assert state == "ACTIVE"


def test_wait_until_stops_on_a_failed_state_rather_than_waiting_it_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phase.time, "sleep", lambda _: None)

    state = phase.wait_until(
        lambda: "DELETING",
        done={"ACTIVE"},
        failed={"DELETING"},
        label="table",
    )

    assert state == "DELETING"


def test_wait_until_says_so_when_the_describe_itself_fails() -> None:
    def denied() -> str:
        raise client_error("AccessDeniedException", "no")

    state = phase.wait_until(
        denied, done={"ACTIVE"}, failed=set(), label="table", timeout=1
    )

    assert state == "describe failed: AccessDeniedException"


def test_wait_until_gives_up_and_reports_the_last_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phase.time, "sleep", lambda _: None)
    clock = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr(phase.time, "monotonic", lambda: next(clock))

    state = phase.wait_until(
        lambda: "CREATING",
        done={"ACTIVE"},
        failed=set(),
        label="table",
        timeout=5,
    )

    assert state == "CREATING"


def test_wait_until_gone_confirms_a_resource_that_really_went() -> None:
    def missing() -> Any:
        raise client_error("ResourceNotFoundException", "gone")

    assert phase.wait_until_gone(missing, "log group") is True


def test_wait_until_gone_does_not_claim_a_clean_account_on_another_error() -> None:
    """Only a not-found answer proves the resource left."""

    def denied() -> Any:
        raise client_error("AccessDeniedException", "no")

    assert phase.wait_until_gone(denied, "log group") is False


def test_wait_until_gone_reports_a_resource_still_present_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phase.time, "sleep", lambda _: None)
    clock = iter([0.0, 0.0, 200.0])
    monkeypatch.setattr(phase.time, "monotonic", lambda: next(clock))

    assert phase.wait_until_gone(lambda: {"still": "here"}, "log group") is False
