"""Talimat 19: calibrated acceptance on PAID (Bright Data) candidates."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

import config
import main
from modules import calibration, excel, field_merge, google_places, hunter, reference_resolution, run_report, stage_a_features
from tools import measure_paid_acceptance


COMPANY = "ALFA MAKİNA SAN. VE TİC. LTD. ŞTİ."
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


def _paid_row(monkeypatch, paid_features, *, prior_candidates=(), searched=None, enabled=True) -> dict:
    monkeypatch.setattr(config, "ENABLE_PAID_CALIBRATED_ACCEPTANCE", enabled)
    monkeypatch.setattr(reference_resolution, "observe", _observe)
    monkeypatch.setattr(google_places, "is_enabled", lambda: False)
    monkeypatch.setattr(hunter, "email_gap_fill_enabled", lambda: False)
    searched_row = searched or {"company": COMPANY, "status": "REVIEW_NEEDED", "website": "", "reason": "no_candidate_passed"}
    monkeypatch.setattr(main, "_process_company_core", lambda *args, **kwargs: (0, dict(searched_row)))
    monkeypatch.setattr(
        stage_a_features, "collect",
        lambda: [{"features": features, "evaluation": {}} for features in paid_features],
    )
    prior = {"company": COMPANY, "status": "REVIEW_NEEDED", "website": "", "stage_a_candidates": list(prior_candidates)}
    return main.paid_gap_fill(0, COMPANY, logging.getLogger("t19"), "", {"_prior_row": prior})[1]


def test_paid_flag_defaults_off():
    assert config.ENABLE_PAID_CALIBRATED_ACCEPTANCE is False


def test_free_evidence_adds_hits_for_same_domain_without_mutating_input():
    paid = _features(query_hits=1, rank_best=5)
    free = [_features(domain="www.alfamakina.com.tr", query_hits=1, rank_best=2)]
    updated = calibration.with_free_evidence(paid, free)
    assert (updated["query_hits"], updated["rank_best"], updated["free_query_hits"]) == (2, 2, 1)
    assert (paid["query_hits"], paid["rank_best"]) == (1, 5)


def test_free_evidence_ignores_other_domains():
    paid = _features(query_hits=1, rank_best=5)
    for free in ([_features(domain="baska.com.tr", query_hits=3)], [], None):
        updated = calibration.with_free_evidence(paid, free)
        assert (updated["query_hits"], updated["rank_best"], updated["free_query_hits"]) == (1, 5, 0)


def test_flag_on_accepts_paid_candidate(monkeypatch):
    row = _paid_row(monkeypatch, [_features()])
    assert row["website"] == "https://alfamakina.com.tr/"
    assert row["website_source"] == "PAID_BRIGHTDATA_CALIBRATED"
    assert row["website_confidence"] == "MEDIUM"
    assert row["email"] == "info@alfamakina.com.tr"
    assert row["reason"].startswith(f"calibrated_acceptance:{RULE_ID}; ")
    assert row["paid_stage_a_candidates"][0]["domain"] == "alfamakina.com.tr"


def test_flag_off_records_features_only(monkeypatch):
    row = _paid_row(monkeypatch, [_features()], enabled=False)
    assert not row.get("website")
    assert row["paid_stage_a_candidates"][0]["free_query_hits"] == 0


def test_free_evidence_completes_query_hits(monkeypatch):
    paid = [_features(query_hits=1)]
    assert not _paid_row(monkeypatch, paid).get("website")
    row = _paid_row(monkeypatch, paid, prior_candidates=[_features(query_hits=1, rank_best=4)])
    assert row["website_source"] == "PAID_BRIGHTDATA_CALIBRATED"
    assert (row["paid_stage_a_candidates"][0]["query_hits"], row["paid_stage_a_candidates"][0]["free_query_hits"]) == (2, 1)


def test_legacy_ok_row_keeps_paid_brightdata_source(monkeypatch):
    searched = {"company": COMPANY, "status": "OK_HIGH_CONFIDENCE", "website": "https://alfa.com.tr/", "reason": "legacy"}
    row = _paid_row(monkeypatch, [_features()], searched=searched)
    assert row["website"] == "https://alfa.com.tr/"
    assert row["website_source"] == "PAID_BRIGHTDATA"
    assert row["website_confidence"] == "HIGH"


def test_paid_calibrated_source_confidence_and_label():
    row = {"website": "https://alfamakina.com.tr/", "website_source": "PAID_BRIGHTDATA_CALIBRATED", "status": "OK_MEDIUM_CONFIDENCE"}
    assert field_merge.field_confidence(row, "website") == "MEDIUM"
    assert run_report._detail_value("Web kaynağı", "PAID_BRIGHTDATA_CALIBRATED") == "Bright Data (kalibre kural)"


def _truth(company: str, source_record_id: str, **overrides) -> dict:
    record = {
        "source_record_id": source_record_id, "company": company, "truth_domain": "alfamakina.com.tr",
        "labelled": True, "stage_a": {"status": "REVIEW_NEEDED", "website": ""},
        "stage_a_brand_prefix_top3": 0, "stage_a_candidates": [],
    }
    record.update(overrides)
    return record


def test_select_firms_keeps_labelled_rows_without_prediction():
    truth_sets = {
        "a": [
            _truth("Alfa Makina", "a:1"),
            _truth("Beta Makina", "a:2", stage_a={"status": "OK_HIGH_CONFIDENCE", "website": "https://beta.com.tr/"}),
            _truth("Gama Makina", "a:3", labelled=False),
            _truth("Delta Makina", "a:4", stage_a_candidates=[_features(domain="delta.com.tr")]),
        ],
        "b": [_truth("Epsilon  Makina", "b:1")],
    }
    firms = measure_paid_acceptance.select_firms(truth_sets)
    assert [(firm["set"], firm["source_record_id"]) for firm in firms] == [("a", "a:1"), ("b", "b:1")]
    with pytest.raises(ValueError):
        measure_paid_acceptance.select_firms({"a": [_truth("Alfa Makina", "a:1")], "b": [_truth("alfa  makina", "b:9")]})


def test_written_input_is_readable_by_pipeline(tmp_path):
    path = tmp_path / "olcum.xlsx"
    measure_paid_acceptance.write_input([{"company": "Alfa Makina"}, {"company": "Beta Makina"}], path)
    assert [record["company"] for record in excel.read_company_records(path)] == ["Alfa Makina", "Beta Makina"]


def test_evaluate_scores_categories_and_adjudicates_new_pairs():
    firms = [
        {"company": name, "set": "a", "source_record_id": f"a:{index}", "truth_domain": "alfamakina.com.tr"}
        for index, name in enumerate(("Alfa Bir", "Alfa Iki", "Alfa Uc", "Alfa Dort", "Alfa Bes", "Alfa Alti"))
    ]
    rows = [
        {"company": "Alfa Bir", "website": "https://alfamakina.com.tr/", "website_source": "OWN_SEARCH_CALIBRATED", "status": "OK_MEDIUM_CONFIDENCE"},
        {"company": "alfa  iki", "website": "https://www.alfamakina.com/", "website_source": "PAID_BRIGHTDATA_CALIBRATED",
         "status": "OK_MEDIUM_CONFIDENCE", "paid_stage_a_candidates": [{"domain": "alfamakina.com"}]},
        {"company": "Alfa Uc", "website": "https://alfagrup.com.tr/", "website_source": "PAID_BRIGHTDATA_CALIBRATED",
         "status": "OK_MEDIUM_CONFIDENCE", "paid_stage_a_candidates": [{"domain": "alfagrup.com.tr"}]},
        {"company": "Alfa Dort", "website": "https://baska.com.tr/", "website_source": "PAID_BRIGHTDATA_CALIBRATED",
         "status": "OK_MEDIUM_CONFIDENCE", "paid_stage_a_candidates": []},
        {"company": "Alfa Bes", "website": "", "status": "REVIEW_NEEDED", "paid_stage_a_candidates": [{"domain": "alfamakina.com.tr"}]},
    ]
    sites = {
        "alfagrup.com.tr": {"reachable": True, "phones": ["02125550000"], "emails": []},
        "alfamakina.com.tr": {"reachable": True, "phones": ["02125550000"], "emails": []},
        "baska.com.tr": {"reachable": True, "phones": ["02165550000"], "emails": []},
    }
    result = measure_paid_acceptance.evaluate(rows, firms, None, fetch=lambda domain: sites[domain])
    summary = result["summary"]
    assert (summary["firms"], summary["matched"], summary["paid_reached"], summary["truth_in_paid_candidates"]) == (6, 5, 4, 2)
    assert summary["FREE"] == {"count": 1, "correct": 1, "correct_strict": 1}
    assert summary["PAID_CALIBRATED"] == {"count": 3, "correct": 2, "correct_strict": 1}
    assert summary["NONE"]["count"] == 1
    assert summary["new_adjudication_pairs"] == 2
    assert summary["paid_calibrated_precision"] == pytest.approx(2 / 3)
    assert summary["paid_calibrated_precision_strict"] == pytest.approx(1 / 3)
    assert [detail["category"] for detail in result["details"]][-1] == "MISSING"


def test_run_rows_and_brightdata_usage_read_from_run_database(tmp_path):
    run_dir = tmp_path / "run19"
    (run_dir / "state").mkdir(parents=True)
    connection = sqlite3.connect(run_dir / "state" / "progress.sqlite3")
    connection.execute("CREATE TABLE results (run_id TEXT, item_index INTEGER, payload TEXT)")
    connection.execute("CREATE TABLE provider_calls (run_id TEXT, provider TEXT, state TEXT)")
    connection.execute("CREATE TABLE provider_query_flights (run_id TEXT, provider TEXT, state TEXT)")
    connection.execute("INSERT INTO results VALUES ('run19', 1, ?)", (json.dumps({"company": "Beta"}),))
    connection.execute("INSERT INTO results VALUES ('run19', 0, ?)", (json.dumps({"company": "Alfa"}),))
    connection.execute("INSERT INTO results VALUES ('baska', 0, ?)", (json.dumps({"company": "Gama"}),))
    connection.executemany("INSERT INTO provider_calls VALUES ('run19', ?, ?)", [
        ("brightdata", "DONE"), ("brightdata", "DONE"), ("brightdata", "FAILED"), ("hunter", "DONE"),
    ])
    connection.executemany("INSERT INTO provider_query_flights VALUES ('run19', 'brightdata', ?)", [("DONE",), ("FAILED",)])
    connection.commit()
    connection.close()
    assert [row["company"] for row in measure_paid_acceptance.read_run_rows(run_dir)] == ["Alfa", "Beta"]
    assert measure_paid_acceptance.brightdata_usage(run_dir) == {
        "calls": {"DONE": 2, "FAILED": 1}, "flights": {"DONE": 1, "FAILED": 1},
    }
