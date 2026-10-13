"""Build a deterministic Vocareum starter-code archive from pinned Git files.

The course source checkout is read-only. Files are read from the pinned Git
commit rather than from the working tree, so local edits and a newer checked-out
revision cannot leak into the student package.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import ConfigError, repository_root
from .course import course_directory, load_definition
from .upload import ZIP_EPOCH

# The repository, commit, and file list are per-course data and live in each
# course's `course.json` `startercode` block. They were module constants, and a
# constant can name one course only, which is what made a second course
# impossible to package.
TARGET = "startercode"
ARCHIVE_NAME = "startercode.zip"
INVENTORY_NAME = "startercode.inventory.json"
INVENTORY_SCHEMA = 1
TYPING_EXTENSIONS_REQUIREMENT = "typing-extensions>=4.0.0"
NUMEXPR_REQUIREMENT = "numexpr>=2.10.0"
REQUESTS_REQUIREMENT = "requests>=2.34.2"
VOCAREUM_REQUIREMENTS = (
    TYPING_EXTENSIONS_REQUIREMENT,
    NUMEXPR_REQUIREMENT,
    REQUESTS_REQUIREMENT,
)
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".ipynb_checkpoints",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "release-evidence",
    }
)
EXCLUDED_NAMES = frozenset({".DS_Store", ".env"})


class StarterCodeError(RuntimeError):
    """Raised when a starter-code package would be incomplete or unsafe."""


@dataclass(frozen=True)
class SourceFile:
    """One immutable file selected from the pinned Git tree."""

    path: str
    payload: bytes

    @property
    def size(self) -> int:
        """Return the file size in bytes."""
        return len(self.payload)

    @property
    def sha256(self) -> str:
        """Return the file SHA-256 digest."""
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class SourceState:
    """Read-only state used to prove the source checkout did not change."""

    head: str
    status: bytes


def run_git(
    source: Path,
    arguments: Sequence[str],
    *,
    text: bool = False,
) -> bytes | str:
    """Run one read-only Git command and return its output."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=source,
        capture_output=True,
        check=False,
        text=text,
    )
    if result.returncode:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", errors="replace")
        else:
            detail = stderr
        raise StarterCodeError(
            f"Git command failed with exit code {result.returncode}: "
            f"git {' '.join(arguments)}\n{detail.strip()}"
        )
    return result.stdout


def source_state(source: Path) -> SourceState:
    """Capture the checked-out revision and every working-tree change."""
    head = run_git(source, ["rev-parse", "HEAD"], text=True)
    status = run_git(
        source,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if not isinstance(head, str) or not isinstance(status, bytes):
        raise StarterCodeError("Git returned an unexpected output type.")
    return SourceState(head=head.strip(), status=status)


def remote_identity(url: str) -> str:
    """Reduce one Git remote URL to the host and path it names.

    The same repository has two spellings, ``git@host:owner/name.git`` and
    ``https://host/owner/name.git``, and a clone may use either. Comparing the
    raw strings rejects a correct checkout for choosing the other one, so
    compare what both spellings agree on.
    """
    text = url.strip().removesuffix(".git")
    for prefix in ("https://", "http://", "ssh://", "git://"):
        text = text.removeprefix(prefix)
    if "@" in text:
        text = text.split("@", 1)[1]
    return text.replace(":", "/", 1).lower()


def validate_source(source: Path, expected_repository: str) -> None:
    """Require the expected local Git checkout without changing it."""
    if not (source / ".git").exists():
        raise StarterCodeError(f"Course source is not a Git checkout: {source}")
    origin = run_git(source, ["remote", "get-url", "origin"], text=True)
    if not isinstance(origin, str):
        raise StarterCodeError("Git returned a non-text origin URL.")
    if remote_identity(origin) != remote_identity(expected_repository):
        raise StarterCodeError(
            f"Course source origin is {origin.strip()}; expected {expected_repository}."
        )


def validate_archive_path(path: str) -> str:
    """Return a safe normalized archive path or refuse it."""
    if not path or "\\" in path or any(ord(character) < 32 for character in path):
        raise StarterCodeError(f"Unsafe starter-code path: {path!r}")
    candidate = PurePosixPath(path)
    unsafe_part = any(part in {"", ".", ".."} for part in candidate.parts)
    if candidate.is_absolute() or unsafe_part:
        raise StarterCodeError(f"Unsafe starter-code path: {path!r}")
    normalized = candidate.as_posix()
    if normalized != path:
        raise StarterCodeError(f"Starter-code path is not normalized: {path!r}")
    return normalized


def should_exclude(path: str) -> bool:
    """Return whether a selected tracked file is unsafe or generated."""
    candidate = PurePosixPath(path)
    parts = candidate.parts
    name = candidate.name
    local_environment = name == ".env" or (
        name.startswith(".env.") and name != ".env.example"
    )
    return bool(
        set(parts) & EXCLUDED_PARTS
        or name in EXCLUDED_NAMES
        or local_environment
        or path.endswith((".pyc", ".pyo", "-executed.ipynb"))
    )


DEPLOY_CONTEXT_MARKER = 'DEPLOY_DIR = MODULE_DIR / "runtime_app"'
# A contract comment the deploy notebook carries directly above its git-identity
# code, not a match on that code's shape. The workshop repo is free to refactor
# how it discovers git metadata as long as this comment stays put; matching the
# implementation itself broke the day that implementation grew a helper function.
GIT_IDENTITY_MARKER = "# Vocareum git identity"
NOTEBOOK_BOOTSTRAP_MARKER = "# Vocareum dependency bootstrap"
NOTEBOOK_BOOTSTRAP_HEADING_SOURCE = """## Prepare workshop dependencies

Run this cell first. It installs the pinned workshop packages once per lab
session when needed, then makes the shared dependency directory available to
this notebook."""
NOTEBOOK_BOOTSTRAP_SOURCE = f"""{NOTEBOOK_BOOTSTRAP_MARKER}
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Find requirements.txt from wherever this notebook was opened.
start = Path.cwd().resolve()
sources = (
    start / "notebooks" / "requirements.txt",
    start / "requirements.txt",
    start.parent / "requirements.txt",
)
requirements = next((path for path in sources if path.is_file()), None)
if requirements is None:
    looked = "\\n  ".join(str(path) for path in sources)
    raise FileNotFoundError(
        f"No requirements.txt found from {{start}}. Looked at:\\n  {{looked}}\\n"
        "Open this notebook from the extracted starter-code tree rather than "
        "from a copy somewhere else in the workspace."
    )

# Pick an install directory every kernel in this session can read.
# Local disk first, because /voc/work is a slow network filesystem.
# WORKSHOP_DEPS_DIR overrides the search.
override = os.environ.get("WORKSHOP_DEPS_DIR")
if override:
    roots = (Path(override).expanduser().resolve(),)
else:
    roots = tuple(
        root / f"workshop-deps-{{os.getuid()}}"
        for root in (Path("/tmp"), Path.home(), requirements.parent)
    )
target = None
refusals = []
for candidate in roots:
    try:
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        probe = candidate / ".write-test"
        probe.write_text("")
        probe.unlink()
    except OSError as error:
        refusals.append(f"{{candidate}}: {{error}}")
        continue
    target = candidate
    break
if target is None:
    refused = "\\n  ".join(refusals)
    if override:
        remedy = (
            f"WORKSHOP_DEPS_DIR names {{override}}, which this account cannot "
            "write. Unset it to fall back to the normal search."
        )
    else:
        remedy = (
            "Set WORKSHOP_DEPS_DIR to a directory you can write, then re-run "
            "this cell."
        )
    raise RuntimeError(
        f"No writable directory for the workshop dependencies.\\n  {{refused}}\\n"
        f"{{remedy}}"
    )

# Report the interpreter, the target filesystem, and free space, and warn
# when either would make the install slow or fail.
fstype = "unknown"
try:
    longest = ""
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        point = fields[1].rstrip("/") + "/"
        if f"{{target}}/".startswith(point) and len(point) > len(longest):
            longest, fstype = point, fields[2]
except OSError:
    pass
free_gb = shutil.disk_usage(target).free / 1e9
print(f"Python       {{sys.version.split()[0]}} at {{sys.executable}}")
print(f"Target       {{target}} ({{fstype}}, {{free_gb:.1f}} GB free)")
print(f"Requirements {{requirements}}")
if fstype.startswith(("nfs", "cifs", "fuse")):
    print(
        f"WARNING: {{target}} is on a network filesystem ({{fstype}}). Writing "
        "roughly 15,000 small files there has taken over ten minutes. Set "
        "WORKSHOP_DEPS_DIR to a local directory to avoid it."
    )
if free_gb < 1.0:
    print(
        f"WARNING: only {{free_gb:.1f}} GB free, and the dependency tree needs "
        "about 0.5 GB."
    )

INSTALL_TIMEOUT_SECONDS = 900

# Install only when requirements.txt has changed since the last install.
stamp = target / "requirements.sha256"
digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
if not stamp.is_file() or stamp.read_text().strip() != digest:
    print(f"Installing workshop dependencies into {{target}}")
    began = time.monotonic()
    install = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--progress-bar",
            "off",
            "--timeout",
            "20",
            "--retries",
            "2",
            "--upgrade",
            "--target",
            str(target),
            "-r",
            str(requirements),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    # Print pip's output as it arrives, and stop the install if it overruns
    # the timeout.
    watchdog = threading.Timer(INSTALL_TIMEOUT_SECONDS, install.kill)
    watchdog.start()
    try:
        for line in install.stdout:
            print(line, end="")
        code = install.wait()
    finally:
        watchdog.cancel()
    elapsed = time.monotonic() - began
    if code != 0:
        if elapsed >= INSTALL_TIMEOUT_SECONDS:
            raise TimeoutError(
                f"Installing into {{target}} passed {{INSTALL_TIMEOUT_SECONDS}} "
                f"seconds and was stopped. That target is on {{fstype}}; if that "
                "is a network filesystem, set WORKSHOP_DEPS_DIR to a local "
                "directory such as /tmp/workshop-deps and re-run this cell."
            )
        raise RuntimeError(
            f"pip install failed with exit status {{code}}. The pip output above "
            f"ends with the reason. A partial install is safe to discard: delete "
            f"{{target}} and re-run this cell."
        )
    stamp.write_text(digest)
    print(f"Installed in {{elapsed:.0f}} seconds")
if str(target) not in sys.path:
    sys.path.insert(0, str(target))
print(f"Workshop dependencies ready in {{target}}")"""


def adapt_notebook_dependencies(payload: bytes) -> bytes:
    """Install workshop requirements where every kernel in a session can read them.

    The hosted instructions create a virtual environment, but Vocareum opens
    notebooks with its own kernels, and it hands out more than one inside a
    single session: measured 2026-08-25, ``1.0_verify_environment.ipynb`` ran
    under ``/usr/local/bin/python3.10`` while ``1.1_build_graph.ipynb`` ran
    under ``/usr/local/venvs/jupyter/bin/python3``. An install aimed at
    ``sys.executable``'s own site-packages therefore has two ways to fail. The
    shared Jupyter virtual environment is not writable by the student account,
    so the install stops on ``Errno 13``; and when it does succeed, it lands
    where the next notebook's kernel cannot see it.

    Installing into one target directory removes both. The student owns that
    directory under either kernel, and every notebook prepends the same path,
    so one install serves the whole session. Where the directory sits decides
    how long it takes: ``/voc/work`` is EFS over NFS, and measured 2026-08-25
    the 448 MB tree left pip in uninterruptible sleep past ten minutes there,
    so the container's local disk is tried first and ``WORKSHOP_DEPS_DIR``
    overrides the search for anyone who needs it elsewhere. An override is
    used verbatim rather than falling back, because a silent fall back hides
    the thing it was set to investigate.

    The cell prints the interpreter, the target's filesystem type, and its
    free space before it starts, and every exit names the next action. Those
    exist because the first failures here were unreadable: the install that
    stopped on ``Errno 13`` said nothing about which of the two kernels it had
    run under, and the one that took ten minutes on NFS was indistinguishable
    from a hang. The requirements lookup supports the same three working
    directories as the workshop notebooks themselves.
    """
    try:
        notebook = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StarterCodeError(
            f"Could not adapt notebook dependencies: {error}"
        ) from error

    cells = notebook.get("cells")
    if not isinstance(cells, list):
        raise StarterCodeError("The pinned notebook has no cell list.")

    occurrences = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        original = cell.get("source")
        source = "".join(original) if isinstance(original, list) else original
        if isinstance(source, str):
            occurrences += source.count(NOTEBOOK_BOOTSTRAP_MARKER)
    if occurrences > 1:
        raise StarterCodeError(
            "The pinned notebook has more than one Vocareum dependency bootstrap."
        )
    if occurrences == 1:
        return payload

    source = NOTEBOOK_BOOTSTRAP_SOURCE.splitlines(True)
    bootstrap = {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source,
    }
    heading = {
        "cell_type": "markdown",
        "metadata": {},
        "source": NOTEBOOK_BOOTSTRAP_HEADING_SOURCE.splitlines(True),
    }
    position = 1 if cells and cells[0].get("cell_type") == "markdown" else 0
    cells[position:position] = [heading, bootstrap]
    return (json.dumps(notebook, indent=1, ensure_ascii=False) + "\n").encode()


def adapt_deploy_notebook(payload: bytes, commit: str) -> bytes:
    """Make the pinned deploy notebook work outside a Git checkout.

    Vocareum drops dotfiles while extracting starter code. The package ships a
    visible ``dockerignore`` template, and this adapter makes the notebook copy
    it to the Docker-recognized name before staging the build. The immutable
    source commit is also written directly because a student workspace has no
    ``.git`` directory from which to discover it.

    The git-identity block is found by ``GIT_IDENTITY_MARKER``, a comment the
    deploy notebook carries on purpose, rather than by matching that block's
    code. An earlier version matched the literal ``git_commit = subprocess.run(``
    call, and a workshop refactor that wrapped it in a helper function silently
    moved the text the adapter was looking for, breaking a release with no
    warning until publish time.
    """
    try:
        notebook = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StarterCodeError(
            f"Could not adapt the deploy notebook: {error}"
        ) from error
    changed = False
    for cell in notebook.get("cells", []):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        original = cell.get("source")
        source = "".join(original) if isinstance(original, list) else original
        if not isinstance(source, str) or DEPLOY_CONTEXT_MARKER not in source:
            continue
        marker = "    )\n\nPACKAGE_SRC ="
        if marker not in source:
            raise StarterCodeError(
                "The pinned deploy notebook no longer has the expected build-context "
                "setup."
            )
        ignore_setup = (
            "    )\n\n"
            'IGNORE_TEMPLATE = DEPLOY_DIR / "dockerignore"\n'
            'IGNORE_FILE = DEPLOY_DIR / ".dockerignore"\n'
            "if not IGNORE_TEMPLATE.is_file():\n"
            '    raise FileNotFoundError(f"Missing {IGNORE_TEMPLATE}")\n'
            "shutil.copy2(IGNORE_TEMPLATE, IGNORE_FILE)\n\n"
            "PACKAGE_SRC ="
        )
        source = source.replace(marker, ignore_setup, 1)
        git_start = source.find(GIT_IDENTITY_MARKER)
        build_info = source.find('(DEPLOY_DIR / "BUILD_INFO.txt")', git_start)
        if git_start < 0 or build_info < 0:
            raise StarterCodeError(
                "The pinned deploy notebook no longer carries the "
                f"{GIT_IDENTITY_MARKER!r} marker directly above its git-identity "
                "block."
            )
        source = (
            source[:git_start]
            + f'git_commit = "{commit}"\ngit_dirty = False\n'
            + source[build_info:]
        )
        cell["source"] = (
            source if isinstance(original, str) else source.splitlines(True)
        )
        changed = True
        break
    if not changed:
        raise StarterCodeError("The deploy notebook build-context cell was not found.")
    return (json.dumps(notebook, indent=1, ensure_ascii=False) + "\n").encode()


def adapt_python_310_requirements(payload: bytes) -> bytes:
    """Declare compatibility dependencies required by Vocareum's kernel."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StarterCodeError(
            f"Could not adapt notebook requirements: {error}"
        ) from error
    lines = text.splitlines()
    missing = [
        requirement for requirement in VOCAREUM_REQUIREMENTS if requirement not in lines
    ]
    if not missing:
        return payload
    separator = "" if text.endswith("\n") else "\n"
    additions = "\n".join(missing)
    return f"{text}{separator}{additions}\n".encode()


@dataclass(frozen=True)
class CourseAdapters:
    """The path rewrites and content rewrites for one course's package.

    Held per course because an adapter names a file, and a file belongs to one
    workshop. These were module-level path comparisons that ran for whatever
    course was being built, so a second course shipping a `README.md` had one
    course's README rewrite applied to it and the build failed. A course with no
    entry ships its pinned files unchanged, which is the correct default.
    """

    renames: dict[str, str]
    content: dict[str, Callable[[bytes, str], bytes]]


COURSE_ADAPTERS: dict[str, CourseAdapters] = {
    "graphrag": CourseAdapters(
        # Vocareum drops dotfiles while extracting starter code, so the one
        # dotfile in the pinned selection ships under a visible name and the
        # deploy notebook restores the real name before it builds the image.
        renames={
            "notebooks/05-agentcore-deploy/runtime_app/.dockerignore": (
                "notebooks/05-agentcore-deploy/runtime_app/dockerignore"
            ),
        },
        content={
            "notebooks/requirements.txt": (
                lambda payload, commit: adapt_python_310_requirements(payload)
            ),
            "notebooks/05-agentcore-deploy/5.1_deploy.ipynb": adapt_deploy_notebook,
        },
    ),
}


def adapt_for_vocareum(
    course: str, path: str, payload: bytes, commit: str
) -> SourceFile:
    """Return one deterministic course file using extractor-safe names."""
    adapters = COURSE_ADAPTERS.get(course)
    if adapters is None:
        return SourceFile(path, payload)
    adapted_payload = payload
    if path.startswith("notebooks/") and path.endswith(".ipynb"):
        adapted_payload = adapt_notebook_dependencies(adapted_payload)
    adapter = adapters.content.get(path)
    if adapter is not None:
        adapted_payload = adapter(adapted_payload, commit)
    return SourceFile(adapters.renames.get(path, path), adapted_payload)


def assert_extractor_safe(path: str, course: str) -> None:
    """Refuse a packaged file Vocareum's extractor would silently drop.

    Vocareum drops dotfiles while extracting starter code, so a dotfile that
    reaches the archive is absent from the student workspace with no error
    anywhere. A course renames such a file explicitly and adapts whatever reads
    it, which is why the rename lists are per course. This turns a missing file
    a student discovers three modules in into a build that stops.
    """
    if PurePosixPath(path).name.startswith("."):
        raise StarterCodeError(
            f"Course {course!r} would package the dotfile {path}, which "
            "Vocareum's extractor drops. Add a visible name for it to that "
            "course's COURSE_ADAPTERS renames, and adapt whatever reads it."
        )


def read_pinned_files(
    source: Path,
    commit: str,
    selected_paths: Sequence[str],
    *,
    course: str,
) -> list[SourceFile]:
    """Read selected regular files from one immutable Git tree."""
    archive = run_git(
        source,
        ["archive", "--format=tar", commit, "--", *selected_paths],
    )
    if not isinstance(archive, bytes):
        raise StarterCodeError("Git archive returned text instead of bytes.")

    files: list[SourceFile] = []
    seen: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            if member.isdir():
                continue
            path = validate_archive_path(member.name)
            if should_exclude(path):
                continue
            if not member.isfile():
                raise StarterCodeError(
                    f"Unsupported starter-code entry type: {member.name}"
                )
            if path in seen:
                raise StarterCodeError(f"Duplicate starter-code path: {path}")
            source_file = bundle.extractfile(member)
            if source_file is None:
                raise StarterCodeError(f"Could not read starter-code file: {path}")
            adapted = adapt_for_vocareum(course, path, source_file.read(), commit)
            assert_extractor_safe(adapted.path, course)
            if adapted.path in seen:
                raise StarterCodeError(
                    f"Duplicate starter-code path after adaptation: {adapted.path}"
                )
            seen.add(adapted.path)
            files.append(adapted)

    files.sort(key=lambda item: item.path)
    if not files:
        raise StarterCodeError("The pinned starter-code selection is empty.")
    return files


def build_archive(files: Sequence[SourceFile]) -> bytes:
    """Build a deterministic multi-file ZIP for Vocareum starter code."""
    buffer = io.BytesIO()
    seen: set[str] = set()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for source_file in sorted(files, key=lambda item: item.path):
            path = validate_archive_path(source_file.path)
            if path in seen:
                raise StarterCodeError(f"Duplicate starter-code path: {path}")
            seen.add(path)
            info = zipfile.ZipInfo(filename=path, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source_file.payload)
    return buffer.getvalue()


def archive_digest(payload: bytes) -> str:
    """Return a SHA-256 digest for the complete starter-code archive."""
    return hashlib.sha256(payload).hexdigest()


def build_inventory(
    files: Sequence[SourceFile],
    archive: bytes,
    *,
    course: str,
    source_repository: str,
    source_commit: str,
) -> dict[str, object]:
    """Return the stable course-file inventory and archive identity."""
    return {
        "schema_version": INVENTORY_SCHEMA,
        "course": course,
        "target": TARGET,
        "source_repository": source_repository,
        "source_commit": source_commit,
        "archive": ARCHIVE_NAME,
        "archive_size": len(archive),
        "archive_sha256": archive_digest(archive),
        "file_count": len(files),
        "files": [
            {
                "path": source_file.path,
                "size": source_file.size,
                "sha256": source_file.sha256,
            }
            for source_file in files
        ],
    }


def verify_package(
    archive: bytes,
    inventory: dict[str, object],
    files: Sequence[SourceFile],
    *,
    course: str,
    source_repository: str,
    source_commit: str,
) -> None:
    """Verify archive paths, bytes, hashes, and inventory before writing."""
    expected = {source_file.path: source_file.payload for source_file in files}
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise StarterCodeError("Starter-code ZIP contains duplicate paths.")
        if names != sorted(expected):
            raise StarterCodeError("Starter-code ZIP paths are missing or unsorted.")
        for name in names:
            validate_archive_path(name)
            if bundle.read(name) != expected[name]:
                raise StarterCodeError(f"Starter-code ZIP bytes differ for {name}.")

    rebuilt = build_inventory(
        files,
        archive,
        course=course,
        source_repository=source_repository,
        source_commit=source_commit,
    )
    if inventory != rebuilt:
        raise StarterCodeError("Starter-code inventory does not match the ZIP.")


def write_package(
    source: Path,
    output: Path,
    *,
    course: str,
    expected_repository: str,
    commit: str,
    selected_paths: Sequence[str],
) -> tuple[Path, Path, dict[str, object]]:
    """Build and verify one package while proving the source stays unchanged."""
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    validate_source(source, expected_repository)
    before = source_state(source)
    files = read_pinned_files(source, commit, selected_paths, course=course)
    archive = build_archive(files)
    inventory = build_inventory(
        files,
        archive,
        course=course,
        source_repository=expected_repository,
        source_commit=commit,
    )
    verify_package(
        archive,
        inventory,
        files,
        course=course,
        source_repository=expected_repository,
        source_commit=commit,
    )
    after = source_state(source)
    if after != before:
        raise StarterCodeError(
            "The course source checkout changed while starter code was built."
        )

    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / ARCHIVE_NAME
    inventory_path = output / INVENTORY_NAME
    with tempfile.NamedTemporaryFile(dir=output, delete=False) as temporary_archive:
        temporary_archive.write(archive)
        temporary_archive_path = Path(temporary_archive.name)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output, delete=False
    ) as temporary_inventory:
        json.dump(inventory, temporary_inventory, indent=2, sort_keys=True)
        temporary_inventory.write("\n")
        temporary_inventory_path = Path(temporary_inventory.name)
    temporary_archive_path.chmod(0o644)
    temporary_inventory_path.chmod(0o644)
    temporary_archive_path.replace(archive_path)
    temporary_inventory_path.replace(inventory_path)
    return archive_path, inventory_path, inventory


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse starter-code package arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course", required=True, help="Course key to package")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "Read-only Git checkout to package. Defaults to the path the "
            "course's startercode.source names."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Directory for the ZIP and inventory. Defaults to "
            ".vocareum/build/<course>/."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Build the selected course package and print its stable identity."""
    args = parse_args(argv)
    root = repository_root()
    env_path = root / ".env"
    definition = load_definition(env_path, args.course)
    starter = definition.startercode
    if starter is None:
        raise ConfigError(
            f"Course {args.course!r} declares no startercode block, so it has "
            "no student package to build. Add one to its course.json."
        )
    if args.source is None:
        source = course_directory(env_path, args.course) / starter.source
    else:
        source = args.source
    output = args.output or root / ".vocareum" / "build" / args.course
    archive_path, inventory_path, inventory = write_package(
        source,
        output,
        course=args.course,
        expected_repository=starter.repository,
        commit=starter.commit,
        selected_paths=starter.paths,
    )
    print(f"course          {inventory['course']}")
    print(f"target          {inventory['target']}")
    print(f"source commit   {inventory['source_commit']}")
    print(f"files           {inventory['file_count']}")
    print(f"archive bytes   {inventory['archive_size']}")
    print(f"archive sha256  {inventory['archive_sha256']}")
    print(f"archive         {archive_path}")
    print(f"inventory       {inventory_path}")
    print("Source checkout unchanged. Nothing was uploaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
