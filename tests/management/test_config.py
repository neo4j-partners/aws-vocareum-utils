from pathlib import Path

import pytest

from vocareum_tools import config
from vocareum_tools.config import ConfigError, Environment


def test_environment_prefers_real_environment_even_when_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("TOKEN", "")

    environment = Environment.from_file(env_file)

    assert environment.get("TOKEN") == ""


def test_environment_parses_supported_dotenv_syntax(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "export FIRST='one'\nSECOND = \"two\"\n# ignored\nINVALID\n",
        encoding="utf-8",
    )

    environment = Environment.from_file(env_file)

    assert environment.get("FIRST") == "one"
    assert environment.get("SECOND") == "two"


def test_environment_reports_all_missing_values(tmp_path: Path) -> None:
    environment = Environment.from_file(tmp_path / "missing.env")

    with pytest.raises(ConfigError, match="FIRST, SECOND"):
        environment.require(("FIRST", "SECOND"))


def test_environment_reports_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.touch()

    def fail(*args: object, **kwargs: object) -> object:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "open", fail)

    with pytest.raises(ConfigError, match="Could not read environment file"):
        Environment.from_file(env_file)


def test_repository_root_falls_back_to_working_directory_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed_module = tmp_path / "site-packages" / "vocareum_tools" / "config.py"
    working_directory = tmp_path / "project"
    working_directory.mkdir()
    monkeypatch.setattr(config, "__file__", str(installed_module))
    monkeypatch.chdir(working_directory)

    assert config.repository_root() == working_directory


def write_course(tmp_path: Path, name: str, body: str) -> Path:
    """Create a course env file beside a base .env and return the base."""
    base = tmp_path / ".env"
    if not base.exists():
        base.write_text("", encoding="utf-8")
    courses = tmp_path / "courses"
    courses.mkdir(exist_ok=True)
    (courses / f"{name}.env").write_text(body, encoding="utf-8")
    return base


def test_course_file_layers_over_the_base_file(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text(
        "VOCAREUM_API_KEY=shared\nVOCAREUM_COURSE_ID=old\n", encoding="utf-8"
    )
    write_course(tmp_path, "graphrag", "VOCAREUM_COURSE_ID=new\n")

    environment = Environment.from_file(base, "graphrag")

    # The token is inherited so a rotated token is pasted once, and the course
    # id is overridden because that is what makes a course a different course.
    assert environment.get("VOCAREUM_API_KEY") == "shared"
    assert environment.get("VOCAREUM_COURSE_ID") == "new"
    assert environment.course == "graphrag"


def test_environment_records_the_file_that_supplied_each_value(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text("SHARED=base\nOVERRIDDEN=base\n", encoding="utf-8")
    write_course(tmp_path, "graphrag", "OVERRIDDEN=course\n")
    course = tmp_path / "courses" / "graphrag.env"

    environment = Environment.from_file(base, "graphrag")

    assert environment.source_of("SHARED") == base
    assert environment.source_of("OVERRIDDEN") == course
    assert environment.source_of("ABSENT") is None


def test_real_environment_still_beats_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / ".env"
    base.write_text("VOCAREUM_COURSE_ID=base\n", encoding="utf-8")
    write_course(tmp_path, "graphrag", "VOCAREUM_COURSE_ID=course\n")
    monkeypatch.setenv("VOCAREUM_COURSE_ID", "shell")

    environment = Environment.from_file(base, "graphrag")

    assert environment.get("VOCAREUM_COURSE_ID") == "shell"
    assert environment.is_shadowed("VOCAREUM_COURSE_ID") is True


def test_course_comes_from_the_environment_when_no_flag_is_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = write_course(tmp_path, "graphrag", "VOCAREUM_PART_ID=7\n")
    monkeypatch.setenv(config.COURSE_VAR, "graphrag")

    assert Environment.from_file(base).get("VOCAREUM_PART_ID") == "7"


def test_the_flag_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = write_course(tmp_path, "graphrag", "VOCAREUM_PART_ID=7\n")
    write_course(tmp_path, "hotel", "VOCAREUM_PART_ID=9\n")
    monkeypatch.setenv(config.COURSE_VAR, "graphrag")

    assert Environment.from_file(base, "hotel").get("VOCAREUM_PART_ID") == "9"


def test_no_course_reads_the_base_file_alone(tmp_path: Path) -> None:
    base = tmp_path / ".env"
    base.write_text("VOCAREUM_PART_ID=1\n", encoding="utf-8")
    write_course(tmp_path, "graphrag", "VOCAREUM_PART_ID=7\n")

    environment = Environment.from_file(base)

    # The single-course behavior this repository had before courses existed.
    # A course directory sitting there must not change it.
    assert environment.get("VOCAREUM_PART_ID") == "1"
    assert environment.course == ""
    assert environment.sources == (base,)


def test_a_named_course_with_no_file_is_an_error_naming_the_ones_that_exist(
    tmp_path: Path,
) -> None:
    base = write_course(tmp_path, "graphrag", "VOCAREUM_PART_ID=7\n")

    with pytest.raises(ConfigError) as error:
        Environment.from_file(base, "brazil")

    # Falling back to .env would run against whatever the base file said and
    # report success for a course it never touched.
    assert "brazil" in str(error.value)
    assert "graphrag" in str(error.value)


@pytest.mark.parametrize("name", ["../secrets", "a/b", "", ".", "-lead"])
def test_an_unusable_course_name_is_refused(tmp_path: Path, name: str) -> None:
    base = write_course(tmp_path, "graphrag", "")

    if not name:
        # An empty name selects no course rather than failing, which is what
        # an unset VOCAREUM_COURSE has to mean.
        assert Environment.from_file(base, name).course == ""
        return
    with pytest.raises(ConfigError):
        Environment.from_file(base, name)


def test_available_courses_ignores_the_example_file(tmp_path: Path) -> None:
    base = write_course(tmp_path, "graphrag", "")
    write_course(tmp_path, "hotel", "")
    (tmp_path / "courses" / "course.env.example").write_text("", encoding="utf-8")

    assert config.available_courses(base) == ["graphrag", "hotel"]


def test_describe_names_every_file_that_was_read(tmp_path: Path) -> None:
    base = write_course(tmp_path, "graphrag", "")

    described = Environment.from_file(base, "graphrag").describe()

    # Printed before the AWS calls start, because a run against the wrong
    # course is the failure this line exists to catch.
    assert "graphrag" in described
    assert str(base) in described
