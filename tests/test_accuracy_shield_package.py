import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import main
from modules import crawler, search


class AccuracyShieldPackageTests(unittest.TestCase):
    def test_matching_site_email_is_not_independent_identity_evidence(self) -> None:
        candidate = {
            "url": "https://wrong-company.com.tr",
            "query": "wrong company contact",
            "reason": "search_text_identity:1/2",
        }
        reasons = [
            "page_identity_missing:0/2",
            "structured_identity_absent",
            "email_domain_match",
        ]
        self.assertFalse(main._has_trusted_website_evidence(candidate, reasons))
        status, confidence = main._confidence_status(95, True, reasons, False)
        self.assertEqual((status, confidence), ("REVIEW_NEEDED", "review"))

    def test_identity_outranks_contacts_when_candidates_compete(self) -> None:
        trusted = {
            "candidate": {"url": "https://brand.com.tr", "_official_query_evidence": 1},
            "reasons": ["page_identity_strong:2/2", "no_email"],
            "context_failed": False,
            "email_failed": False,
            "has_contact": False,
            "final_score": 82,
        }
        wrong_with_contacts = {
            "candidate": {"url": "https://other.com.tr", "_official_query_evidence": 2},
            "reasons": ["page_identity_missing:0/2", "email_domain_match"],
            "context_failed": False,
            "email_failed": False,
            "has_contact": True,
            "final_score": 99,
        }
        ranked = sorted(
            [wrong_with_contacts, trusted],
            key=lambda item: main._evaluation_rank_key("Brand", item),
            reverse=True,
        )
        self.assertIs(ranked[0], trusted)

    def test_contact_query_deep_url_is_kept_as_crawl_seed(self) -> None:
        candidates = {}
        search._add_search_results(
            candidates,
            "Example Brand",
            "example brand iletisim",
            [{"href": "https://examplebrand.com.tr/iletisim", "title": "Example Brand", "body": ""}],
        )
        candidate = candidates["examplebrand.com.tr"]
        self.assertEqual(candidate["url"], "https://examplebrand.com.tr")
        self.assertEqual(candidate["_contact_seed_urls"], ["https://examplebrand.com.tr/iletisim"])

    def test_crawl_seeds_reject_other_domains_and_non_contact_pages(self) -> None:
        safe = crawler._safe_contact_seed_urls("https://example.com.tr", [
            "https://example.com.tr/iletisim",
            "https://example.com.tr/products",
            "https://evil.com.tr/contact",
            "file:///contact",
        ])
        self.assertEqual(safe, ["https://example.com.tr/iletisim"])

    def test_profile_generic_external_link_is_not_authoritative(self) -> None:
        candidates = {}
        with patch("modules.search._profile_external_websites", return_value=[
            {"url": "https://parent.com.tr", "label": "Parent", "explicit_website": False},
            {"url": "https://brand.com.tr", "label": "Website", "explicit_website": True},
        ]):
            search._add_profile_candidates(candidates, "Brand", {"profile_url": "https://fair.example/brand"})
        self.assertEqual(candidates["parent.com.tr"]["query"], "source_profile_link")
        self.assertEqual(candidates["parent.com.tr"]["_source_profile_evidence"], 0)
        self.assertEqual(candidates["brand.com.tr"]["query"], "source_profile")
        self.assertEqual(candidates["brand.com.tr"]["_source_profile_evidence"], 1)
        self.assertGreater(candidates["brand.com.tr"]["score"], candidates["parent.com.tr"]["score"])

    def test_unverified_candidate_is_kept_for_review_not_marked_ok(self) -> None:
        reasons = ["page_identity_missing:0/2", "email_domain_match"]
        status, confidence = main._confidence_status(99, True, reasons, False)
        self.assertEqual(status, "REVIEW_NEEDED")
        self.assertEqual(confidence, "review")
        self.assertIn("website_identity_not_independently_verified", reasons)


if __name__ == "__main__":
    unittest.main()
