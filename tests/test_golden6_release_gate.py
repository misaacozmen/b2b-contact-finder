import copy
import json
import hashlib
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
from openpyxl import Workbook
from modules import checkpoint
from modules.run_context import RunConfig
from tools import evaluate_release_gate
from tools.free_only_contract import expected_offline_run_config
from tools.evaluate_release_gate import (
    _package_files_valid, _provider_http_calls, _receipt_artifact_name,
    evaluate_evidence, receipt_eligibility,
)


def _valid_evidence() -> dict:
    return {
        "behavioral": {
            "package_valid": True,
            "replay_miss_count": 0,
            "replay_network_events": 0,
            "provider_http_calls": 0,
            "provider_call_count": 0,
            "free_only_config_valid": True,
            "all_results_unique_ids": 20,
            "expected_all_results_order_match": True,
            "actual_is_subset": True,
            "validator_status": "PASS",
            "issues": [],
            "positive_denominators": True,
            "live_replay_metrics_equal": True,
            "live_artifact_hash": "artifact",
            "offline_artifact_hash": "artifact",
            "receipt_eligible": True,
        },
        "checks": {
            "js_default_unset_true": True,
            "browser_smoke": True,
            "command_results": {
                name: {"returncode": 0}
                for name in ("compileall", "pytest", "benchmark", "pip_check", "help", "diff_check")
            },
            "offline_test_env_used": True,
            "offline_test_env_removed": True,
            "tests_intact": True,
            "tracked_clean": True,
            "feature_remote_exact": True,
            "ci_token_masked": True,
            "ci": {"head_sha": "head", "jobs_success": True},
        },
        "git": {
            "branch": "codex/release-hardening",
            "head_sha": "head",
            "origin_feature_sha": "head",
            "gate_base_sha": "base",
            "origin_main_sha": "base",
            "origin_main_ancestor": True,
            "no_divergence_or_force": True,
        },
    }


PROVIDERS = ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")
_OMIT = object()
PROVIDER_TELEMETRY_FIELDS = (
    "configured_limit", "effective_limit", "reserved_total", "reserved", "running",
    "done", "failed", "unknown", "physical_http_attempts", "retry_attempts",
    "inherited_uses", "budget_blocked_items",
)


def _write_workbook(path: Path, source_ids: list[str]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["source_record_id"])
    for source_id in source_ids:
        sheet.append([source_id])
    workbook.save(path)
    workbook.close()


def _artifact_fixture(
    root: Path, source_ids: list[str], request_count: object, replay_miss_count: object,
    *, legacy_counters: object = _OMIT, provider_field: tuple[str, object] | None = None,
    scheduler_receipt: dict | None = None,
) -> tuple[Path, str, dict]:
    staging = root / "output" / "artifacts" / "staging"
    staging.mkdir(parents=True)
    for name in ("contacts.xlsx", "website_candidates.xlsx", "all_results.xlsx"):
        _write_workbook(staging / name, source_ids)
    provider_budgets = {
        provider: {field: 0 for field in PROVIDER_TELEMETRY_FIELDS}
        for provider in PROVIDERS
    }
    provider_budgets["llm"]["physical_http_attempts"] = request_count
    if provider_field is not None:
        field, value = provider_field
        provider_budgets["llm"][field] = value
    telemetry = copy.deepcopy(scheduler_receipt) if scheduler_receipt is not None else {
        "receipt_schema_version": 1, "paid_required": 0, "paid_completed": 0,
        "provider_budgets": provider_budgets,
        "paid_query_plan": {
            "plan_version": 1, "paid_query_plan_count": 0,
            "paid_query_plan_sha256": hashlib.sha256(b"[]").hexdigest(),
        },
    }
    if scheduler_receipt is not None and request_count:
        telemetry["provider_budgets"]["llm"]["physical_http_attempts"] = request_count
    if scheduler_receipt is not None and provider_field is not None:
        field, value = provider_field
        telemetry["provider_budgets"]["llm"][field] = value
    if legacy_counters is not _OMIT:
        telemetry["counters"] = legacy_counters
    telemetry_json = json.dumps(telemetry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    (staging / "telemetry.json").write_text(json.dumps(telemetry, ensure_ascii=False, indent=2), encoding="utf-8")
    (staging / "report.txt").write_text(f"SCHEDULER_RECEIPT_JSON={telemetry_json}\n", encoding="utf-8")
    (staging / "discovery_coverage.json").write_text(
        json.dumps({"replay_miss_count": replay_miss_count}), encoding="utf-8",
    )
    files = {
        path.name: {"sha256": evaluate_release_gate._sha256(path), "bytes": path.stat().st_size}
        for path in sorted(staging.iterdir())
    }
    material = "".join(f"{name}:{files[name]['sha256']}\n" for name in sorted(files))
    artifact_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    artifact = staging.with_name(artifact_hash)
    staging.rename(artifact)
    return artifact, artifact_hash, files


def _database_fixture(path: Path, run_id: str, input_hash: str, source_ids: list[str]) -> None:
    budgets = {provider: 0 for provider in PROVIDERS}
    items = [
        {
            "item_index": index, "source_record_id": source_id, "free_state": "DONE",
            "paid_required": False, "paid_state": "NOT_REQUIRED",
        }
        for index, source_id in enumerate(source_ids)
    ]
    results = []
    for source_id, index in zip(source_ids, range(len(source_ids))):
        payload = json.dumps({
            "source_record_id": source_id, "paid_required": False,
            "paid_state": "NOT_REQUIRED", "paid_recommended": True,
            "paid_skipped_reason": "disabled_by_explicit_free_only_finalization",
        }, separators=(",", ":"))
        results.append({"item_index": index, "payload": payload})
    with patch.object(config, "PROGRESS_DB_FILE", path):
        checkpoint.initialize_run(
            run_id=run_id, input_hash=input_hash, run_signature=f"fixture:{run_id}",
            context={
                "phase": "FREE", "paid_query_limit_per_company": 2,
                "budget_details": {
                    provider: {
                        "population_count": len(source_ids), "ratio": 0.5,
                        "ratio_numerator": 1, "ratio_denominator": 2, "explicit_cap": 3,
                    }
                    for provider in PROVIDERS
                },
            }, budgets=budgets, items=items,
        )
        with sqlite3.connect(path) as connection:
            connection.executemany(
                "INSERT INTO results(run_id,item_index,payload) VALUES(?,?,?)",
                [(run_id, result["item_index"], result["payload"]) for result in results],
            )
            connection.executemany(
                "UPDATE run_items SET payload_sha256=? WHERE run_id=? AND item_index=?",
                [
                    (hashlib.sha256(result["payload"].encode("utf-8")).hexdigest(), run_id, result["item_index"])
                    for result in results
                ],
            )
        checkpoint.transition_phase(run_id, "FINALIZING", expected_count=len(source_ids))


def _finalize_database_fixture(
    path: Path, run_id: str, input_hash: str, artifact_hash: str, files: dict,
    manifest_path: Path, run_config: dict, config_sha: str, source_ids: list[str],
) -> dict:
    with patch.object(config, "PROGRESS_DB_FILE", path):
        checkpoint.begin_finalization_intent(
            run_id=run_id, generation=f"fixture:{run_id}",
            input_snapshot_sha256=input_hash, result_snapshot_sha256=f"result:{run_id}",
        )
        canonical = checkpoint.canonical_scheduler_receipt(run_id)
        checkpoint.mark_finalization_artifact_and_outbox(
            run_id=run_id, generation=f"fixture:{run_id}",
            result_snapshot_sha256=f"result:{run_id}", artifact_set_sha256=artifact_hash,
            artifacts={"artifact_set_sha256": artifact_hash, "files": files}, memory_rows=[],
        )
        manifest = _artifact_manifest(
            run_id, input_hash, run_config, config_sha,
            artifact_hash, files, source_ids, telemetry=canonical,
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        checkpoint.complete_finalization_intent(
            run_id=run_id, artifact_set_sha256=artifact_hash,
            manifest_sha256=evaluate_release_gate._sha256(manifest_path),
        )
        checkpoint.transition_phase(run_id, "COMPLETE", expected_count=len(source_ids))
    return canonical


def _rebuild_run_artifact(root: Path, run_id: str, mutate, *, receipt_path: Path | None = None, live: bool = False) -> None:
    run_root = root / run_id
    manifest_path = run_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_hash = manifest["artifact_set_sha256"]
    artifact_dir = run_root / "output" / "artifacts" / old_hash
    mutate(artifact_dir)
    files = {
        path.name: {"sha256": evaluate_release_gate._sha256(path), "bytes": path.stat().st_size}
        for path in sorted(artifact_dir.iterdir()) if path.is_file()
    }
    material = "".join(f"{name}:{files[name]['sha256']}\n" for name in sorted(files))
    new_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    new_artifact_dir = artifact_dir.with_name(new_hash)
    artifact_dir.rename(new_artifact_dir)
    manifest["artifact_set_sha256"] = new_hash
    manifest["files"] = files
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_hash = evaluate_release_gate._sha256(manifest_path)
    with sqlite3.connect(run_root / "state" / "progress.sqlite3") as connection:
        connection.execute(
            "UPDATE finalization_intent SET artifact_set_sha256=?,manifest_sha256=? WHERE run_id=?",
            (new_hash, manifest_hash, run_id),
        )
        connection.commit()
    if live:
        package_path = root / "package" / "package_manifest.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["live_run"].update({
            "artifact_set_sha256": new_hash,
            "artifact_dir": str(new_artifact_dir),
            "manifest_sha256": manifest_hash,
        })
        package_path.write_text(json.dumps(package), encoding="utf-8")
        if receipt_path is not None:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["package_manifest_sha256"] = evaluate_release_gate._sha256(package_path)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    elif receipt_path is not None:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt.update({"artifact_set_sha256": new_hash, "artifact_dir": str(new_artifact_dir)})
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


def _mutate_manifest_telemetry(root: Path, run_id: str) -> None:
    run_root = root / run_id
    manifest_path = run_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["telemetry"]["paid_query_plan"]["paid_query_plan_count"] = 1
    telemetry_json = json.dumps(manifest["telemetry"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["telemetry_sha256"] = hashlib.sha256(telemetry_json.encode("utf-8")).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with sqlite3.connect(run_root / "state" / "progress.sqlite3") as connection:
        connection.execute(
            "UPDATE finalization_intent SET manifest_sha256=? WHERE run_id=?",
            (evaluate_release_gate._sha256(manifest_path), run_id),
        )
        connection.commit()


def _mutate_intent_telemetry(root: Path, run_id: str) -> None:
    with sqlite3.connect(root / run_id / "state" / "progress.sqlite3") as connection:
        row = connection.execute(
            "SELECT telemetry_snapshot_json FROM finalization_intent WHERE run_id=?", (run_id,)
        ).fetchone()
        telemetry = json.loads(row[0])
        telemetry["paid_query_plan"]["paid_query_plan_count"] = 1
        telemetry_json = json.dumps(telemetry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "UPDATE finalization_intent SET telemetry_snapshot_json=?,telemetry_sha256=? WHERE run_id=?",
            (telemetry_json, hashlib.sha256(telemetry_json.encode("utf-8")).hexdigest(), run_id),
        )
        connection.commit()


def _mutate_db_canonical_source(root: Path, run_id: str) -> None:
    payload = "canonical-source-drift"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with sqlite3.connect(root / run_id / "state" / "progress.sqlite3") as connection:
        connection.execute(
            "INSERT INTO paid_query_plan_entries(run_id,item_index,plan_version,query_kind,round_ordinal,query_ordinal,normalized_query,query_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, 0, 1, "primary", 0, 0, payload, digest),
        )
        connection.commit()


def _mutate_synchronized_positive_plan(root: Path, receipt_path: Path) -> None:
    """Add one real plan row and rebuild every offline telemetry replica around it."""
    run_id = "offline-run"
    run_root = root / run_id
    database_path = run_root / "state" / "progress.sqlite3"
    payload = "canonical-positive-plan"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO paid_query_plan_entries(run_id,item_index,plan_version,query_kind,round_ordinal,query_ordinal,normalized_query,query_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, 0, 1, "primary", 0, 0, payload, digest),
        )
        connection.commit()

    with patch.object(config, "PROGRESS_DB_FILE", database_path):
        canonical = checkpoint.canonical_scheduler_receipt(run_id)
    canonical_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    canonical_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    manifest_path = run_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_hash = manifest["artifact_set_sha256"]
    artifact_dir = run_root / "output" / "artifacts" / old_hash
    (artifact_dir / "telemetry.json").write_text(
        json.dumps(canonical, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (artifact_dir / "report.txt").write_text(
        f"SCHEDULER_RECEIPT_JSON={canonical_json}\n", encoding="utf-8",
    )
    files = {
        path.name: {"sha256": evaluate_release_gate._sha256(path), "bytes": path.stat().st_size}
        for path in sorted(artifact_dir.iterdir())
    }
    material = "".join(f"{name}:{files[name]['sha256']}\n" for name in sorted(files))
    new_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    new_artifact_dir = artifact_dir.with_name(new_hash)
    artifact_dir.rename(new_artifact_dir)
    manifest.update({
        "artifact_set_sha256": new_hash,
        "files": files,
        "telemetry": canonical,
        "telemetry_sha256": canonical_hash,
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE finalization_intent SET artifact_set_sha256=?,manifest_sha256=?,telemetry_snapshot_json=?,telemetry_sha256=? WHERE run_id=?",
            (new_hash, manifest_hash, canonical_json, canonical_hash, run_id),
        )
        connection.commit()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.update({"artifact_set_sha256": new_hash, "artifact_dir": str(new_artifact_dir)})
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


def _mutate_telemetry_file(artifact_dir: Path, mutation) -> None:
    path = artifact_dir / "telemetry.json"
    telemetry = json.loads(path.read_text(encoding="utf-8"))
    mutation(telemetry)
    path.write_text(json.dumps(telemetry, ensure_ascii=False, indent=2), encoding="utf-8")


def _artifact_manifest(run_id: str, input_hash: str, run_config: dict, config_sha: str, artifact_hash: str, files: dict, source_ids: list[str], *, telemetry: dict | None = None) -> dict:
    manifest = {
        "run_id": run_id, "input_sha256": input_hash, "complete": True, "phase": "COMPLETE",
        "finalized": True, "status": "complete_free_only", "paid_enabled": False,
        "finalize_without_paid": True, "run_config": run_config, "config_sha256": config_sha,
        "ordered_source_record_ids": source_ids, "item_count": len(source_ids),
        "artifact_set_sha256": artifact_hash, "files": files,
    }
    if telemetry is not None:
        manifest["telemetry"] = telemetry
        telemetry_json = json.dumps(telemetry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest["telemetry_sha256"] = hashlib.sha256(telemetry_json.encode("utf-8")).hexdigest()
    return manifest


def _gate_fixture(
    root: Path, *, threshold: int | None = None, mutate_offline=None,
    telemetry_live: object = 0, telemetry_offline: object = 0, replay_miss_count: object = 0,
    legacy_counters_live: object = _OMIT, legacy_counters_offline: object = _OMIT,
    provider_field_live: tuple[str, object] | None = None,
    provider_field_offline: tuple[str, object] | None = None,
) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    package_dir = root / "package"
    package_dir.mkdir()
    input_path = root / "input.xlsx"
    input_path.write_bytes(b"golden-input")
    source_ids = [f"source:{index}" for index in range(20)]
    snapshot_path = package_dir / "replay_snapshot.json.gz"
    snapshot_path.write_bytes(b"snapshot")
    payload_path = package_dir / "replay_shards" / "body-000.json.gz"
    payload_path.parent.mkdir()
    payload_path.write_bytes(b"body-shard")
    with patch.object(config, "SEARCH_CACHE_MODE", "refresh"), patch.object(config, "CRAWL_CACHE_MODE", "refresh"):
        live_config = RunConfig.from_config(
            paid_enabled=False, budgets={provider: 0 for provider in PROVIDERS}, finalize_without_paid=True,
        ).as_dict()
    offline_config = expected_offline_run_config(live_config)
    if threshold is not None:
        offline_config["effective_settings"]["publication_policy_min_safety_score"] = threshold
    if mutate_offline is not None:
        mutate_offline(offline_config)
    live_root = root / "live-run"
    offline_root = root / "offline-run"
    live_root.joinpath("state").mkdir(parents=True)
    offline_root.joinpath("state").mkdir(parents=True)
    input_hash = evaluate_release_gate._sha256(input_path)
    _database_fixture(live_root / "state" / "progress.sqlite3", "live-run", input_hash, source_ids)
    _database_fixture(offline_root / "state" / "progress.sqlite3", "offline-run", input_hash, source_ids)
    live_sha = RunConfig.from_dict(live_config).sha256
    try:
        offline_sha = RunConfig.from_dict(offline_config).sha256
    except Exception:
        offline_sha = hashlib.sha256(
            json.dumps(offline_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    with patch.object(config, "PROGRESS_DB_FILE", live_root / "state" / "progress.sqlite3"):
        checkpoint.begin_finalization_intent(
            run_id="live-run", generation="fixture:live-run", input_snapshot_sha256=input_hash,
            result_snapshot_sha256="result:live-run",
        )
        live_canonical = checkpoint.canonical_scheduler_receipt("live-run")
    with patch.object(config, "PROGRESS_DB_FILE", offline_root / "state" / "progress.sqlite3"):
        checkpoint.begin_finalization_intent(
            run_id="offline-run", generation="fixture:offline-run", input_snapshot_sha256=input_hash,
            result_snapshot_sha256="result:offline-run",
        )
        offline_canonical = checkpoint.canonical_scheduler_receipt("offline-run")
    live_artifact, live_artifact_hash, live_files = _artifact_fixture(
        live_root, source_ids, telemetry_live, 0,
        legacy_counters=legacy_counters_live, provider_field=provider_field_live,
        scheduler_receipt=live_canonical,
    )
    offline_artifact, offline_artifact_hash, offline_files = _artifact_fixture(
        offline_root, source_ids, telemetry_offline, replay_miss_count,
        legacy_counters=legacy_counters_offline, provider_field=provider_field_offline,
        scheduler_receipt=offline_canonical,
    )
    _finalize_database_fixture(
        live_root / "state" / "progress.sqlite3", "live-run", input_hash,
        live_artifact_hash, live_files, live_root / "manifest.json", live_config, live_sha, source_ids,
    )
    _finalize_database_fixture(
        offline_root / "state" / "progress.sqlite3", "offline-run", input_hash,
        offline_artifact_hash, offline_files, offline_root / "manifest.json", offline_config, offline_sha, source_ids,
    )
    live_manifest = json.loads((live_root / "manifest.json").read_text(encoding="utf-8"))
    offline_manifest = json.loads((offline_root / "manifest.json").read_text(encoding="utf-8"))
    package_manifest = {
        "schema_version": 1, "paid_enabled": False, "finalize_without_paid": True,
        "run_config": live_config, "config_sha256": live_sha,
        "source_integrity_before": {"sha256": "same"}, "source_integrity_after": {"sha256": "same"},
        "ordered_source_record_ids": source_ids,
        "input": {
            "path": str(input_path), "sha256": input_hash,
            "ordered_id_sha256": hashlib.sha256(json.dumps(source_ids, separators=(",", ":")).encode()).hexdigest(),
            "record_count": 20,
        },
        "replay": {
            "snapshot": snapshot_path.name, "snapshot_sha256": evaluate_release_gate._sha256(snapshot_path),
            "body_shards": [{"path": payload_path.relative_to(package_dir).as_posix(), "sha256": evaluate_release_gate._sha256(payload_path), "bytes": payload_path.stat().st_size}],
        },
        "files": {
            snapshot_path.name: {"sha256": evaluate_release_gate._sha256(snapshot_path), "bytes": snapshot_path.stat().st_size},
            payload_path.relative_to(package_dir).as_posix(): {"sha256": evaluate_release_gate._sha256(payload_path), "bytes": payload_path.stat().st_size},
        },
        "package_manifest_hash_scope": "all package files except package_manifest.json",
        "live_run": {
            "run_id": "live-run", "run_dir": str(live_root),
            "manifest_sha256": evaluate_release_gate._sha256(live_root / "manifest.json"),
            "config_sha256": live_sha, "artifact_dir": str(live_artifact), "artifact_set_sha256": live_artifact_hash,
        },
    }
    package_path = package_dir / "package_manifest.json"
    package_path.write_text(json.dumps(package_manifest), encoding="utf-8")
    receipt = {
        "schema_version": 2, "status": "PASS", "exit_code": 0, "pipeline_exit_code": 0,
        "evidence_class": "REPLAY_PASS", "replay_evidence_eligible": True,
        "failure_reason": "", "exception": "", "free_only_config_valid": True,
        "paid_budget_nonzero": False, "budget_check_completed": True, "budget_offenders": [],
        "threshold_mismatch": False, "threshold_mismatch_details": [],
        "config_mismatch": False, "config_mismatch_details": [],
        "config_invalid_details": [], "config_invalid": False,
        "network_activity": False, "complete_run_missing": False,
        "replay_pipeline_failure": False, "database_validation_failed": False,
        "paid_activity_nonzero": False, "paid_activity_check_completed": True,
        "paid_provider_calls": 0, "paid_activity": {
            "provider_calls": 0, "paid_attempts": 0, "paid_attempt_calls": 0,
            "provider_query_flights": 0, "flight_consumers": 0, "paid_budget_blocks": 0,
        },
        "replay_network_events": 0, "network_events": [], "failure_reasons": [],
        "package_manifest_sha256": evaluate_release_gate._sha256(package_path),
        "input_sha256": input_hash,
        "replay_snapshot_sha256": evaluate_release_gate._sha256(snapshot_path),
        "run_id": "offline-run", "run_dir": str(offline_root),
        "artifact_set_sha256": offline_artifact_hash, "artifact_dir": str(offline_artifact),
    }
    receipt_path = root / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return package_dir, receipt_path, payload_path


def _evaluate_fixture(
    root: Path, *, threshold: int | None = None, mutate_offline=None, failure: str = "",
    mutate_receipt=None, telemetry_live: object = 0, telemetry_offline: object = 0,
    replay_miss_count: object = 0, decoy_live_artifact: bool = False,
    receipt_schema_version: int | None = None,
    legacy_counters_live: object = _OMIT, legacy_counters_offline: object = _OMIT,
    provider_field_live: tuple[str, object] | None = None,
    provider_field_offline: tuple[str, object] | None = None,
    mutate_finalization=None,
):
    package_dir, receipt_path, payload_path = _gate_fixture(
        root, threshold=threshold, mutate_offline=mutate_offline,
        telemetry_live=telemetry_live, telemetry_offline=telemetry_offline,
        replay_miss_count=replay_miss_count,
        legacy_counters_live=legacy_counters_live, legacy_counters_offline=legacy_counters_offline,
        provider_field_live=provider_field_live, provider_field_offline=provider_field_offline,
    )
    if failure == "missing_snapshot":
        (package_dir / "replay_snapshot.json.gz").unlink()
    elif failure == "bad_snapshot":
        (package_dir / "replay_snapshot.json.gz").write_bytes(b"mutated")
    elif failure == "package_file":
        payload_path.write_bytes(b"mutated")
    elif failure == "bundle":
        manifest_path = root / "offline-run" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["item_count"] = 19
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif failure == "db":
        connection = sqlite3.connect(root / "offline-run" / "state" / "progress.sqlite3")
        try:
            connection.execute("UPDATE runs SET phase='RUNNING' WHERE run_id='offline-run'")
            connection.commit()
        finally:
            connection.close()
    if mutate_finalization is not None:
        mutate_finalization(root, receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if decoy_live_artifact:
        package_path = package_dir / "package_manifest.json"
        package_manifest = json.loads(package_path.read_text(encoding="utf-8"))
        package_manifest["live_run"]["artifact_dir"] = str(root / "decoy-live-artifact")
        package_path.write_text(json.dumps(package_manifest), encoding="utf-8")
        receipt["package_manifest_sha256"] = evaluate_release_gate._sha256(package_path)
    if receipt_schema_version is not None:
        receipt["schema_version"] = receipt_schema_version
    if mutate_receipt is not None:
        mutate_receipt(receipt)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    def fake_run(_repo, _name, _command, _env):
        return {"returncode": 0}

    report_data = {
        "status": "PASS", "issues": [],
        "fields": {
            field: {"tp": 1, "fp": 1, "fn": 1, "precision": 0.5, "recall": 0.5}
            for field in ("website", "email", "phone")
        },
        "stages": {"published_count": 1, "expected_websites": 1, "selected_count": 1},
        "records": {},
    }
    repo_state = {
        "branch": "test", "head_sha": "head", "origin_feature_sha": "head",
        "gate_base_sha": "base", "origin_main_sha": "base",
        "origin_main_ancestor": True, "no_divergence_or_force": True,
        "divergence": {"behind": 0, "ahead": 0}, "tracked_clean": True,
    }
    ci = {"head_sha": "head", "jobs_success": True}
    process = SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch.object(evaluate_release_gate, "_validator_report", return_value=(report_data, [])), \
        patch.object(evaluate_release_gate, "_repo_state", return_value=repo_state), \
        patch.object(evaluate_release_gate, "_run_command", side_effect=fake_run), \
        patch.object(evaluate_release_gate, "query_github_ci", return_value=ci), \
        patch.object(evaluate_release_gate, "_test_integrity", return_value=True), \
        patch.object(evaluate_release_gate.subprocess, "run", return_value=process):
        return evaluate_release_gate.evaluate(
            root, package_dir, receipt_path, root / "gate-report.json",
        )


class Golden6ReleaseGateTests(unittest.TestCase):
    def test_v2_receipt_requires_all_independent_success_fields(self):
        receipt = {
            "schema_version": 2, "status": "PASS", "exit_code": 0, "pipeline_exit_code": 0,
            "evidence_class": "REPLAY_PASS", "replay_evidence_eligible": True,
            "failure_reason": "", "paid_budget_nonzero": False, "budget_check_completed": True,
            "budget_offenders": [], "threshold_mismatch": False, "threshold_mismatch_details": [],
            "config_mismatch": False, "config_mismatch_details": [], "paid_activity_nonzero": False,
            "paid_activity_check_completed": True, "paid_provider_calls": 0,
            "paid_activity": {
                "provider_calls": 0, "paid_attempts": 0, "paid_attempt_calls": 0,
                "provider_query_flights": 0, "flight_consumers": 0, "paid_budget_blocks": 0,
            }, "replay_network_events": 0, "network_events": [], "exception": "",
            "config_invalid_details": [], "free_only_config_valid": True,
            "config_invalid": False, "network_activity": False, "complete_run_missing": False,
            "replay_pipeline_failure": False, "database_validation_failed": False,
            "failure_reasons": [],
        }
        self.assertFalse(receipt_eligibility(receipt)[0])
        rejected = dict(receipt, evidence_class="REJECTED_THRESHOLD_CANDIDATE", threshold_mismatch=True)
        self.assertFalse(receipt_eligibility(rejected)[0])
        for field, value in (
            ("paid_activity", {"provider_calls": 0}),
            ("paid_provider_calls", False),
            ("network_events", {"unexpected": True}),
            ("budget_offenders", [{"provider": "llm", "value": 0}]),
        ):
            assert not receipt_eligibility(dict(receipt, **{field: value}))[0]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.bin"
            input_path.write_bytes(b"input")
            package_manifest_path = root / "package_manifest.json"
            package_manifest = {
                "input": {"path": str(input_path), "sha256": evaluate_release_gate._sha256(input_path)},
                "replay": {"snapshot_sha256": "replay"},
            }
            package_manifest_path.write_text(json.dumps(package_manifest), encoding="utf-8")
            offline_root = root / "run"
            offline_root.mkdir()
            offline_manifest_path = offline_root / "manifest.json"
            offline_manifest = {"run_id": "run", "artifact_set_sha256": "a" * 64}
            offline_manifest_path.write_text(json.dumps(offline_manifest), encoding="utf-8")
            chained = dict(
                receipt,
                package_manifest_sha256=evaluate_release_gate._sha256(package_manifest_path),
                input_sha256=evaluate_release_gate._sha256(input_path), replay_snapshot_sha256="replay", run_id="run",
                run_dir=str(offline_root), artifact_set_sha256="a" * 64,
                artifact_dir=str(offline_root / "output" / "artifacts" / ("a" * 64)),
            )
            assert receipt_eligibility(
                chained,
                package_manifest=package_manifest,
                package_manifest_path=package_manifest_path,
                offline_manifest=offline_manifest,
                offline_manifest_path=offline_manifest_path,
            ) == (True, [])
            assert not receipt_eligibility(
                dict(chained, run_id="wrong"),
                package_manifest=package_manifest,
                package_manifest_path=package_manifest_path,
                offline_manifest=offline_manifest,
                offline_manifest_path=offline_manifest_path,
            )[0]

    def test_legacy_fail_receipt_is_never_eligible(self):
        activity = {
            "provider_calls": 0, "paid_attempts": 0, "paid_attempt_calls": 0,
            "provider_query_flights": 0, "flight_consumers": 0, "paid_budget_blocks": 0,
        }
        receipt = {
            "schema_version": 1, "status": "FAIL", "exit_code": 0, "exception": "",
            "paid_budget_nonzero": False, "paid_provider_calls": 0,
            "replay_network_events": 0, "network_events": [], "paid_activity": activity,
        }
        self.assertFalse(receipt_eligibility(receipt)[0])

    def test_rejected_receipts_use_non_behavioral_artifact_roles(self):
        self.assertEqual(_receipt_artifact_name({"evidence_class": "REPLAY_PASS"}, False), "failed_replay_receipt")
        self.assertEqual(_receipt_artifact_name({"evidence_class": "REJECTED_THRESHOLD_CANDIDATE"}, False), "failed_replay_receipt")
        self.assertEqual(
            _receipt_artifact_name({"evidence_class": "REJECTED_THRESHOLD_CANDIDATE"}, False, threshold_only=True, threshold_candidate=False),
            "failed_replay_receipt",
        )
        self.assertEqual(_receipt_artifact_name({"evidence_class": "REPLAY_PASS"}, True), "replay_receipt")

    def test_valid_evidence_sets_all_required_flags(self):
        result = evaluate_evidence(_valid_evidence())
        self.assertTrue(result["behavioral_recall_validated"])
        self.assertTrue(result["release_ready"])
        self.assertTrue(result["merge_allowed"])
        self.assertEqual(result["failure_reasons"], [])

    def test_missing_id_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["all_results_unique_ids"] = 19
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_bad_package_hash_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["package_valid"] = False
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_replay_miss_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["replay_miss_count"] = 1
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_network_event_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["replay_network_events"] = 1
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_zero_denominator_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["positive_denominators"] = False
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_wrong_ci_head_or_job_blocks_release_and_merge(self):
        evidence = _valid_evidence()
        evidence["checks"]["ci"]["head_sha"] = "other"
        result = evaluate_evidence(evidence)
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

        evidence = _valid_evidence()
        evidence["checks"]["ci"]["jobs_success"] = False
        result = evaluate_evidence(evidence)
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_advanced_or_diverged_main_blocks_merge(self):
        evidence = _valid_evidence()
        evidence["git"]["origin_main_ancestor"] = False
        result = evaluate_evidence(evidence)
        self.assertTrue(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

        evidence = _valid_evidence()
        evidence["git"]["no_divergence_or_force"] = False
        result = evaluate_evidence(evidence)
        self.assertTrue(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_manual_flag_injection_is_ignored(self):
        evidence = _valid_evidence()
        evidence.update({
            "behavioral_recall_validated": True,
            "release_ready": True,
            "merge_allowed": True,
        })
        evidence["behavioral"]["package_valid"] = False
        result = evaluate_evidence(copy.deepcopy(evidence))
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_ci_auth_falls_back_to_git_credential_without_exposing_secret(self):
        token = "credential-token-not-evidence"
        gh = subprocess.CompletedProcess(["gh"], 1, "", "")
        credential = subprocess.CompletedProcess(
            ["git"], 0, f"protocol=https\nhost=github.com\nusername=user\npassword={token}\n", "",
        )
        with patch.dict(evaluate_release_gate.os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}, clear=False), patch.object(
            evaluate_release_gate.subprocess, "run", side_effect=[gh, credential],
        ) as run:
            resolved, method = evaluate_release_gate._ci_auth_token(Path.cwd())
        self.assertEqual(resolved, token)
        self.assertEqual(method, "git_credential")
        self.assertNotIn(token, repr(run.call_args_list))


def _threshold_receipt(receipt: dict) -> None:
    receipt.update({
        "status": "FAIL", "exit_code": 3, "evidence_class": "REJECTED_THRESHOLD_CANDIDATE",
        "replay_evidence_eligible": False, "failure_reason": "REJECTED_THRESHOLD_CANDIDATE",
        "exception": "REJECTED_THRESHOLD_CANDIDATE", "free_only_config_valid": False,
        "threshold_mismatch": True,
        "threshold_mismatch_details": [{
            "field": "run_config.effective_settings.publication_policy_min_safety_score",
            "expected": 75, "actual": 70,
        }],
        "failure_reasons": ["REJECTED_THRESHOLD_CANDIDATE"],
    })


def test_evaluate_exact_config_pass_is_the_only_replay_receipt(tmp_path):
    report = _evaluate_fixture(tmp_path / "exact")
    assert "replay_receipt" in report["artifacts"]
    assert "offline_artifact_dir" in report["artifacts"]
    assert len(report["offline_artifact_hash"]) == 64


def test_evaluate_v1_clean_threshold_75_pass_is_preserved(tmp_path):
    report = _evaluate_fixture(tmp_path / "v1-clean-threshold-75", receipt_schema_version=1)
    assert "replay_receipt" in report["artifacts"]


def test_evaluate_clean_threshold_receipts_are_rejected_candidates(tmp_path):
    for threshold in (70, 72):
        report = _evaluate_fixture(
            tmp_path / f"threshold-{threshold}",
            threshold=threshold,
            mutate_receipt=_threshold_receipt,
        )
        assert "rejected_threshold_candidate_receipt" in report["artifacts"]
        assert "replay_receipt" not in report["artifacts"]
        assert "offline_artifact_dir" not in report["artifacts"]
        assert report["offline_artifact_hash"] == ""
        assert report["provider_call_count"] == -1
        assert report["metrics"]["offline"] == {}


def test_evaluate_negative_replay_evidence_never_becomes_behavioral_artifact(tmp_path):
    cases = {
        "missing-snapshot": {"failure": "missing_snapshot"},
        "bad-snapshot": {"failure": "bad_snapshot"},
        "package-file": {"failure": "package_file"},
        "bundle": {"failure": "bundle"},
        "database": {"failure": "db"},
        "non-threshold-config": {"mutate_offline": lambda value: value.update(search_provider="spoofed")},
        "type-config": {"mutate_offline": lambda value: value["effective_settings"].update(enable_llm=1)},
        "positive-budget": {"mutate_offline": lambda value: value["budgets"].update(llm=1)},
        "paid-activity": {"mutate_receipt": lambda value: value.update(
            paid_activity_nonzero=True, paid_provider_calls=1,
            paid_activity={
                "provider_calls": 1, "paid_attempts": 0, "paid_attempt_calls": 0,
                "provider_query_flights": 0, "flight_consumers": 0, "paid_budget_blocks": 0,
            },
        )},
        "spoof-threshold": {
            "threshold": 70,
            "mutate_receipt": lambda value: (_threshold_receipt(value), value.update(paid_activity_nonzero=True)),
        },
        "decoy-live-artifact": {"decoy_live_artifact": True},
    }
    for name, options in cases.items():
        report = _evaluate_fixture(tmp_path / name, **options)
        assert "failed_replay_receipt" in report["artifacts"], name
        assert "replay_receipt" not in report["artifacts"], name
        assert "rejected_threshold_candidate_receipt" not in report["artifacts"], name
        assert "offline_artifact_dir" not in report["artifacts"], name
        assert report["offline_artifact_hash"] == "", name
        assert report["provider_call_count"] == -1, name
        assert report["metrics"]["offline"] == {}, name


def test_evaluate_rejects_nonzero_or_malformed_replay_telemetry(tmp_path):
    cases = (
        {"telemetry_live": 1},
        {"telemetry_offline": 7},
        {"replay_miss_count": 1},
    )
    for index, options in enumerate(cases):
        report = _evaluate_fixture(tmp_path / f"telemetry-{index}", **options)
        assert "failed_replay_receipt" in report["artifacts"]
        assert "replay_receipt" not in report["artifacts"]
        assert report["metrics"]["offline"] == {}
        assert "offline_artifact_dir" not in report["artifacts"]

    report = _evaluate_fixture(
        tmp_path / "threshold-telemetry", threshold=70,
        telemetry_offline=4, mutate_receipt=_threshold_receipt,
    )
    assert "failed_replay_receipt" in report["artifacts"]
    assert "rejected_threshold_candidate_receipt" not in report["artifacts"]
    assert report["metrics"]["offline"] == {}


def test_provider_http_telemetry_is_exact_and_fail_closed(tmp_path):
    source_ids = [f"source:{index}" for index in range(20)]
    for index, value, valid, expected in (
        (0, 0, True, 0), (1, -1, False, None), (2, True, False, None), (3, 1.0, False, None),
    ):
        artifact, _hash, _files = _artifact_fixture(tmp_path / str(index), source_ids, value, 0)
        observation = _provider_http_calls(artifact)
        assert observation["valid"] is valid
        assert observation["count"] == expected


def test_free_only_canonical_paid_query_plan_count_is_exact_zero(tmp_path):
    source_ids = [f"source:{index}" for index in range(20)]
    artifact, _hash, _files = _artifact_fixture(tmp_path, source_ids, 0, 0)
    canonical = json.loads((artifact / "telemetry.json").read_text(encoding="utf-8"))
    checkpoint.validate_free_only_canonical_telemetry(canonical)
    for value in (1, -1, True, 0.0):
        candidate = copy.deepcopy(canonical)
        candidate["paid_query_plan"]["paid_query_plan_count"] = value
        with unittest.TestCase().assertRaisesRegex(
            checkpoint.EvidenceInvariant, r"^TELEMETRY_CANONICAL_PAID_QUERY_PLAN$",
        ):
            checkpoint.validate_free_only_canonical_telemetry(candidate)


def test_package_registry_is_complete_path_safe_and_body_shard_bound(tmp_path):
    package_dir, _receipt_path, body_shard = _gate_fixture(tmp_path / "package-registry")
    package_path = package_dir / "package_manifest.json"
    manifest = json.loads(package_path.read_text(encoding="utf-8"))
    for replacement in ("bad", {}, {"../escape": {"sha256": "0" * 64, "bytes": 0}}):
        candidate = copy.deepcopy(manifest)
        candidate["files"] = replacement
        assert _package_files_valid(package_dir, candidate)[0] is False

    candidate = copy.deepcopy(manifest)
    candidate["files"].pop("replay_snapshot.json.gz")
    assert _package_files_valid(package_dir, candidate)[0] is False
    candidate = copy.deepcopy(manifest)
    candidate["files"].pop("replay_shards/body-000.json.gz")
    assert _package_files_valid(package_dir, candidate)[0] is False

    extra = package_dir / "unregistered.bin"
    extra.write_bytes(b"extra")
    try:
        assert _package_files_valid(package_dir, manifest)[0] is False
    finally:
        extra.unlink()

    body_shard.write_bytes(b"mutated-body-shard")
    assert _package_files_valid(package_dir, manifest)[0] is False


def test_evaluate_telemetry_sources_cannot_mask_each_other(tmp_path):
    cases = (
        {"telemetry_offline": 3, "legacy_counters_offline": {}},
        {"telemetry_offline": 3, "legacy_counters_offline": {"unrelated.counter": 99}},
        {"telemetry_offline": 3, "legacy_counters_offline": {}},
        {"telemetry_offline": 0, "legacy_counters_offline": {"http.search.requests": 3}},
    )
    for index, options in enumerate(cases):
        report = _evaluate_fixture(tmp_path / f"source-pair-{index}", **options)
        assert "failed_replay_receipt" in report["artifacts"]
        assert "rejected_threshold_candidate_receipt" not in report["artifacts"]
        assert "replay_receipt" not in report["artifacts"]
        assert report["metrics"]["offline"] == {}


def test_evaluate_synchronized_positive_plan_is_canonical_failure_only(tmp_path):
    report = _evaluate_fixture(
        tmp_path / "positive-canonical-plan",
        mutate_finalization=_mutate_synchronized_positive_plan,
    )
    assert "failed_replay_receipt" in report["artifacts"]
    assert "replay_receipt" not in report["artifacts"]
    assert "rejected_threshold_candidate_receipt" not in report["artifacts"]
    assert "offline_artifact_dir" not in report["artifacts"]
    assert report["offline_artifact_hash"] == ""
    assert report["metrics"]["offline"] == {}
    assert report["provider_call_count"] == -1
    assert "TELEMETRY_CANONICAL_PAID_QUERY_PLAN" in report["failure_reasons"]
    assert not any(
        "TELEMETRY_REPLICA_" in reason or "HASH_MISMATCH" in reason
        for reason in report["failure_reasons"]
    )
    receipt = json.loads((tmp_path / "positive-canonical-plan" / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["paid_budget_nonzero"] is False
    assert "PAID_BUDGET_NONZERO" not in report["failure_reasons"]


def test_evaluate_each_canonical_provider_budget_field_is_non_behavioral(tmp_path):
    for field in PROVIDER_TELEMETRY_FIELDS:
        report = _evaluate_fixture(
            tmp_path / field, provider_field_offline=(field, 1),
        )
        assert "failed_replay_receipt" in report["artifacts"], field
        assert "replay_receipt" not in report["artifacts"], field
        assert report["metrics"]["offline"] == {}, field


def test_evaluate_rejects_recomputed_duplicate_and_out_of_range_thresholds(tmp_path):
    cases = (
        lambda value: (
            value["thresholds"].update(REVIEW_SCORE=60),
            value["effective_settings"].update(review_score=70),
        ),
        lambda value: (
            value["thresholds"].update(REVIEW_SCORE=101),
            value["effective_settings"].update(review_score=101),
        ),
        lambda value: (
            value["thresholds"].update(REVIEW_SCORE=-1),
            value["effective_settings"].update(review_score=-1),
        ),
    )
    for index, mutate in enumerate(cases):
        report = _evaluate_fixture(tmp_path / f"threshold-semantic-{index}", mutate_offline=mutate)
        assert "failed_replay_receipt" in report["artifacts"]
        assert "rejected_threshold_candidate_receipt" not in report["artifacts"]
        assert report["metrics"]["offline"] == {}


def test_evaluate_requires_every_canonical_telemetry_replica(tmp_path):
    cases = (
        (
            "provider-missing",
            lambda root, receipt: _rebuild_run_artifact(
                root, "offline-run",
                lambda artifact: _mutate_telemetry_file(
                    artifact, lambda value: value["provider_budgets"]["llm"].pop("done")
                ),
                receipt_path=receipt,
            ),
            "TELEMETRY_REPLICA_IMMUTABLE",
        ),
        (
            "provider-extra",
            lambda root, receipt: _rebuild_run_artifact(
                root, "offline-run",
                lambda artifact: _mutate_telemetry_file(
                    artifact, lambda value: value["provider_budgets"]["llm"].update(extra=0)
                ),
                receipt_path=receipt,
            ),
            "TELEMETRY_REPLICA_IMMUTABLE",
        ),
        (
            "provider-wrong-type",
            lambda root, receipt: _rebuild_run_artifact(
                root, "offline-run",
                lambda artifact: _mutate_telemetry_file(
                    artifact, lambda value: value["provider_budgets"]["llm"].update(done=0.0)
                ),
                receipt_path=receipt,
            ),
            "TELEMETRY_REPLICA_IMMUTABLE",
        ),
        (
            "artifact-paid-plan-drift",
            lambda root, receipt: _rebuild_run_artifact(
                root, "offline-run",
                lambda artifact: _mutate_telemetry_file(
                    artifact, lambda value: value["paid_query_plan"].update(paid_query_plan_count=1)
                ),
                receipt_path=receipt,
            ),
            "TELEMETRY_REPLICA_IMMUTABLE",
        ),
        (
            "manifest-drift",
            lambda root, _receipt: _mutate_manifest_telemetry(root, "offline-run"),
            "TELEMETRY_REPLICA_MANIFEST",
        ),
        (
            "intent-drift",
            lambda root, _receipt: _mutate_intent_telemetry(root, "offline-run"),
            "TELEMETRY_REPLICA_INTENT",
        ),
        (
            "report-missing",
            lambda root, receipt: _rebuild_run_artifact(
                root, "offline-run", lambda artifact: (artifact / "report.txt").unlink(), receipt_path=receipt,
            ),
            "TELEMETRY_REPLICA_REPORT_MISSING",
        ),
        (
            "report-duplicate",
            lambda root, receipt: _rebuild_run_artifact(
                root, "offline-run",
                lambda artifact: (artifact / "report.txt").write_text(
                    (artifact / "report.txt").read_text(encoding="utf-8")
                    + (artifact / "report.txt").read_text(encoding="utf-8"), encoding="utf-8",
                ),
                receipt_path=receipt,
            ),
            "TELEMETRY_REPLICA_REPORT",
        ),
        (
            "db-canonical-source-drift",
            lambda root, _receipt: _mutate_db_canonical_source(root, "offline-run"),
            "TELEMETRY_CANONICAL_PAID_QUERY_PLAN",
        ),
        (
            "live-artifact-drift",
            lambda root, receipt: _rebuild_run_artifact(
                root, "live-run",
                lambda artifact: _mutate_telemetry_file(
                    artifact, lambda value: value["provider_budgets"]["llm"].update(done=1)
                ),
                receipt_path=receipt, live=True,
            ),
            "LIVE_TELEMETRY_REPLICA:TELEMETRY_REPLICA_IMMUTABLE",
        ),
    )
    for name, mutation, expected in cases:
        report = _evaluate_fixture(tmp_path / name, mutate_finalization=mutation)
        assert "failed_replay_receipt" in report["artifacts"], name
        assert "replay_receipt" not in report["artifacts"], name
        assert "rejected_threshold_candidate_receipt" not in report["artifacts"], name
        assert "offline_artifact_dir" not in report["artifacts"], name
        assert report["offline_artifact_hash"] == "", name
        assert report["provider_call_count"] == -1, name
        assert report["metrics"]["offline"] == {}, name
        assert any(expected in reason for reason in report["failure_reasons"]), (name, report["failure_reasons"])


def test_numeric_gate_evidence_rejects_bool_and_float_zero_values():
    for field in (
        "replay_miss_count", "replay_network_events", "provider_http_calls",
        "provider_call_count", "all_results_unique_ids",
    ):
        for value in (False, 0.0):
            evidence = _valid_evidence()
            evidence["behavioral"][field] = value
            assert evaluate_evidence(evidence)["behavioral_recall_validated"] is False
    evidence = _valid_evidence()
    evidence["checks"]["command_results"]["pytest"]["returncode"] = 0.0
    assert evaluate_evidence(evidence)["release_ready"] is False


if __name__ == "__main__":
    unittest.main()
