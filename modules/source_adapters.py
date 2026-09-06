"""Source-specific discovery contracts for official exhibitor surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any

from modules import scorer
from modules.source_normalizer import is_website_label, normalize_url


ORGANIZER_HOSTS = {
    "texhibitionist.com", "zuchex.com", "hometex.com.tr",
    "ambiente.messefrankfurt.com", "exhibitorsearch.messefrankfurt.com",
    "api.messefrankfurt.com", "informa.com", "tobb.org.tr", "xing.com",
}
REJECTED_HOSTS = {
    "apps.apple.com", "play.google.com", "facebook.com", "instagram.com",
    "linkedin.com", "twitter.com", "x.com", "youtube.com", "pinterest.com",
    "xing.com", "informa.com", "tobb.org.tr",
}
ASSET_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp")


@dataclass(frozen=True)
class AdapterResult:
    company_site_candidates: tuple[dict[str, Any], ...]
    exhibitor_identity_fields: dict[str, Any]
    listed_contacts: dict[str, Any]
    rejected_links: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]


def classify_link(url: str, *, label: str = "", company_name: str = "") -> dict[str, Any]:
    raw = str(url or "").strip()
    normalized = normalize_url(raw)
    candidate = normalized["normalized_value"]
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    reason = ""
    if normalized["status"] != "present" or not host:
        reason = normalized["rejection_reason"] or "invalid_url"
    elif host in REJECTED_HOSTS:
        reason = "non_company_host"
    elif host in ORGANIZER_HOSTS:
        reason = "organizer_or_source_host"
    elif any(parsed.path.casefold().endswith(suffix) for suffix in ASSET_SUFFIXES):
        reason = "asset_url"
    elif host in {"-", "www"} or raw.casefold() in {"http://-", "https://-", "-"}:
        reason = "placeholder_url"
    explicit = is_website_label(label)
    domain_match = bool(company_name and scorer.public_brand_domain_match(company_name, candidate))
    if not reason and not explicit and not domain_match:
        reason = "unlabelled_external_link"
    return {
        "url": candidate or raw,
        "label": str(label or "").strip(),
        "explicit_website": explicit,
        "company_domain_match": domain_match,
        "role": "company_candidate" if not reason else "unknown",
        "reason": reason,
    }


class SourceAdapter:
    source: str

    def parse(self, record: dict[str, Any]) -> AdapterResult:
        raise NotImplementedError


class _StructuredAdapter(SourceAdapter):
    def parse(self, record: dict[str, Any]) -> AdapterResult:
        website = str(record.get("listed_website") or "").strip()
        link = classify_link(website, label="Website", company_name=str(record.get("display_name") or record.get("legal_name") or "")) if website else None
        rejected = () if link is None or link["role"] == "company_candidate" else (link,)
        candidates = () if link is None or link["role"] != "company_candidate" else (link,)
        return AdapterResult(
            company_site_candidates=candidates,
            exhibitor_identity_fields={key: record.get(key) for key in ("display_name", "legal_name", "brand", "listed_address") if record.get(key)},
            listed_contacts={key: record.get(key) for key in ("listed_email", "listed_phone") if record.get(key)},
            rejected_links=rejected,
            evidence=tuple(record.get("evidence", ())),
        )


class TexhibitionDetailAdapter(_StructuredAdapter):
    source = "texhibition_2026"


class ZuchexListingAdapter(_StructuredAdapter):
    source = "zuchex_2026"


class HometexDetailAdapter(_StructuredAdapter):
    source = "hometex_2026"


class AmbienteApiAdapter(_StructuredAdapter):
    source = "ambiente_2026"


SOURCE_ADAPTERS: dict[str, SourceAdapter] = {
    adapter.source: adapter
    for adapter in (
        TexhibitionDetailAdapter(), ZuchexListingAdapter(),
        HometexDetailAdapter(), AmbienteApiAdapter(),
    )
}


def adapter_for_source(source: str) -> SourceAdapter:
    try:
        return SOURCE_ADAPTERS[str(source).strip()]
    except KeyError as exc:
        raise ValueError(f"no source adapter registered for {source!r}") from exc
