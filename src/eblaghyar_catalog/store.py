from __future__ import annotations

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

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def upsert(self, manifest: dict, package_path: Path) -> None:
        parties = manifest.get("parties") or []
        searchable = " ".join(str(manifest.get(key) or "") for key in (
            "notice_no", "case_no", "archive_no", "issuer_unit_name",
            "subject_type", "document_type", "notice_text", "other_data_description",
        )) + " " + " ".join(
            f"{item.get('role') or ''} {item.get('name') or ''}"
            for item in parties if isinstance(item, dict)
        )
        values = (
            str(manifest["notice_id"]), manifest.get("notice_no"), manifest.get("case_no"),
            manifest.get("archive_no"), manifest.get("web_datetime"),
            manifest.get("issuer_unit_name"), manifest.get("subject_type"),
            json.dumps(parties, ensure_ascii=False),
            json.dumps(manifest, ensure_ascii=False), str(package_path.resolve()),
            manifest["package"]["sha256"], normalize(searchable),
        )
        with self._connect() as db:
            db.execute("""
                INSERT INTO notices VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(notice_id) DO UPDATE SET
                    notice_no=excluded.notice_no, case_no=excluded.case_no,
                    archive_no=excluded.archive_no, web_datetime=excluded.web_datetime,
                    issuer_unit_name=excluded.issuer_unit_name,
                    subject_type=excluded.subject_type, parties_json=excluded.parties_json,
                    manifest_json=excluded.manifest_json, package_path=excluded.package_path,
                    package_sha256=excluded.package_sha256, search_text=excluded.search_text
            """, values)

    def search(self, query: str, limit: int = 50) -> list[dict]:
        needle = normalize(query)
        if not needle:
            return []
        limit = min(max(int(limit), 1), 100)
        with self._connect() as db:
            rows = db.execute("""
                SELECT notice_id, notice_no, case_no, archive_no, web_datetime,
                       issuer_unit_name, subject_type, parties_json, package_path
                FROM notices
                WHERE search_text LIKE ? OR notice_no=? OR case_no=? OR archive_no=?
                ORDER BY web_datetime DESC, notice_no, notice_id LIMIT ?
            """, (f"%{needle}%", needle, needle, needle, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["parties"] = json.loads(item.pop("parties_json"))
            item["package_available"] = Path(item["package_path"]).is_file()
            result.append(item)
        return result
