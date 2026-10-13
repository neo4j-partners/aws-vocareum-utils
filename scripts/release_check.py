#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# ///
"""Check that what a student will install is what this repository holds.

`uv run pytest` proves the package works *here*. It imports `workshop_lab` out
of `src/`, which is the copy no student ever runs. What a student runs is a
GitHub archive of a different repository, built by pip, at whatever ref the
notebook names. Four things sit between the two, and each has failed at least
once somewhere:

1. the published copy under `../neo4j-sec-filings-graphrag-workshop/staging` is
   stale, because the sync was never run;
2. it was synced but never committed, so the remote still serves the old files;
3. it was committed but never pushed;
4. the generated `pyproject.toml` does not actually build, or builds a package
   that cannot be imported, which no test in this repository would notice
   because no test in this repository installs it.

This is the check for all four, plus the ref. It is a script and not a test on
purpose. It shells out to git, builds a wheel, makes a virtualenv, and with
`--fetch` downloads from GitHub, none of which belongs in a suite that has to
run offline. Run it by hand before a class:

    ./scripts/release_check.py                # everything local
    ./scripts/release_check.py --fetch        # also download the pinned ref
    ./scripts/release_check.py --gate --course graphrag
    ./scripts/release_check.py --skip-build   # ref and publication only, fast

Exit 0 means the ref a student installs, the files behind it, and the version
step 0 prints all agree. Anything else prints what to do and exits 1.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = REPOSITORY_ROOT / "scripts" / "sync_workshop_lab.py"
NOTEBOOK = REPOSITORY_ROOT / "notebooks" / "lab-01-verify-environment.ipynb"
SOURCE = REPOSITORY_ROOT / "src" / "workshop_lab"
DEFAULT_TARGET = REPOSITORY_ROOT.parent / "neo4j-sec-filings-graphrag-workshop"

ARCHIVE = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
    r"/archive/(?P<ref>[\w./-]+)\.zip#subdirectory=(?P<subdir>\w+)"
)


def load_sync() -> ModuleType:
    """Import `sync_workshop_lab.py` by path, for its plan and its diff.

    Reused rather than reimplemented: "is the published copy current" has to
    mean the same thing here as it does in `--check` and in the drift test, or
    a release passes a check the drift test would have failed.
    """
    spec = importlib.util.spec_from_file_location("sync_workshop_lab", SYNC_SCRIPT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import {SYNC_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def package_version() -> str:
    """`__version__` from the source package, read rather than imported."""
    for line in (SOURCE / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("src/workshop_lab/__init__.py declares no __version__")


def install_url() -> str:
    """The URL out of the notebook cell a student runs.

    The notebook is the only one of the three copies that matters here, because
    it is the one that executes.
    `tests/management/test_notebook_install_url.py` keeps the README and the
    package docstring saying the same thing.
    """
    cells = json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
    source = "".join(
        line
        for cell in cells
        if cell["cell_type"] == "code"
        for line in cell["source"]
        if "%pip install" in line
    )
    found = ARCHIVE.findall(source)
    if len(found) != 1:
        raise SystemExit(f"expected one install URL in the notebook, got {len(found)}")
    match = ARCHIVE.search(source)
    assert match is not None
    return match.group(0)


def ref_of(url: str) -> str:
    """The git ref an archive URL names."""
    match = ARCHIVE.fullmatch(url)
    if match is None:
        raise SystemExit(f"not an archive URL: {url}")
    return match["ref"]


def git(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in the target checkout, without raising."""
    return subprocess.run(
        ["git", "-C", str(target), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def check_ref(ref: str, version: str, tags: list[str]) -> list[str]:
    """Say what is wrong with the ref a student would install.

    A branch is the finding, not an error: `refs/heads/main` is the working
    default and is exactly what must not be in front of a class, because a push
    mid-session changes the code under the students already in it.

    A tag has to exist and has to be `v<version>`. A tag that names a different
    version means step 0 prints one release while installing another, and the
    printed line is the only signal a student has about which code they are on.
    """
    if ref == "refs/heads/main":
        return [
            (
                "the notebook installs refs/heads/main, which moves. Tag the "
                f"workshop repository v{version} and pin refs/tags/v{version} "
                "before teaching."
            )
        ]
    if not ref.startswith("refs/tags/"):
        return [f"unrecognised ref {ref}: expected refs/heads/main or refs/tags/<tag>"]
    tag = ref.removeprefix("refs/tags/")
    problems = []
    if tag != f"v{version}":
        problems.append(
            f"the notebook pins {tag} and the package declares {version}. "
            f"Step 0 would print a version the pinned code is not."
        )
    if tag not in tags:
        problems.append(
            f"tag {tag} does not exist in the workshop repository. "
            f"Students would get a 404 from step 0."
        )
    return problems


def check_publication(target: Path) -> list[str]:
    """Published, committed, and pushed, which are three separate claims."""
    sync = load_sync()
    staging = target / "staging"
    problems = []

    wanted, stale = sync.plan(staging)
    drift = sync.differences(wanted, stale)
    if drift:
        problems.append(
            f"{staging} is stale against src/workshop_lab: {'; '.join(drift)}. "
            "Run ./scripts/sync_workshop_lab.py"
        )

    status = git(target, "status", "--porcelain")
    if status.returncode != 0:
        return [*problems, f"{target} is not a git checkout: {status.stderr.strip()}"]
    if status.stdout.strip():
        # Porcelain is a two-column status, a space, then the path. Sliced off
        # the raw lines: stripping the output first would eat the leading space
        # of an unstaged-modification line and take the path's first letter too.
        changed = ", ".join(line[3:] for line in status.stdout.splitlines()[:5])
        problems.append(
            f"{target} has uncommitted changes ({changed}). The archive endpoint "
            "serves commits, so an uncommitted sync reaches nobody."
        )

    ahead = git(target, "rev-list", "--count", "@{upstream}..HEAD")
    if ahead.returncode != 0:
        problems.append(
            f"{target} has no upstream to compare against: {ahead.stderr.strip()}"
        )
    elif ahead.stdout.strip() not in {"", "0"}:
        problems.append(
            f"{target} is {ahead.stdout.strip()} commit(s) ahead of its upstream. "
            "Push, or the pinned ref will not carry these files."
        )
    return problems


def check_build(target: Path) -> list[str]:
    """Build the generated package, install it, import it out of the venv.

    The import is the point. `uv run pytest` imports `workshop_lab` from
    `src/`, so nothing in this repository has ever executed the copy pip
    builds, and the file that copy is built by (`pyproject.toml`) is generated
    by a script with no test of its own. This asks the only question that
    matters about it: does the thing a student installs import, and is it the
    installed one rather than this checkout's.
    """
    staging = target / "staging"
    if not (staging / "pyproject.toml").exists():
        return [f"{staging}/pyproject.toml does not exist; nothing to build"]
    if shutil.which("uv") is None:
        return ["uv is not on PATH, so the build check cannot run"]

    with tempfile.TemporaryDirectory() as workspace:
        work = Path(workspace)
        # Built from a copy, never in place. setuptools writes `build/` and an
        # `.egg-info` beside the sources it is given, and this check must not
        # leave debris in the repository it is checking: the first run of it did
        # exactly that, and the --fetch comparison then reported seventeen
        # phantom files that were only its own output.
        source = work / "staging"
        debris = shutil.ignore_patterns("build", "dist", "*.egg-info")
        shutil.copytree(staging, source, ignore=debris)
        build = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(work), str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        if build.returncode != 0:
            return [f"uv build failed for {staging}: {build.stderr.strip()[:400]}"]

        wheels = sorted(work.glob("*.whl"))
        if len(wheels) != 1:
            return [f"expected one wheel, got {[path.name for path in wheels]}"]

        venv = work / "venv"
        made = subprocess.run(
            ["uv", "venv", str(venv)], capture_output=True, text=True, check=False
        )
        if made.returncode != 0:
            return [f"uv venv failed: {made.stderr.strip()[:400]}"]

        python = venv / "bin" / "python"
        installed = subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheels[0])],
            capture_output=True,
            text=True,
            check=False,
        )
        if installed.returncode != 0:
            return [f"installing {wheels[0].name} failed: {installed.stderr[:400]}"]

        script = (
            "import json, workshop_lab; "
            "print(json.dumps({'version': workshop_lab.__version__, "
            "'file': workshop_lab.__file__}))"
        )
        imported = subprocess.run(
            [str(python), "-c", script], capture_output=True, text=True, check=False
        )
        if imported.returncode != 0:
            return [f"importing the installed package failed: {imported.stderr[:400]}"]

        report = json.loads(imported.stdout)
        problems = []
        if report["version"] != package_version():
            problems.append(
                f"the built package imports as {report['version']} and the source "
                f"declares {package_version()}"
            )
        if str(venv) not in report["file"]:
            problems.append(
                f"the import resolved to {report['file']}, outside the venv, so this "
                "check measured the source tree rather than the built package"
            )
        print(f"  built and imported {wheels[0].name} as {report['version']}")
        return problems


def check_remote(url: str, target: Path) -> list[str]:
    """Download the exact URL step 0 runs and compare it to the source.

    This is the only check here that answers what a student's kernel will
    actually receive. Everything above reasons about local state and infers the
    remote from it; this one asks GitHub.

    Compared against `sync.plan`, which is the text `src/workshop_lab` says the
    published copy should hold, rather than against whatever files are sitting
    in `staging/`. Two reasons: it collapses "stale locally" and "stale
    remotely" into the one question that matters, and the published directory
    also accumulates build output that was never meant to be published.
    """
    print(f"  fetching {url}")
    plain = url.split("#", 1)[0]
    try:
        with urllib.request.urlopen(plain, timeout=60) as response:
            payload = response.read()
    except OSError as error:
        return [f"could not fetch {plain}: {error}"]

    staging = target / "staging"
    wanted, _ = load_sync().plan(staging)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        # A GitHub archive wraps everything in one <repo>-<ref> directory.
        served = {
            name.split("/", 1)[1]: archive.read(name)
            for name in archive.namelist()
            if "/staging/" in name and not name.endswith("/")
        }
    if not served:
        return [f"{plain} carries no staging/ files at all"]

    problems = []
    for path, text in sorted(wanted.items()):
        key = f"staging/{path.relative_to(staging).as_posix()}"
        if key not in served:
            problems.append(f"{key} is not in the archive the notebook installs")
        elif served[key].decode("utf-8") != text:
            problems.append(f"{key} is served stale: the archive is not the source")
    print(f"  checked {len(wanted)} published file(s) against {plain}")
    return problems


def gate_command(
    env_file: Path, course: str | None = None, region: str | None = None
) -> list[str]:
    """Build the inspect-only ship-gate command."""
    command = [
        "uv",
        "run",
        "vocareum-expect",
        "--no-start",
        "--env-file",
        str(env_file),
    ]
    if course:
        command.extend(["--course", course])
    if region:
        command.extend(["--region", region])
    return command


def check_gate(
    env_file: Path, course: str | None = None, region: str | None = None
) -> list[str]:
    """Run the ship gate without starting a session and retain its failure."""
    if shutil.which("uv") is None:
        return ["uv is not on PATH, so the inspect-only ship gate cannot run"]
    environment = dict(os.environ)
    # This script runs in an inline uv environment. A nested `uv run` must
    # select the repository project instead of warning about and ignoring the
    # inline environment it inherited.
    environment.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        gate_command(env_file, course, region),
        cwd=REPOSITORY_ROOT,
        check=False,
        env=environment,
    )
    if result.returncode:
        return [
            (
                f"the inspect-only ship gate exited {result.returncode}. Read "
                "its output above; release_check never starts or extends the "
                "session."
            )
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"the workshop repository checkout (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="also download the pinned URL and compare it to the local files",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="skip the build, install and import check, which is the slow one",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="also run the ship gate against an already-open session",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPOSITORY_ROOT / ".env",
        help="base env file for --gate (default: repository .env)",
    )
    parser.add_argument("--course", metavar="NAME", help="course for --gate")
    parser.add_argument("--region", help="AWS region override for --gate")
    args = parser.parse_args(argv)

    if not args.target.is_dir():
        raise SystemExit(f"{args.target} does not exist. Clone it or pass --target.")

    url = install_url()
    version = package_version()
    tags = [
        line.strip()
        for line in git(args.target, "tag", "--list").stdout.splitlines()
        if line.strip()
    ]

    print(f"workshop_lab {version}")
    print(f"step 0 installs {url}")

    problems: list[str] = []
    print("ref")
    problems += check_ref(ref_of(url), version, tags)
    print("publication")
    problems += check_publication(args.target)
    if args.skip_build:
        print("build: skipped")
    else:
        print("build")
        problems += check_build(args.target)
    if args.fetch:
        print("remote")
        problems += check_remote(url, args.target)
    else:
        print("remote: skipped, pass --fetch to ask GitHub what it serves")
    if args.gate:
        print("ship gate: inspect only")
        sys.stdout.flush()
        problems += check_gate(args.env_file.expanduser(), args.course, args.region)
    else:
        print("ship gate: skipped, pass --gate to inspect an open session")

    if problems:
        # The findings go to stderr so a wrapper can separate them, which means
        # they overtake the progress lines unless stdout is pushed out first.
        sys.stdout.flush()
        print(f"\nNOT READY TO TEACH. {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("\nReady. The pinned ref, the published files and the version agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
