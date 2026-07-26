"""Adaptive, discovery-only search query planning.

Inputs from fair profiles, directories and search snippets may widen discovery,
but queries produced here never become identity evidence.  Candidate pages must
still pass the first-party crawl and publication gates.
"""

from __future__ import annotations

import re

from modules import scorer


def _split_hints(value: object, limit: int = 3) -> list[str]:
    values = re.split(r"[,;/|\n]+", str(value or ""))
    result: list[str] = []
    for value in values:
        cleaned = re.sub(r"\s+", " ", value).strip(" -\t\r\n")
        if 2 <= len(cleaned) <= 80 and cleaned not in result:
            result.append(cleaned)
    return result[:limit]


def _city_hint(address: object) -> str:
    """Return a conservative city-like tail from a discovery address."""
    parts = _split_hints(address, limit=10)
    ignored = {"turkiye", "turkey", "tr", "osb", "organize sanayi bolgesi"}
    for part in reversed(parts):
        normalized = scorer.normalize_text(part)
        normalized = re.sub(r"\b\d{4,6}\b", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if (
            normalized
            and normalized not in ignored
            and not any(char.isdigit() for char in normalized)
            and 2 <= len(normalized) <= 30
        ):
            return normalized
    return ""


def _query_key(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip().casefold()


def query_intent(query: str) -> str:
    normalized = scorer.normalize_text(query)
    if any(term in normalized for term in ("distributor", "temsilci", "marka resmi")):
        return "relationship"
    if any(term in normalized for term in ("kvkk", "ticari unvan")):
        return "legal_identity"
    if any(term in normalized for term in ("contact", "iletisim")):
        return "contact"
    if "official website" in normalized and "turkiye" in normalized:
        return "country_official"
    if any(term in normalized for term in ("official website", "resmi sitesi", "web sitesi")):
        return "official"
    return "context"


def diverse_queries(queries: list[str], limit: int) -> list[str]:
    """Use a bounded budget across intents before repeating name variants."""
    if limit <= 0:
        return []
    unique = list(dict.fromkeys(query for query in queries if query))
    selected: list[str] = []
    seen_intents: set[str] = set()
    for query in unique:
        intent = query_intent(query)
        if intent in seen_intents:
            continue
        selected.append(query)
        seen_intents.add(intent)
        if len(selected) >= limit:
            return selected
    selected_set = set(selected)
    selected.extend(
        query for query in unique
        if query not in selected_set
    )
    return selected[:limit]


def adaptive_queries(
    company_name: str,
    metadata: dict | None,
    already_run: set[str] | None = None,
    related_name_hints: list[str] | None = None,
    context_terms: list[str] | None = None,
    evidence_gaps: set[str] | None = None,
    limit: int = 8,
) -> list[str]:
    """Plan the next queries for the evidence gaps still left unresolved.

    ``None`` keeps the complete legacy plan for callers that do not yet expose
    discovery state.  The search pipeline passes an explicit set and replans
    after every adaptive query, so a newly resolved gap does not spend another
    request on a stale query family.
    """
    metadata = metadata or {}
    corporate_words = {
        scorer.normalize_text(word)
        for word in ("anonim", "limited", "sirket", "sirketi", "sanayi", "ticaret")
    }
    brand_tokens = [
        token for token in scorer.primary_brand_tokens(company_name, limit=2)
        if token not in corporate_words
    ]
    full_name = " ".join(scorer._raw_company_tokens(company_name)).strip()
    brand = " ".join(brand_tokens).strip() or full_name
    if not brand:
        return []

    contexts = [value for value in (context_terms or []) if value]
    brands = _split_hints(metadata.get("brands"))
    representations = _split_hints(metadata.get("representations"))
    city = _city_hint(metadata.get("listed_address"))
    related = [*_split_hints(";".join(related_name_hints or []), limit=4)]
    gaps = evidence_gaps if evidence_gaps is not None else {
        "no_candidates", "ambiguous_candidates", "missing_intrinsic_domain",
        "missing_legal_name", "missing_local_signal", "relationship_hint",
    }

    # Preserve the established high-value order inside each state.  Metadata
    # and snippet hints remain discovery-only regardless of which gap selected
    # them.
    planned = [
        *(f'"{hint}" Turkiye official website' for hint in related if "relationship_hint" in gaps),
        f'"{brand}" Turkiye resmi sitesi' if gaps & {
            "no_candidates", "ambiguous_candidates", "missing_intrinsic_domain",
        } else "",
        f'"{full_name}" web sitesi' if full_name != brand and gaps & {
            "no_candidates", "ambiguous_candidates", "missing_intrinsic_domain",
        } else "",
        f'"{brand}" kvkk' if gaps & {"no_candidates", "missing_legal_name"} else "",
        f'"{full_name}" ticari unvan' if full_name and "missing_legal_name" in gaps else "",
        *(f'"{item}" Turkiye distributor temsilci' for item in representations if gaps & {
            "no_candidates", "missing_intrinsic_domain", "relationship_hint",
        }),
        *(f'"{item}" Turkiye marka resmi sitesi' for item in brands if gaps & {
            "no_candidates", "missing_intrinsic_domain", "relationship_hint",
        }),
        f'"{full_name}" "{city}"' if full_name and city and gaps & {
            "ambiguous_candidates", "missing_local_signal",
        } else "",
        f'"{brand}" {contexts[0]} Turkiye' if contexts and gaps & {
            "ambiguous_candidates", "missing_local_signal",
        } else "",
        f'"{brand}" "{metadata.get("sector", "")}"' if metadata.get("sector") and gaps & {
            "ambiguous_candidates", "missing_local_signal",
        } else "",
        f'"{brand}" iletisim Turkiye' if gaps & {
            "no_candidates", "missing_local_signal",
        } else "",
    ]
    seen = {_query_key(value) for value in (already_run or set())}
    result: list[str] = []
    for query in planned:
        key = _query_key(query)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(re.sub(r"\s+", " ", query).strip())
        if len(result) >= limit:
            break
    return result
