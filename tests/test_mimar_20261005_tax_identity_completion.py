"""Talimat 28: tax identifier evidence is kept, own subdomains count, the paid phase searches the NIP."""

from __future__ import annotations

import pytest

import config
from modules import country_profile, identity, pipeline_runner, publication_policy, runtime, search, tax_identity

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


def test_identifier_seen_by_discovery_survives_the_evaluation_crawl(poland):
    pages = [_page("https://marka.pl/", "Witamy w sklepie")]
    assert tax_identity.evaluation_reasons({"tax_id": NIP}, pages) == []
    assert tax_identity.evaluation_reasons({"tax_id": NIP}, pages, "https://marka.pl/regulamin") == [
        "tax_id_match:https://marka.pl/regulamin"
    ]
    shown = [_page("https://marka.pl/kontakt", f"NIP {NIP}")]
    assert tax_identity.evaluation_reasons({"tax_id": NIP}, shown, "https://marka.pl/regulamin") == [
        "tax_id_match:https://marka.pl/kontakt"
    ]


def test_company_own_subdomains_count_and_mirrors_do_not(poland):
    for url in ("https://kup.marka.pl/regulamin/", "https://bip.marka.pl/", "https://www2.marka.pl/o-firmie"):
        assert tax_identity.company_host(url) and tax_identity.company_page(url), url
    assert not tax_identity.company_host("https://przyklad.katalog.pl/")
    assert tax_identity.company_host("https://marka.pl/en-pl/products/pompa-1300")
    assert not tax_identity.company_page("https://marka.pl/en-pl/products/pompa-1300")


def test_deep_result_page_leads_to_the_site_home(monkeypatch, poland):
    pages, sites = [], []
    monkeypatch.setattr(search.crawler, "fetch_page", lambda url: pages.append(url) or "")

    def fake_fetch_site(url, contact_seed_urls=None, profile="full", evidence_scopes=None, identity_seed_urls=None):
        sites.append(url)
        return {"url": url, "pages": [_page(url + "/", f"Marka Sp. z o.o. NIP {NIP}")]}

    monkeypatch.setattr(search.crawler, "fetch_site", fake_fetch_site)
    candidates: dict = {}
    search._add_tax_id_candidate(candidates, NIP, [{"href": "https://marka.pl/en-pl/products/pompa-1300"}])
    assert pages == [] and sites == ["https://marka.pl"]
    assert candidates["marka.pl"]["query"] == "tax_id_verified"


def test_identifier_outweighs_owner_wording_and_missing_contacts(poland):
    conflict = [{"kind": "structured_owner_mismatch", "polarity": "conflict"}]
    candidate = {"url": "https://marka.pl", "query": "tax_id_verified", "_entity_evidence_url": "https://marka.pl/kontakt"}
    assessment = identity.assess("PRZYKLAD SP. Z O.O.", candidate, ["context_conflict:semantic_entity_type:x"], {})
    assert assessment["conflicts"] and assessment["publishable"]
    evaluation = {
        "candidate": candidate,
        "reasons": ["tax_id_match:https://marka.pl/kontakt"],
        "identity_assessment": {**assessment, "conflicts": conflict},
        "has_contact": False,
        "_identity_resolution": "candidate_resolved_by_tax_id_match",
    }
    decision = publication_policy.evaluate(
        "PRZYKLAD SP. Z O.O.", evaluation, "OK_MEDIUM_CONFIDENCE", minimum_safety_score=75,
    )
    assert decision["action"] == "allow_legacy_publication", decision["hard_blockers"]


def test_paid_plan_puts_the_identifier_query_first(monkeypatch, poland):
    monkeypatch.setattr(search, "_primary_queries", lambda *_: ['"przyklad" kontakt', '"przyklad" oficjalna strona'])
    plan = pipeline_runner._paid_plan_for({"company": "PRZYKLAD SP. Z O.O.", "tax_id": NIP}, 2)
    assert plan == ['"5550001119"', '"przyklad" kontakt']
    assert pipeline_runner._paid_plan_for({"company": "PRZYKLAD SP. Z O.O."}, 2) == [
        '"przyklad" kontakt', '"przyklad" oficjalna strona',
    ]
    country_profile.apply("TR")
    assert pipeline_runner._paid_plan_for({"company": "ORNEK A.S.", "tax_id": NIP}, 1) == ['"przyklad" kontakt']


@pytest.mark.parametrize("phase", ["FREE", "PAID"])
def test_identifier_query_results_are_checked_in_both_phases(monkeypatch, poland, phase):
    checked = []
    queries = []
    nip_query = tax_identity.query(NIP)
    monkeypatch.setattr(runtime, "phase", lambda: phase)
    monkeypatch.setattr(runtime, "durable_run_id", lambda: "")
    monkeypatch.setattr(search, "_primary_queries", lambda *_: [nip_query] if phase == "PAID" else [])
    monkeypatch.setattr(search, "_safe_search_text", lambda query: (
        queries.append(query)
        or search.SearchResults([{"href": "https://marka.pl/", "title": "Marka"}], "live", "test", result_state="COMPLETED")
    ))
    monkeypatch.setattr(
        search, "_add_tax_id_candidate",
        lambda candidates, tax_id, results, company: checked.append((tax_id, len(results), company)),
    )
    search.find_candidate_domains("PRZYKLAD SP. Z O.O.", {"tax_id": NIP})
    assert queries.count(nip_query) == 1
    assert checked == [(NIP, 1, "PRZYKLAD SP. Z O.O.")]


def test_directory_profile_pages_of_the_firm_are_not_checked(monkeypatch, poland):
    company = "PRZYKLAD MASZYNY KOWALSKI SP. Z O.O."
    assert tax_identity.profile_page(f"https://rejestr-firm.pl/{NIP}", NIP, company)
    assert tax_identity.profile_page("https://miasto.pl/przyklad-maszyny-kowalski-sp-z-o-o", NIP, company)
    assert not tax_identity.profile_page("https://przyklad.pl/o-firmie-przyklad-maszyny", NIP, company)
    assert not tax_identity.profile_page("https://marka.pl/regulamin", NIP, company)
    fetched = []
    monkeypatch.setattr(search.crawler, "fetch_page", lambda url: fetched.append(url) or f"NIP {NIP}")
    monkeypatch.setattr(search.crawler, "fetch_site", lambda url, **_: fetched.append(url) or {"url": url, "pages": []})
    candidates: dict = {}
    search._add_tax_id_candidate(
        candidates, NIP, [{"href": "https://miasto.pl/przyklad-maszyny-kowalski-sp-z-o-o"}], company,
    )
    # Talimat 32: the profile page is read for links to the firm's site, never crawled as its site.
    assert fetched == ["https://miasto.pl/przyklad-maszyny-kowalski-sp-z-o-o"] and candidates == {}
