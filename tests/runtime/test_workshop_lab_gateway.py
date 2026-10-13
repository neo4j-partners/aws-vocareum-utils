"""Offline tests for step 11, the Lambda and Gateway boundary.

Nothing here reaches AWS or the network. `call_gateway` is the one method that
would, so it is replaced wholesale in most tests and driven directly in the few
that test the signing itself.

The tests worth reading are the ones about the two invoke checks. `tools/list`
never reaches the Lambda, and a refused `tools/call` comes back as HTTP 200, so
both of the obvious ways to check this step pass on an account where the tool
call does not work.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest
from botocore.exceptions import ClientError

from workshop_lab.gateway import (
    ADD_PERMISSION_CHECK,
    CREATE_FUNCTION_CHECK,
    CREATE_GATEWAY_CHECK,
    CREATE_TARGET_CHECK,
    DISCOVERABLE_CHECK,
    HANDLER_MARKER,
    HANDLER_SOURCE,
    INVOKE_CHECK,
    LIST_TARGETS_CHECK,
    TOOL_CALL_CHECK,
    TOOL_NAME,
    GatewayBoundary,
    package,
)
from workshop_lab.harness import FAIL, PASS, SKIP, Harness
from workshop_lab.naming import Names
from workshop_lab.selection import WORKSHOP_TAG_KEY

ACCOUNT = "123456789012"
REGION = "us-east-1"
LAMBDA_ROLE = f"arn:aws:iam::{ACCOUNT}:role/workshop-verify-lambda-role"
GATEWAY_ROLE = f"arn:aws:iam::{ACCOUNT}:role/workshop-verify-gateway-role"
LAMBDA_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:hotel-booking-verify-tool"
GATEWAY_URL = "https://verify-gateway.gateway.bedrock-agentcore.us-east-1.amazonaws.com"


def denial(operation: str, code: str = "AccessDeniedException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, operation)


def gone(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no"}}, operation
    )


class FakeLambda:
    def __init__(self) -> None:
        self.refuse: set[str] = set()
        self.exists = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if name in self.refuse:
            raise denial(name)

    def create_function(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_function", kwargs)
        self.exists = True
        return {"FunctionArn": LAMBDA_ARN}

    def get_function(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_function", kwargs))
        if not self.exists:
            raise gone("get_function")
        return {"Configuration": {"FunctionArn": LAMBDA_ARN}}

    def delete_function(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_function", kwargs)
        self.exists = False
        return {}

    def add_permission(self, **kwargs: Any) -> dict[str, Any]:
        self._record("add_permission", kwargs)
        return {}


class FakeControl:
    def __init__(self) -> None:
        self.refuse: set[str] = set()
        self.existing_gateways: list[dict[str, Any]] = []
        # None answers every gateway in one page, which is the ordinary case.
        # An int makes ListGateways paginate, which is the case the sweep used
        # to walk past.
        self.gateway_page_size: int | None = None
        self.existing_targets: list[dict[str, Any]] = []
        self.gateway_url = GATEWAY_URL
        self.listed_target_names: list[str] | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if name in self.refuse:
            raise denial(name)

    def list_gateways(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_gateways", kwargs)
        if self.gateway_page_size is None:
            return {"items": self.existing_gateways}
        start = int(kwargs.get("nextToken") or 0)
        end = start + self.gateway_page_size
        page: dict[str, Any] = {"items": self.existing_gateways[start:end]}
        if end < len(self.existing_gateways):
            page["nextToken"] = str(end)
        return page

    def get_gateway(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_gateway", kwargs))
        if kwargs["gatewayIdentifier"] == "stale-gw" and not self.existing_gateways:
            raise gone("get_gateway")
        return {"status": "READY"}

    def create_gateway(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_gateway", kwargs)
        return {"gatewayId": "gw-1", "gatewayUrl": self.gateway_url}

    def delete_gateway(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_gateway", kwargs)
        self.existing_gateways = []
        return {}

    def list_gateway_targets(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_gateway_targets", kwargs)
        if kwargs["gatewayIdentifier"] == "stale-gw":
            return {"items": self.existing_targets}
        names = (
            self.listed_target_names
            if self.listed_target_names is not None
            else ["verify-target"]
        )
        return {"items": [{"name": one, "targetId": "t-1"} for one in names]}

    def get_gateway_target(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_gateway_target", kwargs))
        if not self.existing_targets and kwargs["targetId"] == "stale-t":
            raise gone("get_gateway_target")
        return {"status": "READY"}

    def create_gateway_target(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_gateway_target", kwargs)
        return {"targetId": "t-1"}

    def delete_gateway_target(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_gateway_target", kwargs)
        self.existing_targets = []
        return {}


class FakeCredentials:
    access_key = "AKIAIOSFODNN7EXAMPLE"
    secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    token = None

    def get_frozen_credentials(self) -> FakeCredentials:
        return self


class FakeSession:
    def __init__(self, clients: dict[str, Any]) -> None:
        self.region_name = REGION
        self._clients = clients

    def client(self, name: str, region_name: str | None = None) -> Any:
        return self._clients[name]

    def get_credentials(self) -> FakeCredentials:
        return FakeCredentials()


@pytest.fixture(autouse=True)
def never_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workshop_lab.harness.time.sleep", lambda _: None)


@pytest.fixture
def awslambda() -> FakeLambda:
    return FakeLambda()


@pytest.fixture
def control() -> FakeControl:
    return FakeControl()


@pytest.fixture
def lab(awslambda: FakeLambda, control: FakeControl) -> Harness:
    printed: list[str] = []
    harness = Harness(
        session=FakeSession(
            {"lambda": awslambda, "bedrock-agentcore-control": control}
        ),
        echo=printed.append,
    )
    harness.account_id = ACCOUNT
    harness._names = Names(prefix="verify", account_id=ACCOUNT, region=REGION)
    harness.printed = printed  # type: ignore[attr-defined]
    return harness


class Replies:
    """Stands in for `call_gateway`, answering each message in turn."""

    def __init__(self, *answers: tuple[int, str]) -> None:
        self.answers = list(answers)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, message: dict[str, Any]) -> tuple[int, str]:
        self.sent.append(message)
        return self.answers.pop(0) if self.answers else (200, "{}")


def working_replies() -> Replies:
    return Replies(
        (200, '{"result": {"tools": []}}'),
        (200, json.dumps({"result": {"content": [{"text": HANDLER_MARKER}]}})),
    )


def build(
    lab: Harness, replies: Replies | None = None, **kwargs: Any
) -> GatewayBoundary:
    boundary = GatewayBoundary(
        lab,
        lambda_role=kwargs.pop("lambda_role", LAMBDA_ROLE),
        gateway_role=kwargs.pop("gateway_role", GATEWAY_ROLE),
    )
    boundary.call_gateway = replies if replies is not None else working_replies()  # type: ignore[method-assign]
    return boundary


def verdicts(lab: Harness) -> dict[str, str]:
    return {name: verdict for name, verdict, _ in lab.results}


def detail(lab: Harness, name: str) -> str:
    return next(text for row, _, text in lab.results if row == name)


def named(lab: Harness, prefix: str) -> str:
    return next(verdict for name, verdict, _ in lab.results if name.startswith(prefix))


# --- the handler ----------------------------------------------------------


def test_the_handler_returns_the_string_the_tool_call_looks_for() -> None:
    """The marker comes out of the Lambda and nowhere else.

    That is the whole design: finding it in the Gateway's reply is the only
    thing that distinguishes a working boundary from a Gateway answering out of
    its own configuration.
    """
    namespace: dict[str, Any] = {}
    exec(compile(HANDLER_SOURCE, "index.py", "exec"), namespace)

    assert namespace["handler"]({"a": 1}, None)["result"] == HANDLER_MARKER


def test_the_package_holds_the_handler_at_the_module_the_config_names() -> None:
    """`Handler="index.handler"` is a path into this archive."""
    with zipfile.ZipFile(io.BytesIO(package())) as archive:
        assert archive.namelist() == ["index.py"]
        assert HANDLER_MARKER in archive.read("index.py").decode()


# --- Lambda ---------------------------------------------------------------


def test_no_lambda_role_skips_the_function_and_the_permission(lab: Harness) -> None:
    build(lab, lambda_role=None).run()

    assert verdicts(lab)[CREATE_FUNCTION_CHECK] == SKIP
    assert verdicts(lab)[ADD_PERMISSION_CHECK] == SKIP
    assert (
        detail(lab, ADD_PERMISSION_CHECK)
        == "needs both the Lambda and the Gateway role"
    )


def test_the_function_name_is_cleared_before_it_is_created(
    lab: Harness, awslambda: FakeLambda
) -> None:
    """A re-run over its own leftover FAILs a create the account plainly allows."""
    awslambda.exists = True

    build(lab).run()

    order = [name for name, _ in awslambda.calls]
    assert order.index("delete_function") < order.index("create_function")


def test_the_function_carries_the_workshop_tag(
    lab: Harness, awslambda: FakeLambda
) -> None:
    build(lab).run()

    kwargs = next(k for name, k in awslambda.calls if name == "create_function")
    assert kwargs["FunctionName"] == "hotel-booking-verify-tool"
    assert WORKSHOP_TAG_KEY in kwargs["Tags"]
    assert kwargs["Handler"] == "index.handler"


def test_create_function_is_retried_through_the_role_consistency_error(
    lab: Harness, awslambda: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lambda refuses a role IAM has only just returned, as InvalidParameterValue."""
    failures = {"left": 2}
    original = awslambda.create_function

    def flaky(**kwargs: Any) -> dict[str, Any]:
        if failures["left"]:
            failures["left"] -= 1
            awslambda.calls.append(("create_function", kwargs))
            raise denial("create_function", "InvalidParameterValueException")
        return original(**kwargs)

    monkeypatch.setattr(awslambda, "create_function", flaky)

    build(lab).run()

    assert named(lab, CREATE_FUNCTION_CHECK) == PASS


def test_a_refused_create_skips_the_permission_but_still_builds_the_gateway(
    lab: Harness, awslambda: FakeLambda
) -> None:
    """The Gateway questions are separate from the Lambda ones and still get asked."""
    awslambda.refuse.add("create_function")

    build(lab).run()

    assert named(lab, CREATE_FUNCTION_CHECK) == FAIL
    assert verdicts(lab)[ADD_PERMISSION_CHECK] == SKIP
    assert verdicts(lab)[CREATE_GATEWAY_CHECK] == PASS
    assert CREATE_TARGET_CHECK not in verdicts(lab)


def test_add_permission_is_measured_on_its_own(
    lab: Harness, awslambda: FakeLambda
) -> None:
    """Provisioning catches only ResourceConflictException and dies on anything else.

    An account can hold a gateway role that already allows lambda:InvokeFunction
    and still fail the deployment on this one call, so it cannot be folded into
    the create.
    """
    awslambda.refuse.add("add_permission")

    build(lab).run()

    assert verdicts(lab)[ADD_PERMISSION_CHECK] == FAIL
    assert verdicts(lab)[CREATE_GATEWAY_CHECK] == PASS


def test_the_permission_names_the_gateway_role_as_principal(
    lab: Harness, awslambda: FakeLambda
) -> None:
    build(lab).run()

    kwargs = next(k for name, k in awslambda.calls if name == "add_permission")
    assert kwargs["Principal"] == GATEWAY_ROLE
    assert kwargs["Action"] == "lambda:InvokeFunction"


# --- Gateway --------------------------------------------------------------


def test_no_gateway_role_skips_the_gateway(lab: Harness) -> None:
    result = build(lab, gateway_role=None).run()

    assert result is None
    assert verdicts(lab)[CREATE_GATEWAY_CHECK] == SKIP


def test_the_gateway_asks_for_the_iam_authorizer(
    lab: Harness, control: FakeControl
) -> None:
    """A gateway with any other authorizer cannot be measured by a signed request."""
    build(lab).run()

    kwargs = next(k for name, k in control.calls if name == "create_gateway")
    assert kwargs["authorizerType"] == "AWS_IAM"
    assert kwargs["protocolType"] == "MCP"
    assert kwargs["name"] == "verify-gateway"


def test_the_gateway_is_tagged_and_its_target_cannot_be(
    lab: Harness, control: FakeControl
) -> None:
    """The one resource this notebook makes that no tag can ever reach.

    `CreateGatewayTarget` has no `tags` member, and there is no TagResource that
    accepts a target ARN either, so the teardown sweep finds a target only by
    enumerating the gateway that holds it. That is why the sweep unions tag with
    name rather than trusting the tag.
    """
    build(lab).run()

    gateway = next(k for name, k in control.calls if name == "create_gateway")
    assert gateway["tags"] == lab.names.tags_map

    target = next(k for name, k in control.calls if name == "create_gateway_target")
    assert "tags" not in target


def test_a_leftover_gateway_is_emptied_before_it_is_deleted(
    lab: Harness, control: FakeControl
) -> None:
    """A gateway still holding targets refuses a delete, and then survives the lab."""
    control.existing_gateways = [{"name": "verify-gateway", "gatewayId": "stale-gw"}]
    control.existing_targets = [{"targetId": "stale-t"}]

    build(lab).run()

    order = [name for name, _ in control.calls]
    assert order.index("delete_gateway_target") < order.index("delete_gateway")
    assert order.index("delete_gateway") < order.index("create_gateway")


def test_a_leftover_gateway_on_the_second_page_is_still_swept(
    lab: Harness, control: FakeControl
) -> None:
    """ListGateways has no boto3 paginator, and a leftover sits wherever it sits.

    A one-page read walked past exactly the resource the sweep exists to remove,
    and the create that followed then failed `ConflictException` and took the
    rest of step 11 down with it.
    """
    control.existing_gateways = [
        {"name": "someone-elses-gateway", "gatewayId": "other-gw"},
        {"name": "verify-gateway", "gatewayId": "stale-gw"},
    ]
    control.gateway_page_size = 1

    build(lab).run()

    deleted = [
        kwargs["gatewayIdentifier"]
        for name, kwargs in control.calls
        if name == "delete_gateway"
    ]
    assert "stale-gw" in deleted
    assert "other-gw" not in deleted


def test_the_sweep_records_nothing(lab: Harness, control: FakeControl) -> None:
    """Deleting last run's litter answers no permission question."""
    control.existing_gateways = [{"name": "verify-gateway", "gatewayId": "stale-gw"}]
    control.existing_targets = [{"targetId": "stale-t"}]

    build(lab).run()

    assert not [name for name, _, _ in lab.results if "stale" in name]


def test_a_refused_list_gateways_does_not_stop_the_step(
    lab: Harness, control: FakeControl
) -> None:
    """The sweep is housekeeping. A refusal there is not the step's measurement."""
    control.refuse.add("list_gateways")

    build(lab).run()

    assert verdicts(lab)[CREATE_GATEWAY_CHECK] == PASS


def test_a_refused_gateway_stops_the_step(lab: Harness, control: FakeControl) -> None:
    control.refuse.add("create_gateway")

    result = build(lab).run()

    assert result is None
    assert verdicts(lab)[CREATE_GATEWAY_CHECK] == FAIL
    assert INVOKE_CHECK not in verdicts(lab)


def test_the_created_resources_register_their_deletes(
    lab: Harness, control: FakeControl, awslambda: FakeLambda
) -> None:
    build(lab).run()

    labels = [label for label, _ in lab.cleanups]
    assert labels == [
        "lambda hotel-booking-verify-tool",
        "gateway gw-1",
        "gateway target t-1",
    ]


# --- target ---------------------------------------------------------------


def test_the_target_supplies_the_field_the_sdk_omits(
    lab: Harness, control: FakeControl
) -> None:
    """The SDK does not list credentialProviderConfigurations as required.

    The service rejects the request without it.
    """
    build(lab).run()

    kwargs = next(k for name, k in control.calls if name == "create_gateway_target")
    assert kwargs["credentialProviderConfigurations"] == [
        {"credentialProviderType": "GATEWAY_IAM_ROLE"}
    ]
    assert kwargs["targetConfiguration"]["mcp"]["lambda"]["lambdaArn"] == LAMBDA_ARN


def test_a_refused_target_skips_the_tool_call_but_still_lists(
    lab: Harness, control: FakeControl
) -> None:
    control.refuse.add("create_gateway_target")

    build(lab).run()

    assert verdicts(lab)[CREATE_TARGET_CHECK] == FAIL
    assert verdicts(lab)[LIST_TARGETS_CHECK] == PASS
    assert verdicts(lab)[TOOL_CALL_CHECK] == SKIP


# --- discoverability ------------------------------------------------------


def test_a_refused_list_says_what_it_costs_the_cleanup(
    lab: Harness, control: FakeControl
) -> None:
    """Without this call a gateway cannot be emptied, so it cannot be deleted."""
    control.refuse.add("list_gateway_targets")

    build(lab).run()

    assert verdicts(lab)[LIST_TARGETS_CHECK] == FAIL
    assert verdicts(lab)[DISCOVERABLE_CHECK] == FAIL
    assert "cannot empty a gateway" in detail(lab, DISCOVERABLE_CHECK)


def test_a_list_that_answers_without_the_created_target_is_a_failure(
    lab: Harness, control: FakeControl
) -> None:
    control.listed_target_names = ["something-else"]

    build(lab).run()

    assert verdicts(lab)[LIST_TARGETS_CHECK] == PASS
    assert verdicts(lab)[DISCOVERABLE_CHECK] == FAIL


def test_no_target_means_no_discoverability_claim(
    lab: Harness, control: FakeControl
) -> None:
    """Nothing was created, so a list that omits it says nothing."""
    control.refuse.add("create_gateway_target")

    build(lab).run()

    assert DISCOVERABLE_CHECK not in verdicts(lab)


# --- the signed call ------------------------------------------------------


def test_the_happy_path_passes_both_invoke_checks(lab: Harness) -> None:
    build(lab).run()

    assert verdicts(lab)[INVOKE_CHECK] == PASS
    assert verdicts(lab)[TOOL_CALL_CHECK] == PASS


def test_the_two_calls_are_tools_list_then_tools_call(lab: Harness) -> None:
    replies = working_replies()

    build(lab, replies).run()

    assert [one["method"] for one in replies.sent] == ["tools/list", "tools/call"]


def test_the_tool_name_is_the_gateways_own_spelling(lab: Harness) -> None:
    """Target name, three underscores, then the name from the tool schema."""
    replies = working_replies()

    build(lab, replies).run()

    assert replies.sent[1]["params"]["name"] == f"verify-target___{TOOL_NAME}"


def test_a_tool_call_answering_200_without_the_lambda_output_is_a_failure(
    lab: Harness,
) -> None:
    """A refused invoke arrives as HTTP 200 with a JSON-RPC error in the body.

    Checking the status alone would pass on an account where the Gateway cannot
    reach the Lambda at all.
    """
    replies = Replies(
        (200, '{"result": {"tools": []}}'),
        (200, '{"error": {"code": -32603, "message": "AccessDeniedException"}}'),
    )

    build(lab, replies).run()

    assert verdicts(lab)[INVOKE_CHECK] == PASS
    assert verdicts(lab)[TOOL_CALL_CHECK] == FAIL
    assert "without the Lambda's output" in detail(lab, TOOL_CALL_CHECK)


def test_a_refused_tools_list_still_attempts_the_tool_call(lab: Harness) -> None:
    """They measure different things, and the second is the one the workshop needs."""
    replies = Replies(
        (403, "denied"),
        (200, json.dumps({"result": {"content": [{"text": HANDLER_MARKER}]}})),
    )

    build(lab, replies).run()

    assert verdicts(lab)[INVOKE_CHECK] == FAIL
    assert verdicts(lab)[TOOL_CALL_CHECK] == PASS


def test_a_gateway_without_a_url_skips_both_invoke_checks(
    lab: Harness, control: FakeControl
) -> None:
    control.gateway_url = ""

    build(lab).run()

    assert verdicts(lab)[INVOKE_CHECK] == SKIP
    assert TOOL_CALL_CHECK not in verdicts(lab)


def test_call_gateway_signs_the_request_and_returns_the_body(
    lab: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one method that would reach the network, driven directly."""
    boundary = GatewayBoundary(lab, LAMBDA_ROLE, GATEWAY_ROLE)
    boundary.gateway_url = GATEWAY_URL
    seen: dict[str, Any] = {}

    class FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true}'

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        seen["headers"] = dict(request.headers)
        seen["data"] = request.data
        return FakeResponse()

    monkeypatch.setattr("workshop_lab.gateway.urllib.request.urlopen", fake_urlopen)

    status, text = boundary.call_gateway({"jsonrpc": "2.0", "id": 1})

    assert (status, text) == (200, '{"ok": true}')
    authorization = next(
        value
        for key, value in seen["headers"].items()
        if key.lower() == "authorization"
    )
    assert "AWS4-HMAC-SHA256" in authorization
    assert "bedrock-agentcore" in authorization


def test_a_network_failure_is_a_status_of_zero_rather_than_a_traceback(
    lab: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cell that raises stops the notebook. A row that says so does not."""
    import urllib.error

    boundary = GatewayBoundary(lab, LAMBDA_ROLE, GATEWAY_ROLE)
    boundary.gateway_url = GATEWAY_URL

    def refuse(request: Any, timeout: int = 0) -> Any:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("workshop_lab.gateway.urllib.request.urlopen", refuse)

    status, text = boundary.call_gateway({"jsonrpc": "2.0", "id": 1})

    assert status == 0
    assert "no route to host" in text


# --- names ----------------------------------------------------------------


def test_every_check_names_an_iam_action_or_a_behaviour(lab: Harness) -> None:
    """Permission rows are keyed on these strings."""
    build(lab).run()

    for name, _, _ in lab.results:
        assert name.startswith(("agentcore:", "lambda:", "gateway "))
