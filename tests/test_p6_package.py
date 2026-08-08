import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
from modules import discovery_coverage, query_planner, runtime, search


class DiverseQueryBudgetTests(unittest.TestCase):
    def test_budget_is_spread_across_query_intents_before_name_variants(self):
        queries = [
            "nova Turkiye official website",
            "nova makina Turkiye official website",
            "nova resmi sitesi",
            "nova makina resmi sitesi",
            "nova otomasyon",
            "nova contact",
        ]
        selected = query_planner.diverse_queries(queries, 4)
        self.assertEqual(
            [query_planner.query_intent(query) for query in selected],
            ["country_official", "official", "context", "contact"],
        )

    def test_diverse_query_selection_is_bounded_and_deterministic(self):
        queries = ["a resmi sitesi", "a contact", "a resmi sitesi"]
        self.assertEqual(
            query_planner.diverse_queries(queries, 2),
            ["a resmi sitesi", "a contact"],
        )

    def test_live_paid_budget_is_reserved_and_spread_across_companies(self):
        with patch.object(config, "SEARCH_PROVIDER", "brightdata"), patch.object(
            config, "SEARCH_CACHE_MODE", "use",
        ), patch.object(config, "BRIGHTDATA_REQUEST_BUDGET", 2000), patch.object(
            config, "DEFAULT_PAID_SEARCH_QUERY_LIMIT", 10,
        ), patch.object(config, "MAX_SEARCH_QUERIES_PER_COMPANY", 0), patch.object(
            config, "BRIGHTDATA_RETRY_RESERVE_FRACTION", 0.20,
        ), patch.object(
            search, "_RUN_PAID_QUERY_LIMIT", None,
        ):
            limit = search.configure_run_budget(225)
        self.assertEqual(limit, 7)

    def test_free_fallback_budget_scales_with_company_count(self):
        with patch.object(config, "SEARCH_HTTP_REQUEST_BUDGET", 0), patch.object(
            config, "DEFAULT_FREE_SEARCH_QUERY_LIMIT_PER_COMPANY", 10,
        ), patch.object(config, "SEARCH_PROVIDER", "ddgs"), patch.object(
            search, "_RUN_PAID_QUERY_LIMIT", None,
        ):
            search.configure_run_budget(80)
            self.assertEqual(config.SEARCH_HTTP_REQUEST_BUDGET, 800)


class DiscoveryCoverageTests(unittest.TestCase):
    def setUp(self):
        runtime.reset()
        discovery_coverage.reset()

    def test_only_unresolved_replay_misses_enter_acquisition_plan(self):
        discovery_coverage.record_query(
            "NOVA MAKINE", '"nova" kvkk', "adaptive", "replay_miss", 0,
            {"missing_legal_name"},
        )
        discovery_coverage.record_query(
            "NOVA MAKINE", '"nova" Turkiye resmi sitesi',
            "adaptive", "cache_hit", 0, {"no_candidates"},
        )
        discovery_coverage.finalize_company(
            "NOVA MAKINE", resolved=False, candidate_count=0,
        )
        payload = discovery_coverage.payload()
        self.assertEqual(payload["replay_miss_count"], 1)
        self.assertEqual(payload["cached_empty_count"], 1)
        self.assertEqual(len(payload["acquisition_plan"]), 1)
        self.assertEqual(payload["acquisition_plan"][0]["intent"], "legal_identity")

    def test_resolved_company_does_not_request_cache_acquisition(self):
        discovery_coverage.record_query(
            "NOVA", "nova Turkiye official website",
            "primary", "replay_miss", 0, {"no_candidates"},
        )
        discovery_coverage.finalize_company(
            "NOVA", resolved=True, candidate_count=1,
        )
        self.assertEqual(discovery_coverage.payload()["acquisition_plan"], [])

    def test_live_budget_gap_enters_acquisition_plan(self):
        discovery_coverage.record_query(
            "NOVA", "nova Turkiye official website",
            "primary", "budget_blocked", 0, {"missing_legal_identity"},
        )
        discovery_coverage.finalize_company(
            "NOVA", resolved=False, candidate_count=1,
        )
        plan = discovery_coverage.payload()["acquisition_plan"]
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["reason"], "unresolved_search_budget_gap")

    def test_published_company_is_removed_from_prior_gap_plan(self):
        discovery_coverage.record_query(
            "NOVA", "nova Turkiye official website",
            "primary", "replay_miss", 0, {"no_candidates"},
        )
        discovery_coverage.finalize_company(
            "NOVA", resolved=False, candidate_count=1,
        )
        discovery_coverage.mark_published("NOVA")
        payload = discovery_coverage.payload()
        self.assertEqual(payload["resolved_companies"], 1)
        self.assertEqual(payload["acquisition_plan"], [])

    def test_written_manifest_is_machine_readable_and_contains_no_authority(self):
        discovery_coverage.record_query(
            "NOVA", "nova contact", "fallback", "replay_miss", 0,
        )
        discovery_coverage.finalize_company(
            "NOVA", resolved=False, candidate_count=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            discovery_coverage.write(path, 2)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["policy_version"], "discovery-coverage-v1")
        self.assertTrue(
            payload["acquisition_plan"][0]["requires_authorized_search"]
        )


class SearchCoverageIntegrationTests(unittest.TestCase):
    def test_brightdata_provider_rate_is_limited_independently(self):
        response = Mock(status_code=200)
        search._BRIGHTDATA_NEXT_REQUEST_AT = 0.0
        with patch.object(
            config, "BRIGHTDATA_REQUESTS_PER_MINUTE", 12,
        ), patch(
            "modules.search.runtime.reserve_api", return_value=True,
        ), patch(
            "modules.search.runtime.wait_for_request_slot",
        ), patch(
            "modules.search.requests.post", return_value=response,
        ), patch(
            "modules.search.time.monotonic", side_effect=[100.0, 100.0],
        ), patch("modules.search.time.sleep") as sleep:
            search._brightdata_post("https://provider.example")
            search._brightdata_post("https://provider.example")
        sleep.assert_called_once_with(5.0)

    def setUp(self):
        runtime.reset()
        discovery_coverage.reset()

    def test_replay_miss_status_is_preserved_without_network(self):
        with patch.object(config, "SEARCH_CACHE_MODE", "replay"), patch(
            "modules.search.cache_store.load", return_value=None,
        ), patch("modules.search._search_text_live") as live:
            results = search._search_text("missing query")
        self.assertEqual(results, [])
        self.assertEqual(results.cache_status, "replay_miss")
        live.assert_not_called()

    def test_exhausted_primary_and_fallback_budgets_are_visible(self):
        with patch.object(config, "SEARCH_PROVIDER", "brightdata"), patch(
            "modules.search._search_text",
            side_effect=search.SearchBudgetExhausted("paid exhausted"),
        ), patch(
            "modules.search._ddgs_text",
            side_effect=search.SearchBudgetExhausted("free exhausted"),
        ):
            results = search._safe_search_text("nova official website")
        self.assertEqual(results.cache_status, "budget_blocked")

    def test_brightdata_honors_body_cooldown_before_decode_retry(self):
        failed = Mock(status_code=200, headers={}, text="minimum of 15 seconds")
        failed.json.side_effect = ValueError("not json")
        succeeded = Mock(
            status_code=200,
            headers={},
            text='{"organic": []}',
        )
        succeeded.json.return_value = {"organic": []}
        with patch.object(config, "BRIGHTDATA_API_KEY", "key"), patch.object(
            config, "BRIGHTDATA_REQUEST_BUDGET", 5,
        ), patch.object(
            config, "BRIGHTDATA_REQUESTS_PER_MINUTE", 0,
        ), patch.object(config, "MAX_RETRIES", 1), patch(
            "modules.search.requests.post", side_effect=[failed, succeeded],
        ), patch("modules.search.runtime.wait_for_request_slot"), patch(
            "modules.search.time.sleep",
        ) as sleep:
            results = search._brightdata_text("nova official website")
        self.assertEqual(results, [])
        sleep.assert_called_once_with(15.0)
        counters = runtime.snapshot()["counters"]
        self.assertEqual(counters["api.brightdata.queries"], 1)
        self.assertEqual(counters["api.brightdata.requests"], 2)
        self.assertEqual(counters["api.brightdata.retries"], 1)


if __name__ == "__main__":
    unittest.main()
