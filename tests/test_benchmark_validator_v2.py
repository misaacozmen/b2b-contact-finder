from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

from openpyxl import Workbook
import pytest

from materialize_legacy_publication_decisions import materialize
from modules import publication_policy
from validate_benchmark_suite import (
    SOURCE_REVIEW_FIELDS,
    _invalid_package_issues,
    _sheet_rows,
    _validate_invalid_registry,
    _validate_v3_ground_truth,
    evaluate_source_review,
)


def _workbook(path: Path, headers: list[str], rows: list[dict]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(header, "") for header in headers])
    workbook.save(path)
    workbook.close()


def _expected_row(source_id: str) -> dict:
    return {
        "source_record_id": source_id, "Company": "Example Textile", "source": "texhibition_2026",
        "official_profile_url": "https://fair.example/profile/1", "listed_legal_name": "Example Textile Ltd",
        "listed_address": "Istanbul", "listed_phone": "+90 212 555 0101", "expected_website": "https://example.test",
        "website_verified": "present", "expected_email": "info@example.test", "email_verified": "present",
        "expected_phone": "+90 212 555 0101", "phone_verified": "present", "expected_publication": "publishable",
        "identity_evidence_urls": "https://example.test/about", "contact_evidence_urls": "https://example.test/contact",
        "observed_at": "2026-09-01T00:00:00Z", "evidence_content_sha256": "a" * 64,
        "reviewer_pass_1": "reviewer-a", "reviewer_pass_2": "reviewer-b", "disagreement_reason": "",
        "label_status": "frozen",
    }


def _actual_row(source_id: str, website: str = "https://example.test") -> dict:
    return {
        "source_record_id": source_id, "website": website, "email": "info@example.test",
        "phone": "+90 212 555 0101", "publication_eligible": True,
        "publication_decision": json.dumps({"source_record_id": source_id, "run_id": "run-v2", "config_sha256": "a" * 64, "publishable": True}),
    }


def _v3_expected_row(source_id: str = "src:v3") -> dict:
    row = _expected_row(source_id)
    row.update({
        "source_listed_website": "www.example.test",
        "source_listed_website_status": "present",
        "expected_website_domains_json": '["example.test"]',
        "expected_emails_json": '["info@example.test"]',
        "expected_phones_e164_json": '["+902125550101"]',
        "expected_publication_reason_codes_json": "[]",
        "website_field_evidence_json": '[{"source":"first_party","url":"https://example.test"}]',
        "email_field_evidence_json": '[{"source":"first_party","url":"https://example.test/contact"}]',
        "phone_field_evidence_json": '[{"source":"first_party","url":"https://example.test/contact"}]',
        "reviewer_provenance_json": '[{"execution_id":"structured-1","method":"structured"},{"execution_id":"rendered-1","method":"rendered"}]',
    })
    row["expected_phone"] = "+902125550101"
    return row


def _manifest(tmp_path: Path, expected: Path) -> Path:
    path = tmp_path / "manifest.json"
    actual = tmp_path / "actual.xlsx"
    actual_hash = hashlib.sha256(actual.read_bytes()).hexdigest()
    source_id = str(_sheet_rows(expected)[0].get("source_record_id") or "src:1")
    (tmp_path / "actual_manifest.json").write_text(json.dumps({
        "schema_version": 2, "status": "complete_free_only", "complete": True, "finalized": True, "phase": "COMPLETE",
        "run_id": "run-v2", "source_record_ids": [source_id],
        "source_record_ids_sha256": hashlib.sha256(json.dumps([source_id], separators=(",", ":")).encode()).hexdigest(),
        "provider_calls": 0, "physical_http_requests": 0,
        "logical_request_count": 0, "browser_page_count": 0, "ocr_page_count": 0,
        "elapsed_seconds": 0.1, "cost": 0, "paid_enabled": False,
        "paid_provider_budgets": {name: 0 for name in ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")},
        "config_sha256": "a" * 64, "runtime_source_tree_sha256": "b" * 64,
        "capability_profile": {"browser": False, "ocr": False},
        "expected_sha256": hashlib.sha256(expected.read_bytes()).hexdigest(),
        "files": {"actual.xlsx": {"sha256": actual_hash, "bytes": actual.stat().st_size}},
    }), encoding="utf-8")
    expected_hash = hashlib.sha256(expected.read_bytes()).hexdigest()
    path.write_text(json.dumps({
        "schema_version": 2,
        "run_id": "run-v2",
        "complete": True,
        "finalized": True,
        "phase": "COMPLETE",
        "status": "complete_free_only",
        "source_record_ids": [source_id],
        "source_record_ids_sha256": hashlib.sha256(json.dumps([source_id], separators=(",", ":")).encode()).hexdigest(),
        "expected_sha256": expected_hash,
        "config_sha256": "a" * 64,
        "runtime_source_tree_sha256": "b" * 64,
        "paid_enabled": False,
        "paid_provider_budgets": {name: 0 for name in ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")},
        "logical_request_count": 0,
        "browser_page_count": 0,
        "ocr_page_count": 0,
        "acceptance": {
            "max_false_publication": 0,
            "max_published_unknown_identity": 0,
            "require_full_source_id_coverage": True,
            "require_all_rows_review_frozen": True,
            "require_nonzero_known_identity_denominator": True,
        },
        "files": {"actual.xlsx": {"sha256": actual_hash, "bytes": actual.stat().st_size}},
        "sets": [{"role": "diagnostic", "expected": expected.name, "actual_manifest": "actual_manifest.json"}],
    }), encoding="utf-8")
    return path


def test_source_review_validator_passes_perfect_actual_and_fails_false_publication(tmp_path: Path) -> None:
    expected = tmp_path / "expected.xlsx"
    actual = tmp_path / "actual.xlsx"
    _workbook(expected, list(SOURCE_REVIEW_FIELDS), [_expected_row("src:1")])
    _workbook(actual, ["source_record_id", "website", "email", "phone", "publication_eligible", "publication_decision"], [_actual_row("src:1")])
    manifest = _manifest(tmp_path, expected)

    passed = subprocess.run(
        [sys.executable, "validate_benchmark_suite.py", "--manifest", str(manifest), "--actual", f"diagnostic={actual}", "--require-actual"],
        capture_output=True, text=True, check=False,
    )
    assert passed.returncode == 0
    assert "quality_status: pass" in passed.stdout

    _workbook(actual, ["source_record_id", "website", "email", "phone", "publication_eligible", "publication_decision"], [_actual_row("src:1", "https://wrong-owner.test")])
    actual_manifest = json.loads((tmp_path / "actual_manifest.json").read_text(encoding="utf-8"))
    actual_manifest["files"]["actual.xlsx"] = {"sha256": hashlib.sha256(actual.read_bytes()).hexdigest(), "bytes": actual.stat().st_size}
    (tmp_path / "actual_manifest.json").write_text(json.dumps(actual_manifest), encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, "validate_benchmark_suite.py", "--manifest", str(manifest), "--actual", f"diagnostic={actual}", "--require-actual"],
        capture_output=True, text=True, check=False,
    )
    assert failed.returncode == 3
    assert "false publication" in failed.stdout


def test_v3_ground_truth_accepts_canonical_sets_and_provenance():
    _validate_v3_ground_truth([_v3_expected_row()], "expected")


@pytest.mark.parametrize("field,value,needle", [
    ("expected_emails_json", '["Info@example.test","info@example.test"]', "duplicate"),
    ("expected_website_domains_json", '["https://example.test"]', "canonical"),
    ("expected_phones_e164_json", '["+902125550101","+902125550101"]', "duplicate"),
    ("expected_phones_e164_json", '["+999"]', "E.164"),
])
def test_v3_ground_truth_rejects_malformed_sets(field: str, value: str, needle: str):
    row = _v3_expected_row()
    row[field] = value
    if field == "expected_emails_json":
        row["email_verified"] = "unknown"
    with pytest.raises(ValueError, match=needle):
        _validate_v3_ground_truth([row], "expected")


def test_v3_ground_truth_rejects_populated_absent_and_unsupported_publication():
    row = _v3_expected_row()
    row["website_verified"] = "absent"
    with pytest.raises(ValueError, match="absent field"):
        _validate_v3_ground_truth([row], "expected")
    row = _v3_expected_row()
    row["expected_publication"] = "abstain"
    with pytest.raises(ValueError, match="reason code"):
        _validate_v3_ground_truth([row], "expected")
    row = _v3_expected_row()
    row["expected_publication"] = "publishable"
    row["expected_emails_json"] = "[]"
    row["expected_phones_e164_json"] = "[]"
    row["expected_email"] = ""
    row["expected_phone"] = ""
    row["email_verified"] = "absent"
    row["phone_verified"] = "absent"
    with pytest.raises(ValueError, match="verified contact"):
        _validate_v3_ground_truth([row], "expected")


def test_v3_metrics_use_domain_and_alternative_contact_sets_and_item_latency(tmp_path: Path):
    expected = tmp_path / "expected.xlsx"
    actual = tmp_path / "actual.xlsx"
    row = _v3_expected_row("src:sets")
    row["expected_website"] = "https://example.test"
    row["expected_website_domains_json"] = '["example.org","example.test"]'
    row["expected_emails_json"] = '["info@example.test","sales@example.test"]'
    row["expected_email"] = "info@example.test"
    row["expected_phones_e164_json"] = '["+902125550101"]'
    row["expected_phone"] = "+902125550101"
    _workbook(expected, list(SOURCE_REVIEW_FIELDS) + [
        "source_listed_website", "source_listed_website_status", "expected_website_domains_json",
        "expected_emails_json", "expected_phones_e164_json", "expected_publication_reason_codes_json",
        "website_field_evidence_json", "email_field_evidence_json", "phone_field_evidence_json", "reviewer_provenance_json",
    ], [row])
    _workbook(actual, ["source_record_id", "website", "email", "alternative_emails", "phone", "alternative_phones", "publication_eligible", "elapsed_seconds"], [{
        "source_record_id": "src:sets", "website": "https://www.example.org", "email": "wrong@example.test",
        "alternative_emails": "info@example.test;other@example.test", "phone": "0212 555 01 01",
        "alternative_phones": "+90 212 000 00 00", "publication_eligible": True, "elapsed_seconds": 1.0,
    }])
    metrics = evaluate_source_review(expected, actual)
    assert metrics["correct_published"] == 1
    assert metrics["publication_recall"] == 1.0
    assert metrics["safe_yield"] == 1.0
    assert metrics["email_tp"] == 1
    assert metrics["phone_tp"] == 1
    assert metrics["verified_complete_contact_recall"] == 1.0
    assert metrics["p50_item_seconds"] == 1.0
    assert metrics["p95_item_seconds"] == 1.0
    assert metrics["run_elapsed_seconds"] is None


def test_unknown_records_remain_in_safe_yield_denominator(tmp_path: Path):
    expected = tmp_path / "expected.xlsx"
    actual = tmp_path / "actual.xlsx"
    row = _v3_expected_row("src:unknown-metric")
    row.update({
        "website_verified": "unknown", "email_verified": "unknown", "phone_verified": "unknown",
        "expected_publication": "unknown", "expected_website": "", "expected_email": "", "expected_phone": "",
        "expected_website_domains_json": "[]", "expected_emails_json": "[]", "expected_phones_e164_json": "[]",
    })
    _workbook(expected, list(SOURCE_REVIEW_FIELDS), [row])
    _workbook(actual, ["source_record_id", "publication_eligible", "elapsed_seconds"], [{"source_record_id": "src:unknown-metric", "publication_eligible": False, "elapsed_seconds": 2.0}])
    metrics = evaluate_source_review(expected, actual)
    assert metrics["unknown_record_count"] == 1
    assert metrics["total_expected_records"] == 1
    assert metrics["denominators"]["safe_yield"] == 1
    assert metrics["safe_yield"] == 0.0


def test_source_review_validator_reports_structural_source_id_failure(tmp_path: Path) -> None:
    expected = tmp_path / "expected.xlsx"
    actual = tmp_path / "actual.xlsx"
    _workbook(expected, list(SOURCE_REVIEW_FIELDS), [_expected_row("src:1")])
    _workbook(actual, ["source_record_id", "website"], [{"source_record_id": "src:other", "website": "https://example.test"}])
    manifest = _manifest(tmp_path, expected)
    result = subprocess.run(
        [sys.executable, "validate_benchmark_suite.py", "--manifest", str(manifest), "--actual", f"diagnostic={actual}", "--require-actual"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "actual source ID order/set" in result.stdout


def test_unknown_identity_publication_is_quality_failure_and_stays_in_coverage_denominator(tmp_path: Path) -> None:
    expected = tmp_path / "expected.xlsx"
    actual = tmp_path / "actual.xlsx"
    row = _expected_row("src:unknown")
    row.update({"website_verified": "unknown", "expected_publication": "unknown", "expected_website": ""})
    _workbook(expected, list(SOURCE_REVIEW_FIELDS), [row])
    _workbook(actual, ["source_record_id", "website", "email", "phone", "publication_eligible", "publication_decision"], [_actual_row("src:unknown")])
    manifest = _manifest(tmp_path, expected)
    result = subprocess.run(
        [sys.executable, "validate_benchmark_suite.py", "--manifest", str(manifest), "--actual", f"diagnostic={actual}", "--require-actual"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 3
    assert "published unknown identity" in result.stdout


def test_pending_review_queue_is_structural_failure_under_require_actual(tmp_path: Path) -> None:
    expected = tmp_path / "expected.xlsx"
    actual = tmp_path / "actual.xlsx"
    row = _expected_row("src:pending")
    row["label_status"] = "pending"
    _workbook(expected, list(SOURCE_REVIEW_FIELDS), [row])
    _workbook(actual, ["source_record_id", "publication_eligible"], [{"source_record_id": "src:pending", "publication_eligible": False}])
    manifest = _manifest(tmp_path, expected)
    result = subprocess.run(
        [sys.executable, "validate_benchmark_suite.py", "--manifest", str(manifest), "--actual", f"diagnostic={actual}", "--require-actual"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "label_status" in result.stdout


def test_frozen_zero_denominators_never_pass(tmp_path: Path) -> None:
    expected = tmp_path / "expected.xlsx"
    actual = tmp_path / "actual.xlsx"
    row = _expected_row("src:zero")
    row.update({"expected_publication": "unknown", "website_verified": "unknown", "email_verified": "unknown", "phone_verified": "unknown"})
    _workbook(expected, list(SOURCE_REVIEW_FIELDS), [row])
    zero_row = _actual_row("src:zero")
    zero_row["publication_eligible"] = False
    zero_row["publication_decision"] = json.dumps({"source_record_id": "src:zero", "run_id": "run-v2", "config_sha256": "a" * 64, "publishable": False})
    _workbook(actual, ["source_record_id", "publication_eligible", "publication_decision"], [zero_row])
    manifest = _manifest(tmp_path, expected)
    result = subprocess.run(
        [sys.executable, "validate_benchmark_suite.py", "--manifest", str(manifest), "--actual", f"diagnostic={actual}", "--require-actual"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 3
    assert "zero" in result.stdout


def test_actual_manifest_is_required_for_every_v2_role(tmp_path: Path) -> None:
    expected = tmp_path / "expected.xlsx"
    actual = tmp_path / "actual.xlsx"
    _workbook(expected, list(SOURCE_REVIEW_FIELDS), [_expected_row("src:manifest")])
    _workbook(actual, ["source_record_id", "website", "email", "phone", "publication_eligible", "publication_decision"], [_actual_row("src:manifest")])
    manifest = _manifest(tmp_path, expected)
    (tmp_path / "actual_manifest.json").unlink()
    result = subprocess.run(
        [sys.executable, "validate_benchmark_suite.py", "--manifest", str(manifest), "--actual", f"diagnostic={actual}"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "actual manifest" in result.stdout


def test_invalid_benchmark_registry_rejects_package_before_quality(tmp_path: Path) -> None:
    manifest = tmp_path / "benchmark_manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 3,
        "package_id_sha256": "882b613292f9feac7b1abb5c2e57598ef404b036ac105f8a2b4e35b6d5606786",
        "sets": [{"role": "diagnostic", "expected_sha256": "faf3cb601a1bbfe758b466375eb4330ff9c4807d31271957499dd2c7ce2fc39d"}],
    }), encoding="utf-8")
    assert _invalid_package_issues(manifest, json.loads(manifest.read_text(encoding="utf-8")))


def test_invalid_benchmark_registry_rejects_duplicate_and_malformed_entries(tmp_path: Path) -> None:
    valid = {
        "schema_version": 1,
        "packages": [{
            "package_id": "a" * 64,
            "package_id_sha256": "a" * 64,
            "benchmark_manifest_file_sha256": "b" * 64,
            "expected_sha256": ["c" * 64],
            "invalid_reason_codes": ["actual_not_finalized"],
        }],
    }
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps({**valid, "packages": valid["packages"] * 2}), encoding="utf-8")
    try:
        _validate_invalid_registry(duplicate)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate registry package was accepted")

    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({**valid, "schema_version": 99}), encoding="utf-8")
    try:
        _validate_invalid_registry(malformed)
    except ValueError as exc:
        assert "schema_version" in str(exc)
    else:
        raise AssertionError("unknown registry schema was accepted")


def test_invalid_benchmark_registry_cannot_be_bypassed_by_expected_hash_only(tmp_path: Path) -> None:
    manifest = tmp_path / "other-name.json"
    payload = {
        "schema_version": 3,
        "sets": [{"role": "diagnostic", "expected_sha256": "9f07f57fb202da60642cb053d074debbe4a1defcd5db5f7351f7d00960593f8a"}],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert _invalid_package_issues(manifest, payload)


def test_invalid_benchmark_registry_schema_v2_accepts_artifact_receipt(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "schema_version": 2,
        "packages": [],
        "artifacts": [{
            "artifact_id": "prepackage",
            "artifact_sha256": {"manifest": "a" * 64, "expected": "b" * 64},
            "invalid_reason_codes": [
                "review_passes_same_tool_hash",
                "website_label_web_and_colon_not_parsed",
            ],
        }],
    }), encoding="utf-8")
    assert _validate_invalid_registry(registry)[0]["artifact_id"] == "prepackage"


def test_invalid_benchmark_registry_v2_rejects_duplicate_artifacts_and_hash_bypass(tmp_path: Path, monkeypatch) -> None:
    base_artifact = {
        "artifact_id": "one",
        "artifact_sha256": {"manifest": "a" * 64},
        "invalid_reason_codes": ["crawler_budget_starved_later_records"],
    }
    duplicate = tmp_path / "duplicate-artifact.json"
    duplicate.write_text(json.dumps({
        "schema_version": 2,
        "packages": [],
        "artifacts": [base_artifact, {**base_artifact, "artifact_id": "two"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate invalid benchmark artifact hash"):
        _validate_invalid_registry(duplicate)

    malformed = tmp_path / "malformed-artifact.json"
    malformed.write_text(json.dumps({
        "schema_version": 2,
        "packages": [],
        "artifacts": [{
            "artifact_id": "broken",
            "artifact_sha256": {"manifest": "not-a-sha"},
            "invalid_reason_codes": ["crawler_budget_starved_later_records"],
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="malformed artifact SHA-256"):
        _validate_invalid_registry(malformed)

    manifest = tmp_path / "manifest.json"
    manifest.write_text("artifact bytes\n", encoding="utf-8")
    artifact_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    registry_path = tmp_path / "registry-match.json"
    registry_path.write_text(json.dumps({
        "schema_version": 2,
        "packages": [],
        "artifacts": [{
            "artifact_id": "manifest-bytes",
            "artifact_sha256": {"manifest": artifact_hash},
            "invalid_reason_codes": ["review_passes_same_tool_hash"],
        }],
    }), encoding="utf-8")
    assert _invalid_package_issues(manifest, {}, registry_path=registry_path)


def test_current_prepackage_is_rejected_before_quality() -> None:
    manifest = Path("output/a8_corrected_work/benchmark_manifest_prepackage.json")
    if not manifest.exists():
        pytest.skip("corrected prepackage receipt is not present")
    result = subprocess.run(
        [sys.executable, "validate_benchmark_suite.py", "--manifest", str(manifest), "--require-actual"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "invalid benchmark package registry match" in result.stdout


def test_legacy_materializer_uses_checkpoint_and_evidence_not_excel(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.sqlite3"
    manifest = tmp_path / "parent_manifest.json"
    payload = json.dumps({"source_record_id": "src:1", "company": "Example"}, separators=(",", ":"))
    with sqlite3.connect(checkpoint) as connection:
        connection.executescript("CREATE TABLE runs(run_id TEXT PRIMARY KEY); CREATE TABLE run_items(run_id TEXT,item_index INTEGER,source_record_id TEXT,payload_sha256 TEXT); CREATE TABLE results(run_id TEXT,item_index INTEGER,payload TEXT);")
        connection.execute("INSERT INTO runs VALUES('run-1')")
        connection.execute("INSERT INTO run_items VALUES(?,?,?,?)", ("run-1", 0, "src:1", hashlib.sha256(payload.encode()).hexdigest()))
        connection.execute("INSERT INTO results VALUES(?,?,?)", ("run-1", 0, payload))
        connection.commit()
    manifest.write_text(json.dumps({"run_id": "run-1", "config_sha256": "a" * 64, "source_record_ids": ["src:1"]}), encoding="utf-8")
    output = tmp_path / "materialized"
    result = materialize(checkpoint_path=checkpoint, manifest_path=manifest, destination=output)
    assert result["count"] == 1
    row = json.loads((output / "publication_decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
    publication_policy.verify_publication_decision(row["publication_decision"])
    assert row["publication_decision"]["source_record_id"] == "src:1"
