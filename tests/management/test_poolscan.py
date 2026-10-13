"""Tests for the Bedrock entitlement pool scan, driven by recorded errors.

The scan spends launches and ends sessions, so the failures worth testing are
the ones where it reports the wrong thing rather than the ones where it errors.
Three of them:

**Error 002 filed as a request-shape problem.** It arrives as a plain
``ValidationException``, which ``probe.classify`` maps to
``INCONCLUSIVE_BAD_INPUT`` for sound general reasons. If the scan inherited that
mapping it would report an entitlement block as "the request was malformed".

**Ambient credentials reported as a lab account.** The account guard cannot
apply to a tool that discovers which account it landed in, so the identity check
is the only thing left. A triple that is present but is not a ``voclabs``
session has to refuse.

**A one-account pool reported as three.** Distinct-account counting is what
decides whether the answer is "these members are entitled" or "entitlement is
random", so a repeat has to count as a repeat.

The error payloads here are the shapes measured live on 2026-08-25.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from vocareum_tools import poolscan
from vocareum_tools.client import ApiError
from vocareum_tools.poolscan import Entitlement, Observation, ScanState
from vocareum_tools.probe import GuardError, Verdict

# Measured live: bedrock-runtime Converse in a non-entitled Vocareum account.
ERROR_002_RESPONSE: dict[str, Any] = {
    "Error": {
        "Code": "ValidationException",
        "Message": (
            "Error 002: Access to Bedrock models is not allowed for this account"
        ),
    },
    "ResponseMetadata": {"RequestId": "fa324550-5767-4ccd-af02-3b158fbd5395"},
}

# The other ValidationException Bedrock returns, which is a genuine request
# problem and must not be confused with the entitlement block.
BAD_MODEL_RESPONSE: dict[str, Any] = {
    "Error": {
        "Code": "ValidationException",
        "Message": "The provided model identifier is invalid.",
    },
    "ResponseMetadata": {"RequestId": "11111111-2222-3333-4444-555555555555"},
}

DENIED_RESPONSE: dict[str, Any] = {
    "Error": {
        "Code": "AccessDeniedException",
        "Message": (
            "User: arn:aws:sts::111111111111:assumed-role/voclabs/user1 is not "
            "authorized to perform: bedrock:InvokeModel because no identity-based "
            "policy allows the bedrock:InvokeModel action"
        ),
    },
    "ResponseMetadata": {"RequestId": "66666666-7777-8888-9999-000000000000"},
}


class FakeSts:
    """An STS client that answers one recorded identity."""

    def __init__(self, identity: dict[str, str]) -> None:
        self._identity = identity

    def get_caller_identity(self) -> dict[str, str]:
        """Return the recorded identity."""
        return self._identity


class FakeSession:
    """A boto3 session stand-in that hands back one recorded client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def client(self, name: str, **kwargs: Any) -> Any:
        """Return the recorded client for any service name."""
        return self._client


def error(response: dict[str, Any], operation: str = "Converse") -> ClientError:
    """Build a ``ClientError`` from a recorded response payload."""
    return ClientError(response, operation)


# ---------------------------------------------------------------------------
# Error 002 is an entitlement answer, not a request-shape answer
# ---------------------------------------------------------------------------


def test_error_002_is_classified_as_an_entitlement_block() -> None:
    """The whole point of the tool: Error 002 must not read as bad input."""
    observation = poolscan.classify_bedrock_error(
        error(ERROR_002_RESPONSE), poolscan.DEFAULT_PROBE_MODEL
    )
    assert observation.entitlement is Entitlement.ERROR_002
    assert observation.request_id == "fa324550-5767-4ccd-af02-3b158fbd5395"


def test_probe_classify_alone_would_have_gotten_this_wrong() -> None:
    """Records why the local match exists rather than a call to classify.

    If ``classify`` ever learns Error 002, this test fails and the local branch
    in ``poolscan`` can be deleted. Until then it documents the gap instead of
    leaving a reader to wonder why the module does not just delegate.
    """
    verdict, _, _ = poolscan.classify(error(ERROR_002_RESPONSE), poolscan.PROBE_ACTION)
    assert verdict is Verdict.INCONCLUSIVE_BAD_INPUT


def test_a_real_validation_error_is_not_reported_as_error_002() -> None:
    """A bad model id is a scan bug, not an account verdict."""
    observation = poolscan.classify_bedrock_error(
        error(BAD_MODEL_RESPONSE), "nonsense.model"
    )
    assert observation.entitlement is Entitlement.OTHER
    assert observation.verdict == str(Verdict.INCONCLUSIVE_BAD_INPUT)


def test_an_iam_denial_is_reported_as_a_denial() -> None:
    """An IAM shortfall must not be filed as missing Bedrock entitlement."""
    observation = poolscan.classify_bedrock_error(
        error(DENIED_RESPONSE), poolscan.DEFAULT_PROBE_MODEL
    )
    assert observation.entitlement is Entitlement.OTHER
    assert observation.verdict == str(Verdict.IAM_DENY)


def test_a_successful_invocation_is_entitled() -> None:
    """A returning Converse call is the only positive evidence there is."""

    class Runtime:
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            return {"output": {"message": {"content": [{"text": "pong"}]}}}

    observation = poolscan.probe_bedrock(
        FakeSession(Runtime()), "us-east-1", poolscan.DEFAULT_PROBE_MODEL
    )
    assert observation.entitlement is Entitlement.ENTITLED
    assert observation.verdict == str(Verdict.OK)


# ---------------------------------------------------------------------------
# The identity check that replaces the account guard
# ---------------------------------------------------------------------------


def test_a_voclabs_session_passes_the_identity_check() -> None:
    """The normal case: a student session in some lab account."""
    session = FakeSession(
        FakeSts(
            {
                "Account": "666666666666",
                "Arn": "arn:aws:sts::666666666666:assumed-role/voclabs/user5263546",
            }
        )
    )
    identity = poolscan.discover_identity(session, "us-east-1")
    assert identity.account == "666666666666"


def test_non_session_credentials_are_refused() -> None:
    """Ambient operator credentials must never be reported as a lab account.

    This is the failure the account guard exists to prevent, reached by the one
    path where the account guard cannot run.
    """
    session = FakeSession(
        FakeSts(
            {
                "Account": "999999999999",
                "Arn": "arn:aws:iam::999999999999:user/ryan",
            }
        )
    )
    with pytest.raises(GuardError, match="not a Vocareum student session"):
        poolscan.discover_identity(session, "us-east-1")


def test_an_unusable_account_id_is_refused() -> None:
    """An empty account is no answer, so it must not become a table row."""
    session = FakeSession(
        FakeSts({"Account": "", "Arn": "arn:aws:sts::0:assumed-role/voclabs/user1"})
    )
    with pytest.raises(GuardError, match="unusable account id"):
        poolscan.discover_identity(session, "us-east-1")


# ---------------------------------------------------------------------------
# Distinct-account accounting, which is what the conclusion rests on
# ---------------------------------------------------------------------------


def observed(
    account: str, entitlement: Entitlement, round_index: int = 1
) -> Observation:
    """Build one recorded observation."""
    return Observation(
        round_index=round_index, account=account, entitlement=entitlement
    )


def test_a_repeat_allocation_counts_once() -> None:
    """Landing in the same member twice is one account, not two."""
    state = ScanState(
        observations=[
            observed("111111111111", Entitlement.ERROR_002, 1),
            observed("111111111111", Entitlement.ERROR_002, 2),
        ]
    )
    assert state.accounts == ["111111111111"]
    assert state.unstable() == []


def test_an_account_answering_two_ways_is_flagged_unstable() -> None:
    """The finding that would invalidate a per-account support request."""
    state = ScanState(
        observations=[
            observed("111111111111", Entitlement.ERROR_002, 1),
            observed("111111111111", Entitlement.ENTITLED, 2),
        ]
    )
    assert state.unstable() == ["111111111111"]


def test_distinct_accounts_keep_first_seen_order() -> None:
    """Order matters only so the printed table matches the run."""
    state = ScanState(
        observations=[
            observed("333333333333", Entitlement.ERROR_002, 1),
            observed("222222222222", Entitlement.ENTITLED, 2),
            observed("333333333333", Entitlement.ERROR_002, 3),
        ]
    )
    assert state.accounts == ["333333333333", "222222222222"]


# ---------------------------------------------------------------------------
# Argument handling, because the defaults are what bound the spend
# ---------------------------------------------------------------------------


def test_max_rounds_defaults_to_twice_the_limit() -> None:
    """Reallocation is not round-robin, so repeats have to be affordable."""
    args = poolscan.parse_args(["--limit", "3"])
    assert args.max_rounds == 6


def test_a_round_budget_that_cannot_reach_the_limit_is_refused() -> None:
    """Otherwise the scan reports fewer accounts than asked and looks done."""
    with pytest.raises(SystemExit):
        poolscan.parse_args(["--limit", "4", "--max-rounds", "2"])


def test_nothing_is_spent_without_yes() -> None:
    """A dry run is the default, because every start costs a launch."""
    args = poolscan.parse_args([])
    assert args.yes is False
    assert args.end_existing is False


# ---------------------------------------------------------------------------
# The scan loop, which is where the launches are spent and the sessions ended
# ---------------------------------------------------------------------------


class FakeFacts:
    """A session-facts stand-in with only the fields the scan reads."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.state = "Ready" if account_id else "Terminated"
        self.pool: list[dict[str, str]] = []

    @property
    def live(self) -> bool:
        """True when a session is up, matching ``endlab.SessionFacts.live``."""
        return bool(self.account_id)


class ScanHarness:
    """Drive ``scan`` against a scripted sequence of allocated accounts.

    Records every session start and end so a test can assert on what the loop
    did to the pool, not only on what it concluded.
    """

    def __init__(self, accounts: list[str], entitled: set[str] | None = None) -> None:
        self.accounts = list(accounts)
        self.entitled = entitled or set()
        self.current: str | None = None
        self.starts: list[str] = []
        self.ends: list[str] = []
        self.goes_down = True

    def install(self, monkeypatch: pytest.MonkeyPatch, preexisting: str = "") -> None:
        """Patch every call ``scan`` makes out to the network."""
        if preexisting:
            self.current = preexisting
        monkeypatch.setattr(poolscan, "fetch_facts", self._fetch_facts)
        monkeypatch.setattr(poolscan, "start_session", self._start_session)
        monkeypatch.setattr(poolscan, "measure_current_session", self._measure)
        monkeypatch.setattr(poolscan, "end_session", self._end_session)
        monkeypatch.setattr(poolscan, "wait_until_down", self._wait_until_down)

    def _fetch_facts(self, config: Any) -> FakeFacts:
        return FakeFacts(self.current or "")

    def _start_session(self, config: Any) -> None:
        self.current = self.accounts.pop(0) if self.accounts else "000000000000"
        self.starts.append(self.current)

    def _measure(self, config: Any, args: Any, *, wait: bool) -> Observation:
        account = self.current or ""
        return Observation(
            round_index=0,
            account=account,
            entitlement=(
                Entitlement.ENTITLED
                if account in self.entitled
                else Entitlement.ERROR_002
            ),
        )

    def _end_session(self, config: Any) -> None:
        self.ends.append(self.current or "")

    def _wait_until_down(self, config: Any) -> bool:
        if self.goes_down:
            self.current = None
        return self.goes_down


def scan_args(**overrides: Any) -> Any:
    """Build parsed arguments for a scan, with --yes already applied."""
    argv = ["--yes", "--no-log"]
    for name, value in overrides.items():
        argv += [f"--{name.replace('_', '-')}", str(value)]
    return poolscan.parse_args(argv)


def run_scan(harness: ScanHarness, args: Any) -> ScanState:
    """Run one scan against the harness and return what it measured."""
    state = ScanState()
    poolscan.scan(object(), args, poolscan.AuditLog(path=None), state)
    return state


def test_the_scan_stops_once_the_limit_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three distinct accounts is three launches, not the six-round budget."""
    harness = ScanHarness(["111111111111", "222222222222", "333333333333"])
    harness.install(monkeypatch)
    state = run_scan(harness, scan_args(limit=3))
    assert state.accounts == ["111111111111", "222222222222", "333333333333"]
    assert len(harness.starts) == 3
    # Two ends, not three: the account measured last is left allocated so the
    # operator can keep working in it.
    assert harness.ends == ["111111111111", "222222222222"]


def test_a_repeat_allocation_does_not_satisfy_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pool that keeps handing back one member must not read as done."""
    harness = ScanHarness(["111111111111", "111111111111", "111111111111"])
    harness.install(monkeypatch)
    state = run_scan(harness, scan_args(limit=3, max_rounds=3))
    assert state.accounts == ["111111111111"]
    assert len(state.observations) == 3


def test_the_round_budget_caps_the_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spend ceiling has to hold even when the limit is never reached.

    A pool that keeps handing back the same member never satisfies --limit, so
    --max-rounds is the only thing between the scan and the whole budget.
    """
    harness = ScanHarness(["111111111111"] * 10)
    harness.install(monkeypatch)
    state = run_scan(harness, scan_args(limit=2, max_rounds=2))
    assert len(harness.starts) == 2
    assert state.accounts == ["111111111111"]


def test_a_preexisting_session_is_never_ended_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The destructive-action guard. Ending wipes the member irreversibly."""
    harness = ScanHarness(["222222222222"])
    harness.install(monkeypatch, preexisting="999999999999")
    state = run_scan(harness, scan_args(limit=3))
    assert harness.ends == []
    assert harness.starts == []
    assert state.accounts == ["999999999999"]


def test_end_existing_lets_the_scan_rotate_past_an_open_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same path with the flag, so the guard is opt-out and not a wall."""
    harness = ScanHarness(["222222222222"])
    harness.install(monkeypatch, preexisting="999999999999")
    args = poolscan.parse_args(["--yes", "--no-log", "--limit", "2", "--end-existing"])
    state = run_scan(harness, args)
    assert harness.ends == ["999999999999"]
    assert state.accounts == ["999999999999", "222222222222"]


def test_a_session_that_will_not_go_down_stops_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting again too early would land in the same member and count twice."""
    harness = ScanHarness(["111111111111", "222222222222"])
    harness.install(monkeypatch)
    harness.goes_down = False
    state = run_scan(harness, scan_args(limit=3))
    assert len(state.observations) == 1
    assert len(harness.starts) == 1


def test_a_refusal_partway_through_keeps_the_rounds_already_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Those launches are spent. Discarding their result would spend them twice."""
    harness = ScanHarness(["111111111111", "222222222222"])
    harness.install(monkeypatch)
    calls = {"n": 0}
    real_measure = harness._measure

    def failing_measure(config: Any, args: Any, *, wait: bool) -> Observation:
        calls["n"] += 1
        if calls["n"] == 2:
            raise GuardError("identity guard refused")
        return real_measure(config, args, wait=wait)

    monkeypatch.setattr(poolscan, "measure_current_session", failing_measure)
    state = ScanState()
    with pytest.raises(GuardError):
        poolscan.scan(object(), scan_args(limit=3), poolscan.AuditLog(path=None), state)
    assert state.accounts == ["111111111111"]


# ---------------------------------------------------------------------------
# The region guard, which the scan enforces rather than warns about
# ---------------------------------------------------------------------------


def test_the_wrong_region_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Bedrock answer from us-west-2 answers a question nobody asked."""
    monkeypatch.setattr(
        poolscan, "fetch_vocareum_credentials", lambda config, wait: object()
    )
    monkeypatch.setattr(poolscan, "guard_credentials", lambda credentials: None)
    monkeypatch.setattr(poolscan, "build_session", lambda credentials: object())
    monkeypatch.setattr(
        poolscan,
        "discover_identity",
        lambda session, region: poolscan.Identity("111111111111", "arn"),
    )

    class Wrong:
        region = "us-west-2"

    with pytest.raises(GuardError, match="Region guard refused"):
        poolscan.measure_current_session(Wrong(), scan_args(), wait=False)


# ---------------------------------------------------------------------------
# The audit log, which is the only record once the accounts are released
# ---------------------------------------------------------------------------


def test_the_audit_log_is_one_json_object_per_line(tmp_path: Any) -> None:
    """JSONL so a run killed mid-scan still leaves a readable file."""
    path = tmp_path / "scan.jsonl"
    with poolscan.open_audit_log(path) as log:
        log.write("run_start", model="m")
        log.write("measurement", account="111111111111")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert [record["event"] for record in records] == ["run_start", "measurement"]
    assert all(record["ts"].endswith("Z") for record in records)
    assert len({record["runId"] for record in records}) == 1


def test_a_new_audit_log_is_private(tmp_path: Any) -> None:
    """The evidence may contain account ids and error details."""
    path = tmp_path / "scan.jsonl"
    with poolscan.open_audit_log(path) as log:
        log.write("run_start")
    assert path.stat().st_mode & 0o777 == 0o600


def test_each_line_is_flushed_as_it_is_written(tmp_path: Any) -> None:
    """A killed run must not lose the evidence its launches already bought."""
    path = tmp_path / "scan.jsonl"
    with poolscan.open_audit_log(path) as log:
        log.write("session_start", round=1)
        # Read from a second handle while the first is still open.
        assert (
            json.loads(path.read_text(encoding="utf-8").splitlines()[0])["round"] == 1
        )


def test_a_disabled_log_writes_nothing_and_needs_no_branches(tmp_path: Any) -> None:
    """--no-log must not make every call site conditional."""
    with poolscan.open_audit_log(None) as log:
        log.write("run_start")
    assert list(tmp_path.iterdir()) == []


def test_the_scan_records_starts_ends_and_measurements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The two calls that cost budget and release an account must be auditable."""
    harness = ScanHarness(["111111111111", "222222222222"])
    harness.install(monkeypatch)
    path = tmp_path / "scan.jsonl"
    state = ScanState()
    with poolscan.open_audit_log(path) as log:
        poolscan.scan(object(), scan_args(limit=2), log, state)

    events = [
        json.loads(line)["event"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert events.count("session_start_requested") == 2
    assert events.count("session_start_accepted") == 2
    assert events.count("session_end_requested") == 1
    assert events.count("session_end_accepted") == 1
    assert events.count("measurement") == 2
    assert "stop" in events


def test_a_failed_start_is_not_logged_as_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """An attempted start and a completed start are different audit facts."""
    harness = ScanHarness(["111111111111"])
    harness.install(monkeypatch)

    def fail_start(config: Any) -> None:
        raise GuardError("start was refused")

    monkeypatch.setattr(poolscan, "start_session", fail_start)
    path = tmp_path / "scan.jsonl"
    with (
        poolscan.open_audit_log(path) as log,
        pytest.raises(GuardError, match="start was refused"),
    ):
        poolscan.scan(object(), scan_args(), log, ScanState())

    events = [
        json.loads(line)["event"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert "session_start_requested" in events
    assert "session_start_accepted" not in events


def test_an_unexpected_failure_gets_a_redacted_terminal_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A crash must remain diagnosable without leaking a token into the log."""

    class FakeConfig:
        region = "us-east-1"

        def part_url(self) -> str:
            return "https://example.test/courses/1/assignments/2/parts/3"

    monkeypatch.setattr(
        poolscan,
        "load_config",
        lambda env_file, region, course: FakeConfig(),
    )

    def fail_scan(config: Any, args: Any, log: Any, state: Any) -> None:
        raise RuntimeError("sessionToken=do-not-record")

    monkeypatch.setattr(poolscan, "scan", fail_scan)
    path = tmp_path / "scan.jsonl"
    with pytest.raises(RuntimeError, match="do-not-record"):
        poolscan.main(["--yes", "--log", str(path)])

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "failed"
    assert records[-1]["errorType"] == "RuntimeError"
    assert "do-not-record" not in records[-1]["error"]
    assert "********" in records[-1]["error"]


def test_log_and_no_log_contradict_each_other() -> None:
    """Silently honouring one of them would mislead about what was recorded."""
    with pytest.raises(SystemExit):
        poolscan.parse_args(["--log", "x.jsonl", "--no-log"])


def test_a_log_path_is_chosen_by_default() -> None:
    """Auditing is the default, because the accounts stop existing afterwards."""
    args = poolscan.parse_args([])
    assert args.log is not None
    assert args.log.suffix == ".jsonl"
    assert args.log.parent.name == poolscan.DEFAULT_LOG_DIR


# ---------------------------------------------------------------------------
# Waiting for the lab stack.
#
# The first live pass invoked two seconds after the start was accepted and got
# AccessDenied on bedrock:InvokeModel, because the grant is a RolePolicy inside
# lab.template that CloudFormation had not applied yet. Six minutes later the
# same account answered Error 002. These tests exist so a round can never again
# report the build's progress as the account's entitlement.
# ---------------------------------------------------------------------------


class FakePaginator:
    """Hand back scripted ``list_stacks`` pages, one call at a time."""

    def __init__(self, owner: FakeCloudFormation) -> None:
        self.owner = owner

    def paginate(self) -> list[dict[str, Any]]:
        """Return one poll's worth of stack summaries."""
        if self.owner.error is not None:
            raise self.owner.error
        rows = (
            self.owner.pages.pop(0)
            if len(self.owner.pages) > 1
            else self.owner.pages[0]
        )
        self.owner.polls += 1
        return [{"StackSummaries": rows}]


class FakeCloudFormation:
    """A CloudFormation stand-in that answers a scripted sequence of polls."""

    def __init__(
        self,
        pages: list[list[dict[str, str]]],
        error: BaseException | None = None,
    ) -> None:
        self.pages = pages
        self.error = error
        self.polls = 0

    def get_paginator(self, name: str) -> FakePaginator:
        """Return the only paginator this fake knows about."""
        assert name == "list_stacks"
        return FakePaginator(self)


class FakeStackSession:
    """A boto3 session stand-in that yields one CloudFormation client."""

    def __init__(self, client: FakeCloudFormation) -> None:
        self._client = client

    def client(self, name: str, **kwargs: Any) -> FakeCloudFormation:
        """Return the fake client regardless of which service was asked for."""
        return self._client


def summary(name: str, status: str) -> dict[str, str]:
    """Build one ``ListStacks`` summary row."""
    return {"StackName": name, "StackStatus": status}


def wait_for(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[list[dict[str, str]]],
    error: BaseException | None = None,
    timeout_seconds: int = 60,
) -> tuple[Any, FakeCloudFormation]:
    """Run ``wait_for_stack`` against scripted pages without real sleeping."""
    monkeypatch.setattr(poolscan.time, "sleep", lambda seconds: None)
    client = FakeCloudFormation(pages, error)
    result = poolscan.wait_for_stack(
        FakeStackSession(client),
        "us-east-1",
        timeout_seconds=timeout_seconds,
        poll_seconds=1,
    )
    return result, client


def test_a_finished_stack_ends_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CREATE_COMPLETE lab stack is the signal the grant has landed."""
    result, _ = wait_for(monkeypatch, [[summary("c100001a1l1t1w1", "CREATE_COMPLETE")]])
    assert result.outcome == poolscan.StackWait.READY
    assert result.can_measure


def test_the_wait_polls_until_the_stack_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-progress build is not an answer, so the wait keeps reading."""
    result, client = wait_for(
        monkeypatch,
        [
            [summary("lab", "CREATE_IN_PROGRESS")],
            [summary("lab", "CREATE_IN_PROGRESS")],
            [summary("lab", "CREATE_COMPLETE")],
        ],
    )
    assert result.outcome == poolscan.StackWait.READY
    assert client.polls == 3


def test_a_failed_stack_ends_the_wait_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotating beats burning the timeout, which is why this watches the stack."""
    result, client = wait_for(
        monkeypatch,
        [[summary("lab", "CREATE_FAILED")], [summary("lab", "CREATE_COMPLETE")]],
        timeout_seconds=9999,
    )
    assert result.outcome == poolscan.StackWait.FAILED
    assert not result.can_measure
    assert client.polls == 1


def test_a_rollback_counts_as_a_failed_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rolled-back build never applied the RolePolicy either."""
    result, _ = wait_for(monkeypatch, [[summary("lab", "ROLLBACK_COMPLETE")]])
    assert result.outcome == poolscan.StackWait.FAILED


def test_a_stack_on_its_way_out_is_not_waited_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleting leftover belongs to the previous session, not this one."""
    result, _ = wait_for(
        monkeypatch,
        [
            [
                summary("old", "DELETE_IN_PROGRESS"),
                summary("old2", "DELETE_COMPLETE"),
                summary("lab", "CREATE_COMPLETE"),
            ]
        ],
    )
    assert result.outcome == poolscan.StackWait.READY


def test_no_stack_at_all_is_not_mistaken_for_a_finished_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty account must not read as ready, or the probe runs too early."""
    result, _ = wait_for(monkeypatch, [[]], timeout_seconds=0)
    assert result.outcome == poolscan.StackWait.TIMEOUT
    assert not result.can_measure


def test_a_stack_that_never_finishes_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged build must not hold the whole scan."""
    result, _ = wait_for(
        monkeypatch, [[summary("lab", "CREATE_IN_PROGRESS")]], timeout_seconds=0
    )
    assert result.outcome == poolscan.StackWait.TIMEOUT


def test_an_unreadable_stack_still_gets_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account that refuses ListStacks can still answer the real question."""
    denied = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "not authorized to perform: cloudformation:ListStacks",
            }
        },
        "ListStacks",
    )
    result, _ = wait_for(monkeypatch, [[]], error=denied)
    assert result.outcome == poolscan.StackWait.UNREADABLE
    assert result.can_measure


def test_a_failed_stack_is_recorded_without_invoking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the grant there is nothing to invoke, and no answer to report."""
    monkeypatch.setattr(
        poolscan,
        "fetch_vocareum_credentials",
        lambda config, wait: SimpleNamespace(
            access_key_id="ASIA", secret_access_key="secret", session_token="token"
        ),
    )
    monkeypatch.setattr(poolscan, "guard_credentials", lambda credentials: None)
    monkeypatch.setattr(poolscan, "build_session", lambda credentials: object())
    monkeypatch.setattr(
        poolscan,
        "discover_identity",
        lambda session, region: poolscan.Identity(account="111111111111", arn="arn"),
    )
    monkeypatch.setattr(
        poolscan,
        "wait_for_stack",
        lambda *a, **k: poolscan.StackWait(poolscan.StackWait.FAILED, "lab FAILED"),
    )

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("probe_bedrock ran without a stack")

    monkeypatch.setattr(poolscan, "probe_bedrock", refuse)

    config = SimpleNamespace(region="us-east-1")
    args = scan_args()
    observation = poolscan.measure_current_session(config, args, wait=True)
    assert observation.entitlement is Entitlement.STACK_FAILED
    assert observation.account == "111111111111"


def test_a_failed_stack_does_not_satisfy_the_limit() -> None:
    """A launch spent on a broken build bought no answer, so it cannot count."""
    state = ScanState()
    state.observations.append(
        Observation(
            round_index=1, account="111111111111", entitlement=Entitlement.STACK_FAILED
        )
    )
    state.observations.append(
        Observation(
            round_index=2, account="222222222222", entitlement=Entitlement.ERROR_002
        )
    )
    assert state.accounts == ["111111111111", "222222222222"]
    assert state.measured_accounts == ["222222222222"]


def test_a_failed_stack_does_not_make_an_account_look_unstable() -> None:
    """Unstable means two entitlement answers, not one answer and one failure."""
    state = ScanState()
    state.observations.append(
        Observation(
            round_index=1, account="111111111111", entitlement=Entitlement.STACK_FAILED
        )
    )
    state.observations.append(
        Observation(
            round_index=2, account="111111111111", entitlement=Entitlement.ERROR_002
        )
    )
    assert state.unstable() == []


# ---------------------------------------------------------------------------
# Waiting out a pool that is still cleaning up.
#
# The course's pool held three members on 2026-08-25. Ending one left every
# remaining member wiping, and Vocareum refused the next start three times with
# HTTP 400 "account is still in cleanup, expect another 5 minutes". The scan
# exited on the first refusal, which meant an unattended pass ended after one
# rotation. A refused start is not a start and costs no launch, so the wait is
# free; what is not free is retrying a refusal that will never clear.
# ---------------------------------------------------------------------------

CLEANUP_REFUSAL = (
    "HTTP 400 from PUT https://api.vocareum.com/api/v2/courses/1/assignments/2"
    '/parts/3/resources/4 Response body: {"error": {"message": "Failed '
    'operation: Start. account is still in cleanup, expect another 5 minutes"}}'
)


class FlakyStart:
    """A ``start_session`` stand-in that refuses a set number of times first."""

    def __init__(self, refusals: int, error: BaseException | None = None) -> None:
        self.refusals = refusals
        self.error = error or ApiError(CLEANUP_REFUSAL, status_code=400)
        self.attempts = 0

    def __call__(self, config: Any) -> None:
        """Refuse until the scripted refusals run out, then succeed."""
        self.attempts += 1
        if self.attempts <= self.refusals:
            raise self.error


def retrying_start(
    monkeypatch: pytest.MonkeyPatch,
    start: FlakyStart,
    *,
    retries: int = 3,
) -> tuple[poolscan.AuditLog, FlakyStart]:
    """Drive ``start_session_with_retry`` without real waiting."""
    monkeypatch.setattr(poolscan.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(poolscan, "start_session", start)
    log = poolscan.AuditLog(path=None)
    poolscan.start_session_with_retry(object(), log, 1, retries=retries, wait_seconds=1)
    return log, start


def test_a_cleanup_refusal_is_recognized() -> None:
    """The status is a plain 400, so the message body is the only signal."""
    assert poolscan.is_cleanup_refusal(ApiError(CLEANUP_REFUSAL, status_code=400))


def test_an_unrelated_refusal_is_not_a_cleanup_refusal() -> None:
    """A wrong part also returns 400 and must not be waited on."""
    error = ApiError("HTTP 400 ... no such part", status_code=400)
    assert not poolscan.is_cleanup_refusal(error)


def test_the_scan_waits_out_a_cleaning_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three refusals then a start is the sequence measured live."""
    _, start = retrying_start(monkeypatch, FlakyStart(refusals=3), retries=3)
    assert start.attempts == 4


def test_a_pool_that_never_clears_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry is bounded, or a wedged member holds the scan forever."""
    monkeypatch.setattr(poolscan.time, "sleep", lambda seconds: None)
    start = FlakyStart(refusals=99)
    monkeypatch.setattr(poolscan, "start_session", start)
    with pytest.raises(ApiError) as caught:
        poolscan.start_session_with_retry(
            object(), poolscan.AuditLog(path=None), 1, retries=2, wait_seconds=1
        )
    assert "still cleaning up" in str(caught.value)
    assert "spent no launch" in str(caught.value)
    assert isinstance(caught.value.__cause__, ApiError)
    assert start.attempts == 3


def test_a_real_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rotated token does not become valid by asking eight more times."""
    monkeypatch.setattr(poolscan.time, "sleep", lambda seconds: None)
    start = FlakyStart(
        refusals=99, error=ApiError("HTTP 401 Unauthorized", status_code=401)
    )
    monkeypatch.setattr(poolscan, "start_session", start)
    with pytest.raises(ApiError, match="401"):
        poolscan.start_session_with_retry(
            object(), poolscan.AuditLog(path=None), 1, retries=8, wait_seconds=1
        )
    assert start.attempts == 1


def test_zero_retries_still_starts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """--cleanup-retries 0 means no waiting, not no starting."""
    _, start = retrying_start(monkeypatch, FlakyStart(refusals=0), retries=0)
    assert start.attempts == 1


def test_every_deferral_is_logged(tmp_path: Any) -> None:
    """A pass that waited half an hour has to say so in the audit log."""
    path = tmp_path / "scan.jsonl"
    start = FlakyStart(refusals=2)
    with (
        poolscan.open_audit_log(path) as log,
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(poolscan.time, "sleep", lambda seconds: None)
        patch.setattr(poolscan, "start_session", start)
        poolscan.start_session_with_retry(object(), log, 1, retries=2, wait_seconds=1)
    events = [json.loads(line)["event"] for line in path.read_text().splitlines()]
    assert events.count("session_start_deferred") == 2
    assert events.count("session_start_accepted") == 1


# ---------------------------------------------------------------------------
# Waiting out an end Vocareum refuses
#
# The first six-account pass measured three accounts and then died on the
# teardown of the third: Vocareum answered the end with ``Lab is setting up.
# Please wait for it to finish`` even though CloudFormation had already
# reported CREATE_COMPLETE and the Bedrock probe had already run. The launch
# was spent and the session stayed open, which is the expensive half of the
# start-side failure, so the bound here stops the scan and names the account
# rather than rotating past a member it never released.
# ---------------------------------------------------------------------------

SETUP_REFUSAL = (
    "HTTP 400 from DELETE https://api.vocareum.com/api/v2/courses/1/assignments"
    '/2/parts/3/resources/4 Response body: {"error": {"message": "Failed '
    'operation: End. Lab is setting up. Please wait for it to finish"}}'
)


class FlakyEnd:
    """An ``end_session`` stand-in that refuses a set number of times first."""

    def __init__(self, refusals: int, error: BaseException | None = None) -> None:
        self.refusals = refusals
        self.error = error or ApiError(SETUP_REFUSAL, status_code=400)
        self.attempts = 0

    def __call__(self, config: Any) -> None:
        """Refuse until the scripted refusals run out, then succeed."""
        self.attempts += 1
        if self.attempts <= self.refusals:
            raise self.error


def retrying_end(
    monkeypatch: pytest.MonkeyPatch,
    end: FlakyEnd,
    *,
    retries: int = 3,
) -> tuple[poolscan.AuditLog, FlakyEnd]:
    """Drive ``end_session_with_retry`` without real waiting."""
    monkeypatch.setattr(poolscan.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(poolscan, "end_session", end)
    log = poolscan.AuditLog(path=None)
    poolscan.end_session_with_retry(
        object(), log, 1, "111111111111", retries=retries, wait_seconds=1
    )
    return log, end


def test_a_setup_refusal_is_recognized() -> None:
    """The live refusal body, matched as the scan will really see it."""
    assert poolscan.is_setup_refusal(ApiError(SETUP_REFUSAL, status_code=400))


def test_a_cleanup_refusal_is_not_a_setup_refusal() -> None:
    """The two refusals are different states and must not share a retry."""
    error = ApiError(CLEANUP_REFUSAL, status_code=400)
    assert not poolscan.is_setup_refusal(error)
    assert not poolscan.is_cleanup_refusal(ApiError(SETUP_REFUSAL, status_code=400))


def test_the_scan_waits_out_a_lab_vocareum_calls_unfinished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One refusal then an accepted end leaves the pool member released."""
    _, end = retrying_end(monkeypatch, FlakyEnd(refusals=1), retries=3)
    assert end.attempts == 2


def test_an_end_that_never_clears_names_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhaustion has to say which account is still holding a spent launch."""
    monkeypatch.setattr(poolscan.time, "sleep", lambda seconds: None)
    end = FlakyEnd(refusals=99)
    monkeypatch.setattr(poolscan, "end_session", end)
    with pytest.raises(ApiError) as caught:
        poolscan.end_session_with_retry(
            object(),
            poolscan.AuditLog(path=None),
            1,
            "777777777777",
            retries=2,
            wait_seconds=1,
        )
    assert "777777777777" in str(caught.value)
    assert "vocareum-end-lab" in str(caught.value)
    assert isinstance(caught.value.__cause__, ApiError)
    assert end.attempts == 3


def test_a_real_end_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rotated token refuses an end too, and waiting cannot fix it."""
    monkeypatch.setattr(poolscan.time, "sleep", lambda seconds: None)
    end = FlakyEnd(
        refusals=99, error=ApiError("HTTP 401 Unauthorized", status_code=401)
    )
    monkeypatch.setattr(poolscan, "end_session", end)
    with pytest.raises(ApiError, match="401"):
        poolscan.end_session_with_retry(
            object(),
            poolscan.AuditLog(path=None),
            1,
            "111111111111",
            retries=8,
            wait_seconds=1,
        )
    assert end.attempts == 1


def test_zero_end_retries_still_ends_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """--end-retries 0 means no waiting, not no ending."""
    _, end = retrying_end(monkeypatch, FlakyEnd(refusals=0), retries=0)
    assert end.attempts == 1


def test_every_end_deferral_is_logged(tmp_path: Any) -> None:
    """The audit log has to show a teardown that took several attempts."""
    path = tmp_path / "scan.jsonl"
    end = FlakyEnd(refusals=2)
    with (
        poolscan.open_audit_log(path) as log,
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(poolscan.time, "sleep", lambda seconds: None)
        patch.setattr(poolscan, "end_session", end)
        poolscan.end_session_with_retry(
            object(), log, 1, "111111111111", retries=2, wait_seconds=1
        )
    events = [json.loads(line)["event"] for line in path.read_text().splitlines()]
    assert events.count("session_end_deferred") == 2
    assert events.count("session_end_accepted") == 1


def test_a_refused_end_still_reports_what_the_scan_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rounds already paid for survive a teardown that will not clear.

    This is the shape of the live failure: two accounts measured, the third
    measured, and then the end refused. The launches are spent either way.
    """
    harness = ScanHarness(["111111111111", "222222222222"])
    harness.install(monkeypatch)
    monkeypatch.setattr(poolscan.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(poolscan, "end_session", FlakyEnd(refusals=99))
    state = ScanState()
    with pytest.raises(ApiError, match="111111111111"):
        poolscan.scan(
            object(),
            scan_args(limit=2, end_retries=1),
            poolscan.AuditLog(path=None),
            state,
        )
    assert state.accounts == ["111111111111"]
