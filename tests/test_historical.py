import json
from pathlib import Path

from eblaghyar_catalog.historical import iter_historical_records
from eblaghyar_catalog.ingest import scan_historical
from eblaghyar_catalog.store import CatalogStore


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_source(root: Path, *, owner: str = "n-1", attachment_file: bool = True):
    repository = root / "notice_pipeline"
    folder = root / "1405-09-01__140500123__attachments-1"
    folder.mkdir(parents=True)
    notice_file = folder / "000_140500123_notice.pdf"
    notice_file.write_bytes(b"notice")
    rendered_file = folder / "001_attachment.pdf"
    if attachment_file:
        rendered_file.write_bytes(b"attachment")
    notice = {
        "notice_id": "n-1",
        "notice_no": "140500123",
        "case_no": "1405999",
        "archive_no": "55",
        "web_datetime": "1405/09/01-10:30",
        "issuer_unit_name": "شعبه حقوقی",
        "subject_type": "ابلاغ رأی",
        "notice_text": "مهلت اعتراض",
        "other_data_description": "توضیحات",
        "download_folder_name": folder.name,
        "notice_document": {"path": "C:/old-root/folder/000_140500123_notice.pdf"},
        "parties": [
            {"role": "خواهان", "name": "علی رضایی"},
            {"role": "خوانده", "name": "مریم احمدی"},
        ],
        "attachment_ids": ["a-1"],
    }
    attachment = {
        "attachment_id": "a-1",
        "notice_id": owner,
        "attachment_ordinal": 1,
        "title": "ضمیمه",
        "source_artifact": None,
        "rendered_artifact": {"path": "C:/old-root/folder/001_attachment.pdf"},
    }
    write_json(repository / "notices" / "items" / "n-1.json", notice)
    write_json(repository / "attachments" / "items" / "a-1.json", attachment)
    return repository, notice_file, rendered_file


def test_historical_adapter_preserves_order_and_recovers_stale_paths(tmp_path):
    repository, notice_file, rendered_file = make_source(tmp_path)
    folder = notice_file.parent
    second_file = folder / "002_attachment.pdf"
    second_file.write_bytes(b"second attachment")
    notice_record = repository / "notices" / "items" / "n-1.json"
    notice = json.loads(notice_record.read_text(encoding="utf-8"))
    notice["attachment_ids"] = ["a-2", "a-1"]
    write_json(notice_record, notice)
    write_json(repository / "attachments" / "items" / "a-2.json", {
        "attachment_id": "a-2",
        "notice_id": "n-1",
        "attachment_ordinal": 2,
        "rendered_artifact": {"path": "C:/old-root/folder/002_attachment.pdf"},
    })
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (notice_file, rendered_file, second_file)
    }

    records = list(iter_historical_records(tmp_path))

    assert len(records) == 1
    record = records[0]
    assert record["parties"] == [
        {"role": "خواهان", "name": "علی رضایی"},
        {"role": "خوانده", "name": "مریم احمدی"},
    ]
    assert record["attachment_ids"] == ["a-2", "a-1"]
    assert record["notice_source_path"] == notice_file
    assert [item["attachment_id"] for item in record["attachments"]] == ["a-2", "a-1"]
    assert [item["source_path"] for item in record["attachments"]] == [
        second_file,
        rendered_file,
    ]
    assert record["complete"] is True
    assert record["incomplete_reasons"] == []
    assert repository == tmp_path / "notice_pipeline"
    for path, expected in before.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == expected


def test_historical_adapter_marks_wrong_owner_incomplete(tmp_path):
    make_source(tmp_path, owner="another-notice")

    record = next(iter_historical_records(tmp_path))

    assert record["complete"] is False
    assert record["incomplete_reasons"] == ["attachment_owner_mismatch:a-1"]


def test_historical_adapter_reads_existing_ocr_sidecars_without_changing_them(
    tmp_path,
):
    _, notice_file, _ = make_source(tmp_path)
    ocr_dir = notice_file.parent / "ocr_text"
    ocr_dir.mkdir()
    notice_sidecar = ocr_dir / "000_notice_text.txt"
    notice_sidecar.write_text("مهلت اعتراض", encoding="utf-8")
    attachment_sidecar = ocr_dir / "001_attachment.pdf.txt"
    attachment_sidecar.write_text("متن OCR ضمیمه", encoding="utf-8")
    before = attachment_sidecar.read_bytes(), attachment_sidecar.stat().st_mtime_ns

    record = next(iter_historical_records(tmp_path))

    assert record["ocr_text"] == "متن OCR ضمیمه"
    assert (attachment_sidecar.read_bytes(), attachment_sidecar.stat().st_mtime_ns) == before


def test_historical_adapter_marks_missing_file_incomplete(tmp_path):
    make_source(tmp_path, attachment_file=False)

    record = next(iter_historical_records(tmp_path))

    assert record["complete"] is False
    assert record["incomplete_reasons"] == ["attachment_file_missing:a-1"]


def test_historical_adapter_rejects_recorded_path_outside_root(tmp_path):
    repository = tmp_path / "notice_pipeline"
    outside = tmp_path.parent / "outside-notice.pdf"
    outside.write_bytes(b"outside")
    write_json(repository / "notices" / "items" / "n-1.json", {
        "notice_id": "n-1",
        "download_folder_name": "missing-folder",
        "notice_document": {"path": str(outside)},
        "parties": [],
        "attachment_ids": [],
    })

    record = next(iter_historical_records(repository))

    assert record["notice_source_path"] is None
    assert record["incomplete_reasons"] == ["notice_file_missing"]


def test_historical_adapter_streams_without_enumerating_every_notice(
    monkeypatch, tmp_path
):
    repository, _, _ = make_source(tmp_path)
    notice_file = repository / "notices" / "items" / "n-1.json"
    original_glob = Path.glob

    def one_then_fail():
        yield notice_file
        raise AssertionError("iterator was consumed eagerly")

    def controlled_glob(path, pattern):
        if path == notice_file.parent and pattern == "*.json":
            return one_then_fail()
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", controlled_glob)

    record = next(iter_historical_records(tmp_path))

    assert record["notice_id"] == "n-1"


def test_historical_scan_indexes_once_and_keeps_source_read_only(tmp_path):
    repository, notice_file, rendered_file = make_source(tmp_path)
    source_files = [
        repository / "notices" / "items" / "n-1.json",
        repository / "attachments" / "items" / "a-1.json",
        notice_file,
        rendered_file,
    ]
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_files
    }
    store = CatalogStore(tmp_path / "catalog.sqlite")

    first = scan_historical(tmp_path, store)
    second = scan_historical(tmp_path, store)

    assert first == {
        "scanned": 1,
        "indexed": 1,
        "unchanged": 0,
        "incomplete": 0,
        "errors": [],
        "stopped": False,
    }
    assert second["indexed"] == 0
    assert second["unchanged"] == 1
    result = store.search("علي رضايي")[0]
    assert result["notice_no"] == "140500123"
    assert result["parties"][0]["role"] == "خواهان"
    assert result["source_kind"] == "historical"
    assert result["source_complete"] is True
    assert result["package_available"] is False
    for path, expected in before.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == expected


def test_historical_scan_indexes_incomplete_record_with_reason(tmp_path):
    make_source(tmp_path, attachment_file=False)
    store = CatalogStore(tmp_path / "catalog.sqlite")

    summary = scan_historical(tmp_path, store)
    result = store.search("140500123")[0]

    assert summary["incomplete"] == 1
    assert result["source_complete"] is False
    assert result["incomplete_reasons"] == ["attachment_file_missing:a-1"]
