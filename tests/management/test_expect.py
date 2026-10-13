"""Offline tests for the ship gate.

Every AWS call here is a stub. The gate's whole job is to turn a read into one
of five answers, so the tests hand it each answer a real read can give and check
which of the five comes out. Nothing needs a lab session, which is the rule for
everything under tests/.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from vocareum_tools import expect
from vocareum_tools.expect import (
    Expectation,
    Gate,
    ManifestError,
    Presence,
    Verdict,
)


def error(code: str, message: str = "no") -> ClientError:
    """Build a ClientError the way botocore hands one out."""
    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


class FakeClient:
    """A client whose every method is looked up in a dict of answers."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            answer = self.answers[name]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return call


class FakeSession:
    """Hands out a FakeClient per service name."""

    def __init__(self, clients: dict[str, FakeClient]) -> None:
        self.clients = clients

    def client(self, name: str, **kwargs: Any) -> FakeClient:
        return self.clients[name]


def gate(**clients: FakeClient) -> Gate:
    return Gate(FakeSession(clients), "us-east-1")  # type: ignore[arg-type]


def stack(status: str, outputs: dict[str, str] | None = None) -> FakeClient:
    return FakeClient(
        {
            "describe_stacks": {
                "Stacks": [
                    {
                        "StackStatus": status,
                        "Outputs": [
                            {"OutputKey": key, "OutputValue": value}
                            for key, value in (outputs or {}).items()
                        ],
                    }
                ]
            }
        }
    )


def check(subject: Gate, kind: str, name: str) -> expect.Result:
    return subject.check(Expectation(kind=kind, name=name, raw=name))


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def write_manifest(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "expected.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_manifest_that_expects_nothing_is_refused(tmp_path: Path) -> None:
    # It would pass against an empty account, which is the failure a gate exists
    # to catch.
    with pytest.raises(ManifestError):
        expect.read_manifest(write_manifest(tmp_path, {}))
    with pytest.raises(ManifestError):
        expect.read_manifest(write_manifest(tmp_path, {"roles": []}))


def test_an_unknown_kind_is_refused_and_lists_the_known_ones(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, {"rolls": ["voclabs"]})

    with pytest.raises(ManifestError) as raised:
        expect.read_manifest(path)

    assert "rolls" in str(raised.value)
    assert "role_policies" in str(raised.value)


def test_a_missing_manifest_says_to_write_one(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as raised:
        expect.read_manifest(tmp_path / "absent.json")

    assert "No manifest" in str(raised.value)


@pytest.mark.parametrize("payload", [[], {"roles": "voclabs"}, {"roles": [""]}])
def test_an_unusable_shape_is_refused(tmp_path: Path, payload: Any) -> None:
    with pytest.raises(ManifestError):
        expect.read_manifest(write_manifest(tmp_path, payload))


def test_environment_placeholders_resolve_and_outputs_are_left_for_later() -> None:
    values = {"LAB_STACK_NAME": "c123"}
    resolved = expect.resolve_env_placeholders(
        "${LAB_STACK_NAME}/${output:BucketName}", values.get
    )

    # A stack output is only knowable after a read that costs a live session,
    # so it survives this pass untouched.
    assert resolved == "c123/${output:BucketName}"


def test_an_unset_placeholder_fails_before_anything_starts() -> None:
    with pytest.raises(ManifestError) as raised:
        expect.resolve_env_placeholders("${LAB_STACK_NAME}", lambda name: "")

    assert "LAB_STACK_NAME" in str(raised.value)


def test_expectations_keep_the_placeholder_they_came_from() -> None:
    built = expect.build_expectations(
        {"stacks": ["${LAB_STACK_NAME}"]}, {"LAB_STACK_NAME": "c123"}.get
    )

    assert built[0].name == "c123"
    assert built[0].raw == "${LAB_STACK_NAME}"
    assert "from ${LAB_STACK_NAME}" in built[0].label()


def test_every_kind_has_an_action_and_an_absence_code() -> None:
    # The two tables are read by name at lookup time, so a kind added to one
    # and not the other raises KeyError against a live account.
    assert set(expect.KIND_ACTIONS) == set(expect.KIND_ABSENT_CODES)
    assert set(expect.MANIFEST_KEYS) == set(expect.KIND_ACTIONS)
    for kind in expect.MANIFEST_KEYS:
        assert hasattr(Gate, kind)


# ---------------------------------------------------------------------------
# Stacks and outputs
# ---------------------------------------------------------------------------


def test_a_complete_stack_is_present() -> None:
    result = check(gate(cloudformation=stack("CREATE_COMPLETE")), "stacks", "c123")

    assert result.presence is Presence.PRESENT
    assert result.met()


def test_a_deleted_stack_is_not_ready() -> None:
    result = check(gate(cloudformation=stack("DELETE_COMPLETE")), "stacks", "c123")

    # diagnose counts DELETE_COMPLETE as success, correctly, because a clean
    # teardown is a success there. A gate asking whether the lab is up must not.
    assert result.presence is Presence.NOT_READY
    assert not result.met()


def test_an_in_progress_stack_is_not_ready() -> None:
    result = check(gate(cloudformation=stack("CREATE_IN_PROGRESS")), "stacks", "c123")

    assert result.presence is Presence.NOT_READY


def test_a_stack_that_never_existed_is_missing() -> None:
    client = FakeClient({"describe_stacks": error("ValidationError", "does not exist")})

    result = check(gate(cloudformation=client), "stacks", "c123")

    # classify maps this to INCONCLUSIVE_BAD_INPUT, which is right for a probe
    # measuring permission and wrong for a gate asking whether it exists.
    assert result.presence is Presence.MISSING


def test_an_output_with_a_value_is_present() -> None:
    subject = gate(cloudformation=stack("CREATE_COMPLETE", {"BucketName": "b-1"}))
    check(subject, "stacks", "c123")

    assert check(subject, "outputs", "BucketName").presence is Presence.PRESENT


def test_an_empty_output_is_missing() -> None:
    subject = gate(cloudformation=stack("CREATE_COMPLETE", {"BucketName": ""}))
    check(subject, "stacks", "c123")

    # lab.template builds its outputs from GetAtt so a populated value is
    # evidence the service really created the resource.
    assert check(subject, "outputs", "BucketName").presence is Presence.MISSING


def test_an_output_placeholder_resolves_from_the_stack_that_was_read() -> None:
    subject = gate(
        cloudformation=stack("CREATE_COMPLETE", {"BucketName": "lab-bucket"}),
        s3=FakeClient({"head_bucket": {}}),
    )
    check(subject, "stacks", "c123")

    result = check(subject, "buckets", "${output:BucketName}")

    assert result.presence is Presence.PRESENT
    assert result.expectation.name == "lab-bucket"


def test_an_output_placeholder_without_a_stack_is_an_error() -> None:
    subject = gate(s3=FakeClient({"head_bucket": {}}))

    result = check(subject, "buckets", "${output:BucketName}")

    # Nothing else knows which stack to read, so this is the manifest's fault
    # and says so rather than reporting the bucket missing.
    assert result.presence is Presence.ERROR
    assert "stacks" in result.detail


def test_the_stack_is_read_once_however_many_outputs_are_named() -> None:
    client = stack("CREATE_COMPLETE", {"BucketName": "b", "ParameterName": "p"})
    subject = gate(cloudformation=client)
    check(subject, "stacks", "c123")
    check(subject, "outputs", "BucketName")
    check(subject, "outputs", "ParameterName")

    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# The other kinds
# ---------------------------------------------------------------------------


def test_a_role_policy_is_read_by_role_and_policy() -> None:
    client = FakeClient({"get_role_policy": {}})

    result = check(gate(iam=client), "role_policies", "voclabs/LabTemplateS3Grant")

    assert result.presence is Presence.PRESENT
    assert client.calls[0][1] == {
        "RoleName": "voclabs",
        "PolicyName": "LabTemplateS3Grant",
    }


def test_a_role_policy_written_without_a_slash_is_a_manifest_error() -> None:
    result = check(gate(iam=FakeClient({})), "role_policies", "LabTemplateS3Grant")

    assert result.presence is Presence.ERROR
    assert "role/policy" in result.detail


def test_a_missing_inline_policy_is_missing_not_denied() -> None:
    client = FakeClient({"get_role_policy": error("NoSuchEntity")})

    assert (
        check(gate(iam=client), "role_policies", "voclabs/X").presence
        is Presence.MISSING
    )


def test_a_bucket_that_answers_404_is_missing() -> None:
    client = FakeClient({"head_bucket": error("404")})

    # S3 answers a HEAD with a bare 404 and no code, which is why the absence
    # codes are per kind and shared with workshop_lab.selection.
    assert check(gate(s3=client), "buckets", "b").presence is Presence.MISSING


def test_a_log_group_matches_only_on_the_exact_name() -> None:
    client = FakeClient(
        {"describe_log_groups": {"logGroups": [{"logGroupName": "/lab/c123-extra"}]}}
    )

    result = check(gate(logs=client), "log_groups", "/lab/c123")

    # DescribeLogGroups takes a prefix, so a prefix hit would pass the gate on
    # a differently named group that happens to start the same way.
    assert result.presence is Presence.MISSING
    assert client.calls[0][1] == {"logGroupNamePrefix": "/lab/c123"}


def test_a_log_group_that_matches_exactly_is_present() -> None:
    client = FakeClient(
        {"describe_log_groups": {"logGroups": [{"logGroupName": "/lab/c123"}]}}
    )

    assert check(gate(logs=client), "log_groups", "/lab/c123").presence is (
        Presence.PRESENT
    )


def test_a_pending_function_is_not_ready() -> None:
    client = FakeClient({"get_function": {"Configuration": {"State": "Pending"}}})

    assert check(gate(**{"lambda": client}), "functions", "f").presence is (
        Presence.NOT_READY
    )


def test_an_active_function_is_present() -> None:
    client = FakeClient({"get_function": {"Configuration": {"State": "Active"}}})

    assert check(gate(**{"lambda": client}), "functions", "f").presence is (
        Presence.PRESENT
    )


def test_a_parameter_that_is_gone_is_missing() -> None:
    client = FakeClient({"get_parameter": error("ParameterNotFound")})

    assert check(gate(ssm=client), "parameters", "/p").presence is Presence.MISSING


# ---------------------------------------------------------------------------
# Denials and other failures
# ---------------------------------------------------------------------------


def test_an_scp_denial_is_unreadable_and_keeps_the_verdict() -> None:
    denial = error(
        "AccessDenied",
        "User is not authorized to perform: iam:GetRole with an explicit deny "
        "in a service control policy",
    )

    result = check(gate(iam=FakeClient({"get_role": denial})), "roles", "voclabs")

    # Absent and unreadable are different answers. Both fail the gate, but a
    # gate failure that is really a missing grant has to say so.
    assert result.presence is Presence.UNREADABLE
    assert result.verdict is Verdict.SCP_DENY
    assert not result.met()


def test_an_iam_denial_is_unreadable_too() -> None:
    denial = error(
        "AccessDenied", "User is not authorized to perform: iam:GetRole on resource"
    )

    result = check(gate(iam=FakeClient({"get_role": denial})), "roles", "voclabs")

    assert result.presence is Presence.UNREADABLE
    assert result.verdict is Verdict.IAM_DENY


def test_an_unexpected_code_is_an_error_not_a_missing_resource() -> None:
    result = check(
        gate(iam=FakeClient({"get_role": error("ThrottlingException")})),
        "roles",
        "voclabs",
    )

    assert result.presence is Presence.ERROR


def test_a_transport_failure_is_an_error() -> None:
    broken = EndpointConnectionError(endpoint_url="https://iam.amazonaws.com/")

    result = check(gate(iam=FakeClient({"get_role": broken})), "roles", "voclabs")

    assert result.presence is Presence.ERROR


def test_every_failing_presence_has_a_next_action() -> None:
    # A gate that only says no sends the reader back to the stack events to
    # work out which kind of no it was.
    for presence in Presence:
        if presence is not Presence.PRESENT:
            assert expect.NEXT_ACTION[presence]


def test_a_missing_stack_names_rotation_before_stack_events() -> None:
    result = results(("stacks", Presence.MISSING))[0]

    action = expect.next_action(result)

    assert "rotated" in action
    assert "vocareum-refresh" in action
    assert action.index("rotated") < action.index("events")


def test_another_missing_kind_keeps_the_general_action() -> None:
    result = results(("buckets", Presence.MISSING))[0]

    assert expect.next_action(result) == expect.NEXT_ACTION[Presence.MISSING]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def results(*pairs: tuple[str, Presence]) -> list[expect.Result]:
    return [
        expect.Result(Expectation(kind, name, name), presence)
        for kind, presence in pairs
        for name in [f"{kind}-1"]
    ]


def test_report_counts_only_unmet_expectations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    unmet = expect.report(
        results(("roles", Presence.PRESENT), ("buckets", Presence.MISSING)), False
    )

    assert unmet == 1
    assert "1 of 2 expectations not met" in capsys.readouterr().out


def test_report_keeps_kind_specific_missing_actions_apart(
    capsys: pytest.CaptureFixture[str],
) -> None:
    expect.report(
        results(("stacks", Presence.MISSING), ("buckets", Presence.MISSING)),
        False,
    )

    printed = capsys.readouterr().out
    assert "stacks-1: The account pool may have rotated" in printed
    assert "buckets-1: The lab did not create this" in printed


def test_report_says_the_lab_is_up_when_everything_is_met(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert expect.report(results(("roles", Presence.PRESENT)), False) == 0
    assert "the lab is up" in capsys.readouterr().out.lower()


def test_json_output_carries_the_manifest_line_and_the_presence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    expect.report(
        [
            expect.Result(
                Expectation("buckets", "lab-bucket", "${output:BucketName}"),
                Presence.MISSING,
                "404",
            )
        ],
        True,
    )

    payload = json.loads(capsys.readouterr().out.split("\n\n")[-1])
    assert payload[0]["manifest"] == "${output:BucketName}"
    assert payload[0]["presence"] == "MISSING"


# ---------------------------------------------------------------------------
# The command, and the manifest this repository ships
# ---------------------------------------------------------------------------


def test_a_bad_manifest_costs_no_lab_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LAB_AWS_ACCOUNT_ID=123456789012\n", encoding="utf-8")
    write_manifest(tmp_path, {"nonsense": ["x"]})

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the session must not be started")

    monkeypatch.setattr(expect, "open_guarded_session", refuse)

    code = expect.main(["--env-file", str(env_file)])

    # Starting a session spends a launch out of a finite budget, so the
    # manifest is read and interpolated first.
    assert code == expect.EXIT_CONFIG


def test_the_course_manifest_is_preferred_over_the_root_one(tmp_path: Path) -> None:
    directory = tmp_path / "courses" / "graphrag"
    directory.mkdir(parents=True)
    (directory / "expected.json").write_text("{}", encoding="utf-8")
    args = expect.parse_args(["--env-file", str(tmp_path / ".env")])

    chosen = expect.manifest_path(args, "graphrag")

    assert chosen == directory / "expected.json"


def test_a_named_course_never_falls_back_to_the_root_manifest(tmp_path: Path) -> None:
    (tmp_path / "expected.json").write_text("{}", encoding="utf-8")
    args = expect.parse_args(["--env-file", str(tmp_path / ".env")])

    with pytest.raises(expect.ManifestError, match="Refusing to use the root"):
        expect.manifest_path(args, "graphrag")


def test_a_named_course_refuses_another_manifest(tmp_path: Path) -> None:
    courses = tmp_path / "courses" / "graphrag"
    courses.mkdir(parents=True)
    expected = courses / "expected.json"
    expected.write_text("{}", encoding="utf-8")
    wrong = tmp_path / "expected.json"
    wrong.write_text("{}", encoding="utf-8")
    args = expect.parse_args(
        [
            "--env-file",
            str(tmp_path / ".env"),
            "--manifest",
            str(wrong),
        ]
    )

    with pytest.raises(expect.ManifestError, match="can use only"):
        expect.manifest_path(args, "graphrag")


def test_the_root_manifest_is_used_when_no_course_is_selected(tmp_path: Path) -> None:
    args = expect.parse_args(["--env-file", str(tmp_path / ".env")])

    assert expect.manifest_path(args, "").name == expect.DEFAULT_MANIFEST_NAME


def test_the_shipped_manifest_is_loadable_and_resolvable() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = expect.read_manifest(
        root / "courses" / "neo4j-fundamentals" / "expected.json"
    )

    built = expect.build_expectations(manifest, {"LAB_STACK_NAME": "c123"}.get)

    # Every placeholder in the committed manifest resolves from configuration
    # or from a stack output, and every kind is one the gate can look up.
    assert built
    for expectation in built:
        assert expectation.kind in expect.MANIFEST_KEYS
        unresolved = expect.PLACEHOLDER.findall(expectation.name)
        assert all(name.startswith(expect.OUTPUT_PREFIX) for name in unresolved)


def test_the_shipped_manifest_names_no_read_the_session_role_cannot_make() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = expect.read_manifest(
        root / "courses" / "neo4j-fundamentals" / "expected.json"
    )

    # Measured 2026-08-23 against a live session: iam:GetRole and
    # iam:GetRolePolicy on voclabs are an explicit deny in Vocareum's own
    # identity-based policy on the session role, so a manifest line naming them
    # could never pass. The nine grants are covered transitively instead: the
    # stack cannot reach CREATE_COMPLETE without CloudFormation having created
    # all nine AWS::IAM::RolePolicy resources.
    assert "roles" not in manifest
    assert "role_policies" not in manifest
    assert manifest["stacks"]


def test_the_shipped_manifest_names_outputs_lab_template_really_declares() -> None:
    root = Path(__file__).resolve().parents[2]
    directory = root / "courses" / "neo4j-fundamentals"
    manifest = expect.read_manifest(directory / "expected.json")
    template = (directory / "lab.template").read_text(encoding="utf-8")

    # An output key the template does not declare is a gate line that fails
    # forever, and the failure reads as a broken lab rather than a typo.
    for key in manifest["outputs"]:
        assert f"\n  {key}:" in template
