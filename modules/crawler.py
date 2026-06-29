from collections import OrderedDict
from urllib.parse import urljoin, urlparse

import requests

import config
from modules.extractor import extract_contact_page_link
from modules.utils import retry_with_backoff


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": config.USER_AGENT, "Accept-Language": "tr,en;q=0.8"})


def _with_scheme(url: str) -> str:
    return url if "://" in url else f"https://{url}"


@retry_with_backoff()
def _fetch(url: str) -> requests.Response:
    response = SESSION.get(
        url,
        timeout=config.REQUEST_TIMEOUT_SEC,
        allow_redirects=True,
        verify=True,
    )
    response.raise_for_status()
    return response


def _try_fetch(url: str) -> tuple[str | None, str | None]:
    try:
        response = _fetch(url)
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type and content_type:
            return None, f"unsupported_content_type:{content_type}"
        response.encoding = response.encoding or response.apparent_encoding
        return response.text, None
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "http_error"
        return None, f"http_{status_code}"
    except requests.exceptions.SSLError:
        return None, "ssl_error"
    except requests.exceptions.ConnectionError:
        return None, "connection_error"
    except requests.exceptions.RequestException as exc:
        return None, exc.__class__.__name__.lower()


def fetch_site(url: str) -> dict:
    base_url = _with_scheme(url)
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    pages: OrderedDict[str, str] = OrderedDict()
    errors: list[str] = []

    html, error = _try_fetch(root)
    if html:
        pages[root] = html
        contact_link = extract_contact_page_link(html, root)
        if contact_link:
            contact_html, contact_error = _try_fetch(contact_link)
            if contact_html:
                pages[contact_link] = contact_html
            elif contact_error:
                errors.append(f"{contact_link}:{contact_error}")
    elif error:
        errors.append(f"{root}:{error}")

    for path in config.CONTACT_PAGE_PATHS:
        contact_url = urljoin(root, path)
        if contact_url in pages:
            continue
        contact_html, contact_error = _try_fetch(contact_url)
        if contact_html:
            pages[contact_url] = contact_html
        elif contact_error:
            errors.append(f"{contact_url}:{contact_error}")

    return {
        "url": root,
        "pages": [{"url": page_url, "html": page_html} for page_url, page_html in pages.items()],
        "error": "; ".join(errors[:5]) if not pages and errors else "",
    }

