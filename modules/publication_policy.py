"""Conservative, evidence-based policy for the publication surface.

The legacy pipeline already contains many hard safety gates.  This module does
not replace or relax them: it may only allow a legacy publication to stand or
downgrade it to review.  The numeric safety score is an auditable ordering
signal for offline risk/coverage analysis, not a probability.
"""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import config

from modules import identity, scorer


POLICY_VERSION = "evidence-risk-v4"
CONTENT_SCHEMA_VERSION = 4
SAFE_RETRIEVAL_METHODS = {
    "http", "http_tls_unverified", "browser_render", "sitemap",
    "official_link_reference", "serp_redirect",
}
OK_STATUSES = {"OK_HIGH_CONFIDENCE", "OK_MEDIUM_CONFIDENCE"}
CONTACT_PROJECTION_FIELDS = {
    "email": (
        "email", "email_source", "email_source_url",
        "alternative_emails", "alternative_email_sources",
    ),
    "phone": (
        "phone", "phone_source", "phone_source_url", "phone_label",
        "alternative_phones", "alternative_phone_sources",
    ),
}
CONTACT_PROJECTION_METADATA = {
    "email": (
        "email_verification", "email_verification_reason",
        "email_publication_status", "email_publication_reason",
    ),
    "phone": (
        "phone_publication_status", "phone_publication_reason",
    ),
}
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


@dataclass(frozen=True)
class ContentDecision:
    """Scheduler-independent decision about what evidence may be exposed."""

    verified: bool
    website_allowed: bool
    email_allowed: bool
    phone_allowed: bool
    reason_codes: tuple[str, ...] = ()
    support_evidence_ids: tuple[str, ...] = ()
    conflict_evidence_ids: tuple[str, ...] = ()
    identity_status: str = "unverified"
    identity_route: str = ""
    complete_contact: bool = False
    missing_evidence: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    policy_version: str = POLICY_VERSION
    target_source_record_id: str = ""
    evidence_fingerprint: str = ""
    schema_version: int = CONTENT_SCHEMA_VERSION
    bound_website: str = ""
    bound_email: str = ""
    bound_phone: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "website_allowed": self.website_allowed,
            "email_allowed": self.email_allowed,
            "phone_allowed": self.phone_allowed,
            "reason_codes": list(self.reason_codes),
            "support_evidence_ids": list(self.support_evidence_ids),
            "conflict_evidence_ids": list(self.conflict_evidence_ids),
            "identity_status": self.identity_status,
            "identity_route": self.identity_route,
            "complete_contact": self.complete_contact,
            "missing_evidence": list(self.missing_evidence),
            "next_actions": list(self.next_actions),
            "policy_version": self.policy_version,
            "target_source_record_id": self.target_source_record_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "schema_version": self.schema_version,
            "bound_website": self.bound_website,
            "bound_email": self.bound_email,
            "bound_phone": self.bound_phone,
        }


@dataclass(frozen=True)
class PublicationDecision:
    """Final export decision; scheduler state is intentionally an input only."""

    publishable: bool
    blockers: tuple[str, ...] = ()
    content_decision: ContentDecision | dict[str, Any] | None = None
    policy_version: str = POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        content = self.content_decision
        if isinstance(content, ContentDecision):
            content = content.as_dict()
        return {
            "publishable": self.publishable,
            "blockers": list(self.blockers),
            "content_decision": content,
            "policy_version": self.policy_version,
        }


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _evidence_ids(value: Any) -> tuple[str, ...]:
    result: list[str] = []
    for item in _sequence(value):
        if isinstance(item, dict):
            item = item.get("evidence_id") or item.get("id") or item.get("source_evidence_id")
        item = str(item or "").strip()
        if item and item not in result:
            result.append(item)
    return tuple(result)


def _typed_contact_allowed(evidence: dict, field: str) -> bool:
    decision = evidence.get(f"{field}_decision")
    if not isinstance(decision, dict):
        contacts = evidence.get("contact_decisions") or evidence.get("contacts") or {}
        decision = contacts.get(field) if isinstance(contacts, dict) else None
    if isinstance(decision, dict) and "allowed" in decision:
        return bool(decision.get("allowed"))
    status = str(evidence.get(f"{field}_publication_status", "")).casefold()
    return bool(evidence.get(field)) and status == "allowed"


def _binding_value(value: Any, field: str = "") -> str:
    value = str(value or "").strip()
    if field == "website":
        return scorer.normalize_domain(value)
    return value.casefold()


def evidence_fingerprint(
    records: Any, *, target_source_record_id: str = "", website: str = "",
    email: str = "", phone: str = "",
) -> str:
    canonical = []
    for item in _sequence(records):
        if isinstance(item, dict):
            canonical.append(item)
    canonical.sort(key=lambda item: str(item.get("evidence_id") or item.get("id") or ""))
    material = json.dumps({
        "records": canonical,
        "target_source_record_id": str(target_source_record_id or ""),
        "website": _binding_value(website, "website"),
        "email": _binding_value(email, "email"),
        "phone": _binding_value(phone, "phone"),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def evaluate_content(evidence: dict[str, Any]) -> ContentDecision:
    """Purely evaluate identity, website, and typed contact evidence.

    Scheduler fields, publication flags, and prior policy decisions are not
    consulted.  This makes the result reusable across FREE, PAID, and export
    stages without changing the content decision.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    assessment = evidence.get("identity_assessment") or evidence.get("identity") or {}
    candidate = evidence.get("candidate") or {}
    if not isinstance(assessment, dict):
        assessment = {}
    if not isinstance(candidate, dict):
        candidate = {}
    reasons = [str(value) for value in _sequence(evidence.get("reasons"))]
    reasons.extend(str(value) for value in _sequence(evidence.get("reason_codes")))
    conflicts = _sequence(evidence.get("identity_conflicts"))
    conflicts.extend(_sequence(assessment.get("conflicts")))
    conflict_ids = _evidence_ids(evidence.get("conflict_evidence_ids"))
    if not conflict_ids:
        conflict_ids = _evidence_ids(conflicts)
    reason_codes: list[str] = []
    for value in reasons:
        if value and value not in reason_codes:
            reason_codes.append(value)
    for item in conflicts:
        kind = item.get("kind") if isinstance(item, dict) else str(item)
        kind = str(kind or "identity_conflict")
        token = f"identity_conflict:{kind}"
        if token not in reason_codes:
            reason_codes.append(token)

    website = str(evidence.get("website") or candidate.get("url") or "").strip()
    company = str(evidence.get("company") or evidence.get("target_company") or "")
    target_source_record_id = str(
        evidence.get("source_record_id") or candidate.get("source_record_id") or ""
    ).strip()
    company_tokens = scorer.legal_identity_tokens(company)
    records = [item for item in _sequence(
        evidence.get("evidence_records") or evidence.get("supporting_evidence")
    ) if isinstance(item, dict)]
    record_by_id = {
        str(item.get("evidence_id") or item.get("id") or "").strip(): item
        for item in records
        if str(item.get("evidence_id") or item.get("id") or "").strip()
    }
    valid_ids = []
    for evidence_id, item in record_by_id.items():
        url = str(item.get("url") or item.get("source_url") or "").strip()
        retrieval = str(item.get("retrieval_method") or "").casefold()
        item_target = str(
            item.get("target_source_record_id")
            or item.get("supports_target_source_record_id")
            or ""
        ).strip()
        item_source = str(
            item.get("evidence_source_record_id")
            or item.get("source_record_id")
            or ""
        ).strip()
        location = item.get("location") or item.get("dom_location") or item.get("text_location")
        relation = str(item.get("relation") or "").casefold()
        structurally_bound = bool(
            target_source_record_id
            and item_target == target_source_record_id
            and item_source
            and str(item.get("observed_business") or "").strip()
            and str(item.get("observation_type") or "").strip()
            and str(item.get("observation_value") or "").strip()
            and location
            and relation in {"supports_target", "same_target", "target_anchor", "first_party_identity", "country"}
        )
        if (
            url.startswith(("http://", "https://"))
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("content_sha256") or item.get("content_hash") or "").casefold())
            and retrieval in SAFE_RETRIEVAL_METHODS
            and structurally_bound
        ):
            valid_ids.append(evidence_id)
    declared_support = _evidence_ids(
        evidence.get("support_evidence_ids") or evidence.get("evidence_ids")
    )
    support_ids = tuple(item for item in declared_support if item in valid_ids)
    missing: list[str] = []
    if declared_support and len(support_ids) != len(declared_support):
        missing.append("support_evidence_reference_invalid")
    requested_route = str(
        evidence.get("identity_route")
        or assessment.get("identity_route")
        or ""
    ).upper()
    website_domain = scorer.normalize_domain(website)
    route_groups = {
        name: [
            item for item in records
            if str(item.get("identity_route") or item.get("route") or "").upper() == name
            and str(item.get("evidence_id") or item.get("id") or "") in support_ids
        ]
        for name in ("LEGAL_NAME", "TARGET_ANCHOR", "BRAND_OWNER")
    }
    # A route's evidence family is checked independently.  In particular, an
    # independent anchor record must never erase a valid first-party route.
    route_items = []
    route_ok_by_name: dict[str, bool] = {}
    for route_name, candidates in route_groups.items():
        family_items = [
            item for item in candidates
            if route_name == "TARGET_ANCHOR"
            or scorer.same_registrable_domain(
                scorer.normalize_domain(str(item.get("final_url") or item.get("url") or "")),
                website_domain,
            )
        ]
        if route_name == "LEGAL_NAME":
            route_ok_by_name[route_name] = any(
            bool(item.get("first_party"))
            and int(item.get("distinctive_token_count", 0) or 0) >= 2
            and bool(item.get("legal_name_match") or item.get("full_name_match"))
                for item in family_items
            )
        elif route_name == "TARGET_ANCHOR":
            anchor_kinds = {
                "phone", "full_address", "address", "company_number",
                "company_registration_number",
            }

            def anchor_value(value: Any, kind: str) -> str:
                text = str(value or "").strip().casefold()
                if kind == "phone":
                    return re.sub(r"\D+", "", text)
                return re.sub(r"[^a-z0-9]+", "", text)

            def valid_anchor_family(anchor: dict) -> bool:
                kind = str(anchor.get("match_kind") or anchor.get("kind") or "").casefold()
                independent_id = str(anchor.get("independent_source_evidence_id") or "").strip()
                candidate_id = str(anchor.get("candidate_evidence_id") or "").strip()
                relation_id = str(anchor.get("relation_evidence_id") or "").strip()
                ids = (independent_id, candidate_id, relation_id)
                if (
                    kind not in anchor_kinds
                    or not bool(anchor.get("matched"))
                    or not bool(anchor.get("name_supported"))
                    or len(set(ids)) != 3
                    or not all(ids)
                    or any(value not in record_by_id or value not in valid_ids for value in ids)
                    or relation_id == independent_id
                    or relation_id == candidate_id
                ):
                    return False
                independent = record_by_id[independent_id]
                candidate_record = record_by_id[candidate_id]
                relation_record = record_by_id[relation_id]
                independent_domain = scorer.normalize_domain(
                    str(independent.get("final_url") or independent.get("url") or "")
                )
                candidate_domain = scorer.normalize_domain(
                    str(candidate_record.get("final_url") or candidate_record.get("url") or "")
                )
                relation_domain = scorer.normalize_domain(
                    str(relation_record.get("final_url") or relation_record.get("url") or "")
                )
                independent_value = anchor_value(
                    independent.get("observation_value"), kind,
                )
                candidate_value = anchor_value(
                    candidate_record.get("anchor_observation_value")
                    or candidate_record.get("observation_value"), kind,
                )
                candidate_observed_value = anchor_value(
                    candidate_record.get("observation_value"), kind,
                )
                relation_value = anchor_value(
                    relation_record.get("observation_value"), kind,
                )
                candidate_role = str(candidate_record.get("role") or "").casefold()
                independent_role = str(independent.get("role") or "").casefold()
                relation_role = str(relation_record.get("role") or "").casefold()
                if (
                    independent_role not in {"independent_observation", "anchor_independent"}
                    or candidate_role not in {
                        "candidate_first_party_observation", "first_party_candidate",
                    }
                    or relation_role not in {"anchor_relation", "target_anchor_relation"}
                ):
                    return False
                if (
                    independent.get("target_source_record_id") != target_source_record_id
                    or candidate_record.get("target_source_record_id") != target_source_record_id
                    or relation_record.get("target_source_record_id") != target_source_record_id
                    or str(independent.get("relation") or "").casefold() != "target_anchor"
                    or str(candidate_record.get("relation") or "").casefold() != "target_anchor"
                    or str(relation_record.get("relation") or "").casefold() != "target_anchor"
                    or str(independent.get("observation_type") or "").casefold() != kind
                    or str(candidate_record.get("anchor_observation_type") or candidate_record.get("observation_type") or "").casefold() != kind
                    or str(relation_record.get("observation_type") or "").casefold() != "target_anchor_relation"
                    or not independent_value
                    or independent_value != candidate_value
                    or candidate_observed_value != candidate_value
                    or independent_value != relation_value
                    or not candidate_record.get("first_party")
                    or not candidate_record.get("name_supported")
                    or not scorer.same_registrable_domain(candidate_domain, website_domain)
                    or not scorer.same_registrable_domain(relation_domain, candidate_domain)
                    or not independent_domain
                    or relation_record.get("independent_source_evidence_id") != independent_id
                    or relation_record.get("candidate_evidence_id") != candidate_id
                    or relation_record.get("relation_evidence_id") != relation_id
                    or relation_id == str(relation_record.get("independent_source_evidence_id") or "")
                    or relation_id == str(relation_record.get("candidate_evidence_id") or "")
                ):
                    return False
                return True

            route_ok_by_name[route_name] = any(
                valid_anchor_family(item) for item in family_items
            )
        else:
            route_ok_by_name[route_name] = any(
            bool(item.get("explicit_relationship") or item.get("brand_owner_relation"))
            and bool(item.get("first_party"))
                for item in family_items
            )
        if route_ok_by_name[route_name]:
            route_items.extend(family_items)
    priority = ("LEGAL_NAME", "TARGET_ANCHOR", "BRAND_OWNER")
    supported_routes = tuple(name for name in priority if route_ok_by_name.get(name))
    route = supported_routes[0] if supported_routes else requested_route
    route_ok = bool(supported_routes)
    if not route_ok:
        missing.append("ownership_route_evidence_missing")
    country_ids = _evidence_ids(evidence.get("country_evidence_ids"))
    country_records = [record_by_id[item] for item in country_ids if item in record_by_id]
    country_ok = bool(
        evidence.get("country_supported") is True
        and country_ids
        and len(country_records) == len(country_ids)
        and all(
            item.get("target_source_record_id") == target_source_record_id
            and item.get("observation_type") == "country"
            and str(item.get("observation_value") or "").strip().casefold() in {"tr", "turkey", "türkiye", "turkiye"}
            and str(item.get("relation") or "").casefold() == "country"
            and scorer.same_registrable_domain(
                scorer.normalize_domain(str(item.get("final_url") or item.get("url") or "")),
                website_domain,
            )
            and item.get("evidence_id") in valid_ids
            for item in country_records
        )
    )
    if not country_ok:
        missing.append("country_evidence_missing")
    if conflicts or assessment.get("conflicts"):
        missing.append("identity_conflict")
    website_allowed = bool(
        website and route in {"LEGAL_NAME", "TARGET_ANCHOR", "BRAND_OWNER"}
        and route_ok and country_ok and not conflicts and not assessment.get("conflicts")
    )
    if not website_allowed:
        reason_codes.append("website_identity_unverified")
    if conflicts or assessment.get("conflicts"):
        website_allowed = False
        reason_codes.append("identity_conflict")
    contact_records = [
        item for item in records
        if str(item.get("evidence_id") or item.get("id") or "") in valid_ids
        and (item.get("contact_fields") or isinstance(item.get("observed_contacts"), dict))
    ]
    def contact_bound(field: str, value: Any) -> bool:
        if not value or not contact_records:
            return False
        normalized = _binding_value(value, field)
        for item in contact_records:
            item_target = str(
                item.get("target_source_record_id")
                or item.get("supports_target_source_record_id")
                or ""
            )
            if item_target != target_source_record_id:
                continue
            item_domain = scorer.normalize_domain(str(item.get("final_url") or item.get("url") or ""))
            if not scorer.same_registrable_domain(item_domain, website_domain):
                if not bool(item.get("cross_domain_relation_verified")):
                    continue
                if str(item.get("relation") or "").casefold() not in {"target_anchor", "same_target", "supports_target"}:
                    continue
            if field not in set(item.get("contact_fields") or []):
                continue
            if str(item.get("relation") or "").casefold() not in {
                "first_party_identity", "supports_target", "same_target", "target_anchor"
            }:
                continue
            observed = item.get("observed_contacts") if isinstance(item.get("observed_contacts"), dict) else {}
            values = [item.get("observation_value"), observed.get(field)]
            if any(_binding_value(candidate, field) == normalized for candidate in values if candidate):
                return True
        return False
    email_allowed = bool(
        website_allowed and _typed_contact_allowed(evidence, "email")
        and contact_bound("email", evidence.get("email"))
    )
    phone_allowed = bool(
        website_allowed and _typed_contact_allowed(evidence, "phone")
        and contact_bound("phone", evidence.get("phone"))
    )
    next_actions: list[str] = []
    if not evidence.get("email"):
        missing.append("email_evidence_missing")
        next_actions.append("acquire_email")
    elif not email_allowed:
        reason_codes.append("email_not_allowed")
        missing.append("email_evidence_missing")
        next_actions.append("acquire_email")
    if not evidence.get("phone"):
        missing.append("phone_evidence_missing")
        next_actions.append("acquire_phone")
    elif not phone_allowed:
        reason_codes.append("phone_not_allowed")
        missing.append("phone_evidence_missing")
        next_actions.append("acquire_phone")
    if len(company_tokens) <= 1 and not route_ok:
        reason_codes.append("single_token_target_anchor_missing")
    return ContentDecision(
        verified=bool(website_allowed),
        website_allowed=bool(website_allowed),
        email_allowed=bool(email_allowed),
        phone_allowed=bool(phone_allowed),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        support_evidence_ids=support_ids,
        conflict_evidence_ids=conflict_ids,
        identity_status="verified" if website_allowed else "unverified",
        identity_route=route,
        complete_contact=bool(website_allowed and email_allowed and phone_allowed),
        missing_evidence=tuple(dict.fromkeys(missing)),
        next_actions=tuple(dict.fromkeys(next_actions)),
        target_source_record_id=target_source_record_id,
        evidence_fingerprint=evidence_fingerprint(
            records,
            target_source_record_id=target_source_record_id,
            website=website,
            email=evidence.get("email", ""),
            phone=evidence.get("phone", ""),
        ),
        schema_version=CONTENT_SCHEMA_VERSION,
        bound_website=_binding_value(website, "website"),
        bound_email=_binding_value(evidence.get("email", ""), "email"),
        bound_phone=_binding_value(evidence.get("phone", ""), "phone"),
    )


def finalize_publication(
    content_decision: ContentDecision | dict[str, Any],
    scheduler_state: dict[str, Any] | None,
    *,
    require_scheduler: bool = True,
) -> PublicationDecision:
    """Combine immutable content evidence with terminal scheduler receipts."""
    raw = content_decision.as_dict() if isinstance(content_decision, ContentDecision) else dict(content_decision or {})
    content = ContentDecision(
        verified=bool(raw.get("verified")),
        website_allowed=bool(raw.get("website_allowed")),
        email_allowed=bool(raw.get("email_allowed")),
        phone_allowed=bool(raw.get("phone_allowed")),
        reason_codes=tuple(str(v) for v in _sequence(raw.get("reason_codes"))),
        support_evidence_ids=_evidence_ids(raw.get("support_evidence_ids")),
        conflict_evidence_ids=_evidence_ids(raw.get("conflict_evidence_ids")),
        identity_status=str(raw.get("identity_status") or "unverified"),
        identity_route=str(raw.get("identity_route") or ""),
        complete_contact=bool(raw.get("complete_contact")),
        missing_evidence=tuple(str(v) for v in _sequence(raw.get("missing_evidence"))),
        next_actions=tuple(str(v) for v in _sequence(raw.get("next_actions"))),
        policy_version=str(raw.get("policy_version") or POLICY_VERSION),
        target_source_record_id=str(raw.get("target_source_record_id") or ""),
        evidence_fingerprint=str(raw.get("evidence_fingerprint") or ""),
        schema_version=int(raw.get("schema_version") or 0),
        bound_website=str(raw.get("bound_website") or ""),
        bound_email=str(raw.get("bound_email") or ""),
        bound_phone=str(raw.get("bound_phone") or ""),
    )
    state = scheduler_state if isinstance(scheduler_state, dict) else {}
    blockers: list[str] = []
    if raw.get("policy_version") != POLICY_VERSION:
        blockers.append("policy_version_mismatch")
    if int(raw.get("schema_version") or 0) != CONTENT_SCHEMA_VERSION:
        blockers.append("content_schema_version_missing_or_mismatch")
    if not content.target_source_record_id:
        blockers.append("target_source_record_id_missing")
    if not content.evidence_fingerprint:
        blockers.append("evidence_fingerprint_missing")
    if not content.support_evidence_ids:
        blockers.append("support_evidence_missing")
    if require_scheduler:
        if "free_state" not in state or "paid_required" not in state or "paid_state" not in state:
            blockers.append("scheduler_receipt_missing")
        free_state = str(state.get("free_state", "")).upper()
        paid_state = str(state.get("paid_state", "")).upper()
        free_recovered_by_paid = bool(state.get("paid_required")) and free_state == "FAILED" and paid_state == "DONE"
        if free_state not in {"DONE", "NOT_REQUIRED"} and not free_recovered_by_paid:
            blockers.append("free_scheduler_not_terminal")
        if bool(state.get("paid_required")) and str(state.get("paid_state", "")).upper() != "DONE":
            blockers.append("paid_scheduler_not_terminal")
        if not bool(state.get("paid_required")) and state.get("paid_state") and str(state.get("paid_state")).upper() not in {"DONE", "NOT_REQUIRED"}:
            blockers.append("paid_scheduler_not_terminal")
    if state.get("quarantine_state") or state.get("quarantine_status"):
        blockers.append("quarantined")
    if state.get("content_status") and str(state.get("content_status")).upper() not in OK_STATUSES:
        blockers.append("content_status_not_publishable")
    if not content.website_allowed:
        blockers.append("website_identity_unverified")
    field_gaps = {
        "email_evidence_missing", "phone_evidence_missing",
        "email_not_allowed", "phone_not_allowed",
    }
    global_missing = [item for item in content.missing_evidence if item not in field_gaps]
    if global_missing:
        blockers.extend(f"missing_evidence:{item}" for item in global_missing)
    if not (content.email_allowed or content.phone_allowed):
        blockers.append("no_allowed_contact_field")
    return PublicationDecision(
        publishable=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        content_decision=content,
    )


def _legacy_is_publishable_row(row: dict) -> bool:
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
        has_target_anchor = bool(
            scorer.compact_domain_core(str(row.get("website", ""))) == "".join(
                scorer.domain_identity_tokens(str(row.get("company", "")))
            )
            and _has_reason(str(row.get("reason", "")).split(";"), (
                *LEGAL_NAME_REASON_PREFIXES, "target_anchor_",
            ))
        )
        if not has_target_anchor:
            return False
    valid_email = bool(row.get("email")) and str(row.get("email_publication_status", "")).casefold() == "allowed"
    valid_phone = bool(row.get("phone")) and str(row.get("phone_publication_status", "")).casefold() == "allowed"
    return valid_email or valid_phone


def _reason_values(row: dict, evaluation: dict) -> set[str]:
    return (
        _normalized_reason_tokens(row.get("reason", ""))
        | _normalized_reason_tokens(row.get("publication_blockers", ""))
        | _normalized_reason_tokens(evaluation.get("reasons", []))
    )


def _context_resolution(row: dict, evaluation: dict) -> dict:
    value = row.get("context_resolution")
    if not isinstance(value, dict):
        value = evaluation.get("context_resolution")
    return value if isinstance(value, dict) else {}


def _context_resolution_is_verified(context: dict) -> bool:
    """Require ownership, activity compatibility, and one independent match."""
    ownership = any(bool(context.get(key)) for key in (
        "legal_ownership_verified", "brand_ownership_verified",
        "target_legal_name_verified", "brand_owner_verified",
        "legal_name_verified", "ownership_verified",
    ))
    compatibility = any(bool(context.get(key)) for key in (
        "candidate_sector_compatible", "candidate_product_compatible",
        "site_sector_product_compatible", "sector_compatible",
    ))
    matches = context.get("independent_matches") or context.get("independent_identity_matches") or []
    if isinstance(matches, dict):
        matches = [matches]
    match_kinds = {"phone", "full_address", "address", "company_number", "company_registration_number"}
    independent_match = False
    for match in matches:
        if not isinstance(match, dict):
            continue
        kind = str(match.get("kind") or match.get("type") or "").casefold().replace(" ", "_")
        url = str(match.get("url") or match.get("source_url") or "").strip()
        content_hash = str(match.get("content_sha256") or match.get("content_hash") or "").casefold()
        source_record_id = str(match.get("source_record_id") or "").strip()
        if kind in match_kinds and url and re.fullmatch(r"[0-9a-f]{64}", content_hash) and source_record_id:
            independent_match = True
            break
    return ownership and compatibility and independent_match and not context.get("conflicts")


def _website_identity_verified(row: dict, evaluation: dict) -> bool:
    assessment = row.get("identity_assessment") or evaluation.get("identity_assessment") or {}
    candidate = evaluation.get("candidate") or {}
    if assessment.get("conflicts") or assessment.get("publishable") is False:
        return False
    website = str(row.get("website") or candidate.get("url") or "").strip()
    if not website:
        return False
    if _context_resolution_is_verified(_context_resolution(row, evaluation)):
        return True
    reasons = _reason_values(row, evaluation)
    identity_evidence = any(token.startswith((
        "page_identity_strong:", "page_identity_medium:", "structured_identity_",
        "legal_name_", "country_identity_tr_", "context_match:",
    )) for token in reasons)
    resolution = str(row.get("identity_resolution") or evaluation.get("identity_resolution") or evaluation.get("_identity_resolution") or "")
    return bool(assessment.get("publishable") is True and (identity_evidence or resolution or candidate))


def _decide_raw_row(
    row: dict,
    evaluation: dict | None = None,
    *,
    require_scheduler: bool = True,
) -> dict:
    """Return the one explainable final publication decision for a row."""
    stored_content = row.get("content_decision") or row.get("__content_decision")
    if isinstance(stored_content, (ContentDecision, dict)):
        content = stored_content if isinstance(stored_content, ContentDecision) else ContentDecision(
            verified=bool(stored_content.get("verified")),
            website_allowed=bool(stored_content.get("website_allowed")),
            email_allowed=bool(stored_content.get("email_allowed")),
            phone_allowed=bool(stored_content.get("phone_allowed")),
            reason_codes=tuple(str(v) for v in _sequence(stored_content.get("reason_codes"))),
            support_evidence_ids=_evidence_ids(stored_content.get("support_evidence_ids")),
            conflict_evidence_ids=_evidence_ids(stored_content.get("conflict_evidence_ids")),
            identity_status=str(stored_content.get("identity_status") or "unverified"),
            identity_route=str(stored_content.get("identity_route") or ""),
            complete_contact=bool(stored_content.get("complete_contact")),
            missing_evidence=tuple(str(v) for v in _sequence(stored_content.get("missing_evidence"))),
            policy_version=str(stored_content.get("policy_version") or POLICY_VERSION),
            target_source_record_id=str(stored_content.get("target_source_record_id") or ""),
            evidence_fingerprint=str(stored_content.get("evidence_fingerprint") or ""),
            schema_version=int(stored_content.get("schema_version") or 0),
            bound_website=str(stored_content.get("bound_website") or ""),
            bound_email=str(stored_content.get("bound_email") or ""),
            bound_phone=str(stored_content.get("bound_phone") or ""),
        )
        scheduler_state = dict(row)
        scheduler_state.setdefault("content_status", row.get("status", ""))
        final = finalize_publication(
            content, scheduler_state, require_scheduler=require_scheduler,
        )
        validation_blockers: list[str] = []
        row_source_id = str(row.get("source_record_id") or "").strip()
        stored_records = row.get("content_evidence_records")
        if not isinstance(stored_records, list):
            stored_records = stored_content.get("evidence_records") if isinstance(stored_content, dict) else None
        if not content.target_source_record_id or not content.evidence_fingerprint or not content.support_evidence_ids or not isinstance(stored_records, list):
            validation_blockers.append("CONTENT_REEVALUATION_REQUIRED")
        if not row_source_id or content.target_source_record_id != row_source_id:
            validation_blockers.append("target_source_record_mismatch")
        expected_fingerprint = evidence_fingerprint(
            stored_records or [],
            target_source_record_id=row_source_id,
            website=row.get("website", ""),
            email=row.get("email", ""),
            phone=row.get("phone", ""),
        )
        if expected_fingerprint != content.evidence_fingerprint:
            validation_blockers.append("evidence_fingerprint_mismatch")
        if not row_source_id or not isinstance(stored_records, list):
            validation_blockers.append("content_evidence_records_missing")
        else:
            evidence_ids_seen: set[str] = set()
            for record in stored_records:
                if not isinstance(record, dict):
                    validation_blockers.append("content_evidence_record_invalid")
                    continue
                evidence_id = str(record.get("evidence_id") or record.get("id") or "").strip()
                record_target = str(
                    record.get("target_source_record_id")
                    or record.get("supports_target_source_record_id")
                    or ""
                ).strip()
                content_sha = str(
                    record.get("content_sha256") or record.get("content_hash") or ""
                ).casefold()
                if not evidence_id or evidence_id in evidence_ids_seen:
                    validation_blockers.append("content_evidence_id_invalid")
                evidence_ids_seen.add(evidence_id)
                if record_target != row_source_id:
                    validation_blockers.append("content_evidence_target_mismatch")
                if not re.fullmatch(r"[0-9a-f]{64}", content_sha):
                    validation_blockers.append("content_evidence_sha256_invalid")
        if content.bound_website != _binding_value(row.get("website", ""), "website"):
            validation_blockers.append("bound_website_mismatch")
        if content.bound_email != _binding_value(row.get("email", ""), "email"):
            validation_blockers.append("bound_email_mismatch")
        if content.bound_phone != _binding_value(row.get("phone", ""), "phone"):
            validation_blockers.append("bound_phone_mismatch")
        if validation_blockers:
            final = PublicationDecision(
                publishable=False,
                blockers=tuple(dict.fromkeys((*final.blockers, *validation_blockers))),
                content_decision=content,
            )
        return {
            **final.as_dict(),
            "website_identity_verified": content.website_allowed,
            "allowed_contact_fields": [
                field for field, allowed in (("email", content.email_allowed), ("phone", content.phone_allowed))
                if allowed
            ],
            "advisory_eligible": bool(row.get("publication_advisory_eligible", row.get("publication_eligible", False))),
        }
    # A persisted row without the immutable content decision is not an
    # alternate publication path.  Scheduler receipts alone cannot authorize
    # content, and legacy rows stay withheld until re-evaluated.
    return {
        "publishable": False,
        "blockers": ["content_decision_missing"],
        "website_identity_verified": False,
        "allowed_contact_fields": [],
        "policy_version": POLICY_VERSION,
        "advisory_eligible": bool(row.get("publication_advisory_eligible", row.get("advisory_eligible", False))),
    }


PROJECTION_SCHEMA_VERSION = 2


def _canonical_hash(value: Any) -> str:
    material = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _field_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _projection_hash(row: dict) -> str:
    return _canonical_hash({
        "source_record_id": str(row.get("source_record_id", "")),
        "website": str(row.get("website", "")),
        "email": str(row.get("email", "")),
        "phone": str(row.get("phone", "")),
        "publication_eligible": bool(row.get("publication_eligible")),
        "allowed_contact_fields": str(row.get("allowed_contact_fields", "")),
    })


def _decision_hash(decision: dict) -> str:
    return _canonical_hash({
        "publishable": bool(decision.get("publishable")),
        "blockers": list(decision.get("blockers", [])),
        "allowed_contact_fields": list(decision.get("allowed_contact_fields", [])),
        "website_identity_verified": bool(decision.get("website_identity_verified")),
    })


def create_projection_receipt(raw_row: dict, projected_row: dict, decision: dict) -> dict:
    """Create an export receipt whose authority is the raw source snapshot."""
    snapshot = json.loads(json.dumps(raw_row, ensure_ascii=False, default=str))
    snapshot.pop("publication_projection_receipt", None)
    snapshot.pop("publication_projection_status", None)
    content = snapshot.get("content_decision") or {}
    records = snapshot.get("content_evidence_records")
    if not isinstance(records, list) and isinstance(content, dict):
        records = content.get("evidence_records")
    if not isinstance(records, list):
        records = []
    snapshot["content_evidence_records"] = records
    raw_fields = {
        field: str(snapshot.get(field, "") or "")
        for field in ("website", "email", "phone")
    }
    receipt = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "raw_snapshot": snapshot,
        "raw_snapshot_sha256": _canonical_hash(snapshot),
        "source_record_id": str(snapshot.get("source_record_id", "")),
        "raw_fields": raw_fields,
        "content_decision": content,
        "content_evidence_records": records,
        "evidence_fingerprint": str(content.get("evidence_fingerprint", "")) if isinstance(content, dict) else "",
        "allowed_contact_fields": list(decision.get("allowed_contact_fields", [])),
        "raw_publishable": bool(decision.get("publishable")),
        "raw_blockers": list(decision.get("blockers", [])),
        "raw_website_identity_verified": bool(decision.get("website_identity_verified")),
        "raw_decision_sha256": _decision_hash(decision),
        "original_field_sha256": {field: _field_hash(value) for field, value in raw_fields.items()},
    }
    receipt["projection_sha256"] = _projection_hash(projected_row)
    return receipt


def validate_projection(row: dict) -> bool:
    """Validate a projected row against its raw snapshot and central policy.

    Every failure is intentionally converted to ``False`` because receipts are
    untrusted persisted data and must never become an exception path.
    """
    try:
        receipt = row.get("publication_projection_receipt")
        if not isinstance(row, dict) or not isinstance(receipt, dict):
            return False
        if int(receipt.get("schema_version", 0)) != PROJECTION_SCHEMA_VERSION:
            return False
        snapshot = receipt.get("raw_snapshot")
        if not isinstance(snapshot, dict):
            return False
        if snapshot.get("publication_projection_receipt") or snapshot.get("publication_projection_status"):
            return False
        if _canonical_hash(snapshot) != str(receipt.get("raw_snapshot_sha256", "")):
            return False
        source_id = str(snapshot.get("source_record_id", "")).strip()
        if not source_id or source_id != str(row.get("source_record_id", "")).strip():
            return False
        for field in ("website", "email", "phone"):
            if str(receipt.get("raw_fields", {}).get(field, "")) != str(snapshot.get(field, "") or ""):
                return False
        content = snapshot.get("content_decision")
        records = snapshot.get("content_evidence_records")
        projected_content = row.get("content_decision") or row.get("__content_decision")
        projected_records = row.get("content_evidence_records")
        if not isinstance(content, dict) or not isinstance(records, list):
            return False
        if not isinstance(projected_content, dict) or not isinstance(projected_records, list):
            return False
        if _canonical_hash(content) != _canonical_hash(projected_content):
            return False
        if _canonical_hash(records) != _canonical_hash(projected_records):
            return False
        if content.get("target_source_record_id") != source_id:
            return False
        expected_fingerprint = evidence_fingerprint(
            records,
            target_source_record_id=source_id,
            website=snapshot.get("website", ""),
            email=snapshot.get("email", ""),
            phone=snapshot.get("phone", ""),
        )
        if str(content.get("evidence_fingerprint", "")) != expected_fingerprint:
            return False
        bound_values = {
            "website": _binding_value(snapshot.get("website", ""), "website"),
            "email": _binding_value(snapshot.get("email", ""), "email"),
            "phone": _binding_value(snapshot.get("phone", ""), "phone"),
        }
        if any(str(content.get(f"bound_{field}", "")) != value for field, value in bound_values.items()):
            return False
        decision = _decide_raw_row(snapshot)
        if _decision_hash(decision) != str(receipt.get("raw_decision_sha256", "")):
            return False
        if bool(decision.get("publishable")) != bool(receipt.get("raw_publishable")):
            return False
        if list(decision.get("blockers", [])) != list(receipt.get("raw_blockers", [])):
            return False
        allowed = list(decision.get("allowed_contact_fields", []))
        if allowed != list(receipt.get("allowed_contact_fields", [])):
            return False
        if bool(decision.get("publishable")) is not True:
            return False
        if not all(_field_hash(snapshot.get(field, "")) == str(receipt.get("original_field_sha256", {}).get(field, "")) for field in ("website", "email", "phone")):
            return False
        for field in ("email", "phone"):
            if field in allowed:
                for name in CONTACT_PROJECTION_FIELDS[field] + CONTACT_PROJECTION_METADATA[field]:
                    if str(row.get(name, "") or "") != str(snapshot.get(name, "") or ""):
                        return False
            else:
                if any(str(row.get(name, "") or "") for name in CONTACT_PROJECTION_FIELDS[field]):
                    return False
                expected_reason = f"{field}_not_published"
                expected_metadata = {
                    f"{field}_publication_status": "suppressed",
                    f"{field}_publication_reason": expected_reason,
                }
                if field == "email":
                    expected_metadata.update({
                        "email_verification": "not_checked",
                        "email_verification_reason": expected_reason,
                    })
                for name, value in expected_metadata.items():
                    if str(row.get(name, "") or "") != value:
                        return False
        if bool(row.get("publication_eligible")) is not True:
            return False
        return str(receipt.get("projection_sha256", "")) == _projection_hash(row)
    except Exception:
        return False


def decide_row(row: dict, evaluation: dict | None = None) -> dict:
    """Return the single central decision for raw or projected rows."""
    if isinstance(row, dict) and isinstance(row.get("publication_projection_receipt"), dict):
        if not validate_projection(row):
            return {
                "publishable": False,
                "blockers": ["publication_projection_invalid"],
                "website_identity_verified": False,
                "allowed_contact_fields": [],
                "policy_version": POLICY_VERSION,
                "advisory_eligible": False,
            }
        snapshot = row["publication_projection_receipt"].get("raw_snapshot")
        return _decide_raw_row(snapshot if isinstance(snapshot, dict) else {})
    return _decide_raw_row(row, evaluation)


def decide_supplied_website_no_call(row: dict, *, source_record_id: str) -> dict:
    """Validate content for the pre-paid supplied-site no-call decision.

    Scheduler completion is intentionally not fabricated here: the caller is
    deciding whether to dispatch, before paid scheduler receipts can exist.
    The normal persisted publication gate still requires those receipts.
    """
    candidate = dict(row) if isinstance(row, dict) else {}
    source_id = str(source_record_id or "").strip()
    if not source_id:
        return {
            "publishable": False,
            "blockers": ["source_record_id_missing"],
            "allowed_contact_fields": [],
        }
    candidate["source_record_id"] = source_id
    if str(candidate.get("status", "")) not in OK_STATUSES:
        return {
            "publishable": False,
            "blockers": ["content_status_not_publishable"],
            "allowed_contact_fields": [],
        }
    if candidate.get("publication_eligible") is not True:
        return {
            "publishable": False,
            "blockers": ["publication_not_advisory_eligible"],
            "allowed_contact_fields": [],
        }
    return _decide_raw_row(candidate, require_scheduler=False)
    evaluation = evaluation if isinstance(evaluation, dict) else (
        row.get("__evaluation") if isinstance(row.get("__evaluation"), dict) else {}
    )
    persisted = all(key in row for key in ("source_record_id", "free_state", "paid_state", "paid_required"))
    strict_persisted = persisted and any(key in row for key in ("score", "email_publication_status", "phone_publication_status"))
    advisory = bool(row.get("publication_advisory_eligible", row.get("advisory_eligible", row.get("publication_eligible") is True)))
    allowed_contact_fields = []
    if row.get("email") and str(row.get("email_publication_status", "")).casefold() == "allowed":
        allowed_contact_fields.append("email")
    if row.get("phone") and str(row.get("phone_publication_status", "")).casefold() == "allowed":
        allowed_contact_fields.append("phone")
    blockers: list[str] = []
    if row.get("quarantine_state") or row.get("quarantine_status") or "legacy_recovery_provisional" in str(row.get("publication_blockers", "")):
        blockers.append("quarantined")
    if strict_persisted:
        if not row.get("source_record_id"):
            blockers.append("source_record_id_missing")
        if str(row.get("free_state", "")).upper() in {"PENDING", "RUNNING", "UNKNOWN", "BLOCKED_BUDGET"}:
            blockers.append("free_scheduler_not_terminal")
        if bool(row.get("paid_required")) and str(row.get("paid_state", "")).upper() != "DONE":
            blockers.append("paid_scheduler_not_terminal")
        if str(row.get("paid_state", "")).upper() not in {"DONE", "NOT_REQUIRED"}:
            blockers.append("paid_scheduler_not_terminal")
        if str(row.get("status", "")) not in OK_STATUSES:
            blockers.append("status_not_publishable")
        if int(row.get("score") or 0) < int(getattr(config, "MIN_ACCEPT_SCORE", 65)):
            blockers.append("score_below_minimum")
        if not _website_identity_verified(row, evaluation):
            blockers.append("website_identity_unverified")
        assessment = row.get("identity_assessment") or evaluation.get("identity_assessment") or {}
        if assessment.get("conflicts"):
            blockers.append("identity_conflict")
        reasons = _reason_values(row, evaluation)
        context = _context_resolution(row, evaluation)
        override_present = any(token == CONTEXT_CONFLICT_OVERRIDE for token in reasons)
        real_conflicts = {
            token for token in reasons
            if any(marker in token for marker in CONFLICT_TOKENS)
            and token != CONTEXT_CONFLICT_OVERRIDE
        }
        if real_conflicts:
            blockers.append("context_conflict")
        if override_present and not _context_resolution_is_verified(context):
            blockers.append("context_resolution_unverified")
        if "cross_domain_email_accepted_from_verified_official_page" in reasons and not evaluation.get("structured_domain_relation"):
            blockers.append("cross_domain_email_unresolved")
        if not allowed_contact_fields:
            blockers.append("no_allowed_contact_field")
        if not _legacy_is_publishable_row(row):
            blockers.append("publication_gate_failed")
        publishable = not blockers
    else:
        publishable = _legacy_is_publishable_row(row)
        if not publishable and advisory:
            blockers.append("publication_gate_failed")
    if not publishable and not blockers:
        blockers.append("publication_gate_failed")
    return {
        "publishable": bool(publishable),
        "blockers": list(dict.fromkeys(blockers)),
        "website_identity_verified": _website_identity_verified(row, evaluation),
        "allowed_contact_fields": allowed_contact_fields,
        "policy_version": POLICY_VERSION,
        "advisory_eligible": advisory,
    }


def is_publishable_row(row: dict) -> bool:
    """Boolean compatibility wrapper around :func:`decide_row`."""
    return bool(decide_row(dict(row) if isinstance(row, dict) else {})["publishable"])


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
    legal_or_ownership_evidence = _has_reason(reasons, ("legal_name_phrase_match:", "legal_name_full_match:", "legal_name_ownership_match:", "target_anchor_")) or bool(evaluation.get("structured_domain_relation"))
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

    if CONTEXT_CONFLICT_OVERRIDE in _normalized_reason_tokens(reasons) and not _context_resolution_is_verified(
        _context_resolution({}, evaluation)
    ):
        blockers.append("context_resolution_unverified")

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

    policy_row = {
        "company": company,
        "website": candidate.get("url", ""),
        "status": proposed_status,
        "publication_eligible": eligible,
        "score": safety_score,
        "identity_assessment": assessment,
        "email": evaluation.get("email", ""),
        "phone": evaluation.get("phone", ""),
        "email_publication_status": evaluation.get("email_publication_status", "suppressed"),
        "phone_publication_status": evaluation.get("phone_publication_status", "suppressed"),
        "__evaluation": evaluation,
    }
    return {
        "policy_version": POLICY_VERSION,
        "mode": "downgrade_only",
        "proposed_status": proposed_status,
        "action": action,
        "eligible": eligible,
        "publishable": bool(eligible),
        "blockers": list(dict.fromkeys(blockers)) if not eligible else [],
        "website_identity_verified": bool(eligible),
        "allowed_contact_fields": [
            field for field in ("email", "phone")
            if evaluation.get(field)
            and str(evaluation.get(f"{field}_publication_status", "")).casefold() == "allowed"
        ],
        "advisory_eligible": eligible,
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
