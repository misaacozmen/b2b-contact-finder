"""British company check for customer files (Talimat 44).

UK law lets a business e-mail a company (Ltd, PLC, LLP and other corporate
bodies) without consent, but not a sole trader or a partnership.  A British
customer file keeps a firm whose name shows no company form only when:

* its own website shows a company number that Companies House lists as an
  active company of a corporate type whose name shares a word with the firm's
  name or web address, or stands on the page that shows the number ("a
  trading brand of ... Ltd"); or
* Companies House holds an active company of a corporate type with exactly
  the firm's name; or
* without a Companies House key: one page of its own website shows a labelled
  company number and a "... Ltd / Limited / LLP / PLC" name sharing a word
  with the firm's name or web address.

Answers are kept in state/company_checks.json, so a sample and the full list
do not ask twice.  The key is read from the key store and never printed.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
import re
import time
import uuid

import requests

import config
from modules import api_configuration, extractor, legal_form, scorer, tax_identity


# Companies House company types that are corporate bodies.  Limited
# partnerships in England and Wales are not; unknown types count as not.
CORPORATE_TYPES = frozenset({
    "ltd", "plc", "llp", "private-unlimited", "private-unlimited-nsc", "private-limited-guarant-nsc",
    "private-limited-guarant-nsc-limited-exemption", "private-limited-shares-section-30-exemption",
    "old-public-company", "northern-ireland", "royal-charter", "registered-society-non-jurisdictional",
    "industrial-and-provident-society", "charitable-incorporated-organisation",
    "scottish-charitable-incorporated-organisation", "uk-establishment", "oversea-company",
    "european-public-limited-liability-company-se", "united-kingdom-societas", "scottish-partnership",
    "further-education-or-sixth-form-college-corporation",
})
# Letters that start a company number: Scotland, Northern Ireland, LLPs, overseas companies and others.
NUMBER_PREFIXES = (
    "SC", "SO", "SL", "SZ", "SA", "SF", "SP", "SR", "SE", "NI", "NC", "NL", "NF", "NP", "NO", "NR", "R0",
    "OC", "LP", "FC", "GE", "GS", "GN", "IP", "RS", "RC", "IC", "SI", "CE", "CS", "AC", "ZC",
)
_PLACE = r"(?:england(?:\s*(?:&|and)\s*wales)?|wales|scotland|northern\s+ireland|the\s+uk|uk)"
_NUMBER_WORD = r"(?:no\b\.?|number|nr\b\.?|#)"
NUMBER_RE = re.compile(
    r"(?i)\b(?:"
    rf"(?:company|companies\s+house)\s+(?:registration\s+|registered\s+|reg\.?\s+)?{_NUMBER_WORD}"
    rf"|registered\s+(?:company\s+)?{_NUMBER_WORD}"
    rf"|registration\s+{_NUMBER_WORD}"
    rf"|reg\.?\s*{_NUMBER_WORD}"
    r"|crn"
    rf"|registered\s+in\s+{_PLACE}(?:\s*(?:,|\.|under|with)?\s*(?:company\s+)?(?:registration\s+)?{_NUMBER_WORD})?"
    r")[\s:.,#()-]{0,8}"
    rf"((?:{'|'.join(NUMBER_PREFIXES)})\s?\d{{6}}|\d{{6,8}})(?![\w])"
)
# Numbers labelled for something else (VAT, charity, FCA, trade marks).
OTHER_NUMBER_WORDS = ("vat", "charity", "fca", "trade mark", "trademark", "patent")
LEGAL_NAME_RE = re.compile(
    r"([A-Z0-9][\w&'’.-]*(?:[ \t]+(?:&|and|of|the|[A-Z0-9][\w&'’.-]*)){0,3})[ \t]+"
    r"(?:Limited|LIMITED|Ltd|LTD|LLP|PLC|plc|Cyfyngedig|Cyf)(?![\w])"
)
FORM_WORDS = frozenset({"limited", "ltd", "plc", "llp", "cic", "cyfyngedig", "cyf", "the"})
# Words many British firms in the fair trade share, and menu and footer words
# that page text runs into a name; a shared word must be more than these.
GENERIC_WORDS = frozenset({
    "about", "all", "and", "build", "builders", "by", "co", "company", "contact", "copyright",
    "creative", "design", "designs", "display", "displays", "event", "events", "exhibition",
    "exhibitions", "global", "graphics", "group", "holdings", "home", "interiors", "international",
    "marketing", "media", "news", "number", "office", "policy", "print", "privacy", "productions",
    "registered", "reserved", "rights", "services", "signs", "site", "solutions", "stand", "stands",
    "studio", "studios", "systems", "terms", "trading", "uk", "us", "website",
}) | legal_form.BUSINESS_WORDS
PAGE_WORDS = (
    "privacy", "terms", "conditions", "legal", "cookie", "about", "contact", "company", "imprint",
    "disclaimer",
)
# A page that labels more numbers than this lists several companies.
MAX_PAGE_NUMBERS = 2
MAX_LOOKUPS = 2
SEARCH_RESULTS = 20


class RegisterError(Exception):
    """Companies House could not be asked."""


def api_key() -> str:
    return config.COMPANIES_HOUSE_API_KEY or api_configuration.load_saved_api_keys().get("companies_house", "")


def normalize_number(value: str) -> str:
    text = re.sub(r"\s", "", str(value or "")).upper()
    if text.isdigit() and 6 <= len(text) <= 8:
        return text.zfill(8)
    return text if re.fullmatch(rf"(?:{'|'.join(NUMBER_PREFIXES)})\d{{6}}", text) else ""


def page_numbers(text: str) -> list[str]:
    """Company numbers a page labels, in order; other registers' numbers are skipped."""
    numbers: list[str] = []
    for match in NUMBER_RE.finditer(text):
        before = text[max(0, match.start() - 20):match.start()].casefold()
        number = normalize_number(match.group(1))
        if number and number not in numbers and not any(word in before for word in OTHER_NUMBER_WORDS):
            numbers.append(number)
    return numbers


def legal_names(text: str) -> list[str]:
    return [" ".join(match.group(0).split()) for match in LEGAL_NAME_RE.finditer(text)]


def _words(name: str) -> set[str]:
    return {word for word in legal_form._fold(name.replace("&", " and ")).split() if word not in FORM_WORDS}


def related(register_name: str, firm: str, website: str = "") -> bool:
    """The register's name shares a distinctive word with the firm's name or web address."""
    firm_words = _words(firm)
    core = scorer.compact_domain_core(website) if website else ""
    for word in _words(register_name) - GENERIC_WORDS:
        if len(word) < 3 or word.isdigit():
            continue
        if word in firm_words or (len(word) >= 4 and word in core):
            return True
    return False


def _bare(name: str) -> str:
    return " ".join(word for word in legal_form._fold(name.replace("&", " and ")).split() if word not in FORM_WORDS)


def same_name(register_name: str, firm: str) -> bool:
    """Both names are the same once forms, "the", "&" and punctuation are set aside."""
    bare = _bare(register_name)
    return bool(bare) and bare == _bare(firm)


def declared(register_name: str, text: str) -> bool:
    """The page names the register's company: "Ornek Displays is a trading brand of Beta Ornek Ltd"."""
    bare = _bare(register_name)
    return len(bare) >= 3 and f" {bare} " in f" {_bare(text)} "


def corporate(status: str, company_type: str) -> bool:
    return status == "active" and company_type in CORPORATE_TYPES


class CompaniesHouse:
    """The Companies House public data API, at most one request per interval."""

    def __init__(self, key: str, session=None, min_interval: float | None = None):
        self._key = key
        self._session = session or requests.Session()
        self._interval = config.COMPANIES_HOUSE_MIN_INTERVAL_SEC if min_interval is None else min_interval
        self._last = 0.0

    def _get(self, path: str, params: dict | None = None) -> dict:
        wait = self._last + self._interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            response = self._session.get(
                f"{config.COMPANIES_HOUSE_API_URL}{path}", params=params, auth=(self._key, ""),
                timeout=config.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            raise RegisterError(type(exc).__name__) from None
        if response.status_code == 404:
            return {}
        if response.status_code != 200:
            raise RegisterError(f"http_{response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise RegisterError("bad_json") from None

    def company(self, number: str) -> dict:
        return self._get(f"/company/{number}")

    def search(self, name: str) -> list[dict]:
        payload = self._get("/search/companies", {"q": name, "items_per_page": SEARCH_RESULTS})
        return [item for item in payload.get("items") or [] if isinstance(item, dict)]


def own_pages(website: str, fetch_page) -> list[dict]:
    """The firm's home page, then its legal and contact pages until one labels a company number."""
    html = fetch_page(website) or ""
    if not html:
        return []
    pages = [{"url": website, "html": html}]
    if page_numbers(extractor._visible_text(html)):
        return pages
    for url in tax_identity.legal_links(pages, config.COMPANY_NUMBER_PAGE_CHECKS, words=PAGE_WORDS):
        html = fetch_page(url) or ""
        if not html:
            continue
        pages.append({"url": url, "html": html})
        if page_numbers(extractor._visible_text(html)):
            break
    return pages


def _site_evidence(pages: list[dict]) -> list[tuple[list[str], str]]:
    """The labelled company numbers and the text of each page that shows a few."""
    evidence = []
    for page in pages:
        text = extractor._visible_text(page.get("html", "") or "")
        numbers = page_numbers(text)
        if 0 < len(numbers) <= MAX_PAGE_NUMBERS:
            evidence.append((numbers, text))
    return evidence


def check_firm(name: str, website: str, *, register: CompaniesHouse | None, fetch_page) -> dict:
    """Whether a firm is a company, how that was shown, and the company number when one was found."""
    result = {"company": False, "evidence": "", "number": "", "register_name": "", "register_error": False}
    if legal_form.is_company(name):
        return {**result, "company": True, "evidence": "name"}
    evidence = _site_evidence(own_pages(website, fetch_page)) if website else []
    texts: dict[str, str] = {}
    for numbers_, text in evidence:
        for number in numbers_:
            texts.setdefault(number, text)
    if register is not None:
        try:
            for number in list(texts)[:MAX_LOOKUPS]:
                profile = register.company(number)
                register_name = str(profile.get("company_name") or "")
                if corporate(profile.get("company_status"), profile.get("type")) and (
                    related(register_name, name, website) or declared(register_name, texts[number])
                ):
                    return {**result, "company": True, "evidence": "register_number", "number": number,
                            "register_name": register_name}
            for item in register.search(name):
                title = str(item.get("title") or "")
                if corporate(item.get("company_status"), item.get("company_type")) and same_name(title, name):
                    return {**result, "company": True, "evidence": "register_name",
                            "number": str(item.get("company_number") or ""), "register_name": title}
            return result
        except RegisterError:
            result["register_error"] = True
    for numbers_, text in evidence:
        legal = next((value for value in legal_names(text) if related(value, name, website)), "")
        if legal:
            return {**result, "company": True, "evidence": "site", "number": numbers_[0], "register_name": legal}
    return result


def _cache_key(name: str, website: str) -> str:
    return f"{' '.join(str(name).casefold().split())}|{scorer.registrable_domain(website) if website else ''}"


def _load_cache() -> dict:
    try:
        payload = json.loads(config.COMPANY_CHECK_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def _save_cache(entries: dict) -> None:
    path = config.COMPANY_CHECK_CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def check_firms(firms: list[tuple[str, str]], *, fetch_page, key: str | None = None, today: date | None = None) -> dict[str, dict]:
    """Each firm name's check; recent answers come from the cache.

    An answer given without Companies House is asked again once a key is saved.
    """
    key = api_key() if key is None else key
    register = CompaniesHouse(key) if key else None
    today = today or date.today()
    oldest = (today - timedelta(days=config.COMPANY_CHECK_MAX_AGE_DAYS)).isoformat()
    cache = _load_cache()
    results: dict[str, dict] = {}
    changed = False
    for name, website in firms:
        if name in results:
            continue
        cache_key = _cache_key(name, website)
        cached = cache.get(cache_key)
        if (
            isinstance(cached, dict) and str(cached.get("checked", "")) >= oldest
            and (cached.get("register") or register is None) and not cached.get("register_error")
        ):
            results[name] = cached
            continue
        result = check_firm(name, website, register=register, fetch_page=fetch_page)
        result = {**result, "checked": today.isoformat(), "register": register is not None}
        results[name] = result
        if result["evidence"] != "name":
            cache[cache_key] = result
            changed = True
    if changed:
        _save_cache(cache)
    return results


def summary(results: dict[str, dict], *, register_on: bool) -> dict:
    counts = {"name": 0, "register_number": 0, "register_name": 0, "site": 0}
    for result in results.values():
        if result.get("company"):
            counts[result.get("evidence")] = counts.get(result.get("evidence"), 0) + 1
    return {
        "companies_house": "on" if register_on else "off",
        **counts,
        "left_out": sum(1 for result in results.values() if not result.get("company")),
        "register_errors": sum(1 for result in results.values() if result.get("register_error")),
    }
