"""Measure Bedrock entitlement across the Vocareum AWS account pool.

Bedrock answers an invocation in a non-entitled Vocareum account with
``ValidationException: Error 002: Access to Bedrock models is not allowed for
this account``. Measured 2026-08-25 in one pool account: the request
reaches the real ``bedrock-runtime.us-east-1.amazonaws.com`` and comes back
with ``x-amzn-ErrorType: ValidationException:...com.amazon.bedrock/``, so the
refusal is Bedrock's own account-level check and not an SCP, an IAM shortfall,
a proxy, or a missing model agreement. In that same account every read API
reports the workshop's three models as ``AUTHORIZED``, ``AVAILABLE``, and
``agreementAvailability AVAILABLE``.

**No read API predicts Error 002.** The only test is a real invocation, and an
invocation needs a session, and a session lands in whichever pool member
Vocareum allocated. So answering "which accounts are entitled" means cycling
sessions, which is what this module does: start, discover the account, invoke
once, end, repeat. Ending a session returns its member to the pool and the next
start lands somewhere else, which is the rotation this walks.

It exists because the alternative was clicking Student View once per account.
Six accounts had been measured that way by 2026-08-25 and exactly one of them
invoked successfully, which is a sample too small to tell "this course is
entitled" from "one account got lucky". That distinction decides whether the
fix is a release retarget or a Vocareum support ticket, so it is worth
measuring properly.

**Every start costs one launch against the budget.** A dry run is the default
and reads the pool without spending anything. ``--yes`` is what spends.

Two things this deliberately does not do:

**It does not run the account guard.** ``probe.guard_account`` refuses unless
STS matches ``LAB_AWS_ACCOUNT_ID``, and a tool whose whole job is to find out
which account it landed in cannot assert the answer in advance. The pin is
ignored here. What replaces it is the credential guard, which still refuses an
empty triple so boto3 can never fall back to ambient credentials, plus an
identity check that the ARN really is an ``assumed-role/voclabs`` session. Those
two together mean a wrong account can only ever be a different lab account,
which is the thing being enumerated.

**It waits for the lab stack before it invokes.** The first live pass measured
one account two seconds after Vocareum accepted the start and got
``AccessDeniedException ... no identity-based policy allows
bedrock:InvokeModel``. Six minutes later the same account, the same model, and
the same session answered Error 002. Credentials appear the moment a member is
allocated, but the ``bedrock:InvokeModel`` grant is an ``AWS::IAM::RolePolicy``
inside ``lab.template`` and does not exist until CloudFormation applies it, so
an invocation before then measures the stack's progress and not the account's
entitlement. At scale that would spend the whole launch budget on rounds that
all answer ``IAM_DENY``. The stack is discovered with ``ListStacks`` rather than
read from ``LAB_STACK_NAME``, for the reason ``refresh.py`` discovers it too:
the pin names the previous rotation's account and the scan is what rotates.
Measured 2026-08-25: the ``voclabs`` session role is allowed
both ``cloudformation:ListStacks`` and ``cloudformation:DescribeStacks``. A
refusal is not fatal, because an account that cannot be watched can still be
invoked; the round says so and probes anyway.

**It does not teach ``probe.classify`` about Error 002.** ``classify`` maps
every ``ValidationException`` to ``INCONCLUSIVE_BAD_INPUT``, on the sound
general rule that a request-shape rejection is not evidence about
authorization. Error 002 is the exception: it is an entitlement answer wearing
a validation error code. Correcting that belongs in ``classify`` and in the
``lab-probe.template`` inline copy together, under ``test_template_drift``, so
this module recognizes the string locally and leaves the shared vocabulary
alone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from .client import ApiError, request_json
from .config import (
    VOCAREUM_SESSION_VARS,
    ConfigError,
    Environment,
    add_course_argument,
    repository_root,
)
from .endlab import END_ACTION, read_facts
from .endpoints import DEFAULT_API_BASE
from .output import section
from .probe import (
    ACCOUNT_VAR,
    BOTO_CONFIG,
    REQUIRED_REGION,
    Config,
    GuardError,
    Verdict,
    build_session,
    classify,
    clip,
    fetch_vocareum_credentials,
    guard_credentials,
    start_session,
)
from .security import redact_text

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_GUARD = 2

# The model the invocation probe uses. Chosen because it is the model the
# workshop's first Bedrock call actually uses and because it resolved for
# Converse in a non-entitled account, so a failure is the entitlement answer
# rather than an unknown-model answer. Overridable, because the id a course
# pins can move.
DEFAULT_PROBE_MODEL = "anthropic.claude-sonnet-4-6"

# The IAM action the probe names, written as an IAM action rather than a boto3
# method so a result lines up with the permission tracker without translation.
PROBE_ACTION = "bedrock:InvokeModel"

# The substring that identifies the account-level entitlement refusal. Matched
# on the message body because the error code is a plain ValidationException and
# says nothing.
ERROR_002_MARKER = "error 002"

# What a Vocareum student session's identity looks like. The account guard is
# not run here, so this is the check that the credentials came from a lab
# session at all rather than from somewhere on the operator's machine.
SESSION_ROLE_MARKER = "assumed-role/voclabs/"

# How long to wait for a session to actually go down after the end call. The
# end call is synchronous and answers 200, but a 200 says the call was accepted
# and the next start has to land on a reallocated member, not the one just
# released.
TEARDOWN_TIMEOUT_SECONDS = 300
TEARDOWN_POLL_SECONDS = 10

# How long to let CloudFormation finish before giving up on a round. The one
# measured build took a little under six minutes from start to the grant
# landing, so the default leaves room for a slow one without letting a wedged
# stack hold the whole scan.
STACK_TIMEOUT_SECONDS = 900
STACK_POLL_SECONDS = 15

# CloudFormation statuses that end a wait. Anything not listed is still moving.
STACK_READY_STATUSES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})
STACK_DEAD_STATUSES = frozenset(
    {
        "CREATE_FAILED",
        "ROLLBACK_IN_PROGRESS",
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_COMPLETE",
        "UPDATE_ROLLBACK_FAILED",
        "DELETE_FAILED",
    }
)
# Stacks on their way out are not this session's lab and must not be waited on.
STACK_IGNORED_STATUSES = frozenset({"DELETE_COMPLETE", "DELETE_IN_PROGRESS"})

# Vocareum refuses a start while the member it would allocate is being wiped:
# ``HTTP 400 ... "Failed operation: Start. account is still in cleanup, expect
# another 5 minutes"``. Measured 2026-08-25 on a course whose pool held
# three members, so ending one left every remaining member in cleanup and the
# next start was refused three times in a row. A refused start is not a start
# and costs no launch, so waiting it out is free. The defaults clear the five
# minutes Vocareum names, with room over.
CLEANUP_MARKER = "still in cleanup"
CLEANUP_RETRIES = 8
CLEANUP_WAIT_SECONDS = 60

# Vocareum refuses an end while it still considers the lab to be building:
# ``HTTP 400 ... "Failed operation: End. Lab is setting up. Please wait for it
# to finish"``. Measured 2026-08-25, and the notable part is
# when: CloudFormation had already
# reported ``CREATE_COMPLETE``, the Bedrock probe had already run, and Vocareum
# still called the lab unfinished. Vocareum's readiness therefore lags the
# stack's, so waiting for the stack does not make the end safe and the retry
# has to exist on its own.
#
# Retrying this one is not free the way retrying a start is. The launch is
# already spent and the session is already open, so the cost of getting it
# wrong is a leaked pool member rather than a wasted attempt, which is why the
# scan raises a message naming the account instead of moving on quietly.
SETUP_MARKER = "lab is setting up"
SETUP_RETRIES = 5
SETUP_WAIT_SECONDS = 30

# Where the audit log lands by default. ``support/`` is git-ignored and is
# already where this repository keeps evidence that names real accounts, which
# is exactly what this file is.
DEFAULT_LOG_DIR = "support"
LOG_PREFIX = "poolscan"


class Entitlement(StrEnum):
    """What one invocation in one account proved about Bedrock there."""

    ENTITLED = "ENTITLED"
    ERROR_002 = "ERROR_002"
    OTHER = "OTHER"
    # The lab stack never came up, so the Bedrock grant never landed and the
    # round proved nothing about the account. Kept apart from OTHER because a
    # scan under time pressure needs to see at a glance which launches were
    # spent on a broken build rather than on an answer.
    STACK_FAILED = "STACK_FAILED"


@dataclass(frozen=True)
class Identity:
    """Who the session credentials turned out to be."""

    account: str
    arn: str


@dataclass
class Observation:
    """One account measured once."""

    round_index: int
    account: str
    entitlement: Entitlement
    error_code: str = ""
    request_id: str = ""
    detail: str = ""
    verdict: str = ""
    started_by_scan: bool = False
    stack: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready mapping."""
        payload = asdict(self)
        payload["entitlement"] = str(self.entitlement)
        return payload


@dataclass
class ScanState:
    """What the scan has learned so far, across rounds."""

    observations: list[Observation] = field(default_factory=list)

    @property
    def accounts(self) -> list[str]:
        """Return the distinct accounts seen, in the order first seen."""
        seen: list[str] = []
        for observation in self.observations:
            if observation.account not in seen:
                seen.append(observation.account)
        return seen

    @property
    def measured_accounts(self) -> list[str]:
        """Return the accounts an invocation actually reached.

        ``--limit`` counts these rather than every account seen. A round whose
        stack never came up spent a launch and learned nothing, so letting it
        satisfy the limit would end the scan holding fewer answers than it was
        asked for.
        """
        return [
            account
            for account in self.accounts
            if self.verdicts_for(account) - {Entitlement.STACK_FAILED}
        ]

    def verdicts_for(self, account: str) -> set[Entitlement]:
        """Return every entitlement answer recorded for one account."""
        return {
            observation.entitlement
            for observation in self.observations
            if observation.account == account
        }

    def unstable(self) -> list[str]:
        """Return accounts that answered two different ways.

        This is the finding the scan exists to rule in or out. An account that
        answers both ways means entitlement is not a property of the account,
        which would make a per-account support ticket the wrong ask.
        """
        return [
            account
            for account in self.accounts
            if len(self.verdicts_for(account) - {Entitlement.STACK_FAILED}) > 1
        ]


def utc_stamp() -> str:
    """Return the current time as a second-precision UTC timestamp."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_log_path(root: Path) -> Path:
    """Return a fresh audit-log path under ``support/``.

    Timestamped rather than fixed, because two scans a day apart are two pieces
    of evidence and the second overwriting the first is how a measured account
    stops being measured.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return root / DEFAULT_LOG_DIR / f"{LOG_PREFIX}-{stamp}.jsonl"


@dataclass
class AuditLog:
    """An append-only JSONL record of everything one scan did.

    JSONL, flushed on every write, rather than one JSON document written at the
    end. This loop spends launches and gives up accounts, so a run that dies in
    round four has already spent four launches and the evidence they bought is
    the only thing that stops them being wasted. A single JSON array is exactly
    the shape that loses it, and losing it means paying again.

    Session starts and session ends are recorded, not only the measurements.
    Those two calls are the ones that cost budget and release an account, so an
    audit that cannot show them is not an audit. The refusal path is recorded
    too, because "the scan stopped without measuring this account" is a fact
    someone reading the file later needs.
    """

    path: Path | None
    handle: TextIO | None = None
    run_id: str = field(default_factory=lambda: uuid4().hex)

    def write(self, event: str, **fields: Any) -> None:
        """Append one event. A closed or disabled log silently records nothing."""
        if self.handle is None:
            return
        record = {
            "ts": utc_stamp(),
            "runId": self.run_id,
            "event": event,
            **fields,
        }
        self.handle.write(json.dumps(record, sort_keys=False) + "\n")
        self.handle.flush()


@contextmanager
def open_audit_log(path: Path | None) -> Iterator[AuditLog]:
    """Open the audit log, creating its directory, and close it on the way out.

    ``path`` of ``None`` yields a log that discards, so every call site can
    write unconditionally and ``--no-log`` needs no branches anywhere else.
    """
    if path is None:
        yield AuditLog(path=None)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        log = AuditLog(path=path, handle=handle)
        try:
            yield log
        finally:
            log.handle = None


def load_config(
    env_path: Path, region_override: str | None, course: str | None = None
) -> Config:
    """Build a :class:`probe.Config` without requiring the account pin.

    ``probe.load_config`` refuses when ``LAB_AWS_ACCOUNT_ID`` is unset, because
    everything else built on it reports about one named account. This scan
    reports about whichever account it reached, so the pin is read when present
    and never compared. Requiring it would make the tool refuse in exactly the
    state it is meant to resolve, which is a rotated pool with a stale pin.
    """
    environment = Environment.from_file(env_path, course)
    try:
        environment.require(VOCAREUM_SESSION_VARS)
    except ConfigError as error:
        raise ConfigError(
            f"{error}\nCopy .env.example to .env and fill in the values, "
            "or export them in your shell."
        ) from error

    return Config(
        api_key=environment.get("VOCAREUM_API_KEY"),
        course_id=environment.get("VOCAREUM_COURSE_ID"),
        assignment_id=environment.get("VOCAREUM_ASSIGNMENT_ID"),
        part_id=environment.get("VOCAREUM_PART_ID"),
        user_id=environment.get("VOCAREUM_USER_ID"),
        # Read so it can be printed for context. Never compared: see the module
        # docstring on why the account guard cannot apply here.
        expected_account=environment.get(ACCOUNT_VAR),
        api_base=(environment.get("VOCAREUM_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
        region=region_override or environment.get("AWS_REGION") or REQUIRED_REGION,
    )


def fetch_facts(config: Config) -> Any:
    """Read the session resource and parse the facts out of it."""
    return read_facts(request_json("GET", config.resource_url(), config.api_key))


def end_session(config: Config) -> Any:
    """Send the synchronous end call. No transaction to poll."""
    return request_json(
        "DELETE", config.resource_url(), config.api_key, body={"action": END_ACTION}
    )


def wait_until_down(config: Config) -> bool:
    """Poll until the session reports itself down, and say whether it did.

    The end call answers 200 immediately. Starting again before Vocareum has
    finished releasing the member is what would land the next round back in the
    same account and make the scan report a pool of one.
    """
    deadline = time.monotonic() + TEARDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not fetch_facts(config).live:
            print("  Session is down. The member has been released.")
            return True
        print(f"  Still up; re-reading in {TEARDOWN_POLL_SECONDS}s")
        time.sleep(TEARDOWN_POLL_SECONDS)
    print(
        f"  Still up after {TEARDOWN_TIMEOUT_SECONDS}s. Stopping rather than "
        "starting again, because the next start would probably land in the "
        "same member and be recorded as a second account."
    )
    return False


def discover_identity(session: Any, region: str) -> Identity:
    """Read who these credentials are, and refuse anything but a lab session.

    This is what stands in for the account guard. The guard exists to stop a
    report about the wrong account, and the failure it was built for is boto3
    silently falling back to the operator's own ambient credentials. The
    credential guard already blocks the empty triple that causes that; this
    blocks the case where a triple is present but is not a student session, so
    the only accounts this can ever report on are lab accounts.
    """
    try:
        sts = session.client("sts", region_name=region, config=BOTO_CONFIG)
        identity = sts.get_caller_identity()
    except (ClientError, BotoCoreError, NoCredentialsError) as error:
        raise GuardError(f"sts:GetCallerIdentity failed: {error}") from error

    account = str(identity.get("Account", "")).strip()
    arn = str(identity.get("Arn", "")).strip()
    print(f"  Account  = {account}")
    print(f"  Identity = {arn}")

    if not re.fullmatch(r"\d{12}", account):
        raise GuardError(
            f"sts:GetCallerIdentity returned an unusable account id: {account!r}."
        )
    if SESSION_ROLE_MARKER not in arn:
        raise GuardError(
            "Identity guard refused the run. These credentials are not a "
            f"Vocareum student session: expected {SESSION_ROLE_MARKER!r} in "
            f"the ARN, got {arn!r}.\nThe account guard is deliberately not run "
            "by this tool, so this is the only thing standing between the scan "
            "and reporting Bedrock entitlement for some account that is not a "
            "lab account at all. Nothing is reported."
        )
    print("  Identity guard passed. This is a Vocareum student session.")
    return Identity(account=account, arn=arn)


def probe_bedrock(session: Any, region: str, model_id: str) -> Observation:
    """Invoke one model once and classify what came back.

    The account and round are filled in by the caller. Only the entitlement
    answer is decided here.
    """
    print(f"  {PROBE_ACTION} via Converse on {model_id}")
    try:
        client = session.client(
            "bedrock-runtime", region_name=region, config=BOTO_CONFIG
        )
        client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1},
        )
    except ClientError as error:
        return classify_bedrock_error(error, model_id)
    except (BotoCoreError, NoCredentialsError) as error:
        return Observation(
            round_index=0,
            account="",
            entitlement=Entitlement.OTHER,
            detail=clip(f"{type(error).__name__}: {error}"),
        )
    print("  ENTITLED. The invocation returned.")
    return Observation(
        round_index=0,
        account="",
        entitlement=Entitlement.ENTITLED,
        verdict=str(Verdict.OK),
    )


def classify_bedrock_error(error: ClientError, model_id: str) -> Observation:
    """Turn one ``ClientError`` into an entitlement answer.

    Error 002 is matched on the message body first, because its error code is a
    plain ``ValidationException`` that carries no signal. Everything else is
    handed to ``probe.classify`` so an IAM shortfall, an SCP ceiling, or a
    model that is simply not in this Region says so rather than being filed as
    an entitlement failure.
    """
    info = error.response.get("Error", {})
    code = str(info.get("Code", "Unknown"))
    message = str(info.get("Message", str(error)))
    request_id = str(error.response.get("ResponseMetadata", {}).get("RequestId", ""))

    if ERROR_002_MARKER in message.lower():
        print(f"  ERROR_002. Bedrock refused the account. RequestId {request_id}")
        return Observation(
            round_index=0,
            account="",
            entitlement=Entitlement.ERROR_002,
            error_code=code,
            request_id=request_id,
            detail=clip(redact_text(message)),
        )

    verdict, _, _ = classify(error, PROBE_ACTION)
    print(f"  OTHER. {code}: {clip(redact_text(message), 160)}")
    print(f"    probe.classify says {verdict}. Model was {model_id}.")
    return Observation(
        round_index=0,
        account="",
        entitlement=Entitlement.OTHER,
        error_code=code,
        request_id=request_id,
        detail=clip(redact_text(message)),
        verdict=str(verdict),
    )


@dataclass(frozen=True)
class StackWait:
    """What watching CloudFormation settled before the invocation ran."""

    outcome: str
    detail: str = ""

    READY = "ready"
    FAILED = "failed"
    TIMEOUT = "timeout"
    UNREADABLE = "unreadable"

    @property
    def can_measure(self) -> bool:
        """Whether an invocation now would answer the entitlement question.

        ``UNREADABLE`` counts. An account that refuses ``ListStacks`` can still
        be invoked, and a round that probes anyway and reports what it saw is
        worth more than a round that refuses to look.
        """
        return self.outcome in (StackWait.READY, StackWait.UNREADABLE)


def live_stacks(client: Any) -> list[tuple[str, str]]:
    """Return ``(name, status)`` for every stack that is not on its way out."""
    rows: list[tuple[str, str]] = []
    paginator = client.get_paginator("list_stacks")
    for page in paginator.paginate():
        for summary in page.get("StackSummaries", []):
            status = str(summary.get("StackStatus", ""))
            if status in STACK_IGNORED_STATUSES:
                continue
            rows.append((str(summary.get("StackName", "")), status))
    return rows


def wait_for_stack(
    session: Any, region: str, *, timeout_seconds: int, poll_seconds: int
) -> StackWait:
    """Block until the lab stack finishes, or until it is clear it will not.

    The stack is discovered rather than named. ``LAB_STACK_NAME`` pins the
    previous rotation's account and this scan is what rotates, so a pin would
    be wrong on every round after the first.

    A build that fails ends the wait immediately instead of burning the
    timeout, which is the whole reason this watches CloudFormation rather than
    retrying the invocation until the grant appears.
    """
    client = session.client("cloudformation", region_name=region, config=BOTO_CONFIG)
    deadline = time.monotonic() + timeout_seconds
    seen = "none yet"
    while True:
        try:
            rows = live_stacks(client)
        except ClientError as error:
            verdict, code, _ = classify(error, "cloudformation:ListStacks")
            print(f"  Cannot watch the stack: {code} ({verdict}). Probing anyway.")
            return StackWait(StackWait.UNREADABLE, detail=f"{code}: {verdict}")
        except (BotoCoreError, NoCredentialsError) as error:
            detail = f"{type(error).__name__}: {error}"
            print(f"  Cannot watch the stack: {clip(detail, 160)}. Probing anyway.")
            return StackWait(StackWait.UNREADABLE, detail=clip(detail))

        dead = [row for row in rows if row[1] in STACK_DEAD_STATUSES]
        if dead:
            names = ", ".join(f"{name} {status}" for name, status in dead)
            print(f"  Stack failed: {names}")
            return StackWait(StackWait.FAILED, detail=clip(names))

        if rows and all(status in STACK_READY_STATUSES for _, status in rows):
            names = ", ".join(f"{name} {status}" for name, status in rows)
            print(f"  Stack is up: {names}")
            return StackWait(StackWait.READY, detail=clip(names))

        seen = (
            ", ".join(f"{name} {status}" for name, status in rows)
            if rows
            else "no stack yet"
        )
        if time.monotonic() >= deadline:
            print(f"  Stack did not finish in {timeout_seconds}s. Last saw: {seen}")
            return StackWait(StackWait.TIMEOUT, detail=clip(seen))
        print(f"  {seen}; re-reading in {poll_seconds}s")
        time.sleep(poll_seconds)


def measure_current_session(
    config: Config, args: argparse.Namespace, *, wait: bool
) -> Observation:
    """Run the guards against the open session and probe Bedrock in it."""
    section("MEASURE 1: credentials")
    credentials = fetch_vocareum_credentials(config, wait=wait)
    guard_credentials(credentials)

    section("MEASURE 2: identity")
    session = build_session(credentials)
    identity = discover_identity(session, config.region)

    section("MEASURE 3: region")
    print(f"  Region = {config.region}")
    if config.region != REQUIRED_REGION:
        raise GuardError(
            f"Region guard refused the run. This scan requires {REQUIRED_REGION}. "
            f"Measuring Bedrock entitlement in {config.region} would answer a "
            "question about a Region the workshop does not run in."
        )
    print(f"  Region guard passed. {REQUIRED_REGION}.")

    section("MEASURE 4: stack")
    stack = wait_for_stack(
        session,
        config.region,
        timeout_seconds=args.stack_timeout,
        poll_seconds=args.stack_poll,
    )
    if not stack.can_measure:
        print("  Not invoking. Without the stack there is no Bedrock grant to test.")
        return Observation(
            round_index=0,
            account=identity.account,
            entitlement=Entitlement.STACK_FAILED,
            detail=clip(f"stack {stack.outcome}: {stack.detail}"),
        )

    section("MEASURE 5: invoke")
    observation = probe_bedrock(session, config.region, args.model)
    observation.account = identity.account
    observation.stack = stack.outcome
    return observation


def is_cleanup_refusal(error: ApiError) -> bool:
    """Whether Vocareum refused a start only because the pool is still wiping.

    Matched on the message body. The status is a plain ``400``, which every
    other malformed or unauthorized request also returns, so the code carries
    no signal and the string is all there is.
    """
    return CLEANUP_MARKER in str(error).lower()


def start_session_with_retry(
    config: Config,
    log: AuditLog,
    round_index: int,
    *,
    retries: int,
    wait_seconds: int,
) -> None:
    """Start a session, waiting out a pool that has not finished cleaning up.

    Only the cleanup refusal is retried. Every other ``ApiError`` is raised
    immediately, because a wrong part, a rotated token, or an expired
    enrollment does not become true by asking again and a scan that kept
    retrying one would look like a slow pool instead of a broken config.
    """
    for attempt in range(1, retries + 2):
        log.write("session_start_requested", round=round_index, attempt=attempt)
        try:
            start_session(config)
        except ApiError as error:
            if not is_cleanup_refusal(error):
                raise
            if attempt > retries:
                raise ApiError(
                    f"Vocareum refused to start a session {attempt} times, "
                    "every time because the pool was still cleaning up. "
                    f"Waited {wait_seconds}s between attempts and spent no "
                    "launch. Either the pool holds fewer members than the "
                    "scan is asking for, or one is wedged in cleanup."
                ) from error
            log.write(
                "session_start_deferred",
                round=round_index,
                attempt=attempt,
                remaining=retries - attempt + 1,
                error=clip(str(error), 500),
            )
            print(
                f"  The pool is still cleaning up, so no launch was spent. "
                f"Waiting {wait_seconds}s and trying again "
                f"(attempt {attempt} of {retries + 1})."
            )
            time.sleep(wait_seconds)
            continue
        log.write("session_start_accepted", round=round_index, attempt=attempt)
        return


def is_setup_refusal(error: ApiError) -> bool:
    """Whether Vocareum refused an end only because it still calls the lab new.

    Matched on the message body, for the same reason ``is_cleanup_refusal``
    matches on the body: the status is a bare ``400`` that every other refusal
    also returns.
    """
    return SETUP_MARKER in str(error).lower()


def end_session_with_retry(
    config: Config,
    log: AuditLog,
    round_index: int,
    account: str,
    *,
    retries: int,
    wait_seconds: int,
) -> None:
    """End a session, waiting out a lab Vocareum still considers unfinished.

    Only the setup refusal is retried, and on exhaustion the error names the
    account. The session is open and the launch is spent by the time this runs,
    so an operator who has to finish the teardown by hand needs to be told
    which account to finish it in. The pool reallocates a released member and
    wipes it, and a member left held is one the next scan cannot measure.
    """
    for attempt in range(1, retries + 2):
        log.write(
            "session_end_requested", round=round_index, account=account, attempt=attempt
        )
        try:
            end_session(config)
        except ApiError as error:
            if not is_setup_refusal(error):
                raise
            if attempt > retries:
                raise ApiError(
                    f"Vocareum refused to end the session in account {account} "
                    f"{attempt} times, every time because it still calls the "
                    f"lab unfinished. Waited {wait_seconds}s between attempts. "
                    "The launch is already spent and the session is still "
                    "open, so end it by hand before the pool is scanned again:"
                    "\n  uv run vocareum-end-lab --course <name> --yes"
                ) from error
            log.write(
                "session_end_deferred",
                round=round_index,
                account=account,
                attempt=attempt,
                remaining=retries - attempt + 1,
                error=clip(str(error), 500),
            )
            print(
                f"  Vocareum still calls the lab unfinished. Waiting "
                f"{wait_seconds}s and trying again "
                f"(attempt {attempt} of {retries + 1})."
            )
            time.sleep(wait_seconds)
            continue
        log.write(
            "session_end_accepted", round=round_index, account=account, attempt=attempt
        )
        return


def scan(
    config: Config, args: argparse.Namespace, log: AuditLog, state: ScanState
) -> ScanState:
    """Cycle sessions until enough distinct accounts have been measured.

    ``state`` is passed in rather than created here so that a refusal partway
    through still leaves the caller holding every round that did complete.
    """
    for round_index in range(1, args.max_rounds + 1):
        section(f"ROUND {round_index} of at most {args.max_rounds}")
        facts = fetch_facts(config)
        started_by_scan = False

        if facts.live:
            print(f"A session is already open in account {facts.account_id}.")
            print("Measuring it costs no launch.")
            log.write("session_present", round=round_index, account=facts.account_id)
        else:
            print("No session is open. Starting one costs one launch.")
            start_session_with_retry(
                config,
                log,
                round_index,
                retries=args.cleanup_retries,
                wait_seconds=args.cleanup_wait,
            )
            started_by_scan = True

        observation = measure_current_session(config, args, wait=started_by_scan)
        observation.round_index = round_index
        observation.started_by_scan = started_by_scan
        state.observations.append(observation)
        log.write("measurement", **observation.as_dict())

        distinct = len(state.measured_accounts)
        print(f"\n  Distinct accounts measured so far: {distinct} of {args.limit}")
        if observation.entitlement is Entitlement.STACK_FAILED:
            print("  This round's stack never came up, so it does not count.")
        if distinct >= args.limit:
            print("  Target reached. Not rotating again.")
            log.write("stop", reason="limit_reached", distinct=distinct)
            break
        if round_index == args.max_rounds:
            print("  Round budget spent. Not rotating again.")
            log.write("stop", reason="round_budget_spent", distinct=distinct)
            break

        section(f"ROUND {round_index}: release the account")
        if not started_by_scan and not args.end_existing:
            print(
                "This session was already open when the scan started, so the "
                "scan will not end it. Ending gives up the account, wipes the "
                "member, and makes anything left in it unreachable.\n"
                "Sweep it, then re-run with --end-existing to let the scan "
                "rotate past a session it did not start."
            )
            log.write(
                "stop",
                reason="would_end_a_session_the_scan_did_not_start",
                account=observation.account,
            )
            break
        print(f"Ending the session in account {observation.account}.")
        end_session_with_retry(
            config,
            log,
            round_index,
            observation.account,
            retries=args.end_retries,
            wait_seconds=args.end_wait,
        )
        if not wait_until_down(config):
            log.write(
                "stop", reason="session_did_not_go_down", account=observation.account
            )
            break
        log.write("session_down", round=round_index, account=observation.account)

    return state


def report(state: ScanState, args: argparse.Namespace) -> None:
    """Print what was measured, one row per observation."""
    if args.json:
        print(
            json.dumps(
                {
                    "model": args.model,
                    "region": REQUIRED_REGION,
                    "observations": [
                        observation.as_dict() for observation in state.observations
                    ],
                    **summarize(state),
                },
                indent=2,
            )
        )
        return

    section("RESULT")
    if not state.observations:
        print("Nothing was measured.")
        return

    print(f"{'round':>5}  {'account':<14}  {'entitlement':<12}  request id")
    for observation in state.observations:
        print(
            f"{observation.round_index:>5}  {observation.account:<14}  "
            f"{observation.entitlement!s:<12}  {observation.request_id or '-'}"
        )

    summary = summarize(state)
    print(f"\nDistinct accounts measured: {len(state.measured_accounts)}")
    print(f"  Bedrock entitled : {summary['entitled'] or 'none'}")
    print(f"  Error 002        : {summary['error002'] or 'none'}")
    if summary["stackFailed"]:
        print(
            f"  Stack never came up: {summary['stackFailed']}. Those launches "
            "were spent without measuring entitlement."
        )

    unstable = state.unstable()
    if unstable:
        print(
            f"\nUNSTABLE: {unstable} answered more than one way across rounds.\n"
            "Entitlement is therefore not a property of the account, so asking "
            "Vocareum to enable specific members is the wrong request."
        )
    elif len(state.observations) > len(state.accounts):
        print(
            "\nEvery repeated account answered the same way both times, which "
            "is evidence that entitlement is a stable property of the account."
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Measure Bedrock entitlement across the Vocareum account pool by "
            "cycling lab sessions. No read API predicts Error 002, so this "
            "invokes a model once per account. Every start costs one launch "
            "against the budget and nothing is started without --yes."
        )
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Actually start and end sessions. Without it this is a dry run "
            "that reads the open session and the pool and spends nothing."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Stop once this many distinct accounts have been measured.",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=0,
        help=(
            "Hard cap on session starts. Defaults to twice --limit, because "
            "reallocation is not round-robin and repeats are expected."
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_PROBE_MODEL,
        help=f"Model id to invoke. Defaults to {DEFAULT_PROBE_MODEL}.",
    )
    parser.add_argument(
        "--end-existing",
        action="store_true",
        help=(
            "Allow the scan to end a session it did not start. Sweep that "
            "account first: ending it wipes the member."
        ),
    )
    parser.add_argument(
        "--stack-timeout",
        type=int,
        default=STACK_TIMEOUT_SECONDS,
        help=(
            "Seconds to let CloudFormation finish before giving up on a round. "
            f"Defaults to {STACK_TIMEOUT_SECONDS}. The Bedrock grant is a "
            "RolePolicy inside lab.template, so invoking before the stack is "
            "up measures the build and not the account."
        ),
    )
    parser.add_argument(
        "--stack-poll",
        type=int,
        default=STACK_POLL_SECONDS,
        help=f"Seconds between stack reads. Defaults to {STACK_POLL_SECONDS}.",
    )
    parser.add_argument(
        "--cleanup-retries",
        type=int,
        default=CLEANUP_RETRIES,
        help=(
            "How many times to wait out a start refused because the pool is "
            f"still cleaning up. Defaults to {CLEANUP_RETRIES}. A refused "
            "start costs no launch, so waiting is free."
        ),
    )
    parser.add_argument(
        "--cleanup-wait",
        type=int,
        default=CLEANUP_WAIT_SECONDS,
        help=(
            "Seconds between those retries. Defaults to "
            f"{CLEANUP_WAIT_SECONDS}. Vocareum says to expect five minutes."
        ),
    )
    parser.add_argument(
        "--end-retries",
        type=int,
        default=SETUP_RETRIES,
        help=(
            "How many times to wait out an end refused because Vocareum still "
            f"calls the lab unfinished. Defaults to {SETUP_RETRIES}. Unlike a "
            "refused start, this one holds a spent launch open, so exhausting "
            "it stops the scan and names the account to end by hand."
        ),
    )
    parser.add_argument(
        "--end-wait",
        type=int,
        default=SETUP_WAIT_SECONDS,
        help=f"Seconds between those retries. Defaults to {SETUP_WAIT_SECONDS}.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON.")
    parser.add_argument(
        "--log",
        type=Path,
        help=(
            "Audit log path. Defaults to a timestamped .jsonl file under "
            f"{DEFAULT_LOG_DIR}/, which is git-ignored."
        ),
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help=(
            "Write no audit log. A run that dies mid-scan then loses the "
            "evidence its spent launches bought, so this is rarely what you "
            "want."
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the base .env file. Defaults to the repository root.",
    )
    add_course_argument(parser)
    parser.add_argument("--region", help="AWS region. Defaults to AWS_REGION.")
    args = parser.parse_args(argv)

    if args.log is not None and args.no_log:
        parser.error("--log and --no-log contradict each other.")
    if args.limit < 1:
        parser.error("--limit must be at least 1.")
    if args.stack_timeout < 0:
        parser.error("--stack-timeout cannot be negative.")
    if args.stack_poll < 1:
        parser.error("--stack-poll must be at least 1 second.")
    if args.cleanup_retries < 0:
        parser.error("--cleanup-retries cannot be negative.")
    if args.cleanup_wait < 1:
        parser.error("--cleanup-wait must be at least 1 second.")
    if args.end_retries < 0:
        parser.error("--end-retries cannot be negative.")
    if args.end_wait < 1:
        parser.error("--end-wait must be at least 1 second.")
    if args.max_rounds < 0:
        parser.error("--max-rounds cannot be negative.")
    if args.max_rounds == 0:
        args.max_rounds = args.limit * 2
    if args.max_rounds < args.limit:
        parser.error(
            f"--max-rounds {args.max_rounds} cannot reach --limit {args.limit} "
            "distinct accounts."
        )
    if args.log is None and not args.no_log:
        args.log = default_log_path(repository_root())
    if args.no_log:
        args.log = None
    return args


def dry_run(config: Config, args: argparse.Namespace) -> int:
    """Read the open session and the pool, and print what --yes would spend."""
    section("DRY RUN")
    facts = fetch_facts(config)
    print(f"  session state = {facts.state or '(absent)'}")
    print(f"  account_id    = {facts.account_id or '(empty, so no session is open)'}")
    if facts.pool:
        print("  aws_linked_accounts:")
        for member in facts.pool:
            print(
                f"    {member.get('account', '?')}  "
                f"inuse={member.get('inuse', '?')}  "
                f"status={member.get('status', '?')}"
            )
    else:
        print("  aws_linked_accounts: (absent, so the pool is not legible here)")

    starts = args.max_rounds if not facts.live else args.max_rounds - 1
    print(
        f"\nWith --yes this would measure up to {args.limit} distinct accounts "
        f"in at most {args.max_rounds} rounds, invoking {args.model} once per "
        f"round, and would spend at most {max(starts, 0)} launches."
    )
    print("Nothing was started or ended. A dry run is the default.")
    return EXIT_OK


def summarize(state: ScanState) -> dict[str, Any]:
    """Return the run-level facts, for the audit log and for ``--json``."""
    return {
        "distinctAccounts": state.accounts,
        "entitled": sorted(
            {
                observation.account
                for observation in state.observations
                if observation.entitlement is Entitlement.ENTITLED
            }
        ),
        "error002": sorted(
            {
                observation.account
                for observation in state.observations
                if observation.entitlement is Entitlement.ERROR_002
            }
        ),
        "stackFailed": sorted(
            {
                observation.account
                for observation in state.observations
                if observation.entitlement is Entitlement.STACK_FAILED
            }
        ),
        "unstableAccounts": state.unstable(),
        "rounds": len(state.observations),
        "measuredAccounts": state.measured_accounts,
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``vocareum-scan-pool``."""
    args = parse_args(argv)
    try:
        config = load_config(args.env_file, args.region, args.course)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG

    if not args.yes:
        try:
            return dry_run(config, args)
        except (ApiError, GuardError) as error:
            print(f"SCAN REFUSED\n{error}", file=sys.stderr)
            return EXIT_GUARD

    # The state is owned here rather than inside ``scan`` so a refusal in round
    # four still reports what rounds one through three measured. Those launches
    # are spent either way, and discarding their result is the one outcome that
    # makes the operator pay for them twice.
    state = ScanState()
    with open_audit_log(args.log) as log:
        if args.log is not None:
            print(f"Audit log: {args.log}")
        log.write(
            "run_start",
            part=config.part_url(),
            course=args.course,
            model=args.model,
            region=config.region,
            limit=args.limit,
            maxRounds=args.max_rounds,
            endExisting=args.end_existing,
            stackTimeout=args.stack_timeout,
            cleanupRetries=args.cleanup_retries,
            endRetries=args.end_retries,
        )
        try:
            scan(config, args, log, state)
        except (ApiError, GuardError) as error:
            log.write(
                "refused",
                error=clip(redact_text(str(error)), 2000),
                **summarize(state),
            )
            print(f"SCAN REFUSED\n{error}", file=sys.stderr)
            report(state, args)
            return EXIT_GUARD
        except KeyboardInterrupt:
            log.write("interrupted", **summarize(state))
            print("\nInterrupted.", file=sys.stderr)
            report(state, args)
            return EXIT_GUARD
        except Exception as error:
            log.write(
                "failed",
                errorType=type(error).__name__,
                error=clip(redact_text(str(error)), 2000),
                **summarize(state),
            )
            raise
        log.write("run_end", **summarize(state))

    report(state, args)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
