"""Crash-safe per-company SQLite checkpoints with legacy JSON compatibility."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import os
import uuid
import time
import threading
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from functools import lru_cache
from contextlib import closing

import config
from modules import redaction, runtime


CANONICAL_PROVIDERS = frozenset({"brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm"})
SCHEDULER_SCHEMA_VERSION = 12
SCHEDULER_REQUIRED_RECEIPT_TRIGGERS = frozenset({
    "paid_attempt_calls_owner_scope_insert", "paid_attempt_calls_owner_scope_update",
    "paid_attempt_calls_inherited_scope_insert", "paid_attempt_calls_inherited_scope_update",
    "flight_consumer_scope_insert", "flight_consumer_scope_update",
    "paid_attempt_block_scope_insert", "paid_attempt_block_scope_update",
    "flight_result_scope_insert", "flight_result_scope_update",
    "flight_terminal_scope_insert", "flight_terminal_scope_update",
    "flight_result_identity_update", "flight_terminal_identity_update", "flight_consumer_identity_update",
    "flight_result_receipt_update", "flight_result_receipt_delete",
    "flight_terminal_receipt_update", "flight_terminal_receipt_delete",
    "provider_call_transport_receipt_update",
})
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


def _is_sha256(value: object) -> bool:
    text = str(value)
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _run_id(input_hash: str, run_signature: str) -> str:
    return hashlib.sha256(f"{input_hash}\0{run_signature}".encode("utf-8")).hexdigest()


_SCHEMA_LOCK = threading.RLock()
class SchedulerInvariantError(RuntimeError):
    """A durable scheduler/ledger contract was violated."""


class StateTransitionInvariant(SchedulerInvariantError):
    pass


class LedgerInvariant(SchedulerInvariantError):
    pass


class EvidenceInvariant(SchedulerInvariantError):
    pass


class ResumeInvariant(SchedulerInvariantError):
    pass


class OutcomeInvariant(SchedulerInvariantError):
    pass


class ReplayInvariantError(SchedulerInvariantError):
    """A behavioral replay record does not match the durable execution."""

    pass


_SCHEMA_READY: set[tuple[str, int, int]] = set()


def _open_connection(path: Path) -> sqlite3.Connection:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _migrate_schema(connection: sqlite3.Connection) -> None:
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if schema_version == SCHEDULER_SCHEMA_VERSION:
        return
    connection.execute("BEGIN IMMEDIATE")
    for trigger in (
        "paid_attempt_calls_owner_scope_insert", "paid_attempt_calls_owner_scope_update",
        "paid_attempt_calls_inherited_scope_insert", "paid_attempt_calls_inherited_scope_update",
        "flight_consumer_scope_insert", "flight_consumer_scope_update",
        "paid_attempt_block_scope_insert", "paid_attempt_block_scope_update",
        "flight_result_scope_insert", "flight_result_scope_update",
        "flight_terminal_scope_insert", "flight_terminal_scope_update",
        "flight_result_identity_update", "flight_terminal_identity_update",
        "flight_consumer_identity_update",
        "flight_result_receipt_update", "flight_result_receipt_delete",
        "flight_terminal_receipt_update", "flight_terminal_receipt_delete",
        "provider_call_transport_receipt_update",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
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
    connection.execute(
        "CREATE TABLE IF NOT EXISTS immutable_input_snapshots (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, snapshot_sha256 TEXT NOT NULL, snapshot_json TEXT NOT NULL, PRIMARY KEY(run_id,item_index))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS run_phase_transitions (run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, from_phase TEXT NOT NULL, to_phase TEXT NOT NULL, transitioned_at TEXT NOT NULL, PRIMARY KEY(run_id,ordinal))"
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
    usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_usage)")}
    for name, definition in {"reserved_total": "INTEGER NOT NULL DEFAULT 0", "unknown": "INTEGER NOT NULL DEFAULT 0"}.items():
        if name not in usage_columns:
            connection.execute(f"ALTER TABLE provider_usage ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_calls (run_id TEXT NOT NULL, call_id TEXT PRIMARY KEY, provider TEXT NOT NULL, item_index INTEGER NOT NULL, phase TEXT NOT NULL, operation TEXT NOT NULL DEFAULT '', request_fingerprint TEXT NOT NULL, state TEXT NOT NULL, result_ref TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_calls)")}
    if "operation" not in columns:
        connection.execute("ALTER TABLE provider_calls ADD COLUMN operation TEXT NOT NULL DEFAULT ''")
    for name, definition in {
        "http_started_at": "TEXT NOT NULL DEFAULT ''",
        "attempt_ordinal": "INTEGER NOT NULL DEFAULT 1",
        "flight_fingerprint": "TEXT NOT NULL DEFAULT ''",
        "endpoint_sha256": "TEXT NOT NULL DEFAULT ''",
        "request_shape_sha256": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE provider_calls ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_call_identity ON provider_calls(run_id,provider,item_index,phase,request_fingerprint)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS replay_entries (run_id TEXT NOT NULL, store TEXT NOT NULL, namespace TEXT NOT NULL, key_sha256 TEXT NOT NULL, schema_version INTEGER NOT NULL, prefix_json TEXT NOT NULL, value_json TEXT NOT NULL, PRIMARY KEY(run_id,store,namespace,key_sha256,schema_version))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS paid_attempts (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, attempt_number INTEGER NOT NULL, phase TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', call_id TEXT NOT NULL DEFAULT '', request_fingerprint TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY(run_id,item_index,attempt_number,phase))"
    )
    paid_attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(paid_attempts)")}
    if "paid_attempt_id" not in paid_attempt_columns:
        connection.execute("ALTER TABLE paid_attempts ADD COLUMN paid_attempt_id TEXT NOT NULL DEFAULT ''")
    if "evidence_kind" not in paid_attempt_columns:
        connection.execute("ALTER TABLE paid_attempts ADD COLUMN evidence_kind TEXT NOT NULL DEFAULT ''")
    if "input_snapshot_sha256" not in paid_attempt_columns:
        connection.execute("ALTER TABLE paid_attempts ADD COLUMN input_snapshot_sha256 TEXT NOT NULL DEFAULT ''")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_paid_attempt_id ON paid_attempts(paid_attempt_id) WHERE paid_attempt_id<>''")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS paid_attempt_calls (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, attempt_number INTEGER NOT NULL, phase TEXT NOT NULL, call_id TEXT NOT NULL, paid_attempt_id TEXT NOT NULL DEFAULT '', provider_call_id TEXT NOT NULL DEFAULT '', relation TEXT NOT NULL DEFAULT 'OWNER' CHECK(relation IN ('OWNER','INHERITED')), PRIMARY KEY(run_id,item_index,attempt_number,phase,call_id), FOREIGN KEY(provider_call_id) REFERENCES provider_calls(call_id))"
    )
    paid_link_columns = {row[1] for row in connection.execute("PRAGMA table_info(paid_attempt_calls)")}
    for name, definition in {
        "paid_attempt_id": "TEXT NOT NULL DEFAULT ''",
        "provider_call_id": "TEXT NOT NULL DEFAULT ''",
        "relation": "TEXT NOT NULL DEFAULT 'OWNER'",
    }.items():
        if name not in paid_link_columns:
            connection.execute(f"ALTER TABLE paid_attempt_calls ADD COLUMN {name} {definition}")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_paid_attempt_provider_relation ON paid_attempt_calls(run_id,item_index,paid_attempt_id,provider_call_id,relation)")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_query_flight_consumers (run_id TEXT NOT NULL, provider TEXT NOT NULL, query_fingerprint TEXT NOT NULL, paid_attempt_id TEXT NOT NULL, item_index INTEGER NOT NULL, provider_call_id TEXT NOT NULL, relation TEXT NOT NULL CHECK(relation IN ('OWNER','INHERITED')), linked_at TEXT NOT NULL, PRIMARY KEY(run_id,provider,query_fingerprint,paid_attempt_id,provider_call_id), FOREIGN KEY(provider_call_id) REFERENCES provider_calls(call_id), FOREIGN KEY(run_id,provider,query_fingerprint) REFERENCES provider_query_flights(run_id,provider,query_fingerprint))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS paid_no_call_evidence (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, paid_attempt_id TEXT NOT NULL, evidence_kind TEXT NOT NULL, input_snapshot_sha256 TEXT NOT NULL, normalized_input_website TEXT NOT NULL, evaluation_payload_sha256 TEXT NOT NULL, result_payload_sha256 TEXT NOT NULL, publication_eligible INTEGER NOT NULL, evaluator_schema_version INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,item_index,paid_attempt_id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS paid_attempt_provider_plan (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, paid_attempt_id TEXT NOT NULL, provider TEXT NOT NULL, plan_ordinal INTEGER NOT NULL, authorized INTEGER NOT NULL, effective_limit INTEGER NOT NULL, PRIMARY KEY(run_id,item_index,paid_attempt_id,provider))"
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
    if schema_version < 10 and "telemetry_sha256" not in finalization_columns:
        connection.execute("ALTER TABLE finalization_intent ADD COLUMN telemetry_sha256 TEXT NOT NULL DEFAULT ''")
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
    connection.execute(
        "CREATE TABLE IF NOT EXISTS free_provider_attempts (attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, item_index INTEGER NOT NULL, bucket TEXT NOT NULL CHECK(bucket IN ('discovery','targeted')), provider TEXT NOT NULL, query_fingerprint TEXT NOT NULL, attempt_ordinal INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('RESERVED','DONE','FAILED')), reserved_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '', error_class TEXT NOT NULL DEFAULT '', UNIQUE(run_id,item_index,bucket,provider,query_fingerprint,attempt_ordinal))"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS ix_free_attempt_run_item ON free_provider_attempts(run_id,item_index,bucket,state)")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_budget_blocks (run_id TEXT NOT NULL, item_index INTEGER NOT NULL, provider TEXT NOT NULL, bucket TEXT NOT NULL DEFAULT '', block_kind TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,item_index,provider,bucket,block_kind))"
    )
    block_columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_budget_blocks)")}
    for name, definition in {"block_id": "TEXT NOT NULL DEFAULT ''", "backend": "TEXT NOT NULL DEFAULT ''"}.items():
        if name not in block_columns:
            connection.execute(f"ALTER TABLE provider_budget_blocks ADD COLUMN {name} {definition}")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_budget_block_id ON provider_budget_blocks(block_id) WHERE block_id<>''")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_free_block_scope ON provider_budget_blocks(run_id,item_index,bucket,block_kind) WHERE provider='ddgs'")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS paid_attempt_block_links (paid_attempt_id TEXT NOT NULL, block_id TEXT NOT NULL, PRIMARY KEY(paid_attempt_id,block_id))"
    )
    if schema_version < 8:
        connection.execute(
            "UPDATE provider_usage SET "
        "reserved_total=(SELECT COUNT(*) FROM provider_calls pc WHERE pc.run_id=provider_usage.run_id AND pc.provider=provider_usage.provider),"
        "reserved=(SELECT COUNT(*) FROM provider_calls pc WHERE pc.run_id=provider_usage.run_id AND pc.provider=provider_usage.provider AND pc.state IN ('RESERVED','RUNNING')) ,"
        "completed=(SELECT COUNT(*) FROM provider_calls pc WHERE pc.run_id=provider_usage.run_id AND pc.provider=provider_usage.provider AND pc.state='DONE'),"
        "failed=(SELECT COUNT(*) FROM provider_calls pc WHERE pc.run_id=provider_usage.run_id AND pc.provider=provider_usage.provider AND pc.state='FAILED'),"
        "unknown=(SELECT COUNT(*) FROM provider_calls pc WHERE pc.run_id=provider_usage.run_id AND pc.provider=provider_usage.provider AND pc.state='UNKNOWN') "
            "WHERE run_id IN (SELECT run_id FROM runs WHERE phase<>'COMPLETE')"
        )
    query_columns = {row[1] for row in connection.execute("PRAGMA table_info(free_query_usage)")}
    legacy_has_bucket_attribution = {"discovery_physical_used", "targeted_physical_used"}.issubset(query_columns)
    query_migrations = {
        "discovery_used": "INTEGER NOT NULL DEFAULT 0",
        "targeted_used": "INTEGER NOT NULL DEFAULT 0",
        "logical_used": "INTEGER NOT NULL DEFAULT 0",
        "logical_quota": "INTEGER NOT NULL DEFAULT 10",
        "discovery_logical_used": "INTEGER NOT NULL DEFAULT 0",
        "targeted_logical_used": "INTEGER NOT NULL DEFAULT 0",
        "physical_used": "INTEGER NOT NULL DEFAULT 0",
        "physical_quota": "INTEGER NOT NULL DEFAULT 10",
        "physical_completed": "INTEGER NOT NULL DEFAULT 0",
        "physical_failed": "INTEGER NOT NULL DEFAULT 0",
        "discovery_physical_used": "INTEGER NOT NULL DEFAULT 0",
        "targeted_physical_used": "INTEGER NOT NULL DEFAULT 0",
        "logical_blocked": "INTEGER NOT NULL DEFAULT 0",
        "physical_blocked": "INTEGER NOT NULL DEFAULT 0",
        "blocked_at": "TEXT NOT NULL DEFAULT ''",
        "blocked_bucket": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in query_migrations.items():
        if name not in query_columns:
            connection.execute(f"ALTER TABLE free_query_usage ADD COLUMN {name} {definition}")
    if schema_version < 8:
        connection.execute(
            "UPDATE free_query_usage SET logical_used=MAX(logical_used,used),logical_quota=quota,"
            "discovery_logical_used=MAX(discovery_logical_used,discovery_used),"
            "targeted_logical_used=MAX(targeted_logical_used,targeted_used) WHERE run_id IN (SELECT run_id FROM runs WHERE phase<>'COMPLETE')"
        )
    legacy_rows = connection.execute(
        "SELECT q.run_id,q.item_index,q.physical_used,q.physical_completed,q.physical_failed,q.discovery_physical_used,q.targeted_physical_used "
        "FROM free_query_usage q JOIN runs r ON r.run_id=q.run_id WHERE r.phase<>'COMPLETE'"
    ).fetchall() if schema_version < 8 else []
    for run_id, item_index, physical_used, completed, failed, discovery_used, targeted_used in legacy_rows:
        values = tuple(int(value or 0) for value in (physical_used, completed, failed, discovery_used, targeted_used))
        physical_used, completed, failed, discovery_used, targeted_used = values
        if not legacy_has_bucket_attribution and physical_used:
            discovery_used = min(6, physical_used)
            targeted_used = physical_used - discovery_used
            completed = failed = 0
            connection.execute("UPDATE free_query_usage SET discovery_physical_used=?,targeted_physical_used=?,physical_completed=0,physical_failed=0 WHERE run_id=? AND item_index=?", (discovery_used, targeted_used, run_id, int(item_index)))
        if min(physical_used, completed, failed, discovery_used, targeted_used) < 0 or completed + failed > physical_used or discovery_used + targeted_used != physical_used or physical_used > 10 or discovery_used > 6 or targeted_used > 4:
            raise LedgerInvariant("legacy free physical counters are inconsistent")
        buckets = ["discovery"] * discovery_used + ["targeted"] * targeted_used
        states = ["DONE"] * completed + ["FAILED"] * failed + ["RESERVED"] * (physical_used - completed - failed)
        expected = Counter(zip(buckets, states))
        actual = Counter(
            (str(bucket), str(state)) for bucket, state in connection.execute(
                "SELECT bucket,state FROM free_provider_attempts WHERE run_id=? AND item_index=?",
                (run_id, int(item_index)),
            ).fetchall()
        )
        if any(count > expected[key] for key, count in actual.items()):
            raise LedgerInvariant("legacy free attempt multiset is not a subset of deterministic counters")
        for bucket in ("discovery", "targeted"):
            for state in ("DONE", "FAILED", "RESERVED"):
                for ordinal in range(actual[(bucket, state)] + 1, expected[(bucket, state)] + 1):
                    identity = f"legacy:{bucket}:{state}:{ordinal}"
                    attempt_id = f"legacy:{run_id}:{int(item_index)}:{bucket}:{state}:{ordinal}"
                    connection.execute("INSERT INTO free_provider_attempts(attempt_id,run_id,item_index,bucket,provider,query_fingerprint,attempt_ordinal,state,reserved_at,finished_at,error_class) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, run_id, int(item_index), bucket, "ddgs", identity, ordinal, state, "legacy-migration", "legacy-migration" if state != "RESERVED" else "", "legacy" if state == "FAILED" else ""))
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_query_flights (run_id TEXT NOT NULL, provider TEXT NOT NULL, query_fingerprint TEXT NOT NULL, state TEXT NOT NULL, owner_token TEXT NOT NULL, lease_expires_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL DEFAULT '', provider_call_id TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}', call_ids_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(run_id,provider,query_fingerprint))"
    )
    flight_columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_query_flights)")}
    for name, definition in {
        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
        "provider_call_id": "TEXT NOT NULL DEFAULT ''",
        "execution_generation": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        if name not in flight_columns:
            connection.execute(f"ALTER TABLE provider_query_flights ADD COLUMN {name} {definition}")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_query_flight_results (run_id TEXT NOT NULL, provider TEXT NOT NULL, query_fingerprint TEXT NOT NULL, execution_generation INTEGER NOT NULL, provider_call_id TEXT NOT NULL, result_json TEXT NOT NULL, result_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,provider,query_fingerprint,execution_generation), FOREIGN KEY(run_id,provider,query_fingerprint) REFERENCES provider_query_flights(run_id,provider,query_fingerprint))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_query_flight_terminals (run_id TEXT NOT NULL,provider TEXT NOT NULL,query_fingerprint TEXT NOT NULL,execution_generation INTEGER NOT NULL,state TEXT NOT NULL CHECK(state IN ('DONE','FAILED','UNKNOWN')),provider_call_id TEXT NOT NULL DEFAULT '',result_sha256 TEXT NOT NULL,call_ids_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(run_id,provider,query_fingerprint,execution_generation),FOREIGN KEY(run_id,provider,query_fingerprint) REFERENCES provider_query_flights(run_id,provider,query_fingerprint))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS paid_query_plan_entries (run_id TEXT NOT NULL,item_index INTEGER NOT NULL,plan_version INTEGER NOT NULL,query_kind TEXT NOT NULL,round_ordinal INTEGER NOT NULL,query_ordinal INTEGER NOT NULL,normalized_query TEXT NOT NULL,query_sha256 TEXT NOT NULL,PRIMARY KEY(run_id,item_index,plan_version,query_kind,round_ordinal,query_ordinal))"
    )
    if schema_version < 9:
        empty_attempt_ids = connection.execute(
            "SELECT COUNT(*) FROM paid_attempts WHERE paid_attempt_id=''"
        ).fetchone()[0]
        if int(empty_attempt_ids):
            raise EvidenceInvariant("legacy paid attempt lacks an unambiguous paid_attempt_id")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_call_run_provider ON provider_calls(run_id,provider,call_id)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_call_run_item ON provider_calls(run_id,item_index,call_id)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_paid_attempt_scope ON paid_attempts(run_id,item_index,paid_attempt_id)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_budget_block_scope_id ON provider_budget_blocks(run_id,item_index,provider,block_id)")

        bad_links = connection.execute(
            "SELECT COUNT(*) FROM paid_attempt_calls l LEFT JOIN paid_attempts a ON a.run_id=l.run_id AND a.item_index=l.item_index AND a.paid_attempt_id=l.paid_attempt_id LEFT JOIN provider_calls c ON c.run_id=l.run_id AND c.call_id=l.provider_call_id WHERE a.paid_attempt_id IS NULL OR c.call_id IS NULL OR (l.relation='OWNER' AND c.item_index<>l.item_index)"
        ).fetchone()[0]
        bad_consumers = connection.execute(
            "SELECT COUNT(*) FROM provider_query_flight_consumers x LEFT JOIN paid_attempts a ON a.run_id=x.run_id AND a.item_index=x.item_index AND a.paid_attempt_id=x.paid_attempt_id LEFT JOIN provider_query_flights f ON f.run_id=x.run_id AND f.provider=x.provider AND f.query_fingerprint=x.query_fingerprint LEFT JOIN provider_calls c ON c.run_id=x.run_id AND c.provider=x.provider AND c.call_id=x.provider_call_id WHERE a.paid_attempt_id IS NULL OR f.query_fingerprint IS NULL OR c.call_id IS NULL"
        ).fetchone()[0]
        bad_blocks = connection.execute(
            "SELECT COUNT(*) FROM paid_attempt_block_links l LEFT JOIN paid_attempts a ON a.paid_attempt_id=l.paid_attempt_id LEFT JOIN provider_budget_blocks b ON b.block_id=l.block_id WHERE a.paid_attempt_id IS NULL OR b.block_id IS NULL OR a.run_id<>b.run_id OR a.item_index<>b.item_index"
        ).fetchone()[0]
        if any(int(value) for value in (bad_links, bad_consumers, bad_blocks)):
            raise EvidenceInvariant("legacy relation row cannot be mapped to one exact scope")

        connection.execute("DROP TABLE IF EXISTS paid_attempt_calls_v9")
        connection.execute(
            "CREATE TABLE paid_attempt_calls_v9 (run_id TEXT NOT NULL,item_index INTEGER NOT NULL,attempt_number INTEGER NOT NULL,phase TEXT NOT NULL,call_id TEXT NOT NULL,paid_attempt_id TEXT NOT NULL CHECK(paid_attempt_id<>''),provider_call_id TEXT NOT NULL,provider TEXT NOT NULL,query_fingerprint TEXT NOT NULL DEFAULT '',execution_generation INTEGER NOT NULL DEFAULT 1 CHECK(execution_generation>0),relation TEXT NOT NULL CHECK(relation IN ('OWNER','INHERITED')),PRIMARY KEY(run_id,item_index,attempt_number,phase,call_id),FOREIGN KEY(run_id,item_index,paid_attempt_id) REFERENCES paid_attempts(run_id,item_index,paid_attempt_id),FOREIGN KEY(run_id,provider,provider_call_id) REFERENCES provider_calls(run_id,provider,call_id))"
        )
        connection.execute(
            "INSERT INTO paid_attempt_calls_v9 SELECT l.run_id,l.item_index,l.attempt_number,l.phase,l.call_id,l.paid_attempt_id,l.provider_call_id,c.provider,c.flight_fingerprint,COALESCE(f.execution_generation,1),l.relation FROM paid_attempt_calls l JOIN provider_calls c ON c.run_id=l.run_id AND c.call_id=l.provider_call_id LEFT JOIN provider_query_flights f ON f.run_id=c.run_id AND f.provider=c.provider AND f.query_fingerprint=c.flight_fingerprint"
        )
        connection.execute("DROP TABLE paid_attempt_calls")
        connection.execute("ALTER TABLE paid_attempt_calls_v9 RENAME TO paid_attempt_calls")
        connection.execute("CREATE UNIQUE INDEX uq_paid_attempt_provider_relation ON paid_attempt_calls(run_id,item_index,paid_attempt_id,provider_call_id,relation)")

        connection.execute("DROP TABLE IF EXISTS provider_query_flight_consumers_v9")
        connection.execute(
            "CREATE TABLE provider_query_flight_consumers_v9 (run_id TEXT NOT NULL,provider TEXT NOT NULL,query_fingerprint TEXT NOT NULL,execution_generation INTEGER NOT NULL CHECK(execution_generation>0),paid_attempt_id TEXT NOT NULL CHECK(paid_attempt_id<>''),item_index INTEGER NOT NULL,provider_call_id TEXT NOT NULL,relation TEXT NOT NULL CHECK(relation IN ('OWNER','INHERITED')),linked_at TEXT NOT NULL,PRIMARY KEY(run_id,provider,query_fingerprint,paid_attempt_id,provider_call_id),FOREIGN KEY(run_id,item_index,paid_attempt_id) REFERENCES paid_attempts(run_id,item_index,paid_attempt_id),FOREIGN KEY(run_id,provider,query_fingerprint) REFERENCES provider_query_flights(run_id,provider,query_fingerprint),FOREIGN KEY(run_id,provider,provider_call_id) REFERENCES provider_calls(run_id,provider,call_id))"
        )
        connection.execute(
            "INSERT INTO provider_query_flight_consumers_v9 SELECT x.run_id,x.provider,x.query_fingerprint,f.execution_generation,x.paid_attempt_id,x.item_index,x.provider_call_id,x.relation,x.linked_at FROM provider_query_flight_consumers x JOIN provider_query_flights f ON f.run_id=x.run_id AND f.provider=x.provider AND f.query_fingerprint=x.query_fingerprint"
        )
        connection.execute("DROP TABLE provider_query_flight_consumers")
        connection.execute("ALTER TABLE provider_query_flight_consumers_v9 RENAME TO provider_query_flight_consumers")

        connection.execute("DROP TABLE IF EXISTS paid_attempt_block_links_v9")
        connection.execute(
            "CREATE TABLE paid_attempt_block_links_v9 (run_id TEXT NOT NULL,item_index INTEGER NOT NULL,provider TEXT NOT NULL,paid_attempt_id TEXT NOT NULL CHECK(paid_attempt_id<>''),block_id TEXT NOT NULL,PRIMARY KEY(run_id,item_index,paid_attempt_id,block_id),FOREIGN KEY(run_id,item_index,paid_attempt_id) REFERENCES paid_attempts(run_id,item_index,paid_attempt_id),FOREIGN KEY(run_id,item_index,provider,block_id) REFERENCES provider_budget_blocks(run_id,item_index,provider,block_id))"
        )
        connection.execute(
            "INSERT INTO paid_attempt_block_links_v9 SELECT a.run_id,a.item_index,b.provider,l.paid_attempt_id,l.block_id FROM paid_attempt_block_links l JOIN paid_attempts a ON a.paid_attempt_id=l.paid_attempt_id JOIN provider_budget_blocks b ON b.block_id=l.block_id"
        )
        connection.execute("DROP TABLE paid_attempt_block_links")
        connection.execute("ALTER TABLE paid_attempt_block_links_v9 RENAME TO paid_attempt_block_links")

        connection.execute("DROP TRIGGER IF EXISTS paid_attempt_calls_owner_scope")
        connection.execute(
            "CREATE TRIGGER paid_attempt_calls_owner_scope BEFORE INSERT ON paid_attempt_calls WHEN NEW.relation='OWNER' AND NOT EXISTS(SELECT 1 FROM provider_calls c WHERE c.run_id=NEW.run_id AND c.item_index=NEW.item_index AND c.call_id=NEW.provider_call_id AND c.provider=NEW.provider) BEGIN SELECT RAISE(ABORT,'OWNER call scope mismatch'); END"
        )
        connection.execute("DROP TRIGGER IF EXISTS paid_attempt_calls_inherited_scope")
        connection.execute(
            "CREATE TRIGGER paid_attempt_calls_inherited_scope BEFORE INSERT ON paid_attempt_calls WHEN NEW.relation='INHERITED' AND NOT EXISTS(SELECT 1 FROM provider_query_flight_consumers x WHERE x.run_id=NEW.run_id AND x.item_index=NEW.item_index AND x.paid_attempt_id=NEW.paid_attempt_id AND x.provider=NEW.provider AND x.query_fingerprint=NEW.query_fingerprint AND x.execution_generation=NEW.execution_generation AND x.provider_call_id=NEW.provider_call_id AND x.relation='INHERITED') BEGIN SELECT RAISE(ABORT,'INHERITED consumer scope mismatch'); END"
        )

        connection.execute("DROP TABLE IF EXISTS provider_query_flight_results_v9")
        connection.execute(
            "CREATE TABLE provider_query_flight_results_v9 (run_id TEXT NOT NULL,provider TEXT NOT NULL,query_fingerprint TEXT NOT NULL,execution_generation INTEGER NOT NULL,provider_call_id TEXT NOT NULL,result_json TEXT NOT NULL,result_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(run_id,provider,query_fingerprint,execution_generation),FOREIGN KEY(run_id,provider,query_fingerprint) REFERENCES provider_query_flights(run_id,provider,query_fingerprint),FOREIGN KEY(run_id,provider,provider_call_id) REFERENCES provider_calls(run_id,provider,call_id))"
        )
        connection.execute("INSERT INTO provider_query_flight_results_v9 SELECT * FROM provider_query_flight_results")
        connection.execute("DROP TABLE provider_query_flight_results")
        connection.execute("ALTER TABLE provider_query_flight_results_v9 RENAME TO provider_query_flight_results")
    if schema_version < 12:
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_flight_generation ON provider_query_flights(run_id,provider,query_fingerprint,execution_generation)")
        trigger_specs = {
            "paid_attempt_calls_owner_scope": "NEW.relation='OWNER' AND NOT EXISTS(SELECT 1 FROM provider_calls c WHERE c.run_id=NEW.run_id AND c.item_index=NEW.item_index AND c.call_id=NEW.provider_call_id AND c.provider=NEW.provider AND ((NEW.query_fingerprint='' AND c.flight_fingerprint='' AND NEW.execution_generation=1) OR (NEW.query_fingerprint<>'' AND c.flight_fingerprint=NEW.query_fingerprint AND (EXISTS(SELECT 1 FROM provider_query_flight_terminals t,json_each(t.call_ids_json) j WHERE t.run_id=NEW.run_id AND t.provider=NEW.provider AND t.query_fingerprint=NEW.query_fingerprint AND t.execution_generation=NEW.execution_generation AND j.value=NEW.provider_call_id) OR (EXISTS(SELECT 1 FROM provider_query_flights f WHERE f.run_id=NEW.run_id AND f.provider=NEW.provider AND f.query_fingerprint=NEW.query_fingerprint AND f.execution_generation=NEW.execution_generation) AND NOT EXISTS(SELECT 1 FROM provider_query_flight_terminals t,json_each(t.call_ids_json) j WHERE t.run_id=NEW.run_id AND t.provider=NEW.provider AND t.query_fingerprint=NEW.query_fingerprint AND j.value=NEW.provider_call_id))))))",
            "paid_attempt_calls_inherited_scope": "NEW.relation='INHERITED' AND NOT EXISTS(SELECT 1 FROM provider_query_flight_consumers x JOIN provider_query_flight_terminals t ON t.run_id=x.run_id AND t.provider=x.provider AND t.query_fingerprint=x.query_fingerprint AND t.execution_generation=x.execution_generation JOIN provider_calls c ON c.run_id=x.run_id AND c.provider=x.provider AND c.call_id=x.provider_call_id LEFT JOIN provider_query_flight_results r ON r.run_id=x.run_id AND r.provider=x.provider AND r.query_fingerprint=x.query_fingerprint AND r.execution_generation=x.execution_generation WHERE x.run_id=NEW.run_id AND x.item_index=NEW.item_index AND x.paid_attempt_id=NEW.paid_attempt_id AND x.provider=NEW.provider AND x.query_fingerprint=NEW.query_fingerprint AND x.execution_generation=NEW.execution_generation AND x.provider_call_id=NEW.provider_call_id AND x.relation='INHERITED' AND ((t.state='DONE' AND r.provider_call_id IS NOT NULL) OR (t.state='FAILED' AND c.state='FAILED')))",
            "flight_consumer_scope": "NOT EXISTS(SELECT 1 FROM paid_attempts a JOIN provider_calls c ON c.run_id=NEW.run_id AND c.provider=NEW.provider AND c.call_id=NEW.provider_call_id JOIN provider_query_flights f ON f.run_id=NEW.run_id AND f.provider=NEW.provider AND f.query_fingerprint=NEW.query_fingerprint WHERE a.run_id=NEW.run_id AND a.item_index=NEW.item_index AND a.paid_attempt_id=NEW.paid_attempt_id AND f.execution_generation=NEW.execution_generation AND (NEW.relation='INHERITED' OR c.item_index=NEW.item_index))",
            "paid_attempt_block_scope": "NOT EXISTS(SELECT 1 FROM paid_attempts a JOIN provider_budget_blocks b ON b.run_id=NEW.run_id AND b.item_index=NEW.item_index AND b.provider=NEW.provider AND b.block_id=NEW.block_id WHERE a.run_id=NEW.run_id AND a.item_index=NEW.item_index AND a.paid_attempt_id=NEW.paid_attempt_id)",
            "flight_result_scope": "NOT EXISTS(SELECT 1 FROM provider_query_flights f JOIN provider_calls c ON c.run_id=NEW.run_id AND c.provider=NEW.provider AND c.call_id=NEW.provider_call_id JOIN paid_attempt_calls l ON l.run_id=c.run_id AND l.provider=c.provider AND l.provider_call_id=c.call_id AND l.query_fingerprint=NEW.query_fingerprint AND l.execution_generation=NEW.execution_generation AND l.relation='OWNER' WHERE f.run_id=NEW.run_id AND f.provider=NEW.provider AND f.query_fingerprint=NEW.query_fingerprint AND f.execution_generation=NEW.execution_generation AND c.flight_fingerprint=NEW.query_fingerprint AND c.state='DONE' AND c.http_started_at<>'' AND length(c.endpoint_sha256)=64 AND length(c.request_shape_sha256)=64)",
            "flight_terminal_scope": "NOT EXISTS(SELECT 1 FROM provider_query_flights f WHERE f.run_id=NEW.run_id AND f.provider=NEW.provider AND f.query_fingerprint=NEW.query_fingerprint AND f.execution_generation=NEW.execution_generation AND json_valid(NEW.call_ids_json) AND ((NEW.state='DONE' AND NEW.provider_call_id<>'' AND EXISTS(SELECT 1 FROM provider_query_flight_results r JOIN provider_calls c ON c.run_id=r.run_id AND c.provider=r.provider AND c.call_id=r.provider_call_id JOIN paid_attempt_calls l ON l.run_id=c.run_id AND l.provider=c.provider AND l.provider_call_id=c.call_id AND l.query_fingerprint=r.query_fingerprint AND l.execution_generation=r.execution_generation AND l.relation='OWNER' WHERE r.run_id=NEW.run_id AND r.provider=NEW.provider AND r.query_fingerprint=NEW.query_fingerprint AND r.execution_generation=NEW.execution_generation AND r.provider_call_id=NEW.provider_call_id AND r.result_sha256=NEW.result_sha256 AND c.state='DONE' AND c.http_started_at<>'' AND length(c.endpoint_sha256)=64 AND length(c.request_shape_sha256)=64 AND c.flight_fingerprint=NEW.query_fingerprint AND EXISTS(SELECT 1 FROM json_each(NEW.call_ids_json) j WHERE j.value=NEW.provider_call_id))) OR (NEW.state IN ('FAILED','UNKNOWN') AND NEW.provider_call_id='')))"
        }
        table_by_trigger = {"paid_attempt_calls_owner_scope": "paid_attempt_calls", "paid_attempt_calls_inherited_scope": "paid_attempt_calls", "flight_consumer_scope": "provider_query_flight_consumers", "paid_attempt_block_scope": "paid_attempt_block_links", "flight_result_scope": "provider_query_flight_results", "flight_terminal_scope": "provider_query_flight_terminals"}
        for trigger, predicate in trigger_specs.items():
            table = table_by_trigger[trigger]
            for operation in ("INSERT", "UPDATE"):
                name = f"{trigger}_{operation.casefold()}"
                connection.execute(f"DROP TRIGGER IF EXISTS {name}")
                connection.execute(f"CREATE TRIGGER {name} BEFORE {operation} ON {table} WHEN {predicate} BEGIN SELECT RAISE(ABORT,'{trigger} mismatch'); END")
        immutable_identity_specs = {
            "flight_result_identity": ("provider_query_flight_results", "NEW.run_id<>OLD.run_id OR NEW.provider<>OLD.provider OR NEW.query_fingerprint<>OLD.query_fingerprint OR NEW.execution_generation<>OLD.execution_generation OR NEW.provider_call_id<>OLD.provider_call_id"),
            "flight_terminal_identity": ("provider_query_flight_terminals", "NEW.run_id<>OLD.run_id OR NEW.provider<>OLD.provider OR NEW.query_fingerprint<>OLD.query_fingerprint OR NEW.execution_generation<>OLD.execution_generation OR NEW.provider_call_id<>OLD.provider_call_id"),
            "flight_consumer_identity": ("provider_query_flight_consumers", "NEW.run_id<>OLD.run_id OR NEW.provider<>OLD.provider OR NEW.query_fingerprint<>OLD.query_fingerprint OR NEW.execution_generation<>OLD.execution_generation OR NEW.paid_attempt_id<>OLD.paid_attempt_id OR NEW.item_index<>OLD.item_index OR NEW.provider_call_id<>OLD.provider_call_id OR NEW.relation<>OLD.relation"),
        }
        for trigger, (table, predicate) in immutable_identity_specs.items():
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}_update")
            connection.execute(f"CREATE TRIGGER {trigger}_update BEFORE UPDATE ON {table} WHEN {predicate} BEGIN SELECT RAISE(ABORT,'{trigger} immutable'); END")
        for trigger, table in {
            "flight_result_receipt": "provider_query_flight_results",
            "flight_terminal_receipt": "provider_query_flight_terminals",
        }.items():
            for operation in ("UPDATE", "DELETE"):
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}_{operation.casefold()}")
                connection.execute(f"CREATE TRIGGER {trigger}_{operation.casefold()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'{trigger} immutable'); END")
        connection.execute("DROP TRIGGER IF EXISTS provider_call_transport_receipt_update")
        connection.execute(
            "CREATE TRIGGER provider_call_transport_receipt_update BEFORE UPDATE ON provider_calls "
            "WHEN (OLD.endpoint_sha256<>'' AND NEW.endpoint_sha256<>OLD.endpoint_sha256) "
            "OR (OLD.request_shape_sha256<>'' AND NEW.request_shape_sha256<>OLD.request_shape_sha256) "
            "OR ((NEW.endpoint_sha256='')<>(NEW.request_shape_sha256='')) "
            "OR (NEW.endpoint_sha256<>'' AND (length(NEW.endpoint_sha256)<>64 OR NEW.endpoint_sha256 GLOB '*[^0-9a-f]*')) "
            "OR (NEW.request_shape_sha256<>'' AND (length(NEW.request_shape_sha256)<>64 OR NEW.request_shape_sha256 GLOB '*[^0-9a-f]*')) "
            "BEGIN SELECT RAISE(ABORT,'provider transport receipt immutable'); END"
        )
    probe_columns = {row[1] for row in connection.execute("PRAGMA table_info(source_probes)")}
    if "lease_expires_at" not in probe_columns:
        connection.execute("ALTER TABLE source_probes ADD COLUMN lease_expires_at TEXT NOT NULL DEFAULT ''")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        connection.rollback()
        raise EvidenceInvariant("foreign key check failed during scheduler migration")
    connection.execute(f"PRAGMA user_version={SCHEDULER_SCHEMA_VERSION}")
    connection.commit()


def _db_identity(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return str(path.resolve()), int(stat.st_dev), int(stat.st_ino)


def _generation_relation_violations(connection: sqlite3.Connection) -> list[str]:
    """Detect forged historical generation links, including writes made with FKs disabled."""
    checks = {
        "flight_result_generation": "SELECT 1 FROM provider_query_flight_results r LEFT JOIN provider_query_flights f ON f.run_id=r.run_id AND f.provider=r.provider AND f.query_fingerprint=r.query_fingerprint LEFT JOIN provider_calls c ON c.run_id=r.run_id AND c.provider=r.provider AND c.call_id=r.provider_call_id LEFT JOIN paid_attempt_calls l ON l.run_id=r.run_id AND l.provider=r.provider AND l.provider_call_id=r.provider_call_id AND l.query_fingerprint=r.query_fingerprint AND l.execution_generation=r.execution_generation AND l.relation='OWNER' WHERE f.query_fingerprint IS NULL OR r.execution_generation<1 OR r.execution_generation>f.execution_generation OR c.call_id IS NULL OR c.state<>'DONE' OR c.http_started_at='' OR length(c.endpoint_sha256)<>64 OR length(c.request_shape_sha256)<>64 OR c.flight_fingerprint<>r.query_fingerprint OR l.provider_call_id IS NULL LIMIT 1",
        "flight_terminal_generation": "SELECT 1 FROM provider_query_flight_terminals t LEFT JOIN provider_query_flights f ON f.run_id=t.run_id AND f.provider=t.provider AND f.query_fingerprint=t.query_fingerprint LEFT JOIN provider_query_flight_results r ON r.run_id=t.run_id AND r.provider=t.provider AND r.query_fingerprint=t.query_fingerprint AND r.execution_generation=t.execution_generation WHERE f.query_fingerprint IS NULL OR t.execution_generation<1 OR t.execution_generation>f.execution_generation OR json_valid(t.call_ids_json)=0 OR (t.state='DONE' AND (t.provider_call_id='' OR r.provider_call_id IS NULL OR r.provider_call_id<>t.provider_call_id OR r.result_sha256<>t.result_sha256 OR NOT EXISTS(SELECT 1 FROM json_each(t.call_ids_json) j WHERE j.value=t.provider_call_id))) OR (t.state IN ('FAILED','UNKNOWN') AND t.provider_call_id<>'') LIMIT 1",
        "flight_consumer_generation": "SELECT 1 FROM provider_query_flight_consumers x LEFT JOIN provider_query_flights f ON f.run_id=x.run_id AND f.provider=x.provider AND f.query_fingerprint=x.query_fingerprint LEFT JOIN provider_query_flight_terminals t ON t.run_id=x.run_id AND t.provider=x.provider AND t.query_fingerprint=x.query_fingerprint AND t.execution_generation=x.execution_generation LEFT JOIN provider_calls c ON c.run_id=x.run_id AND c.provider=x.provider AND c.call_id=x.provider_call_id LEFT JOIN paid_attempt_calls l ON l.run_id=x.run_id AND l.item_index=x.item_index AND l.paid_attempt_id=x.paid_attempt_id AND l.provider=x.provider AND l.provider_call_id=x.provider_call_id AND l.query_fingerprint=x.query_fingerprint AND l.execution_generation=x.execution_generation AND l.relation=x.relation WHERE f.query_fingerprint IS NULL OR x.execution_generation<1 OR x.execution_generation>f.execution_generation OR t.query_fingerprint IS NULL OR c.call_id IS NULL OR l.provider_call_id IS NULL OR NOT EXISTS(SELECT 1 FROM json_each(t.call_ids_json) j WHERE j.value=x.provider_call_id) LIMIT 1",
        "terminal_flight_receipt": "SELECT 1 FROM provider_query_flights f LEFT JOIN provider_query_flight_terminals t ON t.run_id=f.run_id AND t.provider=f.provider AND t.query_fingerprint=f.query_fingerprint AND t.execution_generation=f.execution_generation WHERE f.state IN ('DONE','FAILED','UNKNOWN') AND (t.query_fingerprint IS NULL OR t.state<>f.state OR t.provider_call_id<>f.provider_call_id OR t.call_ids_json<>f.call_ids_json) LIMIT 1",
        "flight_terminal_call_ids_shape": "SELECT 1 FROM provider_query_flight_terminals t WHERE json_type(t.call_ids_json)<>'array' OR EXISTS(SELECT 1 FROM json_each(t.call_ids_json) j WHERE typeof(j.value)<>'text' OR j.value='') OR (SELECT COUNT(*) FROM json_each(t.call_ids_json))<>(SELECT COUNT(DISTINCT value) FROM json_each(t.call_ids_json)) LIMIT 1",
        "terminal_call_scope": "SELECT 1 FROM provider_query_flight_terminals t JOIN json_each(t.call_ids_json) j LEFT JOIN provider_calls c ON c.run_id=t.run_id AND c.provider=t.provider AND c.call_id=j.value LEFT JOIN paid_attempt_calls l ON l.run_id=t.run_id AND l.provider=t.provider AND l.provider_call_id=j.value AND l.query_fingerprint=t.query_fingerprint AND l.execution_generation=t.execution_generation AND l.relation='OWNER' WHERE json_type(t.call_ids_json)<>'array' OR typeof(j.value)<>'text' OR c.call_id IS NULL OR c.flight_fingerprint<>t.query_fingerprint OR l.provider_call_id IS NULL OR (t.state='DONE' AND c.state NOT IN ('DONE','FAILED')) OR (t.state='FAILED' AND c.state<>'FAILED') OR (t.state='UNKNOWN' AND c.state NOT IN ('FAILED','UNKNOWN','DONE')) LIMIT 1",
        "unknown_terminal_state": "SELECT 1 FROM provider_query_flight_terminals t WHERE t.state='UNKNOWN' AND json_array_length(t.call_ids_json)>0 AND NOT EXISTS(SELECT 1 FROM json_each(t.call_ids_json) j JOIN provider_calls c ON c.run_id=t.run_id AND c.provider=t.provider AND c.call_id=j.value WHERE c.state IN ('UNKNOWN','DONE')) LIMIT 1",
    }
    violations: list[str] = []
    for name, query in checks.items():
        try:
            if connection.execute(query).fetchone():
                violations.append(name)
        except sqlite3.Error:
            violations.append(name)
    try:
        for result_json, result_sha256 in connection.execute("SELECT result_json,result_sha256 FROM provider_query_flight_results"):
            if hashlib.sha256(str(result_json).encode()).hexdigest() != str(result_sha256):
                violations.append("flight_result_hash")
                break
        for result_json, result_sha256 in connection.execute(
            "SELECT f.result_json,t.result_sha256 FROM provider_query_flights f JOIN provider_query_flight_terminals t "
            "ON t.run_id=f.run_id AND t.provider=f.provider AND t.query_fingerprint=f.query_fingerprint "
            "AND t.execution_generation=f.execution_generation WHERE f.state IN ('DONE','FAILED','UNKNOWN')"
        ):
            if hashlib.sha256(str(result_json).encode()).hexdigest() != str(result_sha256):
                violations.append("flight_terminal_hash")
                break
    except sqlite3.Error:
        violations.append("flight_receipt_hash")
    return sorted(set(violations))


def _schema_constraint_violations(connection: sqlite3.Connection) -> list[str]:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(provider_calls)")}
    triggers = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    violations = []
    if not {"endpoint_sha256", "request_shape_sha256"}.issubset(columns):
        violations.append("transport_receipt_columns")
    if not SCHEDULER_REQUIRED_RECEIPT_TRIGGERS.issubset(triggers):
        violations.append("receipt_triggers")
    return violations


def _initialize_schema_once(path: Path) -> None:
    target = Path(path).resolve()
    with _SCHEMA_LOCK:
        identity = _db_identity(target)
        if identity is not None and identity in _SCHEMA_READY:
            return
        if identity is not None:
            try:
                with tempfile.TemporaryDirectory(prefix="scheduler-schema-probe-") as probe_dir:
                    probe_path = Path(probe_dir) / target.name
                    shutil.copyfile(target, probe_path)
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(target) + suffix)
                        if sidecar.is_file():
                            shutil.copyfile(sidecar, Path(str(probe_path) + suffix))
                    with closing(sqlite3.connect(f"file:{probe_path.as_posix()}?mode=ro", uri=True)) as probe:
                        version = int(probe.execute("PRAGMA user_version").fetchone()[0])
                        tables = {str(row[0]) for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        phases = {str(row[0]) for row in probe.execute("SELECT phase FROM runs")} if "runs" in tables else set()
                        provider_call_columns = {str(row[1]) for row in probe.execute("PRAGMA table_info(provider_calls)")}
                        triggers = {str(row[0]) for row in probe.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            except sqlite3.Error as exc:
                raise EvidenceInvariant("scheduler schema cannot be inspected read-only") from exc
            if version == SCHEDULER_SCHEMA_VERSION:
                required_triggers = {
                    f"{name}_{operation}"
                    for name in (
                        "flight_result_scope", "flight_terminal_scope", "flight_consumer_scope",
                        "paid_attempt_calls_owner_scope", "paid_attempt_calls_inherited_scope",
                        "paid_attempt_block_scope",
                    )
                    for operation in ("insert", "update")
                } | {"flight_result_identity_update", "flight_terminal_identity_update", "flight_consumer_identity_update"}
                required_triggers |= {
                    "flight_result_receipt_update", "flight_result_receipt_delete",
                    "flight_terminal_receipt_update", "flight_terminal_receipt_delete",
                    "provider_call_transport_receipt_update",
                }
                if (
                    not {"endpoint_sha256", "request_shape_sha256"}.issubset(provider_call_columns)
                    or not required_triggers.issubset(triggers)
                ):
                    raise EvidenceInvariant("scheduler schema 12 is missing required generation receipt constraints")
                _SCHEMA_READY.add(identity)
                return
            if "COMPLETE" in phases:
                raise StateTransitionInvariant("completed checkpoint schema is read-only and cannot be migrated")
        with closing(_open_connection(target)) as connection:
            _migrate_schema(connection)
        current = _db_identity(target)
        if current is None:
            raise SchedulerInvariantError("schema initialization did not create database")
        _SCHEMA_READY.difference_update([entry for entry in _SCHEMA_READY if entry[0] == str(target)])
        _SCHEMA_READY.add(current)


def _connect() -> sqlite3.Connection:
    target = Path(config.PROGRESS_DB_FILE).resolve()
    _initialize_schema_once(target)
    connection = _open_connection(target)
    incomplete = connection.execute("SELECT 1 FROM runs WHERE phase<>'COMPLETE' LIMIT 1").fetchone()
    if incomplete:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        schema_violations = _schema_constraint_violations(connection)
        generation_violations = _generation_relation_violations(connection)
        if violations or schema_violations or generation_violations:
            connection.close()
            detail = "foreign key" if violations else ",".join(schema_violations or generation_violations)
            raise EvidenceInvariant(f"relational evidence check failed for incomplete run: {detail}")
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
            raise StateTransitionInvariant("source probe owner lost during heartbeat")
        connection.commit()
    return lease_expires


def finish_source_probe(*, run_id: str, host: str, owner_token: str, snapshot: dict[str, Any] | None = None, error: str = "") -> None:
    state = "ERROR" if error else "DONE"
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("UPDATE source_probes SET state=?,snapshot_json=?,error=?,updated_at=?,lease_expires_at='' WHERE run_id=? AND host=? AND state='RUNNING' AND owner_token=?", (state, json.dumps(_json_safe(snapshot or {}), ensure_ascii=False), str(error), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id, host, owner_token))
        if cursor.rowcount != 1:
            connection.rollback()
            raise StateTransitionInvariant("source probe owner lost")
        connection.commit()


def _reserve_free_query(*, run_id: str, item_index: int, bucket: str, kind: str) -> dict[str, Any]:
    bucket = str(bucket or "").casefold()
    if bucket not in {"discovery", "targeted"}:
        bucket = "discovery"
    bucket_limit = 6 if bucket == "discovery" else 4
    used_column = f"{bucket}_{kind}_used"
    total_column = f"{kind}_used"
    quota_column = f"{kind}_quota"
    blocked_column = f"{kind}_blocked"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO free_query_usage(run_id,item_index,used,quota,discovery_used,targeted_used,logical_quota,physical_quota) VALUES(?,?,0,10,0,0,10,10)",
            (run_id, int(item_index)),
        )
        row = connection.execute(
            f"SELECT logical_used,logical_quota,physical_used,physical_quota,{used_column},{quota_column} FROM free_query_usage WHERE run_id=? AND item_index=?",
            (run_id, int(item_index)),
        ).fetchone()
        total_used, total_limit = int(row[0 if kind == "logical" else 2]), int(row[1 if kind == "logical" else 3])
        bucket_used = int(row[4])
        if total_used >= total_limit:
            accepted, reason = False, f"{kind}_total_exhausted"
        elif bucket_used >= bucket_limit:
            accepted, reason = False, f"{kind}_bucket_exhausted"
        else:
            accepted, reason = True, "accepted"
            legacy = ",used=used+1,discovery_used=discovery_used+?" if kind == "logical" else ""
            legacy_value = 1 if bucket == "discovery" else 0
            if kind == "logical" and bucket == "targeted":
                legacy = ",used=used+1,targeted_used=targeted_used+?"
            connection.execute(
                f"UPDATE free_query_usage SET {total_column}={total_column}+1,{used_column}={used_column}+1{legacy} WHERE run_id=? AND item_index=?",
                ((legacy_value if bucket == "discovery" else 1), run_id, int(item_index)) if legacy else (run_id, int(item_index)),
            )
            total_used += 1
            bucket_used += 1
        if not accepted:
            block_id = hashlib.sha256(f"{run_id}\0{int(item_index)}\0ddgs\0{bucket}\0{kind}".encode()).hexdigest()
            block = connection.execute(
                "INSERT OR IGNORE INTO provider_budget_blocks(run_id,item_index,provider,bucket,block_kind,created_at,block_id,backend) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, int(item_index), "ddgs", bucket, kind, now, block_id, "ddgs"),
            )
            if block.rowcount == 1:
                connection.execute(
                    f"UPDATE free_query_usage SET {blocked_column}={blocked_column}+1,blocked_at=CASE WHEN blocked_at='' THEN ? ELSE blocked_at END,blocked_bucket=CASE WHEN blocked_bucket='' THEN ? ELSE blocked_bucket END WHERE run_id=? AND item_index=?",
                    (now, bucket, run_id, int(item_index)),
                )
        current = connection.execute("SELECT logical_used,logical_quota,physical_used,physical_quota FROM free_query_usage WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
        connection.commit()
    return {"accepted": accepted, "reason": reason, "bucket": bucket, "logical_used": int(current[0]), "logical_limit": int(current[1]), "physical_used": int(current[2]), "physical_limit": int(current[3])}


def reserve_free_logical_query(*, run_id: str, item_index: int, bucket: str, query_fingerprint: str = "") -> dict[str, Any]:
    return _reserve_free_query(run_id=run_id, item_index=item_index, bucket=bucket, kind="logical")


def reserve_free_physical_attempt(*, run_id: str, item_index: int, bucket: str, query_fingerprint: str = "", backend: str = "") -> dict[str, Any]:
    bucket = bucket if bucket in {"discovery", "targeted"} else "discovery"
    provider = str(backend or "ddgs").casefold()
    fingerprint = str(query_fingerprint or "")
    bucket_limit = 6 if bucket == "discovery" else 4
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO free_query_usage(run_id,item_index,used,quota,discovery_used,targeted_used,logical_quota,physical_quota) VALUES(?,?,0,10,0,0,10,10)",
            (run_id, int(item_index)),
        )
        totals = connection.execute(
            "SELECT COUNT(*),SUM(CASE WHEN bucket=? THEN 1 ELSE 0 END) FROM free_provider_attempts WHERE run_id=? AND item_index=?",
            (bucket, run_id, int(item_index)),
        ).fetchone()
        counters = connection.execute(f"SELECT physical_used,{bucket}_physical_used FROM free_query_usage WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
        total_used = max(int(totals[0] or 0), int(counters[0] or 0))
        bucket_used = max(int(totals[1] or 0), int(counters[1] or 0))
        if total_used >= 10 or bucket_used >= bucket_limit:
            reason = "physical_total_exhausted" if total_used >= 10 else "physical_bucket_exhausted"
            block_id = hashlib.sha256(f"{run_id}\0{int(item_index)}\0ddgs\0{bucket}\0physical".encode()).hexdigest()
            block = connection.execute(
                "INSERT OR IGNORE INTO provider_budget_blocks(run_id,item_index,provider,bucket,block_kind,created_at,block_id,backend) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, int(item_index), "ddgs", bucket, "physical", now, block_id, str(provider)),
            )
            if block.rowcount == 1:
                connection.execute(
                    "UPDATE free_query_usage SET physical_blocked=physical_blocked+1,blocked_at=CASE WHEN blocked_at='' THEN ? ELSE blocked_at END,blocked_bucket=CASE WHEN blocked_bucket='' THEN ? ELSE blocked_bucket END WHERE run_id=? AND item_index=?",
                    (now, bucket, run_id, int(item_index)),
                )
            connection.commit()
            return {"accepted": False, "reason": reason, "bucket": bucket, "attempt_id": "", "attempt_ordinal": 0, "logical_used": 0, "logical_limit": 10, "physical_used": total_used, "physical_limit": 10}
        ordinal = int(connection.execute(
            "SELECT COALESCE(MAX(attempt_ordinal),0)+1 FROM free_provider_attempts WHERE run_id=? AND item_index=? AND bucket=? AND provider=? AND query_fingerprint=?",
            (run_id, int(item_index), bucket, provider, fingerprint),
        ).fetchone()[0])
        attempt_id = hashlib.sha256(f"{run_id}\0{item_index}\0{bucket}\0{provider}\0{fingerprint}\0{ordinal}".encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO free_provider_attempts(attempt_id,run_id,item_index,bucket,provider,query_fingerprint,attempt_ordinal,state,reserved_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (attempt_id, run_id, int(item_index), bucket, provider, fingerprint, ordinal, "RESERVED", now),
        )
        connection.execute(
            f"UPDATE free_query_usage SET physical_used=physical_used+1,{bucket}_physical_used={bucket}_physical_used+1 WHERE run_id=? AND item_index=?",
            (run_id, int(item_index)),
        )
        usage = connection.execute("SELECT logical_used,logical_quota,physical_used,physical_quota FROM free_query_usage WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
        connection.commit()
    return {"accepted": True, "reason": "accepted", "bucket": bucket, "attempt_id": attempt_id, "attempt_ordinal": ordinal, "logical_used": int(usage[0]), "logical_limit": int(usage[1]), "physical_used": int(usage[2]), "physical_limit": int(usage[3])}


def free_search_capacity(*, run_id: str, item_index: int, bucket: str) -> dict[str, Any]:
    bucket = bucket if bucket in {"discovery", "targeted"} else "discovery"
    logical_column = f"{bucket}_logical_used"
    physical_column = f"{bucket}_physical_used"
    bucket_limit = 6 if bucket == "discovery" else 4
    with closing(_connect()) as connection:
        row = connection.execute(f"SELECT logical_used,logical_quota,physical_used,physical_quota,{logical_column},{physical_column} FROM free_query_usage WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
    if not row:
        return {"available": True, "bucket": bucket, "logical_used": 0, "logical_limit": bucket_limit, "physical_used": 0, "physical_limit": bucket_limit}
    available = int(row[0]) < int(row[1]) and int(row[2]) < int(row[3]) and int(row[4]) < bucket_limit and int(row[5]) < bucket_limit
    return {"available": available, "bucket": bucket, "logical_used": int(row[0]), "logical_limit": int(row[1]), "physical_used": int(row[2]), "physical_limit": int(row[3])}


def complete_free_physical_attempt(*, attempt_id: str, success: bool, error_class: str | None = None, run_id: str | None = None) -> None:
    column = "physical_completed" if success else "physical_failed"
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT run_id,item_index,state FROM free_provider_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
        if row is None or (run_id is not None and str(row[0]) != str(run_id)):
            connection.rollback()
            raise SchedulerInvariantError("free physical attempt completion references unknown/wrong-run attempt")
        if str(row[2]) != "RESERVED":
            connection.rollback()
            raise SchedulerInvariantError("free physical attempt completed more than once")
        state = "DONE" if success else "FAILED"
        sanitized_error = str(redaction.sanitize(str(error_class or "")))[:120]
        cursor = connection.execute(
            "UPDATE free_provider_attempts SET state=?,finished_at=?,error_class=? WHERE attempt_id=? AND state='RESERVED'",
            (state, datetime.now(timezone.utc).isoformat(timespec="microseconds"), sanitized_error, str(attempt_id)),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise SchedulerInvariantError("free physical attempt completion CAS failed")
        connection.execute(f"UPDATE free_query_usage SET {column}={column}+1 WHERE run_id=? AND item_index=?", (str(row[0]), int(row[1])))
        connection.commit()


def reserve_free_search_query(*, run_id: str, item_index: int, limit: int = 10, bucket: str | None = None) -> bool:
    """Reserve one live free query atomically for exactly one item.

    An explicit intent gets six discovery slots or four targeted slots.  The
    omitted bucket keeps the legacy ten-query total view for low-level callers.
    """
    if bucket:
        return bool(reserve_free_logical_query(run_id=run_id, item_index=item_index, bucket=bucket)["accepted"])
    limit = max(1, min(10, int(limit)))
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT OR IGNORE INTO free_query_usage(run_id,item_index,used,quota,logical_quota,physical_quota) VALUES(?,?,0,?,?,10)", (run_id, int(item_index), limit, limit))
        cursor = connection.execute("UPDATE free_query_usage SET used=used+1,logical_used=logical_used+1 WHERE run_id=? AND item_index=? AND logical_used<logical_quota", (run_id, int(item_index)))
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
    _initialize_schema_once(Path(path) if path is not None else config.PROGRESS_DB_FILE)


def seal_checkpoint_for_handoff(
    active_db: Path,
    *,
    run_id: str,
    expected_count: int,
) -> dict[str, Any]:
    """Validate and seal an idle owned checkpoint before taking its snapshot.

    This is the only handoff boundary that changes the live SQLite journal
    mode.  It uses an explicit read/write connection with a zero busy timeout;
    an active reader, writer, worker, or provider lease therefore fails closed.
    No sidecar is removed by this function: SQLite removes its own WAL state
    when the journal mode transition commits.
    """
    active_db = Path(active_db).resolve()
    if not active_db.is_file():
        raise FileNotFoundError(active_db)
    uri = f"{active_db.as_uri()}?mode=rw"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=0)) as connection:
            connection.execute("PRAGMA busy_timeout=0")
            row = connection.execute("SELECT run_id,phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or str(row[0]) != str(run_id):
                raise RuntimeError("handoff checkpoint run identity mismatch")
            item_count, source_count = connection.execute(
                "SELECT COUNT(*),COUNT(DISTINCT source_record_id) FROM run_items WHERE run_id=?",
                (run_id,),
            ).fetchone()
            result_count = connection.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            if int(item_count) != int(expected_count) or int(source_count) != int(expected_count) or int(result_count) != int(expected_count):
                raise RuntimeError("handoff checkpoint coverage is not exact")
            running_items = connection.execute(
                "SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state='RUNNING' OR paid_state='RUNNING')",
                (run_id,),
            ).fetchone()[0]
            unresolved_calls = connection.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')",
                (run_id,),
            ).fetchone()[0]
            running_probes = connection.execute(
                "SELECT COUNT(*) FROM source_probes WHERE run_id=? AND state='RUNNING'",
                (run_id,),
            ).fetchone()[0]
            if running_items or unresolved_calls or running_probes:
                raise RuntimeError("handoff checkpoint is not idle")

            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and int(checkpoint[0]) != 0:
                connection.rollback()
                raise RuntimeError("handoff WAL checkpoint is busy")
            journal_mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).casefold()
            if journal_mode != "delete":
                connection.rollback()
                raise RuntimeError("handoff checkpoint could not switch to DELETE journal mode")
            connection.commit()
    except sqlite3.Error as exc:
        raise RuntimeError("handoff checkpoint requires an idle owned database") from exc
    if any(Path(f"{active_db}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
        raise RuntimeError("handoff checkpoint still has SQLite sidecars after sealing")
    return {"sha256": file_hash(active_db), "bytes": active_db.stat().st_size, "run_id": str(run_id)}


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
            with closing(sqlite3.connect(staging)) as destination_tmp:
                journal_mode = str(destination_tmp.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).casefold()
                if journal_mode != "delete":
                    raise RuntimeError("handoff staging must use DELETE journal mode")
                source.backup(destination_tmp)
                destination_tmp.commit()

        with closing(sqlite3.connect(staging)) as staged:
            journal_mode = str(staged.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).casefold()
            if journal_mode != "delete":
                raise RuntimeError("handoff staging must use DELETE journal mode")
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
                       items: list[dict[str, Any]], results: list[dict[str, Any]],
                       input_snapshots: dict[int, dict[str, Any]] | None = None) -> None:
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
                raise ResumeInvariant("duplicate seed payload index")
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
                        raise ResumeInvariant(f"seed payload JSON invalid: {index}") from exc
                    payload_source_id = str(payload.get("source_record_id", ""))
                    if payload_source_id != str(item["source_record_id"]):
                        connection.rollback()
                        raise ResumeInvariant(f"seed payload source ID mismatch: {index}")
                    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                    if item.get("payload_sha256") and str(item["payload_sha256"]) != payload_hash:
                        connection.rollback()
                        raise ResumeInvariant(f"seed payload hash mismatch: {index}")
                    item["payload_sha256"] = payload_hash
                    for field in ("quarantine_state", "quarantine_status", "publication_blockers"):
                        item_value = str(item.get(field, ""))
                        payload_value = str(payload.get(field, ""))
                        if item_value and item_value != payload_value:
                            connection.rollback()
                            raise ResumeInvariant(f"seed {field} mismatch: {index}")
                        if not item_value and payload_value:
                            item[field] = payload_value
                    allowed_quarantine_markers = {"legacy_recovery_provisional", "HANDOFF_PENDING"}
                    payload_markers = set(value.strip() for value in str(payload.get("publication_blockers", "")).replace(",", ";").split(";") if value.strip())
                    if (item.get("quarantine_state") or item.get("quarantine_status") or allowed_quarantine_markers & set(value.strip() for value in str(item.get("publication_blockers", "")).replace(",", ";").split(";") if value.strip())) and (payload.get("publication_eligible") is not False or not allowed_quarantine_markers & payload_markers):
                        connection.rollback()
                        raise ResumeInvariant(f"seed quarantine publication mismatch: {index}")
                if provisional_seed and not any(str(item.get(field, "")) for field in ("quarantine_state", "quarantine_status", "publication_blockers")):
                    connection.rollback()
                    raise ResumeInvariant(f"seed quarantine metadata is missing: {index}")
                normalized_items.append(item)
            now = str(context.get("seed_timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
            connection.execute(
                "INSERT INTO runs(run_id,input_hash,run_signature,updated_at,phase,context_json,budgets_json,attempt_number,runtime_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, input_hash, run_signature, now, str(context.get("phase", "FREE")), json.dumps(_json_safe(context), ensure_ascii=False), json.dumps(_json_safe(budgets), ensure_ascii=False), 1, json.dumps({"phase": context.get("phase", "FREE"), "counters": {}}, ensure_ascii=False)),
            )
            for item in normalized_items:
                snapshot_payload = (input_snapshots or {}).get(int(item["item_index"]), item)
                snapshot_json = json.dumps(_json_safe(snapshot_payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                snapshot_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
                connection.execute("INSERT OR IGNORE INTO immutable_input_snapshots(run_id,item_index,snapshot_sha256,snapshot_json) VALUES(?,?,?,?)", (run_id, int(item["item_index"]), snapshot_sha256, snapshot_json))
                connection.execute(
                    "INSERT INTO run_items(run_id,item_index,source_record_id,free_state,paid_required,paid_state,free_attempts,paid_attempts,last_error,payload_sha256,quarantine_state,quarantine_status,publication_blockers) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, int(item["item_index"]), str(item["source_record_id"]), str(item.get("free_state", "PENDING")), int(bool(item.get("paid_required"))), str(item.get("paid_state", "NOT_REQUIRED")), int(item.get("free_attempts", 0)), int(item.get("paid_attempts", 0)), str(item.get("last_error", "")), str(item.get("payload_sha256", "")), str(item.get("quarantine_state", "")), str(item.get("quarantine_status", "")), str(item.get("publication_blockers", ""))),
                )
            for result in results:
                connection.execute(
                    "INSERT INTO results(run_id,item_index,payload) VALUES(?,?,?)",
                    (run_id, int(result["item_index"]), str(result["payload"])),
                )
            for provider, limit in {provider: int(budgets.get(provider, 0)) for provider in CANONICAL_PROVIDERS}.items():
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
        budgets = {provider: int(budgets.get(provider, 0)) for provider in CANONICAL_PROVIDERS}
        context_json = json.dumps(_json_safe(context), ensure_ascii=False)
        budgets_json = json.dumps(_json_safe(budgets), ensure_ascii=False)
        existing_run = connection.execute("SELECT input_hash,run_signature,budgets_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if existing_run and (str(existing_run[0]) != str(input_hash) or str(existing_run[1]) != str(run_signature) or json.loads(existing_run[2] or "{}") != json.loads(budgets_json)):
            connection.rollback()
            raise ResumeInvariant("resume frozen run identity or provider limits changed")
        connection.execute(
            "INSERT OR IGNORE INTO runs(run_id,input_hash,run_signature,updated_at,phase,context_json,budgets_json,attempt_number,runtime_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, input_hash, run_signature, timestamp, str(context.get("phase", "FREE")), context_json, budgets_json, 1, json.dumps(_json_safe(runtime.snapshot()), ensure_ascii=False)),
        )
        connection.execute(
            "INSERT OR IGNORE INTO run_phase_transitions(run_id,ordinal,from_phase,to_phase,transitioned_at) VALUES(?,0,'',?,?)",
            (run_id, str(context.get("phase", "FREE")), timestamp),
        )
        for item in items:
            snapshot_json = json.dumps(_json_safe(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            snapshot_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            existing_snapshot = connection.execute("SELECT snapshot_sha256 FROM immutable_input_snapshots WHERE run_id=? AND item_index=?", (run_id, int(item["item_index"]))).fetchone()
            if existing_snapshot and str(existing_snapshot[0]) != snapshot_sha256:
                connection.rollback()
                raise SchedulerInvariantError("immutable input snapshot changed during resume")
            connection.execute("INSERT OR IGNORE INTO immutable_input_snapshots(run_id,item_index,snapshot_sha256,snapshot_json) VALUES(?,?,?,?)", (run_id, int(item["item_index"]), snapshot_sha256, snapshot_json))
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


def immutable_input_snapshot_sha256(run_id: str, item_index: int) -> str:
    with closing(_connect()) as connection:
        row = connection.execute("SELECT snapshot_sha256 FROM immutable_input_snapshots WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
    if not row:
        raise SchedulerInvariantError("immutable input snapshot is missing")
    return str(row[0])


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
            raise StateTransitionInvariant("run invariant failed: item/source count")
        if require_payloads and int(result_count) != expected_count:
            raise StateTransitionInvariant("run invariant failed: payload count")
        return {"items": int(item_count), "source_ids": int(source_count), "payloads": int(result_count)}


def transition_phase(run_id: str, new_phase: str, *, expected_count: int) -> None:
    if new_phase not in {"FREE", "PAID", "FINALIZING", "COMPLETE"}:
        raise ValueError(f"invalid phase: {new_phase}")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row or new_phase not in PHASE_TRANSITIONS.get(str(row[0]), set()):
            connection.rollback()
            raise StateTransitionInvariant(f"invalid phase transition to {new_phase}")
        item_count, source_count = connection.execute("SELECT COUNT(*),COUNT(DISTINCT source_record_id) FROM run_items WHERE run_id=?", (run_id,)).fetchone()
        if item_count != expected_count or source_count != expected_count:
            connection.rollback()
            raise StateTransitionInvariant("run invariant failed: item/source count")
        if new_phase in {"PAID", "FINALIZING", "COMPLETE"}:
            pending_free = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state IN ('PENDING','RUNNING')", (run_id,)).fetchone()[0]
            if pending_free:
                connection.rollback()
                raise StateTransitionInvariant("free work is not terminal")
        if new_phase in {"FINALIZING", "COMPLETE"}:
            pending_paid = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')", (run_id,)).fetchone()[0]
            if pending_paid:
                connection.rollback()
                raise StateTransitionInvariant("paid work is not terminal")
        if new_phase == "COMPLETE":
            pending = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET') OR (paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')))", (run_id,)).fetchone()[0]
            if pending:
                connection.rollback()
                raise StateTransitionInvariant("run has nonterminal scheduler items")
            intent = connection.execute(
                "SELECT status,artifact_set_sha256,manifest_sha256,memory_plan_sha256,memory_plan_count,memory_plan_committed FROM finalization_intent WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not intent or str(intent[0]) != "COMPLETE" or not str(intent[1]) or not str(intent[2]) or int(intent[5] or 0) != 1:
                connection.rollback()
                raise StateTransitionInvariant("COMPLETE requires a completed memory plan receipt")
            entries, plan_hash = _outbox_plan(connection, run_id)
            if len(entries) != int(intent[4]) or plan_hash != str(intent[3]):
                connection.rollback()
                raise StateTransitionInvariant("COMPLETE memory receipt mismatch")
        transitioned_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        ordinal = int(connection.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM run_phase_transitions WHERE run_id=?", (run_id,)).fetchone()[0])
        connection.execute("INSERT INTO run_phase_transitions(run_id,ordinal,from_phase,to_phase,transitioned_at) VALUES(?,?,?,?,?)", (run_id, ordinal, str(row[0]), new_phase, transitioned_at))
        connection.execute("UPDATE runs SET phase=?,updated_at=? WHERE run_id=?", (new_phase, transitioned_at, run_id))
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
            raise StateTransitionInvariant("finalization CAS requires FINALIZING phase")
        pending = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET') OR (paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')))", (run_id,)).fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=?", (run_id,)).fetchone()[0]
        if int(count) != int(expected_count) or pending:
            connection.rollback()
            raise StateTransitionInvariant("finalization CAS invariant failed")
        unresolved = connection.execute("SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')", (run_id,)).fetchone()[0]
        if unresolved:
            connection.rollback()
            raise StateTransitionInvariant("finalization CAS has unresolved provider calls")
        intent = connection.execute(
            "SELECT status,artifact_set_sha256,manifest_sha256,memory_plan_sha256,memory_plan_count,memory_plan_committed FROM finalization_intent WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if not intent or str(intent[0]) != "COMPLETE" or not str(intent[1]) or not str(intent[2]) or int(intent[5] or 0) != 1:
            connection.rollback()
            raise StateTransitionInvariant("finalization CAS requires a completed memory plan receipt")
        entries, plan_hash = _outbox_plan(connection, run_id)
        if len(entries) != int(intent[4]) or plan_hash != str(intent[3]):
            connection.rollback()
            raise StateTransitionInvariant("finalization CAS memory receipt mismatch")
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


def finalize_free_only(*, run_id: str, expected_count: int) -> dict[str, int]:
    """Finalize paid recommendations without creating paid work or evidence."""
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        phase_row = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not phase_row or str(phase_row[0]) != "FREE":
            connection.rollback()
            raise StateTransitionInvariant("free-only finalization requires FREE phase")

        items = connection.execute(
            "SELECT item_index,free_state,paid_required,paid_state,paid_attempts "
            "FROM run_items WHERE run_id=? ORDER BY item_index",
            (run_id,),
        ).fetchall()
        results = connection.execute(
            "SELECT item_index,payload FROM results WHERE run_id=? ORDER BY item_index",
            (run_id,),
        ).fetchall()
        if len(items) != int(expected_count) or len(results) != int(expected_count):
            connection.rollback()
            raise OutcomeInvariant("free-only finalization requires an exact terminal result snapshot")
        if any(str(row[1]) not in {"DONE", "FAILED", "NOT_REQUIRED"} for row in items):
            connection.rollback()
            raise OutcomeInvariant("free-only finalization requires terminal free items")

        counters = {
            "provider_calls": connection.execute("SELECT COUNT(*) FROM provider_calls WHERE run_id=?", (run_id,)).fetchone()[0],
            "paid_attempts": connection.execute("SELECT COUNT(*) FROM paid_attempts WHERE run_id=?", (run_id,)).fetchone()[0],
            "paid_attempt_calls": connection.execute("SELECT COUNT(*) FROM paid_attempt_calls WHERE run_id=?", (run_id,)).fetchone()[0],
            "provider_query_flights": connection.execute("SELECT COUNT(*) FROM provider_query_flights WHERE run_id=?", (run_id,)).fetchone()[0],
        }
        if any(int(value) for value in counters.values()):
            connection.rollback()
            raise OutcomeInvariant(f"free-only finalization found paid durable activity: {counters}")
        usage_rows = connection.execute(
            "SELECT provider,configured_limit,effective_limit,reserved,completed,failed,unknown "
            "FROM provider_usage WHERE run_id=?",
            (run_id,),
        ).fetchall()
        if any(any(int(value or 0) != 0 for value in row[1:]) for row in usage_rows):
            connection.rollback()
            raise OutcomeInvariant("free-only finalization found non-zero paid provider usage")

        item_by_index = {int(row[0]): row for row in items}
        seen: set[int] = set()
        recommended = 0
        for item_index, payload_text in results:
            item = item_by_index.get(int(item_index))
            if item is None:
                connection.rollback()
                raise OutcomeInvariant("free-only result has no scheduler item")
            seen.add(int(item_index))
            payload = json.loads(str(payload_text))
            was_recommended = bool(item[2]) or bool(payload.get("paid_recommended"))
            if was_recommended:
                recommended += 1
            payload["paid_recommended"] = was_recommended
            payload["paid_skipped_reason"] = (
                "disabled_by_explicit_free_only_finalization" if was_recommended
                else str(payload.get("paid_skipped_reason", ""))
            )
            payload["paid_required"] = False
            payload["paid_state"] = "NOT_REQUIRED"
            payload["paid_attempts"] = int(item[4])
            safe_payload = json.dumps(
                _json_safe(redaction.sanitize(payload)),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(safe_payload.encode("utf-8")).hexdigest()
            connection.execute(
                "UPDATE run_items SET paid_required=0,paid_state='NOT_REQUIRED',payload_sha256=? "
                "WHERE run_id=? AND item_index=?",
                (digest, run_id, int(item_index)),
            )
            connection.execute(
                "UPDATE results SET payload=? WHERE run_id=? AND item_index=?",
                (safe_payload, run_id, int(item_index)),
            )
        if seen != set(item_by_index):
            connection.rollback()
            raise OutcomeInvariant("free-only result/item index set mismatch")
        connection.commit()
    return {"items": int(expected_count), "paid_recommended": recommended, **counters}


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


def derive_provider_budgets(run_id: str) -> dict[str, dict[str, object]]:
    """Return durable provider usage without changing the legacy telemetry shape."""
    with closing(_connect()) as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=?", (run_id,)).fetchone()[0])
        free_completed = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state='DONE'", (run_id,)).fetchone()[0])
        item_terminal = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state IN ('DONE','FAILED','NOT_REQUIRED')", (run_id,)).fetchone()[0])
        paid_required = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_required=1", (run_id,)).fetchone()[0])
        paid_completed = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_required=1 AND paid_state IN ('DONE','FAILED','NOT_REQUIRED')", (run_id,)).fetchone()[0])
        free_failed = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state='FAILED'", (run_id,)).fetchone()[0])
        result_count = int(connection.execute("SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)).fetchone()[0])
        manifest_count = int(connection.execute("SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)).fetchone()[0])
        context_row = connection.execute(
            "SELECT context_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        context = json.loads(context_row[0] or "{}") if context_row else {}
        usage_rows = connection.execute(
            "SELECT provider,configured_limit,effective_limit,reserved,completed,failed "
            "FROM provider_usage WHERE run_id=? ORDER BY provider",
            (run_id,),
        ).fetchall()
    metadata = context.get("budget_details", {}) if isinstance(context, dict) else {}
    runtime_counters = runtime.snapshot().get("counters", {})
    provider_budgets = {}
    for provider, configured, effective, reserved, completed, failed in usage_rows:
        detail = dict(metadata.get(provider, {})) if isinstance(metadata, dict) else {}
        detail.update({
            "population_count": int(detail.get("population_count", total)),
            "ratio": detail.get("ratio"),
            "explicit_cap": detail.get("explicit_cap"),
            "effective_budget": int(effective),
            "reserved": int(reserved),
            "completed": int(completed),
            "blocked": int(runtime_counters.get(f"api.{provider}.budget_blocked", 0)),
        })
        provider_budgets[str(provider)] = detail
    return provider_budgets


def canonical_scheduler_receipt_from_connection(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Derive the stable scheduler receipt from one caller-owned SQLite connection."""
    if connection is not None:
        total = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=?", (run_id,)).fetchone()[0])
        free_completed = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state='DONE'", (run_id,)).fetchone()[0])
        item_terminal = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state IN ('DONE','FAILED','NOT_REQUIRED')", (run_id,)).fetchone()[0])
        paid_required = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_required=1", (run_id,)).fetchone()[0])
        paid_completed = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_required=1 AND paid_state IN ('DONE','FAILED','NOT_REQUIRED')", (run_id,)).fetchone()[0])
        free_failed = int(connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=? AND free_state='FAILED'", (run_id,)).fetchone()[0])
        result_count = int(connection.execute("SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)).fetchone()[0])
        manifest_count = int(connection.execute("SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)).fetchone()[0])
        free_rows = connection.execute(
            "SELECT COALESCE(SUM(logical_used),0),COALESCE(SUM(logical_blocked),0),COALESCE(SUM(discovery_logical_used),0),COALESCE(SUM(targeted_logical_used),0) FROM free_query_usage WHERE run_id=?",
            (run_id,),
        ).fetchone()
        free_attempts = {str(state): int(count) for state, count in connection.execute("SELECT state,COUNT(*) FROM free_provider_attempts WHERE run_id=? GROUP BY state", (run_id,)).fetchall()}
        free_bucket_states = {(str(bucket), str(state)): int(count) for bucket, state, count in connection.execute("SELECT bucket,state,COUNT(*) FROM free_provider_attempts WHERE run_id=? GROUP BY bucket,state", (run_id,)).fetchall()}
        provider_rows = connection.execute(
            "SELECT provider,configured_limit,effective_limit,reserved,completed,failed,reserved_total,unknown FROM provider_usage WHERE run_id=? ORDER BY provider",
            (run_id,),
        ).fetchall()
        context_row = connection.execute("SELECT context_json,budgets_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        frozen_context = json.loads(context_row[0] or "{}") if context_row else {}
        frozen_budgets = json.loads(context_row[1] or "{}") if context_row else {}
        call_states = {}
        for provider, state, count in connection.execute(
            "SELECT provider,state,COUNT(*) FROM provider_calls WHERE run_id=? GROUP BY provider,state",
            (run_id,),
        ).fetchall():
            if type(provider) is not str or type(state) is not str or state not in {"RESERVED", "RUNNING", "DONE", "FAILED", "UNKNOWN"}:
                raise LedgerInvariant("canonical provider call state is invalid")
            call_states[(provider, state)] = int(count)
        http_counts = {str(provider): int(count) for provider, count in connection.execute("SELECT provider,COUNT(*) FROM provider_calls WHERE run_id=? AND http_started_at<>'' GROUP BY provider", (run_id,)).fetchall()}
        retry_counts = {str(provider): int(count) for provider, count in connection.execute("SELECT provider,COUNT(*) FROM provider_calls WHERE run_id=? AND http_started_at<>'' AND attempt_ordinal>1 GROUP BY provider", (run_id,)).fetchall()}
        inherited_counts = {str(provider): int(count) for provider, count in connection.execute("SELECT provider,COUNT(*) FROM provider_query_flight_consumers WHERE run_id=? AND relation='INHERITED' GROUP BY provider", (run_id,)).fetchall()}
        blocked_counts = {str(provider): int(count) for provider, count in connection.execute("SELECT provider,COUNT(*) FROM provider_budget_blocks WHERE run_id=? AND provider<>'ddgs' GROUP BY provider", (run_id,)).fetchall()}
        free_blocks = {(str(bucket), str(kind)): int(count) for bucket, kind, count in connection.execute("SELECT bucket,block_kind,COUNT(*) FROM provider_budget_blocks WHERE run_id=? AND provider='ddgs' GROUP BY bucket,block_kind", (run_id,)).fetchall()}
        plan_rows = connection.execute("SELECT item_index,plan_version,query_kind,round_ordinal,query_ordinal,normalized_query,query_sha256 FROM paid_query_plan_entries WHERE run_id=? ORDER BY item_index,plan_version,query_kind,round_ordinal,query_ordinal", (run_id,)).fetchall()
        if type(free_rows) is not tuple or any(type(value) is not int or value < 0 for value in free_rows):
            raise LedgerInvariant("canonical free query counters are invalid")
        if type(frozen_context) is not dict or type(frozen_budgets) is not dict:
            raise LedgerInvariant("canonical run context is invalid")
    if (
        type(frozen_budgets) is not dict
        or len(provider_rows) != len(CANONICAL_PROVIDERS)
        or set(frozen_budgets) != CANONICAL_PROVIDERS
        or {str(row[0]) for row in provider_rows} != CANONICAL_PROVIDERS
        or not {provider for provider, _state in call_states}.issubset(CANONICAL_PROVIDERS)
    ):
        raise LedgerInvariant("canonical scheduler receipt requires exactly six providers")
    provider_budgets = {}
    budget_metadata = frozen_context.get("budget_details", {})
    if type(budget_metadata) is not dict:
        raise LedgerInvariant("canonical budget metadata is invalid")
    for provider, configured, effective, reserved_aggregate, completed_aggregate, failed_aggregate, reserved_total_aggregate, unknown_aggregate in provider_rows:
        reserved_state = call_states.get((provider, "RESERVED"), 0)
        running_state = call_states.get((provider, "RUNNING"), 0)
        unknown = call_states.get((provider, "UNKNOWN"), 0)
        done_state = call_states.get((provider, "DONE"), 0)
        failed_state = call_states.get((provider, "FAILED"), 0)
        values = (configured, effective, reserved_aggregate, completed_aggregate, failed_aggregate, reserved_total_aggregate, unknown_aggregate)
        frozen_limit = frozen_budgets.get(provider)
        if (
            any(type(value) is not int or value < 0 for value in values)
            or type(frozen_limit) is not int
            or frozen_limit < 0
            or configured != frozen_limit
            or effective != frozen_limit
            or effective > configured
        ):
            raise LedgerInvariant(f"provider frozen budget mismatch: {provider}")
        expected_aggregate = {
            "reserved_total": reserved_state + running_state + done_state + failed_state + unknown,
            "reserved": reserved_state + running_state, "completed": done_state, "failed": failed_state, "unknown": unknown,
        }
        actual_aggregate = {"reserved_total": int(reserved_total_aggregate), "reserved": int(reserved_aggregate), "completed": int(completed_aggregate), "failed": int(failed_aggregate), "unknown": int(unknown_aggregate)}
        if actual_aggregate != expected_aggregate or int(reserved_total_aggregate) > int(effective):
            raise LedgerInvariant(f"provider aggregate ledger mismatch: {provider}")
        detail = budget_metadata.get(provider, {})
        if type(detail) is not dict:
            raise LedgerInvariant(f"canonical budget metadata is invalid: {provider}")
        population_count = detail.get("population_count", total)
        ratio = detail.get("ratio")
        explicit_cap = detail.get("explicit_cap", int(configured))
        ratio_numerator = detail.get("ratio_numerator")
        ratio_denominator = detail.get("ratio_denominator")
        if (
            type(population_count) is not int or population_count < 0
            or (ratio is not None and (type(ratio) not in {int, float} or isinstance(ratio, bool) or ratio < 0 or type(ratio) is float and not math.isfinite(ratio)))
            or (explicit_cap is not None and (type(explicit_cap) is not int or explicit_cap < 0))
            or (ratio_numerator is not None and (type(ratio_numerator) is not int or ratio_numerator < 0))
            or (ratio_denominator is not None and (type(ratio_denominator) is not int or ratio_denominator < 0))
        ):
            raise LedgerInvariant(f"canonical budget metadata is invalid: {provider}")
        provider_budgets[str(provider)] = {
            "population_count": population_count,
            "ratio": ratio, "ratio_numerator": ratio_numerator, "ratio_denominator": ratio_denominator,
            "explicit_cap": explicit_cap,
            "configured_limit": int(configured), "effective_limit": int(effective),
            "reserved_total": int(reserved_total_aggregate), "done": int(completed_aggregate), "failed": int(failed_aggregate),
            "unknown": int(unknown_aggregate), "reserved": reserved_state, "running": running_state,
            "physical_http_attempts": http_counts.get(str(provider), 0), "retry_attempts": retry_counts.get(str(provider), 0),
            "inherited_uses": inherited_counts.get(str(provider), 0), "budget_blocked_items": blocked_counts.get(str(provider), 0),
        }
    paid_query_limit = frozen_context.get("paid_query_limit_per_company", 0)
    if type(paid_query_limit) is not int or paid_query_limit < 0:
        raise LedgerInvariant("canonical paid query limit is invalid")
    for plan_row in plan_rows:
        if (
            len(plan_row) != 7
            or any(type(value) is not int or value < 0 for value in plan_row[:2] + plan_row[3:5])
            or type(plan_row[2]) is not str
            or type(plan_row[5]) is not str
            or type(plan_row[6]) is not str
            or not _is_sha256(plan_row[6])
        ):
            raise LedgerInvariant("canonical paid query plan is invalid")
    plan_material = json.dumps([list(row) for row in plan_rows], ensure_ascii=False, separators=(",", ":"))
    return {
        "receipt_schema_version": 1,
        "total_items": total, "free_completed": free_completed,
        "free_failed": free_failed, "item_terminal": item_terminal,
        "paid_required": paid_required, "paid_completed": paid_completed,
        "result_count": result_count, "manifest_count": manifest_count,
        "free_queries": {
            "logical_used": int(free_rows[0]), "logical_accepted": int(free_rows[0]), "logical_blocked": int(free_rows[1]),
            "discovery_logical_accepted": int(free_rows[2]), "targeted_logical_accepted": int(free_rows[3]),
            "physical_attempted": sum(free_attempts.values()),
            "physical_done": free_attempts.get("DONE", 0), "physical_failed": free_attempts.get("FAILED", 0), "physical_reserved": free_attempts.get("RESERVED", 0),
            "physical_blocked": sum(value for (bucket, kind), value in free_blocks.items() if kind == "physical"),
            "discovery_physical_attempted": sum(free_bucket_states.get(("discovery", state), 0) for state in ("DONE", "FAILED", "RESERVED")), "targeted_physical_attempted": sum(free_bucket_states.get(("targeted", state), 0) for state in ("DONE", "FAILED", "RESERVED")),
            "buckets": {
                bucket: {
                    "logical_accepted": int(free_rows[2 if bucket == "discovery" else 3]),
                    "physical_attempted": sum(free_bucket_states.get((bucket, state), 0) for state in ("DONE", "FAILED", "RESERVED")),
                    "done": free_bucket_states.get((bucket, "DONE"), 0), "failed": free_bucket_states.get((bucket, "FAILED"), 0), "reserved": free_bucket_states.get((bucket, "RESERVED"), 0),
                    "unique_logical_blocks": free_blocks.get((bucket, "logical"), 0), "unique_physical_blocks": free_blocks.get((bucket, "physical"), 0),
                } for bucket in ("discovery", "targeted")
            },
        },
        "provider_budgets": provider_budgets,
        "paid_query_limit_per_company": paid_query_limit,
        "paid_query_plan": {
            "plan_version": 1,
            "paid_query_plan_count": len(plan_rows),
            "paid_query_plan_sha256": hashlib.sha256(plan_material.encode("utf-8")).hexdigest(),
        },
    }


def canonical_scheduler_receipt(run_id: str) -> dict[str, Any]:
    """Derive the stable scheduler receipt solely from durable scheduler rows."""
    with closing(_connect()) as connection:
        return canonical_scheduler_receipt_from_connection(connection, run_id)


def canonical_scheduler_receipt_json(run_id: str) -> str:
    return json.dumps(canonical_scheduler_receipt(run_id), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def derive_telemetry(run_id: str) -> dict[str, Any]:
    return canonical_scheduler_receipt(run_id)


def initialize_run_items(run_id: str, items: list[dict[str, Any]]) -> None:
    normalized = [dict(item, item_index=index, free_state="DONE", paid_required=True, paid_state="RUNNING") for index, item in enumerate(items)]
    initialize_run(run_id=run_id, input_hash="test", run_signature="test", context={"phase": "PAID"}, budgets={"brightdata": 20}, items=normalized)


def ensure_provider_budget(run_id: str, provider: str, *, configured_limit: int, effective_limit: int) -> None:
    with closing(_connect()) as connection:
        existing = connection.execute("SELECT configured_limit,effective_limit FROM provider_usage WHERE run_id=? AND provider=?", (run_id, provider)).fetchone()
        if existing and (int(existing[0]) != int(configured_limit) or int(existing[1]) != int(effective_limit)):
            raise ResumeInvariant(f"provider budget changed during run: {provider}")
        connection.execute("INSERT OR IGNORE INTO provider_usage(run_id,provider,configured_limit,effective_limit) VALUES(?,?,?,?)", (run_id, provider, int(configured_limit), int(effective_limit)))
        connection.commit()


def freeze_paid_query_plan(*, run_id: str, item_index: int, queries: list[str] | tuple[str, ...], query_kind: str = "primary", round_ordinal: int = 0, plan_version: int = 1) -> list[str]:
    normalized = [" ".join(str(query).split()) for query in queries if " ".join(str(query).split())]
    rows = [(ordinal, query, hashlib.sha256(query.encode("utf-8")).hexdigest()) for ordinal, query in enumerate(normalized)]
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT query_ordinal,normalized_query,query_sha256 FROM paid_query_plan_entries WHERE run_id=? AND item_index=? AND plan_version=? AND query_kind=? AND round_ordinal=? ORDER BY query_ordinal", (run_id, int(item_index), int(plan_version), str(query_kind), int(round_ordinal))).fetchall()
        if existing and existing != rows:
            connection.rollback()
            raise ResumeInvariant("durable paid query plan drift")
        if not existing:
            connection.executemany("INSERT INTO paid_query_plan_entries(run_id,item_index,plan_version,query_kind,round_ordinal,query_ordinal,normalized_query,query_sha256) VALUES(?,?,?,?,?,?,?,?)", [(run_id, int(item_index), int(plan_version), str(query_kind), int(round_ordinal), ordinal, query, digest) for ordinal, query, digest in rows])
        connection.commit()
    return normalized


def load_paid_query_plan(run_id: str, item_index: int, *, query_kind: str = "primary", round_ordinal: int = 0, plan_version: int = 1) -> list[str]:
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT normalized_query,query_sha256 FROM paid_query_plan_entries WHERE run_id=? AND item_index=? AND plan_version=? AND query_kind=? AND round_ordinal=? ORDER BY query_ordinal", (run_id, int(item_index), int(plan_version), str(query_kind), int(round_ordinal))).fetchall()
    for query, digest in rows:
        if hashlib.sha256(str(query).encode("utf-8")).hexdigest() != str(digest):
            raise ResumeInvariant("durable paid query plan hash mismatch")
    return [str(query) for query, _digest in rows]


def paid_query_plan_receipt(run_id: str) -> dict[str, Any]:
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT item_index,plan_version,query_kind,round_ordinal,query_ordinal,normalized_query,query_sha256 FROM paid_query_plan_entries WHERE run_id=? ORDER BY item_index,plan_version,query_kind,round_ordinal,query_ordinal", (run_id,)).fetchall()
    material = json.dumps([list(row) for row in rows], ensure_ascii=False, separators=(",", ":"))
    return {"plan_version": 1, "paid_query_plan_count": len(rows), "paid_query_plan_sha256": hashlib.sha256(material.encode()).hexdigest()}


def claim_provider_query_flight(*, run_id: str, provider: str, query_fingerprint: str, owner_token: str, lease_seconds: float = 30) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=max(0.1, float(lease_seconds)))).isoformat(timespec="microseconds")
    timestamp = now.isoformat(timespec="microseconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO provider_query_flights(run_id,provider,query_fingerprint,state,owner_token,lease_expires_at,heartbeat_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, provider, query_fingerprint, "RUNNING", owner_token, expires, timestamp, timestamp, timestamp),
        )
        row = connection.execute(
            "SELECT state,owner_token,lease_expires_at,result_json,call_ids_json,provider_call_id,heartbeat_at,execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=?",
            (run_id, provider, query_fingerprint),
        ).fetchone()
        leader = cursor.rowcount == 1
        connection.commit()
    return {"leader": leader, "state": str(row[0]), "owner_token": str(row[1]), "lease_expires_at": str(row[2]), "result": json.loads(row[3] or "{}"), "call_ids": json.loads(row[4] or "[]"), "provider_call_id": str(row[5] or ""), "heartbeat_at": str(row[6] or ""), "execution_generation": int(row[7]), "potentially_charged": False}


def start_new_provider_query_execution(*, run_id: str, provider: str, query_fingerprint: str, owner_token: str, lease_seconds: float = 30) -> dict[str, Any]:
    """Explicitly start the next generation of a terminal, reconciled query flight."""
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat(timespec="microseconds")
    expires = (now + timedelta(seconds=max(0.1, float(lease_seconds)))).isoformat(timespec="microseconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT f.state,f.execution_generation,f.call_ids_json,f.result_json,t.state,t.provider_call_id,t.result_sha256,t.call_ids_json,r.provider_call_id,r.result_json,r.result_sha256,pc.state FROM provider_query_flights f LEFT JOIN provider_query_flight_terminals t ON t.run_id=f.run_id AND t.provider=f.provider AND t.query_fingerprint=f.query_fingerprint AND t.execution_generation=f.execution_generation LEFT JOIN provider_query_flight_results r ON r.run_id=f.run_id AND r.provider=f.provider AND r.query_fingerprint=f.query_fingerprint AND r.execution_generation=f.execution_generation LEFT JOIN provider_calls pc ON pc.run_id=f.run_id AND pc.provider=f.provider AND pc.call_id=t.provider_call_id WHERE f.run_id=? AND f.provider=? AND f.query_fingerprint=?",
            (run_id, provider, query_fingerprint),
        ).fetchone()
        try:
            current_call_ids = json.loads(str(row[2] or "[]")) if row else None
            terminal_call_ids = json.loads(str(row[7] or "[]")) if row else None
        except (TypeError, ValueError):
            current_call_ids = terminal_call_ids = None
        terminal_valid = bool(
            row and str(row[0]) in {"DONE", "FAILED"} and str(row[4]) == str(row[0])
            and isinstance(current_call_ids, list) and isinstance(terminal_call_ids, list)
            and all(isinstance(call_id, str) and call_id for call_id in current_call_ids)
            and len(current_call_ids) == len(set(current_call_ids)) and terminal_call_ids == current_call_ids
            and hashlib.sha256(str(row[3] or "{}").encode()).hexdigest() == str(row[6] or "")
        )
        if terminal_valid and str(row[0]) == "DONE":
            terminal_valid = bool(
                row[8] and str(row[5]) == str(row[8]) and str(row[9]) == str(row[3])
                and str(row[10]) == str(row[6]) and hashlib.sha256(str(row[9]).encode()).hexdigest() == str(row[10])
                and str(row[11]) == "DONE"
            )
        elif terminal_valid:
            if current_call_ids:
                placeholders = ",".join("?" for _ in current_call_ids)
                failed_rows = connection.execute(
                    "SELECT c.call_id,c.state,l.query_fingerprint,l.execution_generation,l.relation "
                    "FROM provider_calls c LEFT JOIN paid_attempt_calls l ON l.run_id=c.run_id AND l.provider=c.provider "
                    "AND l.provider_call_id=c.call_id AND l.relation='OWNER' "
                    f"WHERE c.run_id=? AND c.provider=? AND c.call_id IN ({placeholders})",
                    (run_id, provider, *current_call_ids),
                ).fetchall()
                terminal_valid = len(failed_rows) == len(current_call_ids) and all(
                    str(call_state) == "FAILED" and str(fingerprint) == query_fingerprint
                    and int(generation) == int(row[1]) and str(relation) == "OWNER"
                    for _call_id, call_state, fingerprint, generation, relation in failed_rows
                )
        if not terminal_valid:
            connection.rollback()
            raise StateTransitionInvariant("new provider query execution requires a reconciled terminal flight")
        generation = int(row[1]) + 1
        # Request fingerprints describe the physical envelope and therefore stay
        # stable across explicitly authorized generations.  Generation-scoped
        # duplicate checks below replace the legacy cross-generation unique index.
        connection.execute("DROP INDEX IF EXISTS uq_provider_call_identity")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_provider_call_identity ON provider_calls(run_id,provider,item_index,phase,request_fingerprint)")
        cursor = connection.execute("UPDATE provider_query_flights SET state='RUNNING',owner_token=?,lease_expires_at=?,heartbeat_at=?,provider_call_id='',result_json='{}',call_ids_json='[]',created_at=?,updated_at=?,execution_generation=? WHERE run_id=? AND provider=? AND query_fingerprint=? AND execution_generation=?", (owner_token, expires, timestamp, timestamp, timestamp, generation, run_id, provider, query_fingerprint, int(row[1])))
        if cursor.rowcount != 1:
            connection.rollback()
            raise StateTransitionInvariant("provider query generation compare-and-swap failed")
        connection.commit()
    return {"leader": True, "state": "RUNNING", "owner_token": owner_token, "lease_expires_at": expires, "result": {}, "call_ids": [], "provider_call_id": "", "heartbeat_at": timestamp, "execution_generation": generation, "potentially_charged": False}


def resolve_expired_provider_query_flight(*, run_id: str, provider: str, query_fingerprint: str, owner_token: str, lease_seconds: float = 30, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    timestamp = current.isoformat(timespec="microseconds")
    expires = (current + timedelta(seconds=max(0.1, float(lease_seconds)))).isoformat(timespec="microseconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT state,owner_token,lease_expires_at,result_json,call_ids_json,execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=?", (run_id, provider, query_fingerprint)).fetchone()
        if not row:
            connection.rollback()
            raise StateTransitionInvariant("provider query flight disappeared")
        state = str(row[0])
        call_ids = list(json.loads(row[4] or "[]"))
        result = json.loads(row[3] or "{}")
        if state in {"DONE", "FAILED", "UNKNOWN"}:
            connection.commit()
            return {"leader": False, "state": state, "result": result, "call_ids": call_ids, "execution_generation": int(row[5])}
        if str(row[2]) > timestamp:
            connection.commit()
            return {"leader": False, "state": "RUNNING", "result": result, "call_ids": call_ids, "execution_generation": int(row[5])}
        states = [str(value[0]) for call_id in call_ids for value in [connection.execute("SELECT state FROM provider_calls WHERE run_id=? AND provider=? AND call_id=?", (run_id, provider, call_id)).fetchone()] if value]
        receipt = connection.execute("SELECT provider_call_id,result_json,result_sha256 FROM provider_query_flight_results WHERE run_id=? AND provider=? AND query_fingerprint=? AND execution_generation=?", (run_id, provider, query_fingerprint, int(row[5]))).fetchone()
        if receipt:
            receipt_call = connection.execute("SELECT state FROM provider_calls WHERE run_id=? AND provider=? AND call_id=?", (run_id, provider, str(receipt[0]))).fetchone()
            receipt_json = str(receipt[1])
            if receipt_call and str(receipt_call[0]) == "DONE" and hashlib.sha256(receipt_json.encode()).hexdigest() == str(receipt[2]):
                result = json.loads(receipt_json)
                connection.execute("UPDATE provider_query_flights SET state='DONE',result_json=?,updated_at=? WHERE run_id=? AND provider=? AND query_fingerprint=? AND state='RUNNING'", (receipt_json, timestamp, run_id, provider, query_fingerprint))
                connection.execute("INSERT OR IGNORE INTO provider_query_flight_terminals(run_id,provider,query_fingerprint,execution_generation,state,provider_call_id,result_sha256,call_ids_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, provider, query_fingerprint, int(row[5]), "DONE", str(receipt[0]), str(receipt[2]), json.dumps(call_ids), timestamp))
                connection.commit()
                return {"leader": False, "state": "DONE", "result": result, "call_ids": call_ids, "execution_generation": int(row[5])}
        if not call_ids or (len(states) == len(call_ids) and all(value == "FAILED" for value in states)):
            cursor = connection.execute("UPDATE provider_query_flights SET owner_token=?,lease_expires_at=?,heartbeat_at=?,provider_call_id='',updated_at=? WHERE run_id=? AND provider=? AND query_fingerprint=? AND state='RUNNING' AND owner_token=? AND lease_expires_at<=?", (owner_token, expires, timestamp, timestamp, run_id, provider, query_fingerprint, str(row[1]), timestamp))
            connection.commit()
            return {"leader": cursor.rowcount == 1, "state": "RUNNING", "result": result, "call_ids": call_ids, "execution_generation": int(row[5])}
        if len(states) != len(call_ids):
            connection.rollback()
            raise EvidenceInvariant("expired flight call history references missing provider calls")
        if any(value in {"RESERVED", "RUNNING", "UNKNOWN", "DONE"} for value in states):
            unsettled = sum(value in {"RESERVED", "RUNNING"} for value in states)
            if unsettled:
                placeholders = ",".join("?" for _ in call_ids)
                connection.execute(
                    f"UPDATE provider_calls SET state='UNKNOWN',updated_at=? WHERE run_id=? AND provider=? AND call_id IN ({placeholders}) AND state IN ('RESERVED','RUNNING')",
                    (timestamp, run_id, provider, *call_ids),
                )
                usage_cursor = connection.execute(
                    "UPDATE provider_usage SET reserved=reserved-?,unknown=unknown+? WHERE run_id=? AND provider=? AND reserved>=?",
                    (unsettled, unsettled, run_id, provider, unsettled),
                )
                if usage_cursor.rowcount != 1:
                    connection.rollback()
                    raise LedgerInvariant("expired flight could not terminalize unsettled provider calls")
            connection.execute("UPDATE provider_query_flights SET state='UNKNOWN',provider_call_id='',updated_at=? WHERE run_id=? AND provider=? AND query_fingerprint=? AND state='RUNNING'", (timestamp, run_id, provider, query_fingerprint))
            result_json = json.dumps(_json_safe(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "INSERT INTO provider_query_flight_terminals(run_id,provider,query_fingerprint,execution_generation,state,provider_call_id,result_sha256,call_ids_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, provider, query_fingerprint, int(row[5]), "UNKNOWN", "", hashlib.sha256(result_json.encode()).hexdigest(), json.dumps(call_ids), timestamp),
            )
            connection.commit()
            return {"leader": False, "state": "UNKNOWN", "result": {}, "call_ids": call_ids, "execution_generation": int(row[5]), "reason": "expired_potentially_charged_flight"}
        connection.rollback()
        raise StateTransitionInvariant("expired flight cannot be reconciled")


def bind_provider_query_flight_call(*, run_id: str, provider: str, query_fingerprint: str, owner_token: str, provider_call_id: str) -> None:
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        valid = connection.execute("SELECT 1 FROM provider_calls WHERE run_id=? AND provider=? AND call_id=? AND state IN ('RESERVED','RUNNING')", (run_id, provider, provider_call_id)).fetchone()
        flight = connection.execute("SELECT provider_call_id,call_ids_json FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=? AND owner_token=? AND state='RUNNING'", (run_id, provider, query_fingerprint, owner_token)).fetchone()
        prior_terminal = True
        if flight and str(flight[0] or ""):
            prior = connection.execute("SELECT state FROM provider_calls WHERE run_id=? AND call_id=?", (run_id, str(flight[0]))).fetchone()
            prior_terminal = bool(prior and str(prior[0]) == "FAILED")
        call_ids = list(json.loads(flight[1] or "[]")) if flight else []
        if provider_call_id not in call_ids:
            call_ids.append(provider_call_id)
        cursor = connection.execute(
            "UPDATE provider_query_flights SET provider_call_id=?,call_ids_json=?,updated_at=? WHERE run_id=? AND provider=? AND query_fingerprint=? AND owner_token=? AND state='RUNNING'",
            (provider_call_id, json.dumps(call_ids), datetime.now(timezone.utc).isoformat(timespec="microseconds"), run_id, provider, query_fingerprint, owner_token),
        ) if valid and flight and prior_terminal else None
        if cursor is None or cursor.rowcount != 1:
            connection.rollback()
            raise StateTransitionInvariant("singleflight provider call binding failed")
        generation = connection.execute("SELECT execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=?", (run_id, provider, query_fingerprint)).fetchone()
        connection.execute(
            "UPDATE provider_calls SET flight_fingerprint=? WHERE run_id=? AND provider=? AND call_id=? AND flight_fingerprint IN ('',?)",
            (query_fingerprint, run_id, provider, provider_call_id, query_fingerprint),
        )
        connection.execute("UPDATE paid_attempt_calls SET query_fingerprint=?,execution_generation=? WHERE run_id=? AND provider=? AND provider_call_id=?", (query_fingerprint, int(generation[0]), run_id, provider, provider_call_id))
        connection.commit()


def heartbeat_provider_query_flight(*, run_id: str, provider: str, query_fingerprint: str, owner_token: str, lease_seconds: float = 30, now_fn=lambda: datetime.now(timezone.utc)) -> str:
    now = now_fn()
    expires = (now + timedelta(seconds=max(0.1, float(lease_seconds)))).isoformat(timespec="microseconds")
    with closing(_connect()) as connection:
        cursor = connection.execute(
            "UPDATE provider_query_flights SET heartbeat_at=?,lease_expires_at=?,updated_at=? WHERE run_id=? AND provider=? AND query_fingerprint=? AND owner_token=? AND state='RUNNING'",
            (now.isoformat(timespec="microseconds"), expires, now.isoformat(timespec="microseconds"), run_id, provider, query_fingerprint, owner_token),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise StateTransitionInvariant("singleflight owner lost during heartbeat")
        connection.commit()
    return expires


def finish_provider_query_flight(*, run_id: str, provider: str, query_fingerprint: str, owner_token: str, state: str, result: dict[str, Any], call_ids: list[str]) -> None:
    state = str(state).upper()
    if state == "DONE":
        raise StateTransitionInvariant("DONE flight completion requires atomic provider-call success")
    if state not in {"FAILED", "UNKNOWN"}:
        raise StateTransitionInvariant(f"invalid flight state: {state}")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT call_ids_json,execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=? AND owner_token=? AND state='RUNNING'", (run_id, provider, query_fingerprint, owner_token)).fetchone()
        if not row:
            connection.rollback()
            raise StateTransitionInvariant("provider query flight owner lost")
        existing_ids = list(json.loads(row[0] or "[]"))
        supplied_ids = list(dict.fromkeys(call_ids))
        if supplied_ids != existing_ids:
            connection.rollback()
            raise EvidenceInvariant("provider query flight call history must exactly match bound append-only calls")
        call_states: list[str] = []
        if existing_ids:
            placeholders = ",".join("?" for _ in existing_ids)
            rows = connection.execute(
                "SELECT c.call_id,c.state,l.query_fingerprint,l.execution_generation,l.relation "
                "FROM provider_calls c LEFT JOIN paid_attempt_calls l ON l.run_id=c.run_id AND l.provider=c.provider "
                "AND l.provider_call_id=c.call_id AND l.relation='OWNER' "
                f"WHERE c.run_id=? AND c.provider=? AND c.call_id IN ({placeholders})",
                (run_id, provider, *existing_ids),
            ).fetchall()
            evidence = {str(call_id): (str(call_state), str(fingerprint), int(generation), str(relation)) for call_id, call_state, fingerprint, generation, relation in rows}
            if len(rows) != len(existing_ids) or set(evidence) != set(existing_ids) or any(
                fingerprint != query_fingerprint or generation != int(row[1]) or relation != "OWNER"
                for _call_state, fingerprint, generation, relation in evidence.values()
            ):
                connection.rollback()
                raise EvidenceInvariant("provider query flight terminal references an unbound generation call")
            call_states = [evidence[call_id][0] for call_id in existing_ids]
        states_valid = (
            (state == "FAILED" and all(call_state == "FAILED" for call_state in call_states))
            or (state == "UNKNOWN" and (not call_states or ("UNKNOWN" in call_states and all(call_state in {"FAILED", "UNKNOWN"} for call_state in call_states))))
        )
        if not states_valid:
            connection.rollback()
            raise StateTransitionInvariant(f"{state} flight completion conflicts with provider call states")
        merged_ids = existing_ids
        result_json = json.dumps(_json_safe(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cursor = connection.execute(
            "UPDATE provider_query_flights SET state=?,provider_call_id='',result_json=?,call_ids_json=?,updated_at=? WHERE run_id=? AND provider=? AND query_fingerprint=? AND owner_token=? AND state='RUNNING'",
            (state, result_json, json.dumps(merged_ids), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id, provider, query_fingerprint, owner_token),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise StateTransitionInvariant("provider query flight owner lost")
        connection.execute("INSERT INTO provider_query_flight_terminals(run_id,provider,query_fingerprint,execution_generation,state,provider_call_id,result_sha256,call_ids_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, provider, query_fingerprint, int(row[1]), state, "", hashlib.sha256(result_json.encode()).hexdigest(), json.dumps(merged_ids), datetime.now(timezone.utc).isoformat(timespec="microseconds")))
        connection.commit()


def complete_provider_call_and_flight_success(*, run_id: str, provider: str, query_fingerprint: str,
                                              owner_token: str, provider_call_id: str,
                                              result: dict[str, Any], call_ids: list[str]) -> None:
    """Atomically persist semantic DONE, success receipt, append-only calls, and flight DONE."""
    result_json = json.dumps(_json_safe(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        call = connection.execute(
            "SELECT c.state,c.flight_fingerprint,c.http_started_at,l.query_fingerprint,l.execution_generation,l.relation,c.endpoint_sha256,c.request_shape_sha256 "
            "FROM provider_calls c JOIN paid_attempt_calls l ON l.run_id=c.run_id AND l.provider=c.provider AND l.provider_call_id=c.call_id "
            "WHERE c.run_id=? AND c.provider=? AND c.call_id=? AND l.relation='OWNER'",
            (run_id, provider, provider_call_id),
        ).fetchone()
        flight = connection.execute(
            "SELECT call_ids_json,execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=? AND owner_token=? AND state='RUNNING'",
            (run_id, provider, query_fingerprint, owner_token),
        ).fetchone()
        existing_ids = list(json.loads(str(flight[0] or "[]"))) if flight else []
        call_matches_flight = bool(
            call and flight and str(call[0]) == "RUNNING" and str(call[1]) == query_fingerprint
            and bool(str(call[2] or "")) and str(call[3]) == query_fingerprint
            and int(call[4]) == int(flight[1]) and str(call[5]) == "OWNER"
            and _is_sha256(call[6]) and _is_sha256(call[7])
            and provider_call_id in existing_ids
        )
        if not call_matches_flight:
            connection.rollback()
            raise StateTransitionInvariant("atomic flight success requires a bound current-generation HTTP call and live owner")
        supplied_ids = list(dict.fromkeys(call_ids))
        if supplied_ids != existing_ids:
            connection.rollback()
            raise EvidenceInvariant("provider query flight call history must exactly match bound append-only calls")
        merged_ids = existing_ids
        usage = connection.execute("SELECT reserved FROM provider_usage WHERE run_id=? AND provider=?", (run_id, provider)).fetchone()
        if not usage or int(usage[0]) <= 0:
            connection.rollback()
            raise LedgerInvariant("provider aggregate reserved counter is invalid at atomic success")
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        connection.execute("UPDATE provider_calls SET state='DONE',result_ref=?,updated_at=? WHERE run_id=? AND provider=? AND call_id=?", (hashlib.sha256(result_json.encode()).hexdigest(), now, run_id, provider, provider_call_id))
        connection.execute("UPDATE provider_usage SET reserved=reserved-1,completed=completed+1 WHERE run_id=? AND provider=?", (run_id, provider))
        connection.execute("INSERT INTO provider_query_flight_results(run_id,provider,query_fingerprint,execution_generation,provider_call_id,result_json,result_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)", (run_id, provider, query_fingerprint, int(flight[1]), provider_call_id, result_json, hashlib.sha256(result_json.encode()).hexdigest(), now))
        cursor = connection.execute("UPDATE provider_query_flights SET state='DONE',provider_call_id=?,result_json=?,call_ids_json=?,updated_at=? WHERE run_id=? AND provider=? AND query_fingerprint=? AND owner_token=? AND state='RUNNING'", (provider_call_id, result_json, json.dumps(merged_ids), now, run_id, provider, query_fingerprint, owner_token))
        if cursor.rowcount != 1:
            connection.rollback()
            raise StateTransitionInvariant("atomic flight success owner lost")
        connection.execute("INSERT INTO provider_query_flight_terminals(run_id,provider,query_fingerprint,execution_generation,state,provider_call_id,result_sha256,call_ids_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, provider, query_fingerprint, int(flight[1]), "DONE", provider_call_id, hashlib.sha256(result_json.encode()).hexdigest(), json.dumps(merged_ids), now))
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            connection.rollback()
            raise EvidenceInvariant("atomic flight success violated relational evidence")
        connection.commit()


def wait_provider_query_flight(*, run_id: str, provider: str, query_fingerprint: str, owner_token: str = "", timeout_seconds: float | None = None, waiter=time.sleep, clock=time.monotonic, now_fn=lambda: datetime.now(timezone.utc)) -> dict[str, Any]:
    deadline = None if timeout_seconds is None else clock() + max(0.0, float(timeout_seconds))
    while deadline is None or clock() <= deadline:
        with closing(_connect()) as connection:
            row = connection.execute(
                "SELECT state,result_json,call_ids_json,lease_expires_at,provider_call_id,heartbeat_at,execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=?",
                (run_id, provider, query_fingerprint),
            ).fetchone()
        if row and str(row[0]) in {"DONE", "FAILED", "UNKNOWN"}:
            return {"state": str(row[0]), "result": json.loads(row[1] or "{}"), "call_ids": json.loads(row[2] or "[]"), "lease_expires_at": str(row[3]), "provider_call_id": str(row[4] or ""), "execution_generation": int(row[6])}
        if row and str(row[3]) <= now_fn().isoformat(timespec="microseconds"):
            resolved = resolve_expired_provider_query_flight(run_id=run_id, provider=provider, query_fingerprint=query_fingerprint, owner_token=owner_token or uuid.uuid4().hex, now=now_fn())
            if resolved.get("leader") or resolved.get("state") in {"DONE", "FAILED", "UNKNOWN"}:
                return resolved
        waiter(0.01)
    raise StateTransitionInvariant("singleflight waiter deadline expired before a durable terminal or reclaim state")


def validate_paid_evidence(run_id: str) -> dict[str, int]:
    from modules import scorer
    with closing(_connect()) as connection:
        items = connection.execute("SELECT item_index,paid_state FROM run_items WHERE run_id=? AND paid_required=1", (run_id,)).fetchall()
        for item_index, paid_state in items:
            attempt = connection.execute(
                "SELECT paid_attempt_id,result,evidence_kind,input_snapshot_sha256 FROM paid_attempts WHERE run_id=? AND item_index=? AND phase='PAID' ORDER BY attempt_number DESC LIMIT 1",
                (run_id, int(item_index)),
            ).fetchone()
            if not attempt:
                raise EvidenceInvariant(f"current paid attempt missing for item {item_index}")
            paid_attempt_id, attempt_result, evidence_kind, snapshot_hash = map(str, attempt)
            valid_results = {
                "DONE": {"COMPLETED", "NO_CALL_NEEDED"}, "FAILED": {"FAILED"},
                "UNKNOWN": {"UNKNOWN"}, "BLOCKED_BUDGET": {"BLOCKED_BUDGET"},
            }
            if str(paid_state) not in valid_results or attempt_result not in valid_results[str(paid_state)]:
                raise EvidenceInvariant(f"paid state and current attempt result mismatch for item {item_index}")
            links = connection.execute(
                "SELECT pc.state,pac.relation,pac.provider,pac.provider_call_id,pc.item_index,pac.query_fingerprint,pac.execution_generation,pc.provider,pc.flight_fingerprint FROM paid_attempt_calls pac JOIN provider_calls pc ON pc.call_id=pac.provider_call_id AND pc.run_id=pac.run_id AND pc.provider=pac.provider WHERE pac.run_id=? AND pac.item_index=? AND pac.paid_attempt_id=?",
                (run_id, int(item_index), paid_attempt_id),
            ).fetchall()
            raw_link_count = connection.execute("SELECT COUNT(*) FROM paid_attempt_calls WHERE run_id=? AND item_index=? AND paid_attempt_id=?", (run_id, int(item_index), paid_attempt_id)).fetchone()[0]
            if int(raw_link_count) != len(links):
                raise EvidenceInvariant("paid attempt relation does not join exact provider call scope")
            states = {str(row[0]) for row in links}
            flight_unknown = connection.execute(
                "SELECT 1 FROM provider_query_flights f JOIN provider_query_flight_consumers c ON c.run_id=f.run_id AND c.provider=f.provider AND c.query_fingerprint=f.query_fingerprint AND c.execution_generation=f.execution_generation WHERE c.run_id=? AND c.item_index=? AND c.paid_attempt_id=? AND f.state='UNKNOWN'",
                (run_id, int(item_index), paid_attempt_id),
            ).fetchone()
            has_unknown_or_nonterminal = bool(
                states.intersection({"UNKNOWN", "RESERVED", "RUNNING"}) or flight_unknown
            )
            if has_unknown_or_nonterminal and (attempt_result != "UNKNOWN" or str(paid_state) != "UNKNOWN"):
                raise EvidenceInvariant("UNKNOWN/nonterminal evidence has precedence over every terminal outcome")
            if not has_unknown_or_nonterminal and "DONE" in states and attempt_result not in {"COMPLETED", "NO_CALL_NEEDED"}:
                raise EvidenceInvariant("DONE evidence has precedence over non-success terminal outcomes")
            for call_state, relation, provider, call_id, owner_item, fingerprint, generation, call_provider, call_fingerprint in links:
                if str(provider) != str(call_provider) or str(fingerprint) != str(call_fingerprint):
                    raise EvidenceInvariant("paid attempt call scope columns differ from provider call")
                if str(relation) == "OWNER" and int(owner_item) != int(item_index):
                    raise EvidenceInvariant("OWNER relation requires provider call item match")
                if str(relation) == "INHERITED":
                    consumer = connection.execute("SELECT 1 FROM provider_query_flight_consumers c JOIN provider_query_flight_terminals t ON t.run_id=c.run_id AND t.provider=c.provider AND t.query_fingerprint=c.query_fingerprint AND t.execution_generation=c.execution_generation JOIN provider_calls pc ON pc.run_id=c.run_id AND pc.provider=c.provider AND pc.call_id=c.provider_call_id LEFT JOIN provider_query_flight_results r ON r.run_id=c.run_id AND r.provider=c.provider AND r.query_fingerprint=c.query_fingerprint AND r.execution_generation=c.execution_generation WHERE c.run_id=? AND c.item_index=? AND c.paid_attempt_id=? AND c.provider_call_id=? AND c.provider=? AND c.query_fingerprint=? AND c.execution_generation=? AND c.relation='INHERITED' AND ((t.state='DONE' AND r.provider_call_id IS NOT NULL) OR (t.state='FAILED' AND pc.state='FAILED'))", (run_id, int(item_index), paid_attempt_id, str(call_id), str(provider), str(fingerprint), int(generation))).fetchone()
                    if not consumer:
                        raise EvidenceInvariant("INHERITED relation lacks same-generation result receipt")
            no_call = False
            if attempt_result == "NO_CALL_NEEDED" and evidence_kind == "supplied_website_publishable_at_paid_entry":
                physical_calls = connection.execute("SELECT COUNT(*) FROM paid_attempt_calls pac JOIN provider_calls pc ON pc.run_id=pac.run_id AND pc.provider=pac.provider AND pc.call_id=pac.provider_call_id WHERE pac.run_id=? AND pac.item_index=? AND pac.paid_attempt_id=? AND pc.http_started_at<>''", (run_id, int(item_index), paid_attempt_id)).fetchone()[0]
                if physical_calls:
                    raise EvidenceInvariant("supplied website no-call evidence requires exact zero paid physical calls")
                immutable = connection.execute("SELECT snapshot_sha256,snapshot_json FROM immutable_input_snapshots WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
                receipt = connection.execute("SELECT input_snapshot_sha256,normalized_input_website,evaluation_payload_sha256,result_payload_sha256,publication_eligible,evaluator_schema_version FROM paid_no_call_evidence WHERE run_id=? AND item_index=? AND paid_attempt_id=?", (run_id, int(item_index), paid_attempt_id)).fetchone()
                result_row = connection.execute("SELECT payload FROM results WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
                if immutable and receipt and result_row:
                    snapshot_payload = json.loads(str(immutable[1])); result_payload = json.loads(str(result_row[0])); evaluation = result_payload.get("known_website_evaluation")
                    evaluation_text = json.dumps(_json_safe(evaluation), ensure_ascii=False, sort_keys=True, separators=(",", ":")) if isinstance(evaluation, dict) else ""
                    result_text = json.dumps(_json_safe(result_payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    normalized = scorer.normalize_domain(str(snapshot_payload.get("website", "")))
                    no_call = bool(not physical_calls and snapshot_hash and snapshot_hash == str(immutable[0]) == str(receipt[0]) and normalized and normalized == str(receipt[1]) == scorer.normalize_domain(str(result_payload.get("website", ""))) and hashlib.sha256(evaluation_text.encode()).hexdigest() == str(receipt[2]) and hashlib.sha256(result_text.encode()).hexdigest() == str(receipt[3]) and int(receipt[4]) == 1 and int(receipt[5]) >= 1 and result_payload.get("publication_eligible") is True and str(result_payload.get("status", "")) in {"OK_HIGH_CONFIDENCE", "OK_MEDIUM_CONFIDENCE"})
            elif attempt_result == "NO_CALL_NEEDED" and evidence_kind == "terminal_paid_result_reference":
                no_call = bool(links) and all(str(row[1]) == "INHERITED" and str(row[0]) == "DONE" for row in links)
            if paid_state == "DONE" and "DONE" not in states and not no_call:
                raise EvidenceInvariant(f"current paid attempt lacks DONE relational evidence for item {item_index}")
            if paid_state == "FAILED" and "FAILED" not in states:
                raise EvidenceInvariant(f"FAILED requires durable FAILED call linked to current paid attempt for item {item_index}")
            if paid_state == "UNKNOWN" and not has_unknown_or_nonterminal:
                raise OutcomeInvariant(f"zero-call UNKNOWN lacks related provider evidence for item {item_index}")
            if paid_state == "BLOCKED_BUDGET":
                if states.intersection({"DONE", "UNKNOWN", "RESERVED", "RUNNING"}):
                    raise EvidenceInvariant("provider evidence has precedence over BLOCKED_BUDGET")
                plans = connection.execute("SELECT provider,authorized,effective_limit FROM paid_attempt_provider_plan WHERE run_id=? AND item_index=? AND paid_attempt_id=?", (run_id, int(item_index), paid_attempt_id)).fetchall()
                if not plans:
                    raise EvidenceInvariant("BLOCKED_BUDGET lacks current frozen provider plan")
                for provider, authorized, effective_limit in plans:
                    if not int(authorized):
                        continue
                    terminal = connection.execute("SELECT 1 FROM paid_attempt_calls pac JOIN provider_calls pc ON pc.call_id=pac.provider_call_id WHERE pac.paid_attempt_id=? AND pc.provider=? AND pc.state IN ('DONE','FAILED') LIMIT 1", (paid_attempt_id, str(provider))).fetchone()
                    blocked = connection.execute("SELECT 1 FROM paid_attempt_block_links l JOIN provider_budget_blocks b ON b.block_id=l.block_id WHERE l.paid_attempt_id=? AND b.run_id=? AND b.item_index=? AND b.provider=? AND b.provider<>'ddgs' LIMIT 1", (paid_attempt_id, run_id, int(item_index), str(provider))).fetchone()
                    if not terminal and not blocked:
                        raise EvidenceInvariant(f"BLOCKED_BUDGET lacks current-plan evidence for provider {provider}")
    return {"historical_zero_call_paid_failure": 0}


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
            raise StateTransitionInvariant("handoff release requires terminal paid snapshot")
        released = 0
        for item_index, payload_text in rows:
            payload = json.loads(str(payload_text))
            prior_blockers = str(payload.get("publication_blockers", ""))
            if (
                "legacy_recovery_provisional" in prior_blockers
                or str(payload.get("source_record_id_quality", "")).casefold() == "legacy_recovery"
            ):
                connection.rollback()
                raise EvidenceInvariant("permanent recovery quarantine cannot be released")
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
            decision_input = dict(payload)
            # HANDOFF_PENDING temporarily overwrote the final boolean; the
            # release decision must recompute it from the preserved evidence.
            if "HANDOFF_PENDING" in prior_blockers:
                decision_input["publication_eligible"] = str(payload.get("status", "")) in publication_policy.OK_STATUSES
            decision = publication_policy.decide_row(decision_input, evaluation)
            payload["publication_eligible"] = bool(decision["publishable"])
            payload["publication_advisory_eligible"] = bool(decision["advisory_eligible"])
            payload["website_identity_verified"] = bool(decision["website_identity_verified"])
            payload["allowed_contact_fields"] = "; ".join(decision["allowed_contact_fields"])
            blockers.extend(str(value) for value in decision["blockers"])
            payload["publication_blockers"] = "; ".join(sorted(set(blockers)))
            safe_payload = json.dumps(_json_safe(redaction.sanitize(payload)), ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(safe_payload.encode("utf-8")).hexdigest()
            connection.execute("UPDATE run_items SET quarantine_state='',quarantine_status='',publication_blockers=?,payload_sha256=? WHERE run_id=? AND item_index=? AND quarantine_state='HANDOFF_PENDING'", (payload["publication_blockers"], digest, run_id, int(item_index)))
            connection.execute("UPDATE results SET payload=? WHERE run_id=? AND item_index=?", (safe_payload, run_id, int(item_index)))
            connection.execute(
                "UPDATE paid_no_call_evidence SET result_payload_sha256=? WHERE run_id=? AND item_index=?",
                (hashlib.sha256(json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(), run_id, int(item_index)),
            )
            released += 1
        if released != int(expected_count):
            connection.rollback()
            raise StateTransitionInvariant("handoff release count mismatch")
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
            raise StateTransitionInvariant(f"scheduler CAS failed for item {item_index}")
        column = "free_state" if current[0] == "RUNNING" else "paid_state" if current[1] == "RUNNING" else ""
        if not column:
            connection.rollback()
            raise StateTransitionInvariant(f"scheduler CAS failed for item {item_index}")
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
        is_handoff_overlay = quarantine_state == "HANDOFF_PENDING" and column == "free_state"
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
            raise StateTransitionInvariant(f"scheduler CAS failed for item {item_index}")
        # A free result may also establish the paid queue while preserving the
        # free terminal state in the same transaction.
        if column == "free_state":
            connection.execute("UPDATE run_items SET paid_state=?,paid_required=? WHERE run_id=? AND item_index=?", (paid_state, int(paid_required), run_id, item_index))
        connection.execute("INSERT OR REPLACE INTO results(run_id,item_index,payload) VALUES(?,?,?)", (run_id, item_index, safe_payload))
        connection.execute("UPDATE runs SET updated_at=?,runtime_json=? WHERE run_id=?", (now, json.dumps(_json_safe(runtime.snapshot()), ensure_ascii=False), run_id))
        connection.commit()


def record_paid_attempt(*, run_id: str, item_index: int, attempt_number: int, result: str,
                        reason: str = "", call_id: str = "", request_fingerprint: str = "", call_ids: list[str] | None = None,
                        call_relations: dict[str, str] | None = None, evidence_kind: str = "", input_snapshot_sha256: str = "") -> None:
    if result not in {"COMPLETED", "NO_CALL_NEEDED", "BLOCKED_BUDGET", "FAILED", "UNKNOWN"}:
        raise OutcomeInvariant(f"invalid paid attempt result: {result}")
    with closing(_connect()) as connection:
        paid_attempt_id = hashlib.sha256(f"{run_id}\0{int(item_index)}\0{int(attempt_number)}\0PAID".encode("utf-8")).hexdigest()
        existing_attempt = connection.execute("SELECT result,paid_attempt_id FROM paid_attempts WHERE run_id=? AND item_index=? AND attempt_number=? AND phase='PAID'", (run_id, int(item_index), int(attempt_number))).fetchone()
        if not existing_attempt:
            item = connection.execute("SELECT paid_required,paid_state FROM run_items WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
            if item and int(item[0]) == 1 and str(item[1]) == "RUNNING":
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                connection.execute("INSERT INTO paid_attempts(run_id,item_index,attempt_number,phase,result,created_at,paid_attempt_id) VALUES(?,?,?,?,?,?,?)", (run_id, int(item_index), int(attempt_number), "PAID", "RUNNING", now, paid_attempt_id))
                for ordinal, (provider_name, limit) in enumerate(connection.execute("SELECT provider,effective_limit FROM provider_usage WHERE run_id=? ORDER BY provider", (run_id,)).fetchall(), 1):
                    connection.execute("INSERT INTO paid_attempt_provider_plan(run_id,item_index,paid_attempt_id,provider,plan_ordinal,authorized,effective_limit) VALUES(?,?,?,?,?,?,?)", (run_id, int(item_index), paid_attempt_id, str(provider_name), ordinal, int(limit) > 0, int(limit)))
                existing_attempt = ("RUNNING", paid_attempt_id)
        if not existing_attempt or str(existing_attempt[0]) not in {"RUNNING", "UNKNOWN"} or str(existing_attempt[1]) != paid_attempt_id:
            connection.rollback()
            raise StateTransitionInvariant("paid attempt terminal update requires the current claimed attempt")
        cursor = connection.execute(
            "UPDATE paid_attempts SET result=?,reason=?,call_id=?,request_fingerprint=?,evidence_kind=?,input_snapshot_sha256=? WHERE run_id=? AND item_index=? AND attempt_number=? AND phase='PAID' AND result IN ('RUNNING','UNKNOWN')",
            (result, str(reason), str(call_id), str(request_fingerprint), str(evidence_kind), str(input_snapshot_sha256), run_id, int(item_index), int(attempt_number)),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise StateTransitionInvariant("paid attempt terminal CAS failed")
        for linked_call_id in sorted(set(call_ids or ([call_id] if call_id else []))):
            valid = connection.execute("SELECT provider,item_index,flight_fingerprint,state FROM provider_calls WHERE run_id=? AND phase='PAID' AND call_id=?", (run_id, linked_call_id)).fetchone()
            if not valid:
                connection.rollback()
                raise EvidenceInvariant(f"paid attempt references unknown provider call: {linked_call_id}")
            relation = str((call_relations or {}).get(linked_call_id, "OWNER")).upper()
            if relation not in {"OWNER", "INHERITED"}:
                connection.rollback()
                raise EvidenceInvariant(f"invalid paid call relation: {relation}")
            provider, owner_item, fingerprint, call_state = str(valid[0]), int(valid[1]), str(valid[2]), str(valid[3])
            if relation == "OWNER" and owner_item != int(item_index):
                connection.rollback()
                raise EvidenceInvariant("OWNER relation requires provider call item match")
            if relation == "OWNER" and fingerprint:
                owner_flight = connection.execute("SELECT state,call_ids_json,execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=?", (run_id, provider, fingerprint)).fetchone()
                if owner_flight and str(owner_flight[0]) in {"DONE", "FAILED", "UNKNOWN"} and linked_call_id in json.loads(owner_flight[1] or "[]"):
                    connection.execute("INSERT OR IGNORE INTO provider_query_flight_consumers(run_id,provider,query_fingerprint,execution_generation,paid_attempt_id,item_index,provider_call_id,relation,linked_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, provider, fingerprint, int(owner_flight[2]), paid_attempt_id, int(item_index), linked_call_id, relation, datetime.now(timezone.utc).isoformat(timespec="microseconds")))
            if relation == "INHERITED":
                flight = connection.execute("SELECT state,call_ids_json,execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=?", (run_id, provider, fingerprint)).fetchone()
                if not flight or str(flight[0]) not in {"DONE", "FAILED", "UNKNOWN"} or linked_call_id not in json.loads(flight[1] or "[]"):
                    connection.rollback()
                    raise EvidenceInvariant("INHERITED relation requires matching terminal flight fingerprint")
                compatible_flight = {"COMPLETED": "DONE", "NO_CALL_NEEDED": "DONE", "FAILED": "FAILED", "UNKNOWN": "UNKNOWN"}.get(result)
                if compatible_flight and str(flight[0]) != compatible_flight:
                    connection.rollback()
                    raise EvidenceInvariant("inherited terminal flight state does not match paid attempt")
                connection.execute("INSERT OR IGNORE INTO provider_query_flight_consumers(run_id,provider,query_fingerprint,execution_generation,paid_attempt_id,item_index,provider_call_id,relation,linked_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, provider, fingerprint, int(flight[2]), paid_attempt_id, int(item_index), linked_call_id, relation, datetime.now(timezone.utc).isoformat(timespec="microseconds")))
            generation_row = connection.execute("SELECT execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=?", (run_id, provider, fingerprint)).fetchone() if fingerprint else None
            connection.execute("INSERT OR IGNORE INTO paid_attempt_calls(run_id,item_index,attempt_number,phase,call_id,paid_attempt_id,provider_call_id,provider,query_fingerprint,execution_generation,relation) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, int(item_index), int(attempt_number), "PAID", linked_call_id, paid_attempt_id, linked_call_id, provider, fingerprint, int(generation_row[0]) if generation_row else 1, relation))
        connection.commit()


def begin_paid_attempt(*, run_id: str, item_index: int, attempt_number: int, provider_plan: list[str] | tuple[str, ...] | None = None) -> None:
    """Durably create the paid attempt at item claim time."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = connection.execute("SELECT paid_required,paid_state FROM run_items WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
        if not item or int(item[0]) != 1 or str(item[1]) != "RUNNING":
            connection.rollback()
            raise StateTransitionInvariant("paid attempt claim requires a paid RUNNING item")
        existing = connection.execute("SELECT result FROM paid_attempts WHERE run_id=? AND item_index=? AND attempt_number=? AND phase='PAID'", (run_id, int(item_index), int(attempt_number))).fetchone()
        if existing and str(existing[0]) not in {"RUNNING", "UNKNOWN"}:
            connection.rollback()
            raise StateTransitionInvariant("paid attempt ordinal already has a terminal result")
        if not existing:
            paid_attempt_id = hashlib.sha256(f"{run_id}\0{int(item_index)}\0{int(attempt_number)}\0PAID".encode("utf-8")).hexdigest()
            connection.execute("INSERT INTO paid_attempts(run_id,item_index,attempt_number,phase,result,created_at,paid_attempt_id) VALUES(?,?,?,?,?,?,?)", (run_id, int(item_index), int(attempt_number), "PAID", "RUNNING", now, paid_attempt_id))
            providers = connection.execute("SELECT provider,effective_limit FROM provider_usage WHERE run_id=? ORDER BY provider", (run_id,)).fetchall()
            planned = set(str(value) for value in provider_plan) if provider_plan is not None else {str(value[0]) for value in providers}
            for ordinal, (provider, effective_limit) in enumerate(providers, 1):
                connection.execute("INSERT INTO paid_attempt_provider_plan(run_id,item_index,paid_attempt_id,provider,plan_ordinal,authorized,effective_limit) VALUES(?,?,?,?,?,?,?)", (run_id, int(item_index), paid_attempt_id, str(provider), ordinal, int(str(provider) in planned and int(effective_limit) > 0), int(effective_limit)))
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


def record_paid_no_call_evidence(*, run_id: str, item_index: int, attempt_number: int) -> None:
    from modules import scorer
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        attempt = connection.execute("SELECT paid_attempt_id,result,evidence_kind,input_snapshot_sha256 FROM paid_attempts WHERE run_id=? AND item_index=? AND attempt_number=? AND phase='PAID'", (run_id, int(item_index), int(attempt_number))).fetchone()
        snapshot = connection.execute("SELECT snapshot_sha256,snapshot_json FROM immutable_input_snapshots WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
        result_row = connection.execute("SELECT payload FROM results WHERE run_id=? AND item_index=?", (run_id, int(item_index))).fetchone()
        if not attempt or str(attempt[1]) != "NO_CALL_NEEDED" or str(attempt[2]) != "supplied_website_publishable_at_paid_entry" or not snapshot or not result_row:
            connection.rollback()
            raise EvidenceInvariant("supplied website no-call evidence prerequisites are missing")
        snapshot_payload = json.loads(str(snapshot[1]))
        result_payload = json.loads(str(result_row[0]))
        evaluation = result_payload.get("known_website_evaluation")
        input_website = scorer.normalize_domain(str(snapshot_payload.get("website", "")))
        result_website = scorer.normalize_domain(str(result_payload.get("website", "")))
        if not input_website or input_website != result_website or not isinstance(evaluation, dict) or not bool(result_payload.get("publication_eligible")) or str(result_payload.get("status", "")) not in {"OK_HIGH_CONFIDENCE", "OK_MEDIUM_CONFIDENCE"}:
            connection.rollback()
            raise EvidenceInvariant("supplied website no-call semantic receipt is invalid")
        evaluation_text = json.dumps(_json_safe(evaluation), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result_text = json.dumps(_json_safe(result_payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection.execute("INSERT OR REPLACE INTO paid_no_call_evidence(run_id,item_index,paid_attempt_id,evidence_kind,input_snapshot_sha256,normalized_input_website,evaluation_payload_sha256,result_payload_sha256,publication_eligible,evaluator_schema_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, int(item_index), str(attempt[0]), str(attempt[2]), str(snapshot[0]), input_website, hashlib.sha256(evaluation_text.encode()).hexdigest(), hashlib.sha256(result_text.encode()).hexdigest(), 1, 1, datetime.now(timezone.utc).isoformat(timespec="microseconds")))
        connection.commit()


def record_provider_budget_block(*, run_id: str, item_index: int, provider: str, block_kind: str, bucket: str = "", backend: str = "") -> bool:
    canonical_provider = "ddgs" if str(provider) == "ddgs" or str(block_kind) in {"logical", "physical"} else str(provider)
    block_id = hashlib.sha256(f"{run_id}\0{int(item_index)}\0{canonical_provider}\0{bucket}\0{block_kind}".encode()).hexdigest()
    with closing(_connect()) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO provider_budget_blocks(run_id,item_index,provider,bucket,block_kind,created_at,block_id,backend) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, int(item_index), canonical_provider, str(bucket), str(block_kind), datetime.now(timezone.utc).isoformat(timespec="microseconds"), block_id, str(backend or (provider if canonical_provider == 'ddgs' else ''))),
        )
        if canonical_provider != "ddgs":
            attempt = connection.execute("SELECT paid_attempt_id FROM paid_attempts WHERE run_id=? AND item_index=? AND phase='PAID' AND result='RUNNING' ORDER BY attempt_number DESC LIMIT 1", (run_id, int(item_index))).fetchone()
            if attempt:
                connection.execute("INSERT OR IGNORE INTO paid_attempt_block_links(run_id,item_index,provider,paid_attempt_id,block_id) VALUES(?,?,?,?,?)", (run_id, int(item_index), canonical_provider, str(attempt[0]), block_id))
        connection.commit()
    return cursor.rowcount == 1


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


def provider_call_transport_context(call_id: str) -> dict[str, Any]:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT c.request_fingerprint,c.flight_fingerprint,l.paid_attempt_id,l.execution_generation FROM provider_calls c LEFT JOIN paid_attempt_calls l ON l.run_id=c.run_id AND l.provider_call_id=c.call_id WHERE c.call_id=? ORDER BY l.paid_attempt_id DESC LIMIT 1",
            (str(call_id),),
        ).fetchone()
    if not row:
        raise LedgerInvariant("transport context references unknown provider call")
    return {
        "request_fingerprint": str(row[0]), "flight_fingerprint": str(row[1]),
        "paid_attempt_id": str(row[2] or ""), "execution_generation": int(row[3] or 1),
    }


def provider_call_exists(*, run_id: str, provider: str, item_index: int, phase: str,
                         request_fingerprint: str) -> bool:
    with closing(_connect()) as connection:
        return connection.execute(
            "SELECT 1 FROM provider_calls WHERE run_id=? AND provider=? AND item_index=? AND phase=? AND request_fingerprint=? LIMIT 1",
            (run_id, provider, int(item_index), phase, request_fingerprint),
        ).fetchone() is not None


def provider_call_for_fingerprint(*, run_id: str, provider: str, item_index: int,
                                  phase: str, request_fingerprint: str,
                                  query_fingerprint: str = "",
                                  execution_generation: int = 0) -> dict[str, str] | None:
    with closing(_connect()) as connection:
        if query_fingerprint and int(execution_generation) > 0:
            row = connection.execute(
                "SELECT c.call_id,c.state,c.result_ref FROM provider_calls c "
                "JOIN paid_attempt_calls l ON l.run_id=c.run_id AND l.provider=c.provider AND l.provider_call_id=c.call_id "
                "WHERE c.run_id=? AND c.provider=? AND c.item_index=? AND c.phase=? AND c.request_fingerprint=? "
                "AND l.query_fingerprint=? AND l.execution_generation=? LIMIT 1",
                (run_id, provider, int(item_index), phase, request_fingerprint,
                 str(query_fingerprint), int(execution_generation)),
            ).fetchone()
        else:
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
    canonical = canonical_scheduler_receipt(run_id)
    if telemetry_snapshot is not None and _json_safe(telemetry_snapshot) != canonical:
        raise EvidenceInvariant("finalization telemetry differs from canonical scheduler receipt")
    telemetry_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    telemetry_sha256 = hashlib.sha256(telemetry_json.encode("utf-8")).hexdigest()
    with closing(_connect()) as connection:
        existing = connection.execute("SELECT generation,input_snapshot_sha256,result_snapshot_sha256,status,telemetry_snapshot_json,telemetry_sha256 FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
        if existing:
            if (str(existing[0]) != str(generation)
                    or str(existing[1]) != str(input_snapshot_sha256)
                    or result_snapshot_sha256 is not None and str(existing[2]) != str(result_snapshot_sha256)):
                raise StateTransitionInvariant("finalization intent identity changed")
            if str(existing[3]) not in {"STARTED", "ARTIFACT_READY", "COMPLETE"}:
                raise StateTransitionInvariant("invalid finalization intent state")
            if str(existing[4]) != telemetry_json or str(existing[5]) != telemetry_sha256:
                raise EvidenceInvariant("frozen finalization telemetry receipt changed")
            connection.commit()
            return
        connection.execute(
            "INSERT INTO finalization_intent(run_id,generation,input_snapshot_sha256,result_snapshot_sha256,started_at,status,output_context_json,telemetry_snapshot_json,telemetry_sha256,finalization_schema_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, str(generation), str(input_snapshot_sha256), str(result_snapshot_sha256 or input_snapshot_sha256), datetime.now(timezone.utc).isoformat(timespec="seconds"), "STARTED", json.dumps({"phase": "FINALIZING", **_json_safe(output_context or {})}, ensure_ascii=False), telemetry_json, telemetry_sha256, 3),
        )
        connection.commit()


def complete_finalization_intent(*, run_id: str, artifact_set_sha256: str, manifest_sha256: str) -> None:
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status,artifact_set_sha256,manifest_sha256 FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            connection.rollback()
            raise StateTransitionInvariant("finalization intent is missing")
        if str(row[0]) == "COMPLETE":
            if str(row[1]) != str(artifact_set_sha256) or str(row[2]) != str(manifest_sha256):
                connection.rollback()
                raise StateTransitionInvariant("finalization receipt identity changed")
            connection.commit()
            return
        if str(row[0]) not in {"STARTED", "ARTIFACT_READY"}:
            connection.rollback()
            raise StateTransitionInvariant("finalization intent is not completable")
        plan = connection.execute("SELECT memory_plan_sha256,memory_plan_count,memory_plan_committed FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
        if not plan or int(plan[2] or 0) != 1:
            connection.rollback()
            raise StateTransitionInvariant("finalization memory plan is not committed")
        entries, plan_hash = _outbox_plan(connection, run_id)
        if len(entries) != int(plan[1]) or plan_hash != str(plan[0]):
            connection.rollback()
            raise StateTransitionInvariant("finalization memory plan exact receipt mismatch")
        if not artifact_set_sha256 or not manifest_sha256:
            connection.rollback()
            raise StateTransitionInvariant("finalization receipt identity is incomplete")
        connection.execute("UPDATE finalization_intent SET artifact_set_sha256=?,manifest_sha256=?,completed_at=?,status='COMPLETE' WHERE run_id=? AND status IN ('STARTED','ARTIFACT_READY')", (str(artifact_set_sha256), str(manifest_sha256), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id))
        connection.commit()


def mark_finalization_artifact(*, run_id: str, artifact_set_sha256: str, output_context: dict[str, Any]) -> None:
    with closing(_connect()) as connection:
        row = connection.execute("SELECT status,artifact_set_sha256,output_context_json FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
        if not row or str(row[0]) not in {"STARTED", "ARTIFACT_READY"}:
            connection.rollback()
            raise StateTransitionInvariant("finalization artifact cannot be prepared in the current state")
        if str(row[1]) and str(row[1]) != str(artifact_set_sha256):
            connection.rollback()
            raise StateTransitionInvariant("finalization artifact identity changed")
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
    frozen_telemetry = canonical_scheduler_receipt(run_id)
    if telemetry_snapshot is not None and _json_safe(telemetry_snapshot) != frozen_telemetry:
        raise EvidenceInvariant("artifact telemetry differs from canonical scheduler receipt")
    telemetry_json = json.dumps(frozen_telemetry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        intent = connection.execute(
            "SELECT generation,result_snapshot_sha256,status,artifact_set_sha256,memory_plan_sha256,memory_plan_count,memory_plan_committed,telemetry_snapshot_json,telemetry_sha256 FROM finalization_intent WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if not intent:
            connection.rollback()
            raise StateTransitionInvariant("finalization intent is missing")
        if str(intent[0]) != str(generation) or str(intent[1]) != str(result_snapshot_sha256):
            connection.rollback()
            raise StateTransitionInvariant("finalization intent snapshot identity changed")
        if str(intent[2]) not in {"STARTED", "ARTIFACT_READY"}:
            connection.rollback()
            raise StateTransitionInvariant("finalization artifact cannot be prepared in the current state")
        if str(intent[7]) != telemetry_json or str(intent[8]) != hashlib.sha256(telemetry_json.encode("utf-8")).hexdigest():
            connection.rollback()
            raise EvidenceInvariant("artifact telemetry receipt changed after freeze")
        if str(intent[3]) and str(intent[3]) != str(artifact_set_sha256):
            connection.rollback()
            raise EvidenceInvariant("finalization artifact identity changed")
        if int(intent[6] or 0) and (str(intent[4]) != plan_hash or int(intent[5]) != plan_count):
            connection.rollback()
            raise EvidenceInvariant("finalization memory plan identity changed")
        existing_rows = connection.execute(
            "SELECT receipt_key,payload FROM memory_outbox WHERE run_id=? ORDER BY receipt_key",
            (run_id,),
        ).fetchall()
        existing_plan = [(str(receipt), str(payload)) for receipt, payload in existing_rows]
        if existing_plan and existing_plan != plan_entries:
            connection.rollback()
            raise EvidenceInvariant("finalization memory outbox exact plan mismatch")
        if not existing_plan and plan_entries:
            if int(intent[6] or 0):
                connection.rollback()
                raise EvidenceInvariant("committed finalization memory plan is missing outbox rows")
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
            raise EvidenceInvariant("finalization memory outbox reread mismatch")
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
            "UPDATE finalization_intent SET artifact_set_sha256=?,output_context_json=?,memory_plan_sha256=?,memory_plan_count=?,memory_plan_committed=1,status='ARTIFACT_READY' WHERE run_id=? AND status IN ('STARTED','ARTIFACT_READY')",
            (str(artifact_set_sha256), json.dumps(output_context, ensure_ascii=False), plan_hash, plan_count, run_id),
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
            raise EvidenceInvariant("current finalization schema has a missing memory plan")
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
        row = connection.execute("SELECT generation,input_snapshot_sha256,result_snapshot_sha256,started_at,artifact_set_sha256,manifest_sha256,completed_at,status,output_context_json,memory_plan_sha256,memory_plan_count,memory_plan_committed,telemetry_snapshot_json,telemetry_sha256,finalization_schema_version FROM finalization_intent WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    fields = ("generation", "input_snapshot_sha256", "result_snapshot_sha256", "started_at", "artifact_set_sha256", "manifest_sha256", "completed_at", "status", "output_context_json", "memory_plan_sha256", "memory_plan_count", "memory_plan_committed", "telemetry_snapshot_json", "telemetry_sha256", "finalization_schema_version")
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


def validate_ledger_equations(run_id: str, *, require_terminal: bool = False) -> dict[str, Any]:
    telemetry = derive_telemetry(run_id)
    free = telemetry["free_queries"]
    if free["physical_attempted"] != free["physical_done"] + free["physical_failed"] + free["physical_reserved"]:
        raise LedgerInvariant("free provider ledger equation failed")
    item_cap = max(1, int(telemetry["total_items"]))
    if free["logical_used"] > 10 * item_cap or free["discovery_logical_accepted"] > 6 * item_cap or free["targeted_logical_accepted"] > 4 * item_cap:
        raise LedgerInvariant("free logical hard cap exceeded")
    if free["physical_attempted"] > 10 * item_cap or free["discovery_physical_attempted"] > 6 * item_cap or free["targeted_physical_attempted"] > 4 * item_cap:
        raise LedgerInvariant("free provider hard cap exceeded")
    if require_terminal and free["physical_reserved"]:
        raise LedgerInvariant("free provider ledger has RESERVED attempts")
    with closing(_connect()) as connection:
        aggregates = connection.execute("SELECT provider,reserved_total,reserved,completed,failed,unknown,effective_limit FROM provider_usage WHERE run_id=?", (run_id,)).fetchall()
        state_counts = {(str(provider), str(state)): int(count) for provider, state, count in connection.execute("SELECT provider,state,COUNT(*) FROM provider_calls WHERE run_id=? GROUP BY provider,state", (run_id,)).fetchall()}
        free_aggregates = connection.execute("SELECT item_index,physical_used,physical_completed,physical_failed,discovery_physical_used,targeted_physical_used FROM free_query_usage WHERE run_id=?", (run_id,)).fetchall()
        for item_index, used, completed, failed, discovery, targeted in free_aggregates:
            attempt_counts = {(str(bucket), str(state)): int(count) for bucket, state, count in connection.execute("SELECT bucket,state,COUNT(*) FROM free_provider_attempts WHERE run_id=? AND item_index=? GROUP BY bucket,state", (run_id, int(item_index))).fetchall()}
            exact = {
                "used": sum(attempt_counts.values()),
                "completed": sum(count for (bucket, state), count in attempt_counts.items() if state == "DONE"),
                "failed": sum(count for (bucket, state), count in attempt_counts.items() if state == "FAILED"),
                "discovery": sum(count for (bucket, state), count in attempt_counts.items() if bucket == "discovery"),
                "targeted": sum(count for (bucket, state), count in attempt_counts.items() if bucket == "targeted"),
            }
            if {"used": int(used), "completed": int(completed), "failed": int(failed), "discovery": int(discovery), "targeted": int(targeted)} != exact:
                raise LedgerInvariant(f"free aggregate ledger mismatch for item {int(item_index)}")
        usage_set = {str(row[0]) for row in aggregates}
        call_set = {provider for provider, _state in state_counts}
        if usage_set != CANONICAL_PROVIDERS or not call_set.issubset(CANONICAL_PROVIDERS):
            raise LedgerInvariant("provider ledger set mismatch")
    for provider, reserved_total, active, completed, failed, unknown, effective_limit in aggregates:
        expected = {
            "reserved_total": sum(state_counts.get((str(provider), state), 0) for state in ("RESERVED", "RUNNING", "DONE", "FAILED", "UNKNOWN")),
            "reserved": state_counts.get((str(provider), "RESERVED"), 0) + state_counts.get((str(provider), "RUNNING"), 0),
            "completed": state_counts.get((str(provider), "DONE"), 0),
            "failed": state_counts.get((str(provider), "FAILED"), 0),
            "unknown": state_counts.get((str(provider), "UNKNOWN"), 0),
        }
        actual = {"reserved_total": int(reserved_total), "reserved": int(active), "completed": int(completed), "failed": int(failed), "unknown": int(unknown)}
        if actual != expected or actual["reserved_total"] != actual["completed"] + actual["failed"] + actual["unknown"] + actual["reserved"] or actual["reserved_total"] > int(effective_limit):
            raise LedgerInvariant(f"finalization scheduler/provider state is not terminal: provider aggregate ledger inconsistency: {provider}")
    for provider, values in telemetry["provider_budgets"].items():
        equation = values["done"] + values["failed"] + values["unknown"] + values["reserved"] + values["running"]
        if values["reserved_total"] != equation:
            raise LedgerInvariant(f"provider ledger equation failed: {provider}")
        if values["reserved_total"] > values["effective_limit"]:
            raise LedgerInvariant(f"finalization scheduler/provider state is not terminal: provider hard cap exceeded: {provider}")
        if require_terminal and (values["reserved"] or values["running"]):
            raise LedgerInvariant(f"provider ledger has nonterminal calls: {provider}")
    return telemetry


_CANONICAL_FREE_ONLY_ZERO_FIELDS = (
    "configured_limit", "effective_limit", "reserved_total", "reserved", "running",
    "done", "failed", "unknown", "physical_http_attempts", "retry_attempts",
    "inherited_uses", "budget_blocked_items",
)


def _strict_json_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return (
            set(left) == set(right)
            and all(_strict_json_equal(left[key], right[key]) for key in left)
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            _strict_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _canonical_json(value: dict[str, Any]) -> tuple[str, str]:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_free_only_canonical_telemetry(canonical: dict[str, Any]) -> None:
    if type(canonical.get("receipt_schema_version")) is not int or canonical["receipt_schema_version"] != 1:
        raise EvidenceInvariant("TELEMETRY_CANONICAL_SCHEMA")
    if type(canonical.get("paid_required")) is not int or canonical["paid_required"] != 0:
        raise EvidenceInvariant("TELEMETRY_CANONICAL_PAID_REQUIRED")
    if type(canonical.get("paid_completed")) is not int or canonical["paid_completed"] != 0:
        raise EvidenceInvariant("TELEMETRY_CANONICAL_PAID_COMPLETED")
    provider_budgets = canonical.get("provider_budgets")
    if type(provider_budgets) is not dict or set(provider_budgets) != CANONICAL_PROVIDERS:
        raise EvidenceInvariant("TELEMETRY_CANONICAL_PROVIDER_SET")
    for provider in CANONICAL_PROVIDERS:
        values = provider_budgets.get(provider)
        if type(values) is not dict:
            raise EvidenceInvariant(f"TELEMETRY_CANONICAL_PROVIDER:{provider}")
        for field in _CANONICAL_FREE_ONLY_ZERO_FIELDS:
            value = values.get(field)
            if type(value) is not int or value != 0:
                raise EvidenceInvariant(f"TELEMETRY_CANONICAL_NONZERO:{provider}.{field}")
    plan = canonical.get("paid_query_plan")
    if type(plan) is not dict or type(plan.get("paid_query_plan_count")) is not int or plan["paid_query_plan_count"] != 0:
        raise EvidenceInvariant("TELEMETRY_CANONICAL_PAID_QUERY_PLAN")


def validate_free_only_canonical_telemetry(canonical: dict[str, Any]) -> None:
    """Validate the canonical free-only zero invariants without duplicating its schema."""
    _validate_free_only_canonical_telemetry(canonical)


def validate_external_finalization_telemetry(
    database_path: Path,
    run_root: Path,
    run_id: str,
    *,
    manifest: dict[str, Any] | None = None,
    require_free_only: bool = False,
) -> dict[str, Any]:
    """Validate all immutable telemetry replicas against a path-bound read-only DB."""
    database_path = Path(database_path).resolve()
    run_root = Path(run_root).resolve()
    manifest_path = run_root / "manifest.json"
    if not database_path.is_file():
        raise EvidenceInvariant("TELEMETRY_REPLICA_DATABASE_MISSING")
    if not manifest_path.is_file():
        raise EvidenceInvariant("TELEMETRY_REPLICA_MANIFEST_MISSING")
    try:
        manifest_value = manifest if manifest is not None else json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceInvariant("TELEMETRY_REPLICA_MANIFEST_INVALID") from exc
    if type(manifest_value) is not dict or manifest_value.get("run_id") != run_id:
        raise EvidenceInvariant("TELEMETRY_REPLICA_MANIFEST_IDENTITY")
    try:
        with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as connection:
            canonical = canonical_scheduler_receipt_from_connection(connection, run_id)
            intent_row = connection.execute(
                "SELECT status,finalization_schema_version,artifact_set_sha256,manifest_sha256,telemetry_snapshot_json,telemetry_sha256 "
                "FROM finalization_intent WHERE run_id=?",
                (run_id,),
            ).fetchone()
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceInvariant("TELEMETRY_REPLICA_DATABASE_INVALID") from exc
    if not intent_row:
        raise EvidenceInvariant("TELEMETRY_REPLICA_INTENT_MISSING")
    status, schema_version, artifact_hash, manifest_hash, intent_json, intent_hash = intent_row
    if status != "COMPLETE" or type(schema_version) is not int or schema_version != 3:
        raise EvidenceInvariant("TELEMETRY_REPLICA_INTENT_STATE")
    if (
        type(artifact_hash) is not str or not _is_sha256(artifact_hash)
        or type(manifest_hash) is not str or not _is_sha256(manifest_hash)
        or type(intent_json) is not str or type(intent_hash) is not str
    ):
        raise EvidenceInvariant("TELEMETRY_REPLICA_INTENT_SHAPE")
    canonical_json, canonical_hash = _canonical_json(canonical)
    if require_free_only:
        _validate_free_only_canonical_telemetry(canonical)
    if not _strict_json_equal(manifest_value.get("telemetry"), canonical) or manifest_value.get("telemetry_sha256") != canonical_hash:
        raise EvidenceInvariant("TELEMETRY_REPLICA_MANIFEST")
    try:
        intent_telemetry = json.loads(intent_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EvidenceInvariant("TELEMETRY_REPLICA_INTENT_JSON") from exc
    if not _strict_json_equal(intent_telemetry, canonical) or intent_hash != canonical_hash:
        raise EvidenceInvariant("TELEMETRY_REPLICA_INTENT")
    if manifest_value.get("artifact_set_sha256") != artifact_hash or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_hash:
        raise EvidenceInvariant("TELEMETRY_REPLICA_MANIFEST_HASH")
    artifact_dir = run_root / "output" / "artifacts" / artifact_hash
    files = manifest_value.get("files")
    if not artifact_dir.is_dir() or type(files) is not dict or not files:
        raise EvidenceInvariant("TELEMETRY_REPLICA_ARTIFACT_SET")
    artifact_entries = list(artifact_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in artifact_entries):
        raise EvidenceInvariant("TELEMETRY_REPLICA_ARTIFACT_SET")
    actual_files = {path.name for path in artifact_entries}
    if set(files) != actual_files:
        raise EvidenceInvariant("TELEMETRY_REPLICA_ARTIFACT_SET")
    aggregate: list[str] = []
    for name, info in sorted(files.items()):
        if type(name) is not str or not name or Path(name).name != name or "/" in name or "\\" in name or type(info) is not dict:
            raise EvidenceInvariant("TELEMETRY_REPLICA_ARTIFACT_SET")
        artifact = artifact_dir / name
        try:
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        except OSError as exc:
            raise EvidenceInvariant("TELEMETRY_REPLICA_ARTIFACT_SET") from exc
        if type(info.get("sha256")) is not str or info["sha256"] != digest or type(info.get("bytes")) is not int or info["bytes"] != artifact.stat().st_size:
            raise EvidenceInvariant("TELEMETRY_REPLICA_ARTIFACT_SET")
        aggregate.append(f"{name}:{digest}\n")
    if hashlib.sha256("".join(aggregate).encode("utf-8")).hexdigest() != artifact_hash:
        raise EvidenceInvariant("TELEMETRY_REPLICA_ARTIFACT_SET")
    telemetry_artifact = artifact_dir / Path(config.TELEMETRY_FILE).name
    report_artifact = artifact_dir / Path(config.REPORT_FILE).name
    if not telemetry_artifact.is_file():
        raise EvidenceInvariant("TELEMETRY_REPLICA_IMMUTABLE_MISSING")
    if not report_artifact.is_file():
        raise EvidenceInvariant("TELEMETRY_REPLICA_REPORT_MISSING")
    try:
        artifact_telemetry = json.loads(telemetry_artifact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceInvariant("TELEMETRY_REPLICA_IMMUTABLE") from exc
    if not _strict_json_equal(artifact_telemetry, canonical):
        raise EvidenceInvariant("TELEMETRY_REPLICA_IMMUTABLE")
    try:
        receipt_lines = [
            line.removeprefix("SCHEDULER_RECEIPT_JSON=")
            for line in report_artifact.read_text(encoding="utf-8").splitlines()
            if line.startswith("SCHEDULER_RECEIPT_JSON=")
        ]
    except (OSError, UnicodeError) as exc:
        raise EvidenceInvariant("TELEMETRY_REPLICA_REPORT") from exc
    if receipt_lines != [canonical_json]:
        raise EvidenceInvariant("TELEMETRY_REPLICA_REPORT")
    return {"canonical": canonical, "canonical_json": canonical_json, "canonical_hash": canonical_hash}


def validate_finalization_telemetry(database_path: Path, run_root: Path, run_id: str, *, manifest: dict[str, Any] | None = None, require_free_only: bool = False) -> dict[str, Any]:
    """Public alias for path-bound external finalization telemetry validation."""
    return validate_external_finalization_telemetry(database_path, run_root, run_id, manifest=manifest, require_free_only=require_free_only)


def _validate_finalization_telemetry_replicas(run_id: str, manifest: dict[str, Any], intent: dict[str, Any], artifact_dir: Path) -> None:
    """Compatibility wrapper used by the in-process finalization contract."""
    validate_external_finalization_telemetry(
        Path(config.PROGRESS_DB_FILE), artifact_dir.parents[2], run_id, manifest=manifest,
    )


def validate_finalization_contract(run_id: str, run_root: Path | None = None, *, require_complete: bool = True) -> dict[str, Any]:
    """Validate the finalization receipt before memory drain or success reporting."""
    with closing(_connect()) as connection:
        nonterminal_items = connection.execute(
            "SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET') OR (paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')))",
            (run_id,),
        ).fetchone()[0]
        nonterminal_calls = connection.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')",
            (run_id,),
        ).fetchone()[0]
    if nonterminal_items or nonterminal_calls:
        raise StateTransitionInvariant("finalization scheduler/provider state is not terminal")
    validate_paid_evidence(run_id)
    validate_ledger_equations(run_id, require_terminal=True)
    root = Path(run_root) if run_root is not None else Path(config.RUNS_DIR) / str(run_id)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise EvidenceInvariant("finalization manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    intent = load_finalization_intent(run_id)
    if not intent or intent.get("status") not in {"COMPLETE", "SKIPPED_LEGACY_NO_PLAN"}:
        raise StateTransitionInvariant("finalization intent is not COMPLETE")
    legacy_without_plan = intent.get("status") == "SKIPPED_LEGACY_NO_PLAN"
    if legacy_without_plan:
        with closing(_connect()) as connection:
            row = connection.execute("SELECT context_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        context = json.loads(row[0] or "{}") if row else {}
        schema_version = intent.get("finalization_schema_version")
        if schema_version is None:
            schema_version = 2
        if int(schema_version) != 0 or str((context.get("lineage") or {}).get("type", "")) != "legacy_recovery":
            raise EvidenceInvariant("SKIPPED_LEGACY_NO_PLAN is not valid for this run")
    artifact_hash = str(manifest.get("artifact_set_sha256", ""))
    if not manifest.get("complete") or manifest.get("phase") != "COMPLETE":
        raise EvidenceInvariant("finalization manifest is not COMPLETE")
    if not artifact_hash or artifact_hash != str(intent.get("artifact_set_sha256", "")):
        raise EvidenceInvariant("finalization artifact identity mismatch")
    manifest_receipt = str(intent.get("manifest_sha256", ""))
    if not manifest_receipt or file_hash(manifest_path) != manifest_receipt:
        raise EvidenceInvariant("finalization manifest receipt hash mismatch")
    artifact_dir = root / "output" / "artifacts" / artifact_hash
    files = manifest.get("files")
    if not artifact_dir.is_dir() or not isinstance(files, dict) or not files:
        raise EvidenceInvariant("finalization immutable artifacts are missing")
    aggregate = []
    for name, info in sorted(files.items()):
        artifact = artifact_dir / str(name)
        if artifact.parent != artifact_dir or not artifact.is_file() or artifact.stat().st_size != int(info.get("bytes", -1)):
            raise EvidenceInvariant(f"finalization artifact metadata mismatch: {name}")
        digest = file_hash(artifact)
        if digest != str(info.get("sha256", "")):
            raise EvidenceInvariant(f"finalization artifact hash mismatch: {name}")
        aggregate.append(f"{artifact.name}:{digest}\n")
    if hashlib.sha256("".join(aggregate).encode("utf-8")).hexdigest() != artifact_hash:
        raise EvidenceInvariant("finalization aggregate artifact hash mismatch")
    if not legacy_without_plan:
        _validate_finalization_telemetry_replicas(run_id, manifest, intent, artifact_dir)
    with closing(_connect()) as connection:
        phase = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
        expected_phase = "COMPLETE" if require_complete else "FINALIZING"
        if not phase or str(phase[0]) != expected_phase:
            raise StateTransitionInvariant("finalization database phase mismatch")
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
                raise StateTransitionInvariant("legacy finalization scheduler/provider state is not terminal")
            return {"run_id": run_id, "artifact_set_sha256": artifact_hash, "memory_plan_count": 0, "manifest_sha256": file_hash(manifest_path)}
        if int(intent.get("memory_plan_committed", 0)) != 1:
            raise StateTransitionInvariant("finalization memory plan is not committed")
        if len(entries) != int(intent.get("memory_plan_count", -1)) or plan_hash != str(intent.get("memory_plan_sha256", "")):
            raise StateTransitionInvariant("finalization memory plan exact receipt mismatch")
        nonterminal = connection.execute(
            "SELECT COUNT(*) FROM run_items WHERE run_id=? AND (free_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET') OR (paid_required=1 AND paid_state IN ('PENDING','RUNNING','UNKNOWN','BLOCKED_BUDGET')))",
            (run_id,),
        ).fetchone()[0]
        unresolved = connection.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND state IN ('RESERVED','RUNNING')",
            (run_id,),
        ).fetchone()[0]
        if nonterminal or unresolved:
            raise StateTransitionInvariant("finalization scheduler/provider state is not terminal")
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
            raise EvidenceInvariant("memory outbox exact receipt mismatch")
        if not existing:
            for receipt_key, payload in entries:
                connection.execute("INSERT INTO memory_outbox(run_id,receipt_key,payload,created_at) VALUES(?,?,?,?)", (run_id, receipt_key, payload, now))
        connection.commit()


def commit_finalization_memory_plan(*, run_id: str, generation: str,
                                    result_snapshot_sha256: str,
                                    telemetry_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Commit the already-enqueued outbox plan in its own transaction."""
    canonical = canonical_scheduler_receipt(run_id)
    if telemetry_snapshot is not None and _json_safe(telemetry_snapshot) != canonical:
        raise EvidenceInvariant("memory plan telemetry differs from canonical scheduler receipt")
    telemetry_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        intent = connection.execute(
            "SELECT generation,result_snapshot_sha256,status,memory_plan_sha256,memory_plan_count,memory_plan_committed,output_context_json,telemetry_snapshot_json,telemetry_sha256 FROM finalization_intent WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if not intent or str(intent[0]) != str(generation) or str(intent[1]) != str(result_snapshot_sha256):
            connection.rollback()
            raise EvidenceInvariant("finalization memory plan identity changed")
        if str(intent[2]) not in {"STARTED", "ARTIFACT_READY"}:
            connection.rollback()
            raise StateTransitionInvariant("finalization memory plan is not committable")
        if str(intent[7]) != telemetry_json or str(intent[8]) != hashlib.sha256(telemetry_json.encode("utf-8")).hexdigest():
            connection.rollback()
            raise EvidenceInvariant("memory plan telemetry receipt changed after freeze")
        entries, plan_hash = _outbox_plan(connection, run_id)
        if int(intent[5] or 0) == 1:
            if len(entries) != int(intent[4]) or plan_hash != str(intent[3]):
                connection.rollback()
                raise EvidenceInvariant("finalization memory plan exact receipt mismatch")
            connection.commit()
            return {"memory_plan_sha256": plan_hash, "memory_plan_count": len(entries), "memory_plan_committed": True}
        context = json.loads(str(intent[6] or "{}"))
        context["telemetry_snapshot"] = canonical
        context["memory_plan_sha256"] = plan_hash
        context["memory_plan_count"] = len(entries)
        connection.execute(
            "UPDATE finalization_intent SET memory_plan_sha256=?,memory_plan_count=?,memory_plan_committed=1,output_context_json=? WHERE run_id=? AND status IN ('STARTED','ARTIFACT_READY')",
            (plan_hash, len(entries), json.dumps(_json_safe(context), ensure_ascii=False), run_id),
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
                          operation: str = "", query_fingerprint: str = "",
                          execution_generation: int = 0) -> str | None:
    if provider not in CANONICAL_PROVIDERS:
        raise LedgerInvariant(f"unknown provider: {provider}")
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
            raise StateTransitionInvariant("provider reservation requires a paid RUNNING item")
        scoped_flight = bool(str(query_fingerprint))
        if scoped_flight != (int(execution_generation) > 0):
            connection.rollback()
            raise StateTransitionInvariant("provider reservation flight identity is incomplete")
        if scoped_flight:
            flight = connection.execute(
                "SELECT state,execution_generation FROM provider_query_flights WHERE run_id=? AND provider=? AND query_fingerprint=?",
                (run_id, provider, str(query_fingerprint)),
            ).fetchone()
            if not flight or str(flight[0]) != "RUNNING" or int(flight[1]) != int(execution_generation):
                connection.rollback()
                raise StateTransitionInvariant("provider reservation requires the active query generation")
            duplicate = connection.execute(
                "SELECT 1 FROM provider_calls c JOIN paid_attempt_calls l "
                "ON l.run_id=c.run_id AND l.provider=c.provider AND l.provider_call_id=c.call_id "
                "WHERE c.run_id=? AND c.provider=? AND c.item_index=? AND c.phase=? AND c.request_fingerprint=? "
                "AND l.query_fingerprint=? AND l.execution_generation=? LIMIT 1",
                (run_id, provider, int(item_index), phase, request_fingerprint,
                 str(query_fingerprint), int(execution_generation)),
            ).fetchone()
        else:
            duplicate = connection.execute(
                "SELECT 1 FROM provider_calls WHERE run_id=? AND provider=? AND item_index=? AND phase=? AND request_fingerprint=? LIMIT 1",
                (run_id, provider, int(item_index), phase, request_fingerprint),
            ).fetchone()
        if duplicate:
            connection.rollback()
            return None
        usage = connection.execute("SELECT reserved,completed,failed,effective_limit,reserved_total,unknown FROM provider_usage WHERE run_id=? AND provider=?", (run_id, provider)).fetchone()
        if not usage:
            connection.rollback()
            raise LedgerInvariant(f"provider budget not initialized: {provider}")
        if int(effective_limit) != int(usage[3]):
            connection.rollback()
            raise ResumeInvariant(f"provider budget changed during run: {provider}")
        actual = {str(state): int(count) for state, count in connection.execute("SELECT state,COUNT(*) FROM provider_calls WHERE run_id=? AND provider=? GROUP BY state", (run_id, provider)).fetchall()}
        if int(usage[0]) != actual.get("RESERVED", 0) + actual.get("RUNNING", 0) or int(usage[1]) != actual.get("DONE", 0) or int(usage[2]) != actual.get("FAILED", 0) or int(usage[5]) != actual.get("UNKNOWN", 0) or int(usage[4]) != sum(actual.values()):
            connection.rollback()
            raise LedgerInvariant(f"provider ledger counters are inconsistent: {provider}")
        if int(usage[4]) >= int(usage[3]):
            connection.rollback()
            return None
        attempt = connection.execute("SELECT attempt_number FROM paid_attempts WHERE run_id=? AND item_index=? AND phase='PAID' AND result='RUNNING' ORDER BY attempt_number DESC LIMIT 1", (run_id, int(item_index))).fetchone()
        if scoped_flight and not attempt:
            connection.rollback()
            raise StateTransitionInvariant("scoped provider reservation requires an active paid attempt")
        connection.execute("UPDATE provider_usage SET reserved_total=reserved_total+1,reserved=reserved+1 WHERE run_id=? AND provider=?", (run_id, provider))
        connection.execute(
            "INSERT INTO provider_calls(run_id,call_id,provider,item_index,phase,operation,request_fingerprint,state,result_ref,created_at,updated_at,flight_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, call_id, provider, item_index, phase, operation, request_fingerprint, "RESERVED", "", now, now, str(query_fingerprint)),
        )
        if attempt:
            paid_attempt_id = hashlib.sha256(f"{run_id}\0{int(item_index)}\0{int(attempt[0])}\0PAID".encode("utf-8")).hexdigest()
            connection.execute("INSERT INTO paid_attempt_calls(run_id,item_index,attempt_number,phase,call_id,paid_attempt_id,provider_call_id,provider,query_fingerprint,execution_generation,relation) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, int(item_index), int(attempt[0]), "PAID", call_id, paid_attempt_id, call_id, provider, str(query_fingerprint), int(execution_generation) if scoped_flight else 1, "OWNER"))
        connection.commit()
    return call_id


def start_provider_call(call_id: str) -> None:
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE provider_calls SET state='RUNNING',updated_at=? WHERE call_id=? AND state='RESERVED'",
            (datetime.now(timezone.utc).isoformat(timespec="microseconds"), str(call_id)),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise LedgerInvariant("provider call must transition RESERVED to RUNNING exactly once")
        connection.commit()


def mark_provider_call_http_started(*, call_id: str, attempt_ordinal: int, flight_fingerprint: str = "") -> None:
    ordinal = int(attempt_ordinal)
    if ordinal < 1:
        raise LedgerInvariant("provider HTTP attempt ordinal must be positive")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE provider_calls SET http_started_at=?,attempt_ordinal=?,flight_fingerprint=?,updated_at=? WHERE call_id=? AND state='RUNNING' AND http_started_at='' AND flight_fingerprint IN ('',?)",
            (now, ordinal, str(flight_fingerprint), now, str(call_id), str(flight_fingerprint)),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise LedgerInvariant("provider HTTP start marker must be written exactly once on RUNNING call")
        connection.commit()


def bind_provider_call_transport_receipt(*, call_id: str, endpoint_sha256: str, request_shape_sha256: str) -> None:
    if not _is_sha256(endpoint_sha256) or not _is_sha256(request_shape_sha256):
        raise LedgerInvariant("provider transport receipt requires SHA-256 identities")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE provider_calls SET endpoint_sha256=?,request_shape_sha256=?,updated_at=? WHERE call_id=? AND state='RUNNING' AND http_started_at<>'' AND endpoint_sha256='' AND request_shape_sha256=''",
            (str(endpoint_sha256), str(request_shape_sha256), now, str(call_id)),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise LedgerInvariant("provider transport receipt must bind exactly once to an HTTP-started RUNNING call")
        connection.commit()


def complete_provider_call(*, call_id: str, state: str, result_ref: str = "") -> None:
    state = {"COMPLETED": "DONE", "EMPTY": "DONE", "CACHE_HIT": "DONE"}.get(str(state).upper(), str(state).upper())
    if state not in {"DONE", "FAILED", "UNKNOWN"}:
        raise LedgerInvariant(f"invalid provider call state: {state}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        call = connection.execute("SELECT run_id,provider,state FROM provider_calls WHERE call_id=?", (call_id,)).fetchone()
        if not call:
            connection.rollback()
            raise LedgerInvariant("provider completion references unknown call")
        if call[2] in {"DONE", "FAILED", "UNKNOWN"}:
            connection.rollback()
            raise LedgerInvariant("provider call completed more than once")
        connection.execute("UPDATE provider_calls SET state=?,result_ref=?,updated_at=? WHERE call_id=?", (state, result_ref, now, call_id))
        usage = connection.execute("SELECT reserved FROM provider_usage WHERE run_id=? AND provider=?", (call[0], call[1])).fetchone()
        if not usage or int(usage[0]) <= 0:
            connection.rollback()
            raise LedgerInvariant("provider aggregate reserved counter is negative")
        connection.execute("UPDATE provider_usage SET reserved=reserved-1,completed=completed+?,failed=failed+?,unknown=unknown+? WHERE run_id=? AND provider=?", (int(state == "DONE"), int(state == "FAILED"), int(state == "UNKNOWN"), call[0], call[1]))
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
            usage = connection.execute("SELECT reserved FROM provider_usage WHERE run_id=? AND provider=?", (run_id, provider)).fetchone()
            if not usage or int(usage[0]) <= 0:
                connection.rollback()
                raise LedgerInvariant("provider aggregate reserved counter is negative during reconcile")
            connection.execute("UPDATE provider_usage SET reserved=reserved-1,unknown=unknown+1 WHERE run_id=? AND provider=?", (run_id, provider))
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
