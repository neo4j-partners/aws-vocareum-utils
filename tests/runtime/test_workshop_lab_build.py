"""Drive `workshop_lab.build` with a fake CodeBuild, offline and without waiting.

The case this file exists for is the one that happens every time: Vocareum stops
the build seconds in, the log stream is empty, and the report has to say the
environment stopped it rather than that the Dockerfile is broken. The second is
the fallback, which is chosen by asking ECR rather than by reading the build
status, so that it stops running by itself if builds ever start working.
"""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from workshop_lab import build as bld
from workshop_lab.build import (
    CODEBUILD_ARM_IMAGE,
    ContainerBuild,
    architecture_from,
    buildspec,
    phase_lines,
    stopped_before_it_started,
)
from workshop_lab.harness import FAIL, PASS, SKIP, Harness
from workshop_lab.naming import PREBUILT_IMAGE, Names

ACCOUNT = "123456789012"
REGION = "us-east-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/workshop-verify-codebuild-role"


def denied(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "explicit deny"}}, operation
    )


def stopped_build(build_id: str = "p:1") -> dict:
    """What batch_get_builds returns for a build Vocareum killed."""
    return {
        "id": build_id,
        "buildStatus": "STOPPED",
        "phases": [
            {"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED"},
            {"phaseType": "PROVISIONING", "phaseStatus": "STOPPED"},
        ],
        "logs": {},
    }


def succeeded_build(architecture: str = "aarch64") -> dict:
    return {
        "id": "p:1",
        "buildStatus": "SUCCEEDED",
        "phases": [{"phaseType": "BUILD", "phaseStatus": "SUCCEEDED"}],
        "logs": {"groupName": "g", "streamName": "s"},
        "_log_lines": [architecture, "pushed"],
    }


class FakeCodeBuild:
    def __init__(self, build: dict | None = None, refuse: set[str] = frozenset()):
        self.calls: list[tuple[str, dict]] = []
        self.build = build if build is not None else stopped_build()
        self.refuse = set(refuse)

    def create_project(self, **kwargs):
        self.calls.append(("create_project", kwargs))
        if "create_project" in self.refuse:
            raise denied("CreateProject")
        return {"project": {"name": kwargs["name"]}}

    def delete_project(self, **kwargs):
        self.calls.append(("delete_project", kwargs))
        return {}

    def start_build(self, **kwargs):
        self.calls.append(("start_build", kwargs))
        if "start_build" in self.refuse:
            raise denied("StartBuild")
        return {"build": {"id": self.build["id"]}}

    def batch_get_builds(self, **kwargs):
        self.calls.append(("batch_get_builds", kwargs))
        return {"builds": [self.build]}


class FakeLogs:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines or []

    def get_log_events(self, **kwargs):
        return {"events": [{"message": line} for line in self.lines]}


class FakeEcr:
    def __init__(self, images: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.images = images if images is not None else []

    def batch_get_image(self, **kwargs):
        self.calls.append(("batch_get_image", kwargs))
        return {"images": self.images}


class FakeCredentials:
    access_key, secret_key, token = "AKIA", "secret", "token"

    def get_frozen_credentials(self):
        return self


class FakeSession:
    def __init__(self, **clients) -> None:
        self.clients = clients
        self.region_name = REGION

    def get_credentials(self):
        return FakeCredentials()

    def client(self, name: str, region_name: str | None = None):
        return self.clients[name]


def lab_with(**clients) -> Harness:
    """A harness past step 2, with its output captured instead of printed."""
    printed: list[str] = []
    lab = Harness(prefix="verify", session=FakeSession(**clients), echo=printed.append)
    lab.account_id = ACCOUNT
    lab._names = Names(prefix="verify", account_id=ACCOUNT, region=REGION)
    lab.printed = printed
    return lab


@pytest.fixture(autouse=True)
def never_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wait_until` polls on a real clock. Nothing here should wait for it."""
    monkeypatch.setattr("workshop_lab.harness.time.sleep", lambda _: None)


def verdicts(lab: Harness) -> dict[str, tuple[str, str]]:
    return {name: (verdict, detail) for name, verdict, detail in lab.results}


# --- the buildspec ------------------------------------------------------------
def test_the_buildspec_runs_the_four_commands_in_order():
    spec = json.loads(buildspec(REGION, "reg.example", "reg.example/repo:verify"))
    commands = [
        command
        for phase in ("pre_build", "build", "post_build")
        for command in spec["phases"][phase]["commands"]
    ]
    assert commands[0] == "uname -m"
    assert "docker login" in commands[1]
    assert commands[2].startswith("docker build")
    assert commands[3].startswith("docker push")


def test_the_buildspec_pushes_the_tag_it_built():
    """A build tagged one thing and pushed another leaves ECR empty and says
    nothing about it."""
    spec = json.loads(buildspec(REGION, "reg.example", "reg.example/repo:verify"))
    assert spec["phases"]["build"]["commands"][0].endswith("reg.example/repo:verify .")
    assert spec["phases"]["post_build"]["commands"] == [
        "docker push reg.example/repo:verify"
    ]


# --- reading a build ----------------------------------------------------------
def test_a_build_stopped_in_provisioning_is_recognised():
    assert stopped_before_it_started(stopped_build()) is True


def test_a_build_stopped_after_a_command_ran_is_not():
    """Stopped during BUILD means the Dockerfile did get its chance."""
    build = stopped_build()
    build["phases"] = [{"phaseType": "BUILD", "phaseStatus": "STOPPED"}]
    assert stopped_before_it_started(build) is False


@pytest.mark.parametrize("status", ["FAILED", "SUCCEEDED", "TIMED_OUT"])
def test_only_a_stopped_build_can_have_been_stopped(status):
    build = stopped_build()
    build["buildStatus"] = status
    assert stopped_before_it_started(build) is False


def test_phase_lines_carry_the_reason_a_phase_gave():
    build = {
        "phases": [
            {
                "phaseType": "PROVISIONING",
                "phaseStatus": "STOPPED",
                "durationInSeconds": 5,
                "contexts": [{"message": "build stopped"}],
            }
        ]
    }
    rendered = phase_lines(build)
    assert "PROVISIONING" in rendered[0]
    assert "5s" in rendered[0]
    assert "build stopped" in rendered[1]


@pytest.mark.parametrize(
    "lines, expected",
    [
        (["aarch64"], "aarch64"),
        (["Reading state", " x86_64 "], "x86_64"),
        ([], "unknown"),
        (["nothing that looks like a machine"], "unknown"),
    ],
)
def test_architecture_is_read_from_the_log_or_reported_unknown(lines, expected):
    assert architecture_from(lines) == expected


# --- the build path -----------------------------------------------------------
def test_no_role_means_no_project_and_no_build_row():
    """There is nothing to say about a build that does not exist, and a FAIL
    would read as a build that ran and lost."""
    lab = lab_with(codebuild=FakeCodeBuild(), logs=FakeLogs(), ecr=FakeEcr())
    ContainerBuild(lab, codebuild_role=None).build()

    assert verdicts(lab) == {
        "codebuild:CreateProject": (SKIP, "no CodeBuild role from step 6")
    }


def test_the_project_asks_for_arm_and_privileged_mode():
    """Privileged mode is what lets a build run Docker, and ARM64 is the only
    architecture AgentCore Runtime accepts."""
    codebuild = FakeCodeBuild()
    lab = lab_with(codebuild=codebuild, logs=FakeLogs(), ecr=FakeEcr())
    ContainerBuild(lab, ROLE).create_project()

    kwargs = next(k for name, k in codebuild.calls if name == "create_project")
    assert kwargs["environment"]["type"] == "ARM_CONTAINER"
    assert kwargs["environment"]["image"] == CODEBUILD_ARM_IMAGE
    assert kwargs["environment"]["privilegedMode"] is True
    assert kwargs["serviceRole"] == ROLE
    assert kwargs["source"]["location"] == (
        f"bedrock-agentcore-codebuild-sources-{ACCOUNT}-{REGION}/verify/source.zip"
    )


def test_the_project_tags_use_codebuilds_lowercase_keys():
    """Every other service here takes Key/Value. CodeBuild takes key/value, and
    the wrong casing is an InvalidInputException that names no field."""
    codebuild = FakeCodeBuild()
    lab = lab_with(codebuild=codebuild, logs=FakeLogs(), ecr=FakeEcr())
    ContainerBuild(lab, ROLE).create_project()

    kwargs = next(k for name, k in codebuild.calls if name == "create_project")
    assert list(kwargs["tags"][0]) == ["key", "value"]


def test_a_created_project_is_registered_for_deletion():
    codebuild = FakeCodeBuild()
    lab = lab_with(codebuild=codebuild, logs=FakeLogs(), ecr=FakeEcr())
    ContainerBuild(lab, ROLE).create_project()

    assert [label for label, _ in lab.cleanups] == [
        "codebuild project bedrock-agentcore-verify-builder"
    ]
    for _, delete in lab.cleanups:
        delete()
    assert ("delete_project", {"name": "bedrock-agentcore-verify-builder"}) in (
        codebuild.calls
    )


def test_a_refused_project_registers_no_cleanup_and_starts_no_build():
    codebuild = FakeCodeBuild(refuse={"create_project"})
    lab = lab_with(codebuild=codebuild, logs=FakeLogs(), ecr=FakeEcr())
    ContainerBuild(lab, ROLE).build()

    assert lab.cleanups == []
    assert "start_build" not in [name for name, _ in codebuild.calls]


# --- what the report says -----------------------------------------------------
def test_a_stopped_build_is_reported_as_the_environment_not_the_dockerfile():
    codebuild = FakeCodeBuild(stopped_build())
    lab = lab_with(codebuild=codebuild, logs=FakeLogs(), ecr=FakeEcr())
    ContainerBuild(lab, ROLE).build()

    build_verdict, detail = verdicts(lab)["codebuild container build"]
    assert build_verdict == FAIL
    assert "PROVISIONING" in detail
    assert verdicts(lab)["build architecture is ARM64"] == (
        SKIP,
        "the build container never started",
    )


def test_a_stopped_build_prints_how_to_confirm_it():
    """The student is being told their build was killed by something they
    cannot see, so the explanation has to be checkable."""
    lab = lab_with(
        codebuild=FakeCodeBuild(stopped_build()), logs=FakeLogs(), ecr=FakeEcr()
    )
    ContainerBuild(lab, ROLE).build()

    printed = "\n".join(lab.printed)
    assert "aws events list-rules --name-prefix voc-codebuild" in printed
    assert "the build wrote no log lines" in printed


def test_a_successful_arm_build_passes_both_rows():
    build = succeeded_build("aarch64")
    lab = lab_with(
        codebuild=FakeCodeBuild(build),
        logs=FakeLogs(build["_log_lines"]),
        ecr=FakeEcr(),
    )
    ContainerBuild(lab, ROLE).build()

    assert verdicts(lab)["codebuild container build"] == (PASS, "uname -m: aarch64")
    assert verdicts(lab)["build architecture is ARM64"] == (PASS, "aarch64")


def test_a_successful_x86_build_fails_the_architecture_row():
    """The build worked and the image still will not run on AgentCore."""
    build = succeeded_build("x86_64")
    lab = lab_with(
        codebuild=FakeCodeBuild(build),
        logs=FakeLogs(build["_log_lines"]),
        ecr=FakeEcr(),
    )
    ContainerBuild(lab, ROLE).build()

    assert verdicts(lab)["codebuild container build"][0] == PASS
    architecture_verdict, detail = verdicts(lab)["build architecture is ARM64"]
    assert architecture_verdict == FAIL
    assert "aarch64" in detail


def test_an_unreadable_log_does_not_fail_the_step():
    """The log is evidence, not the measurement. Losing it loses the detail."""

    class RefusingLogs:
        def get_log_events(self, **kwargs):
            raise denied("GetLogEvents")

    build = succeeded_build()
    lab = lab_with(codebuild=FakeCodeBuild(build), logs=RefusingLogs(), ecr=FakeEcr())
    ContainerBuild(lab, ROLE).build()

    assert verdicts(lab)["codebuild container build"] == (PASS, "uname -m: unknown")


# --- the fallback -------------------------------------------------------------
def test_an_image_already_in_ecr_skips_the_copy(monkeypatch: pytest.MonkeyPatch):
    """This is what makes the fallback self-healing. If Vocareum ever stops
    killing builds, the copy stops running with no change here."""
    called = []
    monkeypatch.setattr(bld, "copy_image", lambda *a, **k: called.append(a))
    ecr = FakeEcr(images=[{"imageId": {"imageTag": "verify"}}])
    lab = lab_with(codebuild=FakeCodeBuild(), logs=FakeLogs(), ecr=ecr)

    assert ContainerBuild(lab, ROLE).ensure_image() is True
    assert called == []
    assert verdicts(lab)["image present in ECR"][0] == PASS


def test_the_lookup_asks_for_the_tag_the_build_pushes():
    ecr = FakeEcr(images=[{"imageId": {}}])
    lab = lab_with(codebuild=FakeCodeBuild(), logs=FakeLogs(), ecr=ecr)
    ContainerBuild(lab, ROLE).image_in_ecr()

    kwargs = next(k for name, k in ecr.calls if name == "batch_get_image")
    assert kwargs["repositoryName"] == "bedrock-agentcore-verify"
    assert kwargs["imageIds"] == [{"imageTag": "verify"}]


def test_an_empty_repository_triggers_the_copy(monkeypatch: pytest.MonkeyPatch):
    copied = []
    monkeypatch.setattr(
        bld,
        "copy_image",
        lambda source, dest, ecr, record: copied.append((source, dest)),
    )
    lab = lab_with(codebuild=FakeCodeBuild(), logs=FakeLogs(), ecr=FakeEcr(images=[]))

    assert ContainerBuild(lab, ROLE).ensure_image() is True
    assert copied == [(PREBUILT_IMAGE, lab.names.image_uri)]


def test_the_copy_source_is_public_and_the_destination_is_this_account(
    monkeypatch: pytest.MonkeyPatch,
):
    """AgentCore Runtime pulls only from ECR in the caller's own account, so
    pointing it at the public copy would fail at Runtime creation instead."""
    copied = []
    monkeypatch.setattr(
        bld,
        "copy_image",
        lambda source, dest, ecr, record: copied.append((source, dest)),
    )
    lab = lab_with(codebuild=FakeCodeBuild(), logs=FakeLogs(), ecr=FakeEcr(images=[]))
    ContainerBuild(lab, ROLE).ensure_image()

    source, destination = copied[0]
    assert source.startswith("public.ecr.aws/")
    assert destination.startswith(f"{ACCOUNT}.dkr.ecr.")


def test_a_failed_copy_fails_the_image_row(monkeypatch: pytest.MonkeyPatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("the source image has no linux/arm64 manifest")

    monkeypatch.setattr(bld, "copy_image", refuse)
    lab = lab_with(codebuild=FakeCodeBuild(), logs=FakeLogs(), ecr=FakeEcr(images=[]))

    assert ContainerBuild(lab, ROLE).ensure_image() is False
    image_verdict, detail = verdicts(lab)["image present in ECR"]
    assert image_verdict == FAIL
    assert "neither CodeBuild nor the registry copy" in detail


def test_the_copy_runs_even_when_the_build_never_started(
    monkeypatch: pytest.MonkeyPatch,
):
    """No role from step 6 is the case the fallback matters most in, and the
    image still has to get into ECR somehow."""
    copied = []
    monkeypatch.setattr(bld, "copy_image", lambda *a, **k: copied.append(a))
    lab = lab_with(codebuild=FakeCodeBuild(), logs=FakeLogs(), ecr=FakeEcr(images=[]))

    assert ContainerBuild(lab, codebuild_role=None).run() is True
    assert len(copied) == 1
