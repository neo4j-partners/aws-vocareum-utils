import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import (
    ClientError,
    NoRegionError,
    ParamValidationError,
    ProfileNotFound,
)

from vocareum_tools import client, probe, selftest
from vocareum_tools.client import TransactionResult
from vocareum_tools.config import ConfigError


def make_config() -> probe.Config:
    return probe.Config(
        api_base="https://example.test/api/v2",
        course_id="course",
        assignment_id="assignment",
        part_id="part",
        user_id="user",
        api_key="api-key",
        expected_account="123456789012",
        region="us-east-1",
    )


class FakeSts:
    """An STS stand-in that answers get_caller_identity from a fixed identity."""

    def __init__(self, identity: dict[str, str] | Exception) -> None:
        self.identity = identity

    def get_caller_identity(self) -> dict[str, str]:
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity


class FakeSession:
    def __init__(self, sts: FakeSts) -> None:
        self.sts = sts

    def client(self, *args: Any, **kwargs: Any) -> FakeSts:
        return self.sts


# ---------------------------------------------------------------------------
# The self-test tables, run again under pytest
#
# The tables live in the shipped package because --self-test has to work inside
# a lab where the suite is not installed. Running them from here too means a
# case added for one is checked by both.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    selftest.CLASSIFIER_CASES,
    ids=[code for code, _, _ in selftest.CLASSIFIER_CASES],
)
def test_classifier_cases(code: str, message: str, expected: probe.Verdict) -> None:
    verdict, _, _ = probe.classify(selftest.make_error(code, message))

    assert verdict is expected


@pytest.mark.parametrize(
    ("probed", "code", "message", "expected"),
    selftest.PROBED_CLASSIFIER_CASES,
    ids=[probed for probed, _, _, _ in selftest.PROBED_CLASSIFIER_CASES],
)
def test_classifier_cases_that_need_the_probed_action(
    probed: str, code: str, message: str, expected: probe.Verdict
) -> None:
    verdict, _, _ = probe.classify(selftest.make_error(code, message), probed)

    assert verdict is expected


@pytest.mark.parametrize(
    ("probed", "named", "expected"),
    selftest.DEPENDENCY_CASES,
    ids=[f"{probed}-{named}" for probed, named, _ in selftest.DEPENDENCY_CASES],
)
def test_a_denial_naming_another_action_measures_the_dependency(
    probed: str, named: str, expected: probe.Verdict
) -> None:
    error = selftest.make_error(
        "AccessDeniedException", selftest.dependency_message(named)
    )

    verdict, _, _ = probe.classify(error, probed)

    assert verdict is expected


@pytest.mark.parametrize(("verdict", "expected"), selftest.STATUS_CASES)
def test_status_cases(verdict: probe.Verdict, expected: str) -> None:
    assert probe.ProbeResult("x", verdict, "").status == expected


@pytest.mark.parametrize(("operation", "expected"), selftest.METHOD_NAME_CASES)
def test_method_name_cases(operation: str, expected: str) -> None:
    assert probe.to_method_name(operation) == expected


@pytest.mark.parametrize(
    ("probe_case", "expected_safe"),
    selftest.SAFETY_CASES,
    ids=[case.action for case, _ in selftest.SAFETY_CASES],
)
def test_safety_cases(probe_case: probe.Probe, expected_safe: bool) -> None:
    assert probe_case.is_safe() is expected_safe


def test_every_verdict_has_a_tracker_status() -> None:
    assert selftest.verdicts_without_status() == []


def test_every_verdict_is_reachable() -> None:
    """A verdict nothing can emit reads as handled when it is not."""
    assert selftest.unreachable_verdicts() == []


# ---------------------------------------------------------------------------
# Probe definitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["invalid", ":Action", "service:", "a:b:c"])
def test_probe_rejects_malformed_actions(action: str) -> None:
    with pytest.raises(ConfigError):
        probe.Probe(action)


def test_probe_file_rejects_non_object_kwargs(tmp_path: Path) -> None:
    path = tmp_path / "probes.json"
    path.write_text(
        json.dumps([{"action": "lambda:GetFunction", "kwargs": []}]),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="non-object kwargs"):
        probe.load_probes_file(path)


def test_probe_file_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "probes.json"
    path.write_text(
        json.dumps([{"action": "lambda:GetFunction", "kwagrs": {}}]),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown fields: kwagrs"):
        probe.load_probes_file(path)


def test_probe_rejects_empty_client_override() -> None:
    with pytest.raises(ConfigError, match="invalid client"):
        probe.Probe("lambda:GetFunction", client="")


# ---------------------------------------------------------------------------
# Which boto3 client serves an action
#
# One IAM service prefix can be served by more than one client. Picking the
# wrong one raises UNKNOWN_ACTION, which records as Untested, so the row reads
# as never measured rather than as measured wrongly. These pin the resolution.
# ---------------------------------------------------------------------------


def operations_of(client_name: str) -> set[str]:
    """Return the operation names botocore's model gives this client."""
    session = boto3.session.Session()
    model = session.client(
        client_name, region_name="us-east-1", config=probe.BOTO_CONFIG
    ).meta.service_model
    return set(model.operation_names)


def test_agentcore_clients_do_not_share_an_operation() -> None:
    """The invariant that makes first-match resolution safe.

    ``CLIENT_CANDIDATES`` takes the first client carrying the operation. That
    is only unambiguous while the control plane and the data plane hold
    disjoint operation names. If a future botocore adds the same name to both,
    this fails and the candidate list needs an explicit per-action rule.
    """
    control, data = probe.CLIENT_CANDIDATES["bedrock-agentcore"]
    assert operations_of(control) & operations_of(data) == set()


@pytest.mark.parametrize(
    ("action", "expected_client"),
    [
        # The one AgentCore action the workshop needs that lives on the data
        # plane. A single service-wide override sent it to
        # the control plane, where there is no such operation.
        ("bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore"),
        # Management actions, one from each block of the Phase 3 tracker.
        ("bedrock-agentcore:CreateAgentRuntime", "bedrock-agentcore-control"),
        ("bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore-control"),
        ("bedrock-agentcore:CreateWorkloadIdentity", "bedrock-agentcore-control"),
        ("bedrock-agentcore:GetMemory", "bedrock-agentcore-control"),
    ],
)
def test_agentcore_actions_reach_the_client_that_serves_them(
    action: str, expected_client: str
) -> None:
    probe_case = probe.Probe(action)

    serving = [
        name
        for name in probe_case.client_names
        if probe_case.operation in operations_of(name)
    ]

    assert serving[:1] == [expected_client]


def test_an_explicit_client_is_the_only_candidate() -> None:
    """A pinned client is not quietly widened into the candidate list.

    A probe file that names a client is making a claim. If the claim is wrong
    the run has to say so, not fall through to a client that happens to work.
    """
    pinned = probe.Probe("bedrock-agentcore:InvokeAgentRuntime", client="s3")
    assert pinned.client_names == ("s3",)


def test_a_service_with_no_candidates_uses_its_own_name() -> None:
    assert probe.Probe("lambda:GetFunction").client_names == ("lambda",)


def test_an_unservable_action_names_every_client_it_tried() -> None:
    """UNKNOWN_ACTION has to say what was tried.

    The operator cannot tell a misspelled action from a candidate list missing
    a plane unless the detail names the clients that were checked.
    """
    result = probe.run_probe(
        boto3.session.Session(),
        "us-east-1",
        probe.Probe("bedrock-agentcore:NoSuchOperation"),
    )

    assert result.verdict is probe.Verdict.UNKNOWN_ACTION
    assert "bedrock-agentcore-control" in result.detail
    assert "bedrock-agentcore" in result.detail


@pytest.mark.parametrize(
    ("operation", "read_only"),
    [
        ("ListFoundationModels", True),
        ("GetFunction", True),
        ("DescribeImages", True),
        ("BatchGetImage", True),
        ("RunInstances", False),
        ("CreateTable", False),
        # Reads like "get" but is not one. Prefix matching has to be anchored
        # to the start of the operation, which it is, and this pins that.
        ("PutBucketPublicAccessBlock", False),
    ],
)
def test_is_read_only_admits_only_reads(operation: str, read_only: bool) -> None:
    assert probe.is_read_only(operation) is read_only


def test_collect_probes_defaults_to_the_identity_check() -> None:
    """Every phase depends on sts:GetCallerIdentity, so it is the safe default."""
    probes = probe.collect_probes(probe.parse_args([]))

    assert [item.action for item in probes] == ["sts:GetCallerIdentity"]


def test_collect_probes_gathers_every_source_in_order(tmp_path: Path) -> None:
    path = tmp_path / "probes.json"
    path.write_text(
        json.dumps([{"action": "s3:ListBuckets", "note": "from a file"}]),
        encoding="utf-8",
    )
    args = probe.parse_args(["--probe", "iam:GetRole", "--probes-file", str(path)])

    probes = probe.collect_probes(args)

    assert [item.action for item in probes] == [
        "iam:GetRole",
        "s3:ListBuckets",
    ]


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "credentials"),
    selftest.GUARD_CASES,
    ids=[label for label, _ in selftest.GUARD_CASES],
)
def test_credential_guard_refuses_blank_fields(
    label: str, credentials: probe.Credentials
) -> None:
    with pytest.raises(probe.GuardError):
        probe.guard_credentials(credentials)


def test_credential_guard_accepts_a_complete_triple() -> None:
    probe.guard_credentials(selftest.COMPLETE_CREDENTIALS)


def test_ambient_credentials_wraps_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe.boto3.session,
        "Session",
        lambda: (_ for _ in ()).throw(NoRegionError()),
    )

    with pytest.raises(probe.GuardError, match="resolve ambient AWS credentials"):
        probe.ambient_credentials()


def test_account_guard_refuses_a_different_account() -> None:
    """Reporting on the wrong account is the failure this whole tool exists to avoid.

    Every permission result the probe prints is only meaningful for the account
    it was collected in, so an unexpected account has to stop the run rather
    than annotate it.
    """
    session: Any = FakeSession(
        FakeSts({"Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/x"})
    )

    with pytest.raises(probe.GuardError, match="999999999999") as error:
        probe.guard_account(session, "us-east-1", "123456789012")

    message = str(error.value)
    assert "rotated the account pool" in message
    assert "vocareum-refresh" in message
    assert "--course" in message


def test_account_guard_accepts_the_expected_account(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Passing means not raising, and printing the identity it accepted.

    The printed account and ARN are the only record of which account a run
    reported on, so the transcript is part of the guard's contract.
    """
    arn = "arn:aws:sts::123456789012:assumed-role/voclabs/user1"
    session: Any = FakeSession(FakeSts({"Account": "123456789012", "Arn": arn}))

    probe.guard_account(session, "us-east-1", "123456789012")

    printed = capsys.readouterr().out
    assert "123456789012" in printed
    assert arn in printed


def test_account_guard_refuses_when_the_identity_call_fails() -> None:
    error = ClientError(
        {"Error": {"Code": "ExpiredTokenException", "Message": "expired"}},
        "GetCallerIdentity",
    )
    session: Any = FakeSession(FakeSts(error))

    with pytest.raises(probe.GuardError, match="GetCallerIdentity failed"):
        probe.guard_account(session, "us-east-1", "123456789012")


def test_account_guard_wraps_client_construction_failures() -> None:
    class BrokenSession:
        def client(self, *args: Any, **kwargs: Any) -> None:
            raise NoRegionError()

    session: Any = BrokenSession()
    with pytest.raises(probe.GuardError, match="GetCallerIdentity failed"):
        probe.guard_account(session, "not-a-region", "123456789012")


def test_build_session_refuses_rather_than_raising_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that cannot be built has to read as a refusal, not a traceback.

    A local AWS config naming a profile that is not there fails here rather than
    on the first call, and a traceback exits 1 with no verdict: the same silence
    as a refusal without saying one happened.
    """
    monkeypatch.setattr(
        probe.boto3.session,
        "Session",
        lambda **kwargs: (_ for _ in ()).throw(ProfileNotFound(profile="")),
    )

    with pytest.raises(probe.GuardError, match="could not build a session"):
        probe.build_session(probe.Credentials("ASIAEXAMPLE", "secret", "token"))


def test_region_guard_warns_without_stopping_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert probe.guard_region(probe.REQUIRED_REGION) is True
    assert probe.guard_region("eu-west-1") is False
    assert "warning" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Session and credentials
# ---------------------------------------------------------------------------


def test_start_session_follows_returned_transaction(monkeypatch: Any) -> None:
    config = make_config()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        probe,
        "request_json",
        lambda method, url, api_key: {"transactionid": "transaction-1"},
    )

    def fake_poll(url: str, api_key: str, **kwargs: Any) -> TransactionResult:
        calls.append((url, api_key))
        return TransactionResult("success", {"state": "success"})

    monkeypatch.setattr(probe, "poll_transaction", fake_poll)

    probe.start_session(config)

    assert calls == [
        ("https://example.test/api/v2/transaction/transaction-1", "api-key")
    ]


def test_start_session_redacts_failure_message(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        probe,
        "request_json",
        lambda *args, **kwargs: {"transactionid": "transaction-1"},
    )
    monkeypatch.setattr(
        probe,
        "poll_transaction",
        lambda *args, **kwargs: TransactionResult(
            "failed",
            {
                "state": "failed",
                "message": (
                    "url=https://example.test/?X-Amz-Signature=secret-signature"
                ),
            },
        ),
    )

    with pytest.raises(probe.GuardError) as raised:
        probe.start_session(make_config())

    assert "secret-signature" not in str(raised.value)


def test_fetch_credentials_retries_after_new_session(monkeypatch: Any) -> None:
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
        probe, "request_json", lambda method, url, api_key: next(payloads)
    )
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)

    credentials = probe.fetch_vocareum_credentials(make_config(), wait=True)

    assert credentials.access_key_id == "ASIAEXAMPLE"


def test_fetch_credentials_refuses_a_record_with_no_triple(monkeypatch: Any) -> None:
    monkeypatch.setattr(probe, "request_json", lambda *args, **kwargs: {"state": "off"})

    with pytest.raises(probe.GuardError, match="no credential triple"):
        probe.fetch_vocareum_credentials(make_config(), wait=False)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def test_run_probe_classifies_local_parameter_validation() -> None:
    class Client:
        def get_function(self, **kwargs: Any) -> None:
            raise ParamValidationError(report="FunctionName is required")

    class Session:
        def client(self, *args: Any, **kwargs: Any) -> Client:
            return Client()

    session: Any = Session()
    result = probe.run_probe(session, "us-east-1", probe.Probe("lambda:GetFunction"))

    assert result.verdict is probe.Verdict.INCONCLUSIVE_BAD_INPUT
