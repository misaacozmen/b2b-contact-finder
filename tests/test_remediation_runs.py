from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook, load_workbook

from prepare_remediation_run import prepare_remediation_run
from reconcile_runs import reconcile_runs


def _input(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["company", "source_record_id", "sector", "listed_legal_name"])
    sheet.append(["Alpha", "src:a", "textile", "Alpha Tekstil"])
    sheet.append(["Beta", "src:b", "textile", "Beta Tekstil"])
    sheet.append(["Gamma", "src:c", "home", "Gamma Ev"])
    workbook.save(path)
    workbook.close()


def test_prepare_remediation_is_source_only_and_covers_review_ids(tmp_path: Path):
    input_path = tmp_path / "input.xlsx"
    baseline = tmp_path / "baseline.json"
    _input(input_path)
    baseline.write_text(json.dumps([
        {"source_record_id": "src:a", "publication_eligible": True},
        {"source_record_id": "src:b", "publication_eligible": False, "publication_blockers": "missing_contact"},
        {"source_record_id": "src:c", "publication_eligible": False, "reason": "identity_conflict"},
    ]), encoding="utf-8")
    result = prepare_remediation_run(
        input_path=input_path, baseline_results=baseline, destination=tmp_path / "child",
    )
    assert result["task_count"] == 2
    plan = json.loads((tmp_path / "child" / "remediation_plan.json").read_text(encoding="utf-8"))
    assert {task["source_record_id"] for task in plan["tasks"]} == {"src:b", "src:c"}
    workbook = load_workbook(tmp_path / "child" / "remediation_input.xlsx", read_only=True, data_only=True)
    try:
        headers = [cell.value for cell in next(workbook.active.iter_rows(max_row=1))]
        assert "email" not in {str(value).casefold() for value in headers}
        assert workbook.active.max_row == 3
    finally:
        workbook.close()


def test_reconcile_retains_failed_child_and_blocks_new_conflict(tmp_path: Path):
    input_path = tmp_path / "input.xlsx"
    baseline = tmp_path / "baseline.json"
    child = tmp_path / "child.json"
    _input(input_path)
    baseline.write_text(json.dumps([
        {"source_record_id": "src:a", "company": "Alpha", "status": "OK_HIGH_CONFIDENCE", "publication_eligible": True, "website": "https://alpha.example"},
        {"source_record_id": "src:b", "company": "Beta", "status": "REVIEW_NEEDED", "publication_eligible": False},
        {"source_record_id": "src:c", "company": "Gamma", "status": "REVIEW_NEEDED", "publication_eligible": False},
    ]), encoding="utf-8")
    child.write_text(json.dumps([
        {"source_record_id": "src:a", "company": "Alpha", "status": "PROCESSING_FAILED", "publication_eligible": False},
        {"source_record_id": "src:c", "company": "Gamma", "status": "OK_HIGH_CONFIDENCE", "publication_eligible": True, "website": "https://other-owner.example", "reason": "identity_conflict"},
    ]), encoding="utf-8")
    manifest = reconcile_runs(
        input_path=input_path, baseline_results=baseline, child_results=child,
        destination=tmp_path / "delivery",
    )
    assert manifest["counts"] == {"all_results": 3, "contacts": 1, "review": 2}
    report = json.loads((tmp_path / "delivery" / "reconciliation_report.json").read_text(encoding="utf-8"))
    assert report["provider_calls_new"] == 0
    evidence = json.loads((tmp_path / "delivery" / "evidence.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert evidence["source_record_id"] == "src:a"
    assert "publication_decision" in evidence
