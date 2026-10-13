"""Offline tests for step 14, the teardown.

This is the step that decides whether a student's account is empty, so the tests
that matter are the ones about what it must never say.

Three of them are the whole reason the step is shaped the way it is. A refused
listing must not be reported as an empty account. A resource CloudFormation owns
must not be deleted, because the stack fails its own teardown at session end if
one of its resources went by hand. And a delete that returned is not a delete
that finished, so the confirm pass is what the verdict is built on.

The fakes below are one class per service, each recording its calls and each able
to refuse a named operation. Nothing here reaches AWS.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from workshop_lab.harness import FAIL, PASS, Harness, items_of
from workshop_lab.naming import Names
from workshop_lab.selection import (
    AGENTCORE_RUNTIME_LOG_PREFIX,
    WORKSHOP_TAG_KEY,
    WORKSHOP_TAG_VALUE,
)
from workshop_lab.teardown import (
    EMPTY_CHECK,
    Target,
    Teardown,
    carries_workshop_tag,
    enumerated_check,
    error_code,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"
PREFIX = "verify"
SOURCE_BUCKET = f"bedrock-agentcore-codebuild-sources-{ACCOUNT}-{REGION}"
TAGS = {WORKSHOP_TAG_KEY: WORKSHOP_TAG_VALUE}


def denial(operation: str, code: str = "AccessDeniedException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, operation)


def absent(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, operation
    )


class Paginator:
    """Stands in for boto3's paginator, one page, which is enough here.

    It defers to the owning fake at `paginate` time rather than capturing a page
    at `get_paginator` time, so the arguments the step passes are recorded and a
    fake that answers an operation with a method is still consulted. Capturing
    early hid both: every paginated call recorded empty arguments, and a fake
    that served a listing from an attribute never reached the code under test.
    """

    def __init__(self, fake: Fake, operation: str) -> None:
        self.fake = fake
        self.operation = operation

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [self.fake.page(self.operation, kwargs)]


class Fake:
    """A service client that records every call and can refuse named ones."""

    def __init__(self) -> None:
        self.refuse: set[str] = set()
        self.deleted: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.listings: dict[str, Any] = {}
        self.tags: dict[str, Any] = {}

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if name in self.refuse:
            raise denial(name)

    def page(self, operation: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """One page for a paginated call, from a method when the fake has one."""
        method = getattr(self, operation, None)
        if method is not None:
            return method(**kwargs)
        self._record(operation, kwargs)
        return self.listings.get(operation, {})

    def get_paginator(self, operation: str) -> Paginator:
        return Paginator(self, operation)


class FakeControl(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.runtimes: list[dict[str, Any]] = []
        self.endpoints: list[dict[str, Any]] = []
        self.gateways: list[dict[str, Any]] = []
        self.gateway_targets: list[dict[str, Any]] = []
        self.memories: list[dict[str, Any]] = []
        self.pages_left = 0

    def list_agent_runtimes(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_agent_runtimes", kwargs)
        if self.pages_left and not kwargs.get("nextToken"):
            return {"agentRuntimes": self.runtimes[:1], "nextToken": "more"}
        if kwargs.get("nextToken"):
            return {"agentRuntimes": self.runtimes[1:]}
        return {"agentRuntimes": self.runtimes}

    def list_agent_runtime_endpoints(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_agent_runtime_endpoints", kwargs)
        return {"runtimeEndpoints": self.endpoints}

    def list_gateways(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_gateways", kwargs)
        return {"items": self.gateways}

    def list_gateway_targets(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_gateway_targets", kwargs)
        return {"items": self.gateway_targets}

    def list_memories(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_memories", kwargs)
        return {"memories": self.memories}

    def list_tags_for_resource(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_tags_for_resource", kwargs)
        return {"tags": self.tags.get(kwargs["resourceArn"], {})}

    def get_gateway(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_gateway", kwargs)
        return {"status": "READY"}

    def delete_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_agent_runtime", kwargs)
        self.deleted.append(f"runtime {kwargs['agentRuntimeId']}")
        return {}

    def delete_agent_runtime_endpoint(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_agent_runtime_endpoint", kwargs)
        self.deleted.append(f"endpoint {kwargs['endpointName']}")
        return {}

    def delete_gateway(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_gateway", kwargs)
        self.gateways = []
        self.deleted.append(f"gateway {kwargs['gatewayIdentifier']}")
        return {}

    def delete_gateway_target(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_gateway_target", kwargs)
        self.gateway_targets = []
        self.deleted.append(f"gateway target {kwargs['targetId']}")
        return {}

    def delete_memory(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_memory", kwargs)
        self.deleted.append(f"memory {kwargs['memoryId']}")
        return {}

    def get_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_agent_runtime", kwargs)
        raise absent("GetAgentRuntime")

    def get_agent_runtime_endpoint(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_agent_runtime_endpoint", kwargs)
        raise absent("GetAgentRuntimeEndpoint")

    def get_gateway_target(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_gateway_target", kwargs)
        raise absent("GetGatewayTarget")

    def get_memory(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_memory", kwargs)
        raise absent("GetMemory")


class FakeIam(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.inline: list[str] = []
        self.attached: list[dict[str, str]] = []

    def list_role_policies(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_role_policies", kwargs)
        return {"PolicyNames": self.inline}

    def list_attached_role_policies(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_attached_role_policies", kwargs)
        return {"AttachedPolicies": self.attached}

    def delete_role_policy(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_role_policy", kwargs)
        return {}

    def detach_role_policy(self, **kwargs: Any) -> dict[str, Any]:
        self._record("detach_role_policy", kwargs)
        return {}

    def delete_role(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_role", kwargs)
        self.deleted.append(f"role {kwargs['RoleName']}")
        return {}

    def get_role(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_role", kwargs)
        raise ClientError(
            {"Error": {"Code": "NoSuchEntity", "Message": "x"}}, "GetRole"
        )


class FakeS3(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.objects: list[dict[str, Any]] = []
        self.bucket_exists = True
        self.delete_errors: list[dict[str, str]] = []

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self._record("head_bucket", kwargs)
        if not self.bucket_exists:
            raise ClientError({"Error": {"Code": "404", "Message": ""}}, "HeadBucket")
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_objects_v2", kwargs)
        return {"Contents": self.objects} if self.objects else {}

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_objects", kwargs)
        if self.delete_errors:
            return {"Errors": self.delete_errors}
        keys = {one["Key"] for one in kwargs["Delete"]["Objects"]}
        self.objects = [one for one in self.objects if one["Key"] not in keys]
        return {}

    def delete_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_bucket", kwargs)
        self.bucket_exists = False
        self.deleted.append(f"bucket {kwargs['Bucket']}")
        return {}


class FakeLogs(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.groups: list[dict[str, Any]] = []

    def describe_log_groups(self, **kwargs: Any) -> dict[str, Any]:
        self._record("describe_log_groups", kwargs)
        prefix = kwargs.get("logGroupNamePrefix", "")
        return {
            "logGroups": [
                one for one in self.groups if one["logGroupName"].startswith(prefix)
            ]
        }

    def list_tags_for_resource(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_tags_for_resource", kwargs)
        return {"tags": self.tags.get(kwargs["resourceArn"], {})}

    def delete_log_group(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_log_group", kwargs)
        self.groups = [
            one for one in self.groups if one["logGroupName"] != kwargs["logGroupName"]
        ]
        self.deleted.append(f"log group {kwargs['logGroupName']}")
        return {}


class FakeCodeBuild(Fake):
    """No `batch_get_projects`, on purpose.

    `lab.template` does not grant `codebuild:BatchGetProjects`, so a student
    session has no such call. Leaving it off the fake means production code that
    reaches for it again fails loudly here rather than in a lab.
    """

    def __init__(self) -> None:
        super().__init__()
        self.projects: list[str] = []

    def list_projects(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_projects", kwargs)
        return {"projects": list(self.projects)}

    def delete_project(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_project", kwargs)
        self.projects = [one for one in self.projects if one != kwargs["name"]]
        self.deleted.append(f"project {kwargs['name']}")
        return {}


class FakeCfn(Fake):
    pass


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sleeps advance a fake clock rather than doing nothing.

    Every waiter in the harness is bounded by wall clock rather than by a count
    of attempts, so a sleep patched to a no-op turns a timeout into a busy loop
    that still takes its full 60 or 300 seconds. Advancing the clock instead is
    what lets a test of the timeout finish, and it keeps the loop counts
    deterministic.
    """
    now = [0.0]

    def advance(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("workshop_lab.harness.time.sleep", advance)
    monkeypatch.setattr("workshop_lab.harness.time.monotonic", lambda: now[0])


@pytest.fixture
def clients() -> dict[str, Any]:
    return {
        "bedrock-agentcore-control": FakeControl(),
        "cloudformation": FakeCfn(),
        "iam": FakeIam(),
        "ecr": Fake(),
        "s3": FakeS3(),
        "codebuild": FakeCodeBuild(),
        "lambda": Fake(),
        "secretsmanager": Fake(),
        "dynamodb": Fake(),
        "logs": FakeLogs(),
    }


class FakeSession:
    def __init__(self, clients: dict[str, Any]) -> None:
        self.region_name = REGION
        self._clients = clients

    def client(self, name: str, region_name: str | None = None) -> Any:
        return self._clients[name]


@pytest.fixture
def lab(clients: dict[str, Any]) -> Harness:
    printed: list[str] = []
    harness = Harness(prefix=PREFIX, session=FakeSession(clients), echo=printed.append)
    harness.account_id = ACCOUNT
    harness._names = Names(prefix=PREFIX, account_id=ACCOUNT, region=REGION)
    harness.printed = printed  # type: ignore[attr-defined]
    return harness


def verdicts(lab: Harness) -> dict[str, str]:
    return {name: verdict for name, verdict, _ in lab.results}


def detail(lab: Harness, name: str) -> str:
    return next(text for row, _, text in lab.results if row == name)


def labels(targets: list[Target]) -> list[str]:
    return [one.label for one in targets]


# --- the small helpers ----------------------------------------------------


def test_the_error_code_is_read_out_of_a_client_error() -> None:
    assert error_code(denial("Whatever", "ThrottlingException")) == (
        "ThrottlingException"
    )


def test_a_non_aws_failure_reports_its_message_truncated() -> None:
    """A row carrying nothing reads as a check that did not run."""
    assert error_code(RuntimeError("x" * 400)) == "x" * 120


def test_the_agentcore_list_key_is_found_rather_than_guessed() -> None:
    """Gateways arrive under "items", runtimes and memories under keys of their own.

    Three hard-coded guesses would not survive a boto3 upgrade renaming one.
    """
    assert items_of({"nextToken": "t", "items": [{"a": 1}]}) == [{"a": 1}]
    assert items_of({"agentRuntimes": [{"b": 2}]}) == [{"b": 2}]
    assert items_of({"ResponseMetadata": {}, "nextToken": "t"}) == []


def test_a_list_of_plain_strings_is_not_mistaken_for_the_items_key() -> None:
    assert items_of({"names": ["a", "b"]}) == []


def test_a_refused_tag_read_counts_as_untagged_rather_than_raising() -> None:
    """The name prefixes already recognise everything this notebook creates."""

    def refuse() -> Any:
        raise denial("ListTagsForResource")

    assert carries_workshop_tag(refuse) is False


def test_an_older_boto3_with_no_tagging_operation_counts_as_untagged() -> None:
    """Step 10 tests hasattr before tagging for the same reason."""

    def missing() -> Any:
        raise AttributeError("no such operation")

    assert carries_workshop_tag(missing) is False


def test_the_tag_is_read_in_both_the_dict_and_the_list_shape() -> None:
    """Services disagree about the shape, and the casing of the keys with it."""
    assert carries_workshop_tag(lambda: TAGS) is True
    assert carries_workshop_tag(
        lambda: [{"Key": WORKSHOP_TAG_KEY, "Value": WORKSHOP_TAG_VALUE}]
    )
    assert carries_workshop_tag(
        lambda: [{"key": WORKSHOP_TAG_KEY, "value": WORKSHOP_TAG_VALUE}]
    )
    assert carries_workshop_tag(lambda: [{"Key": "Other", "Value": "x"}]) is False


# --- selection ------------------------------------------------------------


def test_the_prefixes_cover_names_that_carry_the_prefix_in_the_middle(
    lab: Harness,
) -> None:
    """A bare prefix match misses the ECR repository, the project and the Lambda."""
    sweep = Teardown(lab)

    assert sweep.ours("verify_runtime")
    assert sweep.ours("workshop-verify-codebuild-role")
    assert sweep.ours("bedrock-agentcore-verify")
    assert sweep.ours("hotel-booking-verify-tool")
    assert not sweep.ours("some-other-role")


def test_a_resource_is_ours_by_tag_when_the_name_does_not_match(
    lab: Harness,
) -> None:
    """agentcore:TagResource is itself measured, so a tag-only sweep has a hole,
    and so does a name-only one."""
    sweep = Teardown(lab)

    assert sweep.ours("unrelated-name", "arn:x", lambda: TAGS)


def test_a_stack_owned_name_is_never_ours_however_it_reads(lab: Harness) -> None:
    """Deleting a stack's resource by hand leaves the stack to fail its own delete."""
    sweep = Teardown(lab)
    sweep.stack_owned = {"workshop-verify-runtime-role"}

    assert not sweep.ours("workshop-verify-runtime-role")
    assert not sweep.ours("x", "workshop-verify-runtime-role", lambda: TAGS)


def test_stack_ownership_is_recorded_by_arn_and_by_its_last_segment(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """A physical id is sometimes an ARN and sometimes a bare name.

    An IAM role arrives as an ARN and a DynamoDB table arrives as a bare name, and
    the two are compared against the same set, so an ARN has to be recorded twice.
    """
    role_arn = f"arn:aws:iam::{ACCOUNT}:role/workshop-lab-runtime"
    clients["cloudformation"].listings = {
        "list_stacks": {"StackSummaries": [{"StackName": "lab", "StackStatus": "OK"}]},
        "list_stack_resources": {
            "StackResourceSummaries": [{"PhysicalResourceId": role_arn}]
        },
    }

    owned = Teardown(lab).read_stack_owned()

    assert role_arn in owned
    assert "workshop-lab-runtime" in owned


def test_a_deleted_stacks_resources_are_not_protected(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """A DELETE_COMPLETE stack owns nothing, and its old names are fair game."""
    clients["cloudformation"].listings = {
        "list_stacks": {
            "StackSummaries": [{"StackName": "old", "StackStatus": "DELETE_COMPLETE"}]
        },
        "list_stack_resources": {
            "StackResourceSummaries": [{"PhysicalResourceId": "leftover"}]
        },
    }

    assert Teardown(lab).read_stack_owned() == set()


def test_a_refused_stack_listing_skips_nothing_and_records_nothing(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """The list only ever widens what is skipped, so losing it deletes no more.

    Losing it quietly could, so it is printed.
    """
    clients["cloudformation"].refuse.add("list_stacks")

    sweep = Teardown(lab)
    assert sweep.read_stack_owned() == set()
    assert sweep.lab.results == []
    assert "could not read CloudFormation stacks" in "\n".join(lab.printed)  # type: ignore[attr-defined]


# --- enumeration ----------------------------------------------------------


def test_a_refused_listing_is_recorded_rather_than_raised(lab: Harness) -> None:
    sweep = Teardown(lab)

    def refuse() -> list[Any]:
        raise denial("ListRoles")

    assert sweep.enumerate_all("iam roles", refuse) == []
    assert verdicts(lab)[enumerated_check("iam roles")] == FAIL
    assert sweep.enumeration_failures == ["iam roles: AccessDeniedException"]


def test_a_listing_that_worked_records_nothing(lab: Harness) -> None:
    """A successful listing is not a permission result, only a refused one is."""
    sweep = Teardown(lab)

    sweep.enumerate_all("iam roles", list)

    assert lab.results == []
    assert sweep.enumerated == ["iam roles"]


def test_a_refused_listing_makes_the_verdict_fail_even_with_nothing_found(
    lab: Harness,
) -> None:
    """This is the one thing the step must never round down to a pass."""
    sweep = Teardown(lab)
    sweep.enumeration_failures.append("iam roles: AccessDeniedException")

    assert sweep.report([], [], []) is False
    assert verdicts(lab)[EMPTY_CHECK] == FAIL
    assert "1 listings refused" in detail(lab, EMPTY_CHECK)


def test_an_agentcore_listing_follows_its_next_token(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """These operations have no paginator, and a leftover from an earlier session
    is exactly the thing sitting on page two."""
    control = clients["bedrock-agentcore-control"]
    control.runtimes = [
        {"agentRuntimeId": "verify_runtime-a", "agentRuntimeName": "verify_runtime"},
        {"agentRuntimeId": "verify_runtime-b", "agentRuntimeName": "verify_runtime"},
    ]
    control.pages_left = 1

    targets = Teardown(lab).collect_runtimes()

    assert len(targets) == 2


# --- collection -----------------------------------------------------------


def test_endpoints_are_deleted_before_the_runtime_that_holds_them(
    lab: Harness, clients: dict[str, Any]
) -> None:
    control = clients["bedrock-agentcore-control"]
    control.runtimes = [
        {"agentRuntimeId": "verify_runtime-x", "agentRuntimeName": "verify_runtime"}
    ]
    control.endpoints = [{"name": "verifyendpoint"}]

    targets = Teardown(lab).collect_runtimes()

    assert labels(targets) == [
        "runtime endpoint verify_runtime-x/verifyendpoint",
        "agent runtime verify_runtime-x verify_runtime",
    ]


def test_the_default_endpoint_belongs_to_the_service_and_is_left_alone(
    lab: Harness, clients: dict[str, Any]
) -> None:
    control = clients["bedrock-agentcore-control"]
    control.runtimes = [
        {"agentRuntimeId": "verify_runtime-x", "agentRuntimeName": "verify_runtime"}
    ]
    control.endpoints = [{"name": "DEFAULT"}]

    targets = Teardown(lab).collect_runtimes()

    assert labels(targets) == ["agent runtime verify_runtime-x verify_runtime"]


def test_a_runtime_that_is_not_ours_is_left_alone(
    lab: Harness, clients: dict[str, Any]
) -> None:
    clients["bedrock-agentcore-control"].runtimes = [
        {"agentRuntimeId": "someone-else", "agentRuntimeName": "someone-else"}
    ]

    assert Teardown(lab).collect_runtimes() == []


def test_the_log_group_is_matched_on_the_tail_under_the_fixed_path(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """The fixed /aws/... path would otherwise never satisfy the prefix filter."""
    name = f"{AGENTCORE_RUNTIME_LOG_PREFIX}verify_runtime-x-verifyendpoint/runtime-logs"
    clients["logs"].groups = [{"logGroupName": name}]

    targets = Teardown(lab).collect_runtime_log_groups()

    assert labels(targets) == [f"log group {name}"]


def test_another_deployments_log_group_under_the_same_path_is_left_alone(
    lab: Harness, clients: dict[str, Any]
) -> None:
    clients["logs"].groups = [
        {"logGroupName": f"{AGENTCORE_RUNTIME_LOG_PREFIX}other-runtime-x-endpoint"}
    ]

    assert Teardown(lab).collect_runtime_log_groups() == []


def test_the_log_group_tag_arn_drops_the_trailing_wildcard(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """ListTagsForResource rejects the ":*" DescribeLogGroups puts on its arn."""
    sweep = Teardown(lab)

    sweep.log_group_tags("/aws/x")()

    kwargs = next(
        k for name, k in clients["logs"].calls if name == "list_tags_for_resource"
    )
    assert kwargs["resourceArn"] == (
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/x"
    )


def test_gateway_targets_are_deleted_before_the_gateway(
    lab: Harness, clients: dict[str, Any]
) -> None:
    control = clients["bedrock-agentcore-control"]
    control.gateways = [{"gatewayId": "verify-gateway-x", "name": "verify-gateway"}]
    control.gateway_targets = [{"targetId": "t1"}]

    targets = Teardown(lab).collect_gateways()

    assert labels(targets) == [
        "gateway target verify-gateway-x/t1",
        "gateway verify-gateway-x verify-gateway",
    ]


def test_the_gateway_delete_waits_for_an_empty_gateway_and_a_settled_status(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """The gateway leaked on 2026-08-04 was deleted 34 seconds after its create,
    the call failed ValidationException, and it was still there two days later."""
    control = clients["bedrock-agentcore-control"]
    control.gateway_targets = []

    Teardown(lab).delete_gateway_when_drained("verify-gateway-x")

    made = [name for name, _ in control.calls]
    assert made.index("list_gateway_targets") < made.index("get_gateway")
    assert made.index("get_gateway") < made.index("delete_gateway")


def test_the_codebuild_probe_turns_an_absent_name_into_an_absence(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """ListProjects answers a deleted project by not naming it, not by raising,
    so wait_until_gone would wait out its whole timeout and call it present."""
    sweep = Teardown(lab)
    clients["codebuild"].projects = []

    with pytest.raises(ClientError) as raised:
        sweep.codebuild_probe("bedrock-agentcore-verify-builder")

    assert raised.value.response["Error"]["Code"] == "ResourceNotFoundException"


def test_the_codebuild_probe_confirms_by_listing_not_by_batch_get(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """`lab.template` grants ListProjects and not BatchGetProjects.

    Under the student session role the batch call answers AccessDenied, which
    `wait_until_gone` reads as "cannot tell" and reports as a survivor, so every
    project this step deleted would come back as still present. This step used to
    call it. `vocareum_tools.sweep` had already worked that out and confirmed by
    listing; both do now.
    """
    clients["codebuild"].projects = ["bedrock-agentcore-verify-builder"]

    found = Teardown(lab).codebuild_probe("bedrock-agentcore-verify-builder")

    assert found == {"name": "bedrock-agentcore-verify-builder"}
    assert [name for name, _ in clients["codebuild"].calls] == ["list_projects"]


def test_the_log_group_probe_matches_the_name_exactly(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """logGroupNamePrefix is a prefix, so a longer surviving name is not this one."""
    clients["logs"].groups = [{"logGroupName": "/aws/x/longer"}]

    with pytest.raises(ClientError):
        Teardown(lab).log_group_probe("/aws/x")


def test_the_bucket_head_renames_a_bare_404(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """boto3 reports HeadBucket's empty 404 as the code "404", which no waiter knows."""
    clients["s3"].bucket_exists = False

    with pytest.raises(ClientError) as raised:
        Teardown(lab).s3_bucket_probe()

    assert raised.value.response["Error"]["Code"] == "ResourceNotFoundException"


def test_an_emptied_bucket_reads_as_gone_to_the_object_probe(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """ListObjectsV2 answers 200 with no Contents rather than with an error."""
    clients["s3"].objects = []

    with pytest.raises(ClientError):
        Teardown(lab).source_objects_probe()


def test_every_object_goes_not_only_this_runs_prefix(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """A prefix-scoped sweep leaves an earlier session's objects, and those hold
    the bucket non-empty so DeleteBucket refuses forever."""
    clients["s3"].objects = [{"Key": "verify/source.zip"}, {"Key": "older/source.zip"}]

    targets = Teardown(lab).collect_source_bucket()

    assert labels(targets) == [
        f"2 objects in s3 bucket {SOURCE_BUCKET}",
        f"s3 bucket {SOURCE_BUCKET}",
    ]


def test_a_stack_owned_bucket_is_not_emptied_either(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """Widening the object sweep without this would make a stack's contents
    deletable, which fails the stack's own teardown at session end."""
    clients["s3"].objects = [{"Key": "verify/source.zip"}]
    sweep = Teardown(lab)
    sweep.stack_owned = {SOURCE_BUCKET}

    assert sweep.collect_source_bucket() == []


def test_every_s3_call_that_accepts_it_carries_the_expected_bucket_owner(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """It is what makes a name computed for the wrong account fail loudly rather
    than empty a bucket belonging to someone else."""
    s3 = clients["s3"]
    s3.objects = [{"Key": "verify/source.zip"}]
    sweep = Teardown(lab)

    for target in sweep.collect_source_bucket():
        target.delete()

    accepting = {"head_bucket", "list_objects_v2", "delete_objects", "delete_bucket"}
    made = [(name, kwargs) for name, kwargs in s3.calls if name in accepting]
    assert made
    assert all(kwargs.get("ExpectedBucketOwner") == ACCOUNT for _, kwargs in made)


def test_the_object_delete_batches_at_a_thousand_keys(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """DeleteObjects takes at most 1000 keys per call."""
    sweep = Teardown(lab)

    sweep.empty_source_bucket([f"key-{n}" for n in range(2500)])

    batches = [k for name, k in clients["s3"].calls if name == "delete_objects"]
    assert [len(one["Delete"]["Objects"]) for one in batches] == [1000, 1000, 500]


def test_a_partial_object_delete_is_raised_rather_than_passing_silently(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """DeleteObjects answers 200 while refusing individual keys in an Errors list."""
    clients["s3"].delete_errors = [{"Key": "a", "Code": "AccessDenied"}]

    with pytest.raises(ClientError) as raised:
        Teardown(lab).empty_source_bucket(["a", "b"])

    assert raised.value.response["Error"]["Code"] == "AccessDenied"
    assert "1 of 2 keys refused" in raised.value.response["Error"]["Message"]


def test_an_empty_key_list_makes_no_call_at_all(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """DeleteObjects with no keys is itself an error."""
    Teardown(lab).empty_source_bucket([])

    assert not [name for name, _ in clients["s3"].calls if name == "delete_objects"]


def test_a_versioned_bucket_names_versioning_rather_than_retrying_forever(
    lab: Harness, clients: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """lab.template grants no s3:ListBucketVersions, so this cannot be worked
    around, only reported."""

    def refuse(**kwargs: Any) -> Any:
        raise denial("DeleteBucket", "BucketNotEmpty")

    monkeypatch.setattr(clients["s3"], "delete_bucket", refuse)

    with pytest.raises(ClientError):
        Teardown(lab).delete_source_bucket(SOURCE_BUCKET)

    assert "object versioning" in "\n".join(lab.printed)  # type: ignore[attr-defined]


def test_a_role_is_emptied_before_it_is_deleted(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """IAM refuses to delete a role that still holds policies."""
    iam = clients["iam"]
    iam.inline = ["inline-one"]
    iam.attached = [{"PolicyArn": "arn:aws:iam::aws:policy/Managed"}]

    Teardown(lab).remove_role("workshop-verify-runtime-role")

    made = [name for name, _ in iam.calls]
    assert made.index("delete_role_policy") < made.index("delete_role")
    assert made.index("detach_role_policy") < made.index("delete_role")


def test_iam_roles_are_selected_by_name_only(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """ListRoles returns no tags, so a tag union would cost one call per role in
    the account, Vocareum's own included."""
    clients["iam"].listings = {
        "list_roles": {
            "Roles": [
                {"RoleName": "workshop-verify-runtime-role", "Arn": "arn:1"},
                {"RoleName": "voclabs", "Arn": "arn:2"},
            ]
        }
    }

    targets = Teardown(lab).collect_roles()

    assert labels(targets) == ["iam role workshop-verify-runtime-role"]
    assert not [name for name, _ in clients["iam"].calls if "tag" in name]


# --- the order ------------------------------------------------------------


def test_the_collection_order_is_dependency_order(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """Every role is passed to something above it, and IAM will not delete a role
    a live service is still using."""
    control = clients["bedrock-agentcore-control"]
    control.runtimes = [
        {"agentRuntimeId": "verify_runtime-x", "agentRuntimeName": "verify_runtime"}
    ]
    control.gateways = [{"gatewayId": "verify-gateway-x", "name": "verify-gateway"}]
    control.memories = [{"id": "verify_memory-x"}]
    log_group = f"{AGENTCORE_RUNTIME_LOG_PREFIX}verify_runtime-x-verifyendpoint"
    clients["logs"].groups = [{"logGroupName": log_group}]
    clients["lambda"].listings = {
        "list_functions": {
            "Functions": [
                {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"}
            ]
        }
    }
    clients["codebuild"].projects = ["bedrock-agentcore-verify-builder"]
    clients["ecr"].listings = {
        "describe_repositories": {
            "repositories": [
                {"repositoryName": "bedrock-agentcore-verify", "repositoryArn": "arn:e"}
            ]
        }
    }
    clients["dynamodb"].listings = {
        "list_tables": {"TableNames": ["workshop-verify-bookings"]}
    }
    clients["secretsmanager"].listings = {
        "list_secrets": {
            "SecretList": [{"Name": "verify/neo4j-command", "ARN": "arn:s"}]
        }
    }
    clients["s3"].objects = [{"Key": "verify/source.zip"}]
    clients["iam"].listings = {
        "list_roles": {"Roles": [{"RoleName": "workshop-verify-runtime-role"}]}
    }

    targets = Teardown(lab).collect()

    assert labels(targets) == [
        "agent runtime verify_runtime-x verify_runtime",
        f"log group {AGENTCORE_RUNTIME_LOG_PREFIX}verify_runtime-x-verifyendpoint",
        "gateway verify-gateway-x verify-gateway",
        "memory verify_memory-x",
        "lambda hotel-booking-verify-tool",
        "codebuild project bedrock-agentcore-verify-builder",
        "ecr repository bedrock-agentcore-verify",
        "dynamodb table workshop-verify-bookings",
        "secret verify/neo4j-command",
        f"1 objects in s3 bucket {SOURCE_BUCKET}",
        f"s3 bucket {SOURCE_BUCKET}",
        "iam role workshop-verify-runtime-role",
    ]


def test_the_log_group_delete_waits_for_every_runtime_first(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """A runtime keeps writing for 100 seconds or more after its delete returns,
    and the next write undoes the log group delete."""
    control = clients["bedrock-agentcore-control"]
    sweep = Teardown(lab)
    sweep.runtime_ids = ["verify_runtime-x"]

    sweep.delete_log_group_when_runtimes_gone("/aws/x")

    made = [name for name, _ in control.calls]
    assert made == ["get_agent_runtime"]
    assert clients["logs"].deleted == ["log group /aws/x"]


# --- the hints ------------------------------------------------------------


def test_the_hints_run_first_and_record_nothing(lab: Harness) -> None:
    """A hint that fails is not a finding: the enumeration covers the same
    resources and it is the one that decides."""
    lab.defer("gateway verify-gateway", lambda: (_ for _ in ()).throw(denial("Delete")))

    Teardown(lab).run_hints()

    assert lab.results == []
    assert "leaving it to the enumeration" in "\n".join(lab.printed)  # type: ignore[attr-defined]


def test_the_hints_run_newest_first(lab: Harness) -> None:
    """They are in reverse order of creation, so that is dependency-safe."""
    order: list[str] = []
    lab.defer("first", lambda: order.append("first"))
    lab.defer("second", lambda: order.append("second"))

    Teardown(lab).run_hints()

    assert order == ["second", "first"]


def test_no_hints_at_all_says_so_rather_than_claiming_a_clean_account(
    lab: Harness,
) -> None:
    """An empty list used to make this step report success for doing nothing."""
    Teardown(lab).run_hints()

    assert "no hints registered" in "\n".join(lab.printed)  # type: ignore[attr-defined]


# --- the verdict ----------------------------------------------------------


def test_a_resource_still_present_fails_the_verdict(lab: Harness) -> None:
    sweep = Teardown(lab)

    assert sweep.report([], [], ["gateway verify-gateway-x"]) is False
    assert verdicts(lab)[EMPTY_CHECK] == FAIL
    assert "1 still present" in detail(lab, EMPTY_CHECK)


def test_an_empty_account_passes_and_names_what_was_read(lab: Harness) -> None:
    sweep = Teardown(lab)
    sweep.enumerated = ["iam roles", "gateways"]

    assert sweep.report([], [], []) is True
    assert verdicts(lab)[EMPTY_CHECK] == PASS
    assert "2 listings read" in detail(lab, EMPTY_CHECK)


def test_a_refused_tag_read_is_counted_without_moving_the_verdict(
    lab: Harness,
) -> None:
    """A denial here is the ordinary state of the account, not a finding.

    `logs:ListTagsForResource` was granted on 2026-08-07 and is still Untested,
    so on any account that refuses it every non-matching name produces one of
    these. Failing the empty check on that would fail it on every student run.
    What must not happen is that the blindness leaves no trace at all.
    """

    def refuse() -> Any:
        raise denial("ListTagsForResource")

    sweep = Teardown(lab)
    sweep.enumerated = ["log groups"]

    assert sweep.ours("unrelated-name", "arn:x", refuse) is False
    assert sweep.report([], [], []) is True
    assert verdicts(lab)[EMPTY_CHECK] == PASS
    assert "1 tag reads refused" in detail(lab, EMPTY_CHECK)
    assert "tag reads refused for 1 of 1" in "\n".join(lab.printed)  # type: ignore[attr-defined]


def test_a_tag_read_that_answered_untagged_is_not_a_refusal(lab: Harness) -> None:
    """The distinction the count exists for. Both answer False to selection."""
    sweep = Teardown(lab)
    sweep.enumerated = ["log groups"]

    assert sweep.ours("unrelated-name", "arn:x", dict) is False
    assert sweep.report([], [], []) is True
    assert sweep.tag_reads.denials == 0
    assert "tag reads refused" not in detail(lab, EMPTY_CHECK)


def test_a_name_that_matches_is_never_charged_a_tag_read(lab: Harness) -> None:
    """`ours` reads tags only once the name has failed, so the count has to
    reflect the reads that happened rather than the resources considered."""
    sweep = Teardown(lab)

    assert sweep.ours("workshop-verify-runtime-role", "arn:r", dict) is True
    assert sweep.tag_reads.reads == 0


def test_a_delete_that_returned_is_not_a_delete_that_finished(
    lab: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping the confirm claimed a clean account five separate times while
    resources were still in it."""
    monkeypatch.setattr(Harness, "wait_until_gone", lambda *args, **kwargs: False)
    target = Target("gateway verify-gateway-x", lambda: None, lambda: None)

    assert Teardown(lab).confirm([target]) == ["gateway verify-gateway-x"]


def test_an_absent_resource_is_not_a_failed_delete(lab: Harness) -> None:
    """Something else already removed it, which is the outcome this wanted."""

    def already_gone() -> Any:
        raise absent("DeleteGateway")

    target = Target("gateway verify-gateway-x", already_gone, lambda: None)

    assert Teardown(lab).delete_all([target]) == []
    assert "absent   gateway verify-gateway-x" in "\n".join(lab.printed)  # type: ignore[attr-defined]


def test_a_refused_delete_is_collected_and_named(lab: Harness) -> None:
    def refuse() -> Any:
        raise denial("DeleteGateway")

    target = Target("gateway verify-gateway-x", refuse, lambda: None)

    assert Teardown(lab).delete_all([target]) == [
        "gateway verify-gateway-x: AccessDeniedException"
    ]


def test_a_delete_that_conflicts_is_retried_rather_than_reported(
    lab: Harness,
) -> None:
    """Deleting an endpoint returns before it is gone and the runtime refuses to
    go while one still exists. That is a timing artifact, not a refusal."""
    attempts: list[int] = []

    def conflicted() -> Any:
        attempts.append(1)
        if len(attempts) < 3:
            raise denial("DeleteAgentRuntime", "ConflictException")
        return {}

    target = Target("agent runtime x", conflicted, lambda: None)

    assert Teardown(lab).delete_all([target]) == []
    assert len(attempts) == 3


def test_the_whole_step_on_an_empty_account_reports_it_empty(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """Nothing was created, nothing is found, and the listings all answered.

    The shared build-source bucket has to be absent too. It is the one resource
    the notebook does not create, so a fake that always answers HeadBucket leaves
    the account non-empty for a reason no student action produced.
    """
    clients["s3"].bucket_exists = False
    sweep = Teardown(lab)

    assert sweep.run() is True
    assert verdicts(lab)[EMPTY_CHECK] == PASS
    assert "nothing to confirm" in "\n".join(lab.printed)  # type: ignore[attr-defined]


def test_the_whole_step_deletes_and_confirms_what_it_found(
    lab: Harness, clients: dict[str, Any]
) -> None:
    control = clients["bedrock-agentcore-control"]
    control.runtimes = [
        {"agentRuntimeId": "verify_runtime-x", "agentRuntimeName": "verify_runtime"}
    ]
    control.memories = [{"id": "verify_memory-x"}]

    assert Teardown(lab).run() is True
    assert control.deleted == ["runtime verify_runtime-x", "memory verify_memory-x"]
    assert verdicts(lab)[EMPTY_CHECK] == PASS


# --- names ----------------------------------------------------------------


def test_every_check_this_step_records_is_one_of_two_shapes(
    lab: Harness, clients: dict[str, Any]
) -> None:
    """Permission rows are keyed on these strings."""
    clients["iam"].refuse.add("list_roles")

    Teardown(lab).run()

    for name, _, _ in lab.results:
        assert name == EMPTY_CHECK or name.startswith("teardown enumerated ")
