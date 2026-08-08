import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

import main
import calibrate_publication
from modules import evidence, excel, publication_policy, report, risk_calibration


def _safe_evaluation(**overrides):
    value = {
        "candidate": {
            "url": "https://acme.com.tr",
            "role": "company_candidate",
            "query": "acme Turkiye official website",
        },
        "reasons": [
            "page_identity_strong:2/2",
            "legal_name_phrase_match:2",
            "country_identity_tr_tld",
            "email_domain_match",
        ],
        "identity_assessment": {
            "conflicts": [],
            "support_count": 2,
            "publishable": True,
            "strong_first_party_bundle": True,
            "provisionally_publishable": True,
            "first_party_bundle_components": 3,
            "decision": "verified",
        },
        "structured_identity": {},
        "has_contact": True,
        "email": "info@acme.com.tr",
        "email_verification": "verified",
        "phone": "02125550000",
        "email_failed": False,
    }
    value.update(overrides)
    return value


class PublicationPolicyTests(unittest.TestCase):
    def test_strong_first_party_identity_allows_only_existing_publication(self):
        decision = publication_policy.evaluate(
            "ACME MAKINA",
            _safe_evaluation(),
            "OK_HIGH_CONFIDENCE",
            minimum_safety_score=75,
        )
        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["action"], "allow_legacy_publication")
        self.assertGreaterEqual(decision["safety_score"], 75)

        review = publication_policy.evaluate(
            "ACME MAKINA",
            _safe_evaluation(),
            "REVIEW_NEEDED",
            minimum_safety_score=75,
        )
        self.assertEqual(review["action"], "retain_legacy_abstention")
        self.assertFalse(review["eligible"])
        self.assertTrue(review["risk_eligible"])

    def test_report_separates_complete_review_rows_from_publications(self):
        rows = [
            {
                "company": "PUBLISHED",
                "website": "https://published.example",
                "email": "info@published.example",
                "phone": "02125550000",
                "status": "OK_HIGH_CONFIDENCE",
                "publication_eligible": True,
                "email_verification": "verified",
            },
            {
                "company": "HELD",
                "website": "https://held.example",
                "email": "info@held.example",
                "phone": "02125550001",
                "status": "REVIEW_NEEDED",
                "publication_eligible": False,
                "publication_blockers": "no_candidate_proved_target_fingerprint",
                "email_verification": "verified",
            },
        ]
        with patch("modules.report.runtime.snapshot", return_value={"counters": {}}), patch(
            "modules.report.discovery_coverage.payload",
            return_value={
                "resolved_companies": 1,
                "unresolved_companies": 1,
                "replay_miss_count": 0,
                "acquisition_plan": [],
            },
        ):
            text = report.build_report(rows, 0)
        self.assertIn("Yayin politikasina uygun firma: 1 (50.0%)", text)
        self.assertIn(
            "Tam iletisim bulundu fakat kimlik incelemesinde: 1 (50.0%)",
            text,
        )

    def test_identity_conflict_forces_downgrade(self):
        evaluation = _safe_evaluation()
        evaluation["identity_assessment"] = {
            **evaluation["identity_assessment"],
            "conflicts": [{"kind": "structured_owner_mismatch"}],
            "provisionally_publishable": False,
            "decision": "conflict",
        }
        decision = publication_policy.evaluate(
            "ACME MAKINA",
            evaluation,
            "OK_MEDIUM_CONFIDENCE",
            minimum_safety_score=75,
        )
        reasons = []
        status, confidence = publication_policy.enforce(
            decision, "OK_MEDIUM_CONFIDENCE", "medium", reasons,
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(status, "REVIEW_NEEDED")
        self.assertEqual(confidence, "review")
        self.assertIn(
            "identity_conflict:structured_owner_mismatch",
            decision["hard_blockers"],
        )
        self.assertTrue(reasons[0].startswith("publication_policy_downgrade:"))

    def test_numeric_score_alone_cannot_overrule_missing_identity(self):
        evaluation = _safe_evaluation(final_score=100)
        evaluation["identity_assessment"] = {
            **evaluation["identity_assessment"],
            "support_count": 0,
            "publishable": False,
            "strong_first_party_bundle": False,
            "provisionally_publishable": False,
            "first_party_bundle_components": 0,
            "decision": "insufficient_independent_support",
        }
        decision = publication_policy.evaluate(
            "ACME MAKINA",
            evaluation,
            "OK_HIGH_CONFIDENCE",
            minimum_safety_score=75,
        )
        self.assertFalse(decision["eligible"])
        self.assertIn("identity_not_publishable", decision["hard_blockers"])

    def test_resolved_first_party_cross_domain_email_is_not_double_blocked(self):
        evaluation = _safe_evaluation(
            email="sales@verified-mail.net",
            email_failed=True,
        )
        evaluation["reasons"] = [
            *evaluation["reasons"],
            "email_domain_mismatch",
            "email_gate_failed",
            "cross_domain_email_accepted_from_verified_official_page",
        ]
        decision = publication_policy.evaluate(
            "ACME MAKINA",
            evaluation,
            "OK_HIGH_CONFIDENCE",
            minimum_safety_score=75,
        )
        self.assertTrue(decision["eligible"])
        self.assertNotIn("email_gate_failed", decision["hard_blockers"])

    def test_main_policy_shadow_mode_audits_without_mutating_status(self):
        evaluation = _safe_evaluation()
        evaluation["identity_assessment"] = {
            **evaluation["identity_assessment"],
            "provisionally_publishable": False,
            "publishable": False,
            "strong_first_party_bundle": False,
        }
        with patch("main.config.PUBLICATION_POLICY_MODE", "shadow"):
            status, confidence = main._apply_publication_policy(
                "ACME MAKINA",
                evaluation,
                "OK_HIGH_CONFIDENCE",
                "high",
                evaluation["reasons"],
            )
        self.assertEqual((status, confidence), ("OK_HIGH_CONFIDENCE", "high"))
        self.assertEqual(
            evaluation["publication_policy"]["action"],
            "would_downgrade_to_review",
        )


class RiskCoverageCalibrationTests(unittest.TestCase):
    def test_curve_is_threshold_ordered_and_coverage_monotonic(self):
        records = [
            {"publication_safety_score": 95, "correct": True},
            {"publication_safety_score": 90, "correct": True},
            {"publication_safety_score": 80, "correct": False},
            {"publication_safety_score": 60, "correct": True},
        ]
        curve = risk_calibration.risk_coverage_curve(records)
        self.assertEqual([point["threshold"] for point in curve], [95, 90, 80, 60])
        self.assertEqual([point["accepted"] for point in curve], [1, 2, 3, 4])
        self.assertEqual(curve[1]["risk"], 0.0)
        self.assertAlmostEqual(curve[-1]["coverage"], 1.0)

    def test_recommendation_maximizes_coverage_at_empirical_target(self):
        records = [
            {
                "publication_safety_score": score,
                "correct": correct,
                "split_role": "validation",
            }
            for score, correct in ((95, True), (90, True), (80, False), (60, True))
        ]
        result = risk_calibration.recommend_threshold(
            records,
            target_precision=0.66,
            minimum_accepted=2,
        )
        self.assertEqual(result["candidate_threshold"], 60)
        self.assertEqual(result["accepted"], 4)
        self.assertFalse(result["deployable"])

    def test_small_perfect_set_is_not_silently_deployable(self):
        records = [
            {
                "publication_safety_score": 90,
                "correct": True,
                "split_role": "validation",
            }
            for _ in range(20)
        ]
        result = risk_calibration.recommend_threshold(
            records,
            target_precision=0.99,
            minimum_accepted=20,
        )
        self.assertEqual(result["precision"], 1.0)
        self.assertLess(result["precision_wilson_lower"], 0.99)
        self.assertFalse(result["deployable"])

    def test_large_perfect_validation_set_can_meet_bound(self):
        records = [
            {
                "publication_safety_score": 90,
                "correct": True,
                "split_role": "calibration",
            }
            for _ in range(400)
        ]
        result = risk_calibration.recommend_threshold(
            records,
            target_precision=0.99,
            minimum_accepted=100,
        )
        self.assertTrue(result["deployable"])
        self.assertGreaterEqual(result["precision_wilson_lower"], 0.99)

    def test_holdout_rows_are_rejected_for_threshold_fitting(self):
        with self.assertRaisesRegex(ValueError, "holdout"):
            risk_calibration.recommend_threshold([
                {
                    "publication_safety_score": 90,
                    "correct": True,
                    "split_role": "holdout",
                }
            ], minimum_accepted=1)

    def test_unlabelled_rows_do_not_enter_curve_denominator(self):
        curve = risk_calibration.risk_coverage_curve([
            {"publication_safety_score": 90, "correct": True},
            {"publication_safety_score": 100},
        ])
        self.assertEqual(curve[0]["labelled_total"], 1)

    def test_string_false_label_is_not_coerced_to_true(self):
        curve = risk_calibration.risk_coverage_curve([
            {"publication_safety_score": 90, "correct": "false"},
        ])
        self.assertEqual(curve[0]["correct"], 0)
        self.assertEqual(curve[0]["risk"], 1.0)

    def test_invalid_label_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid calibration label"):
            risk_calibration.risk_coverage_curve([
                {"publication_safety_score": 90, "correct": "maybe"},
            ])

    def test_jsonl_calibration_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            path.write_text(
                '{"publication_safety_score": 90, "correct": true, "split_role": "validation"}\n'
                '{"publication_safety_score": 70, "correct": false, "split_role": "validation"}\n',
                encoding="utf-8",
            )
            records = calibrate_publication.load_records(path)
        self.assertEqual(len(records), 2)
        self.assertTrue(records[0]["correct"])


class PublicationAuditOutputTests(unittest.TestCase):
    def test_excel_and_evidence_keep_policy_provenance(self):
        row = {
            "company": "ACME MAKINA",
            "website": "https://acme.com.tr",
            "status": "OK_HIGH_CONFIDENCE",
            "publication_policy_version": publication_policy.POLICY_VERSION,
            "publication_policy_action": "allow_legacy_publication",
            "publication_eligible": True,
            "publication_safety_score": 97,
            "publication_risk_index": 3,
            "publication_risk_tier": "low",
            "publication_blockers": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "contacts.xlsx"
            evidence_path = Path(directory) / "evidence.jsonl"
            excel.write_contacts(workbook_path, [row])
            evidence.write_jsonl(evidence_path, [row])

            workbook = load_workbook(workbook_path, read_only=True, data_only=True)
            try:
                headers = [cell.value for cell in workbook.active[1]]
                values = [cell.value for cell in workbook.active[2]]
            finally:
                workbook.close()
            output = dict(zip(headers, values))
            self.assertEqual(output["publication_safety_score"], 97)
            self.assertEqual(output["publication_risk_tier"], "low")

            record = json.loads(evidence_path.read_text(encoding="utf-8").strip())
            self.assertEqual(
                record["selected"]["publication_policy"]["version"],
                publication_policy.POLICY_VERSION,
            )
            self.assertEqual(
                record["selected"]["publication_policy"]["safety_score"],
                97,
            )


if __name__ == "__main__":
    unittest.main()
