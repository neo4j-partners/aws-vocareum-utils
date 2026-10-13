"""Check the student notebook as a file, without running any of it.

None of this needs AWS or Vocareum. What it catches is the class of mistake that
is otherwise only found by a student in a live lab: a cell id Vocareum rejects, a
syntax error four cells down, a `%pip install` that lands after the first import
of what it installs, or a reformatted 80 KB JSON file that buries the real change
in the diff.

This file used to also hold the drift check between the notebook's generated
registry cell and `notebooks/workshop_registry.py`. That cell is gone: the code
is installed as part of `workshop_lab` now, and
`tests/runtime/test_workshop_lab_drift.py` guards the published copy instead.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from vocareum_tools.config import repository_root

NOTEBOOK_PATH = repository_root() / "notebooks" / "lab-01-verify-environment.ipynb"


def notebook() -> dict[str, Any]:
    return json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))


def code_cells() -> list[dict[str, Any]]:
    return [cell for cell in notebook()["cells"] if cell["cell_type"] == "code"]


def cell_source(cell_id: str) -> str:
    for cell in notebook()["cells"]:
        if cell.get("id") == cell_id:
            return "".join(cell["source"])
    raise AssertionError(f"{NOTEBOOK_PATH.name} has no cell {cell_id!r}")


def without_magics(source: str) -> str:
    """Blank out IPython line magics, keeping the line numbering intact.

    Step 0 installs `workshop_lab` with `%pip install`, which is not Python and
    never will be. Blanking the line rather than dropping it keeps a reported
    syntax error on the line the student would have to go and look at.
    """
    return "\n".join(
        "" if line.lstrip().startswith(("%", "!")) else line
        for line in source.splitlines()
    )


def test_the_notebook_keeps_its_committed_json_formatting() -> None:
    """Anything that rewrites the file has to preserve the shape.

    Without this, one tool writing it back with different indent settings
    reformats every line and buries the real change in the diff.
    """
    raw = NOTEBOOK_PATH.read_text(encoding="utf-8")

    assert json.dumps(json.loads(raw), indent=1, ensure_ascii=False) + "\n" == raw


def test_every_cell_id_is_unique_and_well_formed() -> None:
    """Vocareum rejects a notebook whose cell ids break the nbformat pattern."""
    ids = [cell["id"] for cell in notebook()["cells"]]

    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-zA-Z0-9\-_]+", one) for one in ids)


def test_every_code_cell_compiles() -> None:
    """A syntax error in a cell is only found by running the notebook."""
    for cell in code_cells():
        compile(without_magics("".join(cell["source"])), cell["id"], "exec")


def test_no_cell_carries_committed_output() -> None:
    """Outputs are cleared before commit. They also carry account identifiers."""
    for cell in code_cells():
        assert not cell.get("outputs"), cell["id"]
        assert cell.get("execution_count") is None, cell["id"]


def test_the_install_comes_before_anything_that_imports_it() -> None:
    """`workshop_lab` is installed by step 0, not shipped beside the notebook.

    A cell importing it above the install is a ModuleNotFoundError on a fresh
    workspace and a silent pass on any machine that happens to have it.
    """
    lines = [
        line for cell in code_cells() for line in "".join(cell["source"]).splitlines()
    ]
    installs = [i for i, line in enumerate(lines) if "%pip install" in line]
    imports = [
        i
        for i, line in enumerate(lines)
        if re.match(r"\s*(from|import) workshop_lab", line)
    ]

    assert installs, "no cell installs workshop_lab"
    assert imports, "no cell imports workshop_lab"
    assert min(installs) < min(imports)


@pytest.mark.parametrize(
    "cell_id, expected",
    [
        ("step0-code", "workshop_lab"),
        ("step6-code", "Roles"),
        ("step7-code", "BuildSource"),
        ("step8-code", "ContainerBuild"),
        ("step9-code", "AgentRuntime"),
        ("step10-code", "Memory"),
        ("step11-code", "GatewayBoundary"),
        ("step12-code", "DataStores"),
        ("step13-code", "PyPIReachable"),
        ("step2b-code", "ModelAccess"),
        ("step14-code", "Teardown"),
    ],
)
def test_the_converted_steps_use_the_package(cell_id: str, expected: str) -> None:
    """A step that quietly grew its own copy back is drift these tests exist for."""
    assert expected in cell_source(cell_id)


def test_no_cell_rebinds_the_harness_as_bare_names() -> None:
    """The scaffolding block that did this is gone, and it should stay gone.

    While the steps were being converted, step 4 rebound `client`, `check`,
    `record`, `sweep`, and the waiters as module-level names so an unconverted
    step below could still call them. Every step is converted now. A cell that
    reintroduces one of these is a step growing its own copy of the harness back,
    which is exactly what the package exists to prevent.
    """
    rebound = re.compile(
        r"^(session|client|check|record|sweep|defer|wait_until|wait_until_gone"
        r"|retry_while|CLEANUPS|REGION|PREFIX)\s*=",
        re.MULTILINE,
    )

    for cell in code_cells():
        assert not rebound.search("".join(cell["source"])), cell["id"]
