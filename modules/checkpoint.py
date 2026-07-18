"""Crash-safe per-company SQLite checkpoints with legacy JSON compatibility."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from functools import lru_cache
from contextlib import closing

import config


def _json_safe(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@lru_cache(maxsize=32)
def _file_hash_cached(path_text: str, size: int, modified_ns: int) -> str:
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_hash(path: Path) -> str:
    stat = path.stat()
    return _file_hash_cached(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _run_id(input_hash: str, run_signature: str) -> str:
    return hashlib.sha256(f"{input_hash}\0{run_signature}".encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    config.PROGRESS_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.PROGRESS_DB_FILE, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, run_signature TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS results (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id, item_index))"
    )
    return connection


def has_progress() -> bool:
    if config.PROGRESS_FILE.exists():
        return True
    if not config.PROGRESS_DB_FILE.exists():
        return False
    try:
        with closing(_connect()) as connection:
            return connection.execute("SELECT 1 FROM results LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def load_progress(input_path: Path, run_signature: str = "") -> dict[str, Any] | None:
    input_hash = file_hash(input_path)
    run_id = _run_id(input_hash, run_signature)
    if config.PROGRESS_DB_FILE.exists():
        try:
            with closing(_connect()) as connection:
                rows = connection.execute(
                    "SELECT item_index, payload FROM results WHERE run_id=? ORDER BY item_index", (run_id,)
                ).fetchall()
            if rows:
                results = [json.loads(payload) for _, payload in rows]
                indexes = {index for index, _ in rows}
                contiguous = -1
                while contiguous + 1 in indexes:
                    contiguous += 1
                return {
                    "input_file_hash": input_hash,
                    "last_completed_index": contiguous,
                    "results_so_far": results,
                    "run_signature": run_signature,
                }
        except (sqlite3.Error, json.JSONDecodeError):
            pass
    if not config.PROGRESS_FILE.exists():
        return None
    try:
        data = json.loads(config.PROGRESS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("input_file_hash") != input_hash or data.get("run_signature", "") != run_signature:
        return None
    return data if data.get("results_so_far") else None


def save_result(input_path: Path, item_index: int, row: dict[str, Any], run_signature: str = "") -> None:
    input_hash = file_hash(input_path)
    run_id = _run_id(input_hash, run_signature)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(_json_safe(row), ensure_ascii=False, separators=(",", ":"))
    with closing(_connect()) as connection:
        connection.execute(
            "INSERT INTO runs(run_id,input_hash,run_signature,updated_at) VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at",
            (run_id, input_hash, run_signature, timestamp),
        )
        connection.execute(
            "INSERT INTO results(run_id,item_index,payload) VALUES(?,?,?) ON CONFLICT(run_id,item_index) DO UPDATE SET payload=excluded.payload",
            (run_id, item_index, payload),
        )
        connection.commit()
    marker = {"input_file_hash": input_hash, "run_signature": run_signature, "timestamp": timestamp, "storage": "sqlite"}
    tmp = config.PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    tmp.replace(config.PROGRESS_FILE)


def save_progress(input_path: Path, last_completed_index: int, results_so_far: list[dict[str, Any]], run_signature: str = "") -> None:
    """Compatibility API; stores each supplied row in SQLite once."""
    for offset, row in enumerate(results_so_far):
        save_result(input_path, int(row.get("__index", offset)), row, run_signature)


def clear_progress() -> None:
    if config.PROGRESS_FILE.exists():
        config.PROGRESS_FILE.unlink()
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{config.PROGRESS_DB_FILE}{suffix}")
        if path.exists():
            path.unlink()
