"""Thread-safe run budgets, global rate limiting and lightweight telemetry."""

from __future__ import annotations

import json
import importlib
import os
import threading
import time
from contextvars import ContextVar
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass

import config


_LOCK = threading.RLock()
_COUNTERS: Counter = Counter()
_STARTED_AT = time.monotonic()
_NEXT_REQUEST_AT = 0.0
_PHASE = "FREE"
_DURABLE_RUN_ID = ""
_DURABLE_BUDGETS: dict[str, int] = {}
_CURRENT_ITEM_INDEX: ContextVar[int] = ContextVar("current_item_index", default=-1)
_CURRENT_OPERATION: ContextVar[str] = ContextVar("current_operation", default="")
_CURRENT_PROVIDER_OUTCOMES: ContextVar[tuple[dict, ...] | None] = ContextVar("current_provider_outcomes", default=None)
_FREE_QUERY_COUNTS: dict[tuple[str, int, str], int] = {}
_CURRENT_SEARCH_BUCKET: ContextVar[str] = ContextVar("current_search_bucket", default="")

_PROVIDER_ALIASES = {
    "brightdata": "brightdata", "google_places": "google_places",
    "brandfetch": "brandfetch", "hunter": "hunter", "hunter_domain_finder": "hunter",
    "linkedin": "linkedin", "linkedin_company": "linkedin", "llm": "llm", "llm_arbiter": "llm",
}


@dataclass(frozen=True)
class Reservation:
    accepted: bool
    provider: str
    operation: str
    item_index: int
    phase: str
    call_id: str = ""
    reason: str = ""
    inherited_state: str = ""

    def __bool__(self) -> bool:
        return self.accepted


class ProviderResult(list):
    """List-compatible provider result carrying durable outcome metadata."""

    def __init__(self, values=(), *, state: str = "EMPTY", reason: str = "", call_ids: tuple[str, ...] = ()):
        super().__init__(values)
        self.state = str(state)
        self.reason = str(reason)
        self.result_state = self.state
        self.result_reason = self.reason
        self.call_ids = tuple(str(value) for value in call_ids if value)
        record_provider_outcome(
            state=self.state, reason=self.reason, call_ids=self.call_ids,
        )


def provider_result(values=(), *, state: str = "EMPTY", reason: str = "", call_ids: tuple[str, ...] = ()) -> ProviderResult:
    return ProviderResult(values, state=state, reason=reason, call_ids=call_ids)


def begin_provider_attempt():
    return _CURRENT_PROVIDER_OUTCOMES.set(())


def record_provider_outcome(*, state: str, reason: str = "", call_ids=(), provider: str = "") -> None:
    current = _CURRENT_PROVIDER_OUTCOMES.get()
    if current is None:
        return
    value = {
        "provider": str(provider),
        "result_state": str(state).upper(),
        "result_reason": str(reason),
        "call_ids": [str(call_id) for call_id in call_ids if call_id],
    }
    _CURRENT_PROVIDER_OUTCOMES.set((*current, value))


def end_provider_attempt(token) -> list[dict]:
    outcomes = list(_CURRENT_PROVIDER_OUTCOMES.get() or ())
    _CURRENT_PROVIDER_OUTCOMES.reset(token)
    return outcomes


def rejected_provider_result(reservation: Reservation) -> ProviderResult:
    reason = str(reservation.reason or "provider_rejected")
    if "duplicate" in reason:
        inherited = str(reservation.inherited_state or "").upper()
        # A test/double may only provide the duplicate rejection reason.  A
        # durable duplicate carries inherited_state and call_id; preserve the
        # legacy logical DUPLICATE result when that lookup is unavailable.
        state = {
            "DONE": "COMPLETED", "COMPLETED": "COMPLETED",
            "EMPTY": "EMPTY", "CACHE_HIT": "CACHE_HIT",
            "FAILED": "FAILED", "UNKNOWN": "UNKNOWN",
        }.get(inherited, "UNKNOWN" if inherited in {"RESERVED", "RUNNING"} else "DUPLICATE") if inherited else "DUPLICATE"
        return provider_result([], state=state, reason=reason, call_ids=(reservation.call_id,))
    return provider_result([], state="BLOCKED_BUDGET", reason=reason)


def unknown_provider_result(reservation: Reservation, exc: BaseException) -> ProviderResult:
    return provider_result([], state="UNKNOWN", reason=f"{type(exc).__name__}:{exc}", call_ids=(reservation.call_id,))


def is_unknown_transport_error(exc: BaseException) -> bool:
    """Classify failures where the provider may have accepted the request."""
    name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    return (
        isinstance(exc, (TimeoutError, ConnectionError))
        or "timeout" in name or "connectionerror" in name
        or any(token in message for token in (
            "connection reset", "connection aborted", "connection refused",
            "remote end closed", "broken pipe", "disconnected", "disconnect",
        ))
    )

def _default_budget(provider: str) -> int:
    return int({
        "brightdata": getattr(config, "BRIGHTDATA_REQUEST_BUDGET", 0),
        "google_places": getattr(config, "GOOGLE_PLACES_REQUEST_BUDGET", 0),
        "brandfetch": getattr(config, "BRANDFETCH_REQUEST_BUDGET", 0),
        "hunter": getattr(config, "HUNTER_REQUEST_BUDGET", 0),
        "linkedin": getattr(config, "LINKEDIN_COMPANY_REQUEST_BUDGET", 0),
        "llm": getattr(config, "LLM_ARBITER_BUDGET", 0),
    }[provider])


def _counter_provider_name(provider: str) -> str:
    return {"linkedin": "linkedin_company", "llm": "llm_arbiter"}.get(provider, provider)


def reset() -> None:
    global _COUNTERS, _STARTED_AT, _NEXT_REQUEST_AT, _PHASE, _DURABLE_RUN_ID, _DURABLE_BUDGETS, _FREE_QUERY_COUNTS
    with _LOCK:
        _COUNTERS = Counter({
            "api.brightdata.requests": max(
                0, int(os.getenv("BRIGHTDATA_REQUEST_OFFSET", "0"))
            ),
            "api.linkedin_company.requests": max(
                0, int(os.getenv("LINKEDIN_COMPANY_REQUEST_OFFSET", "0"))
            ),
            "api.llm_arbiter.requests": max(
                0, int(os.getenv("LLM_ARBITER_REQUEST_OFFSET", "0"))
            ),
            "api.google_places.requests": max(
                0, int(os.getenv("GOOGLE_PLACES_REQUEST_OFFSET", "0"))
            ),
            "api.brandfetch.requests": max(
                0, int(os.getenv("BRANDFETCH_REQUEST_OFFSET", "0"))
            ),
            "api.hunter.requests": max(
                0, int(os.getenv("HUNTER_REQUEST_OFFSET", "0"))
            ),
            "api.hunter_domain_finder.requests": max(
                0, int(os.getenv("HUNTER_REQUEST_OFFSET", "0"))
            ),
        })
        _STARTED_AT = time.monotonic()
        _NEXT_REQUEST_AT = 0.0
        _PHASE = "FREE"
        _DURABLE_RUN_ID = ""
        _DURABLE_BUDGETS = {}
        _FREE_QUERY_COUNTS = {}
    _CURRENT_ITEM_INDEX.set(-1)
    _CURRENT_OPERATION.set("")
    _CURRENT_SEARCH_BUCKET.set("")
    _CURRENT_PROVIDER_OUTCOMES.set(None)


def configure_durable_run(run_id: str, budgets: dict[str, int]) -> None:
    global _DURABLE_RUN_ID, _DURABLE_BUDGETS
    _DURABLE_RUN_ID = str(run_id)
    _DURABLE_BUDGETS = {str(key): int(value) for key, value in budgets.items()}


def durable_run_id() -> str:
    return _DURABLE_RUN_ID


def paid_access_allowed(provider: str) -> bool:
    """Central paid authorization and budget gate for fallback adapters."""
    canonical = _PROVIDER_ALIASES.get(str(provider))
    if canonical is None:
        return False
    if not bool(getattr(config, "PAID_ENABLED", True)):
        return False
    if _DURABLE_RUN_ID:
        return _PHASE == "PAID" and int(_DURABLE_BUDGETS.get(canonical, 0)) > 0
    # Volatile adapter tests and direct callers have no durable phase; their
    # configured positive budget is the authorization boundary.
    return _default_budget(canonical) > 0


def set_item_context(item_index: int, operation: str = "") -> None:
    _CURRENT_ITEM_INDEX.set(int(item_index))
    _CURRENT_OPERATION.set(str(operation))


def set_search_bucket(bucket: str = "") -> None:
    value = str(bucket or "").casefold()
    _CURRENT_SEARCH_BUCKET.set(value if value in {"discovery", "targeted"} else "")


def search_bucket() -> str:
    return _CURRENT_SEARCH_BUCKET.get()


def request_fingerprint(provider: str, operation: str, request: object) -> str:
    canonical = _PROVIDER_ALIASES.get(str(provider))
    if canonical is None:
        raise ValueError(f"unknown provider: {provider}")
    payload = json.dumps({"provider": canonical, "operation": str(operation), "request": request}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return __import__("hashlib").sha256(payload.encode("utf-8")).hexdigest()


def complete_api(reservation: Reservation | str, state: str = "DONE", result_ref: str = "") -> None:
    # Keep lightweight provider unit-test doubles compatible; durable runs
    # always receive a Reservation carrying the exact call_id.
    if isinstance(reservation, bool):
        return
    if isinstance(reservation, Reservation):
        canonical = reservation.provider
        call_id = reservation.call_id
    else:
        canonical = _PROVIDER_ALIASES.get(str(reservation))
        call_id = str(reservation) if canonical is None else ""
    if canonical is None:
        raise ValueError(f"unknown provider: {reservation}")
    if _DURABLE_RUN_ID:
        checkpoint = importlib.import_module("modules.checkpoint")
        if not call_id:
            raise ValueError("durable provider completion requires reservation call_id")
        checkpoint.complete_provider_call(call_id=call_id, result_ref=result_ref, state=state)
    state_map = {"DONE": "COMPLETED", "FAILED": "FAILED", "UNKNOWN": "UNKNOWN"}
    record_provider_outcome(
        state=state_map.get(str(state).upper(), str(state).upper()),
        reason=result_ref, call_ids=(call_id,), provider=canonical,
    )


def set_phase(phase: str) -> None:
    global _PHASE
    if str(phase) not in {"FREE", "PAID", "FINALIZING", "COMPLETE"}:
        raise ValueError(f"invalid runtime phase: {phase}")
    with _LOCK:
        _PHASE = str(phase)


def phase() -> str:
    with _LOCK:
        return _PHASE


def restore(snapshot: dict | None) -> None:
    """Restore durable counters when resuming a run; never refresh budgets."""
    global _COUNTERS, _STARTED_AT, _NEXT_REQUEST_AT, _PHASE
    counters = (snapshot or {}).get("counters", {})
    with _LOCK:
        _COUNTERS = Counter({str(key): int(value) for key, value in counters.items()})
        _STARTED_AT = time.monotonic()
        _NEXT_REQUEST_AT = 0.0
        _PHASE = str((snapshot or {}).get("phase", "FREE"))


def record(name: str, amount: int = 1) -> None:
    with _LOCK:
        _COUNTERS[name] += amount


def wait_for_request_slot() -> None:
    global _NEXT_REQUEST_AT
    rate = max(float(config.GLOBAL_REQUESTS_PER_SECOND), 0.0)
    if rate <= 0:
        return
    interval = 1.0 / rate
    with _LOCK:
        now = time.monotonic()
        wait = max(0.0, _NEXT_REQUEST_AT - now)
        _NEXT_REQUEST_AT = max(now, _NEXT_REQUEST_AT) + interval
    if wait:
        time.sleep(wait)


def reserve_api(provider: str, budget: int | None = None, *, operation: str = "", request_fingerprint: str = "") -> Reservation:
    """Return a durable reservation or an explicit rejection reason."""
    canonical = _PROVIDER_ALIASES.get(str(provider))
    if canonical is None:
        raise ValueError(f"unknown provider: {provider}")
    operation = str(operation or _CURRENT_OPERATION.get())
    item_index = _CURRENT_ITEM_INDEX.get()
    phase_name = _PHASE
    def rejected(reason: str) -> Reservation:
        record_provider_outcome(
            state="DUPLICATE" if "duplicate" in reason else "BLOCKED_BUDGET",
            reason=reason, provider=canonical,
        )
        return Reservation(False, canonical, operation, item_index, phase_name, reason=reason)
    if not bool(getattr(config, "PAID_ENABLED", True)):
        record(f"api.{provider}.budget_blocked")
        return rejected("paid_disabled")
    if budget is not None and budget <= 0 or (_PHASE == "FREE" and _DURABLE_RUN_ID):
        record(f"api.{provider}.budget_blocked")
        return rejected("budget_disabled" if budget is not None and budget <= 0 else "free_phase")
    if _DURABLE_RUN_ID:
        checkpoint = importlib.import_module("modules.checkpoint")
        effective = _DURABLE_BUDGETS.get(canonical)
        if effective is None:
            raise RuntimeError(f"provider budget not initialized: {canonical}")
        fingerprint = request_fingerprint or f"{canonical}:{_CURRENT_ITEM_INDEX.get()}:{operation}"
        previous = checkpoint.provider_call_for_fingerprint(
            run_id=_DURABLE_RUN_ID, provider=canonical,
            item_index=_CURRENT_ITEM_INDEX.get(), phase=_PHASE,
            request_fingerprint=fingerprint,
        )
        if previous:
            record(f"api.{provider}.duplicate_request")
            return Reservation(False, canonical, operation, item_index, phase_name,
                               call_id=previous["call_id"],
                               reason="duplicate_request",
                               inherited_state=previous["state"])
        call_id = checkpoint.reserve_provider_call(
            run_id=_DURABLE_RUN_ID, provider=canonical, item_index=_CURRENT_ITEM_INDEX.get(),
            phase=_PHASE, operation=operation or _CURRENT_OPERATION.get(),
            request_fingerprint=fingerprint,
            configured_limit=int(effective), effective_limit=int(effective),
        )
        if not call_id:
            previous = checkpoint.provider_call_for_fingerprint(
                run_id=_DURABLE_RUN_ID, provider=canonical,
                item_index=_CURRENT_ITEM_INDEX.get(), phase=_PHASE,
                request_fingerprint=fingerprint,
            )
            if previous:
                record(f"api.{provider}.duplicate_request")
                return Reservation(False, canonical, operation, item_index, phase_name,
                                   call_id=previous["call_id"],
                                   reason="duplicate_request",
                                   inherited_state=previous["state"])
            record(f"api.{provider}.budget_blocked")
            return rejected("budget_exhausted")
        record_provider_outcome(state="RESERVED", call_ids=(call_id,), provider=canonical)
        return Reservation(True, canonical, operation, item_index, phase_name, call_id=call_id)
    with _LOCK:
        budget = int(_default_budget(canonical) if budget is None else budget)
        counter_provider = _counter_provider_name(canonical)
        used_key = f"api.{counter_provider}.requests"
        if _COUNTERS[used_key] >= budget:
            _COUNTERS[f"api.{counter_provider}.budget_blocked"] += 1
            return rejected("budget_exhausted")
        _COUNTERS[used_key] += 1
        call_id = f"volatile:{canonical}:{item_index}:{operation}"
        record_provider_outcome(state="RESERVED", call_ids=(call_id,), provider=canonical)
        return Reservation(True, canonical, operation, item_index, phase_name, call_id=call_id)


def reserve_crawler_http(budget: int) -> bool:
    """Atomically reserve one crawler request; zero/negative means unlimited."""
    with _LOCK:
        used_key = "http.crawler.requests"
        if budget > 0 and _COUNTERS[used_key] >= budget:
            _COUNTERS["http.crawler.budget_blocked"] += 1
            return False
        _COUNTERS[used_key] += 1
        return True


def reserve_search_query(budget: int, *, bucket: str | None = None) -> bool:
    """Atomically reserve one free query per durable (run,item), max ten."""
    item_index = _CURRENT_ITEM_INDEX.get()
    run_id = _DURABLE_RUN_ID or "volatile"
    limit = 10 if int(budget) <= 0 else min(10, int(budget))
    bucket = str(bucket if bucket is not None else _CURRENT_SEARCH_BUCKET.get()).casefold()
    if bucket not in {"discovery", "targeted"}:
        bucket = ""
    if item_index >= 0 and _DURABLE_RUN_ID:
        checkpoint = importlib.import_module("modules.checkpoint")
        accepted = checkpoint.reserve_free_search_query(
            run_id=_DURABLE_RUN_ID, item_index=item_index, limit=limit, bucket=bucket,
        )
        if not accepted:
            record("http.search.budget_blocked")
            return False
        record("http.search.requests")
        return True
    with _LOCK:
        key = (run_id, int(item_index), bucket)
        bucket_limit = {"discovery": 6, "targeted": 4}.get(bucket, limit)
        if _FREE_QUERY_COUNTS.get(key, 0) >= min(limit, bucket_limit):
            _COUNTERS["http.search.budget_blocked"] += 1
            return False
        _FREE_QUERY_COUNTS[key] = _FREE_QUERY_COUNTS.get(key, 0) + 1
        _COUNTERS["http.search.requests"] += 1
        return True


def snapshot() -> dict:
    with _LOCK:
        counters = dict(sorted(_COUNTERS.items()))
        elapsed = time.monotonic() - _STARTED_AT
    companies = int(counters.get("pipeline.companies", 0))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 3),
        "phase": phase(),
        "counters": counters,
        "budgets": {
            "crawler_http": config.CRAWLER_HTTP_REQUEST_BUDGET,
            "search_queries": config.SEARCH_HTTP_REQUEST_BUDGET,
            "brightdata": config.BRIGHTDATA_REQUEST_BUDGET,
            "linkedin_company": config.LINKEDIN_COMPANY_REQUEST_BUDGET,
            "llm_arbiter": config.LLM_ARBITER_BUDGET,
            "google_places": config.GOOGLE_PLACES_REQUEST_BUDGET,
            "hunter": config.HUNTER_REQUEST_BUDGET,
            "hunter_domain_finder": config.HUNTER_REQUEST_BUDGET,
            "brandfetch": config.BRANDFETCH_REQUEST_BUDGET,
        },
        "per_company": {
            "search_candidates": round(counters.get("pipeline.candidates_discovered", 0) / companies, 3) if companies else 0,
            "identity_candidates_evaluated": round(counters.get("pipeline.identity_candidates_evaluated", 0) / companies, 3) if companies else 0,
            "full_candidates_evaluated": round(counters.get("pipeline.full_candidates_evaluated", 0) / companies, 3) if companies else 0,
            "crawler_http_requests": round(counters.get("http.crawler.requests", 0) / companies, 3) if companies else 0,
            "brightdata_requests": round(counters.get("api.brightdata.requests", 0) / companies, 3) if companies else 0,
            "linkedin_company_requests": round(counters.get("api.linkedin_company.requests", 0) / companies, 3) if companies else 0,
            "llm_arbiter_requests": round(counters.get("api.llm_arbiter.requests", 0) / companies, 3) if companies else 0,
        },
    }


def write(path: Path, snapshot_data: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(snapshot_data if snapshot_data is not None else snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
        if temporary.exists():
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
