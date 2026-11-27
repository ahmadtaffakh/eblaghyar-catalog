from __future__ import annotations

from pathlib import Path

from .manifest import ManifestValidationError, read_pair
from .store import CatalogStore


def scan_exchange(exchange_dir: str | Path, store: CatalogStore) -> dict:
    root = Path(exchange_dir)
    result = {"scanned": 0, "indexed": 0, "incomplete": 0, "errors": []}
    if not root.is_dir():
        return result
    for sidecar in sorted(root.glob("*.cmf")):
        archive = sidecar.with_suffix(".zip")
        result["scanned"] += 1
        if not archive.is_file():
            result["incomplete"] += 1
            continue
        try:
            manifest = read_pair(archive)
            store.upsert(manifest, archive)
            result["indexed"] += 1
        except ManifestValidationError as error:
            result["errors"].append({"file": archive.name, "error": str(error)})
    return result
