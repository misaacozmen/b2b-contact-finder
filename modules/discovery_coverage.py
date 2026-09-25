"""Thread-safe discovery coverage and cache-acquisition audit."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from modules import publication_policy, query_planner, runtime, scorer


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
    durable_run_id = str(runtime.durable_run_id() or "").strip()
    if durable_run_id and source_id:
        from modules import checkpoint
        checkpoint.record_discovery_attempt(
            run_id=durable_run_id,
            source_record_id=source_id,
            attempt_id=checkpoint.discovery_event_id(
                f"query:{phase}:{_query_key(query)}",
                cache_status=cache_status, result_count=int(result_count),
                evidence_gaps=sorted(evidence_gaps or set()),
            ),
            stage="query",
            provider="",
            query_id=_query_key(query),
            transport_outcome=cache_status,
            semantic_result="RESULTS" if int(result_count) else "EMPTY",
            reason=";".join(sorted(evidence_gaps or set())),
        )
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
    loss_stage: str = "",
    primary_reason: str = "",
    missing_evidence: set[str] | list[str] | tuple[str, ...] | None = None,
    next_action: str = "",
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
            "loss_stage": str(loss_stage or ""),
            "primary_reason": str(primary_reason or terminal_reason or ""),
            "missing_evidence": sorted({str(value) for value in (missing_evidence or ()) if str(value)}),
            "next_action": str(next_action or ""),
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
    durable_run_id = str(runtime.durable_run_id() or "").strip()
    attempts: list[dict] = []
    durable_sources_loaded = False
    coverage_incomplete = False
    coverage_error = ""
    if durable_run_id:
        from modules import checkpoint
        try:
            durable_items = checkpoint.load_run_items(durable_run_id)
        except sqlite3.Error as exc:
            durable_items = []
            coverage_incomplete = True
            coverage_error = f"{type(exc).__name__}:{exc}"
        except Exception as exc:
            durable_items = []
            coverage_incomplete = True
            coverage_error = f"{type(exc).__name__}:{exc}"
        if durable_items and not coverage_incomplete:
            snapshots = checkpoint.load_input_snapshots(durable_run_id)
            durable_results = checkpoint.load_results_by_id(durable_run_id)
            durable_companies: list[dict] = []
            durable_sources: list[dict] = []
            for item in durable_items:
                index = int(item.get("item_index", 0))
                source_id = str(item.get("source_record_id", ""))
                snapshot = snapshots.get(index, {})
                result = durable_results.get(index, {}) if isinstance(durable_results, dict) else {}
                company = str(snapshot.get("company") or snapshot.get("name") or source_id)
                free_state = str(item.get("free_state") or "")
                paid_state = str(item.get("paid_state") or "")
                processed = bool(result) or free_state in {"DONE", "FAILED", "NOT_REQUIRED"}
                resolved = publication_policy.is_publishable_row(result) if isinstance(result, dict) else False
                durable_companies.append({
                    "company": company, "source_record_id": source_id,
                    "original_index": index, "resolved": resolved,
                    "candidate_count": len(result.get("candidates", [])) if isinstance(result, dict) and isinstance(result.get("candidates"), list) else int(bool(result)),
                    "terminal_reason": "processed" if processed else "source_not_processed",
                    "loss_stage": "" if resolved else _typed_loss_stage(item, result),
                    "primary_reason": str((result or {}).get("reason", "") if isinstance(result, dict) else ""),
                    "missing_evidence": list((result or {}).get("content_decision", {}).get("missing_evidence", []) if isinstance(result, dict) and isinstance(result.get("content_decision"), dict) else []),
                    "next_action": "publish" if resolved else _typed_next_action(item, result),
                })
                durable_sources.append({
                    "source_record_id": source_id, "company": company,
                    "original_index": index,
                    "stages": ["input", "scheduler"] if processed else ["input"],
                })
            companies = durable_companies
            durable_sources_loaded = True
            with _LOCK:
                sources = [dict(value) for value in _SOURCES.values()]
            source_ids = {str(value.get("source_record_id")) for value in sources}
            sources.extend(value for value in durable_sources if value["source_record_id"] not in source_ids)
        elif not coverage_incomplete:
            try:
                attempts = checkpoint.load_discovery_attempts(durable_run_id)
            except Exception:
                attempts = []
            attempt_source_ids = sorted({str(item.get("source_record_id", "")).strip() for item in attempts if str(item.get("source_record_id", "")).strip()})
            if attempt_source_ids:
                companies = [{
                    "company": source_id,
                    "source_record_id": source_id,
                    "original_index": None,
                    "resolved": False,
                    "candidate_count": 0,
                    "terminal_reason": "attempt_only_no_scheduler_snapshot",
                    "loss_stage": "discovery_attempt",
                    "primary_reason": "durable_attempt_without_input_snapshot",
                    "missing_evidence": ["input_snapshot"],
                    "next_action": "restore_input_snapshot_before_resume",
                } for source_id in attempt_source_ids]
                sources = [{
                    "source_record_id": source_id,
                    "company": source_id,
                    "original_index": None,
                    "stages": ["attempt_only"],
                } for source_id in attempt_source_ids]
                durable_sources_loaded = True
    unresolved = {
        _coverage_key(row["company"], row.get("source_record_id"))
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
            "source_record_id": row.get("source_record_id", ""),
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
    if not durable_sources_loaded:
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
    if durable_run_id:
        from modules import checkpoint
        if not attempts:
            try:
                attempts = checkpoint.load_discovery_attempts(durable_run_id)
            except Exception:
                attempts = []
    attempts_by_source: dict[str, list[dict]] = {}
    for attempt in attempts:
        attempts_by_source.setdefault(str(attempt.get("source_record_id", "")), []).append(attempt)
    for row in source_rows:
        source_attempts = attempts_by_source.get(str(row.get("source_record_id", "")), [])
        row["attempt_count"] = len(source_attempts)
        row["attempted_stages"] = sorted({str(item.get("stage", "")) for item in source_attempts if item.get("stage")})
        company_row = next((item for item in companies if item.get("source_record_id") == row.get("source_record_id")), None)
        row["loss_stage"] = str((company_row or {}).get("loss_stage", "") or "")
        row["primary_reason"] = str((company_row or {}).get("primary_reason", "") or row.get("terminal_reason", "") or "")
        row["missing_evidence"] = list((company_row or {}).get("missing_evidence", []) or [])
        row["next_action"] = str((company_row or {}).get("next_action", "") or ("publish" if row.get("status") == "resolved" else "resume_typed_stage"))
    return {
        "policy_version": POLICY_VERSION,
        "company_count": len(companies),
        "source_count": len(sources),
        "coverage_complete": (not coverage_incomplete) and bool(sources) and len(source_rows) == len(sources) and all(row["status"] != "unprocessed" for row in source_rows),
        "coverage_incomplete": coverage_incomplete,
        "coverage_error": coverage_error,
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
        "attempt_count": len(attempts),
        "discovery_attempts": attempts,
    }


def _typed_loss_stage(item: dict, result: dict) -> str:
    if not isinstance(result, dict):
        return "scheduler_free_pending" if str(item.get("free_state")) in {"PENDING", "RUNNING"} else "scheduler_paid_pending"
    content = result.get("content_decision") if isinstance(result.get("content_decision"), dict) else {}
    missing = {str(value) for value in content.get("missing_evidence", [])}
    if any("website" in value or "ownership" in value or "identity" in value for value in missing):
        return "identity_discovery"
    if "email" in " ".join(missing):
        return "contact_email"
    if "phone" in " ".join(missing):
        return "contact_phone"
    if str(item.get("paid_state")) in {"PENDING", "RUNNING", "UNKNOWN"}:
        return "scheduler_paid"
    return "content_evaluation"


def _typed_next_action(item: dict, result: dict) -> str:
    stage = _typed_loss_stage(item, result)
    return {
        "identity_discovery": "authorized_website_search",
        "contact_email": "acquire_email",
        "contact_phone": "acquire_phone",
        "scheduler_paid": "resume_paid_attempt",
        "scheduler_free_pending": "resume_free_attempt",
        "scheduler_paid_pending": "resume_paid_attempt",
        "content_evaluation": "reevaluate_content",
    }.get(stage, "reevaluate_content")


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
