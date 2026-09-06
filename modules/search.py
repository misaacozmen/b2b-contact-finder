import json
import logging
import math
import os
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
from modules import (
    aliases,
    cache_store,
    checkpoint,
    company_resolvers,
    crawler,
    discovery_coverage,
    discovery_rules,
    entity_memory,
    exhibitor_scraper,
    extractor,
    google_places,
    query_planner,
    run_budget,
    runtime,
    scorer,
    site_mapper,
    source_adapters,
)

DISCOVERY_ONLY_ROLES = discovery_rules.DISCOVERY_ONLY_ROLES


LOGGER = logging.getLogger("contact_finder")
PREFERRED_BACKENDS = ["duckduckgo", "google", "brave", "yahoo", "yandex"]
FALLBACK_BACKENDS = ["mojeek", "grokipedia"]
_SOURCE_HEALTH_LOCK = threading.Lock()
_SOURCE_HEALTH: dict[str, dict] = {}
_SOURCE_PROFILE_HTTP_LOCKS_LOCK = threading.Lock()
_SOURCE_PROFILE_HTTP_LOCKS: dict[str, threading.Lock] = {}
_BRIGHTDATA_RATE_LOCK = threading.Lock()
_BRIGHTDATA_NEXT_REQUEST_AT = 0.0
_BRIGHTDATA_CIRCUIT_LOCK = threading.Lock()
_BRIGHTDATA_CONSECUTIVE_FAILURES = 0
_BRIGHTDATA_CIRCUIT_OPEN_UNTIL = 0.0
_DDGS_FAILURE_LOCK = threading.Lock()
_DDGS_CONSECUTIVE_FAILURES: dict[str, int] = {}
_DDGS_CIRCUIT_OPEN: set[str] = set()
_BRIGHTDATA_INFLIGHT = threading.BoundedSemaphore(
    config.BRIGHTDATA_MAX_INFLIGHT_QUERIES
)
_ROLE_PRIORITY = discovery_rules.ROLE_PRIORITY


class SearchBackendError(RuntimeError):
    pass


class BrightDataSearchError(RuntimeError):
    pass


class BrightDataProviderRejected(BrightDataSearchError):
    def __init__(self, provider_result):
        self.provider_result = provider_result
        super().__init__(provider_result.result_reason or provider_result.result_state)


class SearchBudgetExhausted(BrightDataSearchError):
    result_state = "BLOCKED_BUDGET"
    reason = "budget_exhausted"


class CandidateList(list):
    def __init__(self, values=(), trace: list[dict] | None = None, source_health: dict | None = None):
        super().__init__(values)
        self.trace = trace or []
        self.source_health = source_health or {}


class SearchResults(list):
    def __init__(self, values=(), cache_status: str = "unknown", provider: str = "", *, result_state: str = "EMPTY", reason: str = "", result_reason: str | None = None, call_ids: tuple[str, ...] = ()):
        super().__init__(values)
        self.cache_status = cache_status
        self.provider = provider
        self.result_state = str(result_state)
        self.result_reason = str(result_reason if result_reason is not None else reason)
        self.call_ids = tuple(str(value) for value in call_ids if value)
        runtime.record_provider_outcome(
            state=self.result_state, reason=self.result_reason,
            call_ids=self.call_ids, provider=self.provider,
        )


def reset_source_health() -> None:
    global _BRIGHTDATA_NEXT_REQUEST_AT, _BRIGHTDATA_CONSECUTIVE_FAILURES
    global _BRIGHTDATA_CIRCUIT_OPEN_UNTIL
    with _SOURCE_HEALTH_LOCK:
        _SOURCE_HEALTH.clear()
    with _BRIGHTDATA_RATE_LOCK:
        _BRIGHTDATA_NEXT_REQUEST_AT = 0.0
    with _BRIGHTDATA_CIRCUIT_LOCK:
        _BRIGHTDATA_CONSECUTIVE_FAILURES = 0
        _BRIGHTDATA_CIRCUIT_OPEN_UNTIL = 0.0
    with _DDGS_FAILURE_LOCK:
        _DDGS_CONSECUTIVE_FAILURES.clear()
        _DDGS_CIRCUIT_OPEN.clear()


def reset_candidate_host_observations() -> None:
    """Compatibility hook; candidate roles no longer depend on run order."""


def _brightdata_circuit_open() -> bool:
    with _BRIGHTDATA_CIRCUIT_LOCK:
        return time.monotonic() < _BRIGHTDATA_CIRCUIT_OPEN_UNTIL


def _record_brightdata_result(success: bool) -> None:
    global _BRIGHTDATA_CONSECUTIVE_FAILURES, _BRIGHTDATA_CIRCUIT_OPEN_UNTIL
    with _BRIGHTDATA_CIRCUIT_LOCK:
        if success:
            _BRIGHTDATA_CONSECUTIVE_FAILURES = 0
            _BRIGHTDATA_CIRCUIT_OPEN_UNTIL = 0.0
            return
        _BRIGHTDATA_CONSECUTIVE_FAILURES += 1
        if (
            _BRIGHTDATA_CONSECUTIVE_FAILURES
            >= config.BRIGHTDATA_CIRCUIT_FAILURE_THRESHOLD
        ):
            _BRIGHTDATA_CIRCUIT_OPEN_UNTIL = (
                time.monotonic() + config.BRIGHTDATA_CIRCUIT_COOLDOWN_SEC
            )
            runtime.record("search.provider_circuit_opened")


_RUN_PAID_QUERY_LIMIT: int | None = None


def configure_run_budget(company_count: int) -> int:
    global _RUN_PAID_QUERY_LIMIT
    _RUN_PAID_QUERY_LIMIT = run_budget.configure_run_budget(company_count)
    return _RUN_PAID_QUERY_LIMIT


def scale_paid_api_budgets(company_count: int) -> dict[str, int]:
    return run_budget.scale_paid_api_budgets(company_count)


def _effective_paid_query_limit() -> int:
    if _RUN_PAID_QUERY_LIMIT is not None:
        return _RUN_PAID_QUERY_LIMIT
    return (
        config.MAX_SEARCH_QUERIES_PER_COMPANY
        if config.MAX_SEARCH_QUERIES_PER_COMPANY > 0
        else config.DEFAULT_PAID_SEARCH_QUERY_LIMIT
    )


def _strongest_candidate_role(*roles: str) -> str:
    return discovery_rules.strongest_candidate_role(*roles)

def _source_health_key(url: str) -> str:
    return scorer.normalize_domain(url)


def _source_profile_http_lock(url: str) -> threading.Lock:
    """Serialize first-party profile requests per host.

    A source profile host is shared by every exhibitor record in that source.
    Keeping its low-rate HTTP calls ordered avoids turning ordinary host
    throttling into false source-profile misses when company workers overlap.
    """
    key = _source_health_key(url)
    with _SOURCE_PROFILE_HTTP_LOCKS_LOCK:
        return _SOURCE_PROFILE_HTTP_LOCKS.setdefault(key, threading.Lock())


def _source_health_storage_key(url: str) -> tuple[str, str]:
    return (runtime.durable_run_id() or "volatile", _source_health_key(url))


def _source_health_snapshot(url: str) -> dict:
    key = _source_health_key(url)
    if not key:
        return {"status": "not_configured", "host": ""}
    with _SOURCE_HEALTH_LOCK:
        state = dict(_SOURCE_HEALTH.get(_source_health_storage_key(url), {}))
    return {"host": key, "status": state.get("status", "unknown"), **state}


def _record_source_health(url: str, status: str, http_status: int | None = None, cooldown_seconds: float | None = None) -> dict:
    key = _source_health_key(url)
    if not key:
        return {"status": "not_configured", "host": ""}
    with _SOURCE_HEALTH_LOCK:
        state = _SOURCE_HEALTH.setdefault(_source_health_storage_key(url), {
            "host": key, "status": "unknown", "attempts": 0,
            "successes": 0, "server_errors": 0, "circuit_open": False,
            "direct_probe_attempted": False, "renderer_probe_attempted": False, "terminal_blocked": False,
            "cooldown_until": 0.0,
        })
        if status != "circuit_open":
            state["attempts"] += 1
        if status == "available":
            state["successes"] += 1
            state["server_errors"] = 0
            state["circuit_open"] = False
            state["terminal_blocked"] = False
        elif status == "server_error":
            state["server_errors"] += 1
            if state["server_errors"] >= config.SOURCE_PROFILE_MAX_SERVER_ERRORS:
                state["circuit_open"] = True
                status = "degraded"
        state["status"] = status
        if status == "direct_probe_attempted":
            state["direct_probe_attempted"] = True
        if status == "renderer_probe_attempted":
            state["renderer_probe_attempted"] = True
        if status == "blocked":
            state["terminal_blocked"] = True
        if http_status == 429:
            state["cooldown_until"] = time.time() + max(0.0, float(cooldown_seconds if cooldown_seconds is not None else 60.0))
        if http_status is not None:
            state["last_http_status"] = http_status
        return dict(state)


def _hydrate_source_health(url: str, snapshot: dict | None) -> dict:
    key = _source_health_key(url)
    if not key or not isinstance(snapshot, dict):
        return _source_health_snapshot(url)
    with _SOURCE_HEALTH_LOCK:
        state = dict(snapshot)
        state.setdefault("host", key)
        state.setdefault("cooldown_until", 0.0)
        _SOURCE_HEALTH[_source_health_storage_key(url)] = state
    return _source_health_snapshot(url)


def _probe_with_heartbeat(profile_url: str, *, run_id: str, host: str, owner_token: str) -> None:
    """Keep the durable owner lease alive across a slow physical probe."""
    stop = threading.Event()
    heartbeat_error: list[BaseException] = []
    lease_seconds = max(1.0, float(getattr(config, "SOURCE_PROBE_LEASE_SEC", 30)))
    interval = max(0.05, min(5.0, lease_seconds / 3.0))

    def beat() -> None:
        while not stop.wait(interval):
            try:
                checkpoint.heartbeat_source_probe(
                    run_id=run_id, host=host, owner_token=owner_token,
                )
            except BaseException as exc:
                heartbeat_error.append(exc)
                stop.set()
                return

    worker = threading.Thread(target=beat, name=f"source-probe-heartbeat-{host}", daemon=True)
    worker.start()
    try:
        _profile_external_websites(profile_url)
    finally:
        stop.set()
        worker.join(timeout=max(1.0, interval * 2.0))
    if heartbeat_error:
        raise RuntimeError(f"source probe heartbeat failed: {heartbeat_error[0]}") from heartbeat_error[0]


def _is_transient_probe_error(exc: BaseException) -> bool:
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code is not None and int(status_code) >= 500:
        return True
    message = str(exc).casefold()
    return runtime.is_unknown_transport_error(exc) or any(
        token in message for token in ("transient", "temporar", "server error", "5xx")
    )


def preflight_source_profiles(records: list[dict], *, run_id: str | None = None) -> list[dict]:
    """Probe each distinct fair host once before parallel company processing."""
    first_profile_by_host: dict[str, str] = {}
    for record in records:
        profile_url = str(record.get("profile_url", "") or "").strip()
        host = _source_health_key(profile_url)
        if host and host not in first_profile_by_host:
            first_profile_by_host[host] = profile_url
    max_attempts = max(1, int(getattr(config, "SOURCE_PROFILE_MAX_TRANSIENT_RETRIES", 2)))
    for profile_url in first_profile_by_host.values():
        host = _source_health_key(profile_url)
        runtime.record("source_profile.preflight_hosts")
        if not run_id or not host:
            _profile_external_websites(profile_url)
            continue
        for attempt in range(max_attempts):
            claim = checkpoint.claim_source_probe(run_id=run_id, host=host)
            if claim.get("state") == "DONE":
                _hydrate_source_health(profile_url, claim.get("snapshot"))
                break
            if not claim.get("owner"):
                snapshot = checkpoint.wait_source_probe(run_id=run_id, host=host)
                if snapshot is not None:
                    _hydrate_source_health(profile_url, snapshot)
                    break
                continue
            finished = False

            def finish_once(snapshot: dict, error: str = "") -> None:
                nonlocal finished
                if finished:
                    return
                finished = True
                checkpoint.finish_source_probe(
                    run_id=run_id, host=host, owner_token=str(claim["owner_token"]),
                    snapshot=snapshot, error=error,
                )

            try:
                _probe_with_heartbeat(
                    profile_url, run_id=run_id, host=host,
                    owner_token=str(claim["owner_token"]),
                )
                probe_snapshot = _source_health_snapshot(profile_url)
                transient_failure = probe_snapshot.get("status") in {"server_error", "degraded", "unavailable", "cooldown"}
                transient_error = RuntimeError(
                    f"source probe transient status={probe_snapshot.get('status')} "
                    f"http_status={probe_snapshot.get('last_http_status', '')}"
                ) if transient_failure else None
                finish_once(probe_snapshot, error=str(transient_error or ""))
                if transient_failure:
                    if attempt + 1 >= max_attempts:
                        raise RuntimeError(
                            f"source preflight exhausted bounded retries: {host}: {transient_error}"
                        ) from transient_error
                    continue
            except BaseException as exc:
                try:
                    finish_once(_source_health_snapshot(profile_url), error=str(exc))
                except BaseException as finish_error:
                    raise finish_error from exc
                if _is_transient_probe_error(exc) and attempt + 1 < max_attempts:
                    continue
                if _is_transient_probe_error(exc):
                    raise RuntimeError(
                        f"source preflight exhausted bounded retries: {host}: {exc}"
                    ) from exc
                raise
            break
        else:
            raise RuntimeError(f"source preflight did not reach a terminal state: {host}")
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
    return discovery_rules.result_url(result)


def _canonical_site_url(raw_url: str) -> str:
    return discovery_rules.canonical_site_url(raw_url)


def _ddgs_text(query: str) -> SearchResults:
    if not runtime.reserve_search_query(config.SEARCH_HTTP_REQUEST_BUDGET, bucket=runtime.search_bucket()):
        LOGGER.warning("Free search query budget exhausted: %s", query)
        raise SearchBudgetExhausted("Free search query budget exhausted")
    empty_attempts = 0
    transport_errors: list[str] = []
    call_ids: list[str] = []
    physical_attempts = 0
    backends = [*PREFERRED_BACKENDS, *FALLBACK_BACKENDS]
    for backend in backends:
        with _DDGS_FAILURE_LOCK:
            if backend in _DDGS_CIRCUIT_OPEN:
                continue
        if physical_attempts >= 3:
            break
        physical_attempts += 1
        call_id = f"ddgs:{backend}:{physical_attempts}"
        call_ids.append(call_id)
        try:
            # DDGS may make a separate physical request per backend.  Each
            # attempt gets its own global limiter slot even though it consumes
            # one logical free query reservation.
            runtime.wait_for_request_slot()
            runtime.record("http.search.physical_http_requests")
            with DDGS(timeout=config.REQUEST_TIMEOUT_SEC) as ddgs:
                results = list(ddgs.text(query, max_results=config.SEARCH_RESULTS_PER_QUERY, backend=backend))
            if results:
                with _DDGS_FAILURE_LOCK:
                    _DDGS_CONSECUTIVE_FAILURES[backend] = 0
                return SearchResults(results, "live", "ddgs", result_state="COMPLETED", reason=f"backend:{backend}", call_ids=tuple(call_ids))
            with _DDGS_FAILURE_LOCK:
                _DDGS_CONSECUTIVE_FAILURES[backend] = 0
            empty_attempts += 1
            LOGGER.debug("DDGS backend '%s' returned 0 results for '%s'", backend, query)
            if empty_attempts >= 2 and not transport_errors:
                return SearchResults([], "live", "ddgs", result_state="EMPTY", reason="two_typed_empty_backends", call_ids=tuple(call_ids))
        except DDGSException as exc:
            message = str(exc).lower()
            if "no results" in message:
                empty_attempts += 1
                LOGGER.debug("DDGS backend '%s' no results for '%s'", backend, query)
                with _DDGS_FAILURE_LOCK:
                    _DDGS_CONSECUTIVE_FAILURES[backend] = 0
                if empty_attempts >= 2 and not transport_errors:
                    return SearchResults([], "live", "ddgs", result_state="EMPTY", reason="two_typed_empty_backends", call_ids=tuple(call_ids))
                continue
            LOGGER.debug("DDGS backend '%s' error for '%s': %s", backend, query, exc)
            transport_errors.append(f"{backend}:{type(exc).__name__}:{exc}")
            with _DDGS_FAILURE_LOCK:
                failures = _DDGS_CONSECUTIVE_FAILURES.get(backend, 0) + 1
                _DDGS_CONSECUTIVE_FAILURES[backend] = failures
                if failures >= 3:
                    _DDGS_CIRCUIT_OPEN.add(backend)
                    runtime.record("search.ddgs_backend_circuit_opened")
        except Exception as exc:
            LOGGER.debug("DDGS backend '%s' failed for '%s': %s", backend, query, exc)
            transport_errors.append(f"{backend}:{type(exc).__name__}:{exc}")
            with _DDGS_FAILURE_LOCK:
                failures = _DDGS_CONSECUTIVE_FAILURES.get(backend, 0) + 1
                _DDGS_CONSECUTIVE_FAILURES[backend] = failures
                if failures >= 3:
                    _DDGS_CIRCUIT_OPEN.add(backend)
                    runtime.record("search.ddgs_backend_circuit_opened")
    if empty_attempts >= 2 and not transport_errors:
        return SearchResults([], "live", "ddgs", result_state="EMPTY", reason="two_typed_empty_backends", call_ids=tuple(call_ids))
    if transport_errors:
        return SearchResults([], "live", "ddgs", result_state="FAILED", reason=";".join(transport_errors[:3]), call_ids=tuple(call_ids))
    return SearchResults([], "live", "ddgs", result_state="UNKNOWN", reason="ddgs_no_healthy_backend", call_ids=tuple(call_ids))


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


def _brightdata_post(url: str, *, _attempt_ordinal: int = 1, **kwargs) -> requests.Response:
    request_body = {"url": url, "json": kwargs.get("json", {}), "ordinal": int(_attempt_ordinal)}
    reservation = runtime.reserve_api("brightdata", operation=f"search.attempt_{_attempt_ordinal}", request_fingerprint=runtime.request_fingerprint("brightdata", "search", request_body))
    if not reservation:
        rejected = runtime.rejected_provider_result(reservation)
        if "duplicate" in str(reservation.reason or ""):
            raise BrightDataProviderRejected(rejected)
        error = SearchBudgetExhausted(f"Bright Data request rejected: {reservation.reason or 'budget_exhausted'}")
        error.result_reason = reservation.reason or "budget_exhausted"
        raise error
    global _BRIGHTDATA_NEXT_REQUEST_AT
    requests_per_minute = max(config.BRIGHTDATA_REQUESTS_PER_MINUTE, 0.0)
    if requests_per_minute:
        interval = 60.0 / requests_per_minute
        with _BRIGHTDATA_RATE_LOCK:
            now = time.monotonic()
            wait = max(0.0, _BRIGHTDATA_NEXT_REQUEST_AT - now)
            _BRIGHTDATA_NEXT_REQUEST_AT = max(
                now, _BRIGHTDATA_NEXT_REQUEST_AT,
            ) + interval
        if wait:
            time.sleep(wait)
    runtime.wait_for_request_slot()
    try:
        response = requests.post(url, timeout=config.BRIGHTDATA_TIMEOUT_SEC, **kwargs)
    except Exception as exc:
        state = "UNKNOWN" if runtime.is_unknown_transport_error(exc) else "FAILED"
        runtime.complete_api(reservation, state)
        exc.provider_call_id = getattr(reservation, "call_id", "")
        exc.request_fingerprint = runtime.request_fingerprint("brightdata", "search", request_body)
        raise
    runtime.complete_api(reservation, "FAILED" if response.status_code >= 400 else "DONE")
    response.provider_call_id = getattr(reservation, "call_id", "")
    response.request_fingerprint = runtime.request_fingerprint("brightdata", "search", request_body)
    return response


def _brightdata_text(query: str) -> list[dict]:
    if not config.BRIGHTDATA_API_KEY:
        raise BrightDataSearchError("BRIGHTDATA_API_KEY is not set")
    runtime.record("api.brightdata.queries")

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
    call_ids: list[str] = []
    last_error: requests.RequestException | None = None
    for attempt in range(config.MAX_RETRIES + 2):
        try:
            response = _brightdata_post(
                config.BRIGHTDATA_ENDPOINT,
                _attempt_ordinal=attempt + 1,
                json=payload,
                headers=headers,
            )
            if getattr(response, "provider_call_id", ""):
                call_ids.append(response.provider_call_id)
            break
        except BrightDataProviderRejected as exc:
            rejected = exc.provider_result
            return SearchResults([], "live", "brightdata", result_state=rejected.result_state, reason=rejected.result_reason, call_ids=rejected.call_ids)
        except requests.RequestException as exc:
            last_error = exc
            if runtime.is_unknown_transport_error(exc):
                raise BrightDataSearchError(f"Bright Data physical attempt is UNKNOWN: {exc}") from exc
            if attempt >= config.MAX_RETRIES + 1:
                raise BrightDataSearchError(f"Bright Data request timed out/failed after retries: {exc}") from exc
            time.sleep(_retry_delay(None, attempt))
        except Exception as exc:
            raise BrightDataSearchError(
                f"Bright Data physical attempt is {'UNKNOWN' if runtime.is_unknown_transport_error(exc) else 'FAILED'}: {exc}"
            ) from exc
    if response is None:
        raise BrightDataSearchError(f"Bright Data request failed: {last_error}")
    if response.status_code == 401:
        raise BrightDataSearchError("Bright Data authentication failed; check BRIGHTDATA_API_KEY")
    if response.status_code in {429, 500, 502, 503, 504}:
        last_detail = response.text[:500].replace("\n", " ")
        for attempt in range(config.MAX_RETRIES + 1):
            time.sleep(_retry_delay(response, attempt))
            runtime.record("api.brightdata.retries")
            response = _brightdata_post(
                config.BRIGHTDATA_ENDPOINT,
                _attempt_ordinal=attempt + 2,
                json=payload,
                headers=headers,
            )
            if getattr(response, "provider_call_id", ""):
                call_ids.append(response.provider_call_id)
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
    for parse_attempt in range(config.BRIGHTDATA_MAX_DECODE_RETRIES + 1):
        try:
            data = _decode_brightdata_response(response)
            break
        except BrightDataSearchError as exc:
            decode_error = exc
            if parse_attempt >= config.BRIGHTDATA_MAX_DECODE_RETRIES:
                raise
            retry_delay = _retry_delay(response, parse_attempt)
            if not response.text.strip():
                retry_delay = max(
                    retry_delay, config.BRIGHTDATA_EMPTY_BODY_RETRY_SEC,
                )
                runtime.record("api.brightdata.empty_body_retries")
            cooldown = re.search(
                r"minimum\s+of\s+(\d+(?:\.\d+)?)\s+seconds?",
                str(exc),
                re.IGNORECASE,
            )
            if cooldown:
                retry_delay = max(
                    retry_delay,
                    min(float(cooldown.group(1)), config.MAX_RETRY_AFTER_SEC),
                )
                runtime.record("api.brightdata.cooldown_retries")
            time.sleep(retry_delay)
            runtime.record("api.brightdata.retries")
            response = _brightdata_post(
                config.BRIGHTDATA_ENDPOINT,
                _attempt_ordinal=config.MAX_RETRIES + 3 + parse_attempt,
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
    return SearchResults(results, "live", "brightdata", result_state="COMPLETED" if results else "EMPTY", reason="results" if results else "empty_response", call_ids=tuple(call_ids))


def _search_text_live(query: str) -> SearchResults:
    if config.SEARCH_PROVIDER == "brightdata":
        with _BRIGHTDATA_INFLIGHT:
            if _brightdata_circuit_open():
                raise BrightDataSearchError("Bright Data circuit is open")
            value = _brightdata_text(query)
    else:
        value = _ddgs_text(query)
    if not isinstance(value, SearchResults):
        raise SearchBackendError("live search adapter contract requires SearchResults")
    return value


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
            "cache_schema_version": config.CACHE_SCHEMA_VERSION,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _coerce_search_results(value, *, cache_status: str, provider: str) -> SearchResults:
    if isinstance(value, SearchResults):
        return SearchResults(
            value, cache_status, value.provider or provider,
            result_state=value.result_state,
            reason=value.result_reason,
            call_ids=value.call_ids,
        )
    if isinstance(value, dict) and value.get("__search_result_state"):
        return SearchResults(
            value.get("values", []) or [], cache_status,
            str(value.get("provider") or provider),
            result_state=str(value.get("__search_result_state") or "UNKNOWN"),
            reason=str(value.get("result_reason") or ""),
            call_ids=tuple(value.get("call_ids") or ()),
        )
    if str(cache_status).casefold() in {"live", "live_fallback", "circuit_fallback"}:
        raise SearchBackendError("live search adapter contract requires SearchResults")
    return SearchResults(
        value or [], cache_status, provider,
        result_state="COMPLETED" if value else "UNKNOWN",
        reason="legacy_cache_metadata_unavailable" if value else "unknown_legacy_empty",
    )


def _cache_search_value(results: SearchResults) -> dict:
    return {
        "__search_result_state": results.result_state,
        "values": list(results),
        "provider": results.provider,
        "result_reason": results.result_reason,
        "call_ids": list(results.call_ids),
    }


def _search_text(query: str) -> SearchResults:
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
            empty_ttl_days=getattr(config, "SEARCH_EMPTY_CACHE_TTL_DAYS", 1 / 24),
        )
        if cached is not None:
            LOGGER.info("Search cache hit: %s", query)
            if isinstance(cached, list) and not cached:
                if mode == "replay":
                    return SearchResults([], "replay_legacy_empty", config.SEARCH_PROVIDER, result_state="UNKNOWN", reason="unknown_legacy_empty")
                cached = None
            else:
                return _coerce_search_results(cached, cache_status="cache_hit", provider=config.SEARCH_PROVIDER)
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
                    empty_ttl_days=getattr(config, "SEARCH_EMPTY_CACHE_TTL_DAYS", 1 / 24),
                )
                if cached is not None:
                    LOGGER.info(
                        "Search replay cache fallback hit: query=%s provider=%s",
                        query, provider,
                    )
                    if isinstance(cached, list) and not cached:
                        return SearchResults([], "replay_legacy_empty", provider, result_state="UNKNOWN", reason="unknown_legacy_empty")
                    return _coerce_search_results(cached, cache_status="replay_fallback_hit", provider=provider)
            LOGGER.warning("Search replay cache miss: %s", query)
            return SearchResults([], "replay_miss", config.SEARCH_PROVIDER, result_state="UNKNOWN", reason="replay_miss")

    results = _search_text_live(query)
    if not isinstance(results, SearchResults):
        raise SearchBackendError("live search adapter contract requires SearchResults")
    if mode in {"use", "refresh"} and results.result_state in {"COMPLETED", "EMPTY"}:
        cache_store.save(
            config.SEARCH_CACHE_DIR,
            "serp",
            cache_key,
            _cache_search_value(results),
            config.CACHE_SCHEMA_VERSION,
        )
    return results


def _safe_search_text(query: str) -> list[dict]:
    """A single provider timeout must not discard every result for a firm."""
    if config.SEARCH_PROVIDER == "brightdata" and _brightdata_circuit_open():
        try:
            fallback = _ddgs_text(query)
            runtime.record(
                "search.circuit_fallback.success" if fallback
                else "search.circuit_fallback.empty"
            )
            if not isinstance(fallback, SearchResults):
                raise SearchBackendError("live search adapter contract requires SearchResults")
            return fallback
        except SearchBudgetExhausted:
            runtime.record("search.circuit_fallback.budget_blocked")
            return SearchResults([], "budget_blocked", "ddgs", result_state="BLOCKED_BUDGET", reason="fallback_budget_exhausted")
    try:
        results = _search_text(query)
        if config.SEARCH_PROVIDER == "brightdata" and getattr(results, "provider", "") == "brightdata" and getattr(results, "result_state", "") in {"COMPLETED", "EMPTY"}:
            _record_brightdata_result(True)
        return results
    except Exception as exc:
        LOGGER.warning("Search query failed; continuing with remaining queries: %s (%s)", query, exc)
        if not isinstance(exc, SearchBudgetExhausted):
            runtime.record("search.provider_failures")
        if config.SEARCH_PROVIDER == "brightdata":
            if not isinstance(exc, SearchBudgetExhausted):
                _record_brightdata_result(False)
            try:
                fallback = _ddgs_text(query)
                runtime.record(
                    "search.fallback.success" if fallback
                    else "search.fallback.empty"
                )
                if not isinstance(fallback, SearchResults):
                    raise SearchBackendError("live search adapter contract requires SearchResults")
                return fallback
            except SearchBudgetExhausted as fallback_exc:
                LOGGER.warning("Free search fallback budget exhausted: %s (%s)", query, fallback_exc)
                runtime.record("search.fallback.budget_blocked")
                return SearchResults([], "budget_blocked", "ddgs", result_state="BLOCKED_BUDGET", reason="fallback_budget_exhausted")
            except Exception as fallback_exc:
                LOGGER.warning("Free search fallback also failed: %s (%s)", query, fallback_exc)
                runtime.record("search.fallback.error")
        if isinstance(exc, SearchBudgetExhausted):
            return SearchResults([], "budget_blocked", config.SEARCH_PROVIDER, result_state="BLOCKED_BUDGET", reason=str(getattr(exc, "result_reason", "budget_exhausted")))
        state = "UNKNOWN" if isinstance(exc, (TimeoutError, requests.Timeout)) or "timeout" in type(exc).__name__.casefold() or getattr(exc, "result_state", "") == "UNKNOWN" else "FAILED"
        return SearchResults([], "error", config.SEARCH_PROVIDER, result_state=state, reason=str(exc))


def _metadata_query_terms(metadata: dict | None) -> list[str]:
    return discovery_rules.metadata_query_terms(metadata)


def _query_priority(query: str) -> int:
    return discovery_rules.query_priority(query)


def _query_trust_bonus(query: str) -> int:
    return discovery_rules.query_trust_bonus(query, query_priority_fn=_query_priority)


def _metadata_context_match_count(metadata: dict | None, text: str) -> int:
    return discovery_rules.metadata_context_match_count(metadata, text)


def _candidate_rank_key(item: dict) -> tuple[int, ...]:
    return discovery_rules.candidate_rank_key(item)


def _candidate_search_control_key(item: dict) -> tuple[int, ...]:
    return discovery_rules.candidate_search_control_key(
        item, candidate_rank_key_fn=_candidate_rank_key,
    )


def _candidate_role(company_name: str, url: str, title: str, snippet: str) -> str:
    return discovery_rules.candidate_role(company_name, url, title, snippet)


def _strongest_candidate_role(*roles: str) -> str:
    return discovery_rules.strongest_candidate_role(*roles, role_priority=_ROLE_PRIORITY)


def _snippet_outbound_websites(result: dict, source_url: str) -> list[str]:
    return discovery_rules.snippet_outbound_websites(
        result, source_url, canonical_site_url_fn=_canonical_site_url,
    )

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
        try:
            url = _result_url(result)
        except ValueError:
            runtime.record("search.malformed_link_items")
            continue
        domain = scorer.normalize_domain(url)
        if not domain or scorer.is_mirror_directory_domain(company_name, domain):
            if domain:
                runtime.record("search.candidate.mirror_rejected")
            else:
                runtime.record("search.malformed_link_items")
            continue

        title = result.get("title", "")
        snippet = result.get("body", "") or result.get("snippet", "")
        source_role = _candidate_role(company_name, url, title, snippet)
        # Indexed catalogues and public records often expose a company's own
        # website as bare text inside a PDF hosted on an otherwise ordinary
        # institutional domain.  PDF extraction remains discovery-only and is
        # still guarded by the legal/public-name check in the helper.
        try:
            pdf_result = urlparse(url).path.casefold().endswith(".pdf")
        except ValueError:
            runtime.record("search.malformed_link_items")
            continue
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
        identity_seed_urls = list(existing.get("_identity_seed_urls", ())) if existing else []
        contact_path = any(
            marker in scorer.normalize_text(unquote(url))
            for marker in ("contact", "iletisim", "bize-ulas")
        )
        # A deep contact result can surface under an official-site query too.
        # Keep it as a crawl seed only when the result itself carries the legal
        # name or an explicit owner/brand relationship; a generic deep link
        # must not steer crawling on weak search evidence alone.
        seed_identity_supported = bool(
            legal_name_evidence
            or ownership_evidence
            or (
                existing
                and (
                    existing.get("_legal_name_evidence")
                    or existing.get("_ownership_evidence")
                    or existing.get("_source_profile_evidence")
                )
            )
        )
        if (
            (_query_priority(query) == 0 or seed_identity_supported)
            and scorer.same_registrable_domain(domain, url)
            and contact_path
            and url not in contact_seed_urls
        ):
            contact_seed_urls.append(url)
        identity_kind = site_mapper.classify(unquote(url))
        if (
            (legal_name_evidence or ownership_evidence)
            and identity_kind in {
                "legal", "privacy", "terms", "about", "locations", "distributors",
            }
            and scorer.same_registrable_domain(domain, url)
            and url not in identity_seed_urls
        ):
            identity_seed_urls.append(url)
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
            "_identity_seed_urls": identity_seed_urls,
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
            existing["_identity_seed_urls"] = identity_seed_urls
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
            existing["_identity_seed_urls"] = identity_seed_urls
            existing["reason"] = re.sub(
                r"query_evidence:\d+; consensus_bonus:\d+",
                f"query_evidence:{len(evidence_queries)}; consensus_bonus:{consensus_bonus}",
                existing["reason"],
            )


def _best_candidate(candidates_by_domain: dict[str, dict]) -> dict | None:
    return discovery_rules.best_candidate(
        candidates_by_domain, candidate_rank_key_fn=_candidate_rank_key,
    )


def _can_early_stop(company_name: str, candidate: dict, metadata: dict | None = None) -> bool:
    return discovery_rules.can_early_stop(company_name, candidate, metadata=metadata)


@lru_cache(maxsize=4096)
def _domain_has_address(domain: str) -> bool:
    try:
        socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    return True


def _primary_queries(company_name: str, metadata: dict | None) -> list[str]:
    return discovery_rules.primary_queries(
        company_name,
        metadata,
        metadata_query_terms_fn=_metadata_query_terms,
        query_priority_fn=_query_priority,
    )


def _query_covers_full_identity(company_name: str, query: str) -> bool:
    return discovery_rules.query_covers_full_identity(company_name, query)


def _fallback_queries(company_name: str, metadata: dict | None) -> list[str]:
    return discovery_rules.fallback_queries(
        company_name,
        metadata,
        metadata_query_terms_fn=_metadata_query_terms,
        query_priority_fn=_query_priority,
    )


def _adaptive_queries(
    company_name: str,
    metadata: dict | None,
    already_run: set[str] | None = None,
    related_name_hints: list[str] | None = None,
    evidence_gaps: set[str] | None = None,
) -> list[str]:
    return discovery_rules.adaptive_queries(
        company_name,
        metadata,
        already_run=already_run,
        related_name_hints=related_name_hints,
        evidence_gaps=evidence_gaps,
        metadata_query_terms_fn=_metadata_query_terms,
    )


def _adaptive_discovery_gaps(
    company_name: str,
    candidates_by_domain: dict[str, dict],
    related_name_hints: list[str] | None = None,
) -> set[str]:
    return discovery_rules.adaptive_discovery_gaps(
        company_name,
        candidates_by_domain,
        related_name_hints=related_name_hints,
        candidate_rank_key_fn=_candidate_rank_key,
    )


def _related_name_hints(company_name: str, title: str, snippet: str) -> list[str]:
    return discovery_rules.related_name_hints(company_name, title, snippet)


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
    return discovery_rules.discovery_needs_expansion(
        company_name,
        candidates_by_domain,
        metadata,
        candidate_search_control_key_fn=_candidate_search_control_key,
        can_early_stop_fn=_can_early_stop,
    )

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
            "_google_places_evidence": [{
                "place_id": place.get("place_id", ""),
                "name": place.get("name", ""),
                "phone": place.get("phone", ""),
                "website": website,
            }],
        }
        existing = candidates_by_domain.get(domain)
        if existing is None:
            candidates_by_domain[domain] = candidate
            continue
        evidence = list(existing.get("_google_places_evidence", ()))
        evidence.extend(candidate["_google_places_evidence"])
        existing["_google_places_evidence"] = list({
            item.get("place_id") or f"{item.get('name')}|{item.get('website')}": item
            for item in evidence
        }.values())
        existing["external_phone"] = (
            existing.get("external_phone") or place.get("phone", "")
        )
        existing["score"] = max(existing.get("score", 0), candidate["score"])


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


def _add_entity_memory_candidates(
    candidates_by_domain: dict[str, dict],
    company_name: str,
) -> None:
    for rank, record in enumerate(entity_memory.candidates(company_name)):
        domain = scorer.normalize_domain(record.get("domain", ""))
        if (
            not domain
            or scorer.is_excluded_domain(domain)
            or domain in candidates_by_domain
        ):
            continue
        candidates_by_domain[domain] = {
            "domain": domain,
            "url": _canonical_site_url(domain),
            "score": min(
                config.PRE_CRAWL_SCORE_CAP,
                config.MIN_ACCEPT_SCORE + 5,
            ),
            "title": "",
            "snippet": "",
            "query": "verified_entity_memory",
            "rank": rank,
            "reason": (
                "verified_entity_memory_discovery_hint;"
                " requires_first_party_revalidation"
            ),
            "role": "company_candidate",
            "_entity_memory_evidence_urls": record.get("evidence_urls", []),
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


def _source_profile_requires_render(profile_url: str) -> bool:
    """Render the official Texhibition detail surface before contact parsing.

    The static response can expose the outbound website while leaving the
    visible exhibitor contact fields in the browser-rendered surface.  This is
    a source-level acquisition rule, not a company-specific exception.
    """
    return bool(
        config.ENABLE_JS_PROFILE_FALLBACK
        and scorer.normalize_domain(profile_url) == "texhibitionist.com"
    )


def _profile_external_websites(
    profile_url: str,
    *,
    allow_forced_render: bool = True,
) -> list[dict]:
    if not profile_url:
        return []
    cache_namespace = (
        "source_profile_links_v5_render"
        if allow_forced_render
        else "source_profile_links_v5_static"
    )
    if config.SEARCH_CACHE_MODE in {"use", "replay"}:
        cached = cache_store.load(
            config.SEARCH_CACHE_DIR, cache_namespace, profile_url,
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
    if health.get("terminal_blocked"):
        runtime.record("source_profile.terminal_blocked_skips")
        return []
    if health.get("direct_probe_attempted") and health.get("last_http_status") in {401, 403}:
        runtime.record("source_profile.direct_probe_latched")
        return []
    if float(health.get("cooldown_until", 0.0) or 0.0) > time.time():
        runtime.record("source_profile.cooldown_skips")
        return []
    if health.get("circuit_open"):
        runtime.record("source_profile.circuit_skips")
        _record_source_health(profile_url, "circuit_open")
        return []

    headers = {"User-Agent": config.USER_AGENT, "Accept-Language": "tr,en;q=0.8"}
    pages: list[tuple[str, str, bool]] = []
    try:
        _record_source_health(profile_url, "direct_probe_attempted")
        with _source_profile_http_lock(profile_url):
            response = crawler._request_with_safe_redirects(
                profile_url, verify=True, bucket="source_profile_http",
            )
        response_url = getattr(response, "_b2b_final_url", getattr(response, "url", profile_url))
        profile_html, rendered = _profile_render_fallback(
            response_url,
            response.text,
            force=_source_profile_requires_render(response_url),
        )
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
            _record_source_health(profile_url, "unavailable", status_code, _retry_delay(getattr(exc, "response", None), 0) if status_code == 429 else None)
        LOGGER.info("Exhibitor profile could not be read: %s (%s)", profile_url, exc)
        if status_code == 429:
            _record_source_health(profile_url, "cooldown", int(status_code), _retry_delay(getattr(exc, "response", None), 0))
            return []
        if status_code not in {401, 403}:
            return []
        if health.get("renderer_probe_attempted"):
            runtime.record("source_profile.renderer_probe_latched")
            _record_source_health(profile_url, "blocked", int(status_code))
            return []
        _record_source_health(profile_url, "renderer_probe_attempted", int(status_code))
        rendered_html, rendered = _profile_render_fallback(
            profile_url,
            force=allow_forced_render,
        )
        if not rendered:
            _record_source_health(profile_url, "blocked", int(status_code))
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
            with _source_profile_http_lock(detail_url):
                detail_response = crawler._request_with_safe_redirects(
                    detail_url, verify=True, bucket="source_profile_http",
                )
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
    profile_contact_evidence: list[dict] = []
    for page_url, page_html, rendered in pages:
        profile_host = scorer.normalize_domain(page_url)
        if profile_host in {"texhibitionist.com", "www.texhibitionist.com"}:
            details = exhibitor_scraper._texhibition_profile_details(page_html, page_url)
            contacts = {
                "emails": ([{
                    "value": details.get("listed_email", ""),
                    "retrieval_method": "browser_render" if rendered else "http",
                }] if details.get("listed_email") else []),
                "phones": ([{
                    "value": details.get("listed_phone", ""),
                    "retrieval_method": "browser_render" if rendered else "http",
                }] if details.get("listed_phone") else []),
            }
            # The rendered detail page can expose repeated contact fields in
            # visible text even when its label wrapper is not recognized by
            # the structured extractor.  Reuse the cleaned detail scope and
            # merge those observations without reintroducing catalogue chrome.
            _, detail_scope = exhibitor_scraper._texhibition_detail_scope(page_html)
            fallback_contacts = extractor.extract_contact_records(
                str(detail_scope), page_url,
                "browser_render" if rendered else "http",
            )
            for field in ("emails", "phones"):
                seen = {str(item.get("value", "")) for item in contacts[field]}
                for item in fallback_contacts.get(field, []):
                    value = str(item.get("value", ""))
                    if value and value not in seen:
                        contacts[field].append(item)
                        seen.add(value)
        else:
        # A catalogue's footer/header often contains the organiser's global
        # phone number.  It is not exhibitor evidence and must not satisfy the
        # source-profile contact gate.  Keep the extraction scope aligned with
        # the source-detail parser used elsewhere in the pipeline.
            contact_scope = BeautifulSoup(page_html, "html.parser")
            for node in contact_scope.select(
                "footer, header, nav, aside, script, style, noscript"
            ):
                node.decompose()
            contacts = extractor.extract_contact_records(
                str(contact_scope), page_url, "browser_render" if rendered else "http",
            )
        for field in ("emails", "phones"):
            for contact in contacts.get(field, []):
                profile_contact_evidence.append({
                    "field": "email" if field == "emails" else "phone",
                    "value": contact.get("value", ""),
                    "source_url": page_url,
                    "retrieval_method": contact.get("retrieval_method", "http"),
                })
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
                "profile_contact_evidence": profile_contact_evidence,
            })
    websites.sort(key=lambda item: bool(item.get("explicit_website")), reverse=True)
    websites = websites[:5]
    if config.SEARCH_CACHE_MODE in {"use", "refresh"}:
        cache_store.save(
            config.SEARCH_CACHE_DIR, cache_namespace, profile_url, websites,
            config.CACHE_SCHEMA_VERSION,
        )
    return websites


def _add_profile_candidates(candidates_by_domain: dict[str, dict], company_name: str, metadata: dict | None) -> None:
    listed_website = str((metadata or {}).get("listed_website", "") or "").strip()
    listed_field = "listed_website"
    if not listed_website:
        source_listed_status = str(
            (metadata or {}).get("source_listed_website_status", "") or ""
        ).strip().casefold()
        if source_listed_status == "present":
            listed_website = str(
                (metadata or {}).get("source_listed_website", "") or ""
            ).strip()
            listed_field = "source_listed_website"
    listing_url = str((metadata or {}).get("listing_url", "") or "").strip()
    listed_domain = scorer.normalize_domain(listed_website)
    listed_link = source_adapters.classify_link(
        listed_website,
        label=str((metadata or {}).get("listed_website_label", "Website") or "Website"),
        company_name=company_name,
    ) if listed_website else {"role": "unknown"}
    if listed_domain and listed_link["role"] == "company_candidate" and not scorer.is_excluded_domain(listed_domain):
        brand_tokens = scorer.domain_identity_tokens(company_name)
        candidates_by_domain[listed_domain] = {
            "domain": listed_domain,
            "url": listed_website,
            "score": config.PRE_CRAWL_SCORE_CAP,
            "title": "",
            "snippet": "",
            "query": "fair_listed_website",
            "rank": 0,
            "reason": f"{listed_field}_discovery_only",
            "role": "company_candidate",
            "_source_profile_evidence": 1,
            "_official_query_evidence": 0,
            "_query_trust_bonus": 0,
            "_metadata_context_matches": 0,
            # These signals only prioritize which domain to crawl. The fair
            # listing remains non-authoritative and cannot satisfy identity or
            # publication gates by itself.
            "_exact_brand_domain": (
                bool(brand_tokens)
                and scorer.compact_domain_core(listed_domain) == "".join(brand_tokens)
            ),
            "_public_brand_domain": scorer.public_brand_domain_match(
                company_name, listed_domain
            ),
            "_search_evidence": [{
                "source": "fair_listing",
                "source_field": listed_field,
                "profile_url": listing_url,
                "rank": 0,
            }],
            "_profile_url": listing_url,
            "_profile_source_page_url": listing_url,
        }

    profile_url = (metadata or {}).get("profile_url", "")
    for rank, link_record in enumerate(_profile_external_websites(profile_url), start=1):
        # String support keeps old fixtures and hand-written integrations
        # compatible; newly extracted records carry semantic link evidence.
        profile_contact_evidence: list[dict] = []
        if isinstance(link_record, str):
            website, label, explicit_website = link_record, "", True
            source_page_url = profile_url
        else:
            website = link_record.get("url", "")
            label = link_record.get("label", "")
            explicit_website = bool(link_record.get("explicit_website"))
            source_page_url = link_record.get("source_page_url", profile_url)
            profile_contact_evidence = list(link_record.get("profile_contact_evidence") or [])
        domain = scorer.normalize_domain(website)
        profile_contact_evidence = [
            item for item in profile_contact_evidence
            if item.get("field") == "phone"
            or (
                item.get("field") == "email"
                and "@" in str(item.get("value", ""))
                and scorer.same_registrable_domain(
                    domain,
                    str(item.get("value", "")).rsplit("@", 1)[-1],
                )
            )
        ]
        # Re-check cached profile links against the current exclusion policy;
        # old cache entries may predate a newly recognized catalogue host.
        if not domain or scorer.is_excluded_domain(domain):
            continue
        candidate_role = _candidate_role(company_name, website, label, "")
        # An unlabeled external link is evidence that a profile links out, not
        # evidence that the target is the company's first-party website.
        if candidate_role == "unknown":
            # Keep generic profile links as low-priority discovery bridges so
            # they remain inspectable, but never let them become authoritative
            # company candidates or publication websites.
            candidate_role = "company_candidate" if explicit_website else "discovery_bridge"
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
            "_source_profile_contact_evidence": profile_contact_evidence,
        }
        existing = candidates_by_domain.get(domain)
        if existing is None or candidate["score"] >= existing["score"]:
            candidates_by_domain[domain] = candidate


def find_profile_candidates(
    company_name: str,
    metadata: dict | None,
) -> CandidateList:
    """Discover only explicit company-site routes supplied by a source profile."""
    candidates_by_domain: dict[str, dict] = {}
    _add_profile_candidates(candidates_by_domain, company_name, metadata)
    source_health = _source_health_snapshot((metadata or {}).get("profile_url", ""))
    trace = []
    if source_health.get("host"):
        trace.append({"source": "exhibitor_profile_health", **source_health})
    return CandidateList(
        sorted(candidates_by_domain.values(), key=_candidate_rank_key, reverse=True),
        trace,
        source_health,
    )


def _bridge_entity_anchor_supported(company_name: str, title: str, source_url: str) -> bool:
    return discovery_rules.bridge_entity_anchor_supported(company_name, title, source_url)


def _bridge_identity_supported(
    company_name: str,
    title: str,
    snippet: str,
    metadata: dict | None,
    source_url: str = "",
) -> bool:
    return discovery_rules.bridge_identity_supported(
        company_name,
        title,
        snippet,
        metadata,
        source_url=source_url,
        bridge_entity_anchor_supported_fn=_bridge_entity_anchor_supported,
    )

def _collect_search_bridge_sources(
    target: dict[str, dict],
    company_name: str,
    query: str,
    results: list[dict],
    metadata: dict | None,
) -> None:
    discovery_rules.collect_search_bridge_sources(
        target,
        company_name,
        query,
        results,
        metadata,
        result_url_fn=_result_url,
        candidate_role_fn=_candidate_role,
        bridge_identity_supported_fn=_bridge_identity_supported,
    )

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
        links = _profile_external_websites(
            source_url,
            allow_forced_render=False,
        )
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


def find_candidate_domains(
    company_name: str,
    metadata: dict | None = None,
    *,
    profile_candidates: CandidateList | None = None,
) -> list[dict]:
    source_record_id = str((metadata or {}).get("source_record_id", "") or "").strip()
    original_index = (metadata or {}).get("original_index")
    if aliases.has_no_website(company_name):
        discovery_coverage.finalize_company(
            company_name, resolved=True, candidate_count=0,
            source_record_id=source_record_id, original_index=original_index,
        )
        return CandidateList([], [{"source": "human_alias", "status": "verified_no_website"}])
    candidates_by_domain: dict[str, dict] = {}
    trace: list[dict] = []
    bridge_sources: dict[str, dict] = {}
    expanded_bridge_urls: set[str] = set()
    executed_queries: set[str] = set()
    related_name_hints: list[str] = []
    full_identity_query_with_results = False
    discovery_query_count = 0
    discovery_budget_blocked = False

    def remove_mirror_candidates() -> None:
        rejected = [
            domain for domain in candidates_by_domain
            if scorer.is_mirror_directory_domain(company_name, domain)
        ]
        for domain in rejected:
            candidates_by_domain.pop(domain, None)
            runtime.record("search.candidate.mirror_rejected")

    _add_verified_alias_candidate(candidates_by_domain, company_name)
    _add_entity_memory_candidates(candidates_by_domain, company_name)
    if profile_candidates is None:
        _add_profile_candidates(candidates_by_domain, company_name, metadata)
    else:
        for candidate in profile_candidates:
            domain = scorer.normalize_domain(candidate.get("url", ""))
            if domain:
                candidates_by_domain[domain] = dict(candidate)
    remove_mirror_candidates()
    source_health = _source_health_snapshot((metadata or {}).get("profile_url", ""))
    if source_health.get("host"):
        trace.append({"source": "exhibitor_profile_health", **source_health})
    def run_query(
        query: str,
        phase: str,
        evidence_gaps: set[str] | None = None,
    ) -> list[dict]:
        nonlocal discovery_query_count, discovery_budget_blocked
        is_discovery_stage = phase != "evidence_completion"
        if is_discovery_stage and config.SEARCH_CACHE_MODE != "replay" and discovery_query_count >= int(getattr(config, "MAX_DISCOVERY_QUERIES_PER_COMPANY", 6)):
            if not discovery_budget_blocked:
                discovery_budget_blocked = True
                trace.append({"source": "ddgs", "phase": phase, "result_state": "BLOCKED_BUDGET", "result_reason": "discovery_query_limit"})
            return SearchResults([], "budget_blocked", "ddgs", result_state="BLOCKED_BUDGET", reason="discovery_query_limit")
        if not query or query in executed_queries:
            return []
        executed_queries.add(query)
        if is_discovery_stage and config.SEARCH_CACHE_MODE != "replay":
            discovery_query_count += 1
        previous_bucket = runtime.search_bucket()
        runtime.set_search_bucket("targeted" if phase == "evidence_completion" else "discovery")
        try:
            results = _safe_search_text(query)
        finally:
            runtime.set_search_bucket(previous_bucket)
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
            source_record_id=source_record_id,
            original_index=original_index,
        )
        trace.append({
            "source": getattr(results, "provider", "") or config.SEARCH_PROVIDER, "query": query,
            "phase": phase,
            "cache_status": getattr(results, "cache_status", "unknown"),
            "result_state": getattr(results, "result_state", "UNKNOWN"),
            "result_reason": getattr(results, "result_reason", ""),
            "call_ids": list(getattr(results, "call_ids", ())),
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
        if getattr(results, "result_state", "") == "BLOCKED_BUDGET":
            discovery_budget_blocked = True
        remove_mirror_candidates()
        return results

    primary_queries = _primary_queries(company_name, metadata)
    paid_total_limit = 0
    if (
        config.SEARCH_PROVIDER == "brightdata"
        and config.SEARCH_CACHE_MODE != "replay"
    ):
        paid_total_limit = _effective_paid_query_limit()
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
        results = run_query(query, "primary")
        if discovery_budget_blocked:
            break
        if results and _query_covers_full_identity(company_name, query):
            full_identity_query_with_results = True
        best = _best_candidate(candidates_by_domain)
        if (
            best
            and best["score"] >= config.EARLY_STOP_SCORE_THRESHOLD
            and full_identity_query_with_results
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
            if discovery_budget_blocked:
                break
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
            if discovery_budget_blocked:
                break
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
            if discovery_budget_blocked:
                break
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

    if config.ENABLE_GOOGLE_PLACES:
        _add_google_places_results(candidates_by_domain, company_name)
        remove_mirror_candidates()
        trace.append({
            "source": "google_places",
            "status": "consulted_for_identity_corroboration",
        })

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

    remove_mirror_candidates()

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
        source_record_id=source_record_id,
        original_index=original_index,
    )
    return CandidateList(
        sorted(candidates_by_domain.values(), key=_candidate_rank_key, reverse=True),
        trace,
        source_health,
    )


def find_targeted_candidates(
    company_name: str,
    metadata: dict | None,
    queries: list[str] | tuple[str, ...],
    *,
    limit: int = 2,
    already_run: set[str] | tuple[str, ...] | list[str] = (),
) -> CandidateList:
    """Run bounded gap-specific discovery; results still require site proof."""
    candidates_by_domain: dict[str, dict] = {}
    trace: list[dict] = []
    source_record_id = str((metadata or {}).get("source_record_id", "") or "").strip()
    original_index = (metadata or {}).get("original_index")
    attempted = {str(value).strip() for value in already_run if str(value).strip()}
    planned_queries = [query for query in dict.fromkeys(queries) if query not in attempted]
    targeted_limit = min(max(0, int(limit)), int(getattr(config, "MAX_TARGETED_QUERIES_PER_COMPANY", 4)))
    for query in planned_queries[:targeted_limit]:
        previous_bucket = runtime.search_bucket()
        runtime.set_search_bucket("targeted")
        try:
            results = _safe_search_text(query)
        finally:
            runtime.set_search_bucket(previous_bucket)
        discovery_coverage.record_query(
            company_name,
            query,
            "evidence_completion",
            getattr(results, "cache_status", "unknown"),
            len(results),
            {"post_crawl_evidence_gap"},
            source_record_id=source_record_id,
            original_index=original_index,
        )
        _add_search_results(
            candidates_by_domain, company_name, query, results, metadata,
        )
        if getattr(results, "result_state", "") == "BLOCKED_BUDGET":
            trace.append({"source": "ddgs", "query": query, "phase": "evidence_completion", "result_state": "BLOCKED_BUDGET", "result_reason": getattr(results, "result_reason", "budget_exhausted")})
            break
        rejected = [
            domain for domain in candidates_by_domain
            if scorer.is_mirror_directory_domain(company_name, domain)
        ]
        for domain in rejected:
            candidates_by_domain.pop(domain, None)
            runtime.record("search.candidate.mirror_rejected")
        trace.append({
            "source": getattr(results, "provider", "") or config.SEARCH_PROVIDER,
            "query": query,
            "phase": "evidence_completion",
            "cache_status": getattr(results, "cache_status", "unknown"),
            "result_state": getattr(results, "result_state", "UNKNOWN"),
            "result_reason": getattr(results, "result_reason", ""),
            "call_ids": list(getattr(results, "call_ids", ())),
            "result_count": len(results),
        })
        runtime.record("autonomy.targeted_queries")
    return CandidateList(
        sorted(
            candidates_by_domain.values(),
            key=_candidate_rank_key,
            reverse=True,
        ),
        trace,
    )


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """Rank publication candidates before discovery-only bridge pages."""
    return sorted(candidates, key=_candidate_rank_key, reverse=True)
