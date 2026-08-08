"""Map high-value pages while staying inside a verified site family."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from modules import scorer


PAGE_MARKERS = {
    "contact": ("contact", "iletisim", "bize-ulas", "bize ulas", "kontakt"),
    "legal": ("kvkk", "aydinlatma", "legal", "imprint", "ticari-bilgi", "sirket-bilgi"),
    "privacy": ("privacy", "gizlilik", "data-protection", "veri-koruma"),
    "terms": (
        "terms", "kullanim-kosul", "kullanim sart", "sozlesme",
        "mesafeli-satis", "distance-sales", "contract",
    ),
    "locations": ("location", "lokasyon", "office", "ofis", "sube", "branch", "factory", "fabrika"),
    "distributors": ("distributor", "distributorler", "bayi", "dealer", "temsilci"),
    "about": ("about", "hakkimizda", "kurumsal", "company", "corporate"),
    "catalog": ("catalog", "catalogue", "katalog", "brochure", "brosur"),
}

KIND_ORDER = ("contact", "legal", "privacy", "locations", "about", "distributors", "terms", "catalog")


def classify(value: str) -> str:
    normalized = scorer.normalize_text(value).replace(" ", "-")
    for kind in KIND_ORDER:
        if any(marker.replace(" ", "-") in normalized for marker in PAGE_MARKERS[kind]):
            return kind
    return ""


def discover(html: str, base_url: str, include_documents: bool = True) -> list[dict]:
    base_host = urlparse(base_url).netloc.casefold()
    found: list[dict] = []
    seen: set[str] = set()
    soup = BeautifulSoup(html or "", "html.parser")

    def add(raw_url: str, label: str = "") -> None:
        url = urldefrag(urljoin(base_url, raw_url or ""))[0]
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return
        if not scorer.same_registrable_domain(parsed.netloc.casefold(), base_host):
            return
        kind = classify(f"{url} {label}")
        if not kind or (not include_documents and kind == "catalog"):
            return
        if parsed.path.casefold().endswith(".pdf") and not include_documents:
            return
        if url not in seen:
            found.append({"url": url, "kind": kind})
            seen.add(url)

    for link in soup.select("a[href], link[href]"):
        label = " ".join(filter(None, (
            link.get_text(" ", strip=True),
            str(link.get("title", "")),
            str(link.get("aria-label", "")),
            " ".join(link.get("rel", [])),
        )))
        add(str(link.get("href", "")), label)

    def walk_json(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in {"url", "contenturl", "mainentityofpage"}:
                    if isinstance(item, str):
                        add(item, str(value.get("name", "")))
                    elif isinstance(item, dict):
                        walk_json(item)
                elif isinstance(item, (dict, list)):
                    walk_json(item)
        elif isinstance(value, list):
            for item in value:
                walk_json(item)

    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            walk_json(json.loads(script.string or script.get_text() or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    # Hydration/config scripts often contain quoted first-party routes even
    # when no anchor was server-rendered. Restrict extraction to paths whose
    # own text classifies as a high-value evidence scope.
    for route in re.findall(
        r"""["']((?:https?://[^"'<>\\\s]+|/[^"'<>\\\s]+))["']""",
        html or "",
    ):
        if classify(route):
            add(route)
    return found


def balanced_urls(
    discovered: list[dict],
    fallback_urls: list[str],
    limit: int,
    preferred_kinds: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    """Pick different evidence scopes before taking duplicate page types."""
    buckets: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    for item in discovered:
        url = urldefrag(str(item.get("url", "")))[0]
        kind = str(item.get("kind", "")) or classify(url)
        if url and kind and url not in seen:
            buckets[kind].append(url)
            seen.add(url)
    for url in fallback_urls:
        url = urldefrag(url)[0]
        kind = classify(url)
        if url and kind and url not in seen:
            buckets[kind].append(url)
            seen.add(url)

    selected: list[str] = []
    preferred = tuple(
        kind for kind in (preferred_kinds or ())
        if kind in KIND_ORDER
    )
    ordered_kinds = (*preferred, *(
        kind for kind in KIND_ORDER if kind not in preferred
    ))
    for kind in ordered_kinds:
        if buckets[kind]:
            selected.append(buckets[kind].pop(0))
            if len(selected) >= limit:
                return selected
        if len(preferred) > 1 and kind == preferred[1]:
            primary = preferred[0]
            if buckets[primary]:
                selected.append(buckets[primary].pop(0))
                if len(selected) >= limit:
                    return selected
    for kind in ordered_kinds:
        for url in buckets[kind]:
            selected.append(url)
            if len(selected) >= limit:
                return selected
    return selected
