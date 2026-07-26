import logging
import os
import json
import re
import socket
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import quote_plus
from urllib.parse import unquote, urlparse

import requests
from ddgs import DDGS
from ddgs.exceptions import DDGSException

import config
from modules import aliases, cache_store, company_resolvers, crawler, discovery_coverage, google_places, query_planner, runtime, scorer


LOGGER = logging.getLogger("contact_finder")
PREFERRED_BACKENDS = ["duckduckgo", "google", "brave", "yahoo", "yandex"]
FALLBACK_BACKENDS = ["mojeek", "grokipedia"]
_SOURCE_HEALTH_LOCK = threading.Lock()
_SOURCE_HEALTH: dict[str, dict] = {}
_CANDIDATE_HOST_LOCK = threading.Lock()
_CANDIDATE_HOST_COMPANIES: dict[str, set[str]] = {}
DISCOVERY_ONLY_ROLES = {"directory", "fair_profile", "shared_listing", "marketplace", "news"}
_ROLE_PRIORITY = {
    "company_candidate": 0, "unknown": 0, "news": 1, "marketplace": 2,
    "directory": 3, "shared_listing": 4, "fair_profile": 5,
}


class SearchBackendError(RuntimeError):
    pass


class BrightDataSearchError(RuntimeError):
    pass


class CandidateList(list):
    def __init__(self, values=(), trace: list[dict] | None = None, source_health: dict | None = None):
        super().__init__(values)
        self.trace = trace or []
        self.source_health = source_health or {}


class SearchResults(list):
    def __init__(self, values=(), cache_status: str = "unknown", provider: str = ""):
        super().__init__(values)
        self.cache_status = cache_status
        self.provider = provider


def reset_source_health() -> None:
    with _SOURCE_HEALTH_LOCK:
        _SOURCE_HEALTH.clear()


def reset_candidate_host_observations() -> None:
    with _CANDIDATE_HOST_LOCK:
        _CANDIDATE_HOST_COMPANIES.clear()


def _observe_candidate_host(company_name: str, domain: str) -> int:
    company_key = scorer.normalize_text(company_name).strip()
    with _CANDIDATE_HOST_LOCK:
        companies = _CANDIDATE_HOST_COMPANIES.setdefault(domain, set())
        if company_key:
            companies.add(company_key)
        return len(companies)


def _strongest_candidate_role(*roles: str) -> str:
    return max((role for role in roles if role), key=lambda role: _ROLE_PRIORITY.get(role, 0), default="unknown")


def _source_health_key(url: str) -> str:
    return scorer.normalize_domain(url)


def _source_health_snapshot(url: str) -> dict:
    key = _source_health_key(url)
    if not key:
        return {"status": "not_configured", "host": ""}
    with _SOURCE_HEALTH_LOCK:
        state = dict(_SOURCE_HEALTH.get(key, {}))
    return {"host": key, "status": state.get("status", "unknown"), **state}


def _record_source_health(url: str, status: str, http_status: int | None = None) -> dict:
    key = _source_health_key(url)
    if not key:
        return {"status": "not_configured", "host": ""}
    with _SOURCE_HEALTH_LOCK:
        state = _SOURCE_HEALTH.setdefault(key, {
            "host": key, "status": "unknown", "attempts": 0,
            "successes": 0, "server_errors": 0, "circuit_open": False,
        })
        if status != "circuit_open":
            state["attempts"] += 1
        if status == "available":
            state["successes"] += 1
            state["server_errors"] = 0
            state["circuit_open"] = False
        elif status == "server_error":
            state["server_errors"] += 1
            if state["server_errors"] >= config.SOURCE_PROFILE_MAX_SERVER_ERRORS:
                state["circuit_open"] = True
                status = "degraded"
        state["status"] = status
        if http_status is not None:
            state["last_http_status"] = http_status
        return dict(state)


def preflight_source_profiles(records: list[dict]) -> list[dict]:
    """Probe each distinct fair host once before parallel company processing."""
    first_profile_by_host: dict[str, str] = {}
    for record in records:
        profile_url = str(record.get("profile_url", "") or "").strip()
        host = _source_health_key(profile_url)
        if host and host not in first_profile_by_host:
            first_profile_by_host[host] = profile_url
    for profile_url in first_profile_by_host.values():
        runtime.record("source_profile.preflight_hosts")
        _profile_external_websites(profile_url)
    return [_source_health_snapshot(url) for url in first_profile_by_host.values()]


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    value = response.headers.get("Retry-After", "") if response is not None else ""
    if value:
        try:
            return max(0.0, min(float(value), 120.0))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, min((retry_at - datetime.now(timezone.utc)).total_seconds(), 120.0))
            except (TypeError, ValueError, OverflowError):
                pass
    return min((attempt + 1) * config.RETRY_BACKOFF_BASE_SEC, 60.0)


def _result_url(result: dict) -> str:
    return result.get("href") or result.get("url") or result.get("link") or ""


def _canonical_site_url(raw_url: str) -> str:
    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def _ddgs_text(query: str) -> list[dict]:
    had_non_error_response = False
    last_hard_error: Exception | None = None

    for backend in PREFERRED_BACKENDS + FALLBACK_BACKENDS:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=config.SEARCH_RESULTS_PER_QUERY, backend=backend))
            had_non_error_response = True
            if results:
                return results
            LOGGER.debug("DDGS backend '%s' returned 0 results for '%s'", backend, query)
        except DDGSException as exc:
            message = str(exc).lower()
            if "no results" in message:
                had_non_error_response = True
                LOGGER.debug("DDGS backend '%s' no results for '%s'", backend, query)
                continue
            LOGGER.debug("DDGS backend '%s' error for '%s': %s", backend, query, exc)
            last_hard_error = exc
        except Exception as exc:
            LOGGER.debug("DDGS backend '%s' failed for '%s': %s", backend, query, exc)
            last_hard_error = exc

    if last_hard_error and not had_non_error_response:
        raise SearchBackendError(f"All DDGS backends failed for '{query}': {last_hard_error}")
    return []


def _decode_brightdata_response(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            preview = response.text[:500].replace("\n", " ")
            raise BrightDataSearchError(f"Bright Data returned non-JSON body: {preview}") from exc

    if isinstance(data, dict) and isinstance(data.get("body"), str):
        try:
            data = json.loads(data["body"])
        except json.JSONDecodeError as exc:
            preview = data["body"][:500].replace("\n", " ")
            raise BrightDataSearchError(f"Bright Data returned non-JSON body: {preview}") from exc
    if not isinstance(data, dict):
        raise BrightDataSearchError(f"Bright Data returned unexpected payload type: {type(data).__name__}")
    return data


def _brightdata_post(url: str, **kwargs) -> requests.Response:
    if not runtime.reserve_api("brightdata", config.BRIGHTDATA_REQUEST_BUDGET):
        raise BrightDataSearchError("Bright Data run budget exhausted")
    runtime.wait_for_request_slot()
    return requests.post(url, timeout=config.BRIGHTDATA_TIMEOUT_SEC, **kwargs)


def _brightdata_text(query: str) -> list[dict]:
    if not config.BRIGHTDATA_API_KEY:
        raise BrightDataSearchError("BRIGHTDATA_API_KEY is not set")

    search_url = (
        f"https://{config.BRIGHTDATA_GOOGLE_DOMAIN}/search"
        f"?q={quote_plus(query)}"
        f"&hl={config.BRIGHTDATA_GOOGLE_HL}"
        f"&gl={config.BRIGHTDATA_GOOGLE_GL}"
    )
    payload = {
        "zone": config.BRIGHTDATA_ZONE,
        "url": search_url,
        "format": "json",
        "country": config.BRIGHTDATA_COUNTRY,
    }
    headers = {
        "Authorization": f"Bearer {config.BRIGHTDATA_API_KEY}",
        "Content-Type": "application/json",
    }
    response = None
    last_error: requests.RequestException | None = None
    for attempt in range(config.MAX_RETRIES + 2):
        try:
            response = _brightdata_post(
                config.BRIGHTDATA_ENDPOINT,
                json=payload,
                headers=headers,
            )
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= config.MAX_RETRIES + 1:
                raise BrightDataSearchError(f"Bright Data request timed out/failed after retries: {exc}") from exc
            time.sleep(_retry_delay(None, attempt))
    if response is None:
        raise BrightDataSearchError(f"Bright Data request failed: {last_error}")
    if response.status_code == 401:
        raise BrightDataSearchError("Bright Data authentication failed; check BRIGHTDATA_API_KEY")
    if response.status_code in {429, 500, 502, 503, 504}:
        last_detail = response.text[:500].replace("\n", " ")
        for attempt in range(config.MAX_RETRIES + 1):
            time.sleep(_retry_delay(response, attempt))
            response = _brightdata_post(
                config.BRIGHTDATA_ENDPOINT,
                json=payload,
                headers=headers,
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            last_detail = response.text[:500].replace("\n", " ")
        if response.status_code in {429, 500, 502, 503, 504}:
            raise BrightDataSearchError(
                f"Bright Data transient failure after retries: HTTP {response.status_code}; "
                f"zone={config.BRIGHTDATA_ZONE!r}; response={last_detail}"
            )
    if response.status_code >= 400:
        detail = response.text[:1000].replace("\n", " ")
        raise BrightDataSearchError(
            f"Bright Data request failed: HTTP {response.status_code}; "
            f"zone={config.BRIGHTDATA_ZONE!r}; url={search_url!r}; response={detail}"
        )
    decode_error: BrightDataSearchError | None = None
    data = None
    for parse_attempt in range(config.MAX_RETRIES + 1):
        try:
            data = _decode_brightdata_response(response)
            break
        except BrightDataSearchError as exc:
            decode_error = exc
            if parse_attempt >= config.MAX_RETRIES:
                raise
            time.sleep(_retry_delay(response, parse_attempt))
            response = _brightdata_post(
                config.BRIGHTDATA_ENDPOINT,
                json=payload,
                headers=headers,
            )
            if response.status_code >= 400:
                detail = response.text[:500].replace("\n", " ")
                raise BrightDataSearchError(f"Bright Data retry failed: HTTP {response.status_code}; response={detail}")
    if data is None:
        raise decode_error or BrightDataSearchError("Bright Data response could not be decoded")
    if os.getenv("BRIGHTDATA_DEBUG"):
        LOGGER.warning("Bright Data response keys: %s", sorted(data.keys()))
        LOGGER.warning("Bright Data response preview: %s", str(data)[:2000])
    organic = data.get("organic") or data.get("organic_results") or data.get("results") or []
    results = []
    for item in organic[: config.SEARCH_RESULTS_PER_QUERY]:
        link = item.get("link") or item.get("url") or ""
        if not link:
            continue
        results.append(
            {
                "href": link,
                "title": item.get("title", ""),
                "body": item.get("description", "") or item.get("snippet", ""),
            }
        )
    return results


def _search_text_live(query: str) -> list[dict]:
    if config.SEARCH_PROVIDER == "brightdata":
        return _brightdata_text(query)
    return _ddgs_text(query)


def _search_cache_key(query: str, provider: str | None = None) -> str:
    provider = (provider or config.SEARCH_PROVIDER).lower()
    return json.dumps(
        {
            "provider": provider,
            "query": query,
            "country": config.TARGET_COUNTRY,
            "count": config.SEARCH_RESULTS_PER_QUERY,
            "google_domain": config.BRIGHTDATA_GOOGLE_DOMAIN if provider == "brightdata" else "",
            "gl": config.BRIGHTDATA_GOOGLE_GL if provider == "brightdata" else "",
            "hl": config.BRIGHTDATA_GOOGLE_HL if provider == "brightdata" else "",
            "zone": config.BRIGHTDATA_ZONE if provider == "brightdata" else "",
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _search_text(query: str) -> list[dict]:
    """Search live or replay a provider response from the persistent cache."""
    mode = config.SEARCH_CACHE_MODE
    cache_key = _search_cache_key(query)
    if mode in {"use", "replay"}:
        cached = cache_store.load(
            config.SEARCH_CACHE_DIR,
            "serp",
            cache_key,
            config.SEARCH_CACHE_TTL_DAYS,
            config.CACHE_SCHEMA_VERSION,
        )
        if cached is not None:
            LOGGER.info("Search cache hit: %s", query)
            return SearchResults(cached, "cache_hit", config.SEARCH_PROVIDER)
        if mode == "replay":
            # Offline reranking must not depend on which provider is enabled
            # in the interactive prompt. This fallback never runs in a mode
            # that could make a live request.
            for provider in config.SEARCH_REPLAY_PROVIDER_FALLBACKS:
                if provider == config.SEARCH_PROVIDER:
                    continue
                cached = cache_store.load(
                    config.SEARCH_CACHE_DIR,
                    "serp",
                    _search_cache_key(query, provider),
                    config.SEARCH_CACHE_TTL_DAYS,
                    config.CACHE_SCHEMA_VERSION,
                )
                if cached is not None:
                    LOGGER.info(
                        "Search replay cache fallback hit: query=%s provider=%s",
                        query, provider,
                    )
                    return SearchResults(cached, "replay_fallback_hit", provider)
            LOGGER.warning("Search replay cache miss: %s", query)
            return SearchResults([], "replay_miss")

    results = _search_text_live(query)
    if mode in {"use", "refresh"}:
        cache_store.save(
            config.SEARCH_CACHE_DIR,
            "serp",
            cache_key,
            results,
            config.CACHE_SCHEMA_VERSION,
        )
    return SearchResults(results, "live", config.SEARCH_PROVIDER)


def _safe_search_text(query: str) -> list[dict]:
    """A single provider timeout must not discard every result for a firm."""
    try:
        return _search_text(query)
    except Exception as exc:
        LOGGER.warning("Search query failed; continuing with remaining queries: %s (%s)", query, exc)
        if config.SEARCH_PROVIDER == "brightdata":
            try:
                return SearchResults(_ddgs_text(query), "live_fallback", "ddgs")
            except Exception as fallback_exc:
                LOGGER.warning("Free search fallback also failed: %s (%s)", query, fallback_exc)
        return SearchResults([], "error", config.SEARCH_PROVIDER)


def _metadata_query_terms(metadata: dict | None) -> list[str]:
    return [config.METADATA_CONTEXTS[context]["query_term"] for context in scorer.metadata_contexts(metadata)[:2]]


def _query_priority(query: str) -> int:
    normalized = scorer.normalize_text(query)
    if "official website" in normalized and any(
        term in normalized for term in (scorer.normalize_text(value) for value in config.TARGET_COUNTRY_QUERY_TERMS)
    ):
        return 3
    if "official website" in normalized or "resmi sitesi" in normalized:
        return 2
    if normalized.endswith(" contact") or normalized.endswith(" iletisim"):
        return 0
    return 1


def _query_trust_bonus(query: str) -> int:
    priority = _query_priority(query)
    if priority == 3:
        return config.TARGET_COUNTRY_OFFICIAL_QUERY_BONUS
    if priority == 2:
        return config.OFFICIAL_WEBSITE_QUERY_BONUS
    return 0


def _metadata_context_match_count(metadata: dict | None, text: str) -> int:
    return sum(
        1
        for context in scorer.metadata_contexts(metadata)
        if scorer.page_matches_metadata_context(text, context)
    )


def _candidate_rank_key(item: dict) -> tuple[int, ...]:
    role = item.get("role", "unknown")
    reason = item.get("reason", "")
    discovery_only = "discovery_only_not_identity_authority" in reason
    # A labelled outbound website is a high-value route to crawl even though
    # the listing/PDF that exposed it remains completely non-authoritative.
    outbound_evidence = item.get("_outbound_discovery_evidence", [])
    strong_outbound_route = bool(
        role == "company_candidate"
        and item.get("score", 0) >= 65
        and (
            item.get("query") == "search_bridge_profile"
            or any(
                urlparse(str(evidence.get("source_url", ""))).path.casefold().endswith(".pdf")
                for evidence in outbound_evidence
            )
        )
    )
    intrinsic_domain_identity = bool(
        item.get("_exact_brand_domain")
        or item.get("_public_brand_domain")
        or re.search(r"(?:^|;\s*)domain_hits:[1-9]\d*/", reason)
    )
    return (
        0 if role in DISCOVERY_ONLY_ROLES else 1,
        0 if discovery_only and not strong_outbound_route else 1,
        item.get("_ownership_evidence", 0),
        1 if role == "company_candidate" or item.get("_exact_brand_domain") or item.get("_public_brand_domain") else 0,
        1 if intrinsic_domain_identity else 0,
        item.get("_legal_name_evidence", 0),
        item["score"],
        item.get("_rare_token_signal", 0),
        item.get("_metadata_context_matches", 0),
        item.get("_official_query_evidence", 0),
        item.get("_query_trust_bonus", 0),
    )


def _candidate_search_control_key(item: dict) -> tuple[int, ...]:
    """Keep corpus rarity from changing query expansion and cache seed sets."""
    key = _candidate_rank_key(item)
    return key[:7] + key[8:]


def _candidate_role(company_name: str, url: str, title: str, snippet: str) -> str:
    """Classify entity-profile results before considering domain similarity."""
    domain = scorer.normalize_domain(url)
    intrinsic_company_domain = scorer.domain_identity_match(company_name, url)[0]
    raw_path = unquote(urlparse(url).path).casefold()
    uuid_company_record = bool(re.search(
        r"/(?:company|firma|member|exhibitor)/"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
        r"(?:/|$)", raw_path,
    ))
    if uuid_company_record:
        return "directory"
    if intrinsic_company_domain:
        return "company_candidate"
    path = scorer.normalize_text(unquote(urlparse(url).path.replace("/", " ")))
    raw_query = unquote(urlparse(url).query).casefold()
    text = scorer.normalize_text(f"{domain} {path} {title} {snippet}")
    directory_markers = (
        "directory", "firma rehberi", "company profile", "companies list",
        "supplier profile", "exporters", "marketplace", "yellow pages",
        "firmalar", "firma listesi", "company directory",
    )
    fair_markers = ("exhibitor", "katilimci", "trade fair", "expo profile")
    marketplace_markers = ("marketplace", "urunleri", "products", "supplier", "satici", "magaza")
    news_markers = ("haber", "news", "basin bulteni", "press release")
    directory_hits = sum(marker in text for marker in directory_markers)
    shared_host_count = _observe_candidate_host(company_name, domain)
    profile_path = any(marker in path.split() for marker in (
        "company", "companies", "firma", "firmalar", "profile", "supplier",
        "exhibitor", "katilimci", "listing", "member", "detail",
    ))
    generic_host = any(keyword in domain for keyword in config.GENERIC_DOMAIN_KEYWORDS)
    entity_query = bool(re.search(
        r"(?:^|&)(?:slug|company|companyid|company_id|firma|member|supplier|exhibitor)=",
        raw_query,
    ))
    entity_detail_variant = bool(re.search(
        r"/(?:company|firma|girisim|girişim|supplier|member|exhibitor)[-_]?(?:profile|profil|detail|detay)(?:/|$)",
        raw_path,
    ))
    numbered_company_record = bool(re.search(
        r"/(?:firma|company|member)[-_]\d+(?:[-_/]|$)", raw_path,
    ))
    if entity_query or entity_detail_variant or numbered_company_record:
        return "directory"
    if any(marker in path for marker in (
        "basin odasi", "basin bulteni", "press room", "press release", "news", "haber",
    )):
        return "news"
    entity_detail_path = bool(re.search(
        r"/(?:company|companies|firma|firmalar|supplier|exhibitor|katilimci|member)/(?:view/)?[^/]+",
        raw_path,
    ))
    generic_brand_detail = generic_host and bool(re.search(r"/(?:brand|detail)/[^/]+", raw_path))
    if directory_hits >= 2 or (
        directory_hits and any(keyword in domain for keyword in config.GENERIC_DOMAIN_KEYWORDS)
    ):
        return "directory"
    if entity_detail_path and directory_hits:
        return "directory"
    if generic_brand_detail:
        return "fair_profile" if any(marker in text for marker in ("fair", "fuar", "expo", "exhibition")) else "directory"
    if sum(marker in text for marker in fair_markers) >= 2:
        return "fair_profile"
    if any(marker in path for marker in ("exhibitor", "katilimci")) and any(
        marker in text for marker in ("fair", "fuar", "expo", "exhibition")
    ):
        return "fair_profile"
    if sum(marker in text for marker in marketplace_markers) >= 2 and (profile_path or generic_host):
        return "marketplace"
    if sum(marker in text for marker in news_markers) >= 2:
        return "news"
    if (
        shared_host_count >= config.SHARED_CANDIDATE_HOST_MIN_COMPANIES
        and (profile_path or generic_host or directory_hits)
    ):
        return "shared_listing"
    return "unknown"


def _snippet_outbound_websites(result: dict, source_url: str) -> list[str]:
    """Extract labelled or bare domains from listing/PDF search evidence."""
    snippet = result.get("body", "") or result.get("snippet", "")
    pattern = re.compile(
        r"(?i)(?:web\s*sitesi|web\s*site|website)\s*[:\-–—,]?\s*"
        r"((?:https?://|www\.)[a-z0-9][a-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)"
    )
    bare_pattern = re.compile(
        r"(?i)(?<![@\w.-])((?:https?://|www\.)?[a-z0-9](?:[a-z0-9-]{0,62}\.)+"
        r"(?:com\.tr|net\.tr|org\.tr|biz\.tr|info\.tr|web\.tr|gen\.tr|com|net|org|tr)"
        r"(?:/[a-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?)"
    )
    source_domain = scorer.normalize_domain(source_url)
    websites: list[str] = []
    # Bare domains are common and sufficiently scoped in indexed PDF text.
    # On ordinary listing pages they are too ambiguous (email domains, ads,
    # neighbouring records), so those pages still require an explicit website
    # label before they may create a discovery bridge.
    pdf_source = urlparse(source_url).path.casefold().endswith(".pdf")
    matches = [*pattern.finditer(snippet)]
    if pdf_source:
        matches.extend(bare_pattern.finditer(snippet))
    for match in matches:
        raw_url = match.group(1).rstrip(".,;:)]}\"")
        website = raw_url if raw_url.startswith(("http://", "https://")) else f"https://{raw_url}"
        domain = scorer.normalize_domain(website)
        if (
            not scorer.is_valid_hostname(domain)
            or domain == source_domain
            or scorer.same_registrable_domain(domain, source_domain)
            or scorer.is_excluded_domain(domain)
            or scorer.is_foreign_country_domain(domain)
        ):
            continue
        websites.append(_canonical_site_url(website))
    return list(dict.fromkeys(value for value in websites if value))


def _add_snippet_outbound_candidates(
    candidates_by_domain: dict[str, dict],
    company_name: str,
    query: str,
    rank: int,
    result: dict,
    identity_name: str | None = None,
) -> None:
    """Use a directory's labelled website field for discovery, never authority."""
    anchor_name = identity_name or company_name
    source_url = _result_url(result)
    title = result.get("title", "")
    snippet = result.get("body", "") or result.get("snippet", "")
    evidence_text = f"{title} {snippet}"
    legal_tokens = scorer.legal_identity_tokens(anchor_name)
    legal_match = scorer.legal_name_phrase_match(anchor_name, evidence_text)
    brand_tokens = scorer.primary_brand_tokens(anchor_name, limit=2)
    normalized_evidence = f" {scorer.normalize_text(evidence_text)} "
    brand_hits = sum(1 for token in brand_tokens if f" {token} " in normalized_evidence)

    websites_with_variants: list[tuple[str, str]] = []
    for website in _snippet_outbound_websites(result, source_url):
        websites_with_variants.append((website, ""))
        domain = scorer.normalize_domain(website)
        # PDF/catalogue OCR and legacy corporate material often disagree only
        # on brand-domain hyphenation. Add one conservative discovery variant
        # when the literal domain already equals a long public-brand anchor
        # after punctuation normalization. The variant still has to be crawled
        # and pass every first-party identity/publication gate.
        if "-" in domain and scorer.public_brand_domain_match(anchor_name, website):
            dehyphenated = domain.replace("-", "")
            if scorer.is_valid_hostname(dehyphenated) and len(scorer.compact_domain_core(dehyphenated)) >= 7:
                websites_with_variants.append((f"https://{dehyphenated}", domain))

    for website, variant_of in list(dict.fromkeys(websites_with_variants)):
        if len(legal_tokens) <= 1:
            identity_in_source = legal_match and scorer.public_brand_domain_match(anchor_name, website)
        else:
            identity_in_source = legal_match or brand_hits >= min(2, len(brand_tokens))
        if not identity_in_source:
            continue

        domain = scorer.normalize_domain(website)
        existing = candidates_by_domain.get(domain)
        evidence = {
            "source_url": source_url,
            "query": query,
            "rank": rank,
            "title": title,
            "snippet": snippet,
            "identity_name": anchor_name,
        }
        if existing:
            outbound_evidence = list(existing.get("_outbound_discovery_evidence", ()))
            if evidence not in outbound_evidence:
                outbound_evidence.append(evidence)
            existing["_outbound_discovery_evidence"] = outbound_evidence
            continue

        candidates_by_domain[domain] = {
            "domain": domain,
            "url": website,
            "score": 68 if variant_of else 70,
            "title": "",
            "snippet": "",
            "query": "snippet_outbound_discovery",
            "rank": rank,
            "reason": (
                "labelled_third_party_outbound_discovery; "
                f"{'orthographic_domain_variant; ' if variant_of else ''}"
                "discovery_only_not_identity_authority"
            ),
            "role": "company_candidate",
            "_official_query_evidence": 0,
            "_public_brand_domain": scorer.public_brand_domain_match(anchor_name, website),
            "_domain_variant_of": variant_of,
            "_outbound_discovery_evidence": [evidence],
        }


def _add_resolver_candidates(
    candidates_by_domain: dict[str, dict], company_name: str, trace: list[dict],
) -> None:
    """Add resolver output as discovery-only candidates with full provenance."""
    results = company_resolvers.resolve_company_domains(company_name)
    trace.append({
        "source": "company_domain_resolvers",
        "status": "consulted",
        "result_count": len(results),
        "results": results,
    })
    for item in results:
        domain = item["domain"]
        evidence = {
            "providers": item.get("providers", []),
            "resolved_name": item.get("resolved_name", ""),
            "rank": item.get("rank", 0),
            "claimed": item.get("claimed", False),
        }
        existing = candidates_by_domain.get(domain)
        if existing:
            items = list(existing.get("_resolver_discovery_evidence", ()))
            if evidence not in items:
                items.append(evidence)
            existing["_resolver_discovery_evidence"] = items
            continue
        details = scorer.score_domain_details(company_name, domain)
        candidates_by_domain[domain] = {
            "domain": domain,
            "url": f"https://{domain}",
            "score": max(details["score"], config.MIN_ACCEPT_SCORE),
            "title": "",
            "snippet": "",
            "query": "company_domain_resolver",
            "rank": item.get("rank", 0),
            "reason": (
                f"{details['reason']}; resolver_discovery:{','.join(item.get('providers', []))}; "
                "discovery_only_not_identity_authority"
            ),
            "role": "company_candidate",
            "_official_query_evidence": 0,
            "_resolver_discovery_evidence": [evidence],
        }


def _add_search_results(
    candidates_by_domain: dict[str, dict],
    company_name: str,
    query: str,
    results: list[dict],
    metadata: dict | None = None,
) -> None:
    query_trust_bonus = _query_trust_bonus(query)
    for rank, result in enumerate(results, start=1):
        url = _result_url(result)
        domain = scorer.normalize_domain(url)
        if not domain:
            continue

        title = result.get("title", "")
        snippet = result.get("body", "") or result.get("snippet", "")
        source_role = _candidate_role(company_name, url, title, snippet)
        # Indexed catalogues and public records often expose a company's own
        # website as bare text inside a PDF hosted on an otherwise ordinary
        # institutional domain.  PDF extraction remains discovery-only and is
        # still guarded by the legal/public-name check in the helper.
        pdf_result = urlparse(url).path.casefold().endswith(".pdf")
        if source_role in DISCOVERY_ONLY_ROLES or scorer.is_excluded_domain(domain) or pdf_result:
            _add_snippet_outbound_candidates(
                candidates_by_domain, company_name, query, rank, result,
            )
        if scorer.is_excluded_domain(domain):
            continue
        existing = candidates_by_domain.get(domain)
        candidate_role = _strongest_candidate_role(
            source_role,
            existing.get("role", "") if existing else "",
        )
        metadata_context_matches = _metadata_context_match_count(metadata, f"{title} {snippet}")
        legal_name_evidence = 1 if scorer.legal_name_phrase_match(company_name, f"{title} {snippet}") else 0
        ownership_evidence = 1 if scorer.ownership_statement_match(company_name, f"{title} {snippet}") else 0
        score_details = scorer.score_domain_details(company_name, url, title=title, snippet=snippet)
        if score_details["score"] <= 0:
            # Brand names and legal company names often differ (MCMBOR/MCM
            # Kimya, Kristal/LaNaturel).  An official-intent result whose own
            # title/snippet names a distinctive brand is useful discovery
            # evidence even when the domain itself is an alias.
            evidence_text = f" {scorer.normalize_text(f'{title} {snippet}')} "
            brand_tokens = scorer.domain_identity_tokens(company_name)
            text_identity_hits = sum(
                1 for token in brand_tokens if len(token) >= 5 and f" {token} " in evidence_text
            )
            if not (text_identity_hits or legal_name_evidence) or not query_trust_bonus:
                continue
            score_details = {
                "score": (62 if legal_name_evidence else 54) + min(max(text_identity_hits - 1, 0), 2) * 4,
                "reason": (
                    f"search_text_identity:{text_identity_hits}/{len(brand_tokens)}; "
                    f"search_legal_name_identity:{len(scorer.legal_identity_tokens(company_name))}"
                    if legal_name_evidence else f"search_text_identity:{text_identity_hits}/{len(brand_tokens)}"
                ),
            }
        rank_bonus = max(config.RESULT_RANK_BONUS_MAX - (rank - 1) * 2, 0)
        metadata_context_bonus = config.METADATA_SEARCH_CONTEXT_BONUS if metadata_context_matches else 0
        role_penalty = 30 if candidate_role in DISCOVERY_ONLY_ROLES else 0
        # Legal-name evidence is a ranking/ownership signal. Keeping it out of
        # the numeric score prevents repeated query bonuses from manufacturing
        # confidence before the candidate site itself is crawled.
        legal_name_bonus = 0
        base_score = score_details["score"] + rank_bonus + query_trust_bonus + metadata_context_bonus + legal_name_bonus - role_penalty
        if base_score <= 0:
            continue

        evidence_queries = set(existing.get("_evidence_queries", ())) if existing else set()
        evidence_queries.add(query)
        search_evidence = list(existing.get("_search_evidence", ())) if existing else []
        hit = {
            "query": query,
            "rank": rank,
            "url": url,
            "title": title,
            "snippet": snippet,
            "role": candidate_role,
        }
        if hit not in search_evidence:
            search_evidence.append(hit)
        candidate_role = _strongest_candidate_role(
            candidate_role,
            existing.get("role", "") if existing else "",
            *(evidence.get("role", "") for evidence in search_evidence),
        )
        hit["role"] = candidate_role
        contact_seed_urls = list(existing.get("_contact_seed_urls", ())) if existing else []
        contact_path = any(
            marker in scorer.normalize_text(unquote(url))
            for marker in ("contact", "iletisim", "bize-ulas")
        )
        # A deep contact result can surface under an official-site query too.
        # Keep it as a crawl seed only when the result itself carries the legal
        # name or an explicit owner/brand relationship; a generic deep link
        # must not steer crawling on weak search evidence alone.
        seed_identity_supported = legal_name_evidence or ownership_evidence
        if (
            (_query_priority(query) == 0 or seed_identity_supported)
            and scorer.same_registrable_domain(domain, url)
            and contact_path
            and url not in contact_seed_urls
        ):
            contact_seed_urls.append(url)
        official_query_evidence = sum(1 for evidence_query in evidence_queries if _query_trust_bonus(evidence_query))
        best_base_score = max(base_score, existing.get("_base_score", 0) if existing else 0)
        consensus_bonus = min(max(len(evidence_queries) - 1, 0) * 4, 8)
        score = min(config.PRE_CRAWL_SCORE_CAP, best_base_score + consensus_bonus)
        combined_reason = score_details["reason"]
        if "clean_single_token_domain:" in combined_reason:
            score = min(score, config.SAFE_OK_MIN_SCORE - 1)
        if "short_name_capped" in combined_reason:
            score = min(score, config.SHORT_COMPANY_MIN_SCORE - 1)
        candidate = {
            "domain": domain,
            "url": _canonical_site_url(url),
            "score": score,
            "title": title,
            "snippet": snippet,
            "query": query,
            "rank": rank,
            "reason": (
                f"{score_details['reason']}; rank_bonus:{rank_bonus}; query_trust_bonus:{query_trust_bonus}; rank:{rank}; "
                f"query_evidence:{len(evidence_queries)}; consensus_bonus:{consensus_bonus}; "
                f"metadata_context_matches:{metadata_context_matches}; metadata_context_bonus:{metadata_context_bonus}; "
                f"candidate_role:{candidate_role}; role_penalty:{role_penalty}"
            ),
            "role": candidate_role,
            "_base_score": best_base_score,
            "_rare_token_signal": score_details.get("rare_token_signal", 0),
            "_evidence_queries": evidence_queries,
            "_official_query_evidence": official_query_evidence,
            "_query_trust_bonus": query_trust_bonus,
            "_metadata_context_matches": max(
                metadata_context_matches,
                existing.get("_metadata_context_matches", 0) if existing else 0,
            ),
            "_legal_name_evidence": max(
                legal_name_evidence,
                existing.get("_legal_name_evidence", 0) if existing else 0,
            ),
            "_ownership_evidence": max(
                ownership_evidence,
                existing.get("_ownership_evidence", 0) if existing else 0,
            ),
            "_exact_brand_domain": (
                bool(scorer.domain_identity_tokens(company_name))
                and scorer.compact_domain_core(domain) == "".join(scorer.domain_identity_tokens(company_name))
            ),
            "_public_brand_domain": scorer.public_brand_domain_match(company_name, domain),
            "_search_evidence": search_evidence,
            "_contact_seed_urls": contact_seed_urls,
        }
        if existing and existing.get("_source_profile_evidence"):
            # A link extracted directly from the supplied exhibitor/profile page
            # is stronger than a later search hit.  Enrich it with search
            # consensus without replacing its provenance or lowering its score.
            existing["score"] = max(existing.get("score", 0), score)
            existing["_base_score"] = max(
                existing.get("_base_score", 0), existing.get("score", 0), base_score
            )
            existing["_evidence_queries"] = evidence_queries
            existing["_official_query_evidence"] = official_query_evidence
            existing["_query_trust_bonus"] = max(
                existing.get("_query_trust_bonus", 0), query_trust_bonus
            )
            existing["_metadata_context_matches"] = max(
                existing.get("_metadata_context_matches", 0), metadata_context_matches
            )
            existing["_rare_token_signal"] = max(
                existing.get("_rare_token_signal", 0), score_details.get("rare_token_signal", 0)
            )
            existing["_legal_name_evidence"] = max(
                existing.get("_legal_name_evidence", 0), legal_name_evidence
            )
            existing["_ownership_evidence"] = max(
                existing.get("_ownership_evidence", 0), ownership_evidence
            )
            existing["_exact_brand_domain"] = existing.get("_exact_brand_domain", False) or candidate["_exact_brand_domain"]
            existing["_public_brand_domain"] = existing.get("_public_brand_domain", False) or candidate["_public_brand_domain"]
            existing["_search_evidence"] = search_evidence
            existing["_contact_seed_urls"] = contact_seed_urls
            if not existing.get("title"):
                existing["title"] = title
            if not existing.get("snippet"):
                existing["snippet"] = snippet
            existing["reason"] = (
                f"{existing.get('reason', 'authoritative_exhibitor_profile_link')}; "
                f"search_query_evidence:{len(evidence_queries)}; search_consensus_bonus:{consensus_bonus}"
            )
            continue
        if existing is None or (base_score, metadata_context_matches) >= (
            existing.get("_base_score", 0),
            existing.get("_metadata_context_matches", 0),
        ):
            candidates_by_domain[domain] = candidate
        else:
            existing["score"] = score
            existing["_evidence_queries"] = evidence_queries
            existing["_official_query_evidence"] = official_query_evidence
            existing["_metadata_context_matches"] = max(
                metadata_context_matches,
                existing.get("_metadata_context_matches", 0),
            )
            existing["_rare_token_signal"] = max(
                existing.get("_rare_token_signal", 0), score_details.get("rare_token_signal", 0)
            )
            existing["_legal_name_evidence"] = max(
                existing.get("_legal_name_evidence", 0), legal_name_evidence
            )
            existing["_ownership_evidence"] = max(
                existing.get("_ownership_evidence", 0), ownership_evidence
            )
            existing["_exact_brand_domain"] = existing.get("_exact_brand_domain", False) or candidate["_exact_brand_domain"]
            existing["_public_brand_domain"] = existing.get("_public_brand_domain", False) or candidate["_public_brand_domain"]
            existing["_search_evidence"] = search_evidence
            existing["_contact_seed_urls"] = contact_seed_urls
            existing["reason"] = re.sub(
                r"query_evidence:\d+; consensus_bonus:\d+",
                f"query_evidence:{len(evidence_queries)}; consensus_bonus:{consensus_bonus}",
                existing["reason"],
            )


def _best_candidate(candidates_by_domain: dict[str, dict]) -> dict | None:
    return max(
        (item for item in candidates_by_domain.values() if item.get("role") not in DISCOVERY_ONLY_ROLES),
        key=_candidate_search_control_key,
        default=None,
    )


def _can_early_stop(company_name: str, candidate: dict, metadata: dict | None) -> bool:
    if candidate.get("query") in {"verified_alias", "verified_entity"}:
        return True
    if candidate.get("query") == "source_profile":
        # Fair and directory links are discovery bridges. Continue searching so
        # stale or misassigned profile links are compared with other candidates.
        return False
    if (
        candidate.get("role") != "company_candidate"
        and not scorer.domain_identity_match(company_name, candidate.get("url", ""))[0]
    ):
        return False
    if not candidate.get("_official_query_evidence", 0):
        return False

    brand_tokens = scorer.domain_identity_tokens(company_name)
    exact_brand_domain = bool(brand_tokens) and scorer.compact_domain_core(candidate["domain"]) == "".join(brand_tokens)
    if not exact_brand_domain and candidate.get("_official_query_evidence", 0) < 2:
        return False

    # A single-token brand is especially prone to homonyms (AYSAN food,
    # electrical, plastic, heating...).  When sector metadata exists, do not
    # stop before a result carries that sector evidence.
    if len(brand_tokens) == 1:
        if scorer.metadata_contexts(metadata) and not candidate.get("_metadata_context_matches", 0):
            return False
        if (
            len(scorer.legal_identity_tokens(company_name)) > 1
            and not candidate.get("_legal_name_evidence")
            and not candidate.get("_ownership_evidence")
        ):
            return False
    return True


@lru_cache(maxsize=4096)
def _domain_has_address(domain: str) -> bool:
    try:
        socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    return True


def _primary_queries(company_name: str, metadata: dict | None) -> list[str]:
    queries: list[str] = []
    seen_queries = set()
    query_inputs = scorer.search_name_variants(company_name)
    for alias in aliases.search_terms(company_name):
        query_inputs.extend(scorer.search_name_variants(alias))
    for query_input in dict.fromkeys(query_inputs):
        for template in config.SEARCH_QUERY_TEMPLATES:
            query = template.format(company=query_input)
            if query not in seen_queries:
                queries.append(query)
                seen_queries.add(query)
        for term in _metadata_query_terms(metadata):
            query = f"{query_input} {term}"
            if query not in seen_queries:
                queries.append(query)
                seen_queries.add(query)
        for country in config.TARGET_COUNTRY_QUERY_TERMS:
            for template in config.SEARCH_COUNTRY_QUERY_TEMPLATES:
                query = template.format(company=query_input, country=country)
                if query not in seen_queries:
                    queries.append(query)
                    seen_queries.add(query)
    if config.MAX_SEARCH_QUERIES_PER_COMPANY > 0:
        return sorted(queries, key=_query_priority, reverse=True)[: config.MAX_SEARCH_QUERIES_PER_COMPANY]
    return sorted(queries, key=_query_priority, reverse=True)


def _fallback_queries(company_name: str, metadata: dict | None) -> list[str]:
    full_name = " ".join(scorer._raw_company_tokens(company_name))
    if not full_name:
        return []
    quoted_name = f'"{full_name}"'
    contexts = _metadata_query_terms(metadata)
    queries = [
        f"{quoted_name} {contexts[0]} resmi sitesi" if contexts else "",
        f"{quoted_name} Turkiye official website",
        f"{quoted_name} iletisim",
    ]
    unique = list(dict.fromkeys(query for query in queries if query))
    return sorted(unique, key=_query_priority, reverse=True)[: config.MAX_FALLBACK_SEARCH_QUERIES]


def _adaptive_queries(
    company_name: str,
    metadata: dict | None,
    already_run: set[str] | None = None,
    related_name_hints: list[str] | None = None,
    evidence_gaps: set[str] | None = None,
) -> list[str]:
    """Build high-information queries only after the static plan is weak.

    These queries target public-brand/legal-name divergence and first-party
    disclosure pages. They do not carry identity authority; they only add
    search candidates that still pass the normal crawl and publication gates.
    """
    return query_planner.adaptive_queries(
        company_name,
        metadata,
        already_run=already_run,
        related_name_hints=related_name_hints,
        context_terms=_metadata_query_terms(metadata),
        evidence_gaps=evidence_gaps,
        limit=config.MAX_ADAPTIVE_SEARCH_QUERIES,
    )


def _adaptive_discovery_gaps(
    company_name: str,
    candidates_by_domain: dict[str, dict],
    related_name_hints: list[str] | None = None,
) -> set[str]:
    """Describe unresolved discovery evidence without granting authority."""
    candidates = [
        item for item in candidates_by_domain.values()
        if item.get("role") not in DISCOVERY_ONLY_ROLES
        and not scorer.is_excluded_domain(item.get("url", ""))
    ]
    gaps: set[str] = set()
    if not candidates:
        gaps.add("no_candidates")
    ranked = sorted(candidates, key=_candidate_rank_key, reverse=True)
    if len(ranked) >= 2 and abs(ranked[0].get("score", 0) - ranked[1].get("score", 0)) <= config.AMBIGUOUS_CANDIDATE_MARGIN:
        gaps.add("ambiguous_candidates")
    brand_tokens = scorer.primary_brand_tokens(company_name, limit=1)
    # A single search hit is not uniqueness evidence for a short public brand;
    # explicitly seek the legal/full-name variant before accepting it.
    if brand_tokens and len(brand_tokens[0]) < 7 and ranked:
        gaps.add("ambiguous_candidates")
    if not any(
        (
            scorer.domain_identity_match(company_name, item.get("url", ""))[0]
            or scorer.public_brand_domain_match(company_name, item.get("url", ""))
        )
        and "search_text_identity:" not in item.get("reason", "")
        for item in candidates
    ):
        gaps.add("missing_intrinsic_domain")
    if not any(
        item.get("_legal_name_evidence") or item.get("_ownership_evidence")
        for item in candidates
    ):
        gaps.add("missing_legal_name")
    if not any(
        scorer.normalize_domain(item.get("url", "")).endswith(".tr")
        or item.get("_metadata_context_matches", 0) > 0
        for item in candidates
    ):
        gaps.add("missing_local_signal")
    if related_name_hints:
        gaps.add("relationship_hint")
    return gaps


def _related_name_hints(company_name: str, title: str, snippet: str) -> list[str]:
    """Extract low-authority related-name hints from a legal-name result.

    Chamber and registry snippets sometimes expose a former/public company name
    inside an industrial-site or facility name.  The hint is used only to form
    another search query; it never contributes identity authority.
    """
    evidence_text = f"{title} {snippet}"
    if not scorer.legal_name_phrase_match(company_name, evidence_text):
        return []
    target_tokens = set(scorer._raw_company_tokens(company_name))
    pattern = re.compile(
        r"((?:[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ0-9-]{1,}[ \t]+){1,4}"
        r"[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ0-9-]{1,})[ \t]+S[İI]T(?:ES[İI])?\.?"
    )
    ignored = {
        scorer.normalize_text(word) for word in (
            *config.LEGAL_COMPANY_WORDS,
            "mahalle", "mahallesi", "cadde", "caddesi", "sokak", "bulvar",
            "organize", "sanayi", "sitesi", "site",
        )
    }
    hints: list[str] = []
    for match in pattern.finditer(evidence_text):
        raw_hint = match.group(1).replace("-", "")
        tokens = [
            token for token in scorer._raw_company_tokens(raw_hint)
            if token not in ignored and len(token) > 2
        ]
        if not 2 <= len(tokens) <= 4:
            continue
        shared = {token for token in tokens if token in target_tokens and len(token) >= 4}
        novel = [token for token in tokens if token not in target_tokens and len(token) >= 4]
        if not shared or not novel:
            continue
        hint = " ".join(tokens)
        if hint not in hints:
            hints.append(hint)
    return hints[:2]


def _add_related_hint_results(
    candidates_by_domain: dict[str, dict],
    company_name: str,
    hint: str,
    query: str,
    results: list[dict],
) -> None:
    """Add hint-matching domains for crawl verification, never as authority."""
    hint_tokens = [token for token in scorer._raw_company_tokens(hint) if len(token) >= 4]
    if not hint_tokens:
        return
    for rank, result in enumerate(results, start=1):
        # A related public/facility name can anchor an outbound domain found
        # in the same indexed PDF.  This is still only a path to crawl: the
        # original legal company must pass every first-party identity gate.
        _add_snippet_outbound_candidates(
            candidates_by_domain, company_name, query, rank, result,
            identity_name=hint,
        )
        url = _result_url(result)
        domain = scorer.normalize_domain(url)
        if (
            not domain or scorer.is_excluded_domain(domain)
            or scorer.is_foreign_country_domain(domain)
        ):
            continue
        title = result.get("title", "")
        snippet = result.get("body", "") or result.get("snippet", "")
        evidence_words = set(scorer._raw_company_tokens(f"{title} {domain}"))
        hits = sum(1 for token in hint_tokens if token in evidence_words or token in scorer.compact_domain_core(domain))
        if hits < min(2, len(hint_tokens)):
            continue
        evidence = {
            "hint": hint, "query": query, "rank": rank,
            "url": url, "title": title, "snippet": snippet,
        }
        existing = candidates_by_domain.get(domain)
        if existing:
            items = list(existing.get("_related_name_discovery", ()))
            if evidence not in items:
                items.append(evidence)
            existing["_related_name_discovery"] = items
            continue
        candidates_by_domain[domain] = {
            "domain": domain,
            "url": _canonical_site_url(url),
            "score": 70,
            "title": title,
            "snippet": snippet,
            "query": "related_name_discovery",
            "rank": rank,
            "reason": "related_name_hint_discovery; discovery_only_not_identity_authority",
            "role": "company_candidate",
            "_official_query_evidence": 0,
            "_query_trust_bonus": 0,
            "_metadata_context_matches": 0,
            "_related_name_discovery": [evidence],
        }


def _discovery_needs_expansion(
    company_name: str,
    candidates_by_domain: dict[str, dict],
    metadata: dict | None,
) -> bool:
    """Return true when pre-crawl evidence is weak, ambiguous or bridge-led."""
    ranked = sorted(
        (
            item for item in candidates_by_domain.values()
            if item.get("role") not in DISCOVERY_ONLY_ROLES
        ),
        key=_candidate_search_control_key,
        reverse=True,
    )
    if not ranked:
        return True
    best = ranked[0]
    if best.get("score", 0) < config.EARLY_STOP_SCORE_THRESHOLD:
        return True
    if not _can_early_stop(company_name, best, metadata):
        return True
    if len(ranked) > 1:
        second = ranked[1]
        if (
            best.get("domain") != second.get("domain")
            and best.get("score", 0) - second.get("score", 0) <= config.AMBIGUOUS_CANDIDATE_MARGIN
        ):
            return True
    return False


def _add_domain_guesses(candidates_by_domain: dict[str, dict], company_name: str) -> None:
    for variant in scorer.search_name_variants(company_name):
        compact_name = "".join(scorer._raw_company_tokens(variant))
        if len(compact_name) < 5 or len(compact_name) > 63:
            continue
        for suffix in config.DOMAIN_GUESS_TLDS:
            domain = f"{compact_name}{suffix}"
            if domain in candidates_by_domain:
                continue
            if not _domain_has_address(domain):
                continue
            details = scorer.score_domain_details(company_name, domain)
            if details["score"] < config.MIN_ACCEPT_SCORE:
                continue
            candidates_by_domain[domain] = {
                "domain": domain,
                "url": f"https://{domain}",
                "score": details["score"],
                "title": "",
                "snippet": "",
                "query": "domain_guess",
                "rank": 0,
                "reason": f"{details['reason']}; domain_guess",
                "role": "company_candidate",
                "_exact_brand_domain": (
                    bool(scorer.domain_identity_tokens(company_name))
                    and scorer.compact_domain_core(domain) == "".join(scorer.domain_identity_tokens(company_name))
                ),
                "_public_brand_domain": scorer.public_brand_domain_match(company_name, domain),
            }


def _add_google_places_results(candidates_by_domain: dict[str, dict], company_name: str) -> None:
    for rank, place in enumerate(google_places.search_company(company_name), start=1):
        website = place["website"]
        domain = scorer.normalize_domain(website)
        if not domain or scorer.is_excluded_domain(domain):
            continue
        details = scorer.score_domain_details(company_name, website, title=place.get("name", ""))
        if details["score"] < config.MIN_ACCEPT_SCORE:
            continue
        candidate = {
            "domain": domain,
            "url": _canonical_site_url(website),
            "score": min(config.PRE_CRAWL_SCORE_CAP, details["score"] + 6),
            "title": place.get("name", ""),
            "snippet": "",
            "query": "google_places",
            "rank": rank,
            "external_phone": place.get("phone", ""),
            "reason": f"{details['reason']}; google_places_match; rank:{rank}",
            "role": "company_candidate",
            "_search_evidence": [{"source": "google_places", "rank": rank, "place_id": place.get("place_id", "")}],
        }
        existing = candidates_by_domain.get(domain)
        if existing is None or candidate["score"] > existing["score"]:
            candidates_by_domain[domain] = candidate


def _add_verified_alias_candidate(candidates_by_domain: dict[str, dict], company_name: str) -> None:
    for rank, record in enumerate(aliases.verified_websites(company_name)):
        website = record.get("url", "")
        domain = scorer.normalize_domain(website)
        if not domain or scorer.is_excluded_domain(domain):
            continue
        candidates_by_domain[domain] = {
            "domain": domain,
            "url": _canonical_site_url(website),
            "score": config.PRE_CRAWL_SCORE_CAP,
            "title": "",
            "snippet": "",
            "query": "verified_entity" if record.get("entity_id") else "verified_alias",
            "rank": rank,
            "reason": f"human_verified_entity_relationship:{record.get('relationship', 'official')}",
            "role": "verified_company",
            "_entity_id": record.get("entity_id", ""),
            "_entity_relationship": record.get("relationship", "official"),
            "_entity_evidence_url": record.get("evidence_url", ""),
            "_entity_verified_at": record.get("verified_at", ""),
        }


def _profile_html_needs_render(html: str, page_url: str) -> bool:
    """Detect profile shells whose outbound website appears only after JS."""
    if not config.ENABLE_JS_PROFILE_FALLBACK or not html:
        return False
    if crawler._looks_like_js_shell(html):
        return True
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    visible = scorer.normalize_text(soup.get_text(" ", strip=True))
    dynamic_markers = (
        "javascript must be enabled", "enable javascript", "javascript is required",
        "javascript acik olmalidir", "bu sayfayi goruntulemek icin javascript",
        "loading exhibitor", "katilimci yukleniyor",
    )
    if any(marker in visible for marker in dynamic_markers):
        return True

    page_domain = scorer.normalize_domain(page_url)
    has_external_http_link = any(
        scorer.normalize_domain(requests.compat.urljoin(page_url, link.get("href", "")))
        and not scorer.same_registrable_domain(
            page_domain,
            scorer.normalize_domain(requests.compat.urljoin(page_url, link.get("href", ""))),
        )
        for link in soup.find_all("a", href=True)
        if not link.get("href", "").startswith(("mailto:", "tel:", "javascript:", "#"))
    )
    # Sparse application shells are common even when they do not contain the
    # conventional #root/#app marker. Avoid rendering normal content pages.
    return not has_external_http_link and len(visible) < 500 and len(soup.find_all("script")) >= 2


def _profile_render_fallback(url: str, html: str = "", force: bool = False) -> tuple[str, bool]:
    if (
        config.SEARCH_CACHE_MODE == "replay"
        or not config.ENABLE_JS_PROFILE_FALLBACK
        or (not force and not _profile_html_needs_render(html, url))
    ):
        return html, False
    runtime.record("source_profile.render_attempts")
    rendered_html, render_error = crawler._try_render(url)
    if rendered_html:
        runtime.record("source_profile.render_successes")
        return rendered_html, True
    runtime.record("source_profile.render_failures")
    if render_error and render_error != "js_fallback_disabled":
        LOGGER.info("Exhibitor profile render fallback failed: %s (%s)", url, render_error)
    return html, False


def _profile_external_websites(profile_url: str) -> list[dict]:
    if not profile_url:
        return []
    if config.SEARCH_CACHE_MODE in {"use", "replay"}:
        cached = cache_store.load(
            config.SEARCH_CACHE_DIR, "source_profile_links_v4", profile_url,
            config.SEARCH_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION,
        )
        if cached is not None:
            _record_source_health(profile_url, "cached_available" if cached else "cached_empty")
            return cached
        if config.SEARCH_CACHE_MODE == "replay":
            legacy = cache_store.load(
                config.SEARCH_CACHE_DIR, "source_profile_links_v3", profile_url,
                config.SEARCH_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION,
            )
            if legacy is None:
                legacy = cache_store.load(
                    config.SEARCH_CACHE_DIR, "source_profile_links_v2", profile_url,
                    config.SEARCH_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION,
                )
            if legacy is None:
                legacy = cache_store.load(
                    config.SEARCH_CACHE_DIR, "source_profile", profile_url,
                    config.SEARCH_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION,
                )
            _record_source_health(profile_url, "cached_available" if legacy else "cached_empty")
            return legacy or []
    from bs4 import BeautifulSoup

    health = _source_health_snapshot(profile_url)
    if health.get("circuit_open"):
        runtime.record("source_profile.circuit_skips")
        _record_source_health(profile_url, "circuit_open")
        return []

    headers = {"User-Agent": config.USER_AGENT, "Accept-Language": "tr,en;q=0.8"}
    pages: list[tuple[str, str, bool]] = []
    try:
        response = crawler._request_with_safe_redirects(profile_url, verify=True)
        response_url = getattr(response, "_b2b_final_url", getattr(response, "url", profile_url))
        profile_html, rendered = _profile_render_fallback(response_url, response.text)
        pages.append((response_url, profile_html, rendered))
        runtime.record("source_profile.successes")
        _record_source_health(profile_url, "available", getattr(response, "status_code", 200))
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code is not None and int(status_code) >= 500:
            runtime.record("source_profile.http_5xx")
            _record_source_health(profile_url, "server_error", int(status_code))
        else:
            runtime.record("source_profile.failures")
            _record_source_health(profile_url, "unavailable", status_code)
        LOGGER.info("Exhibitor profile could not be read: %s (%s)", profile_url, exc)
        if status_code not in {401, 403, 429}:
            return []
        rendered_html, rendered = _profile_render_fallback(profile_url, force=True)
        if not rendered:
            return []
        pages.append((profile_url, rendered_html, True))
        _record_source_health(profile_url, "available", int(status_code))

    # IFCO's listing profile contains mostly template links; its /detail page
    # carries the exhibitor's actual website. Follow only same-host detail
    # links, never arbitrary external navigation.
    first_page_url, first_page_html, _ = pages[0]
    first_soup = BeautifulSoup(first_page_html, "html.parser")
    profile_domain = scorer.normalize_domain(profile_url)
    detail_urls = []
    for link in first_soup.find_all("a", href=True):
        detail_url = requests.compat.urljoin(first_page_url, link.get("href", ""))
        if scorer.normalize_domain(detail_url) == profile_domain and urlparse(detail_url).path.rstrip("/").endswith("/detail"):
            detail_urls.append(detail_url)
    for detail_url in dict.fromkeys(detail_urls):
        if detail_url.rstrip("/") == first_page_url.rstrip("/"):
            continue
        try:
            detail_response = crawler._request_with_safe_redirects(detail_url, verify=True)
            final_detail_url = getattr(detail_response, "_b2b_final_url", getattr(detail_response, "url", detail_url))
            detail_html, rendered = _profile_render_fallback(final_detail_url, detail_response.text)
            pages.append((final_detail_url, detail_html, rendered))
        except requests.RequestException as exc:
            LOGGER.info("Exhibitor detail could not be read: %s (%s)", detail_url, exc)

    raw_urls: list[tuple[str, str, bool, str, bool]] = []
    website_markers = ("website", "web site", "web sitesi", "internet sitesi", "resmi site", "official site", "web")
    for page_url, page_html, rendered in pages:
        soup = BeautifulSoup(page_html, "html.parser")
        for link in soup.find_all("a", href=True):
            label = " ".join(filter(None, [
                link.get_text(" ", strip=True), link.get("title", ""), link.get("aria-label", ""),
            ]))
            normalized_label = scorer.normalize_text(label)
            container_text = link.parent.get_text(" ", strip=True) if link.parent else ""
            normalized_context = scorer.normalize_text(container_text[:500])
            raw_urls.append((
                link.get("href", ""),
                label,
                any(marker in normalized_label or marker in normalized_context for marker in website_markers),
                page_url,
                rendered,
            ))
        # Some catalogues render a website as plain text instead of a hyperlink.
        visible_text = soup.get_text(" ", strip=True)
        for match in re.finditer(
            r"(?i)(?<![@\w.-])(?:https?://|www\.)?[a-z0-9][a-z0-9.-]+\.(?:com\.tr|com|net\.tr|net|org\.tr|org|tr|cc)(?:/[^\s<>'\"]*)?",
            visible_text,
        ):
            context = visible_text[max(0, match.start() - 100):match.end() + 100]
            normalized_context = scorer.normalize_text(context)
            raw_urls.append((
                match.group(0), match.group(0),
                any(marker in normalized_context for marker in website_markers),
                page_url,
                rendered,
            ))
    websites: list[dict] = []
    for raw_url, label, explicit_website, source_page_url, rendered in raw_urls:
        if not raw_url or raw_url.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        if raw_url.startswith("//"):
            normalized = f"https:{raw_url}"
        elif "://" in raw_url:
            normalized = raw_url
        elif raw_url.startswith("www.") or ("." in raw_url and not raw_url.startswith("/")):
            normalized = f"https://{raw_url}"
        else:
            continue
        domain = scorer.normalize_domain(normalized)
        if not domain or domain == profile_domain or domain.endswith(f".{profile_domain}"):
            continue
        if scorer.is_excluded_domain(domain) or scorer.is_foreign_country_domain(domain):
            continue
        if urlparse(normalized).path.casefold().endswith((
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar",
        )):
            continue
        canonical = _canonical_site_url(normalized)
        if canonical and all(item["url"] != canonical for item in websites):
            websites.append({
                "url": canonical,
                "label": label,
                "explicit_website": explicit_website,
                "source_page_url": source_page_url,
                "rendered": rendered,
            })
    websites.sort(key=lambda item: bool(item.get("explicit_website")), reverse=True)
    websites = websites[:5]
    if config.SEARCH_CACHE_MODE in {"use", "refresh"}:
        cache_store.save(
            config.SEARCH_CACHE_DIR, "source_profile_links_v4", profile_url, websites,
            config.CACHE_SCHEMA_VERSION,
        )
    return websites


def _add_profile_candidates(candidates_by_domain: dict[str, dict], company_name: str, metadata: dict | None) -> None:
    profile_url = (metadata or {}).get("profile_url", "")
    for rank, link_record in enumerate(_profile_external_websites(profile_url), start=1):
        # String support keeps old fixtures and hand-written integrations
        # compatible; newly extracted records carry semantic link evidence.
        if isinstance(link_record, str):
            website, label, explicit_website = link_record, "", True
            source_page_url = profile_url
        else:
            website = link_record.get("url", "")
            label = link_record.get("label", "")
            explicit_website = bool(link_record.get("explicit_website"))
            source_page_url = link_record.get("source_page_url", profile_url)
        domain = scorer.normalize_domain(website)
        # Re-check cached profile links against the current exclusion policy;
        # old cache entries may predate a newly recognized catalogue host.
        if not domain or scorer.is_excluded_domain(domain):
            continue
        candidate_role = _candidate_role(company_name, website, label, "")
        if candidate_role in {"unknown", "company_candidate"}:
            candidate_role = "company_candidate"
        candidate = {
            "domain": domain,
            "url": website,
            "score": config.PRE_CRAWL_SCORE_CAP if explicit_website else config.MEDIUM_CONFIDENCE_SCORE,
            "title": "",
            "snippet": "",
            "query": "source_profile" if explicit_website else "source_profile_link",
            "rank": rank,
            "reason": (
                f"authoritative_exhibitor_profile_website_link; label:{label}; rank:{rank}"
                if explicit_website else f"generic_exhibitor_profile_external_link; label:{label}; rank:{rank}"
            ),
            "role": candidate_role,
            "_source_profile_evidence": 1 if explicit_website else 0,
            "_official_query_evidence": 0,
            "_query_trust_bonus": 0,
            "_metadata_context_matches": 0,
            "_search_evidence": [{"source": "source_profile", "profile_url": profile_url, "rank": rank}],
            "_profile_url": profile_url,
            "_profile_source_page_url": source_page_url,
        }
        existing = candidates_by_domain.get(domain)
        if existing is None or candidate["score"] >= existing["score"]:
            candidates_by_domain[domain] = candidate


def _bridge_entity_anchor_supported(company_name: str, title: str, source_url: str) -> bool:
    """Require the search result to point at the target's own profile.

    Directory snippets often contain several company rows.  A full legal-name
    hit in that surrounding text must not let the current profile owner's
    website escape into another company's candidate pool.  The result title or
    profile URL therefore has to identify the target entity.  An explicit
    ownership statement is handled separately by ``_bridge_identity_supported``
    so genuinely different public brands can still be discovered.
    """
    title_text = scorer.normalize_text(title)
    parsed = urlparse(source_url)
    url_text = scorer.normalize_text(f"{unquote(parsed.path)} {unquote(parsed.query)}")
    if scorer.legal_name_phrase_match(company_name, title):
        return True
    brand_tokens = scorer.primary_brand_tokens(company_name, limit=2)
    if not brand_tokens:
        return False
    title_words = set(title_text.split())
    url_words = set(url_text.split())
    if len(brand_tokens) >= 2:
        return all(token in title_words for token in brand_tokens) or all(
            token in url_words for token in brand_tokens
        )
    token = brand_tokens[0]
    return len(token) >= 5 and (token in title_words or token in url_words)


def _bridge_identity_supported(
    company_name: str,
    title: str,
    snippet: str,
    metadata: dict | None,
    source_url: str = "",
) -> bool:
    """Require the bridge result itself to identify the requested company."""
    evidence_text = f"{title} {snippet}"
    # A local, explicit legal-owner/brand statement can safely connect a public
    # brand whose title and URL naturally differ from the exhibitor legal name.
    if scorer.ownership_statement_match(company_name, evidence_text):
        return True
    if not _bridge_entity_anchor_supported(company_name, title, source_url):
        return False
    if scorer.legal_name_phrase_match(company_name, evidence_text):
        return True
    brand_tokens = scorer.primary_brand_tokens(company_name, limit=2)
    normalized = f" {scorer.normalize_text(evidence_text)} "
    brand_hits = sum(1 for token in brand_tokens if f" {token} " in normalized)
    if len(brand_tokens) >= 2:
        return brand_hits == len(brand_tokens)
    if not brand_tokens or len(brand_tokens[0]) < 5 or not brand_hits:
        return False
    contexts = scorer.metadata_contexts(metadata)
    return not contexts or any(
        scorer.page_matches_metadata_context(evidence_text, context) for context in contexts
    )


def _collect_search_bridge_sources(
    target: dict[str, dict],
    company_name: str,
    query: str,
    results: list[dict],
    metadata: dict | None,
) -> None:
    blocked = {
        scorer.normalize_domain(domain) for domain in config.PROFILE_BRIDGE_BLOCKED_DOMAINS
    }
    for rank, result in enumerate(results, start=1):
        url = _result_url(result)
        domain = scorer.normalize_domain(url)
        if not domain or any(domain == item or domain.endswith(f".{item}") for item in blocked):
            continue
        title = result.get("title", "")
        snippet = result.get("body", "") or result.get("snippet", "")
        role = _candidate_role(company_name, url, title, snippet)
        path = scorer.normalize_text(unquote(urlparse(url).path.replace("/", " ")))
        profile_shaped = any(marker in path.split() for marker in (
            "company", "firma", "profile", "supplier", "exhibitor", "katilimci", "member", "detail",
        ))
        if role not in {"directory", "fair_profile", "shared_listing", "marketplace"} and not (
            scorer.is_excluded_domain(domain) and profile_shaped
        ):
            continue
        if not _bridge_identity_supported(company_name, title, snippet, metadata, url):
            continue
        current = target.get(url)
        record = {
            "url": url, "domain": domain, "query": query, "rank": rank,
            "title": title, "snippet": snippet, "role": role,
        }
        if current is None or rank < current.get("rank", 999):
            target[url] = record


def _expand_search_bridge_candidates(
    candidates_by_domain: dict[str, dict],
    company_name: str,
    bridge_sources: dict[str, dict],
    trace: list[dict],
    expanded_urls: set[str],
) -> None:
    pending = sorted(
        (record for url, record in bridge_sources.items() if url not in expanded_urls),
        key=lambda item: (item.get("rank", 999), item.get("url", "")),
    )[: config.MAX_SEARCH_BRIDGE_FETCHES]
    for source in pending:
        source_url = source["url"]
        expanded_urls.add(source_url)
        links = _profile_external_websites(source_url)
        trace.append({
            "source": "search_bridge_profile", "profile_url": source_url,
            "result_count": len(links), "role": source.get("role", ""),
        })
        for rank, link_record in enumerate(links, start=1):
            if isinstance(link_record, str):
                website, label, explicit = link_record, "", True
                source_page_url, rendered = source_url, False
            else:
                website = link_record.get("url", "")
                label = link_record.get("label", "")
                explicit = bool(link_record.get("explicit_website"))
                source_page_url = link_record.get("source_page_url", source_url)
                rendered = bool(link_record.get("rendered"))
            # Search-discovered bridge pages are less trustworthy than the
            # supplied exhibitor profile. Only their explicitly labelled
            # website field may create a candidate.
            if not explicit:
                continue
            domain = scorer.normalize_domain(website)
            if (
                not domain or scorer.is_excluded_domain(domain)
                or scorer.is_foreign_country_domain(domain)
                or scorer.same_registrable_domain(domain, source.get("domain", ""))
            ):
                continue
            evidence = {
                "source_url": source_url, "source_page_url": source_page_url,
                "query": source.get("query", ""), "rank": source.get("rank", 0),
                "title": source.get("title", ""), "snippet": source.get("snippet", ""),
                "rendered": rendered,
            }
            existing = candidates_by_domain.get(domain)
            if existing:
                items = list(existing.get("_search_bridge_evidence", ()))
                if evidence not in items:
                    items.append(evidence)
                existing["_search_bridge_evidence"] = items
                continue
            role = _candidate_role(company_name, website, label, "")
            candidates_by_domain[domain] = {
                "domain": domain,
                "url": _canonical_site_url(website),
                "score": 72,
                "title": "",
                "snippet": "",
                "query": "search_bridge_profile",
                "rank": rank,
                "reason": "labelled_search_bridge_outbound_discovery; discovery_only_not_identity_authority",
                "role": "company_candidate" if role in {"unknown", "company_candidate"} else role,
                "_official_query_evidence": 0,
                "_query_trust_bonus": 0,
                "_metadata_context_matches": 0,
                "_search_bridge_evidence": [evidence],
            }


def find_candidate_domains(company_name: str, metadata: dict | None = None) -> list[dict]:
    if aliases.has_no_website(company_name):
        discovery_coverage.finalize_company(
            company_name, resolved=True, candidate_count=0,
        )
        return CandidateList([], [{"source": "human_alias", "status": "verified_no_website"}])
    candidates_by_domain: dict[str, dict] = {}
    trace: list[dict] = []
    bridge_sources: dict[str, dict] = {}
    expanded_bridge_urls: set[str] = set()
    executed_queries: set[str] = set()
    related_name_hints: list[str] = []
    _add_verified_alias_candidate(candidates_by_domain, company_name)
    _add_profile_candidates(candidates_by_domain, company_name, metadata)
    source_health = _source_health_snapshot((metadata or {}).get("profile_url", ""))
    if source_health.get("host"):
        trace.append({"source": "exhibitor_profile_health", **source_health})
    def run_query(
        query: str,
        phase: str,
        evidence_gaps: set[str] | None = None,
    ) -> list[dict]:
        if not query or query in executed_queries:
            return []
        executed_queries.add(query)
        results = _safe_search_text(query)
        observed_gaps = evidence_gaps or _adaptive_discovery_gaps(
            company_name, candidates_by_domain, related_name_hints,
        )
        discovery_coverage.record_query(
            company_name,
            query,
            phase,
            getattr(results, "cache_status", "unknown"),
            len(results),
            observed_gaps,
        )
        trace.append({
            "source": config.SEARCH_PROVIDER, "query": query,
            "phase": phase,
            "cache_status": getattr(results, "cache_status", "unknown"),
            "result_count": len(results), "results": results,
        })
        for result in results:
            for hint in _related_name_hints(
                company_name,
                result.get("title", ""),
                result.get("body", "") or result.get("snippet", ""),
            ):
                if hint not in related_name_hints:
                    related_name_hints.append(hint)
        _collect_search_bridge_sources(
            bridge_sources, company_name, query, results, metadata,
        )
        _add_search_results(candidates_by_domain, company_name, query, results, metadata)
        return results

    primary_queries = _primary_queries(company_name, metadata)
    paid_total_limit = 0
    if (
        config.SEARCH_PROVIDER == "brightdata"
        and config.SEARCH_CACHE_MODE != "replay"
    ):
        paid_total_limit = (
            config.MAX_SEARCH_QUERIES_PER_COMPANY
            if config.MAX_SEARCH_QUERIES_PER_COMPANY > 0
            else config.DEFAULT_PAID_SEARCH_QUERY_LIMIT
        )
        if paid_total_limit > 0:
            reserve = min(config.PAID_SEARCH_ADAPTIVE_RESERVE, max(paid_total_limit - 1, 0))
            primary_queries = query_planner.diverse_queries(
                primary_queries,
                max(paid_total_limit - reserve, 1),
            )
    elif (
        config.SEARCH_PROVIDER == "brightdata"
        and config.MAX_SEARCH_QUERIES_PER_COMPANY <= 0
        and config.DEFAULT_PAID_SEARCH_QUERY_LIMIT > 0
    ):
        # Offline replay may consult every legacy primary cache key without
        # consuming the paid allowance; this keeps old regressions comparable.
        primary_queries = primary_queries[: config.DEFAULT_PAID_SEARCH_QUERY_LIMIT]
    for query in primary_queries:
        run_query(query, "primary")
        best = _best_candidate(candidates_by_domain)
        if (
            best
            and best["score"] >= config.EARLY_STOP_SCORE_THRESHOLD
            and _can_early_stop(company_name, best, metadata)
        ):
            break

    _expand_search_bridge_candidates(
        candidates_by_domain, company_name, bridge_sources, trace, expanded_bridge_urls,
    )

    if _discovery_needs_expansion(company_name, candidates_by_domain, metadata):
        adaptive_queries: list[str] = []
        adaptive_states: list[dict] = []
        while len(adaptive_queries) < config.MAX_ADAPTIVE_SEARCH_QUERIES:
            if paid_total_limit > 0 and len(executed_queries) >= paid_total_limit:
                break
            gaps = _adaptive_discovery_gaps(
                company_name, candidates_by_domain, related_name_hints,
            )
            planned = _adaptive_queries(
                company_name, metadata, executed_queries,
                related_name_hints, evidence_gaps=gaps,
            )
            if not planned:
                break
            query = planned[0]
            adaptive_queries.append(query)
            adaptive_states.append({"query": query, "evidence_gaps": sorted(gaps)})
            results = run_query(query, "adaptive", gaps)
            hint_queries = {
                f'"{hint}" Turkiye official website': hint
                for hint in related_name_hints if hint
            }
            if query in hint_queries:
                _add_related_hint_results(
                    candidates_by_domain, company_name, hint_queries[query], query, results,
                )
            best = _best_candidate(candidates_by_domain)
            if (
                best and best.get("score", 0) >= config.EARLY_STOP_SCORE_THRESHOLD
                and _can_early_stop(company_name, best, metadata)
                and not _discovery_needs_expansion(company_name, candidates_by_domain, metadata)
            ):
                break
        trace.append({
            "source": "adaptive_discovery", "status": "expanded",
            "planned_queries": adaptive_queries,
            "states": adaptive_states,
        })
        _expand_search_bridge_candidates(
            candidates_by_domain, company_name, bridge_sources, trace, expanded_bridge_urls,
        )

    best = _best_candidate(candidates_by_domain)
    if not best or best["score"] < config.MIN_ACCEPT_SCORE:
        for query in _fallback_queries(company_name, metadata):
            if paid_total_limit > 0 and len(executed_queries) >= paid_total_limit:
                break
            run_query(
                query,
                "fallback",
                _adaptive_discovery_gaps(
                    company_name, candidates_by_domain, related_name_hints,
                ),
            )
            best = _best_candidate(candidates_by_domain)
            if best and best["score"] >= config.EARLY_STOP_SCORE_THRESHOLD and _can_early_stop(company_name, best, metadata):
                break
        _expand_search_bridge_candidates(
            candidates_by_domain, company_name, bridge_sources, trace, expanded_bridge_urls,
        )

    best = _best_candidate(candidates_by_domain)
    if (
        _discovery_needs_expansion(company_name, candidates_by_domain, metadata)
        and (
            config.ENABLE_BRANDFETCH_DOMAIN_SEARCH
            or config.ENABLE_HUNTER_DOMAIN_FINDER
        )
    ):
        _add_resolver_candidates(candidates_by_domain, company_name, trace)
        best = _best_candidate(candidates_by_domain)

    if not best or best["score"] < config.MIN_ACCEPT_SCORE:
        _add_google_places_results(candidates_by_domain, company_name)
        trace.append({"source": "google_places", "status": "consulted_after_search_miss"})

    best = _best_candidate(candidates_by_domain)
    has_domain_identity_candidate = any(
        "search_text_identity:" not in candidate.get("reason", "")
        for candidate in candidates_by_domain.values()
    )
    if (
        config.SEARCH_CACHE_MODE != "replay"
        and (not best or best["score"] < config.MIN_ACCEPT_SCORE or not has_domain_identity_candidate)
    ):
        _add_domain_guesses(candidates_by_domain, company_name)

    discovery_coverage.finalize_company(
        company_name,
        resolved=bool(
            best
            and best.get("score", 0) >= config.MIN_ACCEPT_SCORE
            and not _discovery_needs_expansion(
                company_name, candidates_by_domain, metadata,
            )
        ),
        candidate_count=sum(
            1 for item in candidates_by_domain.values()
            if item.get("role") not in DISCOVERY_ONLY_ROLES
        ),
    )
    return CandidateList(
        sorted(candidates_by_domain.values(), key=_candidate_rank_key, reverse=True),
        trace,
        source_health,
    )


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """Rank publication candidates before discovery-only bridge pages."""
    return sorted(candidates, key=_candidate_rank_key, reverse=True)
