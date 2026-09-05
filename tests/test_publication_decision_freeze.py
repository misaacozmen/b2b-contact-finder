from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from materialize_legacy_publication_decisions import materialize
from modules import evidence, output_artifacts, publication_policy, quality_audit, report


CONFIG_HASH = "a" * 64


def _row(source_id: str = "src:A", run_id: str = "run-1") -> dict:
    return {
        "company": "Example",
        "source_record_id": source_id,
        "run_id": run_id,
        "config_sha256": CONFIG_HASH,
        "status": "REVIEW_NEEDED",
        "publication_eligible": False,
        "publication_advisory_eligible": False,
        "publication_blockers": "manual_review",
        "reason": "manual_review",
        "__evaluation": {},
    }


def _frozen(source_id: str = "src:A", run_id: str = "run-1") -> dict:
    row = _row(source_id, run_id)
    decision = publication_policy.freeze_publication_decision(row, {}, config_sha256=CONFIG_HASH)
    row["publication_blockers"] = "; ".join(decision["blockers"])
    publication_policy.apply_frozen_decision_fields(row, decision)
    return row


def test_normal_finalization_freezes_once(tmp_path: Path):
    row = _row()
    snapshot = {"generated_at": "2000-01-01T00:00:00+00:00", "counters": {}, "budgets": {}}
    with patch.multiple(
        config,
        OUTPUT_DIR=tmp_path,
        CONTACTS_FILE=tmp_path / "contacts.xlsx",
        VERIFIED_CONTACTS_FILE=tmp_path / "verified.xlsx",
        REVIEW_QUEUE_FILE=tmp_path / "review.xlsx",
        FAILED_FILE=tmp_path / "failed.xlsx",
        CANDIDATES_FILE=tmp_path / "candidates.xlsx",
        EVIDENCE_FILE=tmp_path / "evidence.jsonl",
        ENTITY_RELATIONSHIPS_FILE=tmp_path / "entity.jsonl",
        QUALITY_AUDIT_FILE=tmp_path / "quality.json",
        DISCOVERY_COVERAGE_FILE=tmp_path / "coverage.json",
        REPORT_FILE=tmp_path / "report.txt",
        TELEMETRY_FILE=tmp_path / "telemetry.json",
    ):
        with patch.object(publication_policy, "decide_row", wraps=publication_policy.decide_row) as decide:
            output_artifacts.write_outputs([row], 0, telemetry_snapshot=snapshot)
        assert decide.call_count == 1


def test_handoff_frozen_decision_is_not_recomputed(tmp_path: Path):
    row = _frozen()
    snapshot = {"generated_at": "2000-01-01T00:00:00+00:00", "counters": {}, "budgets": {}}
    with patch.multiple(
        config,
        OUTPUT_DIR=tmp_path,
        CONTACTS_FILE=tmp_path / "contacts.xlsx",
        VERIFIED_CONTACTS_FILE=tmp_path / "verified.xlsx",
        REVIEW_QUEUE_FILE=tmp_path / "review.xlsx",
        FAILED_FILE=tmp_path / "failed.xlsx",
        CANDIDATES_FILE=tmp_path / "candidates.xlsx",
        EVIDENCE_FILE=tmp_path / "evidence.jsonl",
        ENTITY_RELATIONSHIPS_FILE=tmp_path / "entity.jsonl",
        QUALITY_AUDIT_FILE=tmp_path / "quality.json",
        DISCOVERY_COVERAGE_FILE=tmp_path / "coverage.json",
        REPORT_FILE=tmp_path / "report.txt",
        TELEMETRY_FILE=tmp_path / "telemetry.json",
    ):
        with patch.object(publication_policy, "decide_row", side_effect=AssertionError("recomputed")):
            output_artifacts.write_outputs([row], 0, telemetry_snapshot=snapshot)


def test_final_surfaces_never_call_decide_row_when_frozen(tmp_path: Path):
    row = _frozen()
    with patch.object(publication_policy, "decide_row", side_effect=AssertionError("final surface recomputed")):
        assert output_artifacts.partition_output_rows([row], require_frozen_decisions=True)[1]
        report.build_report([row], 0, require_frozen_decisions=True)
        quality_audit.payload([row])
        evidence.write_jsonl(tmp_path / "evidence.jsonl", [row])


def test_deleting_evaluation_cannot_change_frozen_decision():
    row = _frozen()
    original = row["publication_decision"]
    row.pop("__evaluation")
    assert publication_policy.freeze_publication_decision(row, {}, config_sha256=CONFIG_HASH) is original
    assert row["publication_decision_sha256"] == original["publication_decision_sha256"]


@pytest.mark.parametrize("field", ["source_record_id", "run_id", "config_sha256", "decision_input_sha256"])
def test_tampering_or_row_rebinding_fails(field: str):
    row = _frozen()
    envelope = json.loads(json.dumps(row["publication_decision"]))
    if field == "source_record_id":
        with pytest.raises(RuntimeError):
            publication_policy.verify_publication_decision(envelope, source_record_id="src:B")
    elif field == "run_id":
        with pytest.raises(RuntimeError):
            publication_policy.verify_publication_decision(envelope, run_id="run-2")
    elif field == "config_sha256":
        with pytest.raises(RuntimeError):
            publication_policy.verify_publication_decision(envelope, config_sha256="b" * 64)
    else:
        envelope[field] = "b" * 64
        with pytest.raises(RuntimeError):
            publication_policy.verify_publication_decision(envelope)


def test_valid_envelope_moved_from_a_to_b_fails():
    envelope = _frozen("src:A")["publication_decision"]
    with pytest.raises(RuntimeError):
        publication_policy.verify_publication_decision(envelope, source_record_id="src:B")


def test_evidence_requires_frozen_envelope(tmp_path: Path):
    with pytest.raises(RuntimeError, match="frozen publication_decision"):
        evidence.write_jsonl(tmp_path / "evidence.jsonl", [_row()])


def test_legacy_missing_evaluation_is_review_with_unjoinable_blocker(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.sqlite3"
    run_id = "run-legacy"
    source_id = "src:legacy"
    payload_text = json.dumps({"company": "Legacy", "source_record_id": source_id}, separators=(",", ":"))
    payload_hash = hashlib.sha256(payload_text.encode()).hexdigest()
    with sqlite3.connect(checkpoint) as connection:
        connection.executescript(
            "CREATE TABLE runs(run_id TEXT PRIMARY KEY);"
            "CREATE TABLE run_items(run_id TEXT,item_index INTEGER,source_record_id TEXT,payload_sha256 TEXT);"
            "CREATE TABLE results(run_id TEXT,item_index INTEGER,payload TEXT);"
        )
        connection.execute("INSERT INTO runs VALUES(?)", (run_id,))
        connection.execute("INSERT INTO run_items VALUES(?,?,?,?)", (run_id, 0, source_id, payload_hash))
        connection.execute("INSERT INTO results VALUES(?,?,?)", (run_id, 0, payload_text))
        connection.commit()
    parent = tmp_path / "parent.json"
    parent.write_text(json.dumps({
        "run_id": run_id,
        "config_sha256": CONFIG_HASH,
        "source_record_ids": [source_id],
    }), encoding="utf-8")
    result = materialize(checkpoint_path=checkpoint, manifest_path=parent, destination=tmp_path / "materialized")
    decisions = [json.loads(line) for line in (tmp_path / "materialized" / "publication_decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert result["count"] == 1
    assert decisions[0]["publication_decision"]["publishable"] is False
    assert "legacy_evidence_unjoinable" in decisions[0]["publication_decision"]["blockers"]
