"""Offline tests for step 2b, the Bedrock model access check.

No model is called here. What is worth testing is the sorting: Bedrock answers
an account that lacks the Anthropic entitlement and an account whose policy is
short with the same error code, and this step exists to tell a student which of
the two they are looking at. A test that only checked PASS and FAIL would pass
while the two were merged.

The other thing held here is the pair of pinned model ids. They are inference
profile ids, and a bare model id measures nothing, so the shape is asserted
rather than left to a reviewer to notice.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from workshop_lab.harness import FAIL, PASS, SKIP, Harness
from workshop_lab.models import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODELS,
    EMBEDDING_REQUEST,
    INFERENCE_CONFIG,
    INVOKE_ACTION,
    MODELS,
    NO_TEXT_DETAIL,
    NOVA_EMBEDDING_MODEL_ID,
    OPUS_MODEL_ID,
    PROMPT,
    SONNET_MODEL_ID,
    STREAM_ACTION,
    TITAN_EMBEDDING_MODEL_ID,
    TITAN_EMBEDDING_REQUEST,
    ModelAccess,
    nova_embeddings_of,
    text_of,
    text_of_stream,
    titan_embeddings_of,
    verdict_for,
)

REGION = "us-east-1"

ANSWER = {"output": {"message": {"content": [{"text": "ready"}]}}}
STREAMED = {"stream": [{"contentBlockDelta": {"delta": {"text": "ready"}}}]}


def embedding_response(dimension: int = EMBEDDING_DIMENSION) -> dict[str, Any]:
    return {
        "body": io.BytesIO(
            json.dumps({"embeddings": [{"embedding": [0.0] * dimension}]}).encode()
        )
    }


def titan_embedding_response(dimension: int = EMBEDDING_DIMENSION) -> dict[str, Any]:
    return {"body": io.BytesIO(json.dumps({"embedding": [0.0] * dimension}).encode())}


class FakeRuntime:
    """A bedrock-runtime client whose two calls are scripted per model id."""

    def __init__(self, answers: dict[tuple[str, str], Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def _answer(self, action: str, model_id: str) -> Any:
        self.calls.append((action, model_id))
        reply = self.answers[(action, model_id)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def converse(self, modelId: str, **kwargs: Any) -> Any:
        self.requests.append(("converse", modelId, kwargs))
        return self._answer(INVOKE_ACTION, modelId)

    def converse_stream(self, modelId: str, **kwargs: Any) -> Any:
        self.requests.append(("converse_stream", modelId, kwargs))
        return self._answer(STREAM_ACTION, modelId)

    def invoke_model(self, modelId: str, **kwargs: Any) -> Any:
        self.requests.append(("invoke_model", modelId, kwargs))
        return self._answer(INVOKE_ACTION, modelId)


class FakeSession:
    region_name = REGION

    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime
        self.asked: list[str] = []

    def client(self, name: str, region_name: str | None = None) -> Any:
        self.asked.append(name)
        assert name == "bedrock-runtime", f"step 2b asked for {name}"
        return self.runtime


def error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


def harness(answers: dict[tuple[str, str], Any]) -> Harness:
    printed: list[str] = []
    lab = Harness(session=FakeSession(FakeRuntime(answers)), echo=printed.append)
    lab.printed = printed  # type: ignore[attr-defined]
    return lab


def all_answering() -> dict[tuple[str, str], Any]:
    return {
        (INVOKE_ACTION, SONNET_MODEL_ID): ANSWER,
        (STREAM_ACTION, SONNET_MODEL_ID): STREAMED,
        (INVOKE_ACTION, OPUS_MODEL_ID): ANSWER,
        (STREAM_ACTION, OPUS_MODEL_ID): STREAMED,
        (INVOKE_ACTION, NOVA_EMBEDDING_MODEL_ID): embedding_response(),
        (INVOKE_ACTION, TITAN_EMBEDDING_MODEL_ID): titan_embedding_response(),
    }


def rows(lab: Harness) -> dict[str, str]:
    return {name: verdict for name, verdict, _ in lab.results}


def detail(lab: Harness, name: str) -> str:
    return next(text for row, _, text in lab.results if row == name)


def printed(lab: Harness) -> str:
    return "\n".join(lab.printed)  # type: ignore[attr-defined]


# --- the pinned models -----------------------------------------------------


def test_both_models_are_named_by_inference_profile_id() -> None:
    """A bare `anthropic.` id answers ValidationException, which measures nothing.

    Every Claude 4 model in the us-east-1 catalog supports INFERENCE_PROFILE
    only, so the `us.` prefix is what makes this a permission question at all.
    """
    for model_id, _ in MODELS:
        assert model_id.startswith("us.anthropic.claude-")


def test_the_sonnet_is_the_one_the_workshop_itself_calls() -> None:
    """The agent labs carry this id in MODEL_ID. A check on a different Sonnet
    reports on a model no lab uses."""
    assert SONNET_MODEL_ID == "us.anthropic.claude-sonnet-4-6"


def test_both_models_are_from_the_4_series_vocareum_enabled() -> None:
    assert "-4-" in SONNET_MODEL_ID
    assert "-4-" in OPUS_MODEL_ID


def test_the_embedding_model_is_the_one_the_retrieval_labs_use() -> None:
    assert NOVA_EMBEDDING_MODEL_ID == "amazon.nova-2-multimodal-embeddings-v1:0"


def test_the_titan_embedding_model_is_the_v2_text_model() -> None:
    assert TITAN_EMBEDDING_MODEL_ID == "amazon.titan-embed-text-v2:0"


def test_the_embedding_request_is_one_small_generic_index_vector() -> None:
    assert EMBEDDING_REQUEST["taskType"] == "SINGLE_EMBEDDING"
    params = EMBEDDING_REQUEST["singleEmbeddingParams"]
    assert params["embeddingPurpose"] == "GENERIC_INDEX"
    assert params["embeddingDimension"] == EMBEDDING_DIMENSION == 1024
    assert params["text"]["value"] == "permission test"


def test_the_titan_embedding_request_uses_its_native_schema() -> None:
    assert TITAN_EMBEDDING_REQUEST == {
        "inputText": "permission test",
        "dimensions": EMBEDDING_DIMENSION,
        "normalize": True,
    }
    assert {model_id for model_id, *_ in EMBEDDING_MODELS} == {
        NOVA_EMBEDDING_MODEL_ID,
        TITAN_EMBEDDING_MODEL_ID,
    }


def test_the_prompt_and_the_token_ceiling_stay_small() -> None:
    """The question is access. A refusal and an answer should cost the same."""
    assert len(PROMPT) < 60
    assert INFERENCE_CONFIG["maxTokens"] <= 64


# --- sorting the failures --------------------------------------------------


def test_an_entitlement_message_is_not_reported_as_a_permission_problem() -> None:
    """Bedrock sends this as AccessDeniedException, the IAM shortfall's own code.

    Reading the code first would send a student to their instructor to widen a
    policy that is already correct, for a subscription only Vocareum can move.
    """
    verdict, line = verdict_for(
        "AccessDeniedException",
        "You don't have access to the model with the specified model ID.",
    )

    assert verdict == FAIL
    assert "not entitled" in line


@pytest.mark.parametrize(
    "message",
    [
        "anthropic.claude-sonnet-4-6 is not available for this account",
        "Anthropic use case details have not been submitted for this account",
    ],
)
def test_every_entitlement_wording_sorts_the_same_way(message: str) -> None:
    verdict, line = verdict_for("ResourceNotFoundException", message)

    assert verdict == FAIL
    assert "not entitled" in line


def test_a_plain_iam_refusal_says_so() -> None:
    verdict, line = verdict_for(
        "AccessDeniedException",
        "User: arn:aws:sts::1:assumed-role/voclabs/user is not authorized to "
        "perform: bedrock:InvokeModel because no identity-based policy allows it",
    )

    assert verdict == FAIL
    assert "IAM or an SCP" in line


def test_a_missing_marketplace_subscription_is_its_own_line() -> None:
    """Observed per model and per operation, with the actions already granted."""
    verdict, line = verdict_for(
        "AccessDeniedException",
        "Your account is not authorized: aws-marketplace:Subscribe",
    )

    assert verdict == FAIL
    assert "Marketplace" in line


def test_a_stale_pin_points_at_this_file_rather_than_at_the_account() -> None:
    """A retired model id is a maintenance job here, not a lost permission."""
    verdict, line = verdict_for(
        "ValidationException", "The provided model identifier is invalid."
    )

    assert verdict == FAIL
    assert "does not know this model id" in line


def test_a_refusal_that_names_a_profile_is_still_read_as_a_refusal() -> None:
    """An IAM denial quotes the resource it refused, which is the profile.

    The bad-id verdicts are matched only under `ValidationException` for this
    reason. Matched on the message alone, a real permission gap would answer
    "change the id in workshop_lab.models" and nobody would look at the policy.
    """
    verdict, line = verdict_for(
        "AccessDeniedException",
        "User: arn:aws:sts::1:assumed-role/voclabs/user is not authorized to "
        "perform: bedrock:InvokeModel on the inference profile "
        "arn:aws:bedrock:us-east-1:1:inference-profile/us.anthropic.claude-opus-4-8",
    )

    assert verdict == FAIL
    assert "IAM or an SCP" in line


def test_a_bare_model_id_is_told_to_use_the_profile() -> None:
    verdict, line = verdict_for(
        "ValidationException",
        "Invocation of model ID anthropic.claude-sonnet-4-6 with on-demand "
        "throughput isn't supported. Retry with the inference profile id.",
    )

    assert verdict == FAIL
    assert "inference-profile id" in line


def test_a_throttle_measures_nothing_and_is_recorded_as_such() -> None:
    """Throttling happens after authorization, so it is neither a pass nor a fail.

    Recording it as FAIL would tell a student their account cannot run the
    workshop when all that happened was a busy minute.
    """
    verdict, line = verdict_for("ThrottlingException", "Too many requests")

    assert verdict == SKIP
    assert "throttled" in line


def test_an_unrecognised_error_keeps_its_code_and_message() -> None:
    """A verdict that swallowed the code would leave nothing to report."""
    verdict, line = verdict_for("ServiceUnavailableException", "try later")

    assert verdict == FAIL
    assert line == "ServiceUnavailableException: try later"


def test_a_long_message_is_truncated() -> None:
    _, line = verdict_for("ValidationException", "x" * 5000)

    assert len(line) < 250


# --- reading the reply -----------------------------------------------------


def test_the_reply_text_is_read_out_of_the_converse_shape() -> None:
    assert text_of(ANSWER) == "ready"


def test_a_reply_with_no_text_block_reads_as_empty() -> None:
    """A tool-use-only or empty reply is not an answer, and says so downstream."""
    assert text_of({"output": {"message": {"content": [{"toolUse": {}}]}}}) == ""
    assert text_of({}) == ""


def test_the_stream_is_drained_rather_than_counted() -> None:
    """A stream that authorizes and then yields nothing is its own outcome."""
    response = {
        "stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "re"}}},
            {"contentBlockDelta": {"delta": {"text": "ady"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    }

    assert text_of_stream(response) == "ready"
    assert text_of_stream({"stream": [{"messageStop": {}}]}) == ""


def test_an_embedding_response_is_read_from_the_invoke_model_body() -> None:
    assert nova_embeddings_of(embedding_response()) == [[0.0] * EMBEDDING_DIMENSION]
    assert nova_embeddings_of({}) == []
    assert nova_embeddings_of({"body": io.BytesIO(b"[]")}) == []
    assert titan_embeddings_of(titan_embedding_response()) == [
        [0.0] * EMBEDDING_DIMENSION
    ]
    assert titan_embeddings_of({"body": io.BytesIO(b"[]")}) == []


# --- the step --------------------------------------------------------------


def test_every_model_is_called_on_both_invoke_actions() -> None:
    """Streaming is refused independently of non-streaming, measured 2026-08-04.

    A Strands agent streams by default, so a check that skipped ConverseStream
    would report a session ready that the workshop's own agents cannot use.
    """
    lab = harness(all_answering())

    assert ModelAccess(lab).run() is True
    assert set(rows(lab)) == {
        f"{INVOKE_ACTION} {SONNET_MODEL_ID}",
        f"{STREAM_ACTION} {SONNET_MODEL_ID}",
        f"{INVOKE_ACTION} {OPUS_MODEL_ID}",
        f"{STREAM_ACTION} {OPUS_MODEL_ID}",
        f"{INVOKE_ACTION} {NOVA_EMBEDDING_MODEL_ID}",
        f"{INVOKE_ACTION} {TITAN_EMBEDDING_MODEL_ID}",
    }
    assert set(rows(lab).values()) == {PASS}


def test_a_row_is_named_for_the_iam_action_and_the_model() -> None:
    """Permission rows are per action and concrete model."""
    lab = harness(all_answering())

    ModelAccess(lab).run()

    assert all(
        name.startswith("bedrock:InvokeModel")
        and (
            " us.anthropic." in name
            or f" {NOVA_EMBEDDING_MODEL_ID}" in name
            or f" {TITAN_EMBEDDING_MODEL_ID}" in name
        )
        for name in rows(lab)
    )


def test_a_passing_row_quotes_what_the_model_said() -> None:
    """A model that authorizes and returns nothing is the failure below."""
    lab = harness(all_answering())

    ModelAccess(lab).run()

    assert "ready" in detail(lab, f"{INVOKE_ACTION} {SONNET_MODEL_ID}")


@pytest.mark.parametrize(
    ("model_id", "expected_body"),
    [
        (NOVA_EMBEDDING_MODEL_ID, EMBEDDING_REQUEST),
        (TITAN_EMBEDDING_MODEL_ID, TITAN_EMBEDDING_REQUEST),
    ],
)
def test_each_embedding_is_requested_with_its_native_payload(
    model_id: str, expected_body: dict[str, Any]
) -> None:
    lab = harness(all_answering())

    assert ModelAccess(lab).run() is True

    runtime = lab.session.runtime
    request = next(
        request
        for request in runtime.requests
        if request[0] == "invoke_model" and request[1] == model_id
    )
    assert request[2]["contentType"] == "application/json"
    assert request[2]["accept"] == "application/json"
    assert json.loads(request[2]["body"]) == expected_body
    assert "1024-dimension" in detail(lab, f"{INVOKE_ACTION} {model_id}")


@pytest.mark.parametrize(
    "response, expected",
    [
        ({"body": io.BytesIO(b'{"embeddings": []}')}, "expected one embedding"),
        (
            {
                "body": io.BytesIO(
                    json.dumps({"embeddings": [{"embedding": [0.0] * 1023}]}).encode()
                )
            },
            "expected a 1024-dimension embedding",
        ),
    ],
)
def test_an_unusable_embedding_response_fails(response: dict, expected: str) -> None:
    answers = all_answering()
    answers[(INVOKE_ACTION, NOVA_EMBEDDING_MODEL_ID)] = response
    lab = harness(answers)

    assert ModelAccess(lab).run() is False
    assert expected in detail(lab, f"{INVOKE_ACTION} {NOVA_EMBEDDING_MODEL_ID}")


def test_an_unusable_titan_embedding_response_fails() -> None:
    answers = all_answering()
    answers[(INVOKE_ACTION, TITAN_EMBEDDING_MODEL_ID)] = titan_embedding_response(1023)
    lab = harness(answers)

    assert ModelAccess(lab).run() is False
    assert "expected a 1024-dimension embedding" in detail(
        lab, f"{INVOKE_ACTION} {TITAN_EMBEDDING_MODEL_ID}"
    )


def test_a_call_that_returns_no_text_fails_rather_than_passing() -> None:
    """HTTP 200 is not an answer. The gateway step learned the same thing."""
    answers = all_answering()
    answers[(INVOKE_ACTION, SONNET_MODEL_ID)] = {"output": {"message": {}}}
    lab = harness(answers)

    assert ModelAccess(lab).run() is False
    assert detail(lab, f"{INVOKE_ACTION} {SONNET_MODEL_ID}") == NO_TEXT_DETAIL


def test_one_refused_model_still_measures_the_other() -> None:
    """The two models are separate entitlements, so one answer is not the pair."""
    answers = all_answering()
    answers[(INVOKE_ACTION, OPUS_MODEL_ID)] = error(
        "AccessDeniedException", "You don't have access to the model"
    )
    lab = harness(answers)

    assert ModelAccess(lab).run() is False
    assert rows(lab)[f"{INVOKE_ACTION} {SONNET_MODEL_ID}"] == PASS
    assert rows(lab)[f"{INVOKE_ACTION} {OPUS_MODEL_ID}"] == FAIL
    assert rows(lab)[f"{STREAM_ACTION} {OPUS_MODEL_ID}"] == PASS


def test_a_failure_sends_the_student_to_the_right_person() -> None:
    answers = all_answering()
    answers[(STREAM_ACTION, SONNET_MODEL_ID)] = error(
        "AccessDeniedException", "no identity-based policy allows the action"
    )
    lab = harness(answers)

    ModelAccess(lab).run()

    assert "different people" in printed(lab)


def test_a_throttled_run_says_to_retry_rather_than_to_report() -> None:
    answers = dict.fromkeys(all_answering(), error("ThrottlingException", "slow down"))
    lab = harness(answers)

    assert ModelAccess(lab).run() is False
    assert set(rows(lab).values()) == {SKIP}
    assert "re-run" in printed(lab)
    assert "Report" not in printed(lab)


def test_a_transport_failure_is_reported_and_does_not_raise() -> None:
    """One unreachable endpoint should not stop the second model being measured."""
    answers = all_answering()
    answers[(INVOKE_ACTION, SONNET_MODEL_ID)] = EndpointConnectionError(
        endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"
    )
    lab = harness(answers)

    assert ModelAccess(lab).run() is False
    assert rows(lab)[f"{INVOKE_ACTION} {SONNET_MODEL_ID}"] == FAIL
    assert rows(lab)[f"{INVOKE_ACTION} {OPUS_MODEL_ID}"] == PASS


def test_the_step_creates_nothing_and_registers_no_teardown() -> None:
    """It touches one read-only data-plane call, so step 14 has nothing to undo."""
    lab = harness(all_answering())

    ModelAccess(lab).run()

    assert lab.cleanups == []
