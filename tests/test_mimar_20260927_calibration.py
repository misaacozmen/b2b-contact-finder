"""IP-8 truth-set and calibration contracts."""

from __future__ import annotations

import pytest

import config
from modules import calibration, reference_inputs, runtime, scorer, search
from tools import calibrate_candidates


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


def test_brand_match_accepts_tld_variant_and_rejects_other_brand():
    assert calibration.brand_match("sigmakarli.com", "sigmakarli.com.tr") is True
    assert calibration.brand_match("tetamek.com.tr", "tetamekmuhendislik.com.tr") is False


def test_wilson_lower_bound_reference_values():
    assert calibration.wilson_lower_bound(42, 45) == pytest.approx(0.8214, abs=0.0005)
    assert calibration.wilson_lower_bound(0, 0) == 0.0


def test_candidate_calibration_selection_is_deterministic():
    records = [
        {
            "labelled": True, "split": "cal", "company": "Acme Industrial",
            "truth_domain": "acme.com", "raw_results": [
                {"domain": "acme.com", "url": "https://acme.com/", "title": "Acme", "rank": 1},
            ],
        },
        {
            "labelled": True, "split": "hold", "company": "Beta Systems",
            "truth_domain": "beta.com", "raw_results": [
                {"domain": "beta.com", "url": "https://beta.com/", "title": "Beta", "rank": 1},
            ],
        },
    ]

    first = calibrate_candidates.calibrate(records)
    second = calibrate_candidates.calibrate(records)

    assert first == second
    assert first["selected"] == {"L": 5, "K": 3}


def test_brand_prefix_admission_admits_airpak():
    company = "AİRPAK HAVALANDIRMA VE FİLTRE SİS. SAN. VE TİC. LTD. ŞTİ."
    candidates = {}
    runtime.reset()

    search._add_search_results(
        candidates,
        company,
        "airpak havalandırma filtre resmi web sitesi",
        [{"href": "https://www.airpak.com.tr/", "title": "AİRPAK", "body": ""}],
    )

    assert candidates["airpak.com.tr"]["score"] >= config.MIN_ACCEPT_SCORE
    assert "brand_prefix_admission" in candidates["airpak.com.tr"]["reason"]


def test_brand_prefix_admission_respects_rank_and_token_length(monkeypatch):
    company = "AİRPAK HAVALANDIRMA VE FİLTRE SİS. SAN. VE TİC. LTD. ŞTİ."
    monkeypatch.setattr(config, "CANDIDATE_BRAND_PREFIX_MAX_RANK", 3)
    monkeypatch.setattr(config, "CANDIDATE_BRAND_PREFIX_MIN_TOKEN_LEN", 4)

    assert search._brand_prefix_admission(company, "airpak.com.tr", 4, {}) is False
    assert search._brand_prefix_admission(company, "airpak.com.tr", 1, {}) is True
    monkeypatch.setattr(config, "CANDIDATE_BRAND_PREFIX_MIN_TOKEN_LEN", 7)
    assert search._brand_prefix_admission(company, "airpak.com.tr", 1, {}) is False


def test_brand_prefix_admission_never_admits_source_host_or_directory():
    company = "AİRPAK HAVALANDIRMA VE FİLTRE SİS. SAN. VE TİC. LTD. ŞTİ."

    assert search._brand_prefix_admission(
        company, "sites.tuyap.com.tr", 1, {"_source_hosts": ["tuyap.com.tr"]},
    ) is False
    assert search._brand_prefix_admission(company, "woodtechistanbul.com", 1, {}) is False
