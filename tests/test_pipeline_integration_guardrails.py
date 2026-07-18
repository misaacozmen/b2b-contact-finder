import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import config
import main
from modules import crawler, runtime, search


def _evaluation(candidate: dict, final_score: int, profile: str = "identity") -> dict:
    reasons = [
        "page_identity_strong:2/2",
        "structured_identity_strong:2/2",
        "country_identity_tr_tld",
    ]
    return {
        "candidate": candidate,
        "crawl_result": {
            "url": candidate["url"],
            "pages": [{"url": candidate["url"], "html": "Example Brand"}],
            "error": "",
            "crawl_profile": profile,
        },
        "email": "", "email_source": "", "email_source_url": "",
        "alternative_emails": [], "email_verification": "not_checked",
        "email_verification_reason": "identity_phase" if profile == "identity" else "no_email",
        "phone": "", "phone_source": "", "phone_source_url": "",
        "phone_label": "", "alternative_phones": [],
        "final_score": final_score, "reasons": reasons,
        "has_contact": False, "context_failed": False, "email_failed": False,
        "structured_identity": {"names": ["Example Brand"], "urls": [], "same_as": []},
    }


class PipelineIntegrationGuardrailTests(unittest.TestCase):
    def test_fifteen_profiles_on_one_failing_host_make_only_two_requests(self):
        records = [
            {"profile_url": f"https://fair.example/company/{index}"}
            for index in range(15)
        ]
        response = Mock(status_code=500)
        error = requests.HTTPError("server error", response=response)
        runtime.reset()
        search.reset_source_health()
        with patch.object(config, "SEARCH_CACHE_MODE", "off"), patch.object(
            config, "SOURCE_PROFILE_MAX_SERVER_ERRORS", 2
        ), patch(
            "modules.search.crawler._request_with_safe_redirects", side_effect=error
        ) as request:
            search.preflight_source_profiles(records)
            for record in records:
                search._profile_external_websites(record["profile_url"])
        counters = runtime.snapshot()["counters"]
        self.assertEqual(request.call_count, 2)
        self.assertEqual(counters.get("source_profile.http_5xx"), 2)
        self.assertEqual(counters.get("source_profile.circuit_skips"), 14)

    def test_eight_light_evaluations_lead_to_at_most_three_full_crawls(self):
        candidates = search.CandidateList([
            {
                "url": f"https://candidate{index}.com.tr",
                "score": 92 - index,
                "query": "official website",
                "reason": "domain_hits:2/2",
                "role": "company_candidate",
            }
            for index in range(8)
        ])
        calls: list[str] = []

        def evaluate(company, candidate, metadata=None, crawl_profile="full", verify_email_domain=True):
            calls.append(crawl_profile)
            index = int(candidate["url"].split("candidate", 1)[1].split(".", 1)[0])
            return _evaluation(candidate, 100 - index * 7, crawl_profile)

        with patch("main.search.find_candidate_domains", return_value=candidates), patch(
            "main._evaluate_candidate", side_effect=evaluate
        ), patch("main.random_delay"):
            main.process_company(0, "Example Brand", Mock())
        self.assertEqual(calls.count("identity"), 8)
        self.assertLessEqual(calls.count("full"), config.MAX_FULL_CANDIDATE_EVALUATIONS)
        self.assertEqual(calls.count("full"), 3)

    def test_identity_phase_never_extracts_contacts_or_checks_mx(self):
        candidate = {
            "url": "https://example.com.tr", "score": 90,
            "query": "official website", "reason": "domain_hits:1/1",
            "role": "company_candidate",
        }
        crawl = {
            "url": candidate["url"],
            "pages": [{"url": candidate["url"], "html": "Example info@example.com.tr +90 212 555 00 00"}],
            "error": "", "tls_insecure": False,
        }
        with patch("main.crawler.fetch_site", return_value=crawl), patch(
            "main.extractor.extract_contact_records", side_effect=AssertionError("contact extraction in identity phase")
        ), patch(
            "main.email_verifier.verify_email", side_effect=AssertionError("MX check in identity phase")
        ):
            result = main._evaluate_candidate(
                "Example", candidate, crawl_profile="identity", verify_email_domain=False,
            )
        self.assertEqual(result["email"], "")
        self.assertEqual(result["phone"], "")

    def test_replay_misses_cannot_reach_search_crawl_or_dns_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            config, "SEARCH_CACHE_DIR", Path(directory) / "search"
        ), patch.object(
            config, "CRAWL_CACHE_DIR", Path(directory) / "crawl"
        ), patch.object(
            config, "EMAIL_CACHE_DIR", Path(directory) / "email"
        ), patch.object(config, "SEARCH_CACHE_MODE", "replay"), patch.object(
            config, "CRAWL_CACHE_MODE", "replay"
        ), patch(
            "modules.search._search_text_live", side_effect=AssertionError("live search")
        ), patch(
            "modules.crawler._fetch_site_live", side_effect=AssertionError("live crawl")
        ), patch(
            "modules.email_verifier._domain_mx_status", side_effect=AssertionError("live DNS")
        ):
            self.assertEqual(search._search_text("missing query"), [])
            self.assertEqual(crawler.fetch_site("https://missing.example")["pages"], [])
            from modules import email_verifier
            self.assertEqual(email_verifier.verify_email("info@missing.example")["status"], "not_checked")

    def test_ambiguous_result_cannot_leak_contacts_to_primary_output(self):
        first = {
            "url": "https://example.com", "score": 92, "query": "official website",
            "reason": "domain_hits:1/1", "role": "company_candidate",
        }
        second = {
            "url": "https://example.com.tr", "score": 91, "query": "official website",
            "reason": "domain_hits:1/1", "role": "company_candidate",
        }
        candidates = search.CandidateList([first, second])

        def evaluate(company, candidate, metadata=None, crawl_profile="full", verify_email_domain=True):
            item = _evaluation(candidate, 95, crawl_profile)
            domain = candidate["url"].split("://", 1)[1]
            item.update({
                "email": f"info@{domain}", "email_source": "website",
                "email_source_url": candidate["url"], "phone": "02125550000",
                "phone_source": "website", "phone_source_url": candidate["url"],
                "has_contact": True,
            })
            return item

        with patch("main.search.find_candidate_domains", return_value=candidates), patch(
            "main._evaluate_candidate", side_effect=evaluate
        ), patch("main.random_delay"):
            _, row = main.process_company(0, "Example", Mock())
        self.assertEqual(row["status"], "WEBSITE_AMBIGUOUS")
        self.assertEqual(row["website"], "")
        self.assertEqual(row["email"], "")
        self.assertEqual(row["phone"], "")

    def test_shared_phone_alone_does_not_join_domains(self):
        first = {
            "candidate": {"url": "https://brand.com.tr"},
            "structured_identity": {}, "phone": "02125550000", "email": "info@brand.com.tr",
        }
        second = {
            "candidate": {"url": "https://brandshop.com"},
            "structured_identity": {}, "phone": "02125550000", "email": "shop@brandshop.com",
        }
        evidence = main._official_family_evidence(first, second, "Brand")
        self.assertFalse(evidence["related"])
        self.assertIn("shared_phone", evidence["edges"])

    def test_different_legal_identifiers_override_other_family_edges(self):
        first = {
            "candidate": {"url": "https://brand.com.tr"},
            "structured_identity": {
                "identifiers": ["taxID:111"], "same_as": ["https://brand.com"], "urls": [],
            },
        }
        second = {
            "candidate": {"url": "https://brand.com"},
            "structured_identity": {"identifiers": ["taxID:222"], "same_as": [], "urls": []},
        }
        evidence = main._official_family_evidence(first, second, "Brand")
        self.assertFalse(evidence["related"])
        self.assertIn("different_legal_identifiers", evidence["conflicts"])

    def test_cached_security_interstitial_is_removed_in_replay(self):
        cached = {
            "url": "https://blocked.example",
            "pages": [{
                "url": "https://blocked.example",
                "html": "<html>T-Mobile: Uwaga! Ta strona stanowi zagrozenie!</html>",
            }],
            "error": "", "tls_insecure": False,
        }
        with patch.object(config, "CRAWL_CACHE_MODE", "replay"), patch(
            "modules.crawler.cache_store.load", return_value=cached
        ), patch(
            "modules.crawler._fetch_site_live", side_effect=AssertionError("live crawl")
        ):
            result = crawler.fetch_site("https://blocked.example")
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["error"], "cached_security_interstitial")


if __name__ == "__main__":
    unittest.main()
