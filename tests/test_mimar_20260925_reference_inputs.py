from __future__ import annotations

import pytest
from openpyxl import Workbook

from modules import excel, reference_inputs, site_mapper


@pytest.mark.parametrize(
    ("value", "expected_url", "expected_status"),
    [
        ("https://www.airpak.com.tr/", "https://www.airpak.com.tr/", "OK"),
        ("www.adacammakina.com", "https://www.adacammakina.com/", "FIXED"),
        ("-www.tenikeller.gen.tr", "https://www.tenikeller.gen.tr/", "FIXED"),
        ("https://-https://keskintestere.com/", "https://keskintestere.com/", "FIXED"),
        ("https://www.abmgrinding.com/en", "https://www.abmgrinding.com/en", "OK"),
        ("-https", "", "INVALID"),
        ("fimaksan", "", "INVALID"),
        ("https://", "", "INVALID"),
        ("BİLKES KESİCİ TAKIM İMALAT", "", "INVALID"),
        ("", "", "EMPTY"),
        ("https://www.instagram.com/x", "", "EXCLUDED_DOMAIN"),
    ],
)
def test_normalize_reference_url(value, expected_url, expected_status):
    assert reference_inputs.normalize_reference_url(value) == (expected_url, expected_status)


def test_strip_references_removes_all_reference_keys_including_target_identity():
    source = {
        "website": "https://company.example/",
        "listed_website": "https://fair.example/",
        "listed_phone": "+90 212 555 0000",
        "listed_email": "sales@company.example",
        "profile_url": "https://fair.example/profile",
        "listing_url": "https://fair.example/listing",
        "source_detail_url": "https://fair.example/detail",
        "source_detail_content_sha256": "a" * 64,
        "source_evidence": [{"url": "https://fair.example/detail"}],
        "target_identity": {
            "website": "https://company.example/",
            "listed_website": "https://fair.example/",
            "listed_phone": "+90 212 555 0000",
            "listed_email": "sales@company.example",
            "profile_url": "https://fair.example/profile",
            "listing_url": "https://fair.example/listing",
            "source_detail_url": "https://fair.example/detail",
            "source_detail_content_sha256": "a" * 64,
            "source_evidence": [{"url": "https://fair.example/detail"}],
        },
    }

    blind = reference_inputs.strip_references(source)

    assert source["website"] == "https://company.example/"
    assert blind["_reference_blind"] is True
    assert blind["source_evidence"] == ""
    assert all(blind[key] == "" for key in reference_inputs.REFERENCE_KEYS)
    assert blind["target_identity"]["source_evidence"] == []
    assert all(blind["target_identity"][key] == "" for key in reference_inputs.REFERENCE_KEYS)


def test_reference_website_prefers_customer_website_column():
    assert reference_inputs.reference_website({
        "website": "www.customer.example",
        "listed_website": "https://fair.example/",
    }) == "https://www.customer.example/"


def test_read_company_records_normalizes_inputs_and_keeps_raw_values(tmp_path):
    path = tmp_path / "reference-input.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["company", "website", "listed_website", "listed_phone"])
    sheet.append(["Example", "www.customer.example", "https://fair.example/", "+90 212 555 00 00"])
    book.save(path)
    book.close()

    record = excel.read_company_records(path)[0]

    assert record["website"] == "https://www.customer.example/"
    assert record["website_raw"] == "www.customer.example"
    assert record["website_input_status"] == "FIXED"
    assert record["listed_website"] == "https://fair.example/"
    assert record["listed_website_raw"] == "https://fair.example/"
    assert record["listed_website_status"] == "OK"
    assert record["listed_phone"] == "02125550000"
    assert record["listed_phone_raw"] == "+90 212 555 00 00"
    assert record["listed_phone_input_status"] == "OK"


def test_site_mapper_discover_skips_invalid_ipv6_href():
    assert site_mapper.discover('<a href="http://[bad">x</a>', "https://example.com") == []


def test_marketplace_domains_are_excluded():
    assert reference_inputs.normalize_reference_url("https://www.makinaturkiye.com") == (
        "", "EXCLUDED_DOMAIN",
    )
