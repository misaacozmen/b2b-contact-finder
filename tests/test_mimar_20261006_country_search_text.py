"""Talimat 29: search text keeps the Turkish dotless i only in the Turkey profile."""

from __future__ import annotations

import pytest

from modules import country_profile, scorer, search, tax_identity

NIP = "5550001119"


@pytest.fixture
def poland():
    country_profile.apply("PL")
    try:
        yield
    finally:
        country_profile.apply("TR")


def test_turkey_keeps_the_dotless_i():
    country_profile.apply("TR")
    assert scorer.tr_lower("KIRMIZI İNŞAAT") == "kırmızı inşaat"
    assert scorer.search_display_core("KIRMIZI ISIK MAKINA SANAYI A.S.").startswith("kırmızı ısık")


def test_poland_lowers_capital_i_to_a_plain_i(poland):
    assert scorer.tr_lower("PRZYKLAD INSTAL İ") == "przyklad instal i"
    company = "PRZYKLAD INSTAL MIKOLAJ SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"
    assert scorer.search_display_core(company) == "przyklad instal mikolaj"
    queries = search._primary_queries(company, {})
    assert queries and not any("ı" in query for query in queries)
    assert queries[0] == "przyklad instal mikolaj"


def test_poland_search_name_ends_at_the_legal_form(poland):
    assert scorer.search_display_core("PRZYKLAD SERWIS S.C., JAN NOWAK, ANNA NOWAK") == "przyklad serwis"
    assert scorer.search_display_core("PRZYKLAD SPÓŁKA CYWILNA JAN NOWAK") == "przyklad"
    assert scorer.search_display_core("PRZYKLAD Sp. z o.o. w organizacji") == "przyklad"
    assert scorer.search_display_core("SPÓŁKA PRZYKLAD") == "przyklad"


def test_directory_page_of_a_one_word_firm_with_its_legal_form_is_a_profile(poland):
    company = "PRZYKLAD SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"
    assert tax_identity.profile_page("https://www.katalog-firm.pl/miasto-przyklad-sp-z-o-o-123.html", NIP, company)
    assert tax_identity.profile_page("https://rejestr.pl/company,77,przyklad-spolka", NIP, company)
    assert not tax_identity.profile_page("https://przyklad.pl/przyklad-sp-z-o-o", NIP, company)
    assert not tax_identity.profile_page("https://www.katalog-firm.pl/miasto-przyklad-123.html", NIP, company)
    assert not tax_identity.profile_page("https://marka.pl/polityka-prywatnosci", NIP, company)


def test_turkey_search_name_is_unchanged_after_legal_words():
    country_profile.apply("TR")
    assert scorer.search_display_core("ORNEK MAKINA SANAYI VE TICARET LTD STI ANKARA") == "ornek makına ankara"
