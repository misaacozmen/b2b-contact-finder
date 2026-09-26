"""Small immutable publication fixtures for tests that exercise export gates."""

from __future__ import annotations

import hashlib

from modules import publication_policy


def publishable_content_decision(
    *, evidence_id: str = "fixture:evidence",
    source_record_id: str = "fixture:default",
    website: str = "https://fixture.example",
    email: str = "info@fixture.example",
    phone: str = "+902120000000",
) -> dict:
    digest = hashlib.sha256(b"fixture first-party content").hexdigest()
    evidence_id = hashlib.sha256(str(evidence_id).encode("utf-8")).hexdigest()
    records = [{
        "evidence_id": evidence_id,
        "target_source_record_id": source_record_id,
        "evidence_source_record_id": "fixture:page",
        "observed_business": "Fixture Company",
        "url": website,
        "final_url": website,
        "content_sha256": digest,
        "retrieval_method": "http",
        "observation_type": "legal_name",
        "observation_value": "Fixture Company",
        "location": {"kind": "page_text", "selector": "body"},
        "relation": "first_party_identity",
        "identity_route": "LEGAL_NAME",
        "first_party": True,
        "distinctive_token_count": 2,
        "legal_name_match": True,
        "full_name_match": True,
        "contact_fields": ["email", "phone"],
        "observed_contacts": {"email": email, "phone": phone},
    }, {
        "evidence_id": hashlib.sha256(f"{evidence_id}:country".encode("utf-8")).hexdigest(),
        "target_source_record_id": source_record_id,
        "evidence_source_record_id": "fixture:country-page",
        "observed_business": "Fixture Company",
        "url": website,
        "final_url": website,
        "content_sha256": digest,
        "retrieval_method": "http",
        "observation_type": "country",
        "observation_value": "TR",
        "location": {"kind": "page_text", "selector": "body", "marker": "TR_country_marker"},
        "relation": "country",
        "identity_route": "COUNTRY",
        "first_party": True,
    }]
    return publication_policy.ContentDecision(
        verified=True, website_allowed=True, email_allowed=True, phone_allowed=True,
        identity_status="verified", identity_route="LEGAL_NAME", complete_contact=True,
        support_evidence_ids=(evidence_id,), target_source_record_id=source_record_id,
        evidence_fingerprint=publication_policy.evidence_fingerprint(
            records, target_source_record_id=source_record_id,
            website=website, email=email, phone=phone,
        ),
        bound_website=publication_policy.scorer.normalize_domain(website),
        bound_email=email.casefold(), bound_phone=phone.casefold(),
    ).as_dict() | {"evidence_records": records}
