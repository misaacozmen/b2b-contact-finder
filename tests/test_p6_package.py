import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
