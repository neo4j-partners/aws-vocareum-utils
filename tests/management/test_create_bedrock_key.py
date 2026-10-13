import importlib.util
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "bedrock-key" / "create-key.py"
SPEC = importlib.util.spec_from_file_location("create_bedrock_key", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
create_key = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(create_key)


def test_rendered_policy_scopes_models_to_account() -> None:
    policy = create_key.rendered_policy("123456789012")

    assert "${BEDROCK_KEY_ACCOUNT_ID}" not in policy
    assert "arn:aws:bedrock:us-east-1:123456789012:inference-profile/" in policy
    assert '"bedrock:CallWithBearerToken"' in policy


def test_secret_file_is_private_and_sourceable(tmp_path: Path) -> None:
    output = tmp_path / "bedrock-key.env"
    text = create_key.secret_env_text(
        "fake-secret",
        "us-east-1",
        "fake-id",
        "2026-09-08T00:00:00Z",
    )

    create_key.atomic_write_secret(output, text)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "AWS_BEARER_TOKEN_BEDROCK=fake-secret" in output.read_text()


def test_expiration_is_normalized_to_utc() -> None:
    value = datetime(2026, 9, 8, tzinfo=UTC)

    assert create_key.expiration_text(value) == "2026-09-08T00:00:00Z"


def test_default_output_is_git_ignored() -> None:
    create_key.ensure_git_ignored(create_key.DEFAULT_OUTPUT)


def test_tracked_output_is_rejected() -> None:
    with pytest.raises(SystemExit, match="not ignored by Git"):
        create_key.ensure_git_ignored(REPOSITORY_ROOT / ".gitignore")


class FakeIam:
    def list_service_specific_credentials(self, **_kwargs):
        return {
            "ServiceSpecificCredentials": [
                {
                    "ServiceSpecificCredentialId": "existing-id",
                    "Status": "Inactive",
                }
            ]
        }


def test_existing_inactive_key_is_still_rejected() -> None:
    with pytest.raises(SystemExit, match=r"existing-id \(Inactive\)"):
        create_key.ensure_no_existing_key(FakeIam(), "bedrock-api-user")


def test_main_creates_policy_key_and_private_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "arns.env"
    output_path = tmp_path / "bedrock-key.env"
    config_path.write_text(
        "\n".join(
            [
                "BEDROCK_KEY_ACCOUNT_ID=123456789012",
                "BEDROCK_KEY_REGION=us-east-1",
                "BEDROCK_KEY_USER_NAME=bedrock-api-user",
                "BEDROCK_KEY_POLICY_NAME=graphrag-bedrock-key-policy",
                "BEDROCK_KEY_ADMIN_PROFILE=admin",
                "BEDROCK_KEY_CREDENTIAL_ID=",
                "BEDROCK_KEY_CREDENTIAL_EXPIRES=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    class SuccessfulIam:
        policy_document = ""

        def get_user(self, **_kwargs):
            return {
                "User": {
                    "Arn": "arn:aws:iam::123456789012:user/workshop/bedrock-api-user",
                    "Path": "/workshop/",
                }
            }

        def put_user_policy(self, **kwargs):
            self.policy_document = kwargs["PolicyDocument"]

        def list_service_specific_credentials(self, **_kwargs):
            return {"ServiceSpecificCredentials": []}

        def create_service_specific_credential(self, **_kwargs):
            return {
                "ServiceSpecificCredential": {
                    "ServiceSpecificCredentialId": "new-id",
                    "ServiceCredentialSecret": "one-time-secret",
                    "ExpirationDate": datetime(2026, 9, 8, tzinfo=UTC),
                }
            }

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

    iam = SuccessfulIam()

    class FakeSession:
        def client(self, service_name):
            return FakeSts() if service_name == "sts" else iam

    monkeypatch.setattr(create_key, "DEFAULT_CONFIG", config_path)
    monkeypatch.setattr(create_key, "DEFAULT_OUTPUT", output_path)
    monkeypatch.setattr(create_key, "ensure_git_ignored", lambda _path: None)
    monkeypatch.setattr(create_key.boto3, "Session", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(sys, "argv", ["create-key.py"])

    assert create_key.main() == 0

    output_text = output_path.read_text(encoding="utf-8")
    config_text = config_path.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert "AWS_BEARER_TOKEN_BEDROCK=one-time-secret" in output_text
    assert "BEDROCK_KEY_CREDENTIAL_ID=new-id" in config_text
    assert "BEDROCK_KEY_CREDENTIAL_EXPIRES=2026-09-08T00:00:00Z" in config_text
    assert "123456789012:inference-profile" in iam.policy_document
    assert "one-time-secret" not in captured.out
