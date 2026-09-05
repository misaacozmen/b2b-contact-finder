"""Materialize legacy decisions only from a verified SQLite checkpoint bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from modules import publication_policy, redaction


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _manifest_ids(payload: dict[str, Any]) -> list[str]:
    values = payload.get("source_record_ids") or payload.get("ordered_source_record_ids")
    if values is None:
        values = payload.get("items") or payload.get("records") or []
    result: list[str] = []
    for item in values:
        value = item.get("source_record_id") if isinstance(item, dict) else item
        value = str(value or "").strip()
        if not value:
            raise RuntimeError("legacy parent manifest contains an empty source_record_id")
        if value in result:
            raise RuntimeError(f"legacy parent manifest contains duplicate source_record_id: {value}")
        result.append(value)
    if not result:
        raise RuntimeError("legacy parent manifest must contain ordered source_record_ids")
    return result


def _file_entry(manifest: dict[str, Any], path: Path | None) -> dict[str, Any] | None:
    if path is None or not isinstance(manifest.get("files"), dict):
        return None
    for key in (path.name, str(path), str(path.resolve())):
        value = manifest["files"].get(key)
        if isinstance(value, dict):
            return value
    return None


def _declared_hash(manifest: dict[str, Any], path: Path | None, field: str) -> str | None:
    if path is None:
        return None
    value = manifest.get(field) or (_file_entry(manifest, path) or {}).get("sha256")
    return str(value) if value else None


def _ensure_input_sidecar(
    *, manifest_path: Path, checkpoint_path: Path, evidence_path: Path | None, manifest: dict[str, Any], sidecar_path: Path
) -> Path:
    checkpoint_hash = _sha256(checkpoint_path)
    evidence_hash = _sha256(evidence_path) if evidence_path is not None else None
    parent_hash = _sha256(manifest_path)
    declared_checkpoint = _declared_hash(manifest, checkpoint_path, "checkpoint_sha256")
    declared_evidence = _declared_hash(manifest, evidence_path, "evidence_sha256")
    if declared_checkpoint and declared_checkpoint != checkpoint_hash:
        raise RuntimeError("legacy checkpoint hash mismatch")
    if declared_evidence and declared_evidence != evidence_hash:
        raise RuntimeError("legacy evidence hash mismatch")
    sidecar = Path(sidecar_path).resolve()
    expected = {
        "parent_manifest_sha256": parent_hash,
        "checkpoint_sha256": checkpoint_hash,
        "evidence_sha256": evidence_hash,
        "checkpoint_bytes": checkpoint_path.stat().st_size,
        "evidence_bytes": evidence_path.stat().st_size if evidence_path is not None else None,
    }
    if sidecar.exists():
        if _read_json(sidecar) != expected:
            raise RuntimeError("legacy materialization sidecar does not match observed inputs")
        return sidecar
    # Historical parents may omit these hashes; record observations in a
    # sidecar and require the same immutable inputs for every later run.
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sidecar


def _verify_input_hashes(
    *, manifest_path: Path, checkpoint_path: Path, evidence_path: Path | None, manifest: dict[str, Any], sidecar: Path
) -> None:
    expected = {
        "parent_manifest_sha256": _sha256(manifest_path),
        "checkpoint_sha256": _sha256(checkpoint_path),
        "evidence_sha256": _sha256(evidence_path) if evidence_path is not None else None,
        "checkpoint_bytes": checkpoint_path.stat().st_size,
        "evidence_bytes": evidence_path.stat().st_size if evidence_path is not None else None,
    }
    if _read_json(sidecar) != expected:
        raise RuntimeError("legacy materialization input changed after sidecar creation")
    declared_checkpoint = _declared_hash(manifest, checkpoint_path, "checkpoint_sha256")
    if declared_checkpoint and declared_checkpoint != expected["checkpoint_sha256"]:
        raise RuntimeError("legacy parent checkpoint hash mismatch")
    if evidence_path is not None:
        declared = _declared_hash(manifest, evidence_path, "evidence_sha256")
        if declared and declared != expected["evidence_sha256"]:
            raise RuntimeError("legacy parent evidence hash mismatch")


def _read_evidence(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise RuntimeError("legacy evidence record must be an object")
        source_id = str(record.get("source_record_id", "") or "").strip()
        if not source_id or source_id in result:
            raise RuntimeError("legacy evidence has empty or duplicate source_record_id")
        result[source_id] = record
    return result


def _checkpoint_rows(checkpoint_path: Path, run_id: str) -> list[dict[str, Any]]:
    if checkpoint_path.suffix.casefold() not in {".sqlite", ".sqlite3", ".db"}:
        raise RuntimeError("legacy materialization requires a SQLite checkpoint")
    try:
        with sqlite3.connect(checkpoint_path) as connection:
            run = connection.execute("SELECT run_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise RuntimeError(f"legacy checkpoint does not contain run_id: {run_id}")
            joined = connection.execute(
                "SELECT i.item_index,i.source_record_id,i.payload_sha256,r.payload "
                "FROM run_items AS i LEFT JOIN results AS r "
                "ON r.run_id=i.run_id AND r.item_index=i.item_index "
                "WHERE i.run_id=? ORDER BY i.item_index",
                (run_id,),
            ).fetchall()
            result_only = connection.execute(
                "SELECT item_index FROM results WHERE run_id=? ORDER BY item_index", (run_id,)
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError("legacy checkpoint could not be read") from exc
    if len(joined) != len(result_only):
        raise RuntimeError("legacy checkpoint has missing or extra result rows")
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item_index, item_source_id, stored_hash, payload_text in joined:
        source_id = str(item_source_id or "").strip()
        if not source_id or source_id in seen_ids:
            raise RuntimeError("legacy checkpoint has empty or duplicate source_record_id")
        seen_ids.add(source_id)
        if payload_text is None:
            raise RuntimeError(f"legacy checkpoint result is missing: {item_index}")
        payload_raw = str(payload_text)
        actual_hash = hashlib.sha256(payload_raw.encode("utf-8")).hexdigest()
        if str(stored_hash or "") != actual_hash:
            raise RuntimeError(f"legacy checkpoint payload hash mismatch: {item_index}")
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"legacy checkpoint payload is invalid JSON: {item_index}") from exc
        if not isinstance(payload, dict) or str(payload.get("source_record_id", "") or "").strip() != source_id:
            raise RuntimeError(f"legacy checkpoint source_record_id mismatch: {item_index}")
        rows.append({"item_index": int(item_index), "payload": payload})
    return rows


def _legacy_review_decision(payload: dict[str, Any], *, config_sha256: str, run_id: str) -> dict[str, Any]:
    row = dict(payload)
    row.pop("publication_decision", None)
    row["run_id"] = run_id
    row["status"] = "REVIEW_NEEDED"
    row["publication_eligible"] = False
    row["publication_advisory_eligible"] = False
    row["publication_blockers"] = "legacy_evidence_unjoinable"
    row["__evaluation"] = {}
    envelope = publication_policy.freeze_publication_decision(row, {}, config_sha256=config_sha256)
    return publication_policy.with_blocker(envelope, "legacy_evidence_unjoinable")


def materialize(
    *,
    checkpoint_path: Path,
    manifest_path: Path,
    evidence_path: Path | None = None,
    output_path: Path | None = None,
    destination: Path | None = None,
    sidecar_path: Path | None = None,
) -> dict[str, Any]:
    """Write a new source-ID evidence/decision bundle without Excel input."""
    checkpoint_path = Path(checkpoint_path).resolve()
    manifest_path = Path(manifest_path).resolve()
    evidence_path = Path(evidence_path).resolve() if evidence_path is not None else None
    target = Path(destination or output_path or "").resolve()
    if not str(target) or target.suffix.casefold() in {".json", ".jsonl"}:
        raise ValueError("legacy materialization requires a new destination directory")
    if target.exists():
        raise FileExistsError(f"legacy materialization destination already exists: {target}")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise RuntimeError("legacy parent manifest must be an object")
    run_id = str(manifest.get("run_id", "") or "").strip()
    config_sha256 = str(manifest.get("config_sha256", manifest.get("run_config_sha256", "")) or "").strip()
    if not run_id or not config_sha256 or len(config_sha256) != 64:
        raise RuntimeError("legacy parent manifest must contain run_id and config_sha256")
    ordered_ids = _manifest_ids(manifest)
    sidecar = _ensure_input_sidecar(
        manifest_path=manifest_path, checkpoint_path=checkpoint_path, evidence_path=evidence_path, manifest=manifest,
        sidecar_path=sidecar_path or target.with_name(target.name + "_input_manifest.json"),
    )
    _verify_input_hashes(
        manifest_path=manifest_path, checkpoint_path=checkpoint_path, evidence_path=evidence_path, manifest=manifest, sidecar=sidecar,
    )
    checkpoint_rows = _checkpoint_rows(checkpoint_path, run_id)
    checkpoint_ids = [str(row["payload"].get("source_record_id", "") or "").strip() for row in checkpoint_rows]
    if checkpoint_ids != ordered_ids:
        raise RuntimeError("legacy manifest source_record_id order does not match SQLite checkpoint")
    evidence = _read_evidence(evidence_path)
    if set(evidence) - set(ordered_ids):
        raise RuntimeError("legacy evidence contains source_record_id absent from parent manifest")

    evidence_records: list[dict[str, Any]] = []
    decision_records: list[dict[str, Any]] = []
    result_records: list[dict[str, Any]] = []
    for checkpoint_row in checkpoint_rows:
        payload = dict(checkpoint_row["payload"])
        source_id = str(payload["source_record_id"])
        evidence_row = evidence.get(source_id)
        combined = dict(payload)
        evaluation = payload.get("__evaluation") if isinstance(payload.get("__evaluation"), dict) else None
        if evidence_row is not None:
            evidence_evaluation = evidence_row.get("evaluation")
            if isinstance(evidence_evaluation, dict):
                combined["__evaluation"] = {**(evaluation or {}), **evidence_evaluation}
                evaluation = combined["__evaluation"]
            combined["source_evidence"] = evidence_row.get("source_evidence", evidence_row.get("field_evidence", []))
        # Legacy flat publication fields are not decision inputs.  Clear them
        # before projecting the newly verified envelope so stale quarantine
        # values cannot be mistaken for a frozen policy result.
        for field, empty in {
            "publication_eligible": None,
            "publication_blockers": "",
            "publication_decision_sha256": "",
            "decision_input_sha256": "",
            "evaluation_sha256": "",
            "config_sha256": "",
            "allowed_contact_fields": [],
        }.items():
            if field in combined:
                combined[field] = empty
        existing = payload.get("publication_decision")
        if isinstance(existing, dict):
            decision = publication_policy.verify_publication_decision(
                existing, source_record_id=source_id, run_id=run_id, config_sha256=config_sha256,
            )
        elif isinstance(evaluation, dict) and evaluation:
            decision = publication_policy.freeze_publication_decision(combined, evaluation, config_sha256=config_sha256)
        else:
            decision = _legacy_review_decision(combined, config_sha256=config_sha256, run_id=run_id)
        combined["publication_decision"] = decision
        combined["publication_decision_sha256"] = decision["publication_decision_sha256"]
        publication_policy.apply_frozen_decision_fields(combined, decision)
        evidence_records.append({
            "run_id": run_id, "item_index": checkpoint_row["item_index"], "source_record_id": source_id,
            "source_evidence": combined.get("source_evidence", []), "evaluation": combined.get("__evaluation", {}),
            "publication_decision": decision, "publication_decision_sha256": decision["publication_decision_sha256"],
        })
        decision_records.append({
            "run_id": run_id, "item_index": checkpoint_row["item_index"], "source_record_id": source_id,
            "publication_decision": decision, "publication_decision_sha256": decision["publication_decision_sha256"],
        })
        result_records.append({
            "run_id": run_id,
            "item_index": checkpoint_row["item_index"],
            "source_record_id": source_id,
            **redaction.sanitize(combined),
        })

    target.mkdir(parents=True, exist_ok=False)
    evidence_out = target / "evidence.jsonl"
    decisions_out = target / "publication_decisions.jsonl"
    results_out = target / "results.jsonl"
    evidence_out.write_text(
        "".join(json.dumps(redaction.sanitize(row), ensure_ascii=False, separators=(",", ":")) + "\n" for row in evidence_records),
        encoding="utf-8",
    )
    decisions_out.write_text(
        "".join(json.dumps(redaction.sanitize(row), ensure_ascii=False, separators=(",", ":")) + "\n" for row in decision_records),
        encoding="utf-8",
    )
    results_out.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in result_records),
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "source_record_count": len(ordered_ids),
        "review_count": sum(not row["publication_decision"]["publishable"] for row in decision_records),
        "legacy_evidence_unjoinable_count": sum("legacy_evidence_unjoinable" in row["publication_decision"]["blockers"] for row in decision_records),
        "source_record_ids_sha256": hashlib.sha256(_canonical(ordered_ids).encode("utf-8")).hexdigest(),
    }
    report_path = target / "materialization_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = {
        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in (evidence_out, decisions_out, results_out, report_path)
    }
    output_manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "source_record_ids": ordered_ids,
        "source_record_ids_sha256": report["source_record_ids_sha256"],
        "parent_manifest_sha256": _sha256(manifest_path),
        "checkpoint_sha256": _sha256(checkpoint_path),
        "evidence_input_sha256": _sha256(evidence_path) if evidence_path is not None else None,
        "files": files,
    }
    output_manifest_path = target / "materialization_manifest.json"
    output_manifest_path.write_text(json.dumps(output_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"count": len(decision_records), "destination": str(target), "manifest": str(output_manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--destination", "--output", dest="destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(materialize(
        checkpoint_path=args.checkpoint,
        manifest_path=args.manifest,
        evidence_path=args.evidence,
        destination=args.destination,
    ), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
