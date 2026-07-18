import unittest
from unittest.mock import Mock, patch

import requests

import config
import main
from modules import identity, scorer, search


class DiscoveryV2HoldoutTests(unittest.TestCase):
    """Synthetic, Golden-independent discovery cases kept as a fixed holdout."""

    def setUp(self):
        search.reset_source_health()
        search.reset_candidate_host_observations()

    def test_adaptive_plan_targets_brand_legal_name_and_disclosure_pages(self):
        queries = search._adaptive_queries(
            "NOVA MAKİNE SANAYİ VE TİCARET LİMİTED ŞİRKETİ",
            {"sector": "Industrial machinery", "description": ""},
        )
        self.assertIn('"nova" Turkiye resmi sitesi', queries)
        self.assertTrue(any("web sitesi" in query for query in queries))
        self.assertTrue(any("kvkk" in query for query in queries))

    def test_high_scoring_short_brand_homonym_triggers_adaptive_search(self):
        seen = []

        def fake_search(query):
            seen.append(query)
            if query == '"nova" Turkiye resmi sitesi':
                return [{
                    "href": "https://novamakine.com.tr/kurumsal",
                    "title": "Nova Makine Sanayi",
                    "body": "Türkiye makine üreticisi",
                }]
            if "official website" in query:
                return [{
                    "href": "https://novaambalaj.com.tr",
                    "title": "Nova Ambalaj",
                    "body": "Oluklu mukavva ambalaj üretimi",
                }]
            return []

        with patch.object(config, "SEARCH_CACHE_MODE", "replay"), patch.object(
            config, "SEARCH_PROVIDER", "ddgs"
        ), patch("modules.search._search_text", side_effect=fake_search), patch(
            "modules.search._profile_external_websites", return_value=[]
        ):
            candidates = search.find_candidate_domains("NOVA MAKİNE")

        self.assertIn('"nova" Turkiye resmi sitesi', seen)
        self.assertIn("novamakine.com.tr", {item["domain"] for item in candidates[:3]})
        self.assertTrue(any(item.get("phase") == "adaptive" for item in candidates.trace))

    def test_labelled_search_profile_link_enters_pool_as_discovery_only(self):
        bridge_sources = {}
        search._collect_search_bridge_sources(
            bridge_sources,
            "ORBITA TEKNOLOJİ",
            "orbita teknoloji Turkiye official website",
            [{
                "href": "https://catalog.example/profile?company=orbita-teknoloji",
                "title": "Orbita Teknoloji company profile",
                "body": "Firma bilgileri ve web sitesi",
            }],
            None,
        )
        candidates = {}
        trace = []
        with patch("modules.search._profile_external_websites", return_value=[{
            "url": "https://orbitabilisim.com.tr",
            "label": "Web Sitesi",
            "explicit_website": True,
            "source_page_url": "https://catalog.example/profile?company=orbita-teknoloji",
            "rendered": True,
        }]):
            search._expand_search_bridge_candidates(
                candidates, "ORBITA TEKNOLOJİ", bridge_sources, trace, set()
            )

        candidate = candidates["orbitabilisim.com.tr"]
        assessment = identity.assess(
            "ORBITA TEKNOLOJİ",
            candidate,
            ["page_identity_strong:2/2", "country_identity_tr_tld"],
            {},
        )
        self.assertEqual(candidate["query"], "search_bridge_profile")
        self.assertIn(
            "search_profile_outbound_link",
            {item["kind"] for item in assessment["neutral"]},
        )
        self.assertNotIn("authority", assessment["support_keys"])

    def test_listing_row_cannot_export_a_different_profile_owners_website(self):
        bridge_sources = {}
        search._collect_search_bridge_sources(
            bridge_sources,
            "VEKTOR TIBBI SISTEMLER SANAYI LIMITED SIRKETI",
            "vektor tibbi sistemler web sitesi",
            [{
                "href": "https://catalog.example/profile?slug=istanbul-medikal",
                "title": "Istanbul Medikal",
                "body": (
                    "VEKTOR TIBBI SISTEMLER SANAYI LIMITED SIRKETI, Istanbul, Turkey. "
                    "Diger katilimcilar ve urunler."
                ),
            }],
            None,
        )
        self.assertEqual(bridge_sources, {})

    def test_profile_url_anchor_can_discover_a_different_official_alias(self):
        bridge_sources = {}
        search._collect_search_bridge_sources(
            bridge_sources,
            "DELTA KOMPOZIT MEDIKAL SANAYI LIMITED SIRKETI",
            "delta kompozit medikal web sitesi",
            [{
                "href": "https://catalog.example/profile?company=delta-kompozit",
                "title": "Delta Kompozit firma profili",
                "body": "Uretici firma bilgileri ve Web Sitesi",
            }],
            None,
        )
        self.assertEqual(len(bridge_sources), 1)
        candidates = {}
        with patch("modules.search._profile_external_websites", return_value=[{
            "url": "https://northstar-motion.com.tr",
            "label": "Web Sitesi",
            "explicit_website": True,
        }]):
            search._expand_search_bridge_candidates(
                candidates,
                "DELTA KOMPOZIT MEDIKAL SANAYI LIMITED SIRKETI",
                bridge_sources,
                [],
                set(),
            )
        self.assertIn("northstar-motion.com.tr", candidates)

    def test_explicit_owner_statement_allows_brand_different_from_legal_name(self):
        self.assertTrue(search._bridge_identity_supported(
            "VEKTOR TEKNOLOJI SANAYI LIMITED SIRKETI",
            "Northstar Robotics",
            "Northstar Robotics, Vektor Teknoloji Sanayi Limited Sirketi markasidir.",
            None,
            "https://catalog.example/company/northstar-robotics",
        ))

    def test_discovery_only_alias_loses_an_identity_tie_to_direct_candidate(self):
        def evaluation(url, reason):
            return {
                "candidate": {
                    "url": url,
                    "query": "vektor resmi sitesi",
                    "reason": reason,
                    "role": "company_candidate",
                    "_official_query_evidence": 0,
                },
                "reasons": [
                    "page_identity_medium:2/4",
                    "structured_identity_weak:1/4",
                    "legal_name_phrase_missing:0/3",
                    "country_identity_tr_phone",
                ],
                "identity_assessment": {
                    "support_count": 1,
                    "provisionally_publishable": False,
                },
                "context_failed": False,
                "email_failed": False,
                "has_contact": True,
                "final_score": 90,
            }

        direct = evaluation("https://target-site.com.tr", "search_text_identity:2/3")
        direct["candidate"]["role"] = "unknown"
        bridge = evaluation(
            "https://other-site.com.tr",
            "labelled_third_party_outbound_discovery; discovery_only_not_identity_authority",
        )
        self.assertGreater(
            main._evaluation_rank_key("VEKTOR SISTEMLER", direct),
            main._evaluation_rank_key("VEKTOR SISTEMLER", bridge),
        )

    def test_legal_name_contact_result_seeds_crawl_from_official_query(self):
        candidates = {}
        search._add_search_results(
            candidates,
            "VEKTOR TIBBI SISTEMLER SANAYI LIMITED SIRKETI",
            "vektor tibbi sistemler Turkiye official website",
            [{
                "href": "https://vektor-store.com.tr/pages/iletisim?source=search",
                "title": "Iletisim",
                "body": "Vektor Tibbi Sistemler Sanayi Limited Sirketi iletisim bilgileri",
            }],
        )
        self.assertEqual(
            candidates["vektor-store.com.tr"]["_contact_seed_urls"],
            ["https://vektor-store.com.tr/pages/iletisim?source=search"],
        )

    def test_generic_contact_result_from_official_query_is_not_a_seed(self):
        candidates = {}
        search._add_search_results(
            candidates,
            "VEKTOR TIBBI SISTEMLER",
            "vektor tibbi sistemler Turkiye official website",
            [{
                "href": "https://vektortibbi.com.tr/iletisim",
                "title": "Iletisim",
                "body": "Bize ulasin",
            }],
        )
        self.assertEqual(candidates["vektortibbi.com.tr"]["_contact_seed_urls"], [])

    def test_multilingual_brand_identity_requires_exact_long_anchor(self):
        self.assertEqual(
            scorer.primary_brand_text_hits(
                "ORBITA ILERI MALZEMELER SANAYI A.S.",
                "Orbita Advanced Materials",
            ),
            (2, 2, 1),
        )
        self.assertEqual(
            scorer.primary_brand_text_hits(
                "NOVA ILERI MALZEMELER SANAYI A.S.",
                "Nova Advanced Materials",
            ),
            (1, 2, 0),
        )

    def test_multilingual_brand_can_form_strong_first_party_bundle(self):
        company = "ORBITA ILERI MALZEMELER SANAYI A.S."
        organization = (
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Organization",'
            '"name":"Orbita Advanced Materials","url":"https://orbita.example"}'
            '</script>'
        )
        pages = [
            {"url": "https://orbita.example", "html": f"<title>Orbita Advanced Materials</title>{organization}"},
            {"url": "https://orbita.example/about", "html": f"<h1>Orbita Advanced Materials</h1>{organization}"},
        ]
        _, page_reason = main._page_identity_score(company, pages)
        _, structured_reason, structured = main._structured_identity_score(company, pages)
        candidate = {
            "url": "https://orbitaadvancedmaterials.com",
            "query": "orbita official website",
            "reason": "search_text_identity:1/3",
            "role": "unknown",
        }
        assessment = identity.assess(
            company,
            candidate,
            [page_reason, structured_reason, "country_identity_tr_text", "no_email"],
            structured,
        )
        self.assertTrue(page_reason.startswith("page_identity_strong:"))
        self.assertTrue(structured_reason.startswith("structured_identity_strong:"))
        self.assertIn("translated=1", page_reason)
        self.assertTrue(assessment["strong_first_party_bundle"])

    def test_numbered_chamber_company_page_is_discovery_only_profile(self):
        role = search._candidate_role(
            "ORBITA TIBBI SISTEMLER A.S.",
            "https://chamber.example/firma-6791-orbita-tibbi.html",
            "ORBITA TIBBI SISTEMLER A.S.",
            "Firma kaydi",
        )
        self.assertEqual(role, "directory")

    def test_registry_facility_name_creates_only_an_adaptive_hint(self):
        company = "ORBITA TIBBI CIHAZLAR URETIM A.S."
        hints = search._related_name_hints(
            company,
            "ORBITA TIBBI CIHAZLAR URETIM A.S.",
            "OSB 4. SK. NOVATEK TIBBI GERECLER SIT. NO 12",
        )
        self.assertEqual(hints, ["novatek tibbi gerecler"])
        queries = search._adaptive_queries(company, None, related_name_hints=hints)
        self.assertEqual(queries[0], '"novatek tibbi gerecler" Turkiye official website')

    def test_related_name_result_is_discovery_only_candidate(self):
        candidates = {}
        search._add_related_hint_results(
            candidates,
            "ORBITA TIBBI CIHAZLAR URETIM A.S.",
            "novatek tibbi gerecler",
            '"novatek tibbi gerecler" Turkiye official website',
            [{
                "href": "https://novatektibbi.com.tr/iletisim",
                "title": "Novatek Tibbi Gerecler",
                "body": "Medikal urunler",
            }],
        )
        candidate = candidates["novatektibbi.com.tr"]
        self.assertEqual(candidate["query"], "related_name_discovery")
        self.assertIn("discovery_only_not_identity_authority", candidate["reason"])
        self.assertEqual(candidate["_official_query_evidence"], 0)

    def test_intrinsic_brand_domain_outranks_legal_name_listing(self):
        listing = {
            "url": "https://catalog.example",
            "score": 23,
            "role": "company_candidate",
            "reason": "domain_hits:0/2; candidate_role:company_candidate",
            "_legal_name_evidence": 1,
        }
        official = {
            "url": "https://novatektibbi.com",
            "score": 84,
            "role": "company_candidate",
            "reason": "domain_hits:1/2; candidate_role:company_candidate",
            "_legal_name_evidence": 0,
        }
        ranked = search.rank_candidates([listing, official])
        self.assertIs(ranked[0], official)

    def test_unlabelled_search_profile_link_cannot_create_candidate(self):
        sources = {
            "https://catalog.example/profile?company=delta": {
                "url": "https://catalog.example/profile?company=delta",
                "domain": "catalog.example",
                "query": "delta",
                "rank": 1,
                "title": "Delta Endüstri",
                "snippet": "Firma profili",
                "role": "directory",
            }
        }
        candidates = {}
        with patch("modules.search._profile_external_websites", return_value=[{
            "url": "https://unrelated.example",
            "label": "Partner",
            "explicit_website": False,
        }]):
            search._expand_search_bridge_candidates(
                candidates, "DELTA ENDÜSTRİ", sources, [], set()
            )
        self.assertEqual(candidates, {})

    def test_js_shell_profile_uses_rendered_explicit_website_field(self):
        response = Mock(
            status_code=200,
            text='<html><body><div id="root"></div><script src="app.js"></script></body></html>',
            url="https://fair.example/exhibitor/orbita",
        )
        response._b2b_final_url = response.url
        rendered = '''
        <html><body><div>Orbita Teknoloji</div>
        <div>Web Sitesi: <a href="https://orbitabilisim.com.tr">Web Sitesi</a></div>
        </body></html>
        '''
        with patch.object(config, "SEARCH_CACHE_MODE", "off"), patch.object(
            config, "ENABLE_JS_PROFILE_FALLBACK", True
        ), patch(
            "modules.search.crawler._request_with_safe_redirects", return_value=response
        ), patch(
            "modules.search.crawler._try_render", return_value=(rendered, None)
        ) as render:
            records = search._profile_external_websites(response.url)

        render.assert_called_once_with(response.url)
        self.assertEqual(records[0]["url"], "https://orbitabilisim.com.tr")
        self.assertTrue(records[0]["explicit_website"])
        self.assertTrue(records[0]["rendered"])

    def test_profile_403_can_render_but_replay_never_renders(self):
        error_response = Mock(status_code=403)
        error = requests.HTTPError("forbidden", response=error_response)
        rendered = '<div>Website <a href="https://delta.com.tr">Web Sitesi</a></div>'
        with patch.object(config, "SEARCH_CACHE_MODE", "off"), patch(
            "modules.search.crawler._request_with_safe_redirects", side_effect=error
        ), patch(
            "modules.search.crawler._try_render", return_value=(rendered, None)
        ) as render:
            records = search._profile_external_websites("https://fair.example/profile/delta")
        self.assertEqual(records[0]["url"], "https://delta.com.tr")
        render.assert_called_once()

        search.reset_source_health()
        with patch.object(config, "SEARCH_CACHE_MODE", "replay"), patch(
            "modules.search.cache_store.load", return_value=None
        ), patch("modules.search.crawler._try_render") as replay_render, patch(
            "modules.search.crawler._request_with_safe_redirects"
        ) as replay_request:
            self.assertEqual(
                search._profile_external_websites("https://fair.example/profile/replay"), []
            )
        replay_render.assert_not_called()
        replay_request.assert_not_called()

    def test_paid_adaptive_expansion_stays_inside_existing_query_ceiling(self):
        seen = []

        def empty_search(query):
            seen.append(query)
            return []

        with patch.object(config, "SEARCH_PROVIDER", "brightdata"), patch.object(
            config, "SEARCH_CACHE_MODE", "off"
        ), patch.object(config, "DEFAULT_PAID_SEARCH_QUERY_LIMIT", 5), patch.object(
            config, "PAID_SEARCH_ADAPTIVE_RESERVE", 2
        ), patch.object(config, "MAX_ADAPTIVE_SEARCH_QUERIES", 2), patch(
            "modules.search._search_text", side_effect=empty_search
        ), patch("modules.search._profile_external_websites", return_value=[]), patch(
            "modules.search._add_google_places_results"
        ), patch("modules.search._add_domain_guesses"):
            candidates = search.find_candidate_domains("VEKTOR OTOMASYON")

        self.assertLessEqual(len(seen), 5)
        self.assertTrue(any(item.get("phase") == "adaptive" for item in candidates.trace))


if __name__ == "__main__":
    unittest.main()
