"""Hold `lab.template` to the invariant both sweepers' fail-open rests on.

Neither sweeper can read CloudFormation reliably, and both carry on when that
read fails. `lab.template` states why that is safe, at `SweepReadOwnStack`: the
handler reports a refusal and carries on, which is safe only because this
template declares nothing the selection can match. `read_stack_owned` in
`workshop_lab.teardown` repeats it, and
`tests/runtime/test_workshop_lab_teardown.py` pins the behaviour that depends
on it.

Three places assert a property of a fourth file, and nothing checked the fourth
file. This does. If a future `lab.template` ever declares a resource either
sweeper would select, the fail-open stops being safe, and the failure would
otherwise appear as a stack resource deleted out from under a live stack in a
student's account, at the one moment nobody is watching.

Nothing here calls AWS or CloudFormation. The template is parsed, its names are
rendered the way CloudFormation would render them, and the shared selection rule
is asked about each one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from vocareum_tools import sweep
from vocareum_tools.config import repository_root
from workshop_lab.selection import (
    AGENTCORE_RUNTIME_LOG_PREFIX,
    WORKSHOP_TAG_KEY,
    WORKSHOP_TAG_VALUE,
    name_prefixes,
    selected_by,
    tag_map,
)

TEMPLATE = repository_root() / "courses" / "neo4j-fundamentals" / "lab.template"
SCRIPT = repository_root() / "scripts" / "simulate_lab_policy.py"

ACCOUNT = "123456789012"
REGION = "us-east-1"

# The stack name and id shapes Vocareum really uses, as measured in live sessions.
# They matter twice: `${AWS::StackName}` lands inside two of the declared names,
# and a resource that declares no name of its own is generated from the stack
# name, so a stack name that could not match a prefix is half of that argument.
STACK_NAME = "c123456a1234567t1w123456789012"
STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK_NAME}"
    "/0f0a1b2c-3d4e-5f60-7a8b-9c0d1e2f3a4b"
)

# The prefix the notebook's own tests use throughout. `sweep.NAME_PREFIXES` is
# the union across every generation, and `tests/management/test_sweep.py` holds it as a
# superset of this one, so asking both is asking the widest rule there is.
NOTEBOOK_PREFIX = "verify"


def load_script() -> ModuleType:
    """Import `scripts/simulate_lab_policy.py` by path, without running it.

    Its CloudFormation loader is reused rather than rewritten. A second YAML
    loader for the same file would be a second thing to keep in step with the
    template's shorthand, and the whole point here is that a template property
    means one thing across the repository.
    """
    spec = importlib.util.spec_from_file_location("simulate_lab_policy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SIMULATOR = load_script()


def context(template: Any) -> dict[str, str]:
    """The `!Sub` names available to a resource, which is more than a policy's.

    `substitution_context` covers the pseudo parameters a PolicyDocument can
    use and every template parameter's default. Resource names reach for two
    more that no policy statement does.
    """
    values = SIMULATOR.substitution_context(ACCOUNT, REGION, template)
    values["AWS::StackName"] = STACK_NAME
    values["AWS::StackId"] = STACK_ID
    return values


def render(node: Any, values: dict[str, str], where: str) -> Any:
    """Resolve a resource property to the string CloudFormation would produce.

    Wider than the simulator's `resolve` because resource names use intrinsics a
    policy document never does: the bucket name is a `!Select` over a `!Split`
    over the stack id, twice over, and it arrives inside the mapping half of a
    `!Sub` list form, which `resolve_sub` requires to be plain strings.

    An intrinsic with no implementation here raises rather than resolving to
    something plausible. A name this file could not render is a name it cannot
    make a claim about, and a silent skip would read as a pass.
    """
    if isinstance(node, SIMULATOR.Intrinsic):
        tag, value = node.tag, node.value
        if tag == "Sub":
            if isinstance(value, list) and len(value) == 2:
                mapping = {
                    key: render(item, values, where) for key, item in value[1].items()
                }
                return SIMULATOR.resolve_sub([value[0], mapping], values, where)
            return SIMULATOR.resolve_sub(value, values, where)
        if tag == "Ref":
            if value in values:
                return values[value]
            # A Ref to another resource resolves to that resource's own
            # physical id, which the generated-name test covers separately.
            return f"<Ref {value}>"
        if tag == "Split":
            separator, text = value
            return render(text, values, where).split(separator)
        if tag == "Select":
            index, items = value
            return render(items, values, where)[int(index)]
        if tag == "GetAtt":
            return f"<GetAtt {value}>"
        raise AssertionError(f"{where}: no rendering for !{tag}")
    if isinstance(node, list):
        return [render(item, values, where) for item in node]
    if isinstance(node, dict):
        return {key: render(item, values, where) for key, item in node.items()}
    return node


@pytest.fixture(scope="module")
def resources() -> dict[str, dict[str, Any]]:
    """Every resource in the template, with its names and tags rendered.

    A resource contributes a name for each `Properties` key called `Name` or
    ending in `Name`. That rule is deliberately mechanical: a future resource
    type brings its own name key with it, and a hand-maintained list of key
    names is exactly the kind of thing that would leave the next one unchecked.
    """
    template = SIMULATOR.load_template(TEMPLATE)
    values = context(template)
    rendered: dict[str, dict[str, Any]] = {}
    for logical, resource in template["Resources"].items():
        properties = resource.get("Properties", {}) or {}
        names = [
            rendered_name
            for key, value in properties.items()
            if key == "Name" or key.endswith("Name")
            if isinstance(
                rendered_name := render(value, values, f"{logical}.{key}"), str
            )
        ]
        rendered[logical] = {
            "type": resource.get("Type", ""),
            "names": names,
            "tags": tag_map(render(properties.get("Tags"), values, logical)),
        }
    return rendered


def test_the_renderer_reproduces_the_name_cloudformation_would_build(resources):
    """The bucket name is the hardest one in the file, so it is the proof.

    Without this the three tests below could all pass on names that were never
    rendered at all.
    """
    assert resources["LabBucket"]["names"] == [f"vocareum-lab-{ACCOUNT}-9c0d1e2f3a4b"]
    assert resources["LabLogGroup"]["names"] == [f"/vocareum-lab/{STACK_NAME}"]


def test_no_declared_name_is_selectable_by_either_sweeper(resources):
    """The invariant `lab.template` claims and neither sweeper verifies.

    Asked with the resource's own tags, so this is the whole union rule and not
    just its name half. `require_prefix` is left False, which is the wider of
    the two IAM answers: a name that passes here passes for IAM too.
    """
    prefixes = (*sweep.NAME_PREFIXES, *name_prefixes(NOTEBOOK_PREFIX))
    for logical, resource in resources.items():
        for name in resource["names"]:
            reason = selected_by(name, resource["tags"], prefixes)
            assert reason == "", f"{logical} declares a selectable name: {name}"


def test_no_resource_carries_the_workshop_tag(resources):
    """The other half of the union, closed on its own.

    Selection is a union, so a stack resource tagged this way would be in scope
    whatever it is called, and no name check above would catch it. The template
    tags with `Name` / `Purpose` / `StackName` instead, and that has to stay
    true rather than merely happen to be true.
    """
    for logical, resource in resources.items():
        assert resource["tags"].get(WORKSHOP_TAG_KEY) != WORKSHOP_TAG_VALUE, logical


def test_no_log_group_sits_under_the_agentcore_runtimes_path(resources):
    """The specific condition the template's own comment names.

    The sweep Lambda holds `logs:DeleteLogGroup` scoped to this path and nothing
    else, and both sweepers select a group by moving the workshop prefixes under
    it. A `lab.template` log group declared here would be reachable by all three
    at once.
    """
    for logical, resource in resources.items():
        for name in resource["names"]:
            assert not name.startswith(AGENTCORE_RUNTIME_LOG_PREFIX), logical


def test_a_generated_name_is_not_selectable_either(resources):
    """The resources that declare no name of their own are covered too.

    CloudFormation builds those as `<stack>-<logical id>-<suffix>`, so the stack
    name leads and the logical id never reaches the front of the string. That is
    why the sweep function and its role are out of scope despite `LabSweep*`
    being a perfectly ordinary prefix to have chosen.
    """
    prefixes = (*sweep.NAME_PREFIXES, *name_prefixes(NOTEBOOK_PREFIX))
    unnamed = [
        logical
        for logical, resource in resources.items()
        if not resource["names"] and resource["type"] != "AWS::IAM::RolePolicy"
    ]
    assert unnamed, "expected at least one generated name to check"
    for logical in unnamed:
        generated = f"{STACK_NAME}-{logical}-A1B2C3D4E5F6"
        assert selected_by(generated, {}, prefixes) == "", logical


def test_the_template_is_the_one_this_repository_ships():
    """A guard against the fixture silently parsing something else."""
    assert TEMPLATE.exists() and Path(TEMPLATE).name == "lab.template"
