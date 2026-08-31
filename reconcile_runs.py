"""Offline, source-ID-only reconciliation of a baseline and remediation run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from modules import excel, output_artifacts, publication_policy, redaction, run_context


FAILURE_STATUSES = frozenset({
    "PROCESSING_FAILED", "SEARCH_FAILED", "WEBSITE_FETCH_FAILED",
    "WEBSITE_NOT_FOUND", "UNKNOWN", "FAILED", "BLOCKED_BUDGET",
})
CONFLICT_MARKERS = ("collision", "conflict", "wrong_owner", "different_owner", "homonym")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _rows(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.suffix.casefold() == ".xlsx":
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            values = [list(row) for row in workbook.active.iter_rows(values_only=True)]
        finally:
            workbook.close()
        if not values:
            return []
        headers = [str(value or "").strip() for value in values[0]]
        return [
            {headers[index]: row[index] if index < len(row) else "" for index in range(len(headers))}
            for row in values[1:]
            if any(value not in (None, "") for value in row)
        ]
    if path.suffix.casefold() == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else payload.get("records", payload.get("rows", []))


def _by_source(path: Path | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if path is None:
        return result
    for row in _rows(Path(path)):
        source_id = str(row.get("source_record_id", "") or "").strip()
        if not source_id:
            raise ValueError(f"result row has no source_record_id: {path}")
        if source_id in result:
            raise ValueError(f"duplicate source_record_id in {path}: {source_id}")
        result[source_id] = dict(row)
    return result


def _decision(row: dict[str, Any]) -> dict[str, Any]:
    decision = publication_policy.decide_row(row)
    row["publication_eligible"] = bool(decision["publishable"])
    row["publication_advisory_eligible"] = bool(decision.get("advisory_eligible", False))
    row["website_identity_verified"] = bool(decision.get("website_identity_verified", False))
    row["allowed_contact_fields"] = "; ".join(decision.get("allowed_contact_fields", []))
    blockers = [
        value.strip()
        for value in str(row.get("publication_blockers", "")).replace(",", ";").split(";")
        if value.strip()
    ]
    blockers.extend(str(value) for value in decision.get("blockers", []) if str(value).strip())
    row["publication_blockers"] = "; ".join(dict.fromkeys(blockers))
    row["publication_policy_version"] = decision["policy_version"]
    return decision


def _is_failure(row: dict[str, Any]) -> bool:
    status = str(row.get("status", "")).strip().upper()
    reason = str(row.get("reason", "")).casefold()
    return status in FAILURE_STATUSES or any(marker in reason for marker in ("processing_failed", "provider_failed"))


def _is_conflict(row: dict[str, Any]) -> bool:
    text = " ".join(str(row.get(key, "") or "") for key in ("publication_blockers", "collision_reason", "reason")).casefold()
    return any(marker in text for marker in CONFLICT_MARKERS)


def _contact_count(row: dict[str, Any]) -> int:
    return sum(bool(str(row.get(field, "") or "").strip()) for field in ("email", "phone"))


def _choose(source_id: str, baseline: dict[str, Any] | None, child: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    if baseline is None and child is None:
        return {"source_record_id": source_id, "status": "REVIEW_NEEDED", "reason": "missing_result"}, "missing_result"
    if baseline is None:
        return dict(child), "child_only"
    if child is None or _is_failure(child):
        return dict(baseline), "baseline_retained_after_failed_child"
    if _is_conflict(child):
        return dict(child), "child_conflict_blocks_baseline"
    if child.get("publication_eligible") is True and baseline.get("publication_eligible") is not True:
        return dict(child), "child_verified_result"
    if baseline.get("publication_eligible") is True and child.get("publication_eligible") is not True:
        return dict(baseline), "baseline_retained_against_nonverified_child"
    child_score = (_contact_count(child), int(child.get("score", 0) or 0), bool(child.get("website")))
    base_score = (_contact_count(baseline), int(baseline.get("score", 0) or 0), bool(baseline.get("website")))
    return (dict(child), "child_improves_evidence") if child_score > base_score else (dict(baseline), "baseline_retained")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(redaction.sanitize(payload), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def reconcile_runs(
    *,
    input_path: Path,
    destination: Path,
    baseline_results: Path | None = None,
    child_results: Path | None = None,
) -> dict[str, Any]:
    input_path, destination = Path(input_path).resolve(), Path(destination).resolve()
    source_records = excel.read_company_records(input_path)
    source_ids: list[str] = []
    for index, record in enumerate(source_records):
        source_id = str(record.get("source_record_id", "") or "").strip()
        if not source_id:
            source_id = run_context.source_record_identity(record)[0]
        source_ids.append(source_id)
        record["source_record_id"] = source_id
        record["original_index"] = index
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("input source_record_id values are not unique")
    baseline = _by_source(baseline_results)
    child = _by_source(child_results)
    unknown_baseline = set(baseline) - set(source_ids)
    unknown_child = set(child) - set(source_ids)
    if unknown_baseline or unknown_child:
        raise ValueError("result contains source_record_id absent from original input")
    reconciliation_run_id = hashlib.sha256(_canonical({
        "input_sha256": _sha256(input_path), "baseline_sha256": _sha256(baseline_results) if baseline_results else None,
        "child_sha256": _sha256(child_results) if child_results else None, "source_ids": source_ids,
    }).encode("utf-8")).hexdigest()
    final_rows: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    for index, record in enumerate(source_records):
        source_id = source_ids[index]
        selected, selection_reason = _choose(source_id, baseline.get(source_id), child.get(source_id))
        selected["company"] = record.get("company", selected.get("company", ""))
        selected["source_record_id"] = source_id
        selected["original_index"] = index
        selected["run_id"] = reconciliation_run_id
        selected.setdefault("free_state", "DONE")
        selected.setdefault("paid_required", False)
        selected.setdefault("paid_state", "NOT_REQUIRED")
        selected.setdefault("status", "REVIEW_NEEDED")
        decision = _decision(selected)
        if _is_conflict(child.get(source_id, {})):
            selected["publication_blockers"] = "; ".join(dict.fromkeys(filter(None, [selected.get("publication_blockers", ""), "reconciliation_new_conflict"])))
            output_artifacts.suppress_all_contacts(selected, "reconciliation_new_conflict")
            _decision(selected)
            selected["publication_eligible"] = False
            selected["delivery_state"] = "REVIEW"
        else:
            selected["delivery_state"] = "DELIVERABLE" if selected.get("publication_eligible") is True else "REVIEW"
        final_rows.append(selected)
        before = baseline.get(source_id, {})
        after_decision = {
            "publishable": bool(selected.get("publication_eligible", False)),
            "blockers": selected.get("publication_blockers", ""),
        }
        if _canonical({"before": before.get("publication_eligible"), "after": after_decision, "reason": selection_reason}) != _canonical({"before": after_decision, "after": after_decision, "reason": "baseline_retained"}):
            lineage.append({
                "source_record_id": source_id,
                "original_index": index,
                "selection": selection_reason,
                "before": {"publication_eligible": before.get("publication_eligible"), "website": before.get("website", ""), "email": bool(before.get("email")), "phone": bool(before.get("phone"))},
                "after": after_decision,
                "child_evidence": child.get(source_id, {}).get("source_evidence", child.get(source_id, {}).get("__evaluation", {})),
            })
    destination.mkdir(parents=True, exist_ok=False)
    excel.write_contacts(destination / "all_results.xlsx", final_rows)
    contacts = [row for row in final_rows if row.get("publication_eligible") is True]
    review = [row for row in final_rows if row.get("publication_eligible") is not True]
    excel.write_contacts(destination / "contacts.xlsx", contacts)
    excel.write_contacts(destination / "review_queue.xlsx", review)
    from modules import evidence
    evidence.write_jsonl(destination / "evidence.jsonl", final_rows)
    report = {
        "schema_version": 1,
        "status": "offline_reconciliation",
        "run_id": reconciliation_run_id,
        "input_count": len(final_rows), "published_count": len(contacts), "review_count": len(review),
        "provider_calls_new": 0, "physical_http_requests_new": 0,
        "elapsed_seconds": None, "cost": None,
        "unknown_historical_cost": True,
        "lineage_change_count": len(lineage),
    }
    _write_json(destination / "reconciliation_report.json", report)
    _write_json(destination / "reconciliation_lineage.json", lineage)
    files = {
        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in sorted(destination.iterdir()) if path.is_file()
    }
    manifest = {
        "schema_version": 1, "run_id": reconciliation_run_id,
        "coverage_complete": len(final_rows) == len(source_ids),
        "source_record_ids": source_ids,
        "counts": {"all_results": len(final_rows), "contacts": len(contacts), "review": len(review)},
        "parent_results": {"path": str(baseline_results) if baseline_results else None, "sha256": _sha256(baseline_results) if baseline_results else None},
        "child_results": {"path": str(child_results) if child_results else None, "sha256": _sha256(child_results) if child_results else None},
        "new_provider_calls": 0, "new_physical_http_requests": 0,
        "files": files,
    }
    _write_json(destination / "delivery_manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", "--original-input", dest="input_path", type=Path, required=True)
    parser.add_argument("--baseline", "--baseline-results", dest="baseline_results", type=Path)
    parser.add_argument("--child", "--child-results", dest="child_results", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(reconcile_runs(**vars(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
