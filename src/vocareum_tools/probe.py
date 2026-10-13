"""Measure what a Vocareum student AWS account allows, one action at a time.

A CloudFormation stack measures what the *template deployer* role may do. This
script measures the other principal: the session role a student's notebook code
actually runs as. It is the engine the phase runners drive.

Three guards run before any result is reported, because a probe that reports on
the wrong AWS account is worse than no probe at all:

1. **Credential guard.** All three credential fields must be present and
   non-empty. A terminated Vocareum session returns empty strings rather than
   an error. boto3 treats empty strings as absent, falls back through its
   default provider chain, and picks up whatever ambient credentials the
   operator happens to hold. That exact sequence already produced one confident
   report about a completely unrelated account.
2. **Account guard.** ``sts:GetCallerIdentity`` runs first, and its account must
   equal the expected account. Nothing is reported when it does not.
3. **Region guard.** The region is recorded and compared against the region the
   workshop requires. A mismatch is reported, not silently tolerated, because
   AgentCore and the Nova embeddings model exist in ``us-east-1`` only.

Every probe outcome is classified into one verdict. The distinction that
matters most is an organization service control policy deny, which nothing
inside the account can lift, against an ordinary IAM shortfall, which can be
fixed in the account. Both arrive as ``AccessDenied``, and only the message
body tells them apart.

A probe names an IAM action, not a boto3 method::

    bedrock:ListFoundationModels
    lambda:GetFunction
    dynamodb:DescribeTable

so the results line up with the permission tracker without translation. Probes
marked read-only create nothing. Probes that would create something are refused
unless ``--allow-mutating`` is passed.

This command is part of the repository's uv project::

    uv run vocareum-probe --self-test
    uv run vocareum-probe --probe bedrock:ListFoundationModels
    uv run vocareum-probe --probes-file probes/phase1-bedrock.json
    uv run vocareum-probe --source ambient --expect-account 123456789012 \
        --probe sts:GetCallerIdentity

Configuration comes from ``.env`` in the repository root, or from real
environment variables, which always win. See ``.env.example``.

Exit codes:
    0  Every probe ran and was classified.
    1  Configuration error.
    2  A guard refused to report, or credentials could not be obtained.
    3  Self-test failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    HTTPClientError,
    NoCredentialsError,
    ParamValidationError,
    PartialCredentialsError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)

from .aws import DEFAULT_REGION, names_other_action
from .client import (
    CREDENTIAL_POLL_SECONDS,
    CREDENTIAL_TIMEOUT_SECONDS,
    TERMINAL_SUCCESS_STATES,
    TRANSACTION_POLL_SECONDS,
    TRANSACTION_TIMEOUT_SECONDS,
    ApiError,
    find_transaction_id,
    poll_transaction,
    request_json,
    wait_for_payload,
)
from .config import (
    VOCAREUM_SESSION_VARS,
    ConfigError,
    Environment,
    add_course_argument,
    repository_root,
)
from .endpoints import DEFAULT_API_BASE, SessionEndpoints
from .output import section
from .security import mask_secret, normalize_key, redact_text

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_GUARD = 2
EXIT_SELF_TEST = 3

# AgentCore and amazon.nova-2-multimodal-embeddings-v1:0 are us-east-1 only, so
# the workshop has one required region rather than a preferred one. It shares a
# value with DEFAULT_REGION but not a meaning: one is where the lab runs unless
# told otherwise, the other is the only place later phases can work at all.
REQUIRED_REGION = "us-east-1"

# Short timeouts and few retries. A probe run walks a long list of actions, and
# a hung socket on one of them should not stall the rest.
BOTO_CONFIG = BotoConfig(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 2, "mode": "standard"},
)

MAX_DETAIL_CHARS = 400

ACCOUNT_VAR = "LAB_AWS_ACCOUNT_ID"


class GuardError(RuntimeError):
    """A guard refused to let the run report anything."""


class Verdict(StrEnum):
    """One classified probe outcome.

    The vocabulary is shared verbatim with the deployer-side probe in
    ``lab-probe.template`` so a result means the same thing whichever principal
    produced it.
    """

    OK = "OK"
    ALLOWED_NOT_FOUND = "ALLOWED_NOT_FOUND"
    INCONCLUSIVE_BAD_INPUT = "INCONCLUSIVE_BAD_INPUT"
    SCP_DENY = "SCP_DENY"
    RCP_DENY = "RCP_DENY"
    IAM_DENY = "IAM_DENY"
    DEPENDENCY_DENIED = "DEPENDENCY_DENIED"
    ACCESS_DENIED_UNCLASSIFIED = "ACCESS_DENIED_UNCLASSIFIED"
    MODEL_ACCESS_NOT_GRANTED = "MODEL_ACCESS_NOT_GRANTED"
    MODEL_LEGACY_INACTIVE = "MODEL_LEGACY_INACTIVE"
    MODEL_USE_CASE_NOT_SUBMITTED = "MODEL_USE_CASE_NOT_SUBMITTED"
    MODEL_NOT_IN_REGION = "MODEL_NOT_IN_REGION"
    THROTTLED = "THROTTLED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    OPT_IN_REQUIRED = "OPT_IN_REQUIRED"
    CREDENTIALS_BROKEN = "CREDENTIALS_BROKEN"
    ENDPOINT_UNREACHABLE = "ENDPOINT_UNREACHABLE"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    UNCLASSIFIED = "UNCLASSIFIED"


# How each verdict lands in the permission tracker Status column.
# Verdicts that carry no authorization signal map to Untested rather than to a
# guess.
TRACKER_STATUS: dict[Verdict, str] = {
    Verdict.OK: "Allowed",
    Verdict.ALLOWED_NOT_FOUND: "Allowed",
    Verdict.THROTTLED: "Allowed",
    Verdict.QUOTA_EXCEEDED: "Allowed",
    Verdict.MODEL_ACCESS_NOT_GRANTED: "Allowed",
    Verdict.MODEL_LEGACY_INACTIVE: "Allowed",
    Verdict.MODEL_USE_CASE_NOT_SUBMITTED: "Allowed",
    Verdict.MODEL_NOT_IN_REGION: "Allowed",
    Verdict.SCP_DENY: "Denied (SCP)",
    Verdict.RCP_DENY: "Denied (SCP)",
    Verdict.IAM_DENY: "Denied (IAM)",
    Verdict.ACCESS_DENIED_UNCLASSIFIED: "Denied (IAM)",
    Verdict.OPT_IN_REQUIRED: "Blocked",
    Verdict.DEPENDENCY_DENIED: "Untested",
    Verdict.INCONCLUSIVE_BAD_INPUT: "Untested",
    Verdict.CREDENTIALS_BROKEN: "Untested",
    Verdict.ENDPOINT_UNREACHABLE: "Untested",
    Verdict.UNKNOWN_ACTION: "Untested",
    Verdict.UNCLASSIFIED: "Untested",
}

NEXT_ACTION: dict[Verdict, str] = {
    Verdict.SCP_DENY: (
        "A service control policy in Vocareum's AWS Organization blocks this. "
        "Nothing inside the account can raise it. Escalate to Vocareum with "
        "the policy id in the detail."
    ),
    Verdict.RCP_DENY: (
        "A resource control policy in the Organization blocks this. Same "
        "escalation path as an SCP."
    ),
    Verdict.IAM_DENY: (
        "The session role lacks the action. Vocareum owns that role, so this "
        "is still an escalation, but it is a different ask than an SCP change."
    ),
    Verdict.DEPENDENCY_DENIED: (
        "The denial names a different action than the one being probed, so it "
        "says nothing about the probed action. Fix the dependency, or provoke "
        "the action a way that does not need it, then probe again."
    ),
    Verdict.MODEL_ACCESS_NOT_GRANTED: (
        "Authorization passed. Model access was never granted for this model "
        "in this region. That is an account setting, not a policy problem."
    ),
    Verdict.MODEL_LEGACY_INACTIVE: (
        "Authorization passed, but the provider marked this model legacy and "
        "this account did not use it during the provider's activity window. "
        "Choose an active model from ListFoundationModels."
    ),
    Verdict.MODEL_USE_CASE_NOT_SUBMITTED: (
        "Authorization passed, but Anthropic use-case details are not active "
        "for the account or destination Region. Submit the provider form and "
        "retry after it propagates."
    ),
    Verdict.MODEL_NOT_IN_REGION: (
        "Authorization passed. The model is not offered here. Use the us. "
        "inference profile or move the lab to us-east-1."
    ),
    Verdict.THROTTLED: (
        "Authorization passed, then throttled. Retry or raise the quota."
    ),
    Verdict.QUOTA_EXCEEDED: (
        "Authorization passed, then hit a quota. Request an increase."
    ),
    Verdict.OPT_IN_REQUIRED: (
        "The account is not subscribed or opted in to this service."
    ),
    Verdict.CREDENTIALS_BROKEN: (
        "The credentials were rejected. Start a fresh lab session."
    ),
    Verdict.ENDPOINT_UNREACHABLE: "Network or endpoint problem, not a policy problem.",
    Verdict.UNKNOWN_ACTION: (
        "boto3 has no such operation for that service. Check the action name "
        "or set an explicit client in the probe."
    ),
}

# IAM action prefixes whose boto3 client is named something else. The value is
# an ordered tuple, not a single name, because one IAM service prefix can be
# served by more than one boto3 client and which one is right depends on the
# operation rather than the service. run_probe takes the first candidate that
# carries the operation.
#
# AgentCore is the case that forced this. Management lives on
# bedrock-agentcore-control and use lives on the bedrock-agentcore data plane,
# so a single mapping sent bedrock-agentcore:InvokeAgentRuntime to a client
# with no such operation. That returns
# UNKNOWN_ACTION, which records as Untested, so the row would have read as
# never measured. The two
# clients share no operation name, checked against botocore's own service
# models by test_agentcore_clients_do_not_share_an_operation, so taking the
# first match cannot pick the wrong one.
CLIENT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "bedrock-agentcore": ("bedrock-agentcore-control", "bedrock-agentcore"),
    "elasticloadbalancing": ("elbv2",),
}

# Actions that are read-only or explicitly non-mutating. Anything outside this
# set needs --allow-mutating, so a careless probe run cannot create billable
# resources in a student account.
READ_ONLY_PREFIXES = (
    "batchcheck",
    "batchget",
    "describe",
    "get",
    "list",
    "lookup",
    "search",
    "view",
)


def is_read_only(operation: str) -> bool:
    """Report whether an operation only reads."""
    lowered = operation.lower()
    return lowered.startswith(READ_ONLY_PREFIXES)


@dataclass(frozen=True)
class Probe:
    """One IAM action to measure, and how to provoke it."""

    action: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    client: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        """Validate probe definitions at the CLI and file boundary."""
        if not re.fullmatch(r"[A-Za-z0-9-]+:[A-Za-z0-9_*]+", self.action):
            raise ConfigError(
                f"Probe {self.action!r} is not an IAM action. Write it as "
                "service:Action, for example bedrock:ListFoundationModels."
            )
        if not isinstance(self.kwargs, dict):
            raise ConfigError(f"Probe {self.action!r} has non-object kwargs.")
        if self.client is not None and (
            not isinstance(self.client, str) or not self.client.strip()
        ):
            raise ConfigError(
                f"Probe {self.action!r} has an invalid client; expected a "
                "non-empty string."
            )

    @property
    def service(self) -> str:
        """Return the IAM service prefix, for example ``bedrock``."""
        return self.action.split(":", 1)[0]

    @property
    def operation(self) -> str:
        """Return the IAM action name, for example ``ListFoundationModels``."""
        return self.action.split(":", 1)[1]

    @property
    def client_names(self) -> tuple[str, ...]:
        """Return the boto3 clients that could serve this action, in order.

        An explicit ``client`` on the probe is the only candidate, so a probe
        file can still pin one deliberately and be told when it is wrong.
        """
        if self.client:
            return (self.client,)
        return CLIENT_CANDIDATES.get(self.service, (self.service,))

    def is_safe(self) -> bool:
        """Report whether this probe creates nothing."""
        return is_read_only(self.operation) or self.kwargs.get("DryRun") is True


@dataclass
class ProbeResult:
    """The classified outcome of one probe."""

    action: str
    verdict: Verdict
    detail: str
    error_code: str = ""

    @property
    def status(self) -> str:
        """Return the permission tracker Status value for this result."""
        return TRACKER_STATUS.get(self.verdict, "Untested")

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-ready mapping."""
        return {
            "action": self.action,
            "verdict": str(self.verdict),
            "status": self.status,
            "errorCode": self.error_code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Credentials:
    """An AWS credential triple, before anything has validated it."""

    access_key_id: str
    secret_access_key: str
    session_token: str

    def missing(self) -> list[str]:
        """Return the names of every field that is absent or blank.

        Blank is the case that matters. A terminated Vocareum session answers
        with empty strings rather than with an error, and boto3 reads an empty
        string as "no credential supplied".
        """
        fields = {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
            "aws_session_token": self.session_token,
        }
        return [name for name, value in fields.items() if not str(value).strip()]

    def as_boto_kwargs(self) -> dict[str, str]:
        """Return the triple as boto3 session keyword arguments."""
        return {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
            "aws_session_token": self.session_token,
        }


@dataclass(frozen=True)
class Config(SessionEndpoints):
    """Everything the run needs, resolved from the environment."""

    api_key: str
    expected_account: str
    region: str


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def clip(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    """Collapse whitespace and trim text, marking any truncation."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(
    env_path: Path,
    region_override: str | None,
    account_override: str | None,
    need_vocareum: bool,
    course: str | None = None,
    *,
    require_account: bool = True,
) -> Config:
    """Build a :class:`Config` from real environment variables and ``.env``.

    Only the expected account id is required when credentials come from
    somewhere other than the Vocareum API, since the rest of the variables only
    exist to reach that API.

    ``require_account=False`` is for ``vocareum-refresh`` alone. It reads the
    account from STS and writes it, and never compares it, so demanding the
    value it exists to fill made a blank pin impossible to bootstrap.
    """
    environment = Environment.from_file(env_path, course)
    # The session variables only exist to reach the Vocareum API. A run that
    # gets its credentials from somewhere else needs none of them.
    if need_vocareum:
        try:
            environment.require(VOCAREUM_SESSION_VARS)
        except ConfigError as error:
            raise ConfigError(
                f"{error}\nCopy .env.example to .env and fill in the values, "
                "or export them in your shell."
            ) from error

    expected = account_override or environment.get(ACCOUNT_VAR)
    if not expected and require_account:
        raise ConfigError(
            f"No expected AWS account. Set {ACCOUNT_VAR} or pass "
            "--expect-account. Refusing to probe an unidentified account."
            f"\nChecked real environment variables and {environment.path}."
        )

    return Config(
        api_key=environment.get("VOCAREUM_API_KEY"),
        course_id=environment.get("VOCAREUM_COURSE_ID"),
        assignment_id=environment.get("VOCAREUM_ASSIGNMENT_ID"),
        part_id=environment.get("VOCAREUM_PART_ID"),
        user_id=environment.get("VOCAREUM_USER_ID"),
        expected_account=expected,
        api_base=(environment.get("VOCAREUM_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
        region=region_override or environment.get("AWS_REGION") or DEFAULT_REGION,
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(error: ClientError, probed_action: str = "") -> tuple[Verdict, str, str]:
    """Map a botocore ``ClientError`` onto a verdict, a code, and a message.

    The order of the checks inside ``AccessDenied`` is load bearing. AWS names
    the blocking policy type in the message body, and that string is the only
    reliable way to tell an organization ceiling apart from a gap in the
    account's own IAM. Both arrive with the same error code.

    ``probed_action`` guards against a denial that belongs to something else.
    A denial naming a dependency the probed action happens to need, measured
    in the student account, would be flatly wrong recorded as the probed
    action's own answer.
    """
    info = error.response.get("Error", {})
    code = str(info.get("Code", "Unknown"))
    message = str(info.get("Message", str(error)))
    lowered = message.lower()

    denial_codes = {
        "AccessDenied",
        "AccessDeniedException",
        "AuthorizationError",
        "AccessDeniedError",
    }

    if code in denial_codes:
        # Checked first, and only within a denial, because a message naming a
        # different action than the one being probed says nothing about the
        # probed action whatever kind of denial it turns out to be. Outside a
        # denial the same wording can appear in text that is not a refusal.
        if names_other_action(message, probed_action):
            return Verdict.DEPENDENCY_DENIED, code, message
        if "service control policy" in lowered:
            return Verdict.SCP_DENY, code, message
        if "resource control policy" in lowered:
            return Verdict.RCP_DENY, code, message
        # IAM's own authorization-failure wording. It never appears in
        # Bedrock's model-access wording, so testing it first is safe.
        if (
            "not authorized to perform" in lowered
            or "no identity-based policy" in lowered
        ):
            return Verdict.IAM_DENY, code, message
        if "access to the model" in lowered or "model access" in lowered:
            return Verdict.MODEL_ACCESS_NOT_GRANTED, code, message
        return Verdict.ACCESS_DENIED_UNCLASSIFIED, code, message

    # Being told a resource does not exist means authorization already passed,
    # because AWS answers an unauthorized caller with AccessDenied rather than
    # revealing whether the resource is there. Naming something that does not
    # exist is how a read permission gets measured without creating anything.
    not_found_codes = {
        "ResourceNotFoundException",
        "ResourceNotFound",
        "NoSuchEntity",
        "NoSuchEntityException",
        "NoSuchBucket",
        "NoSuchKey",
        "EntityNotFoundException",
        "NotFoundException",
    }
    # Bedrock also uses ResourceNotFoundException when a legacy model is
    # unavailable to an account that did not use it recently. That is a model
    # lifecycle block, not a Region problem.
    if code == "ResourceNotFoundException" and (
        "marked by provider as legacy" in lowered
        or ("legacy" in lowered and "last 30 days" in lowered)
    ):
        return Verdict.MODEL_LEGACY_INACTIVE, code, message
    if code == "ResourceNotFoundException" and "use case details" in lowered:
        return Verdict.MODEL_USE_CASE_NOT_SUBMITTED, code, message

    # Bedrock reuses ResourceNotFoundException to mean "that model is not in
    # this region", which is worth telling apart from an ordinary missing
    # resource even though both land on Allowed. lab-probe.template and
    # lab-bedrock.template record the same mapping.
    #
    # The colon is load-bearing. Matching the bare prefix also caught
    # bedrock-agentcore, which has no models in it, so every absent gateway,
    # runtime, or memory was reported as a model that does not exist in this
    # Region, advice that cannot be acted on. Both land on Allowed, so the
    # tracker Status survived the confusion and the verdict did not.
    if code == "ResourceNotFoundException" and probed_action.startswith("bedrock:"):
        return Verdict.MODEL_NOT_IN_REGION, code, message
    if code in not_found_codes:
        return Verdict.ALLOWED_NOT_FOUND, code, message

    # Request-shape validation is NOT evidence of authorization. A malformed
    # or unknown parameter is rejected before the authorization check ever
    # runs, measured against a principal holding AdministratorAccess. Treating
    # these as Allowed would manufacture permissions that were never tested.
    inconclusive_codes = {
        "ValidationException",
        "ValidationError",
        "InvalidParameterValue",
        "InvalidParameterValueException",
        "InvalidParameterCombination",
        "MissingParameter",
    }
    if code in inconclusive_codes:
        return Verdict.INCONCLUSIVE_BAD_INPUT, code, message

    mapped: dict[str, Verdict] = {
        "ThrottlingException": Verdict.THROTTLED,
        "Throttling": Verdict.THROTTLED,
        "TooManyRequestsException": Verdict.THROTTLED,
        "RequestLimitExceeded": Verdict.THROTTLED,
        "ServiceQuotaExceededException": Verdict.QUOTA_EXCEEDED,
        "LimitExceededException": Verdict.QUOTA_EXCEEDED,
        "LimitExceeded": Verdict.QUOTA_EXCEEDED,
        "OptInRequired": Verdict.OPT_IN_REQUIRED,
        "SubscriptionRequiredException": Verdict.OPT_IN_REQUIRED,
        "UnrecognizedClientException": Verdict.CREDENTIALS_BROKEN,
        "InvalidClientTokenId": Verdict.CREDENTIALS_BROKEN,
        "InvalidSignatureException": Verdict.CREDENTIALS_BROKEN,
        "ExpiredToken": Verdict.CREDENTIALS_BROKEN,
        "ExpiredTokenException": Verdict.CREDENTIALS_BROKEN,
    }
    return mapped.get(code, Verdict.UNCLASSIFIED), code, message


# ---------------------------------------------------------------------------
# Credentials and guards
# ---------------------------------------------------------------------------


def find_credential_triple(payload: Any) -> Credentials | None:
    """Recursively find a dict holding all three AWS credential fields.

    Vocareum does not document the response shape, so key names are matched
    case-insensitively and punctuation-insensitively. Values are taken exactly
    as returned, including empty strings, so the credential guard is the one
    place that decides whether they are usable.
    """
    if isinstance(payload, dict):
        access_key = secret_key = token = None
        for key, value in payload.items():
            if not isinstance(value, str):
                continue
            normalized = normalize_key(key)
            if access_key is None and "accesskeyid" in normalized:
                access_key = value
            elif secret_key is None and "secretaccesskey" in normalized:
                secret_key = value
            elif token is None and (
                "sessiontoken" in normalized or "securitytoken" in normalized
            ):
                token = value
        if access_key is not None and secret_key is not None and token is not None:
            return Credentials(access_key, secret_key, token)
        for value in payload.values():
            found = find_credential_triple(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_credential_triple(item)
            if found is not None:
                return found
    return None


def start_session(config: Config) -> None:
    """Start or extend the lab session and follow its transaction."""
    print(f"PUT {config.resource_url()}")
    payload = request_json("PUT", config.resource_url(), config.api_key)
    print("Session start requested.")
    transaction_id = find_transaction_id(payload)
    if transaction_id is None:
        print("No transaction ID was returned; waiting for credentials directly.")
        return

    result = poll_transaction(
        config.transaction_url(transaction_id),
        config.api_key,
        poll_seconds=TRANSACTION_POLL_SECONDS,
        timeout_seconds=TRANSACTION_TIMEOUT_SECONDS,
        on_state=lambda state: print(f"  transaction state={state}"),
    )
    if result.timed_out:
        raise GuardError(
            f"Session start was still pending after {TRANSACTION_TIMEOUT_SECONDS}s."
        )
    if result.state.lower() not in TERMINAL_SUCCESS_STATES:
        detail = ""
        if isinstance(result.payload, dict) and result.payload.get("message"):
            detail = f" Message: {redact_text(str(result.payload['message']))}"
        raise GuardError(f"Session start ended in state {result.state!r}.{detail}")


def fetch_vocareum_credentials(config: Config, *, wait: bool) -> Credentials:
    """Fetch credentials, retrying while a newly started session initializes."""
    print(f"GET {config.resource_url()}")
    _, found = wait_for_payload(
        lambda: request_json("GET", config.resource_url(), config.api_key),
        find_credential_triple,
        poll_seconds=CREDENTIAL_POLL_SECONDS,
        timeout_seconds=CREDENTIAL_TIMEOUT_SECONDS,
        wait=wait,
        on_retry=lambda seconds: print(
            f"  credentials not ready; retrying in {seconds}s"
        ),
    )
    if found is None:
        raise GuardError(
            "The Vocareum session record contained no credential triple. "
            "The session is probably not running."
        )
    return found


def ambient_credentials() -> Credentials:
    """Read whatever credentials the local environment already holds.

    Only reachable behind an explicit ``--source ambient`` and an explicit
    ``--expect-account``, so the fallback that caused the original false report
    can never happen by default.
    """
    try:
        frozen = boto3.session.Session().get_credentials()
    except BotoCoreError as error:
        raise GuardError(
            f"Could not resolve ambient AWS credentials: {error}"
        ) from error
    if frozen is None:
        raise GuardError("No ambient AWS credentials are configured.")
    try:
        resolved = frozen.get_frozen_credentials()
    except BotoCoreError as error:
        raise GuardError(
            f"Could not resolve ambient AWS credentials: {error}"
        ) from error
    return Credentials(
        resolved.access_key or "",
        resolved.secret_key or "",
        resolved.token or "",
    )


def guard_credentials(credentials: Credentials) -> None:
    """Refuse to continue unless all three credential fields carry a value."""
    missing = credentials.missing()
    if missing:
        raise GuardError(
            "Credential guard refused the run. Empty or absent: "
            + ", ".join(missing)
            + ".\nA terminated Vocareum session returns empty credential "
            "strings. boto3 would treat those as absent, fall back to ambient "
            "credentials, and report on the wrong AWS account. Nothing is "
            "reported."
        )
    print(f"  aws_access_key_id     = {credentials.access_key_id}")
    print(f"  aws_secret_access_key = {mask_secret(credentials.secret_access_key)}")
    print(f"  aws_session_token     = {mask_secret(credentials.session_token)}")
    print("  Credential guard passed. All three fields carry a value.")


def build_session(credentials: Credentials) -> boto3.session.Session:
    """Build a boto3 session from a credential triple.

    Raises :class:`GuardError` on a failure, because a session that cannot be
    built is a run that cannot report. A broken local AWS config, an AWS_PROFILE
    naming a profile that is not there, fails here rather than on the first
    call, and letting the traceback out exits 1 with no verdict: the same
    silence as a refusal, without saying a refusal happened.
    """
    try:
        return boto3.session.Session(**credentials.as_boto_kwargs())
    except BotoCoreError as error:
        raise GuardError(
            f"boto3 could not build a session from these credentials: {error}"
        ) from error


def guard_account(
    session: boto3.session.Session, region: str, expected_account: str
) -> None:
    """Confirm the credentials really belong to the expected account.

    Raises :class:`GuardError` rather than returning a verdict. There is no
    third outcome to report: either the account is the expected one and the run
    continues, or nothing is reported at all.
    """
    try:
        client = session.client("sts", region_name=region, config=BOTO_CONFIG)
        identity = client.get_caller_identity()
    except (ClientError, BotoCoreError, NoCredentialsError) as error:
        raise GuardError(f"sts:GetCallerIdentity failed: {error}") from error

    account = str(identity.get("Account", ""))
    print(f"  Account  = {account}")
    print(f"  Identity = {identity.get('Arn', '')}")

    if account != expected_account:
        raise GuardError(
            f"Account guard refused the run. Expected {expected_account}, "
            f"reached {account}.\nThese credentials belong to a different AWS "
            "account. Nothing is reported.\nVocareum may have rotated the "
            "account pool. Run vocareum-refresh with the same --course "
            "selection to refresh the pinned account and stack."
        )
    print(f"  Account guard passed. This is {expected_account}.")


def guard_region(region: str) -> bool:
    """Record the region and report whether it is the region the workshop needs."""
    print(f"  Region = {region}")
    if region == REQUIRED_REGION:
        print(f"  Region guard passed. {REQUIRED_REGION} supports AgentCore and Nova.")
        return True
    print(
        f"  Region guard warning. The workshop requires {REQUIRED_REGION}. "
        "AgentCore and amazon.nova-2-multimodal-embeddings-v1:0 do not exist "
        f"in {region}, so later phases will fail here for reasons that are "
        "not permission problems."
    )
    return False


def open_guarded_session(
    config: Config,
    *,
    source: str,
    no_start: bool,
    account_named: bool,
    ambient_refusal: str,
    region_refusal: str = "",
) -> boto3.session.Session:
    """Run the three guards in order and hand back a session that passed them.

    Every tool that touches a live session needs the same three answers before
    it may report anything: are the credentials real, is this the account we
    were told to act in, and is this the region the workshop exists in. This
    grew as one copy per tool and the copies drifted in the two places tools
    genuinely differ, so those two are parameters and the rest is shared.

    ``ambient_refusal`` is the message for a run that asked for ambient
    credentials without naming the account. Ambient credentials are exactly
    what the credential trap falls back to, so the account has to be named on
    purpose, and how loudly that is said depends on what the tool then does.

    ``region_refusal``, when it carries text, makes a wrong region fatal rather
    than a warning. A tool that reads is still useful in the wrong region and
    reports a region it names; a tool that deletes or passes a verdict is not,
    because it would report an empty or failing account while the real one sits
    untouched somewhere else.

    Raises :class:`GuardError` or :class:`ApiError`. Both mean nothing was
    reported, which is the whole point of the sequence.
    """
    section("GUARD 1: obtain credentials")
    if source == "ambient":
        if not account_named:
            raise GuardError(ambient_refusal)
        print("Reading ambient credentials from the local environment.")
        credentials = ambient_credentials()
    else:
        if not no_start:
            start_session(config)
        credentials = fetch_vocareum_credentials(config, wait=not no_start)
    guard_credentials(credentials)

    section("GUARD 2: confirm the account")
    session = build_session(credentials)
    guard_account(session, config.region, config.expected_account)

    section("GUARD 3: confirm the region")
    if not guard_region(config.region) and region_refusal:
        raise GuardError(region_refusal)
    return session


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def to_method_name(operation: str) -> str:
    """Convert an IAM action name to its boto3 method name."""
    step = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", operation)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step).lower()


def run_probe(session: boto3.session.Session, region: str, probe: Probe) -> ProbeResult:
    """Run one probe and classify whatever comes back."""
    method_name = to_method_name(probe.operation)
    method = None
    unbuildable: list[str] = []
    for client_name in probe.client_names:
        try:
            client = session.client(client_name, region_name=region, config=BOTO_CONFIG)
        except BotoCoreError as error:
            unbuildable.append(f"no boto3 client named {client_name}: {error}")
            continue
        method = getattr(client, method_name, None)
        if method is not None:
            break

    if method is None:
        # Name every candidate. A probe that lands here is unmeasured, and the
        # operator has to know which clients were tried to tell a misspelled
        # action from a service whose candidate list is missing a plane.
        tried = ", ".join(probe.client_names)
        detail = f"no client among {tried} has operation {method_name}"
        if unbuildable:
            detail = f"{detail}; {'; '.join(unbuildable)}"
        return ProbeResult(probe.action, Verdict.UNKNOWN_ACTION, clip(detail))

    try:
        response = method(**probe.kwargs)
    except ClientError as error:
        verdict, code, message = classify(error, probe.action)
        return ProbeResult(probe.action, verdict, clip(redact_text(message)), code)
    except ParamValidationError as error:
        return ProbeResult(
            probe.action,
            Verdict.INCONCLUSIVE_BAD_INPUT,
            clip(str(error)),
        )
    except (NoCredentialsError, PartialCredentialsError) as error:
        return ProbeResult(
            probe.action,
            Verdict.CREDENTIALS_BROKEN,
            clip(str(error)),
        )
    except (BotoConnectionError, HTTPClientError) as error:
        return ProbeResult(
            probe.action,
            Verdict.ENDPOINT_UNREACHABLE,
            clip(str(error)),
        )
    except BotoCoreError as error:
        return ProbeResult(probe.action, Verdict.UNCLASSIFIED, clip(str(error)))

    metadata = (
        response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    )
    return ProbeResult(
        probe.action,
        Verdict.OK,
        clip(f"HTTP {metadata.get('HTTPStatusCode', 200)}, call succeeded"),
    )


def load_probes_file(path: Path) -> list[Probe]:
    """Load a JSON list of probe definitions."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"Could not read probes from {path}: {error}") from error
    if not isinstance(payload, list):
        raise ConfigError(f"{path} must hold a JSON list of probe objects.")

    probes: list[Probe] = []
    for entry in payload:
        if not isinstance(entry, dict) or "action" not in entry:
            raise ConfigError(f"Every probe in {path} needs an 'action' field.")
        unknown = sorted(set(entry) - {"action", "kwargs", "client", "note"})
        if unknown:
            raise ConfigError(
                f"Probe {entry['action']!r} in {path} has unknown fields: "
                + ", ".join(unknown)
            )
        probes.append(
            Probe(
                action=str(entry["action"]),
                kwargs=entry.get("kwargs", {}),
                client=entry.get("client"),
                note=str(entry.get("note", "")),
            )
        )
    return probes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def report(results: list[ProbeResult], as_json: bool) -> None:
    """Print the probe results as a table, and optionally as JSON."""
    section("RESULTS")
    width = max((len(item.action) for item in results), default=10)
    print(f"{'Action'.ljust(width)}  {'Status':13s}  Verdict")
    print(f"{'-' * width}  {'-' * 13}  {'-' * 28}")
    for item in results:
        print(f"{item.action.ljust(width)}  {item.status:13s}  {item.verdict}")

    print()
    for item in results:
        action = NEXT_ACTION.get(item.verdict)
        print(f"{item.action}")
        print(f"  verdict: {item.verdict}  ({item.status})")
        if item.error_code:
            print(f"  code:    {item.error_code}")
        if item.detail:
            print(f"  detail:  {item.detail}")
        if action:
            print(f"  next:    {action}")
        print()

    if as_json:
        section("JSON")
        print(json.dumps([item.as_dict() for item in results], indent=2))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Measure what a Vocareum student AWS account allows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--probe",
        action="append",
        default=[],
        metavar="SERVICE:ACTION",
        help="An IAM action to measure. Repeatable.",
    )
    parser.add_argument(
        "--probes-file",
        type=Path,
        help="JSON list of probe objects with action, kwargs, client, note.",
    )
    parser.add_argument(
        "--source",
        choices=("vocareum", "ambient"),
        default="vocareum",
        help="Where credentials come from. Default vocareum.",
    )
    parser.add_argument(
        "--expect-account",
        help=(
            "AWS account the credentials must belong to. "
            "Defaults to LAB_AWS_ACCOUNT_ID."
        ),
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Do not start or extend the lab session before fetching credentials.",
    )
    parser.add_argument(
        "--allow-mutating",
        action="store_true",
        help="Permit probes that would create or change resources.",
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
        "--json", action="store_true", help="Also print results as JSON."
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Check the classifier and the guards offline, then exit.",
    )
    return parser.parse_args(argv)


def collect_probes(args: argparse.Namespace) -> list[Probe]:
    """Assemble the probe list from every source the arguments name."""
    probes: list[Probe] = []
    probes.extend(Probe(action=spec) for spec in args.probe)
    if args.probes_file:
        probes.extend(load_probes_file(args.probes_file.expanduser()))
    if not probes:
        # sts:GetCallerIdentity is the only action every phase depends on, so
        # it is the sensible default when no probe was named.
        probes.append(Probe("sts:GetCallerIdentity", note="Phase 0 identity check."))
    return probes


def main(argv: list[str] | None = None) -> int:
    """Run the guards, then the probes, and report."""
    args = parse_args(argv)

    if args.self_test:
        # Imported here rather than at module scope because selftest imports
        # this module for the classifier and the guards it checks.
        from .selftest import run_self_test

        return run_self_test()

    try:
        probes = collect_probes(args)
        config = load_config(
            args.env_file.expanduser(),
            args.region,
            args.expect_account,
            need_vocareum=args.source == "vocareum",
            course=args.course,
        )
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG

    unsafe = [probe.action for probe in probes if not probe.is_safe()]
    if unsafe and not args.allow_mutating:
        print(
            "These probes would create or change resources: "
            + ", ".join(unsafe)
            + "\nRe-run with --allow-mutating if that is intended.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    try:
        session = open_guarded_session(
            config,
            source=args.source,
            no_start=args.no_start,
            account_named=bool(args.expect_account),
            ambient_refusal=(
                "--source ambient requires --expect-account. Ambient "
                "credentials are exactly what the credential trap falls "
                "back to, so the account must be named on purpose."
            ),
        )
    except (ApiError, GuardError) as error:
        print(f"\nREFUSED TO REPORT\n{error}", file=sys.stderr)
        return EXIT_GUARD

    section(f"PROBES ({len(probes)})")
    results: list[ProbeResult] = []
    for configured_probe in probes:
        print(f"  {configured_probe.action} ...")
        results.append(run_probe(session, config.region, configured_probe))

    report(results, args.json)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
