"""Hold the published copy of `workshop_lab` to `src/workshop_lab/`.

The lab notebook installs its helpers with pip from the public workshop
repository, which students already use. That leaves one package in two
repositories, and a hand-kept copy drifts.

`scripts/sync_workshop_lab.py` writes one into the other. This holds it there.

**These tests skip when the workshop repository is not checked out beside this
one.** They compare files on disk, and the other repository is not a dependency
of this one. A skip here means nothing was verified, so the sync is still the
step that has to run before a push.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = ROOT / "scripts" / "sync_workshop_lab.py"


def load_sync():
    """Import the sync script by path. It is a script, not a package module."""
    spec = importlib.util.spec_from_file_location("sync_workshop_lab", SYNC_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync = load_sync()


def published_or_skip() -> Path:
    target = sync.DEFAULT_TARGET
    if not (target / sync.PACKAGE).is_dir():
        pytest.skip(f"no published copy at {target}")
    return target


def test_every_source_module_is_published():
    """A module added to the source is one the installed package must export."""
    target = published_or_skip()
    wanted, _ = sync.plan(target)
    missing = [path.name for path in wanted if not path.exists()]
    assert missing == [], f"run ./scripts/sync_workshop_lab.py: missing {missing}"


def test_published_copy_matches_the_source():
    target = published_or_skip()
    wanted, _ = sync.plan(target)
    for path, text in sorted(wanted.items()):
        if path.exists():
            assert path.read_text(encoding="utf-8") == text, (
                f"{path.name} differs from src/workshop_lab/. "
                "Run ./scripts/sync_workshop_lab.py"
            )


def test_no_stale_module_is_left_published():
    """A module deleted here has to leave there.

    Otherwise the notebook keeps installing a package that still exports it, and
    a student runs code no longer in source control.
    """
    target = published_or_skip()
    _, stale = sync.plan(target)
    assert stale == [], f"run ./scripts/sync_workshop_lab.py: stale {stale}"


def test_the_published_version_is_the_source_version():
    """Step 0 prints this number, so it has to name the code that actually ran."""
    target = published_or_skip()
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{sync.package_version()}"' in pyproject


def test_the_generated_header_names_where_to_edit():
    """A published file has to say it is generated, or someone edits it there."""
    target = published_or_skip()
    for path in (target / sync.PACKAGE).glob("*.py"):
        head = path.read_text(encoding="utf-8")[:400]
        assert "generated" in head
        assert "aws-vocareum/src/workshop_lab/" in head
