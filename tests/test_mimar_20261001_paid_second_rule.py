"""Talimat 20: second calibrated rule for PAID (Bright Data) candidates."""

from __future__ import annotations

import logging

import config
import main
from modules import google_places, hunter, reference_resolution, stage_a_features
from tools import measure_paid_acceptance


COMPANY = "ALFA MAKİNA SAN. VE TİC. LTD. ŞTİ."
FIRST_ID = "L3_R3_H2_Ccontact_N0_U0"
SECOND_ID = "L3_R2_H1_Ccontact_N0_U0"


def _features(**overrides) -> dict:
    features = {
        "domain": "alfamakina.com.tr", "url": "https://alfamakina.com.tr/",
        "reachable": True, "s3": True, "s4": True, "conflict": False, "parked": False,
        "brand_prefix_len": 4, "rank_best": 1, "query_hits": 1,
        "tr_phone": True, "same_domain_email": False, "legacy_final_score": 60,
    }
    features.update(overrides)
    return features


def _observe(evaluation, url, metadata):
    return {"emails": ["info@alfamakina.com.tr"], "phones": ["0212 555 00 00"], "listed_email": ""}


def _paid_row(monkeypatch, paid_features, *, second=True, first=True) -> dict:
    monkeypatch.setattr(config, "ENABLE_PAID_CALIBRATED_ACCEPTANCE", first)
    monkeypatch.setattr(config, "ENABLE_PAID_SECOND_RULE", second)
    monkeypatch.setattr(reference_resolution, "observe", _observe)
    monkeypatch.setattr(google_places, "is_enabled", lambda: False)
    monkeypatch.setattr(hunter, "email_gap_fill_enabled", lambda: False)
    searched = {"company": COMPANY, "status": "REVIEW_NEEDED", "website": "", "reason": "no_candidate_passed"}
    monkeypatch.setattr(main, "_process_company_core", lambda *args, **kwargs: (0, dict(searched)))
    monkeypatch.setattr(
        stage_a_features, "collect",
        lambda: [{"features": features, "evaluation": {}} for features in paid_features],
    )
    prior = {"company": COMPANY, "status": "REVIEW_NEEDED", "website": "", "stage_a_candidates": []}
    return main.paid_gap_fill(0, COMPANY, logging.getLogger("t20"), "", {"_prior_row": prior})[1]


def test_second_rule_constants_and_default_off():
    assert config.PAID_SECOND_RULE_ID == SECOND_ID
    assert config.PAID_SECOND_RULE == {"L": 3, "R": 2, "H": 1, "C": "contact", "N": 0, "U": 0}
    assert config.ENABLE_PAID_SECOND_RULE is False


def test_single_google_hit_at_rank_two_is_accepted_by_second_rule(monkeypatch):
    row = _paid_row(monkeypatch, [_features(rank_best=2)])
    assert row["website"] == "https://alfamakina.com.tr/"
    assert row["website_source"] == "PAID_BRIGHTDATA_CALIBRATED"
    assert row["website_confidence"] == "MEDIUM"
    assert row["reason"].startswith(f"calibrated_acceptance:{SECOND_ID}; ")


def test_second_rule_flag_off_keeps_single_hit_rejected(monkeypatch):
    assert not _paid_row(monkeypatch, [_features()], second=False).get("website")


def test_second_rule_needs_first_paid_rule_enabled(monkeypatch):
    assert not _paid_row(monkeypatch, [_features()], first=False).get("website")


def test_first_rule_takes_precedence(monkeypatch):
    row = _paid_row(monkeypatch, [_features(query_hits=2)])
    assert row["reason"].startswith(f"calibrated_acceptance:{FIRST_ID}; ")


def test_second_rule_rejects_rank_three_and_missing_contact(monkeypatch):
    assert not _paid_row(monkeypatch, [_features(rank_best=3)]).get("website")
    assert not _paid_row(monkeypatch, [_features(tr_phone=False, same_domain_email=False)]).get("website")
    assert not _paid_row(monkeypatch, [_features(brand_prefix_len=2)]).get("website")


def test_free_stage_acceptance_still_uses_first_rule_only(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PAID_SECOND_RULE", True)
    monkeypatch.setattr(reference_resolution, "observe", _observe)
    row = {"company": COMPANY, "status": "REVIEW_NEEDED", "website": "", "reason": "no_candidate_passed"}
    assert main._apply_calibrated_acceptance(row, [{"features": _features(), "evaluation": {}}], {}) is row


def test_measurement_tool_separates_second_rule_additions():
    firms = [
        {"company": name, "set": "a", "source_record_id": f"a:{index}", "truth_domain": "alfamakina.com.tr"}
        for index, name in enumerate(("Alfa Bir", "Alfa Iki"))
    ]
    rows = [
        {"company": "Alfa Bir", "website": "https://alfamakina.com.tr/", "website_source": "PAID_BRIGHTDATA_CALIBRATED",
         "status": "OK_MEDIUM_CONFIDENCE", "reason": f"calibrated_acceptance:{FIRST_ID}; x", "paid_stage_a_candidates": []},
        {"company": "Alfa Iki", "website": "https://alfamakina.com.tr/", "website_source": "PAID_BRIGHTDATA_CALIBRATED",
         "status": "OK_MEDIUM_CONFIDENCE", "reason": f"calibrated_acceptance:{SECOND_ID};", "paid_stage_a_candidates": []},
    ]
    summary = measure_paid_acceptance.evaluate(rows, firms, None, fetch=lambda domain: {})["summary"]
    assert summary["PAID_CALIBRATED"] == {"count": 1, "correct": 1, "correct_strict": 1}
    assert summary["PAID_CALIBRATED_2"] == {"count": 1, "correct": 1, "correct_strict": 1}
    assert summary["paid_second_precision"] == 1.0
