#!/usr/bin/env -S uv run
"""Run the student-role logs and network checks from Phase 5:A.

Rows 104 through 112, the nine CloudWatch Logs actions, plus rows 115, 116 and
117, which belong to Phase 4 and are folded in here because they are three read
calls and Phase 5 cannot meet its "no `Untested` rows remain" criterion without
them. Twelve rows, which is every row still `Untested` in the tracker.

``vocareum-probe`` cannot drive the logs block for the same reason it could not
drive Phases 2.5 and 3: the block is stateful. ``CreateLogStream`` needs a group
that exists, ``PutLogEvents`` needs a stream, ``DescribeLogStreams`` needs
something to describe, and the three delivery rows need a resource policy and a
destination before a delivery can join them. A flat list of probes with static
arguments cannot express that ordering, so the ordering lives here while the
guards and the error classifier stay shared with the general probe.

``Measurement``, ``Recorder``, ``wait_until_gone`` and ``teardown_guaranteed``
come from ``vocareum_tools.phase``. They were a copy of the Phase 2.5 runner's
versions, on the reasoning that a copy keeps a Phase 5 change from altering a
settled result. What it produced instead was divergence: the copy here had
already lost ``call_in_teardown``, and nothing would have reported that.

Three things about the shape of this block are worth knowing before reading the
code, because all three came out of reading the workshop content rather than
out of a run:

* **The log group name is not arbitrary.** ``static/iam_policy.json`` scopes the
  workshop's whole ``Logs`` statement to four log group ARN patterns, and
  ``/aws/lambda/hotel-booking-*`` is the one a student's own code writes to.
  Phase 2 established that a result does not carry across a prefix change, so
  the probe uses that prefix rather than a name of its own choosing.
* **The four delivery-side rows are not exercised by any workshop step.** The
  observability module reads two AgentCore log groups in the CloudWatch console
  and calls nothing. Rows 109 through 112 appear only in the
  ``static/iam_policy.json`` allow-list, which is the situation row 79 was
  closed ``N/A`` for. They are measured here anyway, because the module claims
  the runtime log group shows spans, and spans in ``aws/spans`` means
  Transaction Search, which is what those actions wire up.
* **A pass on rows 110 through 112 does not mean spans work.** Enabling
  Transaction Search also needs ``xray:UpdateTraceSegmentDestination`` and
  ``xray:UpdateIndexingRule``, and no X-Ray action appears anywhere in the
  workshop policy. That gap is a content finding rather than a measurement,
  so nothing here measures it.

Row 110 wants a live AgentCore runtime as its delivery source. The runtime the
deploy-path evidence run creates is torn down inside that run's own process, so
by default this probe sends a well-formed ARN for a runtime that does not exist
and takes whatever answer comes back: a not-found answer settles the row under
guideline 7, and a ``ValidationException`` does not, in which case the row needs
a second pass with ``--delivery-source-arn`` pointed at a live runtime. Which of
the two comes back is not predictable from the API model, so the probe records
it rather than assuming it.

    ./scripts/run_phase5_logs.py --all --output support/phase5a.json
    ./scripts/run_phase5_logs.py --block logs --block network --no-start
    ./scripts/run_phase5_logs.py --block delivery --delivery-source-arn ARN

Exit codes:
    0  Every planned action ran and was classified.
    1  Configuration error.
    2  A guard refused to report, or credentials could not be obtained.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from vocareum_tools.client import ApiError
from vocareum_tools.config import repository_root
from vocareum_tools.phase import (
    Recorder,
    json_default,
    teardown_guaranteed,
    wait_until_gone,
)
from vocareum_tools.probe import (
    BOTO_CONFIG,
    ConfigError,
    GuardError,
    Verdict,
    build_session,
    fetch_vocareum_credentials,
    guard_account,
    guard_credentials,
    guard_region,
    load_config,
    start_session,
)

BLOCKS = ("logs", "delivery", "network", "secret")

# `static/iam_policy.json` scopes the workshop's Logs statement to four log
# group patterns. This is the one a student's own code writes to, so it is the
# one worth measuring: Phase 2 found that an S3 result did not carry across a
# prefix change, and there is no reason to assume logs behave differently. The
# `phase5probe` suffix makes an abandoned group identifiable by sight.
LOG_GROUP = "/aws/lambda/hotel-booking-phase5probe"
LOG_STREAM = "phase5probe-stream"

# Named so that anything left behind names the phase that left it. There is no
# name for the resource policy: it is scoped to a log group ARN instead, and the
# API rejects a request carrying both.
DELIVERY_SOURCE_NAME = "phase5probe-source"
DELIVERY_DESTINATION_NAME = "phase5probe-destination"
SECRET_NAME = "phase5probe-neo4j-credentials"

# Row 117 gets two entries. The first asks whether the action is authorized at
# all, using a secret that does not exist, which under guideline 7 is a complete
# answer and creates nothing. The second reads a real secret, because the
# workshop reads real Neo4j credentials and a not-found answer is a weaker
# record of that than a value that came back.
MISSING_SECRET_NAME = "phase5probe-no-such-secret"

# AgentCore runtime ARNs look like this. Row 110 needs a delivery source and the
# workshop's source would be a runtime, so a well-formed ARN for a runtime that
# does not exist is the closest thing to the real call that creates nothing.
MISSING_RUNTIME_ID = "phase5probe_no_such_runtime-XXXXXXXXXX"


def probe_logs(logs: Any, recorder: Recorder) -> str | None:
    """Measure rows 104 through 108, and return the log group ARN if created.

    The ARN is what the delivery block needs as a destination, so the two blocks
    are ordered rather than independent. Returning ``None`` means the delivery
    block has nothing real to point at and says so instead of measuring against
    a name that does not exist.
    """
    # Cheapest call in the block and the only one that needs nothing to exist,
    # so it runs first. A denial here would not predict the rest: this one is
    # scoped to the whole account and the others to a log group ARN.
    recorder.call(
        "107",
        "logs:DescribeLogGroups",
        lambda: logs.describe_log_groups(limit=25),
    )

    verdict, _ = recorder.call(
        "104",
        "logs:CreateLogGroup",
        lambda: logs.create_log_group(logGroupName=LOG_GROUP),
        context=LOG_GROUP,
    )
    if verdict is not Verdict.OK:
        # Guideline 9: stop at a denial rather than working around it. Without a
        # group, rows 105, 106 and 108 would answer ResourceNotFoundException,
        # which the classifier reads as Allowed, and a run of Allowed rows
        # measured against nothing is worse than a gap.
        print("  CreateLogGroup did not succeed, so the rest of the block is skipped.")
        return None

    recorder.defer(
        f"log group {LOG_GROUP}",
        lambda: remove_log_group(logs, recorder),
    )

    verdict, _ = recorder.call(
        "105",
        "logs:CreateLogStream",
        lambda: logs.create_log_stream(
            logGroupName=LOG_GROUP, logStreamName=LOG_STREAM
        ),
        context=LOG_STREAM,
    )
    if verdict is Verdict.OK:
        recorder.call(
            "106",
            "logs:PutLogEvents",
            lambda: logs.put_log_events(
                logGroupName=LOG_GROUP,
                logStreamName=LOG_STREAM,
                logEvents=[
                    {
                        # Milliseconds, and CloudWatch rejects a timestamp too
                        # far from now, so it has to be the real clock.
                        "timestamp": int(time.time() * 1000),
                        "message": "Phase 5:A probe event",
                    }
                ],
            ),
            context=LOG_STREAM,
        )

    # Row 108 already has one incidental reading against it, taken on a
    # CodeBuild-created group while answering a different question, and the
    # ticket reports that reading as weak for exactly that reason. This is the
    # measurement it should have had: a group this run created, described on
    # purpose.
    recorder.call(
        "108",
        "logs:DescribeLogStreams",
        lambda: logs.describe_log_streams(logGroupName=LOG_GROUP, limit=5),
        context=LOG_GROUP,
    )

    return log_group_arn(logs)


def log_group_arn(logs: Any) -> str | None:
    """Return the ARN of the probe's log group, or None if it cannot be read.

    `CreateLogGroup` returns nothing, so the ARN has to be looked up. The
    lookup is not a tracker row and its failure is not a result: it means the
    delivery block loses its destination, which the caller reports.
    """
    try:
        groups = logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP, limit=5)
    except ClientError as error:
        print(f"        could not read the log group ARN: {error}")
        return None
    for group in groups.get("logGroups", []):
        if group.get("logGroupName") == LOG_GROUP:
            # The delivery destination wants the ARN without the trailing :*
            # that describe_log_groups appends.
            return str(group["arn"]).removesuffix(":*")
    print(f"        {LOG_GROUP} was created but does not appear in describe")
    return None


def remove_log_group(logs: Any, recorder: Recorder) -> None:
    """Delete the probe's log group and confirm it is gone.

    `DeleteLogGroup` is scaffolding rather than a tracker row: the workshop's
    policy does not grant it and no workshop step deletes a log group, so a
    failure here is residue to report, not a measurement.
    """
    logs.delete_log_group(logGroupName=LOG_GROUP)
    if not wait_until_gone(
        lambda: logs.describe_log_streams(logGroupName=LOG_GROUP, limit=1),
        f"log group {LOG_GROUP}",
    ):
        recorder.note(
            "104",
            "logs:CreateLogGroup (teardown)",
            Verdict.UNCLASSIFIED,
            f"{LOG_GROUP} was deleted but still answers describe, so the "
            "account may not be clean.",
        )


def report_ambient_policies(logs: Any) -> None:
    """Print the account-scoped resource policies this run is leaving alone.

    Not a tracker row and not a measurement. It exists so the run's own console
    log records the state of the one thing in this block that is account-wide
    configuration, which makes an unrelated later change attributable to
    something other than this probe.
    """
    try:
        response = logs.describe_resource_policies(limit=50, policyScope="ACCOUNT")
    except ClientError as error:
        # Reading is not required in order to write safely here, so a denial is
        # worth printing and nothing more.
        print(f"        could not list account resource policies: {error}")
        return
    names = [p.get("policyName") for p in response.get("resourcePolicies", [])]
    print(
        f"        account-scoped resource policies, left untouched: {names or 'none'}"
    )


def probe_delivery(
    logs: Any,
    recorder: Recorder,
    account: str,
    region: str,
    destination_arn: str | None,
    source_arn: str | None,
) -> None:
    """Measure rows 109 through 112, the delivery wiring behind spans."""
    if destination_arn is None:
        # Rows 111 and 112 need a real destination and row 109 needs something
        # to attach a policy to. Measuring them against a log group that does
        # not exist would collect not-found answers, which guideline 7 reads as
        # Allowed, and that would put four Allowed rows on the record for a
        # wiring nothing verified.
        for row, action in (
            ("109", "logs:PutResourcePolicy"),
            ("110", "logs:PutDeliverySource"),
            ("111", "logs:PutDeliveryDestination"),
            ("112", "logs:CreateDelivery"),
        ):
            recorder.note(
                row,
                action,
                Verdict.INCONCLUSIVE_BAD_INPUT,
                "Not attempted: the logs block did not produce a log group to "
                "deliver into, so there was nothing real to measure against.",
            )
        return

    # Read before write, and record what was already there. A resource policy is
    # configuration rather than a resource, so a Put that lands on a name
    # already in use replaces what was there with no warning and nothing to
    # restore it from. The write below cannot collide, because passing
    # `resourceArn` scopes the policy to a log group this run created seconds
    # ago, and nothing can already be attached to it. This reads the
    # account-scoped policies instead, which is the set a careless write would
    # have damaged, and prints them so the run's own log records the ambient
    # state it did not disturb.
    report_ambient_policies(logs)

    # Scoped to the probe's own log group rather than left account-wide, so the
    # blast radius of the write is one group this run created.
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Phase5ProbeDelivery",
                "Effect": "Allow",
                "Principal": {"Service": "delivery.logs.amazonaws.com"},
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"{destination_arn}:*",
            }
        ],
    }
    # No `policyName`. The first pass sent both it and `resourceArn` and got
    # `InvalidParameterException: Both policy name and resource arn cannot be
    # specified at the same time`, which is a bad-parameter answer rather than an
    # authorization answer and left the row Untested. The two arguments select
    # between the two kinds of policy: a name makes it account-scoped, an ARN
    # makes it scoped to that resource. The workshop's case is a policy on a log
    # group, so the ARN is the right one, and it also keeps the write off
    # account-wide configuration.
    verdict, written = recorder.call(
        "109",
        "logs:PutResourcePolicy",
        lambda: logs.put_resource_policy(
            policyDocument=json.dumps(policy),
            resourceArn=destination_arn,
        ),
        context=f"scoped to {LOG_GROUP}",
    )
    if verdict is Verdict.OK and written is not None:
        # `expectedRevisionId` is not optional on a resource-scoped delete. The
        # first fixed run left the delete answering `Invalid ExpectedRevisionId`
        # and only stayed clean because deleting the log group took the policy
        # with it, which is luck rather than teardown. The revision comes back
        # from the Put, so the delete is given the value rather than guessing.
        revision = written.get("revisionId") or written["resourcePolicy"]["revisionId"]
        recorder.defer(
            f"resource policy on {LOG_GROUP}",
            lambda: logs.delete_resource_policy(
                resourceArn=destination_arn, expectedRevisionId=revision
            ),
        )

    # The source the workshop would use is an AgentCore runtime. Passing one
    # that does not exist asks the authorization question without creating
    # anything, and the answer says which kind of answer this API gives: a
    # not-found settles the row, a ValidationException does not.
    resource_arn = source_arn or (
        f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{MISSING_RUNTIME_ID}"
    )
    context = "live runtime" if source_arn else "runtime that does not exist"
    source_verdict, _ = recorder.call(
        "110",
        "logs:PutDeliverySource",
        lambda: logs.put_delivery_source(
            name=DELIVERY_SOURCE_NAME,
            resourceArn=resource_arn,
            logType="APPLICATION_LOGS",
        ),
        context=context,
    )
    if source_verdict is Verdict.OK:
        recorder.defer(
            f"delivery source {DELIVERY_SOURCE_NAME}",
            lambda: logs.delete_delivery_source(name=DELIVERY_SOURCE_NAME),
        )
    elif source_verdict is Verdict.INCONCLUSIVE_BAD_INPUT and source_arn is None:
        recorder.note(
            "110",
            "logs:PutDeliverySource (what would settle it)",
            Verdict.INCONCLUSIVE_BAD_INPUT,
            "The API answered on the parameter rather than on authorization, "
            "so this row needs a second pass with --delivery-source-arn "
            "pointed at a runtime that is alive at the time of the call.",
        )

    # Unlike the source, the destination points at a log group this run created,
    # so this row gets a real answer either way.
    destination_verdict, destination = recorder.call(
        "111",
        "logs:PutDeliveryDestination",
        lambda: logs.put_delivery_destination(
            name=DELIVERY_DESTINATION_NAME,
            outputFormat="json",
            deliveryDestinationType="CWL",
            deliveryDestinationConfiguration={
                "destinationResourceArn": destination_arn
            },
        ),
        context=LOG_GROUP,
    )
    if destination_verdict is Verdict.OK:
        recorder.defer(
            f"delivery destination {DELIVERY_DESTINATION_NAME}",
            lambda: logs.delete_delivery_destination(name=DELIVERY_DESTINATION_NAME),
        )

    if destination is None:
        recorder.note(
            "112",
            "logs:CreateDelivery",
            Verdict.INCONCLUSIVE_BAD_INPUT,
            "Not attempted: row 111 produced no delivery destination, so there "
            "was no ARN to join a source to.",
        )
        return

    destination_ref = destination["deliveryDestination"]["arn"]
    verdict, _ = recorder.call(
        "112",
        "logs:CreateDelivery",
        lambda: logs.create_delivery(
            deliverySourceName=DELIVERY_SOURCE_NAME,
            deliveryDestinationArn=destination_ref,
        ),
        context=(
            "real source and destination"
            if source_verdict is Verdict.OK
            else "destination is real, source was never created"
        ),
    )
    if verdict is Verdict.OK:
        recorder.defer("delivery", lambda: remove_delivery(logs))


def remove_delivery(logs: Any) -> None:
    """Delete the delivery joining the probe's source and destination.

    `DeleteDelivery` takes an id rather than a name, and `CreateDelivery` is
    only measured when it succeeded, so the id is looked up rather than stored:
    a lookup failure here is residue to report, not a lost measurement.
    """
    for delivery in logs.describe_deliveries(limit=50).get("deliveries", []):
        if delivery.get("deliverySourceName") == DELIVERY_SOURCE_NAME:
            logs.delete_delivery(id=delivery["id"])
            return
    raise ClientError(
        {
            "Error": {
                "Code": "NotFound",
                "Message": f"no delivery for source {DELIVERY_SOURCE_NAME}",
            }
        },
        "DescribeDeliveries",
    )


def probe_network(ec2: Any, recorder: Recorder) -> None:
    """Measure rows 115 and 116, the two network lookups still open.

    Both are Phase 4 rows, folded in here because they are read calls against
    the default VPC and Phase 5 cannot claim no `Untested` rows remain while
    they are open. `MaxResults` is 5 on both because that is the floor the EC2
    API accepts, not because five is interesting.
    """
    recorder.call(
        "115",
        "ec2:DescribeSecurityGroups",
        lambda: ec2.describe_security_groups(MaxResults=5),
    )
    recorder.call(
        "116",
        "ec2:DescribeNetworkInterfaces",
        lambda: ec2.describe_network_interfaces(MaxResults=5),
    )


def probe_secret(secrets: Any, recorder: Recorder) -> None:
    """Measure row 117 twice, once against nothing and once against a secret."""
    # The authorization question on its own, which creates nothing. Under
    # guideline 7 a not-found answer here is a complete answer for the row.
    recorder.call(
        "117",
        "secretsmanager:GetSecretValue",
        lambda: secrets.get_secret_value(SecretId=MISSING_SECRET_NAME),
        context="secret that does not exist",
    )

    # The workshop reads real Neo4j credentials out of a real secret, so the row
    # gets a second entry taken the way the workshop takes it. CreateSecret and
    # DeleteSecret are scaffolding: neither is a tracker row and neither is in
    # the workshop policy, so a failure on them is residue, not a result.
    try:
        secrets.create_secret(
            Name=SECRET_NAME,
            SecretString=json.dumps({"username": "neo4j", "password": "probe-only"}),
            Description="Phase 5:A probe, safe to delete",
        )
    except ClientError as error:
        code = error.response["Error"]["Code"]
        recorder.note(
            "117",
            "secretsmanager:GetSecretValue (real secret)",
            Verdict.INCONCLUSIVE_BAD_INPUT,
            f"Not attempted: CreateSecret answered {code}, so there was no "
            "real secret to read. The not-found entry above still stands.",
        )
        return

    recorder.defer(
        f"secret {SECRET_NAME}",
        # Without the force flag the secret lingers for a seven day recovery
        # window, and the account is wiped when the session ends, so a scheduled
        # deletion would read as residue that never resolves.
        lambda: secrets.delete_secret(
            SecretId=SECRET_NAME, ForceDeleteWithoutRecovery=True
        ),
    )
    recorder.call(
        "117",
        "secretsmanager:GetSecretValue",
        lambda: secrets.get_secret_value(SecretId=SECRET_NAME),
        context="real secret created by this run",
    )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Student-role logs/network checks, Phase 5:A"
    )
    parser.add_argument(
        "--block",
        action="append",
        choices=BLOCKS,
        help="Run only this block. Repeatable. Defaults to every block.",
    )
    parser.add_argument(
        "--all", action="store_true", help="Run every block in dependency order."
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Use the current lab session without starting or extending it.",
    )
    parser.add_argument("--region", help="Override AWS_REGION.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the repository environment file.",
    )
    parser.add_argument(
        "--delivery-source-arn",
        help=(
            "Measure row 110 against this live resource instead of a runtime "
            "ARN that does not exist. Needed only if the default pass answers "
            "on the parameter rather than on authorization."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the measurements to this JSON file.",
    )
    return parser.parse_args()


def main() -> int:
    """Guard the identity, measure the blocks in order, then always tear down."""
    args = parse_args()
    blocks = BLOCKS if (args.all or not args.block) else tuple(args.block)

    try:
        config = load_config(args.env_file, args.region, None, True)
        if not args.no_start:
            start_session(config)
        credentials = fetch_vocareum_credentials(config, wait=not args.no_start)
        guard_credentials(credentials)
        session = build_session(credentials)
        guard_account(session, config.region, config.expected_account)
        if not guard_region(config.region):
            return 2
        account = session.client(
            "sts", region_name=config.region, config=BOTO_CONFIG
        ).get_caller_identity()["Account"]
    except (ApiError, ConfigError, GuardError) as error:
        # ApiError belongs here with the guards. A Vocareum API timeout that
        # escapes as a traceback reads like a crash rather than a refusal, and
        # the whole point of the guards is that a run either reports against a
        # confirmed identity or says plainly that it reported nothing.
        print(f"REFUSED TO REPORT: {error}")
        return 2

    recorder = Recorder(action_width=44)
    logs = session.client("logs", region_name=config.region, config=BOTO_CONFIG)
    destination_arn: str | None = None

    with teardown_guaranteed(recorder):
        if "logs" in blocks:
            print("\n=== block logs ===")
            destination_arn = probe_logs(logs, recorder)
        if "delivery" in blocks:
            print("\n=== block delivery ===")
            probe_delivery(
                logs,
                recorder,
                account,
                config.region,
                destination_arn,
                args.delivery_source_arn,
            )
        if "network" in blocks:
            print("\n=== block network ===")
            probe_network(
                session.client("ec2", region_name=config.region, config=BOTO_CONFIG),
                recorder,
            )
        if "secret" in blocks:
            print("\n=== block secret ===")
            probe_secret(
                session.client(
                    "secretsmanager", region_name=config.region, config=BOTO_CONFIG
                ),
                recorder,
            )

    print("\n=== measurements ===")
    for measurement in recorder.measurements:
        print(
            f"  row {measurement.row:<6} {measurement.status:<14} {measurement.action}"
        )

    if args.output:
        args.output.write_text(
            json.dumps(
                [m.as_dict() for m in recorder.measurements],
                indent=2,
                default=json_default,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nwrote {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
