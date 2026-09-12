import json
import hashlib
import re
import time
import unicodedata
from datetime import datetime, timezone
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes

import config
from modules import network_guard, phone, run_context, scorer


HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept-Language": "tr,en;q=0.8",
}


def _session() -> requests.Session:
    return network_guard.harden_session(requests.Session())


def _clean(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", unescape(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", _clean(value)).casefold()
    folded = "".join(char for char in normalized if not unicodedata.combining(char))
    return folded.replace("ı", "i")


def _catalog_host(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.casefold().removeprefix("www.")


def _absolute_url(base_url: str, href: str) -> str:
    resolved = urljoin(base_url, href)
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not _catalog_host(base_url) or _catalog_host(base_url) != _catalog_host(resolved):
        return ""
    return resolved


def _normalize_website(value: str) -> str:
    value = str(value or "")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return ""
    value = _clean(value)
    if not value:
        return ""
    if value.startswith("//"):
        value = f"https:{value}"
    elif "://" not in value:
        value = f"https://{value}"
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname or ""
        if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
            return ""
        hostname_ascii = hostname.encode("idna").decode("ascii")
        labels = hostname_ascii.rstrip(".").split(".")
        if not labels or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            or not re.fullmatch(r"[A-Za-z0-9-]+", label)
            for label in labels
        ):
            return ""
        if len(hostname_ascii.rstrip(".")) > 253:
            return ""
        # Accessing port rejects malformed port syntax and keeps the
        # normalized result fail-closed for values copied from catalogues.
        parsed.port
    except (UnicodeError, ValueError):
        return ""
    return value


def _bounded_body(response: requests.Response) -> bytes:
    content_length = response.headers.get("content-length", "")
    try:
        declared_size = int(content_length) if content_length else 0
    except ValueError:
        declared_size = 0
    if declared_size > config.MAX_HTTP_RESPONSE_BYTES:
        raise requests.RequestException("response_too_large")
    chunks: list[bytes] = []
    received = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        received += len(chunk)
        if received > config.MAX_HTTP_RESPONSE_BYTES:
            raise requests.RequestException("response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_body(response: requests.Response, body: bytes) -> str:
    encoding = response.encoding or requests.utils.get_encoding_from_headers(
        response.headers
    )
    if not isinstance(encoding, str) or not encoding:
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            matches = list(from_bytes(body))
            turkish = next(
                (match for match in matches if match.encoding.casefold() in {
                    "cp1254", "windows-1254", "iso8859_9", "iso-8859-9",
                }),
                None,
            )
            detected = turkish or (matches[0] if matches else None)
            encoding = detected.encoding if detected is not None else "windows-1254"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _request_bounded(
    session: requests.Session,
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    data: dict | None = None,
    json_body: dict | None = None,
    timeout: int,
) -> tuple[bytes, str]:
    current = url
    current_method = method.upper()
    current_data = data
    origin_host = _catalog_host(url)
    for redirect_count in range(config.MAX_HTTP_REDIRECTS + 1):
        allowed, reason = network_guard.validate_public_http_url(current)
        if not allowed:
            raise requests.exceptions.InvalidURL(f"blocked_network_target:{reason}")
        request = session.post if current_method == "POST" else session.get
        kwargs = {
            "headers": headers,
            "timeout": timeout,
            "allow_redirects": False,
            "stream": True,
        }
        if current_method == "POST":
            if json_body is not None:
                kwargs["json"] = json_body
            else:
                kwargs["data"] = current_data
        response = request(current, **kwargs)
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location", "").strip()
                if not location:
                    raise requests.exceptions.InvalidURL("redirect_without_location")
                target = urljoin(current, location)
                if origin_host != _catalog_host(target):
                    raise requests.exceptions.InvalidURL(
                        f"cross_domain_redirect:{target}"
                    )
                if redirect_count >= config.MAX_HTTP_REDIRECTS:
                    raise requests.exceptions.TooManyRedirects(
                        f"redirect_limit:{url}"
                    )
                if response.status_code == 303 or (
                    response.status_code in {301, 302} and current_method == "POST"
                ):
                    current_method = "GET"
                    current_data = None
                current = target
                continue
            response.raise_for_status()
            body = _bounded_body(response)
            return body, _decode_body(response, body)
        finally:
            response.close()
    raise requests.exceptions.TooManyRedirects(f"redirect_limit:{url}")


def _get(session: requests.Session, url: str) -> str:
    last_error: requests.RequestException | None = None
    for attempt in range(config.MAX_RETRIES + 2):
        try:
            return _request_bounded(
                session,
                url,
                method="GET",
                headers=HEADERS,
                timeout=max(config.REQUEST_TIMEOUT_SEC, 30),
            )[1]
        except requests.RequestException as exc:
            last_error = exc
            if isinstance(
                exc,
                (requests.exceptions.InvalidURL, requests.exceptions.TooManyRedirects),
            ):
                raise
            if attempt >= config.MAX_RETRIES + 1:
                break
            time.sleep((attempt + 1) * config.RETRY_BACKOFF_BASE_SEC)
    raise last_error  # type: ignore[misc]


def _post_json(
    session: requests.Session,
    url: str,
    *,
    data: dict,
    headers: dict[str, str],
) -> dict:
    _body, text = _request_bounded(
        session,
        url,
        method="POST",
        headers=headers,
        data=data,
        timeout=config.REQUEST_TIMEOUT_SEC,
    )
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("invalid_json_object")
    return payload


def _post_graphql(session: requests.Session, url: str, payload: dict) -> dict:
    _body, text = _request_bounded(
        session,
        url,
        method="POST",
        headers={**HEADERS, "Content-Type": "application/json"},
        json_body=payload,
        timeout=max(config.REQUEST_TIMEOUT_SEC, 30),
    )
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("invalid_graphql_response")
    if result.get("errors"):
        raise ValueError(f"graphql_error:{result['errors'][0]}")
    return result


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


def _idos_list_rows(
    html: str,
    listing_url: str = "https://crm.idos.events/portal/catalogue/75",
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    cards = soup.select("article.cv2-card, .catalogue-card")
    for card in cards:
        name_node = card.select_one(".cv2-card-name") or card.select_one(".exhibitor-name")
        country_node = card.select_one(".cv2-country span") or card.select_one(".catalogue-country")
        sector_node = card.select_one(".cv2-card-sectors") or card.select_one(".catalogue-sectors")
        link_node = card.select_one("a.stretched-link[href]") or card.select_one(
            'a[href*="/portal/catalogue/75/"]'
        )
        company = _clean(name_node.get_text(" ", strip=True) if name_node else "")
        country = _clean(country_node.get_text(" ", strip=True) if country_node else "")
        if country and "türkiye" not in country.casefold() and "turkiye" not in country.casefold():
            continue
        profile_url = _absolute_url(
            "https://crm.idos.events", link_node.get("href", "") if link_node else ""
        )
        if not company:
            continue
        location_node = card.select_one(".cv2-stand")
        if location_node:
            location = _clean(location_node.get_text(" ", strip=True))
        else:
            legacy_location = BeautifulSoup(str(card), "html.parser")
            for node in legacy_location.select(
                ".exhibitor-name, .catalogue-country, .catalogue-sectors, a[href]"
            ):
                node.decompose()
            location = _clean(legacy_location.get_text(" ", strip=True))
        hall_match = re.search(
            r"(?:Hall\s*/\s*Salon|Hall|Salon)\s*[:\-]\s*(.+?)(?=\s+(?:Stand\s*/\s*Booth|Stand|Booth)\s*[:\-]|$)",
            location,
            re.I,
        )
        stand_match = re.search(
            r"(?:Stand\s*/\s*Booth|Stand|Booth)\s*[:\-]\s*(.+)$",
            location,
            re.I,
        )
        rows.append({
            "company": company,
            "website": "",
            "listed_website": "",
            "source": "idos_f_istanbul",
            "country": country or "Türkiye",
            "profile_url": profile_url,
            "listing_url": listing_url,
            "sector": _clean(sector_node.get_text(" ", strip=True) if sector_node else "gida icecek makine ambalaj"),
            "description": "",
            "listed_phone": "",
            "listed_address": "",
            "listed_phone_status": "NOT_REQUESTED",
            "listed_address_status": "NOT_REQUESTED",
            "source_detail_status": "NOT_REQUESTED",
            "source_evidence": "[]",
            "hall": _clean(hall_match.group(1)) if hall_match else "",
            "stand": _clean(stand_match.group(1)) if stand_match else "",
        })
    return dedupe_rows(rows)


def _idos_detail_scope(html: str):
    soup = BeautifulSoup(html, "html.parser")
    scope = soup.select_one(".cv2-detail")
    if scope is None:
        candidates = soup.select("main, article, [class*='participant'], [class*='profile'], [class*='detail']")
        scope = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=soup)
    for node in scope.select("footer, header, nav, aside, script, style, noscript"):
        node.decompose()
    return soup, scope


def _idos_profile_details(html: str, profile_url: str) -> dict:
    soup, scope = _idos_detail_scope(html)
    heading = scope.select_one(".cv2-detail-side h2") or scope.select_one("h1, h2")
    company = _clean(heading.get_text(" ", strip=True) if heading else "")
    website = ""
    for link in scope.select(".cv2-detail-side .cv2-side-actions a[href]"):
        href = str(link.get("href", ""))
        candidate = _normalize_website(href)
        if candidate and _catalog_host(candidate) != _catalog_host(profile_url):
            website = candidate
            break
    description_node = scope.select_one(".cv2-prose")
    address, _ = _detail_value(scope, ("firma adresi", "company address", "address", "adres"))
    phone_value, phone_links = _detail_value(
        scope, ("phone", "telephone", "telefon", "tel", "gsm", "mobil")
    )
    return {
        "company": company,
        "website": website,
        "description": _clean(description_node.get_text(" ", strip=True) if description_node else ""),
        "listed_phone": _texhibition_labelled_phone(phone_value, phone_links),
        "listed_address": address,
        "source_detail_url": profile_url,
        "source_detail_content_sha256": hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest(),
    }


def _beauty_labelled_value(scope, labels: tuple[str, ...]) -> tuple[str, list[str]]:
    wanted = {_fold(label).rstrip(":") for label in labels}
    for label in scope.find_all(["b", "strong", "label", "dt", "th"]):
        label_text = _fold(label.get_text(" ", strip=True)).rstrip(":")
        if label_text not in wanted:
            continue
        container = label.parent
        if container and container.name == "tr":
            cells = container.find_all(["th", "td"], recursive=False)
            if len(cells) > 1:
                value_node = cells[-1]
                return _clean(value_node.get_text(" ", strip=True)), [
                    str(link.get("href", "")) for link in value_node.find_all("a", href=True)
                ]
        if label.name == "dt" and label.find_next_sibling("dd"):
            value_node = label.find_next_sibling("dd")
            return _clean(value_node.get_text(" ", strip=True)), [
                str(link.get("href", "")) for link in value_node.find_all("a", href=True)
            ]
        values: list[str] = []
        links: list[str] = []
        for sibling in label.next_siblings:
            if getattr(sibling, "name", None) == "hr":
                break
            if getattr(sibling, "name", None) in {"b", "strong", "label", "dt", "th"}:
                break
            if hasattr(sibling, "find_all"):
                if getattr(sibling, "name", None) == "a" and sibling.get("href"):
                    links.append(str(sibling.get("href")))
                links.extend(str(link.get("href", "")) for link in sibling.find_all("a", href=True))
                text = _clean(sibling.get_text(" ", strip=True))
            else:
                text = _clean(str(sibling))
            if text:
                values.append(text)
        return _clean(" ".join(values)), links
    return "", []


def _beauty_label_value(html: str, label_text: str) -> str:
    scope = BeautifulSoup(html, "html.parser") if isinstance(html, str) else html
    return _beauty_labelled_value(scope, (label_text,))[0]


def _beauty_detail_scope(html: str):
    soup = BeautifulSoup(html, "html.parser")
    scope = soup.select_one("main")
    if scope is None:
        candidates = soup.select("article, [class*='participant'], [class*='profile'], [class*='detail']")
        scope = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=soup)
    for node in scope.select("footer, header, nav, aside, script, style, noscript"):
        node.decompose()
    return soup, scope


def _beauty_profile_details(html: str, profile_url: str) -> dict:
    soup, scope = _beauty_detail_scope(html)
    heading = scope.select_one("h1")
    company = _clean(heading.get_text(" ", strip=True) if heading else "")
    address, _ = _beauty_labelled_value(
        scope, ("Firma Adresi", "Adres", "Address", "Company Address")
    )
    phone_value, phone_links = _beauty_labelled_value(
        scope, ("Telefon", "Phone", "Telephone", "Tel", "GSM", "Mobil")
    )
    website_value, website_links = _beauty_labelled_value(
        scope, ("Firma Websitesi", "Website", "Web Sitesi")
    )
    website = ""
    for candidate in [*website_links, website_value]:
        website = _normalize_website(candidate)
        if website:
            break
    normalized_phone = _texhibition_labelled_phone(phone_value, phone_links)
    sector = _beauty_label_value(scope, "Ürün Grupları") or _beauty_label_value(scope, "Urun Gruplari")
    return {
        "company": company,
        "website": website,
        "sector": sector,
        "description": _meta_description(str(soup)),
        "listed_phone": normalized_phone,
        "listed_address": address,
        "source_detail_url": profile_url,
        "source_detail_content_sha256": hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest(),
    }


def _metalexpo_list_rows(
    html: str,
    list_url: str = "https://www.metalexpo.com.tr/katilimci-listesi-2026",
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    blocked_hosts = (
        "metalexpo.com.tr", "linkedin.com", "instagram.com", "facebook.com",
        "youtube.com", "twitter.com", "x.com", "wa.me",
    )
    for block in soup.select(".katilimci-text"):
        fields = [_clean(item.get_text(" ", strip=True)) for item in block.select(".text")]
        if not fields or not fields[0]:
            continue
        company = fields[0]
        location = fields[1] if len(fields) > 1 else ""
        link = block.find_parent("a", href=True)
        listed_website = _normalize_website(link.get("href", "")) if link else ""
        if any(host in listed_website.casefold() for host in blocked_hosts):
            listed_website = ""

        hall_match = re.search(r"\bHALL\s*([0-9]+)\b", location, re.I)
        hall = hall_match.group(1) if hall_match else ""
        stand = re.sub(r"^\s*HALL\s*[0-9]+\s*/?\s*", "", location, flags=re.I)
        if not hall:
            stand_hall = re.search(r"\b([0-9]+)[A-Z]\s*[-/]", stand, re.I)
            hall = stand_hall.group(1) if stand_hall else ""

        rows.append({
            "company": company,
            "website": "",
            "listed_website": listed_website,
            "source": "metalexpo_2026",
            "country": "",
            "profile_url": "",
            "listing_url": list_url,
            "hall": hall,
            "stand": stand,
            "sector": "demir celik metalurji metal isleme",
            "description": "",
            "listed_phone": "",
            "listed_address": "",
            "listed_phone_status": "NOT_REQUESTED",
            "listed_address_status": "NOT_REQUESTED",
            "source_detail_status": "NOT_REQUESTED",
            "source_evidence": "[]",
        })
    return dedupe_rows(rows)


def scrape_metalexpo(fetch_details: bool = False, delay_sec: float = 0.4) -> list[dict]:
    list_url = "https://www.metalexpo.com.tr/katilimci-listesi-2026"
    session = _session()
    rows = _metalexpo_list_rows(_get(session, list_url), list_url)
    for row in rows:
        if fetch_details:
            _set_source_detail_state(row, "UNAVAILABLE_NO_PROFILE_URL")
        else:
            _set_source_detail_state(row, "NOT_REQUESTED")
    return rows


def _source_evidence_normalized(field: str, value: str) -> str:
    if field == "listed_phone":
        return phone.normalize_phone(value)
    return " ".join(scorer.normalize_text(value).split())


_SOURCE_DETAIL_FAILURE_STATUSES = {
    "NOT_REQUESTED",
    "UNAVAILABLE_NO_PROFILE_URL",
    "UNAVAILABLE_FETCH_ERROR",
    "UNAVAILABLE_PROFILE_IDENTITY_MISMATCH",
}


def _set_source_detail_state(row: dict, status: str, profile_url: str = "") -> None:
    if status not in _SOURCE_DETAIL_FAILURE_STATUSES:
        raise ValueError(f"invalid_source_detail_status:{status}")
    row.setdefault("listed_phone", "")
    row.setdefault("listed_address", "")
    row["listed_phone"] = ""
    row["listed_address"] = ""
    row["listed_phone_status"] = "NOT_REQUESTED" if status == "NOT_REQUESTED" else "UNAVAILABLE"
    row["listed_address_status"] = "NOT_REQUESTED" if status == "NOT_REQUESTED" else "UNAVAILABLE"
    row["source_detail_status"] = status
    if profile_url:
        row["source_detail_url"] = profile_url
    else:
        row.pop("source_detail_url", None)
    row["source_evidence"] = "[]"


def _record_profile_observation(row: dict, details: dict, html: str, profile_url: str) -> None:
    """Attach auditable field observations after profile identity has matched."""
    source_id, quality = run_context.source_record_identity(row, default_source="unknown")
    row["source_record_id"] = source_id
    row["source_record_id_quality"] = quality
    content_hash = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row.update({
        "source_detail_status": "COMPLETED",
        "source_detail_url": profile_url,
        "source_detail_content_sha256": content_hash,
    })
    normalized_phone = phone.normalize_phone(str(details.get("listed_phone", "") or ""))
    normalized_address = _clean(str(details.get("listed_address", "") or ""))
    row["listed_phone"] = normalized_phone
    row["listed_address"] = normalized_address
    claims = []
    for field in ("listed_phone", "listed_address"):
        value = row[field]
        status = "OBSERVED_PRESENT" if value else "OBSERVED_ABSENT"
        row[f"{field}_status"] = status
        claims.append({
            "source_record_id": source_id,
            "field": field,
            "value": value,
            "normalized_value": _source_evidence_normalized(field, value) if value else "",
            "observation": status,
            "url": profile_url,
            "content_sha256": content_hash,
            "observed_at": observed_at,
        })
    row["source_evidence"] = json.dumps(claims, ensure_ascii=False, sort_keys=True)


def _texhibition_list_rows(
    html: str,
    listing_url: str = "https://www.texhibitionist.com/katilimcilar?v=1",
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    for link in soup.select('a[href*="/exhibitors/"], a[href*="/katilimcilar/"]'):
        item = link.select_one(".item")
        title = item.select_one(".title") if item else None
        if not title:
            continue
        company = _clean(title.get_text(" ", strip=True))
        if not company:
            continue
        category = item.select_one(".category")
        location = _clean(item.select_one(".location").get_text(" ", strip=True)) if item.select_one(".location") else ""
        hall_match = re.search(r"\bHall\s+([^/]+)", location, re.I)
        stand_match = re.search(r"/\s*(.+)$", location)
        rows.append({
            "company": company,
            "website": "",
            "listed_website": "",
            "source": "texhibition_2026",
            "country": "",
            "profile_url": _absolute_url(listing_url, link.get("href", "")),
            "listing_url": listing_url,
            "source_detail_status": "NOT_REQUESTED",
            "listed_phone": "",
            "listed_address": "",
            "listed_phone_status": "NOT_REQUESTED",
            "listed_address_status": "NOT_REQUESTED",
            "source_evidence": "[]",
            "hall": _clean(hall_match.group(1)) if hall_match else "",
            "stand": _clean(stand_match.group(1)) if stand_match else "",
            "sector": _clean(category.get_text(" ", strip=True)) if category else "",
            "description": "",
        })
    for row in rows:
        # The Turkish and English routes expose the same exhibitor slug.  Use
        # that route-independent key so a language switch cannot mint a new
        # source identity.
        parsed_path = urlparse(row["profile_url"])
        slug = parsed_path.path.rstrip("/").split("/")[-1].casefold()
        source_id = "texhibition_2026:" + hashlib.sha256(run_context.canonical_json({"profile_slug": slug}).encode("utf-8")).hexdigest()
        quality = "derived"
        row["source_record_id"] = source_id
        row["source_record_id_quality"] = quality
    return dedupe_rows(rows)


def _detail_value(scope, labels: tuple[str, ...]) -> tuple[str, list[str]]:
    wanted = {_fold(label) for label in labels}
    # Texhibition's production detail card stores each field in its own item.
    # Read only that item's direct key/value pair so a neighbouring value can
    # never be attributed to the requested label.
    for item in scope.select(".item"):
        key_node = item.find(class_="key", recursive=False)
        value_node = item.find(class_="value", recursive=False)
        if key_node is None or value_node is None:
            continue
        key = _fold(key_node.get_text(" ", strip=True)).rstrip(":")
        if key not in wanted:
            continue
        return _clean(value_node.get_text(" ", strip=True)), [
            str(link.get("href", "")) for link in value_node.find_all("a", href=True)
        ]
    for tag in scope.find_all(["dt", "th", "label", "strong", "b", "span", "div"]):
        label = _fold(tag.get_text(" ", strip=True)).rstrip(":")
        if not label or not any(label == item or label.startswith(item + ":") for item in wanted):
            continue
        container = tag.parent
        if container and container.name == "tr":
            cells = container.find_all(["th", "td"], recursive=False)
            if len(cells) > 1:
                value_node = cells[-1]
                return _clean(value_node.get_text(" ", strip=True)), [str(link.get("href", "")) for link in value_node.find_all("a", href=True)]
        if tag.name == "dt" and tag.find_next_sibling("dd"):
            value_node = tag.find_next_sibling("dd")
            return _clean(value_node.get_text(" ", strip=True)), [str(link.get("href", "")) for link in value_node.find_all("a", href=True)]
        if container and container.select_one(":scope > .key") is tag:
            value_node = container.find(class_="value", recursive=False)
            if value_node and value_node is not tag:
                return _clean(value_node.get_text(" ", strip=True)), [str(link.get("href", "")) for link in value_node.find_all("a", href=True)]
        sibling = tag.find_next_sibling()
        if sibling:
            return _clean(sibling.get_text(" ", strip=True)), [str(link.get("href", "")) for link in sibling.find_all("a", href=True)]
    return "", []


_TEXHIBITION_BLOCKED_WEBSITE_HOSTS = {
    "texhibitionist.com",
    "texhibition.com",
}
_TEXHIBITION_ASSET_SUFFIXES = {
    ".7z", ".bmp", ".doc", ".docx", ".gif", ".jpeg", ".jpg", ".pdf",
    ".png", ".rar", ".svg", ".tif", ".tiff", ".webp", ".xls", ".xlsx",
    ".zip",
}


def _texhibition_website_is_catalog_or_asset(value: str) -> bool:
    parsed = urlparse(value)
    host = _catalog_host(value)
    if not host:
        return True
    if host in _TEXHIBITION_BLOCKED_WEBSITE_HOSTS or any(
        host.endswith(f".{blocked}") for blocked in _TEXHIBITION_BLOCKED_WEBSITE_HOSTS
    ):
        return True
    path = (parsed.path or "").casefold()
    if any(path.endswith(suffix) for suffix in _TEXHIBITION_ASSET_SUFFIXES):
        return True
    return any(token in path for token in ("certificate", "certification", "oeko-tex", "oeko_tex"))


def _texhibition_labelled_website(value: str, links: list[str]) -> str:
    # Only the explicitly-labelled Website/Web Site field is in scope.  The
    # surrounding detail page is intentionally never used as a fallback.
    candidates = [*links, value]
    for candidate in candidates:
        normalized = _normalize_website(candidate)
        if normalized and not _texhibition_website_is_catalog_or_asset(normalized):
            return normalized
    return ""


def _texhibition_labelled_phone(value: str, links: list[str]) -> str:
    # tel: links are accepted only when they occur inside the labelled phone
    # field; no page-wide number search is permitted.
    candidates = [
        unquote(link.split(":", 1)[1].split("?", 1)[0]).strip()
        for link in links
        if ":" in link and link.casefold().startswith("tel:")
    ]
    candidates.append(value)
    for candidate in candidates:
        normalized = phone.normalize_phone(_clean(candidate))
        if normalized:
            return normalized
    return ""


def _texhibition_labelled_email(value: str, links: list[str]) -> str:
    # mailto: links are accepted only when they occur inside the labelled
    # email field; page text and unrelated footer links are out of scope.
    candidates = [
        unquote(link.split(":", 1)[1].split("?", 1)[0]).strip()
        for link in links
        if ":" in link and link.casefold().startswith("mailto:")
    ]
    candidates.append(value)
    for candidate in candidates:
        match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", candidate, re.I)
        if match:
            return match.group(0)
    return ""


def _texhibition_detail_scope(html: str):
    soup = BeautifulSoup(html, "html.parser")
    candidates = soup.select("main, article, [class*='profile'], [class*='detail'], [id*='profile'], [id*='detail']")
    scope = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=soup)
    for node in scope.select("footer, header, nav, aside, script, style, noscript"):
        node.decompose()
    return soup, scope


def _texhibition_profile_details(html: str, profile_url: str) -> dict:
    """Extract labelled exhibitor fields while excluding site-wide footer data."""
    soup, scope = _texhibition_detail_scope(html)
    legal_name, _ = _detail_value(scope, ("legal name", "company name", "firma unvani", "ticari unvan", "company"))
    if not legal_name:
        heading = scope.select_one("h1")
        legal_name = _clean(heading.get_text(" ", strip=True) if heading else "")
    website_value, website_links = _detail_value(scope, ("website", "web site", "web sitesi", "firma website"))
    address, _ = _detail_value(scope, ("address", "adres", "company address", "firma adresi"))
    country, _ = _detail_value(scope, ("country", "ulke", "ülke"))
    description, _ = _detail_value(scope, ("description", "about company", "about", "aciklama", "açıklama"))
    brands, _ = _detail_value(scope, ("brands", "brand", "markalar"))
    representations, _ = _detail_value(scope, ("representations", "representation", "temsilcilikler"))
    phone, phone_links = _detail_value(scope, ("phone", "telephone", "telefon", "tel", "gsm", "mobil"))
    email, email_links = _detail_value(scope, ("email", "e-mail", "e posta", "e-posta", "eposta"))

    website = _texhibition_labelled_website(website_value, website_links)
    email = _texhibition_labelled_email(email, email_links)
    phone = _texhibition_labelled_phone(phone, phone_links)
    if not description:
        description = _meta_description(str(soup))
    details = {
        "company": legal_name,
        "listed_legal_name": legal_name,
        "listed_website": website,
        "website": website,
        "listed_address": address,
        "country": country,
        "description": description,
        "brands": brands,
        "representations": representations,
        "listed_phone": phone,
        "listed_email": email,
        "source_detail_url": profile_url,
        "source_detail_content_sha256": hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest(),
        "source_detail_status": "COMPLETED",
        "listed_phone_status": "OBSERVED_PRESENT" if phone else "OBSERVED_ABSENT",
        "listed_address_status": "OBSERVED_PRESENT" if address else "OBSERVED_ABSENT",
    }
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    claims = []
    for field, value in details.items():
        if field not in {"listed_legal_name", "listed_website", "listed_address", "country", "description", "brands", "representations", "listed_phone", "listed_email"} or not value:
            continue
        claim = {
            "source_record_id": "", "field": field, "value": value,
            "url": profile_url,
            "content_sha256": details["source_detail_content_sha256"],
            "observed_at": observed_at,
        }
        if field in {"listed_phone", "listed_address"}:
            claim["normalized_value"] = _source_evidence_normalized(field, value)
        claims.append(claim)
    details["source_evidence"] = json.dumps(claims, ensure_ascii=False, sort_keys=True)
    claims = json.loads(details["source_evidence"])
    for field, status in (
        ("listed_phone", details["listed_phone_status"]),
        ("listed_address", details["listed_address_status"]),
    ):
        if status == "OBSERVED_ABSENT":
            claims.append({
                "source_record_id": "", "field": field, "value": "",
                "observation": status, "url": profile_url,
                "content_sha256": details["source_detail_content_sha256"],
                "observed_at": observed_at,
            })
    details["source_evidence"] = json.dumps(claims, ensure_ascii=False, sort_keys=True)
    return details


def _apply_source_detail(row: dict, html: str, profile_url: str) -> dict:
    details = _texhibition_profile_details(html, profile_url)
    listing_key = _profile_company_key(str(row.get("company", "")))
    profile_key = _profile_company_key(str(details.get("company", "")))
    if not listing_key or not profile_key or listing_key != profile_key:
        _set_source_detail_state(row, "UNAVAILABLE_PROFILE_IDENTITY_MISMATCH", profile_url)
        return row
    for key in (
        "listed_legal_name", "listed_website", "website", "country",
        "description", "brands", "representations", "listed_email",
    ):
        value = details.get(key)
        if value not in ("", None):
            row[key] = value
    _record_profile_observation(row, details, html, profile_url)
    return row


def scrape_texhibition(fetch_details: bool = False, delay_sec: float = 0.4) -> list[dict]:
    session = _session()
    rows: list[dict] = []
    for listing_url in (
        "https://www.texhibitionist.com/en/exhibitors?v=1",
        "https://www.texhibitionist.com/katilimcilar?v=1",
    ):
        page = 1
        visited_urls: set[str] = set()
        page_fingerprints: set[str] = set()
        expected_total: int | None = None
        terminal_evidence = False
        while True:
            if page > config.MAX_TEXHIBITION_PAGES:
                raise ValueError("texhibition_page_limit_exceeded")
            url = listing_url if page == 1 else f"{listing_url}&page={page}"
            if url in visited_urls:
                raise ValueError("texhibition_url_cycle")
            visited_urls.add(url)
            html = _get(session, url)
            page_rows = _texhibition_list_rows(html, listing_url)
            if not page_rows:
                break
            soup = BeautifulSoup(html, "html.parser")
            total_node = soup.select_one("[data-total], [data-total-count]")
            if total_node:
                raw_total = total_node.get("data-total") or total_node.get("data-total-count")
                if str(raw_total).isdigit():
                    expected_total = int(raw_total)
            fingerprint = hashlib.sha256(run_context.canonical_json(sorted(row["source_record_id"] for row in page_rows)).encode()).hexdigest()
            if fingerprint in page_fingerprints:
                raise ValueError("texhibition_source_id_cycle")
            page_fingerprints.add(fingerprint)
            rows.extend(page_rows)
            next_page = soup.select_one(f'a[href*="page={page + 1}"]')
            if not next_page:
                terminal_evidence = expected_total is not None or bool(soup.select_one("[rel='next'], .pagination, [class*='pagination'], [data-total], [data-total-count]"))
                if not terminal_evidence:
                    raise ValueError("texhibition_missing_terminal_pagination_evidence")
                break
            page += 1
            time.sleep(delay_sec)
        if rows:
            break
    if not terminal_evidence and rows:
        raise ValueError("texhibition_missing_terminal_pagination_evidence")
    if expected_total is not None and len({row["source_record_id"] for row in rows}) != expected_total:
        raise ValueError("texhibition_total_count_mismatch")
    if fetch_details:
        rows = dedupe_rows(rows)
        for row in rows:
            profile_url = str(row.get("profile_url", ""))
            if not profile_url:
                _set_source_detail_state(row, "UNAVAILABLE_NO_PROFILE_URL")
                continue
            try:
                _apply_source_detail(row, _get(session, profile_url), profile_url)
            except (requests.RequestException, ValueError):
                _set_source_detail_state(row, "UNAVAILABLE_FETCH_ERROR", profile_url)
            time.sleep(delay_sec)
    else:
        for row in rows:
            _set_source_detail_state(row, "NOT_REQUESTED", str(row.get("profile_url", "")))
    return dedupe_rows(rows)


def scrape_zuchex(fetch_details: bool = False, delay_sec: float = 0.4) -> list[dict]:
    view_id = config.ZUCHEX_VIEW_ID
    event_id = config.ZUCHEX_EVENT_ID
    if not view_id or not event_id or not config.ZUCHEX_FILTER_ID or not config.ZUCHEX_FILTER_VALUE_ID:
        raise ValueError("zuchex_missing_required_ids")
    endpoint = "https://visit.zuchex.com/api/graphql"
    query = """
    query EventExhibitorListViewConnectionQuery(
      $viewId: ID!, $eventId: ID!, $endCursor: String
      $selectedFilters: [Core_EventExhibitorListViewFilterInput!]
    ) {
      view: Core_eventExhibitorListView(viewId: $viewId, filters: $selectedFilters) {
        id
        exhibitors(cursor: {first: 50, after: $endCursor}) {
          nodes { id: _id name withEvent(eventId: $eventId) { booth } }
          pageInfo { hasNextPage endCursor }
          totalCount
        }
      }
    }
    """
    session = _session()
    rows: list[dict] = []
    unique_ids: set[str] = set()
    expected_total: int | None = None
    seen_cursors: set[str] = set()
    page_number = 0
    cursor = None
    selected_filters = [{
        "mustEventFiltersIn": [{
                "filterId": config.ZUCHEX_FILTER_ID,
                "values": [config.ZUCHEX_FILTER_VALUE_ID],
        }],
    }]
    listing_url = "https://www.zuchex.com/tr/ziyaretci/Katilimci-Listesi-2026.html"
    while True:
        page_number += 1
        if page_number > config.MAX_ZUCHEX_PAGES:
            raise ValueError("zuchex_page_limit_exceeded")
        if cursor is not None:
            if cursor in seen_cursors:
                raise ValueError("zuchex_cursor_cycle")
            seen_cursors.add(cursor)
        payload = _post_graphql(session, endpoint, {
            "query": query,
            "variables": {
                "viewId": view_id,
                "eventId": event_id,
                "endCursor": cursor,
                "selectedFilters": selected_filters,
            },
        })
        try:
            data = payload["data"]
            view = data["view"]
            connection = view["exhibitors"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            total_count = int(connection["totalCount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("zuchex_invalid_nested_graphql_schema") from exc
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise ValueError("zuchex_invalid_nested_graphql_schema")
        if expected_total is None:
            expected_total = total_count
        elif expected_total != total_count:
            raise ValueError("zuchex_total_count_changed")
        for item in nodes:
            if not isinstance(item, dict):
                raise ValueError("zuchex_invalid_node")
            source_id = str(item.get("_id") or item.get("id") or "").strip()
            if not source_id:
                raise ValueError("zuchex_missing_source_id")
            if source_id in unique_ids:
                continue
            unique_ids.add(source_id)
            event_data = item.get("withEvent") or {}
            profile_url = next(
                (str(item.get(key)).strip() for key in ("profileUrl", "profile_url", "url") if item.get(key)),
                "",
            )
            rows.append({
                "company": _clean(item.get("name", "")),
                "website": "",
                "listed_website": "",
                "source": "zuchex_2026",
                "country": "Türkiye",
                "profile_url": profile_url,
                "listing_url": listing_url,
                "source_detail_status": "NOT_REQUESTED",
                "listed_phone": "",
                "listed_address": "",
                "listed_phone_status": "NOT_REQUESTED",
                "listed_address_status": "NOT_REQUESTED",
                "source_evidence": "[]",
                "hall": "",
                "stand": _clean(event_data.get("booth", "")),
                "sector": "ev ve mutfak esyalari",
                "description": "",
                "_id": source_id,
                "source_record_id": f"zuchex_2026:{source_id}",
            })
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise ValueError("zuchex_missing_pagination_cursor")
        time.sleep(delay_sec)
    if expected_total is None or len(unique_ids) != expected_total:
        raise ValueError(
            f"zuchex_total_count_mismatch:{len(unique_ids)}!={expected_total}"
        )
    if fetch_details:
        # The public list schema does not guarantee a profile URL.  Follow a
        # URL only when it is actually present in the observed node payload;
        # no guessed GraphQL field or endpoint is introduced.
        for row in rows:
            profile_url = str(row.get("profile_url", "") or "")
            if not profile_url:
                _set_source_detail_state(row, "UNAVAILABLE_NO_PROFILE_URL")
                continue
            try:
                _apply_source_detail(row, _get(session, profile_url), profile_url)
            except (requests.RequestException, ValueError):
                _set_source_detail_state(row, "UNAVAILABLE_FETCH_ERROR", profile_url)
            time.sleep(delay_sec)
    else:
        for row in rows:
            _set_source_detail_state(row, "NOT_REQUESTED", str(row.get("profile_url", "")))
    return dedupe_rows(rows)


def scrape_ifco(fetch_details: bool = False, delay_sec: float = 0.4) -> list[dict]:
    base_url = "https://www.ifco.com.tr"
    list_url = f"{base_url}/tr/fuar/katilimcilar"
    session = _session()
    rows_by_profile: dict[str, dict] = {}
    page = 1

    while True:
        url = list_url if page == 1 else f"{list_url}?page={page}"
        html = _get(session, url)
        soup = BeautifulSoup(html, "html.parser")
        links = soup.select('a[href*="/fair/exhibitors/"], a[href*="/tr/fuar/exhibitors/"]')
        page_rows = 0

        for link in links:
            href = link.get("href", "")
            lowered_href = href.casefold()
            if any(part in lowered_href for part in ("/detail", "/showroom", "contact-form")):
                continue
            image = link.find("img", alt=True)
            company = _clean(image["alt"] if image else link.get_text(" ", strip=True))
            if not company:
                continue
            profile_url = _absolute_url(base_url, href)
            if profile_url in rows_by_profile:
                continue
            row = {
                "company": company,
                "website": "",
                "source": "ifco",
                "country": "",
                "profile_url": profile_url,
                "sector": "tekstil giyim moda hazir giyim",
                "description": "",
                "listed_phone": "",
                "listed_address": "",
            }
            if fetch_details and not profile_url:
                _set_source_detail_state(row, "UNAVAILABLE_NO_PROFILE_URL")
            elif fetch_details:
                try:
                    detail_html = _get(session, profile_url)
                    _soup_detail, scope = _texhibition_detail_scope(detail_html)
                    heading = scope.select_one("h1")
                    details = {
                        "company": _clean(heading.get_text(" ", strip=True) if heading else ""),
                        "listed_address": "",
                        "listed_phone": "",
                    }
                    details["listed_address"], _ = _detail_value(
                        scope, ("company address", "firma adresi", "address", "adres")
                    )
                    phone_value, phone_links = _detail_value(
                        scope, ("phone", "telephone", "telefon", "tel", "gsm", "mobil")
                    )
                    details["listed_phone"] = _texhibition_labelled_phone(phone_value, phone_links)
                    if _profile_company_key(company) == _profile_company_key(details["company"]):
                        row["website"] = _first_external_website(str(scope), "ifco.com.tr")
                        row["description"] = _meta_description(detail_html)
                        _record_profile_observation(row, details, detail_html, profile_url)
                    else:
                        _set_source_detail_state(row, "UNAVAILABLE_PROFILE_IDENTITY_MISMATCH", profile_url)
                    time.sleep(delay_sec)
                except requests.RequestException:
                    _set_source_detail_state(row, "UNAVAILABLE_FETCH_ERROR", profile_url)
            else:
                _set_source_detail_state(row, "NOT_REQUESTED", profile_url)
            rows_by_profile[profile_url] = row
            page_rows += 1

        next_page = soup.select_one(f'a[href*="page={page + 1}"]')
        if not next_page or page_rows == 0:
            break
        page += 1
        time.sleep(delay_sec)

    return dedupe_rows(list(rows_by_profile.values()))


def scrape_idos(fetch_details: bool = False, delay_sec: float = 0.4) -> list[dict]:
    base_url = "https://crm.idos.events"
    catalogue_url = f"{base_url}/portal/catalogue/75"
    session = _session()
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
        page_rows_data = _idos_list_rows(html, url)
        page_rows = 0

        for row in page_rows_data:
            source_id = str(row.get("source_record_id", ""))
            if not source_id or source_id in rows_by_profile:
                continue
            profile_url = str(row.get("profile_url", ""))
            if fetch_details and not profile_url:
                _set_source_detail_state(row, "UNAVAILABLE_NO_PROFILE_URL")
            elif fetch_details:
                try:
                    detail_html = _get(session, profile_url)
                    details = _idos_profile_details(detail_html, profile_url)
                    if _profile_company_key(str(row.get("company", ""))) == _profile_company_key(details["company"]):
                        row["website"] = details["website"]
                        row["description"] = details["description"]
                        _record_profile_observation(row, details, detail_html, profile_url)
                    else:
                        _set_source_detail_state(row, "UNAVAILABLE_PROFILE_IDENTITY_MISMATCH", profile_url)
                    time.sleep(delay_sec)
                except (requests.RequestException, ValueError):
                    _set_source_detail_state(row, "UNAVAILABLE_FETCH_ERROR", profile_url)
            else:
                _set_source_detail_state(row, "NOT_REQUESTED", profile_url)
            rows_by_profile[source_id] = row
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
    _soup, scope = _beauty_detail_scope(html)
    value, links = _beauty_labelled_value(
        scope, ("Firma Websitesi", "Website", "Web Sitesi")
    )
    for candidate in [*links, value]:
        website = _normalize_website(candidate)
        if website:
            return website
    return ""


def scrape_beauty_eurasia(fetch_details: bool = True, delay_sec: float = 0.4) -> list[dict]:
    endpoint = "https://beautyeurasia.com/ERAForms/companies_list.php?l=tr&exhibition=24&y=2026"
    session = _session()
    headers = {
        **HEADERS,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://beautyeurasia.com/tr/2026-katilimci-listesi",
    }
    rows: list[dict] = []
    start = 0
    length = 100

    while True:
        payload = _post_json(
            session,
            endpoint,
            data=_beauty_datatable_payload(start, length),
            headers=headers,
        )
        data = payload.get("data", [])
        if not data:
            break
        for row in data:
            company = _beauty_cell_text(row[1] if len(row) > 1 else "")
            country = _beauty_cell_text(row[2] if len(row) > 2 else "")
            if "türkiye" not in country.lower() and "turkiye" not in country.lower():
                continue
            profile_url = _beauty_profile_url(row)
            result_row = {
                "company": company.replace(" Yeni katılımcı", "").strip(),
                "website": "",
                "source": "beauty_eurasia",
                "country": country,
                "profile_url": profile_url,
                "sector": "",
                "description": "",
            }
            if fetch_details and not profile_url:
                _set_source_detail_state(result_row, "UNAVAILABLE_NO_PROFILE_URL")
            elif fetch_details:
                try:
                    detail_html = _get(session, profile_url)
                    details = _beauty_profile_details(detail_html, profile_url)
                    if _profile_company_key(result_row["company"]) == _profile_company_key(details["company"]):
                        result_row["website"] = details["website"]
                        result_row["sector"] = details["sector"]
                        result_row["description"] = details["description"]
                        _record_profile_observation(result_row, details, detail_html, profile_url)
                    else:
                        _set_source_detail_state(result_row, "UNAVAILABLE_PROFILE_IDENTITY_MISMATCH", profile_url)
                    time.sleep(delay_sec)
                except (requests.RequestException, ValueError):
                    _set_source_detail_state(result_row, "UNAVAILABLE_FETCH_ERROR", profile_url)
            else:
                _set_source_detail_state(result_row, "NOT_REQUESTED", profile_url)
            rows.append(result_row)
        start += length
        if start >= int(payload.get("recordsTotal", start)):
            break
        time.sleep(delay_sec)
    return dedupe_rows(rows)


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
                details["listed_phone"] = phone.normalize_phone(text)
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


def _brand_values(card) -> str:
    """Read brand names that belong to this card, without crossing card boundaries."""
    heading = next(
        (
            item
            for item in card.select("h3.brand-desc-title")
            if _fold(item.get_text(" ", strip=True)) == _fold("Markalar")
        ),
        None,
    )
    if heading is None:
        return ""

    values: list[str] = []
    pending: list[str] = []
    for sibling in heading.next_siblings:
        name = getattr(sibling, "name", None)
        if name == "button" or (name and name.startswith("h")):
            break
        if name == "br":
            value = _clean(" ".join(pending))
            if value:
                values.append(value.rstrip(",;"))
            pending = []
            continue
        text = sibling.get_text(" ", strip=True) if name else str(sibling)
        if _clean(text):
            pending.append(_clean(text))
    value = _clean(" ".join(pending))
    if value:
        values.append(value.rstrip(",;"))
    return "; ".join(dict.fromkeys(item for item in values if item))


def _profile_company_key(value: str) -> str:
    folded = re.sub(r"\btemsilci\s+firma\b", " ", _fold(value))
    return re.sub(r"[^a-z0-9]+", "", folded)


def _merge_brand_catalog_profile(row: dict, details: dict, *, website_field: str) -> bool:
    """Merge a detail page only when its heading identifies the listing company."""
    listing_key = _profile_company_key(str(row.get("company", "")))
    profile_key = _profile_company_key(str(details.get("company", "")))
    if not listing_key or not profile_key or listing_key != profile_key:
        return False
    for field, value in details.items():
        if not value or field == "company":
            continue
        row[website_field if field == "website" else field] = value
    return True


def _brand_catalog_list_rows(
    html: str,
    base_url: str,
    *,
    source: str,
    sector: str,
    listing_url: str = "",
) -> list[dict]:
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
            "source": source,
            "country": _clean(country_el.get_text(" ", strip=True) if country_el else "Türkiye"),
            "profile_url": _absolute_url(base_url, href),
            "listing_url": listing_url,
            "sector": sector,
            "description": "",
            "listed_phone": "",
            "listed_email": "",
            "listed_address": "",
            "listed_phone_status": "NOT_REQUESTED",
            "listed_address_status": "NOT_REQUESTED",
            "source_detail_status": "NOT_REQUESTED",
            "source_evidence": "[]",
            "hall": hall,
            "stand": stand,
            "brands": _brand_values(card),
            "representations": "",
        })
    return rows


def _maktek_list_rows(html: str, base_url: str) -> list[dict]:
    return _brand_catalog_list_rows(
        html,
        base_url,
        source="maktek_avrasya_2026",
        sector="makine, takım tezgahları, metal işleme ve üretim teknolojileri",
        listing_url=f"{base_url}/katilimci-listesi?country=T%C3%9CRK%C4%B0YE",
    )


def scrape_maktek(fetch_details: bool = True, delay_sec: float = 0.2) -> list[dict]:
    base_url = "https://www.maktekfuari.com"
    list_url = f"{base_url}/katilimci-listesi?country=T%C3%9CRK%C4%B0YE"
    session = _session()
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
            if fetch_details and not profile_url:
                _set_source_detail_state(row, "UNAVAILABLE_NO_PROFILE_URL")
            elif fetch_details:
                try:
                    detail_html = _get(session, profile_url)
                    details = _maktek_profile_details(detail_html)
                    if _merge_brand_catalog_profile(row, details, website_field="website"):
                        _record_profile_observation(row, details, detail_html, profile_url)
                    else:
                        _set_source_detail_state(row, "UNAVAILABLE_PROFILE_IDENTITY_MISMATCH", profile_url)
                    time.sleep(delay_sec)
                except requests.RequestException:
                    _set_source_detail_state(row, "UNAVAILABLE_FETCH_ERROR", profile_url)
            else:
                _set_source_detail_state(row, "NOT_REQUESTED", profile_url)
            rows_by_profile[profile_url] = row
        page += 1
        if page <= max_page:
            time.sleep(delay_sec)
    return dedupe_rows(list(rows_by_profile.values()))


def scrape_foodist(fetch_details: bool = True, delay_sec: float = 0.2) -> list[dict]:
    base_url = "https://www.foodistexpo.com"
    list_url = f"{base_url}/katilimci-listesi?country=T%C3%9CRK%C4%B0YE"
    session = _session()
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

        for row in _brand_catalog_list_rows(
            html,
            base_url,
            source="foodist_expo_turkiye",
            sector="gıda ve içecek",
            listing_url=list_url,
        ):
            country = _fold(row.get("country", ""))
            if country and "turkiye" not in country:
                continue
            profile_url = row["profile_url"]
            if profile_url in rows_by_profile:
                continue
            if fetch_details and not profile_url:
                _set_source_detail_state(row, "UNAVAILABLE_NO_PROFILE_URL")
            elif fetch_details:
                try:
                    detail_html = _get(session, profile_url)
                    details = _maktek_profile_details(detail_html)
                    if _merge_brand_catalog_profile(row, details, website_field="listed_website"):
                        _record_profile_observation(row, details, detail_html, profile_url)
                    else:
                        _set_source_detail_state(row, "UNAVAILABLE_PROFILE_IDENTITY_MISMATCH", profile_url)
                    time.sleep(delay_sec)
                except requests.RequestException:
                    _set_source_detail_state(row, "UNAVAILABLE_FETCH_ERROR", profile_url)
            else:
                _set_source_detail_state(row, "NOT_REQUESTED", profile_url)
            rows_by_profile[profile_url] = row
        page += 1
        if page <= max_page:
            time.sleep(delay_sec)
    return dedupe_rows(list(rows_by_profile.values()))


def dedupe_rows(rows: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for row in rows:
        row = dict(row)
        company = _clean(row.get("company", ""))
        if not company:
            continue
        source_id, quality = run_context.source_record_identity(row, default_source="unknown")
        row["source_record_id_quality"] = quality
        row["company"] = company
        row["source_record_id"] = source_id
        existing = deduped.get(source_id)
        if existing is None:
            deduped[source_id] = row
            continue
        for field, value in row.items():
            if not existing.get(field) and value:
                existing[field] = value
    return list(deduped.values())
