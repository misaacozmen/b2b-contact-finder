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


def connected_domain_components(evaluations: list[dict]) -> dict[str, str]:
    """Collapse domains only when a first-party page declares the relation."""
    domains = {
        scorer.normalize_domain(item.get("candidate", {}).get("url", ""))
        for item in evaluations
    }
    domains.discard("")
    parent = {domain: domain for domain in domains}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    values = list(evaluations)
    for index, first in enumerate(values):
        first_domain = scorer.normalize_domain(
            first.get("candidate", {}).get("url", "")
        )
        if not first_domain:
            continue
        for second in values[index + 1:]:
            second_domain = scorer.normalize_domain(
                second.get("candidate", {}).get("url", "")
            )
            if not second_domain or first_domain == second_domain:
                continue
            if scorer.same_registrable_domain(first_domain, second_domain):
                union(first_domain, second_domain)
                continue
            edges = typed_domain_edges(
                first_domain,
                second_domain,
                first.get("structured_identity", {}),
                second.get("structured_identity", {}),
            )
            if edges:
                union(first_domain, second_domain)
    return {domain: find(domain) for domain in domains}


def same_official_family(
    first_domain: str,
    second_domain: str,
    components: dict[str, str],
) -> bool:
    first = scorer.normalize_domain(first_domain)
    second = scorer.normalize_domain(second_domain)
    return bool(
        first and second and (
            scorer.same_registrable_domain(first, second)
            or (
                first in components
                and second in components
                and components[first] == components[second]
            )
        )
    )
