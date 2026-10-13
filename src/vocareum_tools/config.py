"""Shared configuration loading for the command-line tools.

One repository, more than one course. The values that say *which* course a tool
is acting on are the course, assignment, part, and user ids, plus the lab
account and stack name that belong to that course's Vocareum part. The values
that say *who is acting* are the API token and the region. Only the first set
changes per course, so the second set would have to be copied into every
course's file if a course file simply replaced ``.env``, and a rotated token
would then have to be pasted in as many places as there are courses.

So a course file is layered over ``.env`` rather than substituted for it. Real
environment variables beat both, which is what keeps CI working unchanged.
:func:`Environment.from_file` reads the base file, then the selected course's
file from ``courses/<name>.env`` beside it, and the later values win.

A course is selected by ``--course NAME`` on any tool, or by ``VOCAREUM_COURSE``
in the real environment or in ``.env``. Selecting none is the single-course
behavior this repository had before, and it stays the default: ``.env`` alone.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


# What it takes to address a part, and what it takes to address one student's
# session within it. The second is the first plus the user, mirroring the
# PartEndpoints and SessionEndpoints pair these variables are read into. Upload
# needs only the part variables, since a workspace belongs to the part rather
# than to a student.
VOCAREUM_PART_VARS = (
    "VOCAREUM_API_KEY",
    "VOCAREUM_COURSE_ID",
    "VOCAREUM_ASSIGNMENT_ID",
    "VOCAREUM_PART_ID",
)
VOCAREUM_SESSION_VARS = (*VOCAREUM_PART_VARS, "VOCAREUM_USER_ID")

# Which variable selects a course, and where a course's file lives. The
# directory sits beside the base env file rather than at a fixed path, so
# `--env-file ~/elsewhere/.env` picks up `~/elsewhere/courses/` and a course
# kept outside this checkout needs no second flag.
COURSE_VAR = "VOCAREUM_COURSE"
COURSES_DIR_NAME = "courses"
COURSE_ENV_SUFFIX = ".env"

# Where one course's committed assets live, when they do not live here. A
# workshop that owns its own repository owns its Vocareum definition too, so
# this names that repository's `vocareum/` directory and the asset lookups
# follow it instead of `courses/<name>/`. Absent is the default and means the
# assets are in this checkout, which is what the local test course does.
#
# Read from the course env file rather than from `course.json`, because the
# definition being looked for is itself inside the directory this value names.
COURSE_ASSET_DIR_VAR = "COURSE_ASSET_DIR"

# A course name is spliced into a filesystem path, so it is checked rather than
# trusted. The pattern also rules out `..` and every separator, which is the
# point: a name is a name, never a path.
COURSE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def read_dotenv(path: Path) -> dict[str, str]:
    """Load a minimal dotenv file, or nothing at all when it is absent.

    An absent file is not an error here. A tool that needs values says so
    through :meth:`Environment.require`, which names every missing variable at
    once, and that reads better than a complaint about a path.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line.removeprefix("export ").lstrip()
                key, separator, value = line.partition("=")
                key = key.strip()
                if not separator or not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    except OSError as error:
        raise ConfigError(f"Could not read environment file {path}: {error}") from error
    return values


def courses_dir(base: Path) -> Path:
    """Return the directory holding the course env files beside ``base``."""
    return base.parent / COURSES_DIR_NAME


def course_env_path(base: Path, course: str) -> Path:
    """Return the env file for one course, beside ``base``."""
    return courses_dir(base) / f"{course}{COURSE_ENV_SUFFIX}"


def available_courses(base: Path) -> list[str]:
    """Return the course names that have a file, sorted.

    ``course.env.example`` does not match the glob, so the documentation file in
    the directory is never offered as a course.
    """
    directory = courses_dir(base)
    if not directory.is_dir():
        return []
    return sorted(item.stem for item in directory.glob(f"*{COURSE_ENV_SUFFIX}"))


def course_asset_dir(base: Path, course: str) -> Path | None:
    """Return the external asset directory for ``course``, or ``None``.

    Read directly rather than through :class:`Environment`, for two reasons. A
    caller may ask before the course env file exists, which
    :meth:`Environment.from_file` treats as an error and this does not. And the
    answer is a path rather than a course value, so it is wanted by the asset
    helpers in :mod:`vocareum_tools.course`, which are handed a base path and a
    name and nothing else.

    Precedence matches :meth:`Environment.get`: a real environment variable
    beats the course file, which beats the base file.
    """
    if COURSE_ASSET_DIR_VAR in os.environ:
        raw = os.environ[COURSE_ASSET_DIR_VAR].strip()
    else:
        values = read_dotenv(base)
        values.update(read_dotenv(course_env_path(base, course)))
        raw = values.get(COURSE_ASSET_DIR_VAR, "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base.parent / candidate
    # Collapsed textually rather than with `resolve`, so the printed path stays
    # readable without following a symlink the operator may have put there on
    # purpose.
    return Path(os.path.normpath(candidate))


@dataclass(frozen=True)
class Environment:
    """Environment variables layered over one or more dotenv files."""

    path: Path
    file_values: dict[str, str]
    # Every file that was read, base first. Kept for the failure message: a
    # missing variable is nearly always a variable that went into the wrong one
    # of the two files, and naming only the base file sends the reader there.
    sources: tuple[Path, ...] = ()
    course: str = ""
    # The file that supplied each layered value. Unlike ``sources``, this is
    # precise enough for a command to update the same file it read without
    # accidentally writing beneath a course override.
    value_sources: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path, course: str | None = None) -> Environment:
        """Load ``path``, then layer the selected course's file over it.

        The course comes from the ``course`` argument, then ``VOCAREUM_COURSE``
        in the real environment, then ``VOCAREUM_COURSE`` in the base file. A
        named course whose file is absent is an error naming the courses that do
        exist, because the alternative is a tool that runs against whatever the
        base file happened to say and reports success for the wrong course.
        """
        base_values = read_dotenv(path)
        selected = (
            course or os.environ.get(COURSE_VAR) or base_values.get(COURSE_VAR, "")
        ).strip()

        values = dict(base_values)
        value_sources = dict.fromkeys(base_values, path)
        sources = [path]
        if selected:
            if not COURSE_NAME_PATTERN.match(selected):
                raise ConfigError(
                    f"Course name {selected!r} is not usable. A course name is "
                    "letters, digits, dot, dash and underscore only, because it "
                    "names a file in "
                    f"{courses_dir(path)}."
                )
            course_path = course_env_path(path, selected)
            if not course_path.is_file():
                known = available_courses(path)
                offer = ", ".join(known) if known else "none found"
                raise ConfigError(
                    f"Course {selected!r} has no file at {course_path}.\n"
                    f"Courses with a file: {offer}."
                )
            course_values = read_dotenv(course_path)
            values.update(course_values)
            value_sources.update(dict.fromkeys(course_values, course_path))
            sources.append(course_path)

        return cls(
            path=path,
            file_values=values,
            sources=tuple(sources),
            course=selected,
            value_sources=value_sources,
        )

    def get(self, name: str, default: str = "") -> str:
        """Return an environment value, falling back to the dotenv files."""
        if name in os.environ:
            return os.environ[name].strip()
        return self.file_values.get(name, default).strip()

    def source_of(self, name: str) -> Path | None:
        """Return the dotenv file that supplied ``name``, before shell overrides."""
        return self.value_sources.get(name)

    def is_shadowed(self, name: str) -> bool:
        """Report whether a real environment variable overrides the file value."""
        return name in os.environ

    def describe(self) -> str:
        """Return a one-line account of where values came from.

        Printed by the tools that report which course they acted on. A run
        against the wrong course is the failure this line exists to catch, and
        it costs nothing to make the answer visible before the AWS calls start.
        """
        files = ", ".join(str(source) for source in self.sources) or "no file"
        if self.course:
            return f"course={self.course} ({files}, real environment wins)"
        return f"course=none ({files}, real environment wins)"

    def require(self, names: tuple[str, ...]) -> None:
        """Raise once with every required value that is missing."""
        missing = [name for name in names if not self.get(name)]
        if missing:
            checked = ", ".join(str(source) for source in self.sources)
            checked = checked or str(self.path)
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + f"\nChecked real environment variables and {checked}."
            )


def repository_root() -> Path:
    """Return the source checkout root, or the working directory when installed."""
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file():
        return source_root
    return Path.cwd()


def add_course_argument(parser: argparse.ArgumentParser) -> None:
    """Add ``--course`` to a tool's parser, worded the same way everywhere.

    Every tool takes it, so the help text is written once. A tool that spelled
    it differently would read as a different flag with a different meaning.
    """
    parser.add_argument(
        "--course",
        default=None,
        metavar="NAME",
        help=(
            "Course to act on. Layers courses/NAME.env over the base .env. "
            f"Defaults to {COURSE_VAR}, or to the base .env alone."
        ),
    )
