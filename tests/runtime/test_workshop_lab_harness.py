"""Drive `workshop_lab.Harness` and `guards` with fakes, offline.

`Harness` takes a session and its checks take callables, which is what makes all
of this testable without a lab launch. Keep it that way: a test that needs a
live session is a test that will not run.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from workshop_lab import guards
from workshop_lab.harness import FAIL, GONE_CODES, PASS, SKIP, Harness


def client_error(code: str, message: str = "nope") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Operation")


class FakeCredentials:
    def __init__(self, access: str, secret: str, token: str) -> None:
        self.access_key, self.secret_key, self.token = access, secret, token

    def get_frozen_credentials(self):
        return self


class FakeSession:
    """Enough of a boto3 Session for the harness: credentials, region, clients."""

    def __init__(self, credentials=None, region: str | None = "us-east-1") -> None:
        self._credentials = credentials
        self.region_name = region
        self.made: list[str] = []

    def get_credentials(self):
        return self._credentials

    def client(self, name: str, region_name: str | None = None):
        self.made.append(name)
        return object()


def harness(session=None, **kwargs) -> tuple[Harness, list[str]]:
    printed: list[str] = []
    if session is None:
        session = FakeSession(FakeCredentials("AKIA", "secret", "token"))
    return Harness(session=session, echo=printed.append, **kwargs), printed


# --- guards ------------------------------------------------------------------
def test_missing_credentials_object_raises():
    lab, _ = harness(FakeSession(None))
    with pytest.raises(RuntimeError, match="No AWS credentials at all"):
        lab.verify_credentials()


@pytest.mark.parametrize(
    "credentials, blank",
    [
        (FakeCredentials("", "secret", "token"), "aws_access_key_id"),
        (FakeCredentials("AKIA", "", "token"), "aws_secret_access_key"),
        (FakeCredentials("AKIA", "secret", ""), "aws_session_token"),
        (FakeCredentials("AKIA", "secret", "   "), "aws_session_token"),
    ],
)
def test_blank_credential_field_raises(credentials, blank):
    """A terminated session returns empty strings, not absent keys.

    boto3 reads an empty string as absent, falls through its provider chain, and
    reports on whatever ambient credentials the machine holds. Whitespace is in
    the table because `.strip()` is what stands between that and a confident
    report about the operator's own account.
    """
    lab, _ = harness(FakeSession(credentials))
    with pytest.raises(RuntimeError, match=blank):
        lab.verify_credentials()


def test_good_credentials_never_print_the_secret():
    """Only the length and the last four characters may reach the output.

    The tail is shown on purpose: it identifies a session without authorizing
    one, the same reasoning `security.py` uses to leave access key IDs visible.
    """
    full_secret = "S" * 36 + "TAIL"
    lab, printed = harness(
        FakeSession(FakeCredentials("A" * 16 + "KEYS", full_secret, "T" * 20 + "TOKN"))
    )
    lab.verify_credentials()
    body = "\n".join(printed)

    assert full_secret not in body
    assert "S" * 10 not in body
    assert "[40 chars]****TAIL" in body


def test_wrong_region_raises():
    lab, _ = harness(FakeSession(FakeCredentials("A", "B", "C"), region="us-west-2"))
    with pytest.raises(RuntimeError, match="needs us-east-1"):
        lab.verify_identity()


def test_no_region_at_all_raises():
    """An absent region is a failure, not a silent us-east-1.

    The harness used to default `region_name` to the required region, so this
    guard compared us-east-1 against us-east-1 and passed for a session that had
    never said where it was, and step 2 then printed a region nobody measured.
    """
    lab, _ = harness(FakeSession(FakeCredentials("A", "B", "C"), region=None))
    assert lab.region == ""
    with pytest.raises(RuntimeError, match="names no region"):
        lab.verify_identity()


def test_mask_keeps_only_the_tail():
    assert guards.mask("ABCDEFGHIJ") == "[10 chars]****GHIJ"
    assert guards.mask("AB") == "****"


# --- check -------------------------------------------------------------------
def test_check_records_pass_and_returns_the_response():
    lab, _ = harness()
    assert lab.check("named", lambda: {"ok": 1}) == {"ok": 1}
    assert lab.results == [("named", PASS, "")]


def test_check_turns_a_none_response_into_true():
    """A call that authorized but returned nothing still answers the question."""
    lab, _ = harness()
    assert lab.check("named", lambda: None) is True


@pytest.mark.parametrize("code", sorted(GONE_CODES))
def test_absent_resource_is_a_pass(code):
    """Authorization succeeded on something that was not there."""
    lab, _ = harness()

    def raise_gone():
        raise client_error(code)

    assert lab.check("named", raise_gone) is None
    assert lab.results[0][1] == PASS


def test_denial_is_a_fail_carrying_the_code():
    lab, _ = harness()

    def denied():
        raise client_error("AccessDeniedException", "explicit deny in an SCP")

    lab.check("named", denied)
    _, verdict, detail = lab.results[0]
    assert verdict == FAIL
    assert "AccessDeniedException" in detail
    assert "explicit deny in an SCP" in detail


def test_botocore_error_is_a_fail_not_an_exception():
    lab, _ = harness()

    def unreachable():
        raise EndpointConnectionError(endpoint_url="https://example.invalid")

    lab.check("named", unreachable)
    assert lab.results[0][1] == FAIL


# --- sweep -------------------------------------------------------------------
def test_sweep_records_nothing_either_way():
    """A delete of last run's litter answers no question the tracker asks."""
    lab, printed = harness()
    lab.sweep("gateway", lambda: None)

    def raise_gone():
        raise client_error("ResourceNotFoundException")

    lab.sweep("gateway", raise_gone)
    assert lab.results == []
    assert "swept a leftover gateway" in "\n".join(printed)


def test_sweep_reports_a_real_failure_without_recording_it():
    lab, printed = harness()

    def denied():
        raise client_error("AccessDeniedException")

    lab.sweep("gateway", denied)
    assert lab.results == []
    assert "could not sweep gateway" in "\n".join(printed)


# --- names ---------------------------------------------------------------------
def test_names_are_unavailable_before_identity_is_verified():
    """Names carry the account id, so reading them early splices in an empty one."""
    lab, _ = harness()
    with pytest.raises(RuntimeError, match="verify_identity"):
        _ = lab.names


# --- summary ---------------------------------------------------------------------
def test_summary_counts_every_verdict_including_extras():
    lab, printed = harness()
    lab.record("a", PASS)
    lab.record("b", FAIL, "denied")
    lab.skip("c", "no driver")
    counts = lab.summary(extra=[("Neo4j", PASS, "")])
    assert counts == {PASS: 2, FAIL: 1, SKIP: 1}
    assert "Something needs attention" in "\n".join(printed)


def test_a_skipped_step_does_not_block_ready():
    """SKIP is the ordinary outcome of steps 8, 9, 11, 12 and 13.

    When the CodeBuild build is stopped early, as every build was until
    2026-08-07, no image is produced and everything downstream of it skips.
    Gating readiness on a zero SKIP count would mean the notebook never once
    told a student their environment was fine.
    """
    lab, printed = harness()
    lab.record("a", PASS)
    lab.skip("codebuild image", "the build did not finish")
    counts = lab.summary(extra=[("Neo4j is reachable and writable", PASS, "")])

    assert counts == {PASS: 2, FAIL: 0, SKIP: 1}
    assert "Environment is ready" in "\n".join(printed)


@pytest.mark.parametrize("verdict", [FAIL, SKIP])
def test_an_unmet_prerequisite_does_block_ready(verdict):
    """Neo4j arrives through `extra` and is a hard prerequisite, unlike a step."""
    lab, printed = harness()
    lab.record("a", PASS)
    lab.summary(extra=[("Neo4j", verdict, "see step 3")])

    assert "Environment is ready" not in "\n".join(printed)
    assert "Something needs attention" in "\n".join(printed)


def test_summary_accepts_a_generator_for_extra():
    """`extra` is consumed twice: once to count, once to test the prerequisites."""
    lab, printed = harness()
    counts = lab.summary(extra=(row for row in [("Neo4j", PASS, "")]))

    assert counts[PASS] == 1
    assert "Environment is ready" in "\n".join(printed)


def test_verify_credentials_returns_nothing():
    """Jupyter displays a cell's last value, and these are live secrets."""
    lab, _ = harness()
    assert lab.verify_credentials() is None
    assert (
        guards.verify_credentials(FakeSession(FakeCredentials("A", "B", "C"))) is None
    )


def test_clients_are_cached_one_per_service():
    session = FakeSession(FakeCredentials("A", "B", "C"))
    lab, _ = harness(session)
    assert lab.client("iam") is lab.client("iam")
    lab.client("ecr")
    assert session.made == ["iam", "ecr"]
