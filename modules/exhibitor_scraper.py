import re
import time
import unicodedata
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import config


HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept-Language": "tr,en;q=0.8",
}


def _clean(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", unescape(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", _clean(value)).casefold()
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _absolute_url(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


def _normalize_website(value: str) -> str:
    value = _clean(value)
    if not value:
        return ""
    if value.startswith("//"):
        return f"https:{value}"
    if "://" not in value:
        return f"https://{value}"
    return value


def _get(session: requests.Session, url: str) -> str:
    last_error: requests.RequestException | None = None
    for attempt in range(config.MAX_RETRIES + 2):
        try:
            response = session.get(url, headers=HEADERS, timeout=max(config.REQUEST_TIMEOUT_SEC, 30))
            response.raise_for_status()
            response.encoding = response.encoding or response.apparent_encoding
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= config.MAX_RETRIES + 1:
                break
            time.sleep((attempt + 1) * config.RETRY_BACKOFF_BASE_SEC)
    raise last_error  # type: ignore[misc]


def _first_external_website(html: str, base_domain: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    blocked = {
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "youtube.com",
        "twitter.com",
        "x.com",
        "google.com",
        "apps.apple.com",
        "play.google.com",
        "wa.me",
    }
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if not href.startswith(("http://", "https://", "//")):
            continue
        website = _normalize_website(href)
        if base_domain in website:
            continue
        if any(domain in website for domain in blocked):
            continue
        return website
    return ""


def _meta_description(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return _clean(meta["content"])
    return ""


def _idos_detail_description(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(string=re.compile(r"About Company", re.I))
    if not heading:
        return ""
    container = heading.find_parent()
    if not container:
        return ""
    next_block = container.find_next("div", class_="text-muted")
    return _clean(next_block.get_text(" ", strip=True)) if next_block else ""


def _beauty_label_value(html: str, label_text: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for label in soup.find_all("b"):
        if label_text.lower() not in _clean(label.get_text(" ", strip=True)).lower():
            continue
        values = []
        for sibling in label.next_siblings:
            if getattr(sibling, "name", None) == "hr":
                break
            text = _clean(sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling))
            if text:
                values.append(text)
        return _clean(" ".join(values))
    return ""


def scrape_ifco(fetch_details: bool = False, delay_sec: float = 0.4) -> list[dict]:
    base_url = "https://www.ifco.com.tr"
    list_url = f"{base_url}/tr/fuar/katilimcilar"
    session = requests.Session()
    rows_by_profile: dict[str, dict] = {}
    page = 1

    while True:
        url = list_url if page == 1 else f"{list_url}?page={page}"
        html = _get(session, url)
        soup = BeautifulSoup(html, "html.parser")
        links = soup.select('a[href*="/tr/fuar/exhibitors/"]')
        page_rows = 0

        for link in links:
            href = link.get("href", "")
            if any(part in href for part in ("/detail", "/showroom", "contact-form")):
                continue
            image = link.find("img", alt=True)
            company = _clean(image["alt"] if image else link.get_text(" ", strip=True))
            if not company:
                continue
            profile_url = _absolute_url(base_url, href)
            if profile_url in rows_by_profile:
                continue
            website = ""
            description = ""
            if fetch_details:
                try:
                    detail_html = _get(session, profile_url)
                    website = _first_external_website(detail_html, "ifco.com.tr")
                    description = _meta_description(detail_html)
                    time.sleep(delay_sec)
                except requests.RequestException:
                    website = ""
            rows_by_profile[profile_url] = {
                "company": company,
                "website": website,
                "source": "ifco",
                "country": "",
                "profile_url": profile_url,
                "sector": "tekstil giyim moda hazir giyim",
                "description": description,
            }
            page_rows += 1

        next_page = soup.select_one(f'a[href*="page={page + 1}"]')
        if not next_page or page_rows == 0:
            break
        page += 1
        time.sleep(delay_sec)

    return list(rows_by_profile.values())


def scrape_idos(fetch_details: bool = False, delay_sec: float = 0.4) -> list[dict]:
    base_url = "https://crm.idos.events"
    catalogue_url = f"{base_url}/portal/catalogue/75"
    session = requests.Session()
    rows_by_profile: dict[str, dict] = {}
    page_numbers: list[int] | None = None

    while True:
        if page_numbers is None:
            page = 0
        elif not page_numbers:
            break
        else:
            page = page_numbers.pop(0)
        url = f"{catalogue_url}?keyword=&ulkeId=1&grup_id=&page={page}"
        try:
            html = _get(session, url)
        except requests.RequestException:
            if page_numbers:
                continue
            raise
        soup = BeautifulSoup(html, "html.parser")
        if page_numbers is None:
            discovered_pages = []
            for link in soup.select('a[href*="page="]'):
                match = re.search(r"[?&]page=(\d+)", link.get("href", ""))
                if match:
                    discovered_pages.append(int(match.group(1)))
            max_page = max(discovered_pages, default=0)
            page_numbers = list(range(2, max_page + 1))
        cards = soup.select(".catalogue-card")
        page_rows = 0

        for card in cards:
            country = _clean(card.select_one(".catalogue-country").get_text(" ", strip=True) if card.select_one(".catalogue-country") else "")
            if country and "türkiye" not in country.lower() and "turkiye" not in country.lower():
                continue
            name_el = card.select_one(".exhibitor-name")
            sector_el = card.select_one(".catalogue-sectors")
            link_el = card.select_one('a[href*="/portal/catalogue/75/"]')
            company = _clean(name_el.get_text(" ", strip=True) if name_el else "")
            profile_url = _absolute_url(base_url, link_el["href"]) if link_el else ""
            if not company or not profile_url or profile_url in rows_by_profile:
                continue
            website = ""
            description = ""
            if fetch_details:
                try:
                    detail_html = _get(session, profile_url)
                    website = _first_external_website(detail_html, "crm.idos.events")
                    description = _idos_detail_description(detail_html)
                    time.sleep(delay_sec)
                except requests.RequestException:
                    website = ""
            rows_by_profile[profile_url] = {
                "company": company,
                "website": website,
                "source": "idos_f_istanbul",
                "country": country or "Türkiye",
                "profile_url": profile_url,
                "sector": _clean(sector_el.get_text(" ", strip=True) if sector_el else "gida icecek makine ambalaj"),
                "description": description,
            }
            page_rows += 1

        if page_rows == 0 and not page_numbers:
            break
        time.sleep(delay_sec)

    return list(rows_by_profile.values())


def _beauty_datatable_payload(start: int, length: int) -> dict:
    payload = {
        "draw": "1",
        "start": str(start),
        "length": str(length),
        "search[value]": "",
        "search[regex]": "false",
        "order[0][column]": "1",
        "order[0][dir]": "asc",
    }
    for index in range(7):
        payload[f"columns[{index}][data]"] = str(index)
        payload[f"columns[{index}][name]"] = ""
        payload[f"columns[{index}][searchable]"] = "true"
        payload[f"columns[{index}][orderable]"] = "true"
        payload[f"columns[{index}][search][value]"] = ""
        payload[f"columns[{index}][search][regex]"] = "false"
    return payload


def _beauty_cell_text(cell: str) -> str:
    return _clean(BeautifulSoup(cell or "", "html.parser").get_text(" ", strip=True))


def _beauty_profile_url(row: list) -> str:
    for cell in row:
        soup = BeautifulSoup(cell or "", "html.parser")
        link = soup.find("a", href=True)
        if link:
            return _absolute_url("https://beautyeurasia.com", link["href"])
    return ""


def _beauty_detail_website(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    labels = soup.find_all("b")
    for label in labels:
        if "websitesi" not in _clean(label.get_text(" ", strip=True)).lower():
            continue
        link = label.find_next("a", href=True)
        if link:
            return _normalize_website(link["href"])
    return ""


def scrape_beauty_eurasia(fetch_details: bool = True, delay_sec: float = 0.4) -> list[dict]:
    endpoint = "https://beautyeurasia.com/ERAForms/companies_list.php?l=tr&exhibition=24&y=2026"
    session = requests.Session()
    headers = {
        **HEADERS,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://beautyeurasia.com/tr/2026-katilimci-listesi",
    }
    rows: list[dict] = []
    start = 0
    length = 100

    while True:
        response = session.post(endpoint, data=_beauty_datatable_payload(start, length), headers=headers, timeout=config.REQUEST_TIMEOUT_SEC)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])
        if not data:
            break
        for row in data:
            company = _beauty_cell_text(row[1] if len(row) > 1 else "")
            country = _beauty_cell_text(row[2] if len(row) > 2 else "")
            if "türkiye" not in country.lower() and "turkiye" not in country.lower():
                continue
            profile_url = _beauty_profile_url(row)
            website = ""
            sector = ""
            description = ""
            if fetch_details and profile_url:
                try:
                    detail_html = _get(session, profile_url)
                    website = _beauty_detail_website(detail_html)
                    sector = _beauty_label_value(detail_html, "Ürün Grupları") or _beauty_label_value(detail_html, "Urun Gruplari")
                    description = _meta_description(detail_html)
                    time.sleep(delay_sec)
                except (requests.RequestException, ValueError):
                    website = ""
            rows.append(
                {
                    "company": company.replace(" Yeni katılımcı", "").strip(),
                    "website": website,
                    "source": "beauty_eurasia",
                    "country": country,
                    "profile_url": profile_url,
                    "sector": sector,
                    "description": description,
                }
            )
        start += length
        if start >= int(payload.get("recordsTotal", start)):
            break
        time.sleep(delay_sec)
    return rows


def _maktek_widget(soup: BeautifulSoup, title: str):
    wanted = _fold(title)
    for heading in soup.select("h4.widget-title"):
        if _fold(heading.get_text(" ", strip=True)) == wanted:
            return heading.find_parent(class_="widget")
    return None


def _cloudflare_email(encoded: str) -> str:
    try:
        key = int(encoded[:2], 16)
        return "".join(
            chr(int(encoded[index:index + 2], 16) ^ key)
            for index in range(2, len(encoded), 2)
        )
    except (TypeError, ValueError):
        return ""


def _maktek_profile_details(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    details = {
        "company": "",
        "website": "",
        "listed_phone": "",
        "listed_email": "",
        "listed_address": "",
        "hall": "",
        "stand": "",
        "brands": "",
        "representations": "",
        "description": "",
    }
    heading = soup.select_one("main h1")
    description = soup.select_one("main .schedule-detail-info p.mb-20")
    details["company"] = _clean(heading.get_text(" ", strip=True) if heading else "")
    details["description"] = _clean(description.get_text(" ", strip=True) if description else "")

    location = _maktek_widget(soup, "Konum Bilgisi")
    if location:
        location_text = _clean(location.get_text(" ", strip=True))
        hall_match = re.search(r"Salon\s*:\s*([^:]+?)(?=\s+Stant\s*:|$)", location_text, re.I)
        stand_match = re.search(r"Stant\s*:\s*(.+)$", location_text, re.I)
        details["hall"] = _clean(hall_match.group(1) if hall_match else "")
        details["stand"] = _clean(stand_match.group(1) if stand_match else "")

    brands = _maktek_widget(soup, "Markalar")
    if brands:
        details["brands"] = "; ".join(dict.fromkeys(
            _clean(item.get_text(" ", strip=True))
            for item in brands.select("li")
            if _clean(item.get_text(" ", strip=True))
        ))

    representations = _maktek_widget(soup, "Temsilcilikler")
    if representations:
        values = [
            _clean(item.get_text(" ", strip=True))
            for item in representations.select("h6")
            if _clean(item.get_text(" ", strip=True))
        ]
        details["representations"] = "; ".join(dict.fromkeys(values))

    contact = _maktek_widget(soup, "İletişim")
    if contact:
        for item in contact.select(".schedule-list > ul > li"):
            text = _clean(item.get_text(" ", strip=True))
            icon = item.find("i")
            classes = set(icon.get("class", [])) if icon else set()
            if any("phone" in name for name in classes):
                details["listed_phone"] = text
            elif any("location" in name for name in classes):
                details["listed_address"] = text
            elif any("globe" in name for name in classes):
                link = item.find("a", href=True)
                if link:
                    details["website"] = _normalize_website(link["href"])
            elif any("envelope" in name for name in classes):
                link = item.find("a", href=True)
                if link and link["href"].startswith("mailto:"):
                    details["listed_email"] = _clean(link["href"][7:].split("?", 1)[0])
                else:
                    encoded = item.select_one("[data-cfemail]")
                    if encoded:
                        details["listed_email"] = _cloudflare_email(encoded.get("data-cfemail", ""))
    return details


def _maktek_list_rows(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    for card in soup.select('a.brand-link[href^="brand/"], a.brand-link[href*="/brand/"]'):
        company_el = card.select_one(".brand-name")
        company = _clean(company_el.get_text(" ", strip=True) if company_el else "")
        href = card.get("href", "")
        if not company or not href:
            continue
        country_el = card.select_one(".brand-country")
        location_texts = [
            _clean(item.get_text(" ", strip=True))
            for item in card.select(".brand-location-info .location-item")
        ]
        hall = ""
        stand = ""
        for text in location_texts:
            if _fold(text).startswith("salon:"):
                hall = _clean(text.split(":", 1)[1])
            elif _fold(text).startswith("stant:"):
                stand = _clean(text.split(":", 1)[1])
        rows.append({
            "company": company,
            "website": "",
            "source": "maktek_avrasya_2026",
            "country": _clean(country_el.get_text(" ", strip=True) if country_el else "Türkiye"),
            "profile_url": _absolute_url(base_url, href),
            "sector": "makine, takım tezgahları, metal işleme ve üretim teknolojileri",
            "description": "",
            "listed_phone": "",
            "listed_email": "",
            "listed_address": "",
            "hall": hall,
            "stand": stand,
            "brands": "",
            "representations": "",
        })
    return rows


def scrape_maktek(fetch_details: bool = True, delay_sec: float = 0.2) -> list[dict]:
    base_url = "https://www.maktekfuari.com"
    list_url = f"{base_url}/katilimci-listesi?country=T%C3%9CRK%C4%B0YE"
    session = requests.Session()
    rows_by_profile: dict[str, dict] = {}
    page = 1
    max_page = 1

    while page <= max_page:
        url = list_url if page == 1 else f"{list_url}&page={page}"
        html = _get(session, url)
        soup = BeautifulSoup(html, "html.parser")
        if page == 1:
            page_numbers = []
            for link in soup.select('a[href*="page="]'):
                match = re.search(r"[?&]page=(\d+)", link.get("href", ""))
                if match:
                    page_numbers.append(int(match.group(1)))
            max_page = max(page_numbers, default=1)

        for row in _maktek_list_rows(html, base_url):
            profile_url = row["profile_url"]
            if profile_url in rows_by_profile:
                continue
            if fetch_details:
                try:
                    detail_html = _get(session, profile_url)
                    details = _maktek_profile_details(detail_html)
                    for field, value in details.items():
                        if value:
                            row[field] = value
                    time.sleep(delay_sec)
                except requests.RequestException:
                    pass
            rows_by_profile[profile_url] = row
        page += 1
        if page <= max_page:
            time.sleep(delay_sec)
    return list(rows_by_profile.values())


def dedupe_rows(rows: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for row in rows:
        key = _clean(row.get("company", "")).casefold()
        if not key:
            continue
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = row
            continue
        if not existing.get("website") and row.get("website"):
            existing["website"] = row["website"]
        if not existing.get("sector") and row.get("sector"):
            existing["sector"] = row["sector"]
        if not existing.get("description") and row.get("description"):
            existing["description"] = row["description"]
        if row.get("source") and row["source"] not in existing.get("source", ""):
            existing["source"] = f"{existing.get('source', '')};{row['source']}".strip(";")
    return list(deduped.values())
