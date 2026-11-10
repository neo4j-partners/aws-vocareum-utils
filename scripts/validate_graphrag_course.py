#!/usr/bin/env -S uv run
"""Check the graphrag course assets before vocareum-release sends them.

``release.validate_course`` runs ``scripts/validate_<course>_course.py`` and
refuses to release when it is missing. This script was never committed, so
every release stopped at that refusal. It runs the same per-course check as
``validate_all.py`` rather than restating it, so the two cannot drift. It skips
pytest, ruff, and cfn-lint: the release runs cfn-lint on the template itself.
"""

from __future__ import annotations

import sys

from validate_all import course_asset_problems

from vocareum_tools.config import ConfigError

COURSE = "graphrag"


def main() -> int:
    """Print every asset problem for the course and fail on any."""
    try:
        problems = course_asset_problems(COURSE)
    except ConfigError as error:
        print(f"FAIL  {error}", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"FAIL  {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"ok    course assets for {COURSE}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
