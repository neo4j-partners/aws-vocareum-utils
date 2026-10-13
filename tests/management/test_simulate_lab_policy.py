"""Offline checks on the lab.template policy simulator.

Nothing here calls AWS. The half of ``scripts/simulate_lab_policy.py`` that
needs IAM is the single ``simulate_custom_policy`` call; everything the tool
could get wrong on its own happens before that call and after it. Those are what
these tests hold.

Three properties matter enough to pin:

* **The template parser never drops a statement.** A dropped grant reads in the
  report exactly like a grant the template never had, which is a false gap, and
  a false gap sends somebody editing a file that was already correct. Every
  malformed shape must raise instead.
* **``!Sub`` resolves to the same string CloudFormation would produce.** An ARN
  that is off by an account id is an ARN that matches nothing, and the answer
  would still look like a considered one.
* **The verdict comparison is exact.** The exit code gates an upload, so an
  ``allowed`` that compares equal to an ``implicitDeny`` would gate nothing.

The script carries PEP 723 inline metadata so it can run standalone under
``uv run``, which means it is not importable as a package module. It is loaded
here by path, the same way it would be run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from vocareum_tools.config import repository_root

SCRIPT = repository_root() / "scripts" / "simulate_lab_policy.py"


def load_script() -> ModuleType:
    """Import the script by path, without running it."""
    spec = importlib.util.spec_from_file_location("simulate_lab_policy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sim = load_script()


ACCOUNT = "000000000000"
REGION = "us-east-1"


FIXTURE = """
AWSTemplateFormatVersion: "2010-09-09"

Parameters:
  ResourcePrefix:
    Type: String
    Default: fixture-lab

Resources:
  NotAPolicy:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Select [0, !Split ["-", "a-b"]]

  FirstGrant:
    Type: AWS::IAM::RolePolicy
    Properties:
      RoleName: voclabs
      PolicyName: FixtureFirst
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Sid: Wide
            Effect: Allow
            Action:
              - sts:GetCallerIdentity
            Resource: "*"
          - Sid: Scoped
            Effect: Allow
            Action:
              - dynamodb:PutItem
            Resource: !Sub "arn:aws:dynamodb:${AWS::Region}:${AWS::AccountId}:t/w-*"

  SecondGrant:
    Type: AWS::IAM::RolePolicy
    Properties:
      RoleName: voclabs
      PolicyName: FixtureSecond
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Sid: Named
            Effect: Allow
            Action:
              - s3:PutObject
            Resource: !Sub
              - "arn:aws:s3:::${Bucket}/*"
              - Bucket: fixture-bucket
"""


def parse(text: str) -> list:
    """Parse a template string straight through to its grants."""
    template = sim.yaml.load(text, Loader=sim.CloudFormationLoader)
    return sim.extract_grants(template, ACCOUNT, REGION)


def test_extract_grants_reads_every_statement_and_skips_other_resources() -> None:
    """Both RolePolicy resources contribute, the bucket contributes nothing."""
    grants = parse(FIXTURE)
    assert [grant.label for grant in grants] == [
        "FirstGrant/Wide",
        "FirstGrant/Scoped",
        "SecondGrant/Named",
    ]
    assert [grant.policy_name for grant in grants] == [
        "FixtureFirst",
        "FixtureFirst",
        "FixtureSecond",
    ]


def test_sub_resolves_pseudo_parameters() -> None:
    """${AWS::Region} and ${AWS::AccountId} become the supplied values."""
    scoped = parse(FIXTURE)[1]
    assert scoped.statement["Resource"] == f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:t/w-*"


def test_sub_resolves_the_list_form() -> None:
    """The [string, mapping] form substitutes from the mapping."""
    named = parse(FIXTURE)[2]
    assert named.statement["Resource"] == "arn:aws:s3:::fixture-bucket/*"


def test_sub_resolves_a_template_parameter_default() -> None:
    """Vocareum passes no parameter values, so a default is the real value."""
    context = sim.substitution_context(
        ACCOUNT, REGION, sim.yaml.load(FIXTURE, Loader=sim.CloudFormationLoader)
    )
    assert context["ResourcePrefix"] == "fixture-lab"


def test_sub_keeps_an_escaped_iam_policy_variable() -> None:
    """${!aws:PrincipalAccount} survives as a literal ${aws:PrincipalAccount}."""
    resolved = sim.resolve_sub(
        "arn:aws:s3:::bucket-${!aws:PrincipalAccount}-${AWS::Region}",
        {"AWS::Region": REGION},
        "where",
    )
    assert resolved == "arn:aws:s3:::bucket-${aws:PrincipalAccount}-us-east-1"


def test_sub_raises_on_an_unknown_variable() -> None:
    """An unresolved ${...} is an error, never an ARN with a hole in it."""
    with pytest.raises(sim.TemplateError, match=r"\$\{Nope\}"):
        sim.resolve_sub("arn:aws:s3:::${Nope}", {}, "FirstGrant.Statement[0]")


def test_unresolvable_intrinsic_in_a_policy_raises() -> None:
    """!GetAtt inside a PolicyDocument fails loudly rather than being dropped."""
    text = FIXTURE.replace(
        '            Resource: "*"',
        "            Resource: !GetAtt NotAPolicy.Arn",
        1,
    )
    with pytest.raises(sim.TemplateError, match="GetAtt"):
        parse(text)


def test_missing_policy_document_raises() -> None:
    """A RolePolicy with no PolicyDocument is a parse failure, not zero grants."""
    text = FIXTURE.replace("      PolicyDocument:", "      NotPolicyDocument:", 1)
    with pytest.raises(sim.TemplateError, match="PolicyDocument"):
        parse(text)


BARE_STATEMENT = """
Resources:
  OnlyGrant:
    Type: AWS::IAM::RolePolicy
    Properties:
      RoleName: voclabs
      PolicyName: Bare
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          Sid: Wide
          Effect: Allow
          Action: sts:GetCallerIdentity
          Resource: "*"
"""


def test_statement_that_is_not_a_list_raises() -> None:
    """IAM allows a bare statement object. This tool refuses to guess at one."""
    with pytest.raises(sim.TemplateError, match="Statement is not a list"):
        parse(BARE_STATEMENT)


def test_template_with_no_role_policy_raises() -> None:
    """Simulating nothing would report every case denied, which is a lie."""
    text = FIXTURE.replace("AWS::IAM::RolePolicy", "AWS::IAM::ManagedPolicy")
    with pytest.raises(sim.TemplateError, match="no AWS::IAM::RolePolicy"):
        parse(text)


def test_statement_without_a_sid_gets_a_positional_label() -> None:
    """A Sid is optional in IAM, so the report falls back to the index."""
    text = FIXTURE.replace(
        "          - Sid: Wide\n            Effect: Allow",
        "          - Effect: Allow",
        1,
    )
    grants = parse(text)
    assert grants[0].label == "FirstGrant/Statement0"


def test_each_statement_becomes_its_own_policy_document() -> None:
    """One statement per document is what makes SourcePolicyId a Sid."""
    inputs = sim.build_policy_inputs(parse(FIXTURE))
    assert len(inputs) == 3
    for item in inputs:
        document = json.loads(item.document)
        assert document["Version"] == "2012-10-17"
        assert document["Statement"] == [item.grant.statement]


def test_too_many_statements_raises_instead_of_truncating() -> None:
    """Outgrowing the ceiling must fail, not send a partial set of grants."""
    grants = parse(FIXTURE) * (sim.MAX_POLICY_DOCUMENTS + 1)
    with pytest.raises(sim.TemplateError, match="statements"):
        sim.build_policy_inputs(grants)


def test_matched_labels_map_source_policy_id_back_to_a_sid() -> None:
    """PolicyInputList.N is 1-based and counts documents in the order sent."""
    inputs = sim.build_policy_inputs(parse(FIXTURE))
    labels = sim.matched_labels(inputs, [{"SourcePolicyId": "PolicyInputList.2"}])
    assert labels == ("FirstGrant/Scoped",)


def test_matched_labels_deduplicate_and_survive_an_unknown_source() -> None:
    """An id the tool did not send is reported verbatim, not silently dropped."""
    inputs = sim.build_policy_inputs(parse(FIXTURE))
    labels = sim.matched_labels(
        inputs,
        [
            {"SourcePolicyId": "PolicyInputList.1"},
            {"SourcePolicyId": "PolicyInputList.1"},
            {"SourcePolicyId": "SomethingElse"},
        ],
    )
    assert labels == ("FirstGrant/Wide", "SomethingElse")


def test_result_ok_is_an_exact_comparison() -> None:
    """The exit code gates an upload, so near enough is not enough."""
    case = sim.Case("s3:PutObject", "arn:aws:s3:::b/k", sim.ALLOWED, "step7")
    assert sim.Result(case=case, actual=sim.ALLOWED).ok
    assert not sim.Result(case=case, actual=sim.IMPLICIT_DENY).ok
    assert not sim.Result(case=case, actual=sim.EXPLICIT_DENY).ok
    assert not sim.Result(case=case, actual="error: no result returned").ok


def test_an_explicit_deny_where_an_implicit_one_was_expected_is_a_mismatch() -> None:
    """The two denials are not interchangeable. Only one can be lifted here."""
    role = "arn:aws:iam::x:role/z"
    case = sim.Case("iam:PutRolePolicy", role, sim.IMPLICIT_DENY, "step6")
    assert not sim.Result(case=case, actual=sim.EXPLICIT_DENY).ok


def test_print_summary_returns_one_when_anything_mismatched(capsys) -> None:
    """Exit code 1 is the gate. It has to follow from the results, not a flag."""
    ok = sim.Case("s3:GetObject", "arn:aws:s3:::b/k", sim.ALLOWED, "step7")
    bad = sim.Case("ecr:TagResource", "arn:aws:ecr:r:a:repository/x", sim.ALLOWED, "s7")
    assert sim.print_summary([sim.Result(case=ok, actual=sim.ALLOWED)]) == 0
    assert (
        sim.print_summary(
            [
                sim.Result(case=ok, actual=sim.ALLOWED),
                sim.Result(case=bad, actual=sim.IMPLICIT_DENY),
            ]
        )
        == 1
    )
    assert "ecr:TagResource" in capsys.readouterr().out


def test_cases_carry_the_supplied_account_prefix_and_region() -> None:
    """Every ARN in the table is built from the same three inputs."""
    cases = sim.build_cases(ACCOUNT, REGION, "verify")
    assert cases
    for case in cases:
        assert case.expected in {sim.ALLOWED, sim.IMPLICIT_DENY, sim.EXPLICIT_DENY}
        if case.resource != "*":
            assert case.resource.startswith("arn:aws:")
    resources = {case.resource for case in cases}
    assert (
        f"arn:aws:s3:::bedrock-agentcore-codebuild-sources-{ACCOUNT}-{REGION}"
        "/verify/source.zip" in resources
    )
    assert f"arn:aws:iam::{ACCOUNT}:role/workshop-verify-codebuild-role" in resources
    assert f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/workshop-verify-bookings" in (
        resources
    )
    assert (
        "arn:aws:bedrock:"
        f"{REGION}::foundation-model/amazon.nova-2-multimodal-embeddings-v1:0"
        in resources
    )
    assert (
        f"arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0"
        in resources
    )


def test_cases_expect_both_decisions() -> None:
    """A harness that only ever expects a denial has not been shown to allow."""
    expected = {case.expected for case in sim.build_cases(ACCOUNT, REGION, "verify")}
    assert expected == {sim.ALLOWED, sim.IMPLICIT_DENY}


def test_the_prefix_argument_reaches_every_derived_name() -> None:
    """--prefix has to move the resource names, or the table describes nothing."""
    cases = sim.build_cases(ACCOUNT, REGION, "zzz")
    assert not any("verify" in case.resource for case in cases)
    assert any("workshop-zzz-codebuild-role" in case.resource for case in cases)


def test_group_by_resource_keeps_the_written_order() -> None:
    """One call per resource, in the order the notebook makes them."""
    cases = [
        sim.Case("a:One", "res-1", sim.ALLOWED, "s"),
        sim.Case("b:Two", "res-2", sim.ALLOWED, "s"),
        sim.Case("a:Three", "res-1", sim.ALLOWED, "s"),
    ]
    grouped = sim.group_by_resource(cases)
    assert list(grouped) == ["res-1", "res-2"]
    assert [case.action for case in grouped["res-1"]] == ["a:One", "a:Three"]


def test_dotenv_value_prefers_the_real_environment(tmp_path, monkeypatch) -> None:
    """Same precedence as Environment.get: a real variable always wins."""
    env_file = tmp_path / ".env"
    env_file.write_text('LAB_AWS_ACCOUNT_ID="111111111111"\n', encoding="utf-8")
    monkeypatch.delenv("LAB_AWS_ACCOUNT_ID", raising=False)
    assert sim.dotenv_value(env_file, "LAB_AWS_ACCOUNT_ID") == "111111111111"
    monkeypatch.setenv("LAB_AWS_ACCOUNT_ID", "222222222222")
    assert sim.dotenv_value(env_file, "LAB_AWS_ACCOUNT_ID") == "222222222222"


def test_dotenv_value_returns_empty_for_a_missing_file() -> None:
    """A missing .env is not an error here. --account-id still covers it."""
    assert sim.dotenv_value(Path("/nonexistent/.env"), "LAB_AWS_ACCOUNT_ID") == ""


def test_the_caveat_names_both_directions(capsys) -> None:
    """The limits belong in the output, not only in a docstring nobody opens."""
    sim.print_caveat()
    printed = capsys.readouterr().out
    assert "Pvoclabs" in printed
    assert "SCP" in printed
    assert sim.ALLOWED in printed
    assert sim.IMPLICIT_DENY in printed
    assert "Only a live lab session settles either direction." in printed


def test_the_real_template_parses_and_every_statement_survives() -> None:
    """lab.template itself, since a template that stops parsing is the failure."""
    template = sim.load_template(
        repository_root() / "courses" / "neo4j-fundamentals" / "lab.template"
    )
    grants = sim.extract_grants(template, ACCOUNT, REGION)
    assert len(grants) >= 9
    for grant in grants:
        assert isinstance(grant.statement.get("Action"), (str, list))
        assert "${" not in json.dumps(grant.statement)
    inputs = sim.build_policy_inputs(grants)
    assert len(inputs) == len(grants)


def test_only_the_student_role_is_simulated() -> None:
    """A grant on another principal must not read as a grant to the student.

    ``lab.template`` carries the account sweep's execution role policy. Folding it
    in would not add a novel action, since the student role already holds
    ``logs:DeleteLogGroup`` under its own ARNs, and that is exactly why the
    over-report would be hard to spot: it widens a resource scope and adds
    ``cloudformation:ListStackResources`` rather than announcing itself as a new
    permission. This tool's only output is authorization answers, so it must
    simulate one principal.
    """
    template = sim.load_template(
        repository_root() / "courses" / "neo4j-fundamentals" / "lab.template"
    )
    grants = sim.extract_grants(template, ACCOUNT, REGION)

    policies = {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::IAM::RolePolicy"
    }
    other = {
        logical_id
        for logical_id, resource in policies.items()
        if resource["Properties"].get("RoleName") != sim.SIMULATED_ROLE
    }
    assert other, "the template no longer has a policy on another role to exclude"

    simulated = {grant.logical_id for grant in grants}
    assert simulated.isdisjoint(other)
    assert simulated == set(policies) - other

    sids = [grant.sid for grant in grants]
    assert not [sid for sid in sids if sid.startswith("Sweep")]

    resources = json.dumps([grant.statement.get("Resource") for grant in grants])
    assert "cloudformation:ListStackResources" not in json.dumps(sids)
    assert ":stack/" not in resources
