import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

import config
import main
from modules import cache_store, checkpoint, crawler, email_verifier, evidence, extractor, google_places, search


class PipelineImprovementTests(unittest.TestCase):
    def test_profile_link_is_discovery_not_publication_authority(self) -> None:
        candidate = {"query": "source_profile", "_source_profile_evidence": 1}
        self.assertFalse(main._has_trusted_website_evidence(
            candidate,
            ["page_identity_missing:0/2", "structured_identity_strong:2/2"],
        ))

    def test_search_cache_can_replay_without_live_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "SEARCH_CACHE_DIR", Path(directory)):
            with patch.object(config, "SEARCH_CACHE_MODE", "use"), patch(
                "modules.search._search_text_live",
                return_value=[{"href": "https://example.com", "title": "Example", "body": ""}],
            ) as live:
                first = search._search_text("Example official website")
                second = search._search_text("Example official website")
            self.assertEqual(first, second)
            live.assert_called_once()

            with patch.object(config, "SEARCH_CACHE_MODE", "replay"), patch(
                "modules.search._search_text_live", side_effect=AssertionError("live call forbidden")
            ):
                self.assertEqual(search._search_text("Example official website"), first)

    def test_search_replay_cache_miss_does_not_call_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "SEARCH_CACHE_DIR", Path(directory)), patch.object(
            config, "SEARCH_CACHE_MODE", "replay"
        ), patch("modules.search._search_text_live", side_effect=AssertionError("live call forbidden")):
            self.assertEqual(search._search_text("Missing query"), [])

    def test_search_replay_can_read_another_provider_cache_without_live_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with patch.object(config, "SEARCH_CACHE_DIR", cache_dir), patch.object(
                config, "SEARCH_PROVIDER", "brightdata"
            ), patch.object(config, "SEARCH_CACHE_MODE", "use"), patch(
                "modules.search._search_text_live",
                return_value=[{"href": "https://example.com", "title": "Example", "body": ""}],
            ):
                expected = search._search_text("Provider-neutral replay")

            with patch.object(config, "SEARCH_CACHE_DIR", cache_dir), patch.object(
                config, "SEARCH_PROVIDER", "ddgs"
            ), patch.object(config, "SEARCH_CACHE_MODE", "replay"), patch(
                "modules.search._search_text_live", side_effect=AssertionError("live call forbidden")
            ):
                self.assertEqual(search._search_text("Provider-neutral replay"), expected)

    def test_candidate_list_preserves_queries_with_zero_results(self) -> None:
        with patch("modules.search._search_text", return_value=[]), patch("modules.search._domain_has_address", return_value=False):
            candidates = search.find_candidate_domains("Trace Example")
        self.assertEqual(candidates, [])
        self.assertTrue(candidates.trace)
        self.assertIn("query", candidates.trace[0])
        self.assertEqual(candidates.trace[0]["result_count"], 0)

    def test_cache_store_round_trip_preserves_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_store.save(root, "test", "key", [], 1)
            self.assertEqual(cache_store.load(root, "test", "key", 30, 1), [])

    def test_checkpoint_serializes_candidate_evidence_sets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.xlsx"
            input_path.write_bytes(b"test")
            progress_path = root / "progress.json"
            with patch.object(config, "PROGRESS_FILE", progress_path), patch.object(
                config, "PROGRESS_DB_FILE", root / "progress.sqlite3"
            ), patch.object(config, "STATE_DIR", root):
                checkpoint.save_progress(
                    input_path, 0,
                    [{"company": "Example", "__candidates": [{"_evidence_queries": {"q2", "q1"}}]}],
                    "signature",
                )
                loaded = checkpoint.load_progress(input_path, "signature")
        self.assertEqual(loaded["results_so_far"][0]["__candidates"][0]["_evidence_queries"], ["q1", "q2"])

    def test_json_ld_identity_and_contact_roles_are_extracted(self) -> None:
        html = """
        <script type="application/ld+json">
        {"@type":"Organization","legalName":"Example Gida Sanayi AS","url":"https://example.com",
         "sameAs":["https://example.com.tr"],
         "contactPoint":{"@type":"ContactPoint","contactType":"specialist",
                         "email":"uzman@example.com","telephone":"+90 232 555 11 22"}}
        </script>
        """
        identity = extractor.extract_organization_evidence(html)
        records = extractor.extract_contact_records(html, "https://example.com/contact")
        self.assertIn("Example Gida Sanayi AS", identity["names"])
        self.assertIn("https://example.com.tr", identity["same_as"])
        self.assertEqual(records["emails"][0]["label"], "specialist")
        self.assertEqual(records["phones"][0]["label"], "specialist")

    def test_cloudflare_protected_email_is_decoded(self) -> None:
        email = "info@example.com"
        key = 0x42
        encoded = bytes([key, *(ord(char) ^ key for char in email)]).hex()
        html = f'<a class="__cf_email__" data-cfemail="{encoded}">email protected</a>'
        self.assertEqual(extractor.extract_emails(html), [email])

    def test_script_and_escape_email_noise_is_not_published(self) -> None:
        html = """
        <a href="mailto:%20sales@example.com">Sales</a>
        <div>u003einfo@example.com</div>
        <div>info@mysite.com</div>
        <script>
        const dsn = "abcdef@sentry.wixpress.com";
        const placeholder = "info@mysite.com";
        </script>
        """
        self.assertEqual(
            extractor.extract_emails(html),
            ["sales@example.com", "info@example.com"],
        )

    def test_phone_ranking_prefers_specialist_over_owner(self) -> None:
        ranked = main._select_phone_records(
            [
                {"value": "+90 212 555 00 01", "label": "owner", "source_url": "https://example.com/contact"},
                {"value": "+90 212 555 00 02", "label": "specialist", "source_url": "https://example.com/contact"},
            ]
        )
        self.assertEqual(ranked[0]["value"], "02125550002")
        self.assertEqual(ranked[0]["label"], "specialist")

    def test_phone_ranking_prefers_corporate_line_when_roles_are_equal(self) -> None:
        ranked = main._select_phone_records([
            {"value": "+90 532 735 53 47", "label": "general", "source_url": "https://example.com"},
            {"value": "444 21 32", "label": "general", "source_url": "https://example.com/contact"},
        ])
        self.assertEqual(ranked[0]["value"], "04442132")

    def test_fax_is_not_selected_as_phone(self) -> None:
        html = "Telefon: +90 212 555 00 01 Fax: +90 212 555 00 02"
        records = extractor.extract_contact_records(html, "https://example.com/contact")
        ranked = main._select_phone_records(records["phones"])
        self.assertEqual([item["value"] for item in ranked], ["02125550001"])

    def test_sitemap_discovers_only_same_host_contact_pages(self) -> None:
        sitemap = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.com/products</loc></url>
        <url><loc>https://example.com/iletisim</loc></url>
        <url><loc>https://other.example/contact</loc></url>
        </urlset>"""
        with patch("modules.crawler._try_fetch", return_value=(sitemap, None)):
            urls = crawler._sitemap_contact_urls("https://example.com", ["https://example.com/sitemap.xml"])
        self.assertEqual(urls, ["https://example.com/iletisim"])

    def test_robots_parser_blocks_disallowed_contact_page(self) -> None:
        robots = "User-agent: *\nDisallow: /private-contact\nSitemap: https://example.com/sitemap.xml"
        with patch("modules.crawler._try_fetch", return_value=(robots, None)):
            parser, sitemaps = crawler._robots_and_sitemaps("https://example.com")
        self.assertFalse(parser.can_fetch(config.USER_AGENT, "https://example.com/private-contact"))
        self.assertIn("https://example.com/sitemap.xml", sitemaps)

    def test_directory_role_is_penalized_but_intrinsic_domain_is_not(self) -> None:
        self.assertEqual(
            search._candidate_role("Example Brand", "https://supplier-list.example", "Company directory", "Supplier profile exporters"),
            "directory",
        )
        self.assertEqual(
            search._candidate_role("Example Brand", "https://examplebrand.com", "Exporter company", "Supplier profile"),
            "company_candidate",
        )

    def test_google_places_cache_avoids_second_api_call(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "places": [{"id": "1", "displayName": {"text": "Example"}, "websiteUri": "https://example.com"}]
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "SEARCH_CACHE_DIR", Path(directory)), patch.object(
            config, "SEARCH_CACHE_MODE", "use"
        ), patch.object(google_places, "is_enabled", return_value=True), patch(
            "modules.google_places.requests.post", return_value=response
        ) as post:
            first = google_places.search_company("Example")
            second = google_places.search_company("Example")
        self.assertEqual(first, second)
        post.assert_called_once()

    def test_crawl_replay_never_calls_live_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "CRAWL_CACHE_DIR", Path(directory)), patch.object(
            config, "CRAWL_CACHE_MODE", "replay"
        ), patch("modules.crawler._fetch_site_live", side_effect=AssertionError("live call forbidden")):
            result = crawler.fetch_site("https://missing.example")
        self.assertEqual(result["error"], "crawl_replay_cache_miss")

    def test_replay_email_verification_does_not_query_dns(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "EMAIL_CACHE_DIR", Path(directory)), patch.object(
            config, "CRAWL_CACHE_MODE", "replay"
        ), patch("modules.email_verifier._domain_mx_status", side_effect=AssertionError("DNS call forbidden")):
            result = email_verifier.verify_email("info@missing.example")
        self.assertEqual(result, {"status": "not_checked", "reason": "mx_replay_cache_miss"})

    def test_evidence_jsonl_contains_field_source_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            evidence.write_jsonl(path, [{
                "company": "Example",
                "website": "https://example.com",
                "email": "info@example.com",
                "email_source_url": "https://example.com/contact",
                "phone": "02125550000",
                "phone_source_url": "https://example.com/contact",
                "status": "OK_HIGH_CONFIDENCE",
                "__candidates": [{"_evidence_queries": {"q1", "q2"}}],
            }])
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["selected"]["email_source_url"], "https://example.com/contact")
        self.assertEqual(payload["candidates"][0]["_evidence_queries"], ["q1", "q2"])

    def test_output_smoke_writes_new_audit_columns_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patches = (
                patch.object(config, "CONTACTS_FILE", root / "contacts.xlsx"),
                patch.object(config, "VERIFIED_CONTACTS_FILE", root / "verified_contacts.xlsx"),
                patch.object(config, "REVIEW_QUEUE_FILE", root / "review_queue.xlsx"),
                patch.object(config, "FAILED_FILE", root / "failed.xlsx"),
                patch.object(config, "CANDIDATES_FILE", root / "candidates.xlsx"),
                patch.object(config, "REPORT_FILE", root / "report.txt"),
                patch.object(config, "EVIDENCE_FILE", root / "evidence.jsonl"),
                patch.object(config, "ENTITY_RELATIONSHIPS_FILE", root / "entity_relationships.jsonl"),
                patch.object(config, "TELEMETRY_FILE", root / "telemetry.json"),
            )
            for context in patches:
                context.start()
            try:
                main._write_outputs([{
                    "company": "Example", "website": "https://example.com", "website_source": "official",
                    "email": "info@example.com", "email_source_url": "https://example.com/contact",
                    "phone": "02125550000", "phone_source_url": "https://example.com/contact",
                    "phone_label": "headquarters", "status": "OK_HIGH_CONFIDENCE", "confidence": "high",
                    "score": 95, "reason": "test", "__candidates": [{"_evidence_queries": {"q1"}}],
                }], 0)
            finally:
                for context in reversed(patches):
                    context.stop()
            workbook = load_workbook(root / "contacts.xlsx", read_only=True)
            try:
                headers = [cell.value for cell in workbook.active[1]]
            finally:
                workbook.close()
            self.assertIn("email_source_url", headers)
            self.assertIn("alternative_phones", headers)
            self.assertTrue((root / "evidence.jsonl").exists())
            self.assertTrue((root / "verified_contacts.xlsx").exists())
            self.assertTrue((root / "review_queue.xlsx").exists())

    def test_structured_same_as_marks_two_domains_as_official_family(self) -> None:
        first = {
            "candidate": {"url": "https://example.com"},
            "structured_identity": {"urls": ["https://example.com"], "same_as": ["https://example.com.tr"]},
        }
        second = {
            "candidate": {"url": "https://example.com.tr"},
            "structured_identity": {"urls": [], "same_as": []},
        }
        self.assertTrue(main._same_official_family(first, second))


if __name__ == "__main__":
    unittest.main()
