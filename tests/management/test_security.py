import urllib.parse

import pytest

from vocareum_tools.security import (
    REDACTED,
    mask_secret,
    redact_payload,
    redact_text,
    safe_url,
)


def test_safe_url_redacts_all_signed_url_authorization_values() -> None:
    url = (
        "https://example.test/file?"
        "X-Amz-Credential=credential-value&"
        "X-Amz-Security-Token=session-value&"
        "X-Amz-Signature=signature-value&"
        "ordinary=visible"
    )

    redacted = safe_url(url)

    assert "credential-value" not in redacted
    assert "session-value" not in redacted
    assert "signature-value" not in redacted
    assert "ordinary=visible" in redacted
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(redacted).query)
    assert query["X-Amz-Credential"] == [REDACTED]
    assert query["X-Amz-Security-Token"] == [REDACTED]
    assert query["X-Amz-Signature"] == [REDACTED]


def test_redact_payload_preserves_access_key_id_but_masks_secrets() -> None:
    payload = {
        "aws_access_key_id": "ASIAEXAMPLE",
        "aws_secret_access_key": "very-secret-value",
        "aws_session_token": "session-value",
    }

    redacted = redact_payload(payload)

    assert redacted["aws_access_key_id"] == "ASIAEXAMPLE"
    assert "very-secret-value" not in redacted["aws_secret_access_key"]
    assert "session-value" not in redacted["aws_session_token"]


def test_redact_text_handles_signed_url_in_unstructured_text() -> None:
    body = (
        "download=https://example.test/file?"
        "X-Amz-Credential=credential-value&X-Amz-Signature=signature-value"
    )

    redacted = redact_text(body)

    assert "credential-value" not in redacted
    assert "signature-value" not in redacted


PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAsecretkeymaterialthatmustneverbeprinted\n"
    "-----END RSA PRIVATE KEY-----"
)


def test_redact_text_withholds_a_private_key_block() -> None:
    """The session record carries an unencrypted SSH key beside the credentials.

    It arrives inside a plain-text error body often enough that the non-JSON
    path has to handle it too, not just the structured one.
    """
    body = f"provisioning failed, key was:\n{PRIVATE_KEY}\nend of report"

    redacted = redact_text(body)

    assert "secretkeymaterial" not in redacted
    assert "BEGIN RSA PRIVATE KEY" not in redacted
    assert "end of report" in redacted


def test_redact_text_withholds_an_unterminated_private_key_block() -> None:
    """A body clipped mid-key still has to be treated as a key."""
    redacted = redact_text(PRIVATE_KEY.split("-----END")[0])

    assert "secretkeymaterial" not in redacted


def test_an_unterminated_key_does_not_swallow_the_failure_reason() -> None:
    """The reason printed after a key is the one thing these bodies are read for.

    A key block with no END line is bounded by the base64 lines that follow it,
    not by the end of the body. Redacting to the end would be just as safe and
    would destroy the diagnostic.
    """
    body = (
        f"provisioning failed.\n{PRIVATE_KEY.split('-----END')[0]}\n"
        "Reason: an explicit deny in a service control policy: p-example"
    )

    redacted = redact_text(body)

    assert "secretkeymaterial" not in redacted
    assert "Reason: an explicit deny in a service control policy: p-example" in redacted


def test_redact_text_masks_secret_fields_in_a_body_that_is_not_valid_json() -> None:
    body = 'Server said: {"session_token": "token-value", "name": "lab"} <truncated'

    redacted = redact_text(body)

    assert "token-value" not in redacted
    assert '"name": "lab"' in redacted


def test_redact_payload_withholds_a_private_key_by_value_alone() -> None:
    redacted = redact_payload({"unhelpfully_named_field": PRIVATE_KEY})

    assert "secretkeymaterial" not in redacted["unhelpfully_named_field"]


def test_mask_secret_keeps_the_tail_and_the_length() -> None:
    """The tail distinguishes two secrets in a transcript. The head names the kind."""
    masked = mask_secret("ASIA" + "x" * 20 + "TAIL")

    assert masked.endswith("TAIL")
    assert "[28 chars]" in masked
    assert "ASIA" not in masked


@pytest.mark.parametrize("value", ["", "abc", "abcd", None, 1234])
def test_mask_secret_reveals_nothing_of_a_short_or_non_string_value(
    value: object,
) -> None:
    assert mask_secret(value) == REDACTED
