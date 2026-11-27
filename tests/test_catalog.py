import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from eblaghyar_catalog.exchange import list_user_requests, publish_request
from eblaghyar_catalog.ingest import scan_exchange
from eblaghyar_catalog.manifest import (
    ManifestValidationError,
    copy_validated_archive,
    read_pair,
    sha256_file,
)
from eblaghyar_catalog.store import CatalogStore
from eblaghyar_catalog.ui import (
    incomplete_reason_text,
    party_names_text,
    record_details_text,
    record_status,
    request_status_text,
    summary_text,
    table_sort_key,
)


def make_pair(root: Path):
    archive = root / "notice-1.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("notice.pdf", b"notice")
        output.writestr("attachment.pdf", b"attachment")
    manifest = {
        "schema_version": 2,
        "notice_id": "n-1",
        "notice_no": "140500123",
        "case_no": "1405999",
        "archive_no": "55",
        "web_datetime": "1405/09/01-10:30",
        "issue_date": "1405/08/30",
        "issuer_unit_name": "شعبه حقوقی",
        "subject_type": "ابلاغ رأی",
        "notice_text": "مهلت اعتراض",
        "other_data_description": "تاریخ حضور :1405/09/18-10:00</br>علت حضور: رسیدگی",
        "ocr_text": "متن استخراج‌شده ضمیمه",
        "parties": [{"role": "تجدیدنظرخواه", "name": "علی رضایی"}],
        "attachment_ids": ["a-1"],
        "files": [
            {"kind": "notice", "attachment_id": None, "notice_id": "n-1", "ordinal": 0,
             "filename": "notice.pdf", "size": 6,
             "sha256": hashlib.sha256(b"notice").hexdigest()},
            {"kind": "attachment", "attachment_id": "a-1", "notice_id": "n-1", "ordinal": 1,
             "filename": "attachment.pdf", "size": 10,
             "sha256": hashlib.sha256(b"attachment").hexdigest()},
        ],
        "package": {"filename": archive.name, "size": archive.stat().st_size,
                    "sha256": sha256_file(archive)},
        "consumers": {"catalog": {"index": True}, "pargar": {
            "attachment": archive.name, "subject": "ابلاغیه 140500123", "body": "متن"
        }},
    }
    archive.with_suffix(".cmf").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return archive


def test_pair_is_validated_indexed_and_searched(tmp_path):
    archive = make_pair(tmp_path)
    assert read_pair(archive)["consumers"]["pargar"]["attachment"] == archive.name
    store = CatalogStore(tmp_path / "catalog.sqlite")
    assert scan_exchange(tmp_path, store)["indexed"] == 1
    assert store.search("علي رضايي")[0]["notice_no"] == "140500123"


def test_rescan_is_idempotent_and_preserves_party_order(tmp_path):
    archive = make_pair(tmp_path)
    manifest_path = archive.with_suffix(".cmf")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parties"] = [
        {"role": "خواهان", "name": "علی رضایی"},
        {"role": "خوانده", "name": "مریم احمدی"},
    ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    store = CatalogStore(tmp_path / "catalog.sqlite")

    assert scan_exchange(tmp_path, store)["indexed"] == 1
    assert scan_exchange(tmp_path, store)["indexed"] == 1

    results = store.search("۱۲۳")
    assert len(results) == 1
    assert results[0]["parties"] == manifest["parties"]


def test_tampered_zip_is_rejected(tmp_path):
    archive = make_pair(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(ManifestValidationError, match="size"):
        read_pair(archive)


def test_validated_package_copy_preserves_content_and_never_overwrites(tmp_path):
    archive = make_pair(tmp_path)
    destination = tmp_path / "downloaded.zip"

    assert copy_validated_archive(archive, destination) == destination
    assert destination.read_bytes() == archive.read_bytes()

    destination.write_bytes(b"keep existing")
    with pytest.raises(FileExistsError):
        copy_validated_archive(archive, destination)
    assert destination.read_bytes() == b"keep existing"


def test_unsupported_manifest_schema_is_rejected(tmp_path):
    archive = make_pair(tmp_path)
    manifest_path = archive.with_suffix(".cmf")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 3
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ManifestValidationError, match="unsupported CMF schema"):
        read_pair(archive)


def test_unsafe_zip_member_is_rejected(tmp_path):
    archive = make_pair(tmp_path)
    manifest_path = archive.with_suffix(".cmf")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../notice.pdf", b"notice")
        output.writestr("attachment.pdf", b"attachment")
    manifest["files"][0]["filename"] = "../notice.pdf"
    manifest["package"]["size"] = archive.stat().st_size
    manifest["package"]["sha256"] = sha256_file(archive)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ManifestValidationError, match="unsafe path"):
        read_pair(archive)


def test_request_uses_flat_non_json_name_and_records_user(monkeypatch, tmp_path):
    monkeypatch.setattr("eblaghyar_catalog.exchange.active_directory_username", lambda: "ORG\\user")
    path = publish_request(tmp_path, "140500123")
    assert path.parent == tmp_path
    assert path.name.startswith("CAT-REQ--") and path.suffix == ".req"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["requested_by"] == "ORG\\user"
    assert not list(tmp_path.glob("*.part"))


def test_request_status_lists_only_current_users_complete_files(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "eblaghyar_catalog.exchange.active_directory_username", lambda: "ORG\\user"
    )
    request_path = publish_request(tmp_path, "140500123")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    response_path = tmp_path / f"CAT-RSP--{request['request_id']}.rsp"
    response_path.write_text(json.dumps({
        "schema_version": 1,
        "request_id": request["request_id"],
        "status": "ready",
        "detail": "package created",
        "updated_at": "2026-11-27T10:00:00+00:00",
        "package_name": "notice.zip",
    }), encoding="utf-8")
    (tmp_path / "CAT-REQ--ignored.req.part").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "eblaghyar_catalog.exchange.active_directory_username", lambda: "ORG\\other"
    )
    publish_request(tmp_path, "999")
    monkeypatch.setattr(
        "eblaghyar_catalog.exchange.active_directory_username", lambda: "ORG\\user"
    )

    items = list_user_requests(tmp_path)

    assert len(items) == 1
    assert items[0]["notice_no"] == "140500123"
    assert items[0]["status"] == "ready"
    assert items[0]["package_name"] == "notice.zip"
    assert request_status_text(items[0]["status"]) == "بسته آماده"


def test_ui_summary_reports_historical_scan_counts():
    assert summary_text({
        "scanned": 3,
        "indexed": 1,
        "unchanged": 1,
        "incomplete": 1,
        "errors": [],
        "stopped": False,
    }) == (
        "بررسی‌شده: 3 | ثبت/به‌روزرسانی: 1 | بدون تغییر: 1 | "
        "دارای فایل ناقص: 1"
    )


def test_ui_distinguishes_historical_index_from_file_and_package_status():
    assert record_status({
        "source_kind": "historical",
        "source_complete": False,
        "package_available": False,
    }) == "سابقه؛ فایل ناقص"
    assert record_status({
        "source_kind": "historical",
        "source_complete": True,
        "package_available": False,
    }) == "مبدأ کامل؛ ZIP آماده نیست"
    assert record_status({"package_available": True}) == "بسته آماده"
    assert incomplete_reason_text(
        "attachment_file_missing:a-1"
    ) == "فایل ضمیمه یافت نشد (a-1)"


def test_table_sort_key_normalizes_persian_digits_and_text():
    assert table_sort_key("۱۲") == table_sort_key("12") == (0, 12)
    assert table_sort_key("علي") == table_sort_key("علی")


def test_search_result_contains_texts_used_by_viewer_details(tmp_path):
    archive = make_pair(tmp_path)
    store = CatalogStore(tmp_path / "catalog.sqlite")
    store.upsert(read_pair(archive), archive)

    result = store.search("140500123")[0]
    details = record_details_text(result)

    assert result["issue_date"] == "1405/08/30"
    assert result["notice_text"] == "مهلت اعتراض"
    assert result["ocr_text"] == "متن استخراج‌شده ضمیمه"
    assert "تاریخ حضور :1405/09/18-10:00\nعلت حضور: رسیدگی" in details
    assert "متن استخراج‌شده ضمیمه" in details
    assert party_names_text(result) == "علی رضایی"


def test_recent_returns_latest_notices_without_changing_empty_search(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite")
    common = {
        "case_no": None,
        "archive_no": None,
        "issuer_unit_name": None,
        "subject_type": None,
        "parties": [],
        "manifest": {},
        "package_path": "",
        "package_sha256": "",
        "search_text": "",
    }
    store.upsert_record({
        **common,
        "notice_id": "older",
        "notice_no": "100",
        "web_datetime": "1405/09/01-10:00",
    })
    store.upsert_record({
        **common,
        "notice_id": "newer",
        "notice_no": "200",
        "web_datetime": "1405/09/02-10:00",
    })

    assert [item["notice_id"] for item in store.recent()] == ["newer", "older"]
    assert store.recent(1)[0]["notice_id"] == "newer"
    assert store.search("") == []


def test_historical_record_does_not_replace_valid_package(tmp_path):
    archive = make_pair(tmp_path)
    store = CatalogStore(tmp_path / "catalog.sqlite")
    store.upsert(read_pair(archive), archive)

    changed = store.upsert_historical({
        "notice_id": "n-1",
        "notice_no": "old-number",
        "parties": [],
        "attachment_ids": [],
        "attachments": [],
        "notice_source_path": None,
        "complete": False,
        "incomplete_reasons": ["notice_file_missing"],
    })

    result = store.search("140500123")[0]
    assert changed is False
    assert result["source_kind"] == "cmf"
    assert result["package_available"] is True
