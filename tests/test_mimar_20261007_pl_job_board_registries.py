"""Talimat 36: a job board and two company registries never count as a Polish firm's own site."""

from __future__ import annotations

import pytest

from modules import country_profile, scorer, search

NIP = "5550001119"


@pytest.fixture
def poland():
    country_profile.apply("PL")
    try:
        yield
    finally:
        country_profile.apply("TR")


@pytest.mark.parametrize("domain", ["praca.pl", "rusprofile.ru", "rejestrkrs.pl"])
def test_job_board_and_registries_are_directories(poland, domain):
    assert scorer.is_excluded_domain(domain)
    assert scorer.is_excluded_domain(f"www.{domain}")


def test_job_advert_showing_the_tax_id_is_not_a_tax_id_site(monkeypatch, poland):
    advert = "https://www.praca.pl/inzynier-projektu_1234567.html"
    fetched = []
    monkeypatch.setattr(search.crawler, "fetch_page", lambda url: fetched.append(url) or f"<html><body>NIP {NIP}</body></html>")
    monkeypatch.setattr(search.crawler, "fetch_site", lambda url, **_: {"url": url, "pages": []})
    candidates: dict = {}
    search._add_tax_id_candidate(candidates, NIP, [{"href": advert, "title": "Inżynier projektu"}], "PRZYKLAD POLSKA SP. Z O.O.")
    assert "praca.pl" not in candidates


def test_turkish_profile_is_unchanged():
    country_profile.apply("TR")
    assert not scorer.is_excluded_domain("praca.pl")
