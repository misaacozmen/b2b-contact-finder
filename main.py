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
from modules import checkpoint, crawler, email_verifier, entity_registry, evidence, excel, extractor, identity, phone, report, runtime, search, secrets_store
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
        "email_verification": "not_checked",
        "email_verification_reason": "no_email",
        "phone": "",
        "phone_source": "",
        "phone_source_url": "",
        "phone_label": "",
        "alternative_phones": "",
        "status": status,
        "confidence": "none",
        "score": score,
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
    return not any(domain == bad or domain.endswith(f".{bad}") for bad in config.BAD_EMAIL_DOMAINS)


def _select_best_email(company: str, website: str, emails: list[str]) -> str:
    website_domain = scorer.normalize_domain(urlparse(website).netloc or website)
    website_root = scorer.compact_domain_core(website_domain)
    tokens = scorer.distinctive_tokens(company)
    candidates = [email for email in dict.fromkeys(emails) if _email_is_usable(email)]
    if not candidates:
        return ""

    def rank(email: str) -> tuple[int, int, str]:
        local, domain = email.split("@", 1)
        email_domain = scorer.normalize_domain(domain)
        email_root = scorer.compact_domain_core(email_domain)
        email_text = scorer.normalize_text(f"{local} {email_root}")
        score = 0

        if email_root and website_root and (email_root == website_root or email_root in website_root or website_root in email_root):
            score += 80
        if any(token in email_text for token in tokens):
            score += 35
        if email_domain.endswith((".com.tr", ".tr")):
            score += 5

        prefix = local.split(".", 1)[0].split("-", 1)[0].lower()
        try:
            priority = config.EMAIL_PRIORITY_PREFIXES.index(prefix)
        except ValueError:
            priority = len(config.EMAIL_PRIORITY_PREFIXES)
        return score, -priority, email

    ranked = sorted(candidates, key=rank, reverse=True)
    best = ranked[0]
    best_score = rank(best)[0]
    return best if best_score >= 15 else ""


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
    }
    for page in pages:
        identity = extractor.extract_organization_evidence(page.get("html", ""))
        for key in combined:
            combined[key].extend(identity.get(key, []))
    combined = {key: list(dict.fromkeys(values)) for key, values in combined.items()}
    relationship_text = " ".join([
        *combined["ownership_statements"], *combined["legal_names"],
        *combined["brand_names"], *combined["related_organizations"],
    ])
    if relationship_text and (
        scorer.legal_name_phrase_match(company, relationship_text)
        or scorer.ownership_statement_match(company, relationship_text)
    ):
        return 14, "structured_identity_strong:1/1@scope=declared_relationship", combined
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
    label_priority = {
        "headquarters": 100, "general": 90, "specialist": 85,
        "sales": 82, "export": 82, "istanbul": 78, "izmir": 78,
        "ankara": 78, "whatsapp": 75, "factory": 65, "branch": 60, "owner": 40,
    }
    by_number: dict[str, dict] = {}

    def number_type_priority(value: str) -> int:
        digits = re.sub(r"\D", "", value)
        if re.fullmatch(r"0?444\d{4}", digits) or digits.startswith("0850"):
            return 3
        if digits.startswith(("02", "03", "04")):
            return 2
        if digits.startswith("05"):
            return 1
        return 0
    for record in records:
        normalized = phone.normalize_phone(record.get("value", ""))
        if not normalized:
            continue
        item = {
            "value": normalized,
            "label": record.get("label", "general"),
            "source_url": record.get("source_url", ""),
        }
        if item["label"] == "fax":
            continue
        current = by_number.get(normalized)
        if current is None or label_priority.get(item["label"], 0) > label_priority.get(current["label"], 0):
            by_number[normalized] = item
    return sorted(
        by_number.values(),
        key=lambda item: (
            label_priority.get(item["label"], 0),
            number_type_priority(item["value"]),
            1 if any(marker in item["source_url"].casefold() for marker in ("contact", "iletisim", "iletişim")) else 0,
        ),
        reverse=True,
    )


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
    return -20, f"metadata_context_missing:0/{len(metadata_contexts)}"


def _email_domain_bonus(website: str, email: str) -> tuple[int, str]:
    if not email:
        return 0, "no_email"
    website_root = scorer.compact_domain_core(website)
    email_root = scorer.compact_domain_core(_email_domain(email))
    if email_root and website_root and (email_root == website_root or email_root in website_root or website_root in email_root):
        return 10, "email_domain_match"
    return -12, "email_domain_mismatch"


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

    reasons = evaluation["reasons"]
    assessment = evaluation.get("identity_assessment") or identity.assess(
        evaluation.get("candidate", {}).get("_identity_company", ""),
        evaluation.get("candidate", {}),
        reasons,
        evaluation.get("structured_identity", {}),
    )
    if assessment.get("strong_first_party_bundle"):
        return False
    if any(reason.startswith(("context_conflict:", "metadata_context_conflict:")) for reason in reasons):
        return True
    metadata_missing = any(reason.startswith("metadata_context_missing:") for reason in reasons)
    page_identity_strong = any(reason.startswith("page_identity_strong:") for reason in reasons)
    structured_identity_strong = any(reason.startswith("structured_identity_strong:") for reason in reasons)
    email_domain_match = "email_domain_match" in reasons
    if _has_trusted_website_evidence(evaluation["candidate"], reasons) and (
        page_identity_strong or structured_identity_strong or email_domain_match
    ):
        return False
    if metadata_missing and _has_trusted_website_evidence(evaluation["candidate"], reasons):
        return False
    return True


def _has_trusted_website_evidence(
    candidate: dict,
    reasons: list[str],
    *,
    unique_candidate: bool = False,
) -> bool:
    """Verify independent evidence, or a strong first-party bundle after uniqueness."""
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
    row["email_verification"] = "not_checked"
    row["email_verification_reason"] = "website_not_found"
    row["phone"] = ""
    row["phone_source"] = ""
    row["phone_source_url"] = ""
    row["phone_label"] = ""
    row["alternative_phones"] = ""


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
        "legal_name_phrase_match:", "legal_name_ownership_match:",
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
    preliminary_reasons = [page_reason, context_reason, email_reason, structured_reason, legal_name_reason, country_reason]
    strong_first_party_identity = any(reason.startswith((
        "page_identity_strong:", "structured_identity_medium:",
        "structured_identity_strong:", "legal_name_phrase_match:",
        "legal_name_ownership_match:",
    )) for reason in preliminary_reasons)
    if (
        context_reason.startswith("metadata_context_missing:")
        and strong_first_party_identity
        and _has_trusted_website_evidence(candidate, preliminary_reasons)
    ):
        context_bonus = 0
        context_reason = f"{context_reason}_softened"
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
) -> dict:
    candidate["_identity_company"] = company
    crawl_result = crawler.fetch_site(
        candidate["url"], candidate.get("_contact_seed_urls", []), profile=crawl_profile,
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
            contacts = extractor.extract_contact_records(page["html"], page["url"])
            email_records.extend(contacts["emails"])
            phone_records.extend(contacts["phones"])

    emails = list(dict.fromkeys(
        record["value"] for record in email_records if _email_is_usable(record["value"])
    ))
    selected_email = _select_best_email(company, crawl_result["url"], emails)
    email_source = "website" if selected_email else ""
    matching_email_records = [record for record in email_records if record["value"] == selected_email]
    selected_email_record = max(
        matching_email_records,
        key=lambda record: 1 if any(marker in record.get("source_url", "").casefold() for marker in ("contact", "iletisim", "iletişim")) else 0,
        default={},
    )
    email_source_url = selected_email_record.get("source_url", "")
    email_verification = (
        email_verifier.verify_email(selected_email)
        if verify_email_domain
        else {"status": "not_checked", "reason": "identity_phase"}
    )
    if email_verification["status"] == "invalid_domain":
        selected_email = ""
        email_source = ""
        email_source_url = ""
    ranked_phone_records = _select_phone_records(phone_records)
    normalized_phones = [record["value"] for record in ranked_phone_records]
    phone_source = "website" if normalized_phones else ""
    # Contact values must come from the crawled official site.  Third-party
    # directory data (such as Google Places or Hunter) is not published.
    final_score, reasons = _score_candidate_with_site(company, candidate, crawl_result, selected_email, normalized_phones, metadata)
    context_failed = "context_gate_failed" in reasons
    email_failed = "email_gate_failed" in reasons

    structured_identity = _structured_identity_score(company, crawl_result["pages"])[2]
    candidate["_structured_identity"] = structured_identity
    identity_assessment = identity.assess(company, candidate, reasons, structured_identity)
    reasons.append(
        f"identity_evidence:{identity_assessment['support_count']};"
        f"decision:{identity_assessment['decision']}"
    )
    return {
        "candidate": candidate,
        "crawl_result": crawl_result,
        "email": selected_email,
        "email_source": email_source,
        "email_source_url": email_source_url,
        "alternative_emails": [value for value in emails if value != selected_email],
        "email_verification": email_verification["status"],
        "email_verification_reason": email_verification["reason"],
        "phone": normalized_phones[0] if normalized_phones else "",
        "phone_source": phone_source if normalized_phones else "",
        "phone_source_url": ranked_phone_records[0]["source_url"] if ranked_phone_records else "",
        "phone_label": ranked_phone_records[0]["label"] if ranked_phone_records else "",
        "alternative_phones": ranked_phone_records[1:],
        "final_score": final_score,
        "reasons": reasons,
        "has_contact": bool(selected_email or normalized_phones),
        "context_failed": context_failed,
        "email_failed": email_failed,
        "structured_identity": structured_identity,
        "identity_assessment": identity_assessment,
    }


def _contact_output_fields(evaluation: dict) -> dict:
    alternative_phones = evaluation.get("alternative_phones", [])
    return {
        "email": evaluation.get("email", ""),
        "email_source": evaluation.get("email_source", ""),
        "email_source_url": evaluation.get("email_source_url", ""),
        "alternative_emails": "; ".join(evaluation.get("alternative_emails", [])),
        "email_verification": evaluation.get("email_verification", "not_checked"),
        "email_verification_reason": evaluation.get("email_verification_reason", ""),
        "phone": evaluation.get("phone", ""),
        "phone_source": evaluation.get("phone_source", ""),
        "phone_source_url": evaluation.get("phone_source_url", ""),
        "phone_label": evaluation.get("phone_label", ""),
        "alternative_phones": "; ".join(
            f"{item.get('value', '')} [{item.get('label', 'general')}]"
            for item in alternative_phones
        ),
    }


def _evaluation_evidence(evaluation: dict) -> dict:
    crawl_result = evaluation.get("crawl_result", {})
    return {
        "candidate": evaluation.get("candidate", {}),
        "final_score": evaluation.get("final_score", 0),
        "reasons": evaluation.get("reasons", []),
        "structured_identity": evaluation.get("structured_identity", {}),
        "identity_assessment": evaluation.get("identity_assessment", {}),
        "crawl": {
            "url": crawl_result.get("url", ""),
            "cache_status": crawl_result.get("cache_status", ""),
            "error": crawl_result.get("error", ""),
            "pages": [page.get("url", "") for page in crawl_result.get("pages", [])],
        },
        "contacts": _contact_output_fields(evaluation),
    }


def _evaluate_candidate_with_stage(
    company: str,
    candidate: dict,
    metadata: dict | None = None,
    crawl_profile: str = "full",
    verify_email_domain: bool = True,
) -> dict:
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
    full_strength = (
        1 if full_assessment.get("provisionally_publishable") else 0,
        full_assessment.get("support_count", 0),
    )
    identity_strength = (
        1 if identity_assessment.get("provisionally_publishable") else 0,
        identity_assessment.get("support_count", 0),
    )
    if identity_strength <= full_strength:
        return full_evaluation

    identity_prefixes = (
        "page_identity_", "structured_identity_", "legal_name_phrase_",
        "legal_name_ownership_", "identity_evidence:",
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
    direct_edges = {
        "same_verified_entity",
        "first_party_domain_link", "shared_legal_identifier",
    }
    operational_edges = {
        "cross_domain_first_party_email", "shared_phone", "shared_structured_address",
    }
    related = not conflicts and (
        bool(set(edges) & direct_edges)
        or len(set(edges) & operational_edges) >= 2
    )
    return {"related": related, "edges": list(dict.fromkeys(edges)), "conflicts": conflicts}


def _same_official_family(first: dict, second: dict, company: str = "") -> bool:
    return bool(_official_family_evidence(first, second, company)["related"])


def _homonym_conflict(company: str, first: dict, second: dict) -> dict:
    """Detect two plausible first-party sites for the same public name."""
    first_domain = scorer.normalize_domain(first.get("candidate", {}).get("url", ""))
    second_domain = scorer.normalize_domain(second.get("candidate", {}).get("url", ""))
    if not first_domain or not second_domain or first_domain == second_domain:
        return {"ambiguous": False, "reason": "same_domain"}
    family = _official_family_evidence(first, second, company)
    if family["related"]:
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
            "legal_name_phrase_match:", "legal_name_ownership_match:",
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
    if _same_official_family(first, second, company):
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


def _unreachable_homonym_conflict(company: str, selected: dict, evaluations: list[dict]) -> dict | None:
    """Keep an inaccessible same-name domain from being silently outranked."""
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
        other_emails = [item.get("email", ""), *item.get("alternative_emails", [])]
        primary["alternative_emails"] = list(dict.fromkeys([
            *primary.get("alternative_emails", []),
            *(email for email in other_emails if email and email != primary.get("email", "")),
        ]))
        if not primary.get("email") and item.get("email"):
            for key in ("email", "email_source", "email_source_url", "email_verification", "email_verification_reason"):
                primary[key] = item.get(key, "")

        other_phones = []
        if item.get("phone"):
            other_phones.append({
                "value": item["phone"], "label": item.get("phone_label", "general"),
                "source_url": item.get("phone_source_url", ""),
            })
        other_phones.extend(item.get("alternative_phones", []))
        known = {phone_item.get("value", "") for phone_item in primary.get("alternative_phones", [])}
        known.add(primary.get("phone", ""))
        primary.setdefault("alternative_phones", []).extend(
            phone_item for phone_item in other_phones if phone_item.get("value", "") not in known
        )
        if not primary.get("phone") and item.get("phone"):
            for key in ("phone", "phone_source", "phone_source_url", "phone_label"):
                primary[key] = item.get(key, "")
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


def _reason_strength(reasons: list[str], prefix: str) -> int:
    levels = {"strong": 3, "medium": 2, "weak": 1, "match": 2}
    for reason in reasons:
        if not reason.startswith(prefix):
            continue
        return max((value for label, value in levels.items() if label in reason), default=0)
    return 0


def _evaluation_rank_key(company: str, item: dict) -> tuple[int, ...]:
    candidate_reason = item["candidate"].get("reason", "")
    intrinsic_domain_evidence = (
        "domain_hits:" in candidate_reason and "search_text_identity:" not in candidate_reason
        and "explicit_cross_domain_redirect:" not in candidate_reason
    ) or scorer.domain_identity_match(company, item["candidate"]["url"])[0]
    candidate = item["candidate"]
    reasons = item["reasons"]
    assessment = item.get("identity_assessment") or identity.assess(
        company, candidate, reasons, item.get("structured_identity", {}),
    )
    return (
        0 if candidate.get("role") in {"directory", "fair_profile", "news"} else 1,
        assessment.get("support_count", 0),
        0 if (
            any(reason.startswith("structured_identity_unmatched:") for reason in reasons)
            and not any(reason.startswith("legal_name_ownership_match:") for reason in reasons)
        ) else 1,
        1 if assessment.get("provisionally_publishable") else 0,
        0 if _is_hard_context_failure(item) else 1,
        1 if candidate.get("query") in {"verified_entity", "verified_alias"} else 0,
        # A bridge-labelled role must not outrank a direct candidate merely
        # because discovery classified it as ``company_candidate``.  Stronger
        # first-party ownership still wins via the support components above.
        0 if "discovery_only_not_identity_authority" in candidate_reason else 1,
        _reason_strength(reasons, "context_"),
        1 if candidate.get("role") == "company_candidate" else 0,
        _reason_strength(reasons, "page_identity_"),
        _reason_strength(reasons, "structured_identity_"),
        _reason_strength(reasons, "legal_name_phrase_"),
        1 if intrinsic_domain_evidence else 0,
        candidate.get("_official_query_evidence", 0),
        item["final_score"],
        candidate.get("_metadata_context_matches", 0),
        1 if item["has_contact"] else 0,
        0 if item["email_failed"] else 1,
    )


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

    crawl_result = evaluation["crawl_result"]
    reasons = evaluation["reasons"]
    final_score = evaluation["final_score"]
    has_contact = evaluation["has_contact"]
    identity_verified = _has_trusted_website_evidence(candidate, reasons)
    if _is_hard_context_failure(evaluation):
        status, confidence = "REVIEW_NEEDED", "review"
    elif evaluation["email_failed"]:
        status, confidence = "REVIEW_NEEDED", "review"
    else:
        status, confidence = _confidence_status(
            final_score, has_contact, reasons,
            identity_verified,
        )

    row = {
        "company": company,
        "website": crawl_result["url"],
        "website_source": candidate["query"],
        **_contact_output_fields(evaluation),
        "status": status,
        "confidence": confidence,
        "score": final_score,
        "reason": "; ".join(reason for reason in reasons if reason),
        "__evaluation": _evaluation_evidence(evaluation),
    }
    if status == "WEBSITE_NOT_FOUND":
        _clear_unpublished_contacts(row)
    elif not identity_verified:
        row["selected_website"] = crawl_result["url"]
        _clear_unpublished_contacts(row)
        row["email_verification_reason"] = "website_identity_unverified"
    return index, _attach_candidates(row, [candidate])


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
        if candidate.get("role") not in identity.EXCLUDED_ROLES
    ]
    best = selectable_candidates[0] if selectable_candidates else None
    if not best or best["score"] < config.MIN_ACCEPT_SCORE:
        random_delay()
        row = _empty_result(company, "WEBSITE_NOT_FOUND", "No candidate passed score threshold")
        return index, _attach_candidates(row, candidates)

    eligible_candidates = [
        candidate
        for candidate in selectable_candidates
        if candidate["score"] >= best["score"] - config.MAX_CANDIDATE_SCORE_GAP
    ][: config.MAX_CANDIDATE_EVALUATIONS]
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
    full_candidates = [
        item["candidate"] for item in ranked_identity[: config.MAX_FULL_CANDIDATE_EVALUATIONS]
    ]
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
    best_eval = ranked_evaluations[0]
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
    ambiguity_match: tuple[dict, dict, int, dict] | None = None
    for second_eval in ranked_evaluations[1:]:
        best_key = _evaluation_rank_key(company, best_eval)
        second_key = _evaluation_rank_key(company, second_eval)
        score_gap = abs(best_eval["final_score"] - second_eval["final_score"])
        same_non_score_evidence = best_key[:13] + best_key[14:] == second_key[:13] + second_key[14:]
        different_domains = scorer.normalize_domain(best_eval["candidate"]["url"]) != scorer.normalize_domain(second_eval["candidate"]["url"])
        homonym = _homonym_conflict(company, best_eval, second_eval)
        if (
            homonym.get("ambiguous")
            or _close_identity_margin_conflict(company, best_eval, second_eval)
            or (
                same_non_score_evidence and different_domains
                and score_gap <= config.AMBIGUOUS_CANDIDATE_MARGIN
                and not _same_official_family(best_eval, second_eval, company)
            )
        ):
            ambiguity_match = (best_eval, second_eval, score_gap, homonym)
            break
    if ambiguity_match:
        best_eval, second_eval, score_gap, homonym = ambiguity_match
        row = _empty_result(
            company,
            "WEBSITE_AMBIGUOUS",
            (
                f"{homonym.get('reason', 'candidate_margin_conflict')}:{score_gap}; "
                f"{best_eval['candidate']['url']} vs {second_eval['candidate']['url']}"
            ),
            best_eval["final_score"],
        )
        row["confidence"] = "review"
        row["__evaluation"] = {
            "ambiguous_candidates": [
                _evaluation_evidence(best_eval),
                _evaluation_evidence(second_eval),
            ],
            "homonym_assessment": homonym,
        }
        random_delay()
        return index, _attach_candidates(row, candidates)
    _merge_official_family_contacts(best_eval, ranked_evaluations[1:], company)
    crawl_result = best_eval["crawl_result"]
    selected_email = best_eval["email"]
    selected_phone = best_eval["phone"]
    final_score = best_eval["final_score"]
    reasons = best_eval["reasons"]
    has_contact = best_eval["has_contact"]

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

    unsupported_candidate = _unsupported_search_text_candidate(company, best_eval)
    # All reachable close competitors have already passed through the homonym
    # and score-margin checks above, so the selected candidate is unique here.
    identity_verified = _has_trusted_website_evidence(
        best_eval["candidate"], reasons, unique_candidate=True,
    )
    if unsupported_candidate:
        reasons.append("unsupported_search_text_candidate_rejected")
        status, confidence = "WEBSITE_NOT_FOUND", "none"
    elif unsafe_identity and not _reviewable_authoritative_candidate(company, best_eval["candidate"]):
        reasons.append("unverified_website_candidate_preserved_for_review")
        status, confidence = "REVIEW_NEEDED", "review"
    elif hard_context_failure and not _reviewable_authoritative_candidate(company, best_eval["candidate"]):
        reasons.append("unverified_website_candidate_preserved_for_review")
        status, confidence = "REVIEW_NEEDED", "review"
    elif unsafe_identity or hard_context_failure:
        reasons.append("authoritative_website_preserved_for_review")
        status, confidence = "REVIEW_NEEDED", "review"
    elif best_eval["email_failed"]:
        status, confidence = "REVIEW_NEEDED", "review"
    else:
        status, confidence = _confidence_status(
            final_score, has_contact, reasons,
            identity_verified,
        )
    random_delay()
    row = {
        "company": company,
        "website": crawl_result["url"],
        "website_source": best_eval["candidate"]["query"],
        **_contact_output_fields(best_eval),
        "status": status,
        "confidence": confidence,
        "score": final_score,
        "reason": "; ".join(reason for reason in reasons if reason),
        "__evaluation": _evaluation_evidence(best_eval),
    }
    if status == "WEBSITE_NOT_FOUND":
        _clear_unpublished_contacts(row)
    elif not identity_verified:
        # Unverified domains remain fully visible in website_candidates.xlsx and
        # the review evidence, but are not published as an official website or
        # allowed to leak their first-party-looking contacts into contacts.xlsx.
        row["selected_website"] = crawl_result["url"]
        _clear_unpublished_contacts(row)
        row["email_verification_reason"] = "website_identity_unverified"
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
    evidence.write_jsonl(config.EVIDENCE_FILE, rows)
    entity_registry.write_observations(config.ENTITY_RELATIONSHIPS_FILE, rows)
    for row in rows:
        row.pop("__index", None)
        row.pop("__candidates", None)
        row.pop("__evaluation", None)
        row.pop("__candidate_evaluations", None)
        row.pop("__search_trace", None)
        row.pop("__source_health", None)
    excel.write_contacts(config.CONTACTS_FILE, rows)
    excel.write_contacts(
        config.VERIFIED_CONTACTS_FILE,
        [row for row in rows if row.get("status") in report.OK_STATUSES],
    )
    excel.write_contacts(
        config.REVIEW_QUEUE_FILE,
        [row for row in rows if row.get("status") not in report.OK_STATUSES],
    )
    excel.write_failed(config.FAILED_FILE, report.failed_rows(rows))
    excel.write_website_candidates(config.CANDIDATES_FILE, rows)
    report_text = report.build_report(rows, elapsed_seconds)
    config.REPORT_FILE.write_text(report_text, encoding="utf-8")
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
    search.reset_source_health()
    search.reset_candidate_host_observations()
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
            "google_places_budget": config.GOOGLE_PLACES_REQUEST_BUDGET,
            "brandfetch_budget": config.BRANDFETCH_REQUEST_BUDGET,
            "hunter_budget": config.HUNTER_REQUEST_BUDGET,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    progress = checkpoint.load_progress(input_file, run_signature)
    results_by_index: dict[int, dict] = {}
    start_index = 0
    if progress:
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
    try:
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_company, idx, record["company"], logger, record.get("website", ""), record): idx
                for idx, record in pending
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
                results_by_index[idx] = row
                checkpoint.save_result(input_file, idx, row, run_signature)
                logger.info("Completed %s/%s: %s", len(results_by_index), len(company_records), row["company"])
    except KeyboardInterrupt:
        logger.warning("Interrupted. Progress checkpoint was saved.")
        raise

    rows = [results_by_index[i] for i in range(len(company_records))]
    report_text = _write_outputs(rows, time.monotonic() - start_time)
    checkpoint.clear_progress()
    return report_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B2B Contact Finder")
    parser.add_argument("--input", type=Path, default=config.INPUT_FILE, help="Path to firms.xlsx")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory")
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
    parser.add_argument("--brightdata-budget", type=int, default=config.BRIGHTDATA_REQUEST_BUDGET, help="Maximum paid Bright Data HTTP requests for this run")
    parser.add_argument("--google-places-budget", type=int, default=config.GOOGLE_PLACES_REQUEST_BUDGET, help="Maximum paid Google Places requests for this run")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config.SEARCH_CACHE_MODE = "replay" if args.rerank_cache else args.search_cache
    config.CRAWL_CACHE_MODE = "replay" if args.rerank_cache else args.crawl_cache
    config.BRIGHTDATA_REQUEST_BUDGET = max(0, args.brightdata_budget)
    config.GOOGLE_PLACES_REQUEST_BUDGET = max(0, args.google_places_budget)
    if args.rerank_cache:
        config.MIN_DELAY_SEC = 0
        config.MAX_DELAY_SEC = 0
    configure_apis_interactively()
    selected_companies = {value.strip() for value in args.companies.split(",") if value.strip()}
    selected_statuses = {value.strip() for value in args.only_status.split(",") if value.strip()}
    print(run(args.input, args.output_dir, selected_companies or None, selected_statuses or None))
