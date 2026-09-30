"""Second-chance search evidence for near-miss Stage A candidates (Talimat 18)."""

from __future__ import annotations

from typing import Iterable

from modules import calibration, discovery_rules, scorer


# Fixed before measurement; do not tune these templates on truth-set results.
CONFIRMATION_QUERY_TEMPLATES = ('"{brand}" iletişim', "{brand} firma web sitesi")


def confirmation_queries(company: str) -> list[str]:
    """Firm-name queries used once to re-check near-miss candidates."""
    brand = " ".join(scorer.primary_brand_tokens(company, limit=2)).strip()
    if len(brand.replace(" ", "")) < 3:
        return []
    return [template.format(brand=brand) for template in CONFIRMATION_QUERY_TEMPLATES]


def near_miss(features: dict, rule: dict) -> bool:
    """The candidate passes every rule condition except search rank and query hits."""
    if not isinstance(features, dict) or not isinstance(rule, dict):
        return False
    if calibration.rule_accepts(features, 0, rule):
        return False
    return calibration.rule_accepts(features, 0, dict(rule, R=99, H=0))


def domain_rank(results: Iterable[dict], domain: str) -> int | None:
    """1-based rank of the domain in normalized search results, or None."""
    if not domain:
        return None
    normalized, _stats = discovery_rules.normalize_serp_results(
        [item for item in (results or ()) if isinstance(item, dict)]
    )
    for item in normalized:
        url = str(item.get("resolved_url") or "")
        if item.get("resolution_status") == "resolved" and url and scorer.same_registrable_domain(url, domain):
            return int(item["rank"])
    return None


def apply_confirmation(features: dict, ranks: Iterable[int | None]) -> dict:
    """Add one query hit per confirmation query that returned the candidate domain."""
    hits = [int(rank) for rank in ranks if rank is not None]
    updated = dict(features)
    updated["confirmation_hits"] = len(hits)
    if hits:
        updated["query_hits"] = int(features.get("query_hits") or 0) + len(hits)
        updated["rank_best"] = min([int(features.get("rank_best") or 99), *hits])
    return updated
