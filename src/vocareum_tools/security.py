"""Redaction helpers for values returned by Vocareum and signed URLs."""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

REDACTED = "********"

SECRET_NAME_FRAGMENTS = (
    "secret",
    "password",
    "passwd",
    "passphrase",
    "privatekey",
    "sshkey",
    "token",
    "credential",
    "signature",
    "apikey",
)

SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "accesstoken",
        "apikey",
        "credential",
        "password",
        "sessiontoken",
        "securitytoken",
        "signature",
        "signintoken",
        "token",
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
    }
)

# The second alternative covers a body that was clipped before the END line.
# It is deliberately narrow, whole lines of base64 and nothing else, because a
# greedy fallback would be just as safe but would also swallow whatever follows
# the key. What follows is usually the failure reason, which is the one thing
# these bodies are read for.
#
# A base64 line and a single prose word are the same thing to a regex, so a
# one-word line immediately under the key is redacted with it. That costs a word
# rather than the rest of the message, which is the trade being made here.
PEM_BLOCK_PATTERN = re.compile(
    r"-----BEGIN[^-\n]*PRIVATE KEY-----"
    r"(?:.*?-----END[^-\n]*PRIVATE KEY-----|(?:\r?\n[A-Za-z0-9+/=]+)*)",
    re.DOTALL,
)
JSON_SECRET_FIELD_PATTERN = re.compile(
    r'("[A-Za-z0-9_.\-]*'
    r"(?:secret|token|password|passphrase|private_?key|ssh_?key|credential"
    r'|signature|api_?key)[A-Za-z0-9_.\-]*"\s*:\s*)"(?:[^"\\]|\\.)*"',
    re.IGNORECASE,
)
SENSITIVE_QUERY_PATTERN = re.compile(
    r"((?:signin|session|security|access)?token"
    r"|x-amz-(?:credential|security-token|signature)"
    r"|credential|signature|api-?key)=([^&\s\"'\\]+)",
    re.IGNORECASE,
)


def normalize_key(key: str) -> str:
    """Lowercase a key and strip every non-alphanumeric character."""
    return "".join(char for char in key.lower() if char.isalnum())


def is_access_key_id(key: str) -> bool:
    """Report whether a key name resembles an AWS access key ID."""
    normalized = normalize_key(key)
    return "accesskeyid" in normalized or normalized in {
        "accesskey",
        "awsaccesskey",
        "keyid",
    }


def is_secret_access_key(key: str) -> bool:
    """Report whether a key name resembles an AWS secret access key."""
    normalized = normalize_key(key)
    return (
        "secretaccesskey" in normalized
        or "secretkey" in normalized
        or normalized in {"secret", "awssecret"}
    )


def is_session_token(key: str) -> bool:
    """Report whether a key name resembles an AWS session token."""
    normalized = normalize_key(key)
    return (
        "sessiontoken" in normalized
        or "securitytoken" in normalized
        or normalized in {"token", "awstoken"}
    )


def mask_secret(value: Any, keep: int = 4) -> str:
    """Return a secret reduced to its length and its last few characters.

    The length is shown rather than one asterisk per character, because a
    session token runs to a couple of thousand characters and a faithful mask
    would bury the rest of the output. The tail is shown rather than the head so
    two secrets can still be told apart in a transcript without printing the
    prefix, which is the half that identifies what kind of credential it is.
    """
    if not isinstance(value, str) or len(value) <= keep:
        return REDACTED
    return f"[{len(value)} chars]{REDACTED}{value[-keep:]}"


def is_private_key(key: str) -> bool:
    """Report whether a key name resembles private key material."""
    normalized = normalize_key(key)
    return "privatekey" in normalized or "sshkey" in normalized


def is_secret_name(key: str) -> bool:
    """Report whether a field name marks a secret value."""
    if is_access_key_id(key):
        return False
    normalized = normalize_key(key)
    return any(fragment in normalized for fragment in SECRET_NAME_FRAGMENTS)


def redact_url(value: str) -> str:
    """Replace sensitive query values in a URL."""
    if "://" not in value or "?" not in value:
        return value
    parts = urllib.parse.urlsplit(value)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(normalize_key(name) in SENSITIVE_QUERY_PARAMS for name, _ in pairs):
        return value
    redacted = [
        (name, REDACTED if normalize_key(name) in SENSITIVE_QUERY_PARAMS else item)
        for name, item in pairs
    ]
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urllib.parse.urlencode(redacted),
            parts.fragment,
        )
    )


def redact_payload(payload: Any, key: str = "") -> Any:
    """Return a copy of a payload with secret values replaced."""
    if isinstance(payload, dict):
        return {name: redact_payload(value, name) for name, value in payload.items()}
    if isinstance(payload, list):
        return [redact_payload(item, key) for item in payload]
    if isinstance(payload, str):
        if is_private_key(key) or (
            "-----BEGIN" in payload and "PRIVATE KEY" in payload
        ):
            return f"{REDACTED} (private key withheld, len={len(payload)})"
        if is_secret_name(key):
            return mask_secret(payload)
        return redact_url(payload)
    if is_private_key(key) or is_secret_name(key):
        return REDACTED
    return payload


def redact_text(body: str) -> str:
    """Redact secrets from an unstructured response body."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        pass
    else:
        return json.dumps(redact_payload(payload), sort_keys=True)

    body = JSON_SECRET_FIELD_PATTERN.sub(rf'\1"{REDACTED}"', body)
    body = PEM_BLOCK_PATTERN.sub(f"{REDACTED} (private key withheld)", body)
    return SENSITIVE_QUERY_PATTERN.sub(rf"\1={REDACTED}", body)


def safe_url(url: str) -> str:
    """Return a URL safe to include in diagnostics."""
    return redact_url(url)
