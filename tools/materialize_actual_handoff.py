"""Materialize a read-only paid-disabled handoff checkpoint for benchmark inspection.

This is deliberately not a production finalizer: no publication decision is
computed or changed here.  The handoff checkpoint remains the source of truth
and every payload is copied only after its durable lineage is verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import excel


PAID_PROVIDER_BUDGETS = {
    "brightdata": 0,
    "google_places": 0,
    "brandfetch": 0,
    "hunter": 0,
    "linkedin": 0,
    "llm": 0,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("run manifest must be an object")
    return payload


def materialize(run_dir: Path, destination: Path, *, expected_sha256: str, runtime_sha256: str) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    manifest_path = run_dir / "manifest.json"
    manifest = _read_manifest(manifest_path)
    run_id = str(manifest.get("run_id") or "")
    artifact_set = str(manifest.get("artifact_set_sha256") or "")
    checkpoint_sha256 = str(manifest.get("checkpoint_sha256") or "")
    if not run_id or not artifact_set or not checkpoint_sha256 or manifest.get("handoff") is not True:
        raise RuntimeError("actual materialization requires an immutable handoff manifest")
    checkpoint = run_dir / "output" / "artifacts" / artifact_set / "recovery_state.sqlite3"
    if not checkpoint.exists() or _sha256(checkpoint) != checkpoint_sha256:
        raise RuntimeError("handoff checkpoint hash mismatch or missing checkpoint")
    declared = (manifest.get("files") or {}).get("recovery_state.sqlite3") or {}
    if declared.get("sha256") != checkpoint_sha256 or int(declared.get("bytes", -1)) != checkpoint.stat().st_size:
        raise RuntimeError("handoff manifest file declaration mismatch")
    if destination.exists():
        raise RuntimeError(f"destination already exists: {destination}")

    uri = f"file:{checkpoint.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("handoff checkpoint integrity_check failed")
        item_rows = connection.execute(
            "SELECT run_id, item_index, source_record_id, payload_sha256 "
            "FROM run_items WHERE run_id = ? ORDER BY item_index",
            (run_id,),
        ).fetchall()
        result_rows = connection.execute(
            "SELECT run_id, item_index, payload FROM results WHERE run_id = ? ORDER BY item_index",
            (run_id,),
        ).fetchall()
        if len(item_rows) != len(result_rows) or len(item_rows) != int(manifest.get("input_count", -1)):
            raise RuntimeError("handoff checkpoint coverage is not exact")
        expected_ids = list(manifest.get("ordered_source_record_ids") or [])
        if [row[2] for row in item_rows] != expected_ids:
            raise RuntimeError("handoff source ID order mismatch")
        rows: list[dict[str, Any]] = []
        raw_records: list[dict[str, Any]] = []
        for item, result in zip(item_rows, result_rows):
            if item[0] != result[0] or item[1] != result[1] or not item[2]:
                raise RuntimeError("handoff run/item/result identity mismatch")
            payload_text = str(result[2])
            if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != item[3]:
                raise RuntimeError(f"handoff payload hash mismatch at item_index={item[1]}")
            payload = json.loads(payload_text)
            if not isinstance(payload, dict) or payload.get("source_record_id") != item[2]:
                raise RuntimeError(f"handoff payload source ID mismatch at item_index={item[1]}")
            rows.append(payload)
            raw_records.append({
                "run_id": run_id,
                "item_index": item[1],
                "source_record_id": item[2],
                "payload_sha256": item[3],
                "payload": payload,
            })
        provider_calls = int(connection.execute("SELECT count(*) FROM provider_calls WHERE run_id = ?", (run_id,)).fetchone()[0])
        runtime_row = connection.execute("SELECT runtime_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        runtime = json.loads(runtime_row[0]) if runtime_row and runtime_row[0] else {}
        counters = runtime.get("counters") if isinstance(runtime, dict) else {}
        physical_http_requests = int((counters or {}).get("http.crawler.requests", 0) or 0)
        elapsed_seconds = runtime.get("elapsed_seconds") if isinstance(runtime, dict) else None
    finally:
        connection.close()

    destination.mkdir(parents=True)
    excel.write_contacts(destination / "all_results.xlsx", rows)
    (destination / "checkpoint_results.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in raw_records),
        encoding="utf-8",
    )
    output_manifest = {
        "schema_version": 1,
        "status": "handoff_checkpoint_materialized",
        "finalized": False,
        "run_id": run_id,
        "source_record_ids": expected_ids,
        "source_record_ids_sha256": _canonical_hash(expected_ids),
        "expected_sha256": expected_sha256,
        "config_sha256": manifest.get("config_sha256"),
        "runtime_source_tree_sha256": runtime_sha256,
        "checkpoint_sha256": checkpoint_sha256,
        "artifact_set_sha256": artifact_set,
        "provider_calls": provider_calls,
        "physical_http_requests": physical_http_requests,
        "elapsed_seconds": elapsed_seconds,
        "cost": 0.0,
        "paid_enabled": False,
        "paid_provider_budgets": PAID_PROVIDER_BUDGETS,
        "files": {
            "all_results.xlsx": {
                "sha256": _sha256(destination / "all_results.xlsx"),
                "bytes": (destination / "all_results.xlsx").stat().st_size,
            },
            "checkpoint_results.jsonl": {
                "sha256": _sha256(destination / "checkpoint_results.jsonl"),
                "bytes": (destination / "checkpoint_results.jsonl").stat().st_size,
            },
        },
        "source_checkpoint": str(checkpoint),
        "note": "The paid-disabled run stopped at handoff; no final publication decision was computed here.",
    }
    (destination / "actual_manifest.json").write_text(
        json.dumps(output_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--runtime-sha256", required=True)
    args = parser.parse_args()
    result = materialize(
        args.run_dir,
        args.destination,
        expected_sha256=args.expected_sha256,
        runtime_sha256=args.runtime_sha256,
    )
    print(json.dumps({"destination": str(args.destination), "source_record_count": len(result["source_record_ids"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
