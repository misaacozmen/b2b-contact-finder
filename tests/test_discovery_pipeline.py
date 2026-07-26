import json
import tempfile
import unittest
from unittest.mock import patch

import config
import main
import validate_golden_xlsx
from modules import aliases, crawler, email_verifier, extractor, google_places, hunter, scorer, search


class FakeResponse:
    def __init__(self, payload=None, text="") -> None:
        self.payload = payload
        self.text = text

    def json(self):
        if self.payload is None:
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self):
        return None


class DiscoveryPipelineTests(unittest.TestCase):
    def test_known_directory_domains_are_excluded(self) -> None:
        for domain in (
            "turkishexporter.com.tr", "turkish-manufacturers.com", "gulfood.com",
            "gso.org.tr", "find.com.tr", "mukellef.info", "europages.com.tr", "bbc.com",
        ):
            with self.subTest(domain=domain):
                self.assertTrue(scorer.is_excluded_domain(domain))

    def test_single_brand_with_sector_metadata_cannot_stop_early_without_sector_match(self) -> None:
        candidate = {
            "domain": "aysanfindik.com",
            "query": "aysan Turkiye official website",
            "_official_query_evidence": 3,
            "_metadata_context_matches": 0,
        }
        self.assertFalse(search._can_early_stop(
            "AYSAN", candidate, {"sector": "Olives, Pickles, Sauces", "description": ""}
        ))

    def test_short_abbreviation_sector_variant_scores_domain_guess(self) -> None:
        company = "ATC KIMYA TUZ TARIM INSAAT HAYVANCILIK NAKLIYE SANAYI VE TICARET LIMITED SIRKETI"
        self.assertIn("atc kimya", scorer.search_name_variants(company))
        details = scorer.score_domain_details(company, "https://atckimya.com")
        self.assertGreaterEqual(details["score"], config.MIN_ACCEPT_SCORE)
        self.assertIn("anchored_primary_bonus", details["reason"])

    def test_intrinsic_company_domain_outranks_directory_with_equal_page_evidence(self) -> None:
        def evaluation(url: str, reason: str) -> dict:
            return {
                "candidate": {
                    "url": url, "query": "official", "_official_query_evidence": 0, "reason": reason,
                },
                "reasons": ["page_identity_medium:1/2", "context_match:1/1", "email_domain_match"],
                "context_failed": False,
                "email_failed": False,
                "has_contact": True,
                "final_score": 100,
            }
        correct = evaluation("https://agoraambalaj.com", "domain_hits:1/2")
        directory = evaluation("https://example-directory.com", "search_text_identity:2/2")
        self.assertGreater(
            main._evaluation_rank_key("AGORA TRADE AMBALAJ", correct),
            main._evaluation_rank_key("AGORA TRADE AMBALAJ", directory),
        )

    def test_human_verified_no_website_skips_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = __import__("pathlib").Path(directory) / "aliases.json"
            path.write_text(json.dumps({"HALITLAR GIDA": {"no_website": True}}), encoding="utf-8")
            with patch.object(config, "COMPANY_ALIASES_FILE", path), patch("modules.search._safe_search_text") as lookup:
                aliases._entries.cache_clear()
                self.assertEqual(search.find_candidate_domains("HALITLAR GIDA"), [])
                lookup.assert_not_called()
            aliases._entries.cache_clear()

    def test_simoni_club_brand_domain_is_not_treated_as_sports_directory(self) -> None:
        matched, _, _ = scorer.domain_identity_match("CARDUCCI SIMONI CLUB", "https://simoniclub.com")
        self.assertTrue(matched)

    def test_short_brand_and_second_company_word_can_form_domain(self) -> None:
        company = "ATC KIMYA TUZ TARIM INSAAT HAYVANCILIK NAKLIYE SANAYI VE TICARET LIMITED SIRKETI"
        matched, _, _ = scorer.domain_identity_match(company, "https://atckimya.com")
        self.assertTrue(matched)

    def test_search_variants_preserve_one_letter_brand_component(self) -> None:
        self.assertIn("brands a", scorer.search_name_variants("BRANDS A TEKSTIL LTD STI"))

    def test_official_search_result_can_discover_public_brand_alias_from_text(self) -> None:
        candidates = {}
        search._add_search_results(
            candidates,
            "KRISTAL KOZMETIK",
            "kristal kozmetik Turkiye official website",
            [{
                "href": "https://lanaturel.com/pages/iletisim-bilgileri",
                "title": "LaNaturel Iletisim Bilgileri",
                "body": "Firma resmi adi Kristal Kozmetik",
            }],
        )
        self.assertIn("lanaturel.com", candidates)
        self.assertIn("search_text_identity", candidates["lanaturel.com"]["reason"])

    def test_exhibitor_profile_link_becomes_high_trust_candidate(self) -> None:
        html = '<a href="/internal">Internal</a><div>Web: www.simoniclub.com</div>'
        response = FakeResponse(text=html)
        with patch("modules.search.crawler._request_with_safe_redirects", return_value=response), patch(
            "modules.search._safe_search_text", return_value=[]
        ):
            candidates = search.find_candidate_domains(
                "CARDUCCI SIMONI CLUB",
                {"profile_url": "https://www.ifco.com.tr/fair/exhibitors/simoni-club/detail"},
            )
        self.assertEqual(candidates[0]["url"], "https://www.simoniclub.com")
        self.assertEqual(candidates[0]["query"], "source_profile")

    def test_one_search_timeout_returns_empty_results_instead_of_aborting_company(self) -> None:
        with patch.object(config, "SEARCH_PROVIDER", "ddgs"), patch(
            "modules.search._search_text", side_effect=RuntimeError("timeout")
        ):
            self.assertEqual(search._safe_search_text("Example official website"), [])

    def test_search_query_and_page_identity_treat_missing_listing_context_as_neutral(self) -> None:
        evaluation = {
            "context_failed": True,
            "reasons": [
                "metadata_context_missing:0/1", "page_identity_strong:1/1",
                "legal_name_phrase_match:1", "no_email",
            ],
            "has_contact": False,
            "candidate": {"query": "explosion Turkiye official website", "_official_query_evidence": 1},
        }
        self.assertFalse(main._is_hard_context_failure(evaluation))

    def test_authoritative_unreachable_website_is_publishable_for_review(self) -> None:
        candidates = [
            {"url": "https://brand.example", "score": 80, "_official_query_evidence": 2},
        ]
        self.assertIs(main._authoritative_unreachable_candidate(candidates), candidates[0])
        self.assertIsNone(main._authoritative_unreachable_candidate([
            {"url": "https://weak.example", "score": 80, "_official_query_evidence": 1},
        ]))

    def test_security_interstitial_is_not_treated_as_company_page(self) -> None:
        self.assertTrue(crawler._looks_like_security_interstitial(
            "<html>Uwaga! Ta strona stanowi zagrozenie.</html>"
        ))

    def test_predefined_contact_path_can_use_browser_fallback(self) -> None:
        def fake_fetch(url: str) -> tuple[str | None, str | None]:
            if url == "https://example.com":
                return "<html><body>Example Brand</body></html>", None
            if url.endswith("/contact"):
                return None, "http_403"
            return None, "http_404"

        with patch("modules.crawler._try_fetch", side_effect=fake_fetch), patch(
            "modules.crawler._try_render",
            return_value=("<html>info@example.com</html>", None),
        ) as render:
            result = crawler.fetch_site("https://example.com")
        self.assertGreaterEqual(len(result["pages"]), 2)
        render.assert_called_once_with("https://example.com/contact")

    def test_golden_phone_normalization_accepts_missing_trunk_zero(self) -> None:
        self.assertEqual(validate_golden_xlsx._phones(5434899334), ["05434899334"])

    def test_interactive_api_setup_asks_for_both_active_api_keys(self) -> None:
        answers = iter(["y", "y"])
        secrets = iter(["google-secret", "bright-secret"])
        with tempfile.TemporaryDirectory() as directory:
            keys_file = __import__("pathlib").Path(directory) / "api_keys.json"
            with patch.object(config, "SAVED_API_KEYS_FILE", keys_file), patch.object(
                config, "ENABLE_GOOGLE_PLACES", False
            ), patch.object(config, "GOOGLE_PLACES_API_KEY", ""), patch.object(
                config, "SEARCH_PROVIDER", "ddgs"
            ), patch.object(config, "BRIGHTDATA_API_KEY", ""):
                main.configure_apis_interactively(
                    input_fn=lambda _prompt: next(answers),
                    secret_fn=lambda _prompt: next(secrets),
                )
                self.assertTrue(config.ENABLE_GOOGLE_PLACES)
                self.assertEqual(config.GOOGLE_PLACES_API_KEY, "google-secret")
                self.assertEqual(config.SEARCH_PROVIDER, "brightdata")
                self.assertEqual(config.BRIGHTDATA_API_KEY, "bright-secret")
                self.assertEqual(
                    main._load_saved_api_keys(),
                    {"google_places": "google-secret", "brightdata": "bright-secret"},
                )
                self.assertNotIn("google-secret", keys_file.read_text(encoding="utf-8"))

    def test_interactive_api_setup_reuses_saved_keys_without_prompting(self) -> None:
        answers = iter(["y", "y", "y", "y"])
        with tempfile.TemporaryDirectory() as directory:
            keys_file = __import__("pathlib").Path(directory) / "api_keys.json"
            keys_file.write_text(
                json.dumps({"google_places": "saved-google", "brightdata": "saved-bright"}),
                encoding="utf-8",
            )
            with patch.object(config, "SAVED_API_KEYS_FILE", keys_file), patch.object(
                config, "GOOGLE_PLACES_API_KEY", ""
            ), patch.object(config, "BRIGHTDATA_API_KEY", ""):
                main.configure_apis_interactively(
                    input_fn=lambda _prompt: next(answers),
                    secret_fn=lambda _prompt: self.fail("Saved key should not be requested again"),
                )
                self.assertEqual(config.GOOGLE_PLACES_API_KEY, "saved-google")
                self.assertEqual(config.BRIGHTDATA_API_KEY, "saved-bright")

    def test_interactive_api_setup_disables_both_without_requesting_keys(self) -> None:
        prompts = []
        answers = iter(["n", "n"])

        def fail_if_secret_requested(_prompt: str) -> str:
            self.fail("Deaktif API için anahtar sorulmamalı")

        with tempfile.TemporaryDirectory() as directory:
            keys_file = __import__("pathlib").Path(directory) / "api_keys.json"
            with patch.object(config, "SAVED_API_KEYS_FILE", keys_file), patch.object(
                config, "ENABLE_GOOGLE_PLACES", True
            ), patch.object(config, "GOOGLE_PLACES_API_KEY", "old-google-key"), patch.object(
                config, "SEARCH_PROVIDER", "brightdata"
            ), patch.object(config, "BRIGHTDATA_API_KEY", "old-bright-key"):
                main.configure_apis_interactively(
                    input_fn=lambda prompt: prompts.append(prompt) or next(answers),
                    secret_fn=fail_if_secret_requested,
                )
                self.assertFalse(config.ENABLE_GOOGLE_PLACES)
                self.assertEqual(config.GOOGLE_PLACES_API_KEY, "")
                self.assertEqual(config.SEARCH_PROVIDER, "ddgs")
                self.assertEqual(config.BRIGHTDATA_API_KEY, "")
        self.assertEqual(len(prompts), 2)

    def test_interactive_api_setup_can_replace_one_saved_key(self) -> None:
        # Google: active + replace. Bright Data: active + reuse.
        answers = iter(["y", "n", "y", "y"])
        secrets = iter(["new-google"])
        with tempfile.TemporaryDirectory() as directory:
            keys_file = __import__("pathlib").Path(directory) / "api_keys.json"
            keys_file.write_text(
                json.dumps({"google_places": "old-google", "brightdata": "saved-bright"}),
                encoding="utf-8",
            )
            with patch.object(config, "SAVED_API_KEYS_FILE", keys_file), patch.object(
                config, "GOOGLE_PLACES_API_KEY", ""
            ), patch.object(config, "BRIGHTDATA_API_KEY", ""):
                main.configure_apis_interactively(
                    input_fn=lambda _prompt: next(answers),
                    secret_fn=lambda _prompt: next(secrets),
                )
                self.assertEqual(config.GOOGLE_PLACES_API_KEY, "new-google")
                self.assertEqual(config.BRIGHTDATA_API_KEY, "saved-bright")
                self.assertEqual(
                    main._load_saved_api_keys(),
                    {"google_places": "new-google", "brightdata": "saved-bright"},
                )
                raw_secret_file = keys_file.read_text(encoding="utf-8")
                self.assertNotIn("new-google", raw_secret_file)
                self.assertNotIn("saved-bright", raw_secret_file)

    def test_domain_identity_ignores_legal_and_business_descriptors(self) -> None:
        cases = [
            ("ANYONG GROUP KOZMETIK SAN. VE TIC. LTD. SIRKETI", "https://anyong.com.tr"),
            ("ARKEVITAL KOZMETIK SAN. VE TIC. LIMITED SIRKETI", "https://arkevital.com"),
            ("PROMARC IC VE DIS TICARET LIMITED SIRKETI", "https://promarc.com.tr"),
            ("ESSEL SELULOZ VE KAGIT SAN. TIC. AS", "https://essel.com.tr"),
        ]
        for company, website in cases:
            with self.subTest(company=company):
                matched, _, _ = scorer.domain_identity_match(company, website)
                self.assertTrue(matched)

    def test_unrelated_short_domain_does_not_match_company(self) -> None:
        matched, _, _ = scorer.domain_identity_match("SERDEN TRIKO", "https://gso.org.tr")
        self.assertFalse(matched)

    def test_short_brand_fragment_cannot_match_inside_another_domain(self) -> None:
        matched, _, _ = scorer.domain_identity_match("AR KAGIT KOZMETIK", "https://yasar.com.tr")
        self.assertFalse(matched)
        matched, _, _ = scorer.domain_identity_match("2F DONUK GIDA", "https://2f.com.tr")
        self.assertTrue(matched)

    def test_short_title_plus_long_brand_domain_is_allowed(self) -> None:
        matched, _, _ = scorer.domain_identity_match(
            "DR. SEYDA ATABAY SAGLIK VE KOZMETIK URUNLERI AS",
            "https://dratabay.com",
        )
        self.assertTrue(matched)
        self.assertGreater(
            scorer.score_domain("DR. SEYDA ATABAY SAGLIK VE KOZMETIK URUNLERI AS", "https://dratabay.com"),
            0,
        )

    def test_one_character_brand_typo_can_match_contextual_domain_prefix(self) -> None:
        company = "ARNISA KIMYA KOZMETIK SANAYI TICARET LIMITED SIRKETI"
        matched, _, _ = scorer.domain_identity_match(company, "https://arniskimya.com")
        self.assertTrue(matched)
        details = scorer.score_domain_details(company, "https://arniskimya.com", title="Arnis Kimya Kozmetik")
        self.assertIn("near_brand_bonus", details["reason"])

    def test_brightdata_decoder_accepts_json_text(self) -> None:
        response = FakeResponse(text=json.dumps({"organic": []}))
        self.assertEqual(search._decode_brightdata_response(response), {"organic": []})

    def test_google_places_returns_open_businesses_with_websites(self) -> None:
        payload = {
            "places": [
                {
                    "id": "place-1",
                    "displayName": {"text": "Example Brand"},
                    "websiteUri": "https://examplebrand.com.tr",
                    "internationalPhoneNumber": "+90 212 555 12 34",
                },
                {"id": "place-2", "businessStatus": "CLOSED_PERMANENTLY", "websiteUri": "https://closed.example"},
            ]
        }
        with patch.object(google_places, "is_enabled", return_value=True), patch("modules.google_places.requests.post", return_value=FakeResponse(payload)):
            places = google_places.search_company("Example Brand")
        self.assertEqual(places, [{"website": "https://examplebrand.com.tr", "phone": "+90 212 555 12 34", "name": "Example Brand", "place_id": "place-1"}])

    def test_google_places_uses_short_brand_query_for_long_legal_name(self) -> None:
        response = FakeResponse({"places": []})
        company = "ATC KIMYA TUZ TARIM INSAAT HAYVANCILIK NAKLIYE SANAYI VE TICARET LIMITED SIRKETI"
        with patch.object(google_places, "is_enabled", return_value=True), patch(
            "modules.google_places.requests.post", return_value=response
        ) as post:
            google_places.search_company(company)
        text_query = post.call_args.kwargs["json"]["textQuery"]
        self.assertLess(len(text_query), len(company))
        self.assertTrue(text_query.endswith(" Turkey"))

    def test_google_places_phone_requires_same_website_domain(self) -> None:
        places = [
            {"website": "https://other.example", "phone": "+90 212 000 00 00", "name": "Other", "place_id": "1"},
            {"website": "https://examplebrand.com.tr", "phone": "+90 212 555 12 34", "name": "Example Brand", "place_id": "2"},
        ]
        with patch("modules.google_places.search_company", return_value=places):
            phone = google_places.find_phone_for_website("Example Brand", "http://examplebrand.com.tr")
        self.assertEqual(phone, "+90 212 555 12 34")

    def test_directory_contacts_are_not_published_without_website_evidence(self) -> None:
        candidate = {
            "url": "https://examplebrand.com.tr",
            "score": 80,
            "query": "google_places",
            "external_phone": "+90 212 555 12 34",
            "reason": "google_places_match",
        }
        crawl_result = {
            "url": candidate["url"],
            "pages": [{"url": candidate["url"], "html": "<title>Example Brand</title>"}],
        }
        with patch("main.crawler.fetch_site", return_value=crawl_result), patch(
            "modules.hunter.find_domain_emails", return_value=[{"email": "info@examplebrand.com.tr"}]
        ):
            evaluation = main._evaluate_candidate("Example Brand", candidate)
        self.assertEqual(evaluation["email"], "")
        self.assertEqual(evaluation["email_source"], "")
        self.assertEqual(evaluation["phone"], "")
        self.assertEqual(evaluation["phone_source"], "")

    def test_hunter_filters_low_confidence_results(self) -> None:
        payload = {"data": {"emails": [{"value": "info@example.com", "confidence": 90}, {"value": "weak@example.com", "confidence": 20}]}}
        with patch.object(hunter, "is_enabled", return_value=True), patch("modules.hunter.requests.get", return_value=FakeResponse(payload)):
            emails = hunter.find_domain_emails("example.com")
        self.assertEqual(emails, [{"email": "info@example.com", "confidence": 90, "sources": []}])

    def test_fallback_search_runs_after_primary_miss(self) -> None:
        original_max = config.MAX_SEARCH_QUERIES_PER_COMPANY
        try:
            config.MAX_SEARCH_QUERIES_PER_COMPANY = 1

            def fake_search(query: str) -> list[dict]:
                if query.startswith('"example brand"'):
                    return [{"href": "https://examplebrand.com.tr", "title": "Example Brand", "body": ""}]
                return []

            with patch("modules.search._search_text", side_effect=fake_search):
                candidates = search.find_candidate_domains("Example Brand", {"sector": "Packaging", "description": ""})
            self.assertTrue(candidates)
            self.assertEqual(candidates[0]["domain"], "examplebrand.com.tr")
            self.assertTrue(candidates[0]["query"].startswith('"example brand"'))
        finally:
            config.MAX_SEARCH_QUERIES_PER_COMPANY = original_max

    def test_google_places_is_used_only_after_search_miss(self) -> None:
        with patch("modules.search._search_text", return_value=[]), patch("modules.search.google_places.search_company", return_value=[{"website": "https://examplebrand.com.tr", "phone": "+90 212 555 12 34", "name": "Example Brand", "place_id": "id"}]):
            candidates = search.find_candidate_domains("Example Brand")
        self.assertEqual(candidates[0]["query"], "google_places")
        self.assertEqual(candidates[0]["external_phone"], "+90 212 555 12 34")

    def test_verified_alias_becomes_auditable_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = __import__("pathlib").Path(directory) / "aliases.json"
            path.write_text(json.dumps({"Legal Company": {"aliases": ["Public Brand"], "website": "https://publicbrand.com.tr"}}), encoding="utf-8")
            with patch.object(config, "COMPANY_ALIASES_FILE", path), patch("modules.search._search_text", return_value=[]):
                aliases._entries.cache_clear()
                candidates = search.find_candidate_domains("Legal Company")
            aliases._entries.cache_clear()
        self.assertEqual(candidates[0]["query"], "verified_alias")
        self.assertEqual(candidates[0]["url"], "https://publicbrand.com.tr")

    def test_repeated_domain_across_queries_gets_consensus_bonus(self) -> None:
        candidates = {}
        result = [{"href": "https://examplebrand.com.tr", "title": "Example Brand", "body": ""}]
        search._add_search_results(candidates, "Example Brand", "example brand resmi sitesi", result)
        first_score = candidates["examplebrand.com.tr"]["score"]
        search._add_search_results(candidates, "Example Brand", "example brand iletisim", result)
        candidate = candidates["examplebrand.com.tr"]
        self.assertEqual(candidate["score"], min(config.PRE_CRAWL_SCORE_CAP, first_score + 4))
        self.assertIn("query_evidence:2", candidate["reason"])

    def test_official_query_evidence_wins_equal_candidate_score(self) -> None:
        candidates = {}
        official = [{"href": "https://examplebrand.com", "title": "Example Brand", "body": ""}]
        contact = [{"href": "https://examplebrand.net", "title": "Example Brand", "body": ""}]
        search._add_search_results(candidates, "Example Brand", "example brand Turkiye official website", official)
        search._add_search_results(candidates, "Example Brand", "example brand contact", contact)

        ranked = sorted(
            candidates.values(),
            key=lambda item: (item["score"], item.get("_official_query_evidence", 0)),
            reverse=True,
        )
        self.assertEqual(ranked[0]["domain"], "examplebrand.com")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertIn(f"query_trust_bonus:{config.TARGET_COUNTRY_OFFICIAL_QUERY_BONUS}", ranked[0]["reason"])

    def test_contact_candidate_keeps_existing_score_when_it_is_only_result(self) -> None:
        candidates = {}
        result = [{"href": "https://examplebrand.com", "title": "Example Brand", "body": ""}]
        details = scorer.score_domain_details("Example Brand", result[0]["href"], title=result[0]["title"])
        search._add_search_results(candidates, "Example Brand", "example brand contact", result)

        expected = min(config.PRE_CRAWL_SCORE_CAP, details["score"] + config.RESULT_RANK_BONUS_MAX)
        self.assertEqual(candidates["examplebrand.com"]["score"], expected)
        self.assertEqual(candidates["examplebrand.com"]["_query_trust_bonus"], 0)

    def test_primary_queries_run_country_official_before_contact_queries(self) -> None:
        queries = search._primary_queries("Example Brand", None)
        first_contact = next(index for index, query in enumerate(queries) if query.endswith(" contact"))
        country_official = next(index for index, query in enumerate(queries) if "Turkiye official website" in query)
        self.assertLess(country_official, first_contact)

    def test_fallback_queries_run_country_official_before_iletisim(self) -> None:
        queries = search._fallback_queries("Example Brand", {"sector": "Packaging", "description": ""})
        self.assertIn("Turkiye official website", queries[0])
        self.assertTrue(queries[-1].endswith(" iletisim"))

    def test_ambiguous_non_exact_domain_does_not_stop_official_search_early(self) -> None:
        candidate = {
            "domain": "aysanelektrik.com.tr",
            "query": "aysan Turkiye official website",
            "_official_query_evidence": 1,
            "_metadata_context_matches": 0,
        }
        metadata = {"sector": "Olives, Pickles, Sauces", "description": ""}
        self.assertFalse(search._can_early_stop("AYSAN", candidate, metadata))

    def test_ambiguous_official_result_does_not_hide_later_sector_match(self) -> None:
        seen_queries = []

        def fake_search(query: str) -> list[dict]:
            seen_queries.append(query)
            if query == "aysan Turkiye official website":
                return [{"href": "https://aysanelektrik.com.tr", "title": "Aysan Elektrik", "body": "Electrical"}]
            if query == "aysan gida":
                return [{"href": "https://aysantursu.com.tr", "title": "Aysan Tursu", "body": "Olives and pickles"}]
            return []

        metadata = {"sector": "Olives, Pickles, Sauces", "description": ""}
        with patch("modules.search._search_text", side_effect=fake_search):
            candidates = search.find_candidate_domains("AYSAN", metadata)
        self.assertIn("aysan gida", seen_queries)
        self.assertEqual(candidates[0]["domain"], "aysantursu.com.tr")

    def test_metadata_evidence_breaks_equal_candidate_score_tie(self) -> None:
        candidates = {}
        search._add_search_results(
            candidates,
            "AYSAN",
            "aysan Turkiye official website",
            [
                {"href": "https://aysanelektrik.com.tr", "title": "Aysan Elektrik", "body": "Electrical systems"},
                {"href": "https://aysantursu.com.tr", "title": "Aysan Tursu", "body": "Olives and pickles"},
            ],
            {"sector": "Olives, Pickles, Sauces", "description": ""},
        )
        candidates["aysanelektrik.com.tr"]["score"] = 92
        candidates["aysantursu.com.tr"]["score"] = 92
        self.assertEqual(search._best_candidate(candidates)["domain"], "aysantursu.com.tr")

    def test_rank_bonus_cannot_admit_domain_without_identity_match(self) -> None:
        candidates = {}
        result = [{"href": "https://unrelated.example", "title": "Example Brand", "body": ""}]
        search._add_search_results(candidates, "Example Brand", "example brand", result)
        self.assertEqual(candidates, {})

    def test_consensus_cannot_admit_short_brand_fragment(self) -> None:
        candidates = {}
        result = [{"href": "https://arsonkagit.com", "title": "AR Kağıt", "body": ""}]
        for query in ("ar kagit", "ar kagit contact", "ar kagit iletisim"):
            search._add_search_results(candidates, "AR KAĞIT KOZMETİK", query, result)
        self.assertEqual(candidates, {})

    def test_unresolvable_domain_guess_is_skipped(self) -> None:
        candidates = {}
        with patch("modules.search._domain_has_address", return_value=False):
            search._add_domain_guesses(candidates, "Example Brand")
        self.assertEqual(candidates, {})

    def test_overlong_domain_guess_is_skipped_before_dns(self) -> None:
        candidates = {}
        with patch("modules.search._domain_has_address") as dns_check:
            search._add_domain_guesses(candidates, "A" * 70)
        dns_check.assert_not_called()
        self.assertEqual(candidates, {})

    def test_contact_links_are_ranked_and_internal_only(self) -> None:
        html = """
        <a href='/about'>About Us</a>
        <a href='/contact'>Contact</a>
        <a href='https://other.example/contact'>Contact</a>
        <a href='/corporate'>Corporate</a>
        """
        links = extractor.extract_contact_page_links(html, "https://example.com", limit=3)
        self.assertEqual(links, ["https://example.com/contact", "https://example.com/about", "https://example.com/corporate"])

    def test_contact_extraction_reads_links_and_json_ld(self) -> None:
        html = """
        <a href="mailto:sales%40example.com">Write to us</a>
        <a href="tel:%2B90-212-555-12-34">Call</a>
        <script type="application/ld+json">
        {"@type":"Organization","contactPoint":{"telephone":"+90 216 555 43 21"}}
        </script>
        """
        self.assertEqual(extractor.extract_emails(html), ["sales@example.com"])
        phones = extractor.extract_phones(html)
        self.assertIn("+90-212-555-12-34", phones)
        self.assertIn("+90 216 555 43 21", phones)

    def test_turkish_contact_link_is_detected(self) -> None:
        html = '<a href="/bize-ulasin">Bize ula\u015f\u0131n</a>'
        self.assertEqual(
            extractor.extract_contact_page_links(html, "https://example.com", limit=1),
            ["https://example.com/bize-ulasin"],
        )

    def test_js_shell_detection(self) -> None:
        self.assertTrue(crawler._looks_like_js_shell('<div id="root"></div><script src="app.js"></script>'))
        self.assertFalse(crawler._looks_like_js_shell('<html><body>Contact us at info@example.com</body></html>'))

    def test_crawler_falls_back_to_http(self) -> None:
        def fake_fetch(url: str) -> tuple[str | None, str | None]:
            if url == "https://example.com":
                return None, "ssl_error"
            if url == "http://example.com":
                return "<html><body>Example</body></html>", None
            return None, "http_404"

        with patch("modules.crawler._try_fetch", side_effect=fake_fetch):
            result = crawler.fetch_site("https://example.com")
        self.assertEqual(result["url"], "http://example.com")
        self.assertEqual(len(result["pages"]), 1)

    def test_crawler_renders_root_after_http_403(self) -> None:
        with patch("modules.crawler._try_fetch", return_value=(None, "http_403")), patch(
            "modules.crawler._try_render", return_value=("<html><body>Example Brand</body></html>", None)
        ) as render:
            result = crawler.fetch_site("https://example.com")
        self.assertEqual(result["url"], "https://example.com")
        self.assertGreaterEqual(len(result["pages"]), 1)
        self.assertEqual(render.call_args_list[0].args[0], "https://example.com")

    def test_missing_listing_context_does_not_reject_non_exact_brand_domain(self) -> None:
        evaluation = {
            "context_failed": True,
            "reasons": ["metadata_context_missing:0/1"],
            "has_contact": True,
            "candidate": {"query": "real focus resmi sitesi"},
            "crawl_result": {"url": "https://realfocusmedia.net"},
        }
        self.assertFalse(main._unsafe_context_identity("REAL FOCUS", evaluation))
        self.assertNotIn("unsafe_context_identity", evaluation["reasons"])

    def test_context_mismatch_keeps_exact_multi_token_brand_for_review(self) -> None:
        evaluation = {
            "context_failed": True,
            "reasons": ["metadata_context_missing:0/1"],
            "has_contact": True,
            "candidate": {"query": "cikolata evreni Turkiye official website"},
            "crawl_result": {"url": "https://cikolataevreni.com"},
        }
        self.assertFalse(main._unsafe_context_identity("CIKOLATA EVRENI", evaluation))

    def test_invalid_mx_email_is_flagged(self) -> None:
        with patch("modules.email_verifier._domain_mx_status", return_value=("invalid_domain", "mx_missing")):
            result = email_verifier.verify_email("info@example.com")
        self.assertEqual(result, {"status": "invalid_domain", "reason": "mx_missing"})

    def test_address_record_is_accepted_as_implicit_mx(self) -> None:
        class FakeResolver:
            def resolve(self, domain, record_type):
                if record_type == "A":
                    return ["203.0.113.10"]
                raise AssertionError(record_type)

        self.assertEqual(
            email_verifier._implicit_mx_status(FakeResolver(), "example.com"),
            ("verified", "implicit_mx_a"),
        )


if __name__ == "__main__":
    unittest.main()
