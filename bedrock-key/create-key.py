#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "boto3>=1.39",
# ]
# ///
"""Create the scoped workshop Bedrock key and save it without printing it."""

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parent
DEFAULT_CONFIG = SCRIPT_DIR / "arns.env"
DEFAULT_OUTPUT = SCRIPT_DIR / "bedrock-key.env"
POLICY_PATH = SCRIPT_DIR / "policy.json"
USER_PATH = "/workshop/"
DEFAULT_CREDENTIAL_AGE_DAYS = 14
SERVICE_NAME = "bedrock.amazonaws.com"

REQUIRED_CONFIG = (
    "BEDROCK_KEY_ACCOUNT_ID",
    "BEDROCK_KEY_REGION",
    "BEDROCK_KEY_USER_NAME",
    "BEDROCK_KEY_POLICY_NAME",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        help=(
            "AWS profile used to create IAM resources (default: AWS_PROFILE or "
            "BEDROCK_KEY_ADMIN_PROFILE from arns.env)"
        ),
    )
    parser.add_argument(
        "--credential-age-days",
        type=int,
        default=DEFAULT_CREDENTIAL_AGE_DAYS,
        metavar="DAYS",
        help=(
            "credential lifetime from 1 to 36600 days "
            f"(default: {DEFAULT_CREDENTIAL_AGE_DAYS})"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"secret env file to create (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "validate local inputs and print the plan without calling AWS or "
            "writing files"
        ),
    )
    return parser.parse_args()


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(
            f"Missing {path}. Copy arns.env.example to arns.env and fill in its values."
        )

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"Invalid assignment at {path}:{line_number}")
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("'\"")
    return values


def validate_inputs(
    config: dict[str, str], credential_age_days: int, output: Path
) -> None:
    missing = [name for name in REQUIRED_CONFIG if not config.get(name)]
    if missing:
        raise SystemExit(
            f"Missing required values in {DEFAULT_CONFIG}: {', '.join(missing)}"
        )
    if not re.fullmatch(r"\d{12}", config["BEDROCK_KEY_ACCOUNT_ID"]):
        raise SystemExit("BEDROCK_KEY_ACCOUNT_ID must be a 12-digit AWS account id")
    if not 1 <= credential_age_days <= 36600:
        raise SystemExit("--credential-age-days must be between 1 and 36600")
    if output.exists():
        raise SystemExit(
            f"Refusing to overwrite {output}. Preserve or remove the old key "
            "file first."
        )
    if not output.parent.is_dir():
        raise SystemExit(f"Output directory does not exist: {output.parent}")
    if not os.access(output.parent, os.W_OK):
        raise SystemExit(f"Output directory is not writable: {output.parent}")
    if not os.access(DEFAULT_CONFIG, os.W_OK):
        raise SystemExit(f"Config file is not writable: {DEFAULT_CONFIG}")
    ensure_git_ignored(output)


def ensure_git_ignored(output: Path) -> None:
    try:
        relative_output = output.relative_to(REPOSITORY_ROOT)
    except ValueError as error:
        raise SystemExit(
            f"Secret output must be inside {REPOSITORY_ROOT} and ignored by Git"
        ) from error

    result = subprocess.run(
        ["git", "check-ignore", "--quiet", str(relative_output)],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"Secret output is not ignored by Git: {output}")


def rendered_policy(account_id: str) -> str:
    raw_policy = POLICY_PATH.read_text(encoding="utf-8")
    policy_text = raw_policy.replace("${BEDROCK_KEY_ACCOUNT_ID}", account_id)
    if "${" in policy_text:
        raise SystemExit(f"Unresolved variable in {POLICY_PATH}")
    try:
        policy = json.loads(policy_text)
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid JSON in {POLICY_PATH}: {error}") from error
    return json.dumps(policy, separators=(",", ":"))


def expected_user_arn(account_id: str, user_name: str) -> str:
    return f"arn:aws:iam::{account_id}:user{USER_PATH}{user_name}"


def reconcile_user(iam, config: dict[str, str], policy: str) -> None:
    user_name = config["BEDROCK_KEY_USER_NAME"]
    expected_arn = expected_user_arn(config["BEDROCK_KEY_ACCOUNT_ID"], user_name)
    try:
        user = iam.get_user(UserName=user_name)["User"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchEntity":
            raise
        user = iam.create_user(
            Path=USER_PATH,
            UserName=user_name,
            Tags=[
                {"Key": "WorkshopResource", "Value": "graphrag-with-neo4j"},
                {"Key": "Purpose", "Value": "workshop-shared-bedrock-key"},
            ],
        )["User"]
        print(f"Created IAM user {user['Arn']}")
    else:
        if user["Arn"] != expected_arn or user["Path"] != USER_PATH:
            raise SystemExit(
                f"Existing user has unexpected identity {user['Arn']}; "
                f"expected {expected_arn}"
            )
        print(f"Using existing IAM user {user['Arn']}")

    iam.put_user_policy(
        UserName=user_name,
        PolicyName=config["BEDROCK_KEY_POLICY_NAME"],
        PolicyDocument=policy,
    )
    print(f"Applied inline policy {config['BEDROCK_KEY_POLICY_NAME']}")


def ensure_no_existing_key(iam, user_name: str) -> None:
    response = iam.list_service_specific_credentials(
        UserName=user_name,
        ServiceName=SERVICE_NAME,
    )
    credentials = response.get("ServiceSpecificCredentials", [])
    if not credentials:
        return

    details = ", ".join(
        f"{item['ServiceSpecificCredentialId']} ({item['Status']})"
        for item in credentials
    )
    raise SystemExit(
        "Refusing to create a second Bedrock key. Delete or otherwise resolve the "
        f"existing credential first: {details}"
    )


def expiration_text(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def secret_env_text(secret: str, region: str, credential_id: str, expires: str) -> str:
    values = {
        "AWS_BEARER_TOKEN_BEDROCK": secret,
        "AWS_DEFAULT_REGION": region,
        "BEDROCK_KEY_CREDENTIAL_ID": credential_id,
        "BEDROCK_KEY_CREDENTIAL_EXPIRES": expires,
    }
    lines = [
        "# Generated by bedrock-key/create-key.py. Contains a live secret.",
        "# Keep this file local and delete it after the credential is retired.",
    ]
    lines.extend(f"{name}={shlex.quote(value)}" for name, value in values.items())
    return "\n".join(lines) + "\n"


def atomic_write_secret(path: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output_file:
            output_file.write(text)
            output_file.flush()
            os.fsync(output_file.fileno())
        temporary_path.replace(path)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def reserve_output(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            stat.S_IRUSR | stat.S_IWUSR,
        )
    except FileExistsError as error:
        raise SystemExit(f"Refusing to overwrite {path}") from error
    os.close(descriptor)


def update_config_credential(credential_id: str, expires: str) -> None:
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    replacements = {
        "BEDROCK_KEY_CREDENTIAL_ID": credential_id,
        "BEDROCK_KEY_CREDENTIAL_EXPIRES": expires,
    }
    for name, value in replacements.items():
        pattern = re.compile(rf"^{re.escape(name)}=.*$", re.MULTILINE)
        text, count = pattern.subn(f"{name}={value}", text)
        if count != 1:
            raise RuntimeError(
                f"Expected exactly one {name} assignment in {DEFAULT_CONFIG}"
            )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{DEFAULT_CONFIG.name}.", suffix=".tmp", dir=DEFAULT_CONFIG.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            config_file.write(text)
            config_file.flush()
            os.fsync(config_file.fileno())
        temporary_path.replace(DEFAULT_CONFIG)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def print_plan(
    config: dict[str, str], profile: str | None, credential_age_days: int, output: Path
) -> None:
    print("Dry run: no AWS calls or file writes were made.")
    print(f"AWS profile: {profile or 'default credential chain'}")
    print(
        "IAM user: "
        + expected_user_arn(
            config["BEDROCK_KEY_ACCOUNT_ID"], config["BEDROCK_KEY_USER_NAME"]
        )
    )
    print(f"Inline policy: {config['BEDROCK_KEY_POLICY_NAME']} from {POLICY_PATH}")
    print(f"Credential lifetime: {credential_age_days} days")
    print(f"Secret output: {output} (mode 0600)")
    print(f"Credential metadata: {DEFAULT_CONFIG}")


def main() -> int:
    args = parse_args()
    output = args.output.expanduser().resolve()
    config = read_env(DEFAULT_CONFIG)
    validate_inputs(config, args.credential_age_days, output)
    policy = rendered_policy(config["BEDROCK_KEY_ACCOUNT_ID"])
    profile = (
        args.profile
        or os.environ.get("AWS_PROFILE")
        or config.get("BEDROCK_KEY_ADMIN_PROFILE")
    )

    if args.dry_run:
        print_plan(config, profile, args.credential_age_days, output)
        return 0

    reserve_output(output)
    iam = None
    credential_id = None
    secret_saved = False
    try:
        session = boto3.Session(
            profile_name=profile, region_name=config["BEDROCK_KEY_REGION"]
        )
        caller_account = session.client("sts").get_caller_identity()["Account"]
        if caller_account != config["BEDROCK_KEY_ACCOUNT_ID"]:
            raise SystemExit(
                f"AWS profile resolves to account {caller_account}; expected "
                f"{config['BEDROCK_KEY_ACCOUNT_ID']}"
            )

        iam = session.client("iam")
        reconcile_user(iam, config, policy)
        ensure_no_existing_key(iam, config["BEDROCK_KEY_USER_NAME"])
        response = iam.create_service_specific_credential(
            UserName=config["BEDROCK_KEY_USER_NAME"],
            ServiceName=SERVICE_NAME,
            CredentialAgeDays=args.credential_age_days,
        )["ServiceSpecificCredential"]

        credential_id = response["ServiceSpecificCredentialId"]
        secret = response["ServiceCredentialSecret"]
        expires = expiration_text(response["ExpirationDate"])
        atomic_write_secret(
            output,
            secret_env_text(
                secret,
                config["BEDROCK_KEY_REGION"],
                credential_id,
                expires,
            ),
        )
        secret_saved = True
        update_config_credential(credential_id, expires)
    except BaseException:
        if credential_id is None:
            output.unlink(missing_ok=True)
        elif not secret_saved:
            try:
                iam.delete_service_specific_credential(
                    UserName=config["BEDROCK_KEY_USER_NAME"],
                    ServiceSpecificCredentialId=credential_id,
                )
            except Exception:
                print(
                    f"WARNING: could not roll back credential {credential_id}; "
                    "delete it manually.",
                    file=sys.stderr,
                )
            else:
                output.unlink(missing_ok=True)
                print(
                    f"Rolled back credential {credential_id} after the secret "
                    "could not be saved.",
                    file=sys.stderr,
                )
        raise

    print(f"Created Bedrock credential {credential_id}, expiring {expires}")
    print(f"Saved its one-time secret to {output} with mode 0600")
    print(f"Updated credential metadata in {DEFAULT_CONFIG}")
    print("The secret was not printed. Store it securely before sharing or cleanup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
