from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .historical import HistoricalSourceError, iter_historical_records
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


def scan_historical(
    source_root: str | Path,
    store: CatalogStore,
    *,
    on_progress: Callable[[dict], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict:
    result = {
        "scanned": 0,
        "indexed": 0,
        "unchanged": 0,
        "incomplete": 0,
        "errors": [],
        "stopped": False,
    }
    try:
        records = iter_historical_records(source_root)
        for record in records:
            if stop_requested and stop_requested():
                result["stopped"] = True
                break
            result["scanned"] += 1
            if not record["complete"]:
                result["incomplete"] += 1
            if store.upsert_historical(record):
                result["indexed"] += 1
            else:
                result["unchanged"] += 1
            if on_progress:
                on_progress(dict(result))
    except (HistoricalSourceError, OSError) as error:
        result["errors"].append(str(error))
    return result
