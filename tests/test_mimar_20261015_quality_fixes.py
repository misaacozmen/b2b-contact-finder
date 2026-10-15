"""Talimat 49: fixes found while checking delivered lists and tracking workbooks."""

import pytest

import config
from modules import calibration, email_kind, extractor, outreach


def _features(domain: str) -> dict:
    return {
        "domain": domain, "url": f"https://{domain}/",
        "reachable": True, "s3": True, "s4": True, "conflict": False, "parked": False,
        "brand_prefix_len": 4, "rank_best": 1, "query_hits": 2,
        "tr_phone": True, "same_domain_email": False, "legacy_final_score": 60,
    }


@pytest.mark.parametrize("domain, accepted", [
    ("ornekbebek.com.tr", True), ("ornekbebek.com", True),
    ("ornekbebek.it", False), ("ornekbebek.co.za", False), ("ornekbebek.de", False),
])
def test_calibrated_rule_leaves_out_other_country_domains(domain, accepted):
    assert calibration.rule_accepts(_features(domain), 1, config.CALIBRATED_ACCEPTANCE_RULE) is accepted


@pytest.mark.parametrize("page, expected", [
    ("<p>E-posta: <span>İnfo@ornekbebek.com.tr</span></p>", ["info@ornekbebek.com.tr"]),
    ("<p>nous envoyer un e-mail à l’adressehello@ornekbebek.online ou</p>", ["hello@ornekbebek.online"]),
    ("<p>E-posta:bilgi@ornekbebek.com.tr</p>", ["bilgi@ornekbebek.com.tr"]),
    ("<p>adres@ornekbebek.com.tr</p>", ["adres@ornekbebek.com.tr"]),
])
def test_addresses_read_whole(page, expected):
    assert extractor.extract_emails(page) == expected


@pytest.mark.parametrize("email, personal", [
    ("ozer.ornekoglu@ornek-catering.com", True), ("ercan@ornek-lojistik.com.tr", True),
    ("satis.destek@ornek-lojistik.com.tr", False), ("stal.kolno@firma-ornek.pl", False),
])
def test_more_turkish_first_names(email, personal):
    assert email_kind.is_personal(email) is personal


@pytest.mark.parametrize("name, expected", [
    ("ASYA ORNEK BİLG.MLZ.VE MATB.SAN.VE TİC.LTD.ŞTİ.", "Asya Ornek"),
    ("ORNEK PROM. SOS. HİZ. MATB.", "Ornek"),
    ("Ornekorganizasyon", "Ornek Organizasyon"),
    ("Ornekmasakiralama", "Ornek Masa Kiralama"),
    ("Kltorganizasyon", "KLT Organizasyon"),
    ("Qcatering", "Q Catering"),
    ("ornekbaski.com.tr", "Ornek Baskı"),
    ("Ornek Catering: Fuar ve Toplantı İkramı", "Ornek Catering"),
    ("Ornek, Fuar Hostesi, Organizasyon Ajansı", "Ornek"),
    ("Ornek-Fuar-Ekipmanlari-Kiralama", "Ornek Fuar Ekipmanlari"),
    ("ORNEK TANITIM HİZMETLERİ", "Ornek Tanıtım Hizmetleri"),
    ("ORNEK TAŞIMACILIK", "Ornek Taşımacılık"),
    ("ORNEK DIGITAL PRINT", "Ornek Digital Print"),
])
def test_greeting_names_from_titles_and_abbreviations(name, expected):
    assert outreach.salutation(name, "TR") == expected
