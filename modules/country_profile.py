"""Country profiles: settings that depend on the target country (Talimat 26).

Türkiye is the reference profile.  Its values are read from config when this
module is imported, so applying "TR" restores exactly what the system has
always used.  A run applies one profile before any work starts
(main.py --country); the run context records the applied values.
"""

from __future__ import annotations

import copy

import config


SETTING_NAMES = (
    "TARGET_COUNTRY", "TARGET_COUNTRY_QUERY_TERMS", "LOCALE",
    "SEARCH_QUERY_TEMPLATES", "SEARCH_COUNTRY_QUERY_TEMPLATES",
    "COUNTRY_QUERY_NAME", "OFFICIAL_SITE_PHRASE", "OFFICIAL_SITE_SHORT_PHRASE",
    "WEBSITE_QUERY_WORD", "CONTACT_QUERY_WORD", "CONTACT_QUERY_WORD_NATIVE",
    "REPRESENTATIVE_QUERY_WORDS", "BRAND_OFFICIAL_QUERY_PHRASE",
    "LEGAL_NOTICE_QUERY_WORD", "TRADE_NAME_QUERY_WORD", "DDGS_REGION",
    "PHONE_DEFAULT_COUNTRY", "PHONE_ALLOWED_COUNTRIES",
    "FOREIGN_COUNTRY_TLDS", "COUNTRY_TLD_BONUSES", "DOMAIN_GUESS_TLDS", "COUNTRY_DOMAIN_SUFFIXES",
    "BRIGHTDATA_GOOGLE_GL", "BRIGHTDATA_GOOGLE_HL", "BRIGHTDATA_COUNTRY",
    "CONTACT_PAGE_PATHS", "IDENTITY_PAGE_PATHS",
    "LEGAL_COMPANY_WORDS", "QUERY_ABBREVIATION_STOPWORDS", "BRAND_CORPORATE_WORDS",
    "EMAIL_PRIORITY_PREFIXES", "CONTACT_EMAIL_LOCALS",
    "COUNTRY_CITY_MARKERS", "COUNTRY_IDENTITY_MARKERS", "COUNTRY_PAGE_MARKERS",
    "COUNTRY_OBSERVATION_VALUES", "ADDRESS_COUNTRY_IGNORED_TERMS",
    "LOCAL_ROUTE_TOKENS", "LOCAL_MAILBOX_TOKENS",
    "EXTRA_CONTACT_LINK_WORDS", "EXTRA_ABOUT_LINK_WORDS", "EXTRA_FREE_MAIL_DOMAINS",
)

TURKEY = {name: copy.deepcopy(getattr(config, name)) for name in SETTING_NAMES}

_POLISH_CITIES = (
    "polska", "poland", "warszawa", "krakow", "lodz", "wroclaw", "poznan", "gdansk",
    "szczecin", "bydgoszcz", "lublin", "katowice", "bialystok", "gdynia", "czestochowa",
    "radom", "torun", "kielce", "rzeszow", "olsztyn", "opole",
)
_POLISH_LEGAL_WORDS = [
    "sp", "z", "o", "oo", "spolka", "ograniczona", "odpowiedzialnoscia", "akcyjna",
    "jawna", "komandytowa", "cywilna", "sa", "spk", "sj", "sc", "i",
]

POLAND = {
    **copy.deepcopy(TURKEY),
    "TARGET_COUNTRY": "PL",
    "TARGET_COUNTRY_QUERY_TERMS": ["Polska"],
    "LOCALE": "pl-PL",
    "SEARCH_QUERY_TEMPLATES": ["{company} oficjalna strona", "{company} contact", "{company} kontakt"],
    "SEARCH_COUNTRY_QUERY_TEMPLATES": ["{company} {country} oficjalna strona internetowa"],
    "COUNTRY_QUERY_NAME": "Polska",
    "OFFICIAL_SITE_PHRASE": "oficjalna strona internetowa",
    "OFFICIAL_SITE_SHORT_PHRASE": "oficjalna strona",
    "WEBSITE_QUERY_WORD": "strona internetowa",
    "CONTACT_QUERY_WORD": "kontakt",
    "CONTACT_QUERY_WORD_NATIVE": "kontakt",
    "REPRESENTATIVE_QUERY_WORDS": "dystrybutor przedstawiciel",
    "BRAND_OFFICIAL_QUERY_PHRASE": "marka oficjalna strona",
    "LEGAL_NOTICE_QUERY_WORD": "rodo",
    "TRADE_NAME_QUERY_WORD": "NIP",
    "DDGS_REGION": "pl-pl",
    "PHONE_DEFAULT_COUNTRY": "PL",
    "PHONE_ALLOWED_COUNTRIES": ["PL"],
    "FOREIGN_COUNTRY_TLDS": sorted({*TURKEY["FOREIGN_COUNTRY_TLDS"], "tr"} - {"pl"}),
    "COUNTRY_TLD_BONUSES": {".com.pl": 8, ".pl": 5},
    "DOMAIN_GUESS_TLDS": [".pl", ".com.pl", ".com"],
    "COUNTRY_DOMAIN_SUFFIXES": (".pl",),
    "BRIGHTDATA_GOOGLE_GL": "pl",
    "BRIGHTDATA_GOOGLE_HL": "pl",
    "BRIGHTDATA_COUNTRY": "pl",
    "CONTACT_PAGE_PATHS": [
        "/kontakt", "/kontakt/", "/contact", "/contact/", "/kontakt.html", "/kontakt.php",
        "/pl/kontakt", "/pl/kontakt/", "/en/contact", "/contact-us", "/dane-kontaktowe",
        "/o-nas", "/o-firmie",
    ],
    "IDENTITY_PAGE_PATHS": [
        "/o-nas", "/o-firmie", "/firma", "/about", "/about-us", "/dane-firmy",
        "/polityka-prywatnosci", "/rodo", "/regulamin", "/impressum", "/privacy-policy", "/legal",
    ],
    "LEGAL_COMPANY_WORDS": [*TURKEY["LEGAL_COMPANY_WORDS"], *_POLISH_LEGAL_WORDS],
    "QUERY_ABBREVIATION_STOPWORDS": [
        *TURKEY["QUERY_ABBREVIATION_STOPWORDS"], *_POLISH_LEGAL_WORDS,
        "o.o", "o.o.", "s.a", "s.a.", "sp.k", "sp.k.", "sp.j", "sp.j.", "s.c", "s.c.",
    ],
    "BRAND_CORPORATE_WORDS": (
        *TURKEY["BRAND_CORPORATE_WORDS"], "spolka", "akcyjna", "jawna", "komandytowa",
        "ograniczona", "odpowiedzialnoscia",
    ),
    "EMAIL_PRIORITY_PREFIXES": [
        "sprzedaz", "handel", "sales", "export", "biuro", "office", "kontakt", "info", "marketing",
    ],
    "CONTACT_EMAIL_LOCALS": (
        "biuro", "kontakt", "info", "office", "sekretariat", "sprzedaz", "handel", "sales",
        "export", "contact",
    ),
    "COUNTRY_CITY_MARKERS": _POLISH_CITIES,
    "COUNTRY_IDENTITY_MARKERS": _POLISH_CITIES,
    "COUNTRY_PAGE_MARKERS": (
        *_POLISH_CITIES, "kraków", "łódź", "wrocław", "poznań", "gdańsk", "białystok",
        "częstochowa", "toruń", "rzeszów",
    ),
    "COUNTRY_OBSERVATION_VALUES": ("pl", "poland", "polska"),
    "ADDRESS_COUNTRY_IGNORED_TERMS": ("polska", "poland", "pl"),
    "LOCAL_ROUTE_TOKENS": ("pl", "pl-pl", "pl_pl", "polska", "poland"),
    "LOCAL_MAILBOX_TOKENS": ("pl", "polska", "poland"),
    "EXTRA_CONTACT_LINK_WORDS": ("kontakt", "dane-kontaktowe", "dane kontaktowe"),
    "EXTRA_ABOUT_LINK_WORDS": (
        "o-nas", "o nas", "o-firmie", "o firmie", "rodo", "polityka-prywatnosci", "polityka prywatnosci",
    ),
    "EXTRA_FREE_MAIL_DOMAINS": (
        "wp.pl", "o2.pl", "onet.pl", "onet.eu", "op.pl", "interia.pl", "interia.eu",
        "gazeta.pl", "tlen.pl", "poczta.fm", "vp.pl", "go2.pl",
    ),
}

PROFILES = {"TR": TURKEY, "PL": POLAND}
LABELS = {"TR": "Türkiye", "PL": "Polonya"}


def apply(code: str) -> None:
    """Make every country setting follow the given profile."""
    code = str(code or "").strip().upper()
    if code not in PROFILES:
        raise ValueError(f"unknown country profile: {code}")
    for name, value in PROFILES[code].items():
        setattr(config, name, copy.deepcopy(value))


def current() -> str:
    return str(config.TARGET_COUNTRY)
