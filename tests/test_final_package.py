import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import config
from modules import crawler, extractor, scorer, search
from validate_golden_xlsx import _hosts, _phones, evaluate, readiness_issues


class FinalPackageTests(unittest.TestCase):
    def test_profile_candidate_is_not_overwritten_by_weaker_search_hit(self) -> None:
        candidates = {}
        with patch("modules.search._profile_external_websites", return_value=["https://official.com.tr"]):
            search._add_profile_candidates(candidates, "Official", {"profile_url": "https://fair.example/p/1"})
        search._add_search_results(
            candidates,
            "Official",
            "Official Türkiye official website",
            [{"href": "https://official.com.tr/contact", "title": "Official", "body": ""}],
        )
        candidate = candidates["official.com.tr"]
        self.assertEqual(candidate["query"], "source_profile")
        self.assertEqual(candidate["_source_profile_evidence"], 1)
        self.assertEqual(candidate["_profile_url"], "https://fair.example/p/1")
        self.assertGreaterEqual(candidate["score"], config.PRE_CRAWL_SCORE_CAP)
        self.assertTrue(candidate["_search_evidence"])

    def test_official_contact_subdomain_is_allowed_but_lookalike_is_not(self) -> None:
        html = """
        <a href="https://info.explosion.com.tr/contact/">İletişim</a>
        <a href="https://explosion.com.tr.evil.com/contact/">Contact</a>
        """
        links = extractor.extract_contact_page_links(
            html, "https://www.explosion.com.tr", 5, allow_official_subdomains=True
        )
        self.assertEqual(links, ["https://info.explosion.com.tr/contact/"])
        self.assertTrue(scorer.same_registrable_domain("www.explosion.com.tr", "info.explosion.com.tr"))
        self.assertFalse(scorer.same_registrable_domain("explosion.com.tr.evil.com", "explosion.com.tr"))

    def test_dynamic_contact_page_without_contacts_requests_render(self) -> None:
        self.assertTrue(crawler._contact_page_needs_render(
            "<html><body>JavaScript must be enabled to view this page.</body></html>"
        ))
        self.assertFalse(crawler._contact_page_needs_render(
            "<html><body>Email: info@example.com</body></html>"
        ))

    def test_golden_parsers_accept_multiple_sites_and_phone_range(self) -> None:
        self.assertEqual(_hosts("https://a.com\nwww.b.com.tr"), ["a.com", "b.com.tr"])
        self.assertEqual(_phones("0212 296 76 38-39"), ["02122967638", "02122967639"])

    def test_unverified_expected_value_is_not_asserted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            actual = root / "actual.xlsx"
            self._write_xlsx(expected, [
                ["Company", "Expected Website", "Expected Email", "Expected Phone", "Website Verified", "Email Verified", "Phone Verified"],
                ["Example", "wrong.example", "wrong@example.com", "02120000000", "", "", ""],
            ], "Manual Report")
            self._write_xlsx(actual, [
                ["company", "website", "email", "phone"],
                ["Example", "https://actual.example", "actual@actual.example", "02121111111"],
            ], "Contacts")
            metrics, complete = evaluate(expected, actual)
            issues = readiness_issues(expected)
        self.assertEqual(metrics, {field: {"tp": 0, "fp": 0, "fn": 0} for field in ("website", "email", "phone")})
        self.assertEqual(complete, [])
        self.assertEqual(len(issues), 3)

    @staticmethod
    def _write_xlsx(path: Path, rows: list[list], title: str) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = title
        for row in rows:
            sheet.append(row)
        workbook.save(path)


if __name__ == "__main__":
    unittest.main()
