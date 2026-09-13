"""Thread-safe discovery coverage and cache-acquisition audit."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from modules import query_planner, runtime, scorer


POLICY_VERSION = "discovery-coverage-v1"
_LOCK = threading.Lock()
_QUERIES: dict[tuple[str, str, str], dict] = {}
_COMPANIES: dict[str, dict] = {}
_SOURCES: dict[str, dict] = {}
_REPLAY_MISSES = 0


def reset() -> None:
    global _QUERIES, _COMPANIES, _SOURCES, _REPLAY_MISSES
    with _LOCK:
        _QUERIES = {}
        _COMPANIES = {}
        _SOURCES = {}
        _REPLAY_MISSES = 0


def record_replay_miss(kind: str) -> None:
    """Count a typed replay miss without allowing a network fallback."""
    global _REPLAY_MISSES
    with _LOCK:
        _REPLAY_MISSES += 1
    runtime.record(f"discovery_coverage.replay_miss.{str(kind or 'unknown')}")


def register_source(
    source_record_id: str,
    *,
    company: str = "",
    original_index: int | None = None,
    stage: str = "input",
) -> None:
    """Register input/scheduler/evidence coverage independently of publication."""
    source_id = str(source_record_id or "").strip()
    if not source_id:
        return
    with _LOCK:
        current = _SOURCES.setdefault(source_id, {
            "source_record_id": source_id,
            "company": company,
            "original_index": original_index,
            "stages": [],
        })
        if company:
            current["company"] = company
        if original_index is not None:
            current["original_index"] = int(original_index)
        if stage and stage not in current["stages"]:
            current["stages"].append(stage)


def _coverage_key(company: str, source_record_id: str | None) -> str:
    return str(source_record_id or "").strip() or scorer.normalize_text(company)


def _query_key(query: str) -> str:
    return " ".join(str(query or "").split()).casefold()


def record_query(
    company: str,
    query: str,
    phase: str,
    cache_status: str,
    result_count: int,
    evidence_gaps: set[str] | None = None,
    *,
    source_record_id: str | None = None,
    original_index: int | None = None,
) -> None:
    if not company or not query:
        return
    source_id = str(source_record_id or "").strip()
    if source_id:
        register_source(source_id, company=company, original_index=original_index, stage="evidence")
    row = {
        "company": company,
        "source_record_id": source_id,
        "original_index": original_index,
        "query": query,
        "phase": phase,
        "intent": query_planner.query_intent(query),
        "cache_status": cache_status or "unknown",
        "result_count": max(0, int(result_count)),
        "evidence_gaps": sorted(evidence_gaps or set()),
    }
    marker = (_coverage_key(company, source_id), phase, _query_key(query))
    with _LOCK:
        _QUERIES[marker] = row
    runtime.record(f"discovery_coverage.query.{row['cache_status']}")
    if not row["result_count"]:
        runtime.record("discovery_coverage.query.empty")


def finalize_company(
    company: str,
    *,
    resolved: bool,
    candidate_count: int,
    source_record_id: str | None = None,
    original_index: int | None = None,
    terminal_reason: str = "",
) -> None:
    source_id = str(source_record_id or "").strip()
    if source_id:
        register_source(source_id, company=company, original_index=original_index, stage="scheduler")
    with _LOCK:
        _COMPANIES[_coverage_key(company, source_id)] = {
            "company": company,
            "source_record_id": source_id,
            "original_index": original_index,
            "resolved": bool(resolved),
            "candidate_count": max(0, int(candidate_count)),
            "terminal_reason": terminal_reason,
        }
    runtime.record(
        "discovery_coverage.company.resolved"
        if resolved else "discovery_coverage.company.unresolved"
    )


def mark_published(
    company: str,
    source_record_id: str | None = None,
    original_index: int | None = None,
) -> None:
    """Remove a company from acquisition needs after first-party publication."""
    source_id = str(source_record_id or "").strip()
    if source_id:
        register_source(source_id, company=company, original_index=original_index, stage="publication")
    key = _coverage_key(company, source_id)
    with _LOCK:
        current = _COMPANIES.get(key, {
            "company": company,
            "source_record_id": source_id,
            "original_index": original_index,
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
            if _coverage_key(row["company"], row.get("source_record_id")) == company_key
            and (
                row["cache_status"] in {"replay_miss", "error", "budget_blocked"}
                or (
                    row["cache_status"] == "live_fallback"
                    and not row["result_count"]
                )
            )
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
            "reason": (
                "unresolved_search_budget_gap"
                if row["cache_status"] == "budget_blocked"
                else "unresolved_provider_gap"
                if row["cache_status"] in {"error", "live_fallback"}
                else "unresolved_replay_cache_gap"
            ),
            "requires_authorized_search": True,
        } for row in selected)
    with _LOCK:
        sources = [dict(value) for value in _SOURCES.values()]
    known = {str(row.get("source_record_id", "")) for row in companies if row.get("source_record_id")}
    source_rows = []
    for source in sorted(sources, key=lambda row: (row.get("original_index") is None, row.get("original_index") or 0, row["source_record_id"])):
        source_id = source["source_record_id"]
        company_row = next((row for row in companies if row.get("source_record_id") == source_id), None)
        source_rows.append({
            **source,
            "status": "resolved" if company_row and company_row.get("resolved") else "unresolved" if company_row else "unprocessed",
            "terminal_reason": (company_row or {}).get("terminal_reason", "") or ("source_not_processed" if source_id not in known else ""),
        })
    with _LOCK:
        replay_misses = int(_REPLAY_MISSES)
    return {
        "policy_version": POLICY_VERSION,
        "company_count": len(companies),
        "source_count": len(sources),
        "coverage_complete": bool(sources) and len(source_rows) == len(sources) and all(row["status"] != "unprocessed" for row in source_rows),
        "source_coverage": source_rows,
        "resolved_companies": sum(1 for row in companies if row["resolved"]),
        "unresolved_companies": sum(1 for row in companies if not row["resolved"]),
        "query_count": len(queries),
        "replay_miss_count": replay_misses + sum(
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
    from modules import redaction

    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized_payload = redaction.sanitize(payload(max_queries_per_company))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(sanitized_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if temporary.exists():
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
