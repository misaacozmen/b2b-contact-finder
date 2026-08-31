import json
import os
import sqlite3
import time
from pathlib import Path
import subprocess
import sys
from collections import Counter

import pytest
from openpyxl import load_workbook

import reconcile_recovery_output as reconcile_module
from modules import excel


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "input" / "firms.xlsx"
V2_DESTINATION = ROOT / "output" / "exhibition_893_reconciled_v2"
DESTINATION = None
PLAN = ROOT / "input" / "remaining_159_plan.json"
MAPPING = ROOT.parent / "Python b2b_recovery_final10_r2" / "runs" / "5ab042f129d9d31108cccc8cecb8549c32f0965c4de418283256284b44f2a36b" / "output" / "artifacts" / "22a2993366d4bbbdebdf5d9ee805b59bc8949506f0f8b2d4edefdc964b4df008" / "source_id_mapping.json"


pytestmark = pytest.mark.skipif(
    os.environ.get("B2B_RUN_INCIDENT_RECONCILIATION") != "1",
    reason="private incident integration requires B2B_RUN_INCIDENT_RECONCILIATION=1",
)


def _require_incident_sources():
    required = (
        ORIGINAL,
        PLAN,
        PLAN.parent / "remaining_159_fresh.xlsx",
        MAPPING,
        MAPPING.parent / "forensic_legacy_payloads.jsonl",
        MAPPING.parent / "recovery_state.sqlite3",
        MAPPING.parents[3] / "manifest.json",
        ROOT.parent / "Python b2b_incident_20260827_893" / "state" / "progress.sqlite3",
        V2_DESTINATION / "contacts.xlsx",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.fail("incident integration opted in but required sources are missing: " + ", ".join(missing))
    run_id = json.loads(PLAN.read_text(encoding="utf-8")).get("expected_run_id", "")
    if not isinstance(run_id, str) or len(run_id) != 64 or any(char not in "0123456789abcdef" for char in run_id):
        pytest.fail("incident integration plan has an invalid expected_run_id")
    for path in (ROOT / "runs" / run_id / "manifest.json", ROOT / "runs" / run_id / "state" / "progress.sqlite3"):
        if not path.is_file():
            pytest.fail(f"incident integration opted in but a remaining-run source is missing: {path}")


@pytest.fixture(scope="module", autouse=True)
def reconciled_output(tmp_path_factory):
    global DESTINATION
    _require_incident_sources()
    os.environ["MAX_WORKERS"] = "12"
    os.environ["REQUEST_TIMEOUT_SEC"] = "2"
    DESTINATION = tmp_path_factory.getbasetemp() / "reconciled_output"
    timing_log = Path(os.environ.get("B2B_TEST_TIMING_LOG", str(DESTINATION.parent / "reconciliation_timing.log")))
    stdout_log = DESTINATION.parent / "reconciliation.stdout.log"
    stderr_log = DESTINATION.parent / "reconciliation.stderr.log"
    timing_log.parent.mkdir(parents=True, exist_ok=True)
    with timing_log.open("a", encoding="utf-8") as handle:
        handle.write(f"reconciliation.start={time.perf_counter():.6f}\n")
    environment = os.environ.copy()
    environment["MAX_WORKERS"] = "12"
    environment["REQUEST_TIMEOUT_SEC"] = "2"
    started = time.perf_counter()
    try:
        with stdout_log.open("w", encoding="utf-8") as stdout, stderr_log.open("w", encoding="utf-8") as stderr:
            subprocess.run(
                [sys.executable, str(ROOT / "reconcile_recovery_output.py"),
                 "--original-input", str(ORIGINAL),
                 "--legacy-db", str(ROOT.parent / "Python b2b_incident_20260827_893" / "state" / "progress.sqlite3"),
                 "--recovery-run", str(ROOT.parent / "Python b2b_recovery_final10_r2" / "runs" / "5ab042f129d9d31108cccc8cecb8549c32f0965c4de418283256284b44f2a36b"),
                 "--remaining-plan", str(PLAN),
                 "--remaining-run", str(ROOT / "runs" / json.loads(PLAN.read_text(encoding="utf-8"))["expected_run_id"]),
                 "--destination", str(DESTINATION)],
                cwd=ROOT, env=environment, check=True, timeout=450,
                stdout=stdout, stderr=stderr,
            )
    finally:
        with timing_log.open("a", encoding="utf-8") as handle:
            handle.write(f"reconciliation.end={time.perf_counter():.6f};elapsed={time.perf_counter() - started:.3f}\n")
    return DESTINATION


def _rows(path: Path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        values = [list(row) for row in workbook.active.iter_rows(values_only=True)]
    finally:
        workbook.close()
    return values[0], values[1:]


def _manifest():
    if not DESTINATION.exists():
        pytest.fail("reconciliation output has not been produced")
    return json.loads((DESTINATION / "delivery_manifest.json").read_text(encoding="utf-8"))


def test_reconciled_output_has_exact_source_coverage_and_order():
    _manifest()
    headers, rows = _rows(DESTINATION / "all_results.xlsx")
    assert len(rows) == 893
    source_index = headers.index("source_record_id")
    actual_ids = [str(row[source_index]) for row in rows]
    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    expected_ids = [str(row["canonical_source_record_id"]) for row in sorted(mapping["mappings"], key=lambda row: int(row["legacy_index"]))]
    assert len(set(actual_ids)) == 893
    assert actual_ids == expected_ids


def test_reconciled_selection_and_delivery_partition_are_exact():
    manifest = _manifest()
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    assert len(plan["selection"]["ordered_index_id"]) == 159
    headers, all_rows = _rows(DESTINATION / "all_results.xlsx")
    ids = [str(row[headers.index("source_record_id")]) for row in all_rows]
    assert set(item["source_record_id"] for item in plan["selection"]["ordered_index_id"]).issubset(set(ids))
    _, contacts = _rows(DESTINATION / "contacts.xlsx")
    _, verified = _rows(DESTINATION / "verified_contacts.xlsx")
    _, review = _rows(DESTINATION / "review_queue.xlsx")
    _, failed = _rows(DESTINATION / "failed.xlsx")
    assert len(all_rows) == 893
    assert len(contacts) == 416
    assert len(verified) == 416
    assert len(review) == 477
    assert len(failed) == 477
    assert sum(bool(row[headers.index("website")]) for row in all_rows) == 632
    assert sum(bool(row[headers.index("email")]) for row in all_rows) == 553
    assert sum(bool(row[headers.index("phone")]) for row in all_rows) == 586
    assert sum(all(row[headers.index(field)] for field in ("website", "email", "phone")) for row in all_rows) == 534
    assert sum(not any(row[headers.index(field)] for field in ("website", "email", "phone")) for row in all_rows) == 261
    assert Counter(row[headers.index("contact_status")] for row in all_rows) == Counter({"complete": 534, "partial": 71, "missing": 288})
    assert Counter(row[headers.index("delivery_state")] for row in all_rows) == Counter({"DELIVERABLE": 416, "REVIEW": 334, "REMEDIATION_PENDING": 143})
    assert Counter(row[headers.index("contact_status")] for row in contacts) == Counter({"complete": 398, "partial": 18})
    assert Counter(row[headers.index("contact_status")] for row in review) == Counter({"complete": 136, "partial": 53, "missing": 288})
    quality_audit = json.loads((DESTINATION / "quality_audit.json").read_text(encoding="utf-8"))
    assert quality_audit["published_count"] == 416
    assert quality_audit["abstain_count"] == 477
    assert manifest["counts"] == {"total": 893, "published": 416, "review": 477, "pending": 143}
    assert manifest["coverage_complete"] is True
    assert manifest["remediation_complete"] is False
    assert manifest["complete"] is False
    all_ids = set(ids)
    contact_headers = _rows(DESTINATION / "contacts.xlsx")[0]
    review_headers = _rows(DESTINATION / "review_queue.xlsx")[0]
    contact_ids = {str(row[contact_headers.index("source_record_id")]) for row in contacts}
    review_ids = {str(row[review_headers.index("source_record_id")]) for row in review}
    assert contact_ids.isdisjoint(review_ids)
    assert contact_ids | review_ids == all_ids
    old_headers, old_rows = _rows(V2_DESTINATION / "contacts.xlsx")
    old_ids = {str(row[old_headers.index("source_record_id")]) for row in old_rows}
    assert len(old_ids) == 319
    assert old_ids <= contact_ids
    pending = {str(row[headers.index("source_record_id")]) for row in all_rows if row[headers.index("delivery_state")] == "REMEDIATION_PENDING"}
    contact_headers, contact_rows = _rows(DESTINATION / "contacts.xlsx")
    assert len(contact_rows) == 416
    assert pending <= review_ids
    assert not pending & contact_ids
    remaining_db = ROOT / "runs" / json.loads(PLAN.read_text(encoding="utf-8"))["expected_run_id"] / "state" / "progress.sqlite3"
    with sqlite3.connect(f"file:{remaining_db.resolve().as_posix()}?mode=ro&immutable=1", uri=True) as connection:
        scheduler = connection.execute("SELECT free_state,paid_required,paid_state FROM run_items ORDER BY item_index").fetchall()
        provider_calls = connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0]
    assert Counter((str(free), bool(required), str(paid)) for free, required, paid in scheduler) == Counter({("DONE", False, "NOT_REQUIRED"): 16, ("DONE", True, "PENDING"): 143})
    assert provider_calls == 0


def test_known_collisions_are_review_only_and_legacy_review_is_not_promoted():
    _manifest()
    headers, rows = _rows(DESTINATION / "all_results.xlsx")
    company_idx = headers.index("company")
    eligible_idx = headers.index("publication_eligible")
    collision_idx = headers.index("collision_reason")
    collisions = {str(row[company_idx]).strip().casefold() for row in rows if str(row[company_idx]).strip().casefold() in {"akel", "akbarkod"}}
    assert collisions == {"akel", "akbarkod"}
    assert all(row[eligible_idx] is not True for row in rows if str(row[company_idx]).strip().casefold() in collisions)
    assert all(str(row[collision_idx] or "") for row in rows if str(row[company_idx]).strip().casefold() in collisions)


def test_second_run_verifies_without_replacing_and_detects_tampering():
    before = (DESTINATION / "delivery_manifest.json").read_bytes()
    result = reconcile_module.reconcile(
        original_input=ORIGINAL,
        legacy_db=ROOT.parent / "Python b2b_incident_20260827_893" / "state" / "progress.sqlite3",
        recovery_run=ROOT.parent / "Python b2b_recovery_final10_r2" / "runs" / "5ab042f129d9d31108cccc8cecb8549c32f0965c4de418283256284b44f2a36b",
        remaining_plan=PLAN,
        remaining_run=ROOT / "runs" / json.loads(PLAN.read_text(encoding="utf-8"))["expected_run_id"],
        destination=DESTINATION,
    )
    assert result["verify_only"] is True
    assert (DESTINATION / "delivery_manifest.json").read_bytes() == before
