import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from openpyxl import Workbook

import config
import main
from modules import crawler, identity, scorer, search
from validate_golden_xlsx import evaluate_stages


class GeneralImprovementPackageTests(unittest.TestCase):
    def test_verified_cross_domain_mailbox_creates_only_discovery_alias(self) -> None:
        evaluation = {
            "crawl_result": {"url": "https://brand.com.tr"},
            "email": "info@parentholding.com",
            "email_verification": "verified",
            "identity_assessment": {
                "support_keys": ["first_party_identity"], "conflicts": [],
            },
        }
        aliases = main._first_party_contact_alias_candidates(
            "Brand Makine A.S.", [evaluation], {"brand.com.tr"},
        )
        self.assertEqual([item["domain"] for item in aliases], ["parentholding.com"])
        self.assertIn("discovery_only_not_identity_authority", aliases[0]["reason"])
        self.assertEqual(aliases[0]["_first_party_alias_evidence"], 1)

    def test_discovery_only_alias_never_outranks_a_direct_candidate(self) -> None:
        direct = {
            "url": "https://brand.com.tr", "domain": "brand.com.tr",
            "score": 70, "role": "unknown", "reason": "search_text_identity:1/2",
        }
        alias = {
            "url": "https://parentholding.com", "domain": "parentholding.com",
            "score": 92, "role": "company_candidate",
            "reason": "first_party_alias; discovery_only_not_identity_authority",
        }
        self.assertEqual(search.rank_candidates([alias, direct])[0], direct)

    def test_public_mailbox_domain_never_creates_company_alias(self) -> None:
        evaluation = {
            "crawl_result": {"url": "https://brand.com.tr"},
            "email": "brand@gmail.com",
            "email_verification": "verified",
            "identity_assessment": {
                "support_keys": ["first_party_identity"], "conflicts": [],
            },
        }
        self.assertEqual(
            main._first_party_contact_alias_candidates(
                "Brand Makine A.S.", [evaluation], {"brand.com.tr"},
            ),
            [],
        )

    @staticmethod
    def _book(path: Path, headers: list[str], rows: list[list], title: str = "Sheet") -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = title
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(path)

    def test_permanent_404_is_not_retried(self) -> None:
        response = Mock(status_code=404)
        error = requests.HTTPError("not found", response=response)
        with patch("modules.crawler._request_with_safe_redirects", side_effect=error) as request, patch(
            "modules.utils.time.sleep"
        ):
            with self.assertRaises(requests.HTTPError):
                crawler._fetch("https://example.com/missing")
        self.assertEqual(request.call_count, 1)

    def test_transient_500_is_retried(self) -> None:
        response = Mock(status_code=500)
        error = requests.HTTPError("server error", response=response)
        with patch("modules.crawler._request_with_safe_redirects", side_effect=error) as request, patch(
            "modules.utils.time.sleep"
        ):
            with self.assertRaises(requests.HTTPError):
                crawler._fetch("https://example.com")
        self.assertEqual(request.call_count, config.MAX_RETRIES + 1)

    def test_contact_attempt_cap_counts_dead_paths(self) -> None:
        def fake_fetch(url: str):
            if url == "https://example.com":
                return "<html>Example</html>", None
            return None, "http_404"

        with patch("modules.crawler._try_fetch", side_effect=fake_fetch) as fetch, patch(
            "modules.crawler._robots_and_sitemaps", return_value=(None, [])
        ), patch.object(config, "MAX_CONTACT_ATTEMPTS", 3), patch.object(
            config, "ENABLE_JS_FALLBACK", False
        ):
            crawler._fetch_site_live("https://example.com")
        self.assertLessEqual(fetch.call_count, 1 + 3)

    def test_replay_reads_legacy_full_crawl_key_without_contact_seed(self) -> None:
        cached = {
            "url": "https://example.com", "pages": [{"url": "https://example.com", "html": "Example"}],
            "error": "", "tls_insecure": False,
        }

        def load(_directory, _namespace, key, _ttl, _schema):
            return cached if "|attempts=" not in key else None

        with patch.object(config, "CRAWL_CACHE_MODE", "replay"), patch(
            "modules.crawler.cache_store.load", side_effect=load
        ), patch("modules.crawler._fetch_site_live", side_effect=AssertionError("live crawl")):
            result = crawler.fetch_site("https://example.com")
        self.assertEqual(result["pages"], cached["pages"])

    def test_replay_reads_older_schema_legacy_key_without_contact_seed(self) -> None:
        cached = {
            "url": "https://example.com", "pages": [{"url": "https://example.com", "html": "Example"}],
            "error": "", "tls_insecure": False,
        }

        def load(_directory, _namespace, key, _ttl, schema):
            is_legacy = "|attempts=" not in key
            return cached if is_legacy and schema == config.CRAWL_CACHE_SCHEMA_VERSION - 1 else None

        with patch.object(config, "CRAWL_CACHE_MODE", "replay"), patch(
            "modules.crawler.cache_store.load", side_effect=load
        ), patch("modules.crawler._fetch_site_live", side_effect=AssertionError("live crawl")):
            result = crawler.fetch_site("https://example.com")
        self.assertEqual(result["pages"], cached["pages"])

    def test_identity_cache_key_does_not_depend_on_contact_seeds(self) -> None:
        seen_keys = []
        cached = {
            "url": "https://example.com", "pages": [{"url": "https://example.com", "html": "Example"}],
            "error": "", "tls_insecure": False, "crawl_profile": "identity",
        }

        def load(_directory, _namespace, key, _ttl, _schema):
            seen_keys.append(key)
            return cached

        with patch.object(config, "CRAWL_CACHE_MODE", "replay"), patch(
            "modules.crawler.cache_store.load", side_effect=load
        ):
            crawler.fetch_site("https://example.com", ["https://example.com/contact"], profile="identity")
        self.assertTrue(seen_keys)
        self.assertNotIn("seeds=", seen_keys[0])

    def test_shared_structural_host_becomes_discovery_only(self) -> None:
        search.reset_candidate_host_observations()
        first = search._candidate_role(
            "Alpha Makina", "https://catalog.example/company/alpha", "Alpha company profile", "supplier profile"
        )
        second = search._candidate_role(
            "Beta Kimya", "https://catalog.example/company/beta", "Beta company profile", "supplier profile"
        )
        self.assertIn(first, {"directory", "unknown"})
        self.assertIn(second, {"directory", "shared_listing"})
        self.assertIn(second, identity.EXCLUDED_ROLES)

    def test_shared_plain_corporate_root_is_not_assumed_to_be_a_directory(self) -> None:
        search.reset_candidate_host_observations()
        search._candidate_role("Alpha", "https://holding.example", "Alpha", "Official brand")
        second = search._candidate_role("Beta", "https://holding.example", "Beta", "Official brand")
        self.assertNotEqual(second, "shared_listing")

    def test_structural_company_listing_is_directory_on_first_company(self) -> None:
        search.reset_candidate_host_observations()
        role = search._candidate_role(
            "Aden Medikal",
            "https://health.example/tr/company/view/aden-medikal",
            "Aden Medikal",
            "Firmalar. Anasayfa / Firmalar / Aden Medikal",
        )
        self.assertEqual(role, "directory")

    def test_discovery_only_role_cannot_regress_when_search_hits_merge(self) -> None:
        search.reset_candidate_host_observations()
        candidates = {}
        search._add_search_results(
            candidates, "Aden Medikal", "aden medikal resmi sitesi",
            [{
                "href": "https://health.example/tr/company/view/aden-medikal",
                "title": "Aden Medikal", "body": "Firmalar. Anasayfa / Firmalar / Aden Medikal",
            }],
        )
        search._add_search_results(
            candidates, "Aden Medikal", "aden medikal contact",
            [{
                "href": "https://health.example/",
                "title": "Aden Medikal", "body": "Aden Medikal contact information",
            }],
        )
        self.assertEqual(candidates["health.example"]["role"], "directory")

    def test_labelled_directory_website_is_discovery_only_bridge(self) -> None:
        candidates = {}
        search._add_search_results(
            candidates,
            "Example Medical Technologies Limited Sirketi",
            "example medical resmi sitesi",
            [{
                "href": "https://directory.example/company/example-medical",
                "title": "Example Medical Technologies Limited Sirketi",
                "body": "Firma profili. Web Sitesi: https://public-alias.com.tr",
            }],
        )
        self.assertEqual(candidates["directory.example"]["role"], "directory")
        self.assertIn("public-alias.com.tr", candidates)
        candidate = candidates["public-alias.com.tr"]
        self.assertEqual(candidate["query"], "snippet_outbound_discovery")
        self.assertIn("discovery_only_not_identity_authority", candidate["reason"])
        assessment = identity.assess(
            "Example Medical Technologies Limited Sirketi", candidate, [],
        )
        self.assertFalse(assessment["publishable"])

    def test_strong_first_party_bundle_requires_unique_candidate_confirmation(self) -> None:
        candidate = {
            "url": "https://publicalias.com.tr",
            "role": "company_candidate",
            "reason": "search_text_identity:1/2",
            "_identity_company": "Different Legal Medikal",
        }
        reasons = [
            "page_identity_strong:2/2",
            "structured_identity_strong:2/2",
            "country_identity_tr_tld",
            "metadata_context_missing:0/1",
            "tls_insecure_transport",
        ]
        assessment = identity.assess("Different Legal Medikal", candidate, reasons)
        self.assertFalse(assessment["publishable"])
        self.assertTrue(assessment["strong_first_party_bundle"])
        self.assertFalse(main._has_trusted_website_evidence(candidate, reasons))
        self.assertTrue(main._has_trusted_website_evidence(candidate, reasons, unique_candidate=True))

    def test_hard_owner_conflict_blocks_strong_bundle(self) -> None:
        candidate = {
            "url": "https://brand.com.tr", "role": "company_candidate",
            "reason": "search_text_identity:1/1",
        }
        reasons = [
            "page_identity_strong:1/1", "structured_identity_unmatched:0/1",
            "legal_name_phrase_match:1", "country_identity_tr_tld",
        ]
        assessment = identity.assess("Brand", candidate, reasons, {"names": ["Different Owner AS"]})
        self.assertFalse(assessment["provisionally_publishable"])
        self.assertFalse(main._has_trusted_website_evidence(candidate, reasons, unique_candidate=True))

    def test_single_word_page_and_phrase_are_not_two_strong_identity_facts(self) -> None:
        candidate = {
            "url": "https://unrelated.com", "role": "unknown",
            "reason": "search_text_identity:1/1",
        }
        reasons = [
            "page_identity_strong:1/1", "structured_identity_absent",
            "legal_name_phrase_match:1", "country_identity_tr_phone",
        ]
        assessment = identity.assess("Singlebrand", candidate, reasons)
        self.assertFalse(assessment["strong_first_party_bundle"])
        self.assertFalse(main._has_trusted_website_evidence(candidate, reasons, unique_candidate=True))

    def test_long_legal_name_does_not_dilute_repeated_public_brand(self) -> None:
        company = (
            "COLEMED PHARMA ULUSLARARASI SAGLIK URUNLERI ITHALAT "
            "IHRACAT SANAYI TICARET LIMITED SIRKETI"
        )
        pages = [
            {"url": "https://colemedpharma.example", "html": "Colemed Pharma urunleri"},
            {"url": "https://colemedpharma.example/contact", "html": "Colemed Pharma iletisim"},
        ]
        score, reason = main._page_identity_score(company, pages)
        self.assertEqual(score, 14)
        self.assertTrue(reason.startswith("page_identity_strong:2/2@scope=public_brand"))

    def test_long_anchored_public_brand_domain_is_general_identity_evidence(self) -> None:
        self.assertTrue(scorer.public_brand_domain_match(
            "TRIOCARE MOBILYA TEKNOLOJI SISTEMLERI IMALAT SANAYI TICARET LIMITED SIRKETI",
            "https://triocare.com.tr",
        ))
        self.assertTrue(scorer.public_brand_domain_match(
            "COLEMED PHARMA ULUSLARARASI SAGLIK URUNLERI LIMITED SIRKETI",
            "https://colemedpharma.com",
        ))
        self.assertFalse(scorer.public_brand_domain_match(
            "KALKAN KOMPOZIT PLASTIK GERI DONUSUM SANAYI TICARET LIMITED SIRKETI",
            "https://kalkan.com.tr",
        ))

    def test_primary_only_public_brand_domain_needs_onsite_email_corroboration(self) -> None:
        candidate = {
            "url": "https://triocare.com.tr", "role": "company_candidate",
            "reason": "search_text_identity:1/6",
        }
        base_reasons = [
            "page_identity_medium:1/2@scope=public_brand,pages=2",
            "structured_identity_medium:1/2@scope=public_brand_partial",
            "country_identity_tr_tld",
        ]
        without_email = identity.assess(
            "TRIOCARE MOBILYA TEKNOLOJI SISTEMLERI LIMITED SIRKETI",
            candidate, base_reasons,
        )
        with_email = identity.assess(
            "TRIOCARE MOBILYA TEKNOLOJI SISTEMLERI LIMITED SIRKETI",
            candidate, [*base_reasons, "email_domain_match"],
        )
        self.assertFalse(without_email["publishable"])
        self.assertTrue(with_email["publishable"])

    def test_legal_name_activity_absence_is_neutral_not_context_conflict(self) -> None:
        score, reason = main._page_context_score(
            "STERILMED MEDICAL ELEKTRIK ELEKTRONIK OTOMASYON GIDA SANAYI",
            [{"url": "https://sterilmed.example", "html": "Sterilmed tibbi urunler"}],
        )
        self.assertEqual(score, 0)
        self.assertEqual(reason, "context_name_not_observed:0/2")

    def test_partial_structured_public_brand_is_not_a_bundle_component(self) -> None:
        candidate = {
            "url": "https://longpublicbrand.com.tr", "role": "unknown",
            "reason": "search_text_identity:1/2",
        }
        reasons = [
            "page_identity_strong:2/2@scope=public_brand,pages=2",
            "structured_identity_medium:1/2@scope=public_brand_partial",
            "country_identity_tr_tld",
        ]
        assessment = identity.assess("Longpublicbrand Different", candidate, reasons)
        self.assertEqual(assessment["first_party_bundle_components"], 1)
        self.assertFalse(assessment["strong_first_party_bundle"])

    def test_stage_denominators_exclude_explicit_unknown_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            actual = root / "actual.xlsx"
            candidates = root / "candidates.xlsx"
            headers = [
                "Company", "Expected Website", "Expected Email", "Expected Phone",
                "Website Verified", "Email Verified", "Phone Verified",
            ]
            self._book(expected, headers, [
                ["Asserted", "asserted.com", "info@asserted.com", "+90 212 555 00 00", "yes", "yes", "yes"],
                ["Unknown", "unknown.com", "", "", "unknown", "unknown", "unknown"],
            ], "Manual Report")
            self._book(actual, ["company", "website", "email", "phone"], [
                ["Asserted", "", "", ""], ["Unknown", "https://wrong.com", "", ""],
            ])
            self._book(candidates, [
                "company", "selected_website", "candidate_1_url", "candidate_2_url", "candidate_3_url",
            ], [
                ["Asserted", "https://asserted.com", "https://asserted.com", "", ""],
                ["Unknown", "https://wrong.com", "https://wrong.com", "", ""],
            ])
            stages = evaluate_stages(expected, actual, candidates)
        self.assertEqual(stages["expected_websites"], 1)
        self.assertEqual(stages["website_asserted_rows"], 1)
        self.assertEqual(stages["website_unknown_rows"], 1)
        self.assertEqual(stages["selection_accuracy"], 1.0)
        self.assertEqual(stages["abstention_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
