from __future__ import annotations

import logging

import config
import main
from modules import crawler, reference_inputs, reference_resolution as resolution, runtime, scorer
from petzoo_fixture_support import FixtureSession, install_harness, load_fixture


def _obs(**updates):
    data = {
        "reachable": True,
        "home_text": "Contact our company for products and services. " * 12,
        "all_text": "Contact our company for products and services. " * 12,
        "title_names": [],
        "legal_names": [],
        "phones": [],
        "emails": [],
        "reference_domain": "example.com",
        "final_domain": "example.com",
        "listed_phone": "",
        "listed_email": "",
        "crawl_error": "",
    }
    data.update(updates)
    return data


def test_verified_by_listed_phone():
    decision = resolution.decide(_obs(listed_phone="02125550000", phones=["02125550000"]), "Example")
    assert decision["tier"] == "REFERENCE_VERIFIED"
    assert "S1_phone" in decision["signals"]


def test_verified_by_name_and_country():
    decision = resolution.decide(_obs(
        reference_domain="airpak.com.tr", final_domain="airpak.com.tr",
        title_names=["airpak"], all_text="istanbul turkey",
    ), "Airpak")
    assert decision["tier"] == "REFERENCE_VERIFIED"
    assert {"S3_name", "S4_country"}.issubset(decision["signals"])


def test_accepted_when_not_contradicted():
    decision = resolution.decide(_obs(
        reference_domain="teksinhidrolik.com", final_domain="teksinhidrolik.com",
        title_names=["teksin hidrolik"],
    ), "AHMET PALABIYIK")
    assert decision["tier"] == "REFERENCE_ACCEPTED"


def test_parked_site_is_unusable():
    decision = resolution.decide(_obs(home_text="cok yakinda buradayiz", all_text="cok yakinda buradayiz"), "ADEKO")
    assert decision["tier"] == "REFERENCE_UNUSABLE"


def test_thin_page_without_signals_is_thin_not_unusable():
    decision = resolution.decide(_obs(
        home_text="x" * 120, all_text="x" * 120,
        reference_domain="unrelated.example", final_domain="unrelated.example",
    ), "Completely Different Company")
    assert decision["tier"] == "REFERENCE_THIN"


def test_thin_page_with_name_match_is_accepted():
    decision = resolution.decide(_obs(
        home_text="acme short page", all_text="acme short page",
        title_names=["acme"], reference_domain="acme.example", final_domain="acme.example",
    ), "Acme")
    assert decision["tier"] == "REFERENCE_ACCEPTED"


def test_conflict_when_site_names_other_company():
    decision = resolution.decide(_obs(legal_names=["xyz makina san ve tic ltd sti"]), "AAB Plastik")
    assert decision["tier"] == "REFERENCE_CONFLICT"


def test_brand_title_alone_is_not_conflict():
    decision = resolution.decide(_obs(title_names=["other brand"], legal_names=[]), "AAB Makina")
    assert decision["tier"] != "REFERENCE_CONFLICT"


def test_unreachable():
    decision = resolution.decide(_obs(reachable=False, crawl_error="timeout"), "Example")
    assert decision == {"tier": "REFERENCE_UNREACHABLE", "signals": [], "reason": "timeout"}


def test_own_search_match_skips_crawl():
    called = []
    result = resolution.complete_with_reference(
        0, "Example", logging.getLogger("test"),
        {"status": "OK_HIGH_CONFIDENCE", "website": "https://www.example.com/"},
        {"website": "https://example.com/"},
        evaluate_fn=lambda *args: called.append(args),
    )
    assert not called
    assert result["website_source"] == "OWN_SEARCH+REFERENCE"


def test_reference_fills_when_stage_a_empty():
    result = resolution.complete_with_reference(
        0, "Example", logging.getLogger("test"),
        {"status": "WEBSITE_NOT_FOUND", "website": ""},
        {"website": "https://example.com/"},
        evaluate_fn=lambda *_: {
            "crawl_result": {"url": "https://example.com/", "pages": [{
                "url": "https://example.com/", "final_url": "https://example.com/",
                "html": "<title>Example</title><body>" + ("contact " * 70) + "</body>",
            }]},
            "structured_identity": {"names": ["Example"], "legal_names": []},
        },
    )
    assert result["website"] == "https://example.com/"
    assert result["status"] == "OK_MEDIUM_CONFIDENCE"


def test_verified_reference_overrides_conflicting_stage_a():
    result = resolution.complete_with_reference(
        0, "Example", logging.getLogger("test"),
        {"status": "OK_MEDIUM_CONFIDENCE", "website": "https://other.com/", "reason": "stage_a"},
        {"website": "https://example.com/", "listed_phone": "02125550000"},
        evaluate_fn=lambda *_: {
            "crawl_result": {"url": "https://example.com/", "pages": [{
                "url": "https://example.com/", "final_url": "https://example.com/",
                "html": "<title>Example</title><body>" + ("contact " * 70) + "<p>0212 555 00 00</p></body>",
            }]},
            "structured_identity": {"names": ["Example"], "legal_names": []},
        },
    )
    assert result["website"] == "https://example.com/"
    assert result["reference_tier"] == "REFERENCE_VERIFIED"


def test_listing_phone_fallback():
    contacts = resolution.select_contacts(_obs(), "example.com", "02125550000")
    assert contacts["phone"] == "02125550000"
    assert contacts["phone_source_tier"] == "REFERENCE_LISTING"


def test_freemail_kept_other_domains_dropped():
    contacts = resolution.select_contacts(_obs(emails=["info@other.example", "person@gmail.com"]), "example.com", "")
    assert contacts["email"] == "person@gmail.com"
    assert contacts["email_source_tier"] == "SITE_FREEMAIL"


def test_reference_pipeline_verifies_petzoo_a000_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("B2B_TEST_OFFLINE", "1")
    _manifest, fixture, _fixture_sha256 = load_fixture()
    record = next(row for row in fixture if row["source_record_id"] == "petzoo:A:000")
    company = record["company"]
    ref = reference_inputs.reference_website(record)
    candidate = {
        "domain": scorer.normalize_domain(ref), "url": ref,
        "score": config.PRE_CRAWL_SCORE_CAP, "title": "", "snippet": "",
        "query": "reference_website", "rank": 0,
        "reason": "reference_website", "role": "company_candidate",
    }
    runtime.reset()
    crawler.clear_page_store()
    _runs_dir, _router, session = install_harness(
        monkeypatch, tmp_path, [record], workers=1,
    )
    assert isinstance(session, FixtureSession)
    try:
        row = resolution.complete_with_reference(
            0, company, logging.getLogger("test"),
            main._empty_result(company, "WEBSITE_NOT_FOUND", ""),
            record,
            evaluate_fn=main._evaluate_candidate_with_stage,
        )
        assert row["reference_tier"] == "REFERENCE_VERIFIED"
        assert scorer.registrable_domain(row["website"]) == "petzoo-a-000.example"
        assert row["phone"] == "02125551212"
        assert "S3_name" in row["reference_signals"]
    finally:
        runtime.reset()
        crawler.clear_page_store()
