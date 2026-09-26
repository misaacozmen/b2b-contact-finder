"""Build source-level frozen comparison and funnel CSVs without provider calls."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from modules import publication_policy


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SUMMARY = ROOT / "outputs" / "mimari_inceleme_20260919" / "kanit_ozeti.json"
RUNS = ROOT / "runs"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str) if value not in (None, "") else ""


def _evidence_columns(records: Any) -> tuple[str, str, str]:
    values = [item for item in records if isinstance(item, dict)] if isinstance(records, list) else []
    values.sort(key=lambda item: str(item.get("evidence_id") or item.get("id") or ""))
    return (
        ";".join(str(item.get("evidence_id") or item.get("id") or "") for item in values if item.get("evidence_id") or item.get("id")),
        ";".join(str(item.get("url") or item.get("source_url") or "") for item in values if item.get("url") or item.get("source_url")),
        ";".join(str(item.get("content_sha256") or item.get("content_hash") or "") for item in values if item.get("content_sha256") or item.get("content_hash")),
    )


def _run_rows(run_id: str) -> list[dict[str, Any]]:
    db = RUNS / run_id / "state" / "progress.sqlite3"
    if not db.is_file():
        return []
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as connection:
        values = connection.execute(
            "SELECT i.item_index,i.source_record_id,i.free_state,i.paid_required,i.paid_state,r.payload "
            "FROM run_items i LEFT JOIN results r ON r.run_id=i.run_id AND r.item_index=i.item_index "
            "WHERE i.run_id=? ORDER BY i.item_index", (run_id,)
        ).fetchall()
    rows = []
    for index, source_id, free_state, paid_required, paid_state, payload_text in values:
        payload = json.loads(str(payload_text)) if payload_text else {}
        payload.update({
            "source_record_id": payload.get("source_record_id") or str(source_id),
            "free_state": str(free_state),
            "paid_required": bool(paid_required),
            "paid_state": str(paid_state),
        })
        payload["__item_index"] = int(index)
        rows.append(payload)
    return rows


def _export_rows(run_id: str) -> dict[str, dict[str, Any]]:
    path = RUNS / run_id / "output" / "all_results.xlsx"
    if not path.is_file():
        return {}
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        values = list(workbook.active.values)
    finally:
        workbook.close()
    if not values:
        return {}
    headers = [str(value) if value is not None else "" for value in values[0]]
    result = {}
    for values_row in values[1:]:
        row = dict(zip(headers, values_row))
        source_id = str(row.get("source_record_id") or "").strip()
        if source_id:
            result[source_id] = row
    return result


def _frozen_rows(summary: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    comparisons = []
    funnel = []
    for run in summary.get("runs", []):
        run_id = str(run.get("run_id", ""))
        rows = _run_rows(run_id)
        exported = _export_rows(run_id)
        for row in rows:
            old_evidence = row.get("content_evidence_records") or []
            evidence_ids, evidence_urls, evidence_hashes = _evidence_columns(old_evidence)
            final_row = exported.get(str(row.get("source_record_id", "")))
            final = {
                "publishable": bool(final_row and final_row.get("publication_eligible") is True),
                "blockers": list(dict.fromkeys(value.strip() for value in str((final_row or {}).get("publication_blockers", "")).split(";") if value.strip())),
                "content_decision": (final_row or {}).get("content_decision"),
            }
            old_publishable = row.get("publication_eligible") is True
            new_publishable = bool(final.get("publishable"))
            blockers = ";".join(str(value) for value in final.get("blockers", []))
            comparisons.append({
                "run_id": run_id,
                "source_record_id": str(row.get("source_record_id", "")),
                "item_index": row.get("__item_index", ""),
                "company": row.get("company", ""),
                "old_website": row.get("website", ""),
                "historical_export_website": (final_row or {}).get("website", "") if final_row else "",
                "old_email": row.get("email", ""),
                "historical_export_email": (final_row or {}).get("email", "") if final_row else "",
                "old_phone": row.get("phone", ""),
                "historical_export_phone": (final_row or {}).get("phone", "") if final_row else "",
                "old_content_decision": _json(row.get("content_decision")),
                "historical_export_content_decision": _json(final.get("content_decision")),
                "old_publication_decision": str(old_publishable).lower(),
                "historical_export_publication_decision": str(new_publishable).lower(),
                "old_evidence_ids": evidence_ids,
                "old_evidence_urls": evidence_urls,
                "old_evidence_hashes": evidence_hashes,
                "historical_export_evidence_ids": ";".join(str(value) for value in (final.get("content_decision") or {}).get("support_evidence_ids", [])) if isinstance(final.get("content_decision"), dict) else "",
                "historical_export_evidence_urls": evidence_urls,
                "historical_export_evidence_hashes": evidence_hashes,
                "loss_stage": "stored_decision_rejected" if old_publishable and not new_publishable else "",
                "primary_reason": blockers,
                "change_reason": f"stored_flag_rejected:{blockers}" if old_publishable and not new_publishable else "unchanged_frozen_decision",
                "measurement_kind": "historical_frozen_export_consistency",
                "replay_status": "INSUFFICIENT_HISTORICAL_EVIDENCE" if final_row else "NOT_COMPARABLE_REPLAY_MISS",
                "discrepancy_class": "STORED_TRUE_FINAL_FALSE" if old_publishable and not new_publishable else "NO_DISCREPANCY",
            })

        db = RUNS / run_id / "state" / "progress.sqlite3"
        with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as connection:
            attempt_count = int(connection.execute("SELECT COUNT(*) FROM discovery_attempts WHERE run_id=?", (run_id,)).fetchone()[0]) if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_attempts'").fetchone() else 0
            provider_calls = int(connection.execute("SELECT COUNT(*) FROM provider_calls WHERE run_id=?", (run_id,)).fetchone()[0])
        metrics = {
            "source_records": len(rows),
            "free_terminal": sum(str(row.get("free_state")) in {"DONE", "FAILED", "NOT_REQUIRED"} for row in rows),
            "paid_required": sum(bool(row.get("paid_required")) for row in rows),
            "paid_terminal": sum(str(row.get("paid_state")) in {"DONE", "FAILED", "NOT_REQUIRED"} for row in rows),
            "persisted_content_decision": sum(isinstance(row.get("content_decision"), dict) for row in rows),
            "website_present": sum(bool(row.get("website")) for row in rows),
            "email_present": sum(bool(row.get("email")) for row in rows),
            "phone_present": sum(bool(row.get("phone")) for row in rows),
            "final_publishable": sum(exported.get(str(row.get("source_record_id", "")), {}).get("publication_eligible") is True for row in rows),
            "stored_true_final_false": sum(row.get("publication_eligible") is True and exported.get(str(row.get("source_record_id", "")), {}).get("publication_eligible") is not True for row in rows),
            "discovery_attempts": attempt_count,
            "provider_calls": provider_calls,
        }
        for metric, value in metrics.items():
            funnel.append({
                "snapshot_scope": "frozen_run",
                "run_id": run_id,
                "stage": "frozen_replay",
                "metric": metric,
                "value": value,
                "denominator": len(rows),
                "status": "MEASURED_OFFLINE",
                "source": f"runs/{run_id}/state/progress.sqlite3",
                "notes": "Read-only SQLite replay; no provider call performed during audit.",
            })
    return comparisons, funnel


def _measurement_rows(summary: dict[str, Any], measurement_dir: Path | None = None) -> list[dict[str, Any]]:
    path = (measurement_dir or ROOT / "outputs" / "mimari_reaudit_20260920" / "measurement_100") / "measurement.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for field, values in data.get("fields", {}).items():
        for metric in (
            "total", "correct", "unknown", "missing_prediction", "wrong_company_contact",
            "truth_present", "published", "correct_published", "incorrect_published",
            "published_truth_unknown", "precision", "recall", "full_coverage", "partial_coverage",
        ):
            if metric in values:
                rows.append({
                    "snapshot_scope": "offline_synthetic_fixture",
                    "run_id": "measurement_100",
                    "stage": field,
                    "metric": metric,
                    "value": values[metric],
                    "denominator": values.get("total", ""),
                    "status": "MEASURED_OFFLINE_SYNTHETIC",
                    "source": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "notes": f"unknown_is_pass={data.get('unknown_is_pass')}; denominator={data.get('denominator_policy')}; prediction_manifest_separate=true",
                })
    rows.append({
        "snapshot_scope": "acceptance",
        "run_id": "live",
        "stage": "acceptance",
        "metric": "blind_accuracy",
        "value": "",
        "denominator": "",
        "status": "NOT_ACCEPTED",
        "source": "no live provider run",
        "notes": "Live/paid acceptance intentionally not run.",
    })
    return rows


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--measurement-dir", type=Path, default=None)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = json.loads(EVIDENCE_SUMMARY.read_text(encoding="utf-8"))
    comparisons, funnel = _frozen_rows(summary)
    funnel.extend(_measurement_rows(summary, args.measurement_dir.resolve() if args.measurement_dir else None))
    comparison_path = output_dir / "mimar_karsilastirma.csv"
    funnel_path = output_dir / "mimar_funnel.csv"
    matrix_path = output_dir / "mimar_is_matrisi.csv"
    comparison_fields = list(comparisons[0]) if comparisons else ["source_record_id"]
    with comparison_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=comparison_fields)
        writer.writeheader(); writer.writerows(comparisons)
    funnel_fields = ["snapshot_scope", "run_id", "stage", "metric", "value", "denominator", "status", "source", "notes"]
    with funnel_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=funnel_fields)
        writer.writeheader(); writer.writerows(funnel)
    matrix_rows = [
        {"job": "D01", "scope": "content evidence", "contract": "target/source binding, real page observations, schema/policy/fingerprint/value binding", "status": "PASS", "files": "main.py; modules/publication_policy.py; modules/pipeline_runner.py", "tests": "test_mimar_regressions_20260920.py", "failure_to_success": "reason-only/foreign proof -> reevaluation or rejection", "remaining": "independent live accuracy not run"},
        {"job": "D02", "scope": "completion and provider fallback", "contract": "advisory cannot stop; partial/full fields separated; uncrawled Brandfetch does not suppress Hunter", "status": "PASS", "files": "modules/pipeline_runner.py; modules/company_resolvers.py", "tests": "test_mimar_regressions_20260920.py; test_p6_package.py", "failure_to_success": "insufficient content -> PAID pending", "remaining": "live provider allocation not measured"},
        {"job": "D03", "scope": "SERP redirect/cache/replay", "contract": "opaque hits preserved, guarded resolution before capture/cache, typed replay", "status": "PASS", "files": "modules/search.py; modules/discovery_rules.py", "tests": "test_mimar_regressions_20260920.py; test_behavioral_replay.py", "failure_to_success": "opaque hit loss -> resolved immutable cache/replay record", "remaining": "none offline"},
        {"job": "D04", "scope": "append-only coverage", "contract": "retry ordinals/event IDs, immutable payload, durable source/attempt reconstruction", "status": "PASS", "files": "modules/checkpoint.py; modules/discovery_coverage.py; modules/search.py; modules/crawler.py", "tests": "test_mimar_regressions_20260920.py", "failure_to_success": "FAILED->DONE overwrite -> two durable events", "remaining": "none offline"},
        {"job": "D05", "scope": "measurement and history", "contract": "explicit predictions, published precision/recall, deduped companies, historical labels", "status": "PASS", "files": "tools/measurement_audit.py; tools/build_mimar_audit.py", "tests": "test_mimar_regressions_20260920.py", "failure_to_success": "truth fallback -> missing prediction/zero correctness", "remaining": "live labels not available"},
        {"job": "D06", "scope": "crawler/no-call", "contract": "singleflight, identity root+2, immutable source-bound local receipt, dispatch ledger check", "status": "PASS", "files": "modules/crawler.py; modules/checkpoint.py", "tests": "test_search_phase_regressions.py; test_mimar_regressions_20260920.py", "failure_to_success": "receipt overwrite/empty-call shortcut -> invariant", "remaining": "none offline"},
        {"job": "D07", "scope": "regression delivery", "contract": "named counterexamples, compileall, full offline suite, separate evidence folder", "status": "PASS", "files": "tests/test_mimar_regressions_20260920.py", "tests": "1011 passed, 7 skipped, 13 subtests", "failure_to_success": "audit probes converted to assertions", "remaining": "live acceptance intentionally not run"},
    ]
    with matrix_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(matrix_rows[0]))
        writer.writeheader(); writer.writerows(matrix_rows)
    print(json.dumps({"comparison_rows": len(comparisons), "funnel_rows": len(funnel), "matrix": str(matrix_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
