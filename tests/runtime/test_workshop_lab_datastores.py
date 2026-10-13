"""Offline tests for step 12, Secrets Manager and DynamoDB.

The two worth reading are the placeholder secret and the retrying table delete.
The first is a rule about what may be written down; the second is the difference
between a table that is torn down and a table that survives the lab.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from workshop_lab.datastores import (
    CREATE_SECRET_CHECK,
    CREATE_TABLE_CHECK,
    DESCRIBE_SECRET_CHECK,
    GET_ITEM_CHECK,
    GET_SECRET_CHECK,
    PUT_ITEM_CHECK,
    ROUND_TRIP_CHECK,
    SECRET_PAYLOAD,
    DataStores,
)
from workshop_lab.harness import FAIL, PASS, SKIP, Harness
from workshop_lab.naming import Names
from workshop_lab.selection import WORKSHOP_TAG_KEY

ACCOUNT = "123456789012"
REGION = "us-east-1"


def denial(operation: str, code: str = "AccessDeniedException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, operation)


class FakeSecrets:
    def __init__(self) -> None:
        self.refuse: set[str] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if name in self.refuse:
            raise denial(name)

    def create_secret(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_secret", kwargs)
        return {"ARN": "arn:aws:secretsmanager:::secret:verify"}

    def describe_secret(self, **kwargs: Any) -> dict[str, Any]:
        self._record("describe_secret", kwargs)
        return {}

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_secret_value", kwargs)
        return {"SecretString": json.dumps(SECRET_PAYLOAD)}

    def delete_secret(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_secret", kwargs)
        return {}


class FakeDynamo:
    def __init__(self) -> None:
        self.refuse: set[str] = set()
        self.item: dict[str, Any] | None = None
        self.busy_deletes = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if name in self.refuse:
            raise denial(name)

    def create_table(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_table", kwargs)
        return {"TableDescription": {"TableStatus": "CREATING"}}

    def describe_table(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_table", kwargs))
        return {"Table": {"TableStatus": "ACTIVE"}}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self._record("put_item", kwargs)
        self.item = kwargs["Item"]
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_item", kwargs)
        return {"Item": self.item} if self.item else {}

    def delete_table(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_table", kwargs)
        if self.busy_deletes:
            self.busy_deletes -= 1
            raise denial("delete_table", "ResourceInUseException")
        return {}


class FakeSession:
    def __init__(self, clients: dict[str, Any]) -> None:
        self.region_name = REGION
        self._clients = clients

    def client(self, name: str, region_name: str | None = None) -> Any:
        return self._clients[name]


@pytest.fixture(autouse=True)
def never_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """One patch, because the table delete waits through `Harness.retry_while`."""
    monkeypatch.setattr("workshop_lab.harness.time.sleep", lambda _: None)


@pytest.fixture
def secrets() -> FakeSecrets:
    return FakeSecrets()


@pytest.fixture
def dynamodb() -> FakeDynamo:
    return FakeDynamo()


@pytest.fixture
def lab(secrets: FakeSecrets, dynamodb: FakeDynamo) -> Harness:
    printed: list[str] = []
    harness = Harness(
        session=FakeSession({"secretsmanager": secrets, "dynamodb": dynamodb}),
        echo=printed.append,
    )
    harness.account_id = ACCOUNT
    harness._names = Names(prefix="verify", account_id=ACCOUNT, region=REGION)
    harness.printed = printed  # type: ignore[attr-defined]
    return harness


def verdicts(lab: Harness) -> dict[str, str]:
    return {name: verdict for name, verdict, _ in lab.results}


def detail(lab: Harness, name: str) -> str:
    return next(text for row, _, text in lab.results if row == name)


# --- the secret -----------------------------------------------------------


def test_the_secret_holds_only_placeholders() -> None:
    """Real Aura credentials do not belong in anything that reaches a log.

    Step 3 already proved they work, so nothing is gained by putting them here.
    """
    assert set(SECRET_PAYLOAD) == {"uri", "username", "password", "database"}
    assert SECRET_PAYLOAD["password"] == "placeholder"
    assert "placeholder" in SECRET_PAYLOAD["uri"]


def test_the_secret_is_created_tagged_and_read_back(
    lab: Harness, secrets: FakeSecrets
) -> None:
    DataStores(lab).run()

    kwargs = next(k for name, k in secrets.calls if name == "create_secret")
    assert kwargs["Name"] == "verify/neo4j-command"
    assert kwargs["Tags"] == [
        {"Key": WORKSHOP_TAG_KEY, "Value": "stop-ai-agent-hallucinations"}
    ]
    assert json.loads(kwargs["SecretString"]) == SECRET_PAYLOAD
    assert verdicts(lab)[DESCRIBE_SECRET_CHECK] == PASS
    assert verdicts(lab)[GET_SECRET_CHECK] == PASS


def test_a_refused_secret_skips_the_two_reads_but_not_the_table(
    lab: Harness, secrets: FakeSecrets
) -> None:
    """The DynamoDB questions are separate and still get asked."""
    secrets.refuse.add("create_secret")

    DataStores(lab).run()

    assert verdicts(lab)[CREATE_SECRET_CHECK] == FAIL
    assert DESCRIBE_SECRET_CHECK not in verdicts(lab)
    assert GET_SECRET_CHECK not in verdicts(lab)
    assert verdicts(lab)[CREATE_TABLE_CHECK] == PASS


def test_the_secret_delete_skips_the_recovery_window(
    lab: Harness, secrets: FakeSecrets
) -> None:
    """A secret in its recovery window still holds the name the next run wants."""
    DataStores(lab).run()

    delete = next(call for label, call in lab.cleanups if label.startswith("secret "))
    delete()
    kwargs = next(k for name, k in secrets.calls if name == "delete_secret")
    assert kwargs["ForceDeleteWithoutRecovery"] is True


# --- the table ------------------------------------------------------------


def test_the_table_matches_the_workshops_shape(
    lab: Harness, dynamodb: FakeDynamo
) -> None:
    DataStores(lab).run()

    kwargs = next(k for name, k in dynamodb.calls if name == "create_table")
    assert kwargs["TableName"] == "workshop-verify-bookings"
    assert [one["KeyType"] for one in kwargs["KeySchema"]] == ["HASH", "RANGE"]
    assert kwargs["BillingMode"] == "PAY_PER_REQUEST"
    assert kwargs["Tags"][0]["Key"] == WORKSHOP_TAG_KEY


def test_a_refused_table_skips_the_item_calls(
    lab: Harness, dynamodb: FakeDynamo
) -> None:
    """There is nothing to write to, so a FAIL on PutItem would measure nothing."""
    dynamodb.refuse.add("create_table")

    DataStores(lab).run()

    assert verdicts(lab)[CREATE_TABLE_CHECK] == FAIL
    assert PUT_ITEM_CHECK not in verdicts(lab)
    assert ROUND_TRIP_CHECK not in verdicts(lab)


def test_the_round_trip_passes_when_the_item_comes_back(lab: Harness) -> None:
    DataStores(lab).run()

    assert verdicts(lab)[PUT_ITEM_CHECK] == PASS
    assert verdicts(lab)[GET_ITEM_CHECK] == PASS
    assert verdicts(lab)[ROUND_TRIP_CHECK] == PASS


def test_the_read_is_consistent(lab: Harness, dynamodb: FakeDynamo) -> None:
    """An eventually consistent read can miss a write this recent and say nothing."""
    DataStores(lab).run()

    kwargs = next(k for name, k in dynamodb.calls if name == "get_item")
    assert kwargs["ConsistentRead"] is True


def test_a_write_that_does_not_read_back_is_a_failure(
    lab: Harness, dynamodb: FakeDynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dynamodb, "get_item", lambda **kwargs: {})

    DataStores(lab).run()

    assert verdicts(lab)[ROUND_TRIP_CHECK] == FAIL
    assert detail(lab, ROUND_TRIP_CHECK) == "the item was written but not read"


def test_a_refused_read_skips_the_round_trip_rather_than_failing_it(
    lab: Harness, dynamodb: FakeDynamo
) -> None:
    """Nothing was read, so nothing is known about whether the write landed."""
    dynamodb.refuse.add("get_item")

    DataStores(lab).run()

    assert verdicts(lab)[GET_ITEM_CHECK] == FAIL
    assert verdicts(lab)[ROUND_TRIP_CHECK] == SKIP


# --- the delete -----------------------------------------------------------


def test_the_table_delete_retries_while_the_table_is_still_busy(
    lab: Harness, dynamodb: FakeDynamo
) -> None:
    """Tagging leaves a table busy for longer than the item calls take.

    A delete that gives up on the first ResourceInUseException leaves the table
    in the account past the end of the lab.
    """
    DataStores(lab).run()
    dynamodb.busy_deletes = 3

    delete = next(call for label, call in lab.cleanups if "dynamodb table" in label)
    delete()

    assert len([name for name, _ in dynamodb.calls if name == "delete_table"]) == 4


def test_the_table_delete_still_raises_anything_that_is_not_a_busy_table(
    lab: Harness, dynamodb: FakeDynamo
) -> None:
    """Step 14 counts a delete that raises. Swallowing one reports a clean account."""
    DataStores(lab).run()
    dynamodb.refuse.add("delete_table")

    delete = next(call for label, call in lab.cleanups if "dynamodb table" in label)
    with pytest.raises(ClientError):
        delete()


def test_both_resources_register_a_delete(lab: Harness) -> None:
    DataStores(lab).run()

    assert [label for label, _ in lab.cleanups] == [
        "secret verify/neo4j-command",
        "dynamodb table workshop-verify-bookings",
    ]


# --- names ----------------------------------------------------------------


def test_every_check_names_an_iam_action_or_a_behaviour(lab: Harness) -> None:
    """Permission rows are keyed on these strings."""
    DataStores(lab).run()

    for name, _, _ in lab.results:
        assert name.startswith(("secretsmanager:", "dynamodb:", "dynamodb item"))
