"""Small atomic JSON cache used by search, Places and crawler replay."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from modules import redaction, replay_snapshot, runtime


_IO_LOCKS = tuple(threading.RLock() for _ in range(64))
_REPLACE_RETRIES = 5


def _io_lock(path: Path) -> threading.RLock:
    return _IO_LOCKS[hash(path) % len(_IO_LOCKS)]


def _replace_with_retry(source: Path, target: Path, namespace: str) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt >= _REPLACE_RETRIES - 1:
                raise
            runtime.record(f"cache.{namespace}.write_retry")
            time.sleep(0.02 * (attempt + 1))


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
    empty_ttl_days: float | None = None,
) -> Any | None:
    if allow_stale is None:
        allow_stale = (
            config.SEARCH_CACHE_MODE == "replay"
            if directory == config.SEARCH_CACHE_DIR
            else config.CRAWL_CACHE_MODE == "replay"
        )
    store = _store_name(directory)
    compressed_path = _path(directory, namespace, key, compressed=True)
    legacy_path = _path(directory, namespace, key, compressed=False)

    try:
        with _io_lock(compressed_path):
            disk_entry_found = compressed_path.exists() or legacy_path.exists()
            canonical_payload: dict[str, Any] | None = None
            is_valid_structure = False

            if disk_entry_found:
                raw_payload = None
                try:
                    if compressed_path.exists():
                        with gzip.open(compressed_path, "rt", encoding="utf-8") as handle:
                            raw_payload = json.load(handle)
                    elif legacy_path.exists():
                        raw_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
                except Exception:
                    raw_payload = None

                if (
                    isinstance(raw_payload, dict)
                    and type(raw_payload.get("schema_version")) is int
                    and isinstance(raw_payload.get("created_at"), str)
                ):
                    try:
                        created_dt = datetime.fromisoformat(raw_payload["created_at"])
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        is_valid_structure = True
                        sanitized_val = redaction.sanitize(raw_payload.get("value"))
                        canonical_payload = {
                            "schema_version": raw_payload["schema_version"],
                            "created_at": raw_payload["created_at"],
                            "value": sanitized_val,
                        }
                    except (ValueError, TypeError):
                        is_valid_structure = False

                if not is_valid_structure:
                    # Invalid/corrupt/non-dict payload: remove disposable cache files
                    compressed_path.unlink(missing_ok=True)
                    legacy_path.unlink(missing_ok=True)
                    if compressed_path.exists() or legacy_path.exists():
                        return None
                else:
                    assert canonical_payload is not None
                    needs_rewrite = (raw_payload != canonical_payload) or (
                        not compressed_path.exists() and legacy_path.exists()
                    )
                    if needs_rewrite:
                        tmp = compressed_path.with_name(
                            f"{compressed_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
                        )
                        try:
                            compressed_path.parent.mkdir(parents=True, exist_ok=True)
                            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as handle:
                                json.dump(canonical_payload, handle, ensure_ascii=False, separators=(",", ":"))
                            _replace_with_retry(tmp, compressed_path, namespace)
                            legacy_path.unlink(missing_ok=True)
                            runtime.record(f"cache.{namespace}.redacted_migration")
                        except Exception:
                            tmp.unlink(missing_ok=True)
                            runtime.record(f"cache.{namespace}.migration_error")
                            compressed_path.unlink(missing_ok=True)
                            legacy_path.unlink(missing_ok=True)
                            if compressed_path.exists() or legacy_path.exists():
                                return None
                            canonical_payload = None
                    elif compressed_path.exists() and legacy_path.exists():
                        legacy_path.unlink(missing_ok=True)

            # 2. Snapshot lookup after disk hygiene
            snapshot_hit, snapshot_value = replay_snapshot.lookup(
                store, namespace, key, schema_version,
            )
            if snapshot_hit:
                return snapshot_value

            # 3. Disk schema and TTL evaluation
            if canonical_payload is None:
                return None

            if canonical_payload["schema_version"] != schema_version:
                return None

            try:
                created = datetime.fromisoformat(canonical_payload["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - created
                value_ttl_days = ttl_days
                if (
                    empty_ttl_days is not None
                    and isinstance(canonical_payload.get("value"), dict)
                    and str(canonical_payload["value"].get("__search_result_state", "")).upper() == "EMPTY"
                ):
                    value_ttl_days = empty_ttl_days
                if value_ttl_days >= 0 and age.total_seconds() > value_ttl_days * 86400:
                    if not allow_stale:
                        runtime.record(f"cache.{namespace}.expired")
                        return None
                    runtime.record(f"cache.{namespace}.stale_hit")
                else:
                    runtime.record(f"cache.{namespace}.hit")
            except (ValueError, TypeError):
                return None

            val = canonical_payload["value"]
            replay_snapshot.record(store, namespace, key, schema_version, val)
            return val

    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        runtime.record(f"cache.{namespace}.miss")
        return None


def save(directory: Path, namespace: str, key: str, value: Any, schema_version: int) -> None:
    compressed_path = _path(directory, namespace, key, compressed=True)
    legacy_path = _path(directory, namespace, key, compressed=False)
    compressed_path.parent.mkdir(parents=True, exist_ok=True)
    sanitized_value = redaction.normalize_unicode_scalars(redaction.sanitize(value))
    payload = {
        "schema_version": schema_version,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "value": sanitized_value,
    }
    tmp = compressed_path.with_name(
        f"{compressed_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with _io_lock(compressed_path):
            try:
                with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                _replace_with_retry(tmp, compressed_path, namespace)
                legacy_path.unlink(missing_ok=True)
            finally:
                tmp.unlink(missing_ok=True)
    except Exception as exc:
        runtime.record(f"cache.{namespace}.write_error")
        runtime.record(f"cache.{namespace}.write_error.{exc.__class__.__name__}")
        return None
    replay_snapshot.record(_store_name(directory), namespace, key, schema_version, sanitized_value)
    runtime.record(f"cache.{namespace}.write")
