from typing import Any

import pytest

from vocareum_tools import client
from vocareum_tools.client import ApiError, wait_for_payload


def test_poll_transaction_reaches_terminal_state(monkeypatch: Any) -> None:
    responses = iter([{"state": "pending"}, {"state": "success"}])
    monkeypatch.setattr(client, "request_json", lambda *args, **kwargs: next(responses))
    states: list[str] = []

    result = client.poll_transaction(
        "https://example.test/transaction/1",
        "api-key",
        poll_seconds=0,
        timeout_seconds=10,
        on_state=states.append,
    )

    assert result.state == "success"
    assert not result.timed_out
    assert states == ["pending", "success"]


def test_poll_transaction_marks_a_run_that_never_settled(monkeypatch: Any) -> None:
    """A timed-out poll must not read as a terminal state.

    The state is still pending when the clock runs out, and a caller that could
    not tell the difference would report an unfinished transaction as its final
    answer.
    """
    polls = 0

    def pending(*args: Any, **kwargs: Any) -> dict[str, str]:
        nonlocal polls
        polls += 1
        return {"state": "pending"}

    monkeypatch.setattr(client, "request_json", pending)

    result = client.poll_transaction(
        "https://example.test/transaction/1",
        "api-key",
        poll_seconds=1,
        timeout_seconds=0,
    )

    assert result.timed_out
    assert result.state == "pending"
    assert polls == 1


def test_wait_for_payload_fetches_once_when_not_waiting() -> None:
    fetches = 0

    def fetch() -> dict[str, str]:
        nonlocal fetches
        fetches += 1
        return {"state": "starting"}

    payload, found = wait_for_payload(
        fetch,
        lambda item: item.get("token"),
        poll_seconds=0,
        timeout_seconds=10,
        wait=False,
    )

    assert found is None
    assert payload == {"state": "starting"}
    assert fetches == 1


def test_wait_for_payload_returns_the_last_payload_after_a_timeout(
    monkeypatch: Any,
) -> None:
    """The payload comes back even when nothing was extracted from it.

    Callers print that body to explain what they did get, so losing it would
    turn a diagnosable empty result into an unexplained one.
    """
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)
    retries: list[int] = []

    payload, found = wait_for_payload(
        lambda: {"state": "starting"},
        lambda item: item.get("token"),
        poll_seconds=1,
        timeout_seconds=0,
        wait=True,
        on_retry=retries.append,
    )

    assert found is None
    assert payload == {"state": "starting"}
    assert retries == []


def test_wait_for_payload_reports_each_retry(monkeypatch: Any) -> None:
    payloads = iter([{"state": "starting"}, {"token": "ready"}])
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)
    retries: list[int] = []

    payload, found = wait_for_payload(
        lambda: next(payloads),
        lambda item: item.get("token"),
        poll_seconds=5,
        timeout_seconds=60,
        wait=True,
        on_retry=retries.append,
    )

    assert found == "ready"
    assert payload == {"token": "ready"}
    assert retries == [5]


def test_clip_body_marks_what_it_dropped() -> None:
    """A clipped body has to say so, or it reads as the whole response."""
    clipped = client.clip_body("x" * 40, limit=10)

    assert clipped.startswith("x" * 10)
    assert "40 characters total" in clipped
    assert client.clip_body("x" * 10, limit=10) == "x" * 10
    assert client.clip_body("") == "(empty)"


def test_find_transaction_id_handles_nested_response() -> None:
    payload = {"resources": [{"Transaction-ID": 12345}]}

    assert client.find_transaction_id(payload) == "12345"


@pytest.mark.parametrize("operation", ["request", "download"])
def test_timeout_errors_do_not_expose_signed_url(
    operation: str, monkeypatch: Any
) -> None:
    url = "https://example.test/file?X-Amz-Signature=secret-signature"
    monkeypatch.setattr(
        client.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    with pytest.raises(ApiError) as raised:
        if operation == "request":
            client.request_json("GET", url, "api-key")
        else:
            client.download_bytes(url)

    assert "secret-signature" not in str(raised.value)
