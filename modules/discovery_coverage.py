"""Thread-safe discovery coverage and cache-acquisition audit."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from modules import query_planner, runtime, scorer


POLICY_VERSION = "discovery-coverage-v1"
_LOCK = threading.Lock()
_QUERIES: dict[tuple[str, str, str], dict] = {}
_COMPANIES: dict[str, dict] = {}


def reset() -> None:
    global _QUERIES, _COMPANIES
    with _LOCK:
        _QUERIES = {}
        _COMPANIES = {}


def _query_key(query: str) -> str:
    return " ".join(str(query or "").split()).casefold()


def record_query(
    company: str,
    query: str,
    phase: str,
    cache_status: str,
    result_count: int,
    evidence_gaps: set[str] | None = None,
) -> None:
    if not company or not query:
        return
    row = {
        "company": company,
        "query": query,
        "phase": phase,
        "intent": query_planner.query_intent(query),
        "cache_status": cache_status or "unknown",
        "result_count": max(0, int(result_count)),
        "evidence_gaps": sorted(evidence_gaps or set()),
    }
    marker = (scorer.normalize_text(company), phase, _query_key(query))
    with _LOCK:
        _QUERIES[marker] = row
    runtime.record(f"discovery_coverage.query.{row['cache_status']}")
    if not row["result_count"]:
        runtime.record("discovery_coverage.query.empty")


def finalize_company(company: str, *, resolved: bool, candidate_count: int) -> None:
    with _LOCK:
        _COMPANIES[scorer.normalize_text(company)] = {
            "company": company,
            "resolved": bool(resolved),
            "candidate_count": max(0, int(candidate_count)),
        }
    runtime.record(
        "discovery_coverage.company.resolved"
        if resolved else "discovery_coverage.company.unresolved"
    )


def mark_published(company: str) -> None:
    """Remove a company from acquisition needs after first-party publication."""
    key = scorer.normalize_text(company)
    with _LOCK:
        current = _COMPANIES.get(key, {
            "company": company,
            "candidate_count": 0,
        })
        current["resolved"] = True
        _COMPANIES[key] = current


def _priority(row: dict) -> tuple[int, int, str]:
    intent_scores = {
        "legal_identity": 100,
        "relationship": 95,
        "country_official": 90,
        "official": 80,
        "context": 70,
        "contact": 60,
    }
    phase_scores = {"adaptive": 3, "primary": 2, "fallback": 1}
    return (
        intent_scores.get(row["intent"], 0),
        phase_scores.get(row["phase"], 0),
        row["query"],
    )


def payload(max_queries_per_company: int = 3) -> dict:
    with _LOCK:
        queries = [dict(row) for row in _QUERIES.values()]
        companies = [dict(row) for row in _COMPANIES.values()]
    unresolved = {
        scorer.normalize_text(row["company"])
        for row in companies if not row["resolved"]
    }
    acquisition_plan: list[dict] = []
    for company_key in sorted(unresolved):
        rows = [
            row for row in queries
            if scorer.normalize_text(row["company"]) == company_key
            and row["cache_status"] in {"replay_miss", "error"}
        ]
        selected: list[dict] = []
        seen_intents: set[str] = set()
        for row in sorted(rows, key=_priority, reverse=True):
            if row["intent"] in seen_intents:
                continue
            selected.append(row)
            seen_intents.add(row["intent"])
            if len(selected) >= max(1, int(max_queries_per_company)):
                break
        acquisition_plan.extend({
            "company": row["company"],
            "query": row["query"],
            "intent": row["intent"],
            "reason": "unresolved_replay_cache_gap",
            "requires_authorized_search": True,
        } for row in selected)
    return {
        "policy_version": POLICY_VERSION,
        "company_count": len(companies),
        "resolved_companies": sum(1 for row in companies if row["resolved"]),
        "unresolved_companies": sum(1 for row in companies if not row["resolved"]),
        "query_count": len(queries),
        "replay_miss_count": sum(
            1 for row in queries if row["cache_status"] == "replay_miss"
        ),
        "cached_empty_count": sum(
            1 for row in queries
            if row["cache_status"] in {"cache_hit", "replay_fallback_hit"}
            and not row["result_count"]
        ),
        "acquisition_plan": acquisition_plan,
        "queries": sorted(
            queries,
            key=lambda row: (
                scorer.normalize_text(row["company"]), row["phase"], row["query"],
            ),
        ),
    }


def write(path: Path, max_queries_per_company: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload(max_queries_per_company),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
