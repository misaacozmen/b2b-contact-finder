"""General, auditable company-to-website identity evidence.

Search snippets and contacts found on a candidate website are useful discovery
signals, but neither is independent proof that the website belongs to the
requested company.  This module keeps identity support, conflicts and neutral
corroboration separate so publication decisions do not depend on a single
opaque score.
"""

from __future__ import annotations

from modules import scorer


EXCLUDED_ROLES = {"directory", "fair_profile", "shared_listing", "marketplace", "news"}


def _has_reason(reasons: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(reason.startswith(prefixes) for reason in reasons)


def _reason_match_count(reasons: list[str], prefixes: tuple[str, ...]) -> int:
    for reason in reasons:
        if not reason.startswith(prefixes):
            continue
        try:
            return int(reason.rsplit(":", 1)[1].split("/", 1)[0])
        except (TypeError, ValueError):
            return 0
    return 0


def _signal(
    kind: str,
    polarity: str,
    source: str,
    independence_key: str,
    detail: str,
    strength: str = "medium",
) -> dict:
    return {
        "kind": kind,
        "polarity": polarity,
        "source": source,
        "independence_key": independence_key,
        "strength": strength,
        "detail": detail,
    }


def _company_types(values: list[str]) -> set[str]:
    types: set[str] = set()
    for value in values:
        normalized = f" {scorer.normalize_text(value)} "
        if any(marker in normalized for marker in (" limited ", " ltd ", " limited sirket ")):
            types.add("limited")
        if any(marker in normalized for marker in (" anonim ", " a s ", " joint stock ")):
            types.add("joint_stock")
        if any(marker in normalized for marker in (" corporation ", " inc ", " incorporated ")):
            types.add("corporation")
    return types


def _legal_identifier_groups(values: list[str]) -> dict[str, set[str]]:
    strong_kinds = {
        "mersis", "vkn", "taxid", "vatid", "leicode", "iso6523code",
        "globallocationnumber", "trade_registry",
    }
    groups: dict[str, set[str]] = {}
    for raw in values:
        kind, separator, value = str(raw).partition(":")
        normalized_kind = scorer.normalize_text(kind).replace(" ", "")
        normalized_value = scorer.normalize_text(value) if separator else ""
        if normalized_kind in strong_kinds and normalized_value:
            groups.setdefault(normalized_kind, set()).add(normalized_value)
    return groups


def assess(company: str, candidate: dict, reasons: list[str], structured_identity: dict | None = None) -> dict:
    """Return support/conflict/neutral evidence and a conservative publish gate."""
    company = company or candidate.get("_identity_company", "")
    structured_identity = structured_identity or {}
    signals: list[dict] = []

    role = candidate.get("role", "")
    if role in EXCLUDED_ROLES:
        signals.append(_signal("excluded_role", "conflict", "search", "role", role, "strong"))

    query = candidate.get("query", "")
    if query in {"verified_entity", "verified_alias"}:
        signals.append(_signal(
            "verified_relationship", "support", "human_registry", "authority",
            candidate.get("_entity_evidence_url", "") or query, "strong",
        ))
    elif query == "input_website":
        signals.append(_signal("supplied_website", "support", "input", "authority", query, "strong"))
    elif candidate.get("_source_profile_evidence"):
        signals.append(_signal(
            "exhibitor_profile_link", "neutral", "fair_profile", "discovery_bridge",
            candidate.get("_profile_url", "") or query, "medium",
        ))
    elif candidate.get("_first_party_alias_evidence"):
        signals.append(_signal(
            "first_party_alias_link", "neutral", "candidate_site", "discovery_bridge",
            candidate.get("_alias_source_url", "") or query, "medium",
        ))
    if candidate.get("_search_bridge_evidence"):
        signals.append(_signal(
            "search_profile_outbound_link", "neutral", "directory_profile", "discovery_bridge",
            candidate.get("_search_bridge_evidence", [{}])[0].get("source_url", ""), "medium",
        ))

    page_identity_strong = _has_reason(reasons, ("page_identity_strong:",))
    page_identity_medium = _has_reason(reasons, ("page_identity_medium:",))
    page_identity = page_identity_medium or page_identity_strong

    reason_text = candidate.get("reason", "")
    intrinsic_reason = (
        "domain_hits:" in reason_text
        and "search_text_identity:" not in reason_text
        and "explicit_cross_domain_redirect:" not in reason_text
    )
    domain_match = bool(company and scorer.domain_identity_match(company, candidate.get("url", ""))[0])
    public_brand_domain = bool(
        company and scorer.public_brand_domain_match(company, candidate.get("url", ""))
    )
    public_brand_tokens = scorer.primary_brand_tokens(company, limit=2) if company else []
    public_brand_compound = bool(
        len(public_brand_tokens) >= 2
        and scorer.compact_domain_core(candidate.get("url", "")) == "".join(public_brand_tokens)
    )
    # A long primary brand alone can still be a surname/homonym. When the
    # domain contains only that primary token, require the site's own matching
    # domain email plus repeated/medium page identity before treating the
    # domain as an independent ownership signal. Exact two-word compounds do
    # not need this extra contact corroboration.
    corroborated_public_brand_domain = public_brand_domain and (
        public_brand_compound
        or (page_identity and "email_domain_match" in reasons)
    )
    if candidate.get("_exact_brand_domain") or intrinsic_reason or domain_match or corroborated_public_brand_domain:
        signals.append(_signal(
            "intrinsic_domain_identity", "support", "domain", "domain_identity",
            scorer.normalize_domain(candidate.get("url", "")), "medium",
        ))
    elif "search_text_identity:" in reason_text or candidate.get("_legal_name_evidence"):
        signals.append(_signal(
            "search_text_identity", "neutral", "search", "search_discovery",
            query or reason_text, "weak",
        ))

    structured_match = _has_reason(reasons, ("structured_identity_medium:", "structured_identity_strong:"))
    structured_bundle_match = _has_reason(reasons, ("structured_identity_strong:",)) or any(
        reason.startswith("structured_identity_medium:")
        and "scope=public_brand_partial" not in reason
        for reason in reasons
    )
    legal_match = _has_reason(reasons, (
        "legal_name_phrase_match:", "legal_name_full_match:",
        "legal_name_ownership_match:",
    ))
    ownership_match = _has_reason(reasons, ("legal_name_ownership_match:",))
    primary_brand = scorer.primary_brand_tokens(company, limit=1)
    domain_core = scorer.compact_domain_core(candidate.get("url", ""))
    anchored_full_legal_match = bool(
        _has_reason(reasons, ("legal_name_full_match:",))
        and primary_brand
        and len(primary_brand[0]) >= 4
        and domain_core.startswith(primary_brand[0])
    )
    target_contexts = set(scorer.explicit_activity_qualifiers(company))
    structured_contexts = {
        token
        for name in structured_identity.get("names", [])
        for token in scorer.explicit_activity_qualifiers(str(name))
    }
    strong_legal_match = (
        ownership_match
        # A full title copied onto a distributor/product page is still only
        # one first-party observation.  It becomes a bundle component when the
        # candidate domain is anchored by the target's primary public brand.
        or anchored_full_legal_match
        or _reason_match_count(reasons, ("legal_name_phrase_match:",)) >= 2
    )
    corroborated_location_contact = bool(
        structured_identity.get("corroborated_addresses")
        or structured_identity.get("corroborated_phones")
    )
    identifier_groups = _legal_identifier_groups(structured_identity.get("identifiers", []))
    strong_legal_identifier = bool(identifier_groups)
    first_party_bundle_components = sum((
        page_identity_strong,
        structured_bundle_match,
        strong_legal_match,
        corroborated_location_contact,
        strong_legal_identifier,
    ))
    if page_identity or structured_match or legal_match:
        detail = ",".join(
            label for label, present in (
                ("page", page_identity), ("structured", structured_match),
                ("legal", legal_match), ("ownership", ownership_match),
                ("address_or_phone", corroborated_location_contact),
                ("legal_identifier", strong_legal_identifier),
            ) if present
        )
        # All of these originate from the candidate website. They deliberately
        # share one independence key and therefore cannot validate each other.
        signals.append(_signal(
            "first_party_company_identity", "support", "candidate_site",
            "first_party_identity", detail, "strong" if ownership_match or structured_match else "medium",
        ))

    ambiguous_identifier_kinds = sorted(
        kind for kind, values in identifier_groups.items() if len(values) > 1
    )
    if ambiguous_identifier_kinds:
        signals.append(_signal(
            "multiple_first_party_legal_identifiers", "conflict", "candidate_site",
            "legal_identifier", ",".join(ambiguous_identifier_kinds), "strong",
        ))

    structured_unmatched = _has_reason(reasons, ("structured_identity_unmatched:",))
    if structured_unmatched:
        if ownership_match:
            signals.append(_signal(
                "structured_owner_difference_resolved", "neutral", "candidate_site",
                "structured_owner", "explicit_brand_owner_relationship", "strong",
            ))
        else:
            names = ", ".join(structured_identity.get("names", [])[:3])
            signals.append(_signal(
                "structured_owner_mismatch", "conflict", "candidate_site",
                "structured_owner", names or "unmatched_structured_name", "strong",
            ))

    target_types = _company_types([company])
    structured_types = _company_types(structured_identity.get("legal_names", []))
    if target_types and structured_types and target_types.isdisjoint(structured_types) and not ownership_match:
        signals.append(_signal(
            "structured_company_type_mismatch", "conflict", "candidate_site",
            "structured_owner_type",
            f"target={','.join(sorted(target_types))};site={','.join(sorted(structured_types))}",
            "strong",
        ))

    # A missing activity word is not an ownership conflict: public brands often
    # omit broad registry activities.  Two explicit and disjoint activity
    # qualifiers are different.  For example, a target named "Brand Makine"
    # must not inherit an Organization named "Brand Ambalaj" merely because
    # the short brand and domain match.  An explicit brand-owner statement can
    # still resolve a legitimate multi-sector or group relationship.
    if (
        target_contexts
        and structured_contexts
        and target_contexts.isdisjoint(structured_contexts)
        and not ownership_match
    ):
        signals.append(_signal(
            "structured_owner_context_mismatch", "conflict", "candidate_site",
            "structured_owner_context",
            f"target={','.join(sorted(target_contexts))};site={','.join(sorted(structured_contexts))}",
            "strong",
        ))

    if "country_identity_unproven" in reasons:
        signals.append(_signal(
            "target_country_unproven", "conflict", "candidate_site", "country",
            "no_tr_tld_phone_or_text", "medium",
        ))
    elif _has_reason(reasons, ("country_identity_tr_",)):
        signals.append(_signal(
            "target_country_supported", "neutral", "candidate_site", "country",
            next(reason for reason in reasons if reason.startswith("country_identity_tr_")), "medium",
        ))

    if "tls_insecure_transport" in reasons:
        signals.append(_signal(
            "insecure_transport", "neutral", "transport", "transport",
            "tls_certificate_verification_failed", "medium",
        ))

    target_country_supported = _has_reason(reasons, ("country_identity_tr_",))
    public_brand_tokens = scorer.primary_brand_tokens(company, limit=2)
    short_generic_brand = bool(
        len(public_brand_tokens) == 1 and len(public_brand_tokens[0]) < 7
    )
    required_bundle_components = 3 if short_generic_brand else 2
    partial_explicit_context = False
    for reason in reasons:
        if not reason.startswith("context_match:"):
            continue
        try:
            context_hits, context_total = (
                int(value) for value in reason.split(":", 1)[1].split("/", 1)
            )
        except (TypeError, ValueError):
            continue
        partial_explicit_context = context_total >= 2 and context_hits < context_total
        if partial_explicit_context:
            break
    domain_anchored = bool(
        candidate.get("_exact_brand_domain")
        or intrinsic_reason
        or domain_match
        or corroborated_public_brand_domain
    )
    # When a target includes multiple context anchors but the candidate site
    # proves only the shared public-brand portion, page metadata and
    # structured metadata can simply repeat the same homonym. Require a third
    # first-party component unless the domain, legal title or ownership
    # statement independently anchors that activity-specific identity.
    partial_activity_identity = bool(
        partial_explicit_context
        and not (target_contexts & structured_contexts)
        and not domain_anchored
        and not strong_legal_match
    )
    if partial_activity_identity:
        required_bundle_components = max(required_bundle_components, 3)
    strong_bundle_inputs = (
        first_party_bundle_components >= required_bundle_components
        and target_country_supported
    )
    metadata_context_missing = next((reason for reason in reasons if reason.startswith((
        "metadata_context_missing:", "metadata_context_not_observed:",
    ))), "")
    if metadata_context_missing:
        signals.append(_signal(
            "business_context_not_observed",
            "neutral",
            "candidate_site", "business_context", metadata_context_missing,
            "weak",
        ))
    intrinsic_context_missing = next((
        reason for reason in reasons if reason.startswith("context_missing:")
    ), "")
    if intrinsic_context_missing:
        signals.append(_signal(
            "business_context_not_observed" if strong_bundle_inputs else "business_context_unmatched",
            "neutral" if strong_bundle_inputs else "conflict",
            "candidate_site", "business_context", intrinsic_context_missing,
            "weak" if strong_bundle_inputs else "medium",
        ))

    for reason in reasons:
        if reason.startswith("context_match:"):
            try:
                hits, total = (int(value) for value in reason.split(":", 1)[1].split("/", 1))
            except (TypeError, ValueError):
                continue
            if total >= 2 and hits < total and not strong_bundle_inputs:
                signals.append(_signal(
                    "partial_business_context", "conflict", "candidate_site", "business_context",
                    reason, "medium",
                ))
        if reason.startswith(("context_conflict:", "metadata_context_conflict:")):
            signals.append(_signal(
                "business_context_conflict", "conflict", "candidate_site", "business_context",
                reason, "strong",
            ))

    support_keys = sorted({
        signal["independence_key"] for signal in signals
        if signal["polarity"] == "support"
    })
    conflicts = [signal for signal in signals if signal["polarity"] == "conflict"]
    neutral = [signal for signal in signals if signal["polarity"] == "neutral"]
    publishable = len(support_keys) >= 2 and not conflicts
    # Multiple strong identity facts from the candidate itself are not
    # independent sources, so this remains provisional until the caller has
    # compared all plausible domains and confirmed that the candidate is
    # unique. Hard owner/country/context conflicts always block this path.
    strong_first_party_bundle = (
        first_party_bundle_components >= required_bundle_components
        and target_country_supported
        and role not in EXCLUDED_ROLES
        and not conflicts
    )
    return {
        "signals": signals,
        "support_keys": support_keys,
        "support_count": len(support_keys),
        "conflicts": conflicts,
        "neutral": neutral,
        "publishable": publishable,
        "strong_first_party_bundle": strong_first_party_bundle,
        "provisionally_publishable": publishable or strong_first_party_bundle,
        "first_party_bundle_components": first_party_bundle_components,
        "required_first_party_bundle_components": required_bundle_components,
        "partial_activity_identity": partial_activity_identity,
        "decision": (
            "verified" if publishable
            else "strong_first_party_needs_uniqueness" if strong_first_party_bundle
            else "conflict" if conflicts
            else "insufficient_independent_support"
        ),
    }
