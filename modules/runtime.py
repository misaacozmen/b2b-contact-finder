"""Thread-safe run budgets, global rate limiting and lightweight telemetry."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import config


_LOCK = threading.Lock()
_COUNTERS: Counter = Counter()
_STARTED_AT = time.monotonic()
_NEXT_REQUEST_AT = 0.0


def reset() -> None:
    global _COUNTERS, _STARTED_AT, _NEXT_REQUEST_AT
    with _LOCK:
        _COUNTERS = Counter()
        _STARTED_AT = time.monotonic()
        _NEXT_REQUEST_AT = 0.0


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


def reserve_api(provider: str, budget: int) -> bool:
    """Atomically reserve one paid call; zero/negative means disabled budget."""
    if budget <= 0:
        record(f"api.{provider}.budget_blocked")
        return False
    with _LOCK:
        used_key = f"api.{provider}.requests"
        if _COUNTERS[used_key] >= budget:
            _COUNTERS[f"api.{provider}.budget_blocked"] += 1
            return False
        _COUNTERS[used_key] += 1
        return True


def snapshot() -> dict:
    with _LOCK:
        counters = dict(sorted(_COUNTERS.items()))
        elapsed = time.monotonic() - _STARTED_AT
    companies = int(counters.get("pipeline.companies", 0))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 3),
        "counters": counters,
        "budgets": {
            "brightdata": config.BRIGHTDATA_REQUEST_BUDGET,
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
        },
    }


def write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
