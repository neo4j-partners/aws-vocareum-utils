import importlib.util
import io
import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from vocareum_tools.probe import ConfigError, GuardError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "check_bedrock.py"
SPEC = importlib.util.spec_from_file_location("check_bedrock", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def client_error() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "AccessDeniedException", "Message": "not allowed"},
            "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "req-123"},
        },
        "Converse",
    )


class FakeRuntime:
    """Records each call and answers like Bedrock, or raises when told to."""

    def __init__(
        self,
        text: str = "ready",
        events: list[dict[str, Any]] | None = None,
        embedding: list[float] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.text = text
        self.events = events
        self.embedding = [0.0] * 1024 if embedding is None else embedding
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("converse", kwargs))
        if self.error:
            raise self.error
        return {"output": {"message": {"content": [{"text": f" {self.text} "}]}}}

    def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("converse_stream", kwargs))
        if self.error:
            raise self.error
        if self.events is None:
            self.events = [
                {"messageStart": {"role": "assistant"}},
                {"contentBlockDelta": {"delta": {"text": "rea"}}},
                {"contentBlockDelta": {"delta": {"text": "dy"}}},
                {"messageStop": {"stopReason": "end_turn"}},
            ]
        return {"stream": iter(self.events)}

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("invoke_model", kwargs))
        if self.error:
            raise self.error
        body = json.dumps({"embeddings": [{"embedding": self.embedding}]})
        return {"body": io.BytesIO(body.encode())}


def test_invoke_sonnet_joins_text_and_sends_the_notebook_request() -> None:
    runtime = FakeRuntime()

    assert check.invoke_sonnet(runtime) == "answered 'ready'"
    ((name, kwargs),) = runtime.calls
    assert name == "converse"
    assert kwargs["modelId"] == "us.anthropic.claude-sonnet-4-6"
    assert kwargs["messages"] == check.MESSAGES
    assert kwargs["inferenceConfig"] == {"maxTokens": 32}


def test_stream_sonnet_joins_text_deltas_and_ignores_other_events() -> None:
    runtime = FakeRuntime()

    assert check.stream_sonnet(runtime) == "answered 'ready'"
    ((name, kwargs),) = runtime.calls
    assert name == "converse_stream"
    assert kwargs["modelId"] == "us.anthropic.claude-sonnet-4-6"
    assert kwargs["messages"] == check.MESSAGES
    assert kwargs["inferenceConfig"] == check.INFERENCE_CONFIG


def test_stream_sonnet_with_no_text_raises() -> None:
    runtime = FakeRuntime(events=[{"messageStart": {}}, {"messageStop": {}}])

    with pytest.raises(ValueError, match="returned no text"):
        check.stream_sonnet(runtime)


def test_invoke_nova_reports_vector_length_and_sends_the_request() -> None:
    runtime = FakeRuntime()

    assert check.invoke_nova(runtime) == "returned a 1024-dimension vector"
    ((name, kwargs),) = runtime.calls
    assert name == "invoke_model"
    assert kwargs["modelId"] == "amazon.nova-2-multimodal-embeddings-v1:0"
    assert json.loads(kwargs["body"]) == check.NOVA_REQUEST


def test_attempt_prints_pass_and_returns_true(capsys: pytest.CaptureFixture) -> None:
    assert check.attempt("label", lambda: "fine") is True
    assert capsys.readouterr().out == "PASS label: fine\n"


def test_attempt_prints_code_message_and_request_id_on_client_error(
    capsys: pytest.CaptureFixture,
) -> None:
    runtime = FakeRuntime(error=client_error())

    assert check.attempt("label", lambda: check.invoke_sonnet(runtime)) is False

    out = capsys.readouterr().out
    assert "FAIL label: AccessDeniedException: not allowed" in out
    assert "HTTP 403 RequestId req-123" in out


def test_attempt_fails_on_an_empty_stream(capsys: pytest.CaptureFixture) -> None:
    runtime = FakeRuntime(events=[])

    assert check.attempt("label", lambda: check.stream_sonnet(runtime)) is False

    assert "FAIL label: ValueError: streaming Sonnet returned no text" in (
        capsys.readouterr().out
    )


class FakeSession:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime

    def client(self, service: str, **_kwargs: Any) -> FakeRuntime:
        assert service == "bedrock-runtime"
        return self.runtime


class FakeConfig:
    region = "us-east-1"


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    def install(runtime: FakeRuntime | None = None, **failures: Exception) -> None:
        def load_config(*_args: Any, **_kwargs: Any) -> FakeConfig:
            if "config" in failures:
                raise failures["config"]
            return FakeConfig()

        def open_session(*_args: Any, **_kwargs: Any) -> FakeSession:
            if "guard" in failures:
                raise failures["guard"]
            assert runtime is not None
            return FakeSession(runtime)

        monkeypatch.setattr(check, "load_config", load_config)
        monkeypatch.setattr(check, "open_guarded_session", open_session)

    return install


def test_main_returns_1_on_config_error(wire, capsys: pytest.CaptureFixture) -> None:
    wire(config=ConfigError("no env"))

    assert check.main([]) == check.EXIT_CONFIG == 1
    assert "Configuration error: no env" in capsys.readouterr().err


def test_main_returns_2_on_guard_error(wire, capsys: pytest.CaptureFixture) -> None:
    wire(guard=GuardError("wrong account"))

    assert check.main([]) == check.EXIT_GUARD == 2
    assert "wrong account" in capsys.readouterr().err


def test_main_returns_0_when_every_call_passes(
    wire, capsys: pytest.CaptureFixture
) -> None:
    runtime = FakeRuntime()
    wire(runtime)

    assert check.main([]) == check.EXIT_OK == 0
    assert [name for name, _ in runtime.calls] == [
        "converse",
        "converse_stream",
        "invoke_model",
    ]
    assert capsys.readouterr().out.count("PASS") == 3


def test_main_returns_3_when_a_call_fails_and_still_runs_the_rest(
    wire, capsys: pytest.CaptureFixture
) -> None:
    runtime = FakeRuntime(error=client_error())
    wire(runtime)

    assert check.main([]) == check.EXIT_FAILED == 3
    assert len(runtime.calls) == 3
    assert capsys.readouterr().out.count("FAIL") == 3
