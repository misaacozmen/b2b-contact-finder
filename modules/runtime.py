"""Thread-safe run budgets, global rate limiting and lightweight telemetry."""

from __future__ import annotations

import json
import hashlib
import importlib
import os
import threading
import time
from contextvars import ContextVar
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Any

import config


_LOCK = threading.RLock()
_METRIC_FLUSH_LOCK = threading.Lock()
_COUNTERS: Counter = Counter()
_PENDING_DURABLE_METRICS: Counter = Counter()
_PENDING_DURABLE_METRIC_EVENTS = 0
_PENDING_DURABLE_METRIC_EVENTS_BY_RUN: Counter = Counter()
_LAST_DURABLE_METRIC_FLUSH = time.monotonic()
_STARTED_AT = time.monotonic()
_NEXT_REQUEST_AT = 0.0
_PHASE = "FREE"
_DURABLE_RUN_ID = ""
_DURABLE_BUDGETS: dict[str, int] = {}
_UNIQUE_TELEMETRY: dict[str, set[str]] = {}
_DURABLE_BUDGET_METADATA: dict[str, dict] = {}
_CURRENT_ITEM_INDEX: ContextVar[int] = ContextVar("current_item_index", default=-1)
_CURRENT_OPERATION: ContextVar[str] = ContextVar("current_operation", default="")
_CURRENT_SOURCE_RECORD_ID: ContextVar[str] = ContextVar("current_source_record_id", default="")
_CURRENT_PROVIDER_OUTCOMES: ContextVar[tuple[dict, ...] | None] = ContextVar("current_provider_outcomes", default=None)
_FREE_QUERY_COUNTS: dict[tuple[str, int, str], int] = {}
_FREE_PROVIDER_ATTEMPTS: dict[str, dict] = {}
_CURRENT_SEARCH_BUCKET: ContextVar[str] = ContextVar("current_search_bucket", default="")
_CURRENT_PROVIDER_DISPATCH_ROUNDS: ContextVar[dict[str, int]] = ContextVar("current_provider_dispatch_rounds", default={})
_CURRENT_PROVIDER_DISPATCH_PHYSICAL: ContextVar[dict[str, bool]] = ContextVar("current_provider_dispatch_physical", default={})
_CURRENT_ITEM_STOP: ContextVar["PaidStopState"] = ContextVar("current_item_stop")
_DURABLE_TELEMETRY: dict = {}
_PAID_TRANSPORT: Callable[["TransportEnvelope"], Any] | None = None

_PROVIDER_ALIASES = {
    "brightdata": "brightdata", "google_places": "google_places",
    "brandfetch": "brandfetch", "hunter": "hunter", "hunter_domain_finder": "hunter",
    "linkedin": "linkedin", "linkedin_company": "linkedin", "llm": "llm", "llm_arbiter": "llm",
}
CANONICAL_PAID_PROVIDERS = frozenset({"brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm"})


class StopScope(str, Enum):
    NONE = "NONE"
    FREE_CURRENT_BUCKET = "FREE_CURRENT_BUCKET"
    PAID_PROVIDER = "PAID_PROVIDER"
    MANUAL_AUTHORIZATION = "MANUAL_AUTHORIZATION"


@dataclass(frozen=True)
class PaidStopState:
    scope: StopScope = StopScope.NONE
    provider: str = ""
    reason: str = ""
    call_ids: tuple[str, ...] = ()


_STOP_PRIORITY = {
    StopScope.NONE: 0,
    StopScope.FREE_CURRENT_BUCKET: 1,
    StopScope.PAID_PROVIDER: 2,
    StopScope.MANUAL_AUTHORIZATION: 3,
}


def reset_item_stop_state(item_index: int) -> PaidStopState:
    set_item_context(item_index)
    state = PaidStopState()
    _CURRENT_ITEM_STOP.set(state)
    _CURRENT_PROVIDER_DISPATCH_PHYSICAL.set({})
    return state


def mark_item_stop(scope: StopScope | str, provider: str = "", reason: str = "", call_ids=()) -> PaidStopState:
    normalized = scope if isinstance(scope, StopScope) else StopScope(str(scope).upper())
    current = item_stop_state()
    if _STOP_PRIORITY[normalized] < _STOP_PRIORITY[current.scope]:
        return current
    merged_ids = tuple(dict.fromkeys((*current.call_ids, *(str(value) for value in call_ids if value))))
    state = PaidStopState(normalized, str(provider or current.provider), str(reason or current.reason), merged_ids)
    _CURRENT_ITEM_STOP.set(state)
    return state


def item_stop_state() -> PaidStopState:
    try:
        return _CURRENT_ITEM_STOP.get()
    except LookupError:
        state = PaidStopState()
        _CURRENT_ITEM_STOP.set(state)
        return state


def paid_provider_call_allowed(provider: str) -> bool:
    canonical = _PROVIDER_ALIASES.get(str(provider), str(provider))
    state = item_stop_state()
    if state.scope == StopScope.MANUAL_AUTHORIZATION:
        return False
    return not (state.scope == StopScope.PAID_PROVIDER and state.provider == canonical)


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
    request_fingerprint: str = ""

    def __bool__(self) -> bool:
        return self.accepted


@dataclass(frozen=True)
class TransportEnvelope:
    provider: str
    endpoint: str
    run_id: str
    item_index: int
    paid_attempt_id: str
    provider_call_id: str
    request_fingerprint: str
    flight_fingerprint: str
    execution_generation: int
    attempt_ordinal: int
    request_shape_sha256: str
    timeout: float


def set_paid_transport(transport: Callable[[TransportEnvelope], Any] | None) -> None:
    global _PAID_TRANSPORT
    _PAID_TRANSPORT = transport


def transport_envelope(reservation: Reservation, *, endpoint: str, attempt_ordinal: int = 1,
                       flight_fingerprint: str = "", request_shape: object = None,
                       timeout: float = 0) -> TransportEnvelope:
    if isinstance(reservation, bool):
        reservation = Reservation(reservation, "brightdata", "legacy_test_double", _CURRENT_ITEM_INDEX.get(), _PHASE, call_id="volatile:test-double", request_fingerprint="legacy_test_double")
    if not reservation.accepted or not reservation.call_id:
        checkpoint = importlib.import_module("modules.checkpoint")
        raise checkpoint.LedgerInvariant("transport envelope requires an accepted provider call")
    context = {}
    if _DURABLE_RUN_ID:
        checkpoint = importlib.import_module("modules.checkpoint")
        context = checkpoint.provider_call_transport_context(reservation.call_id)
    shape_json = json.dumps(request_shape, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    shape_sha256 = hashlib.sha256(shape_json.encode("utf-8")).hexdigest()
    if _DURABLE_RUN_ID:
        checkpoint.bind_provider_call_transport_receipt(
            call_id=reservation.call_id,
            endpoint_sha256=hashlib.sha256(str(endpoint).encode("utf-8")).hexdigest(),
            request_shape_sha256=shape_sha256,
        )
    return TransportEnvelope(
        provider=reservation.provider, endpoint=str(endpoint), run_id=_DURABLE_RUN_ID,
        item_index=reservation.item_index, paid_attempt_id=str(context.get("paid_attempt_id", "")),
        provider_call_id=reservation.call_id,
        request_fingerprint=reservation.request_fingerprint or str(context.get("request_fingerprint", "")),
        flight_fingerprint=str(flight_fingerprint or context.get("flight_fingerprint", "")),
        execution_generation=int(context.get("execution_generation", 1)),
        attempt_ordinal=int(attempt_ordinal),
        request_shape_sha256=shape_sha256,
        timeout=float(timeout),
    )


def invoke_paid_transport(envelope: TransportEnvelope, default_call: Callable[[], Any]) -> Any:
    transport = _PAID_TRANSPORT
    return transport(envelope) if transport is not None else default_call()


@dataclass(frozen=True)
class FreeReservation:
    accepted: bool
    reason: str
    bucket: str
    logical_used: int
    logical_limit: int
    physical_used: int
    physical_limit: int
    attempt_id: str = ""
    attempt_ordinal: int = 0

    def __bool__(self) -> bool:
        return self.accepted


@dataclass(frozen=True)
class FreeCapacity:
    available: bool
    bucket: str
    logical_used: int
    logical_limit: int
    physical_used: int
    physical_limit: int

    def __bool__(self) -> bool:
        return self.available


class ProviderResult(list):
    """List-compatible provider result carrying durable outcome metadata."""

    def __init__(self, values=(), *, state: str = "EMPTY", reason: str = "", call_ids: tuple[str, ...] = (), provider: str = "", origin: str = "LIVE_OWNER", flight_fingerprint: str = "", call_relations: dict[str, str] | None = None, stop_scope: StopScope | str = StopScope.NONE):
        super().__init__(values)
        self.state = str(state)
        self.reason = str(reason)
        self.result_state = self.state
        self.result_reason = self.reason
        self.call_ids = tuple(str(value) for value in call_ids if value)
        self.provider = str(provider)
        self.origin = str(origin)
        self.flight_fingerprint = str(flight_fingerprint)
        self.call_relations = {str(key): str(value) for key, value in (call_relations or {}).items()}
        self.stop_scope = stop_scope if isinstance(stop_scope, StopScope) else StopScope(str(stop_scope).upper())
        self.stop_scope = apply_provider_outcome_stop(
            self.state, self.provider, self.reason, self.call_ids, self.stop_scope,
        ).scope
        record_provider_outcome(
            state=self.state, reason=self.reason, call_ids=self.call_ids, provider=self.provider,
            origin=self.origin, flight_fingerprint=self.flight_fingerprint,
            call_relations=self.call_relations, stop_scope=self.stop_scope,
        )


def provider_result(values=(), *, state: str = "EMPTY", reason: str = "", call_ids: tuple[str, ...] = (), **metadata) -> ProviderResult:
    return ProviderResult(values, state=state, reason=reason, call_ids=call_ids, **metadata)


def apply_provider_outcome_stop(state: str, provider: str, reason: str = "", call_ids=(), stop_scope: StopScope | str = StopScope.NONE) -> PaidStopState:
    """Apply the one canonical paid-outcome stop rule."""
    canonical = _PROVIDER_ALIASES.get(str(provider), str(provider))
    normalized_state = str(state).upper()
    requested = stop_scope if isinstance(stop_scope, StopScope) else StopScope(str(stop_scope).upper())
    if normalized_state == "UNKNOWN" and canonical in CANONICAL_PAID_PROVIDERS and _PHASE == "PAID" and _CURRENT_ITEM_INDEX.get() >= 0:
        requested = StopScope.MANUAL_AUTHORIZATION
    if _CURRENT_ITEM_INDEX.get() < 0:
        return item_stop_state()
    if requested == StopScope.NONE:
        return item_stop_state()
    return mark_item_stop(requested, canonical, reason, call_ids)


def begin_provider_attempt():
    return _CURRENT_PROVIDER_OUTCOMES.set(())


def record_provider_outcome(*, state: str, reason: str = "", call_ids=(), provider: str = "", origin: str = "LIVE_OWNER", flight_fingerprint: str = "", call_relations: dict | None = None, stop_scope: StopScope | str = StopScope.NONE) -> None:
    current = _CURRENT_PROVIDER_OUTCOMES.get()
    if current is None:
        return
    value = {
        "provider": str(provider),
        "result_state": str(state).upper(),
        "result_reason": str(reason),
        "call_ids": [str(call_id) for call_id in call_ids if call_id],
        "origin": str(origin),
        "flight_fingerprint": str(flight_fingerprint),
        "call_relations": {str(key): str(value) for key, value in (call_relations or {}).items()},
        "stop_scope": (stop_scope.value if isinstance(stop_scope, StopScope) else str(stop_scope)),
    }
    _CURRENT_PROVIDER_OUTCOMES.set((*current, value))


def end_provider_attempt(token) -> list[dict]:
    outcomes = list(_CURRENT_PROVIDER_OUTCOMES.get() or ())
    _CURRENT_PROVIDER_OUTCOMES.reset(token)
    return outcomes


def terminal_provider_query_follower_receipts(provider: str) -> tuple[dict, ...]:
    """Return terminal inherited query receipts observed in this paid attempt."""
    canonical = _PROVIDER_ALIASES.get(str(provider), str(provider))
    receipts = []
    for outcome in _CURRENT_PROVIDER_OUTCOMES.get() or ():
        if (
            str(outcome.get("provider", "")) != canonical
            or str(outcome.get("origin", "")) != "SINGLEFLIGHT_FOLLOWER"
            or str(outcome.get("result_state", "")).upper() not in {"COMPLETED", "EMPTY"}
        ):
            continue
        fingerprint = str(outcome.get("flight_fingerprint", ""))
        relations = outcome.get("call_relations") or {}
        for call_id in outcome.get("call_ids", ()):
            if fingerprint and str(relations.get(str(call_id), "")).upper() == "INHERITED":
                receipts.append({"query_fingerprint": fingerprint, "provider_call_id": str(call_id)})
    return tuple(receipts)


def rejected_provider_result(reservation: Reservation) -> ProviderResult:
    reason = str(reservation.reason or "provider_rejected")
    if reason == "item_stop_guard":
        stop = item_stop_state()
        return provider_result(
            [], state="UNKNOWN" if stop.scope == StopScope.MANUAL_AUTHORIZATION else "FAILED",
            reason=stop.reason or reason, call_ids=stop.call_ids,
            provider=stop.provider or reservation.provider, stop_scope=stop.scope,
        )
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
        return provider_result(
            [], state=state, reason=reason, call_ids=(reservation.call_id,),
            provider=reservation.provider,
            call_relations={reservation.call_id: "INHERITED"} if reservation.call_id else {},
            origin="SINGLEFLIGHT_FOLLOWER",
        )
    return provider_result([], state="BLOCKED_BUDGET", reason=reason, provider=reservation.provider, origin="BUDGET_BLOCK")


def unknown_provider_result(reservation: Reservation, exc: BaseException) -> ProviderResult:
    return provider_result([], state="UNKNOWN", reason=f"{type(exc).__name__}:{exc}", call_ids=(reservation.call_id,), provider=reservation.provider)


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
    return max(0, int({
        "brightdata": getattr(config, "BRIGHTDATA_REQUEST_BUDGET", 0),
        "google_places": getattr(config, "GOOGLE_PLACES_REQUEST_BUDGET", 0),
        "brandfetch": getattr(config, "BRANDFETCH_REQUEST_BUDGET", 0),
        "hunter": getattr(config, "HUNTER_REQUEST_BUDGET", 0),
        "linkedin": getattr(config, "LINKEDIN_COMPANY_REQUEST_BUDGET", 0),
        "llm": getattr(config, "LLM_ARBITER_BUDGET", 0),
    }[provider]))


def _counter_provider_name(provider: str) -> str:
    return {"linkedin": "linkedin_company", "llm": "llm_arbiter"}.get(provider, provider)


def reset() -> None:
    global _COUNTERS, _STARTED_AT, _NEXT_REQUEST_AT, _PHASE, _DURABLE_RUN_ID, _DURABLE_BUDGETS, _DURABLE_BUDGET_METADATA, _UNIQUE_TELEMETRY, _FREE_QUERY_COUNTS, _FREE_PROVIDER_ATTEMPTS, _DURABLE_TELEMETRY
    flush_operational_metrics()
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
        _DURABLE_BUDGET_METADATA = {}
        _UNIQUE_TELEMETRY = {}
        _FREE_QUERY_COUNTS = {}
        _FREE_PROVIDER_ATTEMPTS = {}
        _DURABLE_TELEMETRY = {}
    _CURRENT_ITEM_INDEX.set(-1)
    _CURRENT_OPERATION.set("")
    _CURRENT_SOURCE_RECORD_ID.set("")
    _CURRENT_SEARCH_BUCKET.set("")
    _CURRENT_PROVIDER_DISPATCH_ROUNDS.set({})
    _CURRENT_PROVIDER_OUTCOMES.set(None)
    _CURRENT_ITEM_STOP.set(PaidStopState())


def configure_durable_run(
    run_id: str,
    budgets: dict[str, int],
    *,
    budget_metadata: dict[str, dict] | None = None,
) -> None:
    global _DURABLE_RUN_ID, _DURABLE_BUDGETS, _DURABLE_BUDGET_METADATA
    flush_operational_metrics()
    _DURABLE_RUN_ID = str(run_id)
    _DURABLE_BUDGETS = {str(key): int(value) for key, value in budgets.items()}
    _DURABLE_BUDGET_METADATA = {
        str(provider): dict(values)
        for provider, values in (budget_metadata or {}).items()
        if isinstance(values, dict)
    }


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


def set_source_record_id(source_record_id: str = "") -> None:
    _CURRENT_SOURCE_RECORD_ID.set(str(source_record_id or "").strip())


def current_source_record_id() -> str:
    return _CURRENT_SOURCE_RECORD_ID.get()


def current_item_index() -> int:
    return _CURRENT_ITEM_INDEX.get()


def set_search_bucket(bucket: str = "") -> None:
    value = str(bucket or "").casefold()
    _CURRENT_SEARCH_BUCKET.set(value if value in {"discovery", "targeted"} else "")


def search_bucket() -> str:
    return _CURRENT_SEARCH_BUCKET.get()


def set_provider_dispatch_rounds(rounds: dict[str, int] | None = None) -> None:
    _CURRENT_PROVIDER_DISPATCH_ROUNDS.set({
        str(provider): int(round_ordinal)
        for provider, round_ordinal in (rounds or {}).items()
    })


def provider_dispatch_round(provider: str) -> int | None:
    value = _CURRENT_PROVIDER_DISPATCH_ROUNDS.get().get(str(provider))
    return int(value) if value is not None else None


def provider_dispatch_physical_consumed(provider: str) -> bool:
    return bool(_CURRENT_PROVIDER_DISPATCH_PHYSICAL.get().get(str(provider), False))


def _mark_provider_dispatch_physical(provider: str) -> None:
    current = dict(_CURRENT_PROVIDER_DISPATCH_PHYSICAL.get())
    current[str(provider)] = True
    _CURRENT_PROVIDER_DISPATCH_PHYSICAL.set(current)


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
    normalized_state = str(state).upper()
    if normalized_state not in {"DONE", "FAILED", "UNKNOWN"}:
        checkpoint = importlib.import_module("modules.checkpoint")
        raise checkpoint.OutcomeInvariant(f"invalid provider terminal state: {state}")
    if _DURABLE_RUN_ID:
        checkpoint = importlib.import_module("modules.checkpoint")
        if not call_id:
            raise checkpoint.LedgerInvariant("durable provider completion requires reservation call_id")
        checkpoint.complete_provider_call(call_id=call_id, result_ref=result_ref, state=normalized_state)
    stop = apply_provider_outcome_stop(normalized_state, canonical, result_ref, (call_id,))
    state_map = {"DONE": "COMPLETED", "FAILED": "FAILED", "UNKNOWN": "UNKNOWN"}
    record_provider_outcome(
        state=state_map[normalized_state],
        reason=result_ref, call_ids=(call_id,), provider=canonical,
        stop_scope=stop.scope if normalized_state == "UNKNOWN" else StopScope.NONE,
    )


def start_api(reservation: Reservation) -> None:
    if isinstance(reservation, bool):
        return
    if not isinstance(reservation, Reservation) or not reservation.accepted:
        checkpoint = importlib.import_module("modules.checkpoint")
        raise checkpoint.LedgerInvariant("accepted provider reservation required")
    if _DURABLE_RUN_ID:
        checkpoint = importlib.import_module("modules.checkpoint")
        checkpoint.start_provider_call(reservation.call_id)


def mark_api_http_started(reservation: Reservation, attempt_ordinal: int = 1, flight_fingerprint: str = "") -> None:
    if isinstance(reservation, bool):
        return
    if not isinstance(reservation, Reservation) or not reservation.accepted:
        checkpoint = importlib.import_module("modules.checkpoint")
        raise checkpoint.LedgerInvariant("accepted provider reservation required")
    if _DURABLE_RUN_ID:
        checkpoint = importlib.import_module("modules.checkpoint")
        checkpoint.mark_provider_call_http_started(
            call_id=reservation.call_id,
            attempt_ordinal=int(attempt_ordinal),
            flight_fingerprint=str(flight_fingerprint),
        )
    # A reservation is only an authorization.  The dispatch budget is
    # physically consumed at the transport boundary, after the durable HTTP
    # start receipt has been written.
    _mark_provider_dispatch_physical(reservation.provider)


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
    global _COUNTERS, _STARTED_AT, _NEXT_REQUEST_AT, _PHASE, _UNIQUE_TELEMETRY
    counters = (snapshot or {}).get("counters", {})
    recorded_unique_keys = (snapshot or {}).get("unique_keys", {})
    with _LOCK:
        _COUNTERS = Counter({str(key): int(value) for key, value in counters.items()})
        _STARTED_AT = time.monotonic()
        _NEXT_REQUEST_AT = 0.0
        _PHASE = str((snapshot or {}).get("phase", "FREE"))
        _UNIQUE_TELEMETRY = {
            str(name): {
                str(key).casefold()
                for key in keys
                if isinstance(key, str) and len(key) == 64
                and all(char in "0123456789abcdef" for char in key.casefold())
            }
            for name, keys in recorded_unique_keys.items()
            if isinstance(keys, (list, tuple, set))
        }


def record(name: str, amount: int = 1) -> None:
    global _PENDING_DURABLE_METRIC_EVENTS
    durable_run_id = ""
    should_flush = False
    amount = int(amount)
    with _LOCK:
        _COUNTERS[name] += amount
        durable_run_id = _DURABLE_RUN_ID
    persist = str(name).startswith((
        "recovery.", "api.", "search.serp.", "http.crawler.", "pipeline.",
        "candidate.", "snapshot.", "cache.", "contact_policy.",
        "source_profile.", "live.site.", "cache.site.",
    ))
    if durable_run_id and str(name) and persist and amount:
        with _LOCK:
            _PENDING_DURABLE_METRICS[(durable_run_id, str(name))] += amount
            _PENDING_DURABLE_METRIC_EVENTS += 1
            _PENDING_DURABLE_METRIC_EVENTS_BY_RUN[durable_run_id] += 1
            should_flush = (
                _PENDING_DURABLE_METRIC_EVENTS >= 64
                or time.monotonic() - _LAST_DURABLE_METRIC_FLUSH >= 1.0
            )
        if should_flush:
            flush_operational_metrics()


def flush_operational_metrics() -> None:
    """Durably batch non-authoritative metrics without opening SQLite per event."""
    global _PENDING_DURABLE_METRIC_EVENTS, _LAST_DURABLE_METRIC_FLUSH
    if not _METRIC_FLUSH_LOCK.acquire(blocking=False):
        return
    try:
        with _LOCK:
            pending = dict(_PENDING_DURABLE_METRICS)
            event_counts = dict(_PENDING_DURABLE_METRIC_EVENTS_BY_RUN)
            _PENDING_DURABLE_METRICS.clear()
            _PENDING_DURABLE_METRIC_EVENTS_BY_RUN.clear()
            _PENDING_DURABLE_METRIC_EVENTS = 0
            _LAST_DURABLE_METRIC_FLUSH = time.monotonic()
        if not pending:
            return
        grouped: dict[str, dict[str, int]] = {}
        for (run_id, metric), amount in pending.items():
            grouped.setdefault(str(run_id), {})[str(metric)] = int(amount)
        failed: dict[tuple[str, str], int] = {}
        for run_id, metrics in grouped.items():
            try:
                checkpoint = importlib.import_module("modules.checkpoint")
                checkpoint.record_operational_metrics_batch(run_id, metrics)
            except Exception:
                failed.update({(run_id, metric): amount for metric, amount in metrics.items()})
        if failed:
            with _LOCK:
                _PENDING_DURABLE_METRICS.update(failed)
                for run_id in {key[0] for key in failed}:
                    count = int(event_counts.get(run_id, 0))
                    _PENDING_DURABLE_METRIC_EVENTS_BY_RUN[run_id] += count
                    _PENDING_DURABLE_METRIC_EVENTS += count
    finally:
        _METRIC_FLUSH_LOCK.release()


def drain_operational_metrics(run_id: str, *, timeout_seconds: float = 2.0) -> bool:
    """Persist one run's queued metrics under a bounded, verifiable barrier."""
    global _PENDING_DURABLE_METRIC_EVENTS, _LAST_DURABLE_METRIC_FLUSH
    target_run_id = str(run_id or "").strip()
    if not target_run_id:
        return False
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    remaining = max(0.0, deadline - time.monotonic())
    if not _METRIC_FLUSH_LOCK.acquire(timeout=remaining):
        return False
    try:
        with _LOCK:
            pending = {
                metric: int(amount)
                for (pending_run_id, metric), amount in _PENDING_DURABLE_METRICS.items()
                if pending_run_id == target_run_id and int(amount)
            }
            event_count = int(_PENDING_DURABLE_METRIC_EVENTS_BY_RUN.pop(target_run_id, 0))
            for metric in pending:
                _PENDING_DURABLE_METRICS.pop((target_run_id, metric), None)
            _PENDING_DURABLE_METRIC_EVENTS = max(0, _PENDING_DURABLE_METRIC_EVENTS - event_count)
        if not pending:
            return event_count == 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with _LOCK:
                _PENDING_DURABLE_METRICS.update({(target_run_id, key): value for key, value in pending.items()})
                _PENDING_DURABLE_METRIC_EVENTS_BY_RUN[target_run_id] += event_count
                _PENDING_DURABLE_METRIC_EVENTS += event_count
            return False
        try:
            checkpoint = importlib.import_module("modules.checkpoint")
            checkpoint.record_operational_metrics_batch(
                target_run_id, pending, timeout_seconds=remaining,
            )
        except Exception:
            with _LOCK:
                _PENDING_DURABLE_METRICS.update({(target_run_id, key): value for key, value in pending.items()})
                _PENDING_DURABLE_METRIC_EVENTS_BY_RUN[target_run_id] += event_count
                _PENDING_DURABLE_METRIC_EVENTS += event_count
            return False
        with _LOCK:
            _LAST_DURABLE_METRIC_FLUSH = time.monotonic()
            return not any(key[0] == target_run_id for key in _PENDING_DURABLE_METRICS) and not _PENDING_DURABLE_METRIC_EVENTS_BY_RUN.get(target_run_id, 0)
    finally:
        _METRIC_FLUSH_LOCK.release()


def freeze_durable_run_metrics(run_id: str) -> None:
    """Disarm writes to an immutable handoff run after its metrics are drained."""
    global _DURABLE_RUN_ID
    target_run_id = str(run_id or "").strip()
    with _LOCK:
        if _DURABLE_RUN_ID != target_run_id:
            raise RuntimeError("cannot freeze metrics for a non-current durable run")
        if any(key[0] == target_run_id for key in _PENDING_DURABLE_METRICS) or _PENDING_DURABLE_METRIC_EVENTS_BY_RUN.get(target_run_id, 0):
            raise RuntimeError("cannot freeze durable run with pending operational metrics")
        _DURABLE_RUN_ID = ""


def record_unique(name: str, key: object) -> None:
    value = str(key or "").strip()
    if not value:
        return
    hashed = hashlib.sha256(value.encode("utf-8")).hexdigest()
    durable_run_id = ""
    with _LOCK:
        _UNIQUE_TELEMETRY.setdefault(str(name), set()).add(hashed)
        durable_run_id = _DURABLE_RUN_ID
    if durable_run_id:
        try:
            checkpoint = importlib.import_module("modules.checkpoint")
            checkpoint.record_operational_unique(durable_run_id, str(name), hashed)
        except Exception:
            pass


def wait_for_request_slot(*, waiter=time.sleep, clock=time.monotonic) -> None:
    global _NEXT_REQUEST_AT
    rate = max(float(config.GLOBAL_REQUESTS_PER_SECOND), 0.0)
    if rate <= 0:
        return
    interval = 1.0 / rate
    with _LOCK:
        now = clock()
        wait = max(0.0, _NEXT_REQUEST_AT - now)
        _NEXT_REQUEST_AT = max(now, _NEXT_REQUEST_AT) + interval
    if wait:
        waiter(wait)


def reserve_api(provider: str, budget: int | None = None, *, operation: str = "", request_fingerprint: str = "",
                flight_fingerprint: str = "", execution_generation: int = 0) -> Reservation:
    """Return a durable reservation or an explicit rejection reason."""
    canonical = _PROVIDER_ALIASES.get(str(provider))
    if canonical is None:
        raise ValueError(f"unknown provider: {provider}")
    operation = str(operation or _CURRENT_OPERATION.get())
    item_index = _CURRENT_ITEM_INDEX.get()
    phase_name = _PHASE
    stop = item_stop_state()
    if not paid_provider_call_allowed(canonical):
        record_provider_outcome(
            state="UNKNOWN" if stop.scope == StopScope.MANUAL_AUTHORIZATION else "FAILED",
            reason=stop.reason, call_ids=stop.call_ids, provider=stop.provider or canonical,
            stop_scope=stop.scope,
        )
        return Reservation(False, canonical, operation, item_index, phase_name,
                           call_id=stop.call_ids[-1] if stop.call_ids else "",
                           reason="item_stop_guard", inherited_state="UNKNOWN" if stop.scope == StopScope.MANUAL_AUTHORIZATION else "FAILED")
    def rejected(reason: str) -> Reservation:
        if _DURABLE_RUN_ID and item_index >= 0:
            checkpoint = importlib.import_module("modules.checkpoint")
            checkpoint.record_provider_budget_block(
                run_id=_DURABLE_RUN_ID, item_index=item_index,
                provider=canonical, bucket=search_bucket(), block_kind=str(reason),
            )
        record_provider_outcome(
            state="DUPLICATE" if "duplicate" in reason else "BLOCKED_BUDGET",
            reason=reason, provider=canonical,
        )
        return Reservation(False, canonical, operation, item_index, phase_name, reason=reason)
    if not bool(getattr(config, "PAID_ENABLED", True)):
        record(f"api.{provider}.budget_blocked")
        return rejected("paid_disabled")
    if budget is not None and budget <= 0:
        record(f"api.{provider}.budget_blocked")
        return rejected("budget_disabled")
    if _DURABLE_RUN_ID:
        checkpoint = importlib.import_module("modules.checkpoint")
        effective = _DURABLE_BUDGETS.get(canonical)
        if effective is None or int(effective) <= 0:
            record(f"api.{provider}.budget_blocked")
            return rejected("budget_disabled")
        if _PHASE == "FREE":
            record(f"api.{provider}.budget_blocked")
            return rejected("free_phase")
        fingerprint = request_fingerprint or f"{canonical}:{_CURRENT_ITEM_INDEX.get()}:{operation}"
        previous = checkpoint.provider_call_for_fingerprint(
            run_id=_DURABLE_RUN_ID, provider=canonical,
            item_index=_CURRENT_ITEM_INDEX.get(), phase=_PHASE,
            request_fingerprint=fingerprint,
            query_fingerprint=flight_fingerprint,
            execution_generation=execution_generation,
        )
        retry_allowed = bool(previous and previous.get("state") == "FAILED" and checkpoint.provider_call_retry_allowed(
            run_id=_DURABLE_RUN_ID, provider=canonical, item_index=_CURRENT_ITEM_INDEX.get(),
            phase=_PHASE, request_fingerprint=fingerprint,
            query_fingerprint=flight_fingerprint, execution_generation=execution_generation,
        ))
        if previous and not retry_allowed:
            record(f"api.{provider}.duplicate_request")
            return Reservation(False, canonical, operation, item_index, phase_name,
                               call_id=previous["call_id"],
                               reason="duplicate_request",
                               inherited_state=previous["state"])
        work_item = checkpoint.provider_work_item_for_request(
            run_id=_DURABLE_RUN_ID, item_index=_CURRENT_ITEM_INDEX.get(),
            provider=canonical, operation=operation,
            request_fingerprint=fingerprint,
        )
        if work_item is None:
            # The production wrapper is the authoritative call preparer for
            # conditional resolvers. Persist its exact request before checking
            # dispatch rights; a job first discovered during this round waits
            # for a later fair allocation and cannot bypass the work ledger.
            need_class = {
                "brightdata": "website",
                "google_places": "contact",
                "brandfetch": "identity",
                "hunter": "identity",
                "linkedin": "identity",
                "llm": "identity",
            }[canonical]
            work_item = checkpoint.ensure_provider_work_item(
                run_id=_DURABLE_RUN_ID, item_index=item_index,
                source_record_id=current_source_record_id(),
                provider=canonical, operation=operation,
                request_fingerprint=fingerprint,
                query_fingerprint=flight_fingerprint,
                plan_version=1, need_class=need_class, state="READY",
            )
        has_work_ledger = checkpoint.provider_work_items_exist(_DURABLE_RUN_ID, canonical)
        if checkpoint.provider_dispatch_round_exists(run_id=_DURABLE_RUN_ID, provider=canonical):
            round_ordinal = provider_dispatch_round(canonical)
            if round_ordinal is None or item_index < 0:
                return rejected("dispatch_not_allocated")
            checkpoint.rebind_dispatch_after_terminal_follower(
                run_id=_DURABLE_RUN_ID, provider=canonical,
                round_ordinal=round_ordinal, item_index=item_index,
                source_record_id=current_source_record_id(),
                job_fingerprint=str(work_item.get("job_fingerprint", "")) if work_item else "",
                terminal_follower_receipts=terminal_provider_query_follower_receipts(canonical),
            )
            if not checkpoint.provider_dispatch_allocation_available(
                run_id=_DURABLE_RUN_ID, provider=canonical,
                round_ordinal=round_ordinal, item_index=item_index,
                source_record_id=current_source_record_id(),
                job_fingerprint=str(work_item.get("job_fingerprint", "")) if work_item else "",
            ):
                return rejected("dispatch_not_allocated")
        elif has_work_ledger:
            # A concrete job exists for this provider, but no current round
            # allocated it.  It must wait for a durable READY allocation.
            return rejected("dispatch_not_allocated")
        try:
            call_id = checkpoint.reserve_provider_call(
                run_id=_DURABLE_RUN_ID, provider=canonical, item_index=_CURRENT_ITEM_INDEX.get(),
                phase=_PHASE, operation=operation or _CURRENT_OPERATION.get(),
                request_fingerprint=fingerprint,
                configured_limit=int(effective), effective_limit=int(effective),
                query_fingerprint=flight_fingerprint,
                execution_generation=execution_generation,
                dispatch_round_ordinal=round_ordinal if checkpoint.provider_dispatch_round_exists(run_id=_DURABLE_RUN_ID, provider=canonical) else None,
                dispatch_source_record_id=current_source_record_id(),
            )
        except checkpoint.DispatchAllocationUnavailable:
            return rejected("dispatch_not_allocated")
        if not call_id:
            previous = checkpoint.provider_call_for_fingerprint(
                run_id=_DURABLE_RUN_ID, provider=canonical,
                item_index=_CURRENT_ITEM_INDEX.get(), phase=_PHASE,
                request_fingerprint=fingerprint,
                query_fingerprint=flight_fingerprint,
                execution_generation=execution_generation,
            )
            retry_allowed = bool(previous and previous.get("state") == "FAILED" and checkpoint.provider_call_retry_allowed(
                run_id=_DURABLE_RUN_ID, provider=canonical, item_index=_CURRENT_ITEM_INDEX.get(),
                phase=_PHASE, request_fingerprint=fingerprint,
                query_fingerprint=flight_fingerprint, execution_generation=execution_generation,
            ))
            if previous and not retry_allowed:
                record(f"api.{provider}.duplicate_request")
                return Reservation(False, canonical, operation, item_index, phase_name,
                                   call_id=previous["call_id"],
                                   reason="duplicate_request",
                                   inherited_state=previous["state"])
            record(f"api.{provider}.budget_blocked")
            return rejected("budget_exhausted")
        record_provider_outcome(state="RESERVED", call_ids=(call_id,), provider=canonical)
        return Reservation(True, canonical, operation, item_index, phase_name, call_id=call_id, request_fingerprint=fingerprint)
    with _LOCK:
        budget = int(_default_budget(canonical) if budget is None else budget)
        if budget <= 0:
            return rejected("budget_disabled")
        counter_provider = _counter_provider_name(canonical)
        used_key = f"api.{counter_provider}.requests"
        if _COUNTERS[used_key] >= budget:
            _COUNTERS[f"api.{counter_provider}.budget_blocked"] += 1
            return rejected("budget_exhausted")
        _COUNTERS[used_key] += 1
        call_id = f"volatile:{canonical}:{item_index}:{operation}"
        record_provider_outcome(state="RESERVED", call_ids=(call_id,), provider=canonical)
        fingerprint = request_fingerprint or f"{canonical}:{item_index}:{operation}"
        return Reservation(True, canonical, operation, item_index, phase_name, call_id=call_id, request_fingerprint=fingerprint)


def reserve_crawler_http(budget: int) -> bool:
    """Atomically reserve one crawler request; zero/negative means unlimited."""
    with _LOCK:
        used_key = "http.crawler.requests"
        if budget > 0 and _COUNTERS[used_key] >= budget:
            _COUNTERS["http.crawler.budget_blocked"] += 1
            return False
        _COUNTERS[used_key] += 1
        return True


def _free_result(values: dict) -> FreeReservation:
    return FreeReservation(
        bool(values["accepted"]), str(values["reason"]), str(values["bucket"]),
        int(values["logical_used"]), int(values["logical_limit"]),
        int(values["physical_used"]), int(values["physical_limit"]),
        str(values.get("attempt_id", "")), int(values.get("attempt_ordinal", 0)),
    )


def _volatile_free_reservation(bucket: str, kind: str, *, query_fingerprint: str = "", backend: str = "ddgs") -> FreeReservation:
    run_id, item_index = _DURABLE_RUN_ID or "volatile", _CURRENT_ITEM_INDEX.get()
    bucket = bucket if bucket in {"discovery", "targeted"} else "discovery"
    bucket_limit = 6 if bucket == "discovery" else 4
    physical_limit = bucket_limit * int(getattr(config, "FREE_SEARCH_PHYSICAL_MULTIPLIER", 2))
    with _LOCK:
        logical_key = (run_id, int(item_index), f"{bucket}:logical")
        physical_key = (run_id, int(item_index), f"{bucket}:physical")
        total_logical = sum(value for (rid, idx, name), value in _FREE_QUERY_COUNTS.items() if rid == run_id and idx == int(item_index) and name.endswith(":logical"))
        total_physical = sum(value for (rid, idx, name), value in _FREE_QUERY_COUNTS.items() if rid == run_id and idx == int(item_index) and name.endswith(":physical"))
        key = logical_key if kind == "logical" else physical_key
        total = total_logical if kind == "logical" else total_physical
        total_limit = 10 * int(getattr(config, "FREE_SEARCH_PHYSICAL_MULTIPLIER", 2)) if kind == "physical" else 10
        if total >= total_limit:
            accepted, reason = False, f"{kind}_total_exhausted"
        elif _FREE_QUERY_COUNTS.get(key, 0) >= (physical_limit if kind == "physical" else bucket_limit):
            accepted, reason = False, f"{kind}_bucket_exhausted"
        else:
            accepted, reason = True, "accepted"
            _FREE_QUERY_COUNTS[key] = _FREE_QUERY_COUNTS.get(key, 0) + 1
            if kind == "logical": total_logical += 1
            else:
                total_physical += 1
        attempt_id, ordinal = "", 0
        if accepted and kind == "physical":
            same = [item for item in _FREE_PROVIDER_ATTEMPTS.values() if item["run_id"] == run_id and item["item_index"] == int(item_index) and item["bucket"] == bucket and item["provider"] == backend and item["query_fingerprint"] == query_fingerprint]
            ordinal = len(same) + 1
            seed = f"{run_id}\0{item_index}\0{bucket}\0{backend}\0{query_fingerprint}\0{ordinal}"
            attempt_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            _FREE_PROVIDER_ATTEMPTS[attempt_id] = {"run_id": run_id, "item_index": int(item_index), "bucket": bucket, "provider": backend, "query_fingerprint": query_fingerprint, "attempt_ordinal": ordinal, "state": "RESERVED"}
        return FreeReservation(accepted, reason, bucket, total_logical, 10, total_physical, total_limit if kind == "physical" else 10, attempt_id, ordinal)


def reserve_free_logical_query(bucket: str, query_fingerprint: str) -> FreeReservation:
    bucket = str(bucket or search_bucket()).casefold()
    if _CURRENT_ITEM_INDEX.get() >= 0 and _DURABLE_RUN_ID:
        module = importlib.import_module("modules.checkpoint")
        return _free_result(module.reserve_free_logical_query(run_id=_DURABLE_RUN_ID, item_index=_CURRENT_ITEM_INDEX.get(), bucket=bucket, query_fingerprint=query_fingerprint))
    return _volatile_free_reservation(bucket, "logical")


def reserve_free_physical_attempt(bucket: str, query_fingerprint: str, backend: str) -> FreeReservation:
    bucket = str(bucket or search_bucket()).casefold()
    if _CURRENT_ITEM_INDEX.get() >= 0 and _DURABLE_RUN_ID:
        module = importlib.import_module("modules.checkpoint")
        return _free_result(module.reserve_free_physical_attempt(run_id=_DURABLE_RUN_ID, item_index=_CURRENT_ITEM_INDEX.get(), bucket=bucket, query_fingerprint=query_fingerprint, backend=backend))
    return _volatile_free_reservation(bucket, "physical", query_fingerprint=query_fingerprint, backend=backend)


def free_search_capacity(bucket: str | None = None) -> FreeCapacity:
    bucket = str(bucket or search_bucket()).casefold()
    if bucket not in {"discovery", "targeted"}:
        bucket = "discovery"
    if _CURRENT_ITEM_INDEX.get() >= 0 and _DURABLE_RUN_ID:
        module = importlib.import_module("modules.checkpoint")
        value = module.free_search_capacity(run_id=_DURABLE_RUN_ID, item_index=_CURRENT_ITEM_INDEX.get(), bucket=bucket)
        return FreeCapacity(bool(value["available"]), bucket, int(value["logical_used"]), int(value["logical_limit"]), int(value["physical_used"]), int(value["physical_limit"]))
    run_id, item_index = _DURABLE_RUN_ID or "volatile", _CURRENT_ITEM_INDEX.get()
    bucket_limit = 6 if bucket == "discovery" else 4
    physical_bucket_limit = bucket_limit * int(getattr(config, "FREE_SEARCH_PHYSICAL_MULTIPLIER", 2))
    physical_total_limit = 10 * int(getattr(config, "FREE_SEARCH_PHYSICAL_MULTIPLIER", 2))
    with _LOCK:
        logical = sum(value for (rid, idx, name), value in _FREE_QUERY_COUNTS.items() if rid == run_id and idx == int(item_index) and name.endswith(":logical"))
        physical = sum(1 for value in _FREE_PROVIDER_ATTEMPTS.values() if value["run_id"] == run_id and value["item_index"] == int(item_index))
        bucket_logical = _FREE_QUERY_COUNTS.get((run_id, int(item_index), f"{bucket}:logical"), 0)
        bucket_physical = sum(1 for value in _FREE_PROVIDER_ATTEMPTS.values() if value["run_id"] == run_id and value["item_index"] == int(item_index) and value["bucket"] == bucket)
    return FreeCapacity(logical < 10 and physical < physical_total_limit and bucket_logical < bucket_limit and bucket_physical < physical_bucket_limit, bucket, logical, 10, physical, physical_total_limit)


def complete_free_physical_attempt(attempt_id: str, success: bool, error_class: str | None = None) -> None:
    if _CURRENT_ITEM_INDEX.get() >= 0 and _DURABLE_RUN_ID:
        module = importlib.import_module("modules.checkpoint")
        module.complete_free_physical_attempt(attempt_id=str(attempt_id), run_id=_DURABLE_RUN_ID, success=bool(success), error_class=error_class)
    else:
        with _LOCK:
            attempt = _FREE_PROVIDER_ATTEMPTS.get(str(attempt_id))
            if attempt is None or attempt.get("run_id") != (_DURABLE_RUN_ID or "volatile"):
                module = importlib.import_module("modules.checkpoint")
                raise module.SchedulerInvariantError("free physical attempt completion references unknown/wrong-run attempt")
            if attempt.get("state") != "RESERVED":
                module = importlib.import_module("modules.checkpoint")
                raise module.SchedulerInvariantError("free physical attempt completed more than once")
            attempt["state"] = "DONE" if success else "FAILED"
            attempt["error_class"] = str(error_class or "")[:120]
    record("http.search.physical_completed" if success else "http.search.physical_failed")


def reserve_search_query(budget: int, *, bucket: str | None = None) -> bool:
    """Atomically reserve one free query per durable (run,item), max ten."""
    selected_bucket = str(bucket if bucket is not None else search_bucket())
    if not selected_bucket and _CURRENT_ITEM_INDEX.get() >= 0 and _DURABLE_RUN_ID:
        module = importlib.import_module("modules.checkpoint")
        limit = 10 if int(budget) <= 0 else min(10, int(budget))
        accepted = module.reserve_free_search_query(run_id=_DURABLE_RUN_ID, item_index=_CURRENT_ITEM_INDEX.get(), limit=limit, bucket=None)
        record("http.search.requests" if accepted else "http.search.budget_blocked")
        return accepted
    if not selected_bucket:
        run_id, item_index = _DURABLE_RUN_ID or "volatile", _CURRENT_ITEM_INDEX.get()
        limit = 10 if int(budget) <= 0 else min(10, int(budget))
        with _LOCK:
            key = (run_id, int(item_index), "legacy:logical")
            accepted = _FREE_QUERY_COUNTS.get(key, 0) < limit
            if accepted:
                _FREE_QUERY_COUNTS[key] = _FREE_QUERY_COUNTS.get(key, 0) + 1
        record("http.search.requests" if accepted else "http.search.budget_blocked")
        return accepted
    result = reserve_free_logical_query(selected_bucket, "legacy")
    record("http.search.requests" if result.accepted else "http.search.budget_blocked")
    return result.accepted


def merge_durable_telemetry(telemetry: dict) -> None:
    global _DURABLE_TELEMETRY
    with _LOCK:
        _DURABLE_TELEMETRY = dict(telemetry or {})


def snapshot() -> dict:
    with _LOCK:
        counters = dict(sorted(_COUNTERS.items()))
        unique_counts = {
            name: len(values) for name, values in sorted(_UNIQUE_TELEMETRY.items())
        }
        unique_keys = {
            name: sorted(values) for name, values in sorted(_UNIQUE_TELEMETRY.items())
        }
        elapsed = time.monotonic() - _STARTED_AT
    companies = int(counters.get("pipeline.companies", 0))
    provider_budget_details = {}
    for provider in sorted(CANONICAL_PAID_PROVIDERS):
        metadata = dict(_DURABLE_BUDGET_METADATA.get(provider, {}))
        effective = int(_DURABLE_BUDGETS.get(provider, metadata.get("effective_limit", 0) or 0))
        provider_budget_details[provider] = {
            "population_count": companies,
            "ratio": None,
            "explicit_cap": None,
            "configured_limit": effective,
            "effective_limit": effective,
            "reserved_total": 0,
            "reserved": 0,
            "running": 0,
            "done": 0,
            "failed": 0,
            "unknown": 0,
            "physical_http_attempts": 0,
            "retry_attempts": 0,
            "inherited_uses": 0,
            "budget_blocked_items": int(counters.get(f"api.{provider}.budget_blocked", 0)),
            **metadata,
        }
    if isinstance(_DURABLE_TELEMETRY.get("provider_budgets"), dict):
        provider_budget_details = dict(_DURABLE_TELEMETRY["provider_budgets"])
    empty_plan_json = "[]"
    durable_scheduler = dict(_DURABLE_TELEMETRY) if _DURABLE_TELEMETRY else {
        "receipt_schema_version": 1,
        "total_items": companies, "free_completed": 0, "free_failed": 0,
        "item_terminal": 0, "paid_required": 0, "paid_completed": 0,
        "result_count": 0, "manifest_count": 0,
        "free_queries": {
            "logical_used": 0, "logical_accepted": 0, "logical_blocked": 0,
            "discovery_logical_accepted": 0, "targeted_logical_accepted": 0,
            "physical_attempted": 0, "physical_done": 0, "physical_failed": 0,
            "physical_reserved": 0, "physical_blocked": 0,
            "discovery_physical_attempted": 0, "targeted_physical_attempted": 0,
            "buckets": {bucket: {"logical_accepted": 0, "physical_attempted": 0, "done": 0, "failed": 0, "reserved": 0, "unique_logical_blocks": 0, "unique_physical_blocks": 0} for bucket in ("discovery", "targeted")},
        },
        "provider_budgets": provider_budget_details,
        "paid_query_limit_per_company": 0,
        "paid_query_plan": {"plan_version": 1, "paid_query_plan_count": 0, "paid_query_plan_sha256": hashlib.sha256(empty_plan_json.encode("utf-8")).hexdigest()},
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 3),
        "phase": phase(),
        "counters": counters,
        "unique_counts": unique_counts,
        "unique_keys": unique_keys,
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
        "provider_budgets": provider_budget_details,
        "durable_scheduler": durable_scheduler,
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
