"""Tests for the account sweep, driven entirely by fakes.

The sweep exists because a resource was left in a live account for two days,
which means the only failures worth testing are the ones that leave something
behind or take something that was not ours: an exclusion that does not exclude,
a gateway deleted before its targets, a resource selected by name that carries
no tag, and a failed delete reported as a success. Every one of those is
reachable with a fake client, so none of these tests needs a lab session.

Three kinds were added after an account was emptied by hand and compared against
what this module would have done: the per-endpoint AgentCore log groups, the
CodeBuild projects, and the AgentCore build-source bucket. Each of those has a
test here that fails if the handler goes away, because "the sweep has no handler
for it" is not a failure any other test in this file can see.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from botocore.exceptions import ClientError

from vocareum_tools import phase, sweep
from vocareum_tools.course import CleanupRules
from workshop_lab.naming import Names
from workshop_lab.selection import TagReads, matches_prefix, name_prefixes


def client_error(code: str, message: str = "no", operation: str = "Op") -> ClientError:
    """Return a ClientError the sweep will read."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def gone(code: str = "ResourceNotFoundException") -> ClientError:
    """Return the answer a describe gives once the resource has left."""
    return client_error(code, "not found", "Describe")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCloudFormation:
    """A CloudFormation client that owns whatever the test says it owns."""

    def __init__(
        self,
        stacks: dict[str, list[str]] | None = None,
        fail: ClientError | None = None,
    ) -> None:
        self.stacks = stacks or {}
        self.fail = fail

    def describe_stacks(self, **_: Any) -> dict[str, Any]:
        """Return every live stack."""
        if self.fail is not None:
            raise self.fail
        return {"Stacks": [{"StackName": name} for name in self.stacks]}

    def describe_stack_resources(self, StackName: str, **_: Any) -> dict[str, Any]:
        """Return the physical resource ids of one stack."""
        return {
            "StackResources": [
                {"PhysicalResourceId": physical}
                for physical in self.stacks.get(StackName, [])
            ]
        }


class FakeAgentCore:
    """The AgentCore control plane, with a recorded call order."""

    def __init__(
        self,
        gateways: list[dict[str, Any]] | None = None,
        targets: list[str] | None = None,
        runtimes: list[dict[str, Any]] | None = None,
        endpoints: list[str] | None = None,
        memories: list[dict[str, Any]] | None = None,
        tags: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.gateways = gateways or []
        self.targets = list(targets or [])
        self.runtimes = runtimes or []
        self.endpoints = list(endpoints or [])
        self.memories = memories or []
        self.tags = tags or {}
        self.calls: list[str] = []
        self.deleted: set[str] = set()

    # -- reads ------------------------------------------------------------
    def list_tags_for_resource(self, resourceArn: str, **_: Any) -> dict[str, Any]:
        """Return the tags a test attached to an ARN."""
        return {"tags": self.tags.get(resourceArn, {})}

    def list_gateways(self, **_: Any) -> dict[str, Any]:
        """Return every gateway."""
        return {"items": self.gateways}

    def list_gateway_targets(self, gatewayIdentifier: str, **_: Any) -> dict[str, Any]:
        """Return the gateway's remaining targets."""
        self.calls.append(f"list_targets {gatewayIdentifier}")
        return {"items": [{"targetId": target} for target in self.targets]}

    def get_gateway(self, gatewayIdentifier: str, **_: Any) -> dict[str, Any]:
        """Describe a gateway, or report it gone once deleted."""
        if f"gateway {gatewayIdentifier}" in self.deleted:
            raise gone()
        return {"status": "READY"}

    def get_gateway_target(self, targetId: str, **_: Any) -> dict[str, Any]:
        """Describe a target, or report it gone once deleted."""
        if targetId not in self.targets:
            raise gone()
        return {"status": "READY"}

    def list_agent_runtimes(self, **_: Any) -> dict[str, Any]:
        """Return every runtime."""
        return {"agentRuntimes": self.runtimes}

    def list_agent_runtime_endpoints(self, **_: Any) -> dict[str, Any]:
        """Return the runtime's remaining endpoints."""
        return {"runtimeEndpoints": [{"name": name} for name in self.endpoints]}

    def get_agent_runtime(self, agentRuntimeId: str, **_: Any) -> dict[str, Any]:
        """Describe a runtime, or report it gone once deleted."""
        if f"runtime {agentRuntimeId}" in self.deleted:
            raise gone()
        return {"status": "READY"}

    def list_memories(self, **_: Any) -> dict[str, Any]:
        """Return every memory."""
        return {"memories": self.memories}

    def get_memory(self, memoryId: str, **_: Any) -> dict[str, Any]:
        """Describe a memory, or report it gone once deleted."""
        if f"memory {memoryId}" in self.deleted:
            raise gone()
        return {"memory": {"status": "ACTIVE"}}

    # -- writes -----------------------------------------------------------
    def delete_gateway_target(self, gatewayIdentifier: str, targetId: str) -> None:
        """Remove one target."""
        self.calls.append(f"delete_target {targetId}")
        self.targets.remove(targetId)

    def delete_gateway(self, gatewayIdentifier: str) -> None:
        """Remove a gateway, refusing while it still has a target."""
        self.calls.append(f"delete_gateway {gatewayIdentifier}")
        if self.targets:
            raise client_error(
                "ValidationException", "gateway still has targets", "DeleteGateway"
            )
        self.deleted.add(f"gateway {gatewayIdentifier}")

    def delete_agent_runtime_endpoint(
        self,
        agentRuntimeId: str,
        endpointName: str,
    ) -> None:
        """Remove one runtime endpoint."""
        self.calls.append(f"delete_endpoint {endpointName}")
        self.endpoints.remove(endpointName)

    def delete_agent_runtime(self, agentRuntimeId: str) -> None:
        """Remove a runtime, refusing while a deletable endpoint still exists.

        DEFAULT does not count. The service creates it with the runtime and
        removes it with the runtime, so a fake that refused on DEFAULT would
        make a correct sweep look like a deadlock.
        """
        self.calls.append(f"delete_runtime {agentRuntimeId}")
        if [name for name in self.endpoints if name != "DEFAULT"]:
            raise client_error("ConflictException", "endpoint present", "Delete")
        self.deleted.add(f"runtime {agentRuntimeId}")

    def delete_memory(self, memoryId: str) -> None:
        """Remove a memory."""
        self.calls.append(f"delete_memory {memoryId}")
        self.deleted.add(f"memory {memoryId}")


class FakeLambda:
    """A Lambda client holding a fixed set of functions."""

    def __init__(
        self,
        functions: list[dict[str, Any]] | None = None,
        tags: dict[str, dict[str, str]] | None = None,
        delete_error: ClientError | None = None,
    ) -> None:
        self.functions = functions or []
        self.tags = tags or {}
        self.delete_error = delete_error
        self.deleted: list[str] = []

    def list_functions(self, **_: Any) -> dict[str, Any]:
        """Return every function."""
        return {"Functions": self.functions}

    def list_tags(self, Resource: str, **_: Any) -> dict[str, Any]:
        """Return one function's tags."""
        return {"Tags": self.tags.get(Resource, {})}

    def delete_function(self, FunctionName: str, **_: Any) -> None:
        """Remove a function, or fail the way the test asked."""
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(FunctionName)

    def get_function(self, FunctionName: str, **_: Any) -> dict[str, Any]:
        """Describe a function, or report it gone once deleted."""
        if FunctionName in self.deleted:
            raise gone()
        return {"Configuration": {"FunctionName": FunctionName}}


class FakeEcr:
    """An ECR client that records whether the delete forced."""

    def __init__(self, repositories: list[dict[str, Any]] | None = None) -> None:
        self.repositories = repositories or []
        self.deleted: list[tuple[str, bool]] = []

    def describe_repositories(self, **kwargs: Any) -> dict[str, Any]:
        """List every repository, or answer a named lookup."""
        names = kwargs.get("repositoryNames")
        if names:
            if any(name in [item[0] for item in self.deleted] for name in names):
                raise gone("RepositoryNotFoundException")
            return {"repositories": [{"repositoryName": names[0]}]}
        return {"repositories": self.repositories}

    def list_tags_for_resource(self, resourceArn: str, **_: Any) -> dict[str, Any]:
        """Return no tags, so only the name prefix can select."""
        return {"tags": []}

    def delete_repository(self, repositoryName: str, force: bool = False) -> None:
        """Remove a repository, refusing an unforced delete of a full one."""
        if not force:
            raise client_error(
                "RepositoryNotEmptyException", "images present", "Delete"
            )
        self.deleted.append((repositoryName, force))


class FakeDynamoDb:
    """A DynamoDB client whose table takes a while to become ACTIVE."""

    def __init__(self, states: list[str] | None = None) -> None:
        self.states = states or ["ACTIVE"]
        self.deleted: list[str] = []

    def list_tables(self, **_: Any) -> dict[str, Any]:
        """Return the one table."""
        return {"TableNames": ["workshop-verify-bookings"]}

    def describe_table(self, TableName: str, **_: Any) -> dict[str, Any]:
        """Describe the table, walking its state list once per call."""
        if TableName in self.deleted:
            raise gone()
        state = self.states[0] if len(self.states) == 1 else self.states.pop(0)
        return {
            "Table": {
                "TableArn": f"arn:aws:dynamodb:::table/{TableName}",
                "TableStatus": state,
            }
        }

    def list_tags_of_resource(self, ResourceArn: str, **_: Any) -> dict[str, Any]:
        """Return the workshop tag."""
        return {
            "Tags": [{"Key": sweep.WORKSHOP_TAG_KEY, "Value": sweep.WORKSHOP_TAG_VALUE}]
        }

    def delete_table(self, TableName: str, **_: Any) -> None:
        """Remove the table, refusing while it is not ACTIVE."""
        if self.states[0] != "ACTIVE":
            raise client_error("ResourceInUseException", "busy", "DeleteTable")
        self.deleted.append(TableName)


class FakeSecrets:
    """A Secrets Manager client that records the delete arguments."""

    def __init__(self, secrets: list[dict[str, Any]] | None = None) -> None:
        self.secrets = secrets or []
        self.deleted: list[tuple[str, bool]] = []

    def list_secrets(self, **_: Any) -> dict[str, Any]:
        """Return every secret, tags included the way the real API does."""
        return {"SecretList": self.secrets}

    def delete_secret(
        self,
        SecretId: str,
        ForceDeleteWithoutRecovery: bool = False,
    ) -> None:
        """Remove a secret."""
        self.deleted.append((SecretId, ForceDeleteWithoutRecovery))

    def describe_secret(self, SecretId: str, **_: Any) -> dict[str, Any]:
        """Describe a secret, or report it gone once deleted."""
        if SecretId in [item[0] for item in self.deleted]:
            raise gone()
        return {"Name": SecretId}


class FakeLogs:
    """A CloudWatch Logs client holding a fixed set of log groups.

    ``describe_log_groups`` reports the ARN with the trailing ``:*`` the real API
    uses, because stripping it is what makes the tag read work at all and a fake
    that handed back the clean form would hide a broken strip.
    """

    def __init__(
        self,
        groups: list[str] | None = None,
        tags: dict[str, dict[str, str]] | None = None,
        delete_error: ClientError | None = None,
    ) -> None:
        self.groups = list(groups or [])
        self.tags = tags or {}
        self.delete_error = delete_error
        self.deleted: list[str] = []

    def describe_log_groups(
        self,
        logGroupNamePrefix: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        """Return the log groups under a prefix, or an empty list for none."""
        return {
            "logGroups": [
                {
                    "logGroupName": name,
                    "arn": f"arn:aws:logs:us-east-1:1:log-group:{name}:*",
                }
                for name in self.groups
                if name.startswith(logGroupNamePrefix) and name not in self.deleted
            ]
        }

    def list_tags_for_resource(self, resourceArn: str, **_: Any) -> dict[str, Any]:
        """Return the tags a test attached to a log group ARN."""
        return {"tags": self.tags.get(resourceArn, {})}

    def delete_log_group(self, logGroupName: str, **_: Any) -> None:
        """Remove a log group, or fail the way the test asked."""
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(logGroupName)


class FakeCodeBuild:
    """A CodeBuild client whose BatchGetProjects can be refused."""

    def __init__(
        self,
        projects: list[str] | None = None,
        tags: dict[str, dict[str, str]] | None = None,
        batch_error: ClientError | None = None,
    ) -> None:
        self.projects = list(projects or [])
        self.tags = tags or {}
        self.batch_error = batch_error
        self.deleted: list[str] = []

    def list_projects(self, **_: Any) -> dict[str, Any]:
        """Return the names of the projects that are still there."""
        return {
            "projects": [name for name in self.projects if name not in self.deleted]
        }

    def batch_get_projects(self, names: list[str], **_: Any) -> dict[str, Any]:
        """Return details for the named projects, in CodeBuild's lowercase tag shape."""
        if self.batch_error is not None:
            raise self.batch_error
        live = [
            name for name in names if name in self.projects and name not in self.deleted
        ]
        return {
            "projects": [
                {
                    "name": name,
                    "arn": f"arn:aws:codebuild:us-east-1:1:project/{name}",
                    "tags": [
                        {"key": key, "value": value}
                        for key, value in self.tags.get(name, {}).items()
                    ],
                }
                for name in live
            ],
            "projectsNotFound": [name for name in names if name not in live],
        }

    def delete_project(self, name: str, **_: Any) -> None:
        """Remove a project."""
        self.deleted.append(name)


class FakeS3:
    """An S3 client holding buckets and their keys.

    Every method records the ``ExpectedBucketOwner`` it was given, including the
    None of a call that gave none. The bucket name is computed from an account
    id and S3 bucket names are global, so a call without that header on the
    delete path could empty a bucket in someone else's account if the name and
    the guarded account ever disagreed.
    """

    def __init__(
        self,
        buckets: list[str] | None = None,
        objects: dict[str, list[str]] | None = None,
        tags: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.buckets = set(buckets or ())
        self.objects = {name: list(keys) for name, keys in (objects or {}).items()}
        self.tags = tags or {}
        self.calls: list[str] = []
        self.owners: list[str | None] = []

    def _owner(self, kwargs: dict[str, Any]) -> None:
        self.owners.append(kwargs.get("ExpectedBucketOwner"))

    def head_bucket(self, Bucket: str, **kwargs: Any) -> dict[str, Any]:
        """Report whether a bucket is there, the way a HEAD does: a bare 404."""
        self._owner(kwargs)
        if Bucket not in self.buckets:
            raise client_error("404", "Not Found", "HeadBucket")
        return {}

    def get_bucket_tagging(self, Bucket: str, **kwargs: Any) -> dict[str, Any]:
        """Return a bucket's tags, or NoSuchTagSet the way S3 does."""
        self._owner(kwargs)
        if Bucket not in self.tags:
            raise client_error("NoSuchTagSet", "no tag set", "GetBucketTagging")
        return {
            "TagSet": [
                {"Key": key, "Value": value} for key, value in self.tags[Bucket].items()
            ]
        }

    def list_objects_v2(self, Bucket: str, **kwargs: Any) -> dict[str, Any]:
        """Return the keys in a bucket."""
        self._owner(kwargs)
        return {"Contents": [{"Key": key} for key in self.objects.get(Bucket, [])]}

    def delete_objects(
        self,
        Bucket: str,
        Delete: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Remove the named keys."""
        self._owner(kwargs)
        for entry in Delete["Objects"]:
            self.objects[Bucket].remove(entry["Key"])
            self.calls.append(f"delete_object {Bucket}/{entry['Key']}")
        return {"Deleted": Delete["Objects"]}

    def delete_bucket(self, Bucket: str, **kwargs: Any) -> None:
        """Remove a bucket, refusing while anything is still in it."""
        self._owner(kwargs)
        self.calls.append(f"delete_bucket {Bucket}")
        if self.objects.get(Bucket):
            raise client_error("BucketNotEmpty", "objects present", "DeleteBucket")
        self.buckets.discard(Bucket)


class FakeIam:
    """An IAM client holding roles, their policies, and their tags."""

    def __init__(
        self,
        roles: list[dict[str, Any]] | None = None,
        inline: dict[str, list[str]] | None = None,
        attached: dict[str, list[str]] | None = None,
        tags: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.roles = roles or []
        self.inline = inline or {}
        self.attached = attached or {}
        self.tags = tags or {}
        self.calls: list[str] = []
        self.deleted: list[str] = []

    def list_roles(self, **_: Any) -> dict[str, Any]:
        """Return every role in the account."""
        return {"Roles": self.roles}

    def list_role_tags(self, RoleName: str, **_: Any) -> dict[str, Any]:
        """Return a role's tags."""
        return {
            "Tags": [
                {"Key": key, "Value": value}
                for key, value in self.tags.get(RoleName, {}).items()
            ]
        }

    def list_role_policies(self, RoleName: str, **_: Any) -> dict[str, Any]:
        """Return a role's inline policy names."""
        return {"PolicyNames": self.inline.get(RoleName, [])}

    def list_attached_role_policies(self, RoleName: str, **_: Any) -> dict[str, Any]:
        """Return a role's attached managed policies."""
        return {
            "AttachedPolicies": [
                {"PolicyName": arn.rsplit("/", 1)[-1], "PolicyArn": arn}
                for arn in self.attached.get(RoleName, [])
            ]
        }

    def delete_role_policy(self, RoleName: str, PolicyName: str) -> None:
        """Remove one inline policy."""
        self.calls.append(f"delete_role_policy {RoleName}/{PolicyName}")
        self.inline[RoleName].remove(PolicyName)

    def detach_role_policy(self, RoleName: str, PolicyArn: str) -> None:
        """Detach one managed policy."""
        self.calls.append(f"detach_role_policy {RoleName}/{PolicyArn}")
        self.attached[RoleName].remove(PolicyArn)

    def delete_role(self, RoleName: str) -> None:
        """Remove a role, refusing while anything is still attached."""
        self.calls.append(f"delete_role {RoleName}")
        if self.inline.get(RoleName) or self.attached.get(RoleName):
            raise client_error("DeleteConflict", "policies attached", "DeleteRole")
        self.deleted.append(RoleName)

    def get_role(self, RoleName: str, **_: Any) -> dict[str, Any]:
        """Describe a role, or report it gone once deleted."""
        if RoleName in self.deleted:
            raise gone("NoSuchEntity")
        return {"Role": {"RoleName": RoleName}}


ACCOUNT = "111122223333"


def clients(account: str = ACCOUNT, **overrides: Any) -> sweep.Clients:
    """Build a Clients whose every service is a fake or an empty stand-in.

    ``account`` is a real parameter rather than a constant because one collector
    computes a name from it, so an empty one is a case worth being able to write.
    """
    defaults: dict[str, Any] = {
        "cloudformation": FakeCloudFormation(),
        "bedrock-agentcore-control": FakeAgentCore(),
        "logs": FakeLogs(),
        "lambda": FakeLambda(),
        "codebuild": FakeCodeBuild(),
        "ecr": FakeEcr(),
        "s3": FakeS3(),
        "dynamodb": FakeDynamoDb(states=["ACTIVE"]),
        "secretsmanager": FakeSecrets(),
        "iam": FakeIam(),
    }
    defaults["dynamodb"].list_tables = lambda **_: {"TableNames": []}  # type: ignore[method-assign]
    defaults.update(overrides)
    return sweep.Clients(account=account, overrides=defaults)


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the polls in phase.py and sweep.py from taking real time."""
    monkeypatch.setattr(sweep.time, "sleep", lambda _: None)
    monkeypatch.setattr("vocareum_tools.phase.time.sleep", lambda _: None)


# ---------------------------------------------------------------------------
# Selection: the tag and the prefix are unioned
# ---------------------------------------------------------------------------


def test_the_workshop_tag_alone_selects_a_resource() -> None:
    """A name the sweep has never heard of is in scope if it carries the tag."""
    candidate = sweep.Candidate(
        kind="lambda",
        identifier="something-a-student-renamed",
        name="something-a-student-renamed",
        tags={sweep.WORKSHOP_TAG_KEY: sweep.WORKSHOP_TAG_VALUE},
    )

    assert sweep.selection_reason(candidate, sweep.NAME_PREFIXES) == "tag"


def test_the_name_prefix_alone_selects_an_untagged_resource() -> None:
    """The case the tag half cannot cover.

    Tagging an AgentCore resource is a measured permission. On an account that
    refuses it the resource exists with no tags at all, so a tag-only sweep
    reports a clean account while the resource sits there.
    """
    candidate = sweep.Candidate(
        kind="gateway", identifier="GW1", name="wsverify-gateway", tags={}
    )

    assert sweep.selection_reason(candidate, sweep.NAME_PREFIXES) == "prefix"


def test_both_halves_are_reported_when_both_match() -> None:
    candidate = sweep.Candidate(
        kind="memory",
        identifier="verify_memory-abc",
        name="verify_memory-abc",
        tags={sweep.WORKSHOP_TAG_KEY: sweep.WORKSHOP_TAG_VALUE},
    )

    assert sweep.selection_reason(candidate, sweep.NAME_PREFIXES) == "tag+prefix"


def test_an_unrelated_untagged_resource_is_out_of_scope() -> None:
    candidate = sweep.Candidate(kind="lambda", identifier="billing", name="billing")

    assert sweep.selection_reason(candidate, sweep.NAME_PREFIXES) == ""


def test_a_tag_cannot_select_an_iam_role_outside_the_granted_prefixes() -> None:
    """A tagged voclabs role must stay out of reach whatever the tag says."""
    candidate = sweep.Candidate(
        kind="iam-role",
        identifier="voclabs",
        name="voclabs",
        tags={sweep.WORKSHOP_TAG_KEY: sweep.WORKSHOP_TAG_VALUE},
    )

    reason = sweep.selection_reason(
        candidate, sweep.IAM_ROLE_PREFIXES, require_prefix=True
    )

    assert reason == ""


def test_an_agentcore_probe_resource_is_in_scope() -> None:
    """run_phase3_agentcore.py leaks under its own prefix and nothing matched it."""
    candidate = sweep.Candidate(
        kind="gateway", identifier="GW9", name="agentcoreprobe-gateway"
    )

    assert sweep.selection_reason(candidate, sweep.NAME_PREFIXES) == "prefix"


def test_the_retired_probe_prefix_still_selects() -> None:
    """The runner's PREFIX was "phase3probe" until it was renamed.

    Runs from before the rename leaked resources under the old name into accounts
    that are still in the Vocareum pool, and this sweep is the only thing that
    will remove them, so the old prefix outlives the rename.
    """
    candidate = sweep.Candidate(
        kind="gateway", identifier="GW9", name="phase3probe-gateway"
    )

    assert sweep.selection_reason(candidate, sweep.NAME_PREFIXES) == "prefix"


def test_a_probe_iam_role_is_in_scope_under_either_prefix_generation() -> None:
    """Probe roles are swept, and they were once wrongly excluded.

    The reasoning that excluded them was that lab.template scopes iam:* to
    workshop-* and AmazonBedrockAgentCoreSDK*. That grant is additive to
    Vocareum's own Pvoclabs policy rather than a ceiling over it, and
    live sessions measured the deletes this sweep needs as Allowed,
    re-measured on phase3probe-* names for exactly this question.
    """
    for name in ("agentcoreprobe-runtime-role", "phase3probe-gateway-role"):
        candidate = sweep.Candidate(kind="iam-role", identifier=name, name=name)

        reason = sweep.selection_reason(
            candidate, sweep.IAM_ROLE_PREFIXES, require_prefix=True
        )

        assert reason == "prefix", name


def test_the_lambda_and_ecr_names_the_notebook_uses_are_matched() -> None:
    """These three put the verify prefix in the middle, which startswith misses."""
    for name in (
        "hotel-booking-verify-tool",
        "bedrock-agentcore-verify",
        "workshop-verify-bookings",
    ):
        candidate = sweep.Candidate(kind="lambda", identifier=name, name=name)
        assert sweep.selection_reason(candidate, sweep.NAME_PREFIXES) == "prefix", name


def test_this_list_stays_a_superset_of_the_one_the_notebook_sweeps_with() -> None:
    """The claim `workshop_lab.selection.name_prefixes` makes about this file.

    The two lists are deliberately different and neither is derived from the
    other: the notebook knows the one prefix its own run used, and this list is
    the union across every generation of names that could still be sitting in a
    pooled account. What must hold is the direction. A name shape the notebook
    would sweep and this would not is a resource left behind in an account that
    is about to become unreachable, and nothing else in the repository would
    say so.

    It is a startswith relation rather than set membership, because `workshop-`
    here already covers `workshop-verify` there.
    """
    missed = [
        one
        for one in name_prefixes("verify")
        if not matches_prefix(one, sweep.NAME_PREFIXES)
    ]

    assert missed == []


def test_every_role_the_notebook_names_is_in_scope_for_the_iam_prefixes() -> None:
    """IAM is the one kind selected by name alone.

    `require_prefix` switches off the tag half for roles, deliberately: the
    account holds Vocareum's own voclabs and vocareum roles, and a role
    selectable by tag would be deletable the moment anything tagged one. The
    cost is that a role name outside these prefixes has nothing left to catch
    it, so the names `workshop_lab.naming` actually builds are checked against
    them here rather than assumed.

    This is a narrower claim than the one above on purpose. The full
    `name_prefixes` list is not a subset of these, and should not be: the
    notebook only ever names a role `workshop-<prefix>-<suffix>`.
    """
    names = Names(prefix="verify", account_id=ACCOUNT, region="us-east-1")
    built = [names.role("runtime-role"), names.role("codebuild-role")]

    missed = [one for one in built if not matches_prefix(one, sweep.IAM_ROLE_PREFIXES)]

    assert missed == []


# ---------------------------------------------------------------------------
# Stack-owned exclusion
# ---------------------------------------------------------------------------


def test_a_stack_owned_resource_is_excluded_rather_than_deleted() -> None:
    """Deleting a stack resource risks DELETE_FAILED, which is worse than a leak."""
    cfn = FakeCloudFormation({"c2225-lab-stack": ["workshop-verify-lambda-role"]})
    iam = FakeIam(
        roles=[
            {"RoleName": "workshop-verify-lambda-role", "Arn": "arn:aws:iam::1:role/a"},
            {
                "RoleName": "workshop-verify-gateway-role",
                "Arn": "arn:aws:iam::1:role/b",
            },
        ]
    )
    sweeper = sweep.Sweeper(clients=clients(cloudformation=cfn, iam=iam))

    report = sweeper.plan()

    assert [item.name for item in report.excluded] == ["workshop-verify-lambda-role"]
    assert [item.name for item in report.targets] == ["workshop-verify-gateway-role"]

    sweeper.execute(report)

    assert iam.deleted == ["workshop-verify-gateway-role"]


def test_a_stack_resource_named_by_arn_still_excludes_the_resource_by_name() -> None:
    """CloudFormation reports a secret by ARN and list_secrets reports it by name."""
    arn = "arn:aws:secretsmanager:us-east-1:1:secret:verify/neo4j-command-AbCdEf"
    cfn = FakeCloudFormation({"lab": [arn]})
    secrets = FakeSecrets(
        [{"Name": "verify/neo4j-command-AbCdEf", "ARN": arn, "Tags": []}]
    )
    sweeper = sweep.Sweeper(clients=clients(cloudformation=cfn, secretsmanager=secrets))

    report = sweeper.plan()

    assert len(report.excluded) == 1
    assert report.targets == []


def test_a_stack_that_cannot_be_read_is_reported_and_fails_the_run() -> None:
    """An unknown exclusion set is not a licence to delete and call it clean."""
    cfn = FakeCloudFormation(fail=client_error("AccessDenied", "no", "DescribeStacks"))
    sweeper = sweep.Sweeper(clients=clients(cloudformation=cfn))

    report = sweeper.plan()

    assert report.stack_owned.unreadable
    assert report.exit_code() == sweep.EXIT_SURVIVED


def test_an_explicit_stack_name_replaces_the_live_stack_scan() -> None:
    cfn = FakeCloudFormation({"lab": ["workshop-verify-a"], "other": ["workshop-b"]})
    iam = FakeIam(
        roles=[
            {"RoleName": "workshop-verify-a", "Arn": "arn:1"},
            {"RoleName": "workshop-b", "Arn": "arn:2"},
        ]
    )
    sweeper = sweep.Sweeper(clients=clients(cloudformation=cfn, iam=iam))

    report = sweeper.plan(stack_name="lab")

    assert report.stack_owned.stacks == ["lab"]
    assert [item.name for item in report.excluded] == ["workshop-verify-a"]
    assert [item.name for item in report.targets] == ["workshop-b"]


# ---------------------------------------------------------------------------
# Ordering: gateway, runtime, and the deletion order across kinds
# ---------------------------------------------------------------------------


def test_a_gateway_is_drained_before_it_is_deleted() -> None:
    """DeleteGateway against a gateway with targets fails and leaks the gateway."""
    control = FakeAgentCore(
        gateways=[
            {"gatewayId": "GW1", "name": "verify-gateway", "gatewayArn": "arn:g"}
        ],
        targets=["T1", "T2"],
    )
    sweeper = sweep.Sweeper(
        clients=clients(**{"bedrock-agentcore-control": control}), kinds=("gateway",)
    )

    report = sweeper.execute(sweeper.plan())

    assert [outcome.state for outcome in report.outcomes] == ["deleted"]
    assert control.calls == [
        "list_targets GW1",
        "delete_target T1",
        "delete_target T2",
        "delete_gateway GW1",
    ]


def test_a_runtime_loses_its_endpoints_before_the_runtime_delete() -> None:
    """The runtime raises ConflictException while an endpoint still exists."""
    control = FakeAgentCore(
        runtimes=[
            {
                "agentRuntimeId": "R1",
                "agentRuntimeName": "verify_runtime",
                "agentRuntimeArn": "arn:r",
            }
        ],
        endpoints=["verifyendpoint", "DEFAULT"],
    )
    sweeper = sweep.Sweeper(
        clients=clients(**{"bedrock-agentcore-control": control}), kinds=("runtime",)
    )

    report = sweeper.execute(sweeper.plan())

    assert [outcome.state for outcome in report.outcomes] == ["deleted"]
    assert control.calls == ["delete_endpoint verifyendpoint", "delete_runtime R1"]
    # DEFAULT is created with the runtime and cannot be deleted on its own.
    # Trying would raise and abort the drain before the runtime.
    assert control.endpoints == ["DEFAULT"]


def test_roles_are_deleted_last_because_everything_else_references_them() -> None:
    control = FakeAgentCore(
        gateways=[{"gatewayId": "GW1", "name": "verify-gateway", "gatewayArn": "arn:g"}]
    )
    order: list[str] = []
    iam = FakeIam(roles=[{"RoleName": "workshop-verify-gateway-role", "Arn": "arn:r"}])
    original_delete_role = iam.delete_role
    original_delete_gateway = control.delete_gateway

    def note_role(RoleName: str) -> None:
        order.append("role")
        original_delete_role(RoleName)

    def note_gateway(gatewayIdentifier: str) -> None:
        order.append("gateway")
        original_delete_gateway(gatewayIdentifier)

    iam.delete_role = note_role  # type: ignore[method-assign]
    control.delete_gateway = note_gateway  # type: ignore[method-assign]

    sweeper = sweep.Sweeper(
        clients=clients(**{"bedrock-agentcore-control": control, "iam": iam})
    )
    sweeper.execute(sweeper.plan())

    assert order == ["gateway", "role"]


def test_a_role_is_emptied_of_policies_before_it_is_deleted() -> None:
    role = "workshop-verify-runtime-role"
    managed = "arn:aws:iam::aws:policy/ReadOnly"
    iam = FakeIam(
        roles=[{"RoleName": role, "Arn": "arn:r"}],
        inline={role: ["inline"]},
        attached={role: [managed]},
    )
    sweeper = sweep.Sweeper(clients=clients(iam=iam), kinds=("iam-role",))

    report = sweeper.execute(sweeper.plan())

    assert [outcome.state for outcome in report.outcomes] == ["deleted"]
    assert iam.calls == [
        f"delete_role_policy {role}/inline",
        f"detach_role_policy {role}/{managed}",
        f"delete_role {role}",
    ]


def test_probe_roles_are_deleted_and_vocareum_roles_are_not() -> None:
    """collect_roles filters by prefix too, so both gates have to agree.

    Both probe prefix generations are swept and voclabs is not, which is the whole
    point of the IAM list being narrower than the general one.
    """
    iam = FakeIam(
        roles=[
            {"RoleName": "agentcoreprobe-runtime-role", "Arn": "arn:new"},
            {"RoleName": "phase3probe-gateway-role", "Arn": "arn:old"},
            {"RoleName": "voclabs", "Arn": "arn:voclabs"},
            {"RoleName": "AWSServiceRoleForBedrockAgentCore", "Arn": "arn:slr"},
        ]
    )
    sweeper = sweep.Sweeper(clients=clients(iam=iam), kinds=("iam-role",))

    report = sweeper.execute(sweeper.plan())

    assert sorted(iam.deleted) == [
        "agentcoreprobe-runtime-role",
        "phase3probe-gateway-role",
    ]
    assert [outcome.state for outcome in report.outcomes] == ["deleted", "deleted"]


# ---------------------------------------------------------------------------
# The per-service traps
# ---------------------------------------------------------------------------


def test_an_ecr_repository_is_deleted_with_force() -> None:
    """Without force the images in it refuse the delete."""
    ecr = FakeEcr(
        [{"repositoryName": "bedrock-agentcore-verify", "repositoryArn": "a"}]
    )
    sweeper = sweep.Sweeper(clients=clients(ecr=ecr), kinds=("ecr",))

    report = sweeper.execute(sweeper.plan())

    assert ecr.deleted == [("bedrock-agentcore-verify", True)]
    assert [outcome.state for outcome in report.outcomes] == ["deleted"]


def test_a_table_is_waited_into_active_before_the_delete() -> None:
    """DynamoDB answers ResourceInUseException for a table that is still CREATING."""
    dynamodb = FakeDynamoDb(states=["CREATING", "ACTIVE", "ACTIVE"])
    sweeper = sweep.Sweeper(clients=clients(dynamodb=dynamodb), kinds=("dynamodb",))

    report = sweeper.execute(sweeper.plan())

    assert dynamodb.deleted == ["workshop-verify-bookings"]
    assert [outcome.state for outcome in report.outcomes] == ["deleted"]


def test_a_secret_is_force_deleted_so_the_name_is_free_next_session() -> None:
    """A scheduled delete holds the name for days and blocks the next CreateSecret."""
    secrets = FakeSecrets(
        [{"Name": "verify/neo4j-command", "ARN": "arn:s", "Tags": []}]
    )
    sweeper = sweep.Sweeper(clients=clients(secretsmanager=secrets), kinds=("secret",))

    sweeper.execute(sweeper.plan())

    assert secrets.deleted == [("arn:s", True)]


# ---------------------------------------------------------------------------
# The three kinds the hand-cleaned account turned up
# ---------------------------------------------------------------------------

RUNTIME_LOG = "/aws/bedrock-agentcore/runtimes/verify_runtime-abc123-DEFAULT"


def test_a_runtime_log_group_is_found_by_the_workshop_name_inside_its_path() -> None:
    """The workshop name is in the middle of a log group name, not at the front."""
    logs = FakeLogs(groups=[RUNTIME_LOG])
    sweeper = sweep.Sweeper(clients=clients(logs=logs), kinds=("log-group",))

    report = sweeper.execute(sweeper.plan())

    assert [item.selected_by for item in report.targets] == ["prefix"]
    assert logs.deleted == [RUNTIME_LOG]
    assert [outcome.state for outcome in report.outcomes] == ["deleted"]


def test_a_log_group_belonging_to_another_agent_is_left_alone() -> None:
    """Listing by the service path is a starting point, not the selection."""
    logs = FakeLogs(
        groups=["/aws/bedrock-agentcore/runtimes/someone-elses-agent-DEFAULT"]
    )
    sweeper = sweep.Sweeper(clients=clients(logs=logs), kinds=("log-group",))

    report = sweeper.execute(sweeper.plan())

    assert report.targets == []
    assert logs.deleted == []


def test_a_log_group_tag_is_read_from_the_arn_without_the_trailing_wildcard() -> None:
    """DescribeLogGroups returns ``...:*`` and ListTagsForResource rejects that form.

    The group here carries no workshop name at all, so the tag is the only thing
    that can select it. If the ARN went to the tag read unstripped it would read
    as untagged and the sweep would walk past a live log group.
    """
    name = "/aws/bedrock-agentcore/runtimes/renamed-by-the-real-workshop-DEFAULT"
    logs = FakeLogs(
        groups=[name],
        tags={
            f"arn:aws:logs:us-east-1:1:log-group:{name}": {
                sweep.WORKSHOP_TAG_KEY: sweep.WORKSHOP_TAG_VALUE
            }
        },
    )
    sweeper = sweep.Sweeper(clients=clients(logs=logs), kinds=("log-group",))

    report = sweeper.plan()

    assert [item.selected_by for item in report.targets] == ["tag"]


def test_log_groups_are_deleted_after_the_runtimes_that_wrote_them() -> None:
    """The service owns the group while the endpoint lives, so the runtime goes
    first.
    """
    control = FakeAgentCore(
        runtimes=[
            {
                "agentRuntimeId": "R1",
                "agentRuntimeName": "verify_runtime",
                "agentRuntimeArn": "arn:r",
            }
        ],
        endpoints=["DEFAULT"],
    )
    logs = FakeLogs(groups=[RUNTIME_LOG])
    order: list[str] = []
    original_delete_runtime = control.delete_agent_runtime
    original_delete_group = logs.delete_log_group

    def note_runtime(agentRuntimeId: str) -> None:
        order.append("runtime")
        original_delete_runtime(agentRuntimeId)

    def note_group(logGroupName: str, **_: Any) -> None:
        order.append("log-group")
        original_delete_group(logGroupName)

    control.delete_agent_runtime = note_runtime  # type: ignore[method-assign]
    logs.delete_log_group = note_group  # type: ignore[method-assign]

    sweeper = sweep.Sweeper(
        clients=clients(**{"bedrock-agentcore-control": control, "logs": logs}),
        kinds=("runtime", "log-group"),
    )
    sweeper.execute(sweeper.plan())

    assert order == ["runtime", "log-group"]


def test_a_codebuild_project_is_found_and_deleted() -> None:
    """The kind did not exist, so a project left by a deploy was never even listed."""
    codebuild = FakeCodeBuild(projects=["verify-agent-build", "unrelated-build"])
    sweeper = sweep.Sweeper(clients=clients(codebuild=codebuild), kinds=("codebuild",))

    report = sweeper.execute(sweeper.plan())

    assert [item.name for item in report.targets] == ["verify-agent-build"]
    assert codebuild.deleted == ["verify-agent-build"]
    assert [outcome.state for outcome in report.outcomes] == ["deleted"]


def test_a_codebuild_tag_is_read_from_codebuilds_lowercase_tag_shape() -> None:
    """CodeBuild spells the tag fields ``key`` and ``value``, not ``Key`` and
    ``Value``.
    """
    codebuild = FakeCodeBuild(
        projects=["renamed-by-the-real-workshop"],
        tags={
            "renamed-by-the-real-workshop": {
                sweep.WORKSHOP_TAG_KEY: sweep.WORKSHOP_TAG_VALUE
            }
        },
    )
    sweeper = sweep.Sweeper(clients=clients(codebuild=codebuild), kinds=("codebuild",))

    report = sweeper.plan()

    assert [item.selected_by for item in report.targets] == ["tag"]


def test_a_refused_batch_get_projects_still_sweeps_and_still_confirms() -> None:
    """lab.template does not grant BatchGetProjects, so neither half may need it.

    Selection falls back to the name prefix, and the confirm has to come from
    ListProjects: BatchGetProjects answers a missing project with an empty list
    and no error, so waiting on it to raise would wait out the whole timeout and
    then call a deleted project present.
    """
    codebuild = FakeCodeBuild(
        projects=["verify-agent-build"],
        batch_error=client_error("AccessDeniedException", "no", "BatchGetProjects"),
    )
    sweeper = sweep.Sweeper(clients=clients(codebuild=codebuild), kinds=("codebuild",))

    report = sweeper.execute(sweeper.plan())

    assert [item.selected_by for item in report.targets] == ["prefix"]
    assert [outcome.state for outcome in report.outcomes] == ["deleted"]


def test_the_build_source_bucket_name_is_computed_from_the_account_and_region() -> None:
    """Hard-coding it would name a bucket in an account the guards refuse to touch."""
    assert (
        sweep.source_bucket_name(ACCOUNT, "us-east-1")
        == f"bedrock-agentcore-codebuild-sources-{ACCOUNT}-us-east-1"
    )


def test_the_build_source_bucket_is_emptied_before_it_is_deleted() -> None:
    """DeleteBucket refuses a bucket holding anything, and this one holds the
    source archives.
    """
    bucket = sweep.source_bucket_name(ACCOUNT, "us-east-1")
    s3 = FakeS3(buckets=[bucket], objects={bucket: ["verify/source.zip"]})
    sweeper = sweep.Sweeper(clients=clients(s3=s3), kinds=("s3-bucket",))

    report = sweeper.execute(sweeper.plan())

    assert s3.calls == [
        f"delete_object {bucket}/verify/source.zip",
        f"delete_bucket {bucket}",
    ]
    assert [outcome.state for outcome in report.outcomes] == ["deleted"]


def test_an_account_with_no_build_source_bucket_finds_nothing() -> None:
    """A 404 from HeadBucket is not a finding, and it is not a read failure either."""
    sweeper = sweep.Sweeper(clients=clients(), kinds=("s3-bucket",))

    report = sweeper.plan()

    assert report.targets == []
    assert report.unreadable == []
    assert report.exit_code() == sweep.EXIT_OK


def test_without_an_account_id_the_bucket_name_is_not_guessed() -> None:
    """The name is two thirds account id, so an absent one means no candidate."""
    bucket = sweep.source_bucket_name(ACCOUNT, "us-east-1")
    s3 = FakeS3(buckets=[bucket])
    sweeper = sweep.Sweeper(clients=clients(account="", s3=s3), kinds=("s3-bucket",))

    report = sweeper.execute(sweeper.plan())

    assert report.targets == []
    assert s3.calls == []


def test_the_stack_owned_bucket_is_excluded_rather_than_deleted() -> None:
    """lab.template creates a bucket of its own and CloudFormation deletes it."""
    bucket = sweep.source_bucket_name(ACCOUNT, "us-east-1")
    cfn = FakeCloudFormation({"lab": [bucket]})
    s3 = FakeS3(buckets=[bucket])
    sweeper = sweep.Sweeper(
        clients=clients(cloudformation=cfn, s3=s3), kinds=("s3-bucket",)
    )

    report = sweeper.execute(sweeper.plan())

    assert [item.name for item in report.excluded] == [bucket]
    assert report.targets == []
    assert s3.calls == []


def test_every_call_on_the_source_bucket_names_the_account_it_expects() -> None:
    """S3 bucket names are global and this one is computed rather than listed.

    The name is `<stem>-<account>-<region>`, built from whatever account id the
    guards read off the live session. Vocareum rotates a pool of accounts, so
    the account this run is guarded against is not the account the last run
    used. Without `ExpectedBucketOwner` a bucket of that name in someone else's
    account answers the read, and then the delete path empties it. With the
    header S3 refuses the call outright.

    Asserted on every call rather than on the deletes, because the reads are
    what decide there is a bucket to delete at all.
    """
    bucket = sweep.source_bucket_name(ACCOUNT, "us-east-1")
    s3 = FakeS3(
        buckets=[bucket],
        objects={bucket: ["verify/source.zip"]},
        tags={bucket: {sweep.WORKSHOP_TAG_KEY: sweep.WORKSHOP_TAG_VALUE}},
    )
    sweeper = sweep.Sweeper(clients=clients(s3=s3), kinds=("s3-bucket",))

    sweeper.execute(sweeper.plan())

    assert s3.owners, "no S3 call was made, so the assertion below proves nothing"
    assert set(s3.owners) == {ACCOUNT}


# ---------------------------------------------------------------------------
# How long the confirm waits
# ---------------------------------------------------------------------------


def test_the_runtime_waits_longer_than_wait_until_gone_defaults_to() -> None:
    """A runtime was measured taking 100 or more seconds to leave the list.

    phase.wait_until_gone defaults to 120 seconds, which is 20 seconds of slack
    against a delay already observed at 100. Under-waiting does not leak
    anything; it reports a false survivor, which sends an instructor to delete a
    resource that was already going.
    """
    default = inspect.signature(phase.wait_until_gone).parameters["timeout"].default

    assert default == 120
    assert sweep.RUNTIME_GONE_TIMEOUT == 300
    assert default < sweep.RUNTIME_GONE_TIMEOUT


def test_the_handlers_own_timeout_is_the_one_that_reaches_the_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-kind number that never leaves the Handler is a comment, not a timeout."""
    seen: list[int] = []

    def record_timeout(*_: Any, timeout: int = 0, **__: Any) -> bool:
        seen.append(timeout)
        return True

    monkeypatch.setattr(sweep, "wait_until_gone", record_timeout)
    control = FakeAgentCore(
        runtimes=[
            {
                "agentRuntimeId": "R1",
                "agentRuntimeName": "verify_runtime",
                "agentRuntimeArn": "arn:r",
            }
        ]
    )
    sweeper = sweep.Sweeper(
        clients=clients(**{"bedrock-agentcore-control": control}), kinds=("runtime",)
    )

    sweeper.execute(sweeper.plan())

    assert seen == [sweep.RUNTIME_GONE_TIMEOUT]


# ---------------------------------------------------------------------------
# A failed delete is reported, never swallowed
# ---------------------------------------------------------------------------


def test_a_refused_delete_is_reported_and_fails_the_run() -> None:
    """Silence on a failed delete is the bug this whole module exists for."""
    awslambda = FakeLambda(
        functions=[
            {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"}
        ],
        delete_error=client_error("AccessDeniedException", "no", "DeleteFunction"),
    )
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda}), kinds=("lambda",))

    report = sweeper.execute(sweeper.plan())

    assert [outcome.state for outcome in report.outcomes] == ["failed"]
    assert "AccessDeniedException" in report.outcomes[0].detail
    assert report.survivors
    assert report.exit_code() == sweep.EXIT_SURVIVED


def test_one_refused_delete_does_not_stop_the_rest_of_the_sweep() -> None:
    awslambda = FakeLambda(
        functions=[
            {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"}
        ],
        delete_error=client_error("AccessDeniedException", "no", "DeleteFunction"),
    )
    iam = FakeIam(roles=[{"RoleName": "workshop-verify-lambda-role", "Arn": "arn:r"}])
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda, "iam": iam}))

    report = sweeper.execute(sweeper.plan())

    assert iam.deleted == ["workshop-verify-lambda-role"]
    assert {outcome.state for outcome in report.outcomes} == {"failed", "deleted"}


def test_a_delete_that_returned_but_left_the_resource_is_a_survivor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delete that returns is not a delete that finished."""
    clock = iter([0.0, 0.0, 0.0, 900.0])
    monkeypatch.setattr("vocareum_tools.phase.time.monotonic", lambda: next(clock))
    awslambda = FakeLambda(
        functions=[
            {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"}
        ]
    )
    # The delete succeeds and the describe keeps answering, which is exactly
    # what a delete that has not finished looks like from the outside.
    awslambda.get_function = lambda **_: {"Configuration": {}}  # type: ignore[method-assign]
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda}), kinds=("lambda",))

    report = sweeper.execute(sweeper.plan())

    assert [outcome.state for outcome in report.outcomes] == ["present"]
    assert report.exit_code() == sweep.EXIT_SURVIVED


def test_a_delete_of_something_already_gone_is_absent_not_a_failure() -> None:
    awslambda = FakeLambda(
        functions=[
            {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"}
        ],
        delete_error=gone(),
    )
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda}), kinds=("lambda",))

    report = sweeper.execute(sweeper.plan())

    assert [outcome.state for outcome in report.outcomes] == ["absent"]
    assert report.exit_code() == sweep.EXIT_OK


def test_a_deleted_ecr_repository_is_confirmed_gone_by_its_own_error_code() -> None:
    """ECR says RepositoryNotFoundException, which the shared default misses."""
    ecr = FakeEcr(
        [{"repositoryName": "bedrock-agentcore-verify", "repositoryArn": "a"}]
    )
    sweeper = sweep.Sweeper(clients=clients(ecr=ecr), kinds=("ecr",))

    report = sweeper.execute(sweeper.plan())

    assert [outcome.state for outcome in report.outcomes] == ["deleted"]


def test_a_service_that_cannot_be_listed_is_reported_and_fails_the_run() -> None:
    """A service the sweep could not read is an account it cannot call clean."""

    class Denied:
        def list_functions(self, **_: Any) -> dict[str, Any]:
            raise client_error("AccessDeniedException", "no", "ListFunctions")

    sweeper = sweep.Sweeper(clients=clients(**{"lambda": Denied()}), kinds=("lambda",))

    report = sweeper.plan()

    assert report.unreadable and report.unreadable[0][0] == "lambda"
    assert report.exit_code() == sweep.EXIT_SURVIVED


def test_a_denied_tag_read_leaves_the_prefix_half_to_do_the_work() -> None:
    """The tag read is the half that can be refused, so it cannot be required."""

    class DeniedTags(FakeLambda):
        def list_tags(self, Resource: str, **_: Any) -> dict[str, Any]:
            raise client_error("AccessDeniedException", "no", "ListTags")

    awslambda = DeniedTags(
        functions=[
            {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"},
            {"FunctionName": "unrelated", "FunctionArn": "arn:u"},
        ]
    )
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda}), kinds=("lambda",))

    report = sweeper.plan()

    assert [item.name for item in report.targets] == ["hotel-booking-verify-tool"]
    assert report.targets[0].selected_by == "prefix"


def test_a_denied_tag_read_is_counted_but_does_not_change_the_exit_code() -> None:
    """Two claims, and the second is the one worth stating.

    An entry in `report.unreadable` exits non-zero, and a refused tag read is
    the ordinary state of an account that was never granted the tagging reads,
    so putting it there would exit non-zero on every clean sweep. It is counted
    and printed instead, which is what lets an instructor tell an empty plan
    from a plan made half blind.
    """

    class DeniedTags(FakeLambda):
        def list_tags(self, Resource: str, **_: Any) -> dict[str, Any]:
            raise client_error("AccessDeniedException", "no", "ListTags")

    awslambda = DeniedTags(
        functions=[
            {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"},
            {"FunctionName": "unrelated", "FunctionArn": "arn:u"},
        ]
    )
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda}), kinds=("lambda",))

    report = sweeper.plan()

    assert report.tag_reads.denials == 2
    assert report.unreadable == []
    assert report.exit_code() == sweep.EXIT_OK
    assert report.as_dict()["tagReads"]["denials"] == 2


def test_a_tag_read_that_answered_is_not_counted_as_a_denial() -> None:
    awslambda = FakeLambda(
        functions=[
            {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"}
        ]
    )
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda}), kinds=("lambda",))

    report = sweeper.plan()

    assert (report.tag_reads.reads, report.tag_reads.denials) == (1, 0)


def test_an_untagged_bucket_answers_rather_than_refusing() -> None:
    """S3 reports an untagged bucket as `NoSuchTagSet`, which is an answer. If
    that counted as a denial the count would fire on every clean sweep, which is
    the same noise the quiet codes were added to stop."""
    reads = TagReads()

    def untagged() -> dict[str, str]:
        raise client_error("NoSuchTagSet", "none", "GetBucketTagging")

    assert (
        sweep.tags_or_empty(
            untagged, "bucket x", quiet_codes={"NoSuchTagSet"}, record=reads
        )
        == {}
    )
    assert (reads.reads, reads.denials) == (1, 0)


def test_the_plan_says_out_loud_that_the_tag_half_was_blind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty plan on an account nobody could read is the failure this whole
    module was written against, and a tag read is one of the ways to not read
    one."""

    class DeniedTags(FakeLambda):
        def list_tags(self, Resource: str, **_: Any) -> dict[str, Any]:
            raise client_error("AccessDeniedException", "no", "ListTags")

    awslambda = DeniedTags(functions=[{"FunctionName": "x", "FunctionArn": "arn:x"}])
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda}), kinds=("lambda",))

    sweep.render_plan(sweeper.plan())

    printed = capsys.readouterr().out
    assert "tag reads refused for 1 of 1" in printed
    assert "only as good as the half that could still see" in printed


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_a_dry_run_deletes_nothing_and_exits_zero() -> None:
    awslambda = FakeLambda(
        functions=[
            {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"}
        ]
    )
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda}), kinds=("lambda",))

    report = sweeper.plan()

    assert report.dry_run is True
    assert len(report.targets) == 1
    assert awslambda.deleted == []
    assert report.outcomes == []
    assert report.exit_code() == sweep.EXIT_OK


def test_the_plan_names_the_stacks_it_read_and_the_ids_it_collected() -> None:
    cfn = FakeCloudFormation({"lab": ["workshop-verify-a", "arn:aws:x:::thing/b"]})
    sweeper = sweep.Sweeper(clients=clients(cloudformation=cfn))

    report = sweeper.plan(account="123456789012", region="us-east-1")
    payload = report.as_dict()

    assert payload["stacks"] == ["lab"]
    assert "workshop-verify-a" in payload["stackResourceIds"]
    assert payload["account"] == "123456789012"
    assert payload["dryRun"] is True


def test_a_failure_detail_is_redacted_before_it_reaches_the_report() -> None:
    awslambda = FakeLambda(
        functions=[
            {"FunctionName": "hotel-booking-verify-tool", "FunctionArn": "arn:l"}
        ],
        delete_error=client_error(
            "ValidationException", '{"password": "hunter2"}', "DeleteFunction"
        ),
    )
    sweeper = sweep.Sweeper(clients=clients(**{"lambda": awslambda}), kinds=("lambda",))

    report = sweeper.execute(sweeper.plan())

    assert "hunter2" not in report.outcomes[0].detail


def test_render_says_why_a_resource_was_excluded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A silent exclusion is indistinguishable from a resource that is not there."""
    cfn = FakeCloudFormation({"lab": ["workshop-verify-a"]})
    iam = FakeIam(roles=[{"RoleName": "workshop-verify-a", "Arn": "arn:r"}])
    sweeper = sweep.Sweeper(clients=clients(cloudformation=cfn, iam=iam))

    sweep.render_plan(sweeper.plan())

    printed = capsys.readouterr().out
    assert "lab" in printed
    assert "CloudFormation owns them" in printed
    assert "skip     iam-role workshop-verify-a" in printed


def test_pages_follows_a_continuation_token_and_stops_without_one() -> None:
    seen: list[str | None] = []

    def call(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs.get("nextToken"))
        if not kwargs.get("nextToken"):
            return {"items": [1], "nextToken": "second"}
        return {"items": [2]}

    collected = [
        item
        for page in sweep.pages(call, "nextToken", "nextToken")
        for item in page["items"]
    ]

    assert collected == [1, 2]
    assert seen == [None, "second"]


def test_course_cleanup_prefixes_extend_only_their_handler_classes() -> None:
    sweeper = sweep.Sweeper(
        clients=clients(),
        cleanup=CleanupRules(
            resource_prefixes=("course-resource-",),
            iam_role_prefixes=("course-role-",),
            s3_bucket_prefixes=("course-bucket-",),
        ),
    )
    handlers = {handler.kind: handler for handler in sweeper.handlers()}

    assert "course-resource-" in handlers["lambda"].prefixes
    assert "course-role-" in handlers["iam-role"].prefixes
    assert "course-bucket-" in handlers["s3-bucket"].prefixes
    assert "course-resource-" not in handlers["iam-role"].prefixes
    assert (
        "/aws/bedrock-agentcore/runtimes/course-resource-"
        in handlers["log-group"].prefixes
    )
