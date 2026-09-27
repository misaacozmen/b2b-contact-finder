"""Collect offline features from FREE-phase Stage A candidate evaluations."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable

from modules import phone, reference_resolution, scorer


_entries: ContextVar[list[dict] | None] = ContextVar("stage_a_feature_entries", default=None)


def begin() -> None:
    """Start a context-local Stage A feature collection."""
    _entries.set([])


def collect() -> list[dict]:
    """Materialize features after domain/profile deduplication."""
    entries = _entries.get()
    if entries is None:
        return []
    return [
        {
            "features": _features(
                item["company"], item["candidate"], item["evaluation"], item["metadata"],
            ),
            "evaluation": item["evaluation"],
        }
        for item in entries
    ]


def end() -> None:
    """Stop collection in the current context."""
    _entries.set(None)


def _compute(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:
        return None


def _crawl_profile(candidate: dict) -> str | None:
    history = candidate.get("_stage_history", []) or []
    for stage in reversed(history):
        name = str(stage.get("stage", "")) if isinstance(stage, dict) else ""
        if name == "full_evaluated":
            return "full"
        if name == "identity_evaluated":
            return "identity"
    return None


def _rank_best(candidate: dict) -> int | None:
    evidence = candidate.get("_search_evidence", []) or []
    query_evidence = [item for item in evidence if isinstance(item, dict) and "query" in item]
    if not query_evidence:
        return 99
    return min(int(item["rank"]) for item in query_evidence)


def _query_hits(candidate: dict) -> int | None:
    evidence = candidate.get("_search_evidence", []) or []
    queries = {item["query"] for item in evidence if isinstance(item, dict) and "query" in item}
    return len(queries)


def _brand_prefix_len(company: str, domain: str | None) -> int | None:
    tokens = scorer.distinctive_tokens(company)
    if not tokens:
        return 0
    if not domain:
        return None
    token = tokens[0]
    core = scorer.compact_domain_core(domain)
    return len(token) if core.startswith(token) else 0


def _same_domain_email(observation: dict | None, domain: str | None) -> bool | None:
    if observation is None or not domain:
        return None
    return any(
        "@" in str(email)
        and scorer.same_registrable_domain(str(email).rsplit("@", 1)[-1], domain)
        for email in observation.get("emails", [])
    )


def _tr_phone(observation: dict | None) -> bool | None:
    if observation is None:
        return None
    return any(phone.normalize_phone(str(value)) for value in observation.get("phones", []))


def _features(company: str, candidate: dict, evaluation: dict, metadata: dict | None) -> dict:
    url = _compute(lambda: candidate["url"])
    domain = _compute(lambda: scorer.registrable_domain(url)) if url is not None else None
    observation = _compute(lambda: reference_resolution.observe(evaluation, url, metadata or {})) if url is not None else None
    signals = _compute(lambda: reference_resolution.site_signals(observation, company)) if observation is not None else None
    return {
        "domain": domain,
        "url": url,
        "crawl_profile": _compute(lambda: _crawl_profile(candidate)),
        "reachable": _compute(lambda: observation["reachable"]) if observation is not None else None,
        "brand_prefix_len": _compute(lambda: _brand_prefix_len(company, domain)),
        "rank_best": _compute(lambda: _rank_best(candidate)),
        "query_hits": _compute(lambda: _query_hits(candidate)),
        "admission": _compute(lambda: "brand_prefix" if "brand_prefix_admission" in str(candidate.get("reason", "")) else "score"),
        "s1": _compute(lambda: signals["s1"]) if signals is not None else None,
        "s2": _compute(lambda: signals["s2"]) if signals is not None else None,
        "s3": _compute(lambda: signals["s3"]) if signals is not None else None,
        "s4": _compute(lambda: signals["s4"]) if signals is not None else None,
        "parked": _compute(lambda: signals["parked"]) if signals is not None else None,
        "thin": _compute(lambda: signals["thin"]) if signals is not None else None,
        "conflict": _compute(lambda: signals["conflict"]) if signals is not None else None,
        "same_domain_email": _compute(lambda: _same_domain_email(observation, domain)),
        "tr_phone": _compute(lambda: _tr_phone(observation)),
        "legacy_final_score": _compute(lambda: evaluation["final_score"]),
        "legacy_publishable": _compute(
            lambda: evaluation.get("identity_assessment", {}).get("publishable", False)
        ),
    }


def record(company: str, candidate: dict, evaluation: dict, metadata: dict | None) -> None:
    """Record one candidate without affecting behavior if collection is inactive."""
    entries = _entries.get()
    if entries is None:
        return
    try:
        url = candidate["url"]
        domain = _compute(lambda: scorer.registrable_domain(url)) if url is not None else None
        profile = _compute(lambda: _crawl_profile(candidate))
        entry = {
            "company": company, "candidate": candidate, "evaluation": evaluation,
            "metadata": metadata or {}, "domain": domain, "profile": profile,
        }
        if domain:
            for index, previous in enumerate(entries):
                if previous.get("domain") != domain:
                    continue
                if (profile == "full") > (previous.get("profile") == "full"):
                    entries[index] = entry
                return
        entries.append(entry)
    except Exception:
        # Feature collection is observational and must not change the run.
        return
