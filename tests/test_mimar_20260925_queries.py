from __future__ import annotations

from types import SimpleNamespace

import pytest
from ddgs.exceptions import DDGSException

import config
from modules import discovery_rules, pipeline_runner, query_planner, runtime, scorer, search


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("AİRPAK HAVALANDIRMA VE FİLTRE SİS. SAN. VE TİC. LTD. ŞTİ.", "airpak havalandırma filtre"),
        ("ADEKO GRUP BİLİŞİM SAN. VE TİC. LTD. ŞTİ.", "adeko grup bilişim"),
        ("ABM MAKİNE SAN. VE TİC. A.Ş.", "abm makine"),
        ("ÇETİNMAK MAKİNE SANAYİ TİCARET ANONİM ŞİRKETİ", "çetinmak makine"),
    ],
)
def test_search_display_core_examples(company, expected):
    assert scorer.search_display_core(company) == expected


def test_tr_lower_never_emits_combining_dot():
    value = scorer.tr_lower("İSTANBUL IĞDIR")
    assert value == "istanbul ığdır"
    assert "\u0307" not in value


def test_primary_queries_never_contain_urls():
    queries = discovery_rules.primary_queries(
        "Example Company",
        {
            "listed_website": "https://fair.example/",
            "website": "https://company.example/",
            "profile_url": "https://fair.example/profile",
        },
    )
    assert queries
    assert all("http" not in query.casefold() for query in queries)


def test_first_query_is_unquoted_display_core():
    queries = discovery_rules.primary_queries(
        "AİRPAK HAVALANDIRMA VE FİLTRE SİS. SAN. VE TİC. LTD. ŞTİ.", {},
    )
    assert queries[0] == "airpak havalandırma filtre"


def test_no_official_website_for_tr_target(monkeypatch):
    monkeypatch.setattr(config, "TARGET_COUNTRY", "TR")
    primary = discovery_rules.primary_queries("Alpha Makine", {"city": "İstanbul"})
    fallback = discovery_rules.fallback_queries("Alpha Makine", {})
    adaptive = discovery_rules.adaptive_queries(
        "Alpha Makine", {}, related_name_hints=["Former Alpha"],
        evidence_gaps={"relationship_hint", "no_candidates"},
    )
    assert all("official website" not in query.casefold() for query in (*primary, *fallback, *adaptive))
    assert any("resmi web sitesi" in query for query in (*primary, *fallback, *adaptive))


def test_no_combining_dot_in_any_generated_query():
    values = [
        *discovery_rules.primary_queries("İSTANBUL IĞDIR", {"city": "İstanbul"}),
        *discovery_rules.fallback_queries("İSTANBUL IĞDIR", {}),
        *query_planner.adaptive_queries(
            "İSTANBUL IĞDIR", {}, related_name_hints=["İzmir Işık"],
            evidence_gaps={"relationship_hint", "no_candidates"},
        ),
    ]
    assert values
    assert all("\u0307" not in query for query in values)


def test_ddgs_falls_through_dead_backends(monkeypatch):
    seen = []

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def text(self, _query, *, max_results, backend):
            seen.append(backend)
            if backend in {"duckduckgo", "google"}:
                raise DDGSException("No results found")
            if backend == "bing":
                return [
                    {"title": "Example", "href": "https://example.com", "body": "Example company"}
                    for _ in range(3)
                ][:max_results]
            return []

    monkeypatch.setattr(search, "PREFERRED_BACKENDS", ["duckduckgo", "google", "bing"])
    monkeypatch.setattr(search, "FALLBACK_BACKENDS", [])
    monkeypatch.setattr(config, "FREE_SEARCH_MAX_BACKENDS_PER_QUERY", 3)
    monkeypatch.setattr(search, "DDGS", FakeDDGS)
    monkeypatch.setattr(search, "_serp_result_state", lambda *_args, **_kwargs: "COMPLETED")
    runtime.reset()
    result = search._ddgs_text("example query")
    assert result
    assert seen == ["duckduckgo", "google", "bing"]


def test_backend_order_demotes_dead_backend():
    runtime.reset()
    for _ in range(6):
        runtime.record_free_backend("bing", "empty")
    assert runtime.free_backend_order(["bing", "yandex", "brave"])[-1] == "bing"


def test_canary_skipped_offline(monkeypatch):
    monkeypatch.setenv("B2B_TEST_OFFLINE", "1")

    class NoNetwork:
        def __init__(self):
            raise AssertionError("offline canary must not create a DDGS client")

    monkeypatch.setattr(search, "DDGS", NoNetwork)
    assert search.free_search_canary() == {"status": "skipped_offline"}


def test_paid_plan_filters_url_queries():
    assert pipeline_runner._paid_plan_queries(
        ['"https://example.com"', "https://company.example", "normal query"],
        "AİRPAK HAVALANDIRMA VE FİLTRE SİS. SAN. VE TİC. LTD. ŞTİ.",
    ) == ["normal query"]
    assert pipeline_runner._paid_plan_queries(
        ["https://example.com"],
        "AİRPAK HAVALANDIRMA VE FİLTRE SİS. SAN. VE TİC. LTD. ŞTİ.",
    ) == ["airpak havalandırma filtre"]


@pytest.mark.parametrize("message", ["captcha", "expect_element", "navigation_timeout"])
def test_brightdata_captcha_header_is_retryable(message):
    response = SimpleNamespace(status_code=400, headers={"x-brd-error": message})
    assert search._brightdata_header_error(response)[3] is True
