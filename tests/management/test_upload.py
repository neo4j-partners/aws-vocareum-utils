import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from vocareum_tools import upload
from vocareum_tools.client import ApiError, TransactionResult


def make_config() -> upload.Config:
    return upload.Config(
        api_base="https://example.test/api/v2",
        course_id="course",
        assignment_id="assignment",
        part_id="part",
        api_key="api-key",
        course="test",
    )


def test_show_current_file_treats_only_not_found_as_missing(
    monkeypatch: Any,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ApiError("forbidden", status_code=403)

    monkeypatch.setattr(upload, "api_json", fail)

    with pytest.raises(ApiError, match="forbidden"):
        upload.show_current_file(make_config(), "private/lab.template")


def test_show_current_file_accepts_confirmed_not_found(monkeypatch: Any) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ApiError("missing", status_code=404)

    monkeypatch.setattr(upload, "api_json", fail)

    assert upload.show_current_file(make_config(), "private/lab.template") is None


def test_show_current_file_rejects_unknown_success_shape(monkeypatch: Any) -> None:
    monkeypatch.setattr(upload, "api_json", lambda *args, **kwargs: {"files": []})

    with pytest.raises(ApiError, match="unknown response shape"):
        upload.show_current_file(make_config(), "private/lab.template")


def test_read_remote_file_returns_downloaded_bytes(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        upload,
        "api_json",
        lambda *args, **kwargs: {"response": {"download_url": "https://x/file"}},
    )
    monkeypatch.setattr(upload, "download", lambda url: b"saved template")

    assert (
        upload.read_remote_file(make_config(), "private/lab.template")
        == b"saved template"
    )


def test_find_download_url_reaches_a_nested_url() -> None:
    payload = {"response": {"files": [{"name": "lab.template"}, {"download_url": "u"}]}}

    assert upload.find_download_url(payload) == "u"


@pytest.mark.parametrize(
    "payload",
    [{}, {"download_url": ""}, {"download_url": "   "}, {"download_url": 5}, []],
    ids=["absent", "empty", "blank", "not-a-string", "empty-list"],
)
def test_find_download_url_refuses_a_url_it_cannot_fetch(payload: Any) -> None:
    """A blank URL has to read as absent.

    show_current_file turns a missing URL into an error rather than into a
    missing file, so a blank one that passed for present would be downloaded
    and would fail somewhere less informative.
    """
    assert upload.find_download_url(payload) is None


def test_build_zip_is_deterministic_and_compressed() -> None:
    first = upload.build_zip("lab.template", b"x" * 1000)
    second = upload.build_zip("lab.template", b"x" * 1000)

    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        info = archive.getinfo("lab.template")
        assert info.compress_type == zipfile.ZIP_DEFLATED
        assert info.compress_size < info.file_size


@pytest.mark.parametrize("value", ["", ".", "..", "../lab.template", "a/b", "a\\b"])
def test_validate_path_segment_rejects_unsafe_names(value: str) -> None:
    with pytest.raises(upload.ConfigError):
        upload.validate_path_segment(value, "--name")


def test_unknown_target_warns_without_blocking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    upload.warn_unknown_target("not-a-real-folder")

    assert "WARNING" in capsys.readouterr().out


def test_known_target_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    upload.warn_unknown_target(upload.DEFAULT_TARGET)

    assert capsys.readouterr().out == ""


def course_config(tmp_path: Path, course: str = "graphrag") -> upload.Config:
    return upload.Config(
        api_base="https://example.test/api/v2",
        course_id="100002",
        assignment_id="100003",
        part_id="100004",
        api_key="api-key",
        course=course,
        env_path=tmp_path / ".env",
    )


def test_named_course_selects_its_template_automatically(tmp_path: Path) -> None:
    template = tmp_path / "courses" / "graphrag" / "lab.template"
    template.parent.mkdir(parents=True)
    template.write_text("Resources: {}\n", encoding="utf-8")

    assert (
        upload.source_path(course_config(tmp_path), upload.parse_args([])) == template
    )


def test_named_course_refuses_the_root_template(tmp_path: Path) -> None:
    root_template = tmp_path / "lab.template"
    root_template.write_text("Resources: {}\n", encoding="utf-8")
    args = upload.parse_args([str(root_template)])

    with pytest.raises(upload.ConfigError, match=r"can upload lab\.template only"):
        upload.source_path(course_config(tmp_path), args)


def test_write_without_a_named_course_is_refused_before_an_api_call(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "artifact.txt"
    source.write_text("payload", encoding="utf-8")
    config = course_config(tmp_path, course="")
    args = upload.parse_args([str(source)])

    monkeypatch.setattr(
        upload,
        "api_json",
        lambda *args, **kwargs: pytest.fail("API call must not happen"),
    )

    with pytest.raises(upload.ConfigError, match="requires a named course"):
        upload.run_upload(config, args)


def test_upload_finds_nested_transaction_id(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        upload,
        "api_json",
        lambda *args, **kwargs: {"response": {"Transaction-ID": 123}},
    )

    transaction_id = upload.upload(
        make_config(), "private", "lab.template", b"Resources: {}\n"
    )

    assert transaction_id == "123"


def test_upload_archive_can_reset_a_complete_target(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def request(method: str, url: str, api_key: str, body: Any) -> dict[str, Any]:
        captured.update(body)
        return {"transactionid": "starter-1"}

    monkeypatch.setattr(upload, "api_json", request)

    transaction_id = upload.upload_archive(
        make_config(), "startercode", b"ready zip", reset=True
    )

    assert transaction_id == "starter-1"
    assert captured["content"][0]["target"] == "startercode"
    assert captured["content"][0]["reset"] == 1


def test_poll_transaction_redacts_message(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        upload,
        "wait_for_transaction",
        lambda *args, **kwargs: TransactionResult(
            "failed",
            {
                "state": "failed",
                "message": (
                    "url=https://example.test/?X-Amz-Signature=secret-signature"
                ),
            },
        ),
    )

    upload.poll_transaction(make_config(), "transaction-1")

    assert "secret-signature" not in capsys.readouterr().out


def test_missing_transaction_id_cannot_report_success(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "artifact.txt"
    source.write_text("Resources: {}\n", encoding="utf-8")
    args = upload.parse_args([str(source), "--no-verify"])
    monkeypatch.setattr(upload, "show_current_file", lambda *args: None)
    monkeypatch.setattr(upload, "upload", lambda *args: None)

    result = upload.run_upload(make_config(), args)

    assert result == upload.EXIT_API


def test_hash_mismatch_after_an_accepted_write_is_not_success(
    tmp_path: Path, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """An accepted write whose bytes come back different must not read as done.

    Proving the file arrived intact is the whole reason this tool downloads it
    again, so the path that reports a mismatch is worth pinning.
    """
    source = tmp_path / "artifact.txt"
    source.write_text("Resources: {}\n", encoding="utf-8")
    args = upload.parse_args([str(source)])
    monkeypatch.setattr(upload, "show_current_file", lambda *args: None)
    monkeypatch.setattr(upload, "upload", lambda *args: "transaction-1")
    monkeypatch.setattr(upload, "poll_transaction", lambda *args: "success")
    monkeypatch.setattr(
        upload, "api_json", lambda *args, **kwargs: {"download_url": "https://x/f"}
    )
    monkeypatch.setattr(upload, "download", lambda url: b"something else entirely")

    result = upload.run_upload(make_config(), args)

    assert result == upload.EXIT_MISMATCH
    assert "does not match" in capsys.readouterr().out


def test_verified_upload_reports_success(tmp_path: Path, monkeypatch: Any) -> None:
    payload = b"Resources: {}\n"
    source = tmp_path / "artifact.txt"
    source.write_bytes(payload)
    args = upload.parse_args([str(source)])
    monkeypatch.setattr(upload, "show_current_file", lambda *args: None)
    monkeypatch.setattr(upload, "upload", lambda *args: "transaction-1")
    monkeypatch.setattr(upload, "poll_transaction", lambda *args: "success")
    monkeypatch.setattr(
        upload, "api_json", lambda *args, **kwargs: {"download_url": "https://x/f"}
    )
    monkeypatch.setattr(upload, "download", lambda url: payload)

    assert upload.run_upload(make_config(), args) == upload.EXIT_OK
