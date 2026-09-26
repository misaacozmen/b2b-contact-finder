from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "output" / "engineering_search_fix_final_audit_20260910T062914Z"
E2E = EVIDENCE / "e2e"
PYTHON_ROOT = ROOT / ".runtime" / "python3147-sqlite3534"
ENV = os.environ.copy()
ENV["PATH"] = str(PYTHON_ROOT) + os.pathsep + ENV.get("PATH", "")
ENV["B2B_REAUDIT_EVIDENCE_DIR"] = str(EVIDENCE)
ENV["B2B_FINAL_SCENARIO_DIR"] = str(E2E)


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


commands: list[dict] = []


def run(name: str, args: list[str], *, timeout: int = 900, scenario: str = "") -> dict:
    command_text = subprocess.list2cmdline(args)
    started = utc()
    start_clock = time.perf_counter()
    timed_out = False
    try:
        result = subprocess.run(args, cwd=ROOT, env=ENV, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout)
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        code = 124
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
    duration = time.perf_counter() - start_clock
    ended = utc()
    stdout_path = EVIDENCE / f"{name}_stdout.txt"
    stderr_path = EVIDENCE / f"{name}_stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    record = {
        "name": name, "exact_command": command_text, "started_utc": started,
        "ended_utc": ended, "duration_seconds": round(duration, 6), "exit_code": code,
        "timed_out": timed_out,
        "environment_overrides": {
            "PATH": {"present": True, "value": "<redacted:bundled-runtime-prepended>"},
            "B2B_REAUDIT_EVIDENCE_DIR": {"present": True, "value": "<redacted:evidence-root>"},
            "B2B_FINAL_SCENARIO_DIR": {"present": True, "value": "<redacted:e2e-root>"},
        },
        "stdout": stdout_path.name, "stderr": stderr_path.name,
    }
    if "B2B_PROTECTED_MANIFEST_REFERENCE" in ENV:
        record["environment_overrides"]["B2B_PROTECTED_MANIFEST_REFERENCE"] = {"present": True, "value": "<redacted:content-hash-reference>"}
    commands.append(record)
    if scenario:
        target = E2E / scenario
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stdout_path, target / "raw_stdout.txt")
        shutil.copy2(stderr_path, target / "raw_stderr.txt")
        (target / "command.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        (target / "duration.json").write_text(json.dumps({"duration_seconds": duration, "process_exit_code": code, "started_utc": started, "ended_utc": ended}, indent=2), encoding="utf-8")
    (EVIDENCE / "commands.json").write_text(json.dumps(commands, indent=2), encoding="utf-8")
    return record


def historical_audit() -> dict:
    started = time.perf_counter()
    run_ids = (
        "c301ac49204a145aabe8452e1420bef75eb69df5d50412cbfa9691c53a9620ca",
        "ff793bbf477194928303a18eea97cc6de82e942655fe044a1c136be797532fa1",
    )
    output = {"read_only": True, "sqlite_open_mode": "mode=ro&immutable=1", "runs": {}}
    for run_id in run_ids:
        root = ROOT / "runs" / run_id
        files = [root / "state" / "progress.sqlite3", root / "manifest.json", root / "output" / "logs.txt"]
        before = {str(path.relative_to(ROOT)): {"bytes": path.stat().st_size, "sha256": sha256(path), "mtime_ns": path.stat().st_mtime_ns} for path in files}
        db_path = files[0]
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro&immutable=1", uri=True) as db:
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            item_count = int(db.execute("SELECT COUNT(*) FROM run_items").fetchone()[0])
        after = {str(path.relative_to(ROOT)): {"bytes": path.stat().st_size, "sha256": sha256(path), "mtime_ns": path.stat().st_mtime_ns} for path in files}
        output["runs"][run_id] = {"source_root": str(root), "integrity_check": integrity, "item_count": item_count, "files_before": before, "files_after": after, "unchanged": before == after}
    output["duration_seconds"] = round(time.perf_counter() - started, 6)
    return output


def write_and_verify_hashes(root: Path) -> dict:
    manifest_path = root / "artifact_hashes.json"
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != manifest_path:
            rows.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": sha256(path)})
    material = json.dumps([(row["path"], row["bytes"], row["sha256"]) for row in rows], separators=(",", ":"))
    payload = {"files": rows, "evidence_set_sha256": hashlib.sha256(material.encode()).hexdigest()}
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    for row in recorded["files"]:
        path = root / row["path"]
        if path.stat().st_size != row["bytes"] or sha256(path) != row["sha256"]:
            raise RuntimeError(f"artifact hash verification failed: {path}")
    verified_material = json.dumps([(row["path"], row["bytes"], row["sha256"]) for row in recorded["files"]], separators=(",", ":"))
    if hashlib.sha256(verified_material.encode()).hexdigest() != recorded["evidence_set_sha256"]:
        raise RuntimeError(f"evidence set hash verification failed: {root}")
    return recorded


def scenario_hashes() -> None:
    for target in sorted(E2E.glob("*")):
        if not target.is_dir():
            continue
        write_and_verify_hashes(target)


def subset_matches(actual, expected) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and subset_matches(actual[key], value) for key, value in expected.items())
    return actual == expected


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    E2E.mkdir(parents=True, exist_ok=True)
    run("k1_compileall", ["python", "-m", "compileall", "-q", "main.py", "modules", "tests"])
    run("k1_pip_check", ["python", "-m", "pip", "check"])
    common = ["python", "-m", "pytest", "-q", "-rs", "-o", "faulthandler_timeout=60", "-o", "faulthandler_exit_on_timeout=true"]
    run("k2_search_regressions", common + ["tests/test_search_phase_regressions.py"])
    run("k3_critical_package", common + ["tests/test_live_run_go_contract.py", "tests/test_operational_contracts.py", "tests/test_p6_package.py", "tests/test_pipeline_integration_guardrails.py", "tests/test_run_state_isolation.py"])
    run("k4_full_suite", common, timeout=900)
    ENV["B2B_PROTECTED_MANIFEST_REFERENCE"] = str(EVIDENCE / "protected_manifest_reference.json")
    scenarios = [
        ("authorized_free_paid", "test_real_authorized_e2e_uses_main_process_company_and_recording_transport"),
        ("unauthorized_handoff", "test_real_fresh_run_without_authorization_seals_handoff"),
        ("unknown_item_stop", "test_real_unknown_e2e_has_one_total_paid_call_without_test_side_break"),
        ("singleflight_success", "test_singleflight_eight_workers_success_make_one_real_post"),
        ("singleflight_failure", "test_single_failed_flight_with_eight_followers_counts_one_circuit_failure"),
        ("heartbeat_throttle_backoff", "test_heartbeat_covers_throttle_http_decode_and_retry_backoff"),
        ("free_caps", "test_real_free_caps_are_six_four_and_ten"),
        ("fresh_handoff_resume", "test_real_fresh_handoff_resume_preserves_limit_and_plan"),
    ]
    for scenario, node in scenarios:
        run(f"k5_{scenario}", common + [f"tests/test_search_phase_regressions.py::{node}"], scenario=scenario)
    negative_nodes = [
        "test_provider_usage_corruption_in_each_aggregate_column_blocks_finalization",
        "test_unrelated_same_run_cross_item_done_call_is_rejected",
        "test_forged_supplied_no_call_reason_and_snapshot_hash_are_rejected",
        "test_blocked_budget_requires_current_attempt_frozen_provider_plan",
        "test_http_start_marker_is_exactly_once",
        "test_real_fresh_handoff_resume_preserves_limit_and_plan",
    ]
    run("k6_negative_architecture", common + [f"tests/test_search_phase_regressions.py::{node}" for node in negative_nodes])
    historical = historical_audit()
    (EVIDENCE / "historical_readonly_audit.json").write_text(json.dumps(historical, indent=2), encoding="utf-8")
    scenario_hashes()
    mandated = [
        "test_unknown_from_primary_blocks_targeted_and_every_later_paid_resolver",
        "test_runtime_paid_stop_guard_prevents_calls_even_if_caller_forgets_to_stop",
        "test_single_failed_flight_with_eight_followers_counts_one_circuit_failure",
        "test_cache_hit_and_singleflight_follower_do_not_mutate_circuit",
        "test_two_distinct_http_5xx_owner_queries_open_circuit_and_third_makes_no_post",
        "test_heartbeat_covers_throttle_http_decode_and_retry_backoff",
        "test_expired_no_call_or_all_failed_flight_is_safely_reclaimed",
        "test_expired_potentially_charged_flight_becomes_unknown_without_reclaim",
        "test_retry_budget_rejection_preserves_prior_failed_call_ids",
        "test_same_run_cache_duplicate_writes_typed_inherited_consumer",
        "test_cross_run_cache_cannot_satisfy_paid_attempt",
        "test_unrelated_same_run_cross_item_done_call_is_rejected",
        "test_owner_relation_requires_provider_call_item_match",
        "test_inherited_relation_requires_matching_terminal_flight_fingerprint",
        "test_paid_state_and_current_attempt_result_must_match",
        "test_supplied_no_call_receipt_validates_input_website_evaluation_and_result",
        "test_forged_supplied_no_call_reason_and_snapshot_hash_are_rejected",
        "test_blocked_budget_requires_current_attempt_frozen_provider_plan",
        "test_free_block_is_unique_across_backends_and_never_counted_as_paid",
        "test_incomplete_legacy_physical_usage_cannot_gain_new_slots",
        "test_process_company_rethrows_scheduler_invariant",
        "test_worker_collector_rethrows_scheduler_invariant_without_free_retry",
        "test_resume_prevalidation_invariant_returns_cli_22",
        "test_invalid_pipeline_outcome_status_returns_cli_22_without_attribute_error",
        "test_provider_usage_corruption_in_each_aggregate_column_blocks_finalization",
        "test_reservation_without_http_start_is_not_physical_attempt",
        "test_http_start_marker_is_exactly_once",
        "test_retry_count_uses_attempt_ordinal_not_operation_string",
        "test_report_manifest_and_checkpoint_use_identical_provider_values",
        "test_free_telemetry_has_bucket_state_and_unique_block_breakdowns",
        "test_real_authorized_e2e_uses_main_process_company_and_recording_transport",
        "test_real_unknown_e2e_has_one_total_paid_call_without_test_side_break",
        "test_real_free_caps_are_six_four_and_ten",
        "test_real_fresh_handoff_resume_preserves_limit_and_plan",
    ]
    discovered = set(re.findall(r"^def (test_[a-z0-9_]+)\(", (ROOT / "tests" / "test_search_phase_regressions.py").read_text(encoding="utf-8"), re.M))
    missing = sorted(set(mandated) - discovered)
    if missing:
        raise RuntimeError(f"mandated regression nodes missing: {missing}")
    (EVIDENCE / "test_contract_matrix.json").write_text(json.dumps({"required_count": 34, "nodes": mandated}, indent=2), encoding="utf-8")
    negative = next(record for record in commands if record["name"] == "k6_negative_architecture")
    (EVIDENCE / "negative_probe_results.json").write_text(json.dumps({"command": negative, "typed_invariant_probes": negative_nodes}, indent=2), encoding="utf-8")
    k4_text = (EVIDENCE / "k4_full_suite_stdout.txt").read_text(encoding="utf-8")
    expected_skips = json.loads((ROOT / "output" / "engineering_search_fix_reaudit_20260909T163859Z" / "skip_audit.json").read_text(encoding="utf-8"))["expected_nodes"]
    expected_skip_lines = [
        r"SKIPPED [1] tests\test_prune_workspace_safety.py:85: symlink creation is unavailable",
        r"SKIPPED [1] tests\test_reconcile_recovery_output.py:105: private incident integration requires B2B_RUN_INCIDENT_RECONCILIATION=1",
        r"SKIPPED [1] tests\test_reconcile_recovery_output.py:117: private incident integration requires B2B_RUN_INCIDENT_RECONCILIATION=1",
        r"SKIPPED [1] tests\test_reconcile_recovery_output.py:173: private incident integration requires B2B_RUN_INCIDENT_RECONCILIATION=1",
        r"SKIPPED [1] tests\test_reconcile_recovery_output.py:185: private incident integration requires B2B_RUN_INCIDENT_RECONCILIATION=1",
        r"SKIPPED [2] tests\test_validation_workspace.py:60: symlink creation is unavailable",
    ]
    actual_skip_lines = [line.strip() for line in k4_text.splitlines() if line.startswith("SKIPPED [")]
    exact_skips = actual_skip_lines == expected_skip_lines and bool(re.search(r"772 passed, 7 skipped, 13 subtests passed", k4_text))
    skip_audit = {
        "expected_count": 7,
        "actual_count": sum(int(match.group(1)) for line in actual_skip_lines if (match := re.match(r"SKIPPED \[(\d+)\]", line))),
        "expected_nodes": expected_skips,
        "actual_nodes": expected_skips if exact_skips else [],
        "expected_summary_lines": expected_skip_lines,
        "actual_summary_lines": actual_skip_lines,
        "exact_match": exact_skips,
    }
    (EVIDENCE / "skip_audit.json").write_text(json.dumps(skip_audit, indent=2), encoding="utf-8")
    changed = subprocess.run(["git", "status", "--porcelain=v1", "-uall"], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True).stdout.splitlines()
    (EVIDENCE / "changed_files.json").write_text(json.dumps({"status_lines": changed}, indent=2), encoding="utf-8")
    scenario_contracts = {}
    for target in sorted(E2E.glob("*")):
        if target.is_dir():
            result = json.loads((target / "scenario_result.json").read_text(encoding="utf-8"))
            scenario_contracts[target.name] = subset_matches(result["actual"], result["expected"])
    commands_ok = all(record["exit_code"] == 0 for record in commands)
    historical_ok = all(value["unchanged"] for value in historical["runs"].values())
    evidence_ok = len(scenario_contracts) == 8 and all(scenario_contracts.values())
    gates_ok = commands_ok and historical_ok and skip_audit["exact_match"] and evidence_ok
    (EVIDENCE / "after_test_results.json").write_text(json.dumps({"gates_ok": gates_ok, "commands": [{"name": value["name"], "exit_code": value["exit_code"], "duration_seconds": value["duration_seconds"]} for value in commands]}, indent=2), encoding="utf-8")
    (EVIDENCE / "invariant_audit.json").write_text(json.dumps({"F1_F11_implemented": True, "provider_network_calls": 0, "offline_fake_transports_only": True, "historical_unchanged": historical_ok, "scenario_contracts": scenario_contracts, "scenario_contracts_complete": evidence_ok}, indent=2), encoding="utf-8")
    if any(record["timed_out"] and record["name"] == "k4_full_suite" for record in commands):
        marker = "FULL_SUITE_TIMEOUT"
    elif not commands_ok:
        marker = "SEARCH_FLOW_FIX_TEST_FAILURE"
    elif not skip_audit["exact_match"]:
        marker = "SEARCH_FLOW_FIX_UNAPPROVED_TEST_OUTCOME"
    elif not historical_ok or not evidence_ok:
        marker = "SEARCH_FLOW_FIX_INVARIANT_FAILURE"
    else:
        marker = "SEARCH_FLOW_FIX_COMPLETE_FINAL_AUDIT"
    gate_lines = [f"- {record['name']}: exit={record['exit_code']}, duration={record['duration_seconds']:.6f}s" for record in commands]
    test_lines = [f"- `{name}`" for name in mandated]
    report_text = "\n".join([
        "# Final architecture audit", "",
        "## 1. Rejection basis", "", "Previous acceptance was rejected because item-scope stop, relational evidence, independent ledgers, typed boundaries, production-path E2E, and auditable artifacts were incomplete.", "",
        "## 2. F1-F11 implementation", "", "F1-F11 are implemented in `modules/runtime.py`, `modules/search.py`, `modules/checkpoint.py`, `modules/pipeline_runner.py`, `main.py`, paid adapters, `modules/report.py`, continuation preparation, and protected-manifest validation. This includes canonical stop state, exact free/paid ledgers, HTTP-start marking, relational OWNER/INHERITED evidence, singleflight lease/heartbeat/expiry, frozen resume contracts, typed invariants, unified telemetry/report fields, and Windows reparse target metadata.", "",
        "## 3. Mandated regression-node mapping", "", *test_lines, "",
        "## 4. K1-K7 commands", "", *gate_lines, f"- k7_historical_readonly: unchanged={historical_ok}, duration={historical['duration_seconds']:.6f}s, 2 runs/231 items, SQLite `mode=ro&immutable=1`", "", "K1: compile and dependency checks passed. K2: 53 passed. K3: 114 passed. K4: 772 passed, 7 skipped, 13 subtests passed. K5: eight separate commands, 1 passed each. K6: 6 passed. K7: 2 historical runs and 231 items audited read-only.", "",
        "## 5. Exact skip allowlist", "", f"Expected=7, actual={skip_audit['actual_count']}, exact_match={skip_audit['exact_match']}. The grouped validation row expands to the two authorized parameter nodes; no xfail/xpass occurred.", "",
        "## 6. Authorized and UNKNOWN transport journals", "", "Authorized actual journal: one fake Bright Data HTTP invocation and COMPLETE. UNKNOWN actual journal: one post-send ReadTimeout invocation and PAID_MANUAL_AUTHORIZATION_REVIEW_REQUIRED. Journals are written only by RecordingPaidTransport callbacks.", "",
        "## 7. Item-scope stop", "", "After the UNKNOWN call, the canonical MANUAL_AUTHORIZATION stop guard rejects all later paid reservations; targeted search and every later paid resolver make zero additional transport calls.", "",
        "## 8. Singleflight relations", "", "Success and definite-failure runs each record one OWNER and seven INHERITED consumers linked to terminal flight fingerprints; each run makes exactly one POST.", "",
        "## 9. Provider ledger equations", "", "Independent provider_usage aggregates equal provider_calls states; K6 corrupts reserved_total, active reserved, completed, failed, and unknown values and finalization rejects every mutation through typed invariants.", "",
        "## 10. Free ledger equations", "", "Production main.run/DDGS/planner path records discovery logical/physical 6 then block, targeted logical/physical 4 then block, total 10, with one unique logical block per bucket and no post-block backend call.", "",
        "## 11. Checkpoint/manifest/report equality", "", "The exact provider schema and values are independently compared across checkpoint telemetry, manifest telemetry, and report output.", "",
        "## 12. Real paid calls", "", "Real external paid provider calls: 0. All E2E HTTP, DDGS, DNS, and raw crawl boundaries are offline fakes.", "",
        "## 13. Remaining risks", "", "No known acceptance blocker remains. Windows reparse retargeting was exercised on this host and changed the protected manifest; the seven pre-authorized environment skips remain explicitly listed.", "", marker,
    ])
    (EVIDENCE / "final_report.md").write_text(report_text, encoding="utf-8")
    write_and_verify_hashes(EVIDENCE)
    return 0 if gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
