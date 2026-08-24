"""Portable, integrity-checked cache entries used by one pipeline run."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules import redaction, runtime


FORMAT_VERSION = 3
_LOCK = threading.Lock()
_ENTRIES: dict[tuple[str, str, str, int], dict[str, Any]] = {}
_LOADED_FROM = ""


def reset() -> None:
    global _ENTRIES, _LOADED_FROM
    with _LOCK:
        _ENTRIES = {}
        _LOADED_FROM = ""


def _safe(value: Any) -> Any:
    return copy.deepcopy(redaction.sanitize(value))


def _key_digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _prefix_digests(value: str) -> list[str]:
    """Hash structured cache-key prefixes without persisting their contents."""
    text = str(value)
    return sorted({
        _key_digest(text[: index + 1])
        for index, character in enumerate(text)
        if character == "|"
    })


def record(store: str, namespace: str, key: str, schema_version: int, value: Any) -> None:
    marker = (
        str(store), str(namespace), _key_digest(str(key)),
        int(schema_version),
    )
    with _LOCK:
        _ENTRIES[marker] = {
            "key_hint": "[REDACTED]",
            "prefix_sha256": _prefix_digests(str(key)),
            "value": _safe(value),
        }


def lookup(store: str, namespace: str, key: str, schema_version: int) -> tuple[bool, Any]:
    marker = (
        str(store), str(namespace), _key_digest(str(key)),
        int(schema_version),
    )
    with _LOCK:
        if marker not in _ENTRIES:
            return False, None
        value = _ENTRIES[marker]["value"]
    runtime.record(f"snapshot.{namespace}.hit")
    return True, _safe(value)


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
    prefix_digest = _key_digest(str(key_prefix))
    with _LOCK:
        matches = [
            entry["value"]
            for marker, entry in _ENTRIES.items()
            if marker[0] == str(store)
            and marker[1] == str(namespace)
            and prefix_digest in entry.get("prefix_sha256", [])
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
    return True, _safe(value)


def _entry_rows() -> list[dict]:
    with _LOCK:
        items = list(_ENTRIES.items())
    return [
        {
            "store": marker[0],
            "namespace": marker[1],
            "key_sha256": marker[2],
            "key_hint": "[REDACTED]",
            "prefix_sha256": list(entry.get("prefix_sha256", [])),
            "schema_version": marker[3],
            "value": _safe(entry["value"]),
        }
        for marker, entry in sorted(items, key=lambda item: item[0])
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
    format_version = payload.get("format_version")
    if format_version not in {1, FORMAT_VERSION}:
        raise ValueError("unsupported replay snapshot format")
    entries = payload.get("entries", [])
    if not isinstance(entries, list) or payload.get("entry_count") != len(entries):
        raise ValueError("invalid replay snapshot entry manifest")
    if hashlib.sha256(_canonical(entries)).hexdigest() != payload.get("entries_sha256"):
        raise ValueError("replay snapshot integrity check failed")
    loaded: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for entry in entries:
        raw_key = str(entry.get("key", ""))
        digest = (
            str(entry.get("key_sha256", ""))
            if format_version == FORMAT_VERSION
            else _key_digest(raw_key)
        )
        if len(digest) != 64:
            raise ValueError("invalid replay snapshot key digest")
        prefix_digests = (
            list(entry.get("prefix_sha256", []))
            if format_version == FORMAT_VERSION
            else _prefix_digests(raw_key)
        )
        if not all(
            isinstance(item, str) and len(item) == 64
            for item in prefix_digests
        ):
            raise ValueError("invalid replay snapshot prefix digest")
        marker = (
            str(entry["store"]),
            str(entry["namespace"]),
            digest,
            int(entry["schema_version"]),
        )
        loaded[marker] = {
            "key_hint": "[REDACTED]",
            "prefix_sha256": sorted(set(prefix_digests)),
            "value": _safe(entry.get("value")),
        }
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
