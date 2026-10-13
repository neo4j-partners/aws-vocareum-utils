#!/usr/bin/env -S uv run
"""Run the student-role AgentCore checks from Phase 3.

Phase 3 cannot be driven by ``vocareum-probe`` alone. Its actions are stateful:
``CreateMemory`` returns the ID that ``GetMemory``, ``UpdateMemory``, and
``DeleteMemory`` consume, a gateway target needs a gateway that already exists,
and an Agent Runtime needs a container image in ECR. A flat list of probes with
static arguments cannot express that, so the ordering lives here while the
guards and the error classifier stay shared with the general probe.

Every block is independently selectable, because a block that creates real
resources should be re-runnable on its own after a failure. Whatever a block
creates is torn down in reverse order even when the block raises, and the
teardown calls are themselves measurements: rows 24, 27, 29, 68, 71, 75, 78,
83, 87, 90, and 103 are all delete or detach actions.

    ./scripts/run_phase3_agentcore.py --block memory
    ./scripts/run_phase3_agentcore.py --block ecr --block codebuild
    ./scripts/run_phase3_agentcore.py --all

Exit codes:
    0  Every planned action ran and was classified.
    1  Configuration error.
    2  A guard refused to report, or credentials could not be obtained.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError, ClientError

from vocareum_tools.client import ApiError
from vocareum_tools.config import repository_root
from vocareum_tools.phase import (
    Recorder,
    json_default,
    teardown_guaranteed,
    wait_until,
)
from vocareum_tools.probe import (
    BOTO_CONFIG,
    ConfigError,
    GuardError,
    Verdict,
    build_session,
    fetch_vocareum_credentials,
    guard_account,
    guard_credentials,
    guard_region,
    load_config,
    start_session,
)
from vocareum_tools.security import redact_text

# One prefix for everything this script creates, so an abandoned run can be
# found and removed by hand. The workshop's own names are workshop-* and
# bedrock-agentcore-*; this is deliberately neither. Phase 3:C briefly moved
# the role names inside workshop-* after iam:PutRolePolicy was denied on a
# phase3probe- role, on the theory that the lab scopes that action by role
# name. A deliberate probe of both prefixes disproved it, so the names are back
# where they were and the denial is recorded as the transient it was.
#
# Was "phase3probe" until 2026-08-06. "Phase 3" is an internal milestone number
# that tells a reader nothing; this says what the runner does. Two constraints on
# the value, both real and both load-bearing. It carries no hyphen, because the
# AgentCore runtime and memory names composed below are {PREFIX}_memory,
# {PREFIX}_runtime, and {PREFIX}endpoint, and AgentCore accepts only letters,
# digits, and underscores there. And it does not start with "ws": a runtime named
# ws* makes InvokeAgentRuntime fail with a WebSocket error, which is what
# "wsverify" in sweep.NAME_PREFIXES is a fossil of. The old name stays in
# sweep.NAME_PREFIXES and sweep.IAM_ROLE_PREFIXES so resources earlier runs
# leaked under it are still swept.
PREFIX = "agentcoreprobe"

# AgentCore trusts this principal for both Gateway and Runtime execution roles.
AGENTCORE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"

# CodeBuild's ARM64 managed image. The Runtime is ARM64 only, so a build that
# produces a runnable agent image has to run on this compute type.
CODEBUILD_ARM_IMAGE = "aws/codebuild/amazonlinux2-aarch64-standard:3.0"

IMAGE_CONTEXT = repository_root() / "probes" / "phase3-agent-image"

BLOCKS = ("memory", "gateway", "ecr", "codebuild", "runtime", "slr")


# ---------------------------------------------------------------------------
# IAM helpers. These were once treated as setup rather than measurement, on the
# grounds that Phase 2 had already measured iam:CreateRole and iam:PassRole as
# Allowed. Phase 3:C disproved that when iam:PutRolePolicy was denied here and
# escaped as a stack trace: the setup path can produce a real authorization
# answer, and an answer that only reaches a traceback is an answer thrown away.
# Every IAM call here is now recorded against its tracker row.
# ---------------------------------------------------------------------------


def trust_policy(principal: str) -> str:
    """Return a service trust policy document."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": principal},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )


def create_role(
    iam: Any,
    recorder: Recorder,
    name: str,
    principal: str,
    permissions: dict[str, Any],
) -> str | None:
    """Create a temporary role with one inline policy, and own its teardown.

    Returns the role ARN, or None if the role cannot be made usable. A role
    that exists but carries no inline policy grants nothing, so it cannot serve
    as an execution role and the caller has to stop. Its teardown is registered
    either way, because the role itself was still created.
    """
    verdict, response = recorder.call(
        "23",
        "iam:CreateRole",
        lambda: iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=trust_policy(principal),
            Description="Temporary role for the Phase 3 probe.",
        ),
        context=name,
    )
    if verdict is not Verdict.OK or response is None:
        return None

    recorder.defer(f"role {name}", lambda: remove_role(iam, recorder, name))

    verdict, _ = recorder.call(
        "26",
        "iam:PutRolePolicy",
        lambda: iam.put_role_policy(
            RoleName=name,
            PolicyName="inline",
            PolicyDocument=json.dumps(permissions),
        ),
        context=name,
    )
    if verdict is not Verdict.OK:
        return None

    # IAM is eventually consistent. A role handed to another service within a
    # second or two of creation is regularly rejected as not assumable, which
    # would be recorded as a permission failure that is really a race.
    time.sleep(12)
    return response["Role"]["Arn"]


def remove_role(iam: Any, recorder: Recorder, name: str) -> None:
    """Delete a role and everything attached to it, recording the delete rows.

    What is attached is discovered rather than assumed. A role whose
    `iam:PutRolePolicy` was denied has no inline policy, and deleting a policy
    that was never created raises `NoSuchEntity`. That aborted this teardown
    before it reached the role and left the role in the account.
    """
    for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        recorder.call_in_teardown(
            "27",
            "iam:DeleteRolePolicy",
            lambda p=policy: iam.delete_role_policy(RoleName=name, PolicyName=p),
            context=f"{name}/{policy}",
        )
    for attached in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
        recorder.call_in_teardown(
            "29",
            "iam:DetachRolePolicy",
            lambda a=attached: iam.detach_role_policy(
                RoleName=name, PolicyArn=a["PolicyArn"]
            ),
            context=f"{name}/{attached['PolicyName']}",
        )
    recorder.call_in_teardown(
        "24",
        "iam:DeleteRole",
        lambda: iam.delete_role(RoleName=name),
        context=name,
    )


def allow(actions: list[str], resource: str = "*") -> dict[str, Any]:
    """Return a policy document allowing the given actions."""
    return {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": actions, "Resource": resource}],
    }


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def block_memory(ctx: Context) -> None:
    """Rows 84 through 87. Create, read, update, and delete one STM_ONLY memory."""
    control = ctx.control
    name = f"{PREFIX}_memory"

    verdict, response = ctx.recorder.call(
        "84",
        "bedrock-agentcore:CreateMemory",
        lambda: control.create_memory(
            name=name,
            description="Phase 3 short-term-only memory probe.",
            eventExpiryDuration=3,
        ),
    )
    if verdict is not Verdict.OK or response is None:
        return

    memory_id = response["memory"]["id"]

    def remove() -> None:
        control.delete_memory(memoryId=memory_id)

    ctx.recorder.defer(f"memory {memory_id}", remove)

    state = wait_until(
        lambda: control.get_memory(memoryId=memory_id)["memory"]["status"],
        done={"ACTIVE"},
        failed={"FAILED"},
        label=f"memory {memory_id}",
    )
    ctx.recorder.call(
        "85",
        "bedrock-agentcore:GetMemory",
        lambda: control.get_memory(memoryId=memory_id),
    )
    ctx.recorder.call(
        "86",
        "bedrock-agentcore:UpdateMemory",
        lambda: control.update_memory(
            memoryId=memory_id,
            description=f"Updated by the Phase 3 probe from state {state}.",
        ),
    )
    # UpdateMemory returns straight away but leaves the memory in UPDATING for
    # about a minute, and DeleteMemory refuses a transitional state. Without
    # this wait row 87 comes back ValidationException, which is correctly
    # classified as Untested and is a false negative: the call is allowed.
    wait_until(
        lambda: control.get_memory(memoryId=memory_id)["memory"]["status"],
        done={"ACTIVE"},
        failed={"FAILED"},
        label=f"memory {memory_id} after update",
    )
    verdict, _ = ctx.recorder.call(
        "87",
        "bedrock-agentcore:DeleteMemory",
        lambda: control.delete_memory(memoryId=memory_id),
    )
    if verdict is Verdict.OK:
        # Only a delete that succeeded spends the teardown. Dropping it either
        # way leaves the resource behind and still reports a clean account,
        # which is the one thing teardown_guaranteed exists to prevent.
        ctx.recorder.cleanups = [
            entry
            for entry in ctx.recorder.cleanups
            if entry[0] != f"memory {memory_id}"
        ]


def block_workload_identity(ctx: Context) -> None:
    """Rows 81 through 83, absent from the Phase 3 checklist but inside its criteria."""
    control = ctx.control
    name = f"{PREFIX}-identity"

    verdict, _ = ctx.recorder.call(
        "81",
        "bedrock-agentcore:CreateWorkloadIdentity",
        lambda: control.create_workload_identity(name=name),
    )
    if verdict is Verdict.OK:

        def remove() -> None:
            control.delete_workload_identity(name=name)

        ctx.recorder.defer(f"workload identity {name}", remove)

    ctx.recorder.call(
        "82",
        "bedrock-agentcore:GetWorkloadIdentity",
        lambda: control.get_workload_identity(name=name),
    )
    delete_verdict, _ = ctx.recorder.call(
        "83",
        "bedrock-agentcore:DeleteWorkloadIdentity",
        lambda: control.delete_workload_identity(name=name),
    )
    if delete_verdict is Verdict.OK:
        ctx.recorder.cleanups = [
            entry
            for entry in ctx.recorder.cleanups
            if entry[0] != f"workload identity {name}"
        ]


def block_gateway(ctx: Context) -> None:
    """Rows 73 through 80. One MCP gateway with one Lambda target."""
    control = ctx.control
    recorder = ctx.recorder

    role_arn = create_role(
        ctx.iam,
        recorder,
        f"{PREFIX}-gateway-role",
        AGENTCORE_PRINCIPAL,
        allow(["lambda:InvokeFunction", "bedrock:InvokeModel"]),
    )
    if role_arn is None:
        print("  gateway block needs a role and could not create one")
        return

    measure_workshop_gateway(ctx, role_arn)

    lambda_arn = create_probe_lambda(ctx)

    name = f"{PREFIX}-gateway"
    verdict, response = recorder.call(
        "73",
        "bedrock-agentcore:CreateGateway",
        lambda: control.create_gateway(
            name=name,
            roleArn=role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
            description="Phase 3 gateway probe.",
        ),
        context="AWS_IAM authorizer, so row 80 is measurable",
    )
    if verdict is not Verdict.OK or response is None:
        return

    gateway_id = response["gatewayId"]
    gateway_url = response.get("gatewayUrl", "")

    def remove_gateway() -> None:
        control.delete_gateway(gatewayIdentifier=gateway_id)

    recorder.defer(f"gateway {gateway_id}", remove_gateway)
    wait_until(
        lambda: control.get_gateway(gatewayIdentifier=gateway_id)["status"],
        done={"READY", "ACTIVE"},
        failed={"FAILED"},
        label=f"gateway {gateway_id}",
    )
    recorder.call(
        "74",
        "bedrock-agentcore:GetGateway",
        lambda: control.get_gateway(gatewayIdentifier=gateway_id),
    )

    target_id = None
    if lambda_arn is not None:
        tool = {
            "name": "probe_tool",
            "description": "Returns a fixed string so the target has a schema.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
        verdict, response = recorder.call(
            "76",
            "bedrock-agentcore:CreateGatewayTarget",
            lambda: control.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=f"{PREFIX}-target",
                # Not in the SDK's required list, but the service rejects the
                # request without it with a ValidationException, which is a
                # bad-parameter answer and would file row 76 as Untested.
                # GATEWAY_IAM_ROLE means the gateway reaches the target with
                # its own execution role, which is the role created above and
                # already granted lambda:InvokeFunction.
                credentialProviderConfigurations=[
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
                targetConfiguration={
                    "mcp": {
                        "lambda": {
                            "lambdaArn": lambda_arn,
                            "toolSchema": {"inlinePayload": [tool]},
                        }
                    }
                },
            ),
        )
        if verdict is Verdict.OK and response is not None:
            target_id = response["targetId"]

            def remove_target() -> None:
                control.delete_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=target_id
                )

            recorder.defer(f"gateway target {target_id}", remove_target)
    else:
        recorder.note(
            "76",
            "bedrock-agentcore:CreateGatewayTarget",
            Verdict.UNCLASSIFIED,
            "Skipped: the probe Lambda the target points at could not be created.",
        )

    if target_id is not None:
        recorder.call(
            "77",
            "bedrock-agentcore:GetGatewayTarget",
            lambda: control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            ),
        )
        # Row 79, SynchronizeGatewayTargets, is deliberately not called. The
        # service refuses it for Lambda targets, and Lambda targets are what
        # Module 6 registers. No workshop code invokes it, so the row is N/A
        # rather than unmeasured, and nothing is emitted for it here.

    measure_invoke_gateway(ctx, gateway_url)

    if target_id is not None:
        verdict, _ = recorder.call(
            "78",
            "bedrock-agentcore:DeleteGatewayTarget",
            lambda: control.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            ),
        )
        if verdict is Verdict.OK:
            recorder.cleanups = [
                entry
                for entry in recorder.cleanups
                if entry[0] != f"gateway target {target_id}"
            ]

            # DeleteGatewayTarget returns before the target is actually gone,
            # and DeleteGateway refuses a gateway that still has one. Without
            # this wait row 75 comes back ValidationException, which is
            # correctly classified as Untested and is a false negative, and
            # the teardown then hits the same window and leaks the gateway.
            def target_state() -> str:
                listed = control.list_gateway_targets(gatewayIdentifier=gateway_id)
                ids = [item["targetId"] for item in listed.get("items", [])]
                return "present" if target_id in ids else "gone"

            wait_until(
                target_state,
                done={"gone"},
                failed=set(),
                label=f"gateway target {target_id} removal",
                timeout=180,
            )

    verdict, _ = recorder.call(
        "75",
        "bedrock-agentcore:DeleteGateway",
        lambda: control.delete_gateway(gatewayIdentifier=gateway_id),
    )
    if verdict is Verdict.OK:
        recorder.cleanups = [
            entry for entry in recorder.cleanups if entry[0] != f"gateway {gateway_id}"
        ]


def measure_workshop_gateway(ctx: Context, role_arn: str) -> None:
    """Row 73 again, with the configuration Module 6 actually uses.

    The rest of this block builds an `AWS_IAM` gateway, because that is the
    only kind whose invoke can be measured: with `authorizerType="NONE"` the
    endpoint takes no IAM decision, so row 80 would have nothing to answer.
    But Module 6 builds a `SEMANTIC` routing gateway with no authorizer, and
    semantic routing is the whole reason it uses a Gateway rather than the
    local FAISS index from Module 2. A permission measured only against the
    keyword-routing configuration would not cover it.

    Created and deleted here. Nothing is registered against it.
    """
    name = f"{PREFIX}-gateway-semantic"
    context = "SEMANTIC routing, no authorizer, as Module 6 builds it"
    verdict, response = ctx.recorder.call(
        "73",
        "bedrock-agentcore:CreateGateway",
        lambda: ctx.control.create_gateway(
            name=name,
            roleArn=role_arn,
            protocolType="MCP",
            protocolConfiguration={
                "mcp": {
                    "searchType": "SEMANTIC",
                    "supportedVersions": ["2025-03-26"],
                }
            },
            authorizerType="NONE",
            description="Phase 3 gateway probe, Module 6 configuration.",
        ),
        context=context,
    )
    if verdict is not Verdict.OK or response is None:
        return

    gateway_id = response["gatewayId"]
    ctx.recorder.defer(
        f"gateway {gateway_id}",
        lambda: ctx.control.delete_gateway(gatewayIdentifier=gateway_id),
    )
    wait_until(
        lambda: ctx.control.get_gateway(gatewayIdentifier=gateway_id)["status"],
        done={"READY", "ACTIVE"},
        failed={"FAILED"},
        label=f"gateway {gateway_id}",
    )
    delete_verdict, _ = ctx.recorder.call(
        "75",
        "bedrock-agentcore:DeleteGateway",
        lambda: ctx.control.delete_gateway(gatewayIdentifier=gateway_id),
        context=context,
    )
    if delete_verdict is Verdict.OK:
        ctx.recorder.cleanups = [
            entry
            for entry in ctx.recorder.cleanups
            if entry[0] != f"gateway {gateway_id}"
        ]


def create_probe_lambda(ctx: Context) -> str | None:
    """Create the trivial Lambda a gateway target can point at."""
    name = f"{PREFIX}-tool"
    role_arn = create_role(
        ctx.iam,
        ctx.recorder,
        f"{PREFIX}-lambda-role",
        "lambda.amazonaws.com",
        allow(["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]),
    )
    if role_arn is None:
        return None

    source = (
        "def handler(event, context):\n"
        "    return {'result': 'phase3 probe tool', 'event': event}\n"
    )
    package = zip_bytes("index.py", source)
    try:
        response = ctx.awslambda.create_function(
            FunctionName=name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="index.handler",
            Code={"ZipFile": package},
            Timeout=10,
        )
    except ClientError as error:
        print(f"  setup failed: create_function {name}: {error}")
        return None

    def remove() -> None:
        ctx.awslambda.delete_function(FunctionName=name)

    ctx.recorder.defer(f"lambda {name}", remove)
    return response["FunctionArn"]


def zip_bytes(filename: str, source: str) -> bytes:
    """Return a one-file zip archive, the shape Lambda's ZipFile expects."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, source)
    return buffer.getvalue()


def measure_invoke_gateway(ctx: Context, gateway_url: str) -> None:
    """Row 80. InvokeGateway is an IAM permission, not a boto3 operation.

    The gateway speaks MCP over HTTPS. Nothing in boto3 calls it, so the only
    way to measure the permission is a SigV4-signed request to the gateway URL
    and a reading of what comes back.
    """
    if not gateway_url:
        ctx.recorder.note(
            "80",
            "bedrock-agentcore:InvokeGateway",
            Verdict.UNCLASSIFIED,
            "The gateway did not report a URL, so no request could be signed.",
        )
        return

    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    ).encode()
    signed = AWSRequest(
        method="POST",
        url=gateway_url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    credentials = ctx.session.get_credentials().get_frozen_credentials()
    SigV4Auth(credentials, "bedrock-agentcore", ctx.region).add_auth(signed)

    request = urllib.request.Request(
        gateway_url, data=body, headers=dict(signed.headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            text = redact_text(response.read().decode("utf-8", "replace"))[:300]
    except urllib.error.HTTPError as error:
        status = error.code
        text = redact_text(error.read().decode("utf-8", "replace"))[:300]
    except (urllib.error.URLError, TimeoutError) as error:
        ctx.recorder.note(
            "80",
            "bedrock-agentcore:InvokeGateway",
            Verdict.ENDPOINT_UNREACHABLE,
            redact_text(str(error)),
        )
        return

    if status == 403 and "explicit deny in a service control" in text:
        verdict = Verdict.SCP_DENY
    elif status == 403 and "not authorized to perform" in text:
        verdict = Verdict.IAM_DENY
    elif status < 400:
        verdict = Verdict.OK
    else:
        # A 4xx that is not an authorization refusal means the request reached
        # the gateway and the gateway answered, which is what the permission
        # question asks. Recorded, not promoted.
        verdict = Verdict.UNCLASSIFIED

    ctx.recorder.note(
        "80",
        "bedrock-agentcore:InvokeGateway",
        verdict,
        f"HTTP {status} from the signed MCP request: {text}",
    )


def block_ecr(ctx: Context) -> None:
    """Rows 88 through 98. Real repository, real image, one row at a time."""
    recorder = ctx.recorder
    ecr = ctx.ecr
    repo = f"{PREFIX}-agent"

    token_verdict, token = recorder.call(
        "88", "ecr:GetAuthorizationToken", ecr.get_authorization_token
    )

    verdict, response = recorder.call(
        "89",
        "ecr:CreateRepository",
        lambda: ecr.create_repository(repositoryName=repo),
    )
    if verdict is Verdict.OK and response is not None:
        ctx.repository_uri = response["repository"]["repositoryUri"]

        # Row 90 is measured in teardown rather than inline, because 3:F boots
        # the image this block pushes and the repository has to outlive 3:D.
        # The delete is still the measurement, so it goes through
        # call_in_teardown, which records the verdict and re-raises: a delete
        # that failed has to be reported as a resource left behind rather than
        # absorbed into a verdict and a clean-looking teardown.
        def remove() -> None:
            recorder.call_in_teardown(
                "90",
                "ecr:DeleteRepository",
                lambda: ecr.delete_repository(repositoryName=repo, force=True),
                repo,
            )

        recorder.defer(f"ecr repository {repo}", remove)
    else:
        # A repository left over from an earlier run is not a failure. Find it,
        # so the rest of the block still has something to push to.
        try:
            existing = ecr.describe_repositories(repositoryNames=[repo])
            ctx.repository_uri = existing["repositories"][0]["repositoryUri"]
        except ClientError:
            print("  no repository available; the rest of the ECR block cannot run")
            return

    recorder.call(
        "91",
        "ecr:DescribeRepositories",
        lambda: ecr.describe_repositories(repositoryNames=[repo]),
    )

    # A digest for a blob that was never uploaded. The call answers with a
    # "failures" list rather than an error, so authorization is what is being
    # measured, and nothing is created.
    absent = "sha256:" + "0" * 64
    recorder.call(
        "92",
        "ecr:BatchCheckLayerAvailability",
        lambda: ecr.batch_check_layer_availability(
            repositoryName=repo, layerDigests=[absent]
        ),
    )

    blob = b"phase3 permission probe layer\n"
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    verdict, upload = recorder.call(
        "93",
        "ecr:InitiateLayerUpload",
        lambda: ecr.initiate_layer_upload(repositoryName=repo),
    )
    upload_id = upload["uploadId"] if verdict is Verdict.OK and upload else None

    if upload_id is not None:
        recorder.call(
            "94",
            "ecr:UploadLayerPart",
            lambda: ecr.upload_layer_part(
                repositoryName=repo,
                uploadId=upload_id,
                partFirstByte=0,
                partLastByte=len(blob) - 1,
                layerPartBlob=blob,
            ),
        )
        recorder.call(
            "95",
            "ecr:CompleteLayerUpload",
            lambda: ecr.complete_layer_upload(
                repositoryName=repo, uploadId=upload_id, layerDigests=[digest]
            ),
        )
    else:
        for row, action in (
            ("94", "ecr:UploadLayerPart"),
            ("95", "ecr:CompleteLayerUpload"),
        ):
            recorder.note(
                row,
                action,
                Verdict.UNCLASSIFIED,
                "Skipped: InitiateLayerUpload returned no uploadId to continue.",
            )

    pushed = push_agent_image(ctx, token if token_verdict is Verdict.OK else None)

    if pushed:
        # BatchGetImage is measured first because rows 96 and 98 both need the
        # manifest it returns. It was once called raw for the manifest and then
        # a second time for its own row; the raw call sat outside the recorder,
        # so a denial there would have escaped as a stack trace and taken rows
        # 96 through 98 with it. One recorded call now serves both purposes.
        verdict, images = recorder.call(
            "97",
            "ecr:BatchGetImage",
            lambda: ecr.batch_get_image(
                repositoryName=repo, imageIds=[{"imageTag": "agent"}]
            ),
        )
        manifest = (
            images["images"][0]["imageManifest"]
            if verdict is Verdict.OK and images and images["images"]
            else None
        )

        if manifest is not None:
            # PutImage is measured by re-registering the manifest docker
            # already pushed under a second tag. The blobs exist, so the only
            # open question is authorization.
            recorder.call(
                "96",
                "ecr:PutImage",
                lambda: ecr.put_image(
                    repositoryName=repo,
                    imageManifest=manifest,
                    imageTag="agent-putimage-probe",
                ),
            )
            layer_digest = json.loads(manifest)["layers"][0]["digest"]
            recorder.call(
                "98",
                "ecr:GetDownloadUrlForLayer",
                lambda: ecr.get_download_url_for_layer(
                    repositoryName=repo, layerDigest=layer_digest
                ),
            )
        else:
            for row, action in (
                ("96", "ecr:PutImage"),
                ("98", "ecr:GetDownloadUrlForLayer"),
            ):
                recorder.note(
                    row,
                    action,
                    Verdict.UNCLASSIFIED,
                    "Skipped: BatchGetImage returned no manifest to act on.",
                )
    else:
        for row, action in (
            ("96", "ecr:PutImage"),
            ("97", "ecr:BatchGetImage"),
            ("98", "ecr:GetDownloadUrlForLayer"),
        ):
            recorder.note(
                row,
                action,
                Verdict.UNCLASSIFIED,
                "Skipped: no image was pushed, so there is no manifest to act on.",
            )


def push_agent_image(ctx: Context, token: dict[str, Any] | None) -> bool:
    """Build the ARM64 probe image and push it, using docker for the transport."""
    if ctx.repository_uri is None or token is None:
        return False
    if not IMAGE_CONTEXT.is_dir():
        print(f"  image context missing: {IMAGE_CONTEXT}")
        return False

    auth = token["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    registry = auth["proxyEndpoint"].removeprefix("https://")

    steps = (
        (
            "docker login",
            ["docker", "login", "--username", user, "--password-stdin", registry],
            password,
        ),
        (
            "docker build",
            [
                "docker",
                "build",
                "--platform",
                "linux/arm64",
                "--provenance",
                "false",
                "-t",
                f"{ctx.repository_uri}:agent",
                str(IMAGE_CONTEXT),
            ],
            None,
        ),
        ("docker push", ["docker", "push", f"{ctx.repository_uri}:agent"], None),
    )
    for label, command, stdin in steps:
        print(f"        {label} ...")
        result = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if result.returncode != 0:
            tail = redact_text((result.stderr or result.stdout).strip())[-400:]
            print(f"        {label} failed: {tail}")
            return False
    return True


def block_codebuild(ctx: Context) -> None:
    """Rows 99 through 103. An ARM64 project that builds the same image."""
    recorder = ctx.recorder
    codebuild = ctx.codebuild
    project = f"{PREFIX}-build"

    role_arn = create_role(
        ctx.iam,
        recorder,
        f"{PREFIX}-codebuild-role",
        "codebuild.amazonaws.com",
        allow(
            [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "ecr:GetAuthorizationToken",
                "ecr:BatchCheckLayerAvailability",
                "ecr:InitiateLayerUpload",
                "ecr:UploadLayerPart",
                "ecr:CompleteLayerUpload",
                "ecr:PutImage",
            ]
        ),
    )
    if role_arn is None:
        print("  codebuild block needs a service role and could not create one")
        return

    buildspec = json.dumps(
        {
            "version": "0.2",
            "phases": {
                "build": {
                    "commands": [
                        "echo Phase 3 ARM64 build probe",
                        "uname -m",
                    ]
                }
            },
        }
    )
    verdict, _ = recorder.call(
        "99",
        "codebuild:CreateProject",
        lambda: codebuild.create_project(
            name=project,
            source={"type": "NO_SOURCE", "buildspec": buildspec},
            artifacts={"type": "NO_ARTIFACTS"},
            environment={
                "type": "ARM_CONTAINER",
                "image": CODEBUILD_ARM_IMAGE,
                "computeType": "BUILD_GENERAL1_SMALL",
            },
            serviceRole=role_arn,
        ),
    )
    if verdict is not Verdict.OK:
        return

    def remove() -> None:
        codebuild.delete_project(name=project)

    recorder.defer(f"codebuild project {project}", remove)

    verdict, started = recorder.call(
        "100",
        "codebuild:StartBuild",
        lambda: codebuild.start_build(projectName=project),
    )
    build_id = started["build"]["id"] if verdict is Verdict.OK and started else None

    if build_id is not None:
        recorder.call(
            "101",
            "codebuild:BatchGetBuilds",
            lambda: codebuild.batch_get_builds(ids=[build_id]),
        )
        state = wait_until(
            lambda: codebuild.batch_get_builds(ids=[build_id])["builds"][0][
                "buildStatus"
            ],
            done={"SUCCEEDED"},
            failed={"FAILED", "FAULT", "STOPPED", "TIMED_OUT"},
            label=f"build {build_id}",
            timeout=600,
            interval=15,
        )
        architecture = read_build_architecture(ctx, build_id)
        print(f"        uname -m in the build log: {architecture}")
        recorder.note(
            "100",
            "codebuild:StartBuild (ARM64 outcome)",
            Verdict.OK if state == "SUCCEEDED" else Verdict.UNCLASSIFIED,
            f"ARM64 build on {CODEBUILD_ARM_IMAGE} finished in state {state}, "
            f"and uname -m in its log reported {architecture}.",
        )
    else:
        recorder.note(
            "101",
            "codebuild:BatchGetBuilds",
            Verdict.UNCLASSIFIED,
            "Skipped: StartBuild returned no build ID to poll.",
        )

    recorder.call("102", "codebuild:ListProjects", codebuild.list_projects)
    verdict, _ = recorder.call(
        "103",
        "codebuild:DeleteProject",
        lambda: codebuild.delete_project(name=project),
    )
    if verdict is Verdict.OK:
        recorder.cleanups = [
            entry
            for entry in recorder.cleanups
            if entry[0] != f"codebuild project {project}"
        ]


def read_build_architecture(ctx: Context, build_id: str) -> str:
    """Return what `uname -m` printed in the build log, or why it is unknown.

    3:E's checklist asks for the architecture the build actually ran on, not
    only the compute type it was configured with. The buildspec runs `uname -m`,
    and the answer is in CloudWatch Logs, which live in the lab account and go
    when the session does. So the log is read in the same run or not at all.

    `logs:GetLogEvents` is not a Phase 3 tracker row, so this is an observation
    rather than a measurement and it is printed rather than recorded against a
    row. It still catches its own errors: an answer that reaches a traceback is
    an answer thrown away, and that rule does not stop applying because the
    answer is not on the tracker.
    """
    try:
        builds = ctx.codebuild.batch_get_builds(ids=[build_id])["builds"]
        stream = builds[0].get("logs", {}) if builds else {}
        group, name = stream.get("groupName"), stream.get("streamName")
        if not group or not name:
            return "unknown, the build reported no CloudWatch log stream"
        events = ctx.logs.get_log_events(
            logGroupName=group, logStreamName=name, startFromHead=True
        )["events"]
    except (ClientError, BotoCoreError) as error:
        return f"unknown, reading the log failed: {redact_text(str(error))[:200]}"

    # CodeBuild echoes the command it is about to run on its own line, so an
    # exact match is what tells the output of `uname -m` from the line
    # announcing it.
    reported = [
        line
        for line in (event["message"].strip() for event in events)
        if line in ("aarch64", "arm64", "x86_64", "amd64")
    ]
    return reported[-1] if reported else "unknown, no uname -m output in the log"


def echoed_payload(
    response: dict[str, Any], sent: dict[str, Any]
) -> tuple[str, Verdict, str]:
    """Report whether the container echoed the payload row 72 sent it.

    3:F's checklist asks for the echo, not only for the verdict. Row 72's own
    Allowed answer is settled by the call returning, so this is a second
    observation against the same row rather than the row's measurement: it says
    whether the container on the other side actually ran, which is what makes
    row 72 a proof that a deployed agent can be called rather than a proof that
    the API accepted a request. Recorded separately so neither claim can be
    mistaken for the other.

    The body is read here rather than left in the response, because the streamed
    payload is consumed once and the run has one chance to look at it.
    """
    action = "bedrock-agentcore:InvokeAgentRuntime (payload echoed)"
    body = response.get("response")
    if body is None:
        return action, Verdict.UNCLASSIFIED, "The invocation returned no response body."
    try:
        received = json.loads(body.read())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        return (
            action,
            Verdict.UNCLASSIFIED,
            f"The response body could not be read as JSON: {redact_text(str(error))}",
        )

    if isinstance(received, dict) and received.get("echo") == sent:
        return (
            action,
            Verdict.OK,
            (
                "The container echoed the payload back and named itself "
                f"{received.get('source')!r}."
            ),
        )
    return (
        action,
        Verdict.UNCLASSIFIED,
        (
            "The container answered but did not echo what was sent: "
            f"{redact_text(json.dumps(received))[:200]}"
        ),
    )


def block_runtime(ctx: Context) -> None:
    """Rows 65 through 72. One Agent Runtime, one endpoint, one invocation."""
    recorder = ctx.recorder
    control = ctx.control

    image = ctx.container_uri()
    if image is None:
        recorder.note(
            "65",
            "bedrock-agentcore:CreateAgentRuntime",
            Verdict.UNCLASSIFIED,
            "Skipped: no ARM64 container image in ECR. Run --block ecr first.",
        )
        return

    role_arn = create_role(
        ctx.iam,
        recorder,
        f"{PREFIX}-runtime-role",
        AGENTCORE_PRINCIPAL,
        allow(
            [
                "ecr:GetAuthorizationToken",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ]
        ),
    )
    if role_arn is None:
        print("  runtime block needs an execution role and could not create one")
        return

    name = f"{PREFIX}_runtime"
    verdict, response = recorder.call(
        "65",
        "bedrock-agentcore:CreateAgentRuntime",
        lambda: control.create_agent_runtime(
            agentRuntimeName=name,
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            description="Phase 3 runtime probe.",
        ),
    )
    if verdict is not Verdict.OK or response is None:
        return

    runtime_id = response["agentRuntimeId"]

    def remove_runtime() -> None:
        control.delete_agent_runtime(agentRuntimeId=runtime_id)

    recorder.defer(f"agent runtime {runtime_id}", remove_runtime)
    state = wait_until(
        lambda: control.get_agent_runtime(agentRuntimeId=runtime_id)["status"],
        done={"READY", "ACTIVE"},
        failed={"CREATE_FAILED", "FAILED"},
        label=f"runtime {runtime_id}",
        timeout=420,
    )
    recorder.call(
        "66",
        "bedrock-agentcore:GetAgentRuntime",
        lambda: control.get_agent_runtime(agentRuntimeId=runtime_id),
    )
    recorder.call(
        "67",
        "bedrock-agentcore:UpdateAgentRuntime",
        lambda: control.update_agent_runtime(
            agentRuntimeId=runtime_id,
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            description=f"Updated by the Phase 3 probe from state {state}.",
        ),
    )
    # UpdateAgentRuntime returns straight away and leaves the runtime in
    # UPDATING while the new version deploys. This is the third place in the
    # phase where a call that returns is not a call that finished, after the
    # memory update and the gateway target delete, so it is waited on rather
    # than assumed. An endpoint created against a transitional runtime would be
    # refused with a ValidationException, which is correctly classified as
    # Untested and would be a false negative on row 69, taking rows 70 and 72
    # with it. UPDATE_FAILED is in the failure set because it is the state this
    # particular wait can settle in.
    wait_until(
        lambda: control.get_agent_runtime(agentRuntimeId=runtime_id)["status"],
        done={"READY", "ACTIVE"},
        failed={"UPDATE_FAILED", "CREATE_FAILED", "FAILED"},
        label=f"runtime {runtime_id} after update",
        timeout=420,
    )

    endpoint = f"{PREFIX}endpoint"
    verdict, _ = recorder.call(
        "69",
        "bedrock-agentcore:CreateAgentRuntimeEndpoint",
        lambda: control.create_agent_runtime_endpoint(
            agentRuntimeId=runtime_id, name=endpoint
        ),
    )
    endpoint_made = verdict is Verdict.OK
    if endpoint_made:

        def remove_endpoint() -> None:
            control.delete_agent_runtime_endpoint(
                agentRuntimeId=runtime_id, endpointName=endpoint
            )

        recorder.defer(f"runtime endpoint {endpoint}", remove_endpoint)
        wait_until(
            lambda: control.get_agent_runtime_endpoint(
                agentRuntimeId=runtime_id, endpointName=endpoint
            )["status"],
            done={"READY", "ACTIVE"},
            failed={"CREATE_FAILED", "FAILED"},
            label=f"endpoint {endpoint}",
            timeout=420,
        )

    recorder.call(
        "70",
        "bedrock-agentcore:GetAgentRuntimeEndpoint",
        lambda: control.get_agent_runtime_endpoint(
            agentRuntimeId=runtime_id, endpointName=endpoint
        ),
    )

    runtime_arn = response["agentRuntimeArn"]
    sent = {"prompt": "phase3 permission probe"}
    verdict, invoked = recorder.call(
        "72",
        "bedrock-agentcore:InvokeAgentRuntime",
        lambda: ctx.dataplane.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier=endpoint if endpoint_made else "DEFAULT",
            payload=json.dumps(sent).encode(),
        ),
    )
    if verdict is Verdict.OK and invoked is not None:
        action, echo_verdict, detail = echoed_payload(invoked, sent)
        # Printed as well as recorded, because `note` prints the detail only for
        # a verdict that is not OK, and the whole point here is to see the echo
        # on a run where everything worked.
        print(f"        {detail}")
        recorder.note("72", action, echo_verdict, detail)

    if endpoint_made:
        verdict, _ = recorder.call(
            "71",
            "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
            lambda: control.delete_agent_runtime_endpoint(
                agentRuntimeId=runtime_id, endpointName=endpoint
            ),
        )
        if verdict is Verdict.OK:
            recorder.cleanups = [
                entry
                for entry in recorder.cleanups
                if entry[0] != f"runtime endpoint {endpoint}"
            ]

            # DeleteAgentRuntimeEndpoint returns before the endpoint is gone,
            # which is the window the gateway target delete opened in 3:C. This
            # was a flat 20-second sleep, and a sleep is a guess: too short and
            # row 68 is refused on a transitional state, which files Untested
            # and leaves the runtime in the account while the teardown reports
            # it removed. Poll for the endpoint to actually disappear instead,
            # the same fix rows 78 and 75 already took.
            def endpoint_state() -> str:
                listed = control.list_agent_runtime_endpoints(agentRuntimeId=runtime_id)
                names = [item["name"] for item in listed.get("runtimeEndpoints", [])]
                return "present" if endpoint in names else "gone"

            wait_until(
                endpoint_state,
                done={"gone"},
                failed=set(),
                label=f"runtime endpoint {endpoint} removal",
                timeout=180,
            )

    verdict, _ = recorder.call(
        "68",
        "bedrock-agentcore:DeleteAgentRuntime",
        lambda: control.delete_agent_runtime(agentRuntimeId=runtime_id),
    )
    if verdict is Verdict.OK:
        recorder.cleanups = [
            entry
            for entry in recorder.cleanups
            if entry[0] != f"agent runtime {runtime_id}"
        ]


def block_slr(ctx: Context) -> None:
    """Row 35. Ask for the service-linked role AgentCore itself would ask for."""
    existing = []
    try:
        paginator = ctx.iam.get_paginator("list_roles")
        for page in paginator.paginate(PathPrefix="/aws-service-role/"):
            existing.extend(
                role["RoleName"]
                for role in page["Roles"]
                if "agentcore" in role["Path"].lower()
                or "agentcore" in role["RoleName"].lower()
            )
    except ClientError as error:
        print(f"  could not list service-linked roles: {error}")

    print(
        f"        AgentCore service-linked roles already present: {existing or 'none'}"
    )

    ctx.recorder.call(
        "35",
        "iam:CreateServiceLinkedRole",
        lambda: ctx.iam.create_service_linked_role(
            AWSServiceName=AGENTCORE_PRINCIPAL,
            Description="Phase 3 service-linked role probe.",
        ),
    )
    ctx.recorder.note(
        "35",
        "iam:CreateServiceLinkedRole (roles observed)",
        Verdict.OK,
        f"AgentCore service-linked roles seen in the account: {existing or 'none'}",
    )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """Everything a block needs, built once after the guards pass."""

    session: Any
    region: str
    account: str
    recorder: Recorder
    repository_uri: str | None = None
    clients: dict[str, Any] = field(default_factory=dict)

    @property
    def control(self) -> Any:
        """Return the AgentCore control-plane client."""
        return self._client("bedrock-agentcore-control")

    @property
    def dataplane(self) -> Any:
        """Return the AgentCore data-plane client, which owns InvokeAgentRuntime."""
        return self._client("bedrock-agentcore")

    @property
    def iam(self) -> Any:
        """Return the IAM client."""
        return self._client("iam")

    @property
    def ecr(self) -> Any:
        """Return the ECR client."""
        return self._client("ecr")

    @property
    def codebuild(self) -> Any:
        """Return the CodeBuild client."""
        return self._client("codebuild")

    @property
    def awslambda(self) -> Any:
        """Return the Lambda client."""
        return self._client("lambda")

    @property
    def logs(self) -> Any:
        """Return the CloudWatch Logs client, used to read the CodeBuild log."""
        return self._client("logs")

    def _client(self, name: str) -> Any:
        """Build a client once, with the shared timeout configuration."""
        if name not in self.clients:
            self.clients[name] = self.session.client(
                name, region_name=self.region, config=BOTO_CONFIG
            )
        return self.clients[name]

    def container_uri(self) -> str | None:
        """Return the pushed probe image URI, discovering it if a block set none."""
        if self.repository_uri is not None:
            return f"{self.repository_uri}:agent"
        try:
            repos = self.ecr.describe_repositories(repositoryNames=[f"{PREFIX}-agent"])
        except ClientError:
            return None
        self.repository_uri = repos["repositories"][0]["repositoryUri"]
        return f"{self.repository_uri}:agent"


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run student-role AgentCore checks, Phase 3."
    )
    parser.add_argument(
        "--block",
        action="append",
        choices=BLOCKS,
        help="Which block to run. Repeatable. Defaults to none, so --all is explicit.",
    )
    parser.add_argument(
        "--all", action="store_true", help="Run every block in dependency order."
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Use the current lab session without starting or extending it.",
    )
    parser.add_argument("--region", help="Override AWS_REGION.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the repository environment file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the measurements to this JSON file.",
    )
    return parser.parse_args()


def main() -> int:
    """Guard the identity, run the selected blocks, then always tear down."""
    args = parse_args()
    selected = list(BLOCKS) if args.all else (args.block or [])
    if not selected:
        print("Nothing selected. Pass --all or one or more --block values.")
        return 1

    try:
        config = load_config(args.env_file, args.region, None, True)
        if not args.no_start:
            start_session(config)
        credentials = fetch_vocareum_credentials(config, wait=not args.no_start)
        guard_credentials(credentials)
        session = build_session(credentials)
        guard_account(session, config.region, config.expected_account)
        if not guard_region(config.region):
            return 2
    except (ApiError, ConfigError, GuardError) as error:
        # ApiError belongs here with the guards. A Vocareum API timeout that
        # escapes as a traceback reads like a crash rather than a refusal, and
        # the whole point of the guards is that a run either reports against a
        # confirmed identity or says plainly that it reported nothing.
        print(f"REFUSED TO REPORT: {error}")
        return 2

    recorder = Recorder()
    ctx = Context(
        session=session,
        region=config.region,
        account=config.expected_account,
        recorder=recorder,
    )

    runners: dict[str, Callable[[Context], None]] = {
        "memory": block_memory,
        "gateway": block_gateway,
        "ecr": block_ecr,
        "codebuild": block_codebuild,
        "runtime": block_runtime,
        "slr": block_slr,
    }

    with teardown_guaranteed(recorder):
        for name in selected:
            print(f"\n=== block {name} ===")
            runners[name](ctx)
            if name == "memory":
                # Rows 81 through 83 are inside Phase 3's completion criteria
                # but missing from its checklist, so they ride with memory.
                print("\n=== block workload-identity ===")
                block_workload_identity(ctx)

    print("\n=== measurements ===")
    for measurement in recorder.measurements:
        print(
            f"  row {measurement.row:<6} {measurement.status:<14} {measurement.action}"
        )

    if args.output:
        args.output.write_text(
            json.dumps(
                [m.as_dict() for m in recorder.measurements],
                indent=2,
                default=json_default,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
