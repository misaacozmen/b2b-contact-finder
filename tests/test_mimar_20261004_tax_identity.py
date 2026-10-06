"""Talimat 27: Polish phone numbers and the fair list's tax identifier (NIP)."""

from __future__ import annotations

import json

import pytest

import config
import main
from modules import (
    country_profile, entity_resolution, excel, extractor, identity, list_extractor, phone,
    publication_policy, search, tax_identity,
)

NIP = "5550001119"
OTHER_NIP = "2223334443"


@pytest.fixture
def poland():
    country_profile.apply("PL")
    try:
        yield
    finally:
        country_profile.apply("TR")


def _page(url: str, body: str) -> dict:
    return {"url": url, "html": f"<html><body>{body}</body></html>"}


def test_tax_identifier_is_off_in_turkey_and_validated_in_poland(poland):
    assert tax_identity.normalize("555-000-11-19") == NIP
    assert tax_identity.normalize("PL 555 000 11 19") == NIP
    assert tax_identity.normalize("5550001118") == ""
    assert tax_identity.normalize("IT01234567890") == ""
    assert tax_identity.from_metadata({"tax_id": "555-00-01-119"}) == NIP
    assert tax_identity.query(NIP) == '"5550001119"'
    country_profile.apply("TR")
    assert config.TAX_ID_FORMAT == ""
    assert tax_identity.normalize(NIP) == ""
    assert tax_identity.evaluation_reasons({"tax_id": NIP}, [_page("https://ornek.pl/", f"NIP {NIP}")]) == []


def test_site_shows_the_identifier_on_its_own_pages(poland):
    pages = [
        _page("https://www.przyklad.pl/", "Witamy"),
        _page("https://www.przyklad.pl/kontakt", "Przyklad Sp. z o.o. NIP: 555-000-11-19 REGON 123456789"),
    ]
    evidence = tax_identity.site_evidence(pages, NIP)
    assert evidence == {"match_url": "https://www.przyklad.pl/kontakt", "other_count": 0}
    assert tax_identity.evaluation_reasons({"tax_id": NIP}, pages) == ["tax_id_match:https://www.przyklad.pl/kontakt"]
    assert tax_identity.site_evidence([_page("https://przyklad.pl/en/contact/", f"VAT ID PL{NIP}")], NIP)["match_url"]


def test_hosted_profiles_deep_pages_and_lists_are_not_company_pages(poland):
    assert not tax_identity.company_page("https://przyklad.katalog.pl/")
    assert not tax_identity.company_page("https://ogloszenia.pl/oferty-uzytkownika-123/o-nas")
    assert tax_identity.company_page("https://przyklad.com.pl/pl/kontakt")
    assert tax_identity.company_page("https://sklep.marka.pl/strona/kontakt")
    assert tax_identity.company_page("https://marka.pl/pl/i/Polityka-prywatnosci/18")
    assert tax_identity.company_page("https://en.marka.pl/about")
    hosted = [_page("https://przyklad.katalog.pl/", f"Przyklad NIP: {NIP}")]
    assert tax_identity.site_evidence(hosted, NIP)["match_url"] == ""
    listing = [_page("https://firmy.pl/", f"A NIP {NIP} B NIP {OTHER_NIP} C NIP 9876543210")]
    assert tax_identity.site_evidence(listing, NIP) == {"match_url": "", "other_count": 0}


def test_site_showing_only_another_identifier_is_a_conflict(poland):
    pages = [_page("https://inna-firma.pl/", f"Inna Firma Sp. z o.o. NIP {OTHER_NIP}")]
    reasons = tax_identity.evaluation_reasons({"tax_id": NIP}, pages)
    assert reasons == ["context_conflict:tax_id:other=1"]
    assessment = identity.assess("PRZYKLAD SP. Z O.O.", {"url": "https://inna-firma.pl", "query": "x"}, reasons, {})
    assert not assessment["publishable"] and assessment["conflicts"]


def test_identifier_on_site_is_independent_authority(poland):
    candidate = {"url": "https://przyklad.pl", "query": '"przyklad" kontakt', "reason": "domain_hits:1"}
    without = identity.assess("PRZYKLAD SP. Z O.O.", candidate, [], {})
    assert not without["publishable"]
    matched = identity.assess(
        "PRZYKLAD SP. Z O.O.", candidate, ["tax_id_match:https://przyklad.pl/kontakt"], {},
    )
    assert matched["publishable"] and "authority" in matched["support_keys"]
    verified = identity.assess(
        "PRZYKLAD SP. Z O.O.",
        {"url": "https://marka.pl", "query": "tax_id_verified", "_entity_evidence_url": "https://marka.pl/kontakt"},
        [], {},
    )
    assert verified["support_keys"] == ["authority"]


def test_polish_numbers_are_read_in_any_grouping(poland):
    html = (
        "<p>Tel. 512 345 678</p><p>kom. 600-100-200</p><p>+48 (22) 333 44 55</p>"
        "<p>NIP: 555-000-11-19 REGON: 631234567</p><p>Data 12.06.2022, nr 123456789</p>"
        '<a href="tel:601222333">Zadzwoń</a>'
    )
    numbers = {phone.normalize_phone(value) for value in extractor.extract_phones(html)} - {""}
    assert numbers == {"512345678", "600100200", "223334455", "601222333"}


def test_turkish_phone_extraction_is_unchanged():
    html = "<p>Tel. 512 345 678</p><p>Telefon: 0212 555 01 02</p>"
    assert extractor.extract_phones(html) == ["0212 555 01 02"]


def test_catalog_tax_identifier_reaches_the_run_input(tmp_path, poland):
    entries = [
        {
            "companyInfo": {"name": f"Przyklad{index} Sp. z o.o.", "website": f"przyklad{index}.pl"},
            "exhibitor": {"nip": NIP if index == 1 else ""},
            "stand": {"hallName": "Hala B", "standNumber": f"B{index}"},
        }
        for index in range(1, 7)
    ]
    records = list_extractor.records_from_texts([json.dumps(entries)])
    assert records[0]["tax_id"] == NIP and records[1]["tax_id"] == ""
    output = tmp_path / "liste.xlsx"
    list_extractor.write_input(records, output, source="Przyklad", listing_url="https://targi.pl/katalog")
    rows = excel.read_company_records(output)
    assert rows[0]["tax_id"] == NIP
    assert "tax_id" not in rows[1]


def test_search_adds_the_first_result_site_that_shows_the_identifier(monkeypatch, poland):
    pages, sites = [], []

    def fake_fetch_page(url):
        pages.append(url)
        body = f"Regulamin. Marka NIP {NIP}" if "marka" in url else "Strona"
        return f"<html><body>{body}</body></html>"

    def fake_fetch_site(url, contact_seed_urls=None, profile="full", evidence_scopes=None, identity_seed_urls=None):
        sites.append((url, profile))
        return {"url": url, "pages": [_page(url + "/", f"Inna NIP {OTHER_NIP}")]}

    monkeypatch.setattr(search.crawler, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(search.crawler, "fetch_site", fake_fetch_site)
    results = [
        {"href": "https://rejestr.io/krs/1/przyklad", "title": "Przyklad"},
        {"href": "https://przyklad.katalog.pl/", "title": "Przyklad"},
        {"href": "https://inna.pl/", "title": "Inna"},
        {"href": "https://www.marka.pl/regulamin", "title": "Marka"},
    ]
    candidates: dict = {}
    search._add_tax_id_candidate(candidates, NIP, results)
    assert pages == ["https://inna.pl/", "https://www.marka.pl/regulamin"]
    assert sites == [("https://inna.pl", "identity")]
    candidate = candidates["marka.pl"]
    assert candidate["query"] == "tax_id_verified" and candidate["role"] == "verified_company"
    assert candidate["url"] == "https://www.marka.pl"
    assert candidate["_entity_evidence_url"] == "https://www.marka.pl/regulamin"

    monkeypatch.setattr(config, "TAX_ID_SITE_CHECKS", 1)
    pages.clear()
    candidates = {}
    search._add_tax_id_candidate(candidates, NIP, results)
    # Talimat 32: the two directory pages are read for links; they are never crawled as sites.
    assert pages == ["https://inna.pl/", "https://rejestr.io/krs/1/przyklad", "https://przyklad.katalog.pl/"]
    assert candidates == {}


def _evaluation(url: str, reasons: list[str], query: str = '"przyklad" kontakt') -> dict:
    return {
        "candidate": {"url": url, "domain": url.split("//", 1)[1], "query": query, "role": "company_candidate"},
        "reasons": reasons,
        "crawl_result": {"url": url, "pages": [_page(url, "Strona")]},
        "has_contact": True,
    }


def test_tax_identifier_decides_identity_resolution(poland):
    homonym = _evaluation(
        "https://przyklad.pl",
        ["page_identity_strong:3/3", "legal_name_full_match:3/3", "country_identity_tr_tld"],
    )
    brand = _evaluation("https://marka.pl", ["tax_id_match:https://marka.pl/kontakt"], "tax_id_verified")
    resolution = entity_resolution.resolve_candidates("PRZYKLAD SP. Z O.O.", [homonym, brand])
    assert resolution.status == "resolved"
    assert resolution.selected is brand
    assert resolution.reason == "candidate_resolved_by_tax_id_match"
    second = _evaluation("https://marka-sklep.pl", ["tax_id_match:https://marka-sklep.pl/regulamin"])
    both = entity_resolution.resolve_candidates("PRZYKLAD SP. Z O.O.", [brand, second])
    assert both.status == "resolved" and both.reason == "candidate_resolved_by_tax_id_match"


def test_later_name_results_do_not_replace_the_verified_site(poland):
    candidates = {"marka.pl": {"domain": "marka.pl", "url": "https://marka.pl", "query": "tax_id_verified", "score": 92}}
    search._add_search_results(
        candidates, "PRZYKLAD SP. Z O.O.", '"przyklad" kontakt',
        [{"href": "https://marka.pl/", "title": "Przyklad marka", "body": ""}],
    )
    assert candidates["marka.pl"]["query"] == "tax_id_verified"


def test_poland_profile_carries_tax_settings_and_directory_domains(poland):
    assert config.TAX_ID_FORMAT == "PL_NIP" and "NIP" in config.TAX_ID_LABELS
    assert set(country_profile.TURKEY["CALIBRATED_DIRECTORY_DOMAINS"]) <= set(config.CALIBRATED_DIRECTORY_DOMAINS)
    assert "krs-pobierz.pl" in config.CALIBRATED_DIRECTORY_DOMAINS


def test_publication_policy_accepts_a_site_showing_the_identifier(poland):
    evaluation = {
        "candidate": {"url": "https://marka.pl", "query": "tax_id_verified", "role": "verified_company"},
        "reasons": ["tax_id_match:https://marka.pl/kontakt", "country_identity_tr_tld"],
        "has_contact": True,
        "email": "biuro@marka.pl",
        "phone": "512345678",
        "_identity_resolution": "candidate_resolved_by_tax_id_match",
    }
    decision = publication_policy.evaluate(
        "STABILERO SP. Z O.O.", evaluation, "OK_MEDIUM_CONFIDENCE", minimum_safety_score=75,
    )
    assert decision["action"] == "allow_legacy_publication", decision["hard_blockers"]
    without = dict(evaluation, reasons=["country_identity_tr_tld"], candidate={"url": "https://marka.pl", "query": "x"})
    blocked = publication_policy.evaluate(
        "STABILERO SP. Z O.O.", without, "OK_MEDIUM_CONFIDENCE", minimum_safety_score=75,
    )
    assert "generic_single_token_identity_not_verified" in blocked["hard_blockers"]


def test_calibrated_fallback_keeps_tax_id_site_and_skips_other_identifiers(monkeypatch, poland):
    monkeypatch.setattr(main.calibration, "rule_accepts", lambda features, top3, rule: True)
    entries = [{
        "features": {"url": "https://inna.pl", "domain": "inna.pl"},
        "evaluation": {"reasons": ["context_conflict:tax_id:other=1"]},
    }]
    review = {"status": "REVIEW_NEEDED", "website": "", "reason": "x"}
    assert main._apply_calibrated_acceptance(review, entries, {}) == review
    settled = {"status": "REVIEW_NEEDED", "website": "https://marka.pl", "reason": "x",
               "__evaluation": {"identity_resolution": "candidate_resolved_by_tax_id_match"}}
    entries[0]["evaluation"]["reasons"] = []
    assert main._apply_calibrated_acceptance(settled, entries, {}) == settled


def test_unreachable_same_name_domain_does_not_block_a_tax_id_site():
    selected = {
        "candidate": {"url": "https://przyklad.pl", "role": "company_candidate"},
        "crawl_result": {"pages": [_page("https://przyklad.pl", "Przyklad")]},
        "reasons": ["page_identity_strong:1/1"], "structured_identity": {},
        "final_score": 90, "has_contact": True, "email_failed": False,
    }
    failed = {
        "candidate": {"url": "https://przyklad.com.pl", "role": "company_candidate", "score": 84},
        "crawl_result": {"pages": [], "error": "timeout"},
        "reasons": ["timeout"], "structured_identity": {},
        "final_score": 0, "has_contact": False, "email_failed": False,
    }
    assert main._unreachable_homonym_conflict("Przyklad", selected, [selected, failed])
    selected["reasons"].append("tax_id_match:https://przyklad.pl/kontakt")
    assert main._unreachable_homonym_conflict("Przyklad", selected, [selected, failed]) is None
