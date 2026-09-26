"""Regression tests for fail-closed publication export gate."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from modules import (
    discovery_coverage,
    entity_memory,
    excel,
    output_artifacts,
    publication_policy,
    quality_audit,
    report,
)
from strict_fixtures import publishable_content_decision


def _publishable_row(**updates):
    row = {
        "source_record_id": "fixture:publication",
        "free_state": "DONE",
        "paid_required": False,
        "paid_state": "NOT_REQUIRED",
        "status": "OK_HIGH_CONFIDENCE",
        "publication_eligible": True,
        "website": "https://fixture.example",
        "email": "info@fixture.example",
        "phone": "+902120000000",
    }
    row.update(updates)
    row["content_decision"] = publishable_content_decision(
        source_record_id=row["source_record_id"],
        website=row.get("website", "https://fixture.example"),
        email=row.get("email", "info@fixture.example"),
        phone=row.get("phone", "+902120000000"),
    )
    return row


class PublicationExportGateTests(unittest.TestCase):
    def test_is_publishable_row_predicate(self):
        self.assertTrue(
            publication_policy.is_publishable_row(_publishable_row())
        )
        self.assertTrue(
            publication_policy.is_publishable_row(_publishable_row(status="OK_MEDIUM_CONFIDENCE"))
        )
        # Shadow mode / downgrade-eligible false cases
        self.assertFalse(
            publication_policy.is_publishable_row(
                {"status": "OK_HIGH_CONFIDENCE", "publication_eligible": False}
            )
        )
        self.assertFalse(
            publication_policy.is_publishable_row(
                {"status": "OK_MEDIUM_CONFIDENCE", "publication_eligible": False}
            )
        )
        # Missing publication_eligible field must fail-closed
        self.assertFalse(
            publication_policy.is_publishable_row({"status": "OK_HIGH_CONFIDENCE"})
        )
        self.assertFalse(
            publication_policy.is_publishable_row(
                {"status": "REVIEW_NEEDED", "publication_eligible": True}
            )
        )
        self.assertFalse(
            publication_policy.is_publishable_row(
                {"status": "WEBSITE_NOT_FOUND", "publication_eligible": True}
            )
        )

    def test_publication_gate_withheld_shadow_mode_isolation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            contacts_file = out_dir / "contacts.xlsx"
            verified_file = out_dir / "verified_contacts.xlsx"
            review_file = out_dir / "review_queue.xlsx"
            failed_file = out_dir / "failed.xlsx"
            candidates_file = out_dir / "candidates.xlsx"
            report_file = out_dir / "report.txt"
            evidence_file = out_dir / "evidence.jsonl"
            entity_rel_file = out_dir / "entity_relationships.jsonl"
            audit_file = out_dir / "quality_audit.json"
            coverage_file = out_dir / "coverage.json"
            snapshot_file = out_dir / "snapshot.json.gz"
            telemetry_file = out_dir / "telemetry.json"
            memory_file = out_dir / "verified_entity_memory.jsonl"

            with patch.multiple(
                config,
                OUTPUT_DIR=out_dir,
                CONTACTS_FILE=contacts_file,
                VERIFIED_CONTACTS_FILE=verified_file,
                REVIEW_QUEUE_FILE=review_file,
                FAILED_FILE=failed_file,
                CANDIDATES_FILE=candidates_file,
                REPORT_FILE=report_file,
                EVIDENCE_FILE=evidence_file,
                ENTITY_RELATIONSHIPS_FILE=entity_rel_file,
                QUALITY_AUDIT_FILE=audit_file,
                DISCOVERY_COVERAGE_FILE=coverage_file,
                REPLAY_SNAPSHOT_FILE=snapshot_file,
                TELEMETRY_FILE=telemetry_file,
                VERIFIED_ENTITY_MEMORY_FILE=memory_file,
                SEARCH_CACHE_MODE="replay",
                CRAWL_CACHE_MODE="replay",
            ):
                discovery_coverage.reset()

                shadow_row = {
                    "company": "Shadow Corp",
                    "status": "OK_HIGH_CONFIDENCE",
                    "publication_eligible": False,
                    "website": "https://shadow.com",
                    "email": "info@shadow.com",
                    "phone": "+902121112233",
                    "score": 90,
                    "__evaluation": {
                        "_identity_resolution": "candidate_resolved_exact_name",
                        "identity_assessment": {"conflicts": []},
                        "crawl": {"pages": ["https://shadow.com"]},
                    },
                }

                # 1. Candidate stage must not be published
                candidates = [
                    {"url": "https://shadow.com", "query": "q", "score": 90}
                ]
                attached = output_artifacts.attach_candidates(dict(shadow_row), candidates)
                stages = [
                    s.get("stage")
                    for eval_item in attached.get("__candidate_evaluations", [])
                    for s in eval_item.get("stages", [])
                ]
                self.assertNotIn("published", stages)
                self.assertIn("selected_for_review", stages)

                # 2. Write outputs and verify surfaces
                report_txt = output_artifacts.write_outputs([shadow_row], 1.0)

                # Not in contacts.xlsx or verified_contacts.xlsx
                published_contacts = excel.read_company_records(contacts_file)
                self.assertEqual(
                    [r["company"] for r in published_contacts if r["company"] == "Shadow Corp"],
                    [],
                )
                verified_contacts = excel.read_company_records(verified_file)
                self.assertEqual(
                    [r["company"] for r in verified_contacts if r["company"] == "Shadow Corp"],
                    [],
                )

                # Must be in review_queue.xlsx
                review_contacts = excel.read_company_records(review_file)
                self.assertIn("Shadow Corp", [r["company"] for r in review_contacts])

                # Discovery coverage must not mark it as published
                cov = discovery_coverage.payload()
                self.assertEqual(cov["resolved_companies"], 0)

                # Entity memory must not remember it
                entity_memory.remember([shadow_row])
                self.assertEqual(entity_memory.candidates("Shadow Corp"), [])

                # Quality audit published count must be 0
                audit_data = quality_audit.payload([shadow_row])
                self.assertEqual(audit_data["published_count"], 0)
                self.assertEqual(audit_data["abstain_count"], 1)

                # Report must reflect 0 verified/published
                self.assertIn("Otomatik kullanima uygun dogrulanmis firma: 0", report_txt)

    def test_publication_gate_eligible_row_passes_to_workbooks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            contacts_file = out_dir / "contacts.xlsx"
            verified_file = out_dir / "verified_contacts.xlsx"
            review_file = out_dir / "review_queue.xlsx"
            failed_file = out_dir / "failed.xlsx"
            candidates_file = out_dir / "candidates.xlsx"
            report_file = out_dir / "report.txt"
            evidence_file = out_dir / "evidence.jsonl"
            entity_rel_file = out_dir / "entity_relationships.jsonl"
            audit_file = out_dir / "quality_audit.json"
            coverage_file = out_dir / "coverage.json"
            snapshot_file = out_dir / "snapshot.json.gz"
            telemetry_file = out_dir / "telemetry.json"
            memory_file = out_dir / "verified_entity_memory.jsonl"

            with patch.multiple(
                config,
                OUTPUT_DIR=out_dir,
                CONTACTS_FILE=contacts_file,
                VERIFIED_CONTACTS_FILE=verified_file,
                REVIEW_QUEUE_FILE=review_file,
                FAILED_FILE=failed_file,
                CANDIDATES_FILE=candidates_file,
                REPORT_FILE=report_file,
                EVIDENCE_FILE=evidence_file,
                ENTITY_RELATIONSHIPS_FILE=entity_rel_file,
                QUALITY_AUDIT_FILE=audit_file,
                DISCOVERY_COVERAGE_FILE=coverage_file,
                REPLAY_SNAPSHOT_FILE=snapshot_file,
                TELEMETRY_FILE=telemetry_file,
                VERIFIED_ENTITY_MEMORY_FILE=memory_file,
                SEARCH_CACHE_MODE="replay",
                CRAWL_CACHE_MODE="replay",
            ):
                discovery_coverage.reset()

                valid_row = {
                    "company": "Valid Corp",
                    "status": "OK_HIGH_CONFIDENCE",
                    "publication_eligible": True,
                    "source_record_id": "fixture:valid",
                    "free_state": "DONE",
                    "paid_required": False,
                    "paid_state": "NOT_REQUIRED",
                    "content_decision": publishable_content_decision(
                        source_record_id="fixture:valid",
                        website="https://valid.com",
                        email="info@valid.com",
                        phone="+902121112233",
                    ),
                    "website": "https://valid.com",
                    "email": "info@valid.com",
                    "phone": "+902121112233",
                    "score": 90,
                    "__evaluation": {
                        "_identity_resolution": "candidate_resolved_exact_name",
                        "identity_assessment": {"conflicts": []},
                        "crawl": {"pages": ["https://valid.com"]},
                    },
                }

                output_artifacts.write_outputs([valid_row], 1.0)

                published_contacts = excel.read_company_records(contacts_file)
                self.assertIn("Valid Corp", [r["company"] for r in published_contacts])
                verified_contacts = excel.read_company_records(verified_file)
                self.assertIn("Valid Corp", [r["company"] for r in verified_contacts])
                review_contacts = excel.read_company_records(review_file)
                self.assertNotIn("Valid Corp", [r["company"] for r in review_contacts])


if __name__ == "__main__":
    unittest.main()
