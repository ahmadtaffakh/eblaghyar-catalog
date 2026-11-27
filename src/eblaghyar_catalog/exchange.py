from __future__ import annotations

import ctypes
import json
import os
import socket
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

REQUEST_STATUSES = {"queued", "processing", "needs_acquisition", "ready", "failed"}


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


def list_user_requests(
    exchange_dir: str | Path, requested_by: str | None = None
) -> list[dict]:
    root = Path(exchange_dir)
    username = active_directory_username() if requested_by is None else requested_by
    if not root.is_dir() or not str(username).strip():
        return []
    requests = []
    for path in root.glob("CAT-REQ--*.req"):
        request_id = path.stem.removeprefix("CAT-REQ--")
        if not request_id or any(
            not (character.isalnum() or character in "_-")
            for character in request_id
        ):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or str(payload.get("request_id") or "") != request_id
            or str(payload.get("requested_by") or "") != username
        ):
            continue
        item = {
            "request_id": request_id,
            "notice_no": str(payload.get("notice_no") or ""),
            "status": "queued",
            "detail": "",
            "created_at": str(payload.get("created_at") or ""),
            "updated_at": str(payload.get("created_at") or ""),
            "package_name": "",
        }
        response_path = root / f"CAT-RSP--{request_id}.rsp"
        if response_path.is_file():
            try:
                response = json.loads(response_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                response = None
            if (
                isinstance(response, dict)
                and response.get("schema_version") == 1
                and str(response.get("request_id") or "") == request_id
                and response.get("status") in REQUEST_STATUSES
            ):
                item.update(
                    status=response["status"],
                    detail=str(response.get("detail") or ""),
                    updated_at=str(response.get("updated_at") or item["updated_at"]),
                    package_name=str(response.get("package_name") or ""),
                )
        requests.append(item)
    return sorted(
        requests,
        key=lambda item: (item["updated_at"], item["created_at"], item["request_id"]),
        reverse=True,
    )
