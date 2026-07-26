import sys
import types
import unittest
from unittest.mock import Mock, patch

import requests

import config
import main
from modules import crawler, evidence_ledger, extractor, query_planner, search, utils
from modules.contact_decision import rank_email_records, rank_phone_records


class AdaptivePlannerP2Tests(unittest.TestCase):
    def test_planner_only_emits_query_families_for_open_gaps(self):
        queries = query_planner.adaptive_queries(
            "ORBITA MAKINE LIMITED SIRKETI",
            {
                "representations": "Atlas Robotics",
                "listed_address": "Konya, Turkiye",
                "sector": "otomasyon",
            },
            evidence_gaps={"missing_legal_name"},
        )
        self.assertTrue(any("kvkk" in value for value in queries))
        self.assertTrue(any("ticari unvan" in value for value in queries))
        self.assertFalse(any("distributor" in value for value in queries))
        self.assertFalse(any("iletisim" in value for value in queries))

    def test_short_brand_single_result_stays_ambiguous(self):
        gaps = search._adaptive_discovery_gaps(
            "NOVA",
            {
                "nova.example": {
                    "url": "https://nova.example",
                    "role": "company_candidate",
                    "score": 80,
                    "reason": "domain_hits:1/1",
                    "_legal_name_evidence": 1,
                    "_metadata_context_matches": 1,
                }
            },
        )
        self.assertIn("ambiguous_candidates", gaps)

    def test_long_public_brand_domain_outranks_legal_name_listing(self):
        ranked = search.rank_candidates([
            {
                "url": "https://listing.example", "score": 92, "role": "unknown",
                "reason": "search_text_identity:5/6", "_public_brand_domain": False,
            },
            {
                "url": "https://orbitacare.example", "score": 78, "role": "unknown",
                "reason": "search_text_identity:1/6", "_public_brand_domain": True,
            },
        ])
        self.assertEqual(ranked[0]["url"], "https://orbitacare.example")

    def test_labelled_pdf_domain_adds_bounded_hyphenation_variant_as_discovery_only(self):
        candidates = {}
        search._add_snippet_outbound_candidates(
            candidates,
            "ORBITA CARE MEDIKAL LIMITED SIRKETI",
            '"orbita care" official website',
            1,
            {
                "href": "https://registry.example/catalog.pdf",
                "title": "Orbita Care Medikal",
                "body": "Orbita Care Medikal Limited Sirketi Web Site: orbita-care.com",
            },
        )
        self.assertIn("orbita-care.com", candidates)
        self.assertIn("orbitacare.com", candidates)
        variant = candidates["orbitacare.com"]
        self.assertEqual(variant["_domain_variant_of"], "orbita-care.com")
        self.assertIn("discovery_only_not_identity_authority", variant["reason"])
        self.assertLessEqual(len(candidates), 2)

    def test_explicit_profile_website_bridge_outranks_unknown_listing_but_stays_discovery_only(self):
        bridge = {
            "url": "https://orbita.example", "score": 70,
            "role": "company_candidate", "query": "search_bridge_profile",
            "reason": "labelled_search_bridge_outbound_discovery; discovery_only_not_identity_authority",
        }
        listing = {
            "url": "https://listing.example", "score": 92,
            "role": "unknown", "query": "brand official website",
            "reason": "search_text_identity:5/6",
        }
        self.assertIs(search.rank_candidates([listing, bridge])[0], bridge)
        self.assertIn("discovery_only_not_identity_authority", bridge["reason"])


class ContactDecisionP2Tests(unittest.TestCase):
    def test_extractor_keeps_service_number_and_ranking_beats_whatsapp(self):
        html = (
            "<p>Cagri Merkezi: 444 21 32</p>"
            '<a href="https://wa.me/905327355347">WhatsApp</a>'
        )
        records = extractor.extract_contact_records(
            html, "https://orbita.example/iletisim", "browser_render",
        )["phones"]
        ranked = rank_phone_records(records)
        self.assertEqual(ranked[0]["value"], "04442132")
        self.assertEqual(ranked[0]["retrieval_method"], "browser_render")
        self.assertEqual(ranked[1]["label"], "whatsapp")

    def test_email_ranking_aggregates_sources_and_rejects_unrelated_global_mailbox(self):
        records = [
            {
                "value": "sales@orbita.com.tr", "label": "sales",
                "source_url": "https://orbita.com.tr", "retrieval_method": "http",
            },
            {
                "value": "sales@orbita.com.tr", "label": "sales",
                "source_url": "https://orbita.com.tr/iletisim", "retrieval_method": "browser_render",
            },
            {
                "value": "info@global-hq.example", "label": "general",
                "source_url": "https://orbita.com.tr/iletisim", "retrieval_method": "http",
            },
            {
                "value": "privacy@orbita.com.tr", "label": "privacy",
                "source_url": "https://orbita.com.tr/privacy", "retrieval_method": "http",
            },
        ]
        ranked = rank_email_records(
            "ORBITA MAKINE", "https://orbita.com.tr", records, main._email_is_usable,
        )
        self.assertEqual([item["value"] for item in ranked], ["sales@orbita.com.tr"])
        self.assertEqual(ranked[0]["source_url"], "https://orbita.com.tr/iletisim")
        self.assertEqual(ranked[0]["observation_count"], 2)
        self.assertEqual(
            set(ranked[0]["retrieval_methods"]), {"http", "browser_render"},
        )

    def test_invalid_top_email_falls_through_to_next_ranked_mailbox(self):
        crawl_result = {
            "url": "https://orbita.example",
            "pages": [{
                "url": "https://orbita.example/contact",
                "html": (
                    '<a href="mailto:sales@orbita.example">Sales</a>'
                    '<a href="mailto:info@orbita.example">Info</a>'
                ),
                "retrieval_method": "http",
            }],
            "error": "",
        }
        verification = [
            {"status": "invalid_domain", "reason": "mx_missing"},
            {"status": "valid", "reason": "mx_found"},
        ]
        with patch("main.crawler.fetch_site", return_value=crawl_result), patch(
            "main.email_verifier.verify_email", side_effect=verification,
        ) as verify, patch(
            "main._score_candidate_with_site", return_value=(80, []),
        ), patch(
            "main._structured_identity_score", return_value=(0, [], {}),
        ), patch(
            "main.identity.assess",
            return_value={"support_count": 2, "decision": "verified"},
        ):
            result = main._evaluate_candidate(
                "ORBITA", {"url": "https://orbita.example", "reason": ""},
            )
        self.assertEqual(result["email"], "info@orbita.example")
        self.assertEqual(result["email_verification"], "valid")
        self.assertEqual(verify.call_count, 2)

    def test_operational_contact_overlap_does_not_resolve_close_identity_ambiguity(self):
        def evaluation(url, email):
            return {
                "candidate": {"url": url, "query": "brand official website"},
                "email": email,
                "phone": "02125550000",
                "structured_identity": {},
                "identity_assessment": {
                    "provisionally_publishable": True,
                    "support_count": 2,
                    "conflicts": [],
                },
                "final_score": 95,
            }

        corporate = evaluation("https://brand.com.tr", "info@brand.com.tr")
        shop = evaluation("https://brandshop.com", "sales@brand.com.tr")
        self.assertTrue(main._same_official_family(corporate, shop, "Brand"))
        self.assertTrue(main._close_identity_margin_conflict("Brand", corporate, shop))


class RecoveryAndProvenanceP2Tests(unittest.TestCase):
    def test_render_policy_blocks_cross_site_active_data_but_allows_passive_asset(self):
        with patch(
            "modules.crawler.network_guard.validate_public_http_url",
            return_value=(True, "public"),
        ):
            self.assertEqual(
                crawler._render_request_policy(
                    "orbita.example", "https://api.other.example/contacts", "fetch",
                ),
                (False, "cross_site_active_data"),
            )
            self.assertEqual(
                crawler._render_request_policy(
                    "orbita.example", "https://cdn.other.example/app.js", "script",
                ),
                (True, "allowed"),
            )
            self.assertEqual(
                crawler._render_request_policy(
                    "orbita.example", "https://api.orbita.example/contacts", "xhr",
                ),
                (True, "allowed"),
            )

    def test_retry_after_header_controls_bounded_backoff(self):
        response = Mock(status_code=429, headers={"Retry-After": "7"})
        error = requests.exceptions.HTTPError(response=response)
        attempts = iter([error, "ok"])

        @utils.retry_with_backoff(max_retries=1, retry_if=lambda exc: True)
        def operation():
            value = next(attempts)
            if isinstance(value, Exception):
                raise value
            return value

        with patch("modules.utils.time.sleep") as sleep:
            self.assertEqual(operation(), "ok")
        sleep.assert_called_once_with(7.0)

    def test_short_native_pdf_uses_bounded_ocr_and_records_method(self):
        response = Mock(content=b"%PDF fixture")
        page = Mock()
        page.extract_text.return_value = "x"
        fake_pypdf = types.SimpleNamespace(PdfReader=lambda stream: types.SimpleNamespace(pages=[page]))
        with patch.dict(sys.modules, {"pypdf": fake_pypdf}), patch(
            "modules.crawler._request_with_safe_redirects", return_value=response,
        ), patch(
            "modules.crawler._try_ocr_pdf", return_value=("OCR contact text", None),
        ) as ocr:
            text, error = crawler._try_extract_pdf("https://orbita.example/catalog.pdf")
        self.assertIsNone(error)
        self.assertIn("OCR contact text", text)
        self.assertEqual(crawler._DOCUMENT_STATE.last["retrieval_method"], "pdf_ocr")
        ocr.assert_called_once_with(response.content)

    def test_replay_cache_marks_missing_historical_provenance_without_mutating_pages(self):
        cached = {
            "url": "https://orbita.example",
            "pages": [{"url": "https://orbita.example", "html": "Official company"}],
        }
        with patch.object(config, "CRAWL_CACHE_MODE", "replay"), patch(
            "modules.crawler.cache_store.load", return_value=cached,
        ):
            result = crawler.fetch_site("https://orbita.example")
        self.assertEqual(result["pages"], cached["pages"])
        self.assertEqual(result["provenance_status"], "legacy_cache_unknown")

    def test_evidence_ledger_distinguishes_selected_contact(self):
        claims = evidence_ledger.evaluation_claims({
            "email": "sales@orbita.example",
            "email_source_url": "https://orbita.example/contact",
            "email_retrieval_method": "browser_render",
            "email_selection_reason": "role=sales;scope=contact",
            "alternative_email_records": [{
                "value": "info@orbita.example",
                "source_url": "https://orbita.example",
                "retrieval_method": "http",
            }],
        })
        selected = next(item for item in claims if item["value"] == "sales@orbita.example")
        alternative = next(item for item in claims if item["value"] == "info@orbita.example")
        self.assertTrue(selected["selected"])
        self.assertFalse(alternative["selected"])
        self.assertEqual(selected["retrieval_method"], "browser_render")


if __name__ == "__main__":
    unittest.main()
