"""Drive `workshop_lab.registry` offline, without a registry and without waiting.

These were written when the same code lived in two files, one of them a
generated notebook cell, and every case ran against both copies. The copy is
gone: `workshop_lab` is installed into the student's environment now, so there is
one body of code and these run against it directly.

`request_with_retry` takes its sleep and its jitter as arguments precisely so the
backoff can be measured without waiting for it.
"""

from __future__ import annotations

import email.message
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from workshop_lab import registry as reg

# ---------------------------------------------------------------------------
# Case tables
# ---------------------------------------------------------------------------

REFERENCE_CASES = [
    (
        "public.ecr.aws/h6l4p0n8/agentcore-verify:verify",
        ("public.ecr.aws", "h6l4p0n8/agentcore-verify", "verify"),
    ),
    (
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/bedrock-agentcore-verify:verify",
        (
            "123456789012.dkr.ecr.us-east-1.amazonaws.com",
            "bedrock-agentcore-verify",
            "verify",
        ),
    ),
    ("registry.example/a/b/c:1.2.3", ("registry.example", "a/b/c", "1.2.3")),
    ("localhost:5000/repo:tag", ("localhost:5000", "repo", "tag")),
]

# Every one of these is a reference the copy must refuse rather than guess at.
BAD_REFERENCE_CASES = [
    "public.ecr.aws/h6l4p0n8/agentcore-verify",
    "localhost:5000/repo",
    "verify:latest",
    ":verify",
    "",
]

DIGEST_CASES = [
    ({"config": {"digest": "sha256:only"}, "layers": []}, ["sha256:only"]),
    (
        {
            "config": {"digest": "sha256:config"},
            "layers": [{"digest": "sha256:one"}, {"digest": "sha256:two"}],
        },
        ["sha256:config", "sha256:one", "sha256:two"],
    ),
]

ARM64 = {"os": "linux", "architecture": "arm64"}
AMD64 = {"os": "linux", "architecture": "amd64"}
DARWIN_ARM64 = {"os": "darwin", "architecture": "arm64"}

PLATFORM_CASES = [
    (
        {
            "manifests": [
                {"digest": "sha256:amd", "platform": AMD64},
                {"digest": "sha256:arm", "platform": ARM64},
            ]
        },
        "sha256:arm",
    ),
    (
        {
            "manifests": [
                {"digest": "sha256:arm", "platform": ARM64},
                {"digest": "sha256:amd", "platform": AMD64},
            ]
        },
        "sha256:arm",
    ),
]

# The architecture alone is not enough. A darwin/arm64 entry would run nowhere
# on AgentCore, and picking it would fail at Runtime creation instead of here.
NO_PLATFORM_CASES = [
    {"manifests": [{"digest": "sha256:mac", "platform": DARWIN_ARM64}]},
    {"manifests": [{"digest": "sha256:amd", "platform": AMD64}]},
    {"manifests": [{"digest": "sha256:bare"}]},
    {"manifests": []},
    {},
]


def http_error(code: int) -> urllib.error.HTTPError:
    """Build an HTTPError the way urllib delivers one."""
    return urllib.error.HTTPError(
        "https://public.ecr.aws/v2/ns/repo/manifests/verify",
        code,
        "from the test",
        email.message.Message(),
        None,
    )


class ScriptedRegistry:
    """Answer a fixed sequence of HTTP statuses and count the calls.

    A status of 200 returns a manifest, anything else raises the HTTPError
    urllib would raise. Running past the end of the script is an error rather
    than a repeat, so a test that expected three calls and got four says so.
    """

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.urls: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.urls)

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        tolerate: tuple[int, ...] = (),
    ) -> tuple[int, bytes, dict[str, str]]:
        if self.calls >= len(self.statuses):
            raise AssertionError(f"unscripted call {self.calls + 1} to {url}")
        status = self.statuses[self.calls]
        self.urls.append(url)
        if status != 200:
            raise http_error(status)
        return (
            200,
            json.dumps({"chosen": url}).encode(),
            {"Content-Type": "application/vnd.oci.image.manifest.v1+json"},
        )


@pytest.fixture
def no_waiting() -> dict[str, Any]:
    """Backoff arguments that record the delays instead of sleeping them."""
    slept: list[float] = []
    return {
        "slept": slept,
        "sleep": slept.append,
        "jitter": lambda low, high: 1.0,
    }


# ---------------------------------------------------------------------------
# The pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("reference", "expected"), REFERENCE_CASES)
def test_parse_reference(reference: str, expected: tuple[str, str, str]) -> None:
    assert reg.parse_reference(reference) == expected


@pytest.mark.parametrize("reference", BAD_REFERENCE_CASES)
def test_parse_reference_refuses_what_it_cannot_read(reference: str) -> None:
    with pytest.raises(ValueError):
        reg.parse_reference(reference)


@pytest.mark.parametrize(("manifest", "expected"), DIGEST_CASES)
def test_referenced_digests_yields_the_config_first(
    manifest: dict[str, Any], expected: list[str]
) -> None:
    """Push order matters. A manifest naming a config ECR does not hold is
    rejected, so the config goes up before the layers that reference it."""
    assert list(reg.referenced_digests(manifest)) == expected


@pytest.mark.parametrize(("index", "expected"), PLATFORM_CASES)
def test_select_platform_manifest_picks_linux_arm64(
    monkeypatch: pytest.MonkeyPatch, index: dict[str, Any], expected: str
) -> None:
    scripted = ScriptedRegistry([200])
    monkeypatch.setattr(reg, "request", scripted)
    source = reg.Registry("public.ecr.aws", "ns/repo", "Bearer t")

    body, media_type = reg.select_platform_manifest(source, json.dumps(index).encode())

    assert scripted.urls == [f"https://public.ecr.aws/v2/ns/repo/manifests/{expected}"]
    assert json.loads(body)["chosen"].endswith(expected)
    assert media_type == "application/vnd.oci.image.manifest.v1+json"


@pytest.mark.parametrize("index", NO_PLATFORM_CASES)
def test_select_platform_manifest_refuses_the_wrong_platform(
    monkeypatch: pytest.MonkeyPatch, index: dict[str, Any]
) -> None:
    scripted = ScriptedRegistry([])
    monkeypatch.setattr(reg, "request", scripted)
    source = reg.Registry("public.ecr.aws", "ns/repo", "Bearer t")

    with pytest.raises(RuntimeError):
        reg.select_platform_manifest(source, json.dumps(index).encode())

    assert scripted.calls == 0


def test_absolute_leaves_a_full_url_alone() -> None:
    assert reg.absolute("host", "https://other/upload") == "https://other/upload"
    assert reg.absolute("host", "/v2/repo/uploads/1") == (
        "https://host/v2/repo/uploads/1"
    )


# ---------------------------------------------------------------------------
# The manifest retry
#
# Measured 2026-08-06: unauthenticated pulls from public.ecr.aws are capped at
# 1 per second per source IP, and a 30-way burst from one IP had 9 of 30
# refused with HTTP 429, every one of them on the manifest GET. A class of
# thirty students may share one egress IP, so this retry is the difference
# between a lab that works and a lab that does not.
# ---------------------------------------------------------------------------


def test_the_manifest_get_retries_a_429_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_waiting: dict[str, Any]
) -> None:
    scripted = ScriptedRegistry([429, 429, 200])
    monkeypatch.setattr(reg, "request", scripted)

    status, _, _ = reg.request_with_retry(
        "GET",
        "https://public.ecr.aws/v2/ns/repo/manifests/verify",
        sleep=no_waiting["sleep"],
        jitter=no_waiting["jitter"],
    )

    assert status == 200
    assert scripted.calls == 3
    assert no_waiting["slept"] == [2, 4]


def test_the_manifest_get_backs_off_further_each_time(
    monkeypatch: pytest.MonkeyPatch, no_waiting: dict[str, Any]
) -> None:
    """Exponential, and capped, so a long outage does not sleep for an hour."""
    monkeypatch.setattr(reg, "request", ScriptedRegistry([429] * 7 + [200]))

    reg.request_with_retry(
        "GET",
        "https://public.ecr.aws/v2/ns/repo/manifests/verify",
        attempts=8,
        sleep=no_waiting["sleep"],
        jitter=no_waiting["jitter"],
    )

    delays = no_waiting["slept"]
    assert delays == sorted(delays)
    assert max(delays) <= reg.MAX_BACKOFF_SECONDS


def test_the_manifest_get_jitters_its_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thirty clients throttled together must not retry together."""
    monkeypatch.setattr(reg, "request", ScriptedRegistry([429, 200]))
    slept: list[float] = []

    reg.request_with_retry(
        "GET",
        "https://public.ecr.aws/v2/ns/repo/manifests/verify",
        sleep=slept.append,
        jitter=lambda low, high: high,
    )

    assert slept == [3.0]


def test_the_manifest_get_does_not_retry_a_404(
    monkeypatch: pytest.MonkeyPatch, no_waiting: dict[str, Any]
) -> None:
    """A missing tag is missing on the sixth attempt too.

    Retrying a 4xx turns a clear answer into a slow one, and the student is
    left watching a notebook that looks busy and is not.
    """
    scripted = ScriptedRegistry([404, 200])
    monkeypatch.setattr(reg, "request", scripted)

    with pytest.raises(urllib.error.HTTPError) as raised:
        reg.request_with_retry(
            "GET",
            "https://public.ecr.aws/v2/ns/repo/manifests/nope",
            sleep=no_waiting["sleep"],
            jitter=no_waiting["jitter"],
        )

    assert raised.value.code == 404
    assert scripted.calls == 1
    assert no_waiting["slept"] == []


@pytest.mark.parametrize("code", [400, 401, 403, 404, 405])
def test_the_manifest_get_retries_no_other_client_error(
    monkeypatch: pytest.MonkeyPatch, no_waiting: dict[str, Any], code: int
) -> None:
    scripted = ScriptedRegistry([code, 200])
    monkeypatch.setattr(reg, "request", scripted)

    with pytest.raises(urllib.error.HTTPError):
        reg.request_with_retry(
            "GET",
            "https://public.ecr.aws/v2/ns/repo/manifests/verify",
            sleep=no_waiting["sleep"],
            jitter=no_waiting["jitter"],
        )

    assert scripted.calls == 1


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_the_manifest_get_retries_a_server_fault(
    monkeypatch: pytest.MonkeyPatch, no_waiting: dict[str, Any], code: int
) -> None:
    """A 5xx says nothing about the request, so it is worth asking again."""
    scripted = ScriptedRegistry([code, 200])
    monkeypatch.setattr(reg, "request", scripted)

    status, _, _ = reg.request_with_retry(
        "GET",
        "https://public.ecr.aws/v2/ns/repo/manifests/verify",
        sleep=no_waiting["sleep"],
        jitter=no_waiting["jitter"],
    )

    assert status == 200
    assert scripted.calls == 2


def test_the_manifest_get_gives_up_and_raises(
    monkeypatch: pytest.MonkeyPatch, no_waiting: dict[str, Any]
) -> None:
    """A throttle that never lifts has to surface, not loop."""
    scripted = ScriptedRegistry([429, 429, 429])
    monkeypatch.setattr(reg, "request", scripted)

    with pytest.raises(urllib.error.HTTPError) as raised:
        reg.request_with_retry(
            "GET",
            "https://public.ecr.aws/v2/ns/repo/manifests/verify",
            attempts=3,
            sleep=no_waiting["sleep"],
            jitter=no_waiting["jitter"],
        )

    assert raised.value.code == 429
    assert scripted.calls == 3


def test_the_copy_puts_the_manifest_get_behind_the_retry() -> None:
    """The bug this fixed was a bare `request` call, and it read fine.

    Behaviour cannot catch a regression here without a full fake registry, so
    the shape is pinned instead: `copy_image` reads its manifest through
    `request_with_retry`, and `select_platform_manifest` does the same for the
    second read a multi-platform index costs.
    """
    source = Path(reg.__file__).read_text(encoding="utf-8")
    body = source.split("def copy_image(", 1)[1]

    assert 'request_with_retry(\n        "GET",' in body
    assert 'request_with_retry(\n                "GET",' in source
