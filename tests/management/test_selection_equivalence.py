"""Hold the notebook's lazy selection and the shared rule to the same answers.

There are two implementations of the one decision that matters. `sweep` asks
`selection.selected_by` with a name and a tag map it has already read.
`workshop_lab.teardown.Teardown.ours` asks the same two questions in the same
order and stops at the first yes, because on that side a tag read is a call per
resource against a service that may refuse it, and a name that has already
matched makes the call pointless.

Merging them was considered and rejected. `ours` is lazy, takes a callable, and
answers a bool; `selected_by` is eager, takes a mapping, and answers the reason
string the instructor's report prints. More to the point, `collect_roles` passes
no `read_tags` at all on purpose, and a mechanical rewrite that handed roles a
tag map would reintroduce the tag-alone IAM hazard `require_prefix` exists to
block. So the two stay separate and this file carries the obligation instead:
one case table, both callers, same verdict.

Nothing here calls AWS. `Teardown` is constructed against a harness with no
clients, and no case reaches a client because `ours` decides from its arguments.
"""

from __future__ import annotations

from typing import Any

import pytest

from workshop_lab.harness import Harness
from workshop_lab.naming import Names
from workshop_lab.selection import (
    WORKSHOP_TAG_KEY,
    WORKSHOP_TAG_VALUE,
    name_prefixes,
    selected_by,
)
from workshop_lab.teardown import Teardown

ACCOUNT = "123456789012"
REGION = "us-east-1"
PREFIX = "verify"
PREFIXES = name_prefixes(PREFIX)

TAGGED = {WORKSHOP_TAG_KEY: WORKSHOP_TAG_VALUE}
OTHER_TAG = {WORKSHOP_TAG_KEY: "some-other-workshop"}
NO_TAGS: dict[str, str] = {}

# One table, both callers. Every row is a name shape that has actually turned up
# in an account this workshop ran in, or a near miss of one: the three names that
# carry the prefix in the middle, Vocareum's own roles, an AWS service-linked
# role, the AgentCore log path, and the build-source bucket that carries neither
# a workshop name nor, on an account that refuses the tagging call, a tag.
CASES: list[tuple[str, dict[str, str]]] = [
    ("verify-runtime", NO_TAGS),
    ("verify_runtime", NO_TAGS),
    ("workshop-verify-runtime-role", NO_TAGS),
    ("bedrock-agentcore-verify", NO_TAGS),
    ("hotel-booking-verify-tool", NO_TAGS),
    ("workshop-verify-bookings", TAGGED),
    ("wsverify-gateway", NO_TAGS),
    ("wsverify-gateway", TAGGED),
    ("unrelated-tenant-app", TAGGED),
    ("unrelated-tenant-app", NO_TAGS),
    ("unrelated-tenant-app", OTHER_TAG),
    ("voclabs", NO_TAGS),
    ("vocareum", TAGGED),
    ("AWSServiceRoleForBedrockAgentCoreRuntimeIdentity", NO_TAGS),
    ("/aws/bedrock-agentcore/runtimes/verify-abc-DEFAULT/runtime-logs", NO_TAGS),
    ("/aws/lambda/hotel-booking-verify-tool", NO_TAGS),
    (f"bedrock-agentcore-codebuild-sources-{ACCOUNT}-{REGION}", TAGGED),
    ("", NO_TAGS),
    ("", TAGGED),
]


@pytest.fixture
def teardown() -> Teardown:
    """A `Teardown` with a prefix and nothing else. No case reaches a client."""

    def no_client(_: str) -> Any:
        raise AssertionError("selection must not need a client")

    lab = Harness(prefix=PREFIX, session=None, echo=lambda _: None)
    lab.account_id = ACCOUNT
    lab._names = Names(prefix=PREFIX, account_id=ACCOUNT, region=REGION)
    lab.client = no_client  # type: ignore[method-assign]
    return Teardown(lab, recording=False)


@pytest.mark.parametrize(("name", "tags"), CASES)
def test_the_two_selections_agree(
    teardown: Teardown, name: str, tags: dict[str, str]
) -> None:
    """The union rule, asked both ways.

    `ours` returns a bool and `selected_by` a reason string, so the comparison
    is against the truthiness of the reason. That is the whole contract between
    them: which resources, not how the answer is phrased.
    """
    assert teardown.ours(name, "arn:x", lambda: tags) is bool(
        selected_by(name, tags, PREFIXES)
    )


@pytest.mark.parametrize(("name", "tags"), CASES)
def test_a_role_agrees_with_the_prefix_required_form(
    teardown: Teardown, name: str, tags: dict[str, str]
) -> None:
    """Passing no `read_tags` is how the notebook says `require_prefix=True`.

    `collect_roles` calls `ours` with a name and nothing else, which makes the
    tag half unreachable for IAM. `sweep` spells the same restriction as a flag.
    A student account holds Vocareum's own roles and AWS's service-linked ones,
    and a role selectable by tag alone would be deletable the moment anything
    tagged one, so the two spellings have to keep meaning the same thing.
    """
    assert teardown.ours(name) is bool(
        selected_by(name, tags, PREFIXES, require_prefix=True)
    )


def test_a_tagged_role_is_out_of_scope_in_both_spellings(teardown: Teardown) -> None:
    """The case the parametrisation above would pass on by accident if `ours`
    ever started reading tags for roles."""
    assert teardown.ours("vocareum") is False
    assert selected_by("vocareum", TAGGED, PREFIXES, require_prefix=True) == ""


def test_the_case_table_exercises_both_halves_and_neither(
    teardown: Teardown,
) -> None:
    """A table of nothing but misses would make every assertion above vacuous."""
    reasons = {selected_by(name, tags, PREFIXES) for name, tags in CASES}
    assert {"prefix", "tag", "tag+prefix", ""} <= reasons
