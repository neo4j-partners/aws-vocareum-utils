"""Offline tests for step 9, the AgentCore Runtime.

Nothing here reaches AWS. The fakes answer the four calls the step makes and can
be told to refuse any of them, which is the whole point: the step's job is to
turn a refusal into one row that names the IAM action, and to keep a container
that started but does not answer distinguishable from a permission it does not
have.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from workshop_lab.harness import FAIL, PASS, SKIP, Harness
from workshop_lab.naming import Names
from workshop_lab.runtime import (
    CREATE_CHECK,
    ECHO_CHECK,
    ENDPOINT_CHECK,
    INVOKE_CHECK,
    READY_CHECK,
    AgentRuntime,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/workshop-verify-runtime-role"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/verify_runtime-ab"


def denial(operation: str, code: str = "AccessDeniedException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, operation)


class FakeBody:
    """The streaming body InvokeAgentRuntime returns under `response`."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


class FakeControl:
    """The AgentCore control plane, as much of it as step 9 touches."""

    def __init__(self) -> None:
        self.refuse: set[str] = set()
        self.runtime_states = ["READY"]
        self.endpoint_states = ["READY"]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.create_validation_failures = 0

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if name in self.refuse:
            raise denial(name)

    def create_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_agent_runtime", kwargs)
        if self.create_validation_failures:
            self.create_validation_failures -= 1
            raise denial("create_agent_runtime", "ValidationException")
        return {"agentRuntimeId": "verify_runtime-ab", "agentRuntimeArn": RUNTIME_ARN}

    def get_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_agent_runtime", kwargs)
        return {
            "status": self.runtime_states.pop(0) if self.runtime_states else "READY"
        }

    def create_agent_runtime_endpoint(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_agent_runtime_endpoint", kwargs)
        return {"name": kwargs["name"]}

    def get_agent_runtime_endpoint(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_agent_runtime_endpoint", kwargs)
        return {
            "status": self.endpoint_states.pop(0) if self.endpoint_states else "READY"
        }

    def delete_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_agent_runtime", kwargs)
        return {}

    def delete_agent_runtime_endpoint(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_agent_runtime_endpoint", kwargs)
        return {}


class FakeDataplane:
    def __init__(self, body: bytes = b'{"echo": {"ping": "verify"}}') -> None:
        self.refuse = False
        self.body = body
        self.calls: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.refuse:
            raise denial("invoke_agent_runtime")
        return {"response": FakeBody(self.body)}


class FakeSession:
    def __init__(self, clients: dict[str, Any]) -> None:
        self.region_name = REGION
        self._clients = clients

    def client(self, name: str, region_name: str | None = None) -> Any:
        return self._clients[name]


@pytest.fixture(autouse=True)
def never_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The waiters poll on a ten second interval. Tests do not wait for it."""
    monkeypatch.setattr("workshop_lab.harness.time.sleep", lambda _: None)


@pytest.fixture
def control() -> FakeControl:
    return FakeControl()


@pytest.fixture
def dataplane() -> FakeDataplane:
    return FakeDataplane()


@pytest.fixture
def lab(control: FakeControl, dataplane: FakeDataplane) -> Harness:
    printed: list[str] = []
    harness = Harness(
        session=FakeSession(
            {"bedrock-agentcore-control": control, "bedrock-agentcore": dataplane}
        ),
        echo=printed.append,
    )
    harness.account_id = ACCOUNT
    harness._names = Names(prefix="verify", account_id=ACCOUNT, region=REGION)
    harness.printed = printed  # type: ignore[attr-defined]
    return harness


def verdicts(lab: Harness) -> dict[str, str]:
    return {name: verdict for name, verdict, _ in lab.results}


def detail(lab: Harness, name: str) -> str:
    return next(text for row, _, text in lab.results if row == name)


# --- the gates ------------------------------------------------------------


def test_no_image_skips_the_whole_step(lab: Harness) -> None:
    """Step 8 produced nothing, so there is nothing to run."""
    result = AgentRuntime(lab, image_pushed=False, runtime_role=ROLE_ARN).run()

    assert result is None
    assert verdicts(lab) == {CREATE_CHECK: SKIP}
    assert detail(lab, CREATE_CHECK) == "no image in ECR from step 8"


def test_no_runtime_role_skips_the_whole_step(lab: Harness) -> None:
    result = AgentRuntime(lab, image_pushed=True, runtime_role=None).run()

    assert result is None
    assert verdicts(lab) == {CREATE_CHECK: SKIP}
    assert detail(lab, CREATE_CHECK) == "no Runtime role from step 6"


def test_a_skipped_step_records_exactly_one_row(lab: Harness) -> None:
    """The rows downstream of a create that never ran say nothing about the account.

    A FAIL on `InvokeAgentRuntime` here would read as an invoke that was refused,
    which is a claim about a permission nothing measured.
    """
    AgentRuntime(lab, image_pushed=False, runtime_role=ROLE_ARN).run()

    assert len(lab.results) == 1


# --- create ---------------------------------------------------------------


def test_the_happy_path_records_all_five_rows(lab: Harness) -> None:
    result = AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert result == "verify_runtime-ab"
    assert verdicts(lab) == {
        CREATE_CHECK: PASS,
        READY_CHECK: PASS,
        ENDPOINT_CHECK: PASS,
        INVOKE_CHECK: PASS,
        ECHO_CHECK: PASS,
    }


def test_create_names_the_image_the_role_and_a_public_network(
    lab: Harness, control: FakeControl
) -> None:
    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    kwargs = next(k for name, k in control.calls if name == "create_agent_runtime")
    assert kwargs["agentRuntimeName"] == "verify_runtime"
    assert (
        kwargs["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
        == lab.names.image_uri
    )
    assert kwargs["roleArn"] == ROLE_ARN
    assert kwargs["networkConfiguration"] == {"networkMode": "PUBLIC"}


def test_the_runtime_and_its_endpoint_are_tagged_in_their_own_create(
    lab: Harness, control: FakeControl
) -> None:
    """Tagged in the create request, not by a call after it.

    A separate TagResource leaves a window where the resource is live and
    unrecognisable to a tag sweep, and the window is the whole of the runtime's
    several-minute create. Nothing here reaches an endpoint before the runtime is
    ready, so both are checked together.
    """
    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    tagged = {
        name: kwargs
        for name, kwargs in control.calls
        if name in {"create_agent_runtime", "create_agent_runtime_endpoint"}
    }
    assert set(tagged) == {"create_agent_runtime", "create_agent_runtime_endpoint"}
    assert all(kwargs["tags"] == lab.names.tags_map for kwargs in tagged.values())


def test_create_is_retried_through_a_validation_exception(
    lab: Harness, control: FakeControl
) -> None:
    """A role IAM has returned is not yet a role AgentCore will accept.

    The refusal arrives as ValidationException and is eventual consistency, not a
    denial. Recording it would report a permission the account has as one it does
    not.
    """
    control.create_validation_failures = 2

    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    creates = [name for name, _ in control.calls if name == "create_agent_runtime"]
    assert len(creates) == 3
    assert verdicts(lab)[CREATE_CHECK] == PASS


def test_a_refused_create_stops_the_step(lab: Harness, control: FakeControl) -> None:
    control.refuse.add("create_agent_runtime")

    result = AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert result is None
    assert verdicts(lab) == {CREATE_CHECK: FAIL}


def test_a_created_runtime_registers_its_delete(
    lab: Harness, control: FakeControl
) -> None:
    """Nothing else in the notebook knows this runtime exists."""
    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    labels = [label for label, _ in lab.cleanups]
    assert "agent runtime verify_runtime-ab" in labels

    for _, delete in lab.cleanups:
        delete()
    deleted = [name for name, _ in control.calls if name.startswith("delete_")]
    assert "delete_agent_runtime" in deleted
    assert "delete_agent_runtime_endpoint" in deleted


# --- ready ----------------------------------------------------------------


def test_a_runtime_that_settles_in_create_failed_reads_as_a_failure(
    lab: Harness, control: FakeControl
) -> None:
    control.runtime_states = ["CREATING", "CREATE_FAILED"]

    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert verdicts(lab)[READY_CHECK] == FAIL
    assert "CREATE_FAILED" in detail(lab, READY_CHECK)


@pytest.mark.parametrize("state", ["ACTIVE", "READY"])
def test_both_settled_names_count_as_ready(
    lab: Harness, control: FakeControl, state: str
) -> None:
    """AgentCore has used both names for the same state across API versions."""
    control.runtime_states = [state]
    control.endpoint_states = [state]

    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert verdicts(lab)[READY_CHECK] == PASS
    assert verdicts(lab)[INVOKE_CHECK] == PASS


# --- endpoint -------------------------------------------------------------


def test_a_refused_endpoint_skips_the_invoke(
    lab: Harness, control: FakeControl
) -> None:
    control.refuse.add("create_agent_runtime_endpoint")

    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert verdicts(lab)[ENDPOINT_CHECK] == FAIL
    assert verdicts(lab)[INVOKE_CHECK] == SKIP
    assert detail(lab, INVOKE_CHECK) == "no endpoint to invoke"


def test_an_endpoint_that_does_not_come_up_skips_the_invoke_naming_its_state(
    lab: Harness, control: FakeControl
) -> None:
    """An endpoint that exists and is not ready is not an endpoint that was refused."""
    control.endpoint_states = ["CREATING", "CREATE_FAILED"]

    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert verdicts(lab)[ENDPOINT_CHECK] == PASS
    assert verdicts(lab)[INVOKE_CHECK] == SKIP
    assert detail(lab, INVOKE_CHECK) == "the endpoint settled in CREATE_FAILED"


def test_the_endpoint_carries_the_name_the_invoke_qualifies_with(
    lab: Harness, control: FakeControl, dataplane: FakeDataplane
) -> None:
    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    created = next(
        k for name, k in control.calls if name == "create_agent_runtime_endpoint"
    )
    assert created["name"] == lab.names.runtime_endpoint
    assert dataplane.calls[0]["qualifier"] == lab.names.runtime_endpoint


# --- invoke ---------------------------------------------------------------


def test_the_payload_carries_the_prefix_the_echo_check_looks_for(
    lab: Harness, dataplane: FakeDataplane
) -> None:
    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert json.loads(dataplane.calls[0]["payload"]) == {"ping": "verify"}
    assert dataplane.calls[0]["agentRuntimeArn"] == RUNTIME_ARN


def test_a_ready_runtime_that_echoes_nothing_is_a_failure_not_a_denial(
    lab: Harness, dataplane: FakeDataplane
) -> None:
    """READY means the service accepted the image, not that the container works.

    These are two rows on purpose. A broken container is a different problem from
    a refused invoke and has to read as one.
    """
    dataplane.body = b'{"echo": {}}'

    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert verdicts(lab)[INVOKE_CHECK] == PASS
    assert verdicts(lab)[ECHO_CHECK] == FAIL


def test_a_refused_invoke_records_no_echo_row(
    lab: Harness, dataplane: FakeDataplane
) -> None:
    """There is no answer to look in, so there is nothing to say about the echo."""
    dataplane.refuse = True

    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert verdicts(lab)[INVOKE_CHECK] == FAIL
    assert ECHO_CHECK not in verdicts(lab)


def test_an_undecodable_answer_does_not_raise(
    lab: Harness, dataplane: FakeDataplane
) -> None:
    """A container is free to answer bytes that are not UTF-8."""
    dataplane.body = b"\xff\xfe not utf-8"

    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert verdicts(lab)[ECHO_CHECK] == FAIL


def test_a_long_answer_is_truncated_in_the_result_row(
    lab: Harness, dataplane: FakeDataplane
) -> None:
    """A result row is read in a terminal, and a runtime may answer kilobytes."""
    dataplane.body = b'{"echo": "verify' + b"x" * 5000 + b'"}'

    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    assert len(detail(lab, ECHO_CHECK)) <= 120


# --- names ----------------------------------------------------------------


def test_every_check_names_an_iam_action_or_a_behaviour(lab: Harness) -> None:
    """Permission rows are keyed on these strings.

    A row named after a boto3 method would have to be translated by hand before it
    could be matched against the tracker.
    """
    AgentRuntime(lab, image_pushed=True, runtime_role=ROLE_ARN).run()

    for name, _, _ in lab.results:
        assert name.startswith(("agentcore:", "runtime "))
