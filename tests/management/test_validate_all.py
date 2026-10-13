from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate_all.py"
COMMIT = "a" * 40


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_all", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_script()


def write_course(
    directory: Path, *, outputs: list[str], commit: str = COMMIT, padding: int = 0
) -> tuple[Path, Path]:
    """Write a template that pins ``commit`` and declares one output."""
    template = directory / "lab.template"
    template.write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Metadata:\n"
        "  SourceRepository: https://github.com/example/workshop.git\n"
        f"  SourceCommit: {commit}\n"
        "Resources:\n"
        "  Sentinel:\n"
        "    Type: AWS::CloudFormation::WaitConditionHandle\n"
        "Outputs:\n"
        "  BucketName:\n"
        "    Value: !Ref Sentinel\n"
        f"# {'x' * padding}\n",
        encoding="utf-8",
    )
    manifest = directory / "expected.json"
    manifest.write_text(
        json.dumps({"stacks": ["${LAB_STACK_NAME}"], "outputs": outputs}),
        encoding="utf-8",
    )
    return template, manifest


def test_a_template_that_agrees_with_its_course_passes(tmp_path: Path) -> None:
    template, manifest = write_course(tmp_path, outputs=["BucketName"])

    assert VALIDATOR.asset_problems(template, manifest, COMMIT) == []


def test_an_oversized_template_fails(tmp_path: Path) -> None:
    template, manifest = write_course(
        tmp_path, outputs=["BucketName"], padding=VALIDATOR.MAX_INLINE_TEMPLATE_BYTES
    )

    problems = VALIDATOR.asset_problems(template, manifest, COMMIT)

    assert len(problems) == 1 and "51,200 bytes" in problems[0]


def test_an_output_missing_from_the_template_fails(tmp_path: Path) -> None:
    template, manifest = write_course(tmp_path, outputs=["BucketName", "BucketArn"])

    problems = VALIDATOR.asset_problems(template, manifest, COMMIT)

    assert len(problems) == 1 and "BucketArn" in problems[0]


def test_a_mismatched_source_pin_fails(tmp_path: Path) -> None:
    template, manifest = write_course(tmp_path, outputs=["BucketName"])

    problems = VALIDATOR.asset_problems(template, manifest, "b" * 40)

    assert len(problems) == 1 and "startercode.commit" in problems[0]


def test_a_course_without_starter_code_needs_no_pin(tmp_path: Path) -> None:
    template, manifest = write_course(
        tmp_path, outputs=["BucketName"], commit="not-a-commit"
    )

    assert VALIDATOR.asset_problems(template, manifest, None) == []
