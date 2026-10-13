import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import NoRegionError

from vocareum_tools import client, diagnose
from vocareum_tools.client import ApiError


def make_config() -> diagnose.Config:
    return diagnose.Config(
        api_base="https://example.test/api/v2",
        course_id="course",
        assignment_id="assignment",
        part_id="part",
        user_id="user",
        api_key="api-key",
        region="us-east-1",
    )


def summary(name: str, status: str, created: datetime) -> dict[str, Any]:
    return {
        "StackName": name,
        "StackStatus": status,
        "StackId": f"arn:aws:cloudformation:us-east-1:1:stack/{name}/id",
        "CreationTime": created,
    }


class FakeCloudFormation:
    """A CloudFormation stand-in that answers from a fixed list of stacks.

    Each poll can return a different list, which is how the wait loop gets
    exercised without sleeping through a real stack creation.
    """

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls = 0

    def next_page(self) -> list[dict[str, Any]]:
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return page


def install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeCloudFormation) -> None:
    monkeypatch.setattr(diagnose, "fetch_stack_summaries", lambda cfn: fake.next_page())
    monkeypatch.setattr(diagnose.time, "sleep", lambda seconds: None)


def test_find_credentials_handles_undocumented_nesting() -> None:
    payload = {
        "resources": [
            {
                "aws_data": {
                    "aws_access_key_id": "ASIAEXAMPLE",
                    "aws_secret_access_key": "secret",
                    "aws_session_token": "token",
                }
            }
        ]
    }

    found = diagnose.find_credentials(payload)

    assert found is not None
    path, credentials = found
    assert path == "$.resources[0].aws_data"
    assert credentials["aws_access_key_id"] == "ASIAEXAMPLE"


def test_parse_scp_denial_extracts_support_fields() -> None:
    reason = (
        "not authorized to perform: dynamodb:CreateTable with an explicit deny "
        "in a service control policy: "
        "arn:aws:organizations::111111111111:policy/o-example/"
        "service_control_policy/p-example"
    )

    denial = diagnose.parse_scp_denial(reason)

    assert denial.denied_action == "dynamodb:CreateTable"
    assert denial.policy_arn is not None
    assert denial.policy_arn.endswith("/p-example")


def test_fetch_credentials_retries_after_start(monkeypatch: Any) -> None:
    payloads = iter(
        [
            {"state": "starting"},
            {
                "aws_access_key_id": "ASIAEXAMPLE",
                "aws_secret_access_key": "secret",
                "aws_session_token": "token",
            },
        ]
    )
    monkeypatch.setattr(
        diagnose, "request_json", lambda *args, **kwargs: next(payloads)
    )
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)

    credentials = diagnose.fetch_credentials(make_config(), wait=True)

    assert credentials is not None
    assert credentials["aws_access_key_id"] == "ASIAEXAMPLE"


def test_find_spend_reads_counters_that_outlive_the_credentials() -> None:
    """Spend is the one field a terminated session still answers with.

    It is also the reason either counter alone is enough: one figure is more
    useful than none, and a null reads the same as a missing figure rather than
    as the text "None".
    """
    payload = {
        "resources": [{"aws_data": {"totalspend": "1.25", "allowedspend": None}}]
    }

    spend = diagnose.find_spend(payload)

    assert spend is not None
    assert spend.path == "$.resources[0].aws_data"
    assert spend.total == "1.25"
    assert spend.allowed == ""


def test_find_spend_reports_nothing_when_the_record_has_no_counters() -> None:
    assert diagnose.find_spend({"resources": [{"aws_data": {"state": "off"}}]}) is None


@pytest.mark.parametrize(
    ("total", "allowed", "expected"),
    [
        ("2.50", "10", 0.25),
        # Vocareum spells "no cap configured" as zero elsewhere in the part
        # record, and dividing by it here would raise.
        ("2.50", "0", None),
        ("", "10", None),
        ("not a number", "10", None),
        ("2.50", "", None),
    ],
    ids=["quarter-spent", "no-cap", "no-total", "non-numeric", "no-allowance"],
)
def test_fraction_used_declines_to_guess(
    total: str, allowed: str, expected: float | None
) -> None:
    assert diagnose.Spend("$", total, allowed).fraction_used() == expected


def test_every_success_status_is_one_the_filter_can_return() -> None:
    """A success status outside the list filter is a branch nothing can take."""
    unlistable = diagnose.SUCCESS_STACK_STATUSES - set(diagnose.STACK_STATUS_FILTER)

    assert unlistable == set()


def test_the_filter_can_see_a_stack_that_is_still_being_created() -> None:
    """Without this, a launch in progress is indistinguishable from no launch."""
    assert "CREATE_IN_PROGRESS" in diagnose.STACK_STATUS_FILTER


def test_wait_for_new_stack_waits_for_the_new_stack_to_finish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stack halfway through creation has no verdict to give yet.

    Returning while it is still CREATE_IN_PROGRESS would report a run with no
    failed events as clean when nothing has been decided.
    """
    launched_at = datetime.now(UTC)
    new = summary("new", "CREATE_IN_PROGRESS", launched_at + timedelta(seconds=5))
    done = summary("new", "CREATE_FAILED", launched_at + timedelta(seconds=5))
    fake = FakeCloudFormation([[new], [new], [done]])
    install_fake(monkeypatch, fake)

    summaries = diagnose.wait_for_new_stack(object(), launched_at)

    assert summaries == [done]
    assert fake.calls == 3
    assert "WARNING" not in capsys.readouterr().out


def test_wait_for_new_stack_refuses_to_report_only_old_stacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-07-25 false negative: an old failure read as this run's result."""
    launched_at = datetime.now(UTC)
    old = summary("old", "CREATE_FAILED", launched_at - timedelta(days=2))
    install_fake(monkeypatch, FakeCloudFormation([[old]]))
    monkeypatch.setattr(diagnose, "STACK_WAIT_TIMEOUT_SECONDS", 0)

    with pytest.raises(ApiError, match="Refusing to report an older stack"):
        diagnose.wait_for_new_stack(object(), launched_at)


def test_wait_for_new_stack_waits_for_the_stack_that_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the newest stack reaches the verdict, so only it settling can end the wait.

    A finished stack sitting behind a newer one that is still being created
    satisfies "some newer stack has settled" while deciding nothing. Ending the
    wait there sent the run on to report that nothing had been decided yet.
    """
    launched_at = datetime.now(UTC)
    newer = launched_at + timedelta(seconds=30)
    building = summary("building", "CREATE_IN_PROGRESS", newer)
    settled = summary("building", "CREATE_FAILED", newer)
    finished = summary("finished", "CREATE_FAILED", launched_at + timedelta(seconds=5))
    fake = FakeCloudFormation([[building, finished], [settled, finished]])
    install_fake(monkeypatch, fake)

    summaries = diagnose.wait_for_new_stack(object(), launched_at)

    assert summaries[0] == settled
    assert fake.calls == 2


def test_the_filter_sees_both_outcomes_of_an_update() -> None:
    """Admitting only the rollback hid every stack that updated successfully."""
    for status in ("UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
        assert status in diagnose.STACK_STATUS_FILTER


def test_build_cloudformation_client_wraps_configuration_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diagnose.boto3,
        "client",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoRegionError()),
    )

    with pytest.raises(ApiError, match="Could not create the CloudFormation client"):
        diagnose.build_cloudformation_client({}, "not-a-region")


def test_created_after_reads_only_a_comparable_timestamp() -> None:
    """A stamp that cannot be compared counts as older, never as newer.

    A naive datetime is the case worth pinning: comparing one against an aware
    datetime raises rather than sorting badly, which would take the whole
    diagnostic down over a field only the freshness check reads.
    """
    moment = datetime.now(UTC)

    assert diagnose.created_after(summary("s", "CREATE_FAILED", moment), moment)
    assert not diagnose.created_after(
        summary("s", "CREATE_FAILED", moment - timedelta(seconds=1)), moment
    )
    assert not diagnose.created_after({"CreationTime": "not a datetime"}, moment)
    assert not diagnose.created_after({}, moment)
    assert not diagnose.created_after(
        {"CreationTime": datetime.now().replace(tzinfo=None)}, moment
    )


def test_summaries_sort_newest_first_around_an_unusable_timestamp() -> None:
    now = datetime.now(UTC)
    oldest = summary("oldest", "CREATE_FAILED", now - timedelta(days=1))
    newest = summary("newest", "CREATE_COMPLETE", now)
    undated = {"StackName": "undated", "StackStatus": "CREATE_FAILED"}
    pages = [[oldest, undated, newest]]

    ordered = sorted(pages[0], key=diagnose.creation_time, reverse=True)

    assert [item.get("StackName") for item in ordered] == [
        "newest",
        "oldest",
        "undated",
    ]


def test_wait_for_new_stack_tolerates_a_stack_stamped_before_the_launch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CloudFormation's clock and this machine's are not the same clock.

    A local clock running fast would hide the new stack behind its own
    timestamp, so the freshness threshold is slackened by
    STACK_CLOCK_SKEW_SECONDS. Without that slack this run warns and reports on
    an earlier stack.
    """
    launched_at = datetime.now(UTC)
    skewed = summary(
        "new",
        "CREATE_FAILED",
        launched_at - timedelta(seconds=diagnose.STACK_CLOCK_SKEW_SECONDS - 1),
    )
    install_fake(monkeypatch, FakeCloudFormation([[skewed]]))

    summaries = diagnose.wait_for_new_stack(object(), launched_at)

    assert summaries == [skewed]
    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "settled"),
    [
        ("CREATE_COMPLETE", True),
        ("CREATE_FAILED", True),
        ("CREATE_IN_PROGRESS", False),
        ("DELETE_IN_PROGRESS", False),
        ("", True),
    ],
)
def test_is_settled_reads_the_in_progress_suffix(status: str, settled: bool) -> None:
    assert diagnose.is_settled({"StackStatus": status}) is settled


def stub_events(monkeypatch: pytest.MonkeyPatch, reasons: dict[str, str]) -> None:
    """Give each stack, by StackId, one failed event carrying the named reason.

    A stack absent from ``reasons`` has no events at all, which is how a clean
    run and a run still in progress both look.
    """

    def events(cfn: Any, stack_id: str) -> list[dict[str, Any]]:
        reason = reasons.get(stack_id)
        if reason is None:
            return []
        return [
            {
                "Timestamp": "2026-07-27T00:00:00Z",
                "LogicalResourceId": "Resource",
                "ResourceType": "AWS::DynamoDB::Table",
                "ResourceStatus": "CREATE_FAILED",
                "ResourceStatusReason": reason,
            }
        ]

    monkeypatch.setattr(diagnose, "describe_stack_events", events)


SCP_REASON = (
    "You are not authorized to perform this operation. User: "
    "arn:aws:sts::1:assumed-role/vocareum/sys1 is not authorized to perform: "
    "dynamodb:CreateTable on resource: arn:aws:dynamodb:us-east-1:1:table/* with an "
    "explicit deny in a service control policy: "
    "arn:aws:organizations::222222222222:policy/o-example/"
    "service_control_policy/p-example"
)


def test_only_the_newest_stack_reaches_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-07-27 regression: an old SCP denial reported over a fresh success.

    The older stack's events are still read and printed, because that history
    is worth having, but they must not decide what this run is reported as.
    """
    now = datetime.now(UTC)
    newest = summary("newest", "CREATE_COMPLETE", now)
    older = summary("older", "CREATE_FAILED", now - timedelta(days=1))
    stub_events(monkeypatch, {older["StackId"]: SCP_REASON})

    diagnosed, scp_denial = diagnose.analyze_stacks(
        object(), [newest, older], all_stacks=True
    )

    assert diagnosed is True
    assert scp_denial is None


def test_an_scp_denial_on_the_newest_stack_does_reach_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    newest = summary("newest", "CREATE_FAILED", now)
    stub_events(monkeypatch, {newest["StackId"]: SCP_REASON})

    diagnosed, scp_denial = diagnose.analyze_stacks(
        object(), [newest], all_stacks=False
    )

    assert diagnosed is True
    assert scp_denial is not None
    assert scp_denial.denied_action == "dynamodb:CreateTable"
    assert scp_denial.policy_arn is not None


def test_a_stack_still_being_created_is_not_reported_as_diagnosed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC)
    building = summary("building", "CREATE_IN_PROGRESS", now)
    stub_events(monkeypatch, {})

    diagnosed, scp_denial = diagnose.analyze_stacks(
        object(), [building], all_stacks=False
    )

    assert diagnosed is False
    assert scp_denial is None
    assert "nothing has been decided yet" in capsys.readouterr().out


def test_in_progress_report_uses_each_resources_newest_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = [
        {
            "LogicalResourceId": "Finished",
            "ResourceStatus": "CREATE_COMPLETE",
            "ResourceType": "AWS::S3::Bucket",
        },
        {
            "LogicalResourceId": "Waiting",
            "ResourceStatus": "CREATE_IN_PROGRESS",
            "ResourceType": "AWS::EC2::Instance",
        },
        {
            "LogicalResourceId": "Finished",
            "ResourceStatus": "CREATE_IN_PROGRESS",
            "ResourceType": "AWS::S3::Bucket",
        },
    ]

    diagnose.print_current_in_progress_events(events)

    output = capsys.readouterr().out
    assert "Waiting CREATE_IN_PROGRESS" in output
    assert "Finished CREATE_IN_PROGRESS" not in output


def test_console_output_decodes_and_prints_only_the_tail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeEC2:
        def get_console_output(self, **kwargs: Any) -> dict[str, str]:
            assert kwargs == {"InstanceId": "i-example", "Latest": True}
            encoded = base64.b64encode(b"first\nsecond\nthird\n").decode()
            return {"Output": encoded}

    diagnose.print_ec2_console_output(FakeEC2(), "i-example", lines=2)

    output = capsys.readouterr().out
    assert "first" not in output
    assert "second" in output
    assert "third" in output


def test_ec2_failure_prints_console_before_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    failed = summary("failed", "CREATE_FAILED", now)
    event = {
        "Timestamp": now,
        "LogicalResourceId": "CodeEditorInstance",
        "PhysicalResourceId": "i-example",
        "ResourceType": "AWS::EC2::Instance",
        "ResourceStatus": "CREATE_FAILED",
        "ResourceStatusReason": "Received FAILURE signal",
    }
    monkeypatch.setattr(diagnose, "describe_stack_events", lambda *args: [event])
    calls: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        diagnose,
        "print_ec2_console_output",
        lambda client, instance_id: calls.append((client, instance_id)),
    )
    ec2 = object()

    diagnosed, _ = diagnose.analyze_stacks(
        object(), [failed], all_stacks=False, ec2_client=ec2
    )

    assert diagnosed is True
    assert calls == [(ec2, "i-example")]
