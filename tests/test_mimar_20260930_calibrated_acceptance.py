"""Talimat 17: calibrated reference-blind Stage A acceptance."""

from __future__ import annotations

import pytest

import config
import main
from modules import calibration, field_merge, reference_resolution, run_report


RULE_ID = "L3_R3_H2_Ccontact_N0_U0"


def _features(**overrides) -> dict:
    features = {
        "domain": "alfamakina.com.tr", "url": "https://alfamakina.com.tr/",
        "reachable": True, "s3": True, "s4": True, "conflict": False, "parked": False,
        "brand_prefix_len": 4, "rank_best": 1, "query_hits": 2,
        "tr_phone": True, "same_domain_email": False, "legacy_final_score": 60,
    }
    features.update(overrides)
    return features


def _observe(evaluation, url, metadata):
    return {"emails": ["info@alfamakina.com.tr"], "phones": ["0212 555 00 00"], "listed_email": ""}


def _stage_a_row() -> dict:
    return {
        "company": "Alfa Makina", "status": "REVIEW_NEEDED", "website": "",
        "email": "eski@baska.com.tr", "email_source_tier": "SITE", "reason": "no_candidate_passed",
    }


def test_rule_constants_match_calibrated_selection():
    assert config.CALIBRATED_ACCEPTANCE_RULE_ID == RULE_ID
    assert config.CALIBRATED_ACCEPTANCE_RULE == {"L": 3, "R": 3, "H": 2, "C": "contact", "N": 0, "U": 0}
    assert config.ENABLE_CALIBRATED_ACCEPTANCE is True


def test_sort_key_prefers_rank_then_hits_then_score_then_domain():
    items = [
        _features(domain="c.com.tr", rank_best=2, query_hits=5, legacy_final_score=100),
        _features(domain="b.com.tr", rank_best=1, query_hits=2, legacy_final_score=90),
        _features(domain="a.com.tr", rank_best=1, query_hits=3, legacy_final_score=10),
        _features(domain="d.com.tr", rank_best=1, query_hits=2, legacy_final_score=90),
    ]
    ordered = [item["domain"] for item in sorted(items, key=calibration.acceptance_sort_key)]
    assert ordered == ["a.com.tr", "b.com.tr", "d.com.tr", "c.com.tr"]


def test_brand_prefix_top3_counts_distinct_top_ranked_prefix_domains():
    features = [
        _features(domain="alfa.com.tr", rank_best=1),
        _features(domain="alfa.com.tr", rank_best=2),
        _features(domain="alfamakina.com.tr", rank_best=3),
        _features(domain="alfagrup.com.tr", rank_best=4),
        _features(domain="beta.com.tr", rank_best=1, brand_prefix_len=0),
    ]
    assert calibration.brand_prefix_top3(features) == 2


def test_accepts_best_candidate_when_stage_a_not_ok(monkeypatch):
    monkeypatch.setattr(reference_resolution, "observe", _observe)
    row = _stage_a_row()
    entries = [
        {"features": _features(domain="alfa.com.tr", url="https://alfa.com.tr/", rank_best=2), "evaluation": {}},
        {"features": _features(), "evaluation": {}},
    ]
    result = main._apply_calibrated_acceptance(row, entries, {})
    assert result["website"] == "https://alfamakina.com.tr/"
    assert result["selected_website"] == "https://alfamakina.com.tr/"
    assert result["website_source"] == "OWN_SEARCH_CALIBRATED"
    assert result["status"] == "OK_MEDIUM_CONFIDENCE"
    assert result["email"] == "info@alfamakina.com.tr"
    assert result["email_source_tier"] == "SITE"
    assert result["phone"]
    assert result["phone_source_tier"] == "SITE"
    assert result["reason"].startswith(f"calibrated_acceptance:{RULE_ID}; ")
    assert row["status"] == "REVIEW_NEEDED" and row["website"] == ""


def test_ok_row_and_rejected_candidates_are_unchanged(monkeypatch):
    monkeypatch.setattr(reference_resolution, "observe", _observe)
    ok_row = {"company": "Alfa Makina", "status": "OK_HIGH_CONFIDENCE", "website": "https://alfa.com.tr/"}
    assert main._apply_calibrated_acceptance(ok_row, [{"features": _features(), "evaluation": {}}], {}) is ok_row
    for features in (
        _features(brand_prefix_len=2),
        _features(query_hits=1),
        _features(rank_best=4),
        _features(tr_phone=False, same_domain_email=False),
        _features(reachable=False),
        _features(conflict=True),
    ):
        row = _stage_a_row()
        assert main._apply_calibrated_acceptance(row, [{"features": features, "evaluation": {}}], {}) is row
    row = _stage_a_row()
    assert main._apply_calibrated_acceptance(row, [], {}) is row


def test_flag_off_disables_acceptance(monkeypatch):
    monkeypatch.setattr(reference_resolution, "observe", _observe)
    monkeypatch.setattr(config, "ENABLE_CALIBRATED_ACCEPTANCE", False)
    row = _stage_a_row()
    assert main._apply_calibrated_acceptance(row, [{"features": _features(), "evaluation": {}}], {}) is row


def test_calibrated_source_is_medium_confidence():
    row = {
        "website": "https://alfamakina.com.tr/", "website_source": "OWN_SEARCH_CALIBRATED",
        "status": "OK_MEDIUM_CONFIDENCE", "email": "info@alfamakina.com.tr", "email_source_tier": "SITE",
        "phone": "0212 555 00 00", "phone_source_tier": "SITE",
    }
    assert field_merge.field_confidence(row, "website") == "MEDIUM"
    assert field_merge.field_confidence(row, "email") == "MEDIUM"
    assert field_merge.field_confidence(row, "phone") == "MEDIUM"
    assert field_merge.ready_for_publication(row) is True


def test_matching_reference_upgrades_calibrated_row():
    row = {
        "company": "Alfa Makina", "website": "https://alfamakina.com.tr/",
        "website_source": "OWN_SEARCH_CALIBRATED", "status": "OK_MEDIUM_CONFIDENCE",
    }

    def fail_evaluate(*_args, **_kwargs):
        pytest.fail("reference must not be crawled when it matches Stage A")

    result = reference_resolution.complete_with_reference(
        0, "Alfa Makina", None, row, {"website": "https://www.alfamakina.com.tr"}, evaluate_fn=fail_evaluate,
    )
    assert result["website_source"] == "OWN_SEARCH+REFERENCE"
    assert result["reference_tier"] == "REFERENCE_MATCHES_OWN_SEARCH"


def test_detail_label_for_calibrated_source():
    assert run_report._detail_value("Web kaynağı", "OWN_SEARCH_CALIBRATED") == "Arama (kalibre kural)"
