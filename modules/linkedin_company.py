"""Last-chance company identity corroboration via Bright Data LinkedIn data."""

from __future__ import annotations

import json
from threading import Lock
from urllib.parse import quote_plus, urlparse

import requests

import config
from modules import runtime, scorer


_CACHE: dict[tuple[str, str], dict | None] = {}
_PROFILE_CACHE: dict[str, dict | None] = {}
_LOCK = Lock()


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


def _reserve(kind: str) -> bool:
    if not runtime.reserve_api(
        "linkedin_company", config.LINKEDIN_COMPANY_REQUEST_BUDGET
    ):
        return False
    runtime.record(f"api.linkedin_company.{kind}_requests")
    return True


def _response_json(response: requests.Response):
    try:
        data = response.json()
    except ValueError:
        data = json.loads(response.text)
    if isinstance(data, dict) and isinstance(data.get("body"), str):
        data = json.loads(data["body"])
    return data


def _find_company_url(company: str) -> str:
    if not _reserve("serp"):
        return ""
    # LinkedIn often hides the company website from indexed snippets. Adding
    # the candidate domain to the query therefore suppresses otherwise exact
    # company-page results; identity is checked against the returned profile
    # name and its scraper-provided website below instead.
    query = f'site:linkedin.com/company "{company}"'
    search_url = (
        f"https://{config.BRIGHTDATA_GOOGLE_DOMAIN}/search"
        f"?q={quote_plus(query)}&hl={config.BRIGHTDATA_GOOGLE_HL}"
        f"&gl={config.BRIGHTDATA_GOOGLE_GL}"
    )
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
    if not isinstance(data, dict):
        return ""
    organic = data.get("organic") or data.get("organic_results") or data.get("results") or []
    for item in organic:
        url = _company_url(item.get("link") or item.get("url") or "")
        observed = " ".join((str(item.get("title", "")), str(item.get("description", "")), str(item.get("snippet", ""))))
        if url and scorer.business_name_identity_match(company, observed):
            return url
    return ""


def _scrape(linkedin_url: str):
    if not _reserve("scrape"):
        return None
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
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        rows = data.get("data") or data.get("results")
        if isinstance(rows, list):
            return rows[0] if rows else None
        return data
    return None


def _resolved_website(website: str) -> str:
    """Resolve LinkedIn campaign short-links before comparing domains."""
    if not scorer.is_valid_hostname(website):
        return website
    try:
        runtime.record("api.linkedin_company.website_redirect_requests")
        response = requests.get(
            website, allow_redirects=True, stream=True, timeout=20
        )
        response.close()
        return str(response.url or website)
    except requests.RequestException:
        return website


def corroborate(company: str, evaluation: dict) -> dict | None:
    """Return verified evidence only when LinkedIn names the candidate website."""
    if not (
        config.ENABLE_LINKEDIN_COMPANY_LOOKUP
        and config.BRIGHTDATA_API_KEY
        and config.LINKEDIN_COMPANY_DATASET_ID
        and config.SEARCH_CACHE_MODE != "replay"
    ):
        return None
    candidate_url = evaluation.get("candidate", {}).get("url", "")
    domain = scorer.normalize_domain(candidate_url)
    if not domain:
        return None
    company_key = scorer.normalize_text(company)
    key = (company_key, scorer.registrable_domain(domain))
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
    return result
