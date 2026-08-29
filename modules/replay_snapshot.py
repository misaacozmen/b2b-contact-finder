"""Portable, integrity-checked cache entries used by one pipeline run."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import threading
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules import redaction, runtime


FORMAT_VERSION = 3
_LOCK = threading.Lock()
_ENTRIES: dict[tuple[str, str, str, int], dict[str, Any]] = {}
_LOADED_FROM = ""
_STORE_DB: Path | None = None
_STORE_RUN_ID = ""
_STORE_READ_ONLY = False


def reset() -> None:
    global _ENTRIES, _LOADED_FROM, _STORE_DB, _STORE_RUN_ID, _STORE_READ_ONLY
    with _LOCK:
        _ENTRIES = {}
        _LOADED_FROM = ""
        _STORE_DB = None
        _STORE_RUN_ID = ""
        _STORE_READ_ONLY = False


def configure_run_store(db_path: Path, run_id: str, *, read_only: bool = False) -> None:
    global _STORE_DB, _STORE_RUN_ID, _STORE_READ_ONLY
    _STORE_DB, _STORE_RUN_ID = Path(db_path), str(run_id)
    _STORE_READ_ONLY = bool(read_only)
    if _STORE_READ_ONLY:
        if not _STORE_DB.exists():
            raise FileNotFoundError(_STORE_DB)
        return
    _STORE_DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(_STORE_DB)) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS replay_entries (run_id TEXT NOT NULL, store TEXT NOT NULL, namespace TEXT NOT NULL, key_sha256 TEXT NOT NULL, schema_version INTEGER NOT NULL, prefix_json TEXT NOT NULL, value_json TEXT NOT NULL, PRIMARY KEY(run_id,store,namespace,key_sha256,schema_version))")
        connection.commit()


def _safe(value: Any) -> Any:
    return copy.deepcopy(redaction.sanitize(value))


def _externalize_crawl_bodies(value: Any, *, shard_root: Path) -> Any:
    """Keep raw crawl bodies out of SQLite; retain a content-addressed shard."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).casefold() in {"body", "html", "raw_body"} and isinstance(item, str) and item:
                raw = item.encode("utf-8")
                digest = hashlib.sha256(raw).hexdigest()
                shard_root.mkdir(parents=True, exist_ok=True)
                target = shard_root / f"{digest}.json.gz"
                if not target.exists():
                    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                    with gzip.open(temporary, "wb", compresslevel=6) as handle:
                        handle.write(raw)
                    temporary.replace(target)
                result[key] = {"__replay_shard__": digest, "bytes": len(raw), "compression": "gzip"}
            else:
                result[key] = _externalize_crawl_bodies(item, shard_root=shard_root)
        return result
    if isinstance(value, list):
        return [_externalize_crawl_bodies(item, shard_root=shard_root) for item in value]
    return value


def _hydrate_crawl_bodies(value: Any) -> Any:
    if isinstance(value, dict):
        marker = value.get("__replay_shard__")
        if marker and _STORE_DB is not None:
            path = _STORE_DB.parent / "replay_shards" / f"{marker}.json.gz"
            if not path.is_file():
                raise RuntimeError(f"replay body shard is missing: {marker}")
            with gzip.open(path, "rb") as handle:
                raw = handle.read()
            if hashlib.sha256(raw).hexdigest() != str(marker):
                raise RuntimeError(f"replay body shard hash mismatch: {marker}")
            return raw.decode("utf-8")
        return {key: _hydrate_crawl_bodies(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_hydrate_crawl_bodies(item) for item in value]
    return value


def _key_digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _store_connection() -> sqlite3.Connection:
    if _STORE_DB is None:
        raise RuntimeError("replay store is not configured")
    if _STORE_READ_ONLY:
        return sqlite3.connect(f"file:{_STORE_DB.resolve()}?mode=ro", uri=True)
    return sqlite3.connect(_STORE_DB)


def _prefix_digests(value: str) -> list[str]:
    """Hash structured cache-key prefixes without persisting their contents."""
    text = str(value)
    return sorted({
        _key_digest(text[: index + 1])
        for index, character in enumerate(text)
        if character == "|"
    })


def record(store: str, namespace: str, key: str, schema_version: int, value: Any) -> None:
    if _STORE_READ_ONLY:
        raise RuntimeError("readonly replay store rejects writes")
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
    if _STORE_DB is not None and _STORE_RUN_ID:
        with closing(sqlite3.connect(_STORE_DB)) as connection:
            sqlite_value = _externalize_crawl_bodies(_safe(value), shard_root=_STORE_DB.parent / "replay_shards") if "crawl" in marker[0].casefold() or "crawl" in marker[1].casefold() else _safe(value)
            connection.execute("INSERT OR REPLACE INTO replay_entries VALUES (?,?,?,?,?,?,?)", (_STORE_RUN_ID, marker[0], marker[1], marker[2], marker[3], json.dumps(_prefix_digests(str(key))), json.dumps(sqlite_value, ensure_ascii=False, separators=(",", ":"))))
            connection.commit()


def lookup(store: str, namespace: str, key: str, schema_version: int) -> tuple[bool, Any]:
    marker = (
        str(store), str(namespace), _key_digest(str(key)),
        int(schema_version),
    )
    with _LOCK:
        if marker not in _ENTRIES:
            if _STORE_DB is None or not _STORE_DB.exists():
                return False, None
            try:
                with closing(sqlite3.connect(_STORE_DB)) as connection:
                    stored = connection.execute("SELECT prefix_json,value_json FROM replay_entries WHERE run_id=? AND store=? AND namespace=? AND key_sha256=? AND schema_version=?", (_STORE_RUN_ID, marker[0], marker[1], marker[2], marker[3])).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                stored = None
            if not stored:
                return False, None
            _ENTRIES[marker] = {"key_hint": "[REDACTED]", "prefix_sha256": json.loads(stored[0]), "value": _hydrate_crawl_bodies(json.loads(stored[1]))}
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
    if not items and _STORE_DB is not None and _STORE_DB.exists():
        try:
            with closing(_store_connection()) as connection:
                items = [((row[0], row[1], row[2], row[3]), {"prefix_sha256": json.loads(row[4]), "value": json.loads(row[5])}) for row in connection.execute("SELECT store,namespace,key_sha256,schema_version,prefix_json,value_json FROM replay_entries WHERE run_id=? ORDER BY store,namespace,key_sha256,schema_version", (_STORE_RUN_ID,))]
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
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


def _iter_entry_rows():
    with _LOCK:
        in_memory = list(_ENTRIES.items())
    if in_memory:
        for marker, entry in sorted(in_memory, key=lambda item: item[0]):
            yield {"store": marker[0], "namespace": marker[1], "key_sha256": marker[2], "key_hint": "[REDACTED]", "prefix_sha256": list(entry.get("prefix_sha256", [])), "schema_version": marker[3], "value": _safe(entry["value"])}
        return
    if _STORE_DB is None or not _STORE_DB.exists():
        return
    try:
        with closing(_store_connection()) as connection:
            cursor = connection.execute("SELECT store,namespace,key_sha256,schema_version,prefix_json,value_json FROM replay_entries WHERE run_id=? ORDER BY store,namespace,key_sha256,schema_version", (_STORE_RUN_ID,))
            for store, namespace, key_sha256, schema_version, prefix_json, value_json in cursor:
                yield {"store": store, "namespace": namespace, "key_sha256": key_sha256, "key_hint": "[REDACTED]", "prefix_sha256": json.loads(prefix_json), "schema_version": int(schema_version), "value": _safe(json.loads(value_json))}
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise


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


def export_shards(
    directory: Path,
    *,
    run_id: str,
    config_hash: str,
    max_uncompressed_bytes: int = 64 * 1024 * 1024,
) -> dict:
    """Export portable replay data as independently verifiable JSONL shards."""
    limit = min(64 * 1024 * 1024, max(1, int(max_uncompressed_bytes)))
    directory = Path(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.exists():
        raise FileExistsError(f"replay export already exists: {directory}")
    staging = directory.parent / f".{directory.name}.{uuid.uuid4().hex}.staging"
    staging.mkdir(parents=True, exist_ok=False)
    shard_records: list[dict] = []
    chunk: list[bytes] = []
    chunk_size = 0
    shard_index = 0

    def flush() -> None:
        nonlocal chunk, chunk_size, shard_index
        if not chunk:
            return
        name = f"part-{shard_index:05d}.jsonl.gz"
        target = staging / name
        temporary = staging / f".{name}.{os.getpid()}.tmp"
        with gzip.open(temporary, "wb", compresslevel=6) as handle:
            for line in chunk:
                handle.write(line)
        target_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(target)
        versions = sorted({int(json.loads(line).get("schema_version", 0)) for line in chunk})
        shard_records.append({
            "path": name,
            "sha256": target_hash,
            "entry_count": len(chunk),
            "uncompressed_bytes": chunk_size,
            "schema_versions": versions,
            "run_id": run_id,
            "config_hash": config_hash,
        })
        shard_index += 1
        chunk = []
        chunk_size = 0

    entry_count = 0
    for row in _iter_entry_rows():
        entry_count += 1
        line = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > limit:
            raise ValueError("single replay entry exceeds shard size limit")
        if chunk and chunk_size + len(line) > limit:
            flush()
        chunk.append(line)
        chunk_size += len(line)
    flush()
    manifest = {
        "format_version": FORMAT_VERSION,
        "run_id": run_id,
        "config_hash": config_hash,
        "entry_count": entry_count,
        "shard_count": len(shard_records),
        "max_uncompressed_bytes": limit,
        "shards": shard_records,
    }
    temporary = staging / f".manifest.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(staging / "manifest.json")
    staging.replace(directory)
    return manifest


def load_shards(
    manifest_path: Path,
    *,
    expected_run_id: str,
    expected_config_hash: str,
    max_uncompressed_bytes: int = 64 * 1024 * 1024,
) -> dict:
    """Load a replay export only when its run and config identity match."""
    global _ENTRIES
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("format_version", -1)) != FORMAT_VERSION:
        raise ValueError("unsupported replay export format")
    if manifest.get("run_id") != expected_run_id or manifest.get("config_hash") != expected_config_hash:
        raise ValueError("replay export does not belong to this run/config")
    shards = manifest.get("shards", [])
    if len(shards) != int(manifest.get("shard_count", -1)):
        raise ValueError("replay shard count mismatch")
    loaded: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    seen_paths: set[str] = set()
    total = 0
    for shard in shards:
        relative = Path(str(shard["path"]))
        if relative.name != str(shard["path"]) or relative.is_absolute():
            raise ValueError("replay shard path traversal")
        if str(shard["path"]) in seen_paths:
            raise ValueError("duplicate replay shard path")
        seen_paths.add(str(shard["path"]))
        path = manifest_path.parent / relative
        if path.resolve().parent != manifest_path.parent.resolve():
            raise ValueError("replay shard path traversal")
        if hashlib.sha256(path.read_bytes()).hexdigest() != shard.get("sha256"):
            raise ValueError("replay shard integrity check failed")
        with gzip.open(path, "rb") as handle:
            raw = handle.read(min(int(max_uncompressed_bytes), 64 * 1024 * 1024) + 1)
        if len(raw) > int(max_uncompressed_bytes) or len(raw) != int(shard.get("uncompressed_bytes", -1)):
            raise ValueError("replay shard exceeds uncompressed size limit")
        shard_count = 0
        for line in raw.splitlines():
            entry = json.loads(line)
            if int(entry.get("schema_version", -1)) not in {int(value) for value in shard.get("schema_versions", [])}:
                raise ValueError("replay schema version mismatch")
            marker = (str(entry["store"]), str(entry["namespace"]), str(entry["key_sha256"]), int(entry["schema_version"]))
            if marker in loaded:
                raise ValueError("duplicate replay entry marker")
            loaded[marker] = {"key_hint": "[REDACTED]", "prefix_sha256": list(entry.get("prefix_sha256", [])), "value": _safe(entry.get("value"))}
            total += 1
            shard_count += 1
        if shard_count != int(shard.get("entry_count", -1)):
            raise ValueError("replay shard entry count mismatch")
    if total != int(manifest.get("entry_count", -1)):
        raise ValueError("replay export entry count mismatch")
    with _LOCK:
        _ENTRIES = dict(loaded)
        global _LOADED_FROM
        _LOADED_FROM = str(manifest_path)
    return {"path": str(manifest_path), "entry_count": total}


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
