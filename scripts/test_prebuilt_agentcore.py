#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.34", "botocore>=1.34"]
# ///
"""Deploy AgentCore from a pre-built image, skipping CodeBuild entirely.

Vocareum stops every CodeBuild build in the student account a few seconds after
it starts, so the workshop's notebook never gets an image into ECR and every
check downstream of it is skipped. That leaves steps 9 onward untested, which
is the part of the lab that actually exercises Amazon Bedrock AgentCore.

This script substitutes a pre-built linux/arm64 image for the build, then runs
the rest of the lab against it: create a Runtime, create an endpoint, invoke it,
and tear all of it down.

The image is copied into the student's own ECR over the OCI registry HTTP API
rather than with ``docker``. AgentCore only accepts an image in the caller's own
account and region, and the Vocareum Notebook lab type has no Docker daemon, so
a copy that needs one would work here and fail in the place it has to run.

    ./scripts/test_prebuilt_agentcore.py
    ./scripts/test_prebuilt_agentcore.py --keep    # leave the runtime standing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_SOURCE = "public.ecr.aws/h6l4p0n8/agentcore-verify:verify"
MANIFEST_TYPES = ",".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)
INDEX_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
READY_STATES = {"ACTIVE", "READY"}
TARGET_PLATFORM = ("linux", "arm64")
UPLOAD_CHUNK = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass
class Report:
    """One line per check, in the order the checks ran."""

    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def record(self, name: str, verdict: str, detail: str = "") -> None:
        self.rows.append((name, verdict, detail))
        print(f"{verdict:<4}  {name}" + (f"  {detail}" if detail else ""), flush=True)

    def summary(self) -> int:
        """Print the tally and return the exit code the run earned."""
        counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for _, verdict, _ in self.rows:
            counts[verdict] = counts.get(verdict, 0) + 1
        print(
            f"\n{counts['PASS']} passed, {counts['FAIL']} failed, "
            f"{counts['SKIP']} skipped"
        )
        return 1 if counts["FAIL"] else 0


# ---------------------------------------------------------------------------
# Registry HTTP
# ---------------------------------------------------------------------------


class StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drop the registry credential when a blob redirects to object storage.

    Blob reads answer 307 to a pre-signed S3 URL. Forwarding the registry's
    Authorization header to that URL makes S3 reject the request for carrying
    two authentication mechanisms, so it has to come off before the hop.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            for store in (redirected.headers, redirected.unredirected_hdrs):
                for key in [k for k in store if k.lower() == "authorization"]:
                    del store[key]
        return redirected


_opener = urllib.request.build_opener(StripAuthOnRedirect)


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    tolerate: tuple[int, ...] = (),
) -> tuple[int, bytes, dict[str, str]]:
    """Make one registry call and return its status, body, and headers."""
    call = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        call.add_header(key, value)
    try:
        with _opener.open(call, timeout=300) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        if error.code in tolerate:
            return error.code, error.read(), dict(error.headers)
        raise


@dataclass(frozen=True)
class Registry:
    """One registry endpoint and the header that authenticates to it."""

    host: str
    repository: str
    auth: str

    def url(self, path: str) -> str:
        return f"https://{self.host}/v2/{self.repository}/{path}"

    def headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": self.auth, **extra}


def parse_reference(reference: str) -> tuple[str, str, str]:
    """Split ``host/namespace/repo:tag`` into host, repository, and tag."""
    remainder, _, tag = reference.rpartition(":")
    if "/" in tag or not remainder:
        raise ValueError(f"expected host/repository:tag, got {reference!r}")
    host, _, repository = remainder.partition("/")
    if not repository:
        raise ValueError(f"expected host/repository:tag, got {reference!r}")
    return host, repository, tag


def public_pull_token(host: str, repository: str) -> str:
    """Fetch the anonymous pull token a public registry hands out."""
    query = urllib.parse.urlencode(
        {"scope": f"repository:{repository}:pull", "service": host}
    )
    _, body, _ = request("GET", f"https://{host}/token/?{query}")
    return f"Bearer {json.loads(body)['token']}"


def private_push_auth(ecr: Any) -> str:
    """Fetch the Basic credential ECR issues for its own registry."""
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    return f"Basic {token}"


def select_platform_manifest(source: Registry, raw: bytes) -> tuple[bytes, str]:
    """Resolve a multi-platform index down to the linux/arm64 manifest."""
    index = json.loads(raw)
    for entry in index.get("manifests", []):
        platform = entry.get("platform", {})
        pair = (platform.get("os"), platform.get("architecture"))
        if pair == TARGET_PLATFORM:
            _, body, response_headers = request(
                "GET",
                source.url(f"manifests/{entry['digest']}"),
                headers=source.headers(Accept=MANIFEST_TYPES),
            )
            return body, response_headers.get("Content-Type", "")
    raise RuntimeError("the source image has no linux/arm64 manifest")


def referenced_digests(manifest: dict[str, Any]) -> Iterator[str]:
    """Yield the config digest and every layer digest, in push order."""
    yield manifest["config"]["digest"]
    for layer in manifest["layers"]:
        yield layer["digest"]


def absolute(host: str, location: str) -> str:
    """Turn a registry's upload Location into a full URL."""
    if location.startswith("http"):
        return location
    return f"https://{host}{location}"


def read_exactly(stream: Any, size: int) -> bytes:
    """Read up to size bytes, short only at the end of the stream."""
    buffer = bytearray()
    while len(buffer) < size:
        piece = stream.read(size - len(buffer))
        if not piece:
            break
        buffer += piece
    return bytes(buffer)


def copy_blob(source: Registry, dest: Registry, digest: str) -> str:
    """Copy one blob if the destination does not already hold it.

    The upload goes out as a sequence of PATCH chunks closed by a PUT rather
    than as a single monolithic PUT. ECR accepts a monolithic PUT for something
    the size of a config blob and drops the connection partway through one the
    size of a layer, which surfaces as a broken pipe rather than an HTTP error.
    Chunking also keeps a layer of any size out of memory.
    """
    status, _, _ = request(
        "HEAD", dest.url(f"blobs/{digest}"), headers=dest.headers(), tolerate=(404,)
    )
    if status == 200:
        return "already present"

    _, _, opened = request(
        "POST",
        dest.url("blobs/uploads/"),
        headers=dest.headers(**{"Content-Length": "0"}),
    )
    location = absolute(dest.host, opened["Location"])

    pull = urllib.request.Request(source.url(f"blobs/{digest}"), method="GET")
    for key, value in source.headers().items():
        pull.add_header(key, value)

    offset = 0
    with _opener.open(pull, timeout=300) as blob:
        while chunk := read_exactly(blob, UPLOAD_CHUNK):
            _, _, patched = request(
                "PATCH",
                location,
                headers=dest.headers(
                    **{
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"{offset}-{offset + len(chunk) - 1}",
                    }
                ),
                body=chunk,
            )
            offset += len(chunk)
            location = absolute(dest.host, patched.get("Location", location))

    separator = "&" if "?" in location else "?"
    request(
        "PUT",
        f"{location}{separator}digest={digest}",
        headers=dest.headers(**{"Content-Length": "0"}),
    )
    return f"{offset // 1024} KiB"


def copy_blob_with_retry(
    source: Registry, dest: Registry, digest: str, attempts: int = 4
) -> str:
    """Copy one blob, starting a fresh upload if the connection drops.

    A 30 MB layer crossing two registries gets its connection reset often
    enough to matter. There is no resuming a half-written chunked upload
    without knowing what the registry kept, so each attempt opens a new upload
    session. Blobs already committed are skipped by the HEAD in ``copy_blob``.
    """
    for attempt in range(1, attempts + 1):
        try:
            return copy_blob(source, dest, digest)
        except (urllib.error.URLError, ConnectionError) as error:
            if attempt == attempts:
                raise
            print(
                f"      blob {digest[7:19]}  {error}, retrying"
                f" ({attempt}/{attempts - 1})",
                flush=True,
            )
            time.sleep(3 * attempt)
    raise AssertionError("unreachable")


def copy_image(source_ref: str, dest_ref: str, ecr: Any, report: Report) -> bool:
    """Copy a whole image between registries without a Docker daemon."""
    source_host, source_repo, source_tag = parse_reference(source_ref)
    dest_host, dest_repo, dest_tag = parse_reference(dest_ref)

    source = Registry(
        source_host, source_repo, public_pull_token(source_host, source_repo)
    )
    dest = Registry(dest_host, dest_repo, private_push_auth(ecr))

    _, raw, response_headers = request(
        "GET",
        source.url(f"manifests/{source_tag}"),
        headers=source.headers(Accept=MANIFEST_TYPES),
    )
    media_type = response_headers.get("Content-Type", "")
    if media_type in INDEX_TYPES:
        raw, media_type = select_platform_manifest(source, raw)

    manifest = json.loads(raw)
    report.record(
        "registry: read source manifest",
        "PASS",
        f"{source_ref}, {len(manifest['layers'])} layers",
    )

    for digest in referenced_digests(manifest):
        outcome = copy_blob_with_retry(source, dest, digest)
        print(f"      blob {digest[7:19]}  {outcome}", flush=True)
    report.record("registry: copy blobs", "PASS", "config and layers in place")

    request(
        "PUT",
        dest.url(f"manifests/{dest_tag}"),
        headers=dest.headers(**{"Content-Type": media_type}),
        body=raw,
    )
    report.record("registry: put manifest", "PASS", dest_ref)
    return True


# ---------------------------------------------------------------------------
# AWS setup
# ---------------------------------------------------------------------------


def ensure_repository(ecr: Any, name: str, report: Report) -> None:
    """Create the destination repository unless it is already there."""
    try:
        ecr.create_repository(repositoryName=name)
        report.record("ecr:CreateRepository", "PASS", name)
    except ecr.exceptions.RepositoryAlreadyExistsException:
        report.record("ecr:CreateRepository", "PASS", f"{name}, already existed")


def create_runtime_role(iam: Any, name: str, report: Report) -> str:
    """Create the role AgentCore assumes, with the notebook's permissions."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "*",
            }
        ],
    }
    created = iam.create_role(
        RoleName=name,
        AssumeRolePolicyDocument=json.dumps(trust),
        Description="AgentCore pre-built image test. Safe to delete.",
    )
    iam.put_role_policy(
        RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(policy)
    )
    report.record("iam:CreateRole runtime role", "PASS", name)
    return created["Role"]["Arn"]


def delete_role(iam: Any, name: str) -> None:
    """Empty a role of inline policies before deleting it, as IAM requires."""
    for attached in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=name, PolicyName=attached)
    iam.delete_role(RoleName=name)


def wait_until(
    describe: Callable[[], str],
    done: set[str],
    failed: set[str],
    label: str,
    timeout: int = 420,
    interval: int = 10,
) -> str:
    """Poll a status until it settles, and report the state it settled in."""
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = describe()
        if state in done or state in failed:
            print(f"      {label} settled in {state}", flush=True)
            return state
        time.sleep(interval)
    print(f"      {label} still {state} after {timeout}s", flush=True)
    return state


def retry_while(
    call: Callable[[], Any], codes: set[str], label: str, timeout: int = 180
) -> Any:
    """Retry while a call fails with a code that means eventual consistency.

    The message is printed along with the code. Printing the code alone hid a
    ``ValidationException`` on the first two ``CreateAgentRuntime`` attempts of
    every run for weeks, and the code by itself says nothing about which field
    the service objected to.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return call()
        except ClientError as error:
            code = error.response["Error"]["Code"]
            message = error.response["Error"].get("Message", "")
            if code not in codes or time.monotonic() >= deadline:
                raise
            print(f"      {label}: {code}: {message}, retrying", flush=True)
            time.sleep(6)


# ---------------------------------------------------------------------------
# The part of the lab this exists to test
# ---------------------------------------------------------------------------


def invoke_runtime(
    dataplane: Any, runtime_arn: str, endpoint: str, prefix: str, report: Report
) -> None:
    """Invoke the endpoint and confirm the container echoed the payload back.

    The notebook omits ``runtimeSessionId``. That path is tried first so this
    reports on the lab as written, and only falls back to supplying one if the
    service turns out to require it.
    """
    payload = json.dumps({"ping": prefix}).encode()
    kwargs: dict[str, Any] = {
        "agentRuntimeArn": runtime_arn,
        "qualifier": endpoint,
        "payload": payload,
    }
    try:
        answer = dataplane.invoke_agent_runtime(**kwargs)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ValidationException":
            raise
        report.record(
            "agentcore:InvokeAgentRuntime without a session id",
            "FAIL",
            error.response["Error"].get("Message", "")[:160],
        )
        kwargs["runtimeSessionId"] = f"{prefix}-{uuid.uuid4().hex}{uuid.uuid4().hex}"
        answer = dataplane.invoke_agent_runtime(**kwargs)

    report.record("agentcore:InvokeAgentRuntime", "PASS")
    body = answer["response"].read().decode("utf-8", "replace")
    if prefix in body:
        report.record("runtime echoed the payload", "PASS", body[:120])
    else:
        report.record("runtime echoed the payload", "FAIL", body[:200])


def run_agentcore(
    control: Any,
    dataplane: Any,
    image_uri: str,
    role_arn: str,
    prefix: str,
    cleanups: list[tuple[str, Callable[[], Any]]],
    report: Report,
    *,
    send_protocol: bool = True,
) -> None:
    """Create a Runtime and endpoint against the image, then invoke it."""
    runtime_name = f"{prefix}_runtime"
    endpoint_name = f"{prefix}endpoint"

    # serverProtocol accepts MCP, HTTP, A2A, and AGUI. Sending it is optional.
    #
    # This argument was once believed to be required, on the theory that
    # omitting it left the runtime with no protocol and made the invoke fail
    # with "Not a WebSocket Upgrade Request". Half of that is true and the
    # conclusion is not. Every runtime the theory was formed on was named ws*,
    # and a ws* name misroutes the invoke to the service's WebSocket handler by
    # itself, producing that same string whatever the protocol says.
    #
    # Separated 2026-08-05 in account ${CONTROL_AWS_ACCOUNT_ID}, us-east-1, with
    # two non-ws prefixes and nothing else different. Omitting the argument does
    # leave the stored record without a protocolConfiguration key:
    # get-agent-runtime on
    # ctlnoproto_runtime-wbyxW494EF returned no such field, while
    # ctlproto_runtime-24RNAx2imA returned serverProtocol HTTP. The invoke
    # passed and the container echoed the payload in both. So the field is
    # absent rather than defaulted in the record, and the service still routes
    # to plain HTTP without it.
    #
    # It stays set here because saying so costs nothing and an absent field is
    # a worse thing to depend on than a stated one. ``--no-protocol`` keeps the
    # control available for a rerun.
    create_kwargs: dict[str, Any] = {
        "agentRuntimeName": runtime_name,
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "description": "AgentCore pre-built image test.",
    }
    if send_protocol:
        create_kwargs["protocolConfiguration"] = {"serverProtocol": "HTTP"}

    # The ValidationException retried here is IAM propagation on the role
    # created moments ago, confirmed 2026-08-05. The message names the role:
    # "Role validation failed for '...'. Please verify that the role exists and
    # its trust policy allows assumption by this service". It fires on exactly
    # the first two attempts of every run without a wait, and on none at all
    # when the role is left to settle for 30s first, which ``--role-delay``
    # does. Nothing about the runtime arguments is wrong.
    runtime = retry_while(
        lambda: control.create_agent_runtime(**create_kwargs),
        codes={"ValidationException"},
        label="create_agent_runtime",
    )
    runtime_id = runtime["agentRuntimeId"]
    runtime_arn = runtime["agentRuntimeArn"]
    cleanups.append(
        (
            f"agent runtime {runtime_id}",
            # Deleting an endpoint returns before the endpoint is actually
            # gone, and the runtime refuses to go while one still exists. The
            # conflict is a timing artifact, so it is waited out rather than
            # reported as a teardown failure.
            lambda: retry_while(
                lambda: control.delete_agent_runtime(agentRuntimeId=runtime_id),
                codes={"ConflictException"},
                label="delete_agent_runtime",
            ),
        )
    )
    report.record("agentcore:CreateAgentRuntime", "PASS", runtime_id)

    state = wait_until(
        lambda: control.get_agent_runtime(agentRuntimeId=runtime_id)["status"],
        done=READY_STATES,
        failed={"CREATE_FAILED", "FAILED"},
        label=f"runtime {runtime_id}",
    )
    if state not in READY_STATES:
        report.record("runtime reached READY", "FAIL", f"settled in {state}")
        report.record(
            "agentcore:InvokeAgentRuntime", "SKIP", "the runtime is not ready"
        )
        return
    report.record("runtime reached READY", "PASS", state)

    control.create_agent_runtime_endpoint(agentRuntimeId=runtime_id, name=endpoint_name)
    cleanups.append(
        (
            f"runtime endpoint {endpoint_name}",
            lambda: control.delete_agent_runtime_endpoint(
                agentRuntimeId=runtime_id, endpointName=endpoint_name
            ),
        )
    )
    report.record("agentcore:CreateAgentRuntimeEndpoint", "PASS", endpoint_name)

    endpoint_state = wait_until(
        lambda: control.get_agent_runtime_endpoint(
            agentRuntimeId=runtime_id, endpointName=endpoint_name
        )["status"],
        done=READY_STATES,
        failed={"CREATE_FAILED", "FAILED"},
        label=f"endpoint {endpoint_name}",
    )
    if endpoint_state not in READY_STATES:
        report.record(
            "agentcore:InvokeAgentRuntime",
            "SKIP",
            f"the endpoint settled in {endpoint_state}",
        )
        return
    report.record("endpoint reached READY", "PASS", endpoint_state)

    invoke_runtime(dataplane, runtime_arn, endpoint_name, prefix, report)


def tear_down(cleanups: list[tuple[str, Callable[[], Any]]], report: Report) -> None:
    """Delete everything the run created, newest first."""
    for label, delete in reversed(cleanups):
        try:
            delete()
            report.record(f"deleted {label}", "PASS")
        except (BotoCoreError, ClientError) as error:
            report.record(f"deleted {label}", "FAIL", str(error)[:160])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="pre-built image")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--prefix", default="wsprebuilt")
    parser.add_argument("--repo", default=None, help="destination ECR repository")
    parser.add_argument("--tag", default="verify")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the runtime, endpoint, and role in place",
    )
    parser.add_argument(
        "--no-protocol",
        action="store_true",
        help="omit protocolConfiguration from create_agent_runtime",
    )
    parser.add_argument(
        "--role-delay",
        type=int,
        default=0,
        help="seconds to wait after creating the runtime role, to test whether"
        " the ValidationException on create_agent_runtime is IAM propagation",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    ecr = session.client("ecr")
    iam = session.client("iam")
    control = session.client("bedrock-agentcore-control")
    dataplane = session.client("bedrock-agentcore")

    repo = args.repo or f"bedrock-agentcore-{args.prefix}"
    registry = f"{account}.dkr.ecr.{args.region}.amazonaws.com"
    image_uri = f"{registry}/{repo}:{args.tag}"
    role_name = f"{args.prefix}-runtime-role"

    print(f"account {account}, region {args.region}")
    print(f"copying {args.source}\n     to {image_uri}\n")

    report = Report()
    cleanups: list[tuple[str, Callable[[], Any]]] = []
    try:
        ensure_repository(ecr, repo, report)
        copy_image(args.source, image_uri, ecr, report)
        role_arn = create_runtime_role(iam, role_name, report)
        cleanups.append((f"iam role {role_name}", lambda: delete_role(iam, role_name)))
        if args.role_delay:
            print(f"      waiting {args.role_delay}s for the role to propagate")
            time.sleep(args.role_delay)
        run_agentcore(
            control,
            dataplane,
            image_uri,
            role_arn,
            args.prefix,
            cleanups,
            report,
            send_protocol=not args.no_protocol,
        )
    except (BotoCoreError, ClientError, urllib.error.HTTPError, RuntimeError) as error:
        report.record("run", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if args.keep:
            print("\n--keep: leaving resources in place")
            for label, _ in reversed(cleanups):
                print(f"      still up: {label}")
        else:
            print()
            tear_down(cleanups, report)

    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
