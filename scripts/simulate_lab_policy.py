#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.34", "botocore>=1.34", "pyyaml>=6"]
# ///
"""Ask IAM whether lab.template's grants cover the calls the lab makes.

Testing a change to ``lab.template`` used to cost a lab launch: upload the file,
Start Lab, run the verification notebook, read the PASS and FAIL lines. Launches
come from a finite budget and each one rotates the Vocareum account pool, so the
loop is slow and the evidence expires. This script replaces
the permission half of that loop with ``iam:SimulateCustomPolicy``, which
evaluates the same policy language AWS evaluates, needs no lab session, needs no
student account, creates nothing, and costs nothing.

The bug class it exists to catch is a resource-ARN mismatch, where an action is
granted against a resource pattern the real call never authorizes against. The
static audit in ``lab-template-audit.md`` found two: ``s3:PutObject`` granted on
``arn:aws:s3:::bedrock-agentcore-*`` while the call authorizes against
``arn:aws:s3:::bedrock-agentcore-codebuild-sources-<account>-us-east-1/verify/source.zip``,
and IAM role names outside ``role/workshop-*``. Reading a template cannot settle
that reliably, because a wildcard match is a rule rather than an impression. The
simulator applies the rule.

What it proves is narrow, and the output says so on every run. See
``print_caveat`` and ``agentcore-status.md`` "Not working" item 3. In short: the
simulation sees the template's grants and nothing else, so ``allowed`` means
"the template grants this" and ``implicitDeny`` means "the template does not
grant this". Neither is a statement about the live ``voclabs`` role, which also
carries Vocareum's unreadable inline policy ``Pvoclabs`` and sits under
organization SCPs.

The parser fails loudly rather than skipping a statement it could not resolve.
A dropped statement reads exactly like a missing grant, which would turn this
tool into a generator of false gaps.

    ./scripts/simulate_lab_policy.py
    ./scripts/simulate_lab_policy.py --account-id 000000000000
    ./scripts/simulate_lab_policy.py --template lab.template --json out.json

Exit codes:
    0  Every case's simulated decision matched its expected decision.
    1  At least one case did not match.
    2  The template could not be parsed, or IAM refused the simulation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
import yaml
from botocore.exceptions import BotoCoreError, ClientError

# The local test course, named once. This script declares its own PEP 723
# dependency set and runs in an isolated environment, so it cannot import
# `vocareum_tools.course.template_path` to resolve the path the way every other
# tool does. The one course it targets is the one that stays in this
# repository by design, so the local shape is correct for it.
DEFAULT_COURSE = "neo4j-fundamentals"
DEFAULT_COURSES_DIR = "courses"

# SimulateCustomPolicy limits. Each policy document is capped at 131072
# characters by the service model. The document count has no published ceiling
# and 100 was accepted in testing on 2026-08-05, so this number is a self-imposed
# guard rather than a measured quota: it exists so that outgrowing it raises
# instead of silently sending a partial set of grants.
MAX_POLICY_DOCUMENTS = 100
MAX_POLICY_CHARS = 131072

# The three decisions SimulateCustomPolicy returns.
ALLOWED = "allowed"
IMPLICIT_DENY = "implicitDeny"
EXPLICIT_DENY = "explicitDeny"

DEFAULT_REGION = "us-east-1"

# The notebook's own constant. Every resource name in the case table is derived
# from it, the same way the notebook derives them.
DEFAULT_PREFIX = "verify"

# Placeholders that stand in for a concrete resource the notebook only learns at
# run time. AgentCore appends ten characters to the name it was given, and
# Secrets Manager appends six. The exact suffix is irrelevant to an IAM wildcard
# match; what matters is that the ARN has the shape the real call will carry.
AGENTCORE_SUFFIX = "0123456789"
SECRET_SUFFIX = "AbCdEf"


class TemplateError(RuntimeError):
    """Raised when lab.template cannot be turned into policy documents."""


@dataclass(frozen=True)
class Intrinsic:
    """An unresolved CloudFormation intrinsic, kept rather than discarded.

    The loader cannot resolve ``!GetAtt`` or ``!Select``, and it should not
    pretend to. Parking the tag here lets the extractor raise naming the exact
    intrinsic it hit, instead of emitting a policy document with a hole in it.
    """

    tag: str
    value: Any


class CloudFormationLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation shorthand tags instead of failing."""


def _construct_intrinsic(
    loader: CloudFormationLoader, tag_suffix: str, node: yaml.Node
) -> Intrinsic:
    """Turn any ``!Tag`` node into an Intrinsic carrying its raw value."""
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return Intrinsic(tag=tag_suffix.lstrip("!"), value=value)


CloudFormationLoader.add_multi_constructor("!", _construct_intrinsic)


_SUB_VARIABLE = re.compile(r"\$\{([^}]*)\}")


def substitution_context(account_id: str, region: str, template: Any) -> dict[str, str]:
    """Return the names ``!Sub`` may use, and the values they resolve to.

    Pseudo parameters first, then every template parameter's default. Vocareum
    passes no parameter values at Start Lab, so a default is the value that will
    really be used, which is the same reasoning the template's own
    ``ResourcePrefix`` comment records.
    """
    context = {
        "AWS::AccountId": account_id,
        "AWS::Region": region,
        "AWS::Partition": "aws",
        "AWS::URLSuffix": "amazonaws.com",
    }
    parameters = template.get("Parameters", {}) if isinstance(template, dict) else {}
    if isinstance(parameters, dict):
        for name, spec in parameters.items():
            if isinstance(spec, dict) and "Default" in spec:
                context.setdefault(name, str(spec["Default"]))
    return context


def resolve_sub(value: Any, context: dict[str, str], where: str) -> str:
    """Resolve one ``!Sub``, in either the scalar form or the list form."""
    local = dict(context)
    if isinstance(value, list):
        if len(value) != 2 or not isinstance(value[0], str):
            raise TemplateError(f"{where}: !Sub list form is not [string, mapping]")
        mapping = value[1]
        if not isinstance(mapping, dict):
            raise TemplateError(f"{where}: !Sub list form is not [string, mapping]")
        for key, item in mapping.items():
            if not isinstance(item, str):
                raise TemplateError(
                    f"{where}: !Sub variable {key!r} is not a plain string"
                )
            local[key] = item
        template_text = value[0]
    elif isinstance(value, str):
        template_text = value
    else:
        raise TemplateError(f"{where}: !Sub takes a string or a two-item list")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.startswith("!"):
            # ${!Literal} is the documented escape for a literal ${Literal},
            # which is how an IAM policy variable survives an Fn::Sub.
            return "${" + name[1:] + "}"
        if name not in local:
            raise TemplateError(f"{where}: !Sub has no value for ${{{name}}}")
        return local[name]

    return _SUB_VARIABLE.sub(replace, template_text)


def resolve(node: Any, context: dict[str, str], where: str) -> Any:
    """Resolve every intrinsic in a subtree, or raise naming the one that stuck.

    Only ``Sub`` and ``Ref`` appear inside a PolicyDocument today. Anything else
    raises rather than resolving to something plausible, because a policy
    document is the input to an authorization answer and a plausible ARN is
    worse than no answer.
    """
    if isinstance(node, Intrinsic):
        if node.tag == "Sub":
            return resolve_sub(node.value, context, where)
        if node.tag == "Ref":
            name = node.value
            if isinstance(name, str) and name in context:
                return context[name]
            raise TemplateError(f"{where}: !Ref {node.value!r} cannot be resolved")
        raise TemplateError(f"{where}: !{node.tag} cannot be resolved")
    if isinstance(node, dict):
        if set(node) == {"Fn::Sub"}:
            return resolve_sub(node["Fn::Sub"], context, where)
        if set(node) == {"Ref"} and node["Ref"] in context:
            return context[node["Ref"]]
        return {key: resolve(item, context, where) for key, item in node.items()}
    if isinstance(node, list):
        return [
            resolve(item, context, f"{where}[{index}]")
            for index, item in enumerate(node)
        ]
    return node


@dataclass(frozen=True)
class Grant:
    """One statement out of one AWS::IAM::RolePolicy, with where it came from."""

    logical_id: str
    policy_name: str
    sid: str
    statement: dict[str, Any]

    @property
    def label(self) -> str:
        """The name printed against a matched statement."""
        return f"{self.logical_id}/{self.sid}"


def load_template(path: Path) -> Any:
    """Parse lab.template, keeping the CloudFormation shorthand tags."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise TemplateError(f"Could not read {path}: {error}") from error
    try:
        document = yaml.load(text, Loader=CloudFormationLoader)
    except yaml.YAMLError as error:
        raise TemplateError(f"Could not parse {path}: {error}") from error
    if not isinstance(document, dict):
        raise TemplateError(f"{path}: top level is not a mapping")
    return document


SIMULATED_ROLE = "voclabs"


def extract_grants(template: Any, account_id: str, region: str) -> list[Grant]:
    """Return every statement of every AWS::IAM::RolePolicy on the student role.

    Every failure here raises. A statement this function drops looks identical
    in the report to a grant the template never had, and a false gap sends
    somebody editing a file that was already correct.

    **A RolePolicy on any other role is skipped, and the skip is announced.**
    This function used to take every RolePolicy in the template, which was right
    while ``voclabs`` was the only role any of them named. The account sweep broke
    that: its execution role is a third principal, and folding its grants in here
    would make the report claim a student may call ``logs:DeleteLogGroup``. The
    whole point of this tool is to answer what a student may do, so an
    over-reported allow is the one answer it must never give. Skipping is
    announced rather than silent because a dropped statement is indistinguishable
    from a missing grant, which is the failure the docstring above warns about.

    The skip happens before ``resolve``, so a policy on a role named by ``!Ref``
    never needs a synthetic stack id. Inventing one would mean printing an ARN
    that looks real and is not.
    """
    resources = template.get("Resources")
    if not isinstance(resources, dict):
        raise TemplateError("Template has no Resources mapping")

    context = substitution_context(account_id, region, template)
    grants: list[Grant] = []
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            raise TemplateError(f"{logical_id}: resource is not a mapping")
        if resource.get("Type") != "AWS::IAM::RolePolicy":
            continue
        properties = resource.get("Properties")
        if not isinstance(properties, dict):
            raise TemplateError(f"{logical_id}: RolePolicy has no Properties mapping")
        role = properties.get("RoleName")
        if role != SIMULATED_ROLE:
            shown = role if isinstance(role, str) else "a name built by an intrinsic"
            print(
                f"skipping {logical_id}: RoleName is {shown}, not {SIMULATED_ROLE}. "
                "A grant on another principal is not a grant to the student."
            )
            continue
        document = properties.get("PolicyDocument")
        if not isinstance(document, dict):
            raise TemplateError(f"{logical_id}: RolePolicy has no PolicyDocument")
        policy_name = properties.get("PolicyName", logical_id)
        if not isinstance(policy_name, str):
            policy_name = logical_id

        statements = document.get("Statement")
        if not isinstance(statements, list) or not statements:
            raise TemplateError(f"{logical_id}: PolicyDocument.Statement is not a list")
        for index, raw in enumerate(statements):
            where = f"{logical_id}.Statement[{index}]"
            if not isinstance(raw, dict):
                raise TemplateError(f"{where}: statement is not a mapping")
            statement = resolve(raw, context, where)
            sid = statement.get("Sid") or f"Statement{index}"
            grants.append(
                Grant(
                    logical_id=logical_id,
                    policy_name=policy_name,
                    sid=str(sid),
                    statement=statement,
                )
            )

    if not grants:
        raise TemplateError("Template contains no AWS::IAM::RolePolicy statements")
    return grants


@dataclass(frozen=True)
class PolicyInput:
    """One member of PolicyInputList: exactly one statement out of the template.

    One statement per document, rather than the whole template in one, and the
    reason is the report. ``MatchedStatements`` names the policy that matched as
    ``PolicyInputList.N`` and locates the statement inside it by line and column,
    never by Sid. With one statement per document the index alone is the answer.
    Packing several statements into a document and recovering the Sid from the
    reported line was tried first and was off by one, which is the sort of quiet
    error that mislabels a report without ever failing.
    """

    document: str
    grant: Grant


def build_policy_inputs(grants: list[Grant]) -> list[PolicyInput]:
    """Wrap each statement in its own policy document, within the API's limits."""
    inputs: list[PolicyInput] = []
    for grant in grants:
        document = json.dumps(
            {"Version": "2012-10-17", "Statement": [grant.statement]},
            separators=(",", ":"),
        )
        if len(document) > MAX_POLICY_CHARS:
            raise TemplateError(
                f"{grant.label}: statement is {len(document)} characters and "
                f"SimulateCustomPolicy accepts {MAX_POLICY_CHARS} per policy."
            )
        inputs.append(PolicyInput(document=document, grant=grant))

    if len(inputs) > MAX_POLICY_DOCUMENTS:
        raise TemplateError(
            f"lab.template has {len(inputs)} statements and this tool sends one "
            f"policy document each, against a ceiling of {MAX_POLICY_DOCUMENTS}. "
            "Raise the ceiling after checking the quota, or split the run. Do "
            "not drop statements: the answer would then cover only part of the "
            "template while still reading like a complete one."
        )
    return inputs


@dataclass(frozen=True)
class Case:
    """One concrete (IAM action, resource ARN) pair the notebook really makes."""

    action: str
    resource: str
    expected: str
    step: str
    note: str = ""


@dataclass
class Result:
    """A case, the decision IAM returned for it, and what matched."""

    case: Case
    actual: str
    matched: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """True when the simulated decision is the one the case expected."""
        return self.actual == self.case.expected

    def as_dict(self) -> dict[str, Any]:
        """The row, flattened for --json."""
        return {
            "step": self.case.step,
            "action": self.case.action,
            "resource": self.case.resource,
            "expected": self.case.expected,
            "actual": self.actual,
            "matched": list(self.matched),
            "ok": self.ok,
            "note": self.case.note,
        }


def build_cases(account_id: str, region: str, prefix: str) -> list[Case]:
    """The calls the verification notebook makes, with real ARNs.

    Derived from the reconciliation table in ``lab-template-audit.md`` and from
    the notebook's own constants. Every action the notebook calls expects
    ``allowed``: this is a list of what the template ought to grant, so a gap the
    audit already found still shows as a mismatch and still fails the gate until
    it is closed. The handful of cases expecting ``implicitDeny`` are deliberate
    negative controls, marked as such in their note.

    Both directions are represented on purpose. A harness that only ever reports
    denials has not been shown to detect an allow.
    """
    agentcore = f"arn:aws:bedrock-agentcore:{region}:{account_id}"
    roles = {
        role: f"arn:aws:iam::{account_id}:role/workshop-{prefix}-{role}-role"
        for role in ("codebuild", "runtime", "lambda", "gateway")
    }
    source_bucket = (
        f"arn:aws:s3:::bedrock-agentcore-codebuild-sources-{account_id}-{region}"
    )
    source_object = f"{source_bucket}/{prefix}/source.zip"
    runtime_arn = f"{agentcore}:runtime/{prefix}_runtime-{AGENTCORE_SUFFIX}"
    memory_arn = f"{agentcore}:memory/{prefix}_memory-{AGENTCORE_SUFFIX}"
    gateway_arn = f"{agentcore}:gateway/{prefix}-gateway-{AGENTCORE_SUFFIX}"
    function_arn = (
        f"arn:aws:lambda:{region}:{account_id}:function:hotel-booking-{prefix}-tool"
    )
    table_arn = (
        f"arn:aws:dynamodb:{region}:{account_id}:table/workshop-{prefix}-bookings"
    )
    secret_arn = (
        f"arn:aws:secretsmanager:{region}:{account_id}:"
        f"secret:{prefix}/neo4j-command-{SECRET_SUFFIX}"
    )
    build_log = (
        f"arn:aws:logs:{region}:{account_id}:log-group:"
        f"/aws/codebuild/bedrock-agentcore-{prefix}-builder:log-stream:*"
    )

    cases: list[Case] = [
        Case("sts:GetCallerIdentity", "*", ALLOWED, "step2"),
        Case(
            "bedrock:InvokeModel",
            "arn:aws:bedrock:"
            f"{region}::foundation-model/amazon.nova-2-multimodal-embeddings-v1:0",
            ALLOWED,
            "step2b",
            "Nova embedding request",
        ),
        Case(
            "bedrock:InvokeModel",
            f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0",
            ALLOWED,
            "step2b",
            "Titan embedding request",
        ),
        Case("bedrock-agentcore:ListAgentRuntimes", "*", ALLOWED, "step5"),
        Case("bedrock-agentcore:ListGateways", "*", ALLOWED, "step5"),
        Case("bedrock-agentcore:ListMemories", "*", ALLOWED, "step5"),
        Case("bedrock-agentcore:ListGatewayTargets", "*", ALLOWED, "step11"),
    ]

    # Step 6. Audit gap 1: the role names drifted outside role/workshop-* and
    # took steps 8, 9, and 11 down with them. The notebook has since gone back
    # to the workshop- prefixed form, so these should all be allowed now, and
    # the last case below is the regression guard for the form that broke.
    for action in (
        "iam:CreateRole",
        "iam:TagRole",
        "iam:PutRolePolicy",
        "iam:ListRolePolicies",
        "iam:DeleteRolePolicy",
        "iam:DeleteRole",
    ):
        cases.append(Case(action, roles["codebuild"], ALLOWED, "step6"))
    for role in ("runtime", "lambda", "gateway"):
        cases.append(Case("iam:CreateRole", roles[role], ALLOWED, "step6"))
        cases.append(Case("iam:PutRolePolicy", roles[role], ALLOWED, "step6"))
    cases.append(
        Case(
            "iam:CreateRole",
            f"arn:aws:iam::{account_id}:role/{prefix}-codebuild-role",
            IMPLICIT_DENY,
            "step6",
            "negative control: the unprefixed name audit gap 1 found",
        )
    )
    cases.append(
        Case(
            "iam:PutRolePolicy",
            f"arn:aws:iam::{account_id}:role/zzztest-role",
            IMPLICIT_DENY,
            "step6",
            "negative control: the probe agentcore-status.md item 3 asks for",
        )
    )

    # Step 7. ECR and the CodeBuild source object. Audit gap 2 is the object
    # ARN, gap 6 is ecr:TagResource on a tagged create.
    cases += [
        Case("ecr:CreateRepository", "*", ALLOWED, "step7"),
        Case("ecr:DeleteRepository", "*", ALLOWED, "step7"),
        Case("ecr:BatchGetImage", "*", ALLOWED, "step8"),
        Case("ecr:DescribeRepositories", "*", ALLOWED, "step13"),
        Case(
            "ecr:TagResource",
            f"arn:aws:ecr:{region}:{account_id}:repository/bedrock-agentcore-{prefix}",
            ALLOWED,
            "step7",
            "audit gap 6: create_repository passes tags",
        ),
        Case("s3:ListBucket", source_bucket, ALLOWED, "step7"),
        Case("s3:CreateBucket", source_bucket, ALLOWED, "step7"),
        Case("s3:PutObject", source_object, ALLOWED, "step7", "audit gap 2"),
        Case("s3:GetObject", source_object, ALLOWED, "step7", "audit gap 2"),
        Case("s3:DeleteObject", source_object, ALLOWED, "step7", "audit gap 2"),
    ]

    # Step 8. CodeBuild, and the build log the notebook tries to read.
    cases += [
        Case("codebuild:CreateProject", "*", ALLOWED, "step8"),
        Case("codebuild:StartBuild", "*", ALLOWED, "step8"),
        Case("codebuild:BatchGetBuilds", "*", ALLOWED, "step8"),
        Case("codebuild:DeleteProject", "*", ALLOWED, "step8"),
        Case("iam:PassRole", roles["codebuild"], ALLOWED, "step8"),
        Case(
            "logs:GetLogEvents",
            build_log,
            ALLOWED,
            "step8",
            "audit gap 5: action absent and /aws/codebuild/* out of scope",
        ),
    ]

    # Step 9. Runtime and endpoint. CreateAgentRuntime is listed against "*"
    # rather than against the runtime ARN, and that is not a shortcut: the
    # action supports no resource type, so IAM authorizes it against "*" and an
    # ARN-scoped statement can never match it. Same shape as the
    # bedrock:ListFoundationModels note in lab.template's own header. The
    # endpoint create below does take a resource, the parent runtime.
    cases.append(Case("iam:PassRole", roles["runtime"], ALLOWED, "step9"))
    cases.append(
        Case(
            "bedrock-agentcore:CreateAgentRuntime",
            "*",
            ALLOWED,
            "step9",
            'no resource-level support, so it needs its own Resource: "*" grant',
        )
    )
    for action in (
        "GetAgentRuntime",
        "CreateAgentRuntimeEndpoint",
        "GetAgentRuntimeEndpoint",
        "InvokeAgentRuntime",
        "DeleteAgentRuntimeEndpoint",
        "DeleteAgentRuntime",
    ):
        cases.append(Case(f"bedrock-agentcore:{action}", runtime_arn, ALLOWED, "step9"))

    # Step 10. Memory, including the tag the cleanup module needs to find it
    # again. Audit gap 4. CreateMemory is against "*" for the same reason
    # CreateAgentRuntime is.
    cases.append(
        Case(
            "bedrock-agentcore:CreateMemory",
            "*",
            ALLOWED,
            "step10",
            'no resource-level support, so it needs its own Resource: "*" grant',
        )
    )
    for action, note in (
        ("GetMemory", ""),
        ("TagResource", "audit gap 4"),
        ("ListTagsForResource", "audit gap 4"),
        ("UpdateMemory", ""),
        ("DeleteMemory", ""),
    ):
        cases.append(
            Case(f"bedrock-agentcore:{action}", memory_arn, ALLOWED, "step10", note)
        )

    # Step 11. Lambda tool and the gateway in front of it.
    cases += [
        Case("iam:PassRole", roles["lambda"], ALLOWED, "step11"),
        Case("lambda:CreateFunction", function_arn, ALLOWED, "step11"),
        Case("lambda:TagResource", function_arn, ALLOWED, "step11"),
        Case("lambda:GetFunction", function_arn, ALLOWED, "step13"),
        Case("lambda:DeleteFunction", function_arn, ALLOWED, "step11"),
        Case("iam:PassRole", roles["gateway"], ALLOWED, "step11"),
    ]
    cases.append(
        Case(
            "bedrock-agentcore:CreateGateway",
            "*",
            ALLOWED,
            "step11",
            'no resource-level support, so it needs its own Resource: "*" grant',
        )
    )
    for action in (
        "GetGateway",
        "CreateGatewayTarget",
        "GetGatewayTarget",
        "InvokeGateway",
        "DeleteGatewayTarget",
        "DeleteGateway",
    ):
        cases.append(
            Case(f"bedrock-agentcore:{action}", gateway_arn, ALLOWED, "step11")
        )

    # Step 12. Secrets Manager, audit gap 3, and DynamoDB, which the audit found
    # clean and which is here to show the harness reports an allow.
    for action, note in (
        ("CreateSecret", "audit gap 3"),
        ("TagResource", "audit gap 3"),
        ("DescribeSecret", "audit gap 3"),
        ("GetSecretValue", ""),
        ("DeleteSecret", "audit gap 3"),
    ):
        cases.append(
            Case(f"secretsmanager:{action}", secret_arn, ALLOWED, "step12", note)
        )
    for action in (
        "CreateTable",
        "TagResource",
        "DescribeTable",
        "PutItem",
        "GetItem",
        "DeleteTable",
    ):
        cases.append(Case(f"dynamodb:{action}", table_arn, ALLOWED, "step12"))
    cases.append(
        Case(
            "dynamodb:CreateTable",
            f"arn:aws:dynamodb:{region}:{account_id}:table/{prefix}-bookings",
            IMPLICIT_DENY,
            "step12",
            "negative control: table/workshop-* is the granted scope",
        )
    )
    return cases


def group_by_resource(cases: list[Case]) -> OrderedDict[str, list[Case]]:
    """Group the cases by resource ARN, preserving the order they were written.

    One SimulateCustomPolicy call carries many actions against one resource, so
    grouping is what turns roughly seventy cases into roughly twenty calls.
    Grouping the other way round is not available: the API takes the cross
    product of ActionNames and ResourceArns, and a cross product would ask
    questions the notebook never asks.
    """
    grouped: OrderedDict[str, list[Case]] = OrderedDict()
    for case in cases:
        grouped.setdefault(case.resource, []).append(case)
    return grouped


def simulate_group(
    iam: Any, documents: list[str], resource: str, actions: list[str]
) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Simulate every action against one resource, following pagination."""
    decisions: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    marker: str | None = None
    while True:
        request: dict[str, Any] = {
            "PolicyInputList": documents,
            "ActionNames": actions,
            "ResourceArns": [resource],
        }
        if marker:
            request["Marker"] = marker
        response = iam.simulate_custom_policy(**request)
        for evaluation in response.get("EvaluationResults", []):
            decisions[evaluation["EvalActionName"]] = (
                evaluation["EvalDecision"],
                evaluation.get("MatchedStatements", []),
            )
        if not response.get("IsTruncated"):
            return decisions
        marker = response.get("Marker")


def matched_labels(
    inputs: list[PolicyInput], statements: list[dict[str, Any]]
) -> tuple[str, ...]:
    """Turn the simulator's SourcePolicyId values back into statement Sids.

    ``PolicyInputList.N`` is 1-based and counts the documents in the order they
    were sent, which is the order ``build_policy_inputs`` produced them in.
    """
    labels: list[str] = []
    for statement in statements:
        source = statement.get("SourcePolicyId", "")
        index = -1
        if source.startswith("PolicyInputList."):
            index = int(source.rsplit(".", 1)[1]) - 1
        if 0 <= index < len(inputs):
            labels.append(inputs[index].grant.label)
        else:
            labels.append(source or "unknown")
    return tuple(dict.fromkeys(labels))


def run_cases(iam: Any, inputs: list[PolicyInput], cases: list[Case]) -> list[Result]:
    """Simulate every case, one call per distinct resource ARN."""
    documents = [item.document for item in inputs]
    results: list[Result] = []
    for resource, group in group_by_resource(cases).items():
        actions = list(dict.fromkeys(case.action for case in group))
        try:
            decisions = simulate_group(iam, documents, resource, actions)
        except ClientError as error:
            # One unrecognized action name fails the whole batch, so fall back
            # to asking one at a time. Losing twenty answers to one bad action
            # would hide the nineteen that were fine.
            decisions = {}
            for action in actions:
                try:
                    decisions.update(simulate_group(iam, documents, resource, [action]))
                except ClientError as single:
                    decisions[action] = (f"error: {error_text(single)}", [])
            if not decisions:
                raise RuntimeError(error_text(error)) from error
        for case in group:
            decision, statements = decisions.get(
                case.action, ("error: no result returned", [])
            )
            results.append(
                Result(
                    case=case,
                    actual=decision,
                    matched=matched_labels(inputs, statements),
                )
            )
    return results


def error_text(error: ClientError) -> str:
    """The message AWS put in the body, which is the only place it names why."""
    return str(error.response.get("Error", {}).get("Message", error))


def print_caveat() -> None:
    """Say what a decision here means, in the output rather than in a docstring.

    This is the whole subject of ``agentcore-status.md`` "Not working" item 3.
    ``AWS::IAM::RolePolicy`` is additive and the simulation only sees what it was
    handed, so neither decision is a statement about the live session role.
    """
    print("What this proves, and what it does not")
    print("  Simulated against lab.template's grants and nothing else.")
    print()
    print("  allowed       lab.template grants this. NOT that a student can do")
    print("                it. The live voclabs role also sits under Vocareum's")
    print("                organization SCPs, including ${VOCAREUM_SCP_POLICY_ID},")
    print("                which no simulation from outside that organization")
    print("                can see, and an SCP deny beats any Allow here.")
    print()
    print("  implicitDeny  lab.template does not grant this. NOT that a student")
    print("                will be refused. voclabs also carries Vocareum's own")
    print("                inline policy Pvoclabs, which the student role cannot")
    print("                read, and which a live session measured as")
    print("                granting iam:CreateRole and iam:DeleteRole outside")
    print("                every prefix this template scopes to.")
    print()
    print("  Only a live lab session settles either direction. Nothing printed")
    print("  here is a permission result.")


def print_report(results: list[Result], inputs: list[PolicyInput]) -> None:
    """Print the cases grouped by resource, mismatches marked."""
    print()
    print(f"Simulated {len(results)} cases against {len(inputs)} statements.")
    current = None
    for result in results:
        if result.case.resource != current:
            current = result.case.resource
            print()
            print(f"  {current}")
        flag = "ok  " if result.ok else "MISS"
        matched = ", ".join(result.matched) if result.matched else "-"
        line = (
            f"    {flag} {result.case.action:<44} "
            f"{result.case.expected:<13} {result.actual:<13} {matched}"
        )
        print(line)
        if not result.ok and result.case.note:
            print(f"         {result.case.note}")


def print_summary(results: list[Result]) -> int:
    """Print the mismatches on their own, and return the process exit code."""
    mismatches = [result for result in results if not result.ok]
    print()
    print(f"{len(results) - len(mismatches)} matched, {len(mismatches)} did not.")
    if not mismatches:
        print("Every case the notebook makes is covered by lab.template's grants.")
        return 0
    print()
    print("Cases where the template does not do what the notebook needs:")
    for result in mismatches:
        note = f"  ({result.case.note})" if result.case.note else ""
        print(
            f"  {result.case.step:<7} {result.case.action:<44} "
            f"expected {result.case.expected}, got {result.actual}{note}"
        )
        print(f"          {result.case.resource}")
    return 1


def dotenv_value(path: Path, name: str) -> str:
    """Read one value out of the repository .env, without a dependency.

    ``vocareum_tools.config`` does this properly, and this script deliberately
    does not import it: the inline script metadata above means ``uv run`` builds
    an isolated environment where the package is not installed. Real environment
    variables still win, which is the same precedence ``Environment.get`` uses.
    """
    if name in os.environ:
        return os.environ[name].strip()
    if not path.is_file():
        return ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().removeprefix("export ").lstrip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value.strip()
    return ""


def repository_root() -> Path:
    """The repository root, found from this file rather than from the cwd."""
    return Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Simulate lab.template's grants against the calls "
            "the verification notebook makes. No lab session."
        )
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=(
            repository_root() / DEFAULT_COURSES_DIR / DEFAULT_COURSE / "lab.template"
        ),
        help=(
            "Template to read the AWS::IAM::RolePolicy grants from. Defaults to "
            f"course {DEFAULT_COURSE!r}'s own template."
        ),
    )
    parser.add_argument(
        "--account-id",
        help="Value for ${AWS::AccountId}. Defaults to LAB_AWS_ACCOUNT_ID.",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"Value for ${{AWS::Region}}. Default {DEFAULT_REGION}.",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"The notebook's PREFIX constant. Default {DEFAULT_PREFIX!r}.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the repository environment file.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Write the results to this JSON file.",
    )
    return parser.parse_args()


def main() -> int:
    """Parse the template, simulate every case, report, and gate on mismatches."""
    args = parse_args()

    account_id = args.account_id or dotenv_value(args.env_file, "LAB_AWS_ACCOUNT_ID")
    if not re.fullmatch(r"\d{12}", account_id or ""):
        print(
            "Need a 12-digit account id for ${AWS::AccountId}. Pass --account-id "
            f"or set LAB_AWS_ACCOUNT_ID in {args.env_file}."
        )
        return 2

    try:
        template = load_template(args.template)
        grants = extract_grants(template, account_id, args.region)
        inputs = build_policy_inputs(grants)
    except TemplateError as error:
        print(f"REFUSED TO REPORT: {error}")
        return 2

    print(f"template   {args.template}")
    print(f"account    {account_id} (substituted for ${{AWS::AccountId}})")
    print(f"region     {args.region}")
    print(f"prefix     {args.prefix}")
    print(
        f"statements {len(grants)} from {len({g.logical_id for g in grants})} "
        "AWS::IAM::RolePolicy resources"
    )
    print()
    print_caveat()

    cases = build_cases(account_id, args.region, args.prefix)
    iam = boto3.client("iam")
    try:
        results = run_cases(iam, inputs, cases)
    except (BotoCoreError, ClientError, RuntimeError) as error:
        print(f"REFUSED TO REPORT: iam:SimulateCustomPolicy failed: {error}")
        return 2

    print_report(results, inputs)
    code = print_summary(results)

    if args.json:
        args.json.write_text(
            json.dumps([result.as_dict() for result in results], indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")

    print()
    print("Reminder: allowed means the template grants it, not that the student")
    print("can do it. Only a live lab session settles that.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
