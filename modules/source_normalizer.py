"""Shared, decision-free normalization for first-party source fields."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


WEBSITE_LABELS = frozenset({
    "web",
    "website",
    "web site",
    "web sitesi",
    "internet sitesi",
    "official website",
    "official site",
})
_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_NAMES = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_label(value: object) -> str:
    """Normalize a source field label without applying any decision policy."""
    text = unicodedata.normalize("NFC", html.unescape(str(value or "")))
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.rstrip(":： ").casefold()


def is_website_label(value: object) -> bool:
    return normalize_label(value) in WEBSITE_LABELS


def _idna_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError:
        return host.casefold().rstrip(".")


def _tracking_free_query(query: str) -> str:
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        folded = key.casefold()
        if folded in _TRACKING_QUERY_NAMES or any(folded.startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES):
            continue
        pairs.append((key, value))
    return urlencode(sorted(pairs), doseq=True)


def normalize_url(value: object, *, source_url: str = "") -> dict[str, str]:
    """Return raw/canonical URL data; relative URLs require same source host."""
    raw = unicodedata.normalize("NFC", html.unescape(str(value or ""))).strip()
    result = {"raw_value": raw, "normalized_value": "", "status": "absent", "rejection_reason": ""}
    if not raw:
        return result
    candidate = raw
    was_relative = candidate.startswith("/")
    source = urlsplit(str(source_url or "").strip())
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif not urlsplit(candidate).scheme and not candidate.startswith("/"):
        candidate = "https://" + candidate
    elif candidate.startswith("/"):
        if not source.hostname:
            result.update(status="rejected", rejection_reason="relative_url_without_source_container")
            return result
        candidate = urljoin(str(source_url), candidate)
    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        result.update(status="rejected", rejection_reason="invalid_url")
        return result
    if any(character.isspace() for character in parsed.hostname):
        result.update(status="rejected", rejection_reason="invalid_host_whitespace")
        return result
    if was_relative and source.hostname and _idna_host(parsed.hostname) != _idna_host(source.hostname):
        result.update(status="rejected", rejection_reason="relative_url_crosses_source_host")
        return result
    host = _idna_host(parsed.hostname)
    try:
        port = parsed.port
    except ValueError:
        result.update(status="rejected", rejection_reason="invalid_port")
        return result
    netloc = host
    if port and not ((parsed.scheme.casefold() == "http" and port == 80) or (parsed.scheme.casefold() == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = parsed.path or ""
    scheme = "https" if parsed.scheme.casefold() in {"http", "https"} else parsed.scheme.casefold()
    canonical = urlunsplit((scheme, netloc, path, _tracking_free_query(parsed.query), ""))
    result.update(normalized_value=canonical, status="present")
    return result


def normalize_source_url(value: object, *, source_url: str = "") -> str:
    """Compatibility scalar returning only the accepted canonical URL."""
    return normalize_url(value, source_url=source_url)["normalized_value"]


def field_evidence(
    *,
    raw_value: object,
    normalized_value: object,
    label_raw: object,
    selector_or_json_pointer: str,
    source_url: str,
    response_bytes: bytes | None = None,
    status: str = "present",
    rejection_reason: str = "",
    observed_at: str | None = None,
) -> dict[str, str]:
    """Build the uniform provenance envelope used by source acquisition/review."""
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat()
    return {
        "raw_value": str(raw_value or ""),
        "normalized_value": str(normalized_value or ""),
        "label_raw": str(label_raw or ""),
        "label_normalized": normalize_label(label_raw),
        "selector_or_json_pointer": str(selector_or_json_pointer or ""),
        "source_url": str(source_url or ""),
        "response_sha256": sha256(response_bytes or b"").hexdigest(),
        "observed_at": str(observed_at),
        "status": str(status),
        "rejection_reason": str(rejection_reason or ""),
    }
