#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "boto3>=1.39.0",
#     "python-dotenv>=1.0.0",
# ]
# ///
"""Run the breaker Lambda handler without deploying it.

The handler lives inline in ``controls.template`` as the ``ZipFile`` body of
``BreakerFunction``. There is no second copy on disk, so this script extracts
that block and executes it. What runs here is the source that deploys, not a
transcription of it that can drift.

Configuration comes from an env file, ``arns.env`` by default. That file is the
same one ``admin.py deploy`` reads, so the credential targeted here is the
credential the deployed breaker would target.

The handler's only side effect is deactivating the shared workshop Bedrock key,
which breaks Bedrock for every student holding it. So a run is a dry run unless
``--execute`` is passed. A dry run swaps in a recording stub for the IAM client
and exercises every other line, including the SNS parsing and the log calls.

    uv run bedrock-key/run_breaker.py                    # dry run
    uv run bedrock-key/run_breaker.py --execute          # really deactivate
    uv run bedrock-key/run_breaker.py --remote           # invoke the deployed function
    uv run bedrock-key/run_breaker.py --alarm-name foo   # name in the synthetic event
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
import types
from dataclasses import dataclass
from pathlib import Path

import boto3
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = HERE / "arns.env"
DEFAULT_TEMPLATE = HERE / "controls.template"

# arns.env name -> the name the handler reads out of its Lambda environment.
HANDLER_ENV = {
    "BEDROCK_KEY_USER_NAME": "KEY_USER_NAME",
    "BEDROCK_KEY_CREDENTIAL_ID": "KEY_CREDENTIAL_ID",
}


class ConfigError(RuntimeError):
    """The env file is missing a value the handler needs."""


@dataclass(frozen=True)
class Settings:
    """Everything this script needs, resolved from the env file."""

    user_name: str
    credential_id: str
    region: str
    profile: str | None
    stack_name: str

    @property
    def function_name(self) -> str:
        return f"{self.stack_name}-breaker"


def load_settings(env_file: Path) -> Settings:
    """Read the env file and return the values the handler and CLI need."""
    if not env_file.is_file():
        raise ConfigError(f"no env file at {env_file}")

    values = {k: v for k, v in dotenv_values(env_file).items() if v}

    missing = [name for name in HANDLER_ENV if not values.get(name)]
    if missing:
        raise ConfigError(
            f"empty or unset in {env_file.name}: {', '.join(missing)}. "
            "No key exists until KEY-LIFECYCLE.md issues one."
        )

    return Settings(
        user_name=values["BEDROCK_KEY_USER_NAME"],
        credential_id=values["BEDROCK_KEY_CREDENTIAL_ID"],
        region=values.get("BEDROCK_KEY_REGION", "us-east-1"),
        profile=values.get("BEDROCK_KEY_ADMIN_PROFILE"),
        stack_name=values.get(
            "BEDROCK_KEY_CONTROLS_STACK", "graphrag-bedrock-controls"
        ),
    )


def extract_handler_source(template: Path) -> str:
    """Return the Python inside the BreakerFunction ZipFile block."""
    lines = template.read_text().splitlines()

    try:
        marker = next(i for i, line in enumerate(lines) if line.strip() == "ZipFile: |")
    except StopIteration:
        raise ConfigError(f"no 'ZipFile: |' block in {template.name}") from None

    body_indent = len(lines[marker]) - len(lines[marker].lstrip())
    body: list[str] = []
    for line in lines[marker + 1 :]:
        indent = len(line) - len(line.lstrip())
        if line.strip() and indent <= body_indent:
            break
        body.append(line)

    source = textwrap.dedent("\n".join(body))
    if "def handler(" not in source:
        raise ConfigError("extracted block defines no handler()")
    return source


def load_handler_module(source: str, settings: Settings) -> types.ModuleType:
    """Execute the extracted source as a module.

    The handler reads its environment at import time, so the variables have to
    be in place before this runs.
    """
    os.environ["KEY_USER_NAME"] = settings.user_name
    os.environ["KEY_CREDENTIAL_ID"] = settings.credential_id

    module = types.ModuleType("breaker")
    module.__dict__["__file__"] = "<controls.template:ZipFile>"
    exec(compile(source, "<controls.template:ZipFile>", "exec"), module.__dict__)
    return module


class RecordingIam:
    """Stands in for the IAM client on a dry run and records the one call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def update_service_specific_credential(self, **kwargs: str) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def sns_event(alarm_name: str) -> dict[str, object]:
    """Build the SNS envelope CloudWatch sends when an alarm fires."""
    message = {
        "AlarmName": alarm_name,
        "NewStateValue": "ALARM",
        "NewStateReason": "run_breaker.py synthetic event",
    }
    return {"Records": [{"Sns": {"Message": json.dumps(message)}}]}


def credential_status(settings: Settings) -> str:
    """Return the live status of the credential, or why it could not be read."""
    session = boto3.Session(profile_name=settings.profile, region_name=settings.region)
    iam = session.client("iam")
    found = iam.list_service_specific_credentials(
        UserName=settings.user_name, ServiceName="bedrock.amazonaws.com"
    )["ServiceSpecificCredentials"]
    for entry in found:
        if entry["ServiceSpecificCredentialId"] == settings.credential_id:
            return str(entry["Status"])
    return f"not found on user {settings.user_name}"


def run_local(settings: Settings, args: argparse.Namespace) -> int:
    """Execute the extracted handler in this process."""
    source = extract_handler_source(args.template)
    print(f"handler: {len(source.splitlines())} lines from {args.template.name}")

    if settings.profile and args.execute:
        os.environ["AWS_PROFILE"] = settings.profile
    os.environ.setdefault("AWS_DEFAULT_REGION", settings.region)

    module = load_handler_module(source, settings)

    stub: RecordingIam | None = None
    if not args.execute:
        stub = RecordingIam()
        module.__dict__["_iam"] = stub

    print(f"target:  {settings.credential_id} on user {settings.user_name}")
    print(
        f"mode:    {'EXECUTE, this deactivates the key' if args.execute else 'dry run'}"
    )
    print("--- handler output ---")
    sys.stdout.flush()

    result = module.handler(sns_event(args.alarm_name), None)

    sys.stderr.flush()
    print("--- end handler output ---")

    print()
    print(f"returned: {json.dumps(result, indent=2)}")

    if stub is not None:
        print(f"suppressed IAM calls: {json.dumps(stub.calls, indent=2)}")
        print("\nNothing was changed. Re-run with --execute to deactivate for real.")
    else:
        print(f"\nlive status now: {credential_status(settings)}")
        print("Bring it back with uv run bedrock-key/admin.py resume")

    return 0


def run_remote(settings: Settings, args: argparse.Namespace) -> int:
    """Invoke the deployed function rather than the local copy."""
    if not args.execute:
        print(
            f"--remote would invoke {settings.function_name} in "
            f"{settings.region}, which deactivates the key.\n"
            "Add --execute to do it."
        )
        return 0

    session = boto3.Session(profile_name=settings.profile, region_name=settings.region)
    response = session.client("lambda").invoke(
        FunctionName=settings.function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(sns_event(args.alarm_name)).encode(),
    )
    payload = response["Payload"].read().decode()

    print(f"status: {response['StatusCode']}")
    if "FunctionError" in response:
        print(f"FunctionError: {response['FunctionError']}")
        print(payload)
        print("\nThe key was probably NOT deactivated. Check it by hand.")
        return 1

    print(f"payload: {payload}")
    print(f"\nlive status now: {credential_status(settings)}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"env file to read (default: {DEFAULT_ENV_FILE.name})",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE,
        help=f"template holding the handler (default: {DEFAULT_TEMPLATE.name})",
    )
    parser.add_argument(
        "--alarm-name",
        default="graphrag-bedrock-controls-burst-spend",
        help="alarm name to put in the synthetic SNS event",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="invoke the deployed function instead of the local copy",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="really call IAM. Without this the run changes nothing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    args = parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        return run_remote(settings, args) if args.remote else run_local(settings, args)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
