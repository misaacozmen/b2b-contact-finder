from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


REPO = Path(__file__).resolve().parents[1]
EXPECTED_SKIPS = {
    "tests/test_prune_workspace_safety.py::test_prune_rejects_root_symlink_without_touching_target",
    "tests/test_reconcile_recovery_output.py::test_reconciled_output_has_exact_source_coverage_and_order",
    "tests/test_reconcile_recovery_output.py::test_reconciled_selection_and_delivery_partition_are_exact",
    "tests/test_reconcile_recovery_output.py::test_known_collisions_are_review_only_and_legacy_review_is_not_promoted",
    "tests/test_reconcile_recovery_output.py::test_second_run_verifies_without_replacing_and_detects_tampering",
    "tests/test_validation_workspace.py::test_reparse_point_in_selected_tree_fails_closed[modules]",
    "tests/test_validation_workspace.py::test_reparse_point_in_selected_tree_fails_closed[tools]",
}
PAID_PROVIDER_HOSTS = {
    "brightdata": ("brightdata.com",),
    "google_places": ("googleapis.com",),
    "brandfetch": ("brandfetch.io",),
    "hunter": ("hunter.io",),
    "linkedin": ("linkedin.com",),
    "llm": ("openrouter.ai",),
}
PAID_HOST_MARKERS = tuple(host for hosts in PAID_PROVIDER_HOSTS.values() for host in hosts)
CANONICAL_PROVIDER_SET = {"brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm"}
EXPECTED_SCHEDULER_SCHEMA_VERSION = 12
REQUIRED_RECEIPT_TRIGGERS = {
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
}
K6_EXPECTED_CLI_EXITS = {
    "test_run_config_requires_exact_canonical_provider_budget_set": [22],
    "test_resume_query_plan_or_config_drift_returns_cli_22_before_network": [22],
    "test_resume_llm_budget_drift_is_rejected_before_saved_credentials": [22],
}
SCENARIO_CONTRACT_VERSION = 3
SCENARIO_CONTRACT = {
    "authorized_free_paid": {"phase": "COMPLETE", "outcome": "COMPLETE", "http": 1, "path": ["->FREE", "FREE->PAID", "PAID->FINALIZING", "FINALIZING->COMPLETE"], "call_states": {"DONE": 1}},
    "unauthorized_handoff": {"phase": "PAID", "outcome": "PAID_PENDING_APPROVAL", "http": 0, "path": ["->FREE", "FREE->PAID"], "call_states": {}},
    "unknown_item_stop": {"phase": "PAID", "outcome": "PAID_MANUAL_AUTHORIZATION_REVIEW_REQUIRED", "http": 1, "path": ["->FREE", "FREE->PAID"], "call_states": {"UNKNOWN": 1}},
    "singleflight_success": {"phase": "COMPLETE", "outcome": "COMPLETE", "http": 1, "path": ["->FREE", "FREE->PAID", "PAID->FINALIZING", "FINALIZING->COMPLETE"], "call_states": {"DONE": 1}, "relations": {"OWNER": 1, "INHERITED": 7}},
    "singleflight_failure": {"phase": "COMPLETE", "outcome": "COMPLETE", "http": 1, "path": ["->FREE", "FREE->PAID", "PAID->FINALIZING", "FINALIZING->COMPLETE"], "call_states": {"FAILED": 1}, "relations": {"OWNER": 1, "INHERITED": 7}},
    "heartbeat_throttle_backoff": {"phase": "COMPLETE", "outcome": "COMPLETE", "http": 2, "path": ["->FREE", "FREE->PAID", "PAID->FINALIZING", "FINALIZING->COMPLETE"], "call_states": {"DONE": 1, "FAILED": 1}},
    "free_caps": {"phase": "PAID", "outcome": "PAID_PENDING_APPROVAL", "http": 0, "path": ["->FREE", "FREE->PAID"], "call_states": {}, "free": (10, 6, 4, 10, 6, 4)},
    "fresh_handoff_resume": {"phase": "COMPLETE", "outcome": "COMPLETE", "http": 2, "path": ["PAID->FINALIZING", "FINALIZING->COMPLETE"], "call_states": {"DONE": 2}},
}
SCENARIO_NODES = {
    "authorized_free_paid": "test_real_authorized_e2e_uses_main_process_company_and_recording_transport",
    "unauthorized_handoff": "test_real_fresh_run_without_authorization_seals_handoff",
    "unknown_item_stop": "test_real_unknown_e2e_has_one_total_paid_call_without_test_side_break",
    "singleflight_success": "test_singleflight_eight_workers_success_make_one_real_post",
    "singleflight_failure": "test_single_failed_flight_with_eight_followers_counts_one_circuit_failure",
    "heartbeat_throttle_backoff": "test_heartbeat_covers_throttle_http_decode_and_retry_backoff",
    "free_caps": "test_real_free_caps_are_six_four_and_ten",
    "fresh_handoff_resume": "test_real_fresh_handoff_resume_preserves_limit_and_plan",
}
REQUIRED_NODES = (
    "test_unknown_each_paid_adapter_stops_all_later_paid_calls", "test_unknown_checkpoint_active_resume_is_valid_and_no_network_occurs",
    "test_runtime_unknown_terminalization_is_canonical_for_every_provider", "test_llm_scheduler_invariant_is_never_converted_to_failed_dict",
    "test_paid_adapters_do_not_mark_done_before_semantic_validation", "test_paid_adapter_cache_write_failure_does_not_change_terminal_result", "test_openrouter_invalid_structured_verdict_is_failed_not_done",
    "test_run_config_requires_exact_canonical_provider_budget_set", "test_provider_set_missing_or_extra_row_blocks_telemetry_and_finalization", "test_current_schema_process_restart_does_not_heal_provider_aggregate_corruption", "test_complete_checkpoint_schema_open_is_byte_for_byte_read_only", "test_incomplete_v11_schema_migrates_transport_receipts_to_v12",
    "test_legacy_free_migration_reconciles_exact_bucket_state_multiset", "test_relation_tables_reject_orphan_and_cross_scope_rows_with_foreign_keys",
    "test_paid_evidence_unknown_has_precedence_over_done", "test_paid_evidence_done_rejects_blocked_budget_outcome", "test_paid_no_call_evidence_requires_exact_zero_call_or_inherited_receipt",
    "test_expired_flight_failed_then_done_receipt_reconciles_done", "test_reclaimed_flight_preserves_all_prior_call_ids_append_only",
    "test_provider_query_execution_generation_is_monotonic_and_relational", "test_new_provider_query_generation_requires_matching_terminal_receipt", "test_new_provider_query_generation_accepts_reconciled_done_receipt", "test_non_atomic_flight_finish_rejects_done", "test_generation_receipts_reject_cross_generation_mutation", "test_atomic_flight_success_rejects_unbound_provider_call", "test_volatile_same_query_independent_failures_open_circuit", "test_singleflight_clocked_heartbeat_covers_every_wait_region",
    "test_real_fresh_handoff_resume_preserves_durable_ordered_query_plan", "test_paid_targeted_query_plan_is_append_only_across_real_rounds_and_resume", "test_resume_query_plan_or_config_drift_returns_cli_22_before_network",
    "test_resume_llm_budget_drift_is_rejected_before_saved_credentials",
    "test_finalization_rejects_each_telemetry_replica_drift", "test_report_rejects_missing_provider_set",
    "test_windows_junction_retarget_changes_protected_manifest", "test_evidence_capture_rejects_missing_journal_manifest_or_artifact",
    "test_recording_transport_uses_production_attempt_and_fingerprints", "test_audit_scenario_validator_rejects_each_tampered_required_artifact", "test_audit_scenario_validator_rejects_rehashed_semantic_command_forgery", "test_audit_scenario_validator_rejects_rehashed_transport_and_relational_forgery",
    "test_offline_paid_network_guard_fails_on_every_default_transport_fallthrough", "test_root_artifact_hash_is_a_marker_gate_and_includes_nested_manifests",
    "test_network_receipt_validator_derives_zero_external_calls_and_rejects_forgery", "test_k6_receipt_validator_rejects_missing_duplicate_and_forged_cases", "test_contract_matrix_has_no_fallback_and_replays_every_receipt", "test_k6_uses_real_negative_resume_drift_not_positive_resume_node",
)
_ADAPTER_ENTRIES = {
    "modules/search.py:_brightdata_text", "modules/google_places.py:search_company",
    "modules/company_resolvers.py:brandfetch_domains", "modules/company_resolvers.py:hunter_domains",
    "modules/hunter.py:find_domain_emails",
    "modules/linkedin_company.py:_find_company_url", "modules/linkedin_company.py:_scrape",
    "modules/llm_arbiter.py:generate",
}
REQUIREMENT_ENTRYPOINTS = {
    "test_unknown_each_paid_adapter_stops_all_later_paid_calls": _ADAPTER_ENTRIES,
    "test_unknown_checkpoint_active_resume_is_valid_and_no_network_occurs": {"main.py:run"},
    "test_runtime_unknown_terminalization_is_canonical_for_every_provider": {"modules/runtime.py:complete_api"},
    "test_llm_scheduler_invariant_is_never_converted_to_failed_dict": {"modules/llm_arbiter.py:arbitrate"},
    "test_paid_adapters_do_not_mark_done_before_semantic_validation": _ADAPTER_ENTRIES,
    "test_paid_adapter_cache_write_failure_does_not_change_terminal_result": _ADAPTER_ENTRIES,
    "test_openrouter_invalid_structured_verdict_is_failed_not_done": {"modules/llm_arbiter.py:generate"},
    "test_run_config_requires_exact_canonical_provider_budget_set": {"modules/run_context.py:from_dict"},
    "test_provider_set_missing_or_extra_row_blocks_telemetry_and_finalization": {"modules/checkpoint.py:derive_telemetry"},
    "test_current_schema_process_restart_does_not_heal_provider_aggregate_corruption": {"modules/checkpoint.py:validate_ledger_equations"},
    "test_complete_checkpoint_schema_open_is_byte_for_byte_read_only": {"modules/checkpoint.py:initialize_schema"},
    "test_incomplete_v11_schema_migrates_transport_receipts_to_v12": {"modules/checkpoint.py:initialize_schema"},
    "test_legacy_free_migration_reconciles_exact_bucket_state_multiset": {"modules/checkpoint.py:initialize_schema"},
    "test_relation_tables_reject_orphan_and_cross_scope_rows_with_foreign_keys": {"modules/checkpoint.py:initialize_schema"},
    "test_paid_evidence_unknown_has_precedence_over_done": {"modules/checkpoint.py:validate_paid_evidence"},
    "test_paid_evidence_done_rejects_blocked_budget_outcome": {"modules/checkpoint.py:validate_paid_evidence"},
    "test_paid_no_call_evidence_requires_exact_zero_call_or_inherited_receipt": {"modules/checkpoint.py:validate_paid_evidence"},
    "test_expired_flight_failed_then_done_receipt_reconciles_done": {"modules/checkpoint.py:resolve_expired_provider_query_flight"},
    "test_reclaimed_flight_preserves_all_prior_call_ids_append_only": {"modules/checkpoint.py:resolve_expired_provider_query_flight"},
    "test_provider_query_execution_generation_is_monotonic_and_relational": {"modules/checkpoint.py:start_new_provider_query_execution"},
    "test_new_provider_query_generation_requires_matching_terminal_receipt": {"modules/checkpoint.py:start_new_provider_query_execution"},
    "test_new_provider_query_generation_accepts_reconciled_done_receipt": {"modules/checkpoint.py:start_new_provider_query_execution"},
    "test_non_atomic_flight_finish_rejects_done": {"modules/checkpoint.py:finish_provider_query_flight"},
    "test_generation_receipts_reject_cross_generation_mutation": {"modules/checkpoint.py:start_new_provider_query_execution"},
    "test_atomic_flight_success_rejects_unbound_provider_call": {"modules/checkpoint.py:complete_provider_call_and_flight_success"},
    "test_volatile_same_query_independent_failures_open_circuit": {"modules/search.py:_brightdata_text"},
    "test_singleflight_clocked_heartbeat_covers_every_wait_region": {"modules/checkpoint.py:heartbeat_provider_query_flight"},
    "test_real_fresh_handoff_resume_preserves_durable_ordered_query_plan": {"main.py:run"},
    "test_paid_targeted_query_plan_is_append_only_across_real_rounds_and_resume": {"modules/search.py:find_targeted_candidates"},
    "test_resume_query_plan_or_config_drift_returns_cli_22_before_network": {"main.py:cli"},
    "test_resume_llm_budget_drift_is_rejected_before_saved_credentials": {"main.py:cli"},
    "test_finalization_rejects_each_telemetry_replica_drift": {"modules/checkpoint.py:validate_finalization_contract"},
    "test_report_rejects_missing_provider_set": {"modules/report.py:build_report"},
    "test_windows_junction_retarget_changes_protected_manifest": {"tests/conftest.py:_protected_manifest"},
    "test_evidence_capture_rejects_missing_journal_manifest_or_artifact": {"modules/checkpoint.py:derive_telemetry"},
    "test_recording_transport_uses_production_attempt_and_fingerprints": {"modules/search.py:_brightdata_text"},
    "test_audit_scenario_validator_rejects_each_tampered_required_artifact": {"tools/closure_audit_runner.py:validate_scenario"},
    "test_audit_scenario_validator_rejects_rehashed_semantic_command_forgery": {"tools/closure_audit_runner.py:validate_scenario"},
    "test_audit_scenario_validator_rejects_rehashed_transport_and_relational_forgery": {"tools/closure_audit_runner.py:validate_scenario"},
    "test_offline_paid_network_guard_fails_on_every_default_transport_fallthrough": _ADAPTER_ENTRIES,
    "test_network_receipt_validator_derives_zero_external_calls_and_rejects_forgery": {"tools/closure_audit_runner.py:validate_network_receipts"},
    "test_k6_receipt_validator_rejects_missing_duplicate_and_forged_cases": {"tools/closure_audit_runner.py:validate_k6_receipts"},
    "test_root_artifact_hash_is_a_marker_gate_and_includes_nested_manifests": {"tools/closure_audit_runner.py:validate_artifact_hashes"},
    "test_contract_matrix_has_no_fallback_and_replays_every_receipt": {"tools/closure_audit_runner.py:validate_contract_matrix"},
    "test_k6_uses_real_negative_resume_drift_not_positive_resume_node": {"modules/checkpoint.py:file_hash"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def file_receipt(path: Path, *, relative_to: Path = REPO) -> dict:
    data = path.read_bytes()
    stat = path.stat()
    try:
        name = path.resolve().relative_to(relative_to.resolve()).as_posix()
    except ValueError:
        name = str(path.resolve())
    return {"path": name, "bytes": len(data), "sha256": sha(data), "mtime_ns": stat.st_mtime_ns}


def source_files() -> list[Path]:
    roots = [REPO / "modules", REPO / "tests", REPO / "tools"]
    files: list[Path] = [
        path for path in REPO.iterdir()
        if path.is_file() and (path.suffix.casefold() in {".py", ".ps1"} or path.name.startswith("requirements"))
    ]
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(set(files), key=lambda path: path.as_posix())


def source_manifest() -> list[dict]:
    return [file_receipt(path) for path in source_files()]


def read_reports(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                row["report_path"] = path.relative_to(paths[0].parent if paths else REPO).as_posix()
                rows.append(row)
    return rows


def call_outcomes(rows: list[dict]) -> dict[tuple[str, str, str, int], dict]:
    return {
        (str(row.get("command_id", "")), str(row["nodeid"]), str(row.get("phase", "")), int(row.get("report_ordinal", -1))): row
        for row in rows if row.get("phase") == "call"
    }


def _validate_scenario(root: Path, *, command_records: list[dict] | None = None) -> dict:
    required = {
        "scenario_result.json", "recording_transport_journal.json", "sanitized_progress.sqlite3",
        "manifest.json", "frozen_config.json", "paid_query_plan.json", "state_transition_trace.json",
        "command.json", "duration.json", "pytest_reports.jsonl",
        "artifact_hashes.json",
    }
    missing = sorted(name for name in required if not (root / name).is_file())
    checks: dict[str, object] = {"missing": missing}
    if missing:
        checks["pass"] = False
        return checks
    try:
        scenario = json.loads((root / "scenario_result.json").read_text(encoding="utf-8"))
        journal = json.loads((root / "recording_transport_journal.json").read_text(encoding="utf-8"))
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        frozen = json.loads((root / "frozen_config.json").read_text(encoding="utf-8"))
        plan_file = json.loads((root / "paid_query_plan.json").read_text(encoding="utf-8"))
        trace_file = json.loads((root / "state_transition_trace.json").read_text(encoding="utf-8"))
        command = json.loads((root / "command.json").read_text(encoding="utf-8"))
        duration = json.loads((root / "duration.json").read_text(encoding="utf-8"))
        reports = [json.loads(line) for line in (root / "pytest_reports.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError, TypeError) as exc:
        return {"pass": False, "parse_error": type(exc).__name__, "missing": missing}
    if not isinstance(scenario, dict) or "expected" in scenario or "pass" in scenario or "actual" in scenario:
        return {"pass": False, "self_authored_decision": True, "missing": missing}
    scenario_name = str(scenario.get("scenario", root.name))
    contract = SCENARIO_CONTRACT.get(scenario_name)
    if not contract or not isinstance(scenario.get("observations"), dict) or not isinstance(journal, list) or not isinstance(plan_file, list) or not isinstance(trace_file, list) or not isinstance(reports, list):
        return {"pass": False, "contract_or_container_invalid": True, "missing": missing}
    db = sqlite3.connect(f"file:{(root / 'sanitized_progress.sqlite3').resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        schema_triggers = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        provider_call_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(provider_calls)")}
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
        run = db.execute("SELECT run_id,phase,context_json FROM runs ORDER BY updated_at DESC LIMIT 1").fetchone()
        started = db.execute("SELECT c.run_id,c.call_id,c.provider,c.item_index,c.request_fingerprint,c.attempt_ordinal,c.flight_fingerprint,COALESCE(l.paid_attempt_id,''),COALESCE(l.execution_generation,1),c.endpoint_sha256,c.request_shape_sha256 FROM provider_calls c LEFT JOIN paid_attempt_calls l ON l.run_id=c.run_id AND l.provider=c.provider AND l.provider_call_id=c.call_id AND l.relation='OWNER' AND l.item_index=c.item_index WHERE c.http_started_at<>'' ORDER BY c.created_at,c.call_id").fetchall()
        plan_rows = db.execute("SELECT item_index,plan_version,query_kind,round_ordinal,query_ordinal,normalized_query,query_sha256 FROM paid_query_plan_entries WHERE run_id=? ORDER BY item_index,plan_version,query_kind,round_ordinal,query_ordinal", (str(run[0]),)).fetchall()
        transition_rows = db.execute("SELECT ordinal,from_phase,to_phase,transitioned_at FROM run_phase_transitions WHERE run_id=? ORDER BY ordinal", (str(run[0]),)).fetchall()
        relations = dict(db.execute("SELECT relation,COUNT(*) FROM provider_query_flight_consumers WHERE run_id=? GROUP BY relation", (str(run[0]),)).fetchall())
        free = db.execute("SELECT COALESCE(SUM(logical_used),0),COALESCE(SUM(discovery_logical_used),0),COALESCE(SUM(targeted_logical_used),0),COALESCE(SUM(physical_used),0),COALESCE(SUM(discovery_physical_used),0),COALESCE(SUM(targeted_physical_used),0),COALESCE(SUM(physical_completed),0),COALESCE(SUM(physical_failed),0) FROM free_query_usage WHERE run_id=?", (str(run[0]),)).fetchone()
        free_attempt_rows = db.execute("SELECT bucket,state,COUNT(*) FROM free_provider_attempts WHERE run_id=? GROUP BY bucket,state", (str(run[0]),)).fetchall()
        usage_rows = db.execute("SELECT provider,configured_limit,effective_limit,reserved_total,reserved,completed,failed,unknown FROM provider_usage WHERE run_id=? ORDER BY provider", (str(run[0]),)).fetchall()
        call_count_rows = db.execute("SELECT provider,state,COUNT(*) FROM provider_calls WHERE run_id=? GROUP BY provider,state", (str(run[0]),)).fetchall()
        call_rows = db.execute("SELECT call_id,provider,item_index,state,http_started_at,flight_fingerprint,endpoint_sha256,request_shape_sha256 FROM provider_calls WHERE run_id=?", (str(run[0]),)).fetchall()
        paid_links = db.execute("SELECT item_index,paid_attempt_id,provider_call_id,provider,query_fingerprint,execution_generation,relation FROM paid_attempt_calls WHERE run_id=?", (str(run[0]),)).fetchall()
        flight_rows = db.execute("SELECT provider,query_fingerprint,execution_generation,state,provider_call_id,result_json,call_ids_json FROM provider_query_flights WHERE run_id=?", (str(run[0]),)).fetchall()
        terminal_rows = db.execute("SELECT provider,query_fingerprint,execution_generation,state,provider_call_id,result_sha256,call_ids_json FROM provider_query_flight_terminals WHERE run_id=?", (str(run[0]),)).fetchall()
        result_rows = db.execute("SELECT provider,query_fingerprint,execution_generation,provider_call_id,result_json,result_sha256 FROM provider_query_flight_results WHERE run_id=?", (str(run[0]),)).fetchall()
        consumer_rows = db.execute("SELECT provider,query_fingerprint,execution_generation,paid_attempt_id,item_index,provider_call_id,relation FROM provider_query_flight_consumers WHERE run_id=?", (str(run[0]),)).fetchall()
        finalization_rows = db.execute("SELECT status,artifact_set_sha256,manifest_sha256 FROM finalization_intent WHERE run_id=?", (str(run[0]),)).fetchall()
    finally:
        db.close()
    def paid_endpoint(provider: str, endpoint: str) -> bool:
        parsed = urlparse(str(endpoint))
        host = str(parsed.hostname or "").casefold()
        markers = PAID_PROVIDER_HOSTS.get(str(provider), ())
        return parsed.scheme.casefold() == "https" and any(host == marker or host.endswith(f".{marker}") for marker in markers)

    journal_keys = [(str(row.get("run_id", "")), str(row.get("provider_call_id", "")), str(row.get("provider", "")), int(row.get("item_index", -1)), str(row.get("request_fingerprint", "")), int(row.get("attempt_ordinal", 0)), str(row.get("flight_fingerprint", "")), str(row.get("paid_attempt_id", "")), int(row.get("execution_generation", 1)), sha(str(row.get("endpoint", "")).encode("utf-8")), str(row.get("request_shape_sha256", ""))) for row in journal]
    started_keys = [(str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4]), int(row[5]), str(row[6]), str(row[7]), int(row[8]), str(row[9]), str(row[10])) for row in started]
    journal_semantic_exact = all(
        set(("run_id", "provider_call_id", "provider", "item_index", "request_fingerprint", "attempt_ordinal", "flight_fingerprint", "paid_attempt_id", "execution_generation", "endpoint", "request_shape_sha256", "invocation_ordinal", "fake_response_class")).issubset(row)
        and paid_endpoint(str(row.get("provider", "")), str(row.get("endpoint", "")))
        and re.fullmatch(r"[0-9a-f]{64}", str(row.get("request_shape_sha256", ""))) is not None
        and int(row.get("invocation_ordinal", 0)) > 0 and bool(str(row.get("fake_response_class", "")))
        for row in journal
    ) and [int(row.get("invocation_ordinal", 0)) for row in journal] == list(range(1, len(journal) + 1))

    calls_by_id = {str(row[0]): {"provider": str(row[1]), "item": int(row[2]), "state": str(row[3]), "http": str(row[4]), "fingerprint": str(row[5]), "endpoint_sha256": str(row[6]), "shape_sha256": str(row[7])} for row in call_rows}
    links = {(int(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]), str(row[6])) for row in paid_links}
    flights = {(str(row[0]), str(row[1])): {"generation": int(row[2]), "state": str(row[3]), "provider_call_id": str(row[4]), "result_json": str(row[5]), "call_ids": json.loads(str(row[6] or "[]"))} for row in flight_rows}
    terminals = {(str(row[0]), str(row[1]), int(row[2])): {"state": str(row[3]), "provider_call_id": str(row[4]), "result_sha256": str(row[5]), "call_ids": json.loads(str(row[6] or "[]"))} for row in terminal_rows}
    results = {(str(row[0]), str(row[1]), int(row[2])): {"provider_call_id": str(row[3]), "result_json": str(row[4]), "result_sha256": str(row[5])} for row in result_rows}
    result_chain_exact = True
    for (provider, fingerprint, generation), result in results.items():
        flight = flights.get((provider, fingerprint)); call = calls_by_id.get(result["provider_call_id"])
        matching_link = any(link[2] == result["provider_call_id"] and link[3:] == (provider, fingerprint, generation, "OWNER") for link in links)
        result_chain_exact = result_chain_exact and bool(
            flight and 1 <= generation <= flight["generation"] and call and call["provider"] == provider
            and call["state"] == "DONE" and call["http"] and call["fingerprint"] == fingerprint
            and matching_link and sha(result["result_json"].encode("utf-8")) == result["result_sha256"]
        )
    terminal_chain_exact = True
    for key, terminal in terminals.items():
        provider, fingerprint, generation = key; flight = flights.get((provider, fingerprint))
        ids = terminal["call_ids"] if isinstance(terminal["call_ids"], list) else []
        ids_exact = len(ids) == len(set(ids)) and all(
            call_id in calls_by_id and calls_by_id[call_id]["provider"] == provider and calls_by_id[call_id]["fingerprint"] == fingerprint
            and any(link[2] == call_id and link[3:] == (provider, fingerprint, generation, "OWNER") for link in links)
            for call_id in ids
        )
        if terminal["state"] == "DONE":
            result = results.get(key)
            state_exact = bool(result and terminal["provider_call_id"] in ids and result["provider_call_id"] == terminal["provider_call_id"] and result["result_sha256"] == terminal["result_sha256"])
        elif terminal["state"] == "FAILED":
            state_exact = terminal["provider_call_id"] == "" and all(calls_by_id[call_id]["state"] == "FAILED" for call_id in ids)
        else:
            states = [calls_by_id[call_id]["state"] for call_id in ids] if ids_exact else []
            state_exact = terminal["provider_call_id"] == "" and bool(states) and "UNKNOWN" in states and all(state in {"FAILED", "UNKNOWN"} for state in states)
        terminal_chain_exact = terminal_chain_exact and bool(flight and 1 <= generation <= flight["generation"] and ids_exact and state_exact)
    for (provider, fingerprint), flight in flights.items():
        if flight["state"] in {"DONE", "FAILED", "UNKNOWN"}:
            terminal = terminals.get((provider, fingerprint, flight["generation"]))
            terminal_chain_exact = terminal_chain_exact and bool(
                terminal and terminal["state"] == flight["state"] and terminal["call_ids"] == flight["call_ids"]
                and terminal["provider_call_id"] == flight["provider_call_id"]
                and terminal["result_sha256"] == sha(flight["result_json"].encode("utf-8"))
            )
    consumer_chain_exact = True
    for provider, fingerprint, generation, attempt_id, item_index, call_id, relation in consumer_rows:
        provider = str(provider); fingerprint = str(fingerprint); generation = int(generation); attempt_id = str(attempt_id); item_index = int(item_index); call_id = str(call_id); relation = str(relation)
        terminal = terminals.get((provider, fingerprint, generation)); result = results.get((provider, fingerprint, generation)); call = calls_by_id.get(call_id)
        link_exact = (item_index, attempt_id, call_id, provider, fingerprint, generation, relation) in links
        terminal_exact = bool(terminal and call_id in terminal["call_ids"])
        state_exact = bool(terminal and call and (
            (
                terminal["state"] == "DONE" and result
                and result["provider_call_id"] in terminal["call_ids"]
                and (
                    (result["provider_call_id"] == call_id and call["state"] == "DONE")
                    or call["state"] == "FAILED"
                )
            )
            or (terminal["state"] == "FAILED" and call["state"] == "FAILED")
            or (terminal["state"] == "UNKNOWN" and call["state"] in {"FAILED", "UNKNOWN"})
        ))
        consumer_chain_exact = consumer_chain_exact and bool(call and call["provider"] == provider and call["fingerprint"] == fingerprint and link_exact and terminal_exact and state_exact and (relation == "INHERITED" or call["item"] == item_index))
    consumer_projection = {
        (int(item_index), str(attempt_id), str(call_id), str(provider), str(fingerprint), int(generation), str(relation))
        for provider, fingerprint, generation, attempt_id, item_index, call_id, relation in consumer_rows
    }
    link_consumer_exact = (
        len(paid_links) == len(links) == len(consumer_rows) == len(consumer_projection)
        and links == consumer_projection
    )
    done_terminal_keys = {key for key, terminal in terminals.items() if terminal["state"] == "DONE"}
    result_coverage_exact = set(results) == done_terminal_keys
    http_call_coverage_exact = True
    for call_id, call in calls_by_id.items():
        if not call["http"]:
            continue
        owner_links = [link for link in links if link[2] == call_id and link[3] == call["provider"] and link[6] == "OWNER"]
        call_covered = len(owner_links) == 1
        if call_covered and call["fingerprint"]:
            owner = owner_links[0]
            terminal = terminals.get((call["provider"], call["fingerprint"], owner[5]))
            call_covered = bool(terminal and call_id in terminal["call_ids"])
            if call["state"] == "DONE":
                result = results.get((call["provider"], call["fingerprint"], owner[5]))
                call_covered = call_covered and bool(result and result["provider_call_id"] == call_id)
        http_call_coverage_exact = http_call_coverage_exact and call_covered
    canonical_plan = [list(row) for row in plan_rows]
    plan_material = json.dumps(canonical_plan, ensure_ascii=False, separators=(",", ":"))
    plan_hash = sha(plan_material.encode("utf-8"))
    canonical_trace = [{"ordinal": row[0], "from": row[1], "to": row[2], "at": row[3]} for row in transition_rows]
    observations = scenario["observations"]
    derived_outcome = (
        "COMPLETE" if str(run[1]) == "COMPLETE" and manifest.get("complete") is True
        else "PAID_MANUAL_AUTHORIZATION_REVIEW_REQUIRED" if manifest.get("manual_authorization_review_required") is True
        else "PAID_PENDING_APPROVAL" if str(run[1]) == "PAID" and manifest.get("handoff") is True and manifest.get("paid_enabled") is False
        else ""
    )
    call_counts = {(str(provider), str(state)): int(count) for provider, state, count in call_count_rows}
    provider_aggregates = {
        str(provider): {
            "configured_limit": int(configured), "effective_limit": int(effective), "reserved_total": int(total),
            "reserved": int(reserved), "completed": int(completed), "failed": int(failed), "unknown": int(unknown),
        }
        for provider, configured, effective, total, reserved, completed, failed, unknown in usage_rows
    }
    provider_aggregate_exact = set(provider_aggregates) == CANONICAL_PROVIDER_SET
    for provider, aggregate in provider_aggregates.items():
        counts = {state: call_counts.get((provider, state), 0) for state in ("RESERVED", "RUNNING", "DONE", "FAILED", "UNKNOWN")}
        provider_aggregate_exact = provider_aggregate_exact and (
            aggregate["configured_limit"] >= aggregate["effective_limit"] >= aggregate["reserved_total"] >= 0
            and aggregate["reserved"] == counts["RESERVED"] + counts["RUNNING"]
            and aggregate["completed"] == counts["DONE"] and aggregate["failed"] == counts["FAILED"]
            and aggregate["unknown"] == counts["UNKNOWN"] and aggregate["reserved_total"] == sum(counts.values())
        )
    free_attempt_counts = {(str(bucket), str(state)): int(count) for bucket, state, count in free_attempt_rows}
    free_attempt_exact = (
        all(bucket in {"discovery", "targeted"} and state in {"RESERVED", "DONE", "FAILED"} for bucket, state in free_attempt_counts)
        and int(free[3]) == sum(free_attempt_counts.values())
        and int(free[4]) == sum(count for (bucket, _state), count in free_attempt_counts.items() if bucket == "discovery")
        and int(free[5]) == sum(count for (bucket, _state), count in free_attempt_counts.items() if bucket == "targeted")
        and int(free[6]) == sum(count for (_bucket, state), count in free_attempt_counts.items() if state == "DONE")
        and int(free[7]) == sum(count for (_bucket, state), count in free_attempt_counts.items() if state == "FAILED")
        and int(free[3]) - int(free[6]) - int(free[7]) == sum(count for (_bucket, state), count in free_attempt_counts.items() if state == "RESERVED")
    )
    manifest_files = manifest.get("files")
    artifact_checks: dict[str, bool] = {}
    artifact_set_required = str(run[1]) == "COMPLETE" or manifest.get("handoff") is True
    artifact_paths_safe = isinstance(manifest_files, dict) if artifact_set_required else manifest_files is None
    if isinstance(manifest_files, dict):
        for filename, info in manifest_files.items():
            relative = Path(str(filename))
            target = (root / relative).resolve()
            path_safe = (
                isinstance(filename, str) and bool(filename) and not relative.is_absolute()
                and ".." not in relative.parts and relative.name == filename
                and target.parent == root.resolve()
            )
            artifact_paths_safe = artifact_paths_safe and path_safe and isinstance(info, dict)
            artifact_checks[str(filename)] = bool(
                path_safe and isinstance(info, dict) and target.is_file()
                and len(target.read_bytes()) == int(info.get("bytes", -1))
                and sha(target.read_bytes()) == info.get("sha256")
                and re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", ""))) is not None
            )
    artifact_set_material = "".join(
        f"{name}:{manifest_files[name].get('sha256', '')}\n" for name in sorted(manifest_files)
    ) if isinstance(manifest_files, dict) else ""
    artifact_set_exact = bool(
        artifact_paths_safe and (
            bool(manifest_files) and str(manifest.get("artifact_set_sha256", ""))
            == sha(artifact_set_material.encode("utf-8"))
            if artifact_set_required
            else "artifact_set_sha256" not in manifest
        )
    )
    if str(run[1]) == "COMPLETE":
        finalization_exact = bool(
            len(finalization_rows) == 1 and str(finalization_rows[0][0]) == "COMPLETE"
            and str(finalization_rows[0][1]) == str(manifest.get("artifact_set_sha256", ""))
            and str(finalization_rows[0][2]) == sha((root / "manifest.json").read_bytes())
        )
    else:
        finalization_exact = not finalization_rows
    absent_checks = {name: not (root / name).exists() for name in scenario.get("observed_absent", [])}
    try:
        nested_hashes = json.loads((root / "artifact_hashes.json").read_text(encoding="utf-8"))
        nested_actual = [file_receipt(path, relative_to=root) for path in sorted(root.iterdir()) if path.is_file() and path.name != "artifact_hashes.json"]
        nested_hash_exact = nested_hashes == nested_actual
    except (OSError, ValueError, TypeError):
        nested_hash_exact = False
    report_calls = [row for row in reports if row.get("phase") == "call"]
    command_id = str(command.get("command_id", ""))
    expected_node = f"tests/test_search_phase_regressions.py::{SCENARIO_NODES[scenario_name]}"
    argv = command.get("argv", [])
    command_semantic_exact = bool(
        isinstance(argv, list) and len(argv) >= 4 and argv[-1] == expected_node
        and argv.count(expected_node) == 1 and "-m" in argv and "pytest" in argv
        and command.get("gate") == "K5" and command.get("name") == f"k5_{scenario_name}"
        and command.get("exit_code") == 0 and command.get("timed_out") is False
        and command.get("source_stable") is True
    )
    root_command_exact = True
    if command_records is not None:
        root_matches = [row for row in command_records if row.get("command_id") == command_id]
        root_command_exact = len(root_matches) == 1 and root_matches[0] == command
    phase_path = [f"{row[1]}->{row[2]}" for row in transition_rows]
    state_counts = {state: count for (provider, state), count in call_counts.items() if provider in CANONICAL_PROVIDER_SET}
    state_counts = {state: sum(count for (provider, candidate), count in call_counts.items() if candidate == state) for state in set(state_counts)}
    expected_contract = (
        str(run[1]) == contract["phase"] and derived_outcome == contract["outcome"]
        and str(observations.get("outcome", "")) == derived_outcome
        and len(journal) == int(contract["http"])
        and phase_path == contract["path"] and state_counts == contract["call_states"]
    )
    if "relations" in contract:
        expected_contract = expected_contract and all(int(relations.get(key, 0)) == value for key, value in contract["relations"].items())
    if "free" in contract:
        expected_contract = expected_contract and tuple(map(int, free[:6])) == tuple(contract["free"])
    paid_endpoint_hits = sum(paid_endpoint(str(row.get("provider", "")), str(row.get("endpoint", ""))) for row in journal)
    checks.update({
        "contract_version": SCENARIO_CONTRACT_VERSION,
        "contract_match": expected_contract,
        "phase": str(run[1]),
        "derived_outcome": derived_outcome,
        "command_exit": command.get("exit_code"),
        "phase_path": phase_path,
        "provider_call_states": state_counts,
        "integrity": integrity,
        "schema_exact": schema_version == EXPECTED_SCHEDULER_SCHEMA_VERSION and REQUIRED_RECEIPT_TRIGGERS.issubset(schema_triggers) and {"endpoint_sha256", "request_shape_sha256"}.issubset(provider_call_columns),
        "foreign_key_violations": foreign_keys,
        "journal_count": len(journal),
        "http_started_count": len(started),
        "journal_ledger_exact": sorted(journal_keys) == sorted(started_keys),
        "journal_semantic_exact": journal_semantic_exact,
        "result_chain_exact": result_chain_exact,
        "terminal_chain_exact": terminal_chain_exact,
        "consumer_chain_exact": consumer_chain_exact,
        "link_consumer_exact": link_consumer_exact,
        "result_coverage_exact": result_coverage_exact,
        "http_call_coverage_exact": http_call_coverage_exact,
        "provider_aggregates": provider_aggregates,
        "provider_aggregate_exact": provider_aggregate_exact,
        "relation_counts": {str(key): int(value) for key, value in relations.items()},
        "free_logical": {"total": int(free[0]), "discovery": int(free[1]), "targeted": int(free[2])},
        "free_physical": {"total": int(free[3]), "discovery": int(free[4]), "targeted": int(free[5])},
        "free_attempt_exact": free_attempt_exact,
        "manifest_db_exact": str(manifest.get("run_id")) == str(run[0]) and str(manifest.get("phase")) == str(run[1]),
        "frozen_context_exact": frozen == json.loads(str(run[2] or "{}")),
        "plan_exact": plan_file == canonical_plan and int(manifest.get("paid_query_plan_count", -1)) == len(canonical_plan) and str(manifest.get("paid_query_plan_sha256", "")) == plan_hash,
        "trace_exact": trace_file == canonical_trace,
        "command_duration_exact": bool(command_id) and command_semantic_exact and root_command_exact and all(command.get(key) == duration.get(key) for key in ("started_at", "ended_at", "duration_seconds", "exit_code", "timed_out")),
        "pytest_exact": len(report_calls) == 1 and report_calls[0].get("nodeid") == expected_node and report_calls[0].get("command_id") == command_id and report_calls[0].get("outcome") == "passed" and not report_calls[0].get("wasxfail") and isinstance(report_calls[0].get("report_ordinal"), int),
        "nested_hash_exact": nested_hash_exact,
        "artifact_checks": artifact_checks,
        "artifact_paths_safe": artifact_paths_safe,
        "artifact_set_exact": artifact_set_exact,
        "finalization_exact": finalization_exact,
        "expected_absent_checks": absent_checks,
        "paid_endpoint_hits": paid_endpoint_hits,
        "paid_endpoint_exact": paid_endpoint_hits == len(journal) == len(started) == int(contract["http"]),
    })
    checks["pass"] = bool(
        expected_contract and integrity == "ok" and checks["schema_exact"] and not foreign_keys and checks["journal_ledger_exact"] and journal_semantic_exact
        and result_chain_exact and terminal_chain_exact and consumer_chain_exact and link_consumer_exact
        and result_coverage_exact and http_call_coverage_exact and checks["paid_endpoint_exact"]
        and provider_aggregate_exact and free_attempt_exact
        and checks["manifest_db_exact"] and checks["frozen_context_exact"] and checks["plan_exact"]
        and checks["trace_exact"] and checks["command_duration_exact"] and checks["pytest_exact"] and nested_hash_exact
        and artifact_paths_safe and artifact_set_exact and finalization_exact
        and all(artifact_checks.values()) and all(absent_checks.values())
    )
    return checks


def validate_scenario(root: Path, *, command_records: list[dict] | None = None) -> dict:
    """Validate untrusted scenario evidence and fail closed on malformed content."""
    try:
        return _validate_scenario(root, command_records=command_records)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error) as exc:
        return {"pass": False, "validation_error": type(exc).__name__}


def artifact_inventory(root: Path) -> list[dict]:
    root = Path(root).resolve()
    excluded = (root / "artifact_hashes.json").resolve()
    return [file_receipt(path, relative_to=root) for path in sorted(root.rglob("*")) if path.is_file() and path.resolve() != excluded]


def write_artifact_hashes(root: Path) -> dict:
    entries = artifact_inventory(root)
    material = json.dumps([(row["path"], row["bytes"], row["sha256"]) for row in entries], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload = {"algorithm": "sorted(relative_path,bytes,sha256); root artifact_hashes.json excluded", "entries": entries, "evidence_sha256": sha(material)}
    dump(Path(root) / "artifact_hashes.json", payload)
    return payload


def validate_artifact_hashes(root: Path) -> dict:
    root = Path(root).resolve(); manifest_path = root / "artifact_hashes.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual = artifact_inventory(root)
        material = json.dumps([(row["path"], row["bytes"], row["sha256"]) for row in actual], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        exact = payload.get("entries") == actual and payload.get("evidence_sha256") == sha(material)
        nested = [row["path"] for row in actual if row["path"].endswith("/artifact_hashes.json")]
        return {"pass": bool(exact and len(nested) == 8), "exact": exact, "nested_manifests": nested, "aggregate_sha256": sha(material)}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"pass": False, "error": type(exc).__name__}


def validate_contract_matrix(path: Path, evidence_root: Path, *, expected_nodes: tuple[str, ...] | list[str] = REQUIRED_NODES, expected_entrypoints: dict[str, set[str]] = REQUIREMENT_ENTRYPOINTS, command_records: list[dict] | None = None) -> dict:
    try:
        matrix = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"pass": False, "error": type(exc).__name__}
    if not isinstance(matrix, dict) or not matrix:
        return {"pass": False, "error": "empty_matrix"}
    failures = []
    expected = {f"R{index}": node for index, node in enumerate(expected_nodes, 1)}
    if set(matrix) != set(expected):
        failures.append("requirement_set")
    if command_records is None:
        try:
            command_records = json.loads((Path(evidence_root) / "commands.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            command_records = []
    command_index = {str(row.get("command_id")): row for row in command_records if isinstance(row, dict)}
    for requirement, expected_node in expected.items():
        row = matrix.get(requirement)
        if not isinstance(row, dict) or row.get("fallback") is not None:
            failures.append(f"{requirement}:invalid"); continue
        if row.get("node") != expected_node:
            failures.append(f"{requirement}:node")
        for field in ("collected_nodeids", "command_ids", "outcomes", "production_function_entries", "receipt_paths"):
            if not row.get(field): failures.append(f"{requirement}:{field}")
        receipt_paths = row.get("receipt_paths", [])
        if not isinstance(receipt_paths, list) or any(not isinstance(receipt, str) or not (Path(evidence_root) / receipt).is_file() for receipt in receipt_paths):
            failures.append(f"{requirement}:receipt_missing")
            continue
        report_paths = [receipt for receipt in receipt_paths if Path(receipt).name.startswith("pytest_") and Path(receipt).suffix == ".jsonl"]
        trace_paths = [receipt for receipt in receipt_paths if receipt.startswith("traces/") and Path(receipt).suffix == ".jsonl"]
        case_paths = [receipt for receipt in receipt_paths if receipt.startswith("k6_receipts/") and Path(receipt).suffix == ".json"]
        report_rows, trace_rows = [], []
        try:
            for receipt in report_paths:
                report_rows.extend(json.loads(line) for line in (Path(evidence_root) / receipt).read_text(encoding="utf-8").splitlines() if line.strip())
            for receipt in trace_paths:
                for line in (Path(evidence_root) / receipt).read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        trace = json.loads(line); trace["trace_path"] = receipt; trace_rows.append(trace)
        except (OSError, ValueError, TypeError):
            failures.append(f"{requirement}:receipt_parse"); continue
        matched_reports = [entry for entry in report_rows if entry.get("phase") == "call" and str(entry.get("nodeid", "")).split("::")[-1].split("[")[0] == expected_node and entry.get("command_id") in row.get("command_ids", [])]
        report_identities = {(str(entry.get("command_id")), str(entry.get("nodeid"))) for entry in matched_reports}
        trace_entries = row.get("production_function_entries", [])
        required_entries = set(expected_entrypoints.get(expected_node, ()))
        if not required_entries:
            failures.append(f"{requirement}:entrypoint_contract_missing")
        if not isinstance(trace_entries, list) or not trace_entries:
            failures.append(f"{requirement}:trace_empty"); trace_entries = []
        matrix_trace_identity_list = [(str(entry.get("command_id")), str(entry.get("nodeid"))) for entry in trace_entries if isinstance(entry, dict)]
        source_trace_identity_list = [(str(entry.get("command_id")), str(entry.get("nodeid"))) for entry in trace_rows if (str(entry.get("command_id")), str(entry.get("nodeid"))) in report_identities]
        matrix_trace_identities = set(matrix_trace_identity_list)
        source_trace_identities = set(source_trace_identity_list)
        traces_exact = (
            matrix_trace_identities == report_identities == source_trace_identities
            and len(matrix_trace_identity_list) == len(matrix_trace_identities)
            and len(source_trace_identity_list) == len(source_trace_identities)
        )
        for entry in trace_entries:
            source = next((candidate for candidate in trace_rows if candidate.get("command_id") == entry.get("command_id") and candidate.get("nodeid") == entry.get("nodeid")), None) if isinstance(entry, dict) else None
            functions = entry.get("function_entries", []) if isinstance(entry, dict) else []
            allowed_trace = all(str(value).startswith(("main.py:", "modules/", "tools/closure_audit_runner.py:", "tests/conftest.py:")) for value in functions)
            if source is None or entry.get("trace_path") != source.get("trace_path") or functions != source.get("function_entries") or not functions or not allowed_trace or not required_entries.intersection(map(str, functions)):
                traces_exact = False
        command_ids = sorted({identity[0] for identity in report_identities})
        commands_exact = bool(command_ids) and command_ids == sorted(row.get("command_ids", [])) and all(command_index.get(command_id, {}).get("exit_code") == 0 and not command_index.get(command_id, {}).get("timed_out") for command_id in command_ids)
        nodes_exact = sorted(identity[1] for identity in report_identities) == sorted(row.get("collected_nodeids", []))
        outcomes_exact = [entry.get("outcome") for entry in matched_reports] == row.get("outcomes", []) and all(entry.get("outcome") == "passed" and not entry.get("wasxfail") for entry in matched_reports)
        expected_k6_identities = {identity for identity in report_identities if command_index.get(identity[0], {}).get("gate") == "K6"}
        case_identities = set()
        try:
            for receipt in case_paths:
                case = json.loads((Path(evidence_root) / receipt).read_text(encoding="utf-8"))
                case_identities.add((str(case.get("command_id", "")), str(case.get("nodeid", ""))))
        except (OSError, ValueError, TypeError):
            case_identities.add(("invalid", "invalid"))
        cases_exact = case_identities == expected_k6_identities and len(case_paths) == len(set(case_paths)) == len(case_identities)
        paths_exact = len(receipt_paths) == len(set(receipt_paths)) and set(receipt_paths) == set(report_paths) | set(trace_paths) | set(case_paths) and bool(report_paths) and bool(trace_paths)
        derived = bool(commands_exact and nodes_exact and outcomes_exact and traces_exact and paths_exact and cases_exact)
        if row.get("pass") is not derived or not derived:
            failures.append(f"{requirement}:outcome")
    return {"pass": not failures, "failures": failures}


def _reason_pattern_matches(pattern: object, reason: object) -> bool:
    if not pattern:
        return True
    try:
        return re.search(str(pattern), str(reason)) is not None
    except re.error:
        return False


def _validate_k6_receipts(path: Path, evidence_root: Path, report_rows: list[dict], command_id: str) -> dict:
    """Re-read one immutable, mutation-scoped receipt for every K6 pytest case."""
    failures: list[str] = []
    try:
        receipts = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError, TypeError):
        receipts = []
        failures.append("missing_or_invalid_jsonl")
    expected_reports = [
        row for row in report_rows
        if row.get("phase") == "call" and row.get("command_id") == command_id
    ]
    expected_nodeids = [str(row.get("nodeid", "")) for row in expected_reports]
    receipt_nodeids = [str(row.get("nodeid", "")) for row in receipts if isinstance(row, dict)]
    if len(expected_nodeids) != len(set(expected_nodeids)) or sorted(receipt_nodeids) != sorted(expected_nodeids):
        failures.append("case_set_or_cardinality")
    if len(receipt_nodeids) != len(set(receipt_nodeids)):
        failures.append("duplicate_case")

    def canonical(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    def artifact_manifest(root: Path) -> list[dict]:
        manifest = []
        for current, directories, files in os.walk(root, followlinks=False):
            directories.sort(); files.sort()
            current_path = Path(current)
            for name in files:
                if name.endswith(("-wal", "-shm")):
                    continue
                artifact = current_path / name
                data = artifact.read_bytes(); stat = artifact.stat()
                manifest.append({
                    "path": artifact.relative_to(root).as_posix(),
                    "absolute_path": str(artifact.resolve()),
                    "bytes": len(data), "sha256": sha(data), "mtime_ns": int(stat.st_mtime_ns),
                })
        return manifest

    def network_records(network_path: str, nodeid: str) -> list[dict]:
        if not network_path or not Path(network_path).is_file():
            return []
        return [
            row for line in Path(network_path).read_text(encoding="utf-8").splitlines()
            if line.strip() for row in [json.loads(line)] if row.get("nodeid") == nodeid
        ]

    def snapshot_exact(snapshot, *, root: Path, nodeid: str, reread: bool = False) -> bool:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("artifact_manifest"), list) or not isinstance(snapshot.get("recorder_journal"), list):
            return False
        manifest = snapshot["artifact_manifest"]
        entries_valid = all(
            isinstance(row, dict) and set(row) == {"path", "absolute_path", "bytes", "sha256", "mtime_ns"}
            and isinstance(row["path"], str) and bool(row["path"])
            and ".." not in Path(row["path"]).parts and not Path(row["path"]).is_absolute()
            and Path(row["absolute_path"]).is_absolute()
            and Path(row["absolute_path"]).resolve() == (root / row["path"]).resolve()
            and isinstance(row["bytes"], int) and row["bytes"] >= 0
            and isinstance(row["sha256"], str) and len(row["sha256"]) == 64
            and isinstance(row["mtime_ns"], int) and row["mtime_ns"] >= 0
            for row in manifest
        )
        network_path = str(snapshot.get("network_receipt_path", ""))
        network = network_records(network_path, nodeid) if reread else None
        exact = bool(
            entries_valid
            and snapshot.get("artifact_root") == str(root.resolve())
            and snapshot.get("artifact_sha256") == sha(canonical(manifest).encode("utf-8"))
            and snapshot.get("recorder_count") == len(snapshot["recorder_journal"])
            and snapshot.get("recorder_sha256") == sha(canonical(snapshot["recorder_journal"]).encode("utf-8"))
            and (not network_path or Path(network_path).is_absolute())
            and isinstance(snapshot.get("network_receipt_count"), int) and snapshot["network_receipt_count"] >= 0
            and isinstance(snapshot.get("network_receipt_sha256"), str) and len(snapshot["network_receipt_sha256"]) == 64
            and isinstance(snapshot.get("socket_blocked"), int) and snapshot["socket_blocked"] >= 0
        )
        if reread:
            try:
                current = artifact_manifest(root)
                exact = exact and current == manifest
                exact = exact and snapshot["network_receipt_count"] == len(network)
                exact = exact and snapshot["network_receipt_sha256"] == sha(canonical(network).encode("utf-8"))
                exact = exact and snapshot["socket_blocked"] == sum(row.get("kind") == "blocked_network" for row in network)
            except (OSError, ValueError, TypeError):
                exact = False
        return exact

    receipt_paths: list[str] = []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            failures.append("non_object_receipt"); continue
        nodeid = str(receipt.get("nodeid", ""))
        case_id = sha(nodeid.encode("utf-8"))
        evidence_path = str(receipt.get("evidence_path", ""))
        expected_path = f"k6_receipts/{case_id}.json"
        root_value = str(receipt.get("disposable_root", ""))
        root = Path(root_value)
        before = receipt.get("before") or {}; after = receipt.get("after") or {}
        rejections = receipt.get("rejections")
        expected = receipt.get("expected") or {}; actual = receipt.get("actual") or {}
        mutation = receipt.get("mutation_evidence") or {}
        rejection_rows_valid = isinstance(rejections, list) and all(isinstance(row, dict) for row in rejections)
        expected_exception_class = ["|".join(map(str, row.get("expected_classes", ()))) for row in rejections] if rejection_rows_valid else []
        actual_exception_class = [str(row.get("class", "")) for row in rejections] if rejection_rows_valid else []
        rejection_exact = rejection_rows_valid and all(
            isinstance(row, dict) and isinstance(row.get("expected_classes"), list) and bool(row["expected_classes"])
            and isinstance(row.get("actual_mro"), list) and bool(row["actual_mro"])
            and row.get("class") == row["actual_mro"][0]
            and all(isinstance(value, str) and bool(value) for value in row["actual_mro"])
            and bool(set(row["expected_classes"]).intersection(row["actual_mro"]))
            and bool(str(row.get("reason", ""))) and bool(str(row.get("source", "")))
            and _reason_pattern_matches(row.get("expected_reason_pattern"), row.get("reason"))
            for row in rejections
        )
        expected_exception_class = expected_exception_class or ["NONE"]
        actual_exception_class = actual_exception_class or ["NONE"]
        node_base = nodeid.split("::")[-1].split("[")[0]
        expected_cli = K6_EXPECTED_CLI_EXITS.get(node_base)
        derived_mutation = {
            "artifact_changed": before.get("artifact_sha256") != after.get("artifact_sha256"),
            "recorder_changed": before.get("recorder_sha256") != after.get("recorder_sha256"),
            "socket_blocked_delta": int(after.get("socket_blocked", 0)) - int(before.get("socket_blocked", 0)),
            "matched_rejections": len(rejections) if isinstance(rejections, list) else -1,
            "cli_exit_observed": bool(actual.get("cli_exit")),
        }
        mutation_observed = bool(
            derived_mutation["artifact_changed"] or derived_mutation["recorder_changed"]
            or derived_mutation["socket_blocked_delta"] > 0 or derived_mutation["matched_rejections"] > 0
            or derived_mutation["cli_exit_observed"]
        )
        if rejection_rows_valid and rejections:
            derived_stable_reason = ";".join(f"{row['class']}:{row['reason']}" for row in rejections)
        elif actual.get("cli_exit"):
            derived_stable_reason = "cli_exit:" + ",".join(map(str, actual["cli_exit"]))
        elif derived_mutation["socket_blocked_delta"] > 0:
            derived_stable_reason = f"socket_guard_blocked:{derived_mutation['socket_blocked_delta']}"
        elif derived_mutation["recorder_changed"]:
            derived_stable_reason = "recorder_journal_changed"
        elif derived_mutation["artifact_changed"]:
            derived_stable_reason = "artifact_state_changed"
        else:
            derived_stable_reason = "no_mutation_evidence"
        expected_mutation_id = nodeid.split("[")[-1].rstrip("]") if "[" in nodeid else nodeid.split("::")[-1]
        exact = bool(
            receipt.get("schema_version") == 2 and receipt.get("command_id") == command_id
            and receipt.get("case_id") == case_id and receipt.get("mutation_id") == expected_mutation_id
            and isinstance(receipt.get("parameters"), dict) and root.is_absolute() and root.is_dir()
            and root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
            and receipt.get("report_outcome") == "passed" and not receipt.get("wasxfail")
            and snapshot_exact(before, root=root, nodeid=nodeid) and snapshot_exact(after, root=root, nodeid=nodeid, reread=True)
            and rejection_exact and mutation == derived_mutation and mutation_observed
            and expected.get("pytest_outcome") == "passed" and actual.get("pytest_outcome") == "passed"
            and expected.get("mutation_observed") is True and actual.get("mutation_observed") is True
            and expected.get("exception_class") == expected_exception_class
            and actual.get("exception_class") == actual_exception_class
            and expected.get("reason_pattern") == [str(row.get("expected_reason_pattern", "")) for row in rejections]
            and actual.get("reason") == [str(row.get("reason", "")) for row in rejections]
            and expected.get("cli_exit") == expected_cli and actual.get("cli_exit") == expected_cli
            and receipt.get("stable_reason") == derived_stable_reason and derived_stable_reason != "no_mutation_evidence"
            and evidence_path == expected_path
        )
        target = Path(evidence_root) / evidence_path
        try:
            exact = exact and json.loads(target.read_text(encoding="utf-8")) == receipt
        except (OSError, ValueError, TypeError):
            exact = False
        if not exact:
            failures.append(f"{nodeid}:receipt_mismatch")
        else:
            receipt_paths.append(evidence_path)
    if any(row.get("outcome") != "passed" or row.get("wasxfail") for row in expected_reports):
        failures.append("pytest_outcome")
    return {
        "pass": bool(expected_reports) and not failures,
        "failures": failures,
        "actual": {"case_count": len(receipts), "nodeids": sorted(receipt_nodeids)},
        "expected": {"case_count": len(expected_reports), "nodeids": sorted(expected_nodeids)},
        "receipt_paths": sorted(receipt_paths),
    }


def validate_k6_receipts(path: Path, evidence_root: Path, report_rows: list[dict], command_id: str) -> dict:
    try:
        return _validate_k6_receipts(path, evidence_root, report_rows, command_id)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, re.error) as exc:
        return {"pass": False, "failures": [f"validation_error:{type(exc).__name__}"]}


def _validate_network_receipts(evidence_root: Path, commands: list[dict], report_rows: list[dict], scenario_results: dict[str, dict]) -> dict:
    provider_set = {"brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm"}
    probe_node = "tests/test_search_phase_regressions.py::test_offline_paid_network_guard_fails_on_every_default_transport_fallthrough"
    pytest_commands = {
        str(row["command_id"]): row for row in commands
        if "-m" in row.get("argv", []) and "pytest" in row.get("argv", [])
    }
    probe_commands = {
        str(row.get("command_id")) for row in report_rows
        if row.get("phase") == "call" and row.get("nodeid") == probe_node and row.get("outcome") == "passed" and not row.get("wasxfail")
    }
    parsed: dict[str, list[dict]] = {}
    failures: list[str] = []
    for command_id in pytest_commands:
        path = Path(evidence_root) / "network" / f"{command_id}.jsonl"
        try:
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError, TypeError):
            failures.append(f"{command_id}:missing_or_invalid"); continue
        parsed[command_id] = records
        if any(not isinstance(row, dict) or row.get("command_id") != command_id for row in records):
            failures.append(f"{command_id}:record_scope")
        armed = [row for row in records if row.get("kind") == "guard_armed" and row.get("command_id") == command_id]
        if len(armed) != 1 or set(armed[0]) != {"kind", "command_id", "pid"} or not isinstance(armed[0].get("pid"), int) or armed[0]["pid"] <= 0:
            failures.append(f"{command_id}:guard_not_armed_once")
        if any(row.get("kind") not in {"guard_armed", "blocked_network", "fallthrough_probe"} for row in records):
            failures.append(f"{command_id}:unknown_record")
        blocked = [row for row in records if row.get("kind") == "blocked_network"]
        if any(
            set(row) != {"kind", "command_id", "nodeid", "event", "args"}
            or row.get("command_id") != command_id or row.get("nodeid") != probe_node
            or str(row.get("event", "")) not in {"socket.getaddrinfo", "socket.gethostbyaddr", "socket.gethostbyname", "socket.gethostbyname_ex", "socket.getnameinfo", "socket.connect", "socket.connect_ex", "socket.sendto", "urllib.Request", "http.client.connect"}
            or not isinstance(row.get("args"), list)
            for row in blocked
        ):
            failures.append(f"{command_id}:unexpected_network_attempt")
        probes = [row for row in records if row.get("kind") == "fallthrough_probe"]
        if command_id in probe_commands:
            observed = {}
            try:
                probes_exact = len(probes) == len(provider_set) and all(
                    set(row) == {"kind", "command_id", "nodeid", "provider", "blocked_events"}
                    and row.get("command_id") == command_id and row.get("nodeid") == probe_node
                    for row in probes
                )
                observed = {str(row.get("provider")): int(row.get("blocked_events", 0)) for row in probes}
            except (TypeError, ValueError):
                probes_exact = False
                failures.append(f"{command_id}:provider_probe_parse")
            if not probes_exact or len(observed) != len(probes) or set(observed) != provider_set or any(value < 1 for value in observed.values()):
                failures.append(f"{command_id}:provider_probe_set")
        elif blocked or probes:
            failures.append(f"{command_id}:network_activity_without_probe")
    if probe_commands != {command_id for command_id in probe_commands if command_id in pytest_commands} or not probe_commands:
        failures.append("fallthrough_probe_commands")
    recorder_interceptions = sum(int(result.get("paid_endpoint_hits", 0)) for result in scenario_results.values())
    actual = {
        "guarded_pytest_commands": len(parsed),
        "expected_guarded_pytest_commands": len(pytest_commands),
        "fallthrough_probe_commands": sorted(probe_commands),
        "blocked_probe_events": sum(1 for records in parsed.values() for row in records if row.get("kind") == "blocked_network"),
        "recorder_interceptions": recorder_interceptions,
        "unexpected_network_attempts": sum(1 for records in parsed.values() for row in records if row.get("kind") == "blocked_network" and row.get("nodeid") != probe_node),
        "real_external_paid_calls": 0 if not failures else None,
    }
    return {
        "pass": not failures and len(parsed) == len(pytest_commands),
        "failures": failures,
        "paid_endpoint_inventory": list(PAID_HOST_MARKERS),
        "receipt_paths": [f"network/{command_id}.jsonl" for command_id in sorted(pytest_commands)],
        "actual": actual,
        "expected": {"all_pytest_commands_guarded": True, "probe_provider_set": sorted(provider_set), "unexpected_network_attempts": 0, "real_external_paid_calls": 0},
    }


def validate_network_receipts(evidence_root: Path, commands: list[dict], report_rows: list[dict], scenario_results: dict[str, dict]) -> dict:
    try:
        return _validate_network_receipts(evidence_root, commands, report_rows, scenario_results)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"pass": False, "failures": [f"validation_error:{type(exc).__name__}"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path)
    args = parser.parse_args()
    if args.evidence_root:
        evidence = args.evidence_root.resolve()
    else:
        pointer = REPO / ".closure_audit_root"
        evidence = Path(pointer.read_text(encoding="utf-8").strip()).resolve()
    evidence.mkdir(parents=True, exist_ok=True)

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
    initial_status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
    initial_diff = subprocess.run(["git", "diff", "--binary", "--no-ext-diff"], cwd=REPO, capture_output=True, text=False)
    initial_sources = source_manifest()
    source_tree_material = json.dumps([(row["path"], row["bytes"], row["sha256"]) for row in initial_sources], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    dump(evidence / "baseline.json", {
        "captured_at": utc_now(), "git_head": head.stdout.strip(), "porcelain": initial_status.stdout.splitlines(),
        "binary_diff_sha256": sha(initial_diff.stdout), "binary_diff_bytes": len(initial_diff.stdout),
        "source_tree_sha256": sha(source_tree_material),
    })

    runner_copy = evidence / "audit_runner_source.py"
    shutil.copyfile(__file__, runner_copy)
    runner_copy_hash = sha(runner_copy.read_bytes())
    before = source_manifest()
    dump(evidence / "source_integrity_before.json", before)

    commands: list[dict] = []
    report_paths: list[Path] = []
    timeout_seen = False
    source_drift_seen = False

    secret_names = ("BRIGHTDATA_API_KEY", "GOOGLE_PLACES_API_KEY", "HUNTER_API_KEY", "BRANDFETCH_CLIENT_ID", "OPENROUTER_API_KEY")

    def run_command(gate: str, name: str, argv: list[str], *, timeout: int = 600, env_overrides: dict[str, str] | None = None) -> dict:
        nonlocal timeout_seen, source_drift_seen
        command_id = f"{gate.lower()}-{len(commands) + 1:02d}-{hashlib.sha256(json.dumps(argv).encode()).hexdigest()[:12]}"
        command_source_before = source_manifest()
        overrides = {key: "" for key in secret_names}
        overrides.update(env_overrides or {})
        overrides.update({
            "B2B_COMMAND_ID": command_id,
            "B2B_PRODUCTION_TRACE_JSONL": str(evidence / "traces" / f"{command_id}.jsonl"),
            "B2B_PRODUCTION_TRACE_NODES": ",".join(REQUIRED_NODES),
            "B2B_PRODUCTION_TRACE_TARGETS": json.dumps({key: sorted(value) for key, value in REQUIREMENT_ENTRYPOINTS.items()}, separators=(",", ":")),
            "B2B_PRODUCTION_TRACE_LATE_NODES": ",".join(("test_resume_query_plan_or_config_drift_returns_cli_22_before_network", "test_resume_llm_budget_drift_is_rejected_before_saved_credentials", "test_finalization_rejects_each_telemetry_replica_drift", "test_evidence_capture_rejects_missing_journal_manifest_or_artifact")),
            "B2B_SOCKET_DENY_JSONL": str(evidence / "network" / f"{command_id}.jsonl"),
        })
        network_path = Path(overrides["B2B_SOCKET_DENY_JSONL"])
        network_path.parent.mkdir(parents=True, exist_ok=True)
        network_path.write_text("", encoding="utf-8")
        env = os.environ.copy()
        env.update(overrides)
        stdout_path = evidence / "logs" / f"{name}.stdout.txt"
        stderr_path = evidence / "logs" / f"{name}.stderr.txt"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        started_utc = utc_now(); started_mono = time.monotonic(); timed_out = False
        try:
            result = subprocess.run(argv, cwd=REPO, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout)
            exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True; timeout_seen = True; exit_code = 124
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        ended_mono = time.monotonic(); ended_utc = utc_now()
        stdout_path.write_text(stdout, encoding="utf-8"); stderr_path.write_text(stderr, encoding="utf-8")
        command_source_after = source_manifest()
        stable = command_source_before == command_source_after and sha(Path(__file__).read_bytes()) == runner_copy_hash
        source_drift_seen = source_drift_seen or not stable
        record = {
            "command_id": command_id, "gate": gate, "name": name, "argv": argv,
            "environment_overrides": {key: ("[EMPTY]" if not value else value) for key, value in overrides.items()},
            "started_at": started_utc, "ended_at": ended_utc,
            "duration_seconds": round(ended_mono - started_mono, 6), "exit_code": exit_code,
            "timeout_seconds": timeout, "timed_out": timed_out,
            "source_stable": stable,
            "source_before_sha256": sha(json.dumps(command_source_before, sort_keys=True).encode()),
            "source_after_sha256": sha(json.dumps(command_source_after, sort_keys=True).encode()),
            "stdout": stdout_path.relative_to(evidence).as_posix(), "stderr": stderr_path.relative_to(evidence).as_posix(),
        }
        commands.append(record)
        return record

    py = sys.executable
    run_command("K1", "k1_compileall", [py, "-m", "compileall", "-q", "main.py", "modules", "tests", "tools"])
    run_command("K1", "k1_pip_check", [py, "-m", "pip", "check"])

    pytest_common = [py, "-m", "pytest", "-q", "-rs", "-o", "faulthandler_timeout=60", "-o", "faulthandler_exit_on_timeout=true"]
    k2_jsonl = evidence / "pytest_k2.jsonl"; report_paths.append(k2_jsonl)
    run_command("K2", "k2_search_regressions", pytest_common + ["tests/test_search_phase_regressions.py"], env_overrides={"B2B_PYTEST_REPORT_JSONL": str(k2_jsonl)})
    k3_jsonl = evidence / "pytest_k3.jsonl"; report_paths.append(k3_jsonl)
    run_command("K3", "k3_critical_package", pytest_common + [
        "tests/test_live_run_go_contract.py", "tests/test_operational_contracts.py", "tests/test_p6_package.py",
        "tests/test_pipeline_integration_guardrails.py", "tests/test_run_state_isolation.py",
    ], env_overrides={"B2B_PYTEST_REPORT_JSONL": str(k3_jsonl)})
    k4_jsonl = evidence / "pytest_k4.jsonl"; report_paths.append(k4_jsonl)
    run_command("K4", "k4_full_suite", pytest_common, timeout=900, env_overrides={"B2B_PYTEST_REPORT_JSONL": str(k4_jsonl)})

    scenarios = SCENARIO_NODES
    for scenario, node in scenarios.items():
        scenario_root = evidence / scenario
        jsonl = evidence / f"pytest_k5_{scenario}.jsonl"; report_paths.append(jsonl)
        record = run_command("K5", f"k5_{scenario}", pytest_common + [f"tests/test_search_phase_regressions.py::{node}"], env_overrides={"B2B_PYTEST_REPORT_JSONL": str(jsonl), "B2B_FINAL_SCENARIO_DIR": str(evidence)})
        if scenario_root.is_dir():
            dump(scenario_root / "command.json", record)
            dump(scenario_root / "duration.json", {key: record[key] for key in ("started_at", "ended_at", "duration_seconds", "exit_code", "timed_out")})
            if jsonl.is_file():
                shutil.copyfile(jsonl, scenario_root / "pytest_reports.jsonl")

    k6_negative_architecture = [
        "test_provider_usage_corruption_in_each_aggregate_column_blocks_finalization",
        "test_provider_set_missing_or_extra_row_blocks_telemetry_and_finalization",
        "test_current_schema_process_restart_does_not_heal_provider_aggregate_corruption",
        "test_complete_checkpoint_schema_open_is_byte_for_byte_read_only",
        "test_incomplete_v11_schema_migrates_transport_receipts_to_v12",
        "test_legacy_free_migration_reconciles_exact_bucket_state_multiset",
        "test_relation_tables_reject_orphan_and_cross_scope_rows_with_foreign_keys",
        "test_paid_evidence_unknown_has_precedence_over_done",
        "test_paid_evidence_done_rejects_blocked_budget_outcome",
        "test_paid_no_call_evidence_requires_exact_zero_call_or_inherited_receipt",
        "test_paid_adapters_do_not_mark_done_before_semantic_validation",
        "test_paid_adapter_cache_write_failure_does_not_change_terminal_result",
        "test_openrouter_invalid_structured_verdict_is_failed_not_done",
        "test_http_start_marker_is_exactly_once",
        "test_expired_flight_failed_then_done_receipt_reconciles_done",
        "test_reclaimed_flight_preserves_all_prior_call_ids_append_only",
        "test_provider_query_execution_generation_is_monotonic_and_relational",
        "test_new_provider_query_generation_requires_matching_terminal_receipt",
        "test_new_provider_query_generation_accepts_reconciled_done_receipt",
        "test_non_atomic_flight_finish_rejects_done",
        "test_generation_receipts_reject_cross_generation_mutation",
        "test_atomic_flight_success_rejects_unbound_provider_call",
        "test_run_config_requires_exact_canonical_provider_budget_set",
        "test_resume_query_plan_or_config_drift_returns_cli_22_before_network",
        "test_resume_llm_budget_drift_is_rejected_before_saved_credentials",
        "test_paid_targeted_query_plan_is_append_only_across_real_rounds_and_resume",
        "test_finalization_rejects_each_telemetry_replica_drift",
        "test_evidence_capture_rejects_missing_journal_manifest_or_artifact",
        "test_offline_paid_network_guard_fails_on_every_default_transport_fallthrough",
        "test_network_receipt_validator_derives_zero_external_calls_and_rejects_forgery",
        "test_k6_receipt_validator_rejects_missing_duplicate_and_forged_cases",
        "test_audit_scenario_validator_rejects_each_tampered_required_artifact",
        "test_audit_scenario_validator_rejects_rehashed_semantic_command_forgery",
        "test_audit_scenario_validator_rejects_rehashed_transport_and_relational_forgery",
        "test_root_artifact_hash_is_a_marker_gate_and_includes_nested_manifests",
        "test_contract_matrix_has_no_fallback_and_replays_every_receipt",
    ]
    k6_jsonl = evidence / "pytest_k6.jsonl"; report_paths.append(k6_jsonl)
    k6_receipt_jsonl = evidence / "k6_mutation_receipts.jsonl"
    k6_command = run_command(
        "K6", "k6_negative_architecture",
        pytest_common + [f"tests/test_search_phase_regressions.py::{node}" for node in k6_negative_architecture],
        env_overrides={"B2B_PYTEST_REPORT_JSONL": str(k6_jsonl), "B2B_K6_RECEIPT_JSONL": str(k6_receipt_jsonl), "B2B_K6_EXPECTED_CLI_EXITS": json.dumps(K6_EXPECTED_CLI_EXITS, separators=(",", ":"))},
    )
    run_command("K7", "k7_historical_readonly", [py, "tools/historical_readonly_audit.py", "--root", str(REPO), "--output", str(evidence / "historical_readonly_audit.json")])

    rows = read_reports(report_paths)
    calls = call_outcomes(rows)
    outcome_counts: dict[str, int] = {}
    for row in rows:
        if row.get("phase") == "call":
            outcome = str(row.get("outcome", "")); outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    skipped = sorted({str(row.get("nodeid")) for row in rows if row.get("outcome") == "skipped"})
    xnodes = sorted({str(row.get("nodeid")) for row in calls.values() if row.get("wasxfail")})
    skip_audit = {"expected_nodes": sorted(EXPECTED_SKIPS), "actual_nodes": skipped, "wasxfail_nodes": xnodes, "pass": set(skipped) == EXPECTED_SKIPS and not xnodes}
    dump(evidence / "skip_audit.json", skip_audit)

    dump(evidence / "commands.json", commands)
    scenario_results = {}
    for scenario in scenarios:
        if (evidence / scenario).is_dir():
            receipts = [file_receipt(path, relative_to=evidence / scenario) for path in sorted((evidence / scenario).iterdir()) if path.is_file() and path.name != "artifact_hashes.json"]
            dump(evidence / scenario / "artifact_hashes.json", receipts)
        result = validate_scenario(evidence / scenario, command_records=commands)
        scenario_results[scenario] = result
    network_validation = validate_network_receipts(evidence, commands, rows, scenario_results)
    dump(evidence / "network_summary.json", network_validation)

    k6_calls = {"|".join(map(str, key)): row for key, row in calls.items() if row.get("report_path", "").endswith("pytest_k6.jsonl")}
    k6_actual_nodes = {str(row.get("nodeid", "")).split("::")[-1].split("[")[0] for row in k6_calls.values()}
    k6_receipt_validation = validate_k6_receipts(k6_receipt_jsonl, evidence, rows, str(k6_command["command_id"]))
    negative = {
        "nodes": k6_calls, "expected_base_nodes": sorted(k6_negative_architecture), "actual_base_nodes": sorted(k6_actual_nodes),
        "exact_node_set": k6_actual_nodes == set(k6_negative_architecture),
        "receipt_validation": k6_receipt_validation,
        "all_call_passed": bool(k6_calls) and k6_actual_nodes == set(k6_negative_architecture) and all(row.get("outcome") == "passed" and not row.get("wasxfail") for row in k6_calls.values()) and k6_receipt_validation.get("pass", False),
    }
    dump(evidence / "negative_probe_results.json", negative)

    required_nodes = list(REQUIRED_NODES)
    trace_rows = []
    for trace_path in sorted((evidence / "traces").glob("*.jsonl")):
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line); row["trace_path"] = trace_path.relative_to(evidence).as_posix(); trace_rows.append(row)
    matrix = {}
    for number, short in enumerate(required_nodes, 1):
        matches = [(str(row.get("nodeid")), row) for row in calls.values() if str(row.get("nodeid", "")).split("::")[-1].split("[")[0] == short]
        command_records = sorted({str(row.get("command_id")) for _, row in matches})
        node_traces = [row for row in trace_rows if row.get("nodeid") in {node for node, _ in matches} and row.get("command_id") in command_records]
        k6_case_receipts = [
            receipt for receipt in k6_receipt_validation.get("receipt_paths", [])
            if any(str(entry.get("nodeid", "")) == str(node) for node, _row in matches for entry in [json.loads((evidence / receipt).read_text(encoding="utf-8"))])
        ]
        receipt_paths = sorted({str(row.get("report_path")) for _, row in matches} | {str(row.get("trace_path")) for row in node_traces} | set(k6_case_receipts))
        matrix[f"R{number}"] = {
            "node": short, "collected_nodeids": [node for node, _ in matches], "command_ids": command_records,
            "outcomes": [row.get("outcome") for _, row in matches],
            "production_function_entries": [
                {"command_id": row["command_id"], "nodeid": row["nodeid"], "trace_path": row["trace_path"], "function_entries": row.get("function_entries", [])}
                for row in node_traces
            ],
            "receipt_paths": receipt_paths,
            "pass": bool(matches) and len(node_traces) == len(matches) and all(REQUIREMENT_ENTRYPOINTS[short].intersection(map(str, row.get("function_entries", []))) for row in node_traces) and all(row.get("outcome") == "passed" and not row.get("wasxfail") for _, row in matches),
        }
    dump(evidence / "test_contract_matrix.json", matrix)
    contract_validation = validate_contract_matrix(evidence / "test_contract_matrix.json", evidence, command_records=commands)

    meta_results = {"scenario_mutations": {}, "semantic_rehashed": {}, "contract_matrix_mutations": {}, "root_hash": {}}
    valid_scenario = next((evidence / name for name, result in scenario_results.items() if result.get("pass")), None)
    scenario_mutations = ["frozen_config.json", "paid_query_plan.json", "state_transition_trace.json", "command.json", "duration.json", "pytest_reports.jsonl", "sanitized_progress.sqlite3", "recording_transport_journal.json", "manifest.json", "scenario_result.json", "artifact_hashes.json"]
    if valid_scenario:
        with tempfile.TemporaryDirectory(prefix="closure-k8-", ignore_cleanup_errors=True) as temp_root:
            for filename in scenario_mutations:
                copy_root = Path(temp_root) / hashlib.sha256(filename.encode()).hexdigest()[:12]
                shutil.copytree(valid_scenario, copy_root)
                target = copy_root / filename
                if target.suffix == ".sqlite3": target.write_bytes(target.read_bytes() + b"tamper")
                elif target.suffix == ".jsonl": target.write_text("{}\n", encoding="utf-8")
                else: target.write_text("{}", encoding="utf-8")
                validation = validate_scenario(copy_root, command_records=commands)
                meta_results["scenario_mutations"][filename] = {"validator_pass": validation.get("pass", False), "pass": not validation.get("pass", False)}
    semantic_source = evidence / "authorized_free_paid"
    if scenario_results.get("authorized_free_paid", {}).get("pass"):
        with tempfile.TemporaryDirectory(prefix="closure-k8-semantic-", ignore_cleanup_errors=True) as temp_root:
            for mutation in ("empty_manifest", "deleted_receipt_chain", "cross_provider_endpoint"):
                copy_root = Path(temp_root) / mutation
                shutil.copytree(semantic_source, copy_root)
                db_path = copy_root / "sanitized_progress.sqlite3"
                if mutation == "empty_manifest":
                    manifest_path = copy_root / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8")); manifest["files"] = {}
                    manifest["artifact_set_sha256"] = sha(b"")
                    dump(manifest_path, manifest)
                    with sqlite3.connect(db_path) as db:
                        db.execute("UPDATE finalization_intent SET artifact_set_sha256=?,manifest_sha256=?", (manifest["artifact_set_sha256"], sha(manifest_path.read_bytes())))
                        db.commit(); db.execute("PRAGMA wal_checkpoint(TRUNCATE)"); db.execute("PRAGMA journal_mode=DELETE")
                elif mutation == "deleted_receipt_chain":
                    with sqlite3.connect(db_path) as db:
                        trigger_sql = [db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()[0] for name in ("flight_result_receipt_delete", "flight_terminal_receipt_delete")]
                        db.execute("DROP TRIGGER flight_result_receipt_delete"); db.execute("DROP TRIGGER flight_terminal_receipt_delete")
                        db.execute("DELETE FROM provider_query_flight_consumers"); db.execute("DELETE FROM provider_query_flight_terminals")
                        db.execute("DELETE FROM provider_query_flight_results"); db.execute("DELETE FROM provider_query_flights")
                        for statement in trigger_sql: db.execute(statement)
                        db.commit(); db.execute("PRAGMA wal_checkpoint(TRUNCATE)"); db.execute("PRAGMA journal_mode=DELETE")
                else:
                    journal_path = copy_root / "recording_transport_journal.json"
                    journal = json.loads(journal_path.read_text(encoding="utf-8")); journal[0]["endpoint"] = "https://api.hunter.io/v2/domain-search"
                    dump(journal_path, journal)
                    with sqlite3.connect(db_path) as db:
                        trigger_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='provider_call_transport_receipt_update'").fetchone()[0]
                        db.execute("DROP TRIGGER provider_call_transport_receipt_update")
                        db.execute("UPDATE provider_calls SET endpoint_sha256=? WHERE call_id=?", (sha(journal[0]["endpoint"].encode()), journal[0]["provider_call_id"]))
                        db.execute(trigger_sql)
                        db.commit(); db.execute("PRAGMA wal_checkpoint(TRUNCATE)"); db.execute("PRAGMA journal_mode=DELETE")
                dump(copy_root / "artifact_hashes.json", [file_receipt(path, relative_to=copy_root) for path in sorted(copy_root.iterdir()) if path.is_file() and path.name != "artifact_hashes.json"])
                validation = validate_scenario(copy_root, command_records=commands)
                meta_results["semantic_rehashed"][mutation] = {"validator_pass": validation.get("pass", False), "pass": not validation.get("pass", False)}
    for field in ("collected_nodeids", "command_ids", "production_function_entries", "receipt_paths", "outcomes"):
        mutated = json.loads(json.dumps(matrix)); first_key = next(iter(mutated)); mutated[first_key][field] = []
        temp_matrix = evidence / f".k8_matrix_{field}.json"; dump(temp_matrix, mutated)
        validation = validate_contract_matrix(temp_matrix, evidence, command_records=commands); temp_matrix.unlink()
        meta_results["contract_matrix_mutations"][field] = {"validator_pass": validation.get("pass", False), "pass": not validation.get("pass", False)}
    with tempfile.TemporaryDirectory(prefix="closure-root-hash-", ignore_cleanup_errors=True) as temp_root:
        candidate = Path(temp_root)
        for index in range(8):
            nested = candidate / f"scenario-{index}"; nested.mkdir(); (nested / "artifact_hashes.json").write_text("[]", encoding="utf-8")
        marker_file = candidate / "final_report.md"; marker_file.write_text("SEARCH_FLOW_FIX_COMPLETE_CLOSURE_AUDIT", encoding="utf-8")
        write_artifact_hashes(candidate); before_meta = validate_artifact_hashes(candidate)
        marker_file.write_text("tampered", encoding="utf-8"); after_meta = validate_artifact_hashes(candidate)
        meta_results["root_hash"] = {"before": before_meta, "after": after_meta, "pass": before_meta.get("pass") and not after_meta.get("pass")}
    meta_results["pass"] = bool(valid_scenario) and len(meta_results["semantic_rehashed"]) == 3 and all(row["pass"] for row in meta_results["scenario_mutations"].values()) and all(row["pass"] for row in meta_results["semantic_rehashed"].values()) and all(row["pass"] for row in meta_results["contract_matrix_mutations"].values()) and bool(meta_results["root_hash"].get("pass"))
    dump(evidence / "k8_fail_closed_meta.json", meta_results)

    historical_path = evidence / "historical_readonly_audit.json"
    historical = json.loads(historical_path.read_text(encoding="utf-8")) if historical_path.is_file() else {}
    def required_node_pass(node: str) -> bool:
        selected = [row for row in calls.values() if str(row.get("nodeid", "")).split("::")[-1].split("[")[0] == node]
        return bool(selected) and all(row.get("outcome") == "passed" and not row.get("wasxfail") for row in selected)

    ledger_actual = {name: {"provider_aggregate_exact": row.get("provider_aggregate_exact"), "providers": sorted(row.get("provider_aggregates", {}))} for name, row in scenario_results.items()}
    free_actual = {
        "logical": scenario_results.get("free_caps", {}).get("free_logical", {}),
        "physical": scenario_results.get("free_caps", {}).get("free_physical", {}),
    }
    relation_actual = {name: {"foreign_key_violations": row.get("foreign_key_violations", []), "relation_counts": row.get("relation_counts", {})} for name, row in scenario_results.items()}
    query_plan_actual = {name: {"plan_exact": row.get("plan_exact"), "phase": row.get("phase")} for name, row in scenario_results.items()}
    singleflight_actual = {name: {"journal": scenario_results.get(name, {}).get("journal_count"), "relations": scenario_results.get(name, {}).get("relation_counts", {})} for name in ("singleflight_success", "singleflight_failure")}
    invariant_audit = {
        "commands": {"source_artifacts": ["commands.json"], "equation": "all subprocess exit_code == 0 and not timed_out", "actual": [{"name": row["name"], "exit_code": row["exit_code"], "timed_out": row["timed_out"]} for row in commands], "expected": "all zero/no timeout", "pass": all(row["exit_code"] == 0 and not row["timed_out"] for row in commands)},
        "scenarios": {"source_artifacts": [f"{name}/scenario_result.json" for name in scenarios], "equation": "eight independent artifact validators pass", "actual": scenario_results, "expected": sorted(scenarios), "pass": len(scenario_results) == 8 and all(row.get("pass") for row in scenario_results.values())},
        "pytest": {"source_artifacts": [path.relative_to(evidence).as_posix() for path in report_paths], "equation": "all call phases pass except exact seven full-suite skips; no xfail", "actual": outcome_counts, "expected": {"failed": 0, "xfailed": 0, "xpassed": 0}, "pass": not any(row.get("outcome") == "failed" or row.get("wasxfail") for row in calls.values())},
        "skip_set": {"source_artifacts": ["skip_audit.json"], "equation": "set(actual skipped nodeids) == allowlist", "actual": skipped, "expected": sorted(EXPECTED_SKIPS), "pass": skip_audit["pass"]},
        "negative_probes": {"source_artifacts": ["pytest_k6.jsonl", "k6_mutation_receipts.jsonl", *k6_receipt_validation.get("receipt_paths", []), "negative_probe_results.json"], "equation": "set(K6 base nodeids) == frozen selection; each parametrized mutation has one re-read before/after receipt; every call passed without xfail", "actual": {"base_nodes": sorted(k6_actual_nodes), "call_count": len(k6_calls), "receipts": k6_receipt_validation.get("actual")}, "expected": {"base_nodes": sorted(k6_negative_architecture), "receipts": k6_receipt_validation.get("expected"), "all_passed": True}, "pass": negative["all_call_passed"]},
        "ledger_provider_set": {"source_artifacts": [f"{name}/sanitized_progress.sqlite3" for name in scenarios], "equation": "provider set == canonical six and each provider_usage aggregate == GROUP BY provider_calls", "actual": ledger_actual, "expected": {"providers": sorted(CANONICAL_PROVIDER_SET), "all_aggregate_exact": True}, "pass": len(ledger_actual) == 8 and all(row.get("provider_aggregate_exact") and set(row.get("provider_aggregates", {})) == CANONICAL_PROVIDER_SET for row in scenario_results.values())},
        "free_6_4_10": {"source_artifacts": ["free_caps/sanitized_progress.sqlite3"], "equation": "SUM logical and physical used=10; each discovery=6 and targeted=4", "actual": free_actual, "expected": {"logical": {"total": 10, "discovery": 6, "targeted": 4}, "physical": {"total": 10, "discovery": 6, "targeted": 4}}, "pass": free_actual == {"logical": {"total": 10, "discovery": 6, "targeted": 4}, "physical": {"total": 10, "discovery": 6, "targeted": 4}}},
        "relation_and_fk": {"source_artifacts": [f"{name}/sanitized_progress.sqlite3" for name in scenarios] + ["pytest_k2.jsonl"], "equation": "PRAGMA foreign_key_check empty in every scenario and cross-scope relation negative test passes", "actual": relation_actual, "expected": {"foreign_key_violations": [], "negative_node_pass": True}, "pass": all(not row.get("foreign_key_violations") for row in scenario_results.values()) and required_node_pass("test_relation_tables_reject_orphan_and_cross_scope_rows_with_foreign_keys")},
        "singleflight_generation_result_reclaim_circuit": {"source_artifacts": ["singleflight_success/sanitized_progress.sqlite3", "singleflight_failure/sanitized_progress.sqlite3", "pytest_k2.jsonl"], "equation": "8 workers reconcile to one owner call; generation/result/reclaim/append-only/binding/circuit nodes all pass", "actual": singleflight_actual, "expected": {"success": {"journal": 1, "OWNER": 1, "INHERITED": 7}, "failure": {"journal": 1, "OWNER": 1, "INHERITED": 7}}, "pass": all(scenario_results.get(name, {}).get("journal_count") == 1 and scenario_results.get(name, {}).get("relation_counts") == {"INHERITED": 7, "OWNER": 1} for name in ("singleflight_success", "singleflight_failure")) and all(required_node_pass(node) for node in ("test_expired_flight_failed_then_done_receipt_reconciles_done", "test_reclaimed_flight_preserves_all_prior_call_ids_append_only", "test_provider_query_execution_generation_is_monotonic_and_relational", "test_new_provider_query_generation_requires_matching_terminal_receipt", "test_new_provider_query_generation_accepts_reconciled_done_receipt", "test_non_atomic_flight_finish_rejects_done", "test_generation_receipts_reject_cross_generation_mutation", "test_atomic_flight_success_rejects_unbound_provider_call", "test_volatile_same_query_independent_failures_open_circuit"))},
        "heartbeat_coverage": {"source_artifacts": ["heartbeat_throttle_backoff/sanitized_progress.sqlite3", "pytest_k2.jsonl"], "equation": "throttle/HTTP/decode/retry scenario and clocked heartbeat node pass", "actual": {"scenario": scenario_results.get("heartbeat_throttle_backoff", {}), "clocked_node_pass": required_node_pass("test_singleflight_clocked_heartbeat_covers_every_wait_region")}, "expected": {"scenario_pass": True, "http": 2, "clocked_node_pass": True}, "pass": bool(scenario_results.get("heartbeat_throttle_backoff", {}).get("pass")) and scenario_results.get("heartbeat_throttle_backoff", {}).get("journal_count") == 2 and required_node_pass("test_singleflight_clocked_heartbeat_covers_every_wait_region")},
        "query_plan_equality": {"source_artifacts": [f"{name}/paid_query_plan.json" for name in scenarios] + [f"{name}/manifest.json" for name in scenarios] + [f"{name}/sanitized_progress.sqlite3" for name in scenarios], "equation": "ordered DB plan == plan artifact and manifest count/hash for every scenario; primary/targeted/resume drift nodes pass", "actual": query_plan_actual, "expected": {"all_plan_exact": True}, "pass": all(row.get("plan_exact") for row in scenario_results.values()) and all(required_node_pass(node) for node in ("test_real_fresh_handoff_resume_preserves_durable_ordered_query_plan", "test_paid_targeted_query_plan_is_append_only_across_real_rounds_and_resume", "test_resume_query_plan_or_config_drift_returns_cli_22_before_network"))},
        "telemetry_replicas": {"source_artifacts": [f"{name}/manifest.json" for name in scenarios] + [f"{name}/sanitized_progress.sqlite3" for name in scenarios] + ["pytest_k2.jsonl"], "equation": "canonical provider ledgers are exact and intent/manifest/telemetry/report drift tests all pass", "actual": {"provider_ledgers": ledger_actual, "replica_node_pass": required_node_pass("test_finalization_rejects_each_telemetry_replica_drift"), "report_node_pass": required_node_pass("test_report_rejects_missing_provider_set")}, "expected": {"all": True}, "pass": all(row.get("provider_aggregate_exact") for row in scenario_results.values()) and required_node_pass("test_finalization_rejects_each_telemetry_replica_drift") and required_node_pass("test_report_rejects_missing_provider_set")},
        "junction_target": {"source_artifacts": ["pytest_k2.jsonl"], "equation": "real junction retarget changes protected manifest while both targets remain intact", "actual": {"node_pass": required_node_pass("test_windows_junction_retarget_changes_protected_manifest")}, "expected": {"node_pass": True}, "pass": required_node_pass("test_windows_junction_retarget_changes_protected_manifest")},
        "historical_readonly": {"source_artifacts": ["historical_readonly_audit.json"], "equation": "before == after and every integrity == ok", "actual": historical, "expected": "unchanged/integrity ok", "pass": bool(historical.get("unchanged")) and all(row.get("integrity") == "ok" for row in historical.get("runs", []))},
        "external_paid_network": {"source_artifacts": ["network_summary.json", *network_validation.get("receipt_paths", []), *[f"{name}/recording_transport_journal.json" for name in scenarios]], "equation": "every pytest child arms the process audit guard; only the six-provider fallthrough probe may trigger blocked events; recorder envelopes reconcile separately; no event can cross the guard", "actual": network_validation.get("actual"), "expected": network_validation.get("expected"), "pass": network_validation.get("pass", False)},
        "contract_matrix": {"source_artifacts": ["test_contract_matrix.json", "commands.json", *sorted({receipt for value in matrix.values() for receipt in value.get("receipt_paths", [])})], "equation": "exact requirement/node set joins independently parsed pytest calls, command records and production traces", "actual": contract_validation, "expected": {"requirements": len(required_nodes), "failures": []}, "pass": contract_validation.get("pass", False)},
        "audit_fail_closed_meta": {"source_artifacts": ["k8_fail_closed_meta.json"], "equation": "every disposable evidence mutation is rejected and pristine evidence passes", "actual": meta_results, "expected": "all mutation validators fail; originals pass", "pass": meta_results["pass"]},
        "nested_artifact_hashes": {"source_artifacts": [f"{name}/artifact_hashes.json" for name in scenarios] + ["k8_fail_closed_meta.json"], "equation": "all eight nested manifests exactly re-read and root-hash tamper meta-probe rejects mutation", "actual": {name: row.get("nested_hash_exact") for name, row in scenario_results.items()}, "expected": {"nested_exact": True, "root_meta_pass": True}, "pass": len(scenario_results) == 8 and all(row.get("nested_hash_exact") for row in scenario_results.values()) and bool(meta_results.get("root_hash", {}).get("pass"))},
    }

    after = source_manifest()
    dump(evidence / "source_integrity_after.json", after)
    source_stable = before == after and sha(Path(__file__).read_bytes()) == runner_copy_hash and not source_drift_seen
    invariant_audit["source_integrity"] = {"source_artifacts": ["audit_runner_source.py", "source_integrity_before.json", "source_integrity_after.json"], "equation": "before bytes/hash/mtime == after and executed runner bytes == copied bytes", "actual": {"manifests_equal": before == after, "runner_bytes_equal": sha(Path(__file__).read_bytes()) == runner_copy_hash}, "expected": {"manifests_equal": True, "runner_bytes_equal": True}, "pass": source_stable}
    dump(evidence / "invariant_audit.json", invariant_audit)
    command_gates = {str(row["command_id"]): str(row["gate"]) for row in commands}
    gate_results: dict[str, dict] = {}
    for row in rows:
        gate = command_gates.get(str(row.get("command_id", "")), "UNKNOWN")
        summary = gate_results.setdefault(gate, {"phase_outcomes": {}, "call_outcomes": {}, "subtests": 0, "reports": 0})
        phase_key = f"{row.get('phase', '')}:{row.get('outcome', '')}"
        summary["phase_outcomes"][phase_key] = summary["phase_outcomes"].get(phase_key, 0) + 1
        summary["reports"] += 1
        if row.get("phase") == "call":
            outcome = str(row.get("outcome", "")); summary["call_outcomes"][outcome] = summary["call_outcomes"].get(outcome, 0) + 1
            summary["subtests"] += int(bool(row.get("subtest")))
    dump(evidence / "after_test_results.json", {"gate_results": gate_results, "pytest_call_outcomes": outcome_counts, "total_call_reports": len(calls), "commands": len(commands)})

    status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
    diff = subprocess.run(["git", "diff", "--binary", "--no-ext-diff"], cwd=REPO, capture_output=True, text=False)
    dump(evidence / "changed_files.json", {"porcelain": status.stdout.splitlines(), "binary_diff_sha256": sha(diff.stdout), "binary_diff_bytes": len(diff.stdout)})

    all_invariants = all(row.get("pass") for row in invariant_audit.values())
    if timeout_seen:
        marker = "FULL_SUITE_TIMEOUT"
    elif not source_stable:
        marker = "SEARCH_FLOW_FIX_EVIDENCE_FAILURE"
    elif any(row.get("outcome") == "failed" for row in calls.values()) or any(record["exit_code"] for record in commands):
        marker = "SEARCH_FLOW_FIX_TEST_FAILURE"
    elif not skip_audit["pass"]:
        marker = "SEARCH_FLOW_FIX_UNAPPROVED_TEST_OUTCOME"
    elif not all_invariants:
        marker = "SEARCH_FLOW_FIX_INVARIANT_FAILURE"
    else:
        marker = "SEARCH_FLOW_FIX_COMPLETE_CLOSURE_AUDIT"
    def report_cases(node: str) -> list[dict]:
        return [
            {"nodeid": row.get("nodeid"), "command_id": row.get("command_id"), "outcome": row.get("outcome"), "wasxfail": row.get("wasxfail", "")}
            for row in calls.values() if str(row.get("nodeid", "")).split("::")[-1].split("[")[0] == node
        ]

    lines = [
        "# Search flow verified closure audit", "",
        "## 1. Kapatılan önceki ret nedenleri", "",
        "- Provider semantic terminalization, UNKNOWN stop, frozen resume config, exact ledger/provider set, migration ve kanıt türetimi gerçek artifact doğrulayıcılarıyla yeniden sınandı.",
        "- Generation call-id sızıntısı, LLM budget resume sapması, semantik command/matrix sahteciliği ve literal ağ-sıfır öz-beyanı kapatıldı.", "",
        "## 2. P1–P6 uygulama yüzeyi", "",
        "- P1: `modules/search.py`, `modules/company_resolvers.py`, `modules/google_places.py`, `modules/hunter.py`, `modules/linkedin_company.py`, `modules/llm_arbiter.py` — semantic terminalization.",
        "- P2: `main.py`, `modules/pipeline_runner.py`, `modules/run_context.py`, `modules/checkpoint.py` — typed resume, frozen config ve schema 10/11→12.",
        "- P3/P4: `modules/checkpoint.py`, `modules/runtime.py`, `modules/search.py` — relational evidence, generation, heartbeat ve circuit.",
        "- P5/P6: `modules/search.py`, `modules/checkpoint.py`, `modules/report.py`, `modules/output_artifacts.py` — query plan, canonical telemetry ve finalization.", "",
        "## 3. Requirement → node → command → trace → receipt", "",
    ]
    for requirement, value in matrix.items():
        traces = sorted({entry.get("trace_path", "") for entry in value.get("production_function_entries", [])})
        lines.append(f"- {requirement} `{value.get('node')}`: commands={value.get('command_ids')}; traces={traces}; receipts={value.get('receipt_paths')}; pass={value.get('pass')}")
    lines.extend(["", "## 4. K1–K8 gerçek komutlar ve gate sonuçları", ""])
    for row in commands:
        lines.append(f"- {row['gate']} `{row['name']}`: exit={row['exit_code']}, timeout={row['timed_out']}, süre={row['duration_seconds']}s, başlangıç={row['started_at']}, bitiş={row['ended_at']}; argv=`{json.dumps(row['argv'], ensure_ascii=False)}`")
    lines.append(f"- Gate outcome totals: `{json.dumps(gate_results, ensure_ascii=False, sort_keys=True)}`")
    lines.extend(["", "## 5. Pytest outcome ve exact skip denetimi", ""])
    lines.append(f"- Call outcomes={outcome_counts}; failed/error/xfail/xpass=0 koşulu={invariant_audit['pytest']['pass']}; skip exact={skip_audit['pass']}.")
    lines.append(f"- Expected skips={sorted(EXPECTED_SKIPS)}")
    lines.append(f"- Actual skips={skipped}")
    lines.extend(["", "## 6. Sekiz production-path offline E2E", ""])
    for name, result in scenario_results.items():
        lines.append(f"- {name}: phase_path={result.get('phase_path')}; outcome={result.get('derived_outcome')}; CLI/test exit={result.get('command_exit')}; journal={result.get('journal_count')}; HTTP-start={result.get('http_started_count')}; reconciliation={result.get('journal_ledger_exact')}; pass={result.get('pass')}")
    lines.extend(["", "## 7. Altı-provider UNKNOWN stop matrisi", "", f"`{json.dumps(report_cases('test_unknown_each_paid_adapter_stops_all_later_paid_calls'), ensure_ascii=False, sort_keys=True)}`", "", "## 8. Adapter malformed/empty/cache-error terminalization matrisi", ""])
    for node in ("test_paid_adapters_do_not_mark_done_before_semantic_validation", "test_paid_adapter_cache_write_failure_does_not_change_terminal_result", "test_openrouter_invalid_structured_verdict_is_failed_not_done"):
        lines.append(f"- {node}: `{json.dumps(report_cases(node), ensure_ascii=False, sort_keys=True)}`")
    lines.extend(["", "## 9. Provider seti ve aggregate↔call denklemleri", "", f"`{json.dumps(ledger_actual, ensure_ascii=False, sort_keys=True)}`", "", "## 10. Free 6/4/10 logical/physical denklemleri", "", f"- actual={free_actual}; expected=logical/physical için ayrı ayrı total=10, discovery=6, targeted=4; pass={invariant_audit['free_6_4_10']['pass']}", "", "## 11. Relation/FK ve paid-evidence precedence", "", f"- relation/FK={json.dumps(relation_actual, ensure_ascii=False, sort_keys=True)}", f"- UNKNOWN>DONE, DONE>budget ve no-call cases pass={all(required_node_pass(node) for node in ('test_paid_evidence_unknown_has_precedence_over_done', 'test_paid_evidence_done_rejects_blocked_budget_outcome', 'test_paid_no_call_evidence_requires_exact_zero_call_or_inherited_receipt'))}", "", "## 12. Singleflight generation/result/reclaim/append-only/heartbeat/circuit", "", f"- actual={json.dumps(singleflight_actual, ensure_ascii=False, sort_keys=True)}; pass={invariant_audit['singleflight_generation_result_reclaim_circuit']['pass']}; heartbeat={invariant_audit['heartbeat_coverage']['pass']}", "", "## 13. Canonical telemetry replica eşitliği", "", f"- pass={invariant_audit['telemetry_replicas']['pass']}; sources={invariant_audit['telemetry_replicas']['source_artifacts']}", "", "## 14. Primary ve targeted ordered query-plan eşitliği", "", f"- actual={json.dumps(query_plan_actual, ensure_ascii=False, sort_keys=True)}; pass={invariant_audit['query_plan_equality']['pass']}", "", "## 15. Junction/reparse target receipt", "", f"- actual={invariant_audit['junction_target']['actual']}; pass={invariant_audit['junction_target']['pass']}", "", "## 16. Tarihsel salt-okunur koşular", "", f"`{json.dumps(historical, ensure_ascii=False, sort_keys=True)}`", "", "## 17. Paid endpoint ve process-level network denetimi", "", f"- inventory={network_validation.get('paid_endpoint_inventory')}; actual={network_validation.get('actual')}; expected={network_validation.get('expected')}; pass={network_validation.get('pass')}", "", "## 18. Source/runner ve artifact hash stabilitesi", "", f"- source pass={source_stable}; nested pass={invariant_audit['nested_artifact_hashes']['pass']}; K8 root mutation pass={meta_results.get('root_hash', {}).get('pass')}. Final report yazıldıktan sonra root manifest yeniden okunmadan process başarı dönmez.", "", "## 19. Kalan riskler", "", "- Gerçek ücretli canary yetkisi verilmedi; dış ağ çağrısı yapılmadı. Doğrulama offline recording transport ve fail-closed socket audit guard kapsamındadır.", "", "## Yeniden okunan invariantların tamamı", ""])
    for key, value in invariant_audit.items():
        lines.append(f"- {key}: pass={value['pass']}; equation={value['equation']}")
    lines.extend(["", "## 20. Nihai durum", "", marker, ""])
    (evidence / "final_report.md").write_text("\n".join(lines), encoding="utf-8")

    write_artifact_hashes(evidence)
    root_hash_validation = validate_artifact_hashes(evidence)
    if not root_hash_validation.get("pass"):
        marker = "SEARCH_FLOW_FIX_EVIDENCE_FAILURE"
        lines[-2] = marker
        (evidence / "final_report.md").write_text("\n".join(lines), encoding="utf-8")
        write_artifact_hashes(evidence)
        validate_artifact_hashes(evidence)
    return 0 if marker == "SEARCH_FLOW_FIX_COMPLETE_CLOSURE_AUDIT" and root_hash_validation.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
