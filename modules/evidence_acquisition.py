"""Bounded evidence-completion planning for unresolved company identities.

The planner is deliberately side-effect free.  It describes which first-party
page scopes and discovery queries are still useful; the pipeline decides
whether the current run mode and budget permit executing them.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from modules import entity_resolution, scorer


@dataclass(frozen=True)
class EvidenceState:
    gaps: frozenset[str]
    crawl_scopes: tuple[str, ...]
    search_queries: tuple[str, ...]
    terminal_reason: str

    @property
    def complete(self) -> bool:
        return not self.gaps


_SCOPE_PRIORITY = {
    "missing_legal_identity": ("legal", "privacy", "about", "terms", "catalog"),
    "missing_relationship": ("about", "legal", "distributors", "catalog"),
    "missing_context": ("about", "catalog", "distributors", "locations"),
    "missing_country": ("locations", "contact", "legal"),
    "missing_contact": ("contact", "locations", "legal"),
    "unreachable_candidates": ("legal", "contact", "about", "privacy"),
    "ambiguous_candidates": ("legal", "about", "locations", "catalog"),
    "missing_identity_coherence": ("legal", "about", "contact", "catalog"),
}
_GAP_ORDER = (
    "missing_legal_identity",
    "missing_relationship",
    "missing_identity_coherence",
    "ambiguous_candidates",
    "missing_context",
    "missing_country",
    "missing_contact",
    "unreachable_candidates",
    "no_candidates",
)


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _full_name(company: str) -> str:
    return re.sub(r"\s+", " ", str(company)).strip(" \t\r\n-")


def _queries(
    company: str,
    metadata: dict,
    gaps: set[str],
    limit: int,
) -> tuple[str, ...]:
    full_name = _full_name(company)
    if not full_name or limit <= 0:
        return ()
    quoted = f'"{full_name}"'
    sector = str(metadata.get("sector", "")).strip()
    address = str(metadata.get("listed_address", "")).strip()
    planned = [
        f"{quoted} official website",
        f"{quoted} ticari unvan KVKK"
        if gaps & {
            "missing_legal_identity", "missing_identity_coherence",
            "ambiguous_candidates",
        } else "",
        f"{quoted} {sector} Turkiye"
        if sector and gaps & {"missing_context", "ambiguous_candidates"} else "",
        f"{quoted} {address}"
        if address and gaps & {"missing_country", "ambiguous_candidates"} else "",
        f"{quoted} iletisim"
        if gaps & {"missing_contact", "unreachable_candidates"} else "",
    ]
    return _unique(planned)[:limit]


def analyze(
    company: str,
    evaluations: list[dict],
    *,
    resolution_status: str = "unresolved",
    metadata: dict | None = None,
    query_limit: int = 3,
) -> EvidenceState:
    """Describe the smallest useful next acquisition step.

    A resolved candidate with a same-site contact is terminal.  Everything
    else is represented as explicit evidence gaps so callers never retry an
    undifferentiated broad search.
    """
    metadata = metadata or {}
    profile = entity_resolution.build_target_profile(company)
    fingerprints = [
        entity_resolution.fingerprint(profile, item)
        for item in evaluations
    ]
    reachable = [value for value in fingerprints if value.reachable]
    ready = [value for value in fingerprints if value.candidate_ready]
    gaps: set[str] = set()

    if resolution_status == "resolved" and any(
        value.candidate_ready and value.has_contact for value in fingerprints
    ):
        return EvidenceState(frozenset(), (), (), "identity_and_contact_complete")
    if not fingerprints:
        gaps.add("no_candidates")
    elif not reachable:
        gaps.add("unreachable_candidates")
    else:
        viable = [
            value for value in reachable
            if (
                value.eligible_role
                and value.conflict_free
                and value.canonical_domain_consistent
            )
        ]
        if not viable:
            gaps.add("missing_identity_coherence")
        else:
            # Plan against one coherent first-party route. Evidence from an
            # unrelated candidate must not fill a different candidate's gap.
            target = max(viable, key=lambda value: (
                value.candidate_ready,
                value.provisionally_publishable,
                value.first_party_identity,
                value.obvious_exact_domain,
                value.legal_strength,
                value.public_brand_domain and value.page_strength >= 2,
                value.page_strength,
                value.structured_strength,
                value.country_supported,
                value.context_match_count,
                value.same_site_contact and value.has_contact,
            ))
        if viable and target.legal_strength < 2:
            gaps.add("missing_legal_identity")
        if viable and target.context_match_count <= 0:
            gaps.add("missing_context")
        if viable and not target.country_supported:
            gaps.add("missing_country")
        if viable and not target.first_party_identity:
            gaps.add("missing_relationship")
        if viable and not (target.same_site_contact and target.has_contact):
            gaps.add("missing_contact")
        if (
            viable
            and resolution_status != "resolved"
            and not target.candidate_ready
            and not gaps
        ):
            gaps.add("missing_identity_coherence")
    if resolution_status == "ambiguous" or len(ready) > 1:
        gaps.add("ambiguous_candidates")

    scopes = _unique(
        scope
        for gap in _GAP_ORDER
        if gap in gaps
        for scope in _SCOPE_PRIORITY.get(gap, ())
    )
    terminal = (
        "no_candidate_discovered"
        if gaps == {"no_candidates"}
        else "bounded_evidence_acquisition_required"
    )
    return EvidenceState(
        frozenset(gaps),
        scopes,
        _queries(company, metadata, gaps, query_limit),
        terminal,
    )


def should_continue(
    previous: EvidenceState | None,
    current: EvidenceState,
    round_number: int,
    max_rounds: int,
) -> bool:
    """Stop on completion, exhausted budget, or a round with no gap progress."""
    if current.complete or round_number >= max_rounds:
        return False
    if previous is not None and current.gaps >= previous.gaps:
        return False
    return bool(current.crawl_scopes or current.search_queries)
