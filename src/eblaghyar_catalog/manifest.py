from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 2
MANIFEST_SUFFIX = ".cmf"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 1024


class ManifestValidationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path(zip_path: str | Path) -> Path:
    return Path(zip_path).with_suffix(MANIFEST_SUFFIX)


def _read_manifest(sidecar: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestValidationError(f"cannot read CMF: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManifestValidationError("unsupported CMF schema")
    return manifest


def _notice_id(manifest: dict[str, Any]) -> str:
    notice_id = str(manifest.get("notice_id") or "").strip()
    if not notice_id or len(notice_id) > 200:
        raise ManifestValidationError("notice_id is missing or invalid")
    return notice_id


def _validate_package(archive_path: Path, manifest: dict[str, Any]) -> None:
    package = manifest.get("package") or {}
    if str(package.get("filename") or "") != archive_path.name:
        raise ManifestValidationError("package filename does not match ZIP")
    if int(package.get("size", -1)) != archive_path.stat().st_size:
        raise ManifestValidationError("package size does not match ZIP")
    if str(package.get("sha256") or "").lower() != sha256_file(archive_path):
        raise ManifestValidationError("package hash does not match ZIP")


def _validate_archive(
    archive_path: Path,
    manifest: dict[str, Any],
    notice_id: str,
    attachment_ids: list[str],
) -> None:
    declared = manifest.get("files")
    if not isinstance(declared, list) or not declared:
        raise ManifestValidationError("file list is missing")
    seen_attachments = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ManifestValidationError("ZIP contains too many entries")
            if sum(item.file_size for item in infos) > MAX_UNCOMPRESSED_BYTES:
                raise ManifestValidationError("ZIP expands beyond the size limit")
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise ManifestValidationError("ZIP contains duplicate names")
            for name in names:
                candidate = PurePosixPath(name)
                if candidate.is_absolute() or ".." in candidate.parts or "\\" in name:
                    raise ManifestValidationError("ZIP contains an unsafe path")
            if len(declared) != len(names):
                raise ManifestValidationError("CMF file list differs from ZIP")
            for index, (entry, name) in enumerate(zip(declared, names)):
                if not isinstance(entry, dict) or entry.get("filename") != name:
                    raise ManifestValidationError("CMF filename/order differs from ZIP")
                if str(entry.get("notice_id") or "") != notice_id:
                    raise ManifestValidationError("file owner differs from notice")
                if index == 0 and entry.get("kind") != "notice":
                    raise ManifestValidationError("first file is not the notice")
                if index > 0:
                    attachment_id = str(entry.get("attachment_id") or "")
                    if attachment_id not in attachment_ids:
                        raise ManifestValidationError("attachment membership is invalid")
                    seen_attachments.append(attachment_id)
                digest = hashlib.sha256()
                size = 0
                with archive.open(name) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                if size != int(entry.get("size", -1)):
                    raise ManifestValidationError(f"file size mismatch: {name}")
                if digest.hexdigest() != str(entry.get("sha256") or "").lower():
                    raise ManifestValidationError(f"file hash mismatch: {name}")
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ManifestValidationError(f"cannot validate ZIP: {error}") from error
    if seen_attachments != attachment_ids:
        raise ManifestValidationError("attachment order differs from notice")


def _validate_pargar(archive_path: Path, manifest: dict[str, Any]) -> None:
    pargar = ((manifest.get("consumers") or {}).get("pargar") or {})
    if str(pargar.get("attachment") or "") != archive_path.name:
        raise ManifestValidationError("Pargar attachment does not match ZIP")
    if not str(pargar.get("subject") or "").strip():
        raise ManifestValidationError("Pargar subject is missing")


def read_pair(zip_path: str | Path) -> dict[str, Any]:
    archive_path = Path(zip_path)
    sidecar = manifest_path(archive_path)
    if not archive_path.is_file() or not sidecar.is_file():
        raise ManifestValidationError("ZIP/CMF pair is incomplete")
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ManifestValidationError("ZIP exceeds the configured size limit")
    manifest = _read_manifest(sidecar)
    notice_id = _notice_id(manifest)
    _validate_package(archive_path, manifest)
    attachment_ids = [str(value) for value in manifest.get("attachment_ids") or []]
    if len(attachment_ids) != len(set(attachment_ids)):
        raise ManifestValidationError("duplicate attachment IDs")
    _validate_archive(archive_path, manifest, notice_id, attachment_ids)
    _validate_pargar(archive_path, manifest)
    return manifest
