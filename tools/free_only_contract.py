"""Shared fail-closed checks for explicit free-only finalization artifacts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


PROVIDERS = ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")
SKIP_REASON = "disabled_by_explicit_free_only_finalization"


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
