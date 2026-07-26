"""Conservative, evidence-based policy for the publication surface.

The legacy pipeline already contains many hard safety gates.  This module does
not replace or relax them: it may only allow a legacy publication to stand or
downgrade it to review.  The numeric safety score is an auditable ordering
signal for offline risk/coverage analysis, not a probability.
"""

from __future__ import annotations

from modules import identity, scorer


POLICY_VERSION = "evidence-risk-v1"
OK_STATUSES = {"OK_HIGH_CONFIDENCE", "OK_MEDIUM_CONFIDENCE"}
EXCLUDED_ROLES = {"directory", "fair_profile", "shared_listing", "marketplace", "news"}


def _has_reason(reasons: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(str(reason).startswith(prefixes) for reason in reasons)


def _bounded_score(value: int) -> int:
    return max(0, min(100, int(value)))


def evaluate(
    company: str,
    evaluation: dict,
    proposed_status: str,
    *,
    minimum_safety_score: int,
) -> dict:
    """Return a downgrade-only publication decision.

    ``safety_score`` deliberately remains distinct from a calibrated
    probability.  Only a disjoint labelled set may turn this ordering into a
    deployable threshold.
    """
    minimum_safety_score = _bounded_score(minimum_safety_score)
    candidate = evaluation.get("candidate", {})
    reasons = list(evaluation.get("reasons", []))
    assessment = evaluation.get("identity_assessment") or identity.assess(
        company,
        candidate,
        reasons,
        evaluation.get("structured_identity", {}),
    )
    conflicts = assessment.get("conflicts", [])
    blockers: list[str] = []

    role = str(candidate.get("role", ""))
    if role in EXCLUDED_ROLES:
        blockers.append(f"excluded_candidate_role:{role}")
    if not assessment.get("provisionally_publishable"):
        blockers.append("identity_not_publishable")
    blockers.extend(
        f"identity_conflict:{item.get('kind', 'unknown')}"
        for item in conflicts
    )
    if not evaluation.get("has_contact"):
        blockers.append("no_first_party_contact")
    cross_domain_email_resolved = (
        "cross_domain_email_accepted_from_verified_official_page" in reasons
    )
    if evaluation.get("email_failed") and not cross_domain_email_resolved:
        blockers.append("email_gate_failed")
    if _has_reason(reasons, (
        "foreign_country_redirect_rejected",
        "unsafe_context_identity",
        "context_gate_failed",
        "unsupported_search_text_candidate_rejected",
    )):
        blockers.append("identity_or_context_safety_gate")

    support_count = int(assessment.get("support_count", 0) or 0)
    bundle_components = int(assessment.get("first_party_bundle_components", 0) or 0)
    score = 25
    score += min(support_count, 3) * 18
    score += min(bundle_components, 4) * 8
    if assessment.get("strong_first_party_bundle"):
        score += 18
    if assessment.get("publishable"):
        score += 10
    if _has_reason(reasons, ("country_identity_tr_",)):
        score += 7
    if evaluation.get("has_contact"):
        score += 5
    if evaluation.get("email") and evaluation.get("email_verification") == "verified":
        score += 3
    if evaluation.get("phone"):
        score += 2
    if scorer.domain_identity_match(company, candidate.get("url", ""))[0]:
        score += 5
    score -= min(len(conflicts), 2) * 35
    if evaluation.get("email_failed") and not cross_domain_email_resolved:
        score -= 20
    if role in EXCLUDED_ROLES:
        score -= 40
    safety_score = _bounded_score(score)

    legacy_publishable = proposed_status in OK_STATUSES
    eligible = not blockers and safety_score >= minimum_safety_score
    if not legacy_publishable:
        action = "retain_legacy_abstention"
    elif eligible:
        action = "allow_legacy_publication"
    else:
        action = "downgrade_to_review"

    if blockers:
        risk_tier = "blocked"
    elif safety_score >= 90:
        risk_tier = "low"
    elif safety_score >= minimum_safety_score:
        risk_tier = "controlled"
    else:
        risk_tier = "elevated"

    return {
        "policy_version": POLICY_VERSION,
        "mode": "downgrade_only",
        "proposed_status": proposed_status,
        "action": action,
        "eligible": eligible,
        "safety_score": safety_score,
        "risk_index": 100 - safety_score,
        "risk_tier": risk_tier,
        "minimum_safety_score": minimum_safety_score,
        "hard_blockers": list(dict.fromkeys(blockers)),
        "evidence_summary": {
            "identity_decision": assessment.get("decision", ""),
            "independent_support_count": support_count,
            "first_party_bundle_components": bundle_components,
            "strong_first_party_bundle": bool(assessment.get("strong_first_party_bundle")),
            "has_contact": bool(evaluation.get("has_contact")),
        },
    }


def enforce(decision: dict, status: str, confidence: str, reasons: list[str]) -> tuple[str, str]:
    """Apply only a downgrade; never promote a legacy review/abstention."""
    if status not in OK_STATUSES or decision.get("action") != "downgrade_to_review":
        return status, confidence
    reason = (
        f"publication_policy_downgrade:{decision.get('policy_version')}:"
        f"safety={decision.get('safety_score', 0)}"
    )
    blockers = decision.get("hard_blockers", [])
    if blockers:
        reason += f":blockers={','.join(blockers)}"
    if reason not in reasons:
        reasons.append(reason)
    return "REVIEW_NEEDED", "review"
