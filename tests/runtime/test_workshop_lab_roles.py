"""Drive `workshop_lab.roles` with a fake IAM client, offline.

The point of most of these is not that the code runs. It is that the calls it
makes are the same calls the notebook cell made before it was a module, down to
the role names and the order. A refactor that quietly renames a role costs every
role its policy, because `lab.template` grants only `role/workshop-*`.
"""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from workshop_lab.harness import FAIL, PASS, Harness
from workshop_lab.naming import Names
from workshop_lab.roles import (
    AGENTCORE_PRINCIPAL,
    ROLE_SPECS,
    Roles,
    allow,
    trust,
)


class FakeCredentials:
    access_key, secret_key, token = "AKIA", "secret", "token"

    def get_frozen_credentials(self):
        return self


class FakeIam:
    """Records every call, and refuses whichever operations it was told to."""

    def __init__(self, refuse: set[str] = frozenset()) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.refuse = set(refuse)
        self.policies: dict[str, list[str]] = {}

    def _maybe_refuse(self, operation: str) -> None:
        if operation in self.refuse:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "explicit deny"}},
                operation,
            )

    def create_role(self, **kwargs):
        self.calls.append(("create_role", kwargs))
        self._maybe_refuse("create_role")
        name = kwargs["RoleName"]
        self.policies.setdefault(name, [])
        return {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{name}"}}

    def put_role_policy(self, **kwargs):
        self.calls.append(("put_role_policy", kwargs))
        self._maybe_refuse("put_role_policy")
        self.policies.setdefault(kwargs["RoleName"], []).append(kwargs["PolicyName"])
        return {}

    def list_role_policies(self, **kwargs):
        self.calls.append(("list_role_policies", kwargs))
        return {"PolicyNames": list(self.policies.get(kwargs["RoleName"], []))}

    def delete_role_policy(self, **kwargs):
        self.calls.append(("delete_role_policy", kwargs))
        self.policies[kwargs["RoleName"]].remove(kwargs["PolicyName"])
        return {}

    def delete_role(self, **kwargs):
        self.calls.append(("delete_role", kwargs))
        if self.policies.get(kwargs["RoleName"]):
            raise ClientError(
                {"Error": {"Code": "DeleteConflict", "Message": "still has policies"}},
                "DeleteRole",
            )
        self.policies.pop(kwargs["RoleName"], None)
        return {}


class FakeSession:
    def __init__(self, iam: FakeIam) -> None:
        self.iam, self.region_name = iam, "us-east-1"

    def get_credentials(self):
        return FakeCredentials()

    def client(self, name: str, region_name: str | None = None):
        return self.iam if name == "iam" else object()


ACCOUNT = "123456789012"


def lab_with(iam: FakeIam) -> Harness:
    """A harness past step 2, without the STS call that normally gets it there."""
    lab = Harness(prefix="verify", session=FakeSession(iam), echo=lambda _: None)
    lab.account_id = ACCOUNT
    lab._names = Names(prefix="verify", account_id=ACCOUNT, region="us-east-1")
    return lab


# --- the names, which are the part that cannot drift -------------------------
def test_the_four_role_names_match_what_the_notebook_created():
    """These exact strings were in the notebook cell before it was a module.

    `lab.template` scopes iam:PutRolePolicy to `role/workshop-*`. A rename that
    drops the prefix costs every role its policy and reports it as a denial.
    """
    iam = FakeIam()
    lab = lab_with(iam)
    Roles(lab).create_all()

    created = [
        kwargs["RoleName"] for name, kwargs in iam.calls if name == "create_role"
    ]
    assert created == [
        "workshop-verify-codebuild-role",
        "workshop-verify-runtime-role",
        "workshop-verify-lambda-role",
        "workshop-verify-gateway-role",
    ]


def test_every_role_name_carries_the_granted_prefix():
    iam = FakeIam()
    lab = lab_with(iam)
    for spec in ROLE_SPECS:
        assert lab.names.role(spec.suffix).startswith("workshop-")


def test_the_agentcore_roles_trust_the_agentcore_principal():
    """Runtime and Gateway are both fronted by one service principal."""
    by_suffix = {spec.suffix: spec for spec in ROLE_SPECS}
    assert by_suffix["runtime-role"].principal == AGENTCORE_PRINCIPAL
    assert by_suffix["gateway-role"].principal == AGENTCORE_PRINCIPAL
    assert by_suffix["codebuild-role"].principal == "codebuild.amazonaws.com"
    assert by_suffix["lambda-role"].principal == "lambda.amazonaws.com"


def test_codebuild_can_complete_a_push_not_only_start_one():
    """A push is five calls. Authorizing three of them fails halfway."""
    codebuild = next(s for s in ROLE_SPECS if s.suffix == "codebuild-role")
    for action in (
        "ecr:GetAuthorizationToken",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
    ):
        assert action in codebuild.actions


# --- policy documents ---------------------------------------------------------
def test_allow_builds_one_statement_over_every_action():
    document = json.loads(allow(["s3:GetObject", "s3:PutObject"]))
    assert document["Statement"] == [
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": "*",
        }
    ]


def test_trust_names_the_service_that_may_assume_the_role():
    document = json.loads(trust("lambda.amazonaws.com"))
    statement = document["Statement"][0]
    assert statement["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole"


def test_roles_are_tagged_so_the_cleanup_can_find_them():
    iam = FakeIam()
    lab = lab_with(iam)
    Roles(lab).create_all()
    for name, kwargs in iam.calls:
        if name == "create_role":
            assert kwargs["Tags"] == lab.names.tags_list


# --- what happens when the account refuses ------------------------------------
def test_a_refused_create_yields_no_arn_and_registers_no_cleanup():
    iam = FakeIam(refuse={"create_role"})
    lab = lab_with(iam)
    arns = Roles(lab).create_all()

    assert set(arns.values()) == {None}
    assert lab.cleanups == []
    assert all(verdict == FAIL for _, verdict, _ in lab.results)


def test_a_refused_policy_still_registers_the_cleanup():
    """The role exists. Not deleting it is how a role gets left in the account.

    PutRolePolicy is the call most likely to be refused here, so registering the
    delete only after it succeeded would leak on exactly the expected failure.
    """
    iam = FakeIam(refuse={"put_role_policy"})
    lab = lab_with(iam)
    arns = Roles(lab).create_all()

    assert set(arns.values()) == {None}
    assert len(lab.cleanups) == len(ROLE_SPECS)


def test_a_refused_policy_reports_no_usable_arn():
    """A role without its policy cannot act, so returning its ARN moves the
    failure into a later step that has nothing to do with it."""
    iam = FakeIam(refuse={"put_role_policy"})
    roles = Roles(lab_with(iam))
    assert roles.create(ROLE_SPECS[0]) is None
    assert roles.all_created is False


def test_all_created_is_true_only_when_every_role_has_its_policy():
    roles = Roles(lab_with(FakeIam()))
    assert roles.all_created is False  # nothing created yet
    roles.create_all()
    assert roles.all_created is True


# --- cleanup ------------------------------------------------------------------
def test_cleanup_empties_the_role_before_deleting_it():
    """IAM answers DeleteConflict on a role that still holds an inline policy."""
    iam = FakeIam()
    lab = lab_with(iam)
    Roles(lab).create_all()

    for _, delete in lab.cleanups:
        delete()  # FakeIam raises DeleteConflict if the order is wrong

    deleted = [k["RoleName"] for n, k in iam.calls if n == "delete_role"]
    assert len(deleted) == len(ROLE_SPECS)
    assert iam.policies == {}


def test_cleanup_is_registered_under_a_name_that_says_what_it_is():
    lab = lab_with(FakeIam())
    Roles(lab).create_all()
    assert [label for label, _ in lab.cleanups] == [
        f"iam role workshop-verify-{spec.suffix}" for spec in ROLE_SPECS
    ]


# --- all_passed, which step 5 gates on ----------------------------------------
def test_all_passed_is_true_when_every_matching_check_passed():
    lab = lab_with(FakeIam())
    lab.record("agentcore:ListGateways", PASS)
    lab.record("agentcore:ListMemories", PASS)
    lab.record("iam:CreateRole x", FAIL, "denied")
    assert lab.all_passed("agentcore:") is True


def test_all_passed_is_false_when_one_matching_check_failed():
    lab = lab_with(FakeIam())
    lab.record("agentcore:ListGateways", PASS)
    lab.record("agentcore:ListMemories", FAIL, "denied")
    assert lab.all_passed("agentcore:") is False


def test_all_passed_is_false_when_nothing_matched():
    """An empty all() is True, and would pass a step nothing was measured on."""
    lab = lab_with(FakeIam())
    assert lab.all_passed("agentcore:") is False


@pytest.mark.parametrize("suffix", [spec.suffix for spec in ROLE_SPECS])
def test_every_spec_grants_at_least_one_action(suffix):
    spec = next(s for s in ROLE_SPECS if s.suffix == suffix)
    assert spec.actions
    assert json.loads(spec.policy)["Statement"][0]["Action"] == list(spec.actions)
