"""Typed first-party edges between legal entities, brands and local sites."""

from __future__ import annotations

from modules import scorer


DIRECT_RELATIONSHIPS = {
    "brand", "parentOrganization", "subOrganization", "branchOf",
    "department", "memberOf", "distributor", "productDivision",
}


def typed_domain_edges(first_domain: str, second_domain: str, first: dict, second: dict) -> list[str]:
    """Return only explicit relationship URLs published by either site."""
    edges: list[str] = []
    for source, target_domain in ((first, second_domain), (second, first_domain)):
        for claim in source.get("relationships", []):
            if not isinstance(claim, dict) or claim.get("kind") not in DIRECT_RELATIONSHIPS:
                continue
            claim_domain = scorer.normalize_domain(claim.get("url", ""))
            if claim_domain and claim_domain == target_domain:
                edges.append(f"first_party_{claim['kind']}")
    return list(dict.fromkeys(edges))


def observation_payload(structured: dict) -> list[dict]:
    return [
        claim for claim in structured.get("relationships", [])
        if isinstance(claim, dict) and claim.get("kind") in DIRECT_RELATIONSHIPS
    ]
