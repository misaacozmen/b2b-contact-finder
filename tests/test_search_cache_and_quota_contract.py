from __future__ import annotations

import concurrent.futures
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from modules import checkpoint, runtime, search


def _typed(state: str, reason: str = "test") -> search.SearchResults:
    values = [{"href": "https://example.test", "title": "Example", "body": ""}] if state == "COMPLETED" else []
    return search.SearchResults(values, "live", "ddgs", result_state=state, reason=reason, call_ids=(f"call-{state}",))


@pytest.mark.parametrize("state", ["COMPLETED", "EMPTY", "FAILED", "UNKNOWN", "BLOCKED_BUDGET", "NOT_ENABLED", "REPLAY_MISS"])
def test_typed_search_state_cache_matrix(tmp_path: Path, state: str):
    with patch.object(config, "SEARCH_CACHE_DIR", tmp_path), patch.object(config, "SEARCH_CACHE_MODE", "refresh"), patch.object(
        search, "_search_text_live", return_value=_typed(state)
    ):
        result = search._search_text(f"query-{state}")
    assert result.result_state == state
    saved = list((tmp_path / "serp").glob("*.json.gz"))
    assert bool(saved) is (state in {"COMPLETED", "EMPTY"})


@pytest.mark.parametrize("values", [[], [{"href": "https://example.test"}]])
def test_untyped_live_list_is_contract_error_and_never_cached(tmp_path: Path, values):
    with patch.object(config, "SEARCH_CACHE_DIR", tmp_path), patch.object(config, "SEARCH_CACHE_MODE", "refresh"), patch.object(
        search, "_search_text_live", return_value=values
    ):
        with pytest.raises(search.SearchBackendError, match="SearchResults"):
            search._search_text("untyped")
    assert not list((tmp_path / "serp").glob("*.json.gz"))


def _seed_quota(tmp_path: Path) -> None:
    db = tmp_path / "quota.sqlite3"
    with patch.object(config, "PROGRESS_DB_FILE", db):
        checkpoint.initialize_schema(db)
        checkpoint.initialize_run(
            run_id="quota-run", input_hash="h", run_signature="s", context={"phase": "FREE"},
            budgets={provider: 0 for provider in checkpoint.CANONICAL_PROVIDERS},
            items=[{"item_index": 0, "source_record_id": "src:quota"}],
        )
    return db


def test_durable_discovery_and_targeted_quota_is_bounded(tmp_path: Path):
    db = _seed_quota(tmp_path)
    with patch.object(config, "PROGRESS_DB_FILE", db):
        runtime.reset()
        runtime.configure_durable_run("quota-run", {})
        runtime.set_item_context(0, "free", "src:quota")
        runtime.set_search_bucket("discovery")
        assert sum(runtime.reserve_search_query(0) for _ in range(7)) == 6
        runtime.set_search_bucket("targeted")
        assert sum(runtime.reserve_search_query(0) for _ in range(5)) == 4
        with __import__("sqlite3").connect(db) as connection:
            row = connection.execute("SELECT used,discovery_used,targeted_used FROM free_query_usage").fetchone()
        assert row == (10, 6, 4)


def test_source_index_mismatch_is_explicit(tmp_path: Path):
    db = _seed_quota(tmp_path)
    with patch.object(config, "PROGRESS_DB_FILE", db):
        with pytest.raises(RuntimeError, match="invariant"):
            checkpoint.reserve_free_search_query(run_id="quota-run", item_index=0, source_record_id="src:other", limit=10)


def test_quota_survives_process_like_restart(tmp_path: Path):
    db = _seed_quota(tmp_path)
    with patch.object(config, "PROGRESS_DB_FILE", db):
        runtime.reset()
        runtime.configure_durable_run("quota-run", {})
        runtime.set_item_context(0, "free", "src:quota")
        assert sum(runtime.reserve_search_query(0) for _ in range(6)) == 6
        runtime.set_search_bucket("targeted")
        assert sum(runtime.reserve_search_query(0) for _ in range(4)) == 4
        runtime.reset()
        runtime.configure_durable_run("quota-run", {})
        runtime.set_item_context(0, "free", "src:quota")
        assert not runtime.reserve_search_query(0)


def test_eight_concurrent_workers_cannot_exceed_quota(tmp_path: Path):
    db = _seed_quota(tmp_path)
    def reserve(_worker: int) -> int:
        return sum(int(checkpoint.reserve_free_search_query(run_id="quota-run", item_index=0, source_record_id="src:quota", limit=10)) for _ in range(4))
    with patch.object(config, "PROGRESS_DB_FILE", db), concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(reserve, range(8)))
    assert sum(accepted) == 10
