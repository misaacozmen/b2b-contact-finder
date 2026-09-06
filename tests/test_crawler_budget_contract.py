from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config
from modules import checkpoint, crawler, runtime, run_context


def _run_db(tmp_path: Path) -> Path:
    db = tmp_path / "crawler.sqlite3"
    with patch.object(config, "PROGRESS_DB_FILE", db):
        checkpoint.initialize_schema(db)
        checkpoint.initialize_run(
            run_id="crawler-run", input_hash="h", run_signature="s", context={"phase": "FREE"},
            budgets={provider: 0 for provider in checkpoint.CANONICAL_PROVIDERS},
            items=[{"item_index": 0, "source_record_id": "src:0"}, {"item_index": 1, "source_record_id": "src:1"}],
        )
    return db


def test_crawler_buckets_are_durable_and_per_item(tmp_path: Path):
    db = _run_db(tmp_path)
    with patch.object(config, "PROGRESS_DB_FILE", db), patch.object(config, "CRAWLER_HTTP_REQUEST_BUDGET", 0):
        runtime.reset()
        runtime.configure_durable_run("crawler-run", {})
        runtime.set_item_context(0, "free", "src:0")
        assert sum(runtime.reserve_crawler_http(0, bucket="source_profile_http") for _ in range(3)) == 2
        assert sum(runtime.reserve_crawler_http(0, bucket="identity_http") for _ in range(10)) == 9
        runtime.set_item_context(1, "free", "src:1")
        assert runtime.reserve_crawler_http(0, bucket="source_profile_http")


def test_crawler_budget_exception_is_typed_and_exact_reason_is_preserved():
    with patch.object(crawler.network_guard, "validate_public_http_url", return_value=(True, "")), patch.object(
        crawler.runtime, "reserve_crawler_http", return_value=False
    ), patch.object(crawler.runtime, "crawler_budget_reason", return_value="crawler_http_budget_exhausted:contact_http:5/5"):
        with pytest.raises(crawler.CrawlerBudgetExhausted, match="contact_http"):
            crawler._request_with_safe_redirects("https://example.test", verify=True, bucket="contact_http")
    with patch.object(crawler, "_fetch", side_effect=crawler.CrawlerBudgetExhausted("crawler_http_budget_exhausted:global:19/19")):
        assert crawler._try_fetch("https://example.test") == (None, "crawler_http_budget_exhausted:global:19/19")


def test_per_item_budget_block_does_not_attempt_the_same_bucket_again():
    crawler._FETCH_STATE.budget_blocked_buckets = set()
    crawler._FETCH_STATE.budget_block_reasons = {}
    crawler._FETCH_STATE.global_budget_blocked = False
    with patch.object(
        crawler, "_try_fetch",
        return_value=(None, "crawler_http_budget_exhausted:identity_http:9/9"),
    ) as fetch:
        first = crawler._bucketed_try_fetch("https://example.test", "identity_http")
        second = crawler._bucketed_try_fetch("http://example.test", "identity_http")
    assert first[1] == second[1] == "crawler_http_budget_exhausted:identity_http:9/9"
    fetch.assert_called_once_with("https://example.test")


def test_run_config_hash_includes_computed_crawler_cap_and_budget_values():
    first = run_context.RunConfig.from_config(paid_enabled=False, input_count=120, unique_profile_host_count=60)
    second = run_context.RunConfig.from_config(paid_enabled=False, input_count=121, unique_profile_host_count=60)
    assert first.as_dict()["effective_settings"]["crawler_global_http_budget"] == 19 * 120 + 2 * 60
    assert first.sha256 != second.sha256
