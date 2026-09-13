"""Shared fail-closed checks for explicit free-only finalization artifacts."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import copy
from pathlib import Path


PROVIDERS = ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")
SKIP_REASON = "disabled_by_explicit_free_only_finalization"
FREE_SEARCH_EXECUTION_NAMESPACE = "free_search_execution_v1"
DNS_ADDRESS_NAMESPACE = "dns_address_v1"
TRACE_SCHEMA_VERSION = 1


def validate_manifest(manifest: dict, *, require_complete: bool = True) -> dict:
    config = manifest.get("run_config")
    if not isinstance(config, dict):
        raise ValueError("free_only_run_config_missing")
    if config.get("finalize_without_paid") is not True:
        raise ValueError("free_only_finalization_not_enabled")
    if (
        config.get("paid_enabled") is not False
        or manifest.get("paid_enabled") is True
        or ("paid_enabled" in manifest and manifest.get("paid_enabled") is not False)
        or ("finalize_without_paid" in manifest and manifest.get("finalize_without_paid") is not True)
    ):
        raise ValueError("free_only_paid_enabled")
    budgets = config.get("budgets")
    if set(budgets or {}) != set(PROVIDERS) or any(
        isinstance(value, bool) or not isinstance(value, int) or value != 0
        for value in (budgets or {}).values()
    ):
        raise ValueError("free_only_paid_budget_nonzero")
    if require_complete and (
        manifest.get("complete") is not True
        or manifest.get("phase") != "COMPLETE"
        or manifest.get("finalized") is not True
        or manifest.get("status") != "complete_free_only"
    ):
        raise ValueError("free_only_manifest_not_complete")
    return config


def expected_offline_run_config(live_config: dict) -> dict:
    """Return the only permitted cache-mode transformation for replay."""
    if not isinstance(live_config, dict):
        raise ValueError("offline_config_source_invalid")
    expected = copy.deepcopy(live_config)
    if expected.get("search_cache_mode") != "refresh" or expected.get("crawl_cache_mode") != "refresh":
        raise ValueError("offline_config_source_modes_invalid")
    settings = expected.get("effective_settings")
    if not isinstance(settings, dict):
        raise ValueError("offline_config_effective_settings_invalid")
    expected["search_cache_mode"] = "replay"
    expected["crawl_cache_mode"] = "replay"
    settings["search_cache_mode"] = "replay"
    settings["crawl_cache_mode"] = "replay"
    return expected


def validate_manifest_config_sha(manifest: dict) -> None:
    """Verify a manifest's own RunConfig hash, without comparing run modes."""
    from modules import run_context

    config = manifest.get("run_config")
    recorded = str(manifest.get("config_sha256") or "")
    if not recorded or run_context.RunConfig.from_dict(config).sha256 != recorded:
        raise ValueError("free_only_config_sha_mismatch")


def validate_database(path: Path, run_id: str, expected_count: int) -> dict[str, int]:
    try:
        with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as connection:
            phase = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_id,)).fetchone()
            items = connection.execute(
                "SELECT paid_required,paid_state,paid_attempts FROM run_items WHERE run_id=? ORDER BY item_index",
                (run_id,),
            ).fetchall()
            results = connection.execute("SELECT payload FROM results WHERE run_id=? ORDER BY item_index", (run_id,)).fetchall()
            counters = {
                "provider_calls": connection.execute("SELECT COUNT(*) FROM provider_calls WHERE run_id=?", (run_id,)).fetchone()[0],
                "paid_attempts": connection.execute("SELECT COUNT(*) FROM paid_attempts WHERE run_id=?", (run_id,)).fetchone()[0],
                "paid_attempt_calls": connection.execute("SELECT COUNT(*) FROM paid_attempt_calls WHERE run_id=?", (run_id,)).fetchone()[0],
                "provider_query_flights": connection.execute("SELECT COUNT(*) FROM provider_query_flights WHERE run_id=?", (run_id,)).fetchone()[0],
                "flight_consumers": connection.execute("SELECT COUNT(*) FROM provider_query_flight_consumers WHERE run_id=?", (run_id,)).fetchone()[0],
                "paid_budget_blocks": connection.execute("SELECT COUNT(*) FROM provider_budget_blocks WHERE run_id=? AND provider<>'ddgs'", (run_id,)).fetchone()[0],
            }
            usage = connection.execute(
                "SELECT configured_limit,effective_limit,reserved,completed,failed,unknown FROM provider_usage WHERE run_id=?",
                (run_id,),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(f"free_only_database_invalid:{type(exc).__name__}") from exc
    if not phase or str(phase[0]) != "COMPLETE":
        raise ValueError("free_only_database_not_complete")
    if len(items) != int(expected_count) or len(results) != int(expected_count):
        raise ValueError("free_only_database_population_mismatch")
    if any(int(required) != 0 or str(state) != "NOT_REQUIRED" or int(attempts) != 0 for required, state, attempts in items):
        raise ValueError("free_only_paid_item_state_invalid")
    if any(int(value) != 0 for value in counters.values()):
        raise ValueError(f"free_only_paid_activity_nonzero:{counters}")
    if any(any(int(value or 0) != 0 for value in row) for row in usage) or len(usage) != len(PROVIDERS):
        raise ValueError("free_only_provider_usage_nonzero_or_incomplete")
    for (payload_text,) in results:
        try:
            payload = json.loads(str(payload_text))
        except json.JSONDecodeError as exc:
            raise ValueError("free_only_result_payload_invalid") from exc
        recommended = payload.get("paid_recommended") is True
        if payload.get("paid_required") is not False or payload.get("paid_state") != "NOT_REQUIRED":
            raise ValueError("free_only_result_state_invalid")
        if not isinstance(payload.get("paid_recommended"), bool):
            raise ValueError("free_only_paid_recommendation_missing")
        if recommended and payload.get("paid_skipped_reason") != SKIP_REASON:
            raise ValueError("free_only_paid_skip_reason_invalid")
    return {key: int(value) for key, value in counters.items()}


def validate_behavioral_replay(path: Path, run_id: str, ordered_source_record_ids: list[str]) -> dict[str, int]:
    """Validate typed free-search/DNS replay closure against the live ledger."""
    try:
        with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as connection:
            item_rows = connection.execute(
                "SELECT item_index,source_record_id FROM run_items WHERE run_id=? ORDER BY item_index",
                (run_id,),
            ).fetchall()
            trace_rows = connection.execute(
                "SELECT store,namespace,key_sha256,schema_version,value_json FROM replay_entries WHERE run_id=? AND namespace IN (?,?) ORDER BY namespace,key_sha256",
                (run_id, "free_search_execution_v1", "dns_address_v1"),
            ).fetchall()
            attempt_rows = connection.execute(
                "SELECT item_index,bucket,provider,query_fingerprint,attempt_ordinal,state,error_class FROM free_provider_attempts WHERE run_id=? ORDER BY item_index,bucket,provider,query_fingerprint,attempt_ordinal",
                (run_id,),
            ).fetchall()
            usage_rows = connection.execute(
                "SELECT item_index,discovery_logical_used,targeted_logical_used,discovery_physical_used,targeted_physical_used,logical_blocked,physical_blocked FROM free_query_usage WHERE run_id=? ORDER BY item_index",
                (run_id,),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(f"behavioral_replay_database_invalid:{type(exc).__name__}") from exc

    id_to_index = {str(source_id): int(index) for index, source_id in item_rows}
    if len(item_rows) != len(ordered_source_record_ids) or [str(row[1]) for row in item_rows] != list(ordered_source_record_ids):
        raise ValueError("behavioral_replay_source_record_id_order_invalid")
    expected_attempts: list[tuple] = []
    logical_counts: dict[tuple[int, str], int] = {}
    blocked_logical_by_item: dict[int, int] = {}
    blocked_physical_by_item: dict[int, int] = {}
    dns_count = 0
    seen_trace_keys: set[str] = set()
    for store, namespace, key_sha, schema_version, value_json in trace_rows:
        if store != "replay" or int(schema_version) != TRACE_SCHEMA_VERSION:
            raise ValueError("behavioral_replay_namespace_invalid")
        try:
            value = json.loads(value_json)
        except json.JSONDecodeError as exc:
            raise ValueError("behavioral_replay_record_invalid_json") from exc
        if not isinstance(value, dict):
            raise ValueError("behavioral_replay_record_not_object")
        if namespace == FREE_SEARCH_EXECUTION_NAMESPACE:
            source_id = str(value.get("source_record_id") or "")
            bucket = str(value.get("bucket") or "")
            query_fingerprint = str(value.get("query_fingerprint") or "")
            if (
                not source_id or source_id not in id_to_index
                or bucket not in {"discovery", "targeted"}
                or len(query_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in query_fingerprint)
                or value.get("provider") != "ddgs"
            ):
                raise ValueError("behavioral_replay_execution_identity_invalid")
            expected_key = hashlib.sha256(f"{source_id}\0{bucket}\0{query_fingerprint}".encode("utf-8")).hexdigest()
            if expected_key != str(key_sha):
                raise ValueError("behavioral_replay_execution_key_mismatch")
            if expected_key in seen_trace_keys:
                raise ValueError("behavioral_replay_execution_duplicate")
            seen_trace_keys.add(expected_key)
            logical = value.get("logical_reservation")
            attempts = value.get("backend_attempts")
            result = value.get("result")
            if not isinstance(logical, dict) or not isinstance(attempts, list) or not isinstance(result, dict):
                raise ValueError("behavioral_replay_execution_shape_invalid")
            logical_result = str(logical.get("result") or "")
            if logical_result not in {"ACCEPTED", "BLOCKED"}:
                raise ValueError("behavioral_replay_logical_result_invalid")
            if logical_result == "ACCEPTED":
                logical_counts[(id_to_index[source_id], bucket)] = logical_counts.get((id_to_index[source_id], bucket), 0) + 1
            else:
                item_index = id_to_index[source_id]
                blocked_logical_by_item[item_index] = blocked_logical_by_item.get(item_index, 0) + 1
            for attempt in attempts:
                if not isinstance(attempt, dict) or not attempt.get("backend"):
                    raise ValueError("behavioral_replay_backend_attempt_invalid")
                reservation = str(attempt.get("result") or "")
                if reservation not in {"ACCEPTED", "BLOCKED"}:
                    raise ValueError("behavioral_replay_backend_reservation_invalid")
                if reservation == "BLOCKED":
                    if attempt.get("outcome") != "BLOCKED":
                        raise ValueError("behavioral_replay_blocked_attempt_outcome_invalid")
                    item_index = id_to_index[source_id]
                    blocked_physical_by_item[item_index] = blocked_physical_by_item.get(item_index, 0) + 1
                    continue
                outcome = str(attempt.get("outcome") or "")
                if outcome not in {"DONE", "FAILED"}:
                    raise ValueError("behavioral_replay_attempt_outcome_invalid")
                expected_attempts.append((
                    id_to_index[source_id], bucket, str(attempt["backend"]).casefold(),
                    query_fingerprint, int(attempt.get("attempt_ordinal", 0)),
                    outcome, str(attempt.get("error_class") or ""),
                ))
        elif namespace == DNS_ADDRESS_NAMESPACE:
            source_id = str(value.get("source_record_id") or "")
            domain = str(value.get("domain") or "")
            if (
                not source_id or source_id not in id_to_index
                or not domain or domain != domain.casefold()
                or not isinstance(value.get("has_address"), bool)
                or value.get("schema_version") != TRACE_SCHEMA_VERSION
            ):
                raise ValueError("behavioral_replay_dns_record_invalid")
            expected_key = hashlib.sha256(f"{source_id}\0{domain}".encode("utf-8")).hexdigest()
            if expected_key != str(key_sha):
                raise ValueError("behavioral_replay_dns_key_mismatch")
            dns_count += 1
    actual_attempts = [
        (int(item_index), str(bucket), str(provider), str(query_fingerprint), int(attempt_ordinal), str(state), str(error_class or ""))
        for item_index, bucket, provider, query_fingerprint, attempt_ordinal, state, error_class in attempt_rows
    ]
    if sorted(expected_attempts) != actual_attempts:
        raise ValueError("behavioral_replay_backend_trace_ledger_mismatch")
    usage_map = {int(row[0]): row for row in usage_rows}
    if set(usage_map) != set(range(len(ordered_source_record_ids))):
        raise ValueError("behavioral_replay_usage_population_invalid")
    for item_index, row in usage_map.items():
        if logical_counts.get((item_index, "discovery"), 0) != int(row[1]) or logical_counts.get((item_index, "targeted"), 0) != int(row[2]):
            raise ValueError("behavioral_replay_logical_trace_ledger_mismatch")
        if sum(1 for attempt in expected_attempts if attempt[0] == item_index and attempt[1] == "discovery") != int(row[3]) or sum(1 for attempt in expected_attempts if attempt[0] == item_index and attempt[1] == "targeted") != int(row[4]):
            raise ValueError("behavioral_replay_physical_trace_ledger_mismatch")
        if (
            blocked_logical_by_item.get(item_index, 0) != int(row[5])
            or blocked_physical_by_item.get(item_index, 0) != int(row[6])
        ):
            raise ValueError("behavioral_replay_block_counter_mismatch")
    return {
        "free_search_execution_count": len(seen_trace_keys),
        "free_search_attempt_count": len(expected_attempts),
        "dns_address_count": dns_count,
        "blocked_logical_count": sum(blocked_logical_by_item.values()),
        "blocked_physical_count": sum(blocked_physical_by_item.values()),
    }
