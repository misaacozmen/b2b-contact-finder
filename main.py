import argparse
import getpass
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import config
from modules import scorer
from modules import candidate_reranker, checkpoint, contact_decision, contact_publication, crawler, discovery_coverage, email_verifier, entity_memory, entity_registry, entity_resolution, entity_semantics, evidence, evidence_acquisition, evidence_ledger, excel, extractor, identity, linkedin_company, llm_arbiter, phone, publication_policy, quality_audit, relationship_graph, replay_snapshot, report, runtime, search, secrets_store
from modules.utils import ensure_directories, random_delay, setup_logging


def _empty_result(company: str, status: str, reason: str = "", score: int = 0) -> dict:
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


def _attach_candidates(row: dict, candidates: list[dict]) -> dict:
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
            final_stage = "published" if status.startswith("OK_") else "selected_for_review"
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


def _email_domain(email: str) -> str:
    if "@" not in email:
        return ""
    return scorer.normalize_domain(email.split("@", 1)[1])


def _email_is_usable(email: str) -> bool:
    domain = _email_domain(email)
    if not domain:
        return False
    local = email.split("@", 1)[0].casefold()
    compact_local = re.sub(r"[._]+", "-", local)
    local_prefix = re.split(r"[._-]", local, maxsplit=1)[0]
    if (
        local in config.BLOCKED_EMAIL_LOCAL_PREFIXES
        or compact_local in config.BLOCKED_EMAIL_LOCAL_PREFIXES
        or local_prefix in config.BLOCKED_EMAIL_LOCAL_PREFIXES
    ):
        return False
    return not any(domain == bad or domain.endswith(f".{bad}") for bad in config.BAD_EMAIL_DOMAINS)


def _select_best_email(company: str, website: str, emails: list[str]) -> str:
    records = [{"value": email, "source_url": "", "label": "general"} for email in emails]
    return _select_best_email_record(company, website, records).get("value", "")


def _select_best_email_record(company: str, website: str, records: list[dict]) -> dict:
    """Prefer a target-country mailbox when an official site has local pages."""
    ranked = contact_decision.rank_email_records(
        company, website, records, _email_is_usable,
    )
    return ranked[0] if ranked else {}


def _page_identity_score(company: str, pages: list[dict]) -> tuple[int, str]:
    tokens = scorer.distinctive_tokens(company)
    if not tokens:
        return 0, "no_distinctive_tokens"
    _HTML_TRUNCATE = 50000
    raw_parts = []
    for page in pages:
        html_content = page.get("html", "")
        if len(html_content) > _HTML_TRUNCATE:
            import logging as _logging
            _logging.getLogger("contact_finder").debug(
                "HTML truncated for page %s: %d → %d chars",
                page.get("url", "?"), len(html_content), _HTML_TRUNCATE,
            )
        raw_parts.append(html_content[:_HTML_TRUNCATE])
    page_texts = [scorer.normalize_text(part) for part in raw_parts]
    text = " ".join(page_texts)
    hits = sum(1 for token in tokens if token in text)
    ratio = hits / len(tokens)
    if ratio >= 0.75:
        return 14, f"page_identity_strong:{hits}/{len(tokens)}"
    if ratio >= 0.5:
        return 8, f"page_identity_medium:{hits}/{len(tokens)}"

    # A long legal name must not dilute a short public brand. Count the brand
    # on distinct first-party pages so a single incidental catalogue mention
    # cannot become strong company identity by itself.
    brand_tokens = scorer.primary_brand_tokens(company, limit=2)
    if brand_tokens:
        brand_hits, _, translated_hits = scorer.primary_brand_text_hits(company, text, limit=2)
        full_brand_pages = sum(
            1 for page_text in page_texts
            if scorer.primary_brand_text_hits(company, page_text, limit=2)[0] == len(brand_tokens)
        )
        primary_brand_pages = sum(1 for page_text in page_texts if brand_tokens[0] in page_text)
        translation_suffix = f",translated={translated_hits}" if translated_hits else ""
        if brand_hits == len(brand_tokens) and full_brand_pages >= 2:
            return 14, (
                f"page_identity_strong:{brand_hits}/{len(brand_tokens)}"
                f"@scope=public_brand,pages={full_brand_pages}{translation_suffix}"
            )
        if brand_hits == len(brand_tokens) or (
            brand_hits / len(brand_tokens) >= 0.5 and primary_brand_pages >= 2
        ):
            return 8, (
                f"page_identity_medium:{brand_hits}/{len(brand_tokens)}"
                f"@scope=public_brand,pages={primary_brand_pages}{translation_suffix}"
            )
    if hits:
        return 3, f"page_identity_weak:{hits}/{len(tokens)}"
    return -20, f"page_identity_missing:0/{len(tokens)}"


def _legal_name_identity_score(company: str, pages: list[dict]) -> tuple[int, str]:
    """Score a contiguous public/legal name separately from loose token hits."""
    text = " ".join(page.get("html", "")[:50000] for page in pages)
    tokens = scorer.legal_identity_tokens(company)
    if not tokens:
        return 0, "legal_name_phrase_unavailable"
    if scorer.ownership_statement_match(company, text):
        return 20, f"legal_name_ownership_match:{min(len(tokens), 4)}"
    if len(tokens) >= 3 and scorer.legal_name_full_phrase_match(company, text):
        return 19, f"legal_name_full_match:{len(tokens)}"
    if scorer.legal_name_phrase_match(company, text):
        return 16, f"legal_name_phrase_match:{min(len(tokens), 4)}"
    return -8, f"legal_name_phrase_missing:0/{min(len(tokens), 4)}"


def _country_identity_score(crawl_result: dict, normalized_phones: list[str]) -> tuple[int, str]:
    """Require a Turkish footprint for search-discovered non-.tr domains."""
    domain = scorer.normalize_domain(crawl_result.get("url", ""))
    if domain.endswith(".tr"):
        return 8, "country_identity_tr_tld"
    if normalized_phones:
        return 8, "country_identity_tr_phone"
    observed_phones = [
        phone.normalize_phone(value)
        for page in crawl_result.get("pages", [])
        for value in extractor.extract_phones(page.get("html", ""))
    ]
    if any(observed_phones):
        return 8, "country_identity_tr_phone"
    text = " ".join(
        scorer.normalize_text(page.get("html", "")[:50000])
        for page in crawl_result.get("pages", [])
    )
    markers = ("turkiye", "turkey", "istanbul", "ankara", "izmir", "bursa", "kocaeli", "konya", "gaziantep")
    if any(re.search(rf"\b{marker}\b", text) for marker in markers):
        return 5, "country_identity_tr_text"
    return -10, "country_identity_unproven"


def _structured_identity_score(company: str, pages: list[dict]) -> tuple[int, str, dict]:
    combined = {
        "names": [], "urls": [], "same_as": [], "addresses": [],
        "identifiers": [], "ownership_statements": [],
        "legal_names": [], "brand_names": [], "related_organizations": [],
        "phones": [], "relationships": [], "claims": [],
    }
    page_identities: list[dict] = []
    for page in pages:
        identity = extractor.extract_organization_evidence(
            page.get("html", ""), page.get("url", ""),
            page.get("retrieval_method", "unknown"),
        )
        page_identities.append(identity)
        for key in combined:
            combined[key].extend(identity.get(key, []))
    relationships = combined.pop("relationships")
    claims = combined.pop("claims")
    combined = {key: list(dict.fromkeys(values)) for key, values in combined.items()}
    seen_relationships = set()
    combined["relationships"] = []
    for claim in relationships:
        marker = (claim.get("kind", ""), claim.get("name", ""), claim.get("url", ""))
        if marker not in seen_relationships:
            seen_relationships.add(marker)
            combined["relationships"].append(claim)
    combined["claims"] = evidence_ledger.deduplicate(claims)

    def repeated_values(key: str) -> list[str]:
        occurrences: dict[str, set[int]] = {}
        originals: dict[str, str] = {}
        for page_index, page_identity in enumerate(page_identities):
            for raw_value in page_identity.get(key, []):
                normalized = scorer.normalize_text(str(raw_value))
                if key == "phones":
                    normalized = re.sub(r"\D", "", str(raw_value))
                if not normalized:
                    continue
                occurrences.setdefault(normalized, set()).add(page_index)
                originals.setdefault(normalized, str(raw_value))
        return [originals[value] for value, sources in occurrences.items() if len(sources) >= 2]

    combined["corroborated_addresses"] = repeated_values("addresses")
    combined["corroborated_phones"] = repeated_values("phones")
    # Keep entity identity separate from entity relationships. A labelled
    # legal name proves who operates the current site, but it does not by
    # itself assert a parent, branch or product-division relationship.
    relationship_text = " ".join([
        *combined["ownership_statements"], *combined["related_organizations"],
        *(
            str(value)
            for relationship in combined["relationships"]
            for value in (relationship.get("name", ""), relationship.get("url", ""))
            if value
        ),
    ])
    if relationship_text and (
        scorer.legal_name_phrase_match(company, relationship_text)
        or scorer.ownership_statement_match(company, relationship_text)
    ):
        return 14, "structured_identity_strong:1/1@scope=declared_relationship", combined
    legal_name_text = " ".join(combined["legal_names"])
    if legal_name_text and scorer.legal_name_phrase_match(company, legal_name_text):
        return 14, "structured_identity_strong:1/1@scope=legal_name", combined
    if not combined["names"] and not combined["urls"]:
        return 0, "structured_identity_absent", combined
    # Canonical URLs identify the crawled domain, not the company owner. They
    # must not be interpreted as a contradictory organization name.
    if not combined["names"]:
        return 0, "structured_identity_urls_only", combined

    tokens = scorer.distinctive_tokens(company)
    names_text = scorer.normalize_text(" ".join(combined["names"]))
    hits = sum(1 for token in tokens if token in names_text)
    ratio = hits / max(len(tokens), 1)
    if ratio >= 0.75:
        return 14, f"structured_identity_strong:{hits}/{len(tokens)}", combined
    if ratio >= 0.5:
        return 8, f"structured_identity_medium:{hits}/{len(tokens)}", combined

    brand_tokens = scorer.primary_brand_tokens(company, limit=2)
    brand_hits, _, translated_hits = scorer.primary_brand_text_hits(company, names_text, limit=2)
    translation_suffix = f",translated={translated_hits}" if translated_hits else ""
    if brand_tokens and brand_hits == len(brand_tokens):
        return 14, (
            f"structured_identity_strong:{brand_hits}/{len(brand_tokens)}"
            f"@scope=public_brand{translation_suffix}"
        ), combined
    if brand_tokens and brand_hits and len(brand_tokens[0]) >= 7:
        return 8, (
            f"structured_identity_medium:{brand_hits}/{len(brand_tokens)}"
            f"@scope=public_brand_partial{translation_suffix}"
        ), combined
    if hits:
        return 3, f"structured_identity_weak:{hits}/{len(tokens)}", combined
    return 0, f"structured_identity_unmatched:0/{len(tokens)}", combined


def _select_phone_records(records: list[dict]) -> list[dict]:
    return contact_decision.rank_phone_records(records)


def _page_context_score(company: str, pages: list[dict], metadata: dict | None = None) -> tuple[int, str]:
    raw_tokens = scorer._raw_company_tokens(company)
    context_tokens = [token for token in raw_tokens if token in config.CONTEXT_VALIDATION_WORDS]
    text = scorer.normalize_text(" ".join(page.get("html", "")[:50000] for page in pages))
    if context_tokens:
        hits = sum(1 for token in context_tokens if token in text)
        if hits:
            # Activity words embedded in a legal company name are useful
            # corroboration, but their absence is not an ownership conflict:
            # public brand sites often omit old or broad registry activities.
            return 4, f"context_name_match:{hits}/{len(context_tokens)}"
        return 0, f"context_name_not_observed:0/{len(context_tokens)}"

    metadata_contexts = scorer.metadata_contexts(metadata)
    if not metadata_contexts:
        return 0, "no_context_tokens"
    hits = sum(1 for context in metadata_contexts if scorer.page_matches_metadata_context(text, context))
    if hits:
        return 8, f"context_match:{hits}/{len(metadata_contexts)}"
    conflicting_contexts = [
        context for context in config.METADATA_CONTEXTS
        if context not in metadata_contexts
        and scorer.metadata_context_occurrence_count(text, context) >= 2
    ]
    if conflicting_contexts:
        return -20, (
            f"metadata_context_conflict:{'+'.join(metadata_contexts)}/"
            f"{'+'.join(conflicting_contexts)}"
        )
    return 0, f"metadata_context_not_observed:0/{len(metadata_contexts)}"


def _email_domain_bonus(website: str, email: str) -> tuple[int, str]:
    if not email:
        return 0, "no_email"
    website_root = scorer.compact_domain_core(website)
    email_root = scorer.compact_domain_core(_email_domain(email))
    if email_root and website_root and (email_root == website_root or email_root in website_root or website_root in email_root):
        return 10, "email_domain_match"
    return -12, "email_domain_mismatch"


def _cross_domain_email_is_safe_first_party(
    evaluation: dict,
    identity_verified: bool,
) -> bool:
    """Allow a cross-domain mailbox only when the verified official site publishes it.

    Corporate groups and business units commonly use a shared mailbox domain.
    The mailbox domain is therefore not an ownership gate by itself; the
    source page, DNS verification and already-established website identity are.
    """
    if not evaluation.get("email_failed") or not identity_verified:
        return False
    selected_email = str(evaluation.get("email", "") or "")
    source_url = str(evaluation.get("email_source_url", "") or "")
    website = str(evaluation.get("crawl_result", {}).get("url", "") or "")
    if not selected_email or not source_url or not website:
        return False
    if evaluation.get("email_verification") != "verified":
        return False
    if not scorer.same_registrable_domain(source_url, website):
        return False
    return _email_domain_bonus(website, selected_email)[1] == "email_domain_mismatch"


def _email_failure_blocks_publication(
    evaluation: dict,
    identity_verified: bool,
) -> bool:
    if not evaluation.get("email_failed"):
        return False
    if not _cross_domain_email_is_safe_first_party(evaluation, identity_verified):
        return True
    marker = "cross_domain_email_accepted_from_verified_official_page"
    if marker not in evaluation["reasons"]:
        evaluation["reasons"].append(marker)
    return False


def _fair_phone_reference_reasons(
    metadata: dict | None,
    official_website: str,
    official_phones: list[str],
) -> list[str]:
    """Compare a fair phone as non-authoritative QA when its website agrees.

    The fair value is never copied into output. A difference is informational:
    contacts extracted from the verified official website remain publishable.
    """
    if not metadata:
        return []
    fair_website = str(metadata.get("website", "") or "")
    fair_phone = str(metadata.get("listed_phone", "") or "")
    if not fair_website or not fair_phone:
        return []
    if not scorer.same_registrable_domain(fair_website, official_website):
        return []
    normalized_reference = phone.normalize_phone(fair_phone)
    if not normalized_reference:
        return []
    if normalized_reference in official_phones:
        return ["fair_phone_reference_match"]
    if official_phones:
        return ["fair_phone_reference_differs_nonblocking"]
    return ["fair_phone_reference_only_not_published"]


def _is_ambiguous_company_name(company: str) -> bool:
    tokens = scorer.distinctive_tokens(company)
    if not tokens:
        return True
    if len(tokens) <= 2:
        return True
    ambiguous = {scorer.normalize_text(word) for word in config.AMBIGUOUS_BRAND_WORDS}
    hits = sum(1 for token in tokens if token in ambiguous or len(token) <= 3)
    return hits >= max(1, len(tokens) // 2)


def _strong_contact_evidence(selected_email: str, normalized_phones: list[str], website: str) -> bool:
    if selected_email:
        email_bonus, _ = _email_domain_bonus(website, selected_email)
        if email_bonus > 0:
            return True
    return bool(normalized_phones and selected_email)


def _is_hard_context_failure(evaluation: dict) -> bool:
    if not evaluation["context_failed"]:
        return False

    if evaluation.get("llm_arbiter_evidence", {}).get("verdict") == "match":
        return False

    reasons = evaluation["reasons"]
    assessment = evaluation.get("identity_assessment") or identity.assess(
        evaluation.get("candidate", {}).get("_identity_company", ""),
        evaluation.get("candidate", {}),
        reasons,
        evaluation.get("structured_identity", {}),
    )
    if any(reason.startswith(("context_conflict:", "metadata_context_conflict:")) for reason in reasons):
        company = evaluation.get("candidate", {}).get("_identity_company", "")
        candidate = evaluation.get("candidate", {})
        exact_compound_identity = (
            len(scorer.domain_identity_tokens(company)) >= 2
            and _exact_brand_domain(company, candidate)
            and any(reason.startswith((
                "structured_identity_medium:", "structured_identity_strong:",
                "legal_name_phrase_match:", "legal_name_full_match:",
                "legal_name_ownership_match:",
            )) for reason in reasons)
        )
        # Sector context is a homonym guard for short/generic brands. It must
        # not override a compound domain backed by first-party legal identity.
        return not exact_compound_identity
    if assessment.get("strong_first_party_bundle"):
        return False
    # Fair, directory and listing metadata is discovery-only. Failure to find
    # its wording on a first-party site can never be a hard conflict.
    if any(reason.startswith((
        "metadata_context_missing:", "metadata_context_not_observed:",
    )) for reason in reasons):
        return False
    return True


def _has_trusted_website_evidence(
    candidate: dict,
    reasons: list[str],
    *,
    unique_candidate: bool = False,
) -> bool:
    """Verify independent evidence, or a strong first-party bundle after uniqueness."""
    if "country_identity_unproven" in reasons:
        return False
    assessment = identity.assess(
        candidate.get("_identity_company", ""), candidate, reasons,
        candidate.get("_structured_identity", {}),
    )
    return bool(
        assessment["publishable"]
        or (unique_candidate and assessment.get("strong_first_party_bundle"))
    )


def _unsafe_context_identity(company: str, evaluation: dict) -> bool:
    """Reject ambiguous search matches when crawled sector evidence contradicts them."""
    if not _is_hard_context_failure(evaluation):
        return False
    if _has_trusted_website_evidence(evaluation["candidate"], evaluation["reasons"]):
        return False
    brand_tokens = scorer.domain_identity_tokens(company)
    domain_core = scorer.compact_domain_core(evaluation["crawl_result"].get("url", ""))
    exact_brand_domain = bool(brand_tokens) and domain_core == "".join(brand_tokens)
    explicit_company_context = bool(scorer.context_tokens(company))
    unsafe = not exact_brand_domain or (len(brand_tokens) == 1 and explicit_company_context)
    if unsafe and "unsafe_context_identity" not in evaluation["reasons"]:
        evaluation["reasons"].append("unsafe_context_identity")
    return unsafe


def _clear_unpublished_contacts(row: dict) -> None:
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


def _apply_risk_caps(
    company: str,
    candidate: dict,
    crawl_result: dict,
    selected_email: str,
    normalized_phones: list[str],
    score: int,
    reasons: list[str],
) -> int:
    if not _is_ambiguous_company_name(company):
        return score

    page_identity = next((reason for reason in reasons if reason.startswith("page_identity_")), "")
    context_ok = any(reason.startswith("context_match:") for reason in reasons)
    strong_contact = _strong_contact_evidence(selected_email, normalized_phones, crawl_result["url"])
    if context_ok and strong_contact:
        return score
    if page_identity.startswith("page_identity_strong") and strong_contact:
        return min(score, config.SAFE_OK_MIN_SCORE)

    # An exact multi-token brand domain with structured/legal first-party
    # identity is not the generic homonym case this cap is meant to catch.
    exact_compound_domain = (
        len(scorer.domain_identity_tokens(company)) >= 2
        and _exact_brand_domain(company, candidate)
    )
    strong_structured_or_legal = any(reason.startswith((
        "structured_identity_medium:", "structured_identity_strong:",
        "legal_name_phrase_match:", "legal_name_full_match:",
        "legal_name_ownership_match:",
    )) for reason in reasons)
    unresolved_owner = any(reason.startswith("structured_identity_unmatched:") for reason in reasons) and not any(
        reason.startswith("legal_name_ownership_match:") for reason in reasons
    )
    if exact_compound_domain and page_identity.startswith("page_identity_strong") and strong_structured_or_legal and not unresolved_owner:
        reasons.append("ambiguous_name_risk_resolved_by_exact_compound_identity")
        return score

    reasons.append("ambiguous_name_risk_cap")
    return min(score, config.REVIEW_SCORE)


def _score_candidate_with_site(company: str, candidate: dict, crawl_result: dict, selected_email: str, normalized_phones: list[str], metadata: dict | None = None) -> tuple[int, list[str]]:
    reasons = [candidate.get("reason", "")]
    page_bonus, page_reason = _page_identity_score(company, crawl_result["pages"])
    context_bonus, context_reason = _page_context_score(company, crawl_result["pages"], metadata)
    email_bonus, email_reason = _email_domain_bonus(crawl_result["url"], selected_email)
    structured_bonus, structured_reason, _ = _structured_identity_score(company, crawl_result["pages"])
    legal_name_bonus, legal_name_reason = _legal_name_identity_score(company, crawl_result["pages"])
    country_bonus, country_reason = _country_identity_score(crawl_result, normalized_phones)
    exact_compound_identity = (
        context_bonus < 0
        and len(scorer.domain_identity_tokens(company)) >= 2
        and _exact_brand_domain(company, candidate)
        and (
            structured_reason.startswith(("structured_identity_medium:", "structured_identity_strong:"))
            or legal_name_reason.startswith((
                "legal_name_phrase_match:", "legal_name_full_match:",
                "legal_name_ownership_match:",
            ))
        )
    )
    if exact_compound_identity:
        context_bonus = 0
        context_reason = "metadata_context_conflict_overridden_by_exact_compound_identity"
    reasons.extend([page_reason, context_reason, email_reason, structured_reason, legal_name_reason, country_reason])
    if crawl_result.get("tls_insecure"):
        reasons.append("tls_insecure_transport")
    # Website selection is identity-first. Email-domain agreement describes
    # contact quality, but cannot make an unrelated website rank higher.
    final_score = max(0, min(
        100,
        int(candidate["score"]) + page_bonus + context_bonus + structured_bonus + legal_name_bonus + country_bonus,
    ))
    final_score = _apply_risk_caps(
        company, candidate, crawl_result, selected_email, normalized_phones, final_score, reasons,
    )
    has_contact = bool(selected_email or normalized_phones)
    if not has_contact:
        reasons.append("no_tr_contact_or_usable_email")
    if context_bonus < 0:
        reasons.append("context_gate_failed")
    if email_bonus < 0:
        reasons.append("email_gate_failed")
    return final_score, reasons


def _evaluate_candidate(
    company: str,
    candidate: dict,
    metadata: dict | None = None,
    crawl_profile: str = "full",
    verify_email_domain: bool = True,
    evidence_scopes: tuple[str, ...] | list[str] | None = None,
) -> dict:
    candidate["_identity_company"] = company
    crawl_result = crawler.fetch_site(
        candidate["url"], candidate.get("_contact_seed_urls", []),
        profile=crawl_profile, evidence_scopes=evidence_scopes,
        identity_seed_urls=candidate.get("_identity_seed_urls", []),
    )
    if not crawl_result["pages"]:
        redirect_target = crawl_result.get("redirect_target", "")
        if redirect_target and not candidate.get("_redirect_depth"):
            redirect_domain = scorer.normalize_domain(redirect_target)
            if redirect_domain and scorer.is_foreign_country_domain(redirect_domain):
                return {
                    "candidate": candidate,
                    "crawl_result": {**crawl_result, "redirect_target": redirect_target},
                    "email": "", "email_verification": "not_checked",
                    "email_verification_reason": "foreign_country_redirect",
                    "phone": "", "final_score": 0,
                    "reasons": [f"foreign_country_redirect_rejected:{redirect_target}"],
                    "has_contact": False, "context_failed": True, "email_failed": False,
                }
            if redirect_domain and not scorer.is_excluded_domain(redirect_domain):
                redirected = {
                    **candidate,
                    "domain": redirect_domain,
                    "url": redirect_target,
                    "reason": f"{candidate.get('reason', '')}; explicit_cross_domain_redirect:{redirect_target}",
                    "_redirect_depth": 1,
                }
                return _evaluate_candidate(
                    company, redirected, metadata,
                    crawl_profile=crawl_profile, verify_email_domain=verify_email_domain,
                    evidence_scopes=evidence_scopes,
                )
        return {
            "candidate": candidate,
            "crawl_result": crawl_result,
            "email": "",
            "email_verification": "not_checked",
            "email_verification_reason": "website_fetch_failed",
            "phone": "",
            "final_score": 0,
            "reasons": [crawl_result.get("error", "website_fetch_failed")],
            "has_contact": False,
            "context_failed": False,
            "email_failed": False,
        }

    email_records: list[dict] = []
    phone_records: list[dict] = []
    if crawl_profile == "full":
        for page in crawl_result["pages"]:
            contacts = extractor.extract_contact_records(
                page["html"], page["url"], page.get("retrieval_method", "unknown"),
            )
            email_records.extend(contacts["emails"])
            phone_records.extend(contacts["phones"])

    ranked_email_records = contact_decision.rank_email_records(
        company, crawl_result["url"], email_records, _email_is_usable,
    )
    verification_limit = max(config.MAX_EMAIL_CANDIDATE_VERIFICATIONS, 1)
    for index, record in enumerate(ranked_email_records):
        verification = (
            email_verifier.verify_email(record["value"])
            if verify_email_domain and index < verification_limit
            else {"status": "not_checked", "reason": "identity_phase"}
        )
        record["verification_status"] = verification["status"]
        record["verification_reason"] = verification["reason"]
        email_domain = record["value"].rsplit("@", 1)[-1]
        record["company_domain_identity"] = bool(
            scorer.domain_identity_match(company, email_domain)[0]
            or scorer.public_brand_domain_match(company, email_domain)
        )

    ranked_phone_records = _select_phone_records(phone_records)
    contact_policy = contact_publication.filter_records(
        crawl_result["url"], ranked_email_records, ranked_phone_records,
    )
    eligible_email_records = contact_policy["eligible_email_records"]
    eligible_phone_records = contact_policy["eligible_phone_records"]
    selected_email_record = eligible_email_records[0] if eligible_email_records else {}
    email_verification = {
        "status": selected_email_record.get("verification_status", "not_checked"),
        "reason": selected_email_record.get(
            "verification_reason", "no_publishable_email",
        ),
    }
    selected_email = selected_email_record.get("value", "")
    email_source = "website" if selected_email else ""
    email_source_url = selected_email_record.get("source_url", "")
    normalized_phones = [record["value"] for record in eligible_phone_records]
    phone_source = "website" if normalized_phones else ""
    # Contact values must come from the crawled official site.  Third-party
    # directory data (such as Google Places or Hunter) is not published.
    final_score, reasons = _score_candidate_with_site(company, candidate, crawl_result, selected_email, normalized_phones, metadata)
    reasons.extend(_fair_phone_reference_reasons(metadata, crawl_result["url"], normalized_phones))
    places_phone = phone.normalize_phone(
        str(candidate.get("external_phone", "") or "")
    )
    places_name_matches = any(
        scorer.business_name_identity_match(
            company, str(item.get("name", "") or "")
        )
        for item in candidate.get("_google_places_evidence", [])
    )
    if places_phone and places_phone in normalized_phones and places_name_matches:
        reasons.append("google_places_first_party_phone_match")
    context_failed = "context_gate_failed" in reasons
    email_failed = "email_gate_failed" in reasons

    structured_identity = _structured_identity_score(company, crawl_result["pages"])[2]
    candidate["_structured_identity"] = structured_identity
    semantic_identity = entity_semantics.assess(
        company, metadata, crawl_result["pages"],
    )
    if semantic_identity["decision"] == "match":
        reasons.append(
            f"semantic_entity_type_match:{','.join(semantic_identity['matches'])}"
        )
    elif semantic_identity["decision"] == "conflict":
        reasons.append(
            f"context_conflict:semantic_entity_type:"
            f"{','.join(semantic_identity['conflicts'])}"
        )
    identity_assessment = identity.assess(company, candidate, reasons, structured_identity)
    reasons.append(
        f"identity_evidence:{identity_assessment['support_count']};"
        f"decision:{identity_assessment['decision']}"
    )
    email_decision = next(
        (
            item for item in contact_policy["emails"]
            if item["eligible"] and item["value"] == selected_email
        ),
        {},
    )
    phone_decision = next(
        (
            item for item in contact_policy["phones"]
            if item["eligible"] and item["value"] == (
                normalized_phones[0] if normalized_phones else ""
            )
        ),
        {},
    )
    return {
        "candidate": candidate,
        "crawl_result": crawl_result,
        "email": selected_email,
        "email_source": email_source,
        "email_source_url": email_source_url,
        "email_selection_reason": selected_email_record.get("selection_reason", ""),
        "email_retrieval_method": selected_email_record.get("retrieval_method", "http"),
        "alternative_emails": [
            record["value"] for record in eligible_email_records
            if record["value"] != selected_email
        ],
        "alternative_email_records": [
            record for record in eligible_email_records
            if record["value"] != selected_email
        ],
        "email_verification": email_verification["status"],
        "email_verification_reason": email_verification["reason"],
        "phone": normalized_phones[0] if normalized_phones else "",
        "phone_source": phone_source if normalized_phones else "",
        "phone_source_url": eligible_phone_records[0]["source_url"] if eligible_phone_records else "",
        "phone_label": eligible_phone_records[0]["label"] if eligible_phone_records else "",
        "phone_selection_reason": eligible_phone_records[0].get("selection_reason", "") if eligible_phone_records else "",
        "phone_retrieval_method": eligible_phone_records[0].get("retrieval_method", "http") if eligible_phone_records else "",
        "alternative_phones": eligible_phone_records[1:],
        "contact_publication": contact_policy,
        "email_publication_status": "allowed" if selected_email else "suppressed",
        "email_publication_reason": email_decision.get(
            "reason", "no_eligible_first_party_email",
        ),
        "phone_publication_status": "allowed" if normalized_phones else "suppressed",
        "phone_publication_reason": phone_decision.get(
            "reason", "no_eligible_first_party_phone",
        ),
        "final_score": final_score,
        "reasons": reasons,
        "has_contact": bool(selected_email or normalized_phones),
        "context_failed": context_failed,
        "email_failed": email_failed,
        "structured_identity": structured_identity,
        "semantic_identity": semantic_identity,
        "identity_assessment": identity_assessment,
    }


def _contact_output_fields(evaluation: dict) -> dict:
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


def _evaluation_evidence(evaluation: dict) -> dict:
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
        "contacts": _contact_output_fields(evaluation),
    }


def _complete_resolution_evidence(
    company: str,
    metadata: dict | None,
    candidates: list[dict],
    evaluations: list[dict],
    resolution: entity_resolution.Resolution,
) -> tuple[list[dict], entity_resolution.Resolution, evidence_acquisition.EvidenceState]:
    """Run bounded, gap-specific search/crawl rounds for an unresolved entity."""
    current = evidence_acquisition.analyze(
        company,
        evaluations,
        resolution_status=resolution.status,
        metadata=metadata,
        query_limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
    )
    previous: evidence_acquisition.EvidenceState | None = None
    rounds: list[dict] = []
    attempted_scopes_by_domain: dict[str, set[str]] = {}
    for round_number in range(1, config.MAX_AUTONOMOUS_RESOLUTION_ROUNDS + 1):
        if not evidence_acquisition.should_continue(
            previous,
            current,
            round_number - 1,
            config.MAX_AUTONOMOUS_RESOLUTION_ROUNDS,
        ):
            break
        runtime.record("autonomy.rounds")
        targeted = search.find_targeted_candidates(
            company,
            metadata,
            current.search_queries,
            limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
        )
        known_domains = {
            scorer.normalize_domain(item.get("url", "")) for item in candidates
        }
        for candidate in targeted:
            domain = scorer.normalize_domain(candidate.get("url", ""))
            if domain and domain not in known_domains:
                candidates.append(candidate)
                known_domains.add(domain)
        candidates[:] = search.rank_candidates(candidates)

        evaluation_by_domain = {
            scorer.normalize_domain(item.get("candidate", {}).get("url", "")): item
            for item in evaluations
        }
        candidate_by_domain = {
            scorer.normalize_domain(candidate.get("url", "")): candidate
            for candidate in candidates
            if candidate.get("role") not in identity.EXCLUDED_ROLES
        }
        priority_domains = [
            scorer.normalize_domain(
                item.get("candidate", {}).get("url", "")
            )
            for item in resolution.contenders
        ]
        priority_domains.extend(
            domain for domain in candidate_by_domain
            if domain not in evaluation_by_domain
        )
        current_scopes = set(current.crawl_scopes)
        selected_domains = list(dict.fromkeys(
            domain for domain in priority_domains
            if domain and (
                domain not in attempted_scopes_by_domain
                or (
                    current_scopes
                    and not current_scopes.issubset(
                        attempted_scopes_by_domain[domain]
                    )
                )
            )
        ))[:config.MAX_TARGETED_CRAWLS_PER_ROUND]
        if not selected_domains:
            break
        for domain in selected_domains:
            runtime.record("autonomy.targeted_crawls")
            attempted_scopes_by_domain.setdefault(domain, set()).update(
                current_scopes
            )
            candidate = candidate_by_domain.get(domain)
            if candidate is None:
                candidate = evaluation_by_domain[domain]["candidate"]
            evaluation_by_domain[domain] = _evaluate_candidate_with_stage(
                company,
                candidate,
                metadata,
                evidence_scopes=current.crawl_scopes,
            )
        evaluations = list(evaluation_by_domain.values())
        evaluations.sort(
            key=lambda item: _evaluation_rank_key(company, item),
            reverse=True,
        )
        previous = current
        resolution = entity_resolution.resolve_candidates(company, evaluations)
        current = evidence_acquisition.analyze(
            company,
            evaluations,
            resolution_status=resolution.status,
            metadata=metadata,
            query_limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
        )
        rounds.append({
            "round": round_number,
            "gaps_before": sorted(previous.gaps),
            "gaps_after": sorted(current.gaps),
            "crawl_scopes": list(previous.crawl_scopes),
            "queries": list(previous.search_queries),
            "evaluated_domains": selected_domains,
            "resolution_status": resolution.status,
        })
        if resolution.status == "resolved":
            break
    automation = {
        "rounds": rounds,
        "remaining_evidence_gaps": sorted(current.gaps),
        "terminal_reason": (
            "resolved_after_evidence_completion"
            if resolution.status == "resolved" and rounds
            else current.terminal_reason
        ),
    }
    for evaluation in evaluations:
        evaluation["_automation"] = automation
    return evaluations, resolution, current


def _try_linkedin_company_corroboration(
    company: str,
    evaluations: list[dict],
    resolution: entity_resolution.Resolution,
) -> entity_resolution.Resolution:
    """Ask LinkedIn only for existing candidates that are still stuck."""
    if resolution.status != "unresolved" or not evaluations:
        return resolution
    profile = entity_resolution.build_target_profile(company)
    targets = [
        item for item in evaluations
        if item.get("crawl_result", {}).get("pages")
        and not entity_resolution.fingerprint(profile, item).candidate_ready
    ]
    for target in targets:
        linkedin_evidence = linkedin_company.corroborate(company, target)
        if not linkedin_evidence:
            continue
        target["linkedin_company_evidence"] = linkedin_evidence
        target.setdefault("candidate", {})["_linkedin_company_evidence"] = (
            linkedin_evidence
        )
        if linkedin_evidence.get("verified"):
            break
    return entity_resolution.resolve_candidates(company, evaluations)


def _llm_context_conflict_candidate(
    company: str,
    metadata: dict | None,
    evaluation: dict,
) -> bool:
    """Select only review-bound context conflicts with meaningful identity proof."""
    reasons = evaluation.get("reasons", [])
    strong_independent_evidence = any(str(reason).startswith((
        "structured_identity_medium:",
        "structured_identity_strong:",
        "legal_name_phrase_match:",
        "legal_name_full_match:",
        "legal_name_ownership_match:",
        "email_domain_match",
    )) for reason in reasons)
    sector_context = str((metadata or {}).get("sector", "") or "").strip()
    unresolved_generic_sector = bool(
        sector_context
        and _is_ambiguous_company_name(company)
        and any(str(reason).startswith((
            "metadata_context_not_observed:",
            "metadata_context_missing:",
            "metadata_context_conflict_overridden_by_exact_compound_identity",
            "context_match:",
        )) for reason in reasons)
    )
    return bool(
        evaluation.get("crawl_result", {}).get("pages")
        and strong_independent_evidence
        and (
            _is_hard_context_failure(evaluation)
            or unresolved_generic_sector
        )
    )


def _llm_arbiter_metadata(metadata: dict | None, company: str) -> tuple[str, str]:
    metadata = metadata or {}
    legal_title = next((
        str(metadata.get(key, "") or "").strip()
        for key in ("legal_name", "legal_title", "legal_company_name", "title")
        if str(metadata.get(key, "") or "").strip()
    ), company)
    sector_context = " | ".join(dict.fromkeys(
        str(metadata.get(key, "") or "").strip()
        for key in ("sector", "category", "fair", "trade_show", "description")
        if str(metadata.get(key, "") or "").strip()
    ))
    return legal_title, sector_context


def _try_llm_arbitration(
    company: str,
    metadata: dict | None,
    evaluations: list[dict],
    resolution: entity_resolution.Resolution,
) -> entity_resolution.Resolution:
    """Arbitrate only close or context-conflicted candidates, then re-resolve."""
    if not evaluations or not llm_arbiter.available():
        return resolution
    targets: dict[int, tuple[dict, set[str]]] = {}
    plausible = [
        item for item in evaluations
        if item.get("crawl_result", {}).get("pages")
        and item.get("identity_assessment", {}).get("provisionally_publishable")
        and not item.get("_llm_arbiter_rejected")
    ]
    for left_index, left in enumerate(plausible[:4]):
        for right in plausible[left_index + 1:4]:
            if _close_identity_margin_conflict(company, left, right):
                targets.setdefault(id(left), (left, set()))[1].add(
                    "close_identity_margin_conflict"
                )
                targets.setdefault(id(right), (right, set()))[1].add(
                    "close_identity_margin_conflict"
                )
    for evaluation in evaluations:
        if evaluation.get("_llm_arbiter_rejected"):
            continue
        if _llm_context_conflict_candidate(company, metadata, evaluation):
            targets.setdefault(id(evaluation), (evaluation, set()))[1].add(
                "sector_context_conflict_with_identity_evidence"
            )
            break
    if not targets:
        return resolution
    legal_title, sector_context = _llm_arbiter_metadata(metadata, company)
    decisions: list[dict] = []
    for evaluation, triggers in targets.values():
        candidate = evaluation.get("candidate", {})
        domain = scorer.normalize_domain(candidate.get("url", ""))
        result = llm_arbiter.arbitrate(
            company,
            legal_title,
            sector_context,
            domain,
            llm_arbiter.summarize_pages(
                evaluation.get("crawl_result", {}).get("pages", [])
            ),
        )
        arbiter_evidence = {
            **result,
            "candidate_domain": domain,
            "triggers": sorted(triggers),
        }
        evaluation["llm_arbiter_evidence"] = arbiter_evidence
        candidate["_llm_arbiter_evidence"] = arbiter_evidence
        decisions.append(arbiter_evidence)
        verdict = result.get("verdict")
        if verdict == "no_match":
            evaluation["_llm_arbiter_rejected"] = True
            evaluation.setdefault("reasons", []).append(
                f"llm_arbiter_no_match:{result.get('reason', '')}"
            )
        elif verdict == "match":
            evaluation.setdefault("reasons", []).append(
                f"llm_arbiter_match:{result.get('reason', '')}"
            )
        else:
            evaluation.setdefault("reasons", []).append(
                f"llm_arbiter_uncertain:{result.get('reason', '')}"
            )
    for evaluation in evaluations:
        evaluation["_llm_arbiter_decisions"] = decisions
    return entity_resolution.resolve_candidates(company, evaluations)


def _evaluate_llm_rejection_fallbacks(
    company: str,
    metadata: dict | None,
    ranked_identity: list[dict],
    evaluations: list[dict],
) -> list[dict]:
    """Continue the bounded candidate waterfall after an arbiter rejection."""
    rejected_count = sum(
        bool(item.get("_llm_arbiter_rejected")) for item in evaluations
    )
    if not rejected_count:
        return evaluations
    evaluated_domains = {
        scorer.normalize_domain(item.get("candidate", {}).get("url", ""))
        for item in evaluations
    }
    fallbacks = [
        item for item in ranked_identity
        if (
            item.get("crawl_result", {}).get("pages")
            and scorer.normalize_domain(
                item.get("candidate", {}).get("url", "")
            ) not in evaluated_domains
            and (
                _full_crawl_worthy(company, item)
                or item.get("identity_assessment", {}).get(
                    "provisionally_publishable"
                )
            )
        )
    ][:rejected_count]
    for light_evaluation in fallbacks:
        full_evaluation = _evaluate_candidate_with_stage(
            company, light_evaluation["candidate"], metadata,
        )
        if not full_evaluation.get("crawl_result", {}).get("pages"):
            continue
        evaluations.append(_preserve_identity_phase_evidence(
            full_evaluation, light_evaluation,
        ))
        runtime.record("pipeline.llm_arbiter_fallback_candidates")
    return sorted(
        evaluations,
        key=lambda item: _evaluation_rank_key(company, item),
        reverse=True,
    )


def _evaluate_candidate_with_stage(
    company: str,
    candidate: dict,
    metadata: dict | None = None,
    crawl_profile: str = "full",
    verify_email_domain: bool = True,
    evidence_scopes: tuple[str, ...] | list[str] | None = None,
) -> dict:
    if evidence_scopes:
        evaluation = _evaluate_candidate(
            company, candidate, metadata, crawl_profile, verify_email_domain,
            evidence_scopes=evidence_scopes,
        )
    else:
        evaluation = _evaluate_candidate(
            company, candidate, metadata, crawl_profile, verify_email_domain,
        )
    history = candidate.setdefault("_stage_history", [])
    stage = "identity_evaluated" if crawl_profile == "identity" else "full_evaluated"
    history.append({
        "stage": stage,
        "reachable": bool(evaluation.get("crawl_result", {}).get("pages")),
        "final_score": evaluation.get("final_score", 0),
        "publishable_identity": bool(
            evaluation.get("identity_assessment", {}).get("provisionally_publishable")
        ),
        "reasons": evaluation.get("reasons", []),
    })
    return evaluation


def _preserve_identity_phase_evidence(full_evaluation: dict, identity_evaluation: dict) -> dict:
    """Do not let contact-page composition erase stronger same-domain identity.

    A full crawl may contain mostly contact/product pages while the dedicated
    identity crawl contains the legal/ownership page.  The full crawl may add a
    hard conflict, but in the absence of one it can only enrich contacts; it
    cannot downgrade already established first-party identity.
    """
    full_assessment = full_evaluation.get("identity_assessment", {})
    identity_assessment = identity_evaluation.get("identity_assessment", {})
    if full_assessment.get("conflicts"):
        return full_evaluation
    # A light crawl may expose useful legal text, but a merely provisional
    # single-site bundle must not override a fuller crawl which could not
    # establish independent identity support.
    if not identity_assessment.get("publishable"):
        return full_evaluation
    full_strength = (
        1 if full_assessment.get("publishable") else 0,
        full_assessment.get("support_count", 0),
    )
    identity_strength = (
        1 if identity_assessment.get("publishable") else 0,
        identity_assessment.get("support_count", 0),
    )
    if identity_strength <= full_strength:
        return full_evaluation

    identity_prefixes = (
        "page_identity_", "structured_identity_", "legal_name_phrase_",
        "legal_name_full_", "legal_name_ownership_", "identity_evidence:",
    )
    contact_reasons = [
        reason for reason in full_evaluation.get("reasons", [])
        if not reason.startswith(identity_prefixes)
    ]
    preserved_reasons = [
        reason for reason in identity_evaluation.get("reasons", [])
        if reason.startswith(identity_prefixes)
    ]
    full_evaluation["reasons"] = [*contact_reasons, *preserved_reasons]
    full_evaluation["identity_assessment"] = identity_assessment
    full_evaluation["structured_identity"] = identity_evaluation.get(
        "structured_identity", full_evaluation.get("structured_identity", {}),
    )
    full_evaluation["final_score"] = max(
        full_evaluation.get("final_score", 0), identity_evaluation.get("final_score", 0),
    )
    history = full_evaluation.get("candidate", {}).get("_stage_history", [])
    if history and history[-1].get("stage") == "full_evaluated":
        history[-1]["publishable_identity"] = bool(
            identity_assessment.get("provisionally_publishable")
        )
        history[-1]["identity_preserved_from_light_crawl"] = True
    return full_evaluation


def _first_party_alias_candidates(
    company: str,
    evaluations: list[dict],
    existing_domains: set[str],
) -> list[dict]:
    """Add direct structured cross-domain links to discovery, never authority."""
    aliases: list[dict] = []
    for evaluation in evaluations:
        assessment = evaluation.get("identity_assessment", {})
        if assessment.get("conflicts") or "first_party_identity" not in assessment.get("support_keys", []):
            continue
        source_url = evaluation.get("crawl_result", {}).get("url", "")
        source_domain = scorer.normalize_domain(source_url)
        structured = evaluation.get("structured_identity", {})
        for field in ("same_as", "urls"):
            for linked_url in structured.get(field, []):
                linked_domain = scorer.normalize_domain(linked_url)
                if (
                    not linked_domain
                    or linked_domain in existing_domains
                    or scorer.same_registrable_domain(linked_domain, source_domain)
                    or scorer.is_excluded_domain(linked_domain)
                    or scorer.is_foreign_country_domain(linked_domain)
                ):
                    continue
                candidate_url = linked_url if "://" in linked_url else f"https://{linked_url}"
                aliases.append({
                    "domain": linked_domain,
                    "url": candidate_url,
                    "score": config.MEDIUM_CONFIDENCE_SCORE,
                    "title": "",
                    "snippet": "",
                    "query": "first_party_alias",
                    "rank": len(aliases) + 1,
                    "reason": f"direct_first_party_structured_link:{field}; discovery_only_not_identity_authority",
                    "role": "company_candidate",
                    "_first_party_alias_evidence": 1,
                    "_alias_source_url": source_url,
                    "_official_query_evidence": 0,
                })
                existing_domains.add(linked_domain)
                if len(aliases) >= config.MAX_FIRST_PARTY_ALIAS_CANDIDATES:
                    return aliases
    return aliases


def _first_party_contact_alias_candidates(
    company: str,
    evaluations: list[dict],
    existing_domains: set[str],
) -> list[dict]:
    """Turn a verified corporate mailbox domain into discovery, never authority.

    The source site must already contain conflict-free first-party identity.
    The linked domain still passes the normal crawl, identity, homonym, country
    and publication gates before any website or contact can be published.
    """
    aliases: list[dict] = []
    non_corporate = {
        scorer.normalize_domain(domain) for domain in config.NON_CORPORATE_EMAIL_DOMAINS
    }
    for evaluation in evaluations:
        assessment = evaluation.get("identity_assessment", {})
        if (
            assessment.get("conflicts")
            or "first_party_identity" not in assessment.get("support_keys", [])
            or evaluation.get("email_verification") != "verified"
        ):
            continue
        source_url = evaluation.get("crawl_result", {}).get("url", "")
        source_domain = scorer.normalize_domain(source_url)
        linked_domain = _email_domain(evaluation.get("email", ""))
        if (
            not linked_domain
            or linked_domain in existing_domains
            or linked_domain in non_corporate
            or scorer.same_registrable_domain(linked_domain, source_domain)
            or scorer.is_excluded_domain(linked_domain)
            or scorer.is_foreign_country_domain(linked_domain)
        ):
            continue
        aliases.append({
            "domain": linked_domain,
            "url": f"https://{linked_domain}",
            "score": config.MEDIUM_CONFIDENCE_SCORE,
            "title": "",
            "snippet": "",
            "query": "first_party_contact_alias",
            "rank": len(aliases) + 1,
            "reason": (
                "verified_first_party_cross_domain_email_discovery; "
                "discovery_only_not_identity_authority"
            ),
            "role": "company_candidate",
            "_first_party_alias_evidence": 1,
            "_alias_source_url": source_url,
            "_official_query_evidence": 0,
        })
        existing_domains.add(linked_domain)
        if len(aliases) >= config.MAX_FIRST_PARTY_CONTACT_ALIAS_CANDIDATES:
            break
    return aliases


def _official_family_evidence(first: dict, second: dict, company: str = "") -> dict:
    """Build cross-domain relationship edges without assuming similar TLDs are related."""
    first_candidate = first.get("candidate", {})
    second_candidate = second.get("candidate", {})
    edges: list[str] = []
    conflicts: list[str] = []
    first_entity = first_candidate.get("_entity_id", "")
    second_entity = second_candidate.get("_entity_id", "")
    if first_entity and first_entity == second_entity:
        edges.append("same_verified_entity")

    first_domain = scorer.normalize_domain(first_candidate.get("url", ""))
    second_domain = scorer.normalize_domain(second_candidate.get("url", ""))
    first_links = {
        scorer.normalize_domain(url)
        for url in [
            *first.get("structured_identity", {}).get("urls", []),
            *first.get("structured_identity", {}).get("same_as", []),
        ]
        if scorer.normalize_domain(url)
    }
    second_links = {
        scorer.normalize_domain(url)
        for url in [
            *second.get("structured_identity", {}).get("urls", []),
            *second.get("structured_identity", {}).get("same_as", []),
        ]
        if scorer.normalize_domain(url)
    }
    if (second_domain and second_domain in first_links) or (first_domain and first_domain in second_links):
        edges.append("first_party_domain_link")

    first_structured = first.get("structured_identity", {})
    second_structured = second.get("structured_identity", {})
    typed_edges = relationship_graph.typed_domain_edges(
        first_domain, second_domain, first_structured, second_structured,
    )
    edges.extend(typed_edges)
    first_ids = {scorer.normalize_text(value) for value in first_structured.get("identifiers", []) if value}
    second_ids = {scorer.normalize_text(value) for value in second_structured.get("identifiers", []) if value}
    if first_ids and second_ids:
        if first_ids & second_ids:
            edges.append("shared_legal_identifier")
        else:
            conflicts.append("different_legal_identifiers")

    first_addresses = {scorer.normalize_text(value) for value in first_structured.get("addresses", []) if value}
    second_addresses = {scorer.normalize_text(value) for value in second_structured.get("addresses", []) if value}
    if first_addresses & second_addresses:
        edges.append("shared_structured_address")

    # A corporate site and its shop/catalogue frequently use different domains.
    # Link them only when the relationship is asserted by first-party contacts:
    # one site publishes the other site's mailbox domain and both publish the
    # same Turkish phone.  Requiring both signals prevents a shared agency email
    # or call-centre number from joining unrelated businesses.
    first_email_domain = _email_domain(first.get("email", ""))
    second_email_domain = _email_domain(second.get("email", ""))
    cross_domain_email = (
        (first_email_domain and first_email_domain == second_domain)
        or (second_email_domain and second_email_domain == first_domain)
    )
    shared_phone = bool(
        first.get("phone") and second.get("phone")
        and first.get("phone") == second.get("phone")
    )
    if cross_domain_email and shared_phone:
        edges.extend(["cross_domain_first_party_email", "shared_phone"])
    elif shared_phone:
        edges.append("shared_phone")

    if company:
        first_core = scorer.compact_domain_core(first_domain)
        second_core = scorer.compact_domain_core(second_domain)
        if (
            first_core
            and first_core == second_core
            and scorer.domain_identity_match(company, first_domain)[0]
            and scorer.domain_identity_match(company, second_domain)[0]
        ):
            edges.append("same_brand_domain_core")

    # A shared name or a similar domain is only corroboration. Join domains
    # when there is a direct first-party/registry edge, or at least two
    # independent operational edges. Distinct legal identifiers always win.
    continuity_edges = {
        edge for edge in typed_edges
        if edge in {
            "first_party_parentOrganization", "first_party_subOrganization",
            "first_party_branchOf", "first_party_department",
            "first_party_productDivision",
        }
    }
    direct_edges = {
        "same_verified_entity",
        "first_party_domain_link", "shared_legal_identifier",
        *continuity_edges,
    }
    operational_edges = {
        "cross_domain_first_party_email", "shared_phone", "shared_structured_address",
    }
    identity_continuity = not conflicts and bool(set(edges) & direct_edges)
    related = not conflicts and (
        identity_continuity
        or len(set(edges) & operational_edges) >= 2
    )
    return {
        "related": related,
        "identity_continuity": identity_continuity,
        "edges": list(dict.fromkeys(edges)),
        "conflicts": conflicts,
    }


def _same_official_family(first: dict, second: dict, company: str = "") -> bool:
    return bool(_official_family_evidence(first, second, company)["related"])


def _homonym_conflict(company: str, first: dict, second: dict) -> dict:
    """Detect two plausible first-party sites for the same public name."""
    first_domain = scorer.normalize_domain(first.get("candidate", {}).get("url", ""))
    second_domain = scorer.normalize_domain(second.get("candidate", {}).get("url", ""))
    if not first_domain or not second_domain or first_domain == second_domain:
        return {"ambiguous": False, "reason": "same_domain"}
    if scorer.same_registrable_domain(first_domain, second_domain):
        return {"ambiguous": False, "reason": "same_registrable_domain"}
    family = _official_family_evidence(first, second, company)
    # Shared phones and cross-domain mailboxes can describe an operational
    # relationship (shop, reseller or call centre), but must not by themselves
    # resolve two otherwise publishable same-name identities.  Only explicit
    # first-party/legal continuity is identity authority here.
    if family.get("identity_continuity"):
        return {"ambiguous": False, "reason": "official_family", "family": family}

    first_assessment = first.get("identity_assessment", {})
    second_assessment = second.get("identity_assessment", {})
    first_conflicted = bool(first_assessment.get("conflicts"))
    second_conflicted = bool(second_assessment.get("conflicts"))
    if first_conflicted != second_conflicted:
        clean = second if first_conflicted else first
        if clean.get("identity_assessment", {}).get("provisionally_publishable"):
            return {
                "ambiguous": False,
                "reason": "identity_conflict_resolution",
                "family": family,
            }

    def first_party(item: dict) -> bool:
        reasons = item.get("reasons", [])
        return any(reason.startswith((
            "page_identity_medium:", "page_identity_strong:",
            "structured_identity_medium:", "structured_identity_strong:",
            "legal_name_phrase_match:", "legal_name_full_match:",
            "legal_name_ownership_match:",
        )) for reason in reasons)

    def context_state(item: dict) -> str:
        reasons = item.get("reasons", [])
        if any(reason.startswith("context_match:") for reason in reasons):
            return "match"
        if any(reason.startswith(("context_missing:", "metadata_context_missing:")) for reason in reasons):
            return "mismatch"
        return "unknown"

    first_candidate = first.get("candidate", {})
    second_candidate = second.get("candidate", {})
    first_domain_match = scorer.domain_identity_match(company, first_domain)[0]
    second_domain_match = scorer.domain_identity_match(company, second_domain)[0]
    if not (first_party(first) and first_party(second) and first_domain_match and second_domain_match):
        return {"ambiguous": False, "reason": "insufficient_dual_first_party_identity", "family": family}

    first_authority = first_candidate.get("query") in {"verified_entity", "verified_alias", "input_website"}
    second_authority = second_candidate.get("query") in {"verified_entity", "verified_alias", "input_website"}
    if first_authority != second_authority:
        return {"ambiguous": False, "reason": "unique_authoritative_resolution", "family": family}

    first_context = context_state(first)
    second_context = context_state(second)
    if {first_context, second_context} == {"match", "mismatch"}:
        return {"ambiguous": False, "reason": "business_context_resolution", "family": family}

    return {
        "ambiguous": True,
        "reason": "unresolved_homonym_candidates",
        "domains": [first_domain, second_domain],
        "family": family,
        "context": [first_context, second_context],
    }


def _close_identity_margin_conflict(company: str, first: dict, second: dict) -> bool:
    """Abstain when two unrelated domains have equally strong publishable identity."""
    first_candidate = first.get("candidate", {})
    second_candidate = second.get("candidate", {})
    first_domain = scorer.normalize_domain(first_candidate.get("url", ""))
    second_domain = scorer.normalize_domain(second_candidate.get("url", ""))
    if not first_domain or not second_domain or first_domain == second_domain:
        return False
    if scorer.same_registrable_domain(first_domain, second_domain):
        return False
    if _official_family_evidence(first, second, company).get("identity_continuity"):
        return False
    authoritative_queries = {"verified_entity", "verified_alias", "input_website"}
    if (
        first_candidate.get("query") in authoritative_queries
        or second_candidate.get("query") in authoritative_queries
    ):
        return False
    first_assessment = first.get("identity_assessment", {})
    second_assessment = second.get("identity_assessment", {})
    if not (
        first_assessment.get("provisionally_publishable")
        and second_assessment.get("provisionally_publishable")
        and not first_assessment.get("conflicts")
        and not second_assessment.get("conflicts")
    ):
        return False
    return (
        first_assessment.get("support_count", 0) == second_assessment.get("support_count", 0)
        and abs(first.get("final_score", 0) - second.get("final_score", 0))
        <= config.AMBIGUOUS_CANDIDATE_MARGIN
    )


def _legal_identity_strength(evaluation: dict) -> int:
    reasons = evaluation.get("reasons", [])
    if any(reason.startswith((
        "legal_name_full_match:",
        "legal_name_ownership_match:",
    )) for reason in reasons):
        return 3
    if any(reason.startswith("legal_name_phrase_match:") for reason in reasons):
        return 2
    return 0


def _preferred_verified_discovery_route(evaluations: list[dict]) -> dict | None:
    """Prefer a discovered route only when its own first-party identity is stronger."""
    publishable = [
        item for item in evaluations
        if (
            item.get("identity_assessment", {}).get("provisionally_publishable")
            and not item.get("identity_assessment", {}).get("conflicts")
        )
    ]
    routes = [
        item for item in publishable
        if item.get("candidate", {}).get("_source_profile_evidence")
    ]
    if len(routes) != 1:
        return None
    route = routes[0]
    route_strength = _legal_identity_strength(route)
    competing_strengths = [
        _legal_identity_strength(item) for item in publishable if item is not route
    ]
    if route_strength < 2 or (
        competing_strengths and route_strength <= max(competing_strengths)
    ):
        return None
    return route


def _unreachable_homonym_conflict(company: str, selected: dict, evaluations: list[dict]) -> dict | None:
    """Keep an inaccessible same-name domain from being silently outranked."""
    if (
        selected.get("candidate", {}).get("_source_profile_evidence")
        and selected.get("identity_assessment", {}).get("provisionally_publishable")
        and not selected.get("identity_assessment", {}).get("conflicts")
        and _legal_identity_strength(selected) >= 2
    ):
        return None
    selected_candidate = selected.get("candidate", {})
    selected_domain = scorer.normalize_domain(selected_candidate.get("url", ""))
    selected_core = scorer.compact_domain_core(selected_domain)
    if not selected_core or not scorer.domain_identity_match(company, selected_domain)[0]:
        return None
    for item in evaluations:
        if item is selected or item.get("crawl_result", {}).get("pages"):
            continue
        candidate = item.get("candidate", {})
        other_domain = scorer.normalize_domain(candidate.get("url", ""))
        if not other_domain or other_domain == selected_domain:
            continue
        if scorer.compact_domain_core(other_domain) != selected_core:
            continue
        if not scorer.domain_identity_match(company, other_domain)[0]:
            continue
        if candidate.get("role") != "company_candidate":
            continue
        if candidate.get("score", 0) < config.MEDIUM_CONFIDENCE_SCORE:
            continue
        return {
            "ambiguous": True,
            "reason": "unreachable_same_name_domain",
            "domains": [selected_domain, other_domain],
            "failed_evaluation": _evaluation_evidence(item),
        }
    return None


def _merge_official_family_contacts(primary: dict, related: list[dict], company: str = "") -> None:
    for item in related:
        if not _same_official_family(primary, item, company):
            continue
        if not _has_trusted_website_evidence(item["candidate"], item.get("reasons", [])):
            continue
        other_email_records = []
        if item.get("email"):
            other_email_records.append({
                "value": item["email"],
                "source_url": item.get("email_source_url", ""),
                "retrieval_method": item.get("email_retrieval_method", "unknown"),
                "verification_status": item.get("email_verification", "not_checked"),
                "verification_reason": item.get("email_verification_reason", ""),
                "official_family_verified": True,
            })
        other_email_records.extend(
            {**record, "official_family_verified": True}
            for record in item.get("alternative_email_records", [])
        )
        other_emails = [record.get("value", "") for record in other_email_records]
        primary["alternative_emails"] = list(dict.fromkeys([
            *primary.get("alternative_emails", []),
            *(email for email in other_emails if email and email != primary.get("email", "")),
        ]))
        known_email_records = {
            record.get("value", "")
            for record in primary.get("alternative_email_records", [])
        }
        known_email_records.add(primary.get("email", ""))
        primary.setdefault("alternative_email_records", []).extend(
            record for record in other_email_records
            if record.get("value", "") not in known_email_records
        )
        if not primary.get("email") and item.get("email"):
            for key in (
                "email", "email_source", "email_source_url",
                "email_verification", "email_verification_reason",
                "email_selection_reason", "email_retrieval_method",
                "email_publication_status", "email_publication_reason",
            ):
                primary[key] = item.get(key, "")
            primary["alternative_emails"] = [
                value for value in primary.get("alternative_emails", [])
                if value != primary["email"]
            ]
            primary["alternative_email_records"] = [
                record for record in primary.get("alternative_email_records", [])
                if record.get("value", "") != primary["email"]
            ]

        other_phones = []
        if item.get("phone"):
            other_phones.append({
                "value": item["phone"], "label": item.get("phone_label", "general"),
                "source_url": item.get("phone_source_url", ""),
                "retrieval_method": item.get("phone_retrieval_method", "unknown"),
                "official_family_verified": True,
            })
        other_phones.extend(
            {**record, "official_family_verified": True}
            for record in item.get("alternative_phones", [])
        )
        known = {phone_item.get("value", "") for phone_item in primary.get("alternative_phones", [])}
        known.add(primary.get("phone", ""))
        primary.setdefault("alternative_phones", []).extend(
            phone_item for phone_item in other_phones if phone_item.get("value", "") not in known
        )
        if not primary.get("phone") and item.get("phone"):
            for key in (
                "phone", "phone_source", "phone_source_url", "phone_label",
                "phone_selection_reason", "phone_retrieval_method",
                "phone_publication_status", "phone_publication_reason",
            ):
                primary[key] = item.get(key, "")
            primary["alternative_phones"] = [
                phone_item for phone_item in primary.get("alternative_phones", [])
                if phone_item.get("value", "") != primary["phone"]
            ]
        primary["has_contact"] = bool(primary.get("email") or primary.get("phone"))
        if "official_site_family_contact_merge" not in primary["reasons"]:
            primary["reasons"].append("official_site_family_contact_merge")


def _confidence_status(score: int, has_contact: bool, reasons: list[str], identity_verified: bool = True) -> tuple[str, str]:
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


def _apply_publication_policy(
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
    if mode == "shadow":
        decision["mode"] = "shadow"
        if decision.get("action") == "downgrade_to_review":
            decision["action"] = "would_downgrade_to_review"
    else:
        status, confidence = publication_policy.enforce(
            decision, status, confidence, reasons,
        )
    evaluation["publication_policy"] = decision
    return status, confidence


def _policy_output_fields(evaluation: dict) -> dict:
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


def _exact_brand_domain(company: str, candidate: dict) -> bool:
    tokens = scorer.domain_identity_tokens(company)
    return bool(tokens) and scorer.compact_domain_core(candidate.get("url", "")) == "".join(tokens)


def _brand_domain_affinity(company: str, candidate: dict) -> bool:
    """Return true when every distinctive brand token occurs in the domain core."""
    tokens = scorer.domain_identity_tokens(company)
    core = scorer.compact_domain_core(candidate.get("url", ""))
    return bool(core and tokens) and all(len(token) >= 4 and token in core for token in tokens)


def _reviewable_authoritative_candidate(company: str, candidate: dict) -> bool:
    domain = scorer.normalize_domain(candidate.get("url", ""))
    if not domain or scorer.is_excluded_domain(domain):
        return False
    if candidate.get("query") in {"verified_alias", "verified_entity"}:
        return True
    exact_domain = _exact_brand_domain(company, candidate)
    return bool(
        exact_domain
        and candidate.get("_official_query_evidence", 0) >= 2
        and candidate.get("score", 0) >= config.MEDIUM_CONFIDENCE_SCORE
    )


def _unsupported_search_text_candidate(company: str, evaluation: dict) -> bool:
    """Reject retailer/directory-like results that never prove company identity."""
    if str(evaluation.get("_identity_resolution", "")).startswith(
        "candidate_resolved_by_"
    ):
        return False
    candidate = evaluation.get("candidate", {})
    reasons = evaluation.get("reasons", [])
    if candidate.get("role") in {"directory", "fair_profile", "news"}:
        return True
    if any(reason.startswith("structured_identity_unmatched:") for reason in reasons) and not any(
        reason.startswith("legal_name_ownership_match:") for reason in reasons
    ):
        return True
    if candidate.get("role") == "company_candidate":
        return False
    if _reviewable_authoritative_candidate(company, candidate):
        return False
    if scorer.domain_identity_match(company, candidate.get("url", ""))[0]:
        return False
    page_missing = any(reason.startswith("page_identity_missing:") for reason in reasons)
    structured_missing = any(
        reason.startswith(("structured_identity_absent", "structured_identity_unmatched:"))
        for reason in reasons
    )
    return page_missing and structured_missing


def _listed_domain_conflict_requires_review(
    company: str,
    evaluation: dict,
    metadata: dict | None,
) -> bool:
    """Quarantine a search domain that contradicts a supplied discovery route."""
    listed_domain = scorer.normalize_domain(
        str((metadata or {}).get("listed_website", "") or "")
    )
    selected_domain = scorer.normalize_domain(
        evaluation.get("candidate", {}).get("url", "")
    )
    if (
        not listed_domain
        or not selected_domain
        or scorer.same_registrable_domain(listed_domain, selected_domain)
    ):
        return False

    reasons = evaluation.get("reasons", [])
    if any(reason.startswith((
        "legal_name_full_match:",
        "legal_name_ownership_match:",
    )) for reason in reasons):
        return False
    phrase_match = any(
        reason.startswith("legal_name_phrase_match:") for reason in reasons
    )
    return not (
        phrase_match
        and len(scorer.legal_identity_tokens(company)) >= 2
    )


def _weak_search_identity_requires_review(company: str, evaluation: dict) -> bool:
    if str(evaluation.get("_identity_resolution", "")).startswith(
        "candidate_resolved_by_"
    ):
        return False
    candidate = evaluation.get("candidate", {})
    if candidate.get("query") in {
        "fair_listed_website",
        "verified_alias",
        "verified_entity",
    }:
        return False
    reasons = evaluation.get("reasons", [])
    if any(reason.startswith((
        "legal_name_full_match:",
        "legal_name_phrase_match:",
        "legal_name_ownership_match:",
    )) for reason in reasons):
        return False
    return bool(
        len(scorer.legal_identity_tokens(company)) >= 2
        and "no_context_tokens" in reasons
        and any(
            reason.startswith("legal_name_phrase_missing:")
            for reason in reasons
        )
        and not _exact_brand_domain(company, candidate)
    )


def _authoritative_unreachable_candidate(candidates: list[dict], company: str = "") -> dict | None:
    eligible = [
        candidate for candidate in candidates
        if (
            _reviewable_authoritative_candidate(company, candidate)
            if company else (
                candidate.get("query") in {"verified_alias", "verified_entity"}
                or (
                    candidate.get("_official_query_evidence", 0) >= 2
                    and candidate.get("score", 0) >= config.MEDIUM_CONFIDENCE_SCORE
                )
            )
        )
    ]
    return max(
        eligible,
        key=lambda candidate: (
            1 if company and _exact_brand_domain(company, candidate) else 0,
            1 if company and _brand_domain_affinity(company, candidate) else 0,
            candidate.get("_official_query_evidence", 0),
            candidate.get("_metadata_context_matches", 0),
            candidate.get("score", 0),
            -candidate.get("rank", 0),
        ),
        default=None,
    )


def _evaluation_rank_key(company: str, item: dict) -> tuple[int, ...]:
    vector = candidate_reranker.evidence_vector(
        company,
        item,
        hard_context_failure=_is_hard_context_failure(item),
    )
    item["rerank_evidence"] = vector
    return candidate_reranker.vector_key(vector)


def _acquisition_brand_domain_match(company: str, candidate: dict) -> bool:
    """Allow a bounded crawl for a page-backed brand+descriptor domain."""
    tokens = scorer.primary_brand_tokens(company, limit=3)
    brand_index = next(
        (index for index, token in enumerate(tokens) if token.isalpha() and len(token) >= 5),
        None,
    )
    if brand_index is None:
        return False
    brand = tokens[brand_index]
    numeric_prefix = "".join(
        token for token in tokens[:brand_index] if token.isdigit()
    )
    core = scorer.compact_domain_core(candidate.get("url", ""))
    return bool(
        core.startswith(brand)
        or (numeric_prefix and core.startswith(f"{numeric_prefix}{brand}"))
    )


def _full_crawl_worthy(company: str, evaluation: dict) -> bool:
    """Spend contact-crawl budget after identity is plausible, not complete.

    Country evidence may live only on contact/legal pages reached by the full
    crawl. Requiring it here creates a circular gate: no full crawl without a
    phone/address, and no phone/address without a full crawl. Publication still
    requires the complete fingerprint in entity_resolution.
    """
    fingerprint = entity_resolution.fingerprint(
        entity_resolution.build_target_profile(company), evaluation,
    )
    if not (
        fingerprint.reachable
        and fingerprint.eligible_role
        and fingerprint.conflict_free
        and fingerprint.canonical_domain_consistent
    ):
        return False
    if fingerprint.provisionally_publishable:
        return True
    return bool(
        fingerprint.obvious_exact_domain
        or fingerprint.exact_brand_domain
        or fingerprint.legal_strength >= 1
        or (
            (
                fingerprint.public_brand_domain
                or _acquisition_brand_domain_match(
                    company, evaluation.get("candidate", {}),
                )
            )
            and (
                fingerprint.page_strength >= 2
                or (
                    fingerprint.page_strength >= 1
                    and fingerprint.official_query_evidence >= 3
                )
            )
        )
    )


def _refine_identity_evidence(
    company: str,
    metadata: dict | None,
    ranked_identity: list[dict],
) -> list[dict]:
    """Use one gap-directed identity recrawl before any broad contact crawl."""
    if any(_full_crawl_worthy(company, item) for item in ranked_identity):
        return ranked_identity
    refined = list(ranked_identity)
    attempts = 0
    for index, evaluation in enumerate(refined):
        if attempts >= config.MAX_IDENTITY_EVIDENCE_RECRAWLS:
            break
        fingerprint = entity_resolution.fingerprint(
            entity_resolution.build_target_profile(company), evaluation,
        )
        if not (
            fingerprint.reachable
            and fingerprint.eligible_role
            and fingerprint.conflict_free
        ):
            continue
        state = evidence_acquisition.analyze(
            company,
            [evaluation],
            resolution_status="unresolved",
            metadata=metadata,
            query_limit=0,
        )
        if not state.crawl_scopes:
            continue
        attempts += 1
        runtime.record("pipeline.identity_evidence_recrawls")
        targeted = _evaluate_candidate_with_stage(
            company,
            evaluation["candidate"],
            metadata,
            crawl_profile="identity",
            verify_email_domain=False,
            evidence_scopes=state.crawl_scopes,
        )
        if targeted.get("crawl_result", {}).get("pages"):
            refined[index] = targeted
    refined.sort(
        key=lambda item: _evaluation_rank_key(company, item),
        reverse=True,
    )
    return refined


def _process_known_website(index: int, company: str, website: str, logger, metadata: dict | None = None) -> tuple[int, dict] | None:
    logger.info("Processing %s with supplied website: %s -> %s", index + 1, company, website)
    candidate = {
        "domain": scorer.normalize_domain(website),
        "url": website,
        "score": config.MIN_ACCEPT_SCORE,
        "title": "",
        "snippet": "",
        "query": "input_website",
        "rank": 0,
        "reason": "input_website",
    }
    evaluation = _evaluate_candidate(company, candidate, metadata)
    if not evaluation["crawl_result"]["pages"]:
        return None
    resolution = entity_resolution.resolve_candidates(company, [evaluation])
    resolution = _try_llm_arbitration(
        company, metadata, [evaluation], resolution,
    )
    if evaluation.get("_llm_arbiter_rejected"):
        return None
    if resolution.status == "resolved" and resolution.selected is not None:
        evaluation = resolution.selected
        evaluation["_identity_resolution"] = resolution.reason
    row = _finalize_selected_evaluation(company, evaluation, metadata)
    return index, _attach_candidates(row, [candidate])


def _finalize_selected_evaluation(
    company: str,
    evaluation: dict,
    metadata: dict | None,
) -> dict:
    """Create the sole publication verdict for a resolved website identity."""
    crawl_result = evaluation["crawl_result"]
    reasons = evaluation["reasons"]
    unsafe_identity = _unsafe_context_identity(company, evaluation)
    hard_context_failure = _is_hard_context_failure(evaluation)
    unsupported_candidate = _unsupported_search_text_candidate(company, evaluation)
    resolution_reason = str(evaluation.get("_identity_resolution", "") or "")
    exact_domain_resolution = resolution_reason.endswith(
        "_exact_full_name_domain"
    )
    identity_verified = resolution_reason.startswith(
        "candidate_resolved_by_"
    ) or exact_domain_resolution or _has_trusted_website_evidence(
        evaluation["candidate"], reasons, unique_candidate=True,
    )
    if unsupported_candidate:
        reasons.append("unsupported_search_text_candidate_rejected")
        status, confidence = "WEBSITE_NOT_FOUND", "none"
    elif unsafe_identity and not _reviewable_authoritative_candidate(
        company, evaluation["candidate"]
    ):
        reasons.append("unverified_website_candidate_preserved_for_review")
        status, confidence = "REVIEW_NEEDED", "review"
    elif hard_context_failure and not _reviewable_authoritative_candidate(
        company, evaluation["candidate"]
    ):
        reasons.append("unverified_website_candidate_preserved_for_review")
        status, confidence = "REVIEW_NEEDED", "review"
    elif unsafe_identity or hard_context_failure:
        reasons.append("authoritative_website_preserved_for_review")
        status, confidence = "REVIEW_NEEDED", "review"
    elif _email_failure_blocks_publication(evaluation, identity_verified):
        status, confidence = "REVIEW_NEEDED", "review"
    elif (
        resolution_reason
        and identity_verified
        and evaluation["has_contact"]
    ):
        status, confidence = (
            ("OK_HIGH_CONFIDENCE", "high")
            if evaluation["final_score"] >= config.HIGH_CONFIDENCE_SCORE
            else ("OK_MEDIUM_CONFIDENCE", "medium")
        )
    else:
        status, confidence = _confidence_status(
            evaluation["final_score"],
            evaluation["has_contact"],
            reasons,
            identity_verified,
        )
    if (
        status.startswith("OK_")
        and evaluation["candidate"].get("query") != "input_website"
        and (
            _listed_domain_conflict_requires_review(company, evaluation, metadata)
            or _weak_search_identity_requires_review(company, evaluation)
        )
    ):
        reasons.append("search_identity_without_legal_or_context_support")
        status, confidence = "REVIEW_NEEDED", "review"
    status, confidence = _apply_publication_policy(
        company, evaluation, status, confidence, reasons,
    )
    row = {
        "company": company,
        "website": crawl_result["url"],
        "website_source": evaluation["candidate"]["query"],
        **_contact_output_fields(evaluation),
        "status": status,
        "confidence": confidence,
        "score": evaluation["final_score"],
        **_policy_output_fields(evaluation),
        "reason": "; ".join(reason for reason in reasons if reason),
        "__evaluation": _evaluation_evidence(evaluation),
    }
    if status == "WEBSITE_NOT_FOUND":
        _clear_unpublished_contacts(row)
    elif not identity_verified:
        row["selected_website"] = crawl_result["url"]
        _clear_unpublished_contacts(row)
        row["email_verification_reason"] = "website_identity_unverified"
    return row


def process_company(index: int, company: str, logger, known_website: str = "", metadata: dict | None = None) -> tuple[int, dict]:
    runtime.record("pipeline.companies")
    logger.info("Processing %s: %s", index + 1, company)
    if known_website:
        try:
            known_result = _process_known_website(index, company, known_website, logger, metadata)
            if known_result:
                random_delay()
                return known_result
            logger.info("Supplied website failed, falling back to search: %s", company)
        except Exception:
            logger.exception("Supplied website evaluation failed for %s, falling back to search", company)

    profile_candidates = search.find_profile_candidates(company, metadata)
    if profile_candidates:
        runtime.record("pipeline.profile_candidates_discovered", len(profile_candidates))
        profile_identity = [
            _evaluate_candidate_with_stage(
                company, candidate, metadata,
                crawl_profile="identity", verify_email_domain=False,
            )
            for candidate in profile_candidates
        ]
        profile_full = []
        for identity_evaluation in profile_identity:
            if not (
                identity_evaluation.get("crawl_result", {}).get("pages")
                and identity_evaluation.get("identity_assessment", {}).get(
                    "provisionally_publishable"
                )
            ):
                continue
            full_evaluation = _evaluate_candidate_with_stage(
                company, identity_evaluation["candidate"], metadata,
            )
            profile_full.append(
                _preserve_identity_phase_evidence(
                    full_evaluation, identity_evaluation,
                )
            )
        runtime.record("pipeline.profile_candidates_evaluated", len(profile_full))
        profile_resolution = entity_resolution.resolve_profile_anchor(
            company, profile_full,
        )
        if profile_resolution.status == "resolved":
            profile_resolution.selected["_identity_resolution"] = (
                profile_resolution.reason
            )
            row = _finalize_selected_evaluation(
                company, profile_resolution.selected, metadata,
            )
            if row["status"].startswith("OK_"):
                row["reason"] = (
                    f"{profile_resolution.reason}; {row['reason']}"
                ).strip("; ")
                random_delay()
                return index, _attach_candidates(row, profile_candidates)

    try:
        candidates = search.find_candidate_domains(company, metadata)
    except Exception as exc:
        logger.exception("Search failed for %s", company)
        random_delay()
        return index, _attach_candidates(_empty_result(company, "SEARCH_FAILED", str(exc)), [])

    runtime.record("pipeline.candidates_discovered", len(candidates))
    source_health = getattr(candidates, "source_health", {})
    if source_health.get("status") in {"degraded", "circuit_open", "unavailable"}:
        runtime.record("pipeline.source_degraded_companies")
    selectable_candidates = [
        candidate for candidate in candidates
        if (
            candidate.get("role") not in identity.EXCLUDED_ROLES
            and (
                candidate["score"] >= config.MIN_ACCEPT_SCORE
                or (
                    candidate["score"] >= config.MIN_ACCEPT_SCORE - 5
                    and candidate.get("role") == "company_candidate"
                    and candidate.get("_official_query_evidence", 0) >= 3
                    and scorer.public_brand_domain_match(
                        company, candidate.get("url", ""),
                    )
                )
            )
        )
    ]
    best = selectable_candidates[0] if selectable_candidates else None
    if not best:
        random_delay()
        row = _empty_result(company, "WEBSITE_NOT_FOUND", "No candidate passed score threshold")
        return index, _attach_candidates(row, candidates)

    eligible_candidates = [
        candidate
        for candidate in selectable_candidates
        if candidate["score"] >= best["score"] - config.MAX_CANDIDATE_SCORE_GAP
    ][: config.MAX_CANDIDATE_EVALUATIONS]
    evaluated_domains = {
        scorer.normalize_domain(candidate.get("url", ""))
        for candidate in eligible_candidates
    }
    eligible_candidates.extend(
        candidate for candidate in selectable_candidates
        if (
            candidate.get("_source_profile_evidence")
            and scorer.normalize_domain(candidate.get("url", "")) not in evaluated_domains
        )
    )
    identity_evaluations = [
        _evaluate_candidate_with_stage(
            company, candidate, metadata,
            crawl_profile="identity", verify_email_domain=False,
        )
        for candidate in eligible_candidates
    ]
    alias_candidates = _first_party_alias_candidates(
        company,
        identity_evaluations,
        {scorer.normalize_domain(candidate.get("url", "")) for candidate in candidates},
    )
    if alias_candidates:
        candidates.extend(alias_candidates)
        candidates[:] = search.rank_candidates(candidates)
        identity_evaluations.extend(
            _evaluate_candidate_with_stage(
                company, candidate, metadata,
                crawl_profile="identity", verify_email_domain=False,
            )
            for candidate in alias_candidates
        )
    runtime.record("pipeline.identity_candidates_evaluated", len(identity_evaluations))
    successful_identity = [item for item in identity_evaluations if item["crawl_result"]["pages"]]
    if not successful_identity:
        random_delay()
        safe_failed_candidates = [
            item["candidate"] for item in identity_evaluations
            if not any(reason.startswith("foreign_country_redirect_rejected:") for reason in item.get("reasons", []))
        ]
        authoritative = _authoritative_unreachable_candidate(safe_failed_candidates, company)
        if authoritative:
            row = _empty_result(
                company,
                "REVIEW_NEEDED",
                "website_unreachable_but_authoritative_evidence",
                authoritative["score"],
            )
            row["website"] = authoritative["url"]
            row["website_source"] = authoritative["query"]
            row["confidence"] = "review"
            row["email_verification_reason"] = "website_unreachable"
            return index, _attach_candidates(row, candidates)
        row = _empty_result(company, "WEBSITE_FETCH_FAILED", identity_evaluations[0]["reasons"][0], best["score"])
        return index, _attach_candidates(row, candidates)

    ranked_identity = sorted(
        successful_identity,
        key=lambda item: _evaluation_rank_key(company, item),
        reverse=True,
    )
    ranked_identity = _refine_identity_evidence(
        company, metadata, ranked_identity,
    )
    full_candidates = [
        item["candidate"]
        for item in ranked_identity
        if _full_crawl_worthy(company, item)
    ][: config.MAX_FULL_CANDIDATE_EVALUATIONS]
    full_candidate_domains = {
        scorer.normalize_domain(candidate.get("url", ""))
        for candidate in full_candidates
    }
    full_candidates.extend([
        item["candidate"]
        for item in ranked_identity
        if (
            entity_resolution.fingerprint(
                entity_resolution.build_target_profile(company), item,
            ).domain_specificity >= 2
            and _full_crawl_worthy(company, item)
            and scorer.normalize_domain(
                item["candidate"].get("url", "")
            ) not in full_candidate_domains
        )
    ][:2])
    if not full_candidates:
        automation_state = evidence_acquisition.analyze(
            company,
            ranked_identity,
            resolution_status="unresolved",
            metadata=metadata,
            query_limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
        )
        # Do not stop at a diagnosis. When initial discovery produced no
        # crawl-worthy identity, execute the bounded evidence plan and feed
        # newly discovered domains back through the same identity gates.
        targeted_candidates = search.find_targeted_candidates(
            company,
            metadata,
            automation_state.search_queries,
            limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
        )
        known_domains = {
            scorer.normalize_domain(item.get("url", "")) for item in candidates
        }
        acquired_identity: list[dict] = []
        for candidate in targeted_candidates:
            domain = scorer.normalize_domain(candidate.get("url", ""))
            if not domain or domain in known_domains:
                continue
            known_domains.add(domain)
            candidates.append(candidate)
            acquired_identity.append(_evaluate_candidate_with_stage(
                company,
                candidate,
                metadata,
                crawl_profile="identity",
                verify_email_domain=False,
                evidence_scopes=automation_state.crawl_scopes,
            ))
            if len(acquired_identity) >= config.MAX_TARGETED_CRAWLS_PER_ROUND:
                break
        if acquired_identity:
            ranked_identity.extend(
                item for item in acquired_identity
                if item.get("crawl_result", {}).get("pages")
            )
            ranked_identity.sort(
                key=lambda item: _evaluation_rank_key(company, item),
                reverse=True,
            )
            ranked_identity = _refine_identity_evidence(
                company, metadata, ranked_identity,
            )
            full_candidates = [
                item["candidate"]
                for item in ranked_identity
                if _full_crawl_worthy(company, item)
            ][: config.MAX_FULL_CANDIDATE_EVALUATIONS]
            runtime.record(
                "pipeline.identity_acquisition_candidates",
                len(acquired_identity),
            )
    if not full_candidates:
        automation_state = evidence_acquisition.analyze(
            company,
            ranked_identity,
            resolution_status="unresolved",
            metadata=metadata,
            query_limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
        )
        row = _empty_result(
            company,
            "REVIEW_NEEDED",
            "no_candidate_proved_target_fingerprint",
            ranked_identity[0].get("final_score", 0),
        )
        row["confidence"] = "review"
        row["__evaluation"] = {
            "candidate_evaluations": [
                _evaluation_evidence(item) for item in ranked_identity
            ],
            "identity_resolution": {
                "status": "unresolved",
                "reason": "no_candidate_proved_target_fingerprint",
            },
            "remaining_evidence_gaps": sorted(automation_state.gaps),
            "automation_terminal_reason": automation_state.terminal_reason,
        }
        random_delay()
        return index, _attach_candidates(row, candidates)
    full_candidate_domains = {
        scorer.normalize_domain(candidate.get("url", ""))
        for candidate in full_candidates
    }
    full_candidates.extend(
        item["candidate"] for item in ranked_identity
        if (
            item.get("candidate", {}).get("_source_profile_evidence")
            and item.get("identity_assessment", {}).get("provisionally_publishable")
            and scorer.normalize_domain(item["candidate"].get("url", ""))
            not in full_candidate_domains
        )
    )
    identity_by_domain = {
        scorer.normalize_domain(item["candidate"].get("url", "")): item
        for item in ranked_identity
    }
    evaluations = []
    for candidate in full_candidates:
        full_evaluation = _evaluate_candidate_with_stage(company, candidate, metadata)
        light_evaluation = identity_by_domain.get(scorer.normalize_domain(candidate.get("url", "")))
        if light_evaluation:
            full_evaluation = _preserve_identity_phase_evidence(full_evaluation, light_evaluation)
        evaluations.append(full_evaluation)
    contact_alias_candidates = _first_party_contact_alias_candidates(
        company,
        evaluations,
        {scorer.normalize_domain(candidate.get("url", "")) for candidate in candidates},
    )
    if contact_alias_candidates:
        candidates.extend(contact_alias_candidates)
        candidates[:] = search.rank_candidates(candidates)
        evaluations.extend(
            _evaluate_candidate_with_stage(company, candidate, metadata)
            for candidate in contact_alias_candidates
        )
    runtime.record("pipeline.full_candidates_evaluated", len(evaluations))
    successful = [item for item in evaluations if item["crawl_result"]["pages"]]
    if not successful:
        # The light crawl proved that candidate pages exist. If a subsequent
        # full contact crawl fails, preserve the identity evidence for review
        # instead of silently selecting a lower-ranked unrelated site.
        successful = ranked_identity[:1]
        evaluations = [*evaluations, *identity_evaluations]
        successful[0]["reasons"].append("full_contact_crawl_failed_identity_only")

    ranked_evaluations = sorted(
        successful,
        key=lambda item: _evaluation_rank_key(company, item),
        reverse=True,
    )
    resolution = entity_resolution.resolve_candidates(
        company, ranked_evaluations,
    )
    automation_state = evidence_acquisition.analyze(
        company,
        ranked_evaluations,
        resolution_status=resolution.status,
        metadata=metadata,
        query_limit=config.MAX_TARGETED_QUERIES_PER_ROUND,
    )
    if (
        resolution.status != "resolved"
        and all("identity_assessment" in item for item in ranked_evaluations)
    ):
        ranked_evaluations, resolution, automation_state = (
            _complete_resolution_evidence(
                company,
                metadata,
                candidates,
                ranked_evaluations,
                resolution,
            )
        )
    if resolution.status == "unresolved":
        resolution = _try_linkedin_company_corroboration(
            company, ranked_evaluations, resolution,
        )
    resolution = _try_llm_arbitration(
        company, metadata, ranked_evaluations, resolution,
    )
    if any(item.get("_llm_arbiter_rejected") for item in ranked_evaluations):
        ranked_evaluations = _evaluate_llm_rejection_fallbacks(
            company, metadata, ranked_identity, ranked_evaluations,
        )
        resolution = entity_resolution.resolve_candidates(
            company, ranked_evaluations,
        )
        resolution = _try_llm_arbitration(
            company, metadata, ranked_evaluations, resolution,
        )
    if resolution.status == "ambiguous":
        row = _empty_result(
            company,
            "WEBSITE_AMBIGUOUS",
            resolution.reason,
            max(
                (item.get("final_score", 0) for item in resolution.contenders),
                default=0,
            ),
        )
        row["confidence"] = "review"
        row["__evaluation"] = {
            "ambiguous_candidates": [
                _evaluation_evidence(item) for item in resolution.contenders
            ],
            "identity_resolution": {
                "status": resolution.status,
                "reason": resolution.reason,
            },
            "remaining_evidence_gaps": sorted(automation_state.gaps),
            "automation_terminal_reason": automation_state.terminal_reason,
        }
        random_delay()
        return index, _attach_candidates(row, candidates)
    if resolution.status != "resolved" or resolution.selected is None:
        row = _empty_result(
            company,
            "REVIEW_NEEDED",
            resolution.reason,
            ranked_evaluations[0].get("final_score", 0),
        )
        row["confidence"] = "review"
        row["__evaluation"] = {
            "candidate_evaluations": [
                _evaluation_evidence(item) for item in resolution.contenders
            ],
            "identity_resolution": {
                "status": resolution.status,
                "reason": resolution.reason,
            },
            "remaining_evidence_gaps": sorted(automation_state.gaps),
            "automation_terminal_reason": automation_state.terminal_reason,
        }
        random_delay()
        return index, _attach_candidates(row, candidates)
    best_eval = resolution.selected
    best_eval["_identity_resolution"] = resolution.reason
    ranked_evaluations.remove(best_eval)
    ranked_evaluations.insert(0, best_eval)
    unreachable_homonym = _unreachable_homonym_conflict(company, best_eval, identity_evaluations)
    if unreachable_homonym:
        row = _empty_result(
            company,
            "WEBSITE_AMBIGUOUS",
            f"unreachable_same_name_domain: {' vs '.join(unreachable_homonym['domains'])}",
            best_eval["final_score"],
        )
        row["confidence"] = "review"
        row["__evaluation"] = {
            "ambiguous_candidates": [
                _evaluation_evidence(best_eval),
                unreachable_homonym["failed_evaluation"],
            ],
            "homonym_assessment": unreachable_homonym,
        }
        random_delay()
        return index, _attach_candidates(row, candidates)
    _merge_official_family_contacts(best_eval, ranked_evaluations[1:], company)
    unsafe_identity = _unsafe_context_identity(company, best_eval)
    hard_context_failure = _is_hard_context_failure(best_eval)
    if (unsafe_identity or hard_context_failure) and not _reviewable_authoritative_candidate(
        company, best_eval["candidate"]
    ):
        failed_candidates = [
            item["candidate"] for item in evaluations
            if not item["crawl_result"]["pages"] and not any(
                reason.startswith("foreign_country_redirect_rejected:") for reason in item.get("reasons", [])
            )
        ]
        fallback = _authoritative_unreachable_candidate(failed_candidates, company)
        if fallback:
            row = _empty_result(
                company,
                "REVIEW_NEEDED",
                "website_unreachable_but_authoritative_evidence",
                fallback["score"],
            )
            row["website"] = fallback["url"]
            row["website_source"] = fallback["query"]
            row["confidence"] = "review"
            row["email_verification_reason"] = "website_unreachable"
            random_delay()
            return index, _attach_candidates(row, candidates)

    row = _finalize_selected_evaluation(company, best_eval, metadata)
    random_delay()
    return index, _attach_candidates(row, candidates)


def _write_outputs(rows: list[dict], elapsed_seconds: float) -> str:
    for row in rows:
        row["website_status"] = (
            "verified" if row.get("website") and str(row.get("status", "")).startswith("OK_")
            else "review" if row.get("website") or row.get("status") == "WEBSITE_AMBIGUOUS"
            else "not_found"
        )
        row["contact_status"] = (
            "complete" if row.get("email") and row.get("phone")
            else "partial" if row.get("email") or row.get("phone")
            else "missing"
        )
        if row.get("status") in report.OK_STATUSES:
            discovery_coverage.mark_published(row.get("company", ""))
    evidence.write_jsonl(config.EVIDENCE_FILE, rows)
    entity_registry.write_observations(config.ENTITY_RELATIONSHIPS_FILE, rows)
    if (
        config.SEARCH_CACHE_MODE != "replay"
        and config.CRAWL_CACHE_MODE != "replay"
    ):
        entity_memory.remember(rows)
    quality_audit.write(config.QUALITY_AUDIT_FILE, rows)
    for row in rows:
        row.pop("__index", None)
        row.pop("__candidates", None)
        row.pop("__evaluation", None)
        row.pop("__candidate_evaluations", None)
        row.pop("__search_trace", None)
        row.pop("__source_health", None)
        row.pop("__paid_escalation_complete", None)
    published_rows = [row for row in rows if row.get("status") in report.OK_STATUSES]
    # contacts.xlsx is the publication surface. Review/abstain rows remain in
    # the dedicated audit artifacts and must never look like published firms.
    excel.write_contacts(config.CONTACTS_FILE, published_rows)
    excel.write_contacts(
        config.VERIFIED_CONTACTS_FILE,
        published_rows,
    )
    excel.write_contacts(
        config.REVIEW_QUEUE_FILE,
        [row for row in rows if row.get("status") not in report.OK_STATUSES],
    )
    excel.write_failed(config.FAILED_FILE, report.failed_rows(rows))
    excel.write_website_candidates(config.CANDIDATES_FILE, rows)
    report_text = report.build_report(rows, elapsed_seconds)
    config.REPORT_FILE.write_text(report_text, encoding="utf-8")
    discovery_coverage.write(
        config.DISCOVERY_COVERAGE_FILE,
        config.DISCOVERY_ACQUISITION_QUERIES_PER_COMPANY,
    )
    replay_snapshot.write(config.REPLAY_SNAPSHOT_FILE)
    runtime.write(config.TELEMETRY_FILE)
    return report_text


def _set_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_DIR = output_dir
    config.CONTACTS_FILE = output_dir / "contacts.xlsx"
    config.VERIFIED_CONTACTS_FILE = output_dir / "verified_contacts.xlsx"
    config.REVIEW_QUEUE_FILE = output_dir / "review_queue.xlsx"
    config.FAILED_FILE = output_dir / "failed.xlsx"
    config.CANDIDATES_FILE = output_dir / "website_candidates.xlsx"
    config.REPORT_FILE = output_dir / "report.txt"
    config.LOG_FILE = output_dir / "logs.txt"
    config.EVIDENCE_FILE = output_dir / "evidence.jsonl"
    config.ENTITY_RELATIONSHIPS_FILE = output_dir / "entity_relationships.jsonl"
    config.TELEMETRY_FILE = output_dir / "telemetry.json"
    config.DISCOVERY_COVERAGE_FILE = output_dir / "discovery_coverage.json"
    config.QUALITY_AUDIT_FILE = output_dir / "quality_audit.json"
    config.REPLAY_SNAPSHOT_FILE = output_dir / "replay_snapshot.json.gz"


def _set_run_state_dir(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    config.STATE_DIR = state_dir
    config.PROGRESS_FILE = state_dir / "progress.json"
    config.PROGRESS_DB_FILE = state_dir / "progress.sqlite3"
    config.SEARCH_CACHE_DIR = state_dir / "search_cache"
    config.CRAWL_CACHE_DIR = state_dir / "crawl_cache"
    config.EMAIL_CACHE_DIR = state_dir / "email_cache"


def _prompt_api_state(label: str, input_fn=input) -> bool:
    active_answers = {"y", "yes", "aktif", "a", "evet", "e", "1"}
    inactive_answers = {"n", "no", "deaktif", "pasif", "d", "hayir", "hayır", "h", "0"}
    while True:
        answer = input_fn(f"{label} aktif mi? [y/n]: ").strip().casefold()
        if answer in active_answers:
            return True
        if answer in inactive_answers:
            return False
        print("Lütfen 'y' veya 'n' yazın.")


def _prompt_use_saved_key(label: str, input_fn=input) -> bool:
    while True:
        answer = input_fn(f"{label}: kayıtlı API anahtarı kullanılsın mı? [y/n]: ").strip().casefold()
        if answer in {"y", "yes", "evet", "e", "1"}:
            return True
        if answer in {"n", "no", "hayir", "hayır", "h", "0"}:
            return False
        print("Lütfen 'y' veya 'n' yazın.")


def _prompt_api_key(label: str, secret_fn=getpass.getpass) -> str:
    while True:
        api_key = secret_fn(f"{label} API anahtarı (gizli): ").strip()
        if api_key:
            return api_key
        print("API aktifken anahtar boş bırakılamaz.")


def _load_saved_api_keys() -> dict[str, str]:
    try:
        payload = json.loads(config.SAVED_API_KEYS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    try:
        payload = secrets_store.decode(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def _save_api_keys(api_keys: dict[str, str]) -> None:
    clean = {key: value for key, value in api_keys.items() if value}
    config.SAVED_API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = secrets_store.encode(clean)
    except OSError:
        # Non-Windows development environments retain compatibility. Production
        # on the supported Windows target always uses user-scoped DPAPI.
        payload = clean
    config.SAVED_API_KEYS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_resolver_settings() -> dict[str, bool]:
    """Load non-secret persisted switches for optional company resolvers."""
    try:
        payload = json.loads(config.RESOLVER_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if key in {"brandfetch_domain_search", "hunter_domain_finder"}
        and isinstance(value, bool)
    }


def _apply_saved_resolver_configuration(saved: dict[str, str] | None = None) -> dict[str, bool]:
    """Apply persisted resolver switches and DPAPI-protected credentials.

    Environment-provided credentials remain the first choice. Persisted switches
    only enable discovery resolvers when a corresponding credential is present.
    """
    saved = saved if saved is not None else _load_saved_api_keys()
    settings = _load_resolver_settings()
    config.BRANDFETCH_CLIENT_ID = config.BRANDFETCH_CLIENT_ID or saved.get("brandfetch", "")
    config.HUNTER_API_KEY = config.HUNTER_API_KEY or saved.get("hunter", "")

    brandfetch_requested = settings.get(
        "brandfetch_domain_search", config.ENABLE_BRANDFETCH_DOMAIN_SEARCH,
    )
    hunter_requested = settings.get(
        "hunter_domain_finder", config.ENABLE_HUNTER_DOMAIN_FINDER,
    )
    config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = bool(
        brandfetch_requested and config.BRANDFETCH_CLIENT_ID
    )
    config.ENABLE_HUNTER_DOMAIN_FINDER = bool(
        hunter_requested and config.HUNTER_API_KEY
    )
    return {
        "brandfetch_domain_search": config.ENABLE_BRANDFETCH_DOMAIN_SEARCH,
        "hunter_domain_finder": config.ENABLE_HUNTER_DOMAIN_FINDER,
    }


def _saved_or_prompted_key(
    label: str,
    key_name: str,
    current_value: str,
    saved: dict[str, str],
    input_fn,
    secret_fn,
) -> str:
    api_key = current_value or saved.get(key_name, "")
    if api_key and _prompt_use_saved_key(label, input_fn):
        print(f"{label}: kayıtlı API anahtarı kullanılacak.")
        return api_key
    if api_key:
        print(f"{label}: yeni API anahtarı girildiğinde kayıtlı anahtar değiştirilecek.")
    api_key = _prompt_api_key(label, secret_fn)
    saved[key_name] = api_key
    return api_key


def configure_apis_interactively(input_fn=input, secret_fn=getpass.getpass) -> None:
    saved = _load_saved_api_keys()
    resolver_states = _apply_saved_resolver_configuration(saved)
    google_active = _prompt_api_state("Google Places API", input_fn)
    config.ENABLE_GOOGLE_PLACES = google_active
    config.GOOGLE_PLACES_API_KEY = (
        _saved_or_prompted_key(
            "Google Places", "google_places", config.GOOGLE_PLACES_API_KEY, saved, input_fn, secret_fn
        )
        if google_active
        else ""
    )

    brightdata_active = _prompt_api_state("Bright Data API", input_fn)
    config.SEARCH_PROVIDER = "brightdata" if brightdata_active else "ddgs"
    config.BRIGHTDATA_API_KEY = (
        _saved_or_prompted_key(
            "Bright Data", "brightdata", config.BRIGHTDATA_API_KEY, saved, input_fn, secret_fn
        )
        if brightdata_active
        else ""
    )
    if google_active:
        saved["google_places"] = config.GOOGLE_PLACES_API_KEY
    if brightdata_active:
        saved["brightdata"] = config.BRIGHTDATA_API_KEY
    if saved:
        _save_api_keys(saved)

    print(
        "Koşu ayarları: "
        f"Google Places={'aktif' if google_active else 'deaktif'}, "
        f"Bright Data={'aktif' if brightdata_active else 'deaktif'}, "
        f"Brandfetch={'aktif' if resolver_states['brandfetch_domain_search'] else 'deaktif'}, "
        f"Hunter Domain Finder={'aktif' if resolver_states['hunter_domain_finder'] else 'deaktif'}"
    )


def _deduplicate_company_records(records: list[dict]) -> tuple[list[dict], int]:
    """Merge duplicate fair rows without losing the richer metadata record."""
    unique: dict[str, dict] = {}
    duplicate_count = 0
    for record in records:
        key = scorer.normalize_text(record.get("company", "")).strip()
        if not key:
            continue
        if key not in unique:
            unique[key] = dict(record)
            continue
        duplicate_count += 1
        current = unique[key]
        for field, value in record.items():
            if not current.get(field) and value:
                current[field] = value
        sources = list(dict.fromkeys(filter(None, [current.get("source", ""), record.get("source", "")])))
        if sources:
            current["source"] = ";".join(sources)
    return list(unique.values()), duplicate_count


_PAID_ESCALATION_STATUSES = {
    "REVIEW_NEEDED", "WEBSITE_NOT_FOUND", "WEBSITE_AMBIGUOUS",
    "WEBSITE_FETCH_FAILED",
}


def _needs_paid_escalation(row: dict) -> bool:
    return bool(
        not row.get("__paid_escalation_complete")
        and (
            row.get("status") in _PAID_ESCALATION_STATUSES
            or row.get("reason") == "no_candidate_proved_target_fingerprint"
        )
    )


def _result_quality_key(row: dict) -> tuple[int, ...]:
    status = str(row.get("status", ""))
    return (
        int(status in report.OK_STATUSES),
        int(bool(row.get("publication_eligible"))),
        int(bool(row.get("website"))),
        int(bool(row.get("email"))) + int(bool(row.get("phone"))),
        int(row.get("score") or 0),
        -int(row.get("reason") == "no_candidate_proved_target_fingerprint"),
    )


def run(
    input_file: Path,
    output_dir: Path | None = None,
    companies: set[str] | None = None,
    only_statuses: set[str] | None = None,
) -> str:
    if output_dir:
        _set_output_dir(output_dir)
    ensure_directories()
    runtime.reset()
    discovery_coverage.reset()
    replay_snapshot.reset()
    if config.REPLAY_SNAPSHOT_INPUT:
        replay_snapshot.load(
            Path(config.REPLAY_SNAPSHOT_INPUT),
            max_uncompressed_bytes=config.REPLAY_SNAPSHOT_MAX_UNCOMPRESSED_BYTES,
        )
    search.reset_source_health()
    search.reset_candidate_host_observations()
    linkedin_company.reset()
    logger = setup_logging()
    start_time = time.monotonic()
    company_records = excel.read_company_records(input_file)
    company_records, duplicate_count = _deduplicate_company_records(company_records)
    if duplicate_count:
        runtime.record("input.duplicates_removed", duplicate_count)
        logger.info("Removed %s duplicate company rows before processing", duplicate_count)
    if companies:
        wanted = {value.casefold() for value in companies}
        company_records = [record for record in company_records if record["company"].casefold() in wanted]
    if only_statuses:
        previous_statuses = excel.read_result_statuses(config.CONTACTS_FILE)
        allowed = {value.casefold() for value in only_statuses}
        company_records = [
            record for record in company_records
            if previous_statuses.get(record["company"].casefold(), "").casefold() in allowed
        ]
    if not company_records:
        raise RuntimeError(f"No companies matched the requested selection in {input_file}")
    scorer.configure_company_token_frequencies([
        record["company"] for record in company_records
    ])
    paid_query_limit = search.configure_run_budget(len(company_records))
    paid_settings = {
        "search_provider": config.SEARCH_PROVIDER,
        "google_places": config.ENABLE_GOOGLE_PLACES,
        "brandfetch": config.ENABLE_BRANDFETCH_DOMAIN_SEARCH,
        "hunter_domain": config.ENABLE_HUNTER_DOMAIN_FINDER,
    }
    paid_escalation_enabled = bool(
        config.SEARCH_CACHE_MODE != "replay"
        and (
            paid_settings["search_provider"] == "brightdata"
            or (paid_settings["google_places"] and config.GOOGLE_PLACES_API_KEY)
            or paid_settings["brandfetch"]
            or paid_settings["hunter_domain"]
        )
    )

    source_preflight = search.preflight_source_profiles(company_records)
    for health in source_preflight:
        logger.info(
            "Source profile preflight: host=%s status=%s server_errors=%s circuit_open=%s",
            health.get("host", ""), health.get("status", "unknown"),
            health.get("server_errors", 0), health.get("circuit_open", False),
        )

    run_signature = json.dumps(
        {
            "companies": sorted(record["company"] for record in company_records),
            "search_cache": config.SEARCH_CACHE_MODE,
            "crawl_cache": config.CRAWL_CACHE_MODE,
            "brightdata_budget": config.BRIGHTDATA_REQUEST_BUDGET,
            "linkedin_company_budget": config.LINKEDIN_COMPANY_REQUEST_BUDGET,
            "linkedin_company_enabled": config.ENABLE_LINKEDIN_COMPANY_LOOKUP,
            "paid_query_limit_per_company": paid_query_limit,
            "google_places_budget": config.GOOGLE_PLACES_REQUEST_BUDGET,
            "brandfetch_budget": config.BRANDFETCH_REQUEST_BUDGET,
            "hunter_budget": config.HUNTER_REQUEST_BUDGET,
            "replay_snapshot": str(config.REPLAY_SNAPSHOT_INPUT or ""),
            "two_pass_paid_escalation": paid_escalation_enabled,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    progress = checkpoint.load_progress(input_file, run_signature)
    results_by_index: dict[int, dict] = {}
    start_index = 0
    if progress:
        # Keep interrupted runs portable as well: the snapshot is checkpointed
        # beside the output and merged only after the matching progress
        # signature has been accepted.
        if not config.REPLAY_SNAPSHOT_INPUT and config.REPLAY_SNAPSHOT_FILE.exists():
            replay_snapshot.load(
                config.REPLAY_SNAPSHOT_FILE,
                max_uncompressed_bytes=config.REPLAY_SNAPSHOT_MAX_UNCOMPRESSED_BYTES,
            )
        results_so_far = progress.get("results_so_far", [])
        for offset, row in enumerate(results_so_far):
            idx = int(row.get("__index", offset))
            results_by_index[idx] = row
        start_index = int(progress.get("last_completed_index", -1)) + 1
        logger.info("Resuming from index %s", start_index)

    pending = [
        (idx, record)
        for idx, record in enumerate(company_records)
        if idx not in results_by_index and idx >= start_index
    ]
    def execute_phase(items: list[tuple[int, dict]], *, paid_phase: bool) -> None:
        if not items:
            return
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_company, idx, record["company"], logger, record.get("website", ""), record): idx
                for idx, record in items
            }
            for future in as_completed(futures):
                try:
                    idx, row = future.result()
                except Exception as exc:
                    idx = futures[future]
                    company = company_records[idx]["company"]
                    logger.exception("Unhandled processing failure for %s", company)
                    row = _empty_result(
                        company,
                        "PROCESSING_FAILED",
                        f"{exc.__class__.__name__}: {exc}",
                    )
                row["__index"] = idx
                row["__paid_escalation_complete"] = bool(
                    paid_phase or not paid_escalation_enabled
                    or row.get("status") in report.OK_STATUSES
                )
                if paid_phase and idx in results_by_index:
                    previous = results_by_index[idx]
                    if _result_quality_key(previous) > _result_quality_key(row):
                        previous["__paid_escalation_complete"] = True
                        row = previous
                results_by_index[idx] = row
                checkpoint.save_result(input_file, idx, row, run_signature)
                if (
                    len(results_by_index)
                    % config.REPLAY_SNAPSHOT_CHECKPOINT_INTERVAL
                    == 0
                ):
                    replay_snapshot.write(config.REPLAY_SNAPSHOT_FILE)
                logger.info("Completed %s/%s: %s", len(results_by_index), len(company_records), row["company"])

    try:
        if paid_escalation_enabled:
            config.SEARCH_PROVIDER = "ddgs"
            config.ENABLE_GOOGLE_PLACES = False
            config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = False
            config.ENABLE_HUNTER_DOMAIN_FINDER = False
            runtime.record("pipeline.free_pass_companies", len(pending))
        execute_phase(pending, paid_phase=False)

        config.SEARCH_PROVIDER = paid_settings["search_provider"]
        config.ENABLE_GOOGLE_PLACES = paid_settings["google_places"]
        config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = paid_settings["brandfetch"]
        config.ENABLE_HUNTER_DOMAIN_FINDER = paid_settings["hunter_domain"]
        escalation = [
            (idx, company_records[idx])
            for idx, row in sorted(results_by_index.items())
            if _needs_paid_escalation(row)
        ]
        if paid_escalation_enabled and escalation:
            runtime.record("pipeline.paid_escalation_companies", len(escalation))
            search.scale_paid_api_budgets(len(escalation))
            search.configure_run_budget(len(escalation))
            execute_phase(escalation, paid_phase=True)
    except KeyboardInterrupt:
        replay_snapshot.write(config.REPLAY_SNAPSHOT_FILE)
        logger.warning("Interrupted. Progress checkpoint was saved.")
        raise
    finally:
        config.SEARCH_PROVIDER = paid_settings["search_provider"]
        config.ENABLE_GOOGLE_PLACES = paid_settings["google_places"]
        config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = paid_settings["brandfetch"]
        config.ENABLE_HUNTER_DOMAIN_FINDER = paid_settings["hunter_domain"]

    rows = [results_by_index[i] for i in range(len(company_records))]
    report_text = _write_outputs(rows, time.monotonic() - start_time)
    checkpoint.clear_progress()
    return report_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B2B Contact Finder")
    parser.add_argument("--input", type=Path, default=config.INPUT_FILE, help="Path to firms.xlsx")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory")
    parser.add_argument(
        "--run-state-dir",
        type=Path,
        default=None,
        help="Run-specific checkpoint and cache directory",
    )
    parser.add_argument("--companies", default="", help="Comma-separated exact company names")
    parser.add_argument("--only-status", default="", help="Run statuses found in the output directory's existing contacts.xlsx")
    parser.add_argument(
        "--search-cache", choices=("use", "refresh", "off", "replay"), default="use",
        help="Persistent SERP/Places cache mode",
    )
    parser.add_argument(
        "--crawl-cache", choices=("use", "refresh", "off", "replay"), default="use",
        help="Persistent official-site page cache mode",
    )
    parser.add_argument("--rerank-cache", action="store_true", help="Offline replay: do not make search or crawl requests")
    parser.add_argument(
        "--replay-snapshot",
        type=Path,
        default=None,
        help="Portable replay_snapshot.json.gz created by an earlier run",
    )
    parser.add_argument("--brightdata-budget", type=int, default=config.BRIGHTDATA_REQUEST_BUDGET, help="Maximum paid Bright Data HTTP requests for this run")
    parser.add_argument("--linkedin-company-budget", type=int, default=config.LINKEDIN_COMPANY_REQUEST_BUDGET, help="Maximum Bright Data LinkedIn Company requests for this run")
    parser.add_argument("--google-places-budget", type=int, default=config.GOOGLE_PLACES_REQUEST_BUDGET, help="Maximum paid Google Places requests for this run")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.run_state_dir:
        _set_run_state_dir(args.run_state_dir)
    config.SEARCH_CACHE_MODE = "replay" if args.rerank_cache else args.search_cache
    config.CRAWL_CACHE_MODE = "replay" if args.rerank_cache else args.crawl_cache
    config.BRIGHTDATA_REQUEST_HARD_CAP = max(0, args.brightdata_budget)
    config.GOOGLE_PLACES_REQUEST_HARD_CAP = max(0, args.google_places_budget)
    config.BRIGHTDATA_REQUEST_BUDGET = config.BRIGHTDATA_REQUEST_HARD_CAP
    config.LINKEDIN_COMPANY_REQUEST_HARD_CAP = max(0, args.linkedin_company_budget)
    config.LINKEDIN_COMPANY_REQUEST_BUDGET = config.LINKEDIN_COMPANY_REQUEST_HARD_CAP
    config.GOOGLE_PLACES_REQUEST_BUDGET = config.GOOGLE_PLACES_REQUEST_HARD_CAP
    config.REPLAY_SNAPSHOT_INPUT = args.replay_snapshot
    if args.rerank_cache:
        config.MIN_DELAY_SEC = 0
        config.MAX_DELAY_SEC = 0
    configure_apis_interactively()
    selected_companies = {value.strip() for value in args.companies.split(",") if value.strip()}
    selected_statuses = {value.strip() for value in args.only_status.split(",") if value.strip()}
    print(run(args.input, args.output_dir, selected_companies or None, selected_statuses or None))
