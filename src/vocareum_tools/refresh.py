"""Refresh the account and stack pins after Vocareum rotates the pool.

Ending a lab session returns its AWS account to a pool. The next session may
land in another account, which makes both ``LAB_AWS_ACCOUNT_ID`` and
``LAB_STACK_NAME`` stale. Every guarded tool then refuses correctly, but the
operator used to have to recover both values by hand.

This command reads an already-open session and starts or extends nothing. It
gets the account from STS and considers only CloudFormation stacks that are not
being deleted or already deleted. Exactly one such stack must exist. Zero is no
answer and more than one is ambiguous, so both are refusals with no override.

A blank ``LAB_AWS_ACCOUNT_ID`` line is accepted, which is how a new course gets
its first pin. The account guard did not apply here even before: refresh writes
the STS answer and never compares it, so requiring the old value only forced
the operator to copy the account in by hand first.

Both pins must come from the same dotenv file. For a selected course that file
must be ``courses/<name>.env``. Real environment overrides are refused because
editing a shadowed file would report success without changing what tools read.
The two replacements are rendered in memory and installed with one atomic file
replacement, preserving every other line and comment.

Exit codes:
    0  The dry run or refresh completed.
    1  Configuration or discovery was refused; nothing was written.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from .client import ApiError
from .config import ConfigError, Environment, add_course_argument, repository_root
from .diagnose import STACK_STATUS_FILTER, fetch_stack_summaries
from .output import section
from .probe import (
    BOTO_CONFIG,
    Config,
    GuardError,
    build_session,
    fetch_vocareum_credentials,
    guard_credentials,
    load_config,
)

EXIT_OK = 0
EXIT_REFUSED = 1

ACCOUNT_VAR = "LAB_AWS_ACCOUNT_ID"
STACK_VAR = "LAB_STACK_NAME"
REFRESH_VARS = (ACCOUNT_VAR, STACK_VAR)

DELETING_STACK_STATUSES = frozenset({"DELETE_IN_PROGRESS", "DELETE_COMPLETE"})
REFRESHABLE_STACK_STATUSES = frozenset(STACK_STATUS_FILTER) - DELETING_STACK_STATUSES


class RefreshError(RuntimeError):
    """Raised when fresh values cannot be identified or safely written."""


@dataclass(frozen=True)
class FreshValues:
    """The two session-scoped values that rotate together."""

    account_id: str
    stack_name: str

    def as_dict(self) -> dict[str, str]:
        """Return the values keyed by their dotenv variable names."""
        return {ACCOUNT_VAR: self.account_id, STACK_VAR: self.stack_name}


def refresh_target(environment: Environment) -> Path:
    """Return the one file that safely owns both refreshable values."""
    shadowed = [name for name in REFRESH_VARS if environment.is_shadowed(name)]
    if shadowed:
        raise RefreshError(
            "Real environment variables shadow the file values: "
            + ", ".join(shadowed)
            + ". Unset them before refreshing; a file edit would not change "
            "what the tools read."
        )

    missing = [name for name in REFRESH_VARS if environment.source_of(name) is None]
    if missing:
        raise RefreshError(
            "Refresh refuses to add variables it did not read from a file: "
            + ", ".join(missing)
            + ". Add both pins to the intended env file first."
        )

    sources = {environment.source_of(name) for name in REFRESH_VARS}
    if len(sources) != 1:
        detail = ", ".join(
            f"{name}={environment.source_of(name)}" for name in REFRESH_VARS
        )
        raise RefreshError(
            "The account and stack pins came from different files. Refusing a "
            f"cross-file update: {detail}."
        )

    target = sources.pop()
    assert target is not None
    if environment.course and target != environment.sources[-1]:
        raise RefreshError(
            f"Course {environment.course!r} inherits its account and stack from "
            f"{target}. Put both values in {environment.sources[-1]} before "
            "refreshing so one course cannot rewrite another course's pins."
        )
    return target


def select_stack(summaries: list[dict[str, Any]]) -> str:
    """Return the only non-deleting stack name, refusing zero or many."""
    candidates = [
        summary
        for summary in summaries
        if str(summary.get("StackStatus", "")) in REFRESHABLE_STACK_STATUSES
        and str(summary.get("StackName", "")).strip()
    ]
    if len(candidates) != 1:
        found = ", ".join(
            f"{item.get('StackName', '(unnamed)')} "
            f"({item.get('StackStatus', 'unknown')})"
            for item in candidates
        )
        detail = found or "none"
        raise RefreshError(
            "Refresh requires exactly one non-deleting CloudFormation stack in "
            f"the session account; found {len(candidates)}: {detail}. Nothing "
            "was written."
        )
    return str(candidates[0]["StackName"]).strip()


def caller_account(session: Any, region: str) -> str:
    """Read and validate the account id for the session credentials."""
    try:
        sts = session.client("sts", region_name=region, config=BOTO_CONFIG)
        identity = sts.get_caller_identity()
    except (ClientError, BotoCoreError, NoCredentialsError) as error:
        raise RefreshError(f"sts:GetCallerIdentity failed: {error}") from error
    account = str(identity.get("Account", "")).strip()
    if not re.fullmatch(r"\d{12}", account):
        raise RefreshError(
            f"sts:GetCallerIdentity returned an unusable account id: {account!r}."
        )
    return account


def discover_values(config: Config) -> FreshValues:
    """Read one open session and discover both values without starting it."""
    section("REFRESH 1: Read the open session")
    credentials = fetch_vocareum_credentials(config, wait=False)
    guard_credentials(credentials)
    session = build_session(credentials)

    section("REFRESH 2: Discover the account and stack")
    account = caller_account(session, config.region)
    try:
        cloudformation = session.client(
            "cloudformation", region_name=config.region, config=BOTO_CONFIG
        )
    except BotoCoreError as error:
        raise RefreshError(
            f"Could not create the CloudFormation client: {error}"
        ) from error
    stack = select_stack(fetch_stack_summaries(cloudformation))
    print(f"  {ACCOUNT_VAR}={account}")
    print(f"  {STACK_VAR}={stack}")
    return FreshValues(account, stack)


def replace_line(line: str, name: str, value: str) -> tuple[str, bool]:
    """Replace one dotenv assignment while preserving its syntax and comment."""
    ending = ""
    body = line
    if body.endswith("\r\n"):
        body, ending = body[:-2], "\r\n"
    elif body.endswith("\n"):
        body, ending = body[:-1], "\n"

    match = re.match(
        rf"^(?P<prefix>\s*(?:export\s+)?{re.escape(name)}\s*=\s*)(?P<rest>.*)$",
        body,
    )
    if match is None:
        return line, False

    rest = match.group("rest")
    comment = re.search(r"\s+#", rest)
    if comment is None:
        token = rest.rstrip()
        suffix = rest[len(token) :]
    else:
        token = rest[: comment.start()].rstrip()
        suffix = rest[len(token) :]

    stripped = token.strip()
    quote = stripped[0] if len(stripped) >= 2 and stripped[0] == stripped[-1] else ""
    if quote not in {"'", '"'}:
        quote = ""
    rendered = f"{quote}{value}{quote}"
    return f"{match.group('prefix')}{rendered}{suffix}{ending}", True


def render_refresh(text: str, values: FreshValues) -> str:
    """Render both replacements, refusing absent or duplicate assignments."""
    lines = text.splitlines(keepends=True)
    replacements = values.as_dict()
    counts = dict.fromkeys(replacements, 0)
    rendered: list[str] = []
    for line in lines:
        current = line
        for name, value in replacements.items():
            current, matched = replace_line(current, name, value)
            counts[name] += int(matched)
        rendered.append(current)

    invalid = {name: count for name, count in counts.items() if count != 1}
    if invalid:
        detail = ", ".join(f"{name}={count} lines" for name, count in invalid.items())
        raise RefreshError(
            "Each refreshable variable must appear exactly once in its source "
            f"file; found {detail}. Nothing was written."
        )
    return "".join(rendered)


def atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` atomically while retaining its permission bits."""
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise RefreshError(f"Could not replace {path}: {error}") from error


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Refresh LAB_AWS_ACCOUNT_ID and LAB_STACK_NAME from an already-open "
            "Vocareum session. This command never starts or extends a session."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the base .env file. Defaults to the repository root.",
    )
    add_course_argument(parser)
    parser.add_argument("--region", help="AWS region. Defaults to AWS_REGION.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the source file and fresh lines without writing them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Refresh the selected configuration file from an open session."""
    args = parse_args(argv)
    env_path = args.env_file.expanduser()
    try:
        environment = Environment.from_file(env_path, args.course)
        target = refresh_target(environment)
        config = load_config(
            env_path, args.region, None, True, args.course, require_account=False
        )
        values = discover_values(config)
        original = target.read_text(encoding="utf-8")
        rendered = render_refresh(original, values)

        section("REFRESH 3: Update the pins")
        print(environment.describe())
        print(f"source: {target}")
        for name, value in values.as_dict().items():
            print(f"  {name}={value}")
        if args.dry_run:
            print("Dry run: nothing written.")
            return EXIT_OK

        atomic_write(target, rendered)
        print("Refreshed both values with one atomic file replacement.")
        return EXIT_OK
    except (ApiError, ConfigError, GuardError, OSError, RefreshError) as error:
        print(f"REFRESH REFUSED\n{error}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
