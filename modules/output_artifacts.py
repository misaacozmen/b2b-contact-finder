from __future__ import annotations

from typing import Callable

import config
from modules import (
    contact_publication,
    discovery_coverage,
    entity_memory,
    entity_registry,
    evidence,
    excel,
    publication_policy,
    quality_audit,
    redaction,
    replay_snapshot,
    report,
    runtime,
    scorer,
)


def attach_candidates(row: dict, candidates: list[dict]) -> dict:
    row["selected_website"] = row.get("selected_website") or row.get("website", "")
    selected_domain = scorer.normalize_domain(row.get("selected_website", ""))
    status = str(row.get("status", ""))
    candidate_evaluations = []
    for candidate in candidates:
        history = candidate.setdefault("_stage_history", [])
        if not any(item.get("stage") == "discovered" for item in history):
            history.insert(0, {
                "stage": "discovered",
                "source": candidate.get("query", ""),
                "score": candidate.get("score", 0),
            })
        candidate_domain = scorer.normalize_domain(candidate.get("url", ""))
        if selected_domain and candidate_domain == selected_domain:
            final_stage = "published" if publication_policy.is_publishable_row(row) else "selected_for_review"
            if not any(item.get("stage") == final_stage for item in history):
                history.append({"stage": final_stage, "status": status})
        elif not any(item.get("stage") in {"rejected", "not_evaluated"} for item in history):
            evaluated = any(
                item.get("stage") in {"identity_evaluated", "full_evaluated"}
                for item in history
            )
            history.append({
                "stage": "rejected" if evaluated else "not_evaluated",
                "reason": "lower_identity_rank_or_failed_gate" if evaluated else "candidate_limit_or_lower_rank",
            })
        candidate_evaluations.append({
            "domain": candidate.get("domain") or candidate_domain,
            "url": candidate.get("url", ""),
            "source": candidate.get("query", ""),
            "stages": history,
        })
    row["__candidates"] = candidates
    row["__candidate_evaluations"] = candidate_evaluations
    row["__search_trace"] = getattr(candidates, "trace", [])
    row["__source_health"] = getattr(candidates, "source_health", {})
    for idx, candidate in enumerate(candidates[:3], start=1):
        row[f"candidate_{idx}_url"] = candidate.get("url", "")
        row[f"candidate_{idx}_score"] = candidate.get("score", "")
        row[f"candidate_{idx}_reason"] = candidate.get("reason", "")
        row[f"candidate_{idx}_query"] = candidate.get("query", "")
        row[f"candidate_{idx}_role"] = candidate.get("role", "")
    return row


def contact_output_fields(evaluation: dict) -> dict:
    alternative_phones = evaluation.get("alternative_phones", [])
    return {
        "email": evaluation.get("email", ""),
        "email_source": evaluation.get("email_source", ""),
        "email_source_url": evaluation.get("email_source_url", ""),
        "alternative_emails": "; ".join(evaluation.get("alternative_emails", [])),
        "alternative_email_sources": "; ".join(
            f"{item.get('value', '')} | {item.get('source_url', '')}"
            for item in evaluation.get("alternative_email_records", [])
        ),
        "email_verification": evaluation.get("email_verification", "not_checked"),
        "email_verification_reason": evaluation.get("email_verification_reason", ""),
        "email_publication_status": evaluation.get(
            "email_publication_status", "suppressed",
        ),
        "email_publication_reason": evaluation.get(
            "email_publication_reason", "",
        ),
        "phone": evaluation.get("phone", ""),
        "phone_source": evaluation.get("phone_source", ""),
        "phone_source_url": evaluation.get("phone_source_url", ""),
        "phone_label": evaluation.get("phone_label", ""),
        "alternative_phones": "; ".join(
            f"{item.get('value', '')} [{item.get('label', 'general')}]"
            for item in alternative_phones
        ),
        "alternative_phone_sources": "; ".join(
            f"{item.get('value', '')} | {item.get('source_url', '')}"
            for item in alternative_phones
        ),
        "phone_publication_status": evaluation.get(
            "phone_publication_status", "suppressed",
        ),
        "phone_publication_reason": evaluation.get(
            "phone_publication_reason", "",
        ),
        "contact_policy_version": evaluation.get(
            "contact_publication", {},
        ).get("policy_version", contact_publication.POLICY_VERSION),
    }


def evaluation_evidence(
    evaluation: dict,
    *,
    contact_output_fields_fn: Callable[[dict], dict] = contact_output_fields,
) -> dict:
    crawl_result = evaluation.get("crawl_result", {})
    return {
        "candidate": evaluation.get("candidate", {}),
        "final_score": evaluation.get("final_score", 0),
        "reasons": evaluation.get("reasons", []),
        "structured_identity": evaluation.get("structured_identity", {}),
        "semantic_identity": evaluation.get("semantic_identity", {}),
        "identity_assessment": evaluation.get("identity_assessment", {}),
        "publication_policy": evaluation.get("publication_policy", {}),
        "rerank_evidence": evaluation.get("rerank_evidence", {}),
        "linkedin_company_evidence": evaluation.get(
            "linkedin_company_evidence", {}
        ),
        "llm_arbiter_evidence": evaluation.get("llm_arbiter_evidence", {}),
        "llm_arbiter_decisions": evaluation.get("_llm_arbiter_decisions", []),
        "contact_publication": evaluation.get("contact_publication", {}),
        "identity_resolution": evaluation.get("_identity_resolution", ""),
        "automation": evaluation.get("_automation", {}),
        "crawl": {
            "url": crawl_result.get("url", ""),
            "cache_status": crawl_result.get("cache_status", ""),
            "error": crawl_result.get("error", ""),
            "pages": [page.get("url", "") for page in crawl_result.get("pages", [])],
            "recovery_trace": crawl_result.get("recovery_trace", []),
        },
        "contacts": contact_output_fields_fn(evaluation),
    }


def policy_output_fields(evaluation: dict) -> dict:
    decision = evaluation.get("publication_policy", {})
    return {
        "publication_policy_version": decision.get(
            "policy_version", publication_policy.POLICY_VERSION,
        ),
        "publication_policy_action": decision.get("action", ""),
        "publication_eligible": bool(decision.get("eligible", False)),
        "publication_safety_score": int(decision.get("safety_score", 0) or 0),
        "publication_risk_index": int(decision.get("risk_index", 100) or 0),
        "publication_risk_tier": decision.get("risk_tier", "blocked"),
        "publication_blockers": "; ".join(decision.get("hard_blockers", [])),
    }


def clear_unpublished_contacts(row: dict) -> None:
    row["website"] = ""
    row["website_source"] = ""
    row["email"] = ""
    row["email_source"] = ""
    row["email_source_url"] = ""
    row["alternative_emails"] = ""
    row["alternative_email_sources"] = ""
    row["email_verification"] = "not_checked"
    row["email_verification_reason"] = "website_not_found"
    row["email_publication_status"] = "suppressed"
    row["email_publication_reason"] = "website_not_published"
    row["phone"] = ""
    row["phone_source"] = ""
    row["phone_source_url"] = ""
    row["phone_label"] = ""
    row["alternative_phones"] = ""
    row["alternative_phone_sources"] = ""
    row["phone_publication_status"] = "suppressed"
    row["phone_publication_reason"] = "website_not_published"


def apply_publication_policy(
    company: str,
    evaluation: dict,
    status: str,
    confidence: str,
    reasons: list[str],
) -> tuple[str, str]:
    decision = publication_policy.evaluate(
        company,
        evaluation,
        status,
        minimum_safety_score=config.PUBLICATION_POLICY_MIN_SAFETY_SCORE,
    )
    mode = str(config.PUBLICATION_POLICY_MODE or "enforce_downgrade_only").strip().casefold()
    hard_security_blockers = {"tls_certificate_unverified"}
    force_enforcement = bool(
        hard_security_blockers.intersection(decision.get("hard_blockers", []))
    )
    if mode == "shadow" and not force_enforcement:
        decision["mode"] = "shadow"
        if decision.get("action") == "downgrade_to_review":
            decision["action"] = "would_downgrade_to_review"
    else:
        status, confidence = publication_policy.enforce(
            decision, status, confidence, reasons,
        )
    evaluation["publication_policy"] = decision
    return status, confidence


def confidence_status(score: int, has_contact: bool, reasons: list[str], identity_verified: bool = True) -> tuple[str, str]:
    if not identity_verified:
        reasons.append("website_identity_not_independently_verified")
        return "REVIEW_NEEDED", "review"
    if score >= config.HIGH_CONFIDENCE_SCORE and has_contact:
        return "OK_HIGH_CONFIDENCE", "high"
    if score >= config.MEDIUM_CONFIDENCE_SCORE and has_contact:
        return "OK_MEDIUM_CONFIDENCE", "medium"
    if score >= config.REVIEW_SCORE:
        reasons.append("needs_manual_review")
        return "REVIEW_NEEDED", "review"
    # A successfully crawled, independently verified official website must not
    # disappear merely because a sector/context penalty pulled its numeric score
    # below the review threshold.  Keep it quarantined for a human decision.
    reasons.append("trusted_website_below_score_preserved_for_review")
    return "REVIEW_NEEDED", "review"


def write_outputs(rows: list[dict], elapsed_seconds: float) -> str:
    for row in rows:
        row["website_status"] = (
            "verified" if row.get("website") and publication_policy.is_publishable_row(row)
            else "review" if row.get("website") or row.get("status") == "WEBSITE_AMBIGUOUS"
            else "not_found"
        )
        row["contact_status"] = (
            "complete" if row.get("email") and row.get("phone")
            else "partial" if row.get("email") or row.get("phone")
            else "missing"
        )
        if publication_policy.is_publishable_row(row):
            discovery_coverage.mark_published(row.get("company", ""))
    evidence.write_jsonl(config.EVIDENCE_FILE, rows)
    entity_registry.write_observations(config.ENTITY_RELATIONSHIPS_FILE, rows)
    if (
        config.SEARCH_CACHE_MODE != "replay"
        and config.CRAWL_CACHE_MODE != "replay"
    ):
        entity_memory.remember([row for row in rows if publication_policy.is_publishable_row(row)])
    quality_audit.write(config.QUALITY_AUDIT_FILE, rows)
    for row in rows:
        row.pop("__index", None)
        row.pop("__candidates", None)
        row.pop("__evaluation", None)
        row.pop("__candidate_evaluations", None)
        row.pop("__search_trace", None)
        row.pop("__source_health", None)
        row.pop("__paid_escalation_complete", None)
    published_rows = [
        row for row in rows
        if publication_policy.is_publishable_row(row)
    ]
    # contacts.xlsx is the publication surface. Review/abstain rows remain in
    # the dedicated audit artifacts and must never look like published firms.
    sanitized_published_rows = redaction.sanitize(published_rows)
    review_rows = [row for row in rows if not publication_policy.is_publishable_row(row)]
    sanitized_review_rows = redaction.sanitize(review_rows)
    sanitized_all_rows = redaction.sanitize(rows)
    excel.write_contacts(config.CONTACTS_FILE, sanitized_published_rows)
    excel.write_contacts(
        config.VERIFIED_CONTACTS_FILE,
        sanitized_published_rows,
    )
    excel.write_contacts(
        config.REVIEW_QUEUE_FILE,
        sanitized_review_rows,
    )
    excel.write_failed(config.FAILED_FILE, redaction.sanitize(report.failed_rows(rows)))
    excel.write_website_candidates(config.CANDIDATES_FILE, sanitized_all_rows)
    report_text = redaction.redact_text(report.build_report(rows, elapsed_seconds))
    config.REPORT_FILE.write_text(report_text, encoding="utf-8")
    discovery_coverage.write(
        config.DISCOVERY_COVERAGE_FILE,
        config.DISCOVERY_ACQUISITION_QUERIES_PER_COMPANY,
    )
    replay_snapshot.write(config.REPLAY_SNAPSHOT_FILE)
    runtime.write(config.TELEMETRY_FILE)
    return report_text


_attach_candidates = attach_candidates
_contact_output_fields = contact_output_fields
_evaluation_evidence = evaluation_evidence
_policy_output_fields = policy_output_fields
_clear_unpublished_contacts = clear_unpublished_contacts
_apply_publication_policy = apply_publication_policy
_confidence_status = confidence_status
_write_outputs = write_outputs
