"""End a Vocareum lab session from the command line.

**There is no End Lab button in the student's AWS view.** That absence is the
whole reason the account sweep exists: a student cannot ask for a teardown, so
the teardown has to be wired to the one session-end signal that always fires,
which is CloudFormation deleting the stack. ``docs/vocareum-api.md`` records the
REST call that produces that signal, and its caller column read "by hand" until
this module. Ending a session by hand meant the sweep-on-Delete path could not be
exercised without an operator clicking through a UI that does not offer the
button, so it went unmeasured.

This is an **operator** tool. A student has no API key, so nothing here is
reachable from a notebook. It exists to force the Delete lifecycle on demand.

Three facts about the call shape it against ``docs/vocareum-api.md``:

**It is synchronous.** It answers ``200`` with ``{"status": "success"}`` and no
``transactionid``. Every other write in this repository is asynchronous and
returns a transaction to poll, so a client that assumes one waits forever on a
field that never arrives. Nothing here polls a transaction.

**Ending a session gives up the AWS account.** The pool reallocates, the released
member goes to ``status: cleanup`` and is wiped, and a different member is
allocated on the next start. So anything left behind is unreachable rather than
merely untidy: it cannot be inspected or cleaned by hand afterwards. Sweep first.
This tool refuses to act without ``--yes`` for that reason, and it prints the
account id and the pool before it ends anything, because ``account_id`` empties
on termination and is gone exactly when you want to know where the last run was.

**Only ``action: end`` is sent.** The reference documents ``end|terminate`` in one
line with no account of how they differ. ``terminate`` has never been exercised
here, so it is treated as unknown and destructive and this module does not expose
it. Adding it needs a measurement, not a flag.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import ApiError, request_json
from .config import (
    VOCAREUM_SESSION_VARS,
    ConfigError,
    Environment,
    add_course_argument,
    repository_root,
)
from .endpoints import DEFAULT_API_BASE, SessionEndpoints
from .output import dump_json, section
from .security import redact_payload

# The only action this tool sends. See the module docstring for why "terminate"
# is absent rather than optional.
END_ACTION = "end"

# What Vocareum reports once the session is down. Checked rather than assumed,
# because a 200 says the call was accepted and not that the state moved.
TERMINATED_STATE = "Terminated"
INACTIVE_STATUS = "inactive"


@dataclass(frozen=True)
class Config(SessionEndpoints):
    """Everything this command needs, resolved from the environment."""

    api_key: str


@dataclass(frozen=True)
class SessionFacts:
    """The account-scoped facts that stop being readable once the session ends.

    Recorded before the end call rather than after, because ``account_id`` and
    every credential field empty on termination while ``totalspend`` survives.
    Whether ``aws_linked_accounts`` survives is unmeasured, so the pool is read
    while it is certainly legible.
    """

    state: str
    status: str
    account_id: str
    total_spend: str
    allowed_spend: str
    session_end: str
    pool: list[dict[str, Any]]

    @property
    def live(self) -> bool:
        """True when the session is still up.

        Tests the fields rather than the containing dict. ``aws_data`` survives
        termination as a populated and therefore truthy dict with empty strings
        inside it, so ``if aws_data:`` reports a dead session as live.
        """
        return bool(self.account_id) and self.state != TERMINATED_STATE


def load_config(env_path: Path, course: str | None = None) -> Config:
    """Build a :class:`Config` from real environment variables and ``.env``."""
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
        api_base=(environment.get("VOCAREUM_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
    )


def read_facts(payload: Any) -> SessionFacts:
    """Pull the session facts out of a resources payload.

    Every field is optional on purpose. A terminated session answers with the
    same shape and empty strings inside it, and a tool that raises on a missing
    key cannot report that state at all.
    """
    resources = payload.get("resources") or [] if isinstance(payload, dict) else []
    resource = resources[0] if resources and isinstance(resources[0], dict) else {}
    clouds = resource.get("cloud") or []
    cloud = clouds[0] if clouds and isinstance(clouds[0], dict) else {}
    data = cloud.get("aws_data") or {}
    if not isinstance(data, dict):
        data = {}

    pool = data.get("aws_linked_accounts") or []
    return SessionFacts(
        state=str(resource.get("state", "")),
        status=str(cloud.get("status", "")),
        account_id=str(data.get("account_id", "")),
        total_spend=str(data.get("totalspend", "")),
        allowed_spend=str(data.get("allowedspend", "")),
        session_end=str(resource.get("sessionend", "")),
        pool=[member for member in pool if isinstance(member, dict)],
    )


def print_facts(facts: SessionFacts) -> None:
    """Print the facts worth having on the record before the account is given up."""
    print(f"  state         = {facts.state or '(absent)'}")
    print(f"  cloud status  = {facts.status or '(absent)'}")
    print(f"  account_id    = {facts.account_id or '(empty, so the session is down)'}")
    spend = f"{facts.total_spend or '?'} of {facts.allowed_spend or '?'} USD"
    print(f"  totalspend    = {spend}")
    print(f"  sessionend    = {facts.session_end or '(absent)'}")
    if facts.pool:
        print("  aws_linked_accounts:")
        for member in facts.pool:
            account = member.get("account", "?")
            print(
                f"    {account}  inuse={member.get('inuse', '?')}  "
                f"status={member.get('status', '?')}"
            )
    else:
        print("  aws_linked_accounts: (absent, so the pool is no longer legible)")


def fetch_facts(config: Config) -> SessionFacts:
    """Read the session resource and report what it says."""
    url = config.resource_url()
    print(f"GET {url}")
    payload = request_json("GET", url, config.api_key)
    return read_facts(payload)


def end_session(config: Config) -> Any:
    """Send the end call and return the decoded response.

    No transaction poll. This endpoint is synchronous, which is the one way it
    differs from every other write here.
    """
    url = config.resource_url()
    print(f"DELETE {url}")
    print(f'body: {{"action": "{END_ACTION}"}}')
    return request_json("DELETE", url, config.api_key, body={"action": END_ACTION})


def run(config: Config, args: argparse.Namespace) -> int:
    """Read the session, end it, and confirm the state actually moved."""
    section("STEP 1: Read the session before ending it")
    print("Recorded now because account_id and the pool stop being readable after.")
    before = fetch_facts(config)
    print_facts(before)

    if not before.live:
        print("\nThis session is already down. Nothing to end.")
        return 0

    if not args.yes:
        section("DRY RUN")
        print("Nothing was ended. A dry run is the default.")
        print(f"\nEnding this session gives up AWS account {before.account_id}.")
        print("The pool reallocates, the released member is wiped, and anything")
        print("left in it becomes unreachable rather than merely untidy. Run this")
        print("first, and only pass --yes once it reports nothing to delete:")
        print(f"\n  uv run vocareum-sweep --no-start --account {before.account_id}")
        print("\nThen re-run this with --yes.")
        return 0

    section("STEP 2: End the session")
    payload = end_session(config)
    print(f"Response: {dump_json(redact_payload(payload))}")

    section("STEP 3: Confirm the state moved")
    print("A 200 says the call was accepted, not that the session is down.")
    after = fetch_facts(config)
    print_facts(after)

    section("RESULT")
    ended = after.state == TERMINATED_STATE or after.status == INACTIVE_STATUS
    if not ended:
        print(f"The call returned 200 but state is still {after.state or '(absent)'}.")
        print("Re-read the session before assuming it is down.")
        return 1

    print(f"Session ended. Account {before.account_id} has been given up.")
    print("CloudFormation tears the stack down asynchronously over the next few")
    print("minutes, so a stack listing taken now can still show it alive.")
    print("DELETE_COMPLETE is the durable signature of a session that has ended.")
    print("\nThe next start allocates a different pool member, so LAB_AWS_ACCOUNT_ID")
    print("in .env is now stale. Read the new allocation rather than guessing it.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "End a Vocareum lab session, which is what forces the CloudFormation "
            "stack Delete the account sweep hangs off. Prints what it would give "
            "up and changes nothing unless --yes is passed."
        )
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Actually end the session. Without it this is a dry run and the "
            "default. Sweep the account first: once the session ends the "
            "account is wiped and cannot be inspected."
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the .env file. Defaults to the repository root.",
    )
    add_course_argument(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``vocareum-end-lab``."""
    args = parse_args(argv)
    try:
        config = load_config(args.env_file, args.course)
    except ConfigError as error:
        print(f"Configuration error: {error}")
        return 2

    print(f"Vocareum API base : {config.api_base}")
    print(f"Course/Assignment : {config.course_id}/{config.assignment_id}")
    print(f"Part/User         : {config.part_id}/{config.user_id}")

    try:
        return run(config, args)
    except ApiError as error:
        print(f"\nAPI error: {error}")
        return 1
