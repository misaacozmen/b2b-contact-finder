"""Company tax identifiers from the fair list (Talimat 27).

A fair catalog can give each exhibitor's tax identifier (Poland: NIP).  A site
that shows the same identifier belongs to that company; a site that shows only
other identifiers belongs to a different company.  config.TAX_ID_FORMAT picks
the identifier format; an empty format turns all of this off.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import config
from modules import extractor, scorer

# A page that labels more identifiers than this is a list of companies.
MAX_PAGE_IDENTIFIERS = 2
LANGUAGE_SEGMENT_RE = re.compile(r"[a-z]{2}(?:[-_][a-z]{2})?")
# Subdomains a company uses for its own shop or language versions.
OWN_SUBDOMAINS = ("www", "sklep", "shop", "store", "b2b", "m")
# Legal and contact pages, where companies print their identifiers.
LEGAL_PATH_WORDS = (
    "regulamin", "polityka", "prywatn", "rodo", "kontakt", "contact", "privacy",
    "impressum", "imprint", "legal", "terms", "warunki", "cookies",
)


def _polish_nip(digits: str) -> bool:
    if len(digits) != 10:
        return False
    total = sum(int(digit) * weight for digit, weight in zip(digits, (6, 5, 7, 2, 3, 4, 5, 6, 7)))
    return total % 11 == int(digits[9])


FORMATS = {"PL_NIP": (10, _polish_nip)}


def normalize(value: object) -> str:
    """Digits of a valid identifier in the configured format, else ""."""
    spec = FORMATS.get(config.TAX_ID_FORMAT)
    if spec is None:
        return ""
    text = str(value or "").strip()
    prefix = re.match(r"[A-Za-z]{2}", text)
    if prefix and prefix.group(0).upper() != config.TARGET_COUNTRY:
        return ""
    digits = re.sub(r"\D", "", text[2:] if prefix else text)
    return digits if spec[1](digits) else ""


def from_metadata(metadata: dict | None) -> str:
    return normalize((metadata or {}).get("tax_id"))


def query(tax_id: str) -> str:
    return f'"{tax_id}"'


def _labelled(text: str) -> set[str]:
    spec = FORMATS.get(config.TAX_ID_FORMAT)
    if spec is None or not config.TAX_ID_LABELS:
        return set()
    labels = "|".join(re.escape(label) for label in config.TAX_ID_LABELS)
    pattern = re.compile(
        rf"(?i)(?<![\w-])(?:{labels})(?![\w-])[^\d]{{0,12}}((?:\d[\s.-]?){{{spec[0] - 1}}}\d)(?!\d)"
    )
    return {value for value in (normalize(match) for match in pattern.findall(text)) if value}


def _shows(text: str, tax_id: str) -> bool:
    return re.search(r"(?<!\d)" + r"[\s.-]?".join(tax_id) + r"(?!\d)", text) is not None


def company_page(url: str) -> bool:
    """A page where a company itself shows its identifier.

    Directories, mirrors and marketplaces show a company's identifier on a
    subdomain named after the company or on a deep profile page.  A company
    shows its own on the home page, a first-level page, or a legal or contact
    page, on its bare domain or a shop/language subdomain.
    """
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").casefold()
    domain = scorer.registrable_domain(url)
    if not host or not domain:
        return False
    if host != domain:
        label = host.removesuffix(f".{domain}")
        if "." in label or not (label in OWN_SUBDOMAINS or LANGUAGE_SEGMENT_RE.fullmatch(label)):
            return False
    segments = [segment for segment in parts.path.casefold().split("/") if segment]
    if segments and LANGUAGE_SEGMENT_RE.fullmatch(segments[0]):
        segments = segments[1:]
    return len(segments) <= 1 or any(word in parts.path.casefold() for word in LEGAL_PATH_WORDS)


def site_evidence(pages: list[dict], tax_id: str) -> dict:
    """The first company page that shows the identifier, and how many other
    identifiers the company pages label."""
    match_url = ""
    others: set[str] = set()
    for page in pages:
        if not company_page(page.get("url", "")):
            continue
        text = extractor._visible_text(page.get("html", "") or "")
        labelled = _labelled(text)
        if len(labelled) > MAX_PAGE_IDENTIFIERS:
            continue
        if not match_url and _shows(text, tax_id):
            match_url = str(page.get("url", "") or "")
        others |= labelled - {tax_id}
    return {"match_url": match_url, "other_count": len(others)}


def evaluation_reasons(metadata: dict | None, pages: list[dict]) -> list[str]:
    """tax_id_match:<page> when the site shows the firm's identifier;
    a context conflict when it shows only other identifiers."""
    tax_id = from_metadata(metadata)
    if not tax_id:
        return []
    evidence = site_evidence(pages, tax_id)
    if evidence["match_url"]:
        return [f"tax_id_match:{evidence['match_url']}"]
    if evidence["other_count"]:
        return [f"context_conflict:tax_id:other={evidence['other_count']}"]
    return []
