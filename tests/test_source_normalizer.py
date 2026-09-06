from __future__ import annotations

import json
from pathlib import Path

from bs4 import BeautifulSoup

from modules.source_normalizer import field_evidence, normalize_label, normalize_url
from modules.exhibitor_scraper import _texhibition_profile_details
from tools.acquire_independent_sources import _labelled_website, _labelled_website_field
from tools.run_source_review_pass import _labelled_company_website


FIXTURES = Path(__file__).parent / "fixtures"


def test_label_contract_is_exact_and_unicode_safe():
    assert normalize_label("  Web&nbsp;Site： ") == "web site"
    assert normalize_label("İnternet   Sitesi:") == "i̇nternet sitesi"


def test_hometex_web_schemeless_is_canonical_and_footer_is_not_used():
    soup = BeautifulSoup((FIXTURES / "hometex_web_schemeless.html").read_text(encoding="utf-8"), "html.parser")
    assert _labelled_website(soup) == "https://www.example-home.com"
    value, evidence = _labelled_website_field(soup, "https://hometex.com.tr/en/example")
    assert value == "https://www.example-home.com"
    assert evidence["label_normalized"] == "web"
    assert _labelled_website(BeautifulSoup("<footer><a href='www.footer.example'>x</a></footer>", "html.parser")) == ""


def test_texhibition_web_site_colon_and_organizer_asset_filter():
    html = (FIXTURES / "texhibition_web_site_schemeless.html").read_text(encoding="utf-8")
    assert _labelled_company_website(BeautifulSoup(html, "html.parser")) == "https://www.example-textile.com"
    details = _texhibition_profile_details(html, "https://www.texhibitionist.com/en/exhibitors/example")
    assert details["listed_website"] == "https://www.example-textile.com"
    assert details["source_listed_website"] == "www.example-textile.com"
    receipt = json.loads(details["source_field_evidence"])[0]
    assert receipt["source_url"].endswith("/example")
    assert receipt["response_sha256"]


def test_ambiente_schemeless_homepage_is_not_rejected():
    payload = json.loads((FIXTURES / "ambiente_homepage.json").read_text(encoding="utf-8"))
    raw = payload["result"]["hits"][0]["exhibitor"]["homepage"]
    normalized = normalize_url(raw)
    assert normalized["status"] == "present"
    assert normalized["normalized_value"] == "https://www.fixture-company.example"


def test_current_independent_acquisition_receipt_has_expected_pool_shape():
    snapshot = Path(__file__).parents[1] / "output" / "a8_corrected_independent_acquisition.json"
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert len(payload["hometex"]["selected"]) == 60
    assert len(payload["ambiente"]["selected"]) == 60
    assert sum(bool(row.get("listed_website")) for row in payload["ambiente"]["selected"]) == 58


def test_url_canonicalization_and_relative_host_guard():
    assert normalize_url("//Example.COM/a?utm_source=x&b=2#fragment")["normalized_value"] == "https://example.com/a?b=2"
    assert normalize_url("/profile", source_url="https://source.example/list") ["normalized_value"] == "https://source.example/profile"
    rejected = normalize_url("https://other.example/profile", source_url="https://source.example/list")
    assert rejected["status"] == "present"
    relative_cross_host = normalize_url("//other.example/profile", source_url="https://source.example/list")
    assert relative_cross_host["status"] == "rejected"


def test_field_evidence_contains_required_provenance_envelope():
    evidence = field_evidence(
        raw_value="www.example.com",
        normalized_value="https://www.example.com/",
        label_raw="Web Site:",
        selector_or_json_pointer="/x/y",
        source_url="https://source.example/page",
        response_bytes=b"fixture",
        observed_at="2026-09-05T00:00:00+00:00",
    )
    assert set(evidence) == {
        "raw_value", "normalized_value", "label_raw", "label_normalized",
        "selector_or_json_pointer", "source_url", "response_sha256",
        "observed_at", "status", "rejection_reason",
    }
