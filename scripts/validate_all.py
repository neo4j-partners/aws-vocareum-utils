#!/usr/bin/env -S uv run
"""Run the complete offline CI and pre-release gate.

Normal development uses ``uv run pytest``, which runs only the management
suite. This command adds the workshop runtime suite, the per-course asset
checks, and repository-wide static checks. It makes no network or service call.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from vocareum_tools.config import ConfigError
from vocareum_tools.course import load_definition, manifest_path, template_path
from vocareum_tools.expect import ManifestError, build_expectations, read_manifest
from vocareum_tools.release import source_revision

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
PYTHON = sys.executable
CFN_LINT = str(Path(PYTHON).with_name("cfn-lint"))

# Named rather than discovered from the course env files. An env file names a
# Vocareum part, and this repository holds parts it does not release. Resolved
# through `template_path` rather than written as a literal path, because a
# course may keep its template in its own repository.
LINTED_COURSES = ("neo4j-fundamentals", "graphrag")

# Vocareum submits lab.template as the CloudFormation inline TemplateBody, which
# allows at most this many bytes. A larger template uploads cleanly and fails
# only at Start Lab, so the limit is checked here, before anything is sent.
MAX_INLINE_TEMPLATE_BYTES = 51_200

CHECKS = (
    [PYTHON, "-m", "pytest", "tests/management"],
    [PYTHON, "-m", "ruff", "check", "."],
    [PYTHON, "-m", "ruff", "format", "--check", "."],
)


class CloudFormationLoader(yaml.SafeLoader):
    """Load CloudFormation YAML, reading short-form intrinsics as plain data."""


def construct_intrinsic(
    loader: CloudFormationLoader, tag_suffix: str, node: yaml.Node
) -> Any:
    """Turn any short-form intrinsic such as ``!Sub`` into ordinary data."""
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {f"Fn::{tag_suffix}": value}


CloudFormationLoader.add_multi_constructor("!", construct_intrinsic)


def present_courses() -> tuple[list[str], list[str]]:
    """Return the courses to check, and why each absent course is skipped.

    graphrag keeps its assets in its own repository, named only by the ignored
    courses/graphrag.env, so a fresh clone has neither and resolves the course
    to a ``courses/graphrag/`` that does not exist. Only that case is skipped,
    and the reason is printed every run because a skip verifies nothing. A
    pointer to a missing directory still raises, and a directory without its
    template still fails.
    """
    found: list[str] = []
    skipped: list[str] = []
    for name in LINTED_COURSES:
        directory = template_path(ENV_PATH, name).parent
        if directory.is_dir():
            found.append(name)
        else:
            skipped.append(f"{name}: no course directory at {directory}")
    return found, skipped


def asset_problems(template: Path, manifest: Path, commit: str | None) -> list[str]:
    """Return every way one course's template disagrees with its own files.

    Three checks, each of which otherwise surfaces only after an upload or a
    launch: the inline size limit, the outputs ``expected.json`` names, and,
    when the course ships starter code, the commit the template pins.
    """
    try:
        size = template.stat().st_size
    except OSError as error:
        return [f"Could not read {template}: {error}"]
    problems: list[str] = []
    if size > MAX_INLINE_TEMPLATE_BYTES:
        problems.append(
            f"{template} is {size:,} bytes. Vocareum sends lab.template as the "
            "CloudFormation inline TemplateBody, which allows at most "
            f"{MAX_INLINE_TEMPLATE_BYTES:,} bytes."
        )

    try:
        loaded = yaml.load(template.read_text(encoding="utf-8"), CloudFormationLoader)
    except (OSError, yaml.YAMLError) as error:
        return [*problems, f"Could not parse {template}: {error}"]
    if not isinstance(loaded, dict):
        return [*problems, f"{template} must contain a CloudFormation mapping."]

    # Every ${VAR} gets a stand-in. Output names are literal, and the real
    # values are pins that may be blank on a new course or expired after a
    # rotation, which has nothing to do with whether the template agrees.
    try:
        expectations = build_expectations(
            read_manifest(manifest), lambda name: f"<{name}>"
        )
    except ManifestError as error:
        problems.append(str(error))
    else:
        named = {item.name for item in expectations if item.kind == "outputs"}
        missing = sorted(named - set(loaded.get("Outputs") or {}))
        if missing:
            problems.append(
                f"{manifest} names outputs missing from {template}: "
                f"{', '.join(missing)}"
            )

    if commit is not None:
        try:
            pinned = source_revision(template)
        except ConfigError as error:
            problems.append(str(error))
        else:
            if pinned != commit:
                problems.append(
                    f"{template} pins SourceCommit {pinned}, but course.json "
                    f"startercode.commit is {commit}."
                )
    return problems


def course_asset_problems(name: str) -> list[str]:
    """Resolve one course's files and check them against each other."""
    definition = load_definition(ENV_PATH, name)
    commit = definition.startercode.commit if definition.startercode else None
    return asset_problems(
        template_path(ENV_PATH, name), manifest_path(ENV_PATH, name), commit
    )


def main() -> int:
    """Run each offline gate, stopping at the first failure."""
    courses, skipped = present_courses()
    for reason in skipped:
        print(f"SKIP course assets and cfn-lint for {reason}", flush=True)
    for name in courses:
        problems = course_asset_problems(name)
        for problem in problems:
            print(f"FAIL  {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"ok    course assets for {name}", flush=True)
    templates = [str(template_path(ENV_PATH, name)) for name in courses]
    for command in (*CHECKS, [CFN_LINT, *templates]):
        print(f"+ {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    print("Complete offline pre-release validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
