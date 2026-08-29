"""Last-chance company identity corroboration via Bright Data LinkedIn data."""

from __future__ import annotations

import json
from threading import Lock
from urllib.parse import quote_plus, urljoin, urlparse

import requests

import config
from modules import cache_store, network_guard, runtime, scorer


_CACHE: dict[tuple[str, str], dict | None] = {}
_PROFILE_CACHE: dict[str, dict | None] = {}


class ProviderText(str):
    """String-compatible LinkedIn provider result with ledger metadata."""

    def __new__(cls, value: str = "", *, state: str = "EMPTY", reason: str = "", call_ids: tuple[str, ...] = ()):
        result = str.__new__(cls, value)
        result.result_state = str(state)
        result.result_reason = str(reason)
        result.call_ids = tuple(str(value) for value in call_ids if value)
        return result


class ProviderRecord(dict):
    """Dict-compatible LinkedIn provider result with ledger metadata."""

    def __init__(self, *args, state: str = "EMPTY", reason: str = "", call_ids: tuple[str, ...] = (), **kwargs):
        super().__init__(*args, **kwargs)
        self.result_state = str(state)
        self.result_reason = str(reason)
        self.call_ids = tuple(str(value) for value in call_ids if value)
_LOCK = Lock()
_WEBSITE_SESSION = network_guard.harden_session(requests.Session())


def reset() -> None:
    with _LOCK:
        _CACHE.clear()
        _PROFILE_CACHE.clear()


def _company_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return ""
    if not path.lower().startswith("/company/"):
        return ""
    return f"https://www.linkedin.com{path}"


def _declared_linkedin_url(evaluation: dict) -> str:
    structured = evaluation.get("structured_identity", {})
    candidate_structured = evaluation.get("candidate", {}).get(
        "_structured_identity", {}
    )
    for value in (
        *structured.get("same_as", []),
        *candidate_structured.get("same_as", []),
    ):
        url = _company_url(value)
        if url:
            return url
    return ""


def _reserve(kind: str, request: object = None):
    reservation = runtime.reserve_api("linkedin", operation=kind, request_fingerprint=runtime.request_fingerprint("linkedin", kind, request or {}))
    if not reservation:
        return reservation
    runtime.record(f"api.linkedin_company.{kind}_requests")
    return reservation


def _response_json(response: requests.Response):
    try:
        data = response.json()
    except ValueError:
        data = json.loads(response.text)
    if isinstance(data, dict) and isinstance(data.get("body"), str):
        data = json.loads(data["body"])
    return data


def _find_company_url(company: str) -> str:
    query = f'site:linkedin.com/company "{company}"'
    reservation = _reserve("serp", {"query": query})
    if not reservation:
        rejected = runtime.rejected_provider_result(reservation)
        return ProviderText("", state=rejected.result_state, reason=rejected.result_reason, call_ids=rejected.call_ids)
    # LinkedIn often hides the company website from indexed snippets. Adding
    # the candidate domain to the query therefore suppresses otherwise exact
    # company-page results; identity is checked against the returned profile
    # name and its scraper-provided website below instead.
    search_url = (
        f"https://{config.BRIGHTDATA_GOOGLE_DOMAIN}/search"
        f"?q={quote_plus(query)}&hl={config.BRIGHTDATA_GOOGLE_HL}"
        f"&gl={config.BRIGHTDATA_GOOGLE_GL}"
    )
    try:
        response = requests.post(
            config.BRIGHTDATA_ENDPOINT,
            json={
                "zone": config.BRIGHTDATA_ZONE,
                "url": search_url,
                "format": "json",
                "country": config.BRIGHTDATA_COUNTRY,
            },
            headers={
                "Authorization": f"Bearer {config.BRIGHTDATA_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=config.BRIGHTDATA_TIMEOUT_SEC,
        )
        response.raise_for_status()
        data = _response_json(response)
        runtime.complete_api(reservation, "DONE")
    except BaseException as exc:
        state = "UNKNOWN" if runtime.is_unknown_transport_error(exc) else "FAILED"
        runtime.complete_api(reservation, state)
        return ProviderText("", state=state, reason=f"{type(exc).__name__}:{exc}", call_ids=(getattr(reservation, "call_id", ""),))
    if not isinstance(data, dict):
        return ""
    organic = data.get("organic") or data.get("organic_results") or data.get("results") or []
    for item in organic:
        url = _company_url(item.get("link") or item.get("url") or "")
        observed = " ".join((str(item.get("title", "")), str(item.get("description", "")), str(item.get("snippet", ""))))
        if url and scorer.business_name_identity_match(company, observed):
            return ProviderText(url, state="COMPLETED", reason="profile_match", call_ids=(getattr(reservation, "call_id", ""),))
    return ProviderText("", state="EMPTY", reason="no_profile_match", call_ids=(getattr(reservation, "call_id", ""),))


def _scrape(linkedin_url: str):
    reservation = _reserve("scrape", {"url": linkedin_url})
    if not reservation:
        rejected = runtime.rejected_provider_result(reservation)
        return ProviderRecord(state=rejected.result_state, reason=rejected.result_reason, call_ids=rejected.call_ids)
    try:
        response = requests.post(
            config.LINKEDIN_COMPANY_ENDPOINT,
            params={
                "dataset_id": config.LINKEDIN_COMPANY_DATASET_ID,
                "format": "json",
                "include_errors": "true",
            },
            json={"input": [{"url": linkedin_url}]},
            headers={
                "Authorization": f"Bearer {config.BRIGHTDATA_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=config.LINKEDIN_COMPANY_TIMEOUT_SEC,
        )
        response.raise_for_status()
        data = _response_json(response)
        runtime.complete_api(reservation, "DONE")
    except BaseException as exc:
        state = "UNKNOWN" if runtime.is_unknown_transport_error(exc) else "FAILED"
        runtime.complete_api(reservation, state)
        return ProviderRecord(state=state, reason=f"{type(exc).__name__}:{exc}", call_ids=(getattr(reservation, "call_id", ""),))
    if isinstance(data, list):
        return ProviderRecord(data[0], state="COMPLETED", reason="record", call_ids=(getattr(reservation, "call_id", ""),)) if data else ProviderRecord(state="EMPTY", reason="empty_response", call_ids=(getattr(reservation, "call_id", ""),))
    if isinstance(data, dict):
        rows = data.get("data") or data.get("results")
        if isinstance(rows, list):
            return ProviderRecord(rows[0], state="COMPLETED", reason="record", call_ids=(getattr(reservation, "call_id", ""),)) if rows else ProviderRecord(state="EMPTY", reason="empty_response", call_ids=(getattr(reservation, "call_id", ""),))
        return ProviderRecord(data, state="COMPLETED", reason="record", call_ids=(getattr(reservation, "call_id", ""),))
    return ProviderRecord(state="EMPTY", reason="empty_response", call_ids=(getattr(reservation, "call_id", ""),))


def _resolved_website(website: str) -> str:
    """Resolve LinkedIn campaign short-links before comparing domains."""
    if not scorer.is_valid_hostname(website):
        return website
    current = website if "://" in website else f"https://{website}"
    redirect_statuses = {301, 302, 303, 307, 308}
    for redirect_count in range(config.MAX_HTTP_REDIRECTS + 1):
        allowed, reason = network_guard.validate_public_http_url(current)
        if not allowed:
            runtime.record("api.linkedin_company.website_redirect_blocked")
            return website
        runtime.record("api.linkedin_company.website_redirect_requests")
        try:
            response = _WEBSITE_SESSION.get(
                current, allow_redirects=False, stream=True, timeout=20
            )
        except requests.RequestException:
            return website
        try:
            if response.status_code not in redirect_statuses:
                response.raise_for_status()
                return current
            location = response.headers.get("location", "").strip()
            if not location:
                return current
            if redirect_count >= config.MAX_HTTP_REDIRECTS:
                runtime.record("api.linkedin_company.website_redirect_limit")
                return website
            current = urljoin(current, location)
        except requests.RequestException:
            return website
        finally:
            response.close()
    return website


def corroborate(company: str, evaluation: dict) -> dict | None:
    """Return verified evidence only when LinkedIn names the candidate website."""
    if not (
        config.ENABLE_LINKEDIN_COMPANY_LOOKUP
        and config.BRIGHTDATA_API_KEY
        and config.LINKEDIN_COMPANY_DATASET_ID
    ):
        return None
    candidate_url = evaluation.get("candidate", {}).get("url", "")
    domain = scorer.normalize_domain(candidate_url)
    if not domain:
        return None
    company_key = scorer.normalize_text(company)
    key = (company_key, scorer.registrable_domain(domain))
    persistent_key = "|".join(key)
    if config.SEARCH_CACHE_MODE in {"use", "replay"}:
        cached = cache_store.load(
            config.SEARCH_CACHE_DIR, "linkedin_company", persistent_key,
            config.SEARCH_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION,
        )
        if isinstance(cached, dict):
            with _LOCK:
                _CACHE[key] = cached
            runtime.record("api.linkedin_company.persistent_cache_hits")
            return dict(cached)
        if config.SEARCH_CACHE_MODE == "replay":
            return None
    with _LOCK:
        if key in _CACHE:
            runtime.record("api.linkedin_company.cache_hits")
            return _CACHE[key]
        profile_cached = company_key in _PROFILE_CACHE
        profile_evidence = _PROFILE_CACHE.get(company_key)

    declared_url = _declared_linkedin_url(evaluation)
    if not profile_cached:
        runtime.record("api.linkedin_company.lookup_attempts")
        try:
            linkedin_url = declared_url or _find_company_url(company)
            if not linkedin_url:
                runtime.record("api.linkedin_company.not_found")
                profile_evidence = None
            else:
                record = _scrape(linkedin_url)
                website = str((record or {}).get("website", "") or "")
                resolved_website = website
                profile_evidence = {
                    "source": "brightdata_linkedin_company",
                    "linkedin_url": linkedin_url,
                    "linkedin_name": str((record or {}).get("name", "") or ""),
                    "website": website,
                    "resolved_website": resolved_website,
                    "website_domain": scorer.normalize_domain(resolved_website),
                    "industry": (
                        (record or {}).get("industries")
                        or (record or {}).get("industry")
                        or ""
                    ),
                    "company_size": (record or {}).get("company_size") or "",
                    "country_code": (record or {}).get("country_code") or "",
                    "provider_result": getattr(record, "result_state", "COMPLETED"),
                    "provider_result_reason": getattr(record, "result_reason", "record"),
                    "provider_call_ids": list(getattr(record, "call_ids", ())),
                }
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
            runtime.record("api.linkedin_company.provider_failures")
            profile_evidence = None
        with _LOCK:
            _PROFILE_CACHE[company_key] = profile_evidence

    if not profile_evidence:
        result = None
    else:
        result = dict(profile_evidence)
        website_match = scorer.same_registrable_domain(
            result.get("resolved_website", ""), candidate_url
        )
        if result.get("website") and not website_match:
            resolved_website = _resolved_website(result["website"])
            result["resolved_website"] = resolved_website
            result["website_domain"] = scorer.normalize_domain(resolved_website)
            website_match = scorer.same_registrable_domain(
                resolved_website, candidate_url
            )
            with _LOCK:
                _PROFILE_CACHE[company_key] = {
                    key: value for key, value in result.items()
                    if key not in {"website_match", "name_match", "verified"}
                }
        name_match = scorer.business_name_identity_match(
            company, result.get("linkedin_name", "")
        )
        verified = bool(website_match and name_match)
        result.update({
            "website_match": website_match,
            "name_match": name_match,
            "verified": verified,
        })
        runtime.record(
            "api.linkedin_company.matches" if verified
            else "api.linkedin_company.mismatches"
        )
    with _LOCK:
        _CACHE[key] = result
    if result is not None and config.SEARCH_CACHE_MODE in {"use", "refresh"}:
        cache_store.save(
            config.SEARCH_CACHE_DIR, "linkedin_company", persistent_key,
            result, config.CACHE_SCHEMA_VERSION,
        )
    return result
