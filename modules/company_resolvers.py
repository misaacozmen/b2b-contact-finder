"""Cheap company-name to domain resolvers used only for candidate discovery.

Resolver output is deliberately not identity evidence.  A returned domain must
still pass the normal first-party crawl, ownership, country and ambiguity gates
before anything can be published.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import requests

import config
from modules import cache_store, runtime, scorer


LOGGER = logging.getLogger("contact_finder")
BRANDFETCH_SEARCH_URL = "https://api.brandfetch.io/v2/search/{company}"
HUNTER_DOMAIN_FINDER_URL = "https://api.hunter.io/v2/domain-finder"


def _safe_request_error(exc: Exception) -> str:
    """Prevent credentials embedded in provider URLs from reaching run logs."""
    return re.sub(
        r"([?&](?:api_key|access_token|token)=)[^&\s]+",
        r"\1[REDACTED]",
        str(exc),
        flags=re.IGNORECASE,
    )


def _cached(namespace: str, key: str):
    if config.SEARCH_CACHE_MODE not in {"use", "replay"}:
        return None
    return cache_store.load(
        config.SEARCH_CACHE_DIR, namespace, key,
        config.SEARCH_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION,
    )


def _save(namespace: str, key: str, value) -> None:
    if config.SEARCH_CACHE_MODE in {"use", "refresh"}:
        cache_store.save(
            config.SEARCH_CACHE_DIR, namespace, key, value,
            config.CACHE_SCHEMA_VERSION,
        )


def _clean_results(items, provider: str) -> list[dict]:
    cleaned: list[dict] = []
    seen: set[str] = set()
    for rank, item in enumerate(items or [], start=1):
        domain = scorer.normalize_domain(item.get("domain", ""))
        if (
            not scorer.is_valid_hostname(domain)
            or scorer.is_excluded_domain(domain)
            or scorer.is_foreign_country_domain(domain)
            or domain in seen
        ):
            continue
        seen.add(domain)
        cleaned.append({
            "provider": provider,
            "domain": domain,
            "resolved_name": (item.get("name") or item.get("company_name") or "").strip(),
            "rank": rank,
            "claimed": bool(item.get("claimed", False)),
        })
    return cleaned[: max(config.COMPANY_RESOLVER_MAX_RESULTS, 1)]


def _name_compatible(company: str, item: dict) -> bool:
    """Reject resolver hits that do not share a defensible brand anchor.

    Resolver APIs may return a syntactically valid but unrelated company for a
    legal-name query.  Their output is discovery-only, but filtering obvious
    name mismatches here also keeps crawl budgets away from unrelated domains.
    Short tokens are deliberately insufficient unless the domain itself is an
    exact public-brand match; long exact brand anchors remain useful for legal
    name/public brand variants.
    """
    domain = item.get("domain", "")
    resolved_name = item.get("resolved_name", "")
    requested = scorer.primary_brand_tokens(company, limit=2)
    resolved_words = set(scorer.legal_identity_tokens(resolved_name))

    if scorer.public_brand_domain_match(company, domain):
        return True
    if not requested or not resolved_words:
        return False
    if len(requested[0]) >= 5 and requested[0] in resolved_words:
        return True
    return len(requested) >= 2 and all(token in resolved_words for token in requested[:2])


def brandfetch_domains(company: str) -> list[dict]:
    """Return Brandfetch name matches without treating them as authoritative."""
    if not (config.ENABLE_BRANDFETCH_DOMAIN_SEARCH and config.BRANDFETCH_CLIENT_ID and company):
        return []
    namespace = "brandfetch_domain_search"
    cached = _cached(namespace, company)
    if cached is not None:
        runtime.record("resolver.brandfetch.cache_hit")
        return _clean_results(cached, "brandfetch")
    if config.SEARCH_CACHE_MODE == "replay":
        runtime.record("resolver.brandfetch.replay_miss")
        return []
    if not runtime.reserve_api("brandfetch", config.BRANDFETCH_REQUEST_BUDGET):
        return []
    try:
        runtime.wait_for_request_slot()
        response = requests.get(
            BRANDFETCH_SEARCH_URL.format(company=quote(company, safe="")),
            params={"c": config.BRANDFETCH_CLIENT_ID},
            timeout=config.BRANDFETCH_TIMEOUT_SEC,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else []
        _save(namespace, company, items)
        return _clean_results(items, "brandfetch")
    except (requests.RequestException, ValueError, TypeError) as exc:
        runtime.record("resolver.brandfetch.error")
        LOGGER.warning("Brandfetch domain search failed for %s: %s", company, exc)
        return []


def hunter_domains(company: str) -> list[dict]:
    """Return Hunter Domain Finder matches; the beta endpoint is discovery-only."""
    if not (config.ENABLE_HUNTER_DOMAIN_FINDER and config.HUNTER_API_KEY and company):
        return []
    namespace = "hunter_domain_finder"
    cached = _cached(namespace, company)
    if cached is not None:
        runtime.record("resolver.hunter.cache_hit")
        return _clean_results(cached, "hunter_domain_finder")
    if config.SEARCH_CACHE_MODE == "replay":
        runtime.record("resolver.hunter.replay_miss")
        return []
    if not runtime.reserve_api("hunter_domain_finder", config.HUNTER_REQUEST_BUDGET):
        return []
    try:
        runtime.wait_for_request_slot()
        response = requests.get(
            HUNTER_DOMAIN_FINDER_URL,
            params={
                "company": company,
                "api_key": config.HUNTER_API_KEY,
                "limit": max(1, min(config.COMPANY_RESOLVER_MAX_RESULTS, 10)),
            },
            timeout=config.HUNTER_TIMEOUT_SEC,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        _save(namespace, company, items)
        return _clean_results(items, "hunter_domain_finder")
    except (requests.RequestException, ValueError, TypeError) as exc:
        runtime.record("resolver.hunter.error")
        LOGGER.warning(
            "Hunter Domain Finder failed for %s: %s",
            company,
            _safe_request_error(exc),
        )
        return []


def resolve_company_domains(company: str) -> list[dict]:
    """Union enabled cheap resolvers while retaining provider provenance."""
    combined: dict[str, dict] = {}
    for item in [*brandfetch_domains(company), *hunter_domains(company)]:
        if not _name_compatible(company, item):
            runtime.record("resolver.name_mismatch_rejected")
            continue
        runtime.record("resolver.name_compatible")
        domain = item["domain"]
        if domain not in combined:
            combined[domain] = {**item, "providers": [item["provider"]]}
            continue
        providers = combined[domain]["providers"]
        if item["provider"] not in providers:
            providers.append(item["provider"])
        combined[domain]["claimed"] = combined[domain]["claimed"] or item["claimed"]
    return list(combined.values())
