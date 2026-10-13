#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "boto3>=1.39",
# ]
# ///
"""Operate the shared workshop Bedrock key and its spend controls."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "arns.env"
DEFAULT_SECRET_PATH = HERE / "bedrock-key.env"
CREATE_SCRIPT = HERE / "create-key.py"
TEMPLATE_PATH = HERE / "controls.template"
SERVICE_NAME = "bedrock.amazonaws.com"


class AdminError(RuntimeError):
    """An operator-facing validation or state error."""


def read_env(path: Path) -> dict[str, str]:
    """Read the simple NAME=value files used by the Bedrock tools."""
    if not path.is_file():
        raise AdminError(
            f"Missing {path}. Copy arns.env.example to arns.env and configure it."
        )

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AdminError(f"Invalid assignment at {path}:{line_number}")
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("'\"")
    return values


def require(
    values: dict[str, str], *names: str, source: Path = CONFIG_PATH
) -> list[str]:
    """Return required configuration values or report all missing names."""
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise AdminError(f"Empty or unset in {source}: {', '.join(missing)}")
    return [values[name] for name in names]


@dataclass(frozen=True)
class Settings:
    account_id: str
    region: str
    user_name: str
    policy_name: str
    credential_id: str | None
    profile: str | None
    stack_name: str
    invocation_log_group: str
    chat_model_id: str
    alert_email: str

    @classmethod
    def load(cls, profile: str | None = None) -> Settings:
        values = read_env(CONFIG_PATH)
        (
            account_id,
            region,
            user_name,
            policy_name,
            stack_name,
            invocation_log_group,
            chat_model_id,
            alert_email,
        ) = require(
            values,
            "BEDROCK_KEY_ACCOUNT_ID",
            "BEDROCK_KEY_REGION",
            "BEDROCK_KEY_USER_NAME",
            "BEDROCK_KEY_POLICY_NAME",
            "BEDROCK_KEY_CONTROLS_STACK",
            "BEDROCK_KEY_INVOCATION_LOG_GROUP",
            "WORKSHOP_CHAT_MODEL_ID",
            "BEDROCK_KEY_ALERT_EMAIL",
        )
        return cls(
            account_id=account_id,
            region=region,
            user_name=user_name,
            policy_name=policy_name,
            credential_id=values.get("BEDROCK_KEY_CREDENTIAL_ID") or None,
            profile=profile
            or os.environ.get("AWS_PROFILE")
            or values.get("BEDROCK_KEY_ADMIN_PROFILE")
            or None,
            stack_name=stack_name,
            invocation_log_group=invocation_log_group,
            chat_model_id=chat_model_id,
            alert_email=alert_email,
        )

    @property
    def breaker_function_name(self) -> str:
        return f"{self.stack_name}-breaker"

    @property
    def breaker_log_group(self) -> str:
        return f"/aws/lambda/{self.breaker_function_name}"

    @property
    def breaker_topic_arn(self) -> str:
        return f"arn:aws:sns:{self.region}:{self.account_id}:{self.stack_name}-breaker"


def session_for(settings: Settings):
    return boto3.Session(profile_name=settings.profile, region_name=settings.region)


def guarded_session(settings: Settings):
    session = session_for(settings)
    actual = session.client("sts").get_caller_identity()["Account"]
    if actual != settings.account_id:
        raise AdminError(
            f"AWS profile resolves to account {actual}; expected {settings.account_id}"
        )
    return session


def list_credentials(iam, settings: Settings) -> list[dict[str, Any]]:
    response = iam.list_service_specific_credentials(
        UserName=settings.user_name,
        ServiceName=SERVICE_NAME,
    )
    return response.get("ServiceSpecificCredentials", [])


def current_credential(iam, settings: Settings) -> dict[str, Any]:
    if not settings.credential_id:
        raise AdminError("BEDROCK_KEY_CREDENTIAL_ID is empty in arns.env")
    for credential in list_credentials(iam, settings):
        if credential["ServiceSpecificCredentialId"] == settings.credential_id:
            return credential
    raise AdminError(
        f"Credential {settings.credential_id} is not present on {settings.user_name}"
    )


def command_status(settings: Settings, _args: argparse.Namespace) -> int:
    session = guarded_session(settings)
    iam = session.client("iam")
    print(f"account: {settings.account_id}")
    print(f"region:  {settings.region}")
    try:
        user = iam.get_user(UserName=settings.user_name)["User"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchEntity":
            raise
        print(f"user:    {settings.user_name} not created")
        print("policy:  not applied")
        print("keys:    none")
    else:
        print(f"user:    {user['Arn']}")
        try:
            policy = iam.get_user_policy(
                UserName=settings.user_name,
                PolicyName=settings.policy_name,
            )["PolicyDocument"]
        except ClientError as error:
            if error.response["Error"]["Code"] != "NoSuchEntity":
                raise
            print(f"policy:  {settings.policy_name} not applied")
        else:
            statement_ids = [
                statement.get("Sid", "<unnamed>")
                for statement in policy.get("Statement", [])
            ]
            print(f"policy:  {settings.policy_name} ({', '.join(statement_ids)})")
        credentials = list_credentials(iam, settings)
        if not credentials:
            print("keys:    none")
        for credential in credentials:
            credential_id = credential["ServiceSpecificCredentialId"]
            marker = " (configured)" if credential_id == settings.credential_id else ""
            print(
                f"key:     {credential_id} {credential['Status']} "
                f"expires {credential.get('ExpirationDate', 'never')}{marker}"
            )

    cloudformation = session.client("cloudformation")
    try:
        stack = cloudformation.describe_stacks(StackName=settings.stack_name)["Stacks"][
            0
        ]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ValidationError":
            raise
        print(f"stack:   {settings.stack_name} not deployed")
    else:
        print(f"stack:   {settings.stack_name} {stack['StackStatus']}")
    return 0


def wait_for_propagation(seconds: int, message: str) -> None:
    if seconds <= 0:
        return
    print(message)
    for remaining in range(seconds, 0, -1):
        print(f"  {remaining:2d}s", end="\r", flush=True)
        time.sleep(1)
    print(" " * 8, end="\r")


def change_key_status(settings: Settings, status: str, wait_seconds: int) -> int:
    session = guarded_session(settings)
    iam = session.client("iam")
    credential = current_credential(iam, settings)
    if credential["Status"] == status:
        print(f"{settings.credential_id} is already {status}")
    else:
        iam.update_service_specific_credential(
            UserName=settings.user_name,
            ServiceSpecificCredentialId=settings.credential_id,
            Status=status,
        )
    recorded = current_credential(iam, settings)
    if recorded["Status"] != status:
        raise AdminError(
            f"IAM reports {recorded['Status']}, expected {status} for "
            f"{settings.credential_id}"
        )
    print(
        f"{settings.credential_id} {status} expires "
        f"{recorded.get('ExpirationDate', 'never')}"
    )
    if status == "Inactive":
        wait_for_propagation(
            wait_seconds,
            "IAM recorded Inactive. Waiting for Bedrock propagation.",
        )
        print("Key stopped. Bedrock calls using it now fail.")
    else:
        wait_for_propagation(
            wait_seconds,
            "IAM recorded Active. Waiting for Bedrock propagation.",
        )
        print("Key active. The class can retry failed calls.")
    return 0


def command_stop(settings: Settings, args: argparse.Namespace) -> int:
    return change_key_status(settings, "Inactive", 0 if args.no_wait else 30)


def command_resume(settings: Settings, args: argparse.Namespace) -> int:
    return change_key_status(settings, "Active", 0 if args.no_wait else 15)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def command_cost(settings: Settings, args: argparse.Namespace) -> int:
    session = guarded_session(settings)
    today = date.today()
    start = args.start or today.replace(day=1)
    end = args.end or today + timedelta(days=1)
    if start >= end:
        raise AdminError("Cost range start must be before end; end is exclusive")
    response = session.client("ce").get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    totals: list[tuple[float, str, str]] = []
    for period in response.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            metric = group["Metrics"]["UnblendedCost"]
            amount = float(metric["Amount"])
            if amount:
                totals.append((amount, group["Keys"][0], metric["Unit"]))
    print(f"Unblended cost {start} through {end} (end exclusive)")
    for amount, service, unit in sorted(totals, reverse=True):
        print(f"{amount:10.2f} {unit:3}  {service}")
    if not totals:
        print("No non-zero service costs returned.")
    return 0


def list_topic_subscriptions(sns, topic_arn: str) -> list[dict[str, str]]:
    """Return every subscription on one topic, following pagination."""
    subscriptions: list[dict[str, str]] = []
    token: str | None = None
    while True:
        request: dict[str, Any] = {"TopicArn": topic_arn}
        if token:
            request["NextToken"] = token
        response = sns.list_subscriptions_by_topic(**request)
        subscriptions.extend(response.get("Subscriptions", []))
        token = response.get("NextToken")
        if not token:
            break
    return subscriptions


def is_subscription_arn(value: str) -> bool:
    """Return whether SNS reported a confirmed subscription ARN."""
    return value.startswith("arn:")


def email_subscriptions(
    subscriptions: list[dict[str, str]], endpoint: str
) -> list[dict[str, str]]:
    """Return subscriptions for one email endpoint, case-insensitively."""
    return [
        item
        for item in subscriptions
        if item.get("Protocol") == "email"
        and item.get("Endpoint", "").casefold() == endpoint.casefold()
    ]


def confirmed_email_subscription(
    sns, subscriptions: list[dict[str, str]], endpoint: str
) -> tuple[dict[str, str], bool] | None:
    """Return a confirmed email subscription and its auth status, if present."""
    for subscription in email_subscriptions(subscriptions, endpoint):
        subscription_arn = subscription.get("SubscriptionArn", "")
        if not is_subscription_arn(subscription_arn):
            continue
        attributes = sns.get_subscription_attributes(
            SubscriptionArn=subscription_arn
        ).get("Attributes", {})
        authenticated = attributes.get("ConfirmationWasAuthenticated") == "true"
        return subscription, authenticated
    return None


def request_email_subscription(sns, topic_arn: str, endpoint: str) -> None:
    """Request a fresh email confirmation for one SNS topic."""
    sns.subscribe(
        TopicArn=topic_arn,
        Protocol="email",
        Endpoint=endpoint,
        ReturnSubscriptionArn=True,
    )
    print(f"Requested a new SNS confirmation email for {endpoint}.")
    print(
        "Copy the Token value from its confirmation link, then run:\n"
        "  uv run bedrock-key/admin.py subscriptions --confirm-token TOKEN"
    )


def ensure_email_subscription(sns, topic_arn: str, endpoint: str) -> bool:
    """Ensure an email subscription exists, requesting confirmation if needed."""
    subscriptions = list_topic_subscriptions(sns, topic_arn)
    confirmed = confirmed_email_subscription(sns, subscriptions, endpoint)
    if confirmed is not None:
        _, authenticated = confirmed
        protection = "protected" if authenticated else "not protected"
        print(f"email subscription: confirmed, unsubscribe {protection}")
        return authenticated

    pending = any(
        item.get("SubscriptionArn") == "PendingConfirmation"
        for item in email_subscriptions(subscriptions, endpoint)
    )
    if pending:
        print(f"email subscription: pending confirmation for {endpoint}")
        return False

    request_email_subscription(sns, topic_arn, endpoint)
    return False


def command_subscriptions(settings: Settings, args: argparse.Namespace) -> int:
    sns = guarded_session(settings).client("sns")
    topic_arn = settings.breaker_topic_arn

    if args.confirm_token:
        response = sns.confirm_subscription(
            TopicArn=topic_arn,
            Token=args.confirm_token,
            AuthenticateOnUnsubscribe="true",
        )
        print(f"Confirmed protected subscription {response['SubscriptionArn']}")

    subscriptions = list_topic_subscriptions(sns, topic_arn)
    confirmed = confirmed_email_subscription(sns, subscriptions, settings.alert_email)
    if args.repair and confirmed is not None and not confirmed[1]:
        sns.unsubscribe(SubscriptionArn=confirmed[0]["SubscriptionArn"])
        subscriptions = [item for item in subscriptions if item is not confirmed[0]]
        confirmed = None

    if args.repair and confirmed is None:
        request_email_subscription(sns, topic_arn, settings.alert_email)
        subscriptions = list_topic_subscriptions(sns, topic_arn)

    if not subscriptions:
        print(f"No subscriptions on {topic_arn}")
    for subscription in subscriptions:
        print(
            f"{subscription['Protocol']:8} "
            f"{subscription['SubscriptionArn']} "
            f"{subscription.get('Endpoint', '')}"
        )

    confirmed = confirmed_email_subscription(sns, subscriptions, settings.alert_email)
    if confirmed is None:
        print("email status: not confirmed; run subscriptions --repair")
        return 1
    if not confirmed[1]:
        print("email status: confirmed but unauthenticated; run subscriptions --repair")
        return 1
    print("email status: confirmed and protected from unauthenticated unsubscribe")
    return 0


def command_alarm(settings: Settings, args: argparse.Namespace) -> int:
    suffixes = {
        "burst": "burst-spend",
        "sustained": "sustained-spend",
        "total": "workshop-total-spend",
        "failure": "breaker-failed",
    }
    alarm_name = f"{settings.stack_name}-{suffixes[args.alarm]}"
    session = guarded_session(settings)
    session.client("cloudwatch").set_alarm_state(
        AlarmName=alarm_name,
        StateValue="ALARM",
        StateReason="bedrock-key/admin.py rehearsal",
    )
    print(f"Forced {alarm_name} to ALARM for rehearsal")
    if args.wait:
        iam = session.client("iam")
        expected = "Active" if args.alarm == "failure" else "Inactive"
        if args.alarm == "failure":
            wait_for_propagation(args.wait, "Waiting for alarm delivery.")
            credential = current_credential(iam, settings)
        else:
            deadline = time.monotonic() + args.wait
            while True:
                credential = current_credential(iam, settings)
                if credential["Status"] == expected or time.monotonic() >= deadline:
                    break
                time.sleep(2)
        print(f"credential: {credential['Status']} (expected {expected})")
        if credential["Status"] != expected:
            raise AdminError(
                f"Alarm rehearsal failed: credential remained "
                f"{credential['Status']} after {args.wait} seconds"
            )
    return 0


def command_logs(settings: Settings, args: argparse.Namespace) -> int:
    logs = guarded_session(settings).client("logs")
    start_time = int(
        (datetime.now(tz=UTC) - timedelta(minutes=args.since_minutes)).timestamp()
        * 1000
    )
    seen: set[str] = set()
    next_token: str | None = None
    while True:
        request: dict[str, Any] = {
            "logGroupName": settings.breaker_log_group,
            "startTime": start_time,
            "interleaved": True,
        }
        if next_token:
            request["nextToken"] = next_token
        response = logs.filter_log_events(**request)
        for event in response.get("events", []):
            event_id = event["eventId"]
            if event_id in seen:
                continue
            seen.add(event_id)
            timestamp = datetime.fromtimestamp(event["timestamp"] / 1000, tz=UTC)
            print(f"{timestamp.isoformat()} {event['message'].rstrip()}")
        new_token = response.get("nextToken")
        if args.follow:
            next_token = new_token
            time.sleep(2)
            continue
        if not new_token or new_token == next_token:
            break
        next_token = new_token
    return 0


def command_invoke_breaker(settings: Settings, args: argparse.Namespace) -> int:
    if not args.yes:
        raise AdminError("invoke-breaker deactivates the key; add --yes to continue")
    session = guarded_session(settings)
    response = session.client("lambda").invoke(
        FunctionName=settings.breaker_function_name,
        InvocationType="RequestResponse",
        Payload=b"{}",
    )
    payload = response["Payload"].read().decode("utf-8")
    print(payload)
    if response.get("FunctionError"):
        raise AdminError(f"Breaker Lambda returned {response['FunctionError']}")
    credential = current_credential(session.client("iam"), settings)
    if credential["Status"] != "Inactive":
        raise AdminError(
            f"Breaker returned successfully but IAM reports {credential['Status']}"
        )
    wait_for_propagation(
        0 if args.no_wait else 30,
        "IAM recorded Inactive. Waiting for Bedrock propagation.",
    )
    print("Breaker test passed. The key is inactive.")
    return 0


def clear_current_credential(credential_id: str) -> bool:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    configured = read_env(CONFIG_PATH).get("BEDROCK_KEY_CREDENTIAL_ID")
    if configured != credential_id:
        return False
    lines = []
    for line in text.splitlines():
        if line.startswith("BEDROCK_KEY_CREDENTIAL_ID="):
            line = "BEDROCK_KEY_CREDENTIAL_ID="
        elif line.startswith("BEDROCK_KEY_CREDENTIAL_EXPIRES="):
            line = "BEDROCK_KEY_CREDENTIAL_EXPIRES="
        lines.append(line)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{CONFIG_PATH.name}.", suffix=".tmp", dir=CONFIG_PATH.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(CONFIG_PATH)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return True


def delete_credential(
    settings: Settings, credential_id: str, *, missing_ok: bool = False
) -> None:
    iam = guarded_session(settings).client("iam")
    matches = {
        item["ServiceSpecificCredentialId"] for item in list_credentials(iam, settings)
    }
    if credential_id not in matches:
        if missing_ok:
            if clear_current_credential(credential_id):
                DEFAULT_SECRET_PATH.unlink(missing_ok=True)
            print(f"Credential {credential_id} was already absent")
            return
        raise AdminError(
            f"Credential {credential_id} is not present on {settings.user_name}"
        )
    iam.delete_service_specific_credential(
        UserName=settings.user_name,
        ServiceSpecificCredentialId=credential_id,
    )
    if clear_current_credential(credential_id):
        DEFAULT_SECRET_PATH.unlink(missing_ok=True)
    print(f"Deleted credential {credential_id}")


def command_delete_key(settings: Settings, args: argparse.Namespace) -> int:
    if not args.yes:
        raise AdminError("delete-key is permanent; add --yes to continue")
    credential_id = args.credential_id or settings.credential_id
    if not credential_id:
        raise AdminError("No credential id supplied or configured in arns.env")
    delete_credential(settings, credential_id)
    return 0


def command_teardown(settings: Settings, args: argparse.Namespace) -> int:
    if not args.yes:
        raise AdminError("teardown deletes the key and controls; add --yes to continue")
    if settings.credential_id:
        delete_credential(settings, settings.credential_id, missing_ok=True)
    else:
        print("No configured credential to delete")

    session = guarded_session(settings)
    bedrock = session.client("bedrock")
    try:
        bedrock.delete_model_invocation_logging_configuration()
    except ClientError as error:
        if error.response["Error"]["Code"] not in {
            "ResourceNotFoundException",
            "ValidationException",
        }:
            raise
        print("Model invocation logging was not configured")
    else:
        print("Deleted model invocation logging configuration")
    cloudformation = session.client("cloudformation")
    if stack_or_none(cloudformation, settings.stack_name) is None:
        print(f"Controls stack {settings.stack_name} was not deployed")
    else:
        cloudformation.delete_stack(StackName=settings.stack_name)
        print(f"Started deletion of stack {settings.stack_name}")
    return 0


def command_test_key(settings: Settings, args: argparse.Namespace) -> int:
    values = read_env(args.secret_file)
    (secret,) = require(values, "AWS_BEARER_TOKEN_BEDROCK", source=args.secret_file)
    os.environ["AWS_BEARER_TOKEN_BEDROCK"] = secret
    client = boto3.client("bedrock-runtime", region_name=settings.region)
    response = client.converse(
        modelId=settings.chat_model_id,
        messages=[{"role": "user", "content": [{"text": "Reply with OK"}]}],
        inferenceConfig={"maxTokens": 8},
    )
    text = response["output"]["message"]["content"][0]["text"]
    print(f"Bedrock replied: {text}")
    return 0


def run_child(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def command_create(args: argparse.Namespace) -> int:
    command = [sys.executable, str(CREATE_SCRIPT)]
    if args.profile:
        command.extend(["--profile", args.profile])
    if args.credential_age_days is not None:
        command.extend(["--credential-age-days", str(args.credential_age_days)])
    if args.output:
        command.extend(["--output", str(args.output)])
    if args.dry_run:
        command.append("--dry-run")
    return run_child(command)


def deploy_parameters(values: dict[str, str]) -> list[dict[str, str]]:
    names = (
        "BEDROCK_KEY_CREDENTIAL_ID",
        "BEDROCK_KEY_CREDENTIAL_EXPIRES",
        "BEDROCK_KEY_ALERT_EMAIL",
        "WORKSHOP_EMBED_MODEL_ID",
        "WORKSHOP_MEMORY_EMBED_MODEL_ID",
        "BEDROCK_KEY_BURST_USD_PER_MINUTE",
        "BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE",
        "BEDROCK_KEY_SUSTAINED_PERIODS",
        "BEDROCK_KEY_TOTAL_USD",
    )
    require(values, *names)
    try:
        burst = float(values["BEDROCK_KEY_BURST_USD_PER_MINUTE"])
        sustained = float(values["BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE"])
        periods = int(values["BEDROCK_KEY_SUSTAINED_PERIODS"])
        total = float(values["BEDROCK_KEY_TOTAL_USD"])
    except ValueError as error:
        raise AdminError("Spend thresholds and periods must be numbers") from error
    if not 0 < sustained < burst:
        raise AdminError(
            "BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE must be positive and below "
            "BEDROCK_KEY_BURST_USD_PER_MINUTE"
        )
    if periods < 2:
        raise AdminError("BEDROCK_KEY_SUSTAINED_PERIODS must be at least 2")
    if total <= 0:
        raise AdminError("BEDROCK_KEY_TOTAL_USD must be positive")

    mapping = {
        "KeyUserName": values["BEDROCK_KEY_USER_NAME"],
        "KeyCredentialId": values["BEDROCK_KEY_CREDENTIAL_ID"],
        "ChatModelId": values["WORKSHOP_CHAT_MODEL_ID"],
        "EmbedModelId": values["WORKSHOP_EMBED_MODEL_ID"],
        "MemoryEmbedModelId": values["WORKSHOP_MEMORY_EMBED_MODEL_ID"],
        "AlertEmail": values["BEDROCK_KEY_ALERT_EMAIL"],
        "InvocationLogGroupName": values["BEDROCK_KEY_INVOCATION_LOG_GROUP"],
        "BurstThresholdUsdPerMinute": values["BEDROCK_KEY_BURST_USD_PER_MINUTE"],
        "SustainedThresholdUsdPerMinute": values[
            "BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE"
        ],
        "SustainedEvaluationPeriods": values["BEDROCK_KEY_SUSTAINED_PERIODS"],
        "WorkshopTotalUsd": values["BEDROCK_KEY_TOTAL_USD"],
    }
    return [
        {"ParameterKey": key, "ParameterValue": value} for key, value in mapping.items()
    ]


def stack_or_none(cloudformation, stack_name: str) -> dict[str, Any] | None:
    try:
        return cloudformation.describe_stacks(StackName=stack_name)["Stacks"][0]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ValidationError":
            raise
        return None


def print_stack_outputs(stack: dict[str, Any]) -> None:
    print("stack outputs:")
    for output in stack.get("Outputs", []):
        print(f"  {output['OutputKey']}: {output['OutputValue']}")


def command_deploy(settings: Settings, args: argparse.Namespace) -> int:
    values = read_env(CONFIG_PATH)
    parameters = deploy_parameters(values)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    session = guarded_session(settings)
    cloudformation = session.client("cloudformation")
    validation = cloudformation.validate_template(TemplateBody=template)
    parameter_names = [item["ParameterKey"] for item in validation["Parameters"]]
    print(f"template parameters: {', '.join(parameter_names)}")

    iam = session.client("iam")
    credential = current_credential(iam, settings)
    print(
        f"credential: {credential['ServiceSpecificCredentialId']} "
        f"{credential['Status']} expires {credential.get('ExpirationDate', 'never')}"
    )
    print(
        "thresholds: "
        f"${values['BEDROCK_KEY_BURST_USD_PER_MINUTE']}/min burst; "
        f"${values['BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE']}/min for "
        f"{values['BEDROCK_KEY_SUSTAINED_PERIODS']} x 5 minutes sustained; "
        f"${values['BEDROCK_KEY_TOTAL_USD']} rolling 4-hour total"
    )
    if args.dry_run:
        print("Dry run: no stack or logging configuration was changed.")
        return 0

    stack = stack_or_none(cloudformation, settings.stack_name)
    if stack is not None and stack["StackStatus"].endswith("_IN_PROGRESS"):
        raise AdminError(
            f"Stack {settings.stack_name} is {stack['StackStatus']}; wait for it "
            "to settle before deploying again"
        )
    if stack is not None and stack["StackStatus"] == "ROLLBACK_COMPLETE":
        raise AdminError(
            f"Stack {settings.stack_name} is ROLLBACK_COMPLETE and cannot be "
            "updated; inspect its events and delete it before redeploying"
        )
    request = {
        "StackName": settings.stack_name,
        "TemplateBody": template,
        "Parameters": parameters,
        "Capabilities": ["CAPABILITY_IAM"],
        "Tags": [
            {"Key": "WorkshopResource", "Value": "graphrag-with-neo4j"},
            {"Key": "Purpose", "Value": "workshop-shared-bedrock-key"},
        ],
    }
    if stack is None:
        print(f"creating stack {settings.stack_name}")
        cloudformation.create_stack(**request)
        cloudformation.get_waiter("stack_create_complete").wait(
            StackName=settings.stack_name
        )
    else:
        print(f"updating stack {settings.stack_name}")
        try:
            cloudformation.update_stack(**request)
        except ClientError as error:
            if error.response["Error"][
                "Code"
            ] != "ValidationError" or "No updates are to be performed" not in str(
                error
            ):
                raise
            print("stack has no changes")
        else:
            cloudformation.get_waiter("stack_update_complete").wait(
                StackName=settings.stack_name
            )

    stack = cloudformation.describe_stacks(StackName=settings.stack_name)["Stacks"][0]
    outputs = {
        item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])
    }
    try:
        log_role_arn = outputs["InvocationLogRoleArn"]
    except KeyError as error:
        raise AdminError("Stack has no InvocationLogRoleArn output") from error

    bedrock = session.client("bedrock")
    print("invocation logging configuration being replaced:")
    try:
        previous = bedrock.get_model_invocation_logging_configuration()
    except ClientError as error:
        if error.response["Error"]["Code"] not in {
            "ResourceNotFoundException",
            "ValidationException",
        }:
            raise
        print("  none")
    else:
        previous.pop("ResponseMetadata", None)
        print(json.dumps(previous, indent=2, default=str))

    bedrock.put_model_invocation_logging_configuration(
        loggingConfig={
            "cloudWatchConfig": {
                "logGroupName": settings.invocation_log_group,
                "roleArn": log_role_arn,
            },
            "textDataDeliveryEnabled": True,
            "imageDataDeliveryEnabled": False,
            "embeddingDataDeliveryEnabled": False,
            "videoDataDeliveryEnabled": False,
        }
    )
    current = bedrock.get_model_invocation_logging_configuration()
    current.pop("ResponseMetadata", None)
    print("invocation logging enabled:")
    print(json.dumps(current, indent=2, default=str))
    print_stack_outputs(stack)
    ensure_email_subscription(
        session.client("sns"), settings.breaker_topic_arn, settings.alert_email
    )
    print("Next: confirm and verify email with `admin.py subscriptions`.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        help="AWS profile; defaults to AWS_PROFILE, then arns.env",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create the user, policy, and key")
    create.add_argument("--credential-age-days", type=int)
    create.add_argument("--output", type=Path)
    create.add_argument("--dry-run", action="store_true")

    deploy = subparsers.add_parser("deploy", help="deploy or validate spend controls")
    deploy.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("status", help="show user, policy, key, and stack state")
    for name in ("pause", "stop", "resume"):
        action = subparsers.add_parser(
            name,
            help="deactivate the key" if name != "resume" else "reactivate the key",
        )
        action.add_argument("--no-wait", action="store_true")

    cost = subparsers.add_parser("cost", help="show account costs grouped by service")
    cost.add_argument("--start", type=parse_date)
    cost.add_argument("--end", type=parse_date, help="exclusive end date")

    subscriptions = subparsers.add_parser(
        "subscriptions", help="show or repair breaker email subscription"
    )
    repair_group = subscriptions.add_mutually_exclusive_group()
    repair_group.add_argument(
        "--repair",
        action="store_true",
        help="request a replacement for a missing, deleted, or unprotected email",
    )
    repair_group.add_argument(
        "--confirm-token",
        metavar="TOKEN",
        help="confirm the emailed token with authenticated unsubscribe protection",
    )
    alarm = subparsers.add_parser("alarm", help="force an alarm for rehearsal")
    alarm.add_argument("alarm", choices=("burst", "sustained", "total", "failure"))
    alarm.add_argument("--wait", type=int, default=20)

    logs = subparsers.add_parser("logs", help="read breaker Lambda logs")
    logs.add_argument("--since-minutes", type=int, default=30)
    logs.add_argument("--follow", action="store_true")

    invoke = subparsers.add_parser(
        "invoke-breaker", help="invoke the breaker Lambda directly"
    )
    invoke.add_argument("--yes", action="store_true")
    invoke.add_argument("--no-wait", action="store_true")

    delete_key = subparsers.add_parser("delete-key", help="permanently delete a key")
    delete_key.add_argument("--credential-id")
    delete_key.add_argument("--yes", action="store_true")

    teardown = subparsers.add_parser(
        "teardown", help="delete the key, logging configuration, and controls stack"
    )
    teardown.add_argument("--yes", action="store_true")

    test_key = subparsers.add_parser(
        "test-key", help="make one bearer-authenticated Bedrock call"
    )
    test_key.add_argument("--secret-file", type=Path, default=DEFAULT_SECRET_PATH)
    return parser


COMMANDS = {
    "deploy": command_deploy,
    "status": command_status,
    "pause": command_stop,
    "stop": command_stop,
    "resume": command_resume,
    "cost": command_cost,
    "subscriptions": command_subscriptions,
    "alarm": command_alarm,
    "logs": command_logs,
    "invoke-breaker": command_invoke_breaker,
    "delete-key": command_delete_key,
    "teardown": command_teardown,
    "test-key": command_test_key,
}


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "create":
            return command_create(args)
        settings = Settings.load(args.profile)
        return COMMANDS[args.command](settings, args)
    except (AdminError, BotoCoreError, ClientError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
