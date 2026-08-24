"""Runtime budget calculations and provider limit scaling."""

from __future__ import annotations

import math

import config
from modules import runtime


def configure_run_budget(company_count: int) -> int:
    """Reserve retry headroom and spread paid discovery across the full run."""
    if company_count > 0 and config.SEARCH_HTTP_REQUEST_BUDGET <= 0:
        config.SEARCH_HTTP_REQUEST_BUDGET = (
            company_count * config.DEFAULT_FREE_SEARCH_QUERY_LIMIT_PER_COMPANY
        )
    configured = (
        config.MAX_SEARCH_QUERIES_PER_COMPANY
        if config.MAX_SEARCH_QUERIES_PER_COMPANY > 0
        else config.DEFAULT_PAID_SEARCH_QUERY_LIMIT
    )
    if (
        config.SEARCH_PROVIDER != "brightdata"
        or config.SEARCH_CACHE_MODE == "replay"
        or config.BRIGHTDATA_REQUEST_BUDGET <= 0
        or company_count <= 0
    ):
        limit = max(0, configured)
    else:
        usable_budget = int(
            config.BRIGHTDATA_REQUEST_BUDGET
            * (1.0 - config.BRIGHTDATA_RETRY_RESERVE_FRACTION)
        )
        fair_share = max(1, usable_budget // company_count)
        limit = min(max(1, configured), fair_share)
    runtime.record("search.paid_query_limit_per_company", limit)
    return limit


def scale_paid_api_budgets(company_count: int) -> dict[str, int]:
    """Scale paid ceilings to the firms that actually need escalation."""
    count = max(0, int(company_count))
    budgets = {
        "brightdata": min(
            config.BRIGHTDATA_REQUEST_HARD_CAP,
            math.ceil(count * config.BRIGHTDATA_REQUEST_RATIO),
        ),
        "google_places": min(
            config.GOOGLE_PLACES_REQUEST_HARD_CAP,
            math.ceil(count * config.GOOGLE_PLACES_REQUEST_RATIO),
        ),
        "hunter": min(
            config.HUNTER_REQUEST_HARD_CAP,
            math.ceil(count * config.HUNTER_REQUEST_RATIO),
        ),
        "brandfetch": min(
            config.BRANDFETCH_REQUEST_HARD_CAP,
            math.ceil(count * config.BRANDFETCH_REQUEST_RATIO),
        ),
    }
    config.BRIGHTDATA_REQUEST_BUDGET = budgets["brightdata"]
    config.GOOGLE_PLACES_REQUEST_BUDGET = budgets["google_places"]
    config.HUNTER_REQUEST_BUDGET = budgets["hunter"]
    config.BRANDFETCH_REQUEST_BUDGET = budgets["brandfetch"]
    for provider, budget in budgets.items():
        runtime.record(f"budget.{provider}.scaled", budget)
    return budgets
