from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from vocareum_tools import probe, refresh
from vocareum_tools.config import Environment

ACCOUNT = "123456789012"
STACK = "c00001a0000002l00000003t0w123456789012"

CAPTURED_OPEN_PAYLOAD = {
    "resources": [
        {
            "aws_data": {
                "aws_access_key_id": "ASIAEXAMPLE",
                "aws_secret_access_key": "captured-secret",
                "aws_session_token": "captured-token",
            }
        }
    ]
}


def summary(name: str, status: str) -> dict[str, Any]:
    return {
        "StackName": name,
        "StackStatus": status,
        "CreationTime": datetime.now(UTC),
    }


class FakePaginator:
    def __init__(self, summaries: list[dict[str, Any]]) -> None:
        self.summaries = summaries

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert "StackStatusFilter" in kwargs
        return [{"StackSummaries": self.summaries}]


class FakeCloudFormation:
    def __init__(self, summaries: list[dict[str, Any]]) -> None:
        self.summaries = summaries

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_stacks"
        return FakePaginator(self.summaries)


class FakeSts:
    def get_caller_identity(self) -> dict[str, str]:
        return {
            "Account": ACCOUNT,
            "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/voclabs/user1",
        }


class FakeSession:
    def __init__(self, summaries: list[dict[str, Any]]) -> None:
        self.summaries = summaries

    def client(self, name: str, **kwargs: Any) -> Any:
        if name == "sts":
            return FakeSts()
        if name == "cloudformation":
            return FakeCloudFormation(self.summaries)
        raise AssertionError(f"unexpected client {name}")


def make_config() -> probe.Config:
    return probe.Config(
        api_base="https://example.test/api/v2",
        course_id="course",
        assignment_id="assignment",
        part_id="part",
        user_id="user",
        api_key="api-key",
        expected_account="000000000000",
        region="us-east-1",
    )


def test_discovery_replays_a_captured_payload_through_the_real_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = FakeSession([summary(STACK, "CREATE_COMPLETE")])
    monkeypatch.setattr(
        probe, "request_json", lambda *args, **kwargs: CAPTURED_OPEN_PAYLOAD
    )
    monkeypatch.setattr(probe.boto3.session, "Session", lambda **kwargs: fake_session)

    values = refresh.discover_values(make_config())

    assert values == refresh.FreshValues(ACCOUNT, STACK)


def test_discovery_refuses_a_session_record_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe, "request_json", lambda *args, **kwargs: {"resources": []}
    )

    with pytest.raises(probe.GuardError, match="probably not running"):
        refresh.discover_values(make_config())


@pytest.mark.parametrize(
    "summaries",
    [
        [],
        [summary("one", "CREATE_COMPLETE"), summary("two", "ROLLBACK_COMPLETE")],
        [summary("old", "DELETE_COMPLETE")],
    ],
    ids=["none", "ambiguous", "deleted-only"],
)
def test_stack_selection_refuses_zero_or_ambiguous_candidates(
    summaries: list[dict[str, Any]],
) -> None:
    with pytest.raises(refresh.RefreshError, match="exactly one"):
        refresh.select_stack(summaries)


def test_refresh_target_uses_the_course_file(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text("SHARED=one\n", encoding="utf-8")
    courses = tmp_path / "courses"
    courses.mkdir()
    course = courses / "graphrag.env"
    course.write_text(
        f"{refresh.ACCOUNT_VAR}=old\n{refresh.STACK_VAR}=old\n",
        encoding="utf-8",
    )

    target = refresh.refresh_target(Environment.from_file(base, "graphrag"))

    assert target == course


def test_refresh_refuses_course_values_inherited_from_the_base(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text(
        f"{refresh.ACCOUNT_VAR}=old\n{refresh.STACK_VAR}=old\n", encoding="utf-8"
    )
    courses = tmp_path / "courses"
    courses.mkdir()
    (courses / "graphrag.env").write_text("COURSE_ONLY=one\n", encoding="utf-8")

    with pytest.raises(refresh.RefreshError, match="inherits"):
        refresh.refresh_target(Environment.from_file(base, "graphrag"))


def test_refresh_refuses_pins_from_different_files(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text(f"{refresh.ACCOUNT_VAR}=old\n", encoding="utf-8")
    courses = tmp_path / "courses"
    courses.mkdir()
    (courses / "graphrag.env").write_text(
        f"{refresh.STACK_VAR}=old\n", encoding="utf-8"
    )

    with pytest.raises(refresh.RefreshError, match="different files"):
        refresh.refresh_target(Environment.from_file(base, "graphrag"))


def test_refresh_refuses_a_real_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / ".env"
    base.write_text(
        f"{refresh.ACCOUNT_VAR}=old\n{refresh.STACK_VAR}=old\n", encoding="utf-8"
    )
    monkeypatch.setenv(refresh.ACCOUNT_VAR, ACCOUNT)

    with pytest.raises(refresh.RefreshError, match="shadow"):
        refresh.refresh_target(Environment.from_file(base))


def test_render_refresh_preserves_comments_quotes_and_other_lines() -> None:
    original = (
        "# keep this\n"
        f"export {refresh.ACCOUNT_VAR} = 'old'  # account comment\n"
        "UNCHANGED=yes\n"
        f'{refresh.STACK_VAR}="old"\n'
    )

    rendered = refresh.render_refresh(original, refresh.FreshValues(ACCOUNT, STACK))

    assert rendered == (
        "# keep this\n"
        f"export {refresh.ACCOUNT_VAR} = '{ACCOUNT}'  # account comment\n"
        "UNCHANGED=yes\n"
        f'{refresh.STACK_VAR}="{STACK}"\n'
    )


def test_render_refuses_before_changing_a_missing_or_duplicate_pair() -> None:
    duplicate = (
        f"{refresh.ACCOUNT_VAR}=one\n"
        f"{refresh.ACCOUNT_VAR}=two\n"
        f"{refresh.STACK_VAR}=stack\n"
    )

    with pytest.raises(refresh.RefreshError, match="exactly once"):
        refresh.render_refresh(duplicate, refresh.FreshValues(ACCOUNT, STACK))


def test_dry_run_prints_both_values_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / ".env"
    original = (
        "VOCAREUM_API_KEY=key\n"
        "VOCAREUM_COURSE_ID=course\n"
        "VOCAREUM_ASSIGNMENT_ID=assignment\n"
        "VOCAREUM_PART_ID=part\n"
        "VOCAREUM_USER_ID=user\n"
        f"{refresh.ACCOUNT_VAR}=old-account\n"
        f"{refresh.STACK_VAR}=old-stack\n"
    )
    base.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        refresh, "discover_values", lambda config: refresh.FreshValues(ACCOUNT, STACK)
    )

    code = refresh.main(["--env-file", str(base), "--dry-run"])

    assert code == refresh.EXIT_OK
    assert base.read_text(encoding="utf-8") == original
    printed = capsys.readouterr().out
    assert str(base) in printed
    assert f"{refresh.ACCOUNT_VAR}={ACCOUNT}" in printed
    assert f"{refresh.STACK_VAR}={STACK}" in printed


def test_refresh_writes_both_values_with_one_atomic_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / ".env"
    base.write_text(
        "VOCAREUM_API_KEY=key\n"
        "VOCAREUM_COURSE_ID=course\n"
        "VOCAREUM_ASSIGNMENT_ID=assignment\n"
        "VOCAREUM_PART_ID=part\n"
        "VOCAREUM_USER_ID=user\n"
        f"{refresh.ACCOUNT_VAR}=old-account\n"
        f"{refresh.STACK_VAR}=old-stack\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        refresh, "discover_values", lambda config: refresh.FreshValues(ACCOUNT, STACK)
    )

    code = refresh.main(["--env-file", str(base)])

    assert code == refresh.EXIT_OK
    updated = base.read_text(encoding="utf-8")
    assert f"{refresh.ACCOUNT_VAR}={ACCOUNT}\n" in updated
    assert f"{refresh.STACK_VAR}={STACK}\n" in updated


def test_refresh_fills_a_blank_account_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / ".env"
    base.write_text(
        "VOCAREUM_API_KEY=key\n"
        "VOCAREUM_COURSE_ID=course\n"
        "VOCAREUM_ASSIGNMENT_ID=assignment\n"
        "VOCAREUM_PART_ID=part\n"
        "VOCAREUM_USER_ID=user\n"
        f"{refresh.ACCOUNT_VAR}=\n"
        f"{refresh.STACK_VAR}=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        refresh, "discover_values", lambda config: refresh.FreshValues(ACCOUNT, STACK)
    )

    code = refresh.main(["--env-file", str(base)])

    assert code == refresh.EXIT_OK
    updated = base.read_text(encoding="utf-8")
    assert f"{refresh.ACCOUNT_VAR}={ACCOUNT}\n" in updated
    assert f"{refresh.STACK_VAR}={STACK}\n" in updated


def test_guarded_tools_still_refuse_a_blank_account_pin(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text(
        "VOCAREUM_API_KEY=key\n"
        "VOCAREUM_COURSE_ID=course\n"
        "VOCAREUM_ASSIGNMENT_ID=assignment\n"
        "VOCAREUM_PART_ID=part\n"
        "VOCAREUM_USER_ID=user\n"
        f"{refresh.ACCOUNT_VAR}=\n",
        encoding="utf-8",
    )

    with pytest.raises(probe.ConfigError, match="No expected AWS account"):
        probe.load_config(base, None, None, True)
