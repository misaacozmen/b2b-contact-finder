"""IP-8 truth-set and calibration contracts."""

from __future__ import annotations

import pytest

import config
from modules import reference_inputs, runtime, scorer, search


def test_strip_references_keeps_source_hosts_for_exclusion_only():
    source = {
        "listing_url": "https://sites.tuyap.com.tr/exhibitors/123",
        "profile_url": "https://test.tuyap.online/company/123",
        "source_detail_url": "https://sites.tuyap.com.tr/detail/123",
        "website": "https://company.example/",
    }

    blind = reference_inputs.strip_references(source)

    assert blind["_source_hosts"] == ["tuyap.com.tr", "tuyap.online"]
    assert all(blind[key] == "" for key in ("listing_url", "profile_url", "source_detail_url", "website"))
    assert source["listing_url"] == "https://sites.tuyap.com.tr/exhibitors/123"


def test_source_host_result_is_not_admitted_as_candidate(monkeypatch):
    normalized = {
        "resolution_attempted": False,
        "resolution_status": "resolved",
        "resolved_url": "https://sites.tuyap.com.tr/exhibitors/alpha",
        "raw_url": "https://sites.tuyap.com.tr/exhibitors/alpha",
        "display_link": "sites.tuyap.com.tr",
        "query_id": "query-id",
        "provider_fields": {"title": "Alpha Makine", "body": ""},
    }
    monkeypatch.setattr(search, "normalize_serp_results", lambda *_args, **_kwargs: ([normalized], {}))
    runtime.reset()
    candidates = {}

    search._add_search_results(
        candidates,
        "Alpha Makine",
        "alpha makine resmi web sitesi",
        [{"href": normalized["resolved_url"], "title": "Alpha Makine"}],
        {"_source_hosts": ["tuyap.com.tr"]},
    )

    assert candidates == {}


@pytest.mark.parametrize(
    "domain",
    [
        "sites.tuyap.com.tr",
        "test.tuyap.online",
        "woodtechistanbul.com",
        "intermobistanbul.com",
        "avrasyapencerekapifuari.com",
        "eurasiaglassfair.com",
    ],
)
def test_organizer_domains_are_excluded(domain):
    assert scorer.is_excluded_domain(domain)


def test_canary_alive_when_second_query_answers(monkeypatch):
    seen = []

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def text(self, query, *, max_results, backend):
            seen.append((query, max_results, backend))
            return [] if query == config.FREE_SEARCH_CANARY_QUERY else [{"title": "answer"}]

    monkeypatch.delenv("B2B_TEST_OFFLINE", raising=False)
    monkeypatch.setattr(search, "PREFERRED_BACKENDS", ["bing"])
    monkeypatch.setattr(search, "FALLBACK_BACKENDS", [])
    monkeypatch.setattr(search, "DDGS", FakeDDGS)
    runtime.reset()

    result = search.free_search_canary()

    assert result == {"status": "ok", "alive": ["bing"], "dead": []}
    assert [row[0] for row in seen] == [
        config.FREE_SEARCH_CANARY_QUERY,
        config.FREE_SEARCH_CANARY_QUERY_2,
    ]
