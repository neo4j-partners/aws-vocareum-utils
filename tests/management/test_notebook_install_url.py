"""Hold the three copies of the install URL to one string, and to the version.

Step 0 of the notebook installs `workshop_lab` from a GitHub archive URL. That
URL is written out in three places, because none of them can import it from the
others: the notebook cell a student runs, `notebooks/README.md`, which is what an
instructor reads before a class, and the package's own module docstring, which is
what a maintainer reads before changing any of it. Three hand-kept copies of one
string is exactly the shape that drifts, and the copy that would be wrong is
whichever one nobody ran.

The second thing here is the ref. `refs/heads/main` moves, so two students who
run step 0 twenty minutes apart can install different code, and a fix pushed
mid-class silently changes the workshop under the people already in it.
`notebooks/README.md` has said to pin it before teaching since it was written.
Pinning is one edit per copy, and what makes that edit safe is this: the moment
the ref is a tag, it has to be `v<__version__>`, so a pinned notebook and the
version line it prints cannot name different releases.

Nothing here fetches the URL. Whether GitHub actually serves that ref is a
question for `scripts/release_check.py`, which is run by hand before a class,
because a test that needs the network is a test that does not run.
"""

from __future__ import annotations

import json
import re

import pytest

from vocareum_tools.config import repository_root
from workshop_lab import __version__

NOTEBOOK = repository_root() / "notebooks" / "lab-01-verify-environment.ipynb"
README = repository_root() / "notebooks" / "README.md"
PACKAGE = repository_root() / "src" / "workshop_lab" / "__init__.py"

ARCHIVE = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
    r"/archive/(?P<ref>[\w./-]+)\.zip#subdirectory=(?P<subdir>\w+)"
)

# The published copy lives in this repository, in this directory. Both are
# spelled out here rather than read from the code under test, because a test
# that derives its expectation from the thing it is testing asserts nothing.
OWNER = "neo4j-partners"
REPO = "neo4j-sec-filings-graphrag-workshop"
SUBDIR = "staging"


def urls(text: str) -> list[str]:
    return [match.group(0) for match in ARCHIVE.finditer(text)]


def notebook_url() -> str:
    """The URL out of the cell a student actually runs.

    Read from the committed JSON rather than from an executed notebook, and
    required to be unique: two `%pip install` lines for this package would mean
    the one that wins is whichever ran last.
    """
    cells = json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
    source = "".join(
        line
        for cell in cells
        if cell["cell_type"] == "code"
        for line in cell["source"]
        if "%pip install" in line
    )
    found = urls(source)
    assert len(found) == 1, f"expected one install URL in the notebook, got {found}"
    return found[0]


@pytest.fixture(scope="module")
def url() -> str:
    return notebook_url()


def test_the_notebook_installs_from_the_public_workshop_repository(url: str) -> None:
    """The notebook installs only `workshop_lab`, from the repository students
    already use. An install pointed anywhere else fails in a way that names
    nothing."""
    match = ARCHIVE.fullmatch(url)
    assert match is not None
    assert (match["owner"], match["repo"], match["subdir"]) == (OWNER, REPO, SUBDIR)


def test_the_readme_documents_the_url_the_notebook_uses(url: str) -> None:
    """An instructor pins the ref by reading the README. A README naming a
    different URL than the notebook runs is worse than no README."""
    assert url in README.read_text(encoding="utf-8")


def test_the_package_docstring_names_the_same_url(url: str) -> None:
    """It is the first thing a maintainer reads before changing any of this."""
    assert url in PACKAGE.read_text(encoding="utf-8")


def test_the_ref_is_one_of_the_two_shapes_that_mean_something(url: str) -> None:
    """A branch that moves, or a tag that does not. Anything else, including a
    ref that no longer exists, is caught here rather than in a classroom."""
    ref = ARCHIVE.fullmatch(url)["ref"]  # type: ignore[index]
    assert ref == "refs/heads/main" or ref.startswith("refs/tags/")


def test_a_pinned_ref_names_the_version_the_package_declares(url: str) -> None:
    """The rule that makes pinning a safe edit rather than a second thing to
    remember. `__version__` is what step 0 prints, so a student's output and the
    tag they installed name the same release or this fails."""
    ref = ARCHIVE.fullmatch(url)["ref"]  # type: ignore[index]
    if not ref.startswith("refs/tags/"):
        pytest.skip("the ref is still a branch; scripts/release_check.py blocks that")
    assert ref == f"refs/tags/v{__version__}"
