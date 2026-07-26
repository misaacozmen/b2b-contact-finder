import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import tempfile

import requests
from openpyxl import Workbook

import config
import main
from modules import crawler, extractor, identity, search
from validate_golden_xlsx import evaluate_stages


class IdentityArchitectureTests(unittest.TestCase):
    @staticmethod
    def _write_book(path: Path, headers: list[str], rows: list[list], title: str = "Sheet") -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = title
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(path)

    def test_search_text_and_first_party_contacts_do_not_make_identity_independent(self):
        candidate = {
            "url": "https://reseller.example", "query": "brand official website",
            "reason": "search_text_identity:2/2", "_legal_name_evidence": 1,
        }
        assessment = identity.assess(
            "Brand Makina", candidate,
            ["page_identity_strong:2/2", "legal_name_phrase_match:2", "email_domain_match"],
        )
        self.assertEqual(assessment["support_keys"], ["first_party_identity"])
        self.assertFalse(assessment["publishable"])

    def test_intrinsic_domain_and_first_party_identity_are_two_sources(self):
        candidate = {
            "url": "https://brandmakina.com.tr", "query": "official website",
            "reason": "domain_hits:2/2", "role": "company_candidate",
        }
        assessment = identity.assess(
            "Brand Makina", candidate,
            ["page_identity_medium:2/2", "country_identity_tr_tld"],
        )
        self.assertEqual(set(assessment["support_keys"]), {"domain_identity", "first_party_identity"})
        self.assertTrue(assessment["publishable"])

    def test_missing_discovery_business_context_is_neutral(self):
        candidate = {
            "url": "https://different.com.tr", "reason": "search_text_identity:1/2",
            "role": "company_candidate",
        }
        assessment = identity.assess(
            "Example Tekstil", candidate,
            ["page_identity_medium:1/2", "metadata_context_missing:0/1", "country_identity_tr_tld"],
        )
        self.assertFalse(assessment["publishable"])
        self.assertNotIn("business_context_unmatched", {item["kind"] for item in assessment["conflicts"]})
        self.assertIn("business_context_not_observed", {item["kind"] for item in assessment["neutral"]})

    def test_structured_owner_conflict_requires_explicit_relationship(self):
        candidate = {
            "url": "https://brand.com.tr", "reason": "domain_hits:1/1",
            "role": "company_candidate",
        }
        conflicted = identity.assess(
            "Brand", candidate,
            ["page_identity_strong:1/1", "structured_identity_unmatched:0/1", "country_identity_tr_tld"],
            {"names": ["Different Holding"]},
        )
        resolved = identity.assess(
            "Brand", candidate,
            ["page_identity_strong:1/1", "structured_identity_unmatched:0/1", "legal_name_ownership_match:1", "country_identity_tr_tld"],
            {"names": ["Different Holding"]},
        )
        self.assertFalse(conflicted["publishable"])
        self.assertTrue(resolved["publishable"])

    def test_canonical_urls_without_organization_name_are_neutral(self):
        pages = [{
            "url": "https://example.com",
            "html": '<link rel="canonical" href="https://example.com"><p>Example Makina</p>',
        }]
        _, reason, structured = main._structured_identity_score("Example Makina", pages)
        self.assertEqual(reason, "structured_identity_urls_only")
        self.assertFalse(structured["names"])

    def test_brand_owner_statement_requires_local_proximity(self):
        near = "Example Sanayi AS ticari unvani ile faaliyet gosteren Example bir markadir."
        far = "Example Sanayi AS " + ("urun " * 200) + "baska bir markadir"
        self.assertTrue(main.scorer.ownership_statement_match("Example Sanayi AS", near))
        self.assertFalse(main.scorer.ownership_statement_match("Example Sanayi AS", far))
        extracted = extractor.extract_organization_evidence(f"<footer>{near}</footer>")
        self.assertTrue(extracted["ownership_statements"])

    def test_two_first_party_same_name_domains_are_ambiguous_without_family_edge(self):
        def evaluation(url, owner):
            return {
                "candidate": {"url": url, "reason": "domain_hits:2/2", "role": "company_candidate"},
                "reasons": ["page_identity_strong:2/2", "structured_identity_strong:2/2", "context_match:1/1"],
                "structured_identity": {"names": [owner], "urls": [], "same_as": [], "identifiers": []},
            }
        result = main._homonym_conflict(
            "Example Makina",
            evaluation("https://examplemakina.com", "Example Makina AŞ"),
            evaluation("https://examplemakina.com.tr", "Example Makina Ltd"),
        )
        self.assertTrue(result["ambiguous"])

    def test_unreachable_same_core_company_domain_blocks_selection(self):
        selected = {
            "candidate": {"url": "https://example.com.tr", "role": "company_candidate"},
            "crawl_result": {"pages": [{"url": "https://example.com.tr", "html": "Example"}]},
            "reasons": ["page_identity_strong:1/1"], "structured_identity": {},
            "final_score": 90, "has_contact": True, "email_failed": False,
        }
        failed = {
            "candidate": {"url": "https://example.com", "role": "company_candidate", "score": 84},
            "crawl_result": {"pages": [], "error": "cached_security_interstitial"},
            "reasons": ["cached_security_interstitial"], "structured_identity": {},
            "final_score": 0, "has_contact": False, "email_failed": False,
        }
        conflict = main._unreachable_homonym_conflict("Example", selected, [selected, failed])
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["reason"], "unreachable_same_name_domain")

    def test_source_profile_circuit_breaker_stops_repeated_5xx(self):
        response = Mock(status_code=500)
        error = requests.HTTPError("server error", response=response)
        search.reset_source_health()
        with patch.object(config, "SEARCH_CACHE_MODE", "off"), patch.object(
            config, "SOURCE_PROFILE_MAX_SERVER_ERRORS", 2
        ), patch("modules.search.crawler._request_with_safe_redirects", side_effect=error) as request:
            search._profile_external_websites("https://fair.example/profile/1")
            search._profile_external_websites("https://fair.example/profile/2")
            search._profile_external_websites("https://fair.example/profile/3")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(search._source_health_snapshot("https://fair.example")["circuit_open"])

    def test_identity_crawl_skips_sitemaps_and_contact_bruteforce(self):
        def fake_fetch(url):
            if url == "https://example.com":
                return '<a href="/about">About us</a>', None
            if url.endswith("/about"):
                return "Example Makina company profile", None
            return None, "http_404"

        with patch.object(config, "CRAWL_CACHE_MODE", "off"), patch(
            "modules.crawler._try_fetch", side_effect=fake_fetch
        ) as fetch, patch(
            "modules.crawler._robots_and_sitemaps", side_effect=AssertionError("identity crawl used sitemap")
        ):
            result = crawler.fetch_site("https://example.com", profile="identity")
        self.assertEqual(result["crawl_profile"], "identity")
        self.assertLessEqual(fetch.call_count, 1 + config.MAX_IDENTITY_PAGES)

    def test_stage_metrics_separate_candidate_selection_and_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            actual = root / "actual.xlsx"
            candidates = root / "candidates.xlsx"
            self._write_book(
                expected,
                ["Company", "Expected Website", "Expected Email", "Expected Phone"],
                [["Example", "example.com", "info@example.com", "+90 212 555 00 00"]],
                "Manual Report",
            )
            self._write_book(
                actual, ["company", "website", "email", "phone"],
                [["Example", "", "", ""]],
            )
            self._write_book(
                candidates,
                ["company", "selected_website", "candidate_1_url", "candidate_2_url", "candidate_3_url"],
                [["Example", "https://wrong.example", "https://wrong.example", "https://example.com", ""]],
            )
            stages = evaluate_stages(expected, actual, candidates)
        self.assertEqual(stages["candidate_recall_at_1"], 0.0)
        self.assertEqual(stages["candidate_recall_at_3"], 1.0)
        self.assertEqual(stages["selection_accuracy"], 0.0)
        self.assertEqual(stages["abstention_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
