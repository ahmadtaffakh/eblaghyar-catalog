import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from eblaghyar_catalog.exchange import publish_request
from eblaghyar_catalog.ingest import scan_exchange
from eblaghyar_catalog.manifest import ManifestValidationError, read_pair, sha256_file
from eblaghyar_catalog.store import CatalogStore


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
        "issuer_unit_name": "شعبه حقوقی",
        "subject_type": "ابلاغ رأی",
        "notice_text": "مهلت اعتراض",
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


def test_request_uses_flat_non_json_name_and_records_user(monkeypatch, tmp_path):
    monkeypatch.setattr("eblaghyar_catalog.exchange.active_directory_username", lambda: "ORG\\user")
    path = publish_request(tmp_path, "140500123")
    assert path.parent == tmp_path
    assert path.name.startswith("CAT-REQ--") and path.suffix == ".req"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["requested_by"] == "ORG\\user"
    assert not list(tmp_path.glob("*.part"))
