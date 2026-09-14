from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import config
import pytest
from modules.run_context import RunConfig
from tools.free_only_contract import inspect_database
from tools.reclassify_offline_replay_receipts import _atomic_write_if_changed, classify_receipt, migrate, write_superseding_record


PROVIDERS = ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")


def _database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            """
            CREATE TABLE runs (run_id TEXT, phase TEXT);
            CREATE TABLE run_items (run_id TEXT, item_index INTEGER, paid_required INTEGER, paid_state TEXT, paid_attempts INTEGER);
            CREATE TABLE results (run_id TEXT, item_index INTEGER, payload TEXT);
            CREATE TABLE provider_calls (run_id TEXT, provider TEXT, state TEXT);
            CREATE TABLE paid_attempts (run_id TEXT);
            CREATE TABLE paid_attempt_calls (run_id TEXT);
            CREATE TABLE provider_query_flights (run_id TEXT);
            CREATE TABLE provider_query_flight_consumers (run_id TEXT);
            CREATE TABLE provider_budget_blocks (run_id TEXT, provider TEXT);
            CREATE TABLE provider_usage (run_id TEXT, provider TEXT, configured_limit INTEGER, effective_limit INTEGER, reserved_total INTEGER, reserved INTEGER, completed INTEGER, failed INTEGER, unknown INTEGER);
            """
        )
        db.execute("INSERT INTO runs VALUES ('run', 'COMPLETE')")
        db.execute("INSERT INTO run_items VALUES ('run', 0, 0, 'NOT_REQUIRED', 0)")
        db.execute(
            "INSERT INTO results VALUES ('run', 0, ?)",
            (json.dumps({
                "paid_required": False, "paid_state": "NOT_REQUIRED", "paid_recommended": True,
                "paid_skipped_reason": "disabled_by_explicit_free_only_finalization",
                "email": "sentinel@example.com", "phone": "+90 555 555 55 55",
            }),),
        )
        db.executemany(
            "INSERT INTO provider_usage VALUES ('run', ?, 0, 0, 0, 0, 0, 0, 0)",
            [(provider,) for provider in PROVIDERS],
        )
        db.commit()


def _fixture(tmp_path: Path) -> Path:
    package_dir = tmp_path / "synthetic-package"
    package_dir.mkdir()
    with patch.object(config, "SEARCH_CACHE_MODE", "refresh"), patch.object(config, "CRAWL_CACHE_MODE", "refresh"):
        run_config = RunConfig.from_config(
            paid_enabled=False,
            budgets={provider: 0 for provider in PROVIDERS},
            finalize_without_paid=True,
        ).as_dict()
    package = {"run_config": run_config, "input": {"record_count": 1}}
    package["config_sha256"] = RunConfig.from_dict(run_config).sha256
    package_path = package_dir / "package_manifest.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    run_dir = tmp_path / "synthetic-run"
    (run_dir / "state").mkdir(parents=True)
    db_path = run_dir / "state" / "progress.sqlite3"
    _database(db_path)
    actual = json.loads(json.dumps(run_config))
    actual["search_cache_mode"] = "replay"
    actual["crawl_cache_mode"] = "replay"
    actual["effective_settings"]["search_cache_mode"] = "replay"
    actual["effective_settings"]["crawl_cache_mode"] = "replay"
    actual["effective_settings"]["publication_policy_min_safety_score"] = 70
    manifest = {
        "run_id": "run", "complete": True, "phase": "COMPLETE", "finalized": True, "status": "complete_free_only",
        "run_config": actual, "config_sha256": RunConfig.from_dict(actual).sha256,
        "artifact_set_sha256": "a" * 64,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    original = {
        "schema_version": 1, "status": "FAIL", "exit_code": 0,
        "package_manifest_sha256": hashlib.sha256(package_path.read_bytes()).hexdigest(),
        "run_dir": str(run_dir), "exception": "offline_free_only_run_config_mismatch",
        "replay_network_events": 0, "network_events": [],
    }
    original_path = tmp_path / "synthetic-receipt.json"
    original_path.write_text(json.dumps(original), encoding="utf-8")
    return original_path


def test_reclassification_uses_manifest_and_db_and_is_idempotent(tmp_path):
    original = _fixture(tmp_path)
    result = classify_receipt(original)
    assert result["schema_version"] == 2
    assert result["failure_reason"] == "REJECTED_THRESHOLD_CANDIDATE"
    assert result["paid_budget_nonzero"] is False
    assert result["paid_provider_calls"] == 0
    assert result["threshold_mismatch"] is True
    assert result["config_mismatch"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert "sentinel@example.com" not in serialized
    assert "+90 555 555 55 55" not in serialized

    first = write_superseding_record(original)
    output = original.with_suffix(".v2.json")
    first_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    first_mtime = output.stat().st_mtime_ns
    second = write_superseding_record(original)
    assert first == second
    assert hashlib.sha256(output.read_bytes()).hexdigest() == first_sha
    assert output.stat().st_mtime_ns == first_mtime
    assert json.loads(original.read_text(encoding="utf-8"))["schema_version"] == 1

    manifest_path = tmp_path / "disposition.json"
    migrate([original], manifest_path)
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["schema_version"] == 2
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest_mtime = manifest_path.stat().st_mtime_ns
    migrate([original], manifest_path)
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == manifest_sha
    assert manifest_path.stat().st_mtime_ns == manifest_mtime


def test_missing_complete_run_is_fail_closed(tmp_path):
    original = tmp_path / "missing.json"
    original.write_text(json.dumps({"schema_version": 1, "status": "FAIL", "exit_code": 22}), encoding="utf-8")
    result = migrate([original])
    record = result["records"][0]
    assert record["failure_reason"] == "COMPLETE_RUN_MISSING"
    assert record["replay_evidence_eligible"] is False
    assert original.with_suffix(".v2.json").is_file()


def test_incomplete_matching_run_is_inspected_but_never_passes(tmp_path):
    original = _fixture(tmp_path)
    manifest_path = tmp_path / "synthetic-run" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "running"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = classify_receipt(original)
    assert result["complete_run_missing"] is True
    assert result["failure_reason"] == "COMPLETE_RUN_MISSING"
    assert result["paid_budget_nonzero"] is False
    assert result["paid_activity_nonzero"] is False


def test_inspect_database_closes_read_only_connection_immediately(tmp_path):
    original = _fixture(tmp_path)
    db_path = tmp_path / "synthetic-run" / "state" / "progress.sqlite3"
    inspect_database(db_path, "run", 1)
    renamed = tmp_path / "renamed.sqlite3"
    db_path.replace(renamed)
    renamed.unlink()


def test_migrator_semantic_paid_enabled_manifest_is_config_invalid(tmp_path):
    original = _fixture(tmp_path)
    package_path = tmp_path / "synthetic-package" / "package_manifest.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["run_config"]["paid_enabled"] = True
    package["run_config"]["finalize_without_paid"] = False
    package["config_sha256"] = RunConfig.from_dict(package["run_config"]).sha256
    package_path.write_text(json.dumps(package), encoding="utf-8")
    receipt = json.loads(original.read_text(encoding="utf-8"))
    receipt["package_manifest_sha256"] = hashlib.sha256(package_path.read_bytes()).hexdigest()
    original.write_text(json.dumps(receipt), encoding="utf-8")
    result = classify_receipt(original)
    assert result["failure_reason"] == "CONFIG_INVALID"
    assert result["evidence_class"] == "REPLAY_FAILURE"
    assert result["replay_evidence_eligible"] is False
    assert "CONFIG_INVALID" in result["failure_reasons"]


def test_migrator_validates_run_manifest_semantics_independently(tmp_path):
    original = _fixture(tmp_path)
    manifest_path = tmp_path / "synthetic-run" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_config"]["paid_enabled"] = True
    manifest["run_config"]["finalize_without_paid"] = False
    manifest["config_sha256"] = RunConfig.from_dict(manifest["run_config"]).sha256
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = classify_receipt(original)
    assert result["failure_reason"] == "CONFIG_INVALID"
    assert result["evidence_class"] == "REPLAY_FAILURE"
    assert result["replay_evidence_eligible"] is False
    assert "CONFIG_INVALID" in result["failure_reasons"]


def test_atomic_writer_keeps_existing_output_on_replace_failure(tmp_path, monkeypatch):
    output = tmp_path / "record.v2.json"
    output.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(Path, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected replace failure")))
    with pytest.raises(OSError, match="injected replace failure"):
        _atomic_write_if_changed(output, "new\n")
    assert output.read_text(encoding="utf-8") == "old\n"
    assert list(tmp_path.glob(".record.v2.json.*.tmp")) == []


def test_negative_database_limit_is_validation_failure_not_paid_budget(tmp_path):
    original = _fixture(tmp_path)
    db_path = tmp_path / "synthetic-run" / "state" / "progress.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE provider_usage SET configured_limit=-1 WHERE provider='llm'")
        db.commit()
    observation = inspect_database(db_path, "run", 1)
    assert observation["paid_budget_nonzero"] is False
    assert observation["database_validation_failed"] is True
    result = classify_receipt(original)
    assert result["failure_reason"] == "DATABASE_VALIDATION_FAILED"
    assert result["paid_budget_nonzero"] is False
    assert result["database_validation_failed"] is True
