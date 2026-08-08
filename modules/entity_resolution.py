"""Resolve one official company identity before contact publication.

Discovery provenance controls crawl order only.  A candidate is accepted from
its own first-party identity evidence; fair, directory and search records never
become identity authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from modules import identity, relationship_graph, scorer


@dataclass(frozen=True)
class TargetProfile:
    company: str
    legal_tokens: tuple[str, ...]
    brand_tokens: tuple[str, ...]
    context_tokens: tuple[str, ...]


@dataclass(frozen=True)
class CandidateFingerprint:
    domain: str
    reachable: bool
    eligible_role: bool
    conflict_free: bool
    provisionally_publishable: bool
    independently_publishable: bool
    first_party_identity: bool
    country_supported: bool
    country_phone_only: bool
    legal_strength: int
    legal_match_count: int
    context_match_count: int
    page_strength: int
    structured_strength: int
    intrinsic_domain: bool
    exact_brand_domain: bool
    public_brand_domain: bool
    obvious_exact_domain: bool
    primary_domain_exact: bool
    short_primary_domain_exact: bool
    primary_domain_contextual: bool
    primary_domain_anchored: bool
    official_query_evidence: int
    same_site_contact: bool
    canonical_domain_consistent: bool
    has_contact: bool
    semantic_match: bool
    semantic_conflict: bool
    direct_entity_identity: bool
    relationship_identity: bool
    places_phone_corroborated: bool
    places_business_corroborated: bool
    structured_business_name_corroborated: bool

    @property
    def verified_identity(self) -> bool:
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.independently_publishable
            and self.first_party_identity
            and self.country_supported
        )

    @property
    def safe_exact_domain_route(self) -> bool:
        """Fast path for a full-name domain without skipping safety checks."""
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.country_supported
            and self.obvious_exact_domain
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )

    @property
    def safe_exact_website_route(self) -> bool:
        """Identify an exact full-name website even when it has no contacts."""
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.country_supported
            and self.obvious_exact_domain
            and self.canonical_domain_consistent
            and self.page_strength >= 1
            and self.official_query_evidence >= 1
        )

    @property
    def safe_public_contact_route(self) -> bool:
        """Resolve a public-brand domain from its own page and contacts."""
        brand_identity = (
            (self.exact_brand_domain and self.page_strength >= 1)
            or (self.public_brand_domain and self.page_strength >= 2)
            or (self.primary_domain_exact and self.page_strength >= 2)
            or (
                self.primary_domain_anchored
                and self.page_strength >= 1
                and self.official_query_evidence >= 3
            )
        )
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.country_supported
            and brand_identity
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
            and self.official_query_evidence >= 2
        )

    @property
    def safe_legal_uniqueness_route(self) -> bool:
        """Resolve a legal-name site only after unusually broad corroboration."""
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.first_party_identity
            and self.country_supported
            and self.legal_strength >= 3
            and self.page_strength >= 3
            and self.official_query_evidence >= 4
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )

    @property
    def safe_places_contact_route(self) -> bool:
        """Use Places only when it independently confirms a first-party phone."""
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.independently_publishable
            and self.country_supported
            and (
                self.places_phone_corroborated
                or self.places_business_corroborated
            )
            and self.page_strength >= 2
            and (
                self.places_phone_corroborated
                or self.official_query_evidence >= 2
            )
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )

    @property
    def safe_structured_brand_route(self) -> bool:
        """Resolve a public brand declared by the site's structured identity."""
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.independently_publishable
            and self.country_supported
            and self.structured_business_name_corroborated
            and self.page_strength >= 2
            and self.structured_strength >= 2
            and self.official_query_evidence >= 2
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )

    @property
    def safe_structured_uniqueness_route(self) -> bool:
        """Use a strong structured brand after broad search corroboration."""
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.country_supported
            and self.structured_business_name_corroborated
            and self.page_strength >= 3
            and self.structured_strength >= 2
            and self.official_query_evidence >= 4
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )

    @property
    def safe_exact_primary_structured_route(self) -> bool:
        """Resolve an exact primary domain that declares the same brand."""
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.independently_publishable
            and self.country_supported
            and self.primary_domain_exact
            and self.page_strength >= 2
            and self.structured_strength >= 2
            and self.official_query_evidence >= 1
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )

    @property
    def safe_short_brand_context_route(self) -> bool:
        """Resolve a four-character exact brand only with broad corroboration."""
        return bool(
            self.reachable
            and self.eligible_role
            and self.conflict_free
            and self.independently_publishable
            and self.country_supported
            and self.short_primary_domain_exact
            and self.page_strength >= 3
            and self.context_match_count >= 1
            and self.official_query_evidence >= 4
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )

    @property
    def safe_verified_first_party_route(self) -> bool:
        """Resolve a domain-backed identity proved by its own site and contact."""
        domain_identity = bool(
            self.public_brand_domain
            or self.primary_domain_exact
            or self.primary_domain_contextual
            or (
                self.structured_business_name_corroborated
                and self.structured_strength >= 2
            )
        )
        return bool(
            self.verified_identity
            and domain_identity
            and self.page_strength >= 2
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )

    @property
    def domain_specificity(self) -> int:
        """Prefer the domain that names the requested entity, not a sibling."""
        if self.obvious_exact_domain:
            return 3
        if self.exact_brand_domain:
            return 2
        if self.primary_domain_contextual:
            return 2
        if self.primary_domain_exact:
            return 1
        return 0

    @property
    def anchor_ready(self) -> bool:
        """A profile-discovered URL whose own site proves the target identity."""
        strong_legal = self.legal_strength >= 3 or (
            self.legal_strength == 2 and self.legal_match_count >= 2
        )
        corroborated_intrinsic = bool(
            self.public_brand_domain
            and self.page_strength >= 2
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )
        return bool(
            self.safe_exact_domain_route
            or self.safe_exact_website_route
            or self.safe_verified_first_party_route
            or (
                self.verified_identity
                and self.has_contact
                and (strong_legal or corroborated_intrinsic)
            )
        )

    @property
    def candidate_ready(self) -> bool:
        """A search candidate with enough intrinsic first-party identity."""
        # A Turkish phone alone cannot disambiguate a generic-TLD homonym.
        # Require either legal identity or independent discovery corroboration.
        weak_country_homonym = bool(
            self.country_phone_only
            and self.legal_strength == 0
            and self.official_query_evidence < 2
            and not self.obvious_exact_domain
            and not self.safe_places_contact_route
            and not self.safe_verified_first_party_route
        )
        if weak_country_homonym:
            return False
        legal_identity = self.legal_strength >= 2
        intrinsic_bundle = bool(
            self.public_brand_domain
            and self.page_strength >= 2
            and self.same_site_contact
            and self.canonical_domain_consistent
            and self.has_contact
        )
        return bool(
            self.safe_exact_domain_route
            or self.safe_exact_website_route
            or self.safe_public_contact_route
            or self.safe_legal_uniqueness_route
            or self.safe_places_contact_route
            or self.safe_structured_brand_route
            or self.safe_structured_uniqueness_route
            or self.safe_exact_primary_structured_route
            or self.safe_short_brand_context_route
            or self.safe_verified_first_party_route
            or (
                self.verified_identity
                and (legal_identity or intrinsic_bundle)
            )
        )


@dataclass(frozen=True)
class Resolution:
    status: str
    selected: dict | None
    contenders: tuple[dict, ...]
    reason: str


def build_target_profile(company: str) -> TargetProfile:
    return TargetProfile(
        company=company,
        legal_tokens=tuple(scorer.legal_identity_tokens(company)),
        brand_tokens=tuple(scorer.primary_brand_tokens(company, limit=2)),
        context_tokens=tuple(scorer.context_tokens(company)),
    )


def _reason_strength(reasons: list[str], prefix: str) -> int:
    levels = {"strong": 3, "medium": 2, "weak": 1}
    values = [
        value
        for reason in reasons
        if str(reason).startswith(prefix)
        for label, value in levels.items()
        if label in str(reason)
    ]
    return max(values, default=0)


def _reason_count(reasons: list[str], prefixes: tuple[str, ...]) -> int:
    for reason in reasons:
        if not str(reason).startswith(prefixes):
            continue
        match = re.search(r":(\d+)(?:/|$)", str(reason))
        if match:
            return int(match.group(1))
    return 0


def fingerprint(
    profile: TargetProfile,
    evaluation: dict,
) -> CandidateFingerprint:
    candidate = evaluation.get("candidate", {})
    reasons = list(evaluation.get("reasons", []))
    assessment = evaluation.get("identity_assessment") or identity.assess(
        profile.company,
        candidate,
        reasons,
        evaluation.get("structured_identity", {}),
    )
    role = str(candidate.get("role", ""))
    legal_strength = 0
    if any(str(reason).startswith("legal_name_ownership_match:") for reason in reasons):
        legal_strength = 4
    elif any(str(reason).startswith("legal_name_full_match:") for reason in reasons):
        legal_strength = 3
    elif any(str(reason).startswith("legal_name_phrase_match:") for reason in reasons):
        legal_strength = 2
    context_count = _reason_count(
        reasons, ("context_match:", "context_name_match:")
    )
    first_party_identity = bool(
        "first_party_identity" in assessment.get("support_keys", [])
        or any(str(reason).startswith((
            "page_identity_medium:",
            "page_identity_strong:",
            "structured_identity_medium:",
            "structured_identity_strong:",
            "legal_name_phrase_match:",
            "legal_name_full_match:",
            "legal_name_ownership_match:",
        )) for reason in reasons)
    )
    domain = scorer.normalize_domain(candidate.get("url", ""))
    brand_tokens = scorer.primary_brand_tokens(profile.company, limit=2)
    brand_compounds = scorer.primary_brand_domain_compounds(
        profile.company, limit=2,
    )
    exact_brand_domain = bool(
        brand_compounds
        and scorer.compact_domain_core(domain) in brand_compounds
    )
    public_brand_domain = scorer.public_brand_domain_match(
        profile.company, domain,
    )
    legal_compound = "".join(profile.legal_tokens)
    obvious_exact_domain = bool(
        legal_compound
        and len(legal_compound) >= 7
        and scorer.compact_domain_core(domain) == legal_compound
    )
    primary_tokens = scorer.primary_brand_tokens(profile.company, limit=1)
    primary_token = primary_tokens[0] if primary_tokens else ""
    mixed_alphanumeric_primary = bool(
        any(char.isalpha() for char in primary_token)
        and any(char.isdigit() for char in primary_token)
    )
    primary_domain_exact = bool(
        primary_tokens
        and (len(primary_token) >= 5 or mixed_alphanumeric_primary)
        and scorer.compact_domain_core(domain) == primary_token
    )
    domain_core = scorer.compact_domain_core(domain)
    contextual_suffixes = {
        token
        for token in profile.legal_tokens[1:]
        if len(token) >= 3 and token != primary_token
    }
    primary_domain_contextual = bool(
        primary_token
        and len(primary_token) >= 5
        and any(
            domain_core == primary_token + suffix
            for suffix in contextual_suffixes
        )
    )
    primary_domain_anchored = bool(
        primary_tokens
        and len(primary_tokens[0]) >= 5
        and scorer.compact_domain_core(domain).startswith(primary_tokens[0])
        and len(
            scorer.compact_domain_core(domain)[len(primary_tokens[0]):]
        ) >= 2
    )
    page_strength = _reason_strength(reasons, "page_identity_")
    structured_strength = _reason_strength(reasons, "structured_identity_")
    # A single-token name can still be a surname or common word. It enters the
    # fast path only with first-party page/structured identity in addition to
    # exact-domain and contact safety.
    if len(profile.legal_tokens) == 1 and not (
        page_strength >= 2 or structured_strength >= 2
    ):
        obvious_exact_domain = False
    contact_source_urls = [
        str(evaluation.get("email_source_url", "") or ""),
        str(evaluation.get("phone_source_url", "") or ""),
    ]
    same_site_contact = any(
        source_url and scorer.same_registrable_domain(source_url, domain)
        for source_url in contact_source_urls
    )
    final_domain = scorer.normalize_domain(
        evaluation.get("crawl_result", {}).get("url", "")
    )
    canonical_domain_consistent = bool(
        final_domain
        and (
            scorer.same_registrable_domain(domain, final_domain)
            or scorer.compact_domain_core(final_domain) == legal_compound
        )
    )
    relationship_identity = any(
        str(reason).startswith("legal_name_ownership_match:")
        for reason in reasons
    )
    direct_entity_identity = bool(
        not relationship_identity
        and (
            legal_strength >= 2
            or obvious_exact_domain
            or (
                public_brand_domain
                and (page_strength >= 2 or structured_strength >= 2)
                and same_site_contact
                and canonical_domain_consistent
            )
        )
    )
    return CandidateFingerprint(
        domain=domain,
        reachable=bool(evaluation.get("crawl_result", {}).get("pages")),
        eligible_role=role not in identity.EXCLUDED_ROLES
        and not scorer.is_excluded_domain(domain)
        and not scorer.is_public_body_domain(domain)
        and not scorer.is_foreign_country_domain(domain),
        conflict_free=not bool(assessment.get("conflicts")),
        provisionally_publishable=bool(
            assessment.get("provisionally_publishable")
        ),
        independently_publishable=bool(assessment.get("publishable")),
        first_party_identity=first_party_identity,
        country_supported=any(
            str(reason).startswith("country_identity_tr_") for reason in reasons
        ),
        country_phone_only=bool(
            "country_identity_tr_phone" in reasons
            and not any(reason in reasons for reason in (
                "country_identity_tr_tld",
                "country_identity_tr_text",
                "country_identity_tr_address",
            ))
        ),
        legal_strength=legal_strength,
        legal_match_count=_reason_count(reasons, (
            "legal_name_ownership_match:",
            "legal_name_full_match:",
            "legal_name_phrase_match:",
        )),
        context_match_count=context_count,
        page_strength=page_strength,
        structured_strength=structured_strength,
        intrinsic_domain=bool(
            scorer.domain_identity_match(profile.company, domain)[0]
        ),
        exact_brand_domain=exact_brand_domain,
        public_brand_domain=public_brand_domain,
        obvious_exact_domain=obvious_exact_domain,
        primary_domain_exact=primary_domain_exact,
        short_primary_domain_exact=bool(
            primary_token
            and len(primary_token) >= 4
            and domain_core == primary_token
        ),
        primary_domain_contextual=primary_domain_contextual,
        primary_domain_anchored=primary_domain_anchored,
        official_query_evidence=int(
            candidate.get("_official_query_evidence", 0) or 0
        ),
        same_site_contact=same_site_contact,
        canonical_domain_consistent=canonical_domain_consistent,
        has_contact=bool(evaluation.get("has_contact")),
        semantic_match=bool(
            evaluation.get("semantic_identity", {}).get("decision") == "match"
        ),
        semantic_conflict=bool(
            evaluation.get("semantic_identity", {}).get("decision") == "conflict"
        ),
        direct_entity_identity=direct_entity_identity,
        relationship_identity=relationship_identity,
        places_phone_corroborated=bool(
            any(
                scorer.business_name_identity_match(
                    profile.company, str(item.get("name", "") or "")
                )
                for item in candidate.get("_google_places_evidence", [])
            )
            and "google_places_first_party_phone_match" in reasons
        ),
        places_business_corroborated=bool(
            any(
                scorer.business_name_identity_match(
                    profile.company, str(item.get("name", "") or "")
                )
                and scorer.same_registrable_domain(
                    str(item.get("website", "") or ""), candidate.get("url", "")
                )
                for item in candidate.get("_google_places_evidence", [])
            )
        ),
        structured_business_name_corroborated=bool(
            any(
                scorer.business_name_identity_match(profile.company, str(name))
                for name in (
                    *evaluation.get("structured_identity", {}).get("names", []),
                    *evaluation.get("structured_identity", {}).get("brand_names", []),
                    *evaluation.get("structured_identity", {}).get("legal_names", []),
                )
            )
        ),
    )


def _identity_key(item: tuple[dict, CandidateFingerprint]) -> tuple[int, ...]:
    _, value = item
    return (
        int(value.safe_exact_domain_route),
        int(value.safe_exact_website_route),
        int(value.safe_public_contact_route),
        int(value.safe_legal_uniqueness_route),
        int(value.safe_places_contact_route),
        int(value.safe_structured_brand_route),
        int(value.safe_structured_uniqueness_route),
        int(value.safe_exact_primary_structured_route),
        int(value.safe_short_brand_context_route),
        int(value.safe_verified_first_party_route),
        value.domain_specificity,
        int(value.verified_identity),
        int(value.direct_entity_identity),
        -int(value.relationship_identity),
        value.legal_strength,
        value.legal_match_count,
        value.context_match_count,
        value.structured_strength,
        value.page_strength,
        int(value.public_brand_domain),
        int(value.exact_brand_domain),
        int(value.intrinsic_domain),
        int(value.has_contact),
        int(value.semantic_match),
        -int(value.semantic_conflict),
        value.official_query_evidence,
    )


def _dominates(
    left: tuple[dict, CandidateFingerprint],
    right: tuple[dict, CandidateFingerprint],
) -> bool:
    """Pareto-style elimination with a few intrinsically decisive signals."""
    _, first = left
    _, second = right
    if first.semantic_conflict != second.semantic_conflict:
        return not first.semantic_conflict
    if first.direct_entity_identity != second.direct_entity_identity:
        return first.direct_entity_identity
    if first.domain_specificity != second.domain_specificity:
        specific, broad = (
            (first, second)
            if first.domain_specificity > second.domain_specificity
            else (second, first)
        )
        specificity_is_proved = bool(
            specific.verified_identity
            and specific.page_strength >= 1
            and specific.country_supported
            and specific.has_contact
            and not specific.semantic_conflict
        )
        if specificity_is_proved:
            return first is specific
    if first.safe_exact_domain_route != second.safe_exact_domain_route:
        return first.safe_exact_domain_route
    dimensions = (
        (first.legal_strength, second.legal_strength),
        (first.legal_match_count, second.legal_match_count),
        (first.context_match_count, second.context_match_count),
        (first.structured_strength, second.structured_strength),
        (first.page_strength, second.page_strength),
        (int(first.semantic_match), int(second.semantic_match)),
        (int(first.intrinsic_domain), int(second.intrinsic_domain)),
        (int(first.has_contact), int(second.has_contact)),
        (first.official_query_evidence, second.official_query_evidence),
    )
    return all(left_value >= right_value for left_value, right_value in dimensions) and any(
        left_value > right_value for left_value, right_value in dimensions
    )


def _tournament(
    ready: list[tuple[dict, CandidateFingerprint]],
    evaluations: list[dict],
) -> list[tuple[dict, CandidateFingerprint]]:
    components = relationship_graph.connected_domain_components(evaluations)
    survivors: list[tuple[dict, CandidateFingerprint]] = []
    for candidate in ready:
        if any(
            _dominates(other, candidate)
            and not relationship_graph.same_official_family(
                other[1].domain, candidate[1].domain, components,
            )
            for other in ready
            if other is not candidate
        ):
            continue
        survivors.append(candidate)
    survivors.sort(key=_identity_key, reverse=True)
    return survivors


def resolve_profile_anchor(
    company: str,
    evaluations: list[dict],
) -> Resolution:
    """Resolve explicit profile routes before broad web search is attempted."""
    profile = build_target_profile(company)
    candidates = [
        (item, fingerprint(profile, item))
        for item in evaluations
        if item.get("candidate", {}).get("_source_profile_evidence")
    ]
    ready = [item for item in candidates if item[1].anchor_ready]
    if not ready:
        return Resolution(
            "unresolved",
            None,
            tuple(item for item, _ in candidates),
            "profile_route_lacks_first_party_identity_bundle",
        )
    ready.sort(key=_identity_key, reverse=True)
    selected, selected_fingerprint = ready[0]
    equally_strong = [
        item
        for item, value in ready[1:]
        if _identity_key((item, value)) == _identity_key(
            (selected, selected_fingerprint)
        )
        and not scorer.same_registrable_domain(
            selected_fingerprint.domain, value.domain
        )
    ]
    if equally_strong:
        return Resolution(
            "ambiguous",
            None,
            tuple([selected, *equally_strong]),
            "multiple_profile_routes_with_equal_first_party_identity",
        )
    return Resolution(
        "resolved",
        selected,
        tuple(item for item, _ in ready),
        (
            "profile_route_resolved_by_exact_full_name_domain"
            if (
                selected_fingerprint.safe_exact_domain_route
                or selected_fingerprint.safe_exact_website_route
            )
            else "profile_route_resolved_by_first_party_identity"
        ),
    )


def resolve_candidates(
    company: str,
    evaluations: list[dict],
) -> Resolution:
    """Select one identity candidate; compare only candidates that prove identity."""
    profile = build_target_profile(company)
    candidates = [
        (item, fingerprint(profile, item))
        for item in evaluations
        if item.get("crawl_result", {}).get("pages")
    ]
    ready = [item for item in candidates if item[1].candidate_ready]
    if not ready:
        return Resolution(
            "unresolved",
            None,
            tuple(item for item, _ in candidates),
            "no_candidate_proved_target_fingerprint",
        )
    ready = _tournament(ready, evaluations)
    components = relationship_graph.connected_domain_components(evaluations)
    selected, selected_fingerprint = ready[0]
    selected_key = _identity_key((selected, selected_fingerprint))
    contenders = []
    for item, value in ready[1:]:
        if relationship_graph.same_official_family(
            selected_fingerprint.domain, value.domain, components,
        ):
            continue
        if _identity_key((item, value)) == selected_key:
            contenders.append(item)
    if contenders:
        return Resolution(
            "ambiguous",
            None,
            tuple([selected, *contenders]),
            "multiple_candidates_proved_equal_target_fingerprint",
        )
    return Resolution(
        "resolved",
        selected,
        tuple(item for item, _ in ready),
        (
            "candidate_resolved_by_exact_full_name_domain"
            if (
                selected_fingerprint.safe_exact_domain_route
                or selected_fingerprint.safe_exact_website_route
            )
            else "candidate_resolved_by_target_fingerprint"
        ),
    )
