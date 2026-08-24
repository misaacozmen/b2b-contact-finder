import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from modules import (
    cache_store,
    candidate_reranker,
    crawler,
    replay_snapshot,
    runtime,
    site_mapper,
)


class ReplaySnapshotTests(unittest.TestCase):
    def setUp(self):
        runtime.reset()
        replay_snapshot.reset()

    def tearDown(self):
        replay_snapshot.reset()

    def test_snapshot_round_trip_replays_without_original_cache_tree(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            source_cache = Path(source) / "crawl_cache"
            target_cache = Path(target) / "crawl_cache"
            snapshot = Path(source) / "replay_snapshot.json.gz"
            value = {"pages": [{"url": "https://official.example/contact", "html": "hello"}]}

            cache_store.save(source_cache, "site", "official", value, 7)
            replay_snapshot.write(snapshot)
            replay_snapshot.reset()
            replay_snapshot.load(snapshot, max_uncompressed_bytes=1024 * 1024)

            self.assertFalse(target_cache.exists())
            self.assertEqual(
                cache_store.load(target_cache, "site", "official", 1, 7),
                value,
            )

    def test_replay_accepts_expired_cache_but_normal_use_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "crawl_cache"
            cache_store.save(cache_dir, "site", "old", {"value": 1}, 3)
            replay_snapshot.reset()
            path = cache_store._path(cache_dir, "site", "old", compressed=True)
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            payload["created_at"] = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat()
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle)

            with patch.object(config, "CRAWL_CACHE_DIR", cache_dir), patch.object(
                config, "CRAWL_CACHE_MODE", "use"
            ):
                self.assertIsNone(cache_store.load(cache_dir, "site", "old", 7, 3))
            with patch.object(config, "CRAWL_CACHE_DIR", cache_dir), patch.object(
                config, "CRAWL_CACHE_MODE", "replay"
            ):
                self.assertEqual(
                    cache_store.load(cache_dir, "site", "old", 7, 3),
                    {"value": 1},
                )

    def test_integrity_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json.gz"
            replay_snapshot.record("crawl_cache", "site", "key", 1, {"ok": True})
            replay_snapshot.write(snapshot)
            with gzip.open(snapshot, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            payload["entries"][0]["value"] = {"ok": False}
            with gzip.open(snapshot, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle)
            replay_snapshot.reset()
            with self.assertRaisesRegex(ValueError, "integrity"):
                replay_snapshot.load(snapshot, max_uncompressed_bytes=1024 * 1024)

    def test_unsupported_intermediate_snapshot_format_is_rejected(self):
        entries = []
        payload = {
            "format_version": 2,
            "entry_count": 0,
            "entries_sha256": __import__("hashlib").sha256(
                replay_snapshot._canonical(entries)
            ).hexdigest(),
            "entries": entries,
        }
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json.gz"
            with gzip.open(snapshot, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaisesRegex(ValueError, "unsupported"):
                replay_snapshot.load(
                    snapshot, max_uncompressed_bytes=1024 * 1024,
                )

    def test_secret_like_fields_are_removed_recursively(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json.gz"
            replay_snapshot.record(
                "search_cache",
                "query",
                "key",
                1,
                {
                    "api_key": "secret",
                    "nested": {"authorization": "Bearer secret", "result": "safe"},
                },
            )
            replay_snapshot.write(snapshot)
            with gzip.open(snapshot, "rt", encoding="utf-8") as handle:
                serialized = json.load(handle)
            value = serialized["entries"][0]["value"]
            self.assertNotIn("api_key", value)
            self.assertEqual(value["nested"], {"result": "safe"})

    def test_signed_urls_and_snapshot_keys_are_redacted_but_replayable(self):
        secret_url = (
            "https://files.example/report?X-Amz-Signature=top-secret&token=abc"
        )
        replay_snapshot.record(
            "crawl_cache", "document", secret_url, 1,
            {"source_url": secret_url},
        )
        found, value = replay_snapshot.lookup(
            "crawl_cache", "document", secret_url, 1,
        )
        self.assertTrue(found)
        self.assertNotIn("top-secret", value["source_url"])
        self.assertNotIn("token=abc", value["source_url"])

        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json.gz"
            replay_snapshot.write(snapshot)
            with gzip.open(snapshot, "rt", encoding="utf-8") as handle:
                serialized = handle.read()
        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("token=abc", serialized)

    def test_secret_bearing_keys_do_not_collide_after_redaction(self):
        first = "https://files.example/report?token=first-secret"
        second = "https://files.example/report?token=second-secret"
        replay_snapshot.record("crawl_cache", "document", first, 1, {"id": 1})
        replay_snapshot.record("crawl_cache", "document", second, 1, {"id": 2})
        self.assertEqual(
            replay_snapshot.lookup("crawl_cache", "document", first, 1)[1],
            {"id": 1},
        )
        self.assertEqual(
            replay_snapshot.lookup("crawl_cache", "document", second, 1)[1],
            {"id": 2},
        )

    def test_legacy_snapshot_is_sanitized_when_rewritten(self):
        secret_key = "https://files.example/report?token=legacy-secret"
        entries = [{
            "store": "crawl_cache",
            "namespace": "document",
            "key": secret_key,
            "schema_version": 1,
            "value": {"source_url": secret_key},
        }]
        payload = {
            "format_version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "entry_count": 1,
            "entries_sha256": __import__("hashlib").sha256(
                replay_snapshot._canonical(entries)
            ).hexdigest(),
            "entries": entries,
        }
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "legacy.json.gz"
            rewritten = Path(directory) / "rewritten.json.gz"
            with gzip.open(legacy, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle)
            replay_snapshot.load(legacy, max_uncompressed_bytes=1024 * 1024)
            self.assertEqual(
                replay_snapshot.lookup(
                    "crawl_cache", "document", secret_key, 1,
                )[1]["source_url"],
                "https://files.example/report?token=[REDACTED]",
            )
            replay_snapshot.write(rewritten)
            with gzip.open(rewritten, "rt", encoding="utf-8") as handle:
                serialized = handle.read()
        self.assertNotIn("legacy-secret", serialized)
        self.assertIn('"format_version":3', serialized)

    def test_redaction_preserves_surrounding_html_and_camel_case_secrets(self):
        replay_snapshot.record(
            "crawl_cache", "site", "key", 1,
            {
                "html": '<a href="https://x.example/?token=abc">Contact</a>',
                "accessToken": "secret",
                "clientSecret": "secret",
                "refreshToken": "secret",
            },
        )
        value = replay_snapshot.lookup("crawl_cache", "site", "key", 1)[1]
        self.assertEqual(
            value["html"],
            '<a href="https://x.example/?token=[REDACTED]">Contact</a>',
        )
        self.assertNotIn("accessToken", value)
        self.assertNotIn("clientSecret", value)
        self.assertNotIn("refreshToken", value)

    def test_azure_and_aws_session_url_secrets_are_redacted(self):
        secret_url = (
            "https://files.example/blob?sig=azure-secret&"
            "X-Amz-Security-Token=aws-session-secret"
        )
        replay_snapshot.record(
            "crawl_cache", "document", "key", 1, {"url": secret_url},
        )
        value = replay_snapshot.lookup("crawl_cache", "document", "key", 1)[1]
        self.assertNotIn("azure-secret", value["url"])
        self.assertNotIn("aws-session-secret", value["url"])

    def test_secret_bearing_prefixes_do_not_cross_match(self):
        first = "https://x.example/?token=first|pages=6|"
        second = "https://x.example/?token=second|pages=6|"
        replay_snapshot.record(
            "crawl_cache", "site", first + "seeds=/contact", 1,
            {"pages": [{"id": "first"}]},
        )
        replay_snapshot.record(
            "crawl_cache", "site", second + "seeds=/contact", 1,
            {"pages": [{"id": "second"}, {"id": "extra"}]},
        )
        found, value = replay_snapshot.lookup_prefix(
            "crawl_cache", "site", first, 1,
        )
        self.assertTrue(found)
        self.assertEqual(value["pages"], [{"id": "first"}])


class RecoveryCoverageTests(unittest.TestCase):
    def setUp(self):
        runtime.reset()

    def test_mapper_finds_json_ld_and_hydration_routes_only_on_official_domain(self):
        html = """
        <script type="application/ld+json">
          {"name":"Official", "url":"/locations/turkiye",
           "department":{"url":"https://outside.example/contact"}}
        </script>
        <script>window.__DATA__={"legal":"/kvkk-aydinlatma"};</script>
        """
        urls = {
            item["url"]
            for item in site_mapper.discover(html, "https://official.example")
        }
        self.assertIn("https://official.example/locations/turkiye", urls)
        self.assertIn("https://official.example/kvkk-aydinlatma", urls)
        self.assertFalse(any("outside.example" in url for url in urls))

    def test_malformed_sitemap_recovers_only_relevant_same_domain_urls(self):
        loose = """
        broken <loc>https://official.example/contact</loc>
        https://official.example/products
        https://outside.example/kvkk
        """
        with patch("modules.crawler._try_fetch", return_value=(loose, None)):
            urls = crawler._sitemap_contact_urls(
                "https://official.example",
                ["https://official.example/sitemap.xml"],
            )
        self.assertEqual(urls, ["https://official.example/contact"])

    def test_static_recovery_page_can_reveal_nested_contact_without_browser(self):
        def fake_fetch(url):
            if url == "https://official.example/about":
                return '<a href="/contact">Contact</a>', None
            if url == "https://official.example/contact":
                return "Official Makine Limited Sirketi info@official.example", None
            return None, "http_403"

        with patch("modules.crawler._try_fetch", side_effect=fake_fetch), patch(
            "modules.crawler._robots_and_sitemaps",
            return_value=(None, ["https://official.example/sitemap.xml"]),
        ), patch(
            "modules.crawler._sitemap_contact_urls",
            return_value=["https://official.example/about"],
        ), patch.object(config, "IDENTITY_PAGE_PATHS", ()), patch.object(
            config, "CONTACT_PAGE_PATHS", ()
        ), patch.object(config, "MAX_STATIC_RECOVERY_PAGES", 1), patch(
            "modules.crawler._try_render"
        ) as render:
            result = crawler._fetch_site_live("https://official.example")

        page_urls = {page["url"] for page in result["pages"]}
        self.assertIn("https://official.example/about", page_urls)
        self.assertIn("https://official.example/contact", page_urls)
        self.assertEqual(result["recovery_trace"][0]["stage"], "static_pages")
        render.assert_not_called()

    def test_unreachable_host_skips_static_and_disabled_browser_recovery(self):
        with patch(
            "modules.crawler._try_fetch",
            return_value=(None, "blocked_network_target:dns_unresolved"),
        ), patch(
            "modules.crawler._robots_and_sitemaps",
        ) as sitemaps, patch(
            "modules.crawler._try_render",
        ) as render, patch.object(
            config, "MAX_HOST_VARIANT_ATTEMPTS", 0,
        ), patch.object(config, "ENABLE_JS_FALLBACK", False):
            result = crawler._fetch_site_live("https://missing.example")

        self.assertEqual(result["pages"], [])
        self.assertEqual(
            result["recovery_trace"][0]["status"],
            "skipped_unrecoverable_root",
        )
        sitemaps.assert_not_called()
        render.assert_not_called()
        counters = runtime.snapshot()["counters"]
        self.assertEqual(counters["recovery.static_skips"], 1)
        self.assertNotIn("recovery.browser_attempts", counters)


def _ranking_item(*, final_score=80, direct=True, components=3, scopes=2):
    return {
        "candidate": {
            "url": "https://official.example",
            "role": "company_candidate",
            "reason": (
                "domain_hits:2/2"
                if direct
                else "search_text_identity:2/2;discovery_only_not_identity_authority"
            ),
        },
        "reasons": [
            "page_identity_strong:2/2",
            "structured_identity_strong:2/2",
            "context_match:city",
        ],
        "identity_assessment": {
            "provisionally_publishable": True,
            "conflicts": [],
            "support_count": components,
            "strong_first_party_bundle": components >= 3,
            "first_party_bundle_components": components,
        },
        "structured_identity": {
            "claims": [
                {"page_scope": scope}
                for scope in ("legal", "contact", "locations")[:scopes]
            ],
        },
        "has_contact": True,
        "email_failed": False,
        "final_score": final_score,
    }


class EvidenceCandidateRerankerTests(unittest.TestCase):
    def test_stronger_multiscope_bundle_beats_higher_numeric_score(self):
        strong = _ranking_item(final_score=82, components=3, scopes=3)
        weak = _ranking_item(final_score=99, components=2, scopes=1)
        self.assertGreater(
            candidate_reranker.rank_key("Official Makine", strong, hard_context_failure=False),
            candidate_reranker.rank_key("Official Makine", weak, hard_context_failure=False),
        )

    def test_non_score_key_really_ignores_final_score(self):
        low = _ranking_item(final_score=60)
        high = _ranking_item(final_score=100)
        self.assertEqual(
            candidate_reranker.non_score_key("Official Makine", low, hard_context_failure=False),
            candidate_reranker.non_score_key("Official Makine", high, hard_context_failure=False),
        )

    def test_discovery_only_candidate_does_not_beat_equivalent_direct_candidate(self):
        direct = _ranking_item(final_score=70, direct=True)
        discovery = _ranking_item(final_score=100, direct=False)
        self.assertGreater(
            candidate_reranker.rank_key("Official Makine", direct, hard_context_failure=False),
            candidate_reranker.rank_key("Official Makine", discovery, hard_context_failure=False),
        )

    def test_explicit_first_party_relationship_can_upgrade_discovery_candidate(self):
        direct = _ranking_item(final_score=100, direct=True)
        relationship = _ranking_item(final_score=70, direct=False)
        relationship["reasons"].append("legal_name_ownership_match:4")
        self.assertGreater(
            candidate_reranker.rank_key(
                "Official Makine", relationship, hard_context_failure=False,
            ),
            candidate_reranker.rank_key(
                "Official Makine", direct, hard_context_failure=False,
            ),
        )


if __name__ == "__main__":
    unittest.main()
