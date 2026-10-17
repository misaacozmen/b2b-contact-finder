"""Talimat 52: Jev as a second judge of the FREE Stage A website (synthetic firms and pages only)."""

from __future__ import annotations

import argparse
import inspect
import json

import pytest

import config
import main
from modules import (
    api_configuration, country_profile, field_merge, reference_resolution, run_launcher, run_report, scorer, site_judge,
)


FAIR = "Ornek Makine Fuari 2026, Istanbul: machinery fair"


def _page(title: str, words: int = 120, legal: str = "", url: str = "https://alfamakina.com.tr/") -> dict:
    body = "".join(
        "<p>" + " ".join(f"kelime{index}" for index in range(start, min(start + 10, words))) + "</p>"
        for start in range(0, words, 10)
    )
    return {
        "url": url,
        "html": (
            f"<html><head><title>{title}</title><meta name='description' content='{title} aciklama'></head>"
            f"<body><p>{body}</p><footer>{legal}</footer><script>var x = 1;</script></body></html>"
        ),
    }


def _features(domain: str, **overrides) -> dict:
    features = {
        "domain": domain, "url": f"https://{domain}/", "reachable": True, "s3": True, "s4": True,
        "conflict": False, "parked": False, "brand_prefix_len": 4, "rank_best": 1, "query_hits": 1,
        "tr_phone": True, "same_domain_email": False, "legacy_final_score": 60,
    }
    features.update(overrides)
    return features


def _entry(domain: str, words: int = 120, **overrides) -> dict:
    return {
        "features": _features(domain, **overrides),
        "evaluation": {"crawl_result": {"pages": [_page(f"{domain} anasayfa", words, url=f"https://{domain}/")]}},
    }


class FakeJev:
    """Answers by the candidate domain written in the request state."""

    def __init__(self, answers: dict[str, tuple[str, float, float]]):
        self.answers = answers
        self.requests: list[dict] = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        self.requests.append(body)
        domain = next(
            line.split(": ", 1)[1] for line in body["state"].split("\n") if line.startswith("Candidate website: ")
        )
        choice, confidence, own = self.answers[domain]
        payload = {"answers": {
            "site": {"type": "choice", "choice": choice, "confidence": confidence,
                     "probabilities": {"kendi_sitesi": own}},
            "kendi": {"type": "noul", "noul": own},
        }}
        return _Response(json.dumps(payload).encode("utf-8"))


class _Response:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _jev_ready(monkeypatch):
    site_judge.reset()
    monkeypatch.setattr(config, "ENABLE_JEV_SITE_CHECK", True)
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "synthetic-key")
    monkeypatch.setattr(config, "JEV_SITE_FAIR", FAIR)
    monkeypatch.setattr(config, "SEARCH_CACHE_MODE", "off")
    monkeypatch.setattr(country_profile, "current", lambda: "TR")
    monkeypatch.setattr(
        reference_resolution, "observe",
        lambda evaluation, url, metadata: {
            "emails": [f"info@{scorer.normalize_domain(url)}"], "phones": ["0212 555 00 00"], "listed_email": "",
        },
    )
    yield
    site_judge.reset()


def _use(monkeypatch, answers: dict) -> FakeJev:
    fake = FakeJev(answers)
    original = site_judge.judge
    monkeypatch.setattr(site_judge, "judge", lambda company, domain, pages, opener=None: original(company, domain, pages, opener=fake))
    return fake


def _ok_row(domain: str = "alfamakina.com.tr") -> dict:
    return {
        "company": "ALFA MAKINA SAN. VE TIC. LTD. STI.", "status": "OK_MEDIUM_CONFIDENCE",
        "website": f"https://{domain}/", "selected_website": f"https://{domain}/", "website_source": "OWN_SEARCH",
        "email": f"info@{domain}", "email_source": f"https://{domain}/iletisim", "email_source_tier": "SITE",
        "phone": "02125550000", "phone_source": f"https://{domain}/", "phone_source_tier": "SITE",
        "alternative_phones": "02125550001", "reason": "identity_verified",
    }


def _empty_row() -> dict:
    return {"company": "ALFA MAKINA SAN. VE TIC. LTD. STI.", "status": "REVIEW_NEEDED", "website": "", "reason": "no_candidate_passed"}


def _apply(row: dict, entries: list[dict]) -> dict:
    return main._apply_jev_site_check(row, entries, row["company"], {})


def test_check_runs_only_with_switch_key_fair_turkey_and_live_mode(monkeypatch):
    assert site_judge.available() is True
    for name, value in (("ENABLE_JEV_SITE_CHECK", False), ("TYPESAFE_API_KEY", ""), ("JEV_SITE_FAIR", "  "),
                        ("SEARCH_CACHE_MODE", "replay")):
        with monkeypatch.context() as patch:
            patch.setattr(config, name, value)
            assert site_judge.available() is False
    monkeypatch.setattr(country_profile, "current", lambda: "PL")
    assert site_judge.available() is False


def test_defaults_are_fixed():
    assert config.JEV_SITE_REJECT_CONFIDENCE == 0.8
    assert config.JEV_SITE_ACCEPT_CONFIDENCE == 0.9
    assert config.JEV_SITE_MIN_WORDS == 40
    assert config.JEV_SITE_MAX_CANDIDATES == 5
    assert site_judge.VETO_CHOICES == ("baska_firma", "rehber")


def test_page_summary_reads_title_description_title_lines_and_home_page_first():
    pages = [
        _page("Iletisim", 10, legal="Beta Ltd. Sti. Adres: Ornek Cad. No 1", url="https://alfa.com.tr/iletisim"),
        _page("Alfa Makina", 300, url="https://alfa.com.tr/"),
    ]
    summary = site_judge.page_summary(pages)
    assert summary["title"] == "Alfa Makina"
    assert summary["description"] == "Alfa Makina aciklama"
    assert summary["words"] == site_judge.TEXT_WORDS
    assert "var x" not in summary["text"]
    assert any("Ltd. Sti." in line for line in summary["legal"])


def test_request_names_the_fair_the_domain_and_the_five_choices():
    body = site_judge.request_body("ALFA MAKINA", "alfamakina.com.tr", site_judge.page_summary([_page("Alfa")]))
    lines = body["state"].split("\n")
    assert lines[0] == "Company name in the exhibitor list: ALFA MAKINA"
    assert lines[1] == f"Fair: {FAIR}"
    assert lines[2] == "Candidate website: alfamakina.com.tr"
    assert body["model"] == config.TYPESAFE_MODEL
    assert set(body["questions"]["site"]["criteria"]) == {"kendi_sitesi", "baska_firma", "bayi_marka", "rehber", "bilgi_yok"}
    assert body["questions"]["kendi"]["type"] == "noul"


def test_judge_parses_caches_and_fails_open():
    fake = FakeJev({"alfamakina.com.tr": ("kendi_sitesi", 0.97, 0.96)})
    pages = [_page("Alfa", 80)]
    verdict = site_judge.judge("ALFA", "https://alfamakina.com.tr/", pages, opener=fake)
    words = site_judge.page_summary(pages)["words"]
    assert words == 81
    assert verdict == {"choice": "kendi_sitesi", "confidence": 0.97, "own": 0.96, "words": words}
    assert site_judge.judge("ALFA", "alfamakina.com.tr", pages, opener=fake) == verdict
    assert len(fake.requests) == 1

    def broken(request, timeout=None):
        raise OSError("down")

    assert site_judge.judge("BETA", "beta.com.tr", pages, opener=broken) is None


def test_reject_and_accept_need_confidence_and_enough_words():
    assert site_judge.rejects({"choice": "baska_firma", "confidence": 0.8, "own": 0.1, "words": 40})
    assert site_judge.rejects({"choice": "rehber", "confidence": 0.95, "own": 0.0, "words": 200})
    assert not site_judge.rejects({"choice": "bayi_marka", "confidence": 0.99, "own": 0.0, "words": 200})
    assert not site_judge.rejects({"choice": "baska_firma", "confidence": 0.79, "own": 0.1, "words": 200})
    assert not site_judge.rejects({"choice": "baska_firma", "confidence": 0.99, "own": 0.0, "words": 39})
    assert site_judge.accepts({"choice": "kendi_sitesi", "confidence": 0.9, "own": 0.9, "words": 40})
    assert not site_judge.accepts({"choice": "kendi_sitesi", "confidence": 0.99, "own": 0.89, "words": 200})
    assert not site_judge.accepts({"choice": "kendi_sitesi", "confidence": 0.99, "own": 0.99, "words": 39})
    assert not site_judge.accepts(None) and not site_judge.rejects(None)


def test_nothing_changes_when_jev_cannot_be_asked(monkeypatch):
    monkeypatch.setattr(config, "JEV_SITE_FAIR", "")
    row = _empty_row()
    assert _apply(row, [_entry("alfamakina.com.tr")]) is row


def test_kept_pick_when_jev_agrees(monkeypatch):
    fake = _use(monkeypatch, {"alfamakina.com.tr": ("kendi_sitesi", 0.95, 0.95)})
    row = _ok_row()
    assert _apply(row, [_entry("alfamakina.com.tr")]) is row
    assert len(fake.requests) == 1


def test_rejected_pick_goes_to_review_without_its_website_and_contacts(monkeypatch):
    _use(monkeypatch, {"alfamakina.com.tr": ("baska_firma", 0.93, 0.03), "alfa-ornek.com": ("bilgi_yok", 0.6, 0.2)})
    result = _apply(_ok_row(), [_entry("alfamakina.com.tr"), _entry("alfa-ornek.com", rank_best=2)])
    assert result["status"] == "REVIEW_NEEDED"
    for field in ("website", "selected_website", "website_source", "email", "email_source", "phone",
                  "phone_source", "alternative_phones"):
        assert result[field] == "", field
    assert result["website_identity_verified"] is False
    assert result["jev_rejected_website"] == "alfamakina.com.tr"
    assert result["reason"].startswith("jev_site_rejected:alfamakina.com.tr:baska_firma:0.93; identity_verified")


def test_rejected_pick_is_replaced_by_the_candidate_jev_accepts(monkeypatch):
    _use(monkeypatch, {"alfamakina.com.tr": ("rehber", 0.9, 0.0), "alfa-ornek.com.tr": ("kendi_sitesi", 0.96, 0.95)})
    result = _apply(_ok_row(), [_entry("alfamakina.com.tr"), _entry("alfa-ornek.com.tr", rank_best=3)])
    assert result["website"] == "https://alfa-ornek.com.tr/"
    assert result["website_source"] == "OWN_SEARCH_JEV"
    assert result["status"] == "OK_MEDIUM_CONFIDENCE"
    assert result["email"] == "info@alfa-ornek.com.tr"
    assert result["jev_rejected_website"] == "alfamakina.com.tr"
    assert result["reason"].startswith("jev_site_accepted:kendi_sitesi:0.96; jev_site_rejected:alfamakina.com.tr")


def test_firm_without_pick_gets_the_candidate_jev_is_surest_of(monkeypatch):
    fake = _use(monkeypatch, {
        "alfa.com.tr": ("kendi_sitesi", 0.92, 0.91),
        "alfamakina.com.tr": ("kendi_sitesi", 0.99, 0.98),
        "alfagrup.com": ("baska_firma", 0.97, 0.02),
    })
    entries = [_entry("alfa.com.tr"), _entry("alfamakina.com.tr", rank_best=2), _entry("alfagrup.com", rank_best=3)]
    result = _apply(_empty_row(), entries)
    assert result["website"] == "https://alfamakina.com.tr/"
    assert result["selected_website"] == "https://alfamakina.com.tr/"
    assert result["website_source"] == "OWN_SEARCH_JEV"
    assert result["status"] == "OK_MEDIUM_CONFIDENCE"
    assert result["reason"] == "jev_site_accepted:kendi_sitesi:0.99; no_candidate_passed"
    assert len(fake.requests) == 3


def test_no_change_below_threshold_or_on_thin_pages(monkeypatch):
    _use(monkeypatch, {"alfamakina.com.tr": ("kendi_sitesi", 0.9, 0.85), "alfa.com.tr": ("kendi_sitesi", 0.99, 0.99)})
    row = _empty_row()
    assert _apply(row, [_entry("alfamakina.com.tr"), _entry("alfa.com.tr", words=20)]) is row


def test_skips_unreachable_parked_foreign_and_excluded_candidates(monkeypatch):
    fake = _use(monkeypatch, {})
    entries = [
        _entry("alfamakina.com.tr", reachable=False),
        _entry("alfa-makina.com.tr", parked=True),
        _entry("alfamakina.de"),
        _entry("facebook.com"),
    ]
    row = _empty_row()
    assert _apply(row, entries) is row
    assert fake.requests == []


def test_asks_about_at_most_five_candidates_once_per_domain(monkeypatch):
    domains = [f"alfa{index}.com.tr" for index in range(7)]
    fake = _use(monkeypatch, {domain: ("bilgi_yok", 0.5, 0.1) for domain in domains})
    entries = [_entry(domain, rank_best=index + 1) for index, domain in enumerate(domains)]
    entries.append(_entry("www.alfa0.com.tr", rank_best=9))
    row = _empty_row()
    assert _apply(row, entries) is row
    assert len(fake.requests) == 5


def test_tax_id_resolved_rows_are_left_alone(monkeypatch):
    fake = _use(monkeypatch, {"alfamakina.com.tr": ("baska_firma", 0.99, 0.0)})
    row = {**_ok_row(), "__evaluation": {"identity_resolution": "candidate_resolved_by_tax_id_match"}}
    assert _apply(row, [_entry("alfamakina.com.tr")]) is row
    assert fake.requests == []


def test_source_label_confidence_key_and_cli_option(monkeypatch):
    row = {"website": "https://alfamakina.com.tr/", "website_source": "OWN_SEARCH_JEV", "status": "OK_MEDIUM_CONFIDENCE"}
    assert field_merge.field_confidence(row, "website") == "MEDIUM"
    assert run_report._detail_value("Web kaynağı", "OWN_SEARCH_JEV") == "Arama (Jev)"
    for name in ("GOOGLE_PLACES_API_KEY", "BRIGHTDATA_API_KEY", "BRANDFETCH_CLIENT_ID", "HUNTER_API_KEY",
                 "ENABLE_BRANDFETCH_DOMAIN_SEARCH", "ENABLE_HUNTER_DOMAIN_FINDER"):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "")
    api_configuration.apply_saved_resolver_configuration({"typesafe": "saved-jev"})
    assert config.TYPESAFE_API_KEY == "saved-jev"
    args = main.parse_args(["--fuar", "  Ornek   Fuar 2026,  Istanbul: makine "])
    monkeypatch.setattr(config, "JEV_SITE_FAIR", "")
    main._apply_cli_options(args)
    assert config.JEV_SITE_FAIR == "Ornek Fuar 2026, Istanbul: makine"
    main._apply_cli_options(argparse.Namespace(**{**vars(args), "fuar": ""}))
    assert config.JEV_SITE_FAIR == ""


def test_process_company_asks_jev_after_the_calibrated_rule():
    source = inspect.getsource(main.process_company)
    calibrated = source.index("_apply_calibrated_acceptance(stage_a_row, entries, blind_metadata)")
    jev = source.index("_apply_jev_site_check(stage_a_row, entries, company, blind_metadata)")
    reference = source.index("reference_resolution.complete_with_reference(")
    assert calibrated < jev < reference


def test_panel_command_passes_the_fair_only_when_given():
    command, _env = run_launcher.build_command(run_launcher.MODE_FREE, "fuar.xlsx", 10, fair="  Ornek  Fuar 2026: makine ")
    assert command[command.index("--fuar") + 1] == "Ornek Fuar 2026: makine"
    command, _env = run_launcher.build_command(run_launcher.MODE_FREE, "fuar.xlsx", 10, fair=" ")
    assert "--fuar" not in command
    assert run_launcher.RunSession(run_launcher.MODE_FREE, "fuar.xlsx", 3, fair=FAIR).fair == FAIR
