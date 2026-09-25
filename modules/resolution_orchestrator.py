"""Multi-round evidence acquisition and candidate resolution orchestrator."""

from __future__ import annotations

from typing import Callable

import config
from modules import (
    entity_resolution,
    evidence_acquisition,
    identity,
    runtime,
    scorer,
    search,
)


def complete_resolution_evidence(
    company: str,
    metadata: dict | None,
    candidates: list[dict],
    evaluations: list[dict],
    resolution: entity_resolution.Resolution,
    evaluate_candidate_with_stage_fn: Callable = None,
    evaluation_rank_key_fn: Callable = None,
) -> tuple[list[dict], entity_resolution.Resolution, evidence_acquisition.EvidenceState]:
    """Run bounded, gap-specific search/crawl rounds for an unresolved entity."""
    current = evidence_acquisition.analyze(
        company,
        evaluations,
        resolution_status=resolution.status,
        metadata=metadata,
        query_limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
    )
    previous: evidence_acquisition.EvidenceState | None = None
    rounds: list[dict] = []
    attempted_scopes_by_domain: dict[str, set[str]] = {}
    attempted_queries: set[str] = set()
    for round_number in range(1, config.MAX_AUTONOMOUS_RESOLUTION_ROUNDS + 1):
        if not evidence_acquisition.should_continue(
            previous,
            current,
            round_number - 1,
            config.MAX_AUTONOMOUS_RESOLUTION_ROUNDS,
        ):
            break
        runtime.record("autonomy.rounds")
        targeted = search.find_targeted_candidates(
            company,
            metadata,
            current.search_queries,
            round_ordinal=round_number + 1,
            limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
            already_run=attempted_queries,
        )
        attempted_queries.update(current.search_queries)
        known_domains = {
            scorer.normalize_domain(item.get("url", "")) for item in candidates
        }
        for candidate in targeted:
            domain = scorer.normalize_domain(candidate.get("url", ""))
            if domain and domain not in known_domains:
                candidates.append(candidate)
                known_domains.add(domain)
        candidates[:] = search.rank_candidates(candidates)

        evaluation_by_domain = {
            scorer.normalize_domain(item.get("candidate", {}).get("url", "")): item
            for item in evaluations
        }
        candidate_by_domain = {
            scorer.normalize_domain(candidate.get("url", "")): candidate
            for candidate in candidates
            if candidate.get("role") not in identity.EXCLUDED_ROLES
        }
        priority_domains = [
            scorer.normalize_domain(
                item.get("candidate", {}).get("url", "")
            )
            for item in resolution.contenders
        ]
        priority_domains.extend(
            domain for domain in candidate_by_domain
            if domain not in evaluation_by_domain
        )
        current_scopes = set(current.crawl_scopes)
        selected_domains = list(dict.fromkeys(
            domain for domain in priority_domains
            if domain and (
                domain not in attempted_scopes_by_domain
                or (
                    current_scopes
                    and not current_scopes.issubset(
                        attempted_scopes_by_domain[domain]
                    )
                )
            )
        ))[:config.MAX_TARGETED_CRAWLS_PER_ROUND]
        if not selected_domains:
            break
        for domain in selected_domains:
            runtime.record("autonomy.targeted_crawls")
            attempted_scopes_by_domain.setdefault(domain, set()).update(
                current_scopes
            )
            candidate = candidate_by_domain.get(domain)
            if candidate is None:
                candidate = evaluation_by_domain[domain]["candidate"]
            if evaluate_candidate_with_stage_fn:
                evaluation_by_domain[domain] = evaluate_candidate_with_stage_fn(
                    company,
                    candidate,
                    metadata,
                    evidence_scopes=current.crawl_scopes,
                )
        evaluations = list(evaluation_by_domain.values())
        if evaluation_rank_key_fn:
            evaluations.sort(
                key=lambda item: evaluation_rank_key_fn(company, item),
                reverse=True,
            )
        previous = current
        resolution = entity_resolution.resolve_candidates(company, evaluations, metadata)
        current = evidence_acquisition.analyze(
            company,
            evaluations,
            resolution_status=resolution.status,
            metadata=metadata,
            query_limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
        )
        rounds.append({
            "round": round_number,
            "gaps_before": sorted(previous.gaps),
            "gaps_after": sorted(current.gaps),
            "crawl_scopes": list(previous.crawl_scopes),
            "queries": list(previous.search_queries),
            "evaluated_domains": selected_domains,
            "resolution_status": resolution.status,
        })
        if resolution.status == "resolved":
            break
    automation = {
        "rounds": rounds,
        "remaining_evidence_gaps": sorted(current.gaps),
        "terminal_reason": (
            "resolved_after_evidence_completion"
            if resolution.status == "resolved" and rounds
            else current.terminal_reason
        ),
    }
    for evaluation in evaluations:
        evaluation["_automation"] = automation
    return evaluations, resolution, current
