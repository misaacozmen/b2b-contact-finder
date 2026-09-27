"""IP-8R: same-firm adjudication and all-data calibration modes (offline)."""

from __future__ import annotations

from modules import calibration
from tools import adjudicate_same_entity, calibrate_acceptance


RULE = {"L": 3, "R": 3, "H": 1, "C": "contact", "N": 1, "U": 0}


def _site(phones=(), emails=(), reachable=True):
    return {"reachable": reachable, "phones": list(phones), "emails": list(emails)}


def _candidate(domain, **overrides):
    candidate = {
        "domain": domain, "url": f"https://{domain}", "reachable": True,
        "s3": True, "s4": True, "conflict": False, "parked": False,
        "brand_prefix_len": 6, "rank_best": 1, "query_hits": 2,
        "tr_phone": True, "same_domain_email": True, "legacy_final_score": 60,
    }
    candidate.update(overrides)
    return candidate


def _record(source_record_id, truth, candidates, split="cal"):
    return {
        "source_record_id": source_record_id, "company": source_record_id.upper(),
        "labelled": True, "split": split, "truth_domain": truth,
        "stage_a": {"status": "REVIEW_NEEDED", "website": ""},
        "stage_a_brand_prefix_top3": 1, "stage_a_candidates": candidates,
    }


def test_extract_contacts_normalizes_and_drops_placeholders():
    text = "Tel: 0212 555 12 41 | +90 (312) 000 00 00 | info@AlfaPack.com. logo@2x.png"
    contacts = calibration.extract_contacts(text)
    assert contacts["phones"] == {"02125551241"}
    assert contacts["emails"] == {"info@alfapack.com"}


def test_same_entity_verdict_rules():
    shared_phone = calibration.same_entity_verdict(
        "a.com", "b.com", _site(phones=["02125551241"]), _site(phones=["02125551241"]),
    )
    assert (shared_phone["verdict"], shared_phone["reason"]) == ("SAME_ENTITY", "shared_phone")
    shared_email = calibration.same_entity_verdict(
        "a.com", "b.com", _site(emails=["x@z.com"]), _site(emails=["x@z.com"]),
    )
    assert shared_email["reason"] == "shared_email"
    cross = calibration.same_entity_verdict(
        "betasan.com.tr", "betawrap.eu",
        _site(emails=["info@betasan.com.tr"]), _site(emails=["betawrap@betasan.com.tr"]),
    )
    assert (cross["verdict"], cross["reason"]) == ("SAME_ENTITY", "cross_domain_email")
    different = calibration.same_entity_verdict(
        "a.com", "b.com", _site(phones=["02125551241"]), _site(phones=["02125551242"]),
    )
    assert different["verdict"] == "DIFFERENT"
    unknown = calibration.same_entity_verdict("a.com", "b.com", _site(reachable=False), _site())
    assert unknown["verdict"] == "UNKNOWN"


def test_truth_match_accepts_only_adjudicated_alternate_domain():
    record = {"source_record_id": "woodtech:gama", "truth_domain": "gamagroup.com.tr"}
    key = calibration.adjudication_key("woodtech:gama", "https://gamakesici.com/")
    adjudication = {"verdicts": {key: "SAME_ENTITY"}}
    assert calibration.truth_match("gamakesici.com", record, adjudication)
    assert not calibration.truth_match("gamakesici.com", record, None)
    assert not calibration.truth_match("gamakesici.com", record, {"verdicts": {key: "DIFFERENT"}})
    assert calibration.truth_match("https://www.gamagroup.com.tr/", record, None)


def test_disagreement_pairs_and_adjudication_use_injected_fetch():
    records = [
        _record("t:gama", "gamagroup.com.tr", [_candidate("gamakesici.com")]),
        _record("t:acme", "acmeboru.com", [_candidate("acmeboru.com")]),
    ]
    pairs = adjudicate_same_entity.disagreement_pairs(records, [None, RULE])
    assert [(pair["source_record_id"], pair["predicted_domain"]) for pair in pairs] == [
        ("t:gama", "gamakesici.com"),
    ]
    sites = {
        "gamakesici.com": _site(emails=["gama@gamagroup.com"]),
        "gamagroup.com.tr": _site(),
    }
    result = adjudicate_same_entity.adjudicate(pairs, fetch=lambda domain: sites[domain])
    assert result["summary"] == {"SAME_ENTITY": 1, "DIFFERENT": 0, "UNKNOWN": 0}
    assert calibrate_acceptance.metrics(records, RULE, result)["precision"] == 1.0
    assert calibrate_acceptance.metrics(records, RULE, None)["precision"] == 0.5


def test_calibrate_all_uses_every_labelled_record_and_evaluate_lists_wrong():
    records = [
        _record(f"t:{index}", f"firm{index}.com", [_candidate(f"firm{index}.com")],
                split="cal" if index % 2 else "hold")
        for index in range(30)
    ]
    records.append(_record("t:bad", "real.com", [_candidate("fake.com")]))
    result, rows = calibrate_acceptance.calibrate_all(records, None)
    assert len(rows) == 144 and {row["split"] for row in rows} == {"all"}
    assert result["selected"] is not None
    assert result["all"]["labelled"] == 31 and result["all"]["correct"] == 30
    rule = {key: result["selected"][key] for key in ("L", "R", "H", "C", "N", "U")}
    evaluated = calibrate_acceptance.evaluate(records, rule, None)
    assert [row["source_record_id"] for row in evaluated["wrong"]] == ["t:bad"]
    assert evaluated["metrics"]["predicted"] == 31
