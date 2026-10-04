"""Talimat 26: country profiles (Türkiye unchanged, Poland added)."""

from __future__ import annotations

import copy

import pytest

import config
import main
from modules import (
    country_profile, discovery_rules, phone, query_planner, reference_resolution, run_launcher, scorer,
)


@pytest.fixture
def poland():
    country_profile.apply("PL")
    try:
        yield
    finally:
        country_profile.apply("TR")


def test_turkey_profile_is_the_existing_configuration():
    before = {name: copy.deepcopy(getattr(config, name)) for name in country_profile.SETTING_NAMES}
    country_profile.apply("TR")
    assert {name: getattr(config, name) for name in country_profile.SETTING_NAMES} == before
    assert country_profile.current() == "TR"


def test_poland_profile_switches_and_turkey_restores(poland):
    assert config.TARGET_COUNTRY == "PL"
    assert config.PHONE_ALLOWED_COUNTRIES == ["PL"]
    assert "pl" not in config.FOREIGN_COUNTRY_TLDS and "tr" in config.FOREIGN_COUNTRY_TLDS
    country_profile.apply("TR")
    assert config.TARGET_COUNTRY == "TR"
    assert config.CONTACT_QUERY_WORD == "iletisim"
    with pytest.raises(ValueError):
        country_profile.apply("XX")


def test_turkish_queries_keep_their_exact_wording():
    queries = discovery_rules.fallback_queries("ORNEK MAKİNA SAN. VE TİC. LTD. ŞTİ.", {})
    assert '"ornek makina san ve tic ltd sti" Turkiye resmi web sitesi' in queries
    assert '"ornek makina san ve tic ltd sti" iletisim' in queries
    assert query_planner.query_intent('"ornek" iletisim Turkiye') == "contact"


def test_polish_queries_use_polish_words(poland):
    name = "PRZYKLAD SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"
    assert scorer.search_display_core(name) == "przyklad"
    queries = discovery_rules.fallback_queries(name, {})
    assert any(query.endswith("Polska oficjalna strona internetowa") for query in queries)
    assert any(query.endswith(" kontakt") for query in queries)
    assert all("Turkiye" not in query and "iletisim" not in query for query in queries)
    assert query_planner.query_intent('"przyklad" kontakt Polska') == "contact"


def test_polish_letters_fold_to_ascii():
    assert scorer.normalize_text("SPÓŁKA Złoty Łódź Kraków") == "spolka zloty lodz krakow"


def test_polish_phones_and_domains(poland):
    assert phone.normalize_phone("+48 22 555 12 34") == "225551234"
    assert phone.normalize_phone("22 555 12 34") == "225551234"
    assert phone.normalize_phone("0212 555 00 00") == ""
    assert not scorer.is_foreign_country_domain("przyklad.pl")
    assert scorer.is_foreign_country_domain("ornek.com.tr")


def test_polish_contact_selection_prefers_biuro_and_domestic_phone(poland):
    obs = {
        "emails": ["jan@przyklad.pl", "biuro@przyklad.pl", "osoba@wp.pl"],
        "phones": ["225551234", "512655725"],
        "listed_email": "",
    }
    contacts = reference_resolution.select_contacts(obs, "przyklad.pl", "")
    assert contacts["email"] == "biuro@przyklad.pl"
    assert contacts["phone"] == "225551234"
    only_free = reference_resolution.select_contacts({**obs, "emails": ["osoba@wp.pl"]}, "przyklad.pl", "")
    assert only_free["email_source_tier"] == "SITE_FREEMAIL"


def test_turkish_contact_selection_is_unchanged():
    obs = {"emails": ["satis@ornek.com.tr", "info@ornek.com.tr"], "phones": ["05325550000", "02125550000"], "listed_email": ""}
    contacts = reference_resolution.select_contacts(obs, "ornek.com.tr", "")
    assert contacts["email"] == "info@ornek.com.tr"
    assert contacts["phone"] == "02125550000"


def test_cli_country_option_applies_the_profile():
    args = main.parse_args(["--country", "PL", "--non-interactive"])
    try:
        main._apply_cli_options(args)
        assert config.TARGET_COUNTRY == "PL"
    finally:
        country_profile.apply("TR")
    assert main.parse_args(["--non-interactive"]).country == "TR"


def test_launcher_passes_country_only_when_not_turkey():
    command, _ = run_launcher.build_command(run_launcher.MODE_FREE, "fuar.xlsx", 10)
    assert "--country" not in command
    command, _ = run_launcher.build_command(run_launcher.MODE_FREE, "fuar.xlsx", 10, country="PL")
    assert command[-4:] == ["--country", "PL", "--no-allow-paid", "--finalize-without-paid"]
