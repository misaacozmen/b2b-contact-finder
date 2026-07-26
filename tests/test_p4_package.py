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
