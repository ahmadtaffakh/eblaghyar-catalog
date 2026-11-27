from __future__ import annotations

import ctypes
import json
import os
import socket
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path


def active_directory_username() -> str:
    if os.name != "nt":
        return os.environ.get("USER", "")
    size = wintypes.ULONG(256)
    buffer = ctypes.create_unicode_buffer(size.value)
    if ctypes.windll.secur32.GetUserNameExW(2, buffer, ctypes.byref(size)):
        return buffer.value
    return os.environ.get("USERNAME", "")


def publish_request(exchange_dir: str | Path, notice_no: str) -> Path:
    request_id = uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "notice_no": str(notice_no).strip(),
        "requested_by": active_directory_username(),
        "requested_from": socket.gethostname(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "queued",
    }
    if not payload["notice_no"]:
        raise ValueError("notice number is required")
    root = Path(exchange_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"CAT-REQ--{request_id}.req"
    temporary = target.with_suffix(".req.part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return target
