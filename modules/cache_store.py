"""Small atomic JSON cache used by search, Places and crawler replay."""

from __future__ import annotations

import hashlib
import gzip
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


import config
from modules import replay_snapshot, runtime


def _path(directory: Path, namespace: str, key: str, compressed: bool = False) -> Path:
    digest = hashlib.sha256(f"{namespace}\0{key}".encode("utf-8")).hexdigest()
    suffix = ".json.gz" if compressed else ".json"
    return directory / namespace / f"{digest}{suffix}"


def _store_name(directory: Path) -> str:
    return directory.name or "cache"


def load(
    directory: Path,
    namespace: str,
    key: str,
    ttl_days: int,
    schema_version: int,
    *,
    allow_stale: bool | None = None,
) -> Any | None:
    if allow_stale is None:
        allow_stale = (
            config.SEARCH_CACHE_MODE == "replay"
            if directory == config.SEARCH_CACHE_DIR
            else config.CRAWL_CACHE_MODE == "replay"
        )
    store = _store_name(directory)
    snapshot_hit, snapshot_value = replay_snapshot.lookup(
        store, namespace, key, schema_version,
    )
    if snapshot_hit:
        return snapshot_value
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
            if not allow_stale:
                runtime.record(f"cache.{namespace}.expired")
                return None
            runtime.record(f"cache.{namespace}.stale_hit")
        runtime.record(f"cache.{namespace}.hit")
        value = payload.get("value")
        replay_snapshot.record(store, namespace, key, schema_version, value)
        return value
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
    replay_snapshot.record(_store_name(directory), namespace, key, schema_version, value)
    runtime.record(f"cache.{namespace}.write")
