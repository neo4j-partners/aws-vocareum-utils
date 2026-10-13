#!/usr/bin/env -S uv run
"""Run the student-role DynamoDB checks from Phase 2.5.

Rows 36 through 47, the twelve DynamoDB actions the hotel-booking tool needs.
``vocareum-probe`` cannot drive them, for the same reason it cannot drive
Phase 3: the block is stateful. ``PutItem`` needs a table that already exists
and has finished becoming ``ACTIVE``, ``Query`` needs an item under a key it can
name, and ``DeleteTable`` is only measurable once everything else is done with
the table. A flat list of probes with static arguments cannot express that
ordering, so the ordering lives here while the guards and the error classifier
stay shared with the general probe.

The table is created with a ``workshop-`` prefixed name, because that is what
row 36 says the workshop uses and because Phase 2 established that a prefix can
matter. It is billed ``PAY_PER_REQUEST``, so an abandoned table costs nothing
at rest.

``Measurement``, ``Recorder``, ``wait_until``, and ``teardown_guaranteed`` come
from ``vocareum_tools.phase``. They were a deliberate copy of the Phase 3
runner's versions, on the reasoning that a copy keeps a Phase 2.5 change from
altering a settled result. What the copies actually did was drift: three
versions of ``Recorder`` existed at once and one of them had lost
``call_in_teardown``. The shared module has unit tests, which is the protection
the copy was standing in for.

    ./scripts/run_phase25_dynamodb.py --no-start
    ./scripts/run_phase25_dynamodb.py --output probes/phase25-dynamodb.json

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
    wait_until,
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

# Row 36 names the workshop's tables as workshop-*, and Phase 2 found that
# S3 results do not carry across a prefix change, so the probe measures the
# prefix the workshop actually uses. The rest of the name marks it as this
# phase's, so an abandoned table is identifiable by sight.
TABLE_NAME = "workshop-phase25probe-bookings"

# The hotel-booking shape: bookings belong to a customer, so the customer is the
# partition and the booking is the sort key. Row 44 needs a partition key it can
# name in a key condition, which a single-attribute key would also give, but a
# composite key is what the tool stores and it costs nothing extra to measure.
CUSTOMER_ID = "phase25probe-customer-1"
BOOKING_ID = "phase25probe-booking-1"


def wait_until_table_gone(
    dynamodb: Any, table: str, timeout: int = 300, interval: int = 5
) -> bool:
    """Poll until the table is really gone, and report whether it went.

    `DeleteTable` returns while the table is still `DELETING`, so a run that
    stopped at the call would claim a clean account while the table was still
    in it.

    `vocareum_tools.phase.wait_until_gone` does the same job for a describe
    callable, but on a 120 second deadline. A table that is still `DELETING`
    is normal well past that, so this one keeps its own 300, and its own name
    so the two are not mistaken for each other.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            dynamodb.describe_table(TableName=table)
        except ClientError as error:
            if error.response["Error"]["Code"] == "ResourceNotFoundException":
                print(f"        table {table} is gone")
                return True
            print(f"        describe while deleting {table}: {error}")
            return False
        time.sleep(interval)
    print(f"        table {table} still present after {timeout}s")
    return False


def item(booking_id: str, nights: int, status: str) -> dict[str, Any]:
    """Return one booking item in the low-level attribute-value shape."""
    return {
        "customer_id": {"S": CUSTOMER_ID},
        "booking_id": {"S": booking_id},
        "hotel": {"S": "Phase 2.5 probe hotel"},
        "nights": {"N": str(nights)},
        "status": {"S": status},
    }


def key(booking_id: str) -> dict[str, Any]:
    """Return the primary key of one booking item."""
    return {
        "customer_id": {"S": CUSTOMER_ID},
        "booking_id": {"S": booking_id},
    }


def delete_table_when_settled(
    dynamodb: Any, table: str, timeout: int = 300, interval: int = 10
) -> Any:
    """Delete the table, waiting out a busy table rather than reporting it.

    Row 47 leaves the table busy for longer than the item calls take to run,
    and `DeleteTable` refuses a table whose tags are still being updated. That
    `ResourceInUseException` is a transitional state and not an authorization
    answer, so waiting it out is what measures row 37; recording it would file
    an Allowed action as untested and leave the table in the account. The busy
    state cannot be polled for instead: `DescribeTable` reports the table
    `ACTIVE` throughout, so the only thing that reports it is the refusal.

    Every other `ClientError` is raised on the first attempt. A denial is the
    answer row 37 exists to get, and sitting on one for the length of the
    deadline would turn the answer into a delay.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return dynamodb.delete_table(TableName=table)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceInUseException":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Out of deadline, so the last refusal is the outcome. The
                # caller records it and re-raises, which is what reports the
                # table as left behind.
                raise
            print(
                f"        table {table} is busy, retrying the delete "
                f"({int(remaining)}s of the deadline left)"
            )
            time.sleep(min(interval, remaining))


def remove_table(dynamodb: Any, recorder: Recorder, table: str) -> None:
    """Delete the table, measuring row 37, and wait for it to actually go."""
    recorder.call_in_teardown(
        "37",
        "dynamodb:DeleteTable",
        lambda: delete_table_when_settled(dynamodb, table),
        context=table,
    )
    wait_until_table_gone(dynamodb, table)


def probe_dynamodb(dynamodb: Any, recorder: Recorder) -> None:
    """Measure rows 36 through 47 against one real workshop-prefixed table."""
    # Cheapest call in the block and the only one that needs nothing to exist,
    # so it runs first. A denial here would not predict the rest: DynamoDB
    # scopes ListTables to the whole account and the others to a table ARN.
    recorder.call("46", "dynamodb:ListTables", lambda: dynamodb.list_tables(Limit=25))

    # No Tags argument. Row 47 is a separate action and gets its own call, so
    # tagging at creation time would leave it credited by something it did not
    # measure.
    verdict, response = recorder.call(
        "36",
        "dynamodb:CreateTable",
        lambda: dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "customer_id", "KeyType": "HASH"},
                {"AttributeName": "booking_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "customer_id", "AttributeType": "S"},
                {"AttributeName": "booking_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        ),
        context=TABLE_NAME,
    )
    if verdict is not Verdict.OK or response is None:
        # Rule 9 of the Phase 3 guidelines: stop at a denial rather than
        # working around it. Without a table the remaining rows would answer
        # ResourceNotFoundException, which the classifier reads as Allowed, and
        # a block of Allowed rows measured against nothing is worse than a gap.
        print("  CreateTable did not succeed, so the rest of the block is skipped.")
        return

    recorder.defer(
        f"table {TABLE_NAME}", lambda: remove_table(dynamodb, recorder, TABLE_NAME)
    )
    table_arn = response["TableDescription"]["TableArn"]

    state = wait_until(
        lambda: dynamodb.describe_table(TableName=TABLE_NAME)["Table"]["TableStatus"],
        done={"ACTIVE"},
        failed={"DELETING", "ARCHIVED", "INACCESSIBLE_ENCRYPTION_CREDENTIALS"},
        label=f"table {TABLE_NAME}",
        interval=5,
    )
    if state != "ACTIVE":
        recorder.note(
            "36",
            "dynamodb:CreateTable (table state)",
            Verdict.UNCLASSIFIED,
            f"The table was accepted but settled in {state} rather than ACTIVE, "
            "so the item rows were not measured against it.",
        )
        return

    recorder.call(
        "38",
        "dynamodb:DescribeTable",
        lambda: dynamodb.describe_table(TableName=TABLE_NAME),
        context=TABLE_NAME,
    )
    recorder.call(
        "47",
        "dynamodb:TagResource",
        lambda: dynamodb.tag_resource(
            ResourceArn=table_arn,
            Tags=[{"Key": "perms-md-phase", "Value": "2.5"}],
        ),
        context=TABLE_NAME,
    )
    recorder.call(
        "39",
        "dynamodb:PutItem",
        lambda: dynamodb.put_item(
            TableName=TABLE_NAME, Item=item(BOOKING_ID, 2, "held")
        ),
        context=BOOKING_ID,
    )
    recorder.call(
        "40",
        "dynamodb:GetItem",
        lambda: dynamodb.get_item(
            TableName=TABLE_NAME, Key=key(BOOKING_ID), ConsistentRead=True
        ),
        context=BOOKING_ID,
    )
    recorder.call(
        "41",
        "dynamodb:UpdateItem",
        lambda: dynamodb.update_item(
            TableName=TABLE_NAME,
            Key=key(BOOKING_ID),
            UpdateExpression="SET #status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": {"S": "confirmed"}},
        ),
        context=BOOKING_ID,
    )
    recorder.call(
        "43",
        "dynamodb:Scan",
        lambda: dynamodb.scan(TableName=TABLE_NAME, Limit=10),
        context=TABLE_NAME,
    )
    recorder.call(
        "44",
        "dynamodb:Query",
        lambda: dynamodb.query(
            TableName=TABLE_NAME,
            KeyConditionExpression="customer_id = :customer",
            ExpressionAttributeValues={":customer": {"S": CUSTOMER_ID}},
        ),
        context=CUSTOMER_ID,
    )
    recorder.call(
        "45",
        "dynamodb:BatchWriteItem",
        lambda: dynamodb.batch_write_item(
            RequestItems={
                TABLE_NAME: [
                    {"PutRequest": {"Item": item("phase25probe-booking-2", 1, "held")}},
                    {"PutRequest": {"Item": item("phase25probe-booking-3", 3, "held")}},
                ]
            }
        ),
        context="2 seeded bookings",
    )
    # Last of the item calls, because the rows above need something to read.
    recorder.call(
        "42",
        "dynamodb:DeleteItem",
        lambda: dynamodb.delete_item(TableName=TABLE_NAME, Key=key(BOOKING_ID)),
        context=BOOKING_ID,
    )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run student-role DynamoDB checks, Phase 2.5."
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
        "--output",
        type=Path,
        help="Write the measurements to this JSON file.",
    )
    return parser.parse_args()


def main() -> int:
    """Guard the identity, measure the block, then always tear down."""
    args = parse_args()

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
    except (ApiError, ConfigError, GuardError) as error:
        # ApiError belongs here with the guards. A Vocareum API timeout that
        # escapes as a traceback reads like a crash rather than a refusal, and
        # the whole point of the guards is that a run either reports against a
        # confirmed identity or says plainly that it reported nothing.
        print(f"REFUSED TO REPORT: {error}")
        return 2

    recorder = Recorder(action_width=40)
    dynamodb = session.client("dynamodb", region_name=config.region, config=BOTO_CONFIG)

    with teardown_guaranteed(recorder):
        print("\n=== block dynamodb ===")
        probe_dynamodb(dynamodb, recorder)

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
