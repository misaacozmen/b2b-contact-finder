"""Small atomic JSON cache used by search, Places and crawler replay."""

from __future__ import annotations

import hashlib
import gzip
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from modules import runtime


def _path(directory: Path, namespace: str, key: str, compressed: bool = False) -> Path:
    digest = hashlib.sha256(f"{namespace}\0{key}".encode("utf-8")).hexdigest()
    suffix = ".json.gz" if compressed else ".json"
    return directory / namespace / f"{digest}{suffix}"


def load(directory: Path, namespace: str, key: str, ttl_days: int, schema_version: int) -> Any | None:
    compressed_path = _path(directory, namespace, key, compressed=True)
    legacy_path = _path(directory, namespace, key)
    try:
        if compressed_path.exists():
            with gzip.open(compressed_path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != schema_version:
            return None
        created = datetime.fromisoformat(payload["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - created
        if ttl_days >= 0 and age.total_seconds() > ttl_days * 86400:
            return None
        runtime.record(f"cache.{namespace}.hit")
        return payload.get("value")
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        runtime.record(f"cache.{namespace}.miss")
        return None


def save(directory: Path, namespace: str, key: str, value: Any, schema_version: int) -> None:
    path = _path(directory, namespace, key, compressed=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": schema_version,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "value": value,
    }
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    runtime.record(f"cache.{namespace}.write")
