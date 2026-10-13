"""Recover a Vocareum lab CloudFormation failure reason without console access.

Vocareum runs ``lab.template`` inside a per-student AWS account when the student
clicks Start Lab. When the stack fails, the session is often torn down before
anyone can read the error in the AWS console, and the console itself may never
become reachable. CloudFormation keeps deleted stacks listable for 90 days, so
the failure reason is still recoverable through the AWS API alone.

This script walks six steps and prints a labeled section for each:

1. Start (or extend) the lab session through the Vocareum REST API.
2. Poll the returned transaction until it reaches a terminal state.
3. Fetch the session record, report the dollars spent against the allowance,
   and dig the temporary AWS credentials out of it.
4. List stacks in the student account, including deleted ones. After a fresh
   start, wait until a stack newer than the launch appears.
5. Read the stack events and isolate the first ``*_FAILED`` event.
6. Interpret that failure against a table of known causes.

Every response body this script prints is redacted first. The Vocareum session
payload carries the session's temporary AWS credentials, a federated console
sign-in token, and an unencrypted SSH private key, none of which belong in a
terminal transcript or a log. Access key ids stay visible, since they identify
a session rather than authorize one.

This command is part of the repository's uv project. The compatibility wrapper
and the project entry point are equivalent::

    uv run vocareum-diagnose
    uv run vocareum-diagnose --no-start
    uv run vocareum-diagnose --no-start --all-stacks --region us-west-2

Configuration comes from a ``.env`` file in the repository root, or from real
environment variables, which always win. See ``.env.example``.

Exit codes:
    0  A failure reason was retrieved, or the stack succeeded.
    1  Configuration error.
    2  The Vocareum API or AWS calls could not complete the diagnostic.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .aws import DEFAULT_REGION, denied_action
from .client import (
    CREDENTIAL_POLL_SECONDS,
    CREDENTIAL_TIMEOUT_SECONDS,
    TRANSACTION_POLL_SECONDS,
    TRANSACTION_TIMEOUT_SECONDS,
    ApiError,
    find_transaction_id,
    request_json,
    wait_for_payload,
)
from .client import (
    poll_transaction as wait_for_transaction,
)
from .config import (
    VOCAREUM_SESSION_VARS,
    ConfigError,
    Environment,
    add_course_argument,
    repository_root,
)
from .endpoints import DEFAULT_API_BASE, SessionEndpoints
from .output import dump_json, rule, section
from .security import (
    is_access_key_id,
    is_secret_access_key,
    is_session_token,
    mask_secret,
    normalize_key,
    redact_payload,
    redact_text,
)

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_API = 2

# A freshly started session does not have a stack yet. Vocareum took about 90
# seconds to produce one on 2026-07-25, so the ceiling is set well above that
# and the poll is slow, since nothing is gained by asking more often.
STACK_WAIT_POLL_SECONDS = 15
STACK_WAIT_TIMEOUT_SECONDS = 300

# CreationTime is stamped by CloudFormation's clock and the launch instant by
# this machine's. A local clock running fast would hide the new stack behind its
# own timestamp for as long as the skew lasts, so the comparison is slackened.
# The cost of the slack is only that a stack created in the two minutes before
# the launch would also qualify, and nothing creates stacks in that window.
STACK_CLOCK_SKEW_SECONDS = 120

# list_stacks returns nothing that is not named here, so a status left out is a
# stack this tool cannot see. CREATE_IN_PROGRESS has to be on the list even
# though a stack in that state has no failure to read yet: it is the state the
# stack from a fresh launch sits in for the first minute or two, and without it
# wait_for_new_stack cannot tell "the launch produced nothing" apart from "the
# launch is still working".
STACK_STATUS_FILTER = [
    "CREATE_IN_PROGRESS",
    "CREATE_FAILED",
    "CREATE_COMPLETE",
    "ROLLBACK_IN_PROGRESS",
    "ROLLBACK_FAILED",
    "ROLLBACK_COMPLETE",
    "DELETE_IN_PROGRESS",
    "DELETE_FAILED",
    "DELETE_COMPLETE",
    # Both outcomes of an update, or neither. Listing only the rollback made the
    # filter admit an update's failure and hide its success, so a stack sitting
    # in UPDATE_COMPLETE was invisible to this tool.
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
]

# Must stay a subset of STACK_STATUS_FILTER. A status listed here but not there
# is a branch that can never be taken, which reads as handled when it is not.
SUCCESS_STACK_STATUSES = frozenset(
    {"CREATE_COMPLETE", "UPDATE_COMPLETE", "DELETE_COMPLETE"}
)

# Ordered. The generic permission rule sits last so a more specific rule wins.
INTERPRETATIONS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("createtags",),
        "Tag-on-create is denied by the lab policy.",
        "Move tags out of the resource creation call.",
    ),
    # Must stay ahead of the generic permission rule below. An SCP denial also
    # carries AccessDenied, but the fix is the opposite of the generic one:
    # nothing added to lab.template can lift it. The phrase is emitted only
    # for an Organizations policy denial, never for a plain IAM denial, so it
    # is a safe discriminator.
    (
        ("explicit deny in a service control policy", "service control policy"),
        (
            "A Service Control Policy on the AWS Organization that owns the "
            "student account denies this action."
        ),
        (
            "An SCP is a permission ceiling on the account. Stop using the denied "
            "action, or ask Vocareum to change the SCP. Nothing in this "
            "repository can raise the ceiling."
        ),
    ),
    (
        ("unauthorizedoperation", "accessdenied"),
        "The lab policy is missing a permission the template needs.",
        (
            "Read the denied action name out of the message and add it to the "
            "policy, or remove the resource that needs it."
        ),
    ),
)

# The substrings above that mark an SCP denial rather than an IAM denial.
SCP_SUBSTRINGS = frozenset(
    {"explicit deny in a service control policy", "service control policy"}
)

# Support needs both of these out of the message, which is otherwise a wall of
# text: which policy to quote, and which action to ask about.
SCP_ARN_PATTERN = re.compile(
    r"arn:aws[a-z\-]*:organizations::\d{12}:policy/[A-Za-z0-9_/\-]+"
)


@dataclass(frozen=True)
class ScpDenial:
    """A Service Control Policy denial recovered from a stack failure event.

    Held separately from the other interpretations because it is the one cause
    that cannot be fixed anywhere in this repository. Both fields are optional
    because they are parsed out of an undocumented free-text message.
    """

    policy_arn: str | None
    denied_action: str | None


@dataclass(frozen=True)
class Spend:
    """Dollars this user has spent on this part, and the ceiling they spend against.

    Vocareum reports both as strings in the ``aws_data`` block of the session
    record, beside the credentials. They outlive the session: a terminated
    session empties every credential field and leaves these two populated, so
    they are readable long after the AWS side of the diagnostic stops working.
    Both are held as raw text and may be empty, since the response shape is
    undocumented and either could be absent.
    """

    path: str
    total: str
    allowed: str

    def fraction_used(self) -> float | None:
        """Return spend as a fraction of the allowance, or ``None``.

        ``None`` covers a missing figure, a non-numeric one, and an allowance of
        zero, which is how Vocareum spells "no cap configured" elsewhere in the
        part record and which would divide by zero here.
        """
        total = parse_amount(self.total)
        allowed = parse_amount(self.allowed)
        if total is None or allowed is None or allowed <= 0:
            return None
        return total / allowed


@dataclass(frozen=True)
class Config(SessionEndpoints):
    """Resolved configuration for one diagnostic run."""

    api_key: str
    region: str


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(
    env_path: Path, region_override: str | None, course: str | None = None
) -> Config:
    """Build a :class:`Config` from real environment variables and ``.env``.

    Real environment variables take precedence over ``.env`` values so the
    script works unchanged in CI. Raises :class:`ConfigError` naming every
    missing required variable at once.
    """
    environment = Environment.from_file(env_path, course)
    try:
        environment.require(VOCAREUM_SESSION_VARS)
    except ConfigError as error:
        raise ConfigError(
            f"{error}\nCopy .env.example to .env and fill in the values, "
            "or export them in your shell."
        ) from error

    region = region_override or environment.get("AWS_REGION") or DEFAULT_REGION
    api_base = (environment.get("VOCAREUM_API_BASE") or DEFAULT_API_BASE).rstrip("/")

    return Config(
        api_key=environment.get("VOCAREUM_API_KEY"),
        course_id=environment.get("VOCAREUM_COURSE_ID"),
        assignment_id=environment.get("VOCAREUM_ASSIGNMENT_ID"),
        part_id=environment.get("VOCAREUM_PART_ID"),
        user_id=environment.get("VOCAREUM_USER_ID"),
        api_base=api_base,
        region=region,
    )


# ---------------------------------------------------------------------------
# Defensive JSON searching
# ---------------------------------------------------------------------------


def parse_amount(value: str) -> float | None:
    """Parse a Vocareum dollar figure, which arrives as a string."""
    try:
        return float(value.strip())
    except (AttributeError, ValueError):
        return None


def find_credentials(
    payload: Any, path: str = "$"
) -> tuple[str, dict[str, str]] | None:
    """Recursively find a dict holding all three AWS credential fields.

    Vocareum does not document the response shape, so key names are matched
    case-insensitively and punctuation-insensitively. Returns the JSON-ish path
    where the credentials were found together with a boto3-ready mapping, or
    ``None`` when no such dict exists.
    """
    if isinstance(payload, dict):
        access_key = secret_key = token = None
        for key, value in payload.items():
            if not isinstance(value, str) or not value.strip():
                continue
            if access_key is None and is_access_key_id(key):
                access_key = value.strip()
            elif secret_key is None and is_secret_access_key(key):
                secret_key = value.strip()
            elif token is None and is_session_token(key):
                token = value.strip()
        if access_key and secret_key and token:
            return path, {
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
                "aws_session_token": token,
            }
        for key, value in payload.items():
            found = find_credentials(value, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found = find_credentials(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def find_spend(payload: Any, path: str = "$") -> Spend | None:
    """Recursively find the dict holding the spend counters.

    Matched the same defensive way as the credential fields, for the same
    reason: the response shape is undocumented, and the endpoint reference
    describes this call as returning session times and credentials without
    mentioning spend at all. Either counter alone is enough to return, since
    one figure is more useful than none.
    """
    if isinstance(payload, dict):
        total = allowed = None
        for key, value in payload.items():
            normalized = normalize_key(key)
            # A JSON null reads the same as a missing figure here, so it is
            # normalized to the empty string rather than to the text "None".
            text = "" if value is None else str(value).strip()
            if normalized == "totalspend":
                total = text
            elif normalized == "allowedspend":
                allowed = text
        if total is not None or allowed is not None:
            return Spend(path=path, total=total or "", allowed=allowed or "")
        for key, value in payload.items():
            found = find_spend(value, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found = find_spend(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# Step 1 and 2: start the session and poll the transaction
# ---------------------------------------------------------------------------


def start_session(config: Config) -> str | None:
    """Start or extend the lab session. Returns a transaction id when present.

    Vocareum documents this endpoint as: starts resources, extends the session
    if already started, launches CloudFormation templates, and issues temporary
    credentials. Those credentials, and an unencrypted SSH private key, come
    back in the response body, so the body is only ever printed redacted.
    """
    section("STEP 1: Start the lab session")
    url = config.resource_url()
    print(f"PUT {url}")

    payload = request_json("PUT", url, config.api_key)
    transaction_id = find_transaction_id(payload)

    if transaction_id is None:
        print("No transactionid in the response. Full response body with")
        print("secret values redacted:")
        print(dump_json(redact_payload(payload)))
        print("Continuing to step 3.")
        return None

    print(f"transactionid: {transaction_id}")
    return transaction_id


def poll_transaction(config: Config, transaction_id: str) -> None:
    """Poll a transaction to a terminal state and print its full message.

    A failed transaction message is one of the places the CloudFormation error
    surfaces, so it is printed in full and never truncated. It still goes
    through the text redactor, which replaces secret values only and leaves the
    diagnostic text alone.
    """
    section("STEP 2: Poll the transaction")
    url = config.transaction_url(transaction_id)
    print(f"GET {url}")
    print(
        f"Polling every {TRANSACTION_POLL_SECONDS}s up to "
        f"{TRANSACTION_TIMEOUT_SECONDS}s."
    )

    result = wait_for_transaction(
        url,
        config.api_key,
        poll_seconds=TRANSACTION_POLL_SECONDS,
        timeout_seconds=TRANSACTION_TIMEOUT_SECONDS,
        on_state=lambda state: print(f"  state={state}"),
    )
    payload = result.payload
    state = result.state
    if result.timed_out:
        print(
            f"  Still pending after {TRANSACTION_TIMEOUT_SECONDS}s. "
            "Giving up on the poll and continuing."
        )

    print(f"Final state: {state}")
    if isinstance(payload, dict):
        print(f"objid: {payload.get('objid', '(not returned)')}")
        print("Full message:")
        print(redact_text(str(payload.get("message", "(no message returned)"))))
    else:
        print("Transaction response was not a JSON object:")
        print(dump_json(redact_payload(payload)))


# ---------------------------------------------------------------------------
# Step 3: fetch credentials
# ---------------------------------------------------------------------------


def print_spend(payload: Any) -> None:
    """Print the AWS spend recorded against this session, if the record has any.

    Printed before the credentials, and outside the branch that gives up on
    them, because spend is the one part of this record that a terminated
    session still answers with.
    """
    spend = find_spend(payload)
    if spend is None:
        print("No spend counters were present on the session record.")
        return

    print(f"Spend found at {spend.path}")
    print(f"  totalspend   = {f'{spend.total} USD' if spend.total else 'absent'}")
    print(f"  allowedspend = {f'{spend.allowed} USD' if spend.allowed else 'absent'}")
    fraction = spend.fraction_used()
    if fraction is not None:
        print(f"  used         = {fraction:.4%} of the allowance")


def fetch_credentials(config: Config, *, wait: bool = False) -> dict[str, str] | None:
    """Fetch the session record, print its spend, and extract AWS credentials.

    A newly started session can return before its credentials exist. When
    ``wait`` is true, retry for the credential initialization window.

    Returns ``None`` when no credential triple is present, after printing the
    response with secret values redacted.
    """
    section("STEP 3: Fetch the session spend and AWS credentials")
    url = config.resource_url()
    print(f"GET {url}")

    payload, found = wait_for_payload(
        lambda: request_json("GET", url, config.api_key),
        find_credentials,
        poll_seconds=CREDENTIAL_POLL_SECONDS,
        timeout_seconds=CREDENTIAL_TIMEOUT_SECONDS,
        wait=wait,
        on_retry=lambda seconds: print(
            f"  credentials not ready; retrying in {seconds}s"
        ),
    )

    print_spend(payload)
    print()

    if found is None:
        print("No AWS credentials were returned by the session resource.")
        print("Response body with secret values redacted:")
        print(dump_json(redact_payload(payload)))
        return None

    path, credentials = found
    print(f"Credentials found at {path}")
    print(f"  aws_access_key_id     = {credentials['aws_access_key_id']}")
    print(
        f"  aws_secret_access_key = {mask_secret(credentials['aws_secret_access_key'])}"
    )
    print(f"  aws_session_token     = {mask_secret(credentials['aws_session_token'])}")
    return credentials


# ---------------------------------------------------------------------------
# Step 4 and 5: CloudFormation
# ---------------------------------------------------------------------------


def build_cloudformation_client(credentials: dict[str, str], region: str) -> Any:
    """Build a CloudFormation client from the session credentials."""
    try:
        return boto3.client("cloudformation", region_name=region, **credentials)
    except BotoCoreError as error:
        raise ApiError(
            f"Could not create the CloudFormation client for region {region!r}: {error}"
        ) from error


def build_ec2_client(credentials: dict[str, str], region: str) -> Any:
    """Build an EC2 client from the same guarded session credentials."""
    try:
        return boto3.client("ec2", region_name=region, **credentials)
    except BotoCoreError as error:
        raise ApiError(
            f"Could not create the EC2 client for region {region!r}: {error}"
        ) from error


# Stands in for a CreationTime that cannot be compared, so such a stack sorts
# last and is never mistaken for one created after the launch.
UNDATED = datetime.min.replace(tzinfo=UTC)


def creation_time(summary: dict[str, Any]) -> datetime:
    """Return a stack summary's CreationTime, or ``UNDATED`` when it has none.

    CloudFormation always stamps one, and always with a timezone. Anything else
    is treated as absent rather than compared, because a naive datetime raises
    against an aware one instead of merely sorting badly, which would take the
    whole diagnostic down over a field nothing important reads.
    """
    created = summary.get("CreationTime")
    if isinstance(created, datetime) and created.tzinfo is not None:
        return created
    return UNDATED


def fetch_stack_summaries(client: Any) -> list[dict[str, Any]]:
    """Return every stack in the account, newest first, including deleted ones.

    Deleted stacks stay listable for 90 days, which is what makes this recovery
    path work after Vocareum tears the session down.
    """
    summaries: list[dict[str, Any]] = []
    try:
        paginator = client.get_paginator("list_stacks")
        for page in paginator.paginate(StackStatusFilter=STACK_STATUS_FILTER):
            summaries.extend(page.get("StackSummaries", []))
    except ClientError as error:
        raise ApiError(f"list_stacks failed: {error}") from error
    except BotoCoreError as error:
        raise ApiError(f"list_stacks could not complete: {error}") from error

    summaries.sort(key=creation_time, reverse=True)
    return summaries


def created_after(summary: dict[str, Any], moment: datetime) -> bool:
    """Report whether a stack summary carries a CreationTime at or after ``moment``."""
    return creation_time(summary) >= moment


def is_settled(summary: dict[str, Any]) -> bool:
    """Report whether a stack has stopped changing.

    CloudFormation spells every transient state ``*_IN_PROGRESS``. A stack in
    one of them has no verdict to give yet: the events it has so far are the
    ones that have already succeeded.
    """
    return not str(summary.get("StackStatus", "")).upper().endswith("_IN_PROGRESS")


def wait_for_new_stack(client: Any, launched_at: datetime) -> list[dict[str, Any]]:
    """Poll until a settled stack newer than the launch exists, then list every stack.

    Without this wait the script produces a false negative that reads as
    authoritative. Vocareum's start call answers with the session record and no
    transaction id while the session is still ``Starting``, so step 2 has nothing
    to poll and returns at once, and the stack has not been created yet. Listing
    stacks at that moment finds only the previous run and reports its failure as
    though it were the result of the template that was just uploaded. That
    happened on 2026-07-25: a template that had already succeeded was reported as
    still failing on a denial the run before it had actually hit.

    Waiting for the new stack to *settle* rather than merely to appear is the
    other half of the same guarantee. A stack halfway through CREATE_IN_PROGRESS
    has no failed event yet, and reporting one that way would say the run was
    clean when nothing has been decided.

    The stack that has to be settled is specifically the newest one, because that
    is the only one ``analyze_stacks`` lets reach the verdict. Accepting any
    settled newer stack ends the wait on a stack whose events are never read:
    with a finished stack sitting behind a newer CREATE_IN_PROGRESS one, the wait
    returns satisfied and the run then reports that nothing has been decided.

    Raises :class:`ApiError` when the wait expires. An older stack may still be
    useful history, but it cannot answer what happened after this launch. The
    caller can inspect that history explicitly with ``--no-start``.
    """
    threshold = launched_at - timedelta(seconds=STACK_CLOCK_SKEW_SECONDS)
    deadline = time.monotonic() + STACK_WAIT_TIMEOUT_SECONDS
    print(
        f"A session was started, so waiting up to {STACK_WAIT_TIMEOUT_SECONDS}s "
        "for a stack created after "
        f"{threshold.isoformat(timespec='seconds')}."
    )

    while True:
        summaries = fetch_stack_summaries(client)
        newer = [summary for summary in summaries if created_after(summary, threshold)]
        if newer and is_settled(newer[0]):
            print(f"{newer[0].get('StackName')} has finished.")
            return summaries
        if time.monotonic() + STACK_WAIT_POLL_SECONDS >= deadline:
            current = (
                f"The newest candidate is {newer[0].get('StackName')} in "
                f"{newer[0].get('StackStatus')}."
                if newer
                else "No candidate stack appeared."
            )
            raise ApiError(
                f"No finished stack newer than the launch appeared within "
                f"{STACK_WAIT_TIMEOUT_SECONDS}s. {current} Refusing to report "
                "an older stack as this run's result. Re-run later, or use "
                "--no-start to inspect stack history explicitly."
            )
        if newer:
            print(
                f"  {newer[0].get('StackName')} is {newer[0].get('StackStatus')}, "
                f"retrying in {STACK_WAIT_POLL_SECONDS}s"
            )
        else:
            print(f"  nothing newer yet, retrying in {STACK_WAIT_POLL_SECONDS}s")
        time.sleep(STACK_WAIT_POLL_SECONDS)


def list_stacks(
    client: Any, launched_at: datetime | None = None
) -> list[dict[str, Any]]:
    """Print the stacks in the account, waiting for a new one after a fresh start.

    ``launched_at`` is the instant the start call returned, or ``None`` when
    ``--no-start`` skipped it and every stack on record is fair game.
    """
    section("STEP 4: Find the stack")

    if launched_at is None:
        summaries = fetch_stack_summaries(client)
    else:
        summaries = wait_for_new_stack(client, launched_at)

    if not summaries:
        print("No stacks matched the failure and deletion status filter.")
        return summaries

    print(f"{len(summaries)} stack(s) found, newest first:")
    for summary in summaries:
        print()
        print(f"  StackName    : {summary.get('StackName')}")
        print(f"  StackStatus  : {summary.get('StackStatus')}")
        print(f"  CreationTime : {summary.get('CreationTime')}")
        print(f"  StackId      : {summary.get('StackId')}")
    return summaries


def describe_stack_events(client: Any, stack_id: str) -> list[dict[str, Any]]:
    """Return every event for a stack, addressed by its StackId ARN.

    CloudFormation rejects a deleted stack by name and accepts it by ARN, so the
    caller must pass the full ``StackId``.
    """
    events: list[dict[str, Any]] = []
    try:
        paginator = client.get_paginator("describe_stack_events")
        for page in paginator.paginate(StackName=stack_id):
            events.extend(page.get("StackEvents", []))
    except ClientError as error:
        raise ApiError(
            f"describe_stack_events failed for {stack_id}: {error}"
        ) from error
    except BotoCoreError as error:
        raise ApiError(
            f"describe_stack_events could not complete for {stack_id}: {error}"
        ) from error
    return events


def print_failed_events(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Print the first failure in full and the rest as a short list.

    Events come back newest first, so they are reversed into chronological order
    before the first ``*_FAILED`` event is selected. Later failures are usually
    cascades from that first one.
    """
    chronological = list(reversed(events))
    failures = [
        event
        for event in chronological
        if str(event.get("ResourceStatus", "")).upper().endswith("FAILED")
    ]

    if not failures:
        print(f"No FAILED events among {len(events)} event(s) for this stack.")
        return None

    first = failures[0]
    print("FIRST FAILURE (unmodified, untruncated):")
    print(f"  Timestamp            : {first.get('Timestamp')}")
    print(f"  LogicalResourceId    : {first.get('LogicalResourceId')}")
    print(f"  ResourceType         : {first.get('ResourceType')}")
    print(f"  ResourceStatus       : {first.get('ResourceStatus')}")
    print(f"  ResourceStatusReason : {first.get('ResourceStatusReason')}")

    remaining = failures[1:]
    print()
    if not remaining:
        print("No further failed events. This was the only failure.")
        return first

    print(f"{len(remaining)} later failed event(s), usually cascades from the first:")
    for event in remaining:
        print(
            f"  - {event.get('Timestamp')} "
            f"{event.get('LogicalResourceId')} "
            f"{event.get('ResourceStatus')}: "
            f"{event.get('ResourceStatusReason')}"
        )
    return first


def print_current_in_progress_events(events: list[dict[str, Any]]) -> None:
    """Print resources whose newest event still has an in-progress status."""
    seen: set[str] = set()
    pending: list[dict[str, Any]] = []
    for event in events:
        logical_id = str(event.get("LogicalResourceId", ""))
        if not logical_id or logical_id in seen:
            continue
        seen.add(logical_id)
        status = str(event.get("ResourceStatus", "")).upper()
        if status.endswith("_IN_PROGRESS"):
            pending.append(event)

    if not pending:
        print("No resource has an in-progress newest event.")
        return

    print("Resources still in progress:")
    for event in pending:
        print(
            f"  - {event.get('LogicalResourceId')} "
            f"{event.get('ResourceStatus')} "
            f"({event.get('ResourceType')})"
        )


def decode_console_output(value: str) -> str:
    """Decode the base64 text returned by EC2 console output."""
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return value
    return decoded.decode("utf-8", errors="replace")


def print_ec2_console_output(client: Any, instance_id: str, lines: int = 100) -> None:
    """Print the final console lines before Vocareum revokes failed-session access."""
    print()
    print(f"EC2 console output tail for {instance_id}:")
    try:
        response = client.get_console_output(InstanceId=instance_id, Latest=True)
    except (ClientError, BotoCoreError) as error:
        print(f"  Console output could not be read: {error}")
        return

    output = decode_console_output(str(response.get("Output", "")))
    selected = output.splitlines()[-lines:]
    if not selected:
        print("  No console output was returned.")
        return
    for line in selected:
        print(f"  {redact_text(line)}")


# ---------------------------------------------------------------------------
# Step 6: interpretation
# ---------------------------------------------------------------------------


def parse_scp_denial(reason: str) -> ScpDenial:
    """Pull the policy ARN and the denied action out of an SCP denial message.

    Both are buried in a single long sentence, and both are what Vocareum
    support needs quoted back at them, so they are lifted out rather than left
    for the reader to find.
    """
    arn_match = SCP_ARN_PATTERN.search(reason)
    return ScpDenial(
        policy_arn=arn_match.group(0) if arn_match else None,
        denied_action=denied_action(reason),
    )


def interpret_failure(reason: str | None) -> ScpDenial | None:
    """Match a status reason against known lab causes and print the fix.

    Returns the parsed denial when the cause was a Service Control Policy, so
    the caller can repeat it in the RESULT section, and ``None`` otherwise.
    """
    section("STEP 6: Interpret the failure")

    if not reason:
        print("The first failed event carried no ResourceStatusReason.")
        print("Nothing to interpret. Check the stack events printed above.")
        return None

    haystack = reason.lower()
    for substrings, cause, fix in INTERPRETATIONS:
        matched = next((text for text in substrings if text in haystack), None)
        if matched is not None:
            print(f"Matched on: {matched!r}")
            print(f"Cause     : {cause}")
            print(f"Fix       : {fix}")
            if matched not in SCP_SUBSTRINGS:
                return None
            denial = parse_scp_denial(reason)
            print(f"Action    : {denial.denied_action or '(not named in the message)'}")
            print(f"SCP ARN   : {denial.policy_arn or '(not named in the message)'}")
            return denial

    print("No known cause matched this failure reason.")
    print("Raw reason:")
    print(reason)
    return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def analyze_stacks(
    client: Any,
    summaries: list[dict[str, Any]],
    all_stacks: bool,
    ec2_client: Any | None = None,
) -> tuple[bool, ScpDenial | None]:
    """Read events for the selected stacks and interpret the newest failure.

    The most recent stack is analyzed by default. Every stack is analyzed when
    ``all_stacks`` is set or when three or fewer stacks were found. Returns
    whether a failure reason was retrieved or a stack succeeded, together with
    the newest stack's Service Control Policy denial, if it had one.

    Only the newest stack decides. The question this script answers is what
    happened on this run, and one stack answers it. Older stacks are still read
    and printed, because their history is worth having, but they cannot reach
    the verdict.
    """
    section("STEP 5: Read the stack events")

    selected = summaries if all_stacks or len(summaries) <= 3 else summaries[:1]
    print(f"Analyzing {len(selected)} of {len(summaries)} stack(s).")

    diagnosed = False
    scp_denial: ScpDenial | None = None
    # Summaries arrive newest first, so position 0 is the stack this run
    # produced and everything after it is history.
    for position, summary in enumerate(selected):
        stack_id = str(summary.get("StackId", ""))
        status = str(summary.get("StackStatus", ""))
        role = (
            "newest, the verdict comes from this one"
            if position == 0
            else "older, context only"
        )
        print()
        rule()
        print(f"Stack: {summary.get('StackName')} ({status})")
        print(f"CreationTime: {summary.get('CreationTime')}  [{role}]")
        print(f"StackId ARN: {stack_id}")
        rule()

        events = describe_stack_events(client, stack_id)
        first = print_failed_events(events)

        if position > 0:
            # An SCP denial on a stack that was deleted days ago is not what
            # happened on this run. Letting one reach the RESULT reported a
            # current CREATE_COMPLETE as an SCP block on 2026-07-27.
            continue

        if first is not None:
            diagnosed = True
            instance_id = str(first.get("PhysicalResourceId", ""))
            if (
                ec2_client is not None
                and first.get("ResourceType") == "AWS::EC2::Instance"
                and instance_id.startswith("i-")
            ):
                print_ec2_console_output(ec2_client, instance_id)
            scp_denial = interpret_failure(first.get("ResourceStatusReason"))
        elif status in SUCCESS_STACK_STATUSES:
            diagnosed = True
            print(f"Stack reached {status} with no failed events.")
        elif not is_settled(summary):
            print_current_in_progress_events(events)
            print(
                f"Stack is still {status}, so nothing has been decided yet. "
                "Re-run once it settles."
            )

    return diagnosed, scp_denial


def run_diagnostic(config: Config, args: argparse.Namespace) -> int:
    """Run every step in order and return the process exit code."""
    print(f"Vocareum API base : {config.api_base}")
    print(f"Course/Assignment : {config.course_id}/{config.assignment_id}")
    print(f"Part/User         : {config.part_id}/{config.user_id}")
    print(f"AWS region        : {config.region}")

    # Captured before the call rather than after it, so a stack CloudFormation
    # started while the request was still in flight still counts as newer.
    launched_at: datetime | None = None

    if args.no_start:
        section("STEP 1 and 2: Skipped")
        print("--no-start was passed. Not launching or extending a lab session.")
    else:
        launched_at = datetime.now(UTC)
        transaction_id = start_session(config)
        if transaction_id is not None:
            poll_transaction(config, transaction_id)

    credentials = fetch_credentials(config, wait=not args.no_start)
    if credentials is None:
        section("RESULT")
        print(
            "Credentials were not returned, so the AWS side of the diagnostic "
            "cannot run.\n"
            "The session has most likely already been torn down. Start a fresh "
            "lab session by running this script without --no-start, then run it "
            "again while the session is still active. Deleted stacks stay "
            "listable for 90 days, so a new session in the same account can "
            "still read the old failure."
        )
        return EXIT_API

    client = build_cloudformation_client(credentials, config.region)
    ec2_client = build_ec2_client(credentials, config.region)
    summaries = list_stacks(client, launched_at)

    if not summaries:
        section("RESULT")
        print(
            "No stacks were found in this account, so no failure reason could "
            "be retrieved.\n"
            "Check that --region matches the region the lab deploys into, and "
            "that the credentials belong to the student account that ran the "
            "template."
        )
        return EXIT_API

    diagnosed, scp_denial = analyze_stacks(
        client, summaries, args.all_stacks, ec2_client
    )

    section("RESULT")
    if scp_denial is not None:
        print(
            "Blocked by a Service Control Policy. This one cannot be fixed in "
            "this repository.\n\n"
            "The SCP is attached in the AWS Organization that owns the student "
            "account, which is Vocareum's, not ours. It sets a ceiling on what "
            "the account is allowed to do. Editing lab.template or creating an "
            "IAM role inside the student account cannot raise that ceiling, so "
            "neither is worth trying."
        )
        print()
        unknown = "(see the event above)"
        print(f"  Denied action : {scp_denial.denied_action or unknown}")
        print(f"  Policy ARN    : {scp_denial.policy_arn or unknown}")
        print()
        print(
            "There are two resolutions. Stop using the denied action, or ask "
            "Vocareum to change the SCP. Quote the policy ARN above on the "
            "ticket, since it names the organization, the management account, "
            "and the policy they need to edit."
        )
        return EXIT_OK
    if diagnosed:
        print("Diagnostic complete. See the sections above.")
        return EXIT_OK
    print(
        "Stacks were found but none carried a failed event or a success "
        "status, so no failure reason could be retrieved.\n"
        "Re-run with --all-stacks to widen the search."
    )
    return EXIT_API


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Recover the CloudFormation failure reason from a Vocareum AWS lab "
            "session without AWS console access."
        ),
        epilog=(
            "Configuration is read from .env in the repository root. Real "
            "environment variables take precedence. See .env.example."
        ),
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help=(
            "Skip steps 1 and 2 and go straight to fetching credentials. Use "
            "this when the session is already running or to avoid burning a "
            "fresh lab launch."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=f"Override AWS_REGION. Default: {DEFAULT_REGION}",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help="Override the default .env location (repository root).",
    )
    add_course_argument(parser)
    parser.add_argument(
        "--all-stacks",
        action="store_true",
        help="Read events for every stack found rather than only the newest.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. See the module docstring for exit code meanings."""
    args = parse_args(argv)

    default_env = repository_root() / ".env"
    env_path = Path(args.env_file).expanduser() if args.env_file else default_env

    try:
        config = load_config(env_path, args.region, args.course)
    except ConfigError as error:
        print("Configuration error.")
        print(error)
        return EXIT_CONFIG

    try:
        return run_diagnostic(config, args)
    except ApiError as error:
        section("RESULT")
        print(f"The diagnostic could not complete: {error}")
        return EXIT_API


if __name__ == "__main__":
    raise SystemExit(main())
