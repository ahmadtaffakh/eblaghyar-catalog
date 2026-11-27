from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(str.maketrans({
        "ك": "ک", "ي": "ی", "ى": "ی", "ۀ": "ه", "ة": "ه",
        "ـ": "", "‌": " ", "‏": "", "‎": "",
        **dict(zip("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")),
    }))
    return " ".join(text.casefold().split())


def _record_from_manifest(manifest: dict, package_path: Path) -> dict[str, Any]:
    parties = manifest.get("parties") or []
    searchable = " ".join(str(manifest.get(key) or "") for key in (
        "notice_no", "case_no", "archive_no", "issuer_unit_name",
        "subject_type", "document_type", "notice_text", "other_data_description",
    )) + " " + " ".join(
        f"{item.get('role') or ''} {item.get('name') or ''}"
        for item in parties if isinstance(item, dict)
    )
    return {
        "notice_id": str(manifest["notice_id"]),
        "notice_no": manifest.get("notice_no"),
        "case_no": manifest.get("case_no"),
        "archive_no": manifest.get("archive_no"),
        "web_datetime": manifest.get("web_datetime"),
        "issuer_unit_name": manifest.get("issuer_unit_name"),
        "subject_type": manifest.get("subject_type"),
        "parties": parties,
        "manifest": manifest,
        "package_path": str(package_path.resolve()),
        "package_sha256": manifest["package"]["sha256"],
        "search_text": normalize(searchable),
        "source_kind": "cmf",
        "source_complete": True,
        "incomplete_reasons": [],
        "source_fingerprint": manifest["package"]["sha256"],
    }


def _historical_file_state(path: Any) -> dict[str, Any] | None:
    if not isinstance(path, Path) or not path.is_file():
        return None
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _result_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["parties"] = json.loads(item.pop("parties_json"))
    manifest = json.loads(item.pop("manifest_json"))
    item["issue_date"] = manifest.get("issue_date")
    item["notice_text"] = manifest.get("notice_text")
    item["other_data_description"] = manifest.get("other_data_description")
    item["ocr_text"] = manifest.get("ocr_text") or manifest.get("extracted_text")
    item["incomplete_reasons"] = json.loads(item.pop("incomplete_reasons_json"))
    item["source_complete"] = bool(item["source_complete"])
    item["package_available"] = bool(item["package_path"]) and Path(
        item["package_path"]
    ).is_file()
    return item


def _record_from_historical(source: dict[str, Any]) -> dict[str, Any]:
    parties = list(source.get("parties") or [])
    attachments = [
        {
            "attachment_id": item.get("attachment_id"),
            "attachment_ordinal": item.get("attachment_ordinal"),
        }
        for item in source.get("attachments") or []
        if isinstance(item, dict)
    ]
    manifest = {
        "source_kind": "historical_repository",
        "notice_id": source["notice_id"],
        "notice_no": source.get("notice_no"),
        "case_no": source.get("case_no"),
        "archive_no": source.get("archive_no"),
        "web_datetime": source.get("web_datetime"),
        "issue_date": source.get("issue_date"),
        "issuer_unit_name": source.get("issuer_unit_name"),
        "subject_type": source.get("subject_type"),
        "notice_text": source.get("notice_text"),
        "other_data_description": source.get("other_data_description"),
        "ocr_text": source.get("ocr_text"),
        "parties": parties,
        "attachment_ids": list(source.get("attachment_ids") or []),
        "attachments": attachments,
        "complete": bool(source.get("complete")),
        "incomplete_reasons": list(source.get("incomplete_reasons") or []),
    }
    file_state = {
        "notice": _historical_file_state(source.get("notice_source_path")),
        "attachments": [
            _historical_file_state(item.get("source_path"))
            for item in source.get("attachments") or []
            if isinstance(item, dict)
        ],
    }
    fingerprint_input = json.dumps(
        {"record": manifest, "files": file_state},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    searchable = " ".join(str(manifest.get(key) or "") for key in (
        "notice_no", "case_no", "archive_no", "issuer_unit_name",
        "subject_type", "notice_text", "other_data_description",
    )) + " " + " ".join(
        f"{item.get('role') or ''} {item.get('name') or ''}"
        for item in parties if isinstance(item, dict)
    )
    return {
        "notice_id": str(source["notice_id"]),
        "notice_no": source.get("notice_no"),
        "case_no": source.get("case_no"),
        "archive_no": source.get("archive_no"),
        "web_datetime": source.get("web_datetime"),
        "issuer_unit_name": source.get("issuer_unit_name"),
        "subject_type": source.get("subject_type"),
        "parties": parties,
        "manifest": manifest,
        "package_path": "",
        "package_sha256": "",
        "search_text": normalize(searchable),
        "source_kind": "historical",
        "source_complete": bool(source.get("complete")),
        "incomplete_reasons": list(source.get("incomplete_reasons") or []),
        "source_fingerprint": hashlib.sha256(fingerprint_input).hexdigest(),
    }


class CatalogStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS notices(
                    notice_id TEXT PRIMARY KEY,
                    notice_no TEXT, case_no TEXT, archive_no TEXT,
                    web_datetime TEXT, issuer_unit_name TEXT, subject_type TEXT,
                    parties_json TEXT NOT NULL, manifest_json TEXT NOT NULL,
                    package_path TEXT NOT NULL, package_sha256 TEXT NOT NULL,
                    search_text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_notice_no ON notices(notice_no);
                CREATE INDEX IF NOT EXISTS ix_case_no ON notices(case_no);
                CREATE INDEX IF NOT EXISTS ix_archive_no ON notices(archive_no);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(notices)")}
            migrations = {
                "source_kind": "TEXT NOT NULL DEFAULT 'cmf'",
                "source_complete": "INTEGER NOT NULL DEFAULT 1",
                "incomplete_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
                "source_fingerprint": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE notices ADD COLUMN {name} {declaration}")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def upsert(self, manifest: dict, package_path: Path) -> None:
        self.upsert_record(_record_from_manifest(manifest, package_path))

    def upsert_historical(self, source: dict[str, Any]) -> bool:
        record = _record_from_historical(source)
        with self._connect() as db:
            current = db.execute(
                "SELECT source_kind, package_path, source_fingerprint "
                "FROM notices WHERE notice_id=?",
                (record["notice_id"],),
            ).fetchone()
        if current and current["source_kind"] == "cmf" and current["package_path"]:
            return False
        if current and current["source_fingerprint"] == record["source_fingerprint"]:
            return False
        self.upsert_record(record)
        return True

    def upsert_record(self, record: dict[str, Any]) -> None:
        values = (
            record["notice_id"], record.get("notice_no"), record.get("case_no"),
            record.get("archive_no"), record.get("web_datetime"),
            record.get("issuer_unit_name"), record.get("subject_type"),
            json.dumps(record["parties"], ensure_ascii=False),
            json.dumps(record["manifest"], ensure_ascii=False), record["package_path"],
            record["package_sha256"], record["search_text"],
            record.get("source_kind", "cmf"),
            int(record.get("source_complete", True)),
            json.dumps(record.get("incomplete_reasons") or [], ensure_ascii=False),
            record.get("source_fingerprint", ""),
        )
        with self._connect() as db:
            db.execute("""
                INSERT INTO notices(
                    notice_id, notice_no, case_no, archive_no, web_datetime,
                    issuer_unit_name, subject_type, parties_json, manifest_json,
                    package_path, package_sha256, search_text, source_kind,
                    source_complete, incomplete_reasons_json, source_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(notice_id) DO UPDATE SET
                    notice_no=excluded.notice_no, case_no=excluded.case_no,
                    archive_no=excluded.archive_no, web_datetime=excluded.web_datetime,
                    issuer_unit_name=excluded.issuer_unit_name,
                    subject_type=excluded.subject_type, parties_json=excluded.parties_json,
                    manifest_json=excluded.manifest_json, package_path=excluded.package_path,
                    package_sha256=excluded.package_sha256, search_text=excluded.search_text,
                    source_kind=excluded.source_kind,
                    source_complete=excluded.source_complete,
                    incomplete_reasons_json=excluded.incomplete_reasons_json,
                    source_fingerprint=excluded.source_fingerprint
            """, values)

    def search(self, query: str, limit: int = 50) -> list[dict]:
        needle = normalize(query)
        if not needle:
            return []
        limit = min(max(int(limit), 1), 100)
        with self._connect() as db:
            rows = db.execute("""
                SELECT notice_id, notice_no, case_no, archive_no, web_datetime,
                       issuer_unit_name, subject_type, parties_json, package_path,
                       source_kind, source_complete, incomplete_reasons_json,
                       manifest_json
                FROM notices
                WHERE search_text LIKE ? OR notice_no=? OR case_no=? OR archive_no=?
                ORDER BY web_datetime DESC, notice_no, notice_id LIMIT ?
            """, (f"%{needle}%", needle, needle, needle, limit)).fetchall()
        return [_result_from_row(row) for row in rows]

    def recent(self, limit: int = 50) -> list[dict]:
        limit = min(max(int(limit), 1), 100)
        with self._connect() as db:
            rows = db.execute("""
                SELECT notice_id, notice_no, case_no, archive_no, web_datetime,
                       issuer_unit_name, subject_type, parties_json, package_path,
                       source_kind, source_complete, incomplete_reasons_json,
                       manifest_json
                FROM notices
                ORDER BY web_datetime DESC, notice_no, notice_id LIMIT ?
            """, (limit,)).fetchall()
        return [_result_from_row(row) for row in rows]

    def notice_counts(self) -> dict[str, int]:
        with self._connect() as db:
            row = db.execute("""
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(source_complete), 0) AS complete
                FROM notices
            """).fetchone()
        return {"total": int(row["total"]), "complete": int(row["complete"])}
