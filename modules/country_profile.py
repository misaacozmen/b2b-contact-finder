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
    "LEGAL_COMPANY_WORDS", "QUERY_ABBREVIATION_STOPWORDS", "NAME_END_LEGAL_WORDS", "BRAND_CORPORATE_WORDS",
    "EMAIL_PRIORITY_PREFIXES", "CONTACT_EMAIL_LOCALS",
    "COUNTRY_CITY_MARKERS", "COUNTRY_IDENTITY_MARKERS", "COUNTRY_PAGE_MARKERS",
    "COUNTRY_OBSERVATION_VALUES", "ADDRESS_COUNTRY_IGNORED_TERMS",
    "LOCAL_ROUTE_TOKENS", "LOCAL_MAILBOX_TOKENS",
    "EXTRA_CONTACT_LINK_WORDS", "EXTRA_ABOUT_LINK_WORDS", "EXTRA_FREE_MAIL_DOMAINS",
    "TAX_ID_FORMAT", "TAX_ID_LABELS", "TAX_ID_PAGE_WORDS", "CALIBRATED_DIRECTORY_DOMAINS",
    "DELIVERY_COMPANIES_ONLY", "COMPANY_REGISTER",
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

# Business registries, phone lookups, portals and fair sites that showed up for
# several different Polish firms in the calibration searches (Talimat 27).
_POLISH_DIRECTORY_DOMAINS = (
    "411.com", "academia.edu", "agoda.com", "alamy.com", "aleo.com", "allegro.pl", "aplikuj.pl",
    "areacodelocator.net", "auto.ru", "autoline.com.pl", "avito.ru", "bab.la", "behance.net",
    "bing.com", "bizintel.pl", "bizraport.pl", "booking.com", "ceginformacio.hu", "ceneo.pl",
    "cenyrolnicze.pl", "chi-chiamato.com", "cylex-polska.pl", "detail.cz", "dreamstime.com",
    "ebay.com", "edu.uz", "eduvulcan.pl", "egospodarka.pl", "eon.de", "equista.pl", "fakt.pl",
    "firmania.pl", "firmbook.eu", "firmy.cz", "github.com", "godzinyotwarcia24.pl", "gov.pl",
    "gowork.pl", "imsig.pl", "infor.pl", "interweb.spb.ru", "kim-ariyor.com", "krdf.pl",
    "krs-online.com.pl", "krs-pobierz.pl", "kurzy.cz", "masio.pl", "misterwhat.pl",
    "mobiletator.com", "money.pl", "monitorkrs.pl", "ngo.pl", "nipregon.pl", "numerostelefono.com",
    "numlookup.com", "numtrace.com", "nuzle.pl", "o2.pl", "oferteo.pl", "ok.ru", "okredo.com",
    "olx.pl", "onet.pl", "otomoto.pl", "owg.pl", "ozon.kz", "panoramafirm.pl", "parp.gov.pl",
    "pb.pl", "pexels.com", "phonedetectivetech.com", "phonenumbers.org", "pkt.pl", "poczytaj.pl",
    "podnikatel.cz", "podobne-firmy.pl", "portaltargowy.pl", "praca.pl", "pracahandlowiec.pl", "pracuj.pl",
    "prezi.com", "pwe-expoplanner.com", "pzhk.pl", "pzj.pl", "qoobus.com", "radiomaryja.pl",
    "rejestr.io", "rejestrkrs.pl", "researchgate.net", "rp.pl", "rusprofile.ru", "rutracker.org", "rutube.ru",
    "scamcall.ru",
    "sggw.edu.pl", "shazam.com", "shutterstock.com", "smartphonetime.altervista.org",
    "sofascore.com", "soundcloud.com", "sprawozdaniaonline.pl", "synteza.pro", "targeo.pl",
    "targiferma.com.pl", "teraz-otwarte.pl", "thephoneindex.com",
    "thesmartphoneantwerpen.altervista.org", "todalocas.org", "top-wet.pl", "trojmiasto.pl",
    "trustmate.io", "tvp.pl", "veterinaryexpopoland.com", "vk.ru", "vkvideo.ru", "vstion.site",
    "warsawexpo.eu", "whocalls.me", "whoisphone.org", "wildberries.ru", "woblink.com",
    "wordpress.com", "wp.pl", "wprost.pl", "wyszukiwarkakrs.pl", "xkrs.pl", "yahoo.co.jp",
    "yandex.ru", "yellowpages.pl", "zakupowe.info", "zamantika.com", "znanylekarz.pl",
)

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
    # Polish names often continue after the legal form with the partners'
    # names ("... SPÓŁKA CYWILNA JAN KOWALSKI") (Talimat 29).
    "NAME_END_LEGAL_WORDS": ("spolka", "sp", "sc", "sa", "sj", "spk"),
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
    "TAX_ID_FORMAT": "PL_NIP",
    "TAX_ID_LABELS": ("NIP", "NIP-UE", "NIP UE", "VAT", "VAT-UE", "VAT UE", "VAT ID", "Tax ID"),
    "TAX_ID_PAGE_WORDS": (
        "regulamin", "polityka", "prywatn", "rodo", "kontakt", "contact", "o-nas", "o-firmie",
        "onas", "dane", "impressum", "nota-prawna", "informacj", "terms", "privacy", "cookies",
        "warunki",
    ),
    "CALIBRATED_DIRECTORY_DOMAINS": sorted({
        *TURKEY["CALIBRATED_DIRECTORY_DOMAINS"], *_POLISH_DIRECTORY_DOMAINS,
    }),
    "DELIVERY_COMPANIES_ONLY": True,
}

# Place names matched inside page text; short or common words (uk, york,
# reading, bath) would also match other countries or ordinary words.
_BRITISH_PLACES = (
    "united kingdom", "great britain", "england", "scotland", "wales", "northern ireland",
    "london", "birmingham", "manchester", "leeds", "glasgow", "edinburgh", "liverpool", "bristol",
    "sheffield", "newcastle upon tyne", "nottingham", "leicester", "coventry", "cardiff", "belfast",
    "southampton", "brighton", "milton keynes", "peterborough", "aberdeen", "northampton",
    "wolverhampton", "portsmouth", "bradford", "huddersfield", "swindon", "norwich", "ipswich",
)
_BRITISH_LEGAL_WORDS = ["plc", "llp", "cic", "uk"]

# Business directories, registry copies, review and job sites, marketplaces,
# venues and organisers that are never an exhibitor's own website (Talimat 44).
_BRITISH_DIRECTORY_DOMAINS = (
    "10times.com", "192.com", "amazon.co.uk", "applegate.co.uk", "bark.com", "bing.com", "bizdb.co.uk",
    "bizstats.co.uk", "brownbook.net", "check-business.co.uk", "checkatrade.com", "clarionevents.com",
    "companieshouse.gov.uk", "companiesintheuk.co.uk", "companieslist.co.uk", "companycheck.co.uk",
    "cv-library.co.uk", "cylex-uk.co.uk", "dnb.co.uk", "duedil.com", "easyfairs.com", "ebay.co.uk",
    "endole.co.uk", "eventbrite.co.uk", "eventbrite.com", "eventcity.co.uk", "excel.london",
    "expodatabase.com", "find-open.co.uk", "freeindex.co.uk", "glassdoor.co.uk", "gov.uk", "gumtree.com",
    "harrogateconventioncentre.co.uk", "hotfrog.co.uk", "houzz.co.uk", "hub.biz", "indeed.co.uk",
    "indeed.com", "lusha.com", "manchestercentral.co.uk", "misterwhat.co.uk", "mybuilder.com",
    "northdata.com", "northdata.de", "olympia.london", "opencorporates.com", "opengovuk.com",
    "owler.com", "ratedpeople.com", "reed.co.uk", "rocketreach.co", "rxglobal.com", "scoot.co.uk",
    "sec.co.uk", "signalhire.com", "tellows.co.uk", "theicc.co.uk", "thenec.co.uk", "thomsonlocal.com",
    "totaljobs.com", "tradefairdates.com", "trustpilot.com", "tuugo.co.uk", "uk-companies.info",
    "ukbusinessforums.co.uk", "ukdata.com", "who-called.co.uk", "yably.co.uk", "yell.com", "yelp.co.uk",
)

UNITED_KINGDOM = {
    **copy.deepcopy(TURKEY),
    "TARGET_COUNTRY": "GB",
    "TARGET_COUNTRY_QUERY_TERMS": ["UK"],
    "LOCALE": "en-GB",
    "SEARCH_QUERY_TEMPLATES": ["{company} official website", "{company} contact", "{company} contact us"],
    "SEARCH_COUNTRY_QUERY_TEMPLATES": ["{company} {country} official website"],
    "COUNTRY_QUERY_NAME": "UK",
    "OFFICIAL_SITE_PHRASE": "official website",
    "OFFICIAL_SITE_SHORT_PHRASE": "official site",
    "WEBSITE_QUERY_WORD": "website",
    "CONTACT_QUERY_WORD": "contact",
    "CONTACT_QUERY_WORD_NATIVE": "contact us",
    "REPRESENTATIVE_QUERY_WORDS": "distributor reseller",
    "BRAND_OFFICIAL_QUERY_PHRASE": "brand official website",
    "LEGAL_NOTICE_QUERY_WORD": "privacy",
    "TRADE_NAME_QUERY_WORD": "company number",
    "DDGS_REGION": "uk-en",
    "PHONE_DEFAULT_COUNTRY": "GB",
    # Guernsey, Jersey and the Isle of Man share +44 but are regions of their own.
    "PHONE_ALLOWED_COUNTRIES": ["GB", "GG", "JE", "IM"],
    "FOREIGN_COUNTRY_TLDS": sorted({*TURKEY["FOREIGN_COUNTRY_TLDS"], "tr"} - {"uk"}),
    "COUNTRY_TLD_BONUSES": {".co.uk": 8, ".org.uk": 5, ".uk": 5},
    "DOMAIN_GUESS_TLDS": [".co.uk", ".com", ".uk"],
    "COUNTRY_DOMAIN_SUFFIXES": (".uk",),
    "BRIGHTDATA_GOOGLE_GL": "uk",
    "BRIGHTDATA_GOOGLE_HL": "en",
    "BRIGHTDATA_COUNTRY": "gb",
    "CONTACT_PAGE_PATHS": [
        "/contact", "/contact/", "/contact-us", "/contact-us/", "/contactus", "/contact.html",
        "/contact-us.html", "/get-in-touch", "/get-in-touch/", "/about/contact", "/pages/contact",
        "/pages/contact-us", "/about", "/about-us",
    ],
    "IDENTITY_PAGE_PATHS": [
        "/about", "/about-us", "/company-information", "/privacy", "/privacy-policy", "/privacy-notice",
        "/cookie-policy", "/terms", "/terms-and-conditions", "/terms-of-use", "/legal", "/legal-notice",
        "/imprint",
    ],
    "LEGAL_COMPANY_WORDS": [*TURKEY["LEGAL_COMPANY_WORDS"], *_BRITISH_LEGAL_WORDS],
    "QUERY_ABBREVIATION_STOPWORDS": [
        *TURKEY["QUERY_ABBREVIATION_STOPWORDS"], *_BRITISH_LEGAL_WORDS, "ltd.", "plc.", "l.l.p", "l.l.p.",
    ],
    # What follows the legal form ("ACME LTD T/A ACME STANDS") is not part of the name.
    "NAME_END_LEGAL_WORDS": ("ltd", "limited", "plc", "llp"),
    "BRAND_CORPORATE_WORDS": (
        *TURKEY["BRAND_CORPORATE_WORDS"], "ltd", "plc", "llp", "company", "group", "holdings",
    ),
    "EMAIL_PRIORITY_PREFIXES": ["sales", "enquiries", "info", "hello", "office", "contact", "marketing"],
    "CONTACT_EMAIL_LOCALS": (
        "info", "enquiries", "enquiry", "hello", "contact", "sales", "office", "admin", "mail", "general",
    ),
    "COUNTRY_CITY_MARKERS": _BRITISH_PLACES,
    "COUNTRY_IDENTITY_MARKERS": _BRITISH_PLACES,
    "COUNTRY_PAGE_MARKERS": _BRITISH_PLACES,
    "COUNTRY_OBSERVATION_VALUES": (
        "gb", "uk", "united kingdom", "great britain", "england", "scotland", "wales", "northern ireland",
    ),
    "ADDRESS_COUNTRY_IGNORED_TERMS": ("united kingdom", "great britain", "uk", "gb", "england"),
    "LOCAL_ROUTE_TOKENS": ("uk", "gb", "en-gb", "en_gb", "united-kingdom"),
    "LOCAL_MAILBOX_TOKENS": ("uk", "gb"),
    "EXTRA_CONTACT_LINK_WORDS": ("contact-us", "contact us", "get-in-touch", "get in touch"),
    "EXTRA_ABOUT_LINK_WORDS": (
        "about-us", "about us", "privacy-policy", "privacy policy", "terms-and-conditions", "company-information",
    ),
    "EXTRA_FREE_MAIL_DOMAINS": (
        "btinternet.com", "btopenworld.com", "btconnect.com", "sky.com", "virginmedia.com", "ntlworld.com",
        "blueyonder.co.uk", "talktalk.net", "tiscali.co.uk", "yahoo.co.uk", "hotmail.co.uk", "live.co.uk",
        "aol.co.uk", "aol.com", "plus.com", "orange.net", "tesco.net", "fsmail.net",
    ),
    # British catalogues carry no tax identifier; company numbers are checked
    # for the customer file instead (modules/company_register.py).
    "TAX_ID_FORMAT": "",
    "TAX_ID_LABELS": (),
    "TAX_ID_PAGE_WORDS": (),
    "CALIBRATED_DIRECTORY_DOMAINS": sorted({
        *TURKEY["CALIBRATED_DIRECTORY_DOMAINS"], *_BRITISH_DIRECTORY_DOMAINS,
    }),
    # UK law lets a business e-mail a company without consent, not a sole trader or a partnership.
    "DELIVERY_COMPANIES_ONLY": True,
    "COMPANY_REGISTER": "companies_house",
}

PROFILES = {"TR": TURKEY, "PL": POLAND, "GB": UNITED_KINGDOM}
LABELS = {"TR": "Türkiye", "PL": "Polonya", "GB": "Birleşik Krallık"}


def apply(code: str) -> None:
    """Make every country setting follow the given profile."""
    code = str(code or "").strip().upper()
    if code not in PROFILES:
        raise ValueError(f"unknown country profile: {code}")
    for name, value in PROFILES[code].items():
        setattr(config, name, copy.deepcopy(value))


def current() -> str:
    return str(config.TARGET_COUNTRY)
