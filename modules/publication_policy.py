"""Conservative, evidence-based policy for the publication surface.

The legacy pipeline already contains many hard safety gates.  This module does
not replace or relax them: it may only allow a legacy publication to stand or
downgrade it to review.  The numeric safety score is an auditable ordering
signal for offline risk/coverage analysis, not a probability.
"""

from __future__ import annotations

from typing import Any

import config

from modules import identity, scorer


POLICY_VERSION = "evidence-risk-v1"
OK_STATUSES = {"OK_HIGH_CONFIDENCE", "OK_MEDIUM_CONFIDENCE"}
EXCLUDED_ROLES = {
    "directory", "fair_profile", "shared_listing", "marketplace", "news",
    "public_body",
}
LEGAL_NAME_REASON_PREFIXES = (
    "legal_name_phrase_match:",
    "legal_name_full_match:",
    "legal_name_ownership_match:",
)
CONTEXT_CONFLICT_OVERRIDE = "metadata_context_conflict_overridden_by_exact_compound_identity"
CONFLICT_TOKENS = {
    "sector_conflict",
    "context_conflict",
    "country_conflict",
    "country_mismatch",
    "foreign_country",
}


def is_publishable_row(row: dict) -> bool:
    persisted_scheduler_row = all(
        key in row for key in ("free_state", "paid_state")
    )
    if (
        row.get("quarantine_state")
        or row.get("quarantine_status")
        or "legacy_recovery_provisional" in str(row.get("publication_blockers", ""))
        or str(row.get("source_record_id_quality", "")).casefold() == "legacy_recovery"
    ):
        return False
    if (
        not row.get("source_record_id")
        or not row.get("free_state")
        or not row.get("paid_state")
        or "paid_required" not in row
    ):
        # Legacy/unit-level policy calls may omit scheduler metadata; only
        # require it when the field is present in a persisted run payload.
        if persisted_scheduler_row:
            return False
    if str(row.get("free_state", "")).upper() in {"PENDING", "RUNNING", "UNKNOWN", "BLOCKED_BUDGET"}:
        return False
    if bool(row.get("paid_required")) and str(row.get("paid_state", "")).upper() != "DONE":
        return False
    if persisted_scheduler_row and str(row.get("paid_state", "")).upper() not in {"DONE", "NOT_REQUIRED"}:
        return False
    eligible = (
        row.get("status") in OK_STATUSES
        and row.get("publication_eligible") is True
    )
    if not eligible or not persisted_scheduler_row:
        return eligible
    # Older direct memory callers do not carry the persisted scoring/contact
    # fields.  The strict publication contract applies once those fields are
    # present in a scheduler payload.
    if not any(key in row for key in ("score", "email_publication_status", "phone_publication_status")):
        return eligible
    if int(row.get("score") or 0) < int(getattr(config, "MIN_ACCEPT_SCORE", 65)):
        return False
    evaluation = dict(row.get("__evaluation") or {})
    if row.get("identity_resolution") and "_identity_resolution" not in evaluation:
        evaluation["_identity_resolution"] = row.get("identity_resolution")
    assessment = row.get("identity_assessment") or evaluation.get("identity_assessment") or {}
    if not bool(assessment.get("publishable")) or assessment.get("conflicts"):
        return False
    reasons = " ".join(str(value) for value in (
        row.get("reason", ""), row.get("publication_blockers", ""),
        evaluation.get("reasons", []) if isinstance(evaluation, dict) else "",
    )).casefold()
    reason_tokens = _normalized_reason_tokens(row.get("reason", ""))
    blocker_tokens = _normalized_reason_tokens(row.get("publication_blockers", ""))
    evaluation_tokens = _normalized_reason_tokens(
        evaluation.get("reasons", []) if isinstance(evaluation, dict) else "",
    )
    conflict_tokens = (reason_tokens | blocker_tokens | evaluation_tokens) - {CONTEXT_CONFLICT_OVERRIDE}
    if any(marker in token for token in conflict_tokens for marker in CONFLICT_TOKENS):
        return False
    if "cross_domain_email_accepted_from_verified_official_page" in reasons and not evaluation.get("structured_domain_relation"):
        return False
    if len(scorer.legal_identity_tokens(str(row.get("company", "")))) <= 1:
        if not (
            scorer.normalize_domain(str(row.get("website", "")))
            and scorer.domain_identity_match(str(row.get("company", "")), str(row.get("website", "")))[0]
            and _has_reason(str(row.get("reason", "")).split(";"), LEGAL_NAME_REASON_PREFIXES)
            and "country_identity_tr_" in reasons
            and "context_match:" in reasons
        ):
            return False
    valid_email = bool(row.get("email")) and str(row.get("email_publication_status", "")).casefold() == "allowed"
    valid_phone = bool(row.get("phone")) and str(row.get("phone_publication_status", "")).casefold() == "allowed"
    return valid_email or valid_phone


def _has_reason(reasons: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(str(reason).strip().startswith(prefixes) for reason in reasons)


def _normalized_reason_tokens(value: Any) -> set[str]:
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    tokens: set[str] = set()
    for value_item in values:
        tokens.update(
            token.strip().casefold()
            for token in str(value_item).split(";")
            if token.strip()
        )
    return tokens


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
    exact_domain_resolution = str(
        evaluation.get("_identity_resolution", "") or ""
    ).endswith("_exact_full_name_domain")
    fingerprint_resolution = str(
        evaluation.get("_identity_resolution", "") or ""
    ).startswith("candidate_resolved_by_")
    legal_or_ownership_evidence = _has_reason(reasons, ("legal_name_phrase_match:", "legal_name_full_match:", "legal_name_ownership_match:")) or bool(evaluation.get("structured_domain_relation"))
    if len(scorer.legal_identity_tokens(company)) <= 1 and not (
        scorer.normalize_domain(candidate.get("url", ""))
        and scorer.domain_identity_match(company, candidate.get("url", ""))[0]
        and legal_or_ownership_evidence
        and _has_reason(reasons, ("country_identity_tr_",))
        and _has_reason(reasons, ("context_match:", "page_identity_strong:"))
    ):
        blockers.append("generic_single_token_identity_not_verified")
    if not assessment.get("publishable"):
        # A fingerprint/fast-path resolution is useful evidence, but never a
        # publication authorization by itself.
        blockers.append("identity_resolution_not_publishable" if fingerprint_resolution else "identity_not_publishable")
    blockers.extend(
        f"identity_conflict:{item.get('kind', 'unknown')}"
        for item in conflicts
    )
    if not evaluation.get("has_contact"):
        blockers.append("no_first_party_contact")
    cross_domain_email_resolved = (
        "cross_domain_email_accepted_from_verified_official_page" in reasons
        and bool(evaluation.get("structured_domain_relation"))
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
    if "tls_insecure_transport" in reasons:
        blockers.append("tls_certificate_unverified")

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
    risk_eligible = not blockers and safety_score >= minimum_safety_score
    # ``eligible`` is the actual publication decision, not an advisory risk
    # score. A legacy review row remains withheld until its identity status is
    # resolved, even when its standalone safety score is high.
    eligible = legacy_publishable and risk_eligible
    if not legacy_publishable:
        action = "retain_legacy_abstention"
    elif risk_eligible:
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
        "risk_eligible": risk_eligible,
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
            "exact_full_name_domain_resolution": exact_domain_resolution,
            "has_contact": bool(evaluation.get("has_contact")),
            "risk_eligible": risk_eligible,
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
