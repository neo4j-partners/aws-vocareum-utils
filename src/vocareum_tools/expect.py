"""Check a live lab against a manifest of what a finished lab holds.

Before this existed, "did the lab come up" was answered by reading output.
``vocareum-diagnose`` prints the stack, the events, and the resources, and a
person decides. That works while one person knows what the stack is supposed to
contain, and it stops working the moment there is a second course, because the
knowledge that used to live in the reader's head is now course-specific and
undocumented.

So the expectation becomes a file. ``expected.json`` lists what a finished lab
holds, this module looks every entry up read-only, and the exit code is the
answer. One manifest per course, selected the same way every other tool selects
a course, by ``--course NAME``.

Three rules the shape here follows:

**A gate reports on the right account or it does not report.** It runs the same
three guards as :mod:`probe` through :func:`probe.open_guarded_session`, and it
makes a wrong region fatal. A gate is a pass or fail verdict, so a pass against
the wrong account is worse than no answer at all. ``diagnose`` deliberately
enforces no account guard, which is why the gate hangs off ``probe`` instead.

**Absent and unreadable are different answers.** An IAM shortfall or an SCP deny
on the read means the gate does not know whether the resource is there. Both
fail the gate, because an unverified expectation is not a met one, but they are
reported apart and the denial is classified with the shared
:func:`probe.classify` vocabulary, so a gate failure that is really a missing
grant says so.

**Ready is narrower than diagnose's success.** ``diagnose.SUCCESS_STACK_STATUSES``
counts ``DELETE_COMPLETE``, correctly, because a clean teardown is a success
there. A gate asking whether the lab is up must not count a deleted stack, so it
carries its own :data:`READY_STACK_STATUSES`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .client import ApiError
from .config import ConfigError, Environment, add_course_argument, repository_root
from .course import manifest_path as course_manifest_path
from .output import rule, section
from .probe import (
    BOTO_CONFIG,
    REQUIRED_REGION,
    GuardError,
    Verdict,
    classify,
    clip,
    load_config,
    open_guarded_session,
)
from .selection import BUCKET_GONE_CODES

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_GUARD = 2
EXIT_UNMET = 3

DEFAULT_MANIFEST_NAME = "expected.json"

# The only stack states that mean the lab is up. Narrower than diagnose's set on
# purpose: see the module docstring.
READY_STACK_STATUSES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})

# A placeholder is either an environment value or one of the stack's own
# outputs. Outputs are what makes a manifest writable at all, because the bucket
# name, the parameter path and the log group name all carry the stack GUID or
# the stack name, so no literal could match them across two sessions.
PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_:]+)\}")
OUTPUT_PREFIX = "output:"


class ManifestError(RuntimeError):
    """The manifest is missing, unreadable, or says something unusable."""


class Presence(StrEnum):
    """What one lookup established about one expected thing.

    Only ``PRESENT`` passes. The other four are kept apart rather than collapsed
    into a single failure because they need different next actions: create the
    resource, wait, fix a grant, or fix the manifest.
    """

    PRESENT = "PRESENT"
    MISSING = "MISSING"
    NOT_READY = "NOT_READY"
    UNREADABLE = "UNREADABLE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Expectation:
    """One line of the manifest, before and after interpolation."""

    kind: str
    name: str
    raw: str

    def label(self) -> str:
        """Return the name, showing the placeholder it came from when it had one."""
        if self.raw == self.name:
            return self.name
        return f"{self.name}  (from {self.raw})"


@dataclass
class Result:
    """What one expectation turned out to be."""

    expectation: Expectation
    presence: Presence
    detail: str = ""
    verdict: Verdict | None = None

    def met(self) -> bool:
        """Report whether this expectation was satisfied."""
        return self.presence is Presence.PRESENT


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

# Every kind a manifest may name, and the IAM action each one is read with. The
# action is here rather than inferred because it is what classify names in a
# denial and what permission rows are keyed by, so a gate failure that is
# really a missing grant names the action to look up.
#
# Deliberately absent: models and DynamoDB tables. A gate line that reads as
# "the model is available" while really only listing model ids is worse than no
# line, and neither has a lookup this repository has measured. Add one with its
# evidence, not ahead of it.
#
# roles and role_policies stay here but are absent from the shipped manifest.
# iam:GetRole and iam:GetRolePolicy on voclabs are an explicit deny in
# Vocareum's own identity-based policy on the session role, measured 2026-08-23,
# so a line naming them could never pass. They remain valid kinds for roles the
# student creates, which the session role may read.
KIND_ACTIONS: dict[str, str] = {
    "stacks": "cloudformation:DescribeStacks",
    "outputs": "cloudformation:DescribeStacks",
    "roles": "iam:GetRole",
    "role_policies": "iam:GetRolePolicy",
    "buckets": "s3:HeadBucket",
    "parameters": "ssm:GetParameter",
    "log_groups": "logs:DescribeLogGroups",
    "functions": "lambda:GetFunction",
}

MANIFEST_KEYS = tuple(KIND_ACTIONS)

# The error code each read answers with when the thing is simply not there.
# Per kind, because there is no shared code: CloudFormation says ValidationError
# for a stack it never had, S3 answers a HEAD with a bare 404, and classify maps
# some of these to INCONCLUSIVE_BAD_INPUT, which is right for a probe measuring
# permission and wrong for a gate asking whether something exists.
KIND_ABSENT_CODES: dict[str, frozenset[str]] = {
    "stacks": frozenset({"ValidationError"}),
    "outputs": frozenset({"ValidationError"}),
    "roles": frozenset({"NoSuchEntity", "NoSuchEntityException"}),
    "role_policies": frozenset({"NoSuchEntity", "NoSuchEntityException"}),
    "buckets": BUCKET_GONE_CODES,
    "parameters": frozenset({"ParameterNotFound", "ParameterVersionNotFound"}),
    "log_groups": frozenset({"ResourceNotFoundException"}),
    "functions": frozenset({"ResourceNotFoundException", "404"}),
}

# Verdicts that mean the read was refused, so presence stayed unknown. Kept as a
# set of verdicts rather than a code test because the SCP-versus-IAM distinction
# is the one this repository exists to make, and the gate should preserve it.
DENIAL_VERDICTS = frozenset(
    {
        Verdict.SCP_DENY,
        Verdict.RCP_DENY,
        Verdict.IAM_DENY,
        Verdict.ACCESS_DENIED_UNCLASSIFIED,
        Verdict.DEPENDENCY_DENIED,
    }
)


def read_manifest(path: Path) -> dict[str, list[str]]:
    """Read and validate the manifest's shape, resolving no placeholder yet.

    Called before the session starts. A typo in the manifest costs nothing to
    find, and finding it after a lab launch costs a launch out of the budget.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError(
            f"No manifest at {path}. Write one, or pass --manifest. "
            "A course states what a finished lab holds; nothing infers it."
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"Could not read manifest {path}: {error}") from error

    if not isinstance(raw, dict):
        raise ManifestError(
            f"Manifest {path} must be a JSON object of kind to list of names."
        )

    unknown = sorted(set(raw) - set(MANIFEST_KEYS))
    if unknown:
        raise ManifestError(
            f"Manifest {path} names unknown kinds: {', '.join(unknown)}.\n"
            f"Known kinds: {', '.join(MANIFEST_KEYS)}."
        )

    manifest: dict[str, list[str]] = {}
    for kind in MANIFEST_KEYS:
        if kind not in raw:
            continue
        names = raw[kind]
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name.strip() for name in names
        ):
            raise ManifestError(
                f"Manifest {path}: {kind} must be a list of non-empty strings."
            )
        if names:
            manifest[kind] = [name.strip() for name in names]

    if not manifest:
        # A manifest that expects nothing passes on an account with nothing in
        # it, which is the exact failure a gate exists to catch.
        raise ManifestError(f"Manifest {path} expects nothing. That cannot pass.")
    return manifest


def resolve_env_placeholders(value: str, get: Any) -> str:
    """Replace ``${VAR}`` with configuration values, leaving ``${output:K}`` alone.

    Two passes exist because the two placeholder kinds are known at different
    times. Configuration is known before anything starts; a stack output is only
    known once the stack has been read, and reading it costs a live session.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.startswith(OUTPUT_PREFIX):
            return match.group(0)
        resolved = get(name)
        if not resolved:
            raise ManifestError(
                f"Manifest placeholder ${{{name}}} has no value. Set {name} in "
                ".env, in the selected course file, or in the environment."
            )
        return resolved

    return PLACEHOLDER.sub(replace, value)


def build_expectations(manifest: dict[str, list[str]], get: Any) -> list[Expectation]:
    """Turn the validated manifest into expectations with configuration resolved."""
    expectations: list[Expectation] = []
    for kind, names in manifest.items():
        for raw in names:
            expectations.append(
                Expectation(kind=kind, name=resolve_env_placeholders(raw, get), raw=raw)
            )
    return expectations


# ---------------------------------------------------------------------------
# Looking each one up
# ---------------------------------------------------------------------------


class Gate:
    """Read-only lookups against one live session.

    Holds the clients and the stack outputs so a manifest naming eight things in
    one stack reads that stack once. Every method here reads. Nothing in this
    module creates, changes, or deletes anything, which is what makes it safe to
    run against a student's session mid-workshop.
    """

    def __init__(self, session: boto3.session.Session, region: str) -> None:
        self.session = session
        self.region = region
        self._clients: dict[str, Any] = {}
        self._outputs: dict[str, str] | None = None
        self.stack_name = ""

    def client(self, name: str) -> Any:
        """Return a memoized client for one service."""
        if name not in self._clients:
            self._clients[name] = self.session.client(
                name, region_name=self.region, config=BOTO_CONFIG
            )
        return self._clients[name]

    # -- the lookups ------------------------------------------------------

    def stacks(self, name: str) -> tuple[Presence, str]:
        """Read one stack and require a status that means the lab is up."""
        response = self.client("cloudformation").describe_stacks(StackName=name)
        stacks = response.get("Stacks") or []
        if not stacks:
            return Presence.MISSING, "DescribeStacks returned no stack."
        stack = stacks[0]
        status = str(stack.get("StackStatus", ""))
        # Cache the outputs while the answer is in hand, and remember which
        # stack they came from so an ${output:K} placeholder can say so.
        self._outputs = {
            str(item.get("OutputKey", "")): str(item.get("OutputValue", ""))
            for item in stack.get("Outputs") or []
        }
        self.stack_name = name
        if status not in READY_STACK_STATUSES:
            return Presence.NOT_READY, f"StackStatus {status}."
        return Presence.PRESENT, f"StackStatus {status}."

    def outputs(self, name: str) -> tuple[Presence, str]:
        """Require one stack output to exist and to carry a value.

        An empty output is a failure, not a pass. ``lab.template`` builds its
        outputs from GetAtt on purpose, so a populated value is evidence the
        service really created the resource, and an empty one is evidence it
        did not.
        """
        outputs = self.stack_outputs()
        if name not in outputs:
            return Presence.MISSING, f"Stack {self.stack_name} has no output {name}."
        if not outputs[name]:
            return Presence.MISSING, f"Output {name} is empty."
        return Presence.PRESENT, clip(outputs[name], 60)

    def roles(self, name: str) -> tuple[Presence, str]:
        """Read one IAM role."""
        response = self.client("iam").get_role(RoleName=name)
        arn = response.get("Role", {}).get("Arn", "")
        return Presence.PRESENT, str(arn)

    def role_policies(self, name: str) -> tuple[Presence, str]:
        """Read one inline role policy, named ``role/policy``.

        Inline rather than managed because every grant ``lab.template`` applies
        is an ``AWS::IAM::RolePolicy``. That is not a style choice: Vocareum's
        session-start merge silently drops statements from an uploaded
        lab.policy, which is why lab.policy is retired.
        """
        role, separator, policy = name.partition("/")
        if not separator or not role or not policy:
            raise ManifestError(
                f"role_policies entry {name!r} must be written role/policy, "
                "for example voclabs/LabTemplateBedrockGrant."
            )
        self.client("iam").get_role_policy(RoleName=role, PolicyName=policy)
        return Presence.PRESENT, f"inline policy on {role}"

    def buckets(self, name: str) -> tuple[Presence, str]:
        """Head one bucket."""
        self.client("s3").head_bucket(Bucket=name)
        return Presence.PRESENT, "HeadBucket answered."

    def parameters(self, name: str) -> tuple[Presence, str]:
        """Read one Parameter Store entry without decrypting it."""
        response = self.client("ssm").get_parameter(Name=name)
        version = response.get("Parameter", {}).get("Version", "")
        return Presence.PRESENT, f"version {version}"

    def log_groups(self, name: str) -> tuple[Presence, str]:
        """Look for one log group by exact name.

        Matched exactly out of a prefix query, because DescribeLogGroups takes a
        prefix and answers with every group under it. A prefix hit would pass the
        gate on a differently named group that happens to start the same way.
        """
        response = self.client("logs").describe_log_groups(logGroupNamePrefix=name)
        for group in response.get("logGroups") or []:
            if group.get("logGroupName") == name:
                return Presence.PRESENT, "log group exists."
        return Presence.MISSING, f"No log group named exactly {name}."

    def functions(self, name: str) -> tuple[Presence, str]:
        """Read one Lambda function, by name or by ARN."""
        response = self.client("lambda").get_function(FunctionName=name)
        state = response.get("Configuration", {}).get("State", "")
        if state and state != "Active":
            return Presence.NOT_READY, f"State {state}."
        return Presence.PRESENT, f"State {state or 'unreported'}."

    # -- driving them -----------------------------------------------------

    def stack_outputs(self) -> dict[str, str]:
        """Return the cached stack outputs, or raise saying why there are none."""
        if self._outputs is None:
            raise ManifestError(
                "An ${output:KEY} placeholder or an outputs entry needs a stack, "
                "so the manifest must also name exactly one stack under "
                '"stacks". Nothing else knows which stack to read.'
            )
        return self._outputs

    def resolve_outputs(self, value: str) -> str:
        """Replace ``${output:KEY}`` with a value the stack really returned."""

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if not name.startswith(OUTPUT_PREFIX):
                return match.group(0)
            key = name.removeprefix(OUTPUT_PREFIX)
            outputs = self.stack_outputs()
            if not outputs.get(key):
                raise ManifestError(
                    f"Stack {self.stack_name} returned no value for output "
                    f"{key}, so ${{{name}}} cannot be resolved."
                )
            return outputs[key]

        return PLACEHOLDER.sub(replace, value)

    def check(self, expectation: Expectation) -> Result:
        """Look one expectation up and say what it turned out to be."""
        lookup = getattr(self, expectation.kind)
        try:
            resolved = self.resolve_outputs(expectation.name)
        except ManifestError as error:
            return Result(expectation, Presence.ERROR, str(error))
        expectation = Expectation(expectation.kind, resolved, expectation.raw)

        try:
            presence, detail = lookup(resolved)
            return Result(expectation, presence, detail)
        except ManifestError as error:
            return Result(expectation, Presence.ERROR, str(error))
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in KIND_ABSENT_CODES[expectation.kind]:
                return Result(expectation, Presence.MISSING, code)
            action = KIND_ACTIONS[expectation.kind]
            verdict, _code, message = classify(error, action)
            detail = clip(message)
            if verdict in DENIAL_VERDICTS:
                return Result(expectation, Presence.UNREADABLE, detail, verdict)
            return Result(expectation, Presence.ERROR, detail, verdict)
        except BotoCoreError as error:
            return Result(expectation, Presence.ERROR, clip(str(error)))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

# What to do about each way an expectation can fail to be met. Printed with the
# failure, because a gate that only says "no" sends the reader back to the
# stack events to work out which kind of no it was.
NEXT_ACTION: dict[Presence, str] = {
    Presence.MISSING: (
        "The lab did not create this, or something deleted it. Read the stack "
        "events with vocareum-diagnose --no-start."
    ),
    Presence.NOT_READY: (
        "It exists but has not finished. Re-run in a minute. A status that "
        "never settles is a lab.template failure, not a slow one."
    ),
    Presence.UNREADABLE: (
        "The read itself was refused, so whether the thing exists is unknown. "
        "This is a permission result: add the row to docs/permissions.md and "
        "fix the grant before trusting any gate run."
    ),
    Presence.ERROR: (
        "Neither present nor absent was established. Usually the manifest, not the lab."
    ),
}

# A missing stack is different from every other missing resource. The pinned
# stack name carries the pool account id and expires when Vocareum rotates the
# allocation, so stack recovery has to name rotation before stack events.
KIND_NEXT_ACTION: dict[tuple[str, Presence], str] = {
    ("stacks", Presence.MISSING): (
        "The account pool may have rotated, leaving LAB_STACK_NAME pinned to "
        "the prior session. Run vocareum-refresh with the same --course "
        "selection, then re-run the gate. If the refreshed stack is still "
        "missing, read its events with vocareum-diagnose --no-start."
    ),
}


def next_action(result: Result) -> str:
    """Return the recovery action for one failed expectation."""
    return KIND_NEXT_ACTION.get(
        (result.expectation.kind, result.presence), NEXT_ACTION[result.presence]
    )


def report(results: list[Result], as_json: bool) -> int:
    """Print the gate result and return the count of unmet expectations."""
    section(f"EXPECTATIONS ({len(results)})")
    width = max((len(result.expectation.kind) for result in results), default=0)
    for result in results:
        mark = "ok  " if result.met() else "FAIL"
        kind = result.expectation.kind.ljust(width)
        print(f"  {mark} {kind}  {result.expectation.label()}")
        if result.detail:
            print(f"         {result.detail}")

    unmet = [result for result in results if not result.met()]
    section("VERDICT")
    if not unmet:
        print(f"All {len(results)} expectations met. The lab is up.")
    else:
        print(f"{len(unmet)} of {len(results)} expectations not met.")
        for presence in Presence:
            group = [result for result in unmet if result.presence is presence]
            if not group:
                continue
            rule()
            print(f"{presence}: " + ", ".join(r.expectation.name for r in group))
            actions: dict[str, list[str]] = {}
            for result in group:
                actions.setdefault(next_action(result), []).append(
                    result.expectation.name
                )
            for action, names in actions.items():
                if len(actions) > 1:
                    print(f"{', '.join(names)}: {action}")
                else:
                    print(action)

    if as_json:
        print()
        print(
            json.dumps(
                [
                    {
                        "kind": result.expectation.kind,
                        "name": result.expectation.name,
                        "manifest": result.expectation.raw,
                        "presence": str(result.presence),
                        "verdict": str(result.verdict) if result.verdict else "",
                        "detail": result.detail,
                    }
                    for result in results
                ],
                indent=2,
            )
        )
    return len(unmet)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Check a live lab against a manifest of what it should hold.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help=(
            "Manifest of expectations. Defaults to courses/NAME/expected.json "
            f"for the selected course, or {DEFAULT_MANIFEST_NAME} beside the "
            "repository root."
        ),
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
    return parser.parse_args(argv)


def manifest_path(args: argparse.Namespace, course: str) -> Path:
    """Return the manifest to read and keep named courses fail closed.

    A course names what a finished lab holds, and two courses hold different
    things, so the manifest is per course the same way the env file is. The
    root default keeps the single-course case working with no course selected.
    """
    base = args.env_file.expanduser()
    if course:
        expected = course_manifest_path(base, course)
        if args.manifest and args.manifest.expanduser().resolve() != expected.resolve():
            raise ManifestError(
                f"Course {course!r} can use only {expected}, not {args.manifest}."
            )
        if not expected.is_file():
            raise ManifestError(
                f"Course {course!r} has no readiness manifest at {expected}. "
                "Refusing to use the root manifest for a named course."
            )
        return expected
    if args.manifest:
        return args.manifest.expanduser()
    return base.parent / DEFAULT_MANIFEST_NAME


def main(argv: list[str] | None = None) -> int:
    """Run the guards, look every expectation up, and report."""
    args = parse_args(argv)

    # The manifest is read and interpolated before anything starts a session.
    # Starting one spends a launch out of a finite budget, and a typo in a
    # manifest should not cost a launch to find.
    try:
        environment = Environment.from_file(args.env_file.expanduser(), args.course)
        manifest = read_manifest(manifest_path(args, environment.course))
        expectations = build_expectations(manifest, environment.get)
        config = load_config(
            args.env_file.expanduser(),
            args.region,
            args.expect_account,
            need_vocareum=args.source == "vocareum",
            course=args.course,
        )
    except (ConfigError, ManifestError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG

    section("GATE")
    print(environment.describe())
    print(f"manifest: {manifest_path(args, environment.course)}")
    print(f"expectations: {len(expectations)}")

    try:
        session = open_guarded_session(
            config,
            source=args.source,
            no_start=args.no_start,
            account_named=bool(args.expect_account),
            ambient_refusal=(
                "--source ambient requires --expect-account. Ambient "
                "credentials are exactly what the credential trap falls back "
                "to, so the account must be named on purpose."
            ),
            region_refusal=(
                "Region guard refused the run. This gate passes or fails a "
                f"lab, and every resource it looks for is in {REQUIRED_REGION} "
                "only, so anywhere else it would fail a lab that is fine."
            ),
        )
    except (ApiError, GuardError) as error:
        print(f"\nREFUSED TO REPORT\n{error}", file=sys.stderr)
        return EXIT_GUARD

    gate = Gate(session, config.region)
    results = [gate.check(expectation) for expectation in expectations]
    return EXIT_UNMET if report(results, args.json) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
