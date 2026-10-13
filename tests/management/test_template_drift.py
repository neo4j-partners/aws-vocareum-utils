"""Hold `lab-probe.template` to the same behaviour as `vocareum_tools`.

The deployer harness carries its classifier as a CloudFormation ``ZipFile``
literal. It runs inside a Lambda that has no access to this package, so the two
copies are kept in step by hand, and a hand-kept copy drifts. Phase 0 caught
one drift this way, Phase 3 caught two more.

This extracts the inline source, executes it, and drives it with the same case
tables the package self-tests against, so the two cannot disagree without a
test failing. It replaces a script that lived in a scratch directory and ran
only when someone remembered.

Nothing here calls AWS. The tables are the shipped ones in
``vocareum_tools.selftest``, so a case added for one harness is checked on both.
"""

from __future__ import annotations

import re
import sys
import textwrap
import types
from typing import Any

import pytest
from botocore.exceptions import ClientError

from vocareum_tools import probe, selftest
from vocareum_tools.config import repository_root

TEMPLATE = repository_root() / "lab-probe.template"

# The line that opens the inline Lambda source in the template.
ZIPFILE_MARKER = "ZipFile: |"


def extract_inline_source() -> str:
    """Return the Python body of the template's ZipFile literal.

    The block ends at the first non-blank line indented less than the first
    body line, which is how YAML delimits a literal scalar.
    """
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == ZIPFILE_MARKER]
    if len(starts) != 1:
        raise AssertionError(
            f"expected exactly one {ZIPFILE_MARKER!r} in {TEMPLATE.name}, "
            f"found {len(starts)}"
        )

    body: list[str] = []
    indent: int | None = None
    for line in lines[starts[0] + 1 :]:
        if not line.strip():
            body.append("")
            continue
        current = len(line) - len(line.lstrip())
        if indent is None:
            indent = current
        elif current < indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


@pytest.fixture(scope="module")
def inline() -> dict[str, Any]:
    """Execute the template's inline source and return its namespace.

    ``cfnresponse`` only exists in the Lambda runtime, so it is stubbed. The
    stub is never called: nothing in this module invokes the handler.
    """
    if "cfnresponse" not in sys.modules:
        stub = types.ModuleType("cfnresponse")
        stub.SUCCESS = "SUCCESS"
        stub.FAILED = "FAILED"
        stub.send = lambda *args, **kwargs: None
        sys.modules["cfnresponse"] = stub

    namespace: dict[str, Any] = {"__name__": "inline_probe"}
    source = extract_inline_source()
    exec(compile(source, f"{TEMPLATE.name}::ZipFile", "exec"), namespace)
    namespace["__source__"] = source
    return namespace


def error_for(code: str, message: str) -> ClientError:
    """Build a ClientError the way botocore delivers one."""
    return ClientError({"Error": {"Code": code, "Message": message}}, "ProbeOperation")


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    selftest.CLASSIFIER_CASES,
    ids=[code for code, _, _ in selftest.CLASSIFIER_CASES],
)
def test_template_classifier_agrees(
    inline: dict[str, Any], code: str, message: str, expected: probe.Verdict
) -> None:
    verdict, _ = inline["classify"](error_for(code, message), "")
    assert verdict == expected.value


@pytest.mark.parametrize(
    ("probed", "code", "message", "expected"),
    selftest.PROBED_CLASSIFIER_CASES,
    ids=[f"{probed}-{code}" for probed, code, _, _ in selftest.PROBED_CLASSIFIER_CASES],
)
def test_template_classifier_agrees_when_the_probed_action_matters(
    inline: dict[str, Any],
    probed: str,
    code: str,
    message: str,
    expected: probe.Verdict,
) -> None:
    verdict, _ = inline["classify"](error_for(code, message), probed)
    assert verdict == expected.value


# ---------------------------------------------------------------------------
# The shared tables
# ---------------------------------------------------------------------------


def test_template_tracker_status_agrees(inline: dict[str, Any]) -> None:
    """A verdict must mean the same tracker status in both harnesses.

    The template keys by the verdict string because it has no enum, and it
    omits every verdict that means Untested, because its lookups all pass
    ``"Untested"`` as the default. So the two tables are equivalent when the
    template carries exactly the verdicts that mean something other than
    Untested, and carries none of the ones that do not.
    """
    package = {
        verdict.value: status for verdict, status in probe.TRACKER_STATUS.items()
    }
    decided = {name: status for name, status in package.items() if status != "Untested"}
    undecided = {name for name, status in package.items() if status == "Untested"}

    assert inline["TRACKER_STATUS"] == decided
    assert undecided - set(inline["TRACKER_STATUS"]) == undecided


def test_the_template_defaults_an_unlisted_verdict_to_untested(
    inline: dict[str, Any],
) -> None:
    """The omission above is only safe while every lookup carries the default.

    A lookup that forgot it would raise KeyError inside the Lambda and lose the
    whole run, or worse, be written as a bare index that returns nothing.
    """
    source: str = inline["__source__"]
    lookups = re.findall(r"TRACKER_STATUS\s*(\.get\(|\[)", source)

    assert lookups, "the template no longer reads TRACKER_STATUS at all"
    assert all(kind == ".get(" for kind in lookups)
    assert 'TRACKER_STATUS.get(item["verdict"], "Untested")' in source


def test_template_client_candidates_agree(inline: dict[str, Any]) -> None:
    """Both harnesses must resolve an action to the same boto3 client.

    A service whose candidate list is missing a plane measures nothing and
    reports UNKNOWN_ACTION, which records as Untested. Row 72,
    bedrock-agentcore:InvokeAgentRuntime, is the case that made this real.
    """
    assert inline["CLIENT_CANDIDATES"] == probe.CLIENT_CANDIDATES


@pytest.mark.parametrize(
    ("operation", "expected"), selftest.METHOD_NAME_CASES, ids=lambda case: str(case)
)
def test_template_method_names_agree(
    inline: dict[str, Any], operation: str, expected: str
) -> None:
    assert inline["method_name"](operation) == expected


# ---------------------------------------------------------------------------
# Client resolution, which the case tables cannot reach
# ---------------------------------------------------------------------------


def test_template_names_every_client_it_tried(inline: dict[str, Any]) -> None:
    """An unmeasured action has to say which clients were checked.

    Without the list, a candidate table missing a plane is indistinguishable
    from a misspelled action name.
    """
    result = inline["run_probe"](
        "us-east-1", {"action": "bedrock-agentcore:NoSuchOperation"}
    )

    assert result["verdict"] == "UNKNOWN_ACTION"
    assert "bedrock-agentcore-control" in result["detail"]
    assert "bedrock-agentcore" in result["detail"]


def test_template_pins_an_explicit_client(inline: dict[str, Any]) -> None:
    """A probe that names a client is not quietly widened to the candidates."""
    result = inline["run_probe"](
        "us-east-1",
        {"action": "bedrock-agentcore:InvokeAgentRuntime", "client": "s3"},
    )

    assert result["verdict"] == "UNKNOWN_ACTION"
    assert "bedrock-agentcore-control" not in result["detail"]


# ---------------------------------------------------------------------------
# Source-level guards
#
# Two past drifts were invisible to behavioural tests because the template
# reached the right answer for the wrong reason. These pin the shape.
# ---------------------------------------------------------------------------


def test_the_bedrock_prefix_guard_keeps_its_colon(inline: dict[str, Any]) -> None:
    """`bedrock` without the colon also matches `bedrock-agentcore`.

    Matching the bare prefix reported every absent AgentCore resource as a
    model missing from the Region. Both verdicts map to Allowed, so no case
    table catches it. Only the source does.

    Matched by regex rather than substring, because the template is formatted
    to its own line length and wraps the call across three lines.
    """
    source: str = inline["__source__"]
    guards = re.findall(r'startswith\(\s*"(bedrock:?)"\s*\)', source)

    assert guards == ["bedrock:"]


def test_the_inline_source_is_not_empty(inline: dict[str, Any]) -> None:
    """A failed extraction would silently pass every test above.

    If the YAML shape changes and the body comes back empty, the exec succeeds
    and every lookup in the namespace fails, which reads as a missing key
    rather than as a broken instrument. This says which it is.
    """
    assert len(inline["__source__"].splitlines()) > 200
