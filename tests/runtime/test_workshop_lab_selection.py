"""Offline tests for the rule both sweepers share.

This module has no client calls in it, so these tests are about meaning rather
than about wiring. What they pin down is the set of things the two sweepers had
already disagreed about once: which tag key casing counts, which S3 codes mean
the bucket is not there, and whether the two ways of matching a log group name
give the same answer.

The last of those is the one worth being explicit about.
`vocareum_tools.sweep` matches a whole log group name against prefixes moved
under the AgentCore path; `workshop_lab.teardown` strips the path off the name
and matches the tail. Both are correct, both suit the code around them, and
neither is going to be rewritten to look like the other. What must not happen is
that they stop meaning the same thing, so that equivalence is asserted here
rather than assumed.
"""

from __future__ import annotations

import pytest

from workshop_lab.naming import Names
from workshop_lab.selection import (
    AGENTCORE_RUNTIME_LOG_PREFIX,
    BUCKET_GONE_CODES,
    DENIAL_SAMPLE,
    WORKSHOP_TAG_KEY,
    WORKSHOP_TAG_VALUE,
    TagReads,
    carries_workshop_tag,
    log_group_prefixes,
    log_group_tail,
    matches_prefix,
    name_prefixes,
    selected_by,
    source_bucket_name,
    stack_owned_keys,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"
PREFIXES = name_prefixes("verify")
TAGGED = {WORKSHOP_TAG_KEY: WORKSHOP_TAG_VALUE}


# --- tag shapes -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {WORKSHOP_TAG_KEY: WORKSHOP_TAG_VALUE},
        [{"Key": WORKSHOP_TAG_KEY, "Value": WORKSHOP_TAG_VALUE}],
        [{"key": WORKSHOP_TAG_KEY, "value": WORKSHOP_TAG_VALUE}],
    ],
    ids=["mapping", "capitalised-pairs", "lowercase-pairs"],
)
def test_every_shape_aws_answers_a_tag_read_with_is_understood(raw: object) -> None:
    """CodeBuild is the lowercase one, and it is not a hypothetical.

    A reader that knew only the capitalised pair saw every CodeBuild project as
    untagged, which is the tag half of selection switching itself off silently.
    """
    assert carries_workshop_tag(raw) is True


@pytest.mark.parametrize(
    "raw",
    [None, {}, [], "", [{"Key": "Other", "Value": "x"}], {"Other": "x"}, 7],
    ids=["none", "empty-map", "empty-list", "string", "other-tag", "other-key", "int"],
)
def test_anything_that_is_not_the_workshop_tag_is_not_the_workshop_tag(
    raw: object,
) -> None:
    """Including shapes that are not tags at all.

    A caller here is deciding whether to delete something, so "the shape
    surprised me" must never read as "it is tagged".
    """
    assert carries_workshop_tag(raw) is False


def test_a_tag_value_that_is_not_the_workshop_value_does_not_count() -> None:
    assert carries_workshop_tag({WORKSHOP_TAG_KEY: "something-else"}) is False


# --- the selection rule ---------------------------------------------------


def test_a_matching_name_alone_selects_a_resource() -> None:
    """Tagging an AgentCore resource is itself a measured permission.

    An account that refuses it produces real workshop resources carrying no tags
    at all, so a tag-only rule walks past them and reports a clean account.
    """
    assert selected_by("verify-gateway", {}, PREFIXES) == "prefix"


def test_the_workshop_tag_alone_selects_a_resource() -> None:
    """The half that survives a rename."""
    assert selected_by("something-else", TAGGED, PREFIXES) == "tag"


def test_both_halves_are_reported_when_both_fire() -> None:
    assert selected_by("verify-gateway", TAGGED, PREFIXES) == "tag+prefix"


def test_neither_half_is_out_of_scope() -> None:
    assert selected_by("voclabs", {}, PREFIXES) == ""


def test_require_prefix_refuses_a_tag_on_its_own() -> None:
    """IAM only. A student account holds Vocareum's own voclabs and vocareum
    roles and whatever service-linked roles AWS put there, and a role selectable
    by tag alone would be deletable the moment anything tagged one."""
    assert selected_by("voclabs", TAGGED, PREFIXES, require_prefix=True) == ""


@pytest.mark.parametrize(
    "name",
    [
        "verify-gateway",
        "verify_runtime",
        "workshop-verify-runtime-role",
        "workshop-verify-bookings",
        "bedrock-agentcore-verify",
        "bedrock-agentcore-verify-builder",
        "hotel-booking-verify-tool",
    ],
)
def test_every_name_one_run_creates_is_matched_by_its_own_prefixes(name: str) -> None:
    """A bare prefix is not enough: three of these carry it in the middle."""
    assert matches_prefix(name, PREFIXES)


def test_the_prefixes_cover_every_name_the_naming_module_builds() -> None:
    """The two are edited in different files, and a name added to one without a
    prefix in the other is a resource nothing ever deletes."""
    names = Names(prefix="verify", account_id=ACCOUNT, region=REGION)
    built = [
        names.role("runtime-role"),
        names.ecr_repository,
        names.codebuild_project,
        names.runtime,
        names.runtime_endpoint,
        names.memory,
        names.gateway,
        names.gateway_target,
        names.tool_function,
        names.secret,
        names.table,
    ]

    assert [one for one in built if not matches_prefix(one, PREFIXES)] == []


def test_a_list_of_prefixes_works_as_well_as_a_tuple() -> None:
    """`str.startswith` takes a tuple and not any iterable, and a caller passing
    a list should get an answer rather than a TypeError at delete-decision
    time."""
    assert matches_prefix("verify-gateway", ["verify"]) is True


# --- log groups -----------------------------------------------------------


@pytest.mark.parametrize(
    "tail",
    [
        "verify_runtime-x-verifyendpoint/runtime-logs",
        "other-runtime-x-endpoint/runtime-logs",
        "workshop-verify-thing",
        "bedrock-agentcore-verify-builder",
        "",
    ],
)
def test_the_two_ways_of_matching_a_log_group_name_agree(tail: str) -> None:
    """The one divergence that had to be kept rather than removed.

    `vocareum_tools.sweep` holds a prefix list and matches whole names;
    `workshop_lab.teardown` holds one name and matches its tail. Each suits the
    code around it. This is what stops them meaning different things.

    The names are built here rather than parametrised whole, because the
    equivalence only holds for a name that is actually under the AgentCore path,
    and that is all either side ever sees: both list with
    `logGroupNamePrefix=AGENTCORE_RUNTIME_LOG_PREFIX` and select from what comes
    back. The test below is the other half of that.
    """
    name = AGENTCORE_RUNTIME_LOG_PREFIX + tail

    assert matches_prefix(log_group_tail(name), PREFIXES) == matches_prefix(
        name, log_group_prefixes(PREFIXES)
    )


def test_the_composed_prefixes_refuse_a_name_not_under_the_path() -> None:
    """The tail form cannot, which is why it is only ever fed a listing.

    `log_group_tail` leaves a name that is not under the path alone, so a caller
    that passed it something else would get the raw name matched against the
    workshop prefixes. The composed form is what carries the path requirement,
    and the listing is what carries it for the tail form.
    """
    assert not matches_prefix("verify-gateway", log_group_prefixes(PREFIXES))
    assert matches_prefix(log_group_tail("verify-gateway"), PREFIXES)


def test_a_log_group_under_the_path_with_a_workshop_name_is_selected() -> None:
    name = f"{AGENTCORE_RUNTIME_LOG_PREFIX}verify_runtime-x-end/runtime-logs"

    assert matches_prefix(log_group_tail(name), PREFIXES)


def test_a_log_group_under_the_path_belonging_to_something_else_is_not() -> None:
    """The path is how these are found at all. It is not how they are chosen."""
    name = f"{AGENTCORE_RUNTIME_LOG_PREFIX}someone-else-x-end/runtime-logs"

    assert not matches_prefix(log_group_tail(name), PREFIXES)


def test_a_name_not_under_the_path_is_left_whole_by_the_tail() -> None:
    assert log_group_tail("/aws/lambda/x") == "/aws/lambda/x"


# --- the build-source bucket ----------------------------------------------


def test_the_source_bucket_name_carries_the_account_and_region() -> None:
    """The account id is half the name and Vocareum rotates the account pool, so
    a remembered name eventually names a bucket in an account the caller is
    guarded against ever touching."""
    assert (
        source_bucket_name(ACCOUNT, REGION)
        == f"bedrock-agentcore-codebuild-sources-{ACCOUNT}-{REGION}"
    )


def test_the_naming_module_builds_the_same_bucket_name() -> None:
    """Two spellings of one name is one sweeper walking past the bucket."""
    names = Names(prefix="verify", account_id=ACCOUNT, region=REGION)

    assert names.source_bucket == source_bucket_name(ACCOUNT, REGION)


def test_all_three_codes_s3_synthesises_for_an_absent_bucket_are_known() -> None:
    """S3 answers a HEAD with a status code and no body, so botocore has no error
    code to report and makes one up. Which of the three arrives depends on the
    operation and the botocore version. The notebook side used to know two."""
    assert sorted(BUCKET_GONE_CODES) == ["404", "NoSuchBucket", "NotFound"]


# --- CloudFormation ownership ---------------------------------------------


def test_a_physical_id_is_recorded_under_both_names_a_list_api_might_use() -> None:
    """A stack that reports a secret by ARN and a list_secrets that reports it by
    name are otherwise two different strings for one resource, and the exclusion
    misses."""
    assert stack_owned_keys("arn:aws:iam::123456789012:role/workshop-lab-runtime") == {
        "arn:aws:iam::123456789012:role/workshop-lab-runtime",
        "workshop-lab-runtime",
    }


def test_a_plain_name_is_recorded_once() -> None:
    assert stack_owned_keys("workshop-lab-runtime") == {"workshop-lab-runtime"}


def test_an_empty_physical_id_contributes_nothing() -> None:
    """An empty string in the exclusion set matches nothing, but it would make
    the count this step prints a lie."""
    assert stack_owned_keys("") == set()


# --- counting the tag reads -------------------------------------------------
# Selection cannot tell a refused tag read from an untagged resource, and both
# sweepers rely on that: the name half decides, so a refusal is not fatal. What
# these pin is that the difference survives as a count, because a run that was
# blind for every resource otherwise reads exactly like a run that found nothing
# tagged, and both read like a clean account.


def test_a_clean_run_of_tag_reads_says_nothing() -> None:
    """The note is for a report. Printing "0 refused" on every sweep trains an
    instructor to skip the line that matters on the one sweep it fires."""
    reads = TagReads()
    reads.answered()
    reads.answered()

    assert reads.denials == 0
    assert reads.note() == ""


def test_a_refusal_is_counted_and_named() -> None:
    reads = TagReads()
    reads.answered()
    reads.refused("log group /aws/bedrock-agentcore/runtimes/verify-x")

    assert (reads.reads, reads.denials) == (2, 1)
    assert "1 of 2" in reads.note()
    assert "verify-x" in reads.note()


def test_the_sample_of_names_stops_but_the_count_does_not() -> None:
    """One refused service and a whole account of them are different findings,
    and the number is what tells them apart. The names are only a hint at which
    service, so they stop early rather than burying the plan under themselves."""
    reads = TagReads()
    for index in range(DENIAL_SAMPLE + 4):
        reads.refused(f"role r{index}")

    assert reads.denials == DENIAL_SAMPLE + 4
    assert len(reads.labels) == DENIAL_SAMPLE
    assert "and more" in reads.note()
