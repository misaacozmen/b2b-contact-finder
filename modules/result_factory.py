"""Canonical factory for initial, empty, or failed candidate rows."""

from __future__ import annotations

from modules import contact_publication, publication_policy


def empty_result(company: str, status: str, reason: str = "", score: int = 0) -> dict:
    return {
        "company": company,
        "website": "",
        "website_source": "",
        "email": "",
        "email_source": "",
        "email_source_url": "",
        "alternative_emails": "",
        "alternative_email_sources": "",
        "email_verification": "not_checked",
        "email_verification_reason": "no_email",
        "email_publication_status": "suppressed",
        "email_publication_reason": reason or status,
        "phone": "",
        "phone_source": "",
        "phone_source_url": "",
        "phone_label": "",
        "alternative_phones": "",
        "alternative_phone_sources": "",
        "phone_publication_status": "suppressed",
        "phone_publication_reason": reason or status,
        "contact_policy_version": contact_publication.POLICY_VERSION,
        "status": status,
        "confidence": "none",
        "score": score,
        "publication_policy_version": publication_policy.POLICY_VERSION,
        "publication_policy_action": "retain_legacy_abstention",
        "publication_eligible": False,
        "publication_safety_score": 0,
        "publication_risk_index": 100,
        "publication_risk_tier": "blocked",
        "publication_blockers": reason or status,
        "reason": reason,
    }
