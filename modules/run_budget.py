"""Runtime budget calculations and provider limit scaling."""

from __future__ import annotations

import math

import config
from modules import runtime


PAID_API_PROVIDERS = ("brightdata", "google_places", "hunter", "brandfetch")


def explicit_paid_api_caps() -> dict[str, int | None]:
    return {
        "brightdata": config.BRIGHTDATA_REQUEST_HARD_CAP,
        "google_places": config.GOOGLE_PLACES_REQUEST_HARD_CAP,
        "hunter": config.HUNTER_REQUEST_HARD_CAP,
        "brandfetch": config.BRANDFETCH_REQUEST_HARD_CAP,
    }


def calculate_paid_api_budgets(
    company_count: int, caps: dict[str, int | None]
) -> dict[str, int]:
    """Calculate effective provider budgets without changing process state."""
    count = max(0, int(company_count))
    ratios = {
        "brightdata": config.BRIGHTDATA_REQUEST_RATIO,
        "google_places": config.GOOGLE_PLACES_REQUEST_RATIO,
        "hunter": config.HUNTER_REQUEST_RATIO,
        "brandfetch": config.BRANDFETCH_REQUEST_RATIO,
    }
    budgets: dict[str, int] = {}
    for provider in PAID_API_PROVIDERS:
        cap = caps.get(provider)
        calculated = math.ceil(count * float(ratios[provider]))
        if cap is None:
            budgets[provider] = calculated
        elif int(cap) <= 0:
            budgets[provider] = 0
        else:
            budgets[provider] = min(calculated, int(cap))
    return budgets


def budget_details(
    company_count: int,
    caps: dict[str, int | None],
    effective_budgets: dict[str, int],
) -> dict[str, dict[str, object]]:
    ratios = {
        "brightdata": config.BRIGHTDATA_REQUEST_RATIO,
        "google_places": config.GOOGLE_PLACES_REQUEST_RATIO,
        "hunter": config.HUNTER_REQUEST_RATIO,
        "brandfetch": config.BRANDFETCH_REQUEST_RATIO,
    }
    return {
        provider: {
            "population_count": max(0, int(company_count)),
            "ratio": float(ratios[provider]),
            "explicit_cap": caps.get(provider),
            "effective_budget": int(effective_budgets.get(provider, 0)),
            "reserved": 0,
            "completed": 0,
            "blocked": 0,
        }
        for provider in PAID_API_PROVIDERS
    }


def configure_run_budget(company_count: int) -> int:
    """Reserve retry headroom and spread paid discovery across the full run."""
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
    budgets = calculate_paid_api_budgets(company_count, explicit_paid_api_caps())
    config.BRIGHTDATA_REQUEST_BUDGET = budgets["brightdata"]
    config.GOOGLE_PLACES_REQUEST_BUDGET = budgets["google_places"]
    config.HUNTER_REQUEST_BUDGET = budgets["hunter"]
    config.BRANDFETCH_REQUEST_BUDGET = budgets["brandfetch"]
    for provider, budget in budgets.items():
        runtime.record(f"budget.{provider}.scaled", budget)
    return budgets
