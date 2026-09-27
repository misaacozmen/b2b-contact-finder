"""Customer/fair supplied reference values: normalization and blind views."""
from __future__ import annotations

import re
from copy import deepcopy
from urllib.parse import urlsplit

from modules import phone, scorer

REFERENCE_KEYS = (
    "website", "listed_website", "listed_phone", "listed_email",
    "profile_url", "listing_url", "source_detail_url", "source_detail_content_sha256",
)
_HOST_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_LEAD_JUNK = "-*•·.,;:'\"<>() \t"


def normalize_reference_url(value: object) -> tuple[str, str]:
    """Return (url, status) with status in OK/FIXED/EMPTY/INVALID/EXCLUDED_DOMAIN."""
    raw = str(value or "").strip()
    if not raw:
        return "", "EMPTY"
    text = raw.strip(_LEAD_JUNK)
    schemes = list(re.finditer(r"https?://", text, flags=re.IGNORECASE))
    text = text[schemes[-1].start():] if schemes else f"https://{text.lstrip('/')}"
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").strip(".").lstrip("-.").casefold()
    except ValueError:
        return "", "INVALID"
    if not host or any(char.isspace() for char in host):
        return "", "INVALID"
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return "", "INVALID"
    if not _HOST_RE.fullmatch(ascii_host):
        return "", "INVALID"
    if scorer.is_excluded_domain(scorer.normalize_domain(ascii_host)):
        return "", "EXCLUDED_DOMAIN"
    scheme = (parts.scheme or "https").casefold()
    path = parts.path if parts.path not in {"", "/"} else "/"
    url = f"{scheme}://{host}{path}"
    unchanged = raw.rstrip("/").casefold() == url.rstrip("/").casefold()
    return url, "OK" if unchanged else "FIXED"


def normalize_reference_phone(value: object) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return "", "EMPTY"
    normalized = phone.normalize_phone(raw)
    return (normalized, "OK") if normalized else ("", "INVALID")


def reference_website(record: dict | None) -> str:
    """Customer 'website' column wins over the fair 'listed_website' column."""
    record = record or {}
    for key in ("website", "listed_website"):
        url, status = normalize_reference_url(record.get(key))
        if status in {"OK", "FIXED"}:
            return url
    return ""


def strip_references(metadata: dict | None) -> dict:
    """Reference-blind view used by the independent search stage."""
    blind = deepcopy(metadata or {})
    source_hosts = sorted({
        scorer.registrable_domain(str(value))
        for key in ("listing_url", "profile_url", "source_detail_url")
        for value in [(metadata or {}).get(key)]
        if str(value or "").strip() and scorer.registrable_domain(str(value))
    })
    for key in REFERENCE_KEYS:
        if key in blind:
            blind[key] = ""
    identity = blind.get("target_identity")
    if isinstance(identity, dict):
        for key in REFERENCE_KEYS:
            if key in identity:
                identity[key] = ""
        identity["source_evidence"] = []
    blind["source_evidence"] = ""
    blind["_reference_blind"] = True
    blind["_source_hosts"] = source_hosts
    return blind
