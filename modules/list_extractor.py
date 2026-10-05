"""Extract exhibitor lists from fair websites or saved list pages.

The extractor does not know any particular fair.  On a list page it looks
for repeated exhibitor cards and for embedded JSON records, keeps the richer
result, follows numbered pagination on the same path and, when the cards carry
no website, reads each exhibitor's profile page on the fair site.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook

from modules import extractor, network_guard, scorer


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT_SEC = 30
REQUEST_DELAY_SEC = 0.5
MAX_PAGES = 60
MAX_PROFILES = 800
MIN_RECORDS = 5
SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "wa.me",
    "whatsapp.com", "t.me", "google.com", "goo.gl", "apple.com", "vimeo.com",
)
NAME_KEYS = (
    "firma_adi", "firma_unvani", "unvan", "company_name", "companyname",
    "company", "firma", "exhibitor_name", "exhibitor", "name", "title",
)
WEB_KEYS = ("website", "web", "web_site", "url", "site", "www", "homepage")
PHONE_KEYS = ("phone", "telefon", "tel", "telephone", "contactphone", "contact_phone", "phone_number", "phonenumber")
EMAIL_KEYS = ("email", "e_mail", "e_posta", "eposta", "mail", "contactemail", "contact_email")
STAND_KEYS = ("stand", "stant", "booth", "standnumber", "stand_number", "boothnumber", "booth_number")
HALL_KEYS = ("hall", "hol", "salon", "hallname", "hall_name")
TAX_ID_KEYS = (
    "nip", "tax_id", "taxid", "tax_number", "taxnumber", "vat_id", "vatid", "vat_number",
    "vatnumber", "vergi_no", "vergino", "vkn",
)
CONTACT_KEYS = (*WEB_KEYS, *PHONE_KEYS, *EMAIL_KEYS)
WEBSITE_VALUE_RE = re.compile(r"^(?:https?://)?(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:[/?#]\S*)?$", re.I)
DATA_URL_RE = re.compile(r"https?:(?:\\?/){2}[^\"'\s<>]+?\.json\b")
MAX_DATA_FILES = 3
COUNTRY_KEYS = ("country", "ulke", "countries")
NAME_ATTRIBUTES = (
    "data-company-name", "data-firm-name", "data-exhibitor-name", "data-company",
    "data-name", "data-firma", "data-title",
)
COUNTRY_ATTRIBUTES = ("data-country", "data-countries", "data-ulke")
LOCATION_ATTRIBUTE_RE = re.compile(r"^data-[\w-]*(?:country|location|ulke)[\w-]*$")
NAME_SELECTORS = (
    "h1", "h2", "h3", "h4", "h5", "h6", "[class*=name]", "[class*=title]",
    "[class*=firma]", "[class*=company]", "strong", "b",
)
PAGE_PARAMETERS = ("page", "sayfa", "p", "pg", "paged")
LEGAL_MARKER_RE = re.compile(
    r"\b(?:a\.?\s?s|ltd|sti|san|tic|inc|llc|gmbh|s\.?a|srl|sp\.?\s?z\s?o\.?\s?o|b\.?v|ag|kg|corp|limited)\b"
)
STAND_RE = re.compile(
    r"\b(?:stand|stant|booth)\s*(?:no)?\s*[:.\-]?\s*([A-Z0-9][A-Z0-9\-]*(?:\s*[/,]\s*[A-Z0-9][A-Z0-9\-]*)*)", re.I,
)
HALL_RE = re.compile(r"\b(?:hall|hol|salon)\s*[:.\-]?\s*([A-Z0-9][A-Z0-9\-]{0,5})", re.I)
CHROME_TOKEN_RE = re.compile(r"(?:^|[-_])(?:menu|nav|navbar|navigation|footer|header|breadcrumb)(?:[-_]|$)")
RECORD_FIELDS = ("company", "website", "phone", "email", "country", "hall", "stand", "profile_url", "tax_id")


def fold(value: object) -> str:
    text = str(value or "").replace("ı", "i").replace("İ", "i").replace("ł", "l").replace("Ł", "l")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _is_social(url: str) -> bool:
    host = scorer.normalize_domain(url)
    return any(host == item or host.endswith(f".{item}") for item in SOCIAL_HOSTS)


def external_website(href: str, page_url: str) -> str:
    """Return an absolute company website link, or "" for site chrome."""
    absolute = urljoin(page_url, str(href or "").strip())
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    if scorer.same_registrable_domain(absolute, page_url) or _is_social(absolute):
        return ""
    if re.search(r"\.(?:png|jpe?g|gif|webp|svg|pdf|css|js|zip)$", parts.path, re.I):
        return ""
    return absolute


def name_from_website(website: str) -> str:
    core = scorer.domain_core(website)
    return core.replace("-", " ").title() if core else ""


def _pick(record: dict, keys: tuple[str, ...]) -> str:
    lowered = {fold(key).replace(" ", "_"): value for key, value in record.items()}
    for key in keys:
        value = lowered.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and _clean(value):
            return _clean(value)
    return ""


def _json_objects(text: str):
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        yield value
        index = text.find("{", end)


def _walk(value, parent=None):
    """Yield (dict, enclosing dict) pairs; list items have no enclosing dict."""
    if isinstance(value, dict):
        yield value, parent
        for item in value.values():
            yield from _walk(item, value)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, None)


def _with_parent_fields(node: dict, parent: dict | None) -> dict:
    """The node's fields, then its parent's simple and one-level nested fields.

    Catalog data often keeps the stand or address beside the company block.
    """
    merged = dict(node)
    nested: dict = {}
    for key, value in (parent or {}).items():
        if isinstance(value, dict):
            for inner_key, inner in value.items():
                nested.setdefault(inner_key, inner)
        elif not isinstance(value, list):
            merged.setdefault(key, value)
    for key, value in nested.items():
        merged.setdefault(key, value)
    return merged


def _website_value(value: str) -> str:
    value = _clean(value)
    return value if WEBSITE_VALUE_RE.match(value) else ""


def data_urls(html: str, page_url: str) -> list[str]:
    """JSON data files on the fair's own site referenced by the page scripts."""
    urls: list[str] = []
    for match in DATA_URL_RE.findall(html):
        url = match.replace("\\/", "/")
        if scorer.same_registrable_domain(url, page_url) and url not in urls:
            urls.append(url)
    return urls[:MAX_DATA_FILES]


def json_records(soup: BeautifulSoup) -> list[dict]:
    """Records from JSON embedded in scripts or element attributes."""
    texts = [script.string or "" for script in soup.find_all("script")]
    for element in soup.find_all(True):
        texts.extend(value for value in element.attrs.values() if isinstance(value, str) and "{" in value)
    return records_from_texts(texts)


def records_from_texts(texts: list[str]) -> list[dict]:
    """Records from the best group of same-shaped JSON objects with a name.

    Groups whose objects carry a website, phone or email win; then groups
    whose names look like companies; then the larger group.
    """
    groups: dict[tuple, list[dict]] = {}
    for text in texts:
        for value in _json_objects(text):
            for node, parent in _walk(value):
                name = _pick(node, NAME_KEYS)
                if name and len(name) <= 200:
                    signature = tuple(sorted(fold(key).replace(" ", "_") for key in node))
                    groups.setdefault(signature, []).append(_with_parent_fields(node, parent))

    def group_score(item) -> tuple[int, int, int]:
        signature, members = item
        legal = sum(1 for node in members if LEGAL_MARKER_RE.search(fold(_pick(node, NAME_KEYS))))
        return (
            1 if any(key in CONTACT_KEYS for key in signature) else 0,
            1 if legal * 5 >= len(members) else 0,
            len(members),
        )

    best = max(groups.items(), key=group_score, default=((), []))[1]
    if len(best) < MIN_RECORDS:
        return []
    records = []
    for node in best:
        records.append({
            "company": _pick(node, NAME_KEYS),
            "website": _website_value(_pick(node, WEB_KEYS)),
            "phone": _pick(node, PHONE_KEYS),
            "email": _pick(node, EMAIL_KEYS),
            "country": _pick(node, COUNTRY_KEYS),
            "hall": _pick(node, HALL_KEYS),
            "stand": _pick(node, STAND_KEYS),
            "profile_url": "",
            "tax_id": _pick(node, TAX_ID_KEYS),
        })
    return records


def _card_attribute(card, attributes: tuple[str, ...]) -> str:
    for attribute in attributes:
        node = card if card.has_attr(attribute) else card.find(attrs={attribute: True})
        if node is not None and _clean(node.get(attribute)):
            return _clean(node.get(attribute))
    return ""


def _card_country(card) -> str:
    """Country from a data attribute, or the last part of a location label."""
    country = _card_attribute(card, COUNTRY_ATTRIBUTES)
    if country:
        return country
    for node in card.find_all(True):
        if any(LOCATION_ATTRIBUTE_RE.search(name) for name in node.attrs):
            text = _clean(node.get_text(" "))
            if text:
                return text.rsplit(",", 1)[-1].strip()
    return ""


def _card_name(card) -> str:
    name = _card_attribute(card, NAME_ATTRIBUTES)
    if name:
        return name
    for selector in NAME_SELECTORS:
        node = card.select_one(selector)
        if node is not None and _clean(node.get_text(" ")):
            return _clean(node.get_text(" "))
    image = card.find("img", alt=True)
    if image is not None and _clean(image.get("alt")):
        return _clean(image.get("alt"))
    link = card.find("a")
    return _clean(link.get_text(" ")) if link is not None else ""


def _card_record(card, page_url: str) -> dict:
    website = ""
    profile = ""
    list_path = urlsplit(page_url).path.rstrip("/")
    for link in card.find_all("a", href=True):
        href = str(link.get("href", "")).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        external = external_website(href, page_url)
        if external:
            website = website or external
            continue
        absolute = urljoin(page_url, href)
        if (
            not profile
            and scorer.same_registrable_domain(absolute, page_url)
            and urlsplit(absolute).path.rstrip("/") not in {"", list_path}
        ):
            profile = absolute
    markup = str(card)
    text = _clean(card.get_text(" "))
    phones = extractor.extract_phones(markup)
    emails = extractor.extract_emails(markup)
    stand = STAND_RE.search(text)
    hall = HALL_RE.search(text)
    return {
        "company": _card_name(card) or name_from_website(website),
        "website": website,
        "phone": phones[0] if phones else "",
        "email": emails[0] if emails else "",
        "country": _card_country(card),
        "hall": _clean(hall.group(1)) if hall and re.search(r"\d", hall.group(1)) else "",
        "stand": _clean(stand.group(1)) if stand and re.search(r"\d", stand.group(1)) else "",
        "profile_url": profile,
        "tax_id": "",
    }


def _record_score(record: dict) -> int:
    # A card without any link, contact or stand is a filter label or menu entry.
    if not (
        record["website"] or record["profile_url"] or record["phone"]
        or record["email"] or record["stand"] or record["hall"]
    ):
        return 0
    return (
        (2 if record["company"] else 0)
        + (2 if record["website"] else 0)
        + (1 if record["profile_url"] else 0)
        + (1 if record["phone"] or record["email"] else 0)
        + (1 if record["stand"] or record["hall"] else 0)
        + (2 if LEGAL_MARKER_RE.search(fold(record["company"])) else 0)
    )


def _site_chrome(soup: BeautifulSoup) -> set[int]:
    """Ids of elements inside page headers, menus, footers and pop-up templates."""
    roots = list(soup.find_all(["header", "nav", "footer", "dialog", "template"]))
    for element in soup.find_all(True):
        tokens = [*element.get("class", []), str(element.get("id", ""))]
        if any(CHROME_TOKEN_RE.search(token.casefold()) for token in tokens if token):
            roots.append(element)
    chrome: set[int] = set()
    for root in roots:
        chrome.add(id(root))
        chrome.update(id(node) for node in root.find_all(True))
    return chrome


def card_records(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Records from the best group of repeated elements with the same classes."""
    chrome = _site_chrome(soup)
    elements = [
        element for element in soup.find_all(True)
        if element.name not in {"script", "style", "option", "svg", "path", "head", "meta", "link"}
        and id(element) not in chrome
    ]
    token_counts = Counter(token for element in elements for token in element.get("class", []))
    groups: dict[tuple, list] = {}
    for element in elements:
        classes = " ".join(sorted(token for token in element.get("class", []) if token_counts[token] > 1))
        if classes or element.name in {"li", "tr", "article"}:
            groups.setdefault((element.name, classes), []).append(element)
    best: list[dict] = []
    best_score = 0
    for members in groups.values():
        if len(members) < MIN_RECORDS:
            continue
        records = [
            record for record in (_card_record(card, page_url) for card in members)
            if record["company"] and _record_score(record)
        ]
        if len(records) < MIN_RECORDS:
            continue
        score = sum(_record_score(record) for record in records)
        if score > best_score:
            best, best_score = records, score
    return best


def parse_records(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    from_json = json_records(soup)
    from_cards = card_records(soup, page_url)
    if not from_json:
        return from_cards
    if not from_cards:
        return from_json
    json_score = sum(_record_score(record) for record in from_json)
    card_score = sum(_record_score(record) for record in from_cards)
    return from_json if json_score >= card_score else from_cards


def page_urls(html: str, page_url: str) -> list[str]:
    """Other pages of a numbered list on the same path, in order."""
    soup = BeautifulSoup(html, "html.parser")
    base = urlsplit(page_url)
    base_query = dict(parse_qsl(base.query))
    seen: dict[int, str] = {}
    for link in soup.find_all("a", href=True):
        parts = urlsplit(urljoin(page_url, str(link.get("href", ""))))
        if parts.netloc != base.netloc or parts.path.rstrip("/") != base.path.rstrip("/"):
            continue
        query = dict(parse_qsl(parts.query))
        for key in PAGE_PARAMETERS:
            value = query.get(key, "")
            if not value.isdigit():
                continue
            rest = {name: item for name, item in query.items() if name != key}
            if rest == {name: item for name, item in base_query.items() if name != key}:
                seen[int(value)] = key
    if not seen:
        return []
    key = Counter(seen.values()).most_common(1)[0][0]
    current = int(base_query[key]) if str(base_query.get(key, "")).isdigit() else 1
    urls = []
    for number in range(1, min(max(seen), MAX_PAGES) + 1):
        if number != current:
            query = urlencode({**base_query, key: str(number)})
            urls.append(urlunsplit((base.scheme, base.netloc, base.path, query, "")))
    return urls


def enrich_from_profiles(records: list[dict], fetch_html, progress=None) -> int:
    """Fill missing website, phone and email from exhibitor profile pages.

    Links, phones and emails repeated on many profile pages belong to the fair
    site itself and are ignored.  Returns the number of pages read.
    """
    targets = [record for record in records if record["profile_url"] and not record["website"]]
    if len(targets) < MIN_RECORDS or len(targets) * 10 < len(records) * 3:
        return 0
    pages = []
    targets = targets[:MAX_PROFILES]
    for number, record in enumerate(targets, start=1):
        if progress:
            progress(f"Firma sayfası {number}/{len(targets)}")
        try:
            html = fetch_html(record["profile_url"])
        except requests.RequestException:
            continue
        soup = BeautifulSoup(html, "html.parser")
        links = list(dict.fromkeys(
            website for link in soup.find_all("a", href=True)
            if (website := external_website(link.get("href", ""), record["profile_url"]))
        ))
        pages.append((record, links, extractor.extract_phones(html), extractor.extract_emails(html)))
    hosts = Counter(host for _, links, _, _ in pages for host in {scorer.registrable_domain(link) for link in links})
    phones = Counter(value for _, _, values, _ in pages for value in set(values))
    emails = Counter(value for _, _, _, values in pages for value in set(values))
    limit = max(3, len(pages) * 3 // 10)
    for record, links, page_phones, page_emails in pages:
        record["website"] = next((link for link in links if hosts[scorer.registrable_domain(link)] < limit), "")
        record["phone"] = record["phone"] or next((value for value in page_phones if phones[value] < limit), "")
        record["email"] = record["email"] or next((value for value in page_emails if emails[value] < limit), "")
    return len(pages)


def dedupe(records: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for record in records:
        key = fold(record["company"])
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(record)
            continue
        for field in RECORD_FIELDS:
            merged[key][field] = merged[key][field] or record[field]
    return list(merged.values())


def make_fetcher(delay: float = REQUEST_DELAY_SEC):
    session = network_guard.harden_session(requests.Session())
    session.headers["User-Agent"] = USER_AGENT
    state = {"last": 0.0}

    def fetch_html(url: str) -> str:
        wait = state["last"] + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SEC)
        finally:
            state["last"] = time.monotonic()
        response.raise_for_status()
        response.encoding = response.encoding or response.apparent_encoding
        return response.text

    return fetch_html


def extract(*, url: str = "", html_files: list[Path] | None = None, fetch_html=None, progress=None) -> dict:
    """Return {"records": [...], "pages": n, "profiles": n} for a URL or saved pages.

    With saved files nothing is fetched; ``url`` is then only the page address,
    used to tell the fair's own links from exhibitor websites.
    """
    records: list[dict] = []
    pages = 0
    if html_files:
        for path in html_files:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            if Path(path).suffix.casefold() == ".json":
                records.extend(records_from_texts([text]))
            else:
                records.extend(parse_records(text, url or "https://saved.invalid/"))
            pages += 1
        return {"records": dedupe(records), "pages": pages, "profiles": 0}
    fetch_html = fetch_html or make_fetcher()
    if progress:
        progress("Liste sayfası 1")
    first = fetch_html(url)
    records.extend(parse_records(first, url))
    pages = 1
    if len(records) < MIN_RECORDS:
        for data_url in data_urls(first, url):
            if progress:
                progress("Katalog veri dosyası")
            try:
                found = records_from_texts([fetch_html(data_url)])
            except requests.RequestException:
                continue
            if len(found) > len(records):
                records = found
        if len(records) >= MIN_RECORDS:
            records = dedupe(records)
            return {"records": records, "pages": pages, "profiles": 0}
    others = page_urls(first, url)
    for number, other in enumerate(others, start=2):
        if progress:
            progress(f"Liste sayfası {number}/{len(others) + 1}")
        try:
            records.extend(parse_records(fetch_html(other), other))
        except requests.RequestException:
            continue
        pages += 1
    records = dedupe(records)
    profiles = enrich_from_profiles(records, fetch_html, progress)
    return {"records": records, "pages": pages, "profiles": profiles}


def build_input(url: str, files: list[Path] | None, name: str, output: Path, progress=None) -> dict:
    """Extract a list and write it as a run input; returns the counts."""
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    result = extract(url=url, html_files=files, progress=progress)
    if not result["records"]:
        raise ValueError("Listede firma bulunamadı.")
    write_input(result["records"], output, source=f"{name} katılımcı listesi", listing_url=url)
    return {**summary(result["records"]), "pages": result["pages"], "profiles": result["profiles"]}


def summary(records: list[dict]) -> dict:
    return {
        "firms": len(records),
        "website": sum(1 for record in records if record["website"]),
        "phone": sum(1 for record in records if record["phone"]),
        "email": sum(1 for record in records if record["email"]),
    }


def _slug(value: str) -> str:
    return fold(value).replace(" ", "-")[:80]


def write_input(records: list[dict], path: Path, *, source: str, listing_url: str) -> None:
    """Write the pipeline input workbook; refuses to overwrite."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    book = Workbook()
    sheet = book.active
    sheet.title = "Katilimcilar"
    sheet.append([
        "company", "listed_website", "listed_phone", "listed_email", "country", "tax_id",
        "hall", "stand", "profile_url", "listing_url", "source", "source_record_id",
    ])
    used: Counter = Counter()
    for record in records:
        key = f"{_slug(source)}:{_slug(record['company'])}"
        used[key] += 1
        if used[key] > 1:
            key = f"{key}-{used[key]}"
        sheet.append([
            record["company"], record["website"], record["phone"], record["email"],
            record["country"], record["tax_id"], record["hall"], record["stand"], record["profile_url"],
            listing_url, source, key,
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
