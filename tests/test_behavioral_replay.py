import gzip
import json

import pytest
from ddgs.exceptions import DDGSException

import config
from modules import discovery_coverage, replay_snapshot, report, runtime, search
from tools.free_only_contract import expected_offline_run_config


SOURCE_ID = "fixture:source-1"


def _set_search_context():
    runtime.set_item_context(0, "free")
    runtime.set_source_record_id(SOURCE_ID)
    runtime.set_search_bucket("discovery")


def _load_snapshot(path):
    replay_snapshot.reset()
    replay_snapshot.load(path, max_uncompressed_bytes=1024 * 1024)


def test_free_search_trace_replays_logical_and_physical_reservations(tmp_path, monkeypatch):
    class EmptyDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, *args, **kwargs):
            return []

    monkeypatch.setattr(search, "DDGS", EmptyDDGS)
    config.SEARCH_CACHE_MODE = "refresh"
    config.CRAWL_CACHE_MODE = "refresh"
    _set_search_context()
    live = search._ddgs_text("Example Query")
    assert live.result_state == "SEARCH_EXHAUSTED"
    snapshot = tmp_path / "replay.json.gz"
    replay_snapshot.write(snapshot)

    class NetworkForbiddenDDGS:
        def __enter__(self):
            raise AssertionError("replay attempted provider access")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(search, "DDGS", NetworkForbiddenDDGS)
    runtime.reset()
    config.SEARCH_CACHE_MODE = "replay"
    config.CRAWL_CACHE_MODE = "replay"
    _load_snapshot(snapshot)
    _set_search_context()
    replayed = search._search_text("Example Query")
    capacity = runtime.free_search_capacity("discovery")
    assert replayed.result_state == live.result_state
    assert list(replayed) == list(live)
    assert capacity.logical_used == 1
    assert capacity.physical_used == 2
    assert discovery_coverage.payload()["replay_miss_count"] == 0


def test_failed_backend_order_and_safe_error_class_replay(tmp_path, monkeypatch):
    calls = []

    class RetryDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query, **kwargs):
            calls.append(kwargs["backend"])
            if len(calls) == 1:
                raise DDGSException("secret-token-must-not-be-recorded")
            return []

    monkeypatch.setattr(search, "DDGS", RetryDDGS)
    config.SEARCH_CACHE_MODE = "refresh"
    config.CRAWL_CACHE_MODE = "refresh"
    _set_search_context()
    search._ddgs_text("Retry Query")
    snapshot = tmp_path / "replay.json.gz"
    replay_snapshot.write(snapshot)
    with gzip.open(snapshot, "rt", encoding="utf-8") as handle:
        raw = handle.read()
    assert "secret-token-must-not-be-recorded" not in raw
    assert calls == ["duckduckgo", "google"]

    class ForbiddenDDGS:
        def __enter__(self):
            raise AssertionError("replay attempted provider access")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(search, "DDGS", ForbiddenDDGS)
    runtime.reset()
    config.SEARCH_CACHE_MODE = "replay"
    config.CRAWL_CACHE_MODE = "replay"
    _load_snapshot(snapshot)
    _set_search_context()
    replayed = search._search_text("Retry Query")
    assert replayed.result_state == "FAILED"
    capacity = runtime.free_search_capacity("discovery")
    assert capacity.physical_used == 2


def test_dns_true_and_false_results_replay_without_socket(tmp_path, monkeypatch):
    config.SEARCH_CACHE_MODE = "refresh"
    config.CRAWL_CACHE_MODE = "refresh"
    _set_search_context()
    monkeypatch.setattr(search.socket, "getaddrinfo", lambda *args, **kwargs: [("ok",)])
    assert search._domain_has_address("Example.COM") is True
    monkeypatch.setattr(search.socket, "getaddrinfo", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("not found")))
    assert search._domain_has_address("missing.example") is False
    snapshot = tmp_path / "dns.json.gz"
    replay_snapshot.write(snapshot)

    runtime.reset()
    config.SEARCH_CACHE_MODE = "replay"
    config.CRAWL_CACHE_MODE = "replay"
    _load_snapshot(snapshot)
    _set_search_context()
    monkeypatch.setattr(search.socket, "getaddrinfo", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DNS in replay")))
    assert search._domain_has_address("example.com") is True
    assert search._domain_has_address("missing.example") is False
    assert discovery_coverage.payload()["replay_miss_count"] == 0


def test_missing_dns_record_is_replay_miss_without_network(monkeypatch):
    runtime.reset()
    replay_snapshot.reset()
    config.SEARCH_CACHE_MODE = "replay"
    config.CRAWL_CACHE_MODE = "replay"
    _set_search_context()
    monkeypatch.setattr(search.socket, "getaddrinfo", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DNS in replay")))
    assert search._domain_has_address("missing.example") is False
    assert discovery_coverage.payload()["replay_miss_count"] == 1


def test_replay_body_shards_survive_run_store_configuration(tmp_path):
    source_db = tmp_path / "source.sqlite3"
    replay_snapshot.configure_run_store(source_db, "source-run")
    replay_snapshot.record(
        "replay", "site", "body-key", 1,
        {"pages": [{"url": "https://example.com", "html": "recorded body"}]},
    )
    snapshot = tmp_path / "snapshot.json.gz"
    replay_snapshot.write(snapshot)

    replay_snapshot.reset()
    replay_snapshot.load(snapshot, max_uncompressed_bytes=1024 * 1024)
    replay_snapshot.configure_run_store(tmp_path / "replay" / "progress.sqlite3", "replay-run")
    found, value = replay_snapshot.lookup("replay", "site", "body-key", 1)
    assert found is True
    assert value["pages"][0]["html"] == "recorded body"


def test_replay_body_sanitizer_preserves_page_semantics(tmp_path):
    source_db = tmp_path / "source.sqlite3"
    replay_snapshot.configure_run_store(source_db, "source-run")
    replay_snapshot.record(
        "crawl_cache", "site", "body-key", 1,
        {"pages": [{"url": "https://example.com", "html": (
            '<main>Example Brand contact info@example.com</main>'
            '<script>const signature = "public-js-marker";</script>'
        )}]},
    )
    snapshot = tmp_path / "snapshot.json.gz"
    replay_snapshot.write(snapshot)

    replay_snapshot.reset()
    replay_snapshot.load(snapshot, max_uncompressed_bytes=1024 * 1024)
    found, value = replay_snapshot.lookup("crawl_cache", "site", "body-key", 1)
    assert found is True
    assert "Example Brand" in value["pages"][0]["html"]
    assert "public-js-marker" not in value["pages"][0]["html"]


def test_report_wall_clock_is_replay_stable():
    assert report.build_report([], 0) == report.build_report([], 9999)


def test_offline_config_changes_only_the_two_cache_modes():
    live = {
        "search_cache_mode": "refresh",
        "crawl_cache_mode": "refresh",
        "paid_enabled": False,
        "finalize_without_paid": True,
        "budgets": {name: 0 for name in ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")},
        "effective_settings": {"search_cache_mode": "refresh", "crawl_cache_mode": "refresh", "other": 1},
    }
    expected = expected_offline_run_config(live)
    assert expected["search_cache_mode"] == "replay"
    assert expected["crawl_cache_mode"] == "replay"
    assert expected["effective_settings"]["search_cache_mode"] == "replay"
    assert expected["effective_settings"]["crawl_cache_mode"] == "replay"
    assert expected["effective_settings"]["other"] == 1
    assert live["search_cache_mode"] == "refresh"
