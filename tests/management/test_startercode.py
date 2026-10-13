import hashlib
import io
import json
import stat
import subprocess
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from vocareum_tools import startercode
from vocareum_tools.config import ConfigError


def git(repository: Path, arguments: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def make_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, ["init"])
    git(repository, ["config", "user.email", "test@example.com"])
    git(repository, ["config", "user.name", "Test User"])
    origin = "https://example.test/workshop.git"
    git(repository, ["remote", "add", "origin", origin])

    files = {
        "CONFIG.txt": b"NEO4J_PASSWORD=replace-me\n",
        "README.md": b"# Workshop\nold\n",
        "notebooks/01/lesson.ipynb": b'{"cells": []}\n',
        "notebooks/01/helper.py": b"VALUE = 1\n",
        "notebooks/.env": b"REAL_SECRET=do-not-package\n",
        "notebooks/.env.local": b"MORE_SECRET=do-not-package\n",
        "notebooks/__pycache__/helper.pyc": b"cache",
        "notebooks/lesson-executed.ipynb": b'{"outputs": ["secret"]}\n',
        "notebooks/05-agentcore-deploy/runtime_app/.dockerignore": b".env\n",
        "setup/verify_setup.py": b"print('ready')\n",
        "setup/developer_test.py": b"raise AssertionError\n",
    }
    for name, payload in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    git(repository, ["add", "."])
    git(repository, ["commit", "-m", "pinned"])
    pinned = git(repository, ["rev-parse", "HEAD"])

    (repository / "README.md").write_text("# Newer workshop\nnew\n", encoding="utf-8")
    git(repository, ["add", "README.md"])
    git(repository, ["commit", "-m", "newer"])
    return repository, origin, pinned


def test_package_reads_the_pinned_tree_and_is_deterministic(tmp_path: Path) -> None:
    source, origin, pinned = make_repository(tmp_path)
    before = startercode.source_state(source)
    output = tmp_path / "output"
    selected = ("CONFIG.txt", "README.md", "notebooks", "setup/verify_setup.py")

    archive_path, inventory_path, inventory = startercode.write_package(
        source,
        output,
        course="graphrag",
        expected_repository=origin,
        commit=pinned,
        selected_paths=selected,
    )
    first_archive = archive_path.read_bytes()
    first_inventory = inventory_path.read_bytes()
    startercode.write_package(
        source,
        output,
        course="graphrag",
        expected_repository=origin,
        commit=pinned,
        selected_paths=selected,
    )

    assert archive_path.read_bytes() == first_archive
    assert inventory_path.read_bytes() == first_inventory
    assert startercode.source_state(source) == before
    assert inventory["course"] == "graphrag"
    assert inventory["source_commit"] == pinned
    assert inventory["archive_sha256"] == hashlib.sha256(first_archive).hexdigest()
    assert json.loads(first_inventory) == inventory
    assert stat.S_IMODE(archive_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(inventory_path.stat().st_mode) == 0o644

    with zipfile.ZipFile(io.BytesIO(first_archive)) as bundle:
        assert bundle.namelist() == [
            "CONFIG.txt",
            "README.md",
            "notebooks/01/helper.py",
            "notebooks/01/lesson.ipynb",
            "notebooks/05-agentcore-deploy/runtime_app/dockerignore",
            "setup/verify_setup.py",
        ]
        assert bundle.read("README.md") == b"# Workshop\nold\n"
        assert all(
            info.date_time == startercode.ZIP_EPOCH for info in bundle.infolist()
        )


@pytest.mark.parametrize(
    "path",
    ["", "../escape", "/absolute", "folder\\file", "folder/../file", "bad\nfile"],
)
def test_unsafe_archive_paths_are_refused(path: str) -> None:
    with pytest.raises(startercode.StarterCodeError, match="Unsafe"):
        startercode.validate_archive_path(path)


def test_duplicate_archive_paths_are_refused() -> None:
    files = [
        startercode.SourceFile("same.txt", b"first"),
        startercode.SourceFile("same.txt", b"second"),
    ]

    with pytest.raises(startercode.StarterCodeError, match="Duplicate"):
        startercode.build_archive(files)


def test_the_only_shipped_dotfile_gets_a_visible_name() -> None:
    """Vocareum drops dotfiles, so the one dotfile ships renamed.

    Asserted through the course registry rather than against a module constant,
    because which paths a course renames is that course's data.
    """
    path = "notebooks/05-agentcore-deploy/runtime_app/.dockerignore"

    adapted = startercode.adapt_for_vocareum(
        "graphrag", path, b"__pycache__\n", "0" * 40
    )

    assert adapted.path == "notebooks/05-agentcore-deploy/runtime_app/dockerignore"
    assert adapted.payload == b"__pycache__\n"


def test_a_course_with_no_adapters_ships_its_pinned_files_unchanged() -> None:
    """An adapter names a file, and a file belongs to one workshop.

    The path comparisons used to run for whatever course was being built, so a
    second course shipping a ``README.md`` had another course's README rewrite
    applied to it and the build failed. No entry means no adaptation.
    """
    assert "neo4j-fundamentals" not in startercode.COURSE_ADAPTERS

    adapted = startercode.adapt_for_vocareum(
        "neo4j-fundamentals", "README.md", b"# Anything at all\n", "0" * 40
    )

    assert adapted.path == "README.md"
    assert adapted.payload == b"# Anything at all\n"


def test_a_registered_course_adapts_only_the_paths_it_names() -> None:
    adapted = startercode.adapt_for_vocareum(
        "graphrag", "README.md", b"# Untouched\n", "0" * 40
    )

    assert adapted.path == "README.md"
    assert adapted.payload == b"# Untouched\n"


def test_vocareum_requirements_declare_kernel_compatibility_once() -> None:
    payload = b"neo4j-graphrag>=1.6.0\n"

    adapted = startercode.adapt_python_310_requirements(payload)
    adapted_twice = startercode.adapt_python_310_requirements(adapted)

    assert adapted.decode().splitlines()[-3:] == [
        "typing-extensions>=4.0.0",
        "numexpr>=2.10.0",
        "requests>=2.34.2",
    ]
    assert adapted_twice == adapted


def test_vocareum_requirements_add_each_missing_compatibility_dependency() -> None:
    payload = b"neo4j-graphrag>=1.6.0\ntyping-extensions>=4.0.0\n"

    adapted = startercode.adapt_python_310_requirements(payload).decode()

    assert adapted.count("typing-extensions>=4.0.0") == 1
    assert adapted.count("numexpr>=2.10.0") == 1
    assert adapted.count("requests>=2.34.2") == 1


def test_graphrag_notebooks_install_requirements_into_a_writable_target() -> None:
    payload = json.dumps(
        {
            "cells": [
                {
                    "cell_type": "markdown",
                    "source": [
                        "# Module 1\n",
                        "## Add five hotels to the preloaded graph\n",
                    ],
                },
                {"cell_type": "code", "source": ["import graph_builder\n"]},
            ]
        }
    ).encode()

    adapted = startercode.adapt_for_vocareum(
        "graphrag", "notebooks/01/module.ipynb", payload, "0" * 40
    )
    cells = json.loads(adapted.payload)["cells"]
    heading = "".join(cells[1]["source"])
    bootstrap = "".join(cells[2]["source"])

    assert cells[0]["cell_type"] == "markdown"
    assert cells[1]["cell_type"] == "markdown"
    assert heading.startswith("## Prepare workshop dependencies\n")
    assert cells[2]["cell_type"] == "code"
    assert startercode.NOTEBOOK_BOOTSTRAP_MARKER in bootstrap
    assert "sys.executable" in bootstrap
    assert '"pip"' in bootstrap
    assert '"requirements.txt"' in bootstrap
    # Vocareum's kernels are not writable by the student and more than one is
    # handed out per session, so the install must not aim at sys.executable's
    # own site-packages and the path must be readable from the next notebook.
    assert '"--target"' in bootstrap
    assert "sys.path.insert(0, str(target))" in bootstrap
    # /voc/work is EFS over NFS and copying the tree there takes over ten
    # minutes, so the container's local disk is tried first and the uid keeps
    # the directory this student's own.
    assert 'for root in (Path("/tmp"), Path.home(), requirements.parent)' in bootstrap
    assert "workshop-deps-{os.getuid()}" in bootstrap
    # A child process writes past the cell into the gateway log, so the output
    # is read from a pipe and printed, and a stall fails instead of hanging.
    assert "subprocess.Popen(" in bootstrap
    assert "for line in install.stdout:" in bootstrap
    assert "INSTALL_TIMEOUT_SECONDS" in bootstrap
    assert '"--quiet"' not in bootstrap
    # The interpreter, the filesystem under the target, and the free space are
    # the three facts that explain every failure this cell has produced, and a
    # network filesystem presents as a hang, so it is named before the wait.
    assert "/proc/mounts" in bootstrap
    assert "shutil.disk_usage(target)" in bootstrap
    assert 'fstype.startswith(("nfs", "cifs", "fuse"))' in bootstrap
    # Every exit names what to do next rather than only what went wrong.
    assert "raise FileNotFoundError(" in bootstrap
    assert "Unset it to fall back to the normal search." in bootstrap
    # An override is used verbatim; a silent fall back to /tmp would hide the
    # thing the override was set to investigate.
    assert 'os.environ.get("WORKSHOP_DEPS_DIR")' in bootstrap
    assert "roots = (Path(override).expanduser().resolve(),)" in bootstrap
    assert "import graph_builder" in "".join(cells[3]["source"])


def test_non_graphrag_notebooks_do_not_get_the_dependency_bootstrap() -> None:
    payload = b'{"cells": []}\n'

    adapted = startercode.adapt_for_vocareum(
        "neo4j-fundamentals", "notebooks/lab.ipynb", payload, "0" * 40
    )

    assert adapted.payload == payload


def test_notebook_dependency_bootstrap_is_not_duplicated() -> None:
    payload = json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "source": startercode.NOTEBOOK_BOOTSTRAP_MARKER,
                }
            ]
        }
    ).encode()

    assert startercode.adapt_notebook_dependencies(payload) == payload


def test_deploy_notebook_recreates_dockerignore_and_uses_pinned_commit() -> None:
    """The identity block is found by its marker comment, not by its code shape.

    The workshop notebook is free to compute git identity however it likes --
    a bare ``subprocess.run(...)`` call here, a helper function there -- as
    long as ``GIT_IDENTITY_MARKER`` still sits directly above it. Matching the
    implementation itself broke a release the day that implementation grew a
    helper function.
    """
    commit = "1" * 40
    source = (
        'DEPLOY_DIR = MODULE_DIR / "runtime_app"\n'
        "if not DEPLOY_DIR.is_dir():\n"
        "    raise FileNotFoundError(\n"
        '        "runtime missing"\n'
        "    )\n\n"
        "PACKAGE_SRC = Path('workshop')\n"
        f"{startercode.GIT_IDENTITY_MARKER}\n"
        'git_commit = subprocess.run(["git", "rev-parse", "HEAD"])\n'
        'git_dirty = bool(subprocess.run(["git", "status"]))\n'
        '(DEPLOY_DIR / "BUILD_INFO.txt").write_text(git_commit)\n'
    )
    payload = json.dumps({"cells": [{"cell_type": "code", "source": source}]}).encode()

    adapted = startercode.adapt_deploy_notebook(payload, commit)
    cell = json.loads(adapted)["cells"][0]["source"]

    assert 'IGNORE_TEMPLATE = DEPLOY_DIR / "dockerignore"' in cell
    assert 'IGNORE_FILE = DEPLOY_DIR / ".dockerignore"' in cell
    assert commit in cell
    assert "git rev-parse" not in cell
    assert startercode.GIT_IDENTITY_MARKER not in cell


def test_deploy_notebook_refuses_a_missing_git_identity_marker() -> None:
    source = (
        'DEPLOY_DIR = MODULE_DIR / "runtime_app"\n'
        "if not DEPLOY_DIR.is_dir():\n"
        "    raise FileNotFoundError(\n"
        '        "runtime missing"\n'
        "    )\n\n"
        "PACKAGE_SRC = Path('workshop')\n"
        'git_commit = subprocess.run(["git", "rev-parse", "HEAD"])\n'
        '(DEPLOY_DIR / "BUILD_INFO.txt").write_text(git_commit)\n'
    )
    payload = json.dumps({"cells": [{"cell_type": "code", "source": source}]}).encode()

    with pytest.raises(startercode.StarterCodeError, match="marker"):
        startercode.adapt_deploy_notebook(payload, "1" * 40)


def test_a_course_without_a_startercode_block_is_refused() -> None:
    """The refusal names the missing block rather than the course name.

    A course that ships no student package is a real state, and the message has
    to say that rather than claim packaging works for one course only.
    """
    with pytest.raises(ConfigError, match="no startercode block"):
        startercode.main(["--course", "neo4j-fundamentals"])


def test_either_spelling_of_one_remote_is_accepted() -> None:
    """A clone over SSH and a clone over HTTPS name the same repository."""
    ssh = "git@github.com:neo4j-partners/neo4j-hotel-booking-agent-workshop.git"
    https = "https://github.com/neo4j-partners/neo4j-hotel-booking-agent-workshop.git"

    assert startercode.remote_identity(ssh) == startercode.remote_identity(https)
    assert startercode.remote_identity(ssh) != startercode.remote_identity(
        "https://github.com/neo4j-partners/other-workshop.git"
    )


def test_a_dotfile_with_no_visible_name_stops_the_build(tmp_path: Path) -> None:
    """A dropped file must fail the build, not the student's third module.

    Vocareum's extractor discards dotfiles. Packaging one produces a workspace
    that is missing a file with no error anywhere, so the guard refuses instead.
    """
    source, origin, pinned = make_repository(tmp_path)
    (source / ".secretsrc").write_bytes(b"unrenamed\n")
    git(source, ["add", ".secretsrc"])
    git(source, ["commit", "-m", "add an unrenamed dotfile"])
    pinned = git(source, ["rev-parse", "HEAD"])

    with pytest.raises(startercode.StarterCodeError, match="extractor drops"):
        startercode.write_package(
            source,
            tmp_path / "output",
            course="graphrag",
            expected_repository=origin,
            commit=pinned,
            selected_paths=(".secretsrc",),
        )
