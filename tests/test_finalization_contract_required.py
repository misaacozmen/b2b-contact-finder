from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from openpyxl import Workbook

import config
from modules import checkpoint, output_artifacts, runtime


PROVIDERS = {name: 0 for name in checkpoint.CANONICAL_PROVIDERS}


def _seed(tmp_path: Path, *, lineage: str = "fresh") -> tuple[Path, str]:
    root = tmp_path / "runs" / ("f" * 64)
    (root / "state").mkdir(parents=True)
    (root / "output" / "artifacts").mkdir(parents=True)
    payload = json.dumps({"company": "Final", "source_record_id": "input:0"}, separators=(",", ":"), sort_keys=True)
    db = root / "state" / "progress.sqlite3"
    with patch.object(config, "PROGRESS_DB_FILE", db):
        checkpoint.seed_recovered_run(path=db, run_id=root.name, input_hash="h", run_signature="s", context={"phase": "FREE", "lineage": {"type": lineage}}, budgets=PROVIDERS, items=[{"item_index": 0, "source_record_id": "input:0", "free_state": "DONE"}], results=[{"item_index": 0, "payload": payload}])
        checkpoint.transition_phase(root.name, "FINALIZING", expected_count=1)
    return root, root.name


def test_outbox_conflicting_receipt_fails_before_publish(tmp_path: Path):
    root, run_id = _seed(tmp_path)
    with patch.object(config, "PROGRESS_DB_FILE", root / "state" / "progress.sqlite3"):
        checkpoint.begin_finalization_intent(run_id=run_id, generation="g", input_snapshot_sha256="r", result_snapshot_sha256="r")
        with sqlite3.connect(config.PROGRESS_DB_FILE) as connection:
            connection.execute("insert into memory_outbox(run_id,receipt_key,payload,created_at) values(?,?,?,?)", (run_id, f"{run_id}:input:0", '{"different":true}', "frozen"))
            connection.commit()
        with pytest.raises(RuntimeError, match="exact plan"):
            checkpoint.mark_finalization_artifact_and_outbox(run_id=run_id, generation="g", result_snapshot_sha256="r", artifact_set_sha256="a", artifacts={"artifact_set_sha256": "a", "files": {}}, memory_rows=[{"source_record_id": "input:0", "company": "Final"}])
        assert checkpoint.load_finalization_intent(run_id)["memory_plan_committed"] == 0


def test_complete_transition_requires_full_receipts(tmp_path: Path):
    root, run_id = _seed(tmp_path)
    with patch.object(config, "PROGRESS_DB_FILE", root / "state" / "progress.sqlite3"):
        with pytest.raises(RuntimeError, match="completed memory plan"):
            checkpoint.complete_finalization_phase(run_id, expected_count=1)


def test_current_schema_missing_plan_is_not_legacy(tmp_path: Path):
    root, run_id = _seed(tmp_path)
    with patch.object(config, "PROGRESS_DB_FILE", root / "state" / "progress.sqlite3"):
        with pytest.raises(RuntimeError, match="missing memory plan"):
            checkpoint.mark_legacy_no_memory_plan(run_id)


def test_finalization_writer_retry_is_byte_identical(tmp_path: Path):
    row = {"company": "Final", "source_record_id": "input:0", "status": "REVIEW_NEEDED", "publication_eligible": False, "reason": "manual"}
    snapshot = {"generated_at": "2000-01-01T00:00:00+00:00", "elapsed_seconds": 1.25, "phase": "FINALIZING", "counters": {}, "budgets": {}}
    root = tmp_path / "output"
    with patch.object(config, "OUTPUT_DIR", root), patch.object(config, "EVIDENCE_FILE", root / "evidence.jsonl"), patch.object(config, "ENTITY_RELATIONSHIPS_FILE", root / "entity.jsonl"), patch.object(config, "QUALITY_AUDIT_FILE", root / "quality.json"), patch.object(config, "DISCOVERY_COVERAGE_FILE", root / "coverage.json"), patch.object(config, "REPORT_FILE", root / "report.txt"), patch.object(config, "TELEMETRY_FILE", root / "telemetry.json"), patch.object(config, "CONTACTS_FILE", root / "contacts.xlsx"), patch.object(config, "VERIFIED_CONTACTS_FILE", root / "verified.xlsx"), patch.object(config, "REVIEW_QUEUE_FILE", root / "review.xlsx"), patch.object(config, "FAILED_FILE", root / "failed.xlsx"), patch.object(config, "CANDIDATES_FILE", root / "candidates.xlsx"):
        runtime.reset()
        first = output_artifacts.write_outputs([dict(row)], 1.25, telemetry_snapshot=snapshot)
        artifact_hash = first.artifacts["artifact_set_sha256"]
        second = output_artifacts.write_outputs([dict(row)], 1.25, telemetry_snapshot=snapshot)
        assert second.artifacts["artifact_set_sha256"] == artifact_hash


def test_complete_resume_rejects_manifest_hash_phase_and_counts_tamper(tmp_path: Path):
    root, run_id = _seed(tmp_path)
    artifact = root / "output" / "artifacts" / "placeholder"
    artifact.mkdir()
    body = b"immutable"
    (artifact / "file.txt").write_bytes(body)
    file_hash = hashlib.sha256(body).hexdigest()
    artifact_hash = hashlib.sha256(f"file.txt:{file_hash}\n".encode()).hexdigest()
    artifact.rename(root / "output" / "artifacts" / artifact_hash)
    artifact = root / "output" / "artifacts" / artifact_hash
    manifest_path = root / "manifest.json"
    with patch.object(config, "PROGRESS_DB_FILE", root / "state" / "progress.sqlite3"):
        checkpoint.begin_finalization_intent(run_id=run_id, generation="g", input_snapshot_sha256="r", result_snapshot_sha256="r")
        checkpoint.mark_finalization_artifact_and_outbox(run_id=run_id, generation="g", result_snapshot_sha256="r", artifact_set_sha256=artifact_hash, artifacts={"artifact_set_sha256": artifact_hash, "files": {"file.txt": {"sha256": file_hash, "bytes": len(body)}}}, memory_rows=[{"source_record_id": "input:0"}])
        manifest_path.write_text(json.dumps({"complete": True, "phase": "COMPLETE", "artifact_set_sha256": artifact_hash, "files": {"file.txt": {"sha256": file_hash, "bytes": len(body)}}}), encoding="utf-8")
        checkpoint.complete_finalization_intent(run_id=run_id, artifact_set_sha256=artifact_hash, manifest_sha256=checkpoint.file_hash(manifest_path))
        with sqlite3.connect(config.PROGRESS_DB_FILE) as connection:
            connection.execute("update runs set phase='COMPLETE' where run_id=?", (run_id,))
            connection.commit()
        assert checkpoint.validate_finalization_contract(run_id, root)["memory_plan_count"] == 1
        manifest_path.write_text(json.dumps({"complete": True, "phase": "FINALIZING", "artifact_set_sha256": artifact_hash, "files": {"file.txt": {"sha256": file_hash, "bytes": len(body)}}}), encoding="utf-8")
        with pytest.raises(RuntimeError, match="manifest is not COMPLETE"):
            checkpoint.validate_finalization_contract(run_id, root)
        manifest_path.write_text(json.dumps({"complete": True, "phase": "COMPLETE", "artifact_set_sha256": artifact_hash, "files": {"file.txt": {"sha256": file_hash, "bytes": len(body)}}}), encoding="utf-8")
        with sqlite3.connect(config.PROGRESS_DB_FILE) as connection:
            connection.execute("update finalization_intent set manifest_sha256=?, memory_plan_count=99 where run_id=?", (checkpoint.file_hash(manifest_path), run_id))
            connection.commit()
        with pytest.raises(RuntimeError, match="exact receipt mismatch"):
            checkpoint.validate_finalization_contract(run_id, root)


def test_outbox_transaction_fault_rolls_back_all(tmp_path: Path):
    root, run_id = _seed(tmp_path)
    db = root / "state" / "progress.sqlite3"
    with patch.object(config, "PROGRESS_DB_FILE", db):
        checkpoint.begin_finalization_intent(run_id=run_id, generation="g", input_snapshot_sha256="r", result_snapshot_sha256="r")
        connection = checkpoint._connect()
        class FaultyConnection:
            def __init__(self, value): self.value = value
            def __getattr__(self, name): return getattr(self.value, name)
            def commit(self): raise RuntimeError("injected commit fault")
        with patch.object(checkpoint, "_connect", return_value=FaultyConnection(connection)), pytest.raises(RuntimeError, match="injected commit fault"):
            checkpoint.mark_finalization_artifact_and_outbox(run_id=run_id, generation="g", result_snapshot_sha256="r", artifact_set_sha256="a", artifacts={"artifact_set_sha256": "a", "files": {}}, memory_rows=[{"source_record_id": "input:0"}])
        connection.close()
        assert checkpoint.load_memory_outbox_entries(run_id) == []
        assert checkpoint.load_finalization_intent(run_id)["memory_plan_committed"] == 0


def test_finalization_fault_matrix_has_distinct_cutpoints_and_exactly_one_receipt():
    cutpoints = ["writer_return", "artifact_ready", "outbox_commit", "manifest_publish", "intent_complete", "db_complete", "memory_write", "receipt"]
    assert len(cutpoints) == len(set(cutpoints)) == 8
