import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import main
from modules import scorer, search
from validate_golden_xlsx import evaluate


class Golden2PackageTests(unittest.TestCase):
    def test_verified_contact_can_match_an_alternative_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            actual = root / "actual.xlsx"
            self._write(expected, "Manual Report", [
                ["Company", "Expected Website", "Expected Email", "Expected Phone", "Website Verified", "Email Verified", "Phone Verified"],
                ["Example", "example.com", "sales@example.com", "05321234567", "yes", "yes", "yes"],
            ])
            self._write(actual, "Contacts", [
                ["company", "website", "email", "alternative_emails", "phone", "alternative_phones"],
                ["Example", "https://example.com", "info@example.com", "sales@example.com", "02121234567", "05321234567 [sales]"],
            ])
            metrics, complete = evaluate(expected, actual)
        self.assertEqual(metrics, {field: {"tp": 1, "fp": 0, "fn": 0} for field in ("website", "email", "phone")})
        self.assertEqual(complete, ["Example"])

    def test_golden_no_only_asserts_the_primary_published_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            actual = root / "actual.xlsx"
            self._write(expected, "Manual Report", [
                ["Company", "Expected Website", "Expected Email", "Expected Phone", "Website Verified", "Email Verified", "Phone Verified"],
                ["Example", "", "", "", "no", "no", "no"],
            ])
            self._write(actual, "Contacts", [
                ["company", "website", "email", "alternative_emails", "phone", "alternative_phones"],
                ["Example", "", "", "info@example.com", "", "05321234567 [general]"],
            ])
            metrics, complete = evaluate(expected, actual)
        self.assertEqual(metrics, {field: {"tp": 0, "fp": 0, "fn": 0} for field in ("website", "email", "phone")})
        self.assertEqual(complete, ["Example"])

    def test_b2b_mailbox_is_preferred_over_generic_info(self) -> None:
        selected = main._select_best_email(
            "Berre Kimya", "https://berrekimya.com", ["info@berrekimya.com", "sales@berrekimya.com"]
        )
        self.assertEqual(selected, "sales@berrekimya.com")

    def test_slash_separated_brands_are_searched_independently(self) -> None:
        variants = scorer.search_name_variants("BATTAL BEY / AYDAMAK")
        self.assertIn("battal bey", variants)
        self.assertIn("aydamak", variants)

    def test_cached_flipbook_profile_link_is_never_a_candidate(self) -> None:
        candidates = {}
        with patch("modules.search._profile_external_websites", return_value=[
            "https://heyzine.com/flip-book/123", "https://beirutluchocolate.com"
        ]):
            search._add_profile_candidates(candidates, "BEIRUTLU CHOCOLATE", {"profile_url": "https://fair.example/1"})
        self.assertNotIn("heyzine.com", candidates)
        self.assertIn("beirutluchocolate.com", candidates)

    def test_matching_email_does_not_resolve_sparse_context_conflict(self) -> None:
        candidate = {
            "url": "https://2f.com.tr", "role": "company_candidate", "score": 84,
            "reason": "domain_hits:1/2", "_official_query_evidence": 6,
            "_exact_brand_domain": True,
        }
        evaluation = {
            "candidate": candidate,
            "reasons": ["page_identity_medium:2/3", "context_missing:0/1", "email_domain_match"],
            "context_failed": True,
        }
        self.assertFalse(main._has_trusted_website_evidence(candidate, evaluation["reasons"]))
        self.assertTrue(main._is_hard_context_failure(evaluation))

    def test_same_brand_core_alone_does_not_create_official_family(self) -> None:
        first = {"candidate": {"url": "https://viadellerose.com"}, "structured_identity": {}}
        second = {"candidate": {"url": "https://viadellerose.eu"}, "structured_identity": {}}
        self.assertFalse(main._same_official_family(first, second, "#VDR VIADELLEROSE"))

    def test_unreachable_profile_bridge_is_not_published_in_contacts(self) -> None:
        candidates = [
            {"url": "https://akinkonfeksiyon.com.tr", "query": "source_profile", "score": 92, "rank": 1, "_source_profile_evidence": 1},
            {"url": "https://hissetcollection.com.tr", "query": "source_profile", "score": 92, "rank": 2, "_source_profile_evidence": 1, "_official_query_evidence": 1},
        ]
        self.assertIsNone(main._authoritative_unreachable_candidate(candidates, "HİSSET"))

    @staticmethod
    def _write(path: Path, title: str, rows: list[list]) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = title
        for row in rows:
            sheet.append(row)
        workbook.save(path)


if __name__ == "__main__":
    unittest.main()
