from collections import OrderedDict
from io import BytesIO
import re
import threading
import warnings
import xml.etree.ElementTree as ET
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlparse

import requests

import config
from modules import cache_store, network_guard, runtime, site_mapper
from modules.extractor import extract_contact_page_links, extract_contact_records, extract_emails, extract_phones
from modules import scorer
from modules.utils import retry_with_backoff


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": config.USER_AGENT, "Accept-Language": "tr,en;q=0.8"})
_FETCH_STATE = threading.local()
_RENDER_STATE = threading.local()
_DOCUMENT_STATE = threading.local()
_SESSION_LOCK = threading.Lock()


def _with_scheme(url: str) -> str:
    return url if "://" in url else f"https://{url}"


def _is_transient_fetch_error(exc: Exception) -> bool:
    """Retry only failures that can reasonably recover moments later."""
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        status = int(response.status_code) if response is not None else 0
        return status in {408, 429} or status >= 500
    return False


@retry_with_backoff(retry_if=_is_transient_fetch_error)
def _fetch(url: str) -> requests.Response:
    return _request_with_safe_redirects(url, verify=True)


def _request_with_safe_redirects(url: str, verify: bool) -> requests.Response:
    current = url
    original_host = urlparse(url).netloc.casefold()
    for redirect_count in range(config.MAX_HTTP_REDIRECTS + 1):
        allowed, reason = network_guard.validate_public_http_url(current)
        if not allowed:
            raise requests.exceptions.InvalidURL(f"blocked_network_target:{reason}")
        runtime.wait_for_request_slot()
        runtime.record("http.crawler.requests")
        with _SESSION_LOCK:
            response = SESSION.get(
                current,
                timeout=config.REQUEST_TIMEOUT_SEC,
                allow_redirects=False,
                verify=verify,
            )
        if response.status_code not in {301, 302, 303, 307, 308}:
            response.raise_for_status()
            setattr(response, "_b2b_final_url", current)
            setattr(response, "_b2b_tls_insecure", not verify)
            return response
        location = response.headers.get("location", "").strip()
        if not location:
            response.raise_for_status()
            return response
        target = urljoin(current, location)
        target_host = urlparse(target).netloc.casefold()
        if not scorer.same_registrable_domain(original_host, target_host):
            raise requests.exceptions.InvalidURL(f"cross_domain_redirect:{target}")
        current = target
        if redirect_count >= config.MAX_HTTP_REDIRECTS:
            raise requests.exceptions.TooManyRedirects(f"redirect_limit:{url}")
    raise requests.exceptions.TooManyRedirects(f"redirect_limit:{url}")


def _try_fetch(url: str) -> tuple[str | None, str | None]:
    _FETCH_STATE.last = {"requested_url": url, "final_url": url, "tls_insecure": False}
    try:
        response = _fetch(url)
        _FETCH_STATE.last = {
            "requested_url": url,
            "final_url": getattr(response, "_b2b_final_url", getattr(response, "url", url)),
            "tls_insecure": bool(getattr(response, "_b2b_tls_insecure", False)),
        }
        content_type = response.headers.get("content-type", "")
        supported = ("text/html", "application/xhtml", "text/plain", "xml", "vcard", "text/x-vcard")
        if not any(value in content_type.casefold() for value in supported) and content_type:
            return None, f"unsupported_content_type:{content_type}"
        response.encoding = response.encoding or response.apparent_encoding
        return response.text, None
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "http_error"
        return None, f"http_{status_code}"
    except requests.exceptions.SSLError:
        # A surprising number of small company sites serve a valid page with
        # an incomplete/legacy certificate chain.  Retry only this failure
        # without certificate verification; the site still has to pass all
        # identity checks before anything is published.
        try:
            from urllib3.exceptions import InsecureRequestWarning
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                response = _request_with_safe_redirects(url, verify=False)
            _FETCH_STATE.last = {
                "requested_url": url,
                "final_url": getattr(response, "_b2b_final_url", getattr(response, "url", url)),
                "tls_insecure": True,
            }
            response.encoding = response.encoding or response.apparent_encoding
            return response.text, None
        except requests.exceptions.RequestException:
            return None, "ssl_error"
    except requests.exceptions.ConnectionError:
        return None, "connection_error"
    except requests.exceptions.InvalidURL as exc:
        return None, str(exc)
    except requests.exceptions.TooManyRedirects:
        return None, "redirect_limit"
    except requests.exceptions.RequestException as exc:
        return None, exc.__class__.__name__.lower()


def _looks_like_js_shell(html: str) -> bool:
    visible_text = re.sub(r"<[^>]+>", " ", html)
    compact = re.sub(r"\s+", " ", visible_text).strip()
    markers = ("id=\"root\"", "id='root'", "id=\"app\"", "id='app'", "__next")
    return len(compact) < 300 and any(marker in html.lower() for marker in markers)


def _contact_page_needs_render(html: str) -> bool:
    if extract_emails(html) or extract_phones(html):
        return False
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).casefold()
    dynamic_markers = (
        "javascript must be enabled", "enable javascript", "javascript is required",
        "javascript acik olmalidir", "javascript açık olmalıdır",
        "bu sayfayi goruntulemek icin javascript", "bu sayfayı görüntülemek için javascript",
    )
    return _looks_like_js_shell(html) or any(marker in text for marker in dynamic_markers)


def _looks_like_security_interstitial(html: str) -> bool:
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).casefold()
    markers = (
        "ta strona stanowi zagro",  # ISP/browser warning observed on legacy sites
        "deceptive site ahead",
        "this site may contain malicious software",
        "privacy error",
        "just a moment", "cloudflare ray id", "cf-chl-", "attention required",
    )
    return any(marker in text for marker in markers)


def _render_request_policy(
    original_host: str, request_url: str, resource_type: str,
) -> tuple[bool, str]:
    """Allow passive assets, but keep rendered active data on the official site."""
    allowed, reason = network_guard.validate_public_http_url(request_url)
    if not allowed:
        return False, f"unsafe_target:{reason}"
    request_host = urlparse(request_url).netloc.casefold()
    if resource_type in {"document", "xhr", "fetch", "eventsource"} and not (
        request_host
        and scorer.same_registrable_domain(original_host.casefold(), request_host)
    ):
        return False, "cross_site_active_data"
    return True, "allowed"


def _try_render(url: str) -> tuple[str | None, str | None]:
    _RENDER_STATE.last = {
        "retrieval_method": "browser_render",
        "blocked_cross_site_active_requests": 0,
        "blocked_unsafe_requests": 0,
    }
    if not config.ENABLE_JS_FALLBACK:
        return None, "js_fallback_disabled"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright_not_installed"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=config.USER_AGENT,
                    locale="tr-TR",
                    ignore_https_errors=True,
                    service_workers="block",
                )
                original_host = urlparse(url).netloc.casefold()

                def guard_route(route) -> None:
                    request_url = route.request.url
                    allowed, reason = _render_request_policy(
                        original_host, request_url, route.request.resource_type,
                    )
                    if not allowed and reason.startswith("unsafe_target:"):
                        _RENDER_STATE.last["blocked_unsafe_requests"] += 1
                        route.abort()
                    elif not allowed:
                        _RENDER_STATE.last["blocked_cross_site_active_requests"] += 1
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", guard_route)
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=config.JS_RENDER_TIMEOUT_SEC * 1000)
                rendered = page.content()
                _RENDER_STATE.last["final_url"] = page.url
                return rendered, None
            finally:
                browser.close()
    except Exception as exc:
        return None, f"js_render_failed:{exc.__class__.__name__.lower()}"


def _renderable_fetch_error(error: str | None) -> bool:
    return bool(error and any(marker in error for marker in (
        "http_401", "http_403", "http_408", "http_429", "timeout",
        "ssl_error", "connection_error", "security_interstitial",
    )))


def _contactish_url(url: str) -> bool:
    value = unquote(url).casefold()
    keywords = (
        "contact", "iletisim", "iletişim", "bize-ulas", "about", "hakkimizda",
        "hakkımızda", "kurumsal", "corporate", "kvkk", "legal", "privacy",
        "gizlilik", "office", "sube", "şube", "whatsapp", "whats-app",
    )
    smart_map_keywords = (
        "terms", "kullanim", "location", "lokasyon", "distributor",
        "bayi", "dealer", "catalog", "catalogue", "katalog", "brochure", "brosur",
    )
    return any(keyword in value for keyword in (*keywords, *smart_map_keywords)) or value.endswith((".vcf", ".vcard"))


def _robots_and_sitemaps(root: str) -> tuple[robotparser.RobotFileParser | None, list[str]]:
    robots_url = urljoin(root.rstrip("/") + "/", "robots.txt")
    content, _ = _try_fetch(robots_url)
    parser = None
    sitemap_urls: list[str] = []
    if content:
        parser = robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(content.splitlines())
        for line in content.splitlines():
            if line.casefold().startswith("sitemap:"):
                sitemap_urls.append(line.split(":", 1)[1].strip())
    sitemap_urls.append(urljoin(root.rstrip("/") + "/", "sitemap.xml"))
    return parser, list(dict.fromkeys(url for url in sitemap_urls if url))[: config.MAX_SITEMAPS]


def _sitemap_contact_urls(root: str, sitemap_urls: list[str]) -> list[str]:
    root_host = urlparse(root).netloc.casefold()
    pending = list(sitemap_urls)
    seen_sitemaps = set()
    found: list[str] = []
    examined = 0
    while pending and len(seen_sitemaps) < config.MAX_SITEMAPS and examined < config.MAX_SITEMAP_URLS:
        sitemap_url = pending.pop(0)
        if sitemap_url in seen_sitemaps or not scorer.same_registrable_domain(
            urlparse(sitemap_url).netloc.casefold(), root_host
        ):
            continue
        seen_sitemaps.add(sitemap_url)
        content, _ = _try_fetch(sitemap_url)
        if not content:
            continue
        try:
            xml_root = ET.fromstring(content)
            locations = [
                (node.text or "").strip()
                for node in xml_root.iter()
                if node.tag.casefold().endswith("loc") and (node.text or "").strip()
            ]
        except ET.ParseError:
            # Some otherwise useful sitemaps are served as loose text or have a
            # broken namespace/header. URL extraction remains same-domain and
            # contact/legal-marker restricted below.
            locations = re.findall(r"https?://[^\s<>'\"]+", content)
        for location in locations:
            examined += 1
            if examined > config.MAX_SITEMAP_URLS:
                break
            if not scorer.same_registrable_domain(urlparse(location).netloc.casefold(), root_host):
                continue
            if location.casefold().endswith((".xml", ".xml.gz")):
                pending.append(location)
            elif _contactish_url(location):
                found.append(location)
    result = list(dict.fromkeys(found))
    runtime.record("recovery.sitemap_urls_found", len(result))
    return result


def _document_links(html: str, base_url: str) -> list[str]:
    from bs4 import BeautifulSoup

    host = urlparse(base_url).netloc.casefold()
    urls = []
    for link in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(base_url, link.get("href", ""))
        if not scorer.same_registrable_domain(urlparse(url).netloc.casefold(), host):
            continue
        label = f"{url} {link.get_text(' ', strip=True)}".casefold()
        if url.casefold().endswith((".vcf", ".vcard")) or (
            url.casefold().endswith(".pdf") and _contactish_url(label)
        ):
            urls.append(url)
    return list(dict.fromkeys(urls))[: config.MAX_DOCUMENT_LINKS]


def _try_ocr_pdf(content: bytes) -> tuple[str | None, str | None]:
    """OCR a small bounded prefix when optional local dependencies exist."""
    if not config.ENABLE_PDF_OCR:
        return None, "pdf_ocr_disabled"
    runtime.record("recovery.pdf_ocr_attempts")
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError:
        return None, "pdf_ocr_dependencies_unavailable"
    try:
        document = fitz.open(stream=content, filetype="pdf")
        texts = []
        scale = max(config.PDF_OCR_DPI, 72) / 72
        matrix = fitz.Matrix(scale, scale)
        page_limit = min(document.page_count, max(config.PDF_OCR_MAX_PAGES, 1))
        for page_number in range(page_limit):
            page = document.load_page(page_number)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.open(BytesIO(pixmap.tobytes("png")))
            texts.append(pytesseract.image_to_string(image, lang="tur+eng"))
        text = "\n".join(value for value in texts if value).strip()
        if text:
            runtime.record("recovery.pdf_ocr_successes")
            return text, None
        return None, "pdf_ocr_no_text"
    except Exception as exc:
        return None, f"pdf_ocr_failed:{exc.__class__.__name__.lower()}"


def _try_extract_pdf(url: str) -> tuple[str | None, str | None]:
    _DOCUMENT_STATE.last = {"retrieval_method": "pdf_text", "source_url": url}
    runtime.record("recovery.pdf_attempts")
    try:
        response = _request_with_safe_redirects(url, verify=True)
        if len(response.content) > 8 * 1024 * 1024:
            return None, "pdf_too_large"
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(response.content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:20]).strip()
        if len(text) >= config.PDF_MIN_TEXT_CHARS:
            runtime.record("recovery.pdf_text_successes")
            return text, None
        ocr_text, ocr_error = _try_ocr_pdf(response.content)
        if ocr_text:
            _DOCUMENT_STATE.last["retrieval_method"] = "pdf_ocr"
            return "\n".join(value for value in (text, ocr_text) if value), None
        if text:
            runtime.record("recovery.pdf_short_text_successes")
            return text, None
        runtime.record("recovery.pdf_failures")
        return None, ocr_error or "pdf_no_text"
    except ImportError:
        return None, "pypdf_not_installed"
    except Exception as exc:
        runtime.record("recovery.pdf_failures")
        return None, f"pdf_failed:{exc.__class__.__name__.lower()}"


def _safe_contact_seed_urls(root: str, seed_urls: list[str] | None) -> list[str]:
    """Keep only contact-like HTTP(S) URLs belonging to the candidate site."""
    root_host = urlparse(root).netloc.casefold()
    safe: list[str] = []
    for raw_url in seed_urls or []:
        candidate_url = _with_scheme(raw_url)
        parsed = urlparse(candidate_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if not scorer.same_registrable_domain(parsed.netloc.casefold(), root_host):
            continue
        if not _contactish_url(candidate_url):
            continue
        safe.append(candidate_url)
    return list(dict.fromkeys(safe))[: config.MAX_CONTACT_PAGES]


def _fetch_site_live(url: str, contact_seed_urls: list[str] | None = None, profile: str = "full") -> dict:
    base_url = _with_scheme(url)
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    pages: OrderedDict[str, str] = OrderedDict()
    page_provenance: dict[str, dict] = {}
    errors: list[str] = []
    tls_insecure = False
    root_retrieval_method = "http"
    recovery_trace: list[dict] = []

    html, error = _try_fetch(root)
    root_meta = getattr(_FETCH_STATE, "last", {})
    tls_insecure = tls_insecure or bool(root_meta.get("tls_insecure"))
    if html and root_meta.get("tls_insecure"):
        root_retrieval_method = "http_tls_unverified"
    final_root_url = root_meta.get("final_url", root)
    if html and scorer.same_registrable_domain(urlparse(final_root_url).netloc, parsed.netloc):
        final_parsed = urlparse(final_root_url)
        root = f"{final_parsed.scheme}://{final_parsed.netloc}"
    if html and _looks_like_security_interstitial(html):
        html, error = None, "security_interstitial"
    if not html and parsed.scheme == "https":
        http_root = f"http://{parsed.netloc}"
        http_html, http_error = _try_fetch(http_root)
        http_meta = getattr(_FETCH_STATE, "last", {})
        tls_insecure = tls_insecure or bool(http_meta.get("tls_insecure"))
        if http_html and _looks_like_security_interstitial(http_html):
            http_html, http_error = None, "security_interstitial"
        if http_html:
            root = http_root
            html = http_html
            root_retrieval_method = (
                "http_tls_unverified" if http_meta.get("tls_insecure") else "http"
            )
            error = None
        elif http_error:
            error = f"https:{error}; http:{http_error}"
    # Static recovery precedes browser rendering. A WAF may block only the
    # homepage while sitemap-listed legal/contact pages remain reachable.
    if not html:
        runtime.record("recovery.homepage_failures")
        _, recovery_sitemaps = _robots_and_sitemaps(root)
        sitemap_pages = _sitemap_contact_urls(root, recovery_sitemaps)
        recovery_urls = site_mapper.balanced_urls(
            [{"url": value, "kind": site_mapper.classify(value)} for value in sitemap_pages],
            [
                *(urljoin(root, path) for path in config.IDENTITY_PAGE_PATHS),
                *(urljoin(root, path) for path in config.CONTACT_PAGE_PATHS),
            ],
            config.MAX_STATIC_RECOVERY_PAGES,
        )
        recovery_trace.append({
            "stage": "static_pages",
            "candidates": len(recovery_urls),
            "sitemap_candidates": len(sitemap_pages),
        })
        for recovery_url in recovery_urls:
            runtime.record("recovery.static_attempts")
            recovery_html, recovery_error = (
                _try_extract_pdf(recovery_url)
                if recovery_url.casefold().endswith(".pdf")
                else _try_fetch(recovery_url)
            )
            if recovery_html and not _looks_like_security_interstitial(recovery_html):
                runtime.record("recovery.static_successes")
                pages[recovery_url] = recovery_html
                page_provenance[recovery_url] = dict(
                    _DOCUMENT_STATE.last
                    if recovery_url.casefold().endswith(".pdf")
                    else {
                        "retrieval_method": "http_tls_unverified"
                        if getattr(_FETCH_STATE, "last", {}).get("tls_insecure")
                        else "http"
                    }
                )
            elif recovery_error:
                errors.append(f"{recovery_url}:{recovery_error}")
    if not html and not pages and _renderable_fetch_error(error):
        runtime.record("recovery.browser_attempts")
        tls_insecure = tls_insecure or bool(error and "ssl_error" in error)
        rendered_html, render_error = _try_render(base_url)
        if rendered_html:
            runtime.record("recovery.browser_successes")
            root = base_url
            html = rendered_html
            root_retrieval_method = "browser_render"
            error = None
        elif render_error and render_error != "js_fallback_disabled":
            errors.append(f"{base_url}:{render_error}")
    if html and _looks_like_js_shell(html):
        runtime.record("recovery.browser_attempts")
        rendered_html, render_error = _try_render(root)
        if rendered_html:
            runtime.record("recovery.browser_successes")
            html = rendered_html
            root_retrieval_method = "browser_render"
        elif render_error and render_error != "js_fallback_disabled":
            errors.append(f"{root}:{render_error}")
    if html:
        pages[root] = html
        page_provenance[root] = (
            dict(getattr(_RENDER_STATE, "last", {}))
            if root_retrieval_method == "browser_render"
            else {"retrieval_method": root_retrieval_method}
        )
    elif error:
        errors.append(f"{root}:{error}")

    if profile == "identity":
        recovered_discovery = []
        for page_url, page_html in pages.items():
            recovered_discovery.extend(
                site_mapper.discover(page_html, page_url, include_documents=False)
            )
        identity_urls = site_mapper.balanced_urls(
            [
                *site_mapper.discover(html or "", root, include_documents=False),
                *recovered_discovery,
            ],
            [urljoin(root, path) for path in config.IDENTITY_PAGE_PATHS],
            config.MAX_IDENTITY_PAGES,
        )
        for identity_url in identity_urls:
            if identity_url in pages:
                continue
            identity_html, identity_error = _try_fetch(identity_url)
            identity_meta = getattr(_FETCH_STATE, "last", {})
            tls_insecure = tls_insecure or bool(identity_meta.get("tls_insecure"))
            if identity_html:
                final_identity_url = identity_meta.get("final_url", identity_url)
                pages[final_identity_url] = identity_html
                page_provenance[final_identity_url] = {
                    "retrieval_method": "http_tls_unverified"
                    if identity_meta.get("tls_insecure") else "http",
                }
            elif identity_error:
                errors.append(f"{identity_url}:{identity_error}")
        return {
            "url": root,
            "pages": [
                {"url": page_url, "html": page_html, **page_provenance.get(
                    page_url, {"retrieval_method": "unknown"},
                )}
                for page_url, page_html in pages.items()
            ],
            "error": "; ".join(errors[:5]) if not pages and errors else "",
            "tls_insecure": tls_insecure,
            "redirect_target": "",
            "crawl_profile": "identity",
            "recovery_trace": recovery_trace,
        }

    robots_parser, sitemap_urls = _robots_and_sitemaps(root)
    contact_urls = _safe_contact_seed_urls(root, contact_seed_urls)
    discovered_contact_urls: set[str] = set()
    discovery_pages = [(root, html)] if html else []
    discovery_pages.extend((page_url, page_html) for page_url, page_html in pages.items())
    for discovery_url, discovery_html in discovery_pages:
        discovered_contact_urls.update(extract_contact_page_links(
            discovery_html,
            discovery_url,
            limit=config.MAX_CONTACT_PAGES,
            allow_official_subdomains=True,
        ))
        contact_urls.extend(site_mapper.balanced_urls(
            site_mapper.discover(discovery_html, discovery_url),
            [],
            config.MAX_CONTACT_PAGES,
        ))
    contact_urls.extend(discovered_contact_urls)
    contact_urls.extend(_sitemap_contact_urls(root, sitemap_urls))
    contact_urls.extend(urljoin(root, path) for path in config.CONTACT_PAGE_PATHS)
    # Use a queue so an about/corporate page can reveal a contact page that was
    # not linked from the homepage. This improves recall without adding search
    # API calls or leaving the official registrable domain.
    contact_queue = list(dict.fromkeys(contact_urls))
    queued = set(contact_queue)
    contact_render_attempts = 0
    contact_attempts = 0
    while contact_queue:
        contact_url = contact_queue.pop(0)
        if len(pages) >= config.MAX_CONTACT_PAGES + 1:
            break
        if contact_url in pages:
            continue
        if robots_parser is not None and not robots_parser.can_fetch(config.USER_AGENT, contact_url):
            continue
        if contact_attempts >= config.MAX_CONTACT_ATTEMPTS:
            runtime.record("crawler.contact_attempt_cap_reached")
            break
        contact_attempts += 1
        runtime.record("crawler.contact_url_attempts")
        contact_retrieval_method = "http"
        contact_html, contact_error = _try_fetch(contact_url)
        contact_meta = getattr(_FETCH_STATE, "last", {})
        tls_insecure = tls_insecure or bool(contact_meta.get("tls_insecure"))
        if contact_html and _looks_like_security_interstitial(contact_html):
            contact_html, contact_error = None, "security_interstitial"
        if not contact_html and contact_error:
            redirect_match = re.search(r"cross_domain_redirect:(https?://[^;\s]+)", contact_error)
            if redirect_match:
                redirect_url = redirect_match.group(1)
                redirect_host = urlparse(redirect_url).netloc.casefold().removeprefix("www.")
                if redirect_host in {"wa.me", "api.whatsapp.com", "web.whatsapp.com"}:
                    # The URL was linked by the official site. Preserve only
                    # that auditable URL; never crawl the external page body.
                    contact_html = f'<a href="{redirect_url}">WhatsApp</a>'
                    contact_retrieval_method = "official_link_reference"
                    contact_error = None
        if not contact_html and _renderable_fetch_error(contact_error) and (
            contact_url in discovered_contact_urls or contact_render_attempts < 2
        ):
            runtime.record("recovery.browser_attempts")
            contact_render_attempts += 1
            rendered_html, render_error = _try_render(contact_url)
            if rendered_html:
                runtime.record("recovery.browser_successes")
                contact_html = rendered_html
                contact_retrieval_method = "browser_render"
                contact_error = None
                contact_render_attempts = 2
            elif render_error and render_error != "js_fallback_disabled":
                errors.append(f"{contact_url}:{render_error}")
        if contact_html and _contact_page_needs_render(contact_html) and contact_render_attempts < 2:
            runtime.record("recovery.browser_attempts")
            contact_render_attempts += 1
            rendered_html, render_error = _try_render(contact_url)
            if rendered_html:
                runtime.record("recovery.browser_successes")
                contact_html = rendered_html
                contact_retrieval_method = "browser_render"
            elif render_error and render_error != "js_fallback_disabled":
                errors.append(f"{contact_url}:{render_error}")
        if contact_html:
            final_contact_url = contact_meta.get("final_url", contact_url)
            pages[final_contact_url] = contact_html
            if contact_retrieval_method == "browser_render":
                page_provenance[final_contact_url] = dict(getattr(_RENDER_STATE, "last", {}))
            else:
                page_provenance[final_contact_url] = {
                    "retrieval_method": (
                        "http_tls_unverified"
                        if contact_retrieval_method == "http" and contact_meta.get("tls_insecure")
                        else contact_retrieval_method
                    ),
                }
            nested_links = extract_contact_page_links(
                contact_html,
                final_contact_url,
                limit=config.MAX_CONTACT_PAGES,
                allow_official_subdomains=True,
            )
            nested_links.extend(
                item["url"] for item in site_mapper.discover(contact_html, final_contact_url)
            )
            for nested_url in nested_links:
                if nested_url not in queued and nested_url not in pages:
                    queued.add(nested_url)
                    discovered_contact_urls.add(nested_url)
                    # A contact link found on a real first-party page is more
                    # useful than the remaining guessed legacy paths.
                    contact_queue.insert(0, nested_url)
        elif contact_error:
            errors.append(f"{contact_url}:{contact_error}")

        # Once the official site has yielded both contact kinds, additional
        # guessed legacy paths add request cost but almost no useful recall.
        # Require a second page so homepage-only boilerplate does not stop the
        # crawl before a real contact page has been inspected.
        if len(pages) >= 2:
            collected_html = " ".join(pages.values())
            collected_contacts = extract_contact_records(collected_html, root)
            usable_phone = any(
                record.get("label") != "fax" for record in collected_contacts["phones"]
            )
            if collected_contacts["emails"] and usable_phone:
                runtime.record("crawler.contact_complete_early_stops")
                break

    document_urls = []
    for page_url, page_html in list(pages.items()):
        document_urls.extend(_document_links(page_html, page_url))
    for document_url in list(dict.fromkeys(document_urls))[: config.MAX_DOCUMENT_LINKS]:
        if robots_parser is not None and not robots_parser.can_fetch(config.USER_AGENT, document_url):
            continue
        if document_url.casefold().endswith((".vcf", ".vcard")):
            document_text, document_error = _try_fetch(document_url)
        else:
            document_text, document_error = _try_extract_pdf(document_url)
        if document_text:
            pages[document_url] = document_text
            page_provenance[document_url] = dict(getattr(_DOCUMENT_STATE, "last", {
                "retrieval_method": "document_unknown",
            }))
        elif document_error:
            errors.append(f"{document_url}:{document_error}")

    redirect_target = ""
    for error_value in [error, *errors]:
        match = re.search(r"cross_domain_redirect:(https?://[^;\s]+)", str(error_value or ""))
        if match:
            redirect_target = match.group(1)
            break
    return {
        "url": root,
        "pages": [
            {"url": page_url, "html": page_html, **page_provenance.get(
                page_url, {"retrieval_method": "unknown"},
            )}
            for page_url, page_html in pages.items()
        ],
        "error": "; ".join(errors[:5]) if not pages and errors else "",
        "tls_insecure": tls_insecure,
        "redirect_target": redirect_target,
        "crawl_profile": "full",
        "recovery_trace": recovery_trace,
    }


def fetch_site(url: str, contact_seed_urls: list[str] | None = None, profile: str = "full") -> dict:
    if profile not in {"identity", "full"}:
        raise ValueError(f"unsupported crawl profile: {profile}")
    mode = config.CRAWL_CACHE_MODE
    safe_seeds = _safe_contact_seed_urls(_with_scheme(url), contact_seed_urls)
    seed_key = "|".join(sorted(safe_seeds))
    cache_key = (
        f"{url}|pages={config.MAX_CONTACT_PAGES}|attempts={config.MAX_CONTACT_ATTEMPTS}"
        f"|sitemaps={config.MAX_SITEMAPS}"
    )
    if profile == "identity":
        cache_key = f"{url}|profile=identity|pages={config.MAX_IDENTITY_PAGES}"
    # Contact seeds do not affect the identity profile, so including them made
    # identical light crawls occupy several cache keys and harmed replay.
    if seed_key and profile == "full":
        cache_key = f"{cache_key}|seeds={seed_key}"
    if mode in {"use", "replay"}:
        cached = cache_store.load(
            config.CRAWL_CACHE_DIR, "site", cache_key,
            config.CRAWL_CACHE_TTL_DAYS, config.CRAWL_CACHE_SCHEMA_VERSION,
        )
        if cached is None and mode == "replay" and profile == "full":
            legacy_key = f"{url}|pages={config.MAX_CONTACT_PAGES}|sitemaps={config.MAX_SITEMAPS}"
            cached = cache_store.load(
                config.CRAWL_CACHE_DIR, "site", legacy_key,
                config.CRAWL_CACHE_TTL_DAYS, config.CRAWL_CACHE_SCHEMA_VERSION,
            )
        # Replay is a diagnostic mode and must never go live. Allow it to read
        # the previous crawl generation so ranking changes remain testable;
        # normal "use" mode deliberately skips this fallback and recrawls.
        if cached is None and mode == "replay" and profile == "full":
            legacy_key = f"{url}|pages={config.MAX_CONTACT_PAGES}|sitemaps={config.MAX_SITEMAPS}"
            for schema_version in dict.fromkeys((
                max(1, config.CRAWL_CACHE_SCHEMA_VERSION - 1),
                config.CACHE_SCHEMA_VERSION,
            )):
                cached = cache_store.load(
                    config.CRAWL_CACHE_DIR, "site", cache_key,
                    config.CRAWL_CACHE_TTL_DAYS, schema_version,
                )
                if cached is None:
                    cached = cache_store.load(
                        config.CRAWL_CACHE_DIR, "site", legacy_key,
                        config.CRAWL_CACHE_TTL_DAYS, schema_version,
                    )
                if cached is not None:
                    break
        if cached is not None:
            cached = dict(cached)
            cached_pages = list(cached.get("pages", []))
            safe_pages = [
                page for page in cached_pages
                if not _looks_like_security_interstitial(page.get("html", ""))
            ]
            cached["pages"] = safe_pages
            if any(not page.get("retrieval_method") for page in safe_pages):
                cached["provenance_status"] = "legacy_cache_unknown"
            if len(safe_pages) != len(cached_pages):
                runtime.record("cache.site.security_interstitial_rejected", len(cached_pages) - len(safe_pages))
                if not safe_pages:
                    cached["error"] = "cached_security_interstitial"
            cached["cache_status"] = "hit"
            return cached
        if mode == "replay":
            if profile == "identity":
                # Old runs only have full-crawl cache entries. Reusing them in
                # replay keeps reranking strictly offline while new live runs
                # receive the cheaper identity-first behavior.
                fallback = fetch_site(url, safe_seeds, profile="full")
                if fallback.get("pages"):
                    fallback = dict(fallback)
                    pages = list(fallback.get("pages", []))
                    identity_pages = [pages[0]]
                    identity_markers = (
                        "about", "hakkimizda", "kurumsal", "company", "corporate",
                        "kvkk", "aydinlatma", "gizlilik", "privacy", "legal",
                    )
                    identity_pages.extend(
                        page for page in pages[1:]
                        if any(marker in scorer.normalize_text(page.get("url", "")) for marker in identity_markers)
                    )
                    for page in pages[1:]:
                        if len(identity_pages) >= config.MAX_IDENTITY_PAGES + 1:
                            break
                        if page not in identity_pages:
                            identity_pages.append(page)
                    fallback["pages"] = identity_pages[: config.MAX_IDENTITY_PAGES + 1]
                    fallback["cache_status"] = "hit_full_fallback"
                    fallback["crawl_profile"] = "identity_replay_fallback"
                return fallback
            return {
                "url": _with_scheme(url), "pages": [],
                "error": "crawl_replay_cache_miss", "cache_status": "miss",
            }
    result = _fetch_site_live(url, safe_seeds, profile=profile)
    result["cache_status"] = "live"
    if mode in {"use", "refresh"}:
        cache_store.save(
            config.CRAWL_CACHE_DIR, "site", cache_key, result,
            config.CRAWL_CACHE_SCHEMA_VERSION,
        )
    return result
