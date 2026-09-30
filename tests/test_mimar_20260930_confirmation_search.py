"""Talimat 18: confirmation queries for near-miss Stage A candidates."""

from __future__ import annotations

import config
from modules import confirmation_search, scorer, search
from tools import evaluate_confirmation_search


RULE = config.CALIBRATED_ACCEPTANCE_RULE


def _features(**overrides) -> dict:
    features = {
        "domain": "alfamakina.com.tr", "url": "https://alfamakina.com.tr/",
        "reachable": True, "s3": True, "s4": True, "conflict": False, "parked": False,
        "brand_prefix_len": 4, "rank_best": 1, "query_hits": 2,
        "tr_phone": True, "same_domain_email": False, "legacy_final_score": 60,
    }
    features.update(overrides)
    return features


def test_confirmation_queries_use_fixed_templates():
    company = "ALFA MAKİNA SAN. VE TİC. LTD. ŞTİ."
    brand = " ".join(scorer.primary_brand_tokens(company, limit=2)).strip()
    assert confirmation_search.confirmation_queries(company) == [f'"{brand}" iletişim', f"{brand} firma web sitesi"]
    assert confirmation_search.confirmation_queries("AB LTD. ŞTİ.") == []


def test_near_miss_only_when_rank_or_hits_block_the_rule():
    assert confirmation_search.near_miss(_features(), RULE) is False
    assert confirmation_search.near_miss(_features(query_hits=1), RULE) is True
    assert confirmation_search.near_miss(_features(rank_best=5), RULE) is True
    assert confirmation_search.near_miss(_features(rank_best=5, query_hits=1), RULE) is True
    assert confirmation_search.near_miss(_features(query_hits=1, brand_prefix_len=2), RULE) is False
    assert confirmation_search.near_miss(_features(query_hits=1, tr_phone=False), RULE) is False
    assert confirmation_search.near_miss(_features(query_hits=1, rank_best=None), RULE) is False


def test_domain_rank_finds_registrable_domain():
    results = [{"href": "https://baska.com.tr/"}, {"href": "https://www.alfamakina.com.tr/iletisim"}]
    assert confirmation_search.domain_rank(results, "alfamakina.com.tr") == 2
    assert confirmation_search.domain_rank(results, "gama.com.tr") is None
    assert confirmation_search.domain_rank([], "alfamakina.com.tr") is None


def test_apply_confirmation_adds_hits_without_mutating_input():
    original = _features(query_hits=1, rank_best=5)
    updated = confirmation_search.apply_confirmation(original, [2, None])
    assert (updated["query_hits"], updated["rank_best"], updated["confirmation_hits"]) == (2, 2, 1)
    assert (original["query_hits"], original["rank_best"]) == (1, 5)
    unchanged = confirmation_search.apply_confirmation(original, [None, None])
    assert (unchanged["query_hits"], unchanged["rank_best"], unchanged["confirmation_hits"]) == (1, 5, 0)


def _record(**overrides) -> dict:
    record = {
        "source_record_id": "test:alfa", "company": "ALFA MAKİNA SAN. VE TİC. LTD. ŞTİ.",
        "truth_domain": "alfamakina.com.tr", "labelled": True,
        "stage_a": {"status": "REVIEW_NEEDED", "website": ""},
        "stage_a_brand_prefix_top3": 1,
        "stage_a_candidates": [_features(query_hits=1)],
        "raw_results": [],
    }
    record.update(overrides)
    return record


def test_tool_counts_new_query_hits_and_scores_additions():
    calls = []

    def fake_search(query):
        calls.append(query)
        return [{"href": "https://www.alfamakina.com.tr/"}]

    result = evaluate_confirmation_search.evaluate([_record()], None, fake_search)
    assert len(calls) == 2
    assert result["summary"]["near_miss_firms"] == 1
    assert result["summary"]["added"] == 1
    assert result["summary"]["added_correct"] == 1
    assert result["details"][0]["prediction"] == "alfamakina.com.tr"


def test_tool_does_not_double_count_an_already_counted_query():
    company = "ALFA MAKİNA SAN. VE TİC. LTD. ŞTİ."
    first, second = confirmation_search.confirmation_queries(company)
    record = _record(
        stage_a_candidates=[_features(query_hits=0)],
        raw_results=[{"query_fingerprint": search._query_fingerprint(first), "domain": "alfamakina.com.tr", "rank": 1}],
    )
    result = evaluate_confirmation_search.evaluate([record], None, lambda query: [{"href": "https://alfamakina.com.tr/"}])
    assert result["summary"]["added"] == 0
    assert second


def test_tool_skips_firms_that_already_have_a_prediction():
    calls = []
    record = _record(stage_a={"status": "OK_HIGH_CONFIDENCE", "website": "https://alfamakina.com.tr/"})
    result = evaluate_confirmation_search.evaluate([record], None, lambda query: calls.append(query) or [])
    assert calls == []
    assert result["summary"]["no_prediction"] == 0
