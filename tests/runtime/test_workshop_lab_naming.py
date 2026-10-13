"""Hold `Names` to the exact strings the notebook cells used to build inline.

These names are not internal. A student's ECR repository, S3 key, and Lambda are
found by them, `lab.template` grants against them, and the sweep script deletes
by them. A refactor that changes one by a character produces an account that
looks clean and is not, so the expected values below are written out literally
rather than derived, and are copied from the pre-refactor notebook cells.
"""

from __future__ import annotations

import pytest

from workshop_lab.naming import PREBUILT_IMAGE, Names
from workshop_lab.selection import WORKSHOP_TAG_KEY, WORKSHOP_TAG_VALUE

ACCOUNT = "123456789012"
REGION = "us-east-1"


@pytest.fixture
def names() -> Names:
    return Names(prefix="verify", account_id=ACCOUNT, region=REGION)


@pytest.mark.parametrize(
    "attribute, expected",
    [
        ("source_bucket", f"bedrock-agentcore-codebuild-sources-{ACCOUNT}-{REGION}"),
        ("source_key", "verify/source.zip"),
        ("ecr_repository", "bedrock-agentcore-verify"),
        ("registry", f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"),
        (
            "image_uri",
            (
                f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
                "/bedrock-agentcore-verify:verify"
            ),
        ),
        ("codebuild_project", "bedrock-agentcore-verify-builder"),
        ("tool_function", "hotel-booking-verify-tool"),
        ("runtime", "verify_runtime"),
        ("runtime_endpoint", "verifyendpoint"),
        ("memory", "verify_memory"),
        ("gateway", "verify-gateway"),
        ("gateway_target", "verify-target"),
        ("secret", "verify/neo4j-command"),
        ("table", "workshop-verify-bookings"),
    ],
)
def test_name_matches_the_notebook_original(names, attribute, expected):
    assert getattr(names, attribute) == expected


@pytest.mark.parametrize(
    "suffix, expected",
    [
        ("codebuild-role", "workshop-verify-codebuild-role"),
        ("runtime-role", "workshop-verify-runtime-role"),
        ("lambda-role", "workshop-verify-lambda-role"),
        ("gateway-role", "workshop-verify-gateway-role"),
    ],
)
def test_role_name_matches_the_notebook_original(names, suffix, expected):
    assert names.role(suffix) == expected


def test_the_runtime_name_uses_underscores():
    """AgentCore rejects a hyphen in a runtime name, and the message says only
    that the name is invalid."""
    assert "-" not in Names("verify", ACCOUNT, REGION).runtime


def test_the_prebuilt_image_is_public_and_the_destination_is_not(names):
    """AgentCore Runtime pulls only from ECR in the same account.

    Step 8 copies from the public source into the private destination, so these
    two must never converge on one registry.
    """
    assert PREBUILT_IMAGE.startswith("public.ecr.aws/")
    assert names.image_uri.startswith(f"{ACCOUNT}.dkr.ecr.")


def test_tags_carry_the_key_the_cleanup_deletes_by(names):
    assert names.tags_map == {WORKSHOP_TAG_KEY: WORKSHOP_TAG_VALUE}
    assert names.tags_list == [{"Key": WORKSHOP_TAG_KEY, "Value": WORKSHOP_TAG_VALUE}]


@pytest.mark.parametrize("prefix", ["ws", "wsverify", "wsx"])
def test_a_ws_prefix_is_refused(prefix):
    """InvokeAgentRuntime routes `ws*` to the WebSocket handler and the call
    never reaches the container."""
    with pytest.raises(ValueError, match="WebSocket"):
        Names(prefix=prefix, account_id=ACCOUNT, region=REGION)


@pytest.mark.parametrize("prefix", ["my-run", "my_run", "my run", ""])
def test_a_non_alphanumeric_prefix_is_refused(prefix):
    """One prefix is spliced into role, repository, and runtime names, and their
    separator rules disagree."""
    with pytest.raises(ValueError, match="alphanumeric"):
        Names(prefix=prefix, account_id=ACCOUNT, region=REGION)
