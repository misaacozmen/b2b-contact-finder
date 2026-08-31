"""Crash-safe per-company SQLite checkpoints with legacy JSON compatibility."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import os
import uuid
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from functools import lru_cache
from contextlib import closing

import config
from modules import redaction, runtime


CANONICAL_PROVIDERS = frozenset({"brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm"})
ITEM_STATES = frozenset({"PENDING", "RUNNING", "DONE", "FAILED", "UNKNOWN", "BLOCKED_BUDGET", "NOT_REQUIRED"})
PHASE_TRANSITIONS = {
    "FREE": {"PAID", "FINALIZING"},
    "PAID": {"FINALIZING"},
    "FINALIZING": {"COMPLETE"},
    "COMPLETE": set(),
}


def _json_safe(value: Any) -> Any:
    value = redaction.normalize_unicode_scalars(value)
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
        "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, run_signature TEXT NOT NULL, updated_at TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'FREE', context_json TEXT NOT NULL DEFAULT '{}', budgets_json TEXT NOT NULL DEFAULT '{}', attempt_number INTEGER NOT NULL DEFAULT 1)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    for name, definition in {
        "phase": "TEXT NOT NULL DEFAULT 'FREE'",
        "context_json": "TEXT NOT NULL DEFAULT '{}'",
        "budgets_json": "TEXT NOT NULL DEFAULT '{}'",
        "attempt_number": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
    if "runtime_json" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN runtime_json TEXT NOT NULL DEFAULT '{}'")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS results (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id, item_index))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS run_items (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, source_record_id TEXT NOT NULL, free_state TEXT NOT NULL, paid_required INTEGER NOT NULL DEFAULT 0, paid_state TEXT NOT NULL, free_attempts INTEGER NOT NULL DEFAULT 0, paid_attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', payload_sha256 TEXT NOT NULL DEFAULT '', PRIMARY KEY(run_id, item_index))"
    )
    item_columns = {row[1] for row in connection.execute("PRAGMA table_info(run_items)")}
    for name, definition in {
        "quarantine_state": "TEXT NOT NULL DEFAULT ''",
        "quarantine_status": "TEXT NOT NULL DEFAULT ''",
        "publication_blockers": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in item_columns:
            connection.execute(f"ALTER TABLE run_items ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_usage (run_id TEXT NOT NULL, provider TEXT NOT NULL, configured_limit INTEGER NOT NULL, effective_limit INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id, provider))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_calls (run_id TEXT NOT NULL, call_id TEXT PRIMARY KEY, provider TEXT NOT NULL, item_index INTEGER NOT NULL, phase TEXT NOT NULL, operation TEXT NOT NULL DEFAULT '', request_fingerprint TEXT NOT NULL, state TEXT NOT NULL, result_ref TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_calls)")}
    if "operation" not in columns:
        connection.execute("ALTER TABLE provider_calls ADD COLUMN operation TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_call_identity ON provider_calls(run_id,provider,item_index,phase,request_fingerprint)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS replay_entries (run_id TEXT NOT NULL, store TEXT NOT NULL, namespace TEXT NOT NULL, key_sha256 TEXT NOT NULL, schema_version INTEGER NOT NULL, prefix_json TEXT NOT NULL, value_json TEXT NOT NULL, PRIMARY KEY(run_id,store,namespace,key_sha256,schema_version))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS paid_attempts (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, attempt_number INTEGER NOT NULL, phase TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', call_id TEXT NOT NULL DEFAULT '', request_fingerprint TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY(run_id,item_index,attempt_number,phase))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS paid_attempt_calls (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, attempt_number INTEGER NOT NULL, phase TEXT NOT NULL, call_id TEXT NOT NULL, PRIMARY KEY(run_id,item_index,attempt_number,phase,call_id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS finalization_intent (run_id TEXT PRIMARY KEY, generation TEXT NOT NULL, input_snapshot_sha256 TEXT NOT NULL, started_at TEXT NOT NULL, artifact_set_sha256 TEXT NOT NULL DEFAULT '', manifest_sha256 TEXT NOT NULL DEFAULT '', completed_at TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, finalization_schema_version INTEGER NOT NULL DEFAULT 2)"
    )
    finalization_columns = {row[1] for row in connection.execute("PRAGMA table_info(finalization_intent)")}
    for name, definition in {
        "result_snapshot_sha256": "TEXT NOT NULL DEFAULT ''",
        "output_context_json": "TEXT NOT NULL DEFAULT '{}'",
        "memory_plan_sha256": "TEXT NOT NULL DEFAULT ''",
        "memory_plan_count": "INTEGER NOT NULL DEFAULT 0",
        "memory_plan_committed": "INTEGER NOT NULL DEFAULT 0",
        "telemetry_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
        "finalization_schema_version": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in finalization_columns:
            connection.execute(f"ALTER TABLE finalization_intent ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS memory_outbox (run_id TEXT NOT NULL, receipt_key TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(run_id,receipt_key))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS memory_receipts (run_id TEXT NOT NULL, receipt_key TEXT NOT NULL, completed_at TEXT NOT NULL, PRIMARY KEY(run_id,receipt_key))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS source_probes (run_id TEXT NOT NULL, host TEXT NOT NULL, state TEXT NOT NULL, owner_token TEXT NOT NULL DEFAULT '', snapshot_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(run_id,host))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS free_query_usage (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, quota INTEGER NOT NULL, PRIMARY KEY(run_id,item_index))"
    )
    probe_columns = {row[1] for row in connection.execute("PRAGMA table_info(source_probes)")}
    if "lease_expires_at" not in probe_columns:
        connection.execute("ALTER TABLE source_probes ADD COLUMN lease_expires_at TEXT NOT NULL DEFAULT ''")
    connection.commit()
    return connection


def claim_source_probe(*, run_id: str, host: str) -> dict[str, Any]:
    """Atomically assign one probe owner per run/host; waiters share its snapshot."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT state,owner_token,snapshot_json,error,lease_expires_at FROM source_probes WHERE run_id=? AND host=?", (run_id, host)).fetchone()
        if row and str(row[0]) == "DONE":
            connection.commit()
            return {"owner": False, "state": "DONE", "snapshot": json.loads(row[2] or "{}")}
        if row and str(row[0]) == "RUNNING" and str(row[4] or "") > now:
            connection.commit()
            return {"owner": False, "state": "RUNNING", "owner_token": str(row[1]), "lease_expires_at": str(row[4])}
        token = uuid.uuid4().hex
        lease_expires = (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(getattr(config, "SOURCE_PROBE_LEASE_SEC", 30))))).isoformat(timespec="seconds")
        if row:
            connection.execute("UPDATE source_probes SET state='RUNNING',owner_token=?,snapshot_json='{}',error='',updated_at=?,lease_expires_at=? WHERE run_id=? AND host=? AND (state<>'RUNNING' OR (state='RUNNING' AND lease_expires_at<=?))", (token, now, lease_expires, run_id, host, now))
        else:
            connection.execute("INSERT INTO source_probes(run_id,host,state,owner_token,updated_at,lease_expires_at) VALUES(?,?,?,?,?,?)", (run_id, host, "RUNNING", token, now, lease_expires))
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return {"owner": False, "state": "RUNNING", "owner_token": str(row[1])}
        connection.commit()
    return {"owner": True, "state": "RUNNING", "owner_token": token, "lease_expires_at": lease_expires}


def heartbeat_source_probe(*, run_id: str, host: str, owner_token: str) -> str:
    """Extend an owned probe lease while its physical operation is running."""
    now = datetime.now(timezone.utc)
    lease_expires = (now + timedelta(seconds=max(1, int(getattr(config, "SOURCE_PROBE_LEASE_SEC", 30))))).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        cursor = connection.execute(
            "UPDATE source_probes SET updated_at=?,lease_expires_at=? "
            "WHERE run_id=? AND host=? AND state='RUNNING' AND owner_token=?",
            (now.isoformat(timespec="seconds"), lease_expires, run_id, host, owner_token),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise RuntimeError("source probe owner lost during heartbeat")
        connection.commit()
    return lease_expires


def finish_source_probe(*, run_id: str, host: str, owner_token: str, snapshot: dict[str, Any] | None = None, error: str = "") -> None:
    state = "ERROR" if error else "DONE"
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("UPDATE source_probes SET state=?,snapshot_json=?,error=?,updated_at=?,lease_expires_at='' WHERE run_id=? AND host=? AND state='RUNNING' AND owner_token=?", (state, json.dumps(_json_safe(snapshot or {}), ensure_ascii=False), str(error), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id, host, owner_token))
        if cursor.rowcount != 1:
            connection.rollback()
            raise RuntimeError("source probe owner lost")
        connection.commit()


def reserve_free_search_query(*, run_id: str, item_index: int, limit: int = 10) -> bool:
    """Reserve one live free query atomically for exactly one item."""
    limit = max(1, min(10, int(limit)))
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO free_query_usage(run_id,item_index,used,quota) VALUES(?,?,0,?)",
            (run_id, int(item_index), limit),
        )
        cursor = connection.execute(
            "UPDATE free_query_usage SET used=used+1 WHERE run_id=? AND item_index=? AND used < quota",
            (run_id, int(item_index)),
        )
        connection.commit()
        return cursor.rowcount == 1


def wait_source_probe(*, run_id: str, host: str, timeout_seconds: float = 30.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() <= deadline:
        with closing(_connect()) as connection:
            row = connection.execute("SELECT state,snapshot_json,lease_expires_at FROM source_probes WHERE run_id=? AND host=?", (run_id, host)).fetchone()
        if row and str(row[0]) == "DONE":
            return json.loads(row[1] or "{}")
        if row and str(row[0]) == "ERROR":
            return None
        if row and str(row[0]) == "RUNNING" and str(row[2] or "") <= datetime.now(timezone.utc).isoformat(timespec="seconds"):
            return None
        time.sleep(0.01)
    raise TimeoutError(f"source probe waiter timed out: {host}")


def initialize_schema(path: Path | None = None) -> None:
    """Create the canonical run schema at an explicitly selected path."""
    target = Path(path) if path is not None else config.PROGRESS_DB_FILE
    original = config.PROGRESS_DB_FILE
    config.PROGRESS_DB_FILE = target
    try:
        connection = _connect()
        connection.close()
    finally:
        config.PROGRESS_DB_FILE = original


def create_handoff_snapshot(
    active_db: Path,
    destination: Path,
    *,
    run_id: str | None = None,
    expected_count: int | None = None,
) -> dict[str, Any]:
    """Create an atomic, replay-free SQLite handoff snapshot.

    The active database is opened read-only and is never vacuumed or otherwise
    modified.  The SQLite backup API copies it to a private staging file;
    replay entries are removed and the resulting file is validated before the
    final atomic replace.
    """
    active_db, destination = Path(active_db).resolve(), Path(destination).resolve()
    if not active_db.is_file():
        raise FileNotFoundError(active_db)
    if active_db == destination or (destination.exists() and active_db.samefile(destination)):
        raise ValueError("handoff snapshot must not replace its active source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{active_db.as_posix()}?mode=ro"
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.staging")

    def snapshot_rows(connection: sqlite3.Connection, selected_run_id: str) -> tuple[list[tuple], list[tuple]]:
        items = connection.execute(
            "SELECT item_index,source_record_id,payload_sha256 FROM run_items WHERE run_id=? ORDER BY item_index",
            (selected_run_id,),
        ).fetchall()
        results = connection.execute(
            "SELECT item_index,payload FROM results WHERE run_id=? ORDER BY item_index",
            (selected_run_id,),
        ).fetchall()
        if len(items) != len(results) or {int(item[0]) for item in items} != {int(row[0]) for row in results}:
            raise RuntimeError("handoff item/result count mismatch")
        item_hashes = {int(item[0]): str(item[2] or "") for item in items}
        for item_index, payload in results:
            digest = hashlib.sha256(str(payload).encode("utf-8")).hexdigest()
            if item_hashes[int(item_index)] and item_hashes[int(item_index)] != digest:
                raise RuntimeError(f"handoff payload hash mismatch: {item_index}")
        return items, [(int(index), hashlib.sha256(str(payload).encode("utf-8")).hexdigest()) for index, payload in results]

    try:
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            source.execute("PRAGMA query_only=ON")
            runs = source.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall()
            selected_run_id = str(run_id or (runs[0][0] if len(runs) == 1 else ""))
            if not selected_run_id or not any(str(row[0]) == selected_run_id for row in runs):
                raise RuntimeError("handoff run identity is ambiguous or missing")
            source_items, source_results = snapshot_rows(source, selected_run_id)
            if expected_count is not None and len(source_items) != int(expected_count):
                raise RuntimeError("handoff expected item count mismatch")
            destination_tmp = sqlite3.connect(staging)
            try:
                source.backup(destination_tmp)
                destination_tmp.commit()
            finally:
                destination_tmp.close()

        with closing(sqlite3.connect(staging)) as staged:
            staged.execute("DELETE FROM replay_entries")
            staged.commit()
            staged.execute("VACUUM")
            if staged.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("handoff staging integrity_check failed")
            staged_items, staged_results = snapshot_rows(staged, selected_run_id)
            if len(staged_items) != len(source_items) or staged_items != source_items or staged_results != source_results:
                raise RuntimeError("handoff staging payload lineage mismatch")
            replay_count = int(staged.execute("SELECT COUNT(*) FROM replay_entries").fetchone()[0])
            if replay_count != 0:
                raise RuntimeError("handoff staging still contains replay entries")
            staged.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        os.replace(staging, destination)
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{staging}{suffix}").unlink(missing_ok=True)
    return {
        "path": str(destination),
        "run_id": selected_run_id,
        "items": len(source_items),
        "results": len(source_results),
        "replay_entries": 0,
        "sha256": file_hash(destination),
        "bytes": destination.stat().st_size,
    }


def replace_handoff_checkpoint(
    snapshot: Path,
    active_db: Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
) -> None:
    """Atomically install a validated snapshot under the caller's run lease.

    The caller must have joined all workers and closed its SQLite connections.
    Existing sidecars or SQLite locks are rejected; they are never removed.
    Copy/validation/replace failures leave the original checkpoint in place.
    """
    snapshot, active_db = Path(snapshot).resolve(), Path(active_db).resolve()
    if not snapshot.is_file() or not active_db.is_file():
        raise FileNotFoundError("handoff snapshot and active checkpoint must exist")
    if snapshot == active_db or snapshot.samefile(active_db):
        raise ValueError("handoff snapshot must differ from the active checkpoint")

    def require_no_sidecars() -> None:
        for path in (snapshot, active_db):
            if any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
                raise RuntimeError("handoff replacement refuses SQLite sidecars or open connections")

    require_no_sidecars()
    before_hash = file_hash(active_db)
    staging = active_db.with_name(f".{active_db.name}.{uuid.uuid4().hex}.staging")
    try:
        with snapshot.open("rb") as source, staging.open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        if staging.stat().st_size != int(expected_bytes) or file_hash(staging) != expected_sha256:
            raise RuntimeError("handoff replacement snapshot hash or size mismatch")
        with closing(sqlite3.connect(f"{staging.as_uri()}?mode=ro&immutable=1", uri=True)) as staged:
            if staged.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("handoff replacement integrity_check failed")

        require_no_sidecars()
        try:
            with closing(sqlite3.connect(f"{active_db.as_uri()}?mode=rw", uri=True, timeout=0)) as active:
                active.execute("BEGIN EXCLUSIVE")
                active.rollback()
        except sqlite3.Error as exc:
            raise RuntimeError("handoff replacement requires an idle checkpoint") from exc
        require_no_sidecars()
        if file_hash(active_db) != before_hash:
            raise RuntimeError("active checkpoint changed during handoff replacement")
        os.replace(staging, active_db)
    finally:
        staging.unlink(missing_ok=True)


def seed_recovered_run(*, path: Path, run_id: str, input_hash: str, run_signature: str,
                       context: dict[str, Any], budgets: dict[str, int],
                       items: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    """Seed a recovered run through the same schema used by the live pipeline."""
    original = config.PROGRESS_DB_FILE
    config.PROGRESS_DB_FILE = Path(path)
    try:
        initialize_schema(Path(path))
        with closing(_connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            result_map = {int(result["item_index"]): str(result["payload"]) for result in results}
            if len(result_map) != len(results):
                connection.rollback()
                raise ValueError("duplicate seed payload index")
            normalized_items = []
            provisional_seed = bool(context.get("provisional")) or str((context.get("lineage") or {}).get("type", "")) == "legacy_recovery"
            for raw_item in items:
                item = dict(raw_item)
                index = int(item["item_index"])
                payload_text = result_map.get(index)
                if payload_text is not None:
                    try:
                        payload = json.loads(payload_text)
                    except json.JSONDecodeError as exc:
                        connection.rollback()
                        raise ValueError(f"seed payload JSON invalid: {index}") from exc
                    payload_source_id = str(payload.get("source_record_id", ""))
                    if payload_source_id != str(item["source_record_id"]):
                        connection.rollback()
                        raise ValueError(f"seed payload source ID mismatch: {index}")
                    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                    if item.get("payload_sha256") and str(item["payload_sha256"]) != payload_hash:
                        connection.rollback()
                        raise ValueError(f"seed payload hash mismatch: {index}")
                    item["payload_sha256"] = payload_hash
                    for field in ("quarantine_state", "quarantine_status", "publication_blockers"):
                        item_value = str(item.get(field, ""))
                        payload_value = str(payload.get(field, ""))
                        if item_value and item_value != payload_value:
                            connection.rollback()
                            raise ValueError(f"seed {field} mismatch: {index}")
                        if not item_value and payload_value:
                            item[field] = payload_value
                    allowed_quarantine_markers = {"legacy_recovery_provisional", "HANDOFF_PENDING"}
                    payload_markers = set(value.strip() for value in str(payload.get("publication_blockers", "")).replace(",", ";").split(";") if value.strip())
                    if (item.get("quarantine_state") or item.get("quarantine_status") or allowed_quarantine_markers & set(value.strip() for value in str(item.get("publication_blockers", "")).replace(",", ";").split(";") if value.strip())) and (payload.get("publication_eligible") is not False or not allowed_quarantine_markers & payload_markers):
                        connection.rollback()
                        raise ValueError(f"seed quarantine publication mismatch: {index}")
                if provisional_seed and not any(str(item.get(field, "")) for field in ("quarantine_state", "quarantine_status", "publication_blockers")):
                    connection.rollback()
                    raise ValueError(f"seed quarantine metadata is missing: {index}")
                normalized_items.append(item)
            now = str(context.get("seed_timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
            connection.execute(
                "INSERT INTO runs(run_id,input_hash,run_signature,updated_at,phase,context_json,budgets_json,attempt_number,runtime_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, input_hash, run_signature, now, str(context.get("phase", "FREE")), json.dumps(_json_safe(context), ensure_ascii=False), json.dumps(_json_safe(budgets), ensure_ascii=False), 1, json.dumps({"phase": context.get("phase", "FREE"), "counters": {}}, ensure_ascii=False)),
            )
            for item in normalized_items:
                connection.execute(
                    "INSERT INTO run_items(run_id,item_index,source_record_id,free_state,paid_required,paid_state,free_attempts,paid_attempts,last_error,payload_sha256,quarantine_state,quarantine_status,publication_blockers) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, int(item["item_index"]), str(item["source_record_id"]), str(item.get("free_state", "PENDING")), int(bool(item.get("paid_required"))), str(item.get("paid_state", "NOT_REQUIRED")), int(item.get("free_attempts", 0)), int(item.get("paid_attempts", 0)), str(item.get("last_error", "")), str(item.get("payload_sha256", "")), str(item.get("quarantine_state", "")), str(item.get("quarantine_status", "")), str(item.get("publication_blockers", ""))),
                )
            for result in results:
                connection.execute(
                    "INSERT INTO results(run_id,item_index,payload) VALUES(?,?,?)",
                    (run_id, int(result["item_index"]), str(result["payload"])),
                )
            for provider, limit in budgets.items():
                connection.execute(
                    "INSERT INTO provider_usage(run_id,provider,configured_limit,effective_limit) VALUES(?,?,?,?)",
                    (run_id, str(provider), int(limit), int(limit)),
                )
            connection.commit()
    finally:
        config.PROGRESS_DB_FILE = original


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
                    "runtime_snapshot": _load_marker_runtime_snapshot(),
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
    if data.get("results_so_far"):
        data["runtime_snapshot"] = _load_marker_runtime_snapshot()
        return data
    return None


def _load_marker_runtime_snapshot() -> dict | None:
    if not config.PROGRESS_FILE.exists():
        return None
    try:
        marker = json.loads(config.PROGRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return marker.get("runtime_snapshot")


def save_result(input_path: Path, item_index: int, row: dict[str, Any], run_signature: str = "") -> None:
    input_hash = file_hash(input_path)
    run_id = _run_id(input_hash, run_signature)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(_json_safe(redaction.sanitize(row)), ensure_ascii=False, separators=(",", ":"))
    with closing(_connect()) as connection:
        connection.execute(
            "INSERT INTO runs(run_id,input_hash,run_signature,updated_at) VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at",
            (run_id, input_hash, run_signature, timestamp),
        )
        connection.execute(
            "INSERT INTO results(run_id,item_index,payload) VALUES(?,?,?) ON CONFLICT(run_id,item_index) DO UPDATE SET payload=excluded.payload",
            (run_id, item_index, payload),
        )
        source_id = str(row.get("source_record_id") or row.get("_id") or f"legacy-derived:{item_index}")
        connection.execute(
            "INSERT INTO run_items(run_id,item_index,source_record_id,free_state,paid_required,paid_state,free_attempts,paid_attempts,last_error,payload_sha256) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,item_index) DO UPDATE SET source_record_id=excluded.source_record_id,payload_sha256=excluded.payload_sha256,last_error=excluded.last_error",
            (run_id, item_index, source_id, "DONE", int(bool(row.get("__paid_escalation_complete"))), "DONE", int(row.get("attempt_number", 1)), 0, str(row.get("reason", "")), hashlib.sha256(payload.encode("utf-8")).hexdigest()),
        )
        connection.execute("UPDATE runs SET runtime_json=? WHERE run_id=?", (json.dumps(_json_safe(runtime.snapshot()), ensure_ascii=False), run_id))
        connection.commit()
    marker = {"input_file_hash": input_hash, "run_signature": run_signature, "timestamp": timestamp, "storage": "sqlite", "runtime_snapshot": runtime.snapshot()}
    tmp = config.PROGRESS_FILE.with_name(
        f".{config.PROGRESS_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    tmp.replace(config.PROGRESS_FILE)


def save_run_context(
    input_path: Path, run_signature: str, *, phase: str, context: dict[str, Any],
    budgets: dict[str, int], attempt_number: int = 1,
) -> str:
    input_hash = file_hash(input_path)
    run_id = _run_id(input_hash, run_signature)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute(
            "INSERT INTO runs(run_id,input_hash,run_signature,updated_at,phase,context_json,budgets_json,attempt_number) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at,phase=excluded.phase,context_json=excluded.context_json,budgets_json=excluded.budgets_json,attempt_number=excluded.attempt_number",
            (run_id, input_hash, run_signature, timestamp, str(phase), json.dumps(_json_safe(context), ensure_ascii=False), json.dumps(_json_safe(budgets), ensure_ascii=False), int(attempt_number)),
        )
        connection.commit()
    return run_id


def initialize_run(*, run_id: str, input_hash: str, run_signature: str, context: dict[str, Any],
                   budgets: dict[str, int], items: list[dict[str, Any]]) -> None:
    """Create the durable run and item state without deriving another run ID."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute(
            "INSERT INTO runs(run_id,input_hash,run_signature,updated_at,phase,context_json,budgets_json,attempt_number,runtime_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET context_json=excluded.context_json,budgets_json=excluded.budgets_json",
            (run_id, input_hash, run_signature, timestamp, str(context.get("phase", "FREE")), json.dumps(_json_safe(context), ensure_ascii=False), json.dumps(_json_safe(budgets), ensure_ascii=False), 1, json.dumps(_json_safe(runtime.snapshot()), ensure_ascii=False)),
        )
        for item in items:
            connection.execute(
                "INSERT INTO run_items(run_id,item_index,source_record_id,free_state,paid_required,paid_state,quarantine_state,quarantine_status,publication_blockers) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,item_index) DO UPDATE SET source_record_id=excluded.source_record_id,paid_required=excluded.paid_required,quarantine_state=excluded.quarantine_state,quarantine_status=excluded.quarantine_status,publication_blockers=excluded.publication_blockers",
                (run_id, int(item["item_index"]), str(item["source_record_id"]), str(item.get("free_state", "PENDING")), int(bool(item.get("paid_required"))), str(item.get("paid_state", "PENDING" if item.get("paid_required") else "NOT_REQUIRED")), str(item.get("quarantine_state", "")), str(item.get("quarantine_status", "")), str(item.get("publication_blockers", ""))),
            )
        for provider, limit in budgets.items():
            connection.execute(
                "INSERT INTO provider_usage(run_id,provider,configured_limit,effective_limit) VALUES(?,?,?,?) ON CONFLICT(run_id,provider) DO NOTHING",
                (run_id, provider, int(limit), int(limit)),
            )
        connection.commit()


def load_run_items(run_id: str) -> list[dict[str, Any]]:
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT item_index,source_record_id,free_state,paid_required,paid_state,free_attempts,paid_attempts,last_error,payload_sha256,quarantine_state,quarantine_status,publication_blockers FROM run_items WHERE run_id=? ORDER BY item_index", (run_id,)).fetchall()
    fields = ("item_index", "source_record_id", "free_state", "paid_required", "paid_state", "free_attempts", "paid_attempts", "last_error", "payload_sha256", "quarantine_state", "quarantine_status", "publication_blockers")
    return [dict(zip(fields, row)) for row in rows]


def claim_item(*, run_id: str, item_index: int, phase: str) -> bool:
    """CAS-claim one scheduler item before any work starts."""
    if phase not in {"FREE", "PAID"}:
        raise ValueError(f"invalid item phase: {phase}")
    column = "free_state" if phase == "FREE" else "paid_state"
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            f"UPDATE run_items SET {column}='RUNNING' WHERE run_id=? AND item_index=? AND {column}='PENDING'",
            (run_id, int(item_index)),
        )
        connection.commit()
        return cursor.rowcount == 1


def recover_interrupted_items(run_id: str) -> dict[str, int]:
    """Recover only scheduler state; payloads never make an item runnable."""
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        free_reset = connection.execute("UPDATE run_items SET free_state='PENDING' WHERE run_id=? AND free_state='RUNNING'", (run_id,)).rowcount
        paid_unknown = connection.execute("UPDATE run_items SET paid_state='UNKNOWN' WHERE run_id=? AND paid_state IN ('RUNNING','RESERVED')", (run_id,)).rowcount
        connection.commit()
    return {"free_reset": free_reset, "paid_unknown": paid_unknown}


def validate_run_invariants(run_id: str, *, expected_count: int, require_payloads: bool = False) -> dict[str, int]:
    with closing(_connect()) as connection:
        item_count, source_count = connection.execute("SELECT COUNT(*),COUNT(DISTINCT source_record_id) FROM run_items WHERE run_id=?", (run_id,)).fetchone()
        result_count = connection.execute("SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)).fetchone()[0]
        if int(item_count) != expected_count or int(source_count) != expected_count:
            raise RuntimeError("run invariant failed: item/source count")
        if require_payloads and int(result_count) != expected_count:
            raise RuntimeError("run invariant failed: payload count")
        return {"items": int(item_count), "source_ids": int(source_count), "payloads": int(result_count)}


def transition_phase(run_id: str, new_phase: str, *, expected_count: int) -> None:
    if new_phase not in {"FREE", "PAID", "FINALIZING", "COMPLETE"}:
        raise ValueError(f"invalid phase: {new_phase}")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row or new_phase not in PHASE_TRANSITIONS.get(str(row[0]), set()):
            connection.rollback()
            raise RuntimeError(f"invalid phase transition to {new_phase}")
        item_count, source_count = connection.execute("SELECT COUNT(*),COUNT(DISTINCT source_record_id) FROM run_items WHERE run_id=?", (run_id,)).fetchone()
        if item_count != expected_count or source_count != expected_count:
            connection.rollback()
            raise RuntimeError("run invariant failed: item/source count")
        if new_phase in {"PAID", "FINALIZING", "COMPLETE"}:
            pending_free = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state IN ('PENDING','RUNNING')", (run_id,)).fetchone()[0]
            if pending_free:
                connection.rollback()
                raise RuntimeError("free work is not terminal")
        if new_phase in {"FINALIZING", "COMPLETE"}:
            pending_paid = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')", (run_id,)).fetchone()[0]
            if pending_paid:
                connection.rollback()
                raise RuntimeError("paid work is not terminal")
        if new_phase == "COMPLETE":
            pending = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET') OR (paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')))", (run_id,)).fetchone()[0]
            if pending:
                connection.rollback()
                raise RuntimeError("run has nonterminal scheduler items")
            intent = connection.execute(
                "SELECT status,artifact_set_sha256,manifest_sha256,memory_plan_sha256,memory_plan_count,memory_plan_committed FROM finalization_intent WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not intent or str(intent[0]) != "COMPLETE" or not str(intent[1]) or not str(intent[2]) or int(intent[5] or 0) != 1:
                connection.rollback()
                raise RuntimeError("COMPLETE requires a completed memory plan receipt")
            entries, plan_hash = _outbox_plan(connection, run_id)
            if len(entries) != int(intent[4]) or plan_hash != str(intent[3]):
                connection.rollback()
                raise RuntimeError("COMPLETE memory receipt mismatch")
        connection.execute("UPDATE runs SET phase=?,updated_at=? WHERE run_id=?", (new_phase, datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id))
        connection.commit()


def complete_finalization_phase(run_id: str, *, expected_count: int) -> None:
    """Idempotent CAS used when publishing succeeded before the DB phase update."""
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row or str(row[0]) == "COMPLETE":
            connection.commit()
            return
        if str(row[0]) != "FINALIZING":
            connection.rollback()
            raise RuntimeError("finalization CAS requires FINALIZING phase")
        pending = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET') OR (paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')))", (run_id,)).fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=?", (run_id,)).fetchone()[0]
        if int(count) != int(expected_count) or pending:
            connection.rollback()
            raise RuntimeError("finalization CAS invariant failed")
        unresolved = connection.execute("SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')", (run_id,)).fetchone()[0]
        if unresolved:
            connection.rollback()
            raise RuntimeError("finalization CAS has unresolved provider calls")
        intent = connection.execute(
            "SELECT status,artifact_set_sha256,manifest_sha256,memory_plan_sha256,memory_plan_count,memory_plan_committed FROM finalization_intent WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if not intent or str(intent[0]) != "COMPLETE" or not str(intent[1]) or not str(intent[2]) or int(intent[5] or 0) != 1:
            connection.rollback()
            raise RuntimeError("finalization CAS requires a completed memory plan receipt")
        entries, plan_hash = _outbox_plan(connection, run_id)
        if len(entries) != int(intent[4]) or plan_hash != str(intent[3]):
            connection.rollback()
            raise RuntimeError("finalization CAS memory receipt mismatch")
        connection.execute("UPDATE runs SET phase='COMPLETE',updated_at=? WHERE run_id=? AND phase='FINALIZING'", (datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id))
        connection.commit()


def load_results_by_id(run_id: str) -> dict[int, dict[str, Any]]:
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT item_index,payload FROM results WHERE run_id=? ORDER BY item_index", (run_id,)).fetchall()
    return {int(index): json.loads(payload) for index, payload in rows}


def freeze_paid_queue(run_id: str, item_indexes: list[int] | None = None) -> list[int]:
    """Freeze the paid set exactly once at the FREE -> PAID boundary."""
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT context_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        context = json.loads(row[0] or "{}") if row else {}
        indexes = [int(item[0]) for item in connection.execute("SELECT item_index FROM run_items WHERE run_id=? AND paid_required=1 AND paid_state='PENDING' ORDER BY item_index", (run_id,)).fetchall()]
        if context.get("paid_queue_frozen"):
            connection.rollback()
            return indexes
        context["paid_queue_frozen"] = True
        context["paid_queue_indexes"] = indexes
        connection.execute("UPDATE runs SET context_json=?,updated_at=? WHERE run_id=?", (json.dumps(_json_safe(context), ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id))
        connection.commit()
    return indexes


def mark_handoff_pending(*, run_id: str, expected_count: int) -> dict[str, int]:
    """Quarantine the complete free snapshot in one durable handoff transaction."""
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        phase = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not phase or str(phase[0]) != "FREE":
            connection.rollback()
            raise RuntimeError("handoff quarantine requires FREE phase")
        items = connection.execute(
            "SELECT item_index,free_state FROM run_items WHERE run_id=? ORDER BY item_index",
            (run_id,),
        ).fetchall()
        if len(items) != int(expected_count) or any(str(state) not in {"DONE", "FAILED", "NOT_REQUIRED"} for _, state in items):
            connection.rollback()
            raise RuntimeError("handoff requires all free items terminal")
        payloads = connection.execute(
            "SELECT item_index,payload FROM results WHERE run_id=? ORDER BY item_index",
            (run_id,),
        ).fetchall()
        if len(payloads) != int(expected_count):
            connection.rollback()
            raise RuntimeError("handoff requires a complete result snapshot")
        for item_index, payload_text in payloads:
            payload = json.loads(str(payload_text))
            blockers = "; ".join(sorted(set(filter(None, [str(payload.get("publication_blockers", "")), "HANDOFF_PENDING"]))))
            payload.update({
                "quarantine_state": "HANDOFF_PENDING",
                "quarantine_status": "PENDING_APPROVAL",
                "publication_eligible": False,
                "publication_blockers": blockers,
            })
            safe_payload = json.dumps(_json_safe(redaction.sanitize(payload)), ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                "UPDATE run_items SET quarantine_state='HANDOFF_PENDING',quarantine_status='PENDING_APPROVAL',publication_blockers=?,payload_sha256=? WHERE run_id=? AND item_index=?",
                (blockers, hashlib.sha256(safe_payload.encode("utf-8")).hexdigest(), run_id, int(item_index)),
            )
            connection.execute(
                "UPDATE results SET payload=? WHERE run_id=? AND item_index=?",
                (safe_payload, run_id, int(item_index)),
            )
        connection.commit()
    return {"items": int(expected_count), "quarantined": int(expected_count)}


def derive_telemetry(run_id: str) -> dict[str, int]:
    """Derive handoff/finalization counters from the durable scheduler tables."""
    with closing(_connect()) as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=?", (run_id,)).fetchone()[0])
        free_completed = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state='DONE'", (run_id,)).fetchone()[0])
        item_terminal = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state IN ('DONE','FAILED','NOT_REQUIRED')", (run_id,)).fetchone()[0])
        paid_required = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_required=1", (run_id,)).fetchone()[0])
        paid_completed = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_required=1 AND paid_state IN ('DONE','FAILED','NOT_REQUIRED')", (run_id,)).fetchone()[0])
        free_failed = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state='FAILED'", (run_id,)).fetchone()[0])
        result_count = int(connection.execute("SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)).fetchone()[0])
        manifest_count = int(connection.execute("SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)).fetchone()[0])
    return {"total_items": total, "free_completed": free_completed, "free_failed": free_failed, "item_terminal": item_terminal, "paid_required": paid_required, "paid_completed": paid_completed, "result_count": result_count, "manifest_count": manifest_count}


def release_handoff_pending(run_id: str, *, expected_count: int) -> dict[str, int]:
    """Atomically remove the temporary overlay and recompute publication policy."""
    from modules import publication_policy
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        phase = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
        pending = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_required=1 AND paid_state NOT IN ('DONE','FAILED','NOT_REQUIRED')", (run_id,)).fetchone()[0]
        unresolved = connection.execute("SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')", (run_id,)).fetchone()[0]
        quarantined = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND quarantine_state='HANDOFF_PENDING'", (run_id,)).fetchone()[0]
        rows = connection.execute("SELECT item_index,payload FROM results WHERE run_id=? ORDER BY item_index", (run_id,)).fetchall()
        if not phase or str(phase[0]) != "PAID" or int(pending) or int(unresolved) or int(quarantined) != int(expected_count) or len(rows) != int(expected_count):
            connection.rollback()
            raise RuntimeError("handoff release requires terminal paid snapshot")
        released = 0
        for item_index, payload_text in rows:
            payload = json.loads(str(payload_text))
            if str(payload.get("quarantine_state", "")) != "HANDOFF_PENDING":
                connection.rollback()
                raise RuntimeError("handoff release requires exact payload quarantine")
            prior_blockers = str(payload.get("publication_blockers", ""))
            if (
                "legacy_recovery_provisional" in prior_blockers
                or str(payload.get("source_record_id_quality", "")).casefold() == "legacy_recovery"
            ):
                connection.rollback()
                raise RuntimeError("permanent recovery quarantine cannot be released")
            payload["quarantine_state"] = ""
            payload["quarantine_status"] = ""
            blockers: list[str] = []
            evaluation = dict(payload.get("__evaluation") if isinstance(payload.get("__evaluation"), dict) else {})
            if payload.get("identity_resolution"):
                evaluation.setdefault("_identity_resolution", payload.get("identity_resolution"))
            evaluation.setdefault("candidate", payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {})
            evaluation.setdefault("identity_assessment", payload.get("identity_assessment") if isinstance(payload.get("identity_assessment"), dict) else {})
            evaluation.setdefault("structured_domain_relation", bool(payload.get("structured_domain_relation")))
            evaluation.setdefault("reasons", [value.strip() for value in str(payload.get("reason", "")).split(";") if value.strip()])
            evaluation.setdefault("has_contact", bool(payload.get("email") or payload.get("phone")))
            evaluation.setdefault("email", payload.get("email", ""))
            evaluation.setdefault("email_failed", "email_gate_failed" in str(payload.get("reason", "")))
            decision = publication_policy.evaluate(
                str(payload.get("company", "")),
                evaluation,
                str(payload.get("status", "")),
                minimum_safety_score=int(getattr(config, "PUBLICATION_POLICY_MIN_SAFETY_SCORE", 75)),
            )
            payload["publication_eligible"] = bool(decision.get("eligible"))
            blockers.extend(str(value) for value in decision.get("hard_blockers", []))
            payload["publication_blockers"] = "; ".join(sorted(set(blockers)))
            safe_payload = json.dumps(_json_safe(redaction.sanitize(payload)), ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(safe_payload.encode("utf-8")).hexdigest()
            connection.execute("UPDATE run_items SET quarantine_state='',quarantine_status='',publication_blockers=?,payload_sha256=? WHERE run_id=? AND item_index=? AND quarantine_state='HANDOFF_PENDING'", (payload["publication_blockers"], digest, run_id, int(item_index)))
            connection.execute("UPDATE results SET payload=? WHERE run_id=? AND item_index=?", (safe_payload, run_id, int(item_index)))
            released += 1
        if released != int(expected_count):
            connection.rollback()
            raise RuntimeError("handoff release count mismatch")
        connection.commit()
    return {"items": int(expected_count), "released": released}


def save_item_transaction(*, run_id: str, item_index: int, source_record_id: str, payload: dict[str, Any],
                          free_state: str, paid_state: str, paid_required: bool, free_attempts: int,
                          paid_attempts: int, last_error: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT free_state,paid_state,free_attempts,quarantine_state,quarantine_status,publication_blockers FROM run_items WHERE run_id=? AND item_index=?",
            (run_id, item_index),
        ).fetchone()
        if not current:
            connection.rollback()
            raise RuntimeError(f"scheduler CAS failed for item {item_index}")
        column = "free_state" if current[0] == "RUNNING" else "paid_state" if current[1] == "RUNNING" else ""
        if not column:
            connection.rollback()
            raise RuntimeError(f"scheduler CAS failed for item {item_index}")
        current_free_attempts = int(current[2])
        if column == "paid_state":
            free_attempts = current_free_attempts
        payload_source_id = str(payload.get("source_record_id", ""))
        if payload_source_id and payload_source_id != str(source_record_id):
            connection.rollback()
            raise ValueError(f"payload source ID mismatch: {item_index}")
        payload.update({
            "source_record_id": str(source_record_id),
            "free_state": str(free_state if column == "free_state" else current[0]),
            "paid_required": bool(paid_required),
            "paid_state": str(paid_state if column == "paid_state" else (paid_state or current[1])),
            "free_attempts": int(free_attempts),
            "paid_attempts": int(paid_attempts),
            "last_error": str(last_error),
        })
        quarantine_state, quarantine_status, blockers = str(current[3] or ""), str(current[4] or ""), str(current[5] or "")
        if not (quarantine_state or quarantine_status or blockers):
            quarantine_state = str(payload.get("quarantine_state", "") or "")
            quarantine_status = str(payload.get("quarantine_status", "") or "")
            blockers = str(payload.get("publication_blockers", "") or "")
        is_legacy_quarantine = quarantine_state == "LEGACY" or "legacy_recovery_provisional" in blockers or str(payload.get("source_record_id_quality", "")).casefold() == "legacy_recovery"
        is_handoff_overlay = quarantine_state == "HANDOFF_PENDING"
        if is_legacy_quarantine or is_handoff_overlay:
            connection.execute(
                "UPDATE run_items SET quarantine_state=?,quarantine_status=?,publication_blockers=? WHERE run_id=? AND item_index=?",
                (quarantine_state, quarantine_status, blockers, run_id, item_index),
            )
        if is_legacy_quarantine or is_handoff_overlay:
            payload["quarantine_state"] = quarantine_state
            payload["quarantine_status"] = quarantine_status
            payload["publication_eligible"] = False
            blocker_values = [value.strip() for value in blockers.replace(";", ",").split(",") if value.strip()]
            if is_legacy_quarantine:
                blocker_values.append("legacy_recovery_provisional")
            payload["publication_blockers"] = ",".join(sorted(set(blocker_values)))
            if is_legacy_quarantine:
                for field in ("website", "email", "phone", "alternative_emails", "alternative_phones"):
                    if field in payload:
                        payload[field] = ""
        safe_payload = json.dumps(_json_safe(redaction.sanitize(payload)), ensure_ascii=False, separators=(",", ":"))
        cursor = connection.execute(
            f"UPDATE run_items SET source_record_id=?,{column}=?,paid_required=?,free_attempts=?,paid_attempts=?,last_error=?,payload_sha256=? WHERE run_id=? AND item_index=? AND {column}=?",
            (source_record_id, free_state if column == "free_state" else paid_state, int(paid_required), free_attempts, paid_attempts, last_error, hashlib.sha256(safe_payload.encode("utf-8")).hexdigest(), run_id, item_index, "RUNNING"),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise RuntimeError(f"scheduler CAS failed for item {item_index}")
        # A free result may also establish the paid queue while preserving the
        # free terminal state in the same transaction.
        if column == "free_state":
            connection.execute("UPDATE run_items SET paid_state=?,paid_required=? WHERE run_id=? AND item_index=?", (paid_state, int(paid_required), run_id, item_index))
        connection.execute("INSERT OR REPLACE INTO results(run_id,item_index,payload) VALUES(?,?,?)", (run_id, item_index, safe_payload))
        connection.execute("UPDATE runs SET updated_at=?,runtime_json=? WHERE run_id=?", (now, json.dumps(_json_safe(runtime.snapshot()), ensure_ascii=False), run_id))
        connection.commit()


def record_paid_attempt(*, run_id: str, item_index: int, attempt_number: int, result: str,
                        reason: str = "", call_id: str = "", request_fingerprint: str = "", call_ids: list[str] | None = None) -> None:
    if result not in {"COMPLETED", "NO_CALL_NEEDED", "BLOCKED_BUDGET", "FAILED", "UNKNOWN"}:
        raise ValueError(f"invalid paid attempt result: {result}")
    with closing(_connect()) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO paid_attempts(run_id,item_index,attempt_number,phase,result,reason,call_id,request_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, int(item_index), int(attempt_number), "PAID", result, str(reason), str(call_id), str(request_fingerprint), datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        for linked_call_id in sorted(set(call_ids or ([call_id] if call_id else []))):
            valid = connection.execute("SELECT 1 FROM provider_calls WHERE run_id=? AND item_index=? AND phase='PAID' AND call_id=?", (run_id, int(item_index), linked_call_id)).fetchone()
            if not valid:
                connection.rollback()
                raise ValueError(f"paid attempt references unknown provider call: {linked_call_id}")
            connection.execute("INSERT OR IGNORE INTO paid_attempt_calls(run_id,item_index,attempt_number,phase,call_id) VALUES(?,?,?,?,?)", (run_id, int(item_index), int(attempt_number), "PAID", linked_call_id))
        connection.commit()


def begin_paid_attempt(*, run_id: str, item_index: int, attempt_number: int) -> None:
    """Durably create the paid attempt at item claim time."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = connection.execute("SELECT paid_required,paid_state FROM run_items WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
        if not item or int(item[0]) != 1 or str(item[1]) != "RUNNING":
            connection.rollback()
            raise RuntimeError("paid attempt claim requires a paid RUNNING item")
        existing = connection.execute("SELECT result FROM paid_attempts WHERE run_id=? AND item_index=? AND attempt_number=? AND phase='PAID'", (run_id, int(item_index), int(attempt_number))).fetchone()
        if existing and str(existing[0]) not in {"RUNNING", "UNKNOWN"}:
            connection.rollback()
            raise RuntimeError("paid attempt ordinal already has a terminal result")
        if not existing:
            connection.execute("INSERT INTO paid_attempts(run_id,item_index,attempt_number,phase,result,created_at) VALUES(?,?,?,?,?,?)", (run_id, int(item_index), int(attempt_number), "PAID", "RUNNING", now))
        connection.commit()


def load_paid_attempts(run_id: str, item_index: int | None = None) -> list[dict[str, Any]]:
    with closing(_connect()) as connection:
        query = "SELECT item_index,attempt_number,result,reason,call_id,request_fingerprint,created_at FROM paid_attempts WHERE run_id=?"
        args: tuple[Any, ...] = (run_id,)
        if item_index is not None:
            query += " AND item_index=?"
            args += (int(item_index),)
        query += " ORDER BY item_index,attempt_number"
        rows = connection.execute(query, args).fetchall()
    fields = ("item_index", "attempt_number", "result", "reason", "call_id", "request_fingerprint", "created_at")
    return [dict(zip(fields, row)) for row in rows]


def load_paid_attempt_calls(run_id: str, item_index: int, attempt_number: int) -> list[str]:
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT call_id FROM paid_attempt_calls WHERE run_id=? AND item_index=? AND attempt_number=? AND phase='PAID' ORDER BY call_id", (run_id, int(item_index), int(attempt_number))).fetchall()
    return [str(row[0]) for row in rows]


def latest_provider_call(run_id: str, item_index: int, *, phase: str = "PAID") -> dict[str, str]:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT call_id,request_fingerprint FROM provider_calls WHERE run_id=? AND item_index=? AND phase=? ORDER BY created_at DESC LIMIT 1",
            (run_id, int(item_index), phase),
        ).fetchone()
    return {"call_id": str(row[0]), "request_fingerprint": str(row[1])} if row else {"call_id": "", "request_fingerprint": ""}


def provider_calls_for_item(run_id: str, item_index: int, *, phase: str = "PAID") -> list[dict[str, str]]:
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT call_id,request_fingerprint,provider,state FROM provider_calls WHERE run_id=? AND item_index=? AND phase=? ORDER BY created_at,call_id", (run_id, int(item_index), phase)).fetchall()
    return [{"call_id": str(call_id), "request_fingerprint": str(fingerprint), "provider": str(provider), "state": str(state)} for call_id, fingerprint, provider, state in rows]


def provider_call_exists(*, run_id: str, provider: str, item_index: int, phase: str,
                         request_fingerprint: str) -> bool:
    with closing(_connect()) as connection:
        return connection.execute(
            "SELECT 1 FROM provider_calls WHERE run_id=? AND provider=? AND item_index=? AND phase=? AND request_fingerprint=? LIMIT 1",
            (run_id, provider, int(item_index), phase, request_fingerprint),
        ).fetchone() is not None


def provider_call_for_fingerprint(*, run_id: str, provider: str, item_index: int,
                                  phase: str, request_fingerprint: str) -> dict[str, str] | None:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT call_id,state,result_ref FROM provider_calls WHERE run_id=? AND provider=? AND item_index=? AND phase=? AND request_fingerprint=? LIMIT 1",
            (run_id, provider, int(item_index), phase, request_fingerprint),
        ).fetchone()
    if not row:
        return None
    return {"call_id": str(row[0]), "state": str(row[1]), "result_ref": str(row[2] or "")}


def begin_finalization_intent(*, run_id: str, generation: str, input_snapshot_sha256: str,
                              output_context: dict[str, Any] | None = None,
                              result_snapshot_sha256: str | None = None,
                              telemetry_snapshot: dict[str, Any] | None = None) -> None:
    with closing(_connect()) as connection:
        existing = connection.execute("SELECT generation,input_snapshot_sha256,result_snapshot_sha256,status FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
        if existing:
            if (str(existing[0]) != str(generation)
                    or str(existing[1]) != str(input_snapshot_sha256)
                    or result_snapshot_sha256 is not None and str(existing[2]) != str(result_snapshot_sha256)):
                raise RuntimeError("finalization intent identity changed")
            if str(existing[3]) not in {"STARTED", "ARTIFACT_READY", "COMPLETE"}:
                raise RuntimeError("invalid finalization intent state")
            connection.commit()
            return
        frozen_telemetry = _json_safe(telemetry_snapshot or {})
        connection.execute(
            "INSERT INTO finalization_intent(run_id,generation,input_snapshot_sha256,result_snapshot_sha256,started_at,status,output_context_json,telemetry_snapshot_json,finalization_schema_version) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, str(generation), str(input_snapshot_sha256), str(result_snapshot_sha256 or input_snapshot_sha256), datetime.now(timezone.utc).isoformat(timespec="seconds"), "STARTED", json.dumps({"phase": "FINALIZING", **_json_safe(output_context or {})}, ensure_ascii=False), json.dumps(frozen_telemetry, ensure_ascii=False), 2),
        )
        connection.commit()


def complete_finalization_intent(*, run_id: str, artifact_set_sha256: str, manifest_sha256: str) -> None:
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status,artifact_set_sha256,manifest_sha256 FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            connection.rollback()
            raise RuntimeError("finalization intent is missing")
        if str(row[0]) == "COMPLETE":
            if str(row[1]) != str(artifact_set_sha256) or str(row[2]) != str(manifest_sha256):
                connection.rollback()
                raise RuntimeError("finalization receipt identity changed")
            connection.commit()
            return
        if str(row[0]) not in {"STARTED", "ARTIFACT_READY"}:
            connection.rollback()
            raise RuntimeError("finalization intent is not completable")
        plan = connection.execute("SELECT memory_plan_sha256,memory_plan_count,memory_plan_committed FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
        if not plan or int(plan[2] or 0) != 1:
            connection.rollback()
            raise RuntimeError("finalization memory plan is not committed")
        entries, plan_hash = _outbox_plan(connection, run_id)
        if len(entries) != int(plan[1]) or plan_hash != str(plan[0]):
            connection.rollback()
            raise RuntimeError("finalization memory plan exact receipt mismatch")
        if not artifact_set_sha256 or not manifest_sha256:
            connection.rollback()
            raise RuntimeError("finalization receipt identity is incomplete")
        connection.execute("UPDATE finalization_intent SET artifact_set_sha256=?,manifest_sha256=?,completed_at=?,status='COMPLETE' WHERE run_id=? AND status IN ('STARTED','ARTIFACT_READY')", (str(artifact_set_sha256), str(manifest_sha256), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id))
        connection.commit()


def mark_finalization_artifact(*, run_id: str, artifact_set_sha256: str, output_context: dict[str, Any]) -> None:
    with closing(_connect()) as connection:
        row = connection.execute("SELECT status,artifact_set_sha256,output_context_json FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
        if not row or str(row[0]) not in {"STARTED", "ARTIFACT_READY"}:
            connection.rollback()
            raise RuntimeError("finalization artifact cannot be prepared in the current state")
        if str(row[1]) and str(row[1]) != str(artifact_set_sha256):
            connection.rollback()
            raise RuntimeError("finalization artifact identity changed")
        existing_context = json.loads(str(row[2] or "{}"))
        existing_context.update(_json_safe(output_context))
        connection.execute("UPDATE finalization_intent SET artifact_set_sha256=?,output_context_json=?,status='ARTIFACT_READY' WHERE run_id=?", (str(artifact_set_sha256), json.dumps(existing_context, ensure_ascii=False), run_id))
        connection.commit()


def _canonical_memory_plan(run_id: str, rows: list[dict[str, Any]]) -> tuple[list[tuple[str, str]], str]:
    safe_rows = [_json_safe(dict(row)) for row in rows]
    source_ids = [str(row.get("source_record_id", "")) for row in safe_rows]
    if any(not value for value in source_ids):
        raise ValueError("memory plan row is missing source_record_id")
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("duplicate memory plan source_record_id")
    entries = sorted(
        (f"{run_id}:{source_id}", json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for source_id, row in zip(source_ids, safe_rows)
    )
    material = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return entries, hashlib.sha256(material.encode("utf-8")).hexdigest()


def mark_finalization_artifact_and_outbox(*, run_id: str, generation: str,
                                          result_snapshot_sha256: str,
                                          artifact_set_sha256: str,
                                          artifacts: dict[str, Any],
                                          memory_rows: list[dict[str, Any]],
                                          counts: dict[str, Any] | None = None,
                                          telemetry_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Commit the immutable artifact receipt and its complete memory plan atomically."""
    safe_artifacts = _json_safe(artifacts)
    plan_entries, plan_hash = _canonical_memory_plan(run_id, memory_rows)
    plan_count = len(plan_entries)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    frozen_telemetry = _json_safe(telemetry_snapshot or {})
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        intent = connection.execute(
            "SELECT generation,result_snapshot_sha256,status,artifact_set_sha256,memory_plan_sha256,memory_plan_count,memory_plan_committed FROM finalization_intent WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if not intent:
            connection.rollback()
            raise RuntimeError("finalization intent is missing")
        if str(intent[0]) != str(generation) or str(intent[1]) != str(result_snapshot_sha256):
            connection.rollback()
            raise RuntimeError("finalization intent snapshot identity changed")
        if str(intent[2]) not in {"STARTED", "ARTIFACT_READY"}:
            connection.rollback()
            raise RuntimeError("finalization artifact cannot be prepared in the current state")
        if str(intent[3]) and str(intent[3]) != str(artifact_set_sha256):
            connection.rollback()
            raise RuntimeError("finalization artifact identity changed")
        if int(intent[6] or 0) and (str(intent[4]) != plan_hash or int(intent[5]) != plan_count):
            connection.rollback()
            raise RuntimeError("finalization memory plan identity changed")
        existing_rows = connection.execute(
            "SELECT receipt_key,payload FROM memory_outbox WHERE run_id=? ORDER BY receipt_key",
            (run_id,),
        ).fetchall()
        existing_plan = [(str(receipt), str(payload)) for receipt, payload in existing_rows]
        if existing_plan and existing_plan != plan_entries:
            connection.rollback()
            raise RuntimeError("finalization memory outbox exact plan mismatch")
        if not existing_plan and plan_entries:
            if int(intent[6] or 0):
                connection.rollback()
                raise RuntimeError("committed finalization memory plan is missing outbox rows")
            for receipt_key, payload in plan_entries:
                connection.execute(
                "INSERT INTO memory_outbox(run_id,receipt_key,payload,created_at) VALUES(?,?,?,?)",
                (run_id, receipt_key, payload, now),
            )
        reread_rows = connection.execute(
            "SELECT receipt_key,payload FROM memory_outbox WHERE run_id=? ORDER BY receipt_key",
            (run_id,),
        ).fetchall()
        reread_plan = [(str(receipt), str(payload)) for receipt, payload in reread_rows]
        reread_material = json.dumps(reread_plan, ensure_ascii=False, separators=(",", ":"))
        reread_hash = hashlib.sha256(reread_material.encode("utf-8")).hexdigest()
        if reread_plan != plan_entries or len(reread_plan) != plan_count or reread_hash != plan_hash:
            connection.rollback()
            raise RuntimeError("finalization memory outbox reread mismatch")
        output_context = {
            "phase": "FINALIZING",
            "artifacts": safe_artifacts,
            "counts": _json_safe(counts or {"memory_plan_count": plan_count}),
            "memory_plan_sha256": plan_hash,
            "memory_plan_count": plan_count,
            "memory_plan_committed": True,
            "telemetry_snapshot": frozen_telemetry,
            "elapsed_seconds": frozen_telemetry.get("elapsed_seconds", 0),
        }
        connection.execute(
            "UPDATE finalization_intent SET artifact_set_sha256=?,output_context_json=?,memory_plan_sha256=?,memory_plan_count=?,memory_plan_committed=1,telemetry_snapshot_json=?,status='ARTIFACT_READY' WHERE run_id=? AND status IN ('STARTED','ARTIFACT_READY')",
            (str(artifact_set_sha256), json.dumps(output_context, ensure_ascii=False), plan_hash, plan_count, json.dumps(frozen_telemetry, ensure_ascii=False), run_id),
        )
        connection.commit()
    return {"memory_plan_sha256": plan_hash, "memory_plan_count": plan_count, "memory_plan_committed": True}


def mark_legacy_no_memory_plan(run_id: str, *, artifact_set_sha256: str = "", manifest_sha256: str = "") -> None:
    """Close an old COMPLETE run without inventing a post-hoc memory plan."""
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        run_row = connection.execute("SELECT context_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        context = json.loads(run_row[0] or "{}") if run_row else {}
        legacy_provenance = str((context.get("lineage") or {}).get("type", "")) == "legacy_recovery"
        row = connection.execute(
            "SELECT status,memory_plan_committed,finalization_schema_version FROM finalization_intent WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if not legacy_provenance or row and (int(row[1] or 0) or int(row[2] or 0) != 0):
            connection.rollback()
            raise RuntimeError("current finalization schema has a missing memory plan")
        if row and int(row[1] or 0):
            connection.commit()
            return
        if row:
            connection.execute(
                "UPDATE finalization_intent SET artifact_set_sha256=COALESCE(NULLIF(?,''),artifact_set_sha256),manifest_sha256=COALESCE(NULLIF(?,''),manifest_sha256),status='SKIPPED_LEGACY_NO_PLAN',completed_at=? WHERE run_id=?",
                (str(artifact_set_sha256), str(manifest_sha256), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id),
            )
        else:
            connection.execute(
                "INSERT INTO finalization_intent(run_id,generation,input_snapshot_sha256,result_snapshot_sha256,started_at,artifact_set_sha256,manifest_sha256,completed_at,status,output_context_json,memory_plan_sha256,memory_plan_count,memory_plan_committed,telemetry_snapshot_json,finalization_schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, f"legacy:{run_id}", "", "", datetime.now(timezone.utc).isoformat(timespec="seconds"), str(artifact_set_sha256), str(manifest_sha256), datetime.now(timezone.utc).isoformat(timespec="seconds"), "SKIPPED_LEGACY_NO_PLAN", json.dumps({"memory_plan_committed": False}, ensure_ascii=False), "", 0, 0, "{}", 0),
            )
        connection.commit()


def load_finalization_intent(run_id: str) -> dict[str, Any] | None:
    with closing(_connect()) as connection:
        row = connection.execute("SELECT generation,input_snapshot_sha256,result_snapshot_sha256,started_at,artifact_set_sha256,manifest_sha256,completed_at,status,output_context_json,memory_plan_sha256,memory_plan_count,memory_plan_committed,telemetry_snapshot_json,finalization_schema_version FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    fields = ("generation", "input_snapshot_sha256", "result_snapshot_sha256", "started_at", "artifact_set_sha256", "manifest_sha256", "completed_at", "status", "output_context_json", "memory_plan_sha256", "memory_plan_count", "memory_plan_committed", "telemetry_snapshot_json", "finalization_schema_version")
    return dict(zip(fields, row))


def _outbox_plan(connection: sqlite3.Connection, run_id: str) -> tuple[list[tuple[str, str]], str]:
    rows = connection.execute(
        "SELECT receipt_key,payload FROM memory_outbox WHERE run_id=? ORDER BY receipt_key",
        (run_id,),
    ).fetchall()
    entries = []
    for receipt_key, payload in rows:
        canonical_payload = json.dumps(json.loads(str(payload)), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        entries.append((str(receipt_key), canonical_payload))
    material = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return entries, hashlib.sha256(material.encode("utf-8")).hexdigest()


def validate_finalization_contract(run_id: str, run_root: Path | None = None, *, require_complete: bool = True) -> dict[str, Any]:
    """Validate the finalization receipt before memory drain or success reporting."""
    root = Path(run_root) if run_root is not None else Path(config.RUNS_DIR) / str(run_id)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("finalization manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    intent = load_finalization_intent(run_id)
    if not intent or intent.get("status") not in {"COMPLETE", "SKIPPED_LEGACY_NO_PLAN"}:
        raise RuntimeError("finalization intent is not COMPLETE")
    legacy_without_plan = intent.get("status") == "SKIPPED_LEGACY_NO_PLAN"
    if legacy_without_plan:
        with closing(_connect()) as connection:
            row = connection.execute("SELECT context_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        context = json.loads(row[0] or "{}") if row else {}
        schema_version = intent.get("finalization_schema_version")
        if schema_version is None:
            schema_version = 2
        if int(schema_version) != 0 or str((context.get("lineage") or {}).get("type", "")) != "legacy_recovery":
            raise RuntimeError("SKIPPED_LEGACY_NO_PLAN is not valid for this run")
    artifact_hash = str(manifest.get("artifact_set_sha256", ""))
    if not manifest.get("complete") or manifest.get("phase") != "COMPLETE":
        raise RuntimeError("finalization manifest is not COMPLETE")
    if not artifact_hash or artifact_hash != str(intent.get("artifact_set_sha256", "")):
        raise RuntimeError("finalization artifact identity mismatch")
    manifest_receipt = str(intent.get("manifest_sha256", ""))
    if not manifest_receipt or file_hash(manifest_path) != manifest_receipt:
        raise RuntimeError("finalization manifest receipt hash mismatch")
    artifact_dir = root / "output" / "artifacts" / artifact_hash
    files = manifest.get("files")
    if not artifact_dir.is_dir() or not isinstance(files, dict) or not files:
        raise RuntimeError("finalization immutable artifacts are missing")
    aggregate = []
    for name, info in sorted(files.items()):
        artifact = artifact_dir / str(name)
        if artifact.parent != artifact_dir or not artifact.is_file() or artifact.stat().st_size != int(info.get("bytes", -1)):
            raise RuntimeError(f"finalization artifact metadata mismatch: {name}")
        digest = file_hash(artifact)
        if digest != str(info.get("sha256", "")):
            raise RuntimeError(f"finalization artifact hash mismatch: {name}")
        aggregate.append(f"{artifact.name}:{digest}\n")
    if hashlib.sha256("".join(aggregate).encode("utf-8")).hexdigest() != artifact_hash:
        raise RuntimeError("finalization aggregate artifact hash mismatch")
    with closing(_connect()) as connection:
        phase = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
        expected_phase = "COMPLETE" if require_complete else "FINALIZING"
        if not phase or str(phase[0]) != expected_phase:
            raise RuntimeError("finalization database phase mismatch")
        entries, plan_hash = _outbox_plan(connection, run_id)
        if legacy_without_plan:
            nonterminal = connection.execute(
                "SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET') OR (paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')))",
                (run_id,),
            ).fetchone()[0]
            unresolved = connection.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')",
                (run_id,),
            ).fetchone()[0]
            if nonterminal or unresolved:
                raise RuntimeError("legacy finalization scheduler/provider state is not terminal")
            return {"run_id": run_id, "artifact_set_sha256": artifact_hash, "memory_plan_count": 0, "manifest_sha256": file_hash(manifest_path)}
        if int(intent.get("memory_plan_committed", 0)) != 1:
            raise RuntimeError("finalization memory plan is not committed")
        if len(entries) != int(intent.get("memory_plan_count", -1)) or plan_hash != str(intent.get("memory_plan_sha256", "")):
            raise RuntimeError("finalization memory plan exact receipt mismatch")
        nonterminal = connection.execute(
            "SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET') OR (paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')))",
            (run_id,),
        ).fetchone()[0]
        unresolved = connection.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')",
            (run_id,),
        ).fetchone()[0]
        if nonterminal or unresolved:
            raise RuntimeError("finalization scheduler/provider state is not terminal")
    return {"run_id": run_id, "artifact_set_sha256": artifact_hash, "memory_plan_count": len(entries), "manifest_sha256": file_hash(manifest_path)}


def result_snapshot_sha256(run_id: str) -> str:
    with closing(_connect()) as connection:
        payloads = connection.execute("SELECT item_index,payload FROM results WHERE run_id=? ORDER BY item_index", (run_id,)).fetchall()
        runtime_json = connection.execute("SELECT runtime_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    material = {"payloads": [[int(index), str(payload)] for index, payload in payloads], "runtime": json.loads((runtime_json or ["{}"]) [0] or "{}")}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def enqueue_memory_rows(run_id: str, rows: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entries, _ = _canonical_memory_plan(run_id, rows)
    with closing(_connect()) as connection:
        existing_rows = connection.execute(
            "SELECT receipt_key,payload FROM memory_outbox WHERE run_id=? ORDER BY receipt_key",
            (run_id,),
        ).fetchall()
        existing = [(str(receipt), str(payload)) for receipt, payload in existing_rows]
        if existing and existing != entries:
            connection.rollback()
            raise RuntimeError("memory outbox exact receipt mismatch")
        if not existing:
            for receipt_key, payload in entries:
                connection.execute("INSERT INTO memory_outbox(run_id,receipt_key,payload,created_at) VALUES(?,?,?,?)", (run_id, receipt_key, payload, now))
        connection.commit()


def commit_finalization_memory_plan(*, run_id: str, generation: str,
                                    result_snapshot_sha256: str,
                                    telemetry_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Commit the already-enqueued outbox plan in its own transaction."""
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        intent = connection.execute(
            "SELECT generation,result_snapshot_sha256,status,memory_plan_sha256,memory_plan_count,memory_plan_committed,output_context_json FROM finalization_intent WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if not intent or str(intent[0]) != str(generation) or str(intent[1]) != str(result_snapshot_sha256):
            connection.rollback()
            raise RuntimeError("finalization memory plan identity changed")
        if str(intent[2]) not in {"STARTED", "ARTIFACT_READY"}:
            connection.rollback()
            raise RuntimeError("finalization memory plan is not committable")
        entries, plan_hash = _outbox_plan(connection, run_id)
        if int(intent[5] or 0) == 1:
            if len(entries) != int(intent[4]) or plan_hash != str(intent[3]):
                connection.rollback()
                raise RuntimeError("finalization memory plan exact receipt mismatch")
            connection.commit()
            return {"memory_plan_sha256": plan_hash, "memory_plan_count": len(entries), "memory_plan_committed": True}
        context = json.loads(str(intent[6] or "{}"))
        if telemetry_snapshot is not None:
            context["telemetry_snapshot"] = _json_safe(telemetry_snapshot)
        context["memory_plan_sha256"] = plan_hash
        context["memory_plan_count"] = len(entries)
        connection.execute(
            "UPDATE finalization_intent SET memory_plan_sha256=?,memory_plan_count=?,memory_plan_committed=1,telemetry_snapshot_json=?,output_context_json=? WHERE run_id=? AND status IN ('STARTED','ARTIFACT_READY')",
            (plan_hash, len(entries), json.dumps(_json_safe(telemetry_snapshot or {}), ensure_ascii=False), json.dumps(_json_safe(context), ensure_ascii=False), run_id),
        )
        connection.commit()
    return {"memory_plan_sha256": plan_hash, "memory_plan_count": len(entries), "memory_plan_committed": True}


def load_memory_outbox(run_id: str) -> list[dict[str, Any]]:
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT receipt_key,payload FROM memory_outbox WHERE run_id=? AND state='PENDING' ORDER BY receipt_key", (run_id,)).fetchall()
    return [{"receipt_key": key, "payload": json.loads(payload)} for key, payload in rows]


def load_memory_outbox_entries(run_id: str) -> list[dict[str, Any]]:
    """Return every durable memory receipt, including terminal replay skips."""
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT receipt_key,payload,state,completed_at FROM memory_outbox WHERE run_id=? ORDER BY receipt_key", (run_id,)).fetchall()
    return [{"receipt_key": key, "payload": json.loads(payload), "state": state, "completed_at": completed_at} for key, payload, state, completed_at in rows]


def skip_memory_outbox(run_id: str) -> int:
    with closing(_connect()) as connection:
        cursor = connection.execute("UPDATE memory_outbox SET state='SKIPPED_REPLAY',completed_at=? WHERE run_id=? AND state='PENDING'", (datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id))
        connection.commit()
    return int(cursor.rowcount)


def complete_memory_receipt(run_id: str, receipt_key: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    actual_key = str(receipt_key)
    with closing(_connect()) as connection:
        if not actual_key.startswith(f"{run_id}:"):
            candidate = f"{run_id}:{actual_key}"
            if connection.execute("SELECT 1 FROM memory_outbox WHERE run_id=? AND receipt_key=?", (run_id, candidate)).fetchone():
                actual_key = candidate
        connection.execute("UPDATE memory_outbox SET state='DONE',completed_at=? WHERE run_id=? AND receipt_key=?", (now, run_id, actual_key))
        receipt = connection.execute("SELECT 1 FROM memory_receipts WHERE run_id=? AND receipt_key=?", (run_id, actual_key)).fetchone()
        if not receipt:
            connection.execute("INSERT INTO memory_receipts(run_id,receipt_key,completed_at) VALUES(?,?,?)", (run_id, actual_key, now))
        connection.commit()


def reserve_provider_call(*, run_id: str, provider: str, item_index: int, phase: str,
                          request_fingerprint: str, configured_limit: int, effective_limit: int,
                          operation: str = "") -> str | None:
    if provider not in CANONICAL_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    call_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = connection.execute(
            "SELECT paid_required,paid_state FROM run_items WHERE run_id=? AND item_index=?",
            (run_id, int(item_index)),
        ).fetchone()
        if phase != "PAID" or not item or int(item[0]) != 1 or str(item[1]) != "RUNNING":
            connection.rollback()
            raise RuntimeError("provider reservation requires a paid RUNNING item")
        duplicate = connection.execute(
            "SELECT 1 FROM provider_calls WHERE run_id=? AND provider=? AND item_index=? AND phase=? AND request_fingerprint=? LIMIT 1",
            (run_id, provider, int(item_index), phase, request_fingerprint),
        ).fetchone()
        if duplicate:
            connection.rollback()
            return None
        usage = connection.execute("SELECT reserved,completed,failed,effective_limit FROM provider_usage WHERE run_id=? AND provider=?", (run_id, provider)).fetchone()
        if not usage:
            connection.rollback()
            raise RuntimeError(f"provider budget not initialized: {provider}")
        if int(effective_limit) != int(usage[3]):
            connection.rollback()
            raise RuntimeError(f"provider budget changed during run: {provider}")
        actual = {str(state): int(count) for state, count in connection.execute("SELECT state,COUNT(*) FROM provider_calls WHERE run_id=? AND provider=? GROUP BY state", (run_id, provider)).fetchall()}
        if int(usage[0]) != actual.get("RESERVED", 0) + actual.get("RUNNING", 0) or int(usage[1]) != actual.get("DONE", 0) or int(usage[2]) != actual.get("FAILED", 0) + actual.get("UNKNOWN", 0):
            connection.rollback()
            raise RuntimeError(f"provider ledger counters are inconsistent: {provider}")
        if not usage or int(usage[1]) + int(usage[0]) + int(usage[2]) >= int(usage[3]):
            connection.rollback()
            return None
        connection.execute("UPDATE provider_usage SET reserved=reserved+1 WHERE run_id=? AND provider=?", (run_id, provider))
        connection.execute("INSERT INTO provider_calls(run_id,call_id,provider,item_index,phase,operation,request_fingerprint,state,result_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, call_id, provider, item_index, phase, operation, request_fingerprint, "RESERVED", "", now, now))
        attempt = connection.execute("SELECT attempt_number FROM paid_attempts WHERE run_id=? AND item_index=? AND phase='PAID' AND result='RUNNING' ORDER BY attempt_number DESC LIMIT 1", (run_id, int(item_index))).fetchone()
        if attempt:
            connection.execute("INSERT INTO paid_attempt_calls(run_id,item_index,attempt_number,phase,call_id) VALUES(?,?,?,?,?)", (run_id, int(item_index), int(attempt[0]), "PAID", call_id))
        connection.commit()
    return call_id


def complete_provider_call(*, call_id: str, state: str, result_ref: str = "") -> None:
    state = {"COMPLETED": "DONE", "EMPTY": "DONE", "CACHE_HIT": "DONE"}.get(str(state).upper(), str(state).upper())
    if state not in {"DONE", "FAILED", "UNKNOWN"}:
        raise ValueError(f"invalid provider call state: {state}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        call = connection.execute("SELECT run_id,provider,state FROM provider_calls WHERE call_id=?", (call_id,)).fetchone()
        if not call or call[2] in {"DONE", "FAILED", "UNKNOWN"}:
            connection.rollback()
            return
        connection.execute("UPDATE provider_calls SET state=?,result_ref=?,updated_at=? WHERE call_id=?", (state, result_ref, now, call_id))
        connection.execute("UPDATE provider_usage SET reserved=MAX(0,reserved-1),completed=completed+?,failed=failed+? WHERE run_id=? AND provider=?", (int(state == "DONE"), int(state != "DONE"), call[0], call[1]))
        connection.commit()


def complete_provider_call_for_context(*, run_id: str, provider: str, item_index: int,
                                       phase: str, state: str, result_ref: str = "") -> None:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT call_id FROM provider_calls WHERE run_id=? AND provider=? AND item_index=? AND phase=? AND state IN ('RESERVED','RUNNING') ORDER BY created_at DESC LIMIT 1",
            (run_id, provider, int(item_index), phase),
        ).fetchone()
    if row:
        complete_provider_call(call_id=str(row[0]), state=state, result_ref=result_ref)


def reconcile_unknown_provider_calls(run_id: str) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        calls = connection.execute("SELECT call_id,provider,item_index FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')", (run_id,)).fetchall()
        for call_id, provider, item_index in calls:
            connection.execute("UPDATE provider_calls SET state='UNKNOWN',updated_at=? WHERE call_id=?", (now, call_id))
            connection.execute("UPDATE provider_usage SET reserved=MAX(0,reserved-1),failed=failed+1 WHERE run_id=? AND provider=?", (run_id, provider))
            connection.execute("UPDATE paid_attempts SET result='UNKNOWN',reason='provider_call_reconciled_unknown' WHERE run_id=? AND item_index=? AND phase='PAID' AND attempt_number IN (SELECT attempt_number FROM paid_attempt_calls WHERE run_id=? AND item_index=? AND phase='PAID' AND call_id=?)", (run_id, int(item_index), run_id, int(item_index), call_id))
            connection.execute("UPDATE run_items SET paid_state='UNKNOWN',last_error='provider_call_reconciled_unknown' WHERE run_id=? AND item_index=? AND paid_required=1", (run_id, int(item_index)))
        connection.commit()
    return len(calls)


def set_phase(input_path: Path, run_signature: str, phase: str) -> None:
    input_hash = file_hash(input_path)
    run_id = _run_id(input_hash, run_signature)
    with closing(_connect()) as connection:
        connection.execute("UPDATE runs SET phase=?,updated_at=? WHERE run_id=?", (str(phase), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id))
        connection.commit()


def load_run_state(input_path: Path, run_signature: str) -> dict[str, Any] | None:
    input_hash = file_hash(input_path)
    run_id = _run_id(input_hash, run_signature)
    if not config.PROGRESS_DB_FILE.exists():
        return None
    with closing(_connect()) as connection:
        row = connection.execute("SELECT phase,context_json,budgets_json,attempt_number FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    return {"run_id": run_id, "phase": row[0], "context": json.loads(row[1] or "{}"), "budgets": json.loads(row[2] or "{}"), "attempt_number": int(row[3] or 1)}


def load_run_state_by_id(run_id: str) -> dict[str, Any] | None:
    if not config.PROGRESS_DB_FILE.exists():
        return None
    with closing(_connect()) as connection:
        row = connection.execute("SELECT input_hash,run_signature,phase,context_json,budgets_json,attempt_number,runtime_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    return {"run_id": run_id, "input_hash": row[0], "run_signature": row[1], "phase": row[2], "context": json.loads(row[3] or "{}"), "budgets": json.loads(row[4] or "{}"), "attempt_number": int(row[5] or 1), "runtime_snapshot": json.loads(row[6] or "{}")}


def set_phase_by_id(run_id: str, phase: str) -> None:
    with closing(_connect()) as connection:
        connection.execute("UPDATE runs SET phase=?,updated_at=? WHERE run_id=?", (str(phase), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id))
        connection.commit()


def save_progress(input_path: Path, last_completed_index: int, results_so_far: list[dict[str, Any]], run_signature: str = "") -> None:
    """Compatibility API; stores each supplied row in SQLite once."""
    for offset, row in enumerate(results_so_far):
        save_result(input_path, int(row.get("__index", offset)), row, run_signature)


def clear_run_progress(input_path: Path, run_signature: str = "") -> None:
    """Remove only one completed run, preserving other checkpoints."""
    input_hash = file_hash(input_path)
    run_id = _run_id(input_hash, run_signature)
    if config.PROGRESS_DB_FILE.exists():
        with closing(_connect()) as connection:
            connection.execute("DELETE FROM results WHERE run_id=?", (run_id,))
            connection.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
            connection.commit()
    # The marker points only to the most recently written run. Remove it when
    # stale or completed; other SQLite runs remain discoverable via has_progress.
    if config.PROGRESS_FILE.exists():
        try:
            marker = json.loads(config.PROGRESS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            marker = {}
        if (
            marker.get("input_file_hash") == input_hash
            and marker.get("run_signature", "") == run_signature
        ):
            config.PROGRESS_FILE.unlink(missing_ok=True)


def clear_progress() -> None:
    """Destructively clear every checkpoint (explicit reset compatibility)."""
    if config.PROGRESS_FILE.exists():
        config.PROGRESS_FILE.unlink()
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{config.PROGRESS_DB_FILE}{suffix}")
        if path.exists():
            path.unlink()
