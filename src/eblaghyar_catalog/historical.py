from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class HistoricalSourceError(ValueError):
    pass


def _repository_root(source_root: str | Path) -> Path:
    root = Path(source_root).resolve()
    for candidate in (root, root / "notice_pipeline"):
        if (candidate / "notices" / "items").is_dir():
            return candidate
    raise HistoricalSourceError("Eblaghyar notice repository was not found")


def _artifact_roots(source_root: str | Path, repository_root: Path) -> tuple[Path, ...]:
    selected = Path(source_root).resolve()
    candidates = [selected, repository_root]
    if selected == repository_root and repository_root.name == "notice_pipeline":
        candidates.append(repository_root.parent)
    return tuple(dict.fromkeys(candidates))


def _inside(candidate: Path, roots: tuple[Path, ...]) -> bool:
    resolved = candidate.resolve()
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def _existing_file(candidate: Path, roots: tuple[Path, ...]) -> Path | None:
    resolved = candidate.resolve()
    if _inside(resolved, roots) and resolved.is_file():
        return resolved
    return None


def _notice_folders(
    notice: dict[str, Any], artifact_roots: tuple[Path, ...]
) -> tuple[Path, ...]:
    folders: list[Path] = []
    recorded = str(notice.get("download_folder_path") or "").strip()
    if recorded:
        path = Path(recorded)
        if path.is_absolute() and _inside(path, artifact_roots) and path.is_dir():
            folders.append(path.resolve())
    folder_name = str(notice.get("download_folder_name") or "").strip()
    if folder_name and Path(folder_name).name == folder_name:
        for root in artifact_roots:
            candidate = root / folder_name
            if _inside(candidate, artifact_roots):
                folders.append(candidate.resolve())
    return tuple(dict.fromkeys(folders))


def _resolve_artifact(
    recorded_paths: list[Any],
    notice_folders: tuple[Path, ...],
    artifact_roots: tuple[Path, ...],
) -> Path | None:
    names: list[str] = []
    for value in recorded_paths:
        text = str(value or "").strip()
        if not text:
            continue
        path = Path(text)
        if path.is_absolute():
            existing = _existing_file(path, artifact_roots)
            if existing is not None:
                return existing
        name = path.name
        if name and name not in names:
            names.append(name)
    for folder in notice_folders:
        for name in names:
            existing = _existing_file(folder / name, artifact_roots)
            if existing is not None:
                return existing
    return None


def _read_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalSourceError(f"cannot read historical record: {path.name}") from error
    if not isinstance(data, dict):
        raise HistoricalSourceError(f"historical record is not an object: {path.name}")
    return data


def _safe_id(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "")
        if character.isalnum() or character in "_-"
    ) or "item"


def _artifact_path(record: Any) -> Any:
    return record.get("path") if isinstance(record, dict) else None


def _adapt_notice(
    notice: dict[str, Any],
    repository_root: Path,
    artifact_roots: tuple[Path, ...],
) -> dict[str, Any]:
    notice_id = str(notice.get("notice_id") or "").strip()
    if not notice_id:
        raise HistoricalSourceError("historical notice_id is missing")
    folders = _notice_folders(notice, artifact_roots)
    notice_document = notice.get("notice_document") or {}
    notice_path = _resolve_artifact(
        [_artifact_path(notice_document)], folders, artifact_roots
    )
    reasons = [] if notice_path is not None else ["notice_file_missing"]
    attachment_ids = [str(value) for value in notice.get("attachment_ids") or []]
    attachments = []
    attachment_items = repository_root / "attachments" / "items"
    for attachment_id in attachment_ids:
        path = attachment_items / f"{_safe_id(attachment_id)}.json"
        if not path.is_file():
            reasons.append(f"attachment_record_missing:{attachment_id}")
            continue
        attachment = _read_object(path)
        if str(attachment.get("notice_id") or "") != notice_id:
            reasons.append(f"attachment_owner_mismatch:{attachment_id}")
            attachments.append({
                "attachment_id": attachment_id,
                "attachment_ordinal": attachment.get("attachment_ordinal"),
                "source_path": None,
            })
            continue
        source_path = _resolve_artifact([
            _artifact_path(attachment.get("rendered_artifact")),
            _artifact_path(attachment.get("source_artifact")),
        ], folders, artifact_roots)
        if source_path is None:
            reasons.append(f"attachment_file_missing:{attachment_id}")
        attachments.append({
            "attachment_id": attachment_id,
            "attachment_ordinal": attachment.get("attachment_ordinal"),
            "source_path": source_path,
        })
    return {
        "notice_id": notice_id,
        "notice_no": notice.get("notice_no"),
        "case_no": notice.get("case_no"),
        "archive_no": notice.get("archive_no"),
        "web_datetime": notice.get("web_datetime"),
        "issuer_unit_name": notice.get("issuer_unit_name"),
        "subject_type": notice.get("subject_type"),
        "notice_text": notice.get("notice_text"),
        "other_data_description": notice.get("other_data_description"),
        "parties": list(notice.get("parties") or []),
        "attachment_ids": attachment_ids,
        "notice_source_path": notice_path,
        "attachments": attachments,
        "complete": not reasons,
        "incomplete_reasons": reasons,
    }


def iter_historical_records(source_root: str | Path) -> Iterator[dict[str, Any]]:
    repository_root = _repository_root(source_root)
    artifact_roots = _artifact_roots(source_root, repository_root)
    notices_dir = repository_root / "notices" / "items"
    for path in notices_dir.glob("*.json"):
        yield _adapt_notice(_read_object(path), repository_root, artifact_roots)
