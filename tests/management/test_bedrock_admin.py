import argparse
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "bedrock-key" / "admin.py"
SPEC = importlib.util.spec_from_file_location("bedrock_admin", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
admin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = admin
SPEC.loader.exec_module(admin)


def settings(credential_id: str | None = "key-id") -> admin.Settings:
    return admin.Settings(
        account_id="123456789012",
        region="us-east-1",
        user_name="bedrock-api-user",
        policy_name="key-policy",
        credential_id=credential_id,
        profile="admin",
        stack_name="controls",
        invocation_log_group="/aws/bedrock/invocations",
        chat_model_id="chat-model",
        alert_email="operator@example.com",
    )


def test_parser_exposes_the_operator_commands() -> None:
    parser = admin.build_parser()

    for command in (
        "create",
        "deploy",
        "status",
        "pause",
        "stop",
        "resume",
        "cost",
        "subscriptions",
        "alarm",
        "logs",
        "invoke-breaker",
        "delete-key",
        "teardown",
        "test-key",
    ):
        arguments = [command]
        if command == "alarm":
            arguments.append("burst")
        parsed = parser.parse_args(arguments)
        assert parsed.command == command

    assert parser.parse_args(["alarm", "total"]).alarm == "total"


class FakeIam:
    def __init__(self, status: str = "Active") -> None:
        self.status = status
        self.updates: list[dict[str, str]] = []

    def list_service_specific_credentials(self, **_kwargs):
        return {
            "ServiceSpecificCredentials": [
                {
                    "ServiceSpecificCredentialId": "key-id",
                    "Status": self.status,
                    "ExpirationDate": "2026-09-08T00:00:00Z",
                }
            ]
        }

    def update_service_specific_credential(self, **kwargs):
        self.updates.append(kwargs)
        self.status = kwargs["Status"]


class FakeSession:
    def __init__(self, clients: dict[str, object]) -> None:
        self.clients = clients

    def client(self, service_name: str):
        return self.clients[service_name]


class FakeSns:
    def __init__(self, subscriptions: list[dict[str, str]] | None = None) -> None:
        self.subscriptions = subscriptions or []
        self.subscribe_requests: list[dict[str, object]] = []
        self.confirm_requests: list[dict[str, str]] = []
        self.unsubscribe_requests: list[str] = []

    def list_subscriptions_by_topic(self, **_kwargs):
        return {"Subscriptions": self.subscriptions}

    def subscribe(self, **kwargs):
        self.subscribe_requests.append(kwargs)
        pending = {
            "Protocol": kwargs["Protocol"],
            "SubscriptionArn": "PendingConfirmation",
            "Endpoint": kwargs["Endpoint"],
        }
        self.subscriptions.append(pending)
        return {"SubscriptionArn": "pending confirmation"}

    def confirm_subscription(self, **kwargs):
        self.confirm_requests.append(kwargs)
        self.subscriptions = [
            item
            for item in self.subscriptions
            if item["SubscriptionArn"] != "PendingConfirmation"
        ]
        self.subscriptions.append(
            {
                "Protocol": "email",
                "SubscriptionArn": "arn:aws:sns:subscription",
                "Endpoint": "operator@example.com",
            }
        )
        return {"SubscriptionArn": "arn:aws:sns:subscription"}

    def unsubscribe(self, **kwargs):
        subscription_arn = kwargs["SubscriptionArn"]
        self.unsubscribe_requests.append(subscription_arn)
        self.subscriptions = [
            item
            for item in self.subscriptions
            if item["SubscriptionArn"] != subscription_arn
        ]

    def get_subscription_attributes(self, **_kwargs):
        authenticated = "true" if self.confirm_requests else "false"
        return {"Attributes": {"ConfirmationWasAuthenticated": authenticated}}


def test_stop_deactivates_configured_key_without_waiting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    iam = FakeIam()
    monkeypatch.setattr(
        admin, "guarded_session", lambda _settings: FakeSession({"iam": iam})
    )

    result = admin.command_stop(settings(), argparse.Namespace(no_wait=True))

    assert result == 0
    assert iam.updates == [
        {
            "UserName": "bedrock-api-user",
            "ServiceSpecificCredentialId": "key-id",
            "Status": "Inactive",
        }
    ]
    assert "Key stopped" in capsys.readouterr().out


class FakeCostExplorer:
    request = None

    def get_cost_and_usage(self, **kwargs):
        self.request = kwargs
        return {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["Amazon Bedrock"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": "12.5", "Unit": "USD"}
                            },
                        },
                        {
                            "Keys": ["Unused"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": "0", "Unit": "USD"}
                            },
                        },
                    ]
                }
            ]
        }


def test_cost_uses_exclusive_dates_and_hides_zero_services(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeCostExplorer()
    monkeypatch.setattr(
        admin, "guarded_session", lambda _settings: FakeSession({"ce": client})
    )

    result = admin.command_cost(
        settings(),
        argparse.Namespace(start=date(2026, 8, 1), end=date(2026, 8, 27)),
    )

    output = capsys.readouterr().out
    assert result == 0
    assert client.request["TimePeriod"] == {
        "Start": "2026-08-01",
        "End": "2026-08-27",
    }
    assert "12.50 USD  Amazon Bedrock" in output
    assert "Unused" not in output


def test_subscriptions_repairs_deleted_email(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sns = FakeSns(
        [
            {
                "Protocol": "email",
                "SubscriptionArn": "Deleted",
                "Endpoint": "operator@example.com",
            }
        ]
    )
    monkeypatch.setattr(
        admin, "guarded_session", lambda _settings: FakeSession({"sns": sns})
    )

    result = admin.command_subscriptions(
        settings(), argparse.Namespace(repair=True, confirm_token=None)
    )

    assert result == 1
    assert sns.subscribe_requests == [
        {
            "TopicArn": settings().breaker_topic_arn,
            "Protocol": "email",
            "Endpoint": "operator@example.com",
            "ReturnSubscriptionArn": True,
        }
    ]
    assert "Requested a new SNS confirmation email" in capsys.readouterr().out


def test_subscriptions_confirms_with_unsubscribe_protection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sns = FakeSns(
        [
            {
                "Protocol": "email",
                "SubscriptionArn": "PendingConfirmation",
                "Endpoint": "operator@example.com",
            }
        ]
    )
    monkeypatch.setattr(
        admin, "guarded_session", lambda _settings: FakeSession({"sns": sns})
    )

    result = admin.command_subscriptions(
        settings(), argparse.Namespace(repair=False, confirm_token="email-token")
    )

    assert result == 0
    assert sns.confirm_requests == [
        {
            "TopicArn": settings().breaker_topic_arn,
            "Token": "email-token",
            "AuthenticateOnUnsubscribe": "true",
        }
    ]
    assert "confirmed and protected" in capsys.readouterr().out


def test_subscriptions_repair_resends_pending_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sns = FakeSns(
        [
            {
                "Protocol": "email",
                "SubscriptionArn": "PendingConfirmation",
                "Endpoint": "operator@example.com",
            }
        ]
    )
    monkeypatch.setattr(
        admin, "guarded_session", lambda _settings: FakeSession({"sns": sns})
    )

    result = admin.command_subscriptions(
        settings(), argparse.Namespace(repair=True, confirm_token=None)
    )

    assert result == 1
    assert len(sns.subscribe_requests) == 1


def test_subscriptions_replaces_unprotected_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscription_arn = "arn:aws:sns:subscription"
    sns = FakeSns(
        [
            {
                "Protocol": "email",
                "SubscriptionArn": subscription_arn,
                "Endpoint": "operator@example.com",
            }
        ]
    )
    monkeypatch.setattr(
        admin, "guarded_session", lambda _settings: FakeSession({"sns": sns})
    )

    result = admin.command_subscriptions(
        settings(), argparse.Namespace(repair=True, confirm_token=None)
    )

    assert result == 1
    assert sns.unsubscribe_requests == [subscription_arn]
    assert len(sns.subscribe_requests) == 1


def test_clear_current_credential_updates_both_metadata_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "arns.env"
    config.write_text(
        "BEDROCK_KEY_CREDENTIAL_ID=key-id\n"
        "BEDROCK_KEY_CREDENTIAL_EXPIRES=2026-09-08T00:00:00Z\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin, "CONFIG_PATH", config)

    assert admin.clear_current_credential("key-id") is True

    assert config.read_text(encoding="utf-8") == (
        "BEDROCK_KEY_CREDENTIAL_ID=\nBEDROCK_KEY_CREDENTIAL_EXPIRES=\n"
    )


def test_delete_current_key_removes_local_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "arns.env"
    secret = tmp_path / "bedrock-key.env"
    config.write_text(
        "BEDROCK_KEY_CREDENTIAL_ID=key-id\n"
        "BEDROCK_KEY_CREDENTIAL_EXPIRES=2026-09-08T00:00:00Z\n",
        encoding="utf-8",
    )
    secret.write_text("AWS_BEARER_TOKEN_BEDROCK=secret\n", encoding="utf-8")

    class DeletingIam(FakeIam):
        deleted = None

        def delete_service_specific_credential(self, **kwargs):
            self.deleted = kwargs

    iam = DeletingIam()
    monkeypatch.setattr(admin, "CONFIG_PATH", config)
    monkeypatch.setattr(admin, "DEFAULT_SECRET_PATH", secret)
    monkeypatch.setattr(
        admin, "guarded_session", lambda _settings: FakeSession({"iam": iam})
    )

    admin.delete_credential(settings(), "key-id")

    assert iam.deleted["ServiceSpecificCredentialId"] == "key-id"
    assert not secret.exists()


def test_destructive_commands_require_confirmation() -> None:
    with pytest.raises(admin.AdminError, match="add --yes"):
        admin.command_delete_key(
            settings(), argparse.Namespace(yes=False, credential_id=None)
        )
    with pytest.raises(admin.AdminError, match="add --yes"):
        admin.command_teardown(settings(), argparse.Namespace(yes=False))


def deploy_values() -> dict[str, str]:
    return {
        "BEDROCK_KEY_USER_NAME": "bedrock-api-user",
        "BEDROCK_KEY_CREDENTIAL_ID": "key-id",
        "BEDROCK_KEY_CREDENTIAL_EXPIRES": "2026-09-08T00:00:00Z",
        "WORKSHOP_CHAT_MODEL_ID": "chat-model",
        "WORKSHOP_EMBED_MODEL_ID": "embed-model",
        "WORKSHOP_MEMORY_EMBED_MODEL_ID": "memory-model",
        "BEDROCK_KEY_ALERT_EMAIL": "operator@example.com",
        "BEDROCK_KEY_INVOCATION_LOG_GROUP": "/aws/bedrock/invocations",
        "BEDROCK_KEY_BURST_USD_PER_MINUTE": "30",
        "BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE": "25",
        "BEDROCK_KEY_SUSTAINED_PERIODS": "3",
        "BEDROCK_KEY_TOTAL_USD": "850",
    }


def test_deploy_parameters_reject_reversed_thresholds() -> None:
    values = deploy_values()
    values["BEDROCK_KEY_SUSTAINED_USD_PER_MINUTE"] = "30"

    with pytest.raises(admin.AdminError, match="must be positive and below"):
        admin.deploy_parameters(values)


def test_deploy_parameters_include_workshop_total() -> None:
    parameters = admin.deploy_parameters(deploy_values())

    assert {item["ParameterKey"]: item["ParameterValue"] for item in parameters}[
        "WorkshopTotalUsd"
    ] == "850"


def test_template_has_sliding_four_hour_total_alarm() -> None:
    template = (REPOSITORY_ROOT / "bedrock-key" / "controls.template").read_text(
        encoding="utf-8"
    )
    total_alarm = template.split("  WorkshopTotalSpendAlarm:", 1)[1].split(
        "\n  # " + "-" * 73,
        1,
    )[0]

    assert "Threshold: !Ref WorkshopTotalUsd" in total_alarm
    assert "Period: 14400" in total_alarm
    assert "WallClockWindow" not in total_alarm
    assert "- !Ref WorkshopTotalSpendAlarm" in template


@pytest.mark.parametrize("value", ["0", "-1"])
def test_deploy_parameters_reject_non_positive_total(value: str) -> None:
    values = deploy_values()
    values["BEDROCK_KEY_TOTAL_USD"] = value

    with pytest.raises(admin.AdminError, match="TOTAL_USD must be positive"):
        admin.deploy_parameters(values)


def test_deploy_dry_run_validates_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "arns.env"
    config.write_text(
        "\n".join(f"{name}={value}" for name, value in deploy_values().items()) + "\n",
        encoding="utf-8",
    )
    template = tmp_path / "controls.template"
    template.write_text("Resources: {}\n", encoding="utf-8")

    class FakeCloudFormation:
        def validate_template(self, **_kwargs):
            return {"Parameters": [{"ParameterKey": "KeyUserName"}]}

        def create_stack(self, **_kwargs):
            raise AssertionError("dry run must not create a stack")

        def update_stack(self, **_kwargs):
            raise AssertionError("dry run must not update a stack")

    iam = FakeIam()
    fake_session = FakeSession({"cloudformation": FakeCloudFormation(), "iam": iam})
    monkeypatch.setattr(admin, "CONFIG_PATH", config)
    monkeypatch.setattr(admin, "TEMPLATE_PATH", template)
    monkeypatch.setattr(admin, "guarded_session", lambda _settings: fake_session)

    result = admin.command_deploy(settings(), argparse.Namespace(dry_run=True))

    assert result == 0
    assert "Dry run: no stack" in capsys.readouterr().out


def test_deploy_creates_stack_and_enables_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "arns.env"
    config.write_text(
        "\n".join(f"{name}={value}" for name, value in deploy_values().items()) + "\n",
        encoding="utf-8",
    )
    template = tmp_path / "controls.template"
    template.write_text("Resources: {}\n", encoding="utf-8")

    class FakeWaiter:
        waited = False

        def wait(self, **_kwargs):
            self.waited = True

    class FakeCloudFormation:
        create_request = None
        describe_count = 0
        waiter = FakeWaiter()

        def validate_template(self, **_kwargs):
            return {"Parameters": [{"ParameterKey": "KeyUserName"}]}

        def describe_stacks(self, **_kwargs):
            self.describe_count += 1
            if self.describe_count == 1:
                raise admin.ClientError(
                    {
                        "Error": {
                            "Code": "ValidationError",
                            "Message": "stack does not exist",
                        }
                    },
                    "DescribeStacks",
                )
            return {
                "Stacks": [
                    {
                        "StackStatus": "CREATE_COMPLETE",
                        "Outputs": [
                            {
                                "OutputKey": "InvocationLogRoleArn",
                                "OutputValue": "arn:aws:iam::123456789012:role/logs",
                            }
                        ],
                    }
                ]
            }

        def create_stack(self, **kwargs):
            self.create_request = kwargs

        def get_waiter(self, name):
            assert name == "stack_create_complete"
            return self.waiter

    class FakeBedrock:
        logging_config = None

        def get_model_invocation_logging_configuration(self):
            if self.logging_config is None:
                raise admin.ClientError(
                    {
                        "Error": {
                            "Code": "ResourceNotFoundException",
                            "Message": "not configured",
                        }
                    },
                    "GetModelInvocationLoggingConfiguration",
                )
            return {"loggingConfig": self.logging_config}

        def put_model_invocation_logging_configuration(self, **kwargs):
            self.logging_config = kwargs["loggingConfig"]

    cloudformation = FakeCloudFormation()
    bedrock = FakeBedrock()
    sns = FakeSns(
        [
            {
                "Protocol": "email",
                "SubscriptionArn": "Deleted",
                "Endpoint": "operator@example.com",
            }
        ]
    )
    fake_session = FakeSession(
        {
            "cloudformation": cloudformation,
            "iam": FakeIam(),
            "bedrock": bedrock,
            "sns": sns,
        }
    )
    monkeypatch.setattr(admin, "CONFIG_PATH", config)
    monkeypatch.setattr(admin, "TEMPLATE_PATH", template)
    monkeypatch.setattr(admin, "guarded_session", lambda _settings: fake_session)

    result = admin.command_deploy(settings(), argparse.Namespace(dry_run=False))

    assert result == 0
    assert cloudformation.create_request["Capabilities"] == ["CAPABILITY_IAM"]
    assert cloudformation.waiter.waited is True
    assert bedrock.logging_config["textDataDeliveryEnabled"] is True
    assert bedrock.logging_config["cloudWatchConfig"]["roleArn"].endswith("/logs")
    assert len(sns.subscribe_requests) == 1
