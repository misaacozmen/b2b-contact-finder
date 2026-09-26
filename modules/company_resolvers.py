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
from modules import cache_store, checkpoint, redaction, runtime, scorer


LOGGER = logging.getLogger("contact_finder")
BRANDFETCH_SEARCH_URL = "https://api.brandfetch.io/v2/search/{company}"
HUNTER_DOMAIN_FINDER_URL = "https://api.hunter.io/v2/domain-finder"


def _safe_request_error(exc: Exception) -> str:
    """Prevent credentials embedded in provider URLs from reaching run logs."""
    return redaction.redact_known_values(str(exc))


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
        if not isinstance(item, dict):
            runtime.record(f"resolver.{provider}.invalid_row")
            continue
        invalid_fields = [
            field for field in ("domain", "name", "company_name")
            if field in item and item.get(field) is not None
            and not isinstance(item.get(field), str)
        ]
        if "claimed" in item and item.get("claimed") is not None and not isinstance(item.get("claimed"), bool):
            invalid_fields.append("claimed")
        if invalid_fields:
            runtime.record(f"resolver.{provider}.invalid_row")
            continue
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
        return runtime.provider_result([], state="NOT_ENABLED", reason="provider_disabled")
    namespace = "brandfetch_domain_search"
    try:
        cached = _cached(namespace, company)
    except Exception:
        runtime.record("resolver.brandfetch.cache_read_error")
        cached = None
    if cached is not None:
        runtime.record("resolver.brandfetch.cache_hit")
        return runtime.provider_result(_clean_results(cached, "brandfetch"), state="CACHE_HIT", reason="cached")
    if config.SEARCH_CACHE_MODE == "replay":
        runtime.record("resolver.brandfetch.replay_miss")
        return runtime.provider_result([], state="REPLAY_MISS", reason="replay_cache_miss")
    reservation = runtime.reserve_api("brandfetch", operation="domain_search", request_fingerprint=runtime.request_fingerprint("brandfetch", "domain_search", {"company": company}))
    if not reservation:
        return runtime.rejected_provider_result(reservation)
    try:
        runtime.start_api(reservation)
        runtime.wait_for_request_slot()
        runtime.mark_api_http_started(reservation, 1)
        endpoint = BRANDFETCH_SEARCH_URL.format(company=quote(company, safe=""))
        envelope = runtime.transport_envelope(reservation, endpoint=endpoint, request_shape={"method": "GET", "company": company}, timeout=config.BRANDFETCH_TIMEOUT_SEC)
        response = runtime.invoke_paid_transport(envelope, lambda: requests.get(
            endpoint,
            params={"c": config.BRANDFETCH_CLIENT_ID},
            timeout=config.BRANDFETCH_TIMEOUT_SEC,
        ))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ValueError("Brandfetch response is not a list of objects")
        items = payload
        cleaned = _clean_results(items, "brandfetch")
        if items and not cleaned:
            raise ValueError("all Brandfetch resolver rows are invalid")
        runtime.complete_api(reservation, "DONE")
        try:
            _save(namespace, company, items)
        except Exception:
            runtime.record("resolver.brandfetch.cache_write_error")
        return runtime.provider_result(cleaned, state="COMPLETED" if cleaned else "EMPTY", reason="results" if cleaned else "empty_response", call_ids=(getattr(reservation, "call_id", ""),))
    except Exception as exc:
        if isinstance(exc, checkpoint.SchedulerInvariantError):
            raise
        state = "UNKNOWN" if runtime.is_unknown_transport_error(exc) else "FAILED"
        runtime.complete_api(reservation, state)
        runtime.record("resolver.brandfetch.error")
        LOGGER.warning(
            "Brandfetch domain search failed for %s: %s",
            company,
            _safe_request_error(exc),
        )
        return runtime.provider_result([], state=state, reason=f"{type(exc).__name__}:{exc}", call_ids=(getattr(reservation, "call_id", ""),))


def hunter_domains(company: str) -> list[dict]:
    """Return Hunter Domain Finder matches; the beta endpoint is discovery-only."""
    if not (config.ENABLE_HUNTER_DOMAIN_FINDER and config.HUNTER_API_KEY and company):
        return runtime.provider_result([], state="NOT_ENABLED", reason="provider_disabled")
    namespace = "hunter_domain_finder"
    try:
        cached = _cached(namespace, company)
    except Exception:
        runtime.record("resolver.hunter.cache_read_error")
        cached = None
    if cached is not None:
        runtime.record("resolver.hunter.cache_hit")
        return runtime.provider_result(_clean_results(cached, "hunter_domain_finder"), state="CACHE_HIT", reason="cached")
    if config.SEARCH_CACHE_MODE == "replay":
        runtime.record("resolver.hunter.replay_miss")
        return runtime.provider_result([], state="REPLAY_MISS", reason="replay_cache_miss")
    reservation = runtime.reserve_api("hunter", operation="domain_search", request_fingerprint=runtime.request_fingerprint("hunter", "domain_search", {"company": company}))
    if not reservation:
        return runtime.rejected_provider_result(reservation)
    # Central authorization is the last gate before the first physical call;
    # duplicate reservations above still inherit their durable result.
    if not runtime.paid_access_allowed("hunter"):
        return runtime.provider_result([], state="NOT_ENABLED", reason="paid_not_authorized", call_ids=(getattr(reservation, "call_id", ""),))
    try:
        runtime.start_api(reservation)
        runtime.wait_for_request_slot()
        runtime.mark_api_http_started(reservation, 1)
        endpoint = HUNTER_DOMAIN_FINDER_URL
        params = {
                "company": company,
                "api_key": config.HUNTER_API_KEY,
                "limit": max(1, min(config.COMPANY_RESOLVER_MAX_RESULTS, 10)),
            }
        envelope = runtime.transport_envelope(reservation, endpoint=endpoint, request_shape={"method": "GET", "company": company, "limit": params["limit"]}, timeout=config.HUNTER_TIMEOUT_SEC)
        response = runtime.invoke_paid_transport(envelope, lambda: requests.get(
            endpoint,
            params=params,
            timeout=config.HUNTER_TIMEOUT_SEC,
        ))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Hunter response is not a JSON object")
        items = payload.get("data", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Hunter response data is not a list of objects")
        cleaned = _clean_results(items, "hunter_domain_finder")
        if items and not cleaned:
            raise ValueError("all Hunter resolver rows are invalid")
        runtime.complete_api(reservation, "DONE")
        try:
            _save(namespace, company, items)
        except Exception:
            runtime.record("resolver.hunter.cache_write_error")
        return runtime.provider_result(cleaned, state="COMPLETED" if cleaned else "EMPTY", reason="results" if cleaned else "empty_response", call_ids=(getattr(reservation, "call_id", ""),))
    except Exception as exc:
        if isinstance(exc, checkpoint.SchedulerInvariantError):
            raise
        state = "UNKNOWN" if runtime.is_unknown_transport_error(exc) else "FAILED"
        runtime.complete_api(reservation, state)
        runtime.record("resolver.hunter.error")
        LOGGER.warning(
            "Hunter Domain Finder failed for %s: %s",
            company,
            _safe_request_error(exc),
        )
        return runtime.provider_result([], state=state, reason=f"{type(exc).__name__}:{exc}", call_ids=(getattr(reservation, "call_id", ""),))


def resolve_company_domains(
    company: str, *, validated_domains: set[str] | None = None,
    validated_identity_domains: set[str] | None = None,
    brandfetch_results: list[dict] | None = None,
    include_hunter: bool = True,
    candidate_evaluator=None,
) -> list[dict]:
    """Resolve discovery candidates; name compatibility is never sufficiency.

    Brandfetch output can suppress Hunter only after a caller supplies a
    domain that has already passed the first-party crawl and identity policy.
    """
    combined: dict[str, dict] = {}
    brandfetch = list(brandfetch_results) if brandfetch_results is not None else brandfetch_domains(company)
    resolver_items = list(brandfetch)
    # ``validated_domains`` is retained for compatibility but is deliberately
    # not sufficient: only a post-crawl identity decision may suppress Hunter.
    validated_domains = {scorer.normalize_domain(value) for value in (validated_domains or set())}
    validated_identity_domains = {
        scorer.normalize_domain(value) for value in (validated_identity_domains or set())
    }
    evaluated_domains = set(validated_identity_domains)
    if candidate_evaluator is not None:
        for item in brandfetch:
            try:
                if candidate_evaluator(item) is True:
                    evaluated_domains.add(scorer.normalize_domain(item.get("domain", "")))
            except Exception:
                runtime.record("resolver.candidate_evaluator_error")
    sufficient_brandfetch = [
        item for item in brandfetch
        if _name_compatible(company, item)
        and scorer.normalize_domain(item.get("domain", "")) in evaluated_domains
    ]
    if not sufficient_brandfetch and include_hunter:
        resolver_items.extend(hunter_domains(company))
        runtime.record("resolver.hunter.conditional_attempt")
    elif sufficient_brandfetch:
        runtime.record("resolver.hunter.skipped_validated_brandfetch")
    for item in resolver_items:
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
