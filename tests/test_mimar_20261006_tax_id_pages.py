"""Talimat 32: the tax identifier is looked for on legal and contact pages, and on sites linked from directories."""

from __future__ import annotations

import pytest

from modules import country_profile, search, tax_identity

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


HOME = _page(
    "https://www.marka.pl/",
    '<a href="/oferta/pompy">Pompy</a><a href="/regulamin-sklepu">Regulamin</a>'
    '<a href="/polityka-prywatnosci/">Polityka</a><a href="https://inna.pl/kontakt">Partner</a>'
    '<a href="/kontakt">Kontakt</a>',
)


def test_legal_links_come_from_the_own_home_page(poland):
    assert tax_identity.legal_links([HOME], 4) == [
        "https://www.marka.pl/regulamin-sklepu", "https://www.marka.pl/polityka-prywatnosci/",
        "https://www.marka.pl/kontakt",
    ]
    read = [HOME, _page("https://www.marka.pl/kontakt", "Kontakt")]
    assert "https://www.marka.pl/kontakt" not in tax_identity.legal_links(read, 4)
    country_profile.apply("TR")
    assert tax_identity.legal_links([HOME], 4) == []


def test_legal_pages_are_read_until_the_identifier_shows(poland):
    fetched = []

    def fetch(url):
        fetched.append(url)
        return f"<html><body>NIP: {NIP}</body></html>" if "polityka" in url else "<html><body>Regulamin</body></html>"

    extra = tax_identity.more_pages([HOME], NIP, fetch)
    assert fetched == ["https://www.marka.pl/regulamin-sklepu", "https://www.marka.pl/polityka-prywatnosci/"]
    assert tax_identity.site_evidence([HOME, *extra], NIP)["match_url"] == "https://www.marka.pl/polityka-prywatnosci/"
    shown = _page("https://www.marka.pl/", f'NIP {NIP} <a href="/regulamin">Regulamin</a>')
    other = _page("https://www.marka.pl/", f'NIP {OTHER_NIP} <a href="/regulamin">Regulamin</a>')
    fetched.clear()
    assert tax_identity.more_pages([shown], NIP, fetch) == [] and tax_identity.more_pages([other], NIP, fetch) == []
    assert fetched == []


def test_deep_own_page_linked_from_home_counts_but_deep_foreign_page_does_not(poland):
    deep = {"url": "https://www.marka.pl/cms/4-o-nas.html", "html": f"NIP {NIP}", "own_link": True}
    assert tax_identity.site_evidence([deep], NIP)["match_url"] == deep["url"]
    assert not tax_identity.company_page("https://ogloszenia.pl/oferty-uzytkownika-123/o-nas")


def test_number_lists_and_lookup_pages_are_never_evidence(poland):
    numbers = " ".join(str(5550001000 + offset) for offset in range(200))
    listing = {"url": "https://numery.example/5550001000", "html": f"<html><body>{numbers} {NIP}</body></html>"}
    assert tax_identity.site_evidence([listing], NIP)["match_url"] == ""
    assert tax_identity.profile_page(f"https://telefony.example/data-7{NIP}", NIP, "")
    assert tax_identity.profile_page(f"https://telefony.example/ring-48{NIP}", NIP, "")
    assert not tax_identity.profile_page("https://marka.pl/kontakt", NIP, "")
    page = _page("https://marka.pl/kontakt", f"NIP {NIP}, REGON 123456785, tel. 22 555 01 02")
    assert tax_identity.site_evidence([page], NIP)["match_url"] == "https://marka.pl/kontakt"


def test_directory_sites_skip_directories_social_and_the_directory_itself(poland):
    html = (
        '<a href="https://www.facebook.com/marka">FB</a><a href="https://bizraport.pl/inna">x</a>'
        '<a href="/krs/2">y</a><a href="https://www.marka.pl/oferta">Strona www</a>'
        '<a href="https://sklep.marka.pl/">Sklep</a><a href="https://inna-firma.pl/">Reklama</a>'
    )
    assert tax_identity.directory_sites(html, "https://targeo.pl/5550001119/nip/firma", 4) == [
        "https://www.marka.pl", "https://inna-firma.pl",
    ]


def test_site_linked_from_a_directory_page_is_verified_on_its_legal_page(monkeypatch, poland):
    profile = f"https://targeo.pl/{NIP}/nip/firma"
    pages_read, sites_crawled = [], []

    def fetch_page(url):
        pages_read.append(url)
        if url == profile:
            return '<html><body><a href="https://www.marka.pl/">www.marka.pl</a></body></html>'
        return f"<html><body>Regulamin. NIP {NIP}</body></html>" if "regulamin" in url else ""

    def fetch_site(url, **_):
        sites_crawled.append(url)
        return {"url": url, "pages": [HOME]}

    monkeypatch.setattr(search.crawler, "fetch_page", fetch_page)
    monkeypatch.setattr(search.crawler, "fetch_site", fetch_site)
    candidates: dict = {}
    search._add_tax_id_candidate(candidates, NIP, [{"href": profile, "title": "Firma"}], "PRZYKLAD SP. Z O.O.")
    assert sites_crawled == ["https://www.marka.pl"]
    assert pages_read[:2] == [profile, "https://www.marka.pl/regulamin-sklepu"]
    candidate = candidates["marka.pl"]
    assert candidate["query"] == "tax_id_verified"
    assert candidate["_entity_evidence_url"] == "https://www.marka.pl/regulamin-sklepu"
