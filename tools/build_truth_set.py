"""Build a reference-labelled truth set from a completed run, offline."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import calibration, reference_inputs, scorer


def _read_run(run_dir: Path) -> list[dict]:
    database = run_dir / "state" / "progress.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"run database not found: {database}")
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT i.item_index,i.source_record_id,r.payload,s.snapshot_json "
            "FROM run_items i "
            "LEFT JOIN results r ON r.run_id=i.run_id AND r.item_index=i.item_index "
            "LEFT JOIN immutable_input_snapshots s ON s.run_id=i.run_id AND s.item_index=i.item_index "
            "WHERE i.run_id=? ORDER BY i.item_index",
            (run_dir.name,),
        ).fetchall()
        records: dict[str, dict] = {}
        for row in rows:
            source_record_id = str(row["source_record_id"] or "")
            payload = json.loads(row["payload"] or "{}")
            snapshot = json.loads(row["snapshot_json"] or "{}")
            reference_url = reference_inputs.reference_website(snapshot)
            truth_domain = scorer.registrable_domain(reference_url)
            truth_tier = str(payload.get("reference_tier") or "")
            labelled = truth_tier in {"REFERENCE_VERIFIED", "REFERENCE_MATCHES_OWN_SEARCH"} and bool(truth_domain)
            split_value = int(hashlib.sha256(source_record_id.encode("utf-8")).hexdigest()[:8], 16) % 2
            records[source_record_id] = {
                "source_record_id": source_record_id,
                "company": str(payload.get("company") or snapshot.get("company") or ""),
                "truth_domain": truth_domain,
                "truth_tier": truth_tier,
                "labelled": bool(labelled),
                "split": "cal" if split_value == 0 else "hold",
                "raw_results": [],
                "stage_a": payload.get("stage_a") if isinstance(payload.get("stage_a"), dict) else {},
                "stage_a_candidates": (
                    payload.get("stage_a_candidates")
                    if isinstance(payload.get("stage_a_candidates"), list) else []
                ),
            }

        replay_rows = connection.execute(
            "SELECT value_json FROM replay_entries "
            "WHERE run_id=? AND namespace='free_search_execution_v2' ORDER BY rowid",
            (run_dir.name,),
        ).fetchall()
        for replay_row in replay_rows:
            entry = json.loads(replay_row["value_json"])
            source_record_id = str(entry.get("source_record_id") or "")
            record = records.get(source_record_id)
            if record is None:
                continue
            completed = [
                attempt for attempt in (entry.get("backend_attempts") or [])
                if attempt.get("result_state") == "COMPLETED"
            ]
            backend = completed[-1].get("backend") if completed else None
            results = ((entry.get("result") or {}).get("values") or [])
            for rank, result in enumerate(results, start=1):
                url = str(result.get("href") or result.get("url") or "")
                record["raw_results"].append({
                    "query_fingerprint": str(entry.get("query_fingerprint") or ""),
                    "backend": backend,
                    "rank": rank,
                    "domain": scorer.registrable_domain(url),
                    "url": url,
                    "title": str(result.get("title") or ""),
                })
        return list(records.values())
    finally:
        connection.close()


def _multi_firm_domains(records: list[dict]) -> list[str]:
    company_ids: dict[str, set[str]] = {}
    labelled_truth = [record["truth_domain"] for record in records if record["labelled"]]
    for record in records:
        source_record_id = record["source_record_id"]
        for result in record["raw_results"]:
            domain = str(result.get("domain") or "")
            if domain:
                company_ids.setdefault(domain, set()).add(source_record_id)
    return sorted(
        domain for domain, firms in company_ids.items()
        if len(firms) >= 3
        and not scorer.is_excluded_domain(domain)
        and not any(calibration.brand_match(domain, truth) for truth in labelled_truth)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    records = _read_run(Path(args.run_dir).resolve())
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    domains = _multi_firm_domains(records)
    (output.parent / "multi_firm_domains.txt").write_text(
        "".join(f"{domain}\n" for domain in domains), encoding="utf-8", newline="\n",
    )
    print(json.dumps({
        "firms": len(records),
        "labelled": sum(bool(record["labelled"]) for record in records),
        "cal": sum(record["labelled"] and record["split"] == "cal" for record in records),
        "hold": sum(record["labelled"] and record["split"] == "hold" for record in records),
        "multi_firm_domains": len(domains),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
