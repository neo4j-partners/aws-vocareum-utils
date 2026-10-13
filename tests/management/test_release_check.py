"""Test the part of the release check that can be tested offline.

`scripts/release_check.py` exists because most of what it does cannot be a test:
it shells out to git, builds a wheel, makes a virtualenv, and with `--fetch`
downloads from GitHub. What *can* be tested is the judgement it applies to what
those steps return, and that is the part with the rules in it: which refs are
allowed, and what a tag has to agree with.

The rest of this file guards against the failure mode a hand-run script has and
a test does not. Nobody runs `release_check.py` on a normal day, so a typo in it
sits undiscovered until the one moment it is being relied on. Importing it here
means the suite at least proves it parses, that its helpers read the real
notebook, and that its copy of the archive regex still matches the one in
`test_notebook_install_url.py`, which is the file that would otherwise be the
only thing keeping the URL honest.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from test_notebook_install_url import ARCHIVE as TEST_ARCHIVE
from test_notebook_install_url import notebook_url

from workshop_lab import __version__

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_check.py"


def load_release_check():
    """Import the release script by path. It is a script, not a package module."""
    spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release = load_release_check()

VERSION = "1.2.3"
TAGS = ["v1.2.2", "v1.2.3"]


# The ref rule, which is the whole reason this script decides anything.


def test_a_branch_ref_is_a_finding_and_says_what_to_do() -> None:
    """`refs/heads/main` is the working default and a teaching hazard, so the
    message has to carry the remedy rather than just the verdict."""
    (problem,) = release.check_ref("refs/heads/main", VERSION, TAGS)
    assert "moves" in problem
    assert f"refs/tags/v{VERSION}" in problem


def test_a_tag_that_exists_and_matches_the_version_is_clean() -> None:
    assert release.check_ref(f"refs/tags/v{VERSION}", VERSION, TAGS) == []


def test_a_tag_naming_another_release_is_a_finding() -> None:
    """Step 0 prints `__version__` from the installed package. A notebook pinned
    to v1.2.2 while the source says 1.2.3 means the printed line and the code a
    student is running name different releases, which is the exact signal that
    line exists to give."""
    problems = release.check_ref("refs/tags/v1.2.2", VERSION, TAGS)
    assert len(problems) == 1
    assert "1.2.2" in problems[0] and VERSION in problems[0]


def test_a_tag_that_does_not_exist_yet_is_a_finding() -> None:
    """The pin-before-teaching edit is three files and a `git push --tags`, and
    doing the first without the second gives every student a 404."""
    problems = release.check_ref(f"refs/tags/v{VERSION}", VERSION, ["v1.2.2"])
    assert len(problems) == 1
    assert "404" in problems[0]


def test_a_tag_that_neither_exists_nor_matches_reports_both() -> None:
    """Two independent mistakes, and fixing the one the script happened to
    mention first would leave the other to be found in a classroom."""
    assert len(release.check_ref("refs/tags/v9.9.9", VERSION, TAGS)) == 2


def test_a_ref_that_is_neither_shape_is_rejected() -> None:
    """A bare `main` or a commit sha resolves for pip and pins nothing a human
    can reason about."""
    (problem,) = release.check_ref("main", VERSION, TAGS)
    assert "expected" in problem


# The script and the test agree about the notebook.


def test_the_script_reads_the_same_url_the_suite_holds() -> None:
    """Two copies of the archive regex, one here and one in the script, because
    the script has to run standalone with no import from `tests/`. This is what
    keeps them from drifting into disagreeing about what a valid URL is."""
    assert release.install_url() == notebook_url()


def test_both_regexes_accept_a_pinned_url_the_other_way_round() -> None:
    """Equality above would still hold if both patterns were broken in the same
    direction. This drives a URL neither has seen through both."""
    pinned = (
        "https://github.com/neo4j-partners/neo4j-sec-filings-graphrag-workshop"
        "/archive/refs/tags/v9.9.9.zip#subdirectory=staging"
    )
    assert TEST_ARCHIVE.fullmatch(pinned) is not None
    assert release.ARCHIVE.fullmatch(pinned) is not None
    assert release.ref_of(pinned) == "refs/tags/v9.9.9"


def test_the_script_reads_the_version_the_package_declares() -> None:
    """Read out of `__init__.py` rather than imported, so the script needs no
    boto3. A parse that silently returned the wrong string would make every
    version comparison here vacuous."""
    assert release.package_version() == __version__


def test_a_ref_that_is_not_a_url_fails_loudly() -> None:
    with pytest.raises(SystemExit):
        release.ref_of("refs/tags/v1.0.0")


def test_gate_command_is_always_inspect_only() -> None:
    command = release.gate_command(Path("/tmp/course.env"), "graphrag", "us-east-1")

    assert command[:3] == ["uv", "run", "vocareum-expect"]
    assert "--no-start" in command
    assert command[command.index("--course") + 1] == "graphrag"
    assert command[command.index("--region") + 1] == "us-east-1"


@pytest.mark.parametrize("returncode", [1, 2, 3])
def test_gate_failure_becomes_a_release_problem(
    returncode: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], returncode),
    )

    problems = release.check_gate(Path(".env"), "graphrag")

    assert len(problems) == 1
    assert str(returncode) in problems[0]


def test_gate_success_adds_no_release_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def succeed(*args, **kwargs):
        called.update(kwargs)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setenv("VIRTUAL_ENV", "/temporary/inline-environment")
    monkeypatch.setattr(release.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(release.subprocess, "run", succeed)

    assert release.check_gate(Path(".env")) == []
    assert "VIRTUAL_ENV" not in called["env"]


def test_gate_reports_when_uv_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release.shutil, "which", lambda name: None)

    problems = release.check_gate(Path(".env"))

    assert problems == ["uv is not on PATH, so the inspect-only ship gate cannot run"]
