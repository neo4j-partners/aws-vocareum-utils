"""Empty a Vocareum student AWS account of everything the workshop created.

The notebook already tears down what it built, but only from ``CLEANUPS``, a
list that lives in the Jupyter kernel. Restart the kernel, re-run a cell, or
close the tab and the list is gone while the resources are not. Measured in
account ``${LAB_AWS_ACCOUNT_ID}`` on 2026-08-06: a ``wsverify-gateway`` created
2026-08-04T00:50:14Z was still ``READY`` two days and four sessions later,
because the ``DeleteGateway`` 34 seconds behind its create had failed and
nothing else ever looked.

Nothing else does look. The AWS student view has no End Lab that cleans the
account, and a real workshop cannot depend on every student reaching a teardown
cell. Vocareum does delete the CloudFormation stack at session end -- stack
``c${COURSE}a${ASSIGNMENT}l${PART}t1w${LAB_AWS_ACCOUNT_ID}`` has five generations, four
of them ``DELETE_COMPLETE``, one per session -- and it deletes nothing else. The
August 4 gateway survived one of those stack deletions, which is the proof.

So cleanup has to be runnable by an instructor, against an account, derived
from that account's own contents, with no session state at all. That is this
module. It is the same shape as the phase runners: it takes clients, classifies
what they raise, and polls rather than trusting a delete that returned, so it is
testable without a lab session.

Three things here are load-bearing rather than tidy:

**Nothing CloudFormation owns is touched.** The lab stack creates resources and
deletes them itself when the session ends. Deleting one out from under it risks
``DELETE_FAILED``, which leaves a stack that Vocareum cannot clear either, which
is a worse account than the one this tool was written to fix. Every physical
resource id in every live stack is collected first and excluded by name, id, and
ARN.

**Selection is a tag union a name prefix, not either alone.** The tag is
``WorkshopResource=stop-ai-agent-hallucinations``. The prefix path is not
redundant: applying that tag to an AgentCore resource is itself one of the
permissions the notebook measures, step 10, so on an account where
``bedrock-agentcore:TagResource`` is refused the resource exists untagged. A
tag-only sweep would walk straight past it and report a clean account. The
prefix path is also the only thing that would have found the August 4 gateway,
whose name is ``wsverify-gateway`` from before the runtime naming bug was
understood.

**A delete that returns is not a delete that finished.** Every delete is
followed by ``wait_until_gone``, and anything still present at the end is
reported and exits non-zero. Silence on a failed delete is the exact bug that
produced this module.

Order is per service and each step of it was paid for:

1. **AgentCore Runtime** -- endpoints first, then the runtime. The runtime
   refuses while an endpoint exists and raises ``ConflictException``. That is
   timing, so it is waited out rather than counted as a failure.
2. **AgentCore Runtime log groups** -- ``/aws/bedrock-agentcore/runtimes/*``,
   one per runtime endpoint, and after the runtime rather than before it. The
   service writes to them for as long as the endpoint exists, and it does not
   remove them when the runtime goes.
3. **AgentCore Gateway** -- wait for the gateway to settle, then drain its
   targets, then delete it. ``DeleteGateway`` against an unsettled gateway
   fails with ``ValidationException``, which is how the August 4 gateway
   leaked.
4. **AgentCore Memory.**
5. **Lambda function.**
6. **CodeBuild project** -- the AgentCore SDK builds the agent image through
   CodeBuild, so a project is left behind by any run that got as far as a
   deploy.
7. **ECR repository** -- ``force=True``, or the images block the delete.
8. **S3 build-source bucket** -- ``bedrock-agentcore-codebuild-sources``
   followed by the account and the region. Objects first, then the bucket, or
   ``DeleteBucket`` refuses. Named from the account and region rather than
   hard-coded, because the account changes every time the Vocareum pool
   rotates.
9. **DynamoDB table** -- must be ``ACTIVE`` before it accepts a delete.
10. **Secrets Manager secret** -- ``ForceDeleteWithoutRecovery``, or the name is
    locked for days and the next session cannot recreate it.
11. **IAM roles** -- inline policies deleted and managed policies detached
    first, and last overall, because the gateway, runtime, and Lambda above all
    reference them.

Steps 2, 6, and 8 were added after the fact. Account ``${LAB_AWS_ACCOUNT_ID}``
was emptied by hand on 2026-08-06 and then compared against what this module
would have done, and those three kinds are what it would have walked past: the
per-endpoint log groups it never listed, the CodeBuild projects it had no
handler for, and the build-source bucket it did not know the name of. A sweep
that reports a clean account it did not fully read is the failure this module
was written against, so all three are handled here rather than noted as known
gaps.

**The rule for recognising a resource is not defined here.** It is
``workshop_lab.selection``, which the notebook's own step 14 teardown imports
too. Closing those same three gaps twice in parallel produced two answers to one
question -- which tag key casing counts, how a log group under the service's own
path is matched, which S3 codes mean the bucket is not there -- and a
disagreement there decides what gets deleted out of an account that is about to
become unreachable. The shared module lives in ``workshop_lab`` and not here
because ``workshop_lab`` is the package a student's notebook installs, alone,
from the workshop repository. This package is never installed in a lab, so an
import the other way round would resolve on an instructor's machine and fail in
every lab.

What stays here is what only an instructor has. This module's ``NAME_PREFIXES``
is the union across every generation of names that could still be sitting in a
pooled account, where the notebook knows the one prefix its own run used;
``tests/management/test_sweep.py`` asserts this list stays a superset of that
one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from workshop_lab.selection import (
    AGENTCORE_RUNTIME_LOG_PREFIX,
    BUCKET_GONE_CODES,
    IMPLICIT_ENDPOINT,
    S3_SOURCE_BUCKET_STEM,
    WORKSHOP_TAG_KEY,
    WORKSHOP_TAG_VALUE,
    TagReads,
    log_group_prefixes,
    selected_by,
    source_bucket_name,
    stack_owned_keys,
    tag_map,
)

from .client import ApiError
from .config import ConfigError, Environment, add_course_argument, repository_root
from .course import CleanupRules, load_definition
from .output import section
from .phase import json_default, wait_until, wait_until_gone
from .probe import (
    BOTO_CONFIG,
    REQUIRED_REGION,
    GuardError,
    load_config,
    open_guarded_session,
)
from .security import redact_text

# Every name shape the workshop and the verification notebook create, as a
# startswith tuple. Written out rather than derived from PREFIX = "verify",
# because three of these names put the prefix in the middle:
# bedrock-agentcore-verify is the ECR repository, hotel-booking-verify-tool is
# the Lambda, and workshop-verify-* is every IAM role and the DynamoDB table.
# "wsverify" is the pre-bugfix runtime prefix, and the August 4 gateway that
# started this carries it, so it stays until that account generation is gone.
# AmazonBedrockAgentCoreSDK* is what the AgentCore SDK names the roles it
# creates on a student's behalf; lab.template grants IAM management over that
# prefix for exactly that reason.
# "agentcoreprobe" is the PREFIX in scripts/run_phase3_agentcore.py. That runner
# creates AgentCore resources under it and leaks them the same way the notebook
# did, and nothing here was matching the name, so a sweep after one of its runs
# reported a clean account. "phase3probe" is what that PREFIX was called before
# it was renamed to say what the runner does rather than which internal milestone
# it belonged to. It stays because past runs left resources under the old name in
# accounts that are still in the Vocareum pool, and this module is the only thing
# that will remove them; it can go once every one of those account generations
# has rotated out, on the same terms as "wsverify" above.
NAME_PREFIXES: tuple[str, ...] = (
    "verify",
    "wsverify",
    "workshop-",
    "bedrock-agentcore-verify",
    "hotel-booking-verify",
    "agentcoreprobe",
    "phase3probe",
    "AmazonBedrockAgentCoreSDK",
)

# IAM is selected by name prefix only and never by tag alone, and that narrowing
# is a safety property rather than an optimisation. The account also holds
# Vocareum's own voclabs and vocareum roles and whatever service-linked roles AWS
# put there, and a role selected by tag alone would be deletable the moment
# anything tagged one. A role has to match one of these prefixes to be in scope
# no matter what tags it carries.
#
# The probe prefixes are in scope, and were once left out on the reasoning that
# lab.template scopes iam:* to workshop-* and AmazonBedrockAgentCoreSDK*, so a
# probe-role delete would be refused anyway and every refusal is a survivor that
# fails the run. That inference is wrong, and it is wrong against a measurement
# rather than against an opinion. lab.template grants through
# AWS::IAM::RolePolicy, which is additive to Vocareum's own inline Pvoclabs
# policy on the voclabs role rather than a ceiling over it. Live sessions
# measured iam:DeleteRole, iam:DeleteRolePolicy, iam:DetachRolePolicy,
# iam:ListRolePolicies, iam:ListAttachedRolePolicies, and iam:GetRole as Allowed,
# re-measured in Phase 3:C on phase3probe-* role names for the express purpose of
# showing the result is not scoped to the workshop- prefix, and measured
# iam:CreateRole and iam:DeleteRole succeeding on those same names even in the
# lab.policy era when iam:PutRolePolicy on them was refused. Every call
# delete_role makes below is in that set, so a probe role is deletable. It also
# has to be: once the Vocareum pool rotates the account away, a role left behind
# is unreachable by anything.
IAM_ROLE_PREFIXES: tuple[str, ...] = (
    "workshop-",
    "AmazonBedrockAgentCoreSDK",
    "agentcoreprobe",
    "phase3probe",
)

# The composed form of the shared prefixes, for the one kind whose names do not
# start with a workshop name. `workshop_lab.selection.log_group_tail` is the same
# test written the other way round, for the notebook's teardown, which holds one
# name rather than a prefix list; a test there proves the two agree.
LOG_GROUP_PREFIXES: tuple[str, ...] = log_group_prefixes(NAME_PREFIXES)

# The build-source bucket stem is the whole prefix list for the bucket kind, so
# that nothing else in the account can be selected by name. The bucket carries
# neither the workshop tag nor a workshop name, which is why it needs its own.
S3_BUCKET_PREFIXES: tuple[str, ...] = (S3_SOURCE_BUCKET_STEM,)

# BatchGetProjects takes at most 100 names per call.
CODEBUILD_BATCH = 100

# How long to keep asking whether a deleted resource has really gone.
#
# phase.wait_until_gone defaults to 120 seconds, which is too thin for anything
# here: measured against account ${LAB_AWS_ACCOUNT_ID} on 2026-08-06, an
# AgentCore Runtime stayed visible in list-agent-runtimes for 100 or more
# seconds after DeleteAgentRuntime returned. A 120 second budget has 20 seconds
# of slack against a delay already observed at 100, so it would report a false
# survivor, and a false survivor is what makes an instructor go delete a
# resource that was already on its way out. The runtime is named separately from
# the shared default because it is the only kind whose delay has been measured,
# so it is the only one whose number means something.
GONE_TIMEOUT = 300
RUNTIME_GONE_TIMEOUT = 300

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_GUARD = 2
EXIT_SURVIVED = 3


class SweepError(RuntimeError):
    """The sweep could not be set up, so nothing was swept."""


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@dataclass
class Clients:
    """One boto3 client per service, or a fake a test supplied instead.

    ``overrides`` is what keeps this module offline-testable. The phase runners
    take callables for the same reason; a sweep needs whole clients rather than
    single calls, because which resources exist is itself something it has to
    discover, so the seam moves up one level.

    ``account`` is here rather than on ``Sweeper`` because one collector needs
    it: the AgentCore build-source bucket is the only resource in the account
    whose name cannot be discovered by listing, only computed, and the account
    id is half of that name.

    ``tag_reads`` is here for the same shape of reason. Every collector holds a
    ``Clients`` and nothing else, and the tag reads happen inside them, one per
    resource, well below the report that has to name how many were refused.
    """

    session: Any = None
    region: str = REQUIRED_REGION
    account: str = ""
    overrides: dict[str, Any] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)
    tag_reads: TagReads = field(default_factory=TagReads)

    def get(self, name: str) -> Any:
        """Return the client for a service, building it once."""
        if name in self.overrides:
            return self.overrides[name]
        if name not in self.cache:
            if self.session is None:
                raise SweepError(
                    f"No boto3 session and no override for the {name} client."
                )
            self.cache[name] = self.session.client(
                name, region_name=self.region, config=BOTO_CONFIG
            )
        return self.cache[name]

    @property
    def control(self) -> Any:
        """Return the AgentCore control-plane client."""
        return self.get("bedrock-agentcore-control")

    @property
    def awslambda(self) -> Any:
        """Return the Lambda client."""
        return self.get("lambda")

    @property
    def ecr(self) -> Any:
        """Return the ECR client."""
        return self.get("ecr")

    @property
    def dynamodb(self) -> Any:
        """Return the DynamoDB client."""
        return self.get("dynamodb")

    @property
    def secrets(self) -> Any:
        """Return the Secrets Manager client."""
        return self.get("secretsmanager")

    @property
    def iam(self) -> Any:
        """Return the IAM client."""
        return self.get("iam")

    @property
    def logs(self) -> Any:
        """Return the CloudWatch Logs client."""
        return self.get("logs")

    @property
    def s3(self) -> Any:
        """Return the S3 client."""
        return self.get("s3")

    @property
    def codebuild(self) -> Any:
        """Return the CodeBuild client."""
        return self.get("codebuild")

    @property
    def cloudformation(self) -> Any:
        """Return the CloudFormation client, read only in this module."""
        return self.get("cloudformation")


# ---------------------------------------------------------------------------
# Paging and retries
# ---------------------------------------------------------------------------


def pages(
    call: Callable[..., dict[str, Any]],
    token_in: str,
    token_out: str,
    **kwargs: Any,
) -> Iterator[dict[str, Any]]:
    """Yield every page of a list call, whatever it named its continuation token.

    Written by hand rather than with ``get_paginator`` because the AgentCore
    control plane has no paginators in botocore, and because a fake client in a
    test would then have to implement the paginator protocol to be driven at
    all. Every service here spells the token differently, which is the only
    reason both names are parameters.
    """
    token: str | None = None
    while True:
        extra = {token_in: token} if token else {}
        payload = call(**kwargs, **extra)
        yield payload
        token = payload.get(token_out)
        if not token:
            return


def retry_while(
    call: Callable[[], Any],
    codes: set[str],
    label: str,
    timeout: int = 180,
    interval: int = 10,
) -> Any:
    """Retry a call while it fails with a code known to be a timing artifact.

    Not in ``phase.py`` because it is not a wait for a state: it is a wait for
    another service's asynchronous work to finish, visible only as a refusal.
    The runtime delete is the case. ``ConflictException`` there means an
    endpoint is still on its way out, which is not a teardown failure and must
    not be recorded as one, because recording it orphans the runtime.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return call()
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code not in codes or time.monotonic() >= deadline:
                raise
            print(f"        {label}: {code}, retrying")
            time.sleep(interval)


# ---------------------------------------------------------------------------
# Candidates, selection, and stack ownership
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One resource found in the account, before anything decided its fate."""

    kind: str
    identifier: str
    name: str
    arn: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    selected_by: str = ""
    note: str = ""

    @property
    def label(self) -> str:
        """Return a one-line name for logs and reports."""
        return f"{self.kind} {self.name or self.identifier}"

    def keys(self) -> set[str]:
        """Return every string CloudFormation could have used to name this."""
        return {value for value in (self.identifier, self.name, self.arn) if value}

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-ready mapping."""
        return {
            "kind": self.kind,
            "identifier": self.identifier,
            "name": self.name,
            "arn": self.arn,
            "selectedBy": self.selected_by,
            "note": self.note,
        }


def selection_reason(
    candidate: Candidate,
    prefixes: tuple[str, ...],
    tag_key: str = WORKSHOP_TAG_KEY,
    tag_value: str = WORKSHOP_TAG_VALUE,
    require_prefix: bool = False,
) -> str:
    """Say why a candidate is in scope, or return an empty string for out of scope.

    The rule itself is ``workshop_lab.selection.selected_by``, shared with the
    notebook's own teardown. This is the ``Candidate``-shaped call into it, kept
    because every handler here holds a candidate and nothing else.
    """
    return selected_by(
        candidate.name,
        candidate.tags,
        prefixes,
        tag_key=tag_key,
        tag_value=tag_value,
        require_prefix=require_prefix,
    )


@dataclass
class StackOwned:
    """The resources the lab stack owns, which the sweep must not touch."""

    stacks: list[str] = field(default_factory=list)
    identifiers: set[str] = field(default_factory=set)
    unreadable: list[str] = field(default_factory=list)

    def owns(self, candidate: Candidate) -> bool:
        """Report whether CloudFormation created this resource."""
        return bool(candidate.keys() & self.identifiers)


def collect_stack_owned(clients: Clients, stack_name: str | None = None) -> StackOwned:
    """Collect every physical resource id CloudFormation currently owns.

    Every live stack is read, not just the one whose name looks like Vocareum's
    ``c<digits>...t1w<account>``. Exclusion is the safe direction: over-exclude
    and a resource survives one more sweep, under-exclude and a stack delete
    fails and the account cannot be recycled at all. So ambiguity widens the
    exclusion rather than narrowing it, and the stacks that contributed are
    named in the report so a surprising exclusion can be traced.

    ``describe_stacks`` with no arguments omits ``DELETE_COMPLETE`` stacks,
    which is what makes the four already-deleted generations of the lab stack
    irrelevant here.
    """
    owned = StackOwned()
    cfn = clients.cloudformation
    try:
        if stack_name:
            names = [stack_name]
        else:
            names = [
                stack["StackName"]
                for page in pages(cfn.describe_stacks, "NextToken", "NextToken")
                for stack in page.get("Stacks", [])
            ]
    except (ClientError, BotoCoreError) as error:
        owned.unreadable.append(f"describe_stacks: {redact_text(str(error))}")
        return owned

    for name in names:
        try:
            response = cfn.describe_stack_resources(StackName=name)
        except (ClientError, BotoCoreError) as error:
            owned.unreadable.append(
                f"describe_stack_resources {name}: {redact_text(str(error))}"
            )
            continue
        owned.stacks.append(name)
        for resource in response.get("StackResources", []):
            owned.identifiers |= stack_owned_keys(
                str(resource.get("PhysicalResourceId", ""))
            )
    return owned


# ---------------------------------------------------------------------------
# Tag reads
# ---------------------------------------------------------------------------


def tags_or_empty(
    read: Callable[[], dict[str, str]],
    label: str,
    quiet_codes: Collection[str] = (),
    record: TagReads | None = None,
) -> dict[str, str]:
    """Read a resource's tags, treating a refusal as no tags rather than as fatal.

    A denied tag read blinds the tag half of the selection for one resource. It
    is not fatal because the prefix half exists for precisely this case, but it
    is printed, because a sweep that found nothing on an account where every tag
    read was refused looks identical to a clean account otherwise.

    ``quiet_codes`` names the codes that are not a refusal at all. S3 answers
    ``GetBucketTagging`` on an untagged bucket with ``NoSuchTagSet``, so an
    untagged bucket would otherwise print a line that reads like a permission
    problem on every clean sweep. A genuine denial still prints.

    ``record`` counts the two apart. The per-resource line above scrolls past in
    a sweep of a busy account, and the number the report needs at the end is how
    many resources the tag half could not see, which no individual line answers.
    A quiet code is an answer, not a denial, and counts as one.
    """
    try:
        tags = read()
    except ClientError as error:
        if str(error.response["Error"]["Code"]) in set(quiet_codes):
            if record is not None:
                record.answered()
            return {}
        if record is not None:
            record.refused(label)
        print(f"  could not read tags for {label}: {redact_text(str(error))[:160]}")
        return {}
    except BotoCoreError as error:
        if record is not None:
            record.refused(label)
        print(f"  could not read tags for {label}: {redact_text(str(error))[:160]}")
        return {}
    if record is not None:
        record.answered()
    return tags


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


def agentcore_tags(clients: Clients, arn: str, label: str) -> dict[str, str]:
    """Return an AgentCore resource's tags as a mapping."""
    if not arn:
        return {}
    control = clients.control
    return tags_or_empty(
        lambda: dict(
            control.list_tags_for_resource(resourceArn=arn).get("tags", {}) or {}
        ),
        label,
        record=clients.tag_reads,
    )


def collect_runtimes(clients: Clients) -> list[Candidate]:
    """Find every AgentCore Runtime in the account."""
    control = clients.control
    found: list[Candidate] = []
    for page in pages(control.list_agent_runtimes, "nextToken", "nextToken"):
        for item in page.get("agentRuntimes", []):
            arn = str(item.get("agentRuntimeArn", ""))
            name = str(item.get("agentRuntimeName", ""))
            found.append(
                Candidate(
                    kind="runtime",
                    identifier=str(item.get("agentRuntimeId", "")),
                    name=name,
                    arn=arn,
                    tags=agentcore_tags(clients, arn, f"runtime {name}"),
                )
            )
    return found


def collect_gateways(clients: Clients) -> list[Candidate]:
    """Find every AgentCore Gateway in the account."""
    control = clients.control
    found: list[Candidate] = []
    for page in pages(control.list_gateways, "nextToken", "nextToken"):
        for item in page.get("items", []):
            arn = str(item.get("gatewayArn", ""))
            name = str(item.get("name", ""))
            found.append(
                Candidate(
                    kind="gateway",
                    identifier=str(item.get("gatewayId", "")),
                    name=name,
                    arn=arn,
                    tags=agentcore_tags(clients, arn, f"gateway {name}"),
                    note=str(item.get("status", "")),
                )
            )
    return found


def collect_memories(clients: Clients) -> list[Candidate]:
    """Find every AgentCore Memory in the account.

    ``list_memories`` returns an id and an ARN but no separate name, and the id
    is ``{name}-{suffix}``, so the id is what the prefix is matched against.
    That is why the notebook's ``verify_memory`` is findable at all.
    """
    control = clients.control
    found: list[Candidate] = []
    for page in pages(control.list_memories, "nextToken", "nextToken"):
        for item in page.get("memories", []):
            identifier = str(item.get("id", ""))
            arn = str(item.get("arn", ""))
            found.append(
                Candidate(
                    kind="memory",
                    identifier=identifier,
                    name=str(item.get("name") or identifier),
                    arn=arn,
                    tags=agentcore_tags(clients, arn, f"memory {identifier}"),
                )
            )
    return found


def collect_functions(clients: Clients) -> list[Candidate]:
    """Find every Lambda function in the account."""
    awslambda = clients.awslambda
    found: list[Candidate] = []
    for page in pages(awslambda.list_functions, "Marker", "NextMarker"):
        for item in page.get("Functions", []):
            name = str(item.get("FunctionName", ""))
            arn = str(item.get("FunctionArn", ""))
            found.append(
                Candidate(
                    kind="lambda",
                    identifier=name,
                    name=name,
                    arn=arn,
                    tags=tags_or_empty(
                        lambda a=arn: dict(
                            awslambda.list_tags(Resource=a).get("Tags", {}) or {}
                        ),
                        f"lambda {name}",
                        record=clients.tag_reads,
                    ),
                )
            )
    return found


def collect_repositories(clients: Clients) -> list[Candidate]:
    """Find every ECR repository in the account."""
    ecr = clients.ecr
    found: list[Candidate] = []
    for page in pages(ecr.describe_repositories, "nextToken", "nextToken"):
        for item in page.get("repositories", []):
            name = str(item.get("repositoryName", ""))
            arn = str(item.get("repositoryArn", ""))
            found.append(
                Candidate(
                    kind="ecr",
                    identifier=name,
                    name=name,
                    arn=arn,
                    tags=tags_or_empty(
                        lambda a=arn: tag_map(
                            ecr.list_tags_for_resource(resourceArn=a).get("tags", [])
                        ),
                        f"ecr repository {name}",
                        record=clients.tag_reads,
                    ),
                )
            )
    return found


def collect_tables(clients: Clients) -> list[Candidate]:
    """Find every DynamoDB table in the account.

    ``list_tables`` returns names only, so each one is described for its ARN.
    A table that vanishes between the two calls is skipped rather than carried
    forward as a candidate that cannot be selected.
    """
    dynamodb = clients.dynamodb
    found: list[Candidate] = []
    for page in pages(
        dynamodb.list_tables, "ExclusiveStartTableName", "LastEvaluatedTableName"
    ):
        for name in page.get("TableNames", []):
            try:
                described = dynamodb.describe_table(TableName=name)["Table"]
            except ClientError as error:
                if error.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                continue
            arn = str(described.get("TableArn", ""))
            found.append(
                Candidate(
                    kind="dynamodb",
                    identifier=str(name),
                    name=str(name),
                    arn=arn,
                    tags=tags_or_empty(
                        lambda a=arn: tag_map(
                            dynamodb.list_tags_of_resource(ResourceArn=a).get(
                                "Tags", []
                            )
                        ),
                        f"table {name}",
                        record=clients.tag_reads,
                    ),
                    note=str(described.get("TableStatus", "")),
                )
            )
    return found


def collect_secrets(clients: Clients) -> list[Candidate]:
    """Find every Secrets Manager secret in the account.

    ``list_secrets`` carries the tags inline, so no second call per secret.
    """
    secrets = clients.secrets
    found: list[Candidate] = []
    for page in pages(secrets.list_secrets, "NextToken", "NextToken"):
        for item in page.get("SecretList", []):
            found.append(
                Candidate(
                    kind="secret",
                    identifier=str(item.get("ARN") or item.get("Name", "")),
                    name=str(item.get("Name", "")),
                    arn=str(item.get("ARN", "")),
                    tags=tag_map(item.get("Tags", [])),
                )
            )
    return found


def log_group_resource_arn(arn: str) -> str:
    """Return the ARN form the Logs tagging API accepts.

    ``describe_log_groups`` reports a log group ARN with a trailing ``:*``, the
    form that matches log streams in a policy. ``ListTagsForResource`` wants the
    group itself and rejects the wildcard form, so the suffix comes off.
    """
    return arn[:-2] if arn.endswith(":*") else arn


def collect_log_groups(clients: Clients) -> list[Candidate]:
    """Find every AgentCore runtime log group in the account.

    Listed by the service's own path prefix rather than by workshop name,
    because the workshop name is in the middle of the group name. Selection then
    runs over the whole group name against ``LOG_GROUP_PREFIXES``, so a group
    belonging to a runtime this sweep does not own is left alone even though it
    was listed.
    """
    logs = clients.logs
    found: list[Candidate] = []
    for page in pages(
        logs.describe_log_groups,
        "nextToken",
        "nextToken",
        logGroupNamePrefix=AGENTCORE_RUNTIME_LOG_PREFIX,
    ):
        for item in page.get("logGroups", []):
            name = str(item.get("logGroupName", ""))
            arn = log_group_resource_arn(
                str(item.get("arn") or item.get("logGroupArn") or "")
            )
            found.append(
                Candidate(
                    kind="log-group",
                    identifier=name,
                    name=name,
                    arn=arn,
                    tags=tags_or_empty(
                        lambda a=arn: dict(
                            logs.list_tags_for_resource(resourceArn=a).get("tags", {})
                            or {}
                        ),
                        f"log group {name}",
                        record=clients.tag_reads,
                    ),
                )
            )
    return found


def codebuild_project_names(clients: Clients) -> list[str]:
    """Return every CodeBuild project name in the account."""
    return [
        str(name)
        for page in pages(clients.codebuild.list_projects, "nextToken", "nextToken")
        for name in page.get("projects", [])
    ]


def codebuild_details(clients: Clients, names: list[str]) -> dict[str, dict[str, Any]]:
    """Read each project's ARN and tags, treating a refusal as neither.

    ``codebuild:BatchGetProjects`` is not among the CodeBuild actions
    ``lab.template`` grants, so on a student session this read is expected to be
    refused. It is attempted anyway, because the tag half of selection is the
    half that finds a project the real workshop named something else, and a
    refusal costs only the tags: the prefix half needs nothing but
    ``ListProjects``.
    """
    codebuild = clients.codebuild
    details: dict[str, dict[str, Any]] = {}
    for start in range(0, len(names), CODEBUILD_BATCH):
        chunk = names[start : start + CODEBUILD_BATCH]
        try:
            response = codebuild.batch_get_projects(names=chunk)
        except (ClientError, BotoCoreError) as error:
            print(
                "  could not read codebuild project details: "
                f"{redact_text(str(error))[:160]}"
            )
            continue
        for project in response.get("projects", []):
            details[str(project.get("name", ""))] = project
    return details


def collect_projects(clients: Clients) -> list[Candidate]:
    """Find every CodeBuild project in the account.

    ``list_projects`` returns names only, so the ARN and the tags come from a
    second call that may well be denied. A project with neither is still
    selectable by name, which is why the two reads are not one.
    """
    names = codebuild_project_names(clients)
    details = codebuild_details(clients, names) if names else {}
    return [
        Candidate(
            kind="codebuild",
            identifier=name,
            name=name,
            arn=str(details.get(name, {}).get("arn", "")),
            tags=tag_map(details.get(name, {}).get("tags", [])),
        )
        for name in names
    ]


def collect_source_buckets(clients: Clients) -> list[Candidate]:
    """Find the AgentCore build-source bucket, if the account has one.

    The one resource here that cannot be discovered by listing.
    ``s3:ListAllMyBuckets`` is not among the S3 actions ``lab.template`` grants,
    so existence is a ``HeadBucket`` against the one name the service could have
    used. An account with no such bucket answers 404, which is not a finding.

    Every S3 call on this bucket passes ``ExpectedBucketOwner``, on the same
    reasoning as the notebook's teardown, which has always passed it. The name is
    computed from an account id, S3 bucket names are global, and the delete path
    below empties a bucket and then removes it. If the guarded account and the
    account in the name ever disagree, this has to fail rather than empty a
    bucket belonging to someone else. The guards make that disagreement unlikely;
    they do not make it impossible, and the cost of the header is nothing.
    """
    if not clients.account:
        print(
            "  no account id given, so the AgentCore build-source bucket cannot "
            "be named and was not looked for"
        )
        return []
    s3 = clients.s3
    name = source_bucket_name(clients.account, clients.region)
    try:
        s3.head_bucket(Bucket=name, ExpectedBucketOwner=clients.account)
    except ClientError as error:
        if str(error.response["Error"]["Code"]) in BUCKET_GONE_CODES:
            return []
        raise
    return [
        Candidate(
            kind="s3-bucket",
            identifier=name,
            name=name,
            arn=f"arn:aws:s3:::{name}",
            tags=tags_or_empty(
                lambda: tag_map(
                    s3.get_bucket_tagging(
                        Bucket=name, ExpectedBucketOwner=clients.account
                    ).get("TagSet", [])
                ),
                f"bucket {name}",
                quiet_codes={"NoSuchTagSet"},
                record=clients.tag_reads,
            ),
        )
    ]


def collect_roles(clients: Clients) -> list[Candidate]:
    """Find the IAM roles the notebook creates, and only those.

    The prefix filter is applied here as well as in selection. Listing every
    role in a Vocareum account returns Vocareum's own and AWS's service-linked
    roles, and reading tags off all of them is a lot of calls to reach a
    decision the prefix already made.
    """
    iam = clients.iam
    found: list[Candidate] = []
    for page in pages(iam.list_roles, "Marker", "Marker"):
        for item in page.get("Roles", []):
            name = str(item.get("RoleName", ""))
            if not name.startswith(IAM_ROLE_PREFIXES):
                continue
            found.append(
                Candidate(
                    kind="iam-role",
                    identifier=name,
                    name=name,
                    arn=str(item.get("Arn", "")),
                    tags=tags_or_empty(
                        lambda n=name: tag_map(
                            iam.list_role_tags(RoleName=n).get("Tags", [])
                        ),
                        f"role {name}",
                        record=clients.tag_reads,
                    ),
                )
            )
    return found


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------


def runtime_endpoint_names(clients: Clients, runtime_id: str) -> list[str]:
    """Return the deletable endpoint names on a runtime."""
    control = clients.control
    names: list[str] = []
    for page in pages(
        control.list_agent_runtime_endpoints,
        "nextToken",
        "nextToken",
        agentRuntimeId=runtime_id,
    ):
        names.extend(
            str(item.get("name", "")) for item in page.get("runtimeEndpoints", [])
        )
    return [name for name in names if name and name != IMPLICIT_ENDPOINT]


def delete_runtime(clients: Clients, candidate: Candidate) -> None:
    """Delete a runtime's endpoints, wait them out, then delete the runtime."""
    control = clients.control
    runtime_id = candidate.identifier
    for endpoint in runtime_endpoint_names(clients, runtime_id):
        control.delete_agent_runtime_endpoint(
            agentRuntimeId=runtime_id, endpointName=endpoint
        )
        # DeleteAgentRuntimeEndpoint returns before the endpoint is gone and the
        # runtime refuses to go while one exists, so the wait is the whole point
        # of doing this in two steps rather than one.
        wait_until(
            lambda name=endpoint: (
                "present"
                if name in runtime_endpoint_names(clients, runtime_id)
                else "gone"
            ),
            done={"gone"},
            failed=set(),
            label=f"runtime endpoint {endpoint}",
            timeout=180,
        )
    retry_while(
        lambda: control.delete_agent_runtime(agentRuntimeId=runtime_id),
        codes={"ConflictException"},
        label=f"delete runtime {runtime_id}",
    )


def delete_gateway(clients: Clients, candidate: Candidate) -> None:
    """Settle the gateway, drain its targets, then delete it.

    The settle wait comes first and is not optional. ``DeleteGateway`` against a
    gateway still in ``CREATING`` or ``UPDATING`` answers ``ValidationException``
    and changes nothing, which is how the August 4 gateway survived four
    sessions.
    """
    control = clients.control
    gateway_id = candidate.identifier
    wait_until(
        lambda: control.get_gateway(gatewayIdentifier=gateway_id)["status"],
        done={"READY", "ACTIVE"},
        failed={"FAILED", "DELETING"},
        label=f"gateway {gateway_id}",
        timeout=180,
    )
    for page in pages(
        control.list_gateway_targets,
        "nextToken",
        "nextToken",
        gatewayIdentifier=gateway_id,
    ):
        for item in page.get("items", []):
            target_id = str(item.get("targetId", ""))
            control.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
            wait_until_gone(
                lambda t=target_id: control.get_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=t
                ),
                f"gateway target {target_id}",
                timeout=180,
            )
    control.delete_gateway(gatewayIdentifier=gateway_id)


def delete_memory(clients: Clients, candidate: Candidate) -> None:
    """Delete an AgentCore memory."""
    clients.control.delete_memory(memoryId=candidate.identifier)


def delete_function(clients: Clients, candidate: Candidate) -> None:
    """Delete a Lambda function."""
    clients.awslambda.delete_function(FunctionName=candidate.identifier)


def delete_repository(clients: Clients, candidate: Candidate) -> None:
    """Delete an ECR repository, images and all.

    ``force`` is required. Without it a repository holding the agent image is
    refused, and the workshop pushes an image into every repository it makes.
    """
    clients.ecr.delete_repository(repositoryName=candidate.identifier, force=True)


def delete_table(clients: Clients, candidate: Candidate) -> None:
    """Wait for a table to be ACTIVE, then delete it.

    DynamoDB refuses a delete against a table that is ``CREATING`` or
    ``UPDATING`` with ``ResourceInUseException``. A table already ``DELETING``
    needs no second delete, and issuing one would raise for no reason, so the
    confirm step is left to notice it went.
    """
    dynamodb = clients.dynamodb
    state = wait_until(
        lambda: dynamodb.describe_table(TableName=candidate.identifier)["Table"][
            "TableStatus"
        ],
        done={"ACTIVE"},
        failed={"DELETING"},
        label=f"table {candidate.identifier}",
        timeout=180,
    )
    if state == "DELETING":
        return
    dynamodb.delete_table(TableName=candidate.identifier)


def delete_secret(clients: Clients, candidate: Candidate) -> None:
    """Delete a secret outright.

    ``ForceDeleteWithoutRecovery`` is the point. A default delete schedules the
    secret for removal in seven days and holds the name the whole time, so the
    next session's ``CreateSecret`` fails on a name that is neither present nor
    gone, and no amount of sweeping frees it.
    """
    clients.secrets.delete_secret(
        SecretId=candidate.identifier, ForceDeleteWithoutRecovery=True
    )


def delete_log_group(clients: Clients, candidate: Candidate) -> None:
    """Delete a CloudWatch Logs log group."""
    clients.logs.delete_log_group(logGroupName=candidate.identifier)


def delete_project(clients: Clients, candidate: Candidate) -> None:
    """Delete a CodeBuild project."""
    clients.codebuild.delete_project(name=candidate.identifier)


def delete_source_bucket(clients: Clients, candidate: Candidate) -> None:
    """Empty the build-source bucket, then delete it.

    ``DeleteBucket`` refuses a bucket holding anything, and the AgentCore build
    path leaves one source archive per deploy in it. One ``DeleteObjects`` per
    page is the right batching by construction: ``list_objects_v2`` returns at
    most 1000 keys per page and ``DeleteObjects`` accepts at most 1000.

    Only current keys are removed. If this bucket ever arrives versioned,
    ``DeleteBucket`` will refuse and the confirm step reports it as a survivor,
    which is the safe direction: the sweep says so rather than claiming a clean
    account.
    """
    s3 = clients.s3
    bucket = candidate.identifier
    for page in pages(
        s3.list_objects_v2,
        "ContinuationToken",
        "NextContinuationToken",
        Bucket=bucket,
        ExpectedBucketOwner=clients.account,
    ):
        keys = [
            {"Key": str(item["Key"])}
            for item in page.get("Contents", [])
            if item.get("Key")
        ]
        if keys:
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": keys, "Quiet": True},
                ExpectedBucketOwner=clients.account,
            )
    s3.delete_bucket(Bucket=bucket, ExpectedBucketOwner=clients.account)


def delete_role(clients: Clients, candidate: Candidate) -> None:
    """Empty a role of policies, then delete it.

    What is attached is discovered rather than assumed, the same lesson
    ``run_phase3_agentcore.remove_role`` records: a role whose
    ``iam:PutRolePolicy`` was denied has no inline policy, and deleting one that
    was never created raises ``NoSuchEntity`` and aborts before the role.
    """
    iam = clients.iam
    name = candidate.identifier
    for page in pages(iam.list_role_policies, "Marker", "Marker", RoleName=name):
        for policy in page.get("PolicyNames", []):
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
    for page in pages(
        iam.list_attached_role_policies, "Marker", "Marker", RoleName=name
    ):
        for attached in page.get("AttachedPolicies", []):
            iam.detach_role_policy(RoleName=name, PolicyArn=attached["PolicyArn"])
    iam.delete_role(RoleName=name)


# ---------------------------------------------------------------------------
# Describes, for confirming a delete finished
# ---------------------------------------------------------------------------


def describe_runtime(clients: Clients, candidate: Candidate) -> Any:
    """Describe a runtime."""
    return clients.control.get_agent_runtime(agentRuntimeId=candidate.identifier)


def describe_gateway(clients: Clients, candidate: Candidate) -> Any:
    """Describe a gateway."""
    return clients.control.get_gateway(gatewayIdentifier=candidate.identifier)


def describe_memory(clients: Clients, candidate: Candidate) -> Any:
    """Describe a memory."""
    return clients.control.get_memory(memoryId=candidate.identifier)


def describe_function(clients: Clients, candidate: Candidate) -> Any:
    """Describe a Lambda function."""
    return clients.awslambda.get_function(FunctionName=candidate.identifier)


def describe_repository(clients: Clients, candidate: Candidate) -> Any:
    """Describe an ECR repository."""
    return clients.ecr.describe_repositories(repositoryNames=[candidate.identifier])


def describe_table(clients: Clients, candidate: Candidate) -> Any:
    """Describe a DynamoDB table."""
    return clients.dynamodb.describe_table(TableName=candidate.identifier)


def describe_secret(clients: Clients, candidate: Candidate) -> Any:
    """Describe a secret."""
    return clients.secrets.describe_secret(SecretId=candidate.identifier)


def describe_log_group(clients: Clients, candidate: Candidate) -> Any:
    """Confirm a log group by listing it, because Logs has no per-group describe.

    ``describe_log_groups`` answers a name that is gone with an empty list and no
    error, so a caller waiting for a raise waits out its whole timeout and then
    calls a deleted group present. The missing name is turned into the error the
    engine already knows how to read.
    """
    name = candidate.identifier
    for page in pages(
        clients.logs.describe_log_groups,
        "nextToken",
        "nextToken",
        logGroupNamePrefix=name,
    ):
        for item in page.get("logGroups", []):
            if str(item.get("logGroupName", "")) == name:
                return item
    raise ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": name}},
        "DescribeLogGroups",
    )


def describe_project(clients: Clients, candidate: Candidate) -> Any:
    """Confirm a CodeBuild project by listing, not by BatchGetProjects.

    Two reasons, and either alone is enough. ``BatchGetProjects`` answers a
    missing project with an empty ``projects`` list and no error, so a caller
    waiting for a raise waits out its whole timeout and then calls a deleted
    project present. And it is not one of the CodeBuild actions
    ``lab.template`` grants, so on a student session it answers ``AccessDenied``,
    which ``wait_until_gone`` reads as "cannot tell" and reports as a survivor.
    ``ListProjects`` is granted and the absence of a name from it is
    unambiguous.
    """
    name = candidate.identifier
    if name in codebuild_project_names(clients):
        return {"name": name}
    raise ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": name}},
        "ListProjects",
    )


def describe_bucket(clients: Clients, candidate: Candidate) -> Any:
    """Describe a bucket by heading it, which authorizes as s3:ListBucket."""
    return clients.s3.head_bucket(
        Bucket=candidate.identifier, ExpectedBucketOwner=clients.account
    )


def describe_role(clients: Clients, candidate: Candidate) -> Any:
    """Describe an IAM role."""
    return clients.iam.get_role(RoleName=candidate.identifier)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Handler:
    """Everything the engine needs to sweep one kind of resource."""

    kind: str
    collect: Callable[[Clients], list[Candidate]]
    delete: Callable[[Clients, Candidate], None]
    describe: Callable[[Clients, Candidate], Any]
    gone_codes: frozenset[str]
    prefixes: tuple[str, ...] = NAME_PREFIXES
    require_prefix: bool = False
    gone_timeout: int = GONE_TIMEOUT


# Deletion order, and the order the report reads in. Roles are last because the
# gateway, runtime, and Lambda above all reference them.
HANDLERS: tuple[Handler, ...] = (
    Handler(
        kind="runtime",
        collect=collect_runtimes,
        delete=delete_runtime,
        describe=describe_runtime,
        gone_codes=frozenset({"ResourceNotFoundException"}),
        gone_timeout=RUNTIME_GONE_TIMEOUT,
    ),
    # After the runtime, not before it. The service owns these groups for as long
    # as the endpoint exists, and it does not take them when the runtime goes.
    Handler(
        kind="log-group",
        collect=collect_log_groups,
        delete=delete_log_group,
        describe=describe_log_group,
        gone_codes=frozenset({"ResourceNotFoundException"}),
        prefixes=LOG_GROUP_PREFIXES,
    ),
    Handler(
        kind="gateway",
        collect=collect_gateways,
        delete=delete_gateway,
        describe=describe_gateway,
        gone_codes=frozenset({"ResourceNotFoundException"}),
    ),
    Handler(
        kind="memory",
        collect=collect_memories,
        delete=delete_memory,
        describe=describe_memory,
        gone_codes=frozenset({"ResourceNotFoundException"}),
    ),
    Handler(
        kind="lambda",
        collect=collect_functions,
        delete=delete_function,
        describe=describe_function,
        gone_codes=frozenset({"ResourceNotFoundException"}),
    ),
    Handler(
        kind="codebuild",
        collect=collect_projects,
        delete=delete_project,
        describe=describe_project,
        gone_codes=frozenset({"ResourceNotFoundException"}),
    ),
    Handler(
        kind="ecr",
        collect=collect_repositories,
        delete=delete_repository,
        describe=describe_repository,
        gone_codes=frozenset({"RepositoryNotFoundException"}),
    ),
    # After CodeBuild, because the project reads its source archive out of this
    # bucket and a build in flight against a deleted bucket is a build that
    # cannot fail cleanly.
    Handler(
        kind="s3-bucket",
        collect=collect_source_buckets,
        delete=delete_source_bucket,
        describe=describe_bucket,
        gone_codes=BUCKET_GONE_CODES,
        prefixes=S3_BUCKET_PREFIXES,
    ),
    Handler(
        kind="dynamodb",
        collect=collect_tables,
        delete=delete_table,
        describe=describe_table,
        gone_codes=frozenset({"ResourceNotFoundException"}),
    ),
    Handler(
        kind="secret",
        collect=collect_secrets,
        delete=delete_secret,
        describe=describe_secret,
        gone_codes=frozenset({"ResourceNotFoundException"}),
    ),
    Handler(
        kind="iam-role",
        collect=collect_roles,
        delete=delete_role,
        describe=describe_role,
        gone_codes=frozenset({"NoSuchEntity"}),
        prefixes=IAM_ROLE_PREFIXES,
        require_prefix=True,
    ),
)

KINDS: tuple[str, ...] = tuple(handler.kind for handler in HANDLERS)


@dataclass
class Outcome:
    """What happened to one selected resource."""

    candidate: Candidate
    state: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready mapping."""
        return {**self.candidate.as_dict(), "state": self.state, "detail": self.detail}


@dataclass
class SweepReport:
    """Everything one sweep found, skipped, deleted, and failed to delete."""

    account: str = ""
    region: str = ""
    dry_run: bool = True
    stack_owned: StackOwned = field(default_factory=StackOwned)
    targets: list[Candidate] = field(default_factory=list)
    excluded: list[Candidate] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    # Refused tag reads, deliberately separate from ``unreadable``. An entry in
    # that list makes the exit code non-zero, and a refused tag read is the
    # ordinary state of an account that has not been granted the tagging reads:
    # putting it there would exit non-zero on every clean sweep. What it does
    # instead is print, so an instructor can see the tag half was blind before
    # believing an empty plan. Revisit the exit code once a live sweep has shown
    # what this number actually is.
    tag_reads: TagReads = field(default_factory=TagReads)

    @property
    def survivors(self) -> list[Outcome]:
        """Return every resource that was not confirmed gone."""
        return [
            item for item in self.outcomes if item.state not in {"deleted", "absent"}
        ]

    def exit_code(self) -> int:
        """Return 0 only when the account is known to be clean.

        A dry run attempts nothing, so nothing can survive one, and it exits 0
        even when it listed work to do. What it will not do is exit 0 while
        something could not be inspected: an unreadable service is an unknown
        account, and reporting an unknown account as clean is the failure mode
        this whole tool was written against.
        """
        if self.unreadable or self.stack_owned.unreadable:
            return EXIT_SURVIVED
        if self.survivors:
            return EXIT_SURVIVED
        return EXIT_OK

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready mapping of the whole sweep."""
        return {
            "account": self.account,
            "region": self.region,
            "dryRun": self.dry_run,
            "stacks": self.stack_owned.stacks,
            "stackResourceIds": sorted(self.stack_owned.identifiers),
            "targets": [item.as_dict() for item in self.targets],
            "excluded": [item.as_dict() for item in self.excluded],
            "unreadable": [
                {"kind": kind, "detail": detail} for kind, detail in self.unreadable
            ],
            "tagReads": {
                "reads": self.tag_reads.reads,
                "denials": self.tag_reads.denials,
                "denied": list(self.tag_reads.labels),
            },
            "outcomes": [item.as_dict() for item in self.outcomes],
        }


@dataclass
class Sweeper:
    """The engine: plan from the account's contents, then act on the plan."""

    clients: Clients
    kinds: tuple[str, ...] = KINDS
    tag_key: str = WORKSHOP_TAG_KEY
    tag_value: str = WORKSHOP_TAG_VALUE
    cleanup: CleanupRules = field(default_factory=CleanupRules)

    def handlers(self) -> tuple[Handler, ...]:
        """Return the selected handlers, in deletion order."""
        selected = []
        for handler in HANDLERS:
            if handler.kind not in self.kinds:
                continue
            extra = self.cleanup.resource_prefixes
            if handler.kind == "iam-role":
                extra = self.cleanup.iam_role_prefixes
            elif handler.kind == "s3-bucket":
                extra = self.cleanup.s3_bucket_prefixes
            elif handler.kind == "log-group":
                extra = log_group_prefixes(self.cleanup.resource_prefixes)
            prefixes = tuple(dict.fromkeys((*handler.prefixes, *extra)))
            selected.append(replace(handler, prefixes=prefixes))
        return tuple(selected)

    def plan(
        self,
        account: str = "",
        region: str = "",
        stack_name: str | None = None,
    ) -> SweepReport:
        """List the account, exclude what CloudFormation owns, and select the rest."""
        report = SweepReport(account=account, region=region)
        report.stack_owned = collect_stack_owned(self.clients, stack_name)

        for handler in self.handlers():
            try:
                candidates = handler.collect(self.clients)
            except (ClientError, BotoCoreError) as error:
                report.unreadable.append((handler.kind, redact_text(str(error))[:300]))
                continue
            for candidate in candidates:
                reason = selection_reason(
                    candidate,
                    handler.prefixes,
                    self.tag_key,
                    self.tag_value,
                    handler.require_prefix,
                )
                if not reason:
                    continue
                selected = Candidate(
                    kind=candidate.kind,
                    identifier=candidate.identifier,
                    name=candidate.name,
                    arn=candidate.arn,
                    tags=candidate.tags,
                    selected_by=reason,
                    note=candidate.note,
                )
                if report.stack_owned.owns(selected):
                    report.excluded.append(selected)
                else:
                    report.targets.append(selected)
        report.tag_reads = self.clients.tag_reads
        return report

    def execute(self, report: SweepReport) -> SweepReport:
        """Delete every target in order, then confirm each one actually went."""
        report.dry_run = False
        by_kind: dict[str, list[Candidate]] = {}
        for target in report.targets:
            by_kind.setdefault(target.kind, []).append(target)

        for handler in self.handlers():
            for target in by_kind.get(handler.kind, []):
                report.outcomes.append(self.remove(handler, target))
        return report

    def remove(self, handler: Handler, target: Candidate) -> Outcome:
        """Delete one resource and report whether it is really gone.

        A delete that raises is recorded and the sweep carries on, because one
        refusal must not hide the rest of the account. A delete that returns is
        still followed by a describe, because the two are not the same claim.
        """
        try:
            handler.delete(self.clients, target)
        except ClientError as error:
            code = str(error.response["Error"]["Code"])
            message = redact_text(str(error.response["Error"].get("Message", "")))
            if code in handler.gone_codes:
                print(f"  absent   {target.label}")
                return Outcome(target, "absent", code)
            print(f"  FAILED   {target.label}: {code}")
            return Outcome(target, "failed", f"{code}: {message}"[:300])
        except BotoCoreError as error:
            print(f"  FAILED   {target.label}: {redact_text(str(error))[:160]}")
            return Outcome(target, "failed", redact_text(str(error))[:300])

        if wait_until_gone(
            lambda: handler.describe(self.clients, target),
            target.label,
            timeout=handler.gone_timeout,
            interval=10,
            gone_codes=handler.gone_codes,
        ):
            print(f"  deleted  {target.label}")
            return Outcome(target, "deleted")
        print(f"  PRESENT  {target.label} after the delete returned")
        return Outcome(
            target, "present", "delete returned but the resource is still there"
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def render_plan(report: SweepReport) -> None:
    """Print what the sweep found, what it excluded, and why."""
    section("PLAN")
    if report.stack_owned.stacks:
        print("Live CloudFormation stacks read for ownership:")
        for name in report.stack_owned.stacks:
            print(f"  {name}")
        print(
            f"  {len(report.stack_owned.identifiers)} physical resource ids collected."
        )
    else:
        print("No live CloudFormation stack. Nothing is excluded as stack-owned.")
    for detail in report.stack_owned.unreadable:
        print(f"  COULD NOT READ {detail}")

    if report.excluded:
        print(
            f"\nExcluded {len(report.excluded)} resource(s) because CloudFormation "
            "owns them. Vocareum deletes the lab stack at session end and deleting "
            "one of its resources first risks DELETE_FAILED:"
        )
        for candidate in report.excluded:
            print(f"  skip     {candidate.label}  [{candidate.selected_by}]")
    else:
        print("\nNothing in scope is stack-owned, so nothing was excluded.")

    if report.unreadable:
        print("\nCould not inspect, so this account is not known to be clean:")
        for kind, detail in report.unreadable:
            print(f"  {kind}: {detail}")

    note = report.tag_reads.note()
    if note:
        print(f"\n{note}.")
        print("  An empty plan below is only as good as the half that could still see.")

    if not report.targets:
        print("\nNothing to delete. Every workshop resource is already gone.")
        return
    print(f"\n{len(report.targets)} resource(s) in scope, in deletion order:")
    for candidate in report.targets:
        state = f"  ({candidate.note})" if candidate.note else ""
        print(f"  delete   {candidate.label}  [{candidate.selected_by}]{state}")


def render_result(report: SweepReport) -> None:
    """Print what was deleted and, more importantly, what was not."""
    section("RESULT")
    counts: dict[str, int] = {}
    for outcome in report.outcomes:
        counts[outcome.state] = counts.get(outcome.state, 0) + 1
    print(
        "deleted {deleted}, already absent {absent}, failed {failed}, "
        "still present {present}".format(
            deleted=counts.get("deleted", 0),
            absent=counts.get("absent", 0),
            failed=counts.get("failed", 0),
            present=counts.get("present", 0),
        )
    )
    survivors = report.survivors
    if not survivors:
        print("\nEvery selected resource is confirmed gone.")
        return
    print("\nLEFT BEHIND. Re-run the sweep, then delete these by hand:")
    for outcome in survivors:
        print(f"  {outcome.candidate.label}: {outcome.detail}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Delete everything the workshop created in a Vocareum student AWS "
            "account. Prints a plan and changes nothing unless --delete is passed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete. Without it this is a dry run and the default.",
    )
    parser.add_argument(
        "--account",
        help=(
            "AWS account the credentials must belong to. Defaults to "
            "LAB_AWS_ACCOUNT_ID, which goes stale every time the Vocareum "
            "account pool rotates, so state it here when in doubt."
        ),
    )
    parser.add_argument(
        "--source",
        choices=("vocareum", "ambient"),
        default="vocareum",
        help="Where credentials come from. Default vocareum.",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Do not start or extend the lab session before fetching credentials.",
    )
    parser.add_argument(
        "--kind",
        action="append",
        choices=KINDS,
        help=(
            "Limit the sweep to one kind of resource. Repeatable. The choices "
            "are listed in deletion order, and that order matters: log-group "
            "belongs after runtime, and s3-bucket after codebuild."
        ),
    )
    parser.add_argument(
        "--stack",
        help=(
            "Name the lab stack explicitly instead of reading every live stack. "
            "Its resources are excluded from the sweep."
        ),
    )
    parser.add_argument("--region", help="AWS region. Defaults to AWS_REGION.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the .env file. Defaults to the repository root.",
    )
    add_course_argument(parser)
    parser.add_argument(
        "--json",
        type=Path,
        help="Write the whole report to this JSON file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Guard the identity, plan the sweep, and only then delete anything.

    Exit codes:
        0  The account is known to be clean, or the dry run read all of it.
        1  Configuration error.
        2  A guard refused to run, so nothing was deleted.
        3  Something survived, or a service could not be inspected.
    """
    args = parse_args(argv)

    try:
        environment = Environment.from_file(args.env_file.expanduser(), args.course)
        config = load_config(
            args.env_file.expanduser(),
            args.region,
            args.account,
            need_vocareum=args.source == "vocareum",
            course=args.course,
        )
        cleanup = (
            load_definition(args.env_file.expanduser(), environment.course).cleanup
            if environment.course
            else CleanupRules()
        )
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        boto_session = open_guarded_session(
            config,
            source=args.source,
            no_start=args.no_start,
            account_named=bool(args.account),
            ambient_refusal=(
                "--source ambient requires --account. Ambient credentials "
                "are what the credential trap falls back to, and this tool "
                "deletes things, so the account must be named on purpose."
            ),
            region_refusal=(
                "Region guard refused the run. Every resource this sweeps is "
                f"in {REQUIRED_REGION} only, so a sweep of anywhere else would "
                "report an empty account while the real one stays full."
            ),
        )
    except (ApiError, GuardError) as error:
        print(f"\nREFUSED TO RUN\n{error}", file=sys.stderr)
        return EXIT_GUARD

    sweeper = Sweeper(
        clients=Clients(
            session=boto_session,
            region=config.region,
            # The build-source bucket's name is the account id and the region.
            # This is the guarded account, which is the only one the credentials
            # can reach by the time this line runs.
            account=config.expected_account,
        ),
        kinds=tuple(args.kind) if args.kind else KINDS,
        cleanup=cleanup,
    )
    report = sweeper.plan(
        account=config.expected_account,
        region=config.region,
        stack_name=args.stack,
    )
    render_plan(report)

    if not args.delete:
        section("DRY RUN")
        print(
            "Nothing was deleted. This is the default.\n"
            "Re-run with --delete to remove the resources listed above."
        )
    elif report.targets:
        section("DELETING")
        sweeper.execute(report)
        render_result(report)
    else:
        report.dry_run = False

    if args.json:
        args.json.write_text(
            json.dumps(report.as_dict(), indent=2, default=json_default) + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")

    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
