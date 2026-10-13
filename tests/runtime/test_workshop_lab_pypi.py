"""Offline tests for step 13, the PyPI reachability check.

No installer runs here. `subprocess.run` is replaced, because a test that
actually reaches PyPI is a test that fails on a plane.

What is worth testing is the reporting. This step turns a pip resolver error
into one sentence a student can act on, and the failure mode it guards against
is a message that names pip's own upgrade advice instead of the cause.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from workshop_lab.harness import FAIL, PASS, Harness
from workshop_lab.pypi import (
    NO_PIP_DETAIL,
    PYPI_CHECK,
    WORKSHOP_REQUIREMENT,
    PyPIReachable,
    failure_detail,
    pip_install,
    try_install,
)

REGION = "us-east-1"


class FakeSession:
    region_name = REGION

    def client(self, name: str, region_name: str | None = None) -> Any:
        raise AssertionError("step 13 makes no AWS call")


@pytest.fixture
def lab() -> Harness:
    printed: list[str] = []
    harness = Harness(session=FakeSession(), echo=printed.append)
    harness.printed = printed  # type: ignore[attr-defined]
    return harness


def done(code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=stdout, stderr=stderr
    )


def arrange(
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess | Exception,
) -> list[list[str]]:
    """Script what the installer does and capture what was run."""
    ran: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        ran.append(command)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("workshop_lab.pypi.subprocess.run", fake_run)
    return ran


def verdicts(lab: Harness) -> dict[str, str]:
    return {name: verdict for name, verdict, _ in lab.results}


def detail(lab: Harness, name: str) -> str:
    return next(text for row, _, text in lab.results if row == name)


# --- the command ----------------------------------------------------------


def test_the_command_asks_for_the_workshops_own_package() -> None:
    """An index carrying a subset of PyPI passes a placeholder and breaks a lab."""
    command = pip_install("/tmp/target")

    assert command[1:4] == ["-m", "pip", "install"]
    assert command[command.index("--target") + 1] == "/tmp/target"
    assert command[-1] == WORKSHOP_REQUIREMENT
    assert WORKSHOP_REQUIREMENT.startswith("neo4j-graphrag")


def test_the_command_skips_the_dependency_tree() -> None:
    """The question is reachability. Resolving the full tree takes minutes."""
    assert "--no-deps" in pip_install("/tmp/target")


def test_the_command_installs_nowhere_permanent() -> None:
    """A step that leaves a package behind changes what later cells import."""
    assert "--target" in pip_install("/tmp/target")


# --- the failure line -----------------------------------------------------


def test_the_reported_line_is_the_error_not_pips_upgrade_notice() -> None:
    """pip prints its own advice after the error, so the last line is routinely it.

    Reporting that as the reason PyPI is unreachable is worse than reporting
    nothing.
    """
    output = (
        "Collecting neo4j-graphrag\n"
        "ERROR: Could not find a version that satisfies the requirement\n"
        "[notice] A new release of pip is available: 21.2.4 -> 25.0\n"
        "[notice] To update, run: pip install --upgrade pip\n"
    )

    assert failure_detail(done(1, stdout=output)).startswith("ERROR: Could not")


def test_the_last_line_is_used_when_nothing_announces_itself_as_an_error() -> None:
    assert failure_detail(done(2, stderr="x failed\nno solution found\n")) == (
        "no solution found"
    )


def test_a_silent_failure_still_names_the_tool_and_the_code() -> None:
    """An empty report reads as a check that did not run."""
    assert failure_detail(done(3)) == "pip exited 3"


def test_the_reported_line_is_truncated() -> None:
    """A result row is read in a terminal and a resolver can print kilobytes."""
    assert len(failure_detail(done(1, stderr="ERROR: " + "x" * 5000))) == 160


# --- try_install ----------------------------------------------------------


def test_a_missing_binary_says_so_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess raises a bare FileNotFoundError that names nothing."""
    arrange(monkeypatch, FileNotFoundError())

    assert try_install() == (False, "pip is not on PATH")


def test_a_hung_install_is_a_failure_with_a_time_in_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked index typically hangs rather than refusing."""
    arrange(monkeypatch, subprocess.TimeoutExpired("pip", 300))

    ok, text = try_install()

    assert not ok
    assert "did not finish within 300s" in text


def test_a_successful_install_names_what_it_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arrange(monkeypatch, done(0))

    ok, text = try_install()

    assert ok
    assert WORKSHOP_REQUIREMENT in text


def test_an_interpreter_without_pip_is_not_reported_as_a_blocked_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It exits 1 like a resolver failure, and nothing reached the network.

    A uv-managed virtualenv is the ordinary way to hit this. Reporting it as an
    unreachable PyPI sends someone to ask about the lab network for a problem
    that is on their own machine.
    """
    arrange(monkeypatch, done(1, stderr="/x/bin/python3: No module named pip\n"))

    assert try_install() == (False, NO_PIP_DETAIL)


# --- the step -------------------------------------------------------------


def test_a_reachable_index_passes_and_says_nothing_further(
    lab: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result row is the whole report. Advice belongs to the failure only."""
    arrange(monkeypatch, done(0))

    assert PyPIReachable(lab).run() is True
    assert verdicts(lab) == {PYPI_CHECK: PASS}
    assert "pip install" not in "\n".join(lab.printed)  # type: ignore[attr-defined]


def test_an_unreachable_index_reports_that_no_lab_can_install(
    lab: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every lab notebook opens with a pip install, so this stops all of them."""
    arrange(monkeypatch, done(1, stderr="ERROR: no network"))

    assert PyPIReachable(lab).run() is False
    assert verdicts(lab) == {PYPI_CHECK: FAIL}
    assert detail(lab, PYPI_CHECK) == "ERROR: no network"
    assert "pip install" in "\n".join(lab.printed)  # type: ignore[attr-defined]


def test_a_missing_pip_points_at_this_machine_not_at_the_lab_network(
    lab: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two failures need two different sentences, or one gets misreported."""
    arrange(monkeypatch, done(1, stderr="/x/bin/python3: No module named pip\n"))

    assert PyPIReachable(lab).run() is False
    assert detail(lab, PYPI_CHECK) == NO_PIP_DETAIL
    printed = "\n".join(lab.printed)  # type: ignore[attr-defined]
    assert "not PyPI" in printed
    assert "No lab in this workshop" not in printed


def test_the_step_makes_no_aws_call(
    lab: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake session raises on `client`, so this passing is the assertion."""
    arrange(monkeypatch, done(0))

    PyPIReachable(lab).run()
