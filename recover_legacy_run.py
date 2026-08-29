"""Network-free, fail-closed migration of the legacy checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import shutil
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from modules import checkpoint, contact_identity, excel, output_artifacts, redaction, run_context, runtime
from modules.pipeline_runner import needs_paid_escalation


EXPECTED_INPUT_HASH = "29f966aa42be30375b1e52532140f0fa36cef094ae0301e7248c74dc275e5515"
MIGRATION_SCHEMA_VERSION = 2
QUARANTINE_STATE = "LEGACY_RECOVERY_PROVISIONAL_V2"
_EXCEL_FIELDS = (
    "company", "entity_id", "legacy_index", "source_record_id", "source_record_id_quality",
    "free_state", "paid_required", "paid_state", "website", "website_source", "website_status",
    "email", "email_source", "email_source_url", "alternative_emails", "alternative_email_sources",
    "email_verification", "email_verification_reason", "email_publication_status",
    "email_publication_reason", "phone", "phone_source", "phone_source_url", "phone_label",
    "alternative_phones", "alternative_phone_sources", "phone_publication_status",
    "phone_publication_reason", "contact_policy_version", "contact_status", "status", "confidence",
    "score", "publication_policy_version", "publication_policy_action", "publication_eligible",
    "publication_safety_score", "publication_risk_index", "publication_risk_tier",
    "publication_blockers", "collision_reason", "reason", "quarantine_state", "quarantine_status",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_legacy_checkpoint(db_path: Path, input_path: Path) -> dict:
    input_hash = _sha256(input_path)
    if input_hash != EXPECTED_INPUT_HASH:
        raise ValueError("legacy input hash mismatch")
    legacy_db_hash = _sha256(db_path)
    uri = f"file:{Path(db_path).resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("legacy checkpoint integrity check failed")
        if connection.execute("SELECT count(*) FROM runs").fetchone()[0] != 1:
            raise ValueError("legacy checkpoint must contain exactly one run")
        run = connection.execute("SELECT run_id,input_hash,run_signature FROM runs").fetchone()
        if not run or run[1] != EXPECTED_INPUT_HASH or not str(run[2]).strip():
            raise ValueError("legacy checkpoint run signature/input hash mismatch")
        rows = connection.execute("SELECT item_index,payload FROM results WHERE run_id=? ORDER BY item_index", (run[0],)).fetchall()
    indexes = [int(index) for index, _payload in rows]
    if len(rows) != 893 or indexes != list(range(893)):
        raise ValueError("legacy checkpoint must contain contiguous indexes 0..892")
    return {"rows": rows, "input_hash": input_hash, "legacy_db_hash": legacy_db_hash, "legacy_run_id": str(run[0]), "legacy_run_signature": str(run[2])}


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.staging.json")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _fast_legacy_sanitize(value, *, debug: bool = False):
    """Preserve legacy evidence/debug trees without repeatedly copying them."""
    if isinstance(value, dict):
        return {
            str(key): _fast_legacy_sanitize(item, debug=debug or str(key).startswith("__"))
            for key, item in value.items()
            if not redaction._sensitive_key(key)
        }
    if isinstance(value, list):
        return [_fast_legacy_sanitize(item, debug=debug) for item in value]
    if isinstance(value, str):
        return value if debug else redaction.redact_known_values(value)
    return value


def recover_legacy_run(db_path: Path, input_path: Path, destination: Path) -> dict:
    validation = validate_legacy_checkpoint(db_path, input_path)
    input_records = excel.read_company_records(input_path)
    if len(input_records) != 893:
        raise ValueError("original input must contain exactly 893 records")
    destination = Path(destination).resolve()
    destination_runs = destination / "runs"
    destination_runs.mkdir(parents=True, exist_ok=True)
    effective_config = run_context.RunConfig.from_config(paid_enabled=False)
    records = []
    source_id_mapping = []
    forensic_rows = []
    for index, payload in validation["rows"]:
        legacy_row = redaction.normalize_unicode_scalars(json.loads(payload))
        input_record = input_records[int(index)]
        if str(input_record.get("company", "")).strip().casefold() != str(legacy_row.get("company", "")).strip().casefold():
            raise ValueError(f"legacy/input company mismatch at index {index}")
        source_id, quality = run_context.source_record_identity(input_record, default_source="input")
        if not source_id or quality not in {"upstream", "derived"}:
            raise ValueError(f"ambiguous source identity at index {index}")
        legacy_source_id = str(legacy_row.get("source_record_id", "")).strip()
        source_id_mapping.append({
            "legacy_index": int(index),
            "company": str(input_record.get("company", "")),
            "legacy_source_record_id": legacy_source_id,
            "canonical_source_record_id": source_id,
            "input_row_sha256": hashlib.sha256(run_context.canonical_json(input_record).encode("utf-8")).hexdigest(),
        })
        forensic_rows.append({
            "legacy_index": int(index),
            "legacy_source_record_id": legacy_source_id,
            "payload_sha256": hashlib.sha256(str(payload).encode("utf-8")).hexdigest(),
            "payload": json.loads(payload),
        })
        row = dict(legacy_row)
        row["__index"] = int(index)
        row["source_record_id"] = source_id
        row["source_record_id_quality"] = quality
        row["quarantine_state"] = QUARANTINE_STATE
        row["quarantine_status"] = "PERMANENT_UNTIL_REVALIDATED"
        row["publication_eligible"] = False
        row["publication_blockers"] = "; ".join(filter(None, [str(row.get("publication_blockers") or ""), "legacy_recovery_provisional"]))
        stored = {str(key): value for key, value in row.items() if not redaction._sensitive_key(key)}
        records.append({
            "index": int(index), "status": row.get("status"), "reason": row.get("reason", ""),
            "paid_complete": row.get("__paid_escalation_complete"), "company": row.get("company", ""),
            "source_record_id": source_id,
            "source_record_id_quality": quality,
            "contacts": {field: contact_identity.contact_key(field, row.get(field)) for field in ("website", "email", "phone")},
            "fields": {key: row.get(key, "") for key in _EXCEL_FIELDS},
            "payload": json.dumps(redaction.sanitize(stored), ensure_ascii=False, separators=(",", ":")),
        })

    free_retry_indexes = {record["index"] for record in records if record["status"] == "PROCESSING_FAILED" and str(record["reason"] or "").strip()}
    paid_indexes = {record["index"] for record in records if record["paid_complete"] is False and record["index"] not in free_retry_indexes and needs_paid_escalation(record)}
    if len(free_retry_indexes) != 2 or len(paid_indexes) != 157:
        raise ValueError(f"legacy queue validation failed: free={len(free_retry_indexes)} paid={len(paid_indexes)}")
    for record in records:
        index = record["index"]
        record["fields"].update({
            "free_state": "PENDING" if index in free_retry_indexes else "DONE",
            "paid_required": index in paid_indexes,
            "paid_state": "PENDING" if index in paid_indexes else "NOT_REQUIRED",
            "quarantine_state": QUARANTINE_STATE,
            "quarantine_status": "PERMANENT_UNTIL_REVALIDATED",
            "publication_eligible": False,
            "publication_blockers": "legacy_recovery_provisional",
        })
        payload = json.loads(record["payload"])
        payload.update(record["fields"])
        record["payload"] = json.dumps(redaction.sanitize(payload), ensure_ascii=False, separators=(",", ":"))

    contact_groups: dict[tuple[str, str], list[int]] = {}
    for record in records:
        for field in ("website", "email", "phone"):
            key = record["contacts"][field]
            if key:
                contact_groups.setdefault((field, key), []).append(record["index"])
    shared_groups = {f"{field}:{value}": sorted(indexes) for (field, value), indexes in contact_groups.items() if len(indexes) > 1}
    legacy_collision_indexes = {record["index"] for record in records if str(record["company"]).strip().casefold() in {"akel", "akbarkod"}}
    for record in records:
        quarantined_fields = dict(record["fields"])
        collision_reason = record["index"] in legacy_collision_indexes and "LEGACY_MERGED_COLLISION" or "legacy_recovery_provisional"
        output_artifacts.suppress_all_contacts(quarantined_fields, collision_reason)
        record["fields"] = quarantined_fields
        payload = json.loads(record["payload"])
        payload.update(quarantined_fields)
        record["payload"] = json.dumps(redaction.sanitize(payload), ensure_ascii=False, separators=(",", ":"))

    lineage = {"type": "legacy_recovery", "legacy_db_sha256": validation["legacy_db_hash"], "legacy_run_id": validation["legacy_run_id"], "legacy_run_signature": validation["legacy_run_signature"], "migration_schema_version": MIGRATION_SCHEMA_VERSION}
    if len({item["canonical_source_record_id"] for item in source_id_mapping}) != 893:
        raise ValueError("original input source identities are not unique")
    run_id = run_context.canonical_run_id(input_sha256=validation["input_hash"], ordered_source_record_ids=[record["source_record_id"] for record in records], effective_config=effective_config.as_dict(), runtime_source_tree_hash=run_context.source_tree_sha256(), lineage=lineage)
    run_root = destination_runs / run_id
    if run_root.exists():
        existing_report = run_root / "output" / "migration_report.json"
        existing_manifest = run_root / "manifest.json"
        if not existing_report.exists() or not existing_manifest.exists():
            raise RuntimeError("existing recovery run is not verifiable")
        existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
        if existing.get("run_id") != run_id or existing.get("input_sha256") != validation["input_hash"]:
            raise RuntimeError("existing recovery run identity mismatch")
        artifact_dir = run_root / "output" / "artifacts" / str(existing.get("artifact_set_sha256", ""))
        for name, info in existing.get("files", {}).items():
            path = artifact_dir / name
            if not path.is_file() or _sha256(path) != info.get("sha256"):
                raise RuntimeError("existing recovery artifact mismatch")
        run_context.validate_run_bundle(run_root, expected_input_hash=validation["input_hash"], expected_config_hash=effective_config.sha256, expected_source_ids=[record["source_record_id"] for record in records])
        return json.loads(existing_report.read_text(encoding="utf-8"))
    staging_root = destination_runs / f".{run_id}.{uuid.uuid4().hex}.staging"
    run_root = staging_root
    output_dir = run_root / "output"
    state_dir = run_root / "state"
    staging_dir = run_root / ".artifact_staging"
    staging_dir.mkdir(parents=True, exist_ok=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    blocked_indexes = set(legacy_collision_indexes)
    for indexes in shared_groups.values():
        blocked_indexes.update(indexes)
    output_rows = []
    for record in records:
        item = dict(record["fields"])
        index = record["index"]
        item.update({
            "legacy_index": index,
            "source_record_id": record["source_record_id"],
            "source_record_id_quality": record["source_record_id_quality"],
            "free_state": "PENDING" if index in free_retry_indexes else "DONE",
            "paid_required": index in paid_indexes,
            "paid_state": "PENDING" if index in paid_indexes else "NOT_REQUIRED",
        })
        item["publication_eligible"] = False
        item["publication_blockers"] = "; ".join(filter(None, [str(item.get("publication_blockers") or ""), "legacy_recovery_provisional"]))
        if index in blocked_indexes or index in free_retry_indexes or index in paid_indexes:
            item["collision_reason"] = "LEGACY_MERGED_COLLISION" if index in legacy_collision_indexes else "legacy_common_contact_collision" if index in blocked_indexes else item.get("collision_reason", "")
            output_artifacts.suppress_all_contacts(item, item["collision_reason"] or "legacy_recovery_provisional")
        output_rows.append(item)

    all_results_path = staging_dir / "all_results.xlsx"
    excel.write_contacts(all_results_path, (redaction.sanitize(item) for item in output_rows))
    if not all_results_path.exists():
        raise RuntimeError("recovery writer produced no all_results.xlsx")
    context = {"run_id": run_id, "input_hash": validation["input_hash"], "phase": "FREE", "lineage": lineage, "provisional": True, "paid_queue_frozen": True}
    budgets = {name: 0 for name in ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")}
    seed_items, seed_results = [], []
    for record in records:
        index = record["index"]
        payload_text = record["payload"]
        if index in legacy_collision_indexes:
            stored_payload = json.loads(payload_text)
            stored_payload["collision_reason"] = "LEGACY_MERGED_COLLISION"
            payload_text = json.dumps(stored_payload, ensure_ascii=False, separators=(",", ":"))
        seed_items.append({
            "item_index": index, "source_record_id": record["source_record_id"],
            "free_state": "PENDING" if index in free_retry_indexes else "DONE",
            "paid_required": index in paid_indexes,
            "paid_state": "PENDING" if index in paid_indexes else "NOT_REQUIRED",
            "last_error": str(record["reason"] or "") if index in free_retry_indexes else "",
            "payload_sha256": hashlib.sha256(payload_text.encode("utf-8")).hexdigest(),
            "quarantine_state": str(record["fields"].get("quarantine_state", "")),
            "quarantine_status": str(record["fields"].get("quarantine_status", "")),
            "publication_blockers": str(record["fields"].get("publication_blockers", "")),
        })
        seed_results.append({"item_index": index, "payload": payload_text})
    checkpoint.seed_recovered_run(
        path=state_dir / "progress.sqlite3", run_id=run_id,
        input_hash=validation["input_hash"], run_signature=validation["legacy_run_signature"],
        context=context, budgets=budgets, items=seed_items, results=seed_results,
    )
    with closing(sqlite3.connect(state_dir / "progress.sqlite3")) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()

    mapping_payload = {
        "schema_version": 1,
        "input_sha256": validation["input_hash"],
        "legacy_db_sha256": validation["legacy_db_hash"],
        "mappings": source_id_mapping,
    }
    mapping_bytes = run_context.canonical_json(mapping_payload).encode("utf-8")
    mapping_payload["mapping_sha256"] = hashlib.sha256(mapping_bytes).hexdigest()
    _atomic_json(staging_dir / "source_id_mapping.json", mapping_payload)
    forensic_path = staging_dir / "forensic_legacy_payloads.jsonl"
    with forensic_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in forensic_rows:
            handle.write(json.dumps(redaction.sanitize(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    counters = runtime.snapshot().get("counters", {})
    network_attempts = sum(int(value) for key, value in counters.items() if key.endswith(".requests"))
    network_blocked = sum(int(value) for key, value in counters.items() if "blocked" in key or key.endswith(".cooldown_skips"))
    network_successes = sum(int(value) for key, value in counters.items() if "success" in key)
    report = {"complete": False, "provisional": True, "migration_schema_version": MIGRATION_SCHEMA_VERSION, "run_id": run_id, "input_sha256": validation["input_hash"], "legacy_db_sha256": validation["legacy_db_hash"], "legacy_run_id": validation["legacy_run_id"], "legacy_run_signature": validation["legacy_run_signature"], "transferred_rows": len(records), "free_retry_indexes": sorted(free_retry_indexes), "free_retry_indexes_sha256": hashlib.sha256(run_context.canonical_json(sorted(free_retry_indexes)).encode()).hexdigest(), "paid_pending_indexes": sorted(paid_indexes), "paid_pending_indexes_sha256": hashlib.sha256(run_context.canonical_json(sorted(paid_indexes)).encode()).hexdigest(), "pending_paid": len(paid_indexes), "pending_free_retry": len(free_retry_indexes), "legacy_collision_indexes": sorted(legacy_collision_indexes), "source_id_quality": {"derived": len(records)}, "common_contact_groups": [{"contact": key, "indexes": indexes} for key, indexes in sorted(shared_groups.items())], "provisional_publication_eligible": 0, "paid_budgets": budgets, "network_attempts": network_attempts, "network_blocked": network_blocked, "network_successes": network_successes, "network_evidence": {"mode": "offline_recovery", "runtime_counter_keys": sorted(key for key in counters if key.endswith(".requests") or "blocked" in key or "success" in key)}, "replay_loaded": False, "counts": {"input": len(records), "free_retry": len(free_retry_indexes), "paid_pending": len(paid_indexes)}}
    _atomic_json(staging_dir / "migration_report.json", report)
    shutil.copy2(state_dir / "progress.sqlite3", staging_dir / "recovery_state.sqlite3")
    artifact_files = [staging_dir / "all_results.xlsx", staging_dir / "migration_report.json", staging_dir / "recovery_state.sqlite3", staging_dir / "source_id_mapping.json", staging_dir / "forensic_legacy_payloads.jsonl"]
    artifact_hash = hashlib.sha256("".join(f"{p.name}:{_sha256(p)}\n" for p in sorted(artifact_files)).encode()).hexdigest()
    artifact_dir = output_dir / "artifacts" / artifact_hash
    artifact_dir.mkdir(parents=True, exist_ok=False)
    files = {}
    for source in artifact_files:
        target = artifact_dir / source.name
        source.replace(target)
        files[target.name] = {"sha256": _sha256(target), "bytes": target.stat().st_size, "rows": len(records) if target.suffix == ".xlsx" else None}
    shutil.copy2(artifact_dir / "all_results.xlsx", output_dir / "all_results.xlsx")
    shutil.copy2(artifact_dir / "migration_report.json", output_dir / "migration_report.json")
    manifest = {"manifest_schema_version": 4, "complete": False, "provisional": True, "quarantine_state": QUARANTINE_STATE, "phase": "FREE", "paid_enabled": False, "run_id": run_id, "input_sha256": validation["input_hash"], "config_sha256": effective_config.sha256, "run_config": effective_config.as_dict(), "runtime_source_tree_sha256": run_context.source_tree_sha256(), "item_count": len(records), "ordered_source_record_ids": [record["source_record_id"] for record in records], "run_signature": validation["legacy_run_signature"], "artifact_set_sha256": artifact_hash, "files": files, "counts": {"input": len(records), "paid_pending": len(paid_indexes), "free_retry": len(free_retry_indexes)}, "lineage": lineage, "source_id_mapping_sha256": mapping_payload["mapping_sha256"], "forensic_payloads_sha256": files["forensic_legacy_payloads.jsonl"]["sha256"], "checkpoint_sha256": files["recovery_state.sqlite3"]["sha256"]}
    _atomic_json(run_root / "manifest.json", manifest)
    staging_dir.rmdir()
    staging_root.replace(destination_runs / run_id)
    run_context.validate_run_bundle(destination_runs / run_id, expected_input_hash=validation["input_hash"], expected_config_hash=effective_config.sha256, expected_source_ids=[record["source_record_id"] for record in records])
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recover legacy checkpoint without network")
    parser.add_argument("--legacy-db", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(recover_legacy_run(args.legacy_db, args.input, args.destination), ensure_ascii=False, indent=2))
