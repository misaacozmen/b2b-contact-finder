"""Independent publication gates for first-party email and phone fields."""

from __future__ import annotations

from urllib.parse import urlparse

from modules import phone, runtime, scorer


POLICY_VERSION = "first-party-contact-v1"
DNS_ACCEPTED = {"verified", "valid"}
DNS_REJECTED = {"invalid_domain"}
SAFE_RETRIEVAL_METHODS = {
    "http",
    "http_tls_unverified",
    "browser_render",
    "pdf_text",
    "pdf_ocr",
    "official_link_reference",
    # Historical crawl entries predate per-page provenance. They remain
    # usable only because replay itself is offline and integrity audited.
    "unknown",
}


def _source_decision(website: str, record: dict) -> tuple[bool, str]:
    source_url = str(record.get("source_url", "") or "")
    if not source_url:
        return False, "missing_first_party_source_url"
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return False, "non_http_first_party_source"
    same_domain = scorer.same_registrable_domain(source_url, website)
    if not same_domain and not record.get("official_family_verified"):
        return False, "source_outside_verified_official_family"
    retrieval_method = str(record.get("retrieval_method", "unknown") or "unknown")
    if retrieval_method not in SAFE_RETRIEVAL_METHODS:
        return False, f"unsupported_retrieval_method:{retrieval_method}"
    return True, (
        "verified_official_family_source"
        if not same_domain
        else "verified_first_party_source"
    )


def evaluate_email(website: str, record: dict) -> dict:
    value = str(record.get("value", "") or "").strip().casefold()
    source_ok, source_reason = _source_decision(website, record)
    blockers: list[str] = []
    if not value or "@" not in value:
        blockers.append("invalid_email_syntax")
    if not source_ok:
        blockers.append(source_reason)

    email_domain = scorer.normalize_domain(value.rsplit("@", 1)[1] if "@" in value else "")
    website_domain = scorer.normalize_domain(website)
    same_mail_domain = bool(
        email_domain and scorer.same_registrable_domain(email_domain, website_domain)
    )
    dns_status = str(record.get("verification_status", "not_checked") or "not_checked")
    if dns_status in DNS_REJECTED:
        blockers.append("email_domain_invalid")
    if not same_mail_domain and dns_status not in DNS_ACCEPTED:
        blockers.append("cross_domain_email_dns_unverified")

    eligible = not blockers
    runtime.record(f"contact_policy.email.{'allowed' if eligible else 'suppressed'}")
    return {
        "policy_version": POLICY_VERSION,
        "field": "email",
        "value": value,
        "eligible": eligible,
        "reason": source_reason if eligible and same_mail_domain else (
            "verified_cross_domain_email_from_first_party_source"
            if eligible else ";".join(dict.fromkeys(blockers))
        ),
        "source_url": record.get("source_url", ""),
        "source_domain": scorer.normalize_domain(record.get("source_url", "")),
        "value_domain": email_domain,
        "dns_status": dns_status,
        "dns_reason": record.get("verification_reason", ""),
    }


def evaluate_phone(website: str, record: dict) -> dict:
    value = phone.normalize_phone(str(record.get("value", "") or ""))
    source_ok, source_reason = _source_decision(website, record)
    blockers: list[str] = []
    if not value:
        blockers.append("invalid_phone")
    if str(record.get("label", "")).casefold() == "fax":
        blockers.append("fax_not_company_phone")
    if not source_ok:
        blockers.append(source_reason)
    eligible = not blockers
    runtime.record(f"contact_policy.phone.{'allowed' if eligible else 'suppressed'}")
    return {
        "policy_version": POLICY_VERSION,
        "field": "phone",
        "value": value,
        "eligible": eligible,
        "reason": source_reason if eligible else ";".join(dict.fromkeys(blockers)),
        "source_url": record.get("source_url", ""),
        "source_domain": scorer.normalize_domain(record.get("source_url", "")),
        "label": record.get("label", "general"),
    }


def filter_records(
    website: str,
    email_records: list[dict],
    phone_records: list[dict],
) -> dict:
    """Return only field values independently safe for publication."""
    email_decisions = [
        {**evaluate_email(website, record), "record": record}
        for record in email_records
    ]
    phone_decisions = [
        {**evaluate_phone(website, record), "record": record}
        for record in phone_records
    ]
    return {
        "policy_version": POLICY_VERSION,
        "emails": email_decisions,
        "phones": phone_decisions,
        "eligible_email_records": [
            item["record"] for item in email_decisions if item["eligible"]
        ],
        "eligible_phone_records": [
            item["record"] for item in phone_decisions if item["eligible"]
        ],
        "suppressed_email_count": sum(not item["eligible"] for item in email_decisions),
        "suppressed_phone_count": sum(not item["eligible"] for item in phone_decisions),
    }
