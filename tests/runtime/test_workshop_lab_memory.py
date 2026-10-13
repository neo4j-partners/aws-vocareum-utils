"""Offline tests for step 10, the AgentCore Memory.

The tag read-back is the part worth testing hardest. A tag that does not stick
costs nothing at the time and costs the account everything at the end of the lab,
because the workshop's cleanup only deletes what carries it.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from workshop_lab.harness import FAIL, PASS, Harness
from workshop_lab.memory import (
    CREATE_CHECK,
    EVENT_EXPIRY_DAYS,
    GET_CHECK,
    LIST_TAGS_CHECK,
    STRATEGIES,
    TAG_CHECK,
    TAG_READBACK_CHECK,
    UPDATE_CHECK,
    Memory,
)
from workshop_lab.naming import Names
from workshop_lab.selection import WORKSHOP_TAG_KEY, WORKSHOP_TAG_VALUE

ACCOUNT = "123456789012"
REGION = "us-east-1"
MEMORY_ID = "verify_memory-abc123"
MEMORY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/{MEMORY_ID}"


def denial(operation: str, code: str = "AccessDeniedException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, operation)


class FakeControl:
    """The AgentCore control plane, as much of it as step 10 touches."""

    def __init__(self) -> None:
        self.refuse: set[str] = set()
        self.states = ["ACTIVE"]
        self.tags: dict[str, str] = {}
        self.stored_tags: dict[str, str] | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if name in self.refuse:
            raise denial(name)

    def create_memory(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_memory", kwargs)
        return {"memory": {"id": MEMORY_ID, "arn": MEMORY_ARN}}

    def get_memory(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_memory", kwargs)
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"memory": {"id": MEMORY_ID, "arn": MEMORY_ARN, "status": state}}

    def tag_resource(self, **kwargs: Any) -> dict[str, Any]:
        self._record("tag_resource", kwargs)
        self.tags.update(kwargs["tags"])
        return {}

    def list_tags_for_resource(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_tags_for_resource", kwargs)
        return {"tags": self.stored_tags if self.stored_tags is not None else self.tags}

    def update_memory(self, **kwargs: Any) -> dict[str, Any]:
        self._record("update_memory", kwargs)
        return {}

    def delete_memory(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_memory", kwargs)
        return {}


class FakeSession:
    def __init__(self, control: FakeControl) -> None:
        self.region_name = REGION
        self.control = control

    def client(self, name: str, region_name: str | None = None) -> Any:
        return self.control


@pytest.fixture(autouse=True)
def never_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workshop_lab.harness.time.sleep", lambda _: None)


@pytest.fixture
def control() -> FakeControl:
    return FakeControl()


@pytest.fixture
def lab(control: FakeControl) -> Harness:
    printed: list[str] = []
    harness = Harness(session=FakeSession(control), echo=printed.append)
    harness.account_id = ACCOUNT
    harness._names = Names(prefix="verify", account_id=ACCOUNT, region=REGION)
    harness.printed = printed  # type: ignore[attr-defined]
    return harness


def verdicts(lab: Harness) -> dict[str, str]:
    return {name: verdict for name, verdict, _ in lab.results}


def detail(lab: Harness, name: str) -> str:
    return next(text for row, _, text in lab.results if row == name)


# --- create ---------------------------------------------------------------


def test_the_happy_path_records_every_row(lab: Harness) -> None:
    result = Memory(lab).run()

    assert result == MEMORY_ID
    assert verdicts(lab) == {
        CREATE_CHECK: PASS,
        GET_CHECK: PASS,
        TAG_CHECK: PASS,
        LIST_TAGS_CHECK: PASS,
        TAG_READBACK_CHECK: PASS,
        UPDATE_CHECK: PASS,
    }


def test_create_asks_for_both_long_term_strategies(
    lab: Harness, control: FakeControl
) -> None:
    """The pair the workshop configures. Either one alone measures less."""
    Memory(lab).run()

    kwargs = next(k for name, k in control.calls if name == "create_memory")
    assert kwargs["name"] == "verify_memory"
    assert kwargs["eventExpiryDuration"] == EVENT_EXPIRY_DAYS
    kinds = {key for strategy in kwargs["memoryStrategies"] for key in strategy}
    assert kinds == {"userPreferenceMemoryStrategy", "semanticMemoryStrategy"}


def test_the_strategy_namespaces_carry_the_actor_placeholder() -> None:
    """AgentCore substitutes `{actorId}` per user. A literal would share memory."""
    for strategy in STRATEGIES:
        for body in strategy.values():
            assert all("{actorId}" in one for one in body["namespaces"])


def test_the_memory_is_tagged_in_the_create_as_well_as_by_tag_resource(
    lab: Harness, control: FakeControl
) -> None:
    """Both, and neither replaces the other.

    `tag_resource` is what measures `bedrock-agentcore:TagResource` for the
    tracker, and it cannot run until the Memory is ACTIVE, a minute or more
    later. Without the tag on the create the Memory is live and invisible to a
    tag sweep for the whole of that window.
    """
    Memory(lab).run()

    created = next(k for name, k in control.calls if name == "create_memory")
    assert created["tags"] == lab.names.tags_map
    assert "tag_resource" in [name for name, _ in control.calls]


def test_a_refused_create_stops_the_step(lab: Harness, control: FakeControl) -> None:
    control.refuse.add("create_memory")

    result = Memory(lab).run()

    assert result is None
    assert verdicts(lab) == {CREATE_CHECK: FAIL}


def test_a_created_memory_registers_its_delete(
    lab: Harness, control: FakeControl
) -> None:
    Memory(lab).run()

    assert [label for label, _ in lab.cleanups] == [f"memory {MEMORY_ID}"]
    lab.cleanups[0][1]()
    assert ("delete_memory", {"memoryId": MEMORY_ID}) in control.calls


# --- tagging --------------------------------------------------------------


def test_the_tag_written_is_the_one_the_workshop_cleanup_looks_for(
    lab: Harness, control: FakeControl
) -> None:
    Memory(lab).run()

    kwargs = next(k for name, k in control.calls if name == "tag_resource")
    assert kwargs["resourceArn"] == MEMORY_ARN
    assert kwargs["tags"] == {WORKSHOP_TAG_KEY: WORKSHOP_TAG_VALUE}


def test_a_tag_that_does_not_stick_is_a_failure_naming_the_consequence(
    lab: Harness, control: FakeControl
) -> None:
    """This is the whole reason the step reads the tag back.

    A silent tagging failure leaves the resource in the account past the end of
    the lab, because cleanup only deletes what carries the tag.
    """
    control.stored_tags = {}

    Memory(lab).run()

    assert verdicts(lab)[TAG_CHECK] == PASS
    assert verdicts(lab)[TAG_READBACK_CHECK] == FAIL
    assert "workshop cleanup will not find this" in detail(lab, TAG_READBACK_CHECK)


def test_a_tag_read_back_with_the_wrong_value_is_a_failure(
    lab: Harness, control: FakeControl
) -> None:
    control.stored_tags = {WORKSHOP_TAG_KEY: "something-else"}

    Memory(lab).run()

    assert verdicts(lab)[TAG_READBACK_CHECK] == FAIL


def test_a_refused_tag_records_no_read_back_row(
    lab: Harness, control: FakeControl
) -> None:
    """Nothing was written, so there is no question about whether it stuck."""
    control.refuse.add("tag_resource")

    Memory(lab).run()

    assert verdicts(lab)[TAG_CHECK] == FAIL
    assert TAG_READBACK_CHECK not in verdicts(lab)


def test_a_refused_list_tags_records_no_read_back_row(
    lab: Harness, control: FakeControl
) -> None:
    control.refuse.add("list_tags_for_resource")

    Memory(lab).run()

    assert verdicts(lab)[LIST_TAGS_CHECK] == FAIL
    assert TAG_READBACK_CHECK not in verdicts(lab)


def test_a_boto3_without_tag_resource_says_to_upgrade_rather_than_raising(
    lab: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AgentCore's tagging API arrived after the client did.

    A missing method is not a permission answer, and an AttributeError out of the
    cell would stop the notebook rather than record anything.
    """
    monkeypatch.delattr(FakeControl, "tag_resource")

    Memory(lab).run()

    assert verdicts(lab)[TAG_CHECK] == FAIL
    assert "upgrade boto3" in detail(lab, TAG_CHECK)
    assert TAG_READBACK_CHECK not in verdicts(lab)
    assert LIST_TAGS_CHECK not in verdicts(lab)


# --- update ---------------------------------------------------------------


def test_the_step_waits_for_active_again_after_the_update(
    lab: Harness, control: FakeControl
) -> None:
    """UpdateMemory leaves the Memory UPDATING for over a minute.

    A delete inside that window is refused, and step 14 does not know to wait.
    """
    control.states = ["CREATING", "ACTIVE", "UPDATING", "UPDATING", "ACTIVE"]

    Memory(lab).run()

    assert verdicts(lab)[UPDATE_CHECK] == PASS
    assert control.states == ["ACTIVE"]


def test_the_update_runs_after_the_tagging(lab: Harness, control: FakeControl) -> None:
    """Tagging first means a resource is findable before anything can leave it stuck."""
    Memory(lab).run()
    order = [name for name, _ in control.calls]

    assert order.index("tag_resource") < order.index("update_memory")


def test_a_refused_update_still_leaves_the_earlier_rows(
    lab: Harness, control: FakeControl
) -> None:
    control.refuse.add("update_memory")

    result = Memory(lab).run()

    assert result == MEMORY_ID
    assert verdicts(lab)[UPDATE_CHECK] == FAIL
    assert verdicts(lab)[CREATE_CHECK] == PASS


# --- names ----------------------------------------------------------------


def test_every_check_names_an_iam_action_or_a_behaviour(lab: Harness) -> None:
    """Permission rows are keyed on these strings."""
    Memory(lab).run()

    for name, _, _ in lab.results:
        assert name.startswith("agentcore:") or name == TAG_READBACK_CHECK
