"""Portable, integrity-checked cache entries used by one pipeline run."""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules import runtime


FORMAT_VERSION = 1
_LOCK = threading.Lock()
_ENTRIES: dict[tuple[str, str, str, int], Any] = {}
_LOADED_FROM = ""


def reset() -> None:
    global _ENTRIES, _LOADED_FROM
    with _LOCK:
        _ENTRIES = {}
        _LOADED_FROM = ""


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe(item)
            for key, item in value.items()
            if str(key).casefold() not in {
                "api_key", "apikey", "access_token", "authorization", "password", "_secret",
            }
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(_safe(item) for item in value)
    return value


def record(store: str, namespace: str, key: str, schema_version: int, value: Any) -> None:
    marker = (str(store), str(namespace), str(key), int(schema_version))
    with _LOCK:
        _ENTRIES[marker] = _safe(value)


def lookup(store: str, namespace: str, key: str, schema_version: int) -> tuple[bool, Any]:
    marker = (str(store), str(namespace), str(key), int(schema_version))
    with _LOCK:
        if marker not in _ENTRIES:
            return False, None
        value = _ENTRIES[marker]
    runtime.record(f"snapshot.{namespace}.hit")
    return True, value


def lookup_prefix(
    store: str,
    namespace: str,
    key_prefix: str,
    schema_version: int,
) -> tuple[bool, Any]:
    """Return the richest cached variant for an exact resource prefix.

    This exists for replay compatibility when bounded crawl settings (for
    example contact seed lists) changed after a snapshot was recorded.
    """
    with _LOCK:
        matches = [
            value
            for marker, value in _ENTRIES.items()
            if marker[0] == str(store)
            and marker[1] == str(namespace)
            and marker[2].startswith(str(key_prefix))
            and marker[3] == int(schema_version)
        ]
    if not matches:
        return False, None
    value = max(
        matches,
        key=lambda item: len(item.get("pages", []))
        if isinstance(item, dict) else 0,
    )
    runtime.record(f"snapshot.{namespace}.prefix_hit")
    return True, value


def _entry_rows() -> list[dict]:
    with _LOCK:
        items = list(_ENTRIES.items())
    return [
        {
            "store": marker[0],
            "namespace": marker[1],
            "key": marker[2],
            "schema_version": marker[3],
            "value": value,
        }
        for marker, value in sorted(items, key=lambda item: item[0])
    ]


def _canonical(entries: list[dict]) -> bytes:
    return json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def write(path: Path) -> None:
    entries = _entry_rows()
    canonical = _canonical(entries)
    payload = {
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entry_count": len(entries),
        "entries_sha256": hashlib.sha256(canonical).hexdigest(),
        "entries": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)
    runtime.record("snapshot.write")
    runtime.record("snapshot.entries_written", len(entries))


def load(path: Path, *, max_uncompressed_bytes: int) -> dict:
    global _LOADED_FROM
    with gzip.open(path, "rb") as handle:
        raw = handle.read(max(1, int(max_uncompressed_bytes)) + 1)
    if len(raw) > int(max_uncompressed_bytes):
        raise ValueError("replay snapshot exceeds uncompressed size limit")
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported replay snapshot format")
    entries = payload.get("entries", [])
    if not isinstance(entries, list) or payload.get("entry_count") != len(entries):
        raise ValueError("invalid replay snapshot entry manifest")
    if hashlib.sha256(_canonical(entries)).hexdigest() != payload.get("entries_sha256"):
        raise ValueError("replay snapshot integrity check failed")
    loaded: dict[tuple[str, str, str, int], Any] = {}
    for entry in entries:
        marker = (
            str(entry["store"]),
            str(entry["namespace"]),
            str(entry["key"]),
            int(entry["schema_version"]),
        )
        loaded[marker] = entry.get("value")
    with _LOCK:
        _ENTRIES.update(loaded)
        _LOADED_FROM = str(path)
    runtime.record("snapshot.load")
    runtime.record("snapshot.entries_loaded", len(loaded))
    return {
        "path": str(path),
        "entry_count": len(loaded),
        "created_at": payload.get("created_at", ""),
    }


def metadata() -> dict:
    with _LOCK:
        return {"entry_count": len(_ENTRIES), "loaded_from": _LOADED_FROM}
