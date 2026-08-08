"""Auditable post-crawl ranking based on identity evidence bundles."""

from __future__ import annotations

from modules import identity, scorer


EXCLUDED_ROLES = {
    "directory", "fair_profile", "news", "marketplace", "shared_listing",
    "public_body",
}


def _strength(reasons: list[str], prefix: str) -> int:
    levels = {"strong": 3, "medium": 2, "weak": 1, "match": 2}
    for reason in reasons:
        if not str(reason).startswith(prefix):
            continue
        return max((value for label, value in levels.items() if label in reason), default=0)
    return 0


def evidence_vector(company: str, item: dict, *, hard_context_failure: bool) -> dict:
    candidate = item.get("candidate", {})
    reasons = item.get("reasons", [])
    assessment = item.get("identity_assessment") or identity.assess(
        company,
        candidate,
        reasons,
        item.get("structured_identity", {}),
    )
    candidate_reason = str(candidate.get("reason", ""))
    intrinsic_domain = bool(
        (
            "domain_hits:" in candidate_reason
            and "search_text_identity:" not in candidate_reason
            and "explicit_cross_domain_redirect:" not in candidate_reason
        )
        or scorer.domain_identity_match(company, candidate.get("url", ""))[0]
    )
    explicit_relationship = max(
        4 if any(str(reason).startswith("legal_name_ownership_match:") for reason in reasons) else 0,
        4 if any(
            str(reason).startswith("structured_identity_strong:")
            and "scope=declared_relationship" in str(reason)
            for reason in reasons
        ) else 0,
        3 if any(str(reason).startswith("legal_name_full_match:") for reason in reasons) else 0,
        2 if any(str(reason).startswith("legal_name_phrase_match:") for reason in reasons) else 0,
    )
    claims = item.get("structured_identity", {}).get("claims", [])
    page_scope_diversity = len({
        str(claim.get("page_scope", ""))
        for claim in claims
        if claim.get("page_scope")
    })
    return {
        "eligible_role": int(
            candidate.get("role") not in EXCLUDED_ROLES
            and not scorer.is_excluded_domain(candidate.get("url", ""))
        ),
        "owner_consistent": int(not any(
            str(reason).startswith("structured_identity_unmatched:")
            for reason in reasons
        ) or any(
            str(reason).startswith("legal_name_ownership_match:")
            for reason in reasons
        )),
        "provisionally_publishable": int(bool(assessment.get("provisionally_publishable"))),
        "hard_context_clear": int(not hard_context_failure),
        "authoritative_registry": int(
            candidate.get("query") in {"verified_entity", "verified_alias"}
        ),
        "direct_candidate": int(
            "discovery_only_not_identity_authority" not in candidate_reason
        ),
        "explicit_relationship": explicit_relationship,
        "conflict_free": int(not assessment.get("conflicts")),
        "independent_support_count": int(assessment.get("support_count", 0) or 0),
        "strong_first_party_bundle": int(bool(assessment.get("strong_first_party_bundle"))),
        "first_party_bundle_components": int(
            assessment.get("first_party_bundle_components", 0) or 0
        ),
        "page_scope_diversity": min(page_scope_diversity, 4),
        "context_strength": _strength(reasons, "context_"),
        "company_candidate_role": int(candidate.get("role") == "company_candidate"),
        "page_identity_strength": _strength(reasons, "page_identity_"),
        "structured_identity_strength": _strength(reasons, "structured_identity_"),
        "legal_full_strength": _strength(reasons, "legal_name_full_"),
        "legal_phrase_strength": _strength(reasons, "legal_name_phrase_"),
        "intrinsic_domain_evidence": int(intrinsic_domain),
        "official_query_evidence": int(candidate.get("_official_query_evidence", 0) or 0),
        "metadata_context_matches": int(candidate.get("_metadata_context_matches", 0) or 0),
        "has_contact": int(bool(item.get("has_contact"))),
        "email_gate_clear": int(not item.get("email_failed")),
        "final_score": int(item.get("final_score", 0) or 0),
    }


_ORDER = (
    "eligible_role",
    "owner_consistent",
    "provisionally_publishable",
    "hard_context_clear",
    "authoritative_registry",
    "explicit_relationship",
    "direct_candidate",
    "conflict_free",
    "independent_support_count",
    "strong_first_party_bundle",
    "first_party_bundle_components",
    "page_scope_diversity",
    "context_strength",
    "company_candidate_role",
    "page_identity_strength",
    "structured_identity_strength",
    "legal_full_strength",
    "legal_phrase_strength",
    "intrinsic_domain_evidence",
    "official_query_evidence",
    "metadata_context_matches",
    "has_contact",
    "email_gate_clear",
    "final_score",
)


def vector_key(vector: dict, *, include_score: bool = True) -> tuple[int, ...]:
    return tuple(
        int(vector.get(field, 0) or 0)
        for field in _ORDER
        if include_score or field != "final_score"
    )


def rank_key(company: str, item: dict, *, hard_context_failure: bool) -> tuple[int, ...]:
    vector = evidence_vector(
        company, item, hard_context_failure=hard_context_failure,
    )
    return vector_key(vector)


def non_score_key(company: str, item: dict, *, hard_context_failure: bool) -> tuple[int, ...]:
    vector = evidence_vector(
        company, item, hard_context_failure=hard_context_failure,
    )
    return vector_key(vector, include_score=False)
