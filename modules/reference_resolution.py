"""Corroborate customer supplied reference websites without coupling to main."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

import config
from modules import extractor, phone, reference_inputs, report, scorer


PARKED_MARKERS = (
    "cok yakinda", "coming soon", "under construction", "yapim asamasinda",
    "bakim calismasi", "site bakimda", "alan adi satilik", "domain is for sale",
    "buy this domain", "this domain is parked", "default web site page", "index of /",
    "web sitesi yayinda degil", "website coming soon",
)
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yandex.com",
    "yandex.com.tr", "icloud.com", "mynet.com", "hotmail.com.tr", "windowslive.com",
})
TR_CITY_MARKERS = (
    "turkiye", "turkey", "istanbul", "ankara", "izmir", "bursa", "konya", "kayseri",
    "gaziantep", "adana", "antalya", "kocaeli", "eskisehir", "denizli", "manisa", "inegol",
)
REFERENCE_TIERS = (
    "REFERENCE_VERIFIED", "REFERENCE_ACCEPTED", "REFERENCE_THIN",
    "REFERENCE_CONFLICT", "REFERENCE_UNUSABLE", "REFERENCE_UNREACHABLE",
)
_CONTACT_PREFIXES = (
    "info", "iletisim", "contact", "satis", "sales", "bilgi", "office", "export", "ihracat",
)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        text = str(value or "").strip()
        key = scorer.normalize_text(text)
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))[-10:]


def _fax_numbers(evaluation: dict) -> set[str]:
    numbers: set[str] = set()
    publication = evaluation.get("contact_publication", {})
    for record in publication.get("phones", []) if isinstance(publication, dict) else []:
        if not isinstance(record, dict):
            continue
        nested = record.get("record", {})
        label = str(record.get("label") or nested.get("label", "")).casefold()
        if label == "fax":
            value = record.get("value") or nested.get("value", "")
            normalized = phone.normalize_phone(str(value or ""))
            if normalized:
                numbers.add(normalized)
    return numbers


def observe(evaluation: dict, reference_url: str, metadata: dict) -> dict:
    crawl = evaluation.get("crawl_result", {}) or {}
    pages = crawl.get("pages", []) or []
    nonempty_pages = [page for page in pages if str(page.get("html", "") or "").strip()]
    structured = evaluation.get("structured_identity", {}) or {}
    title_names: list[str] = []
    legal_names = [str(value) for value in structured.get("legal_names", []) if value]
    title_names.extend(str(value) for value in structured.get("names", []) if value)
    title_names.extend(legal_names)
    visible_texts = []
    phones: list[str] = []
    emails: list[str] = []
    for page in pages:
        html_text = str(page.get("html", "") or "")
        if not html_text:
            continue
        visible = extractor._visible_text(html_text)
        visible_texts.append(visible)
        soup = BeautifulSoup(html_text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if title:
            title_names.append(title)
        title_names.extend(
            node.get("content", "")
            for node in soup.select('meta[property="og:site_name"][content]')
        )
        organization = extractor.extract_organization_evidence(
            html_text, str(page.get("url", "") or reference_url),
            str(page.get("retrieval_method", "http") or "http"),
        )
        title_names.extend(organization.get("names", []))
        title_names.extend(organization.get("legal_names", []))
        legal_names.extend(organization.get("legal_names", []))
        phones.extend(
            normalized for raw in extractor.extract_phones(html_text)
            if (normalized := phone.normalize_phone(raw))
        )
        emails.extend(str(value).strip().casefold() for value in extractor.extract_emails(html_text))
    excluded_faxes = _fax_numbers(evaluation)
    unique_phones = [value for value in _unique(phones) if value not in excluded_faxes]
    all_text = scorer.normalize_text(" ".join(visible_texts)[:200_000])
    first_page = pages[0] if pages else {}
    listed_phone, _ = reference_inputs.normalize_reference_phone((metadata or {}).get("listed_phone"))
    listed_email = str((metadata or {}).get("listed_email", "") or "").strip().casefold()
    return {
        "reachable": bool(nonempty_pages),
        "home_text": scorer.normalize_text(visible_texts[0]) if visible_texts else "",
        "all_text": all_text,
        "title_names": [scorer.normalize_text(value) for value in _unique(title_names)],
        "legal_names": [scorer.normalize_text(value) for value in _unique(legal_names)],
        "phones": unique_phones,
        "emails": _unique(emails),
        "reference_domain": scorer.registrable_domain(reference_url),
        "final_domain": scorer.registrable_domain(str(first_page.get("final_url", "") or "")),
        "listed_phone": listed_phone,
        "listed_email": listed_email,
        "crawl_error": str(crawl.get("error", "") or ""),
    }


def site_signals(obs: dict, company: str) -> dict:
    """Return the pure identity and quality signals for an observed site."""
    home = str(obs.get("home_text", "") or "")
    has_contact = bool(obs.get("phones") or obs.get("emails"))
    parked = any(marker in home for marker in PARKED_MARKERS) and not has_contact
    thin = len(home.strip()) < 300 and not has_contact
    domain = obs.get("final_domain") or obs.get("reference_domain", "")
    tokens = [token for token in scorer.distinctive_tokens(company)[:2] if len(token) >= 3]
    compact_domain = scorer.compact_domain_core(domain)
    names_text = " ".join(scorer.normalize_text(name) for name in obs.get("title_names", []))
    s1 = bool(obs.get("listed_phone")) and _digits(obs["listed_phone"]) in {
        _digits(value) for value in obs.get("phones", [])
    }
    email_domains = {
        email.rsplit("@", 1)[1] for email in obs.get("emails", []) if "@" in email
    }
    domain_email_match = False
    listed_email = str(obs.get("listed_email", "") or "")
    if listed_email and "@" in listed_email:
        listed_domain = listed_email.rsplit("@", 1)[-1]
        domain_email_match = scorer.same_registrable_domain(listed_domain, domain)
    s2 = bool(listed_email) and (
        listed_email in obs.get("emails", []) or domain_email_match
    )
    s3 = any(token in compact_domain or token in names_text or token in home for token in tokens)
    s4 = (
        str(domain).endswith(".tr")
        or any(value.startswith("0") and len(_digits(value)) == 10 for value in obs.get("phones", []))
        or any(marker in str(obs.get("all_text", "") or "") for marker in TR_CITY_MARKERS)
    )
    s2 = s2 or (s3 and any(scorer.same_registrable_domain(mail_domain, domain) for mail_domain in email_domains))
    signals = [
        name for name, matched in (
            ("S1_phone", s1), ("S2_email", s2), ("S3_name", s3), ("S4_country", s4),
        ) if matched
    ]
    foreign_legal = [
        name for name in obs.get("legal_names", [])
        if name and not any(token in scorer.normalize_text(name) for token in tokens)
    ]
    conflict = bool(foreign_legal and not (s1 or s2 or s3))
    return {
        "s1": bool(s1), "s2": bool(s2), "s3": bool(s3), "s4": bool(s4),
        "parked": bool(parked), "thin": bool(thin), "conflict": conflict,
        "has_contact": bool(has_contact),
    }


def decide(obs: dict, company: str) -> dict:
    if not obs.get("reachable"):
        return {"tier": "REFERENCE_UNREACHABLE", "signals": [], "reason": obs.get("crawl_error") or "unreachable"}
    signals_state = site_signals(obs, company)
    signals = [
        name for name, key in (
            ("S1_phone", "s1"), ("S2_email", "s2"),
            ("S3_name", "s3"), ("S4_country", "s4"),
        ) if signals_state[key]
    ]
    if signals_state["parked"]:
        return {"tier": "REFERENCE_UNUSABLE", "signals": [], "reason": "parked"}
    s1, s2, s3, s4 = (signals_state[key] for key in ("s1", "s2", "s3", "s4"))
    if signals_state["conflict"]:
        return {"tier": "REFERENCE_CONFLICT", "signals": signals, "reason": "site_names_other_company"}
    if signals_state["thin"] and not (s1 or s2 or s3):
        return {"tier": "REFERENCE_THIN", "signals": signals, "reason": "thin_page_unconfirmed"}
    if s1 or s2 or (s3 and s4):
        return {"tier": "REFERENCE_VERIFIED", "signals": signals, "reason": "reference_corroborated"}
    return {"tier": "REFERENCE_ACCEPTED", "signals": signals, "reason": "reference_supplied_not_contradicted"}


def select_contacts(obs: dict, reference_domain: str, listed_phone: str) -> dict:
    emails = [str(value).strip().casefold() for value in obs.get("emails", []) if "@" in str(value)]
    ranked_emails = []
    for index, email in enumerate(emails):
        domain = email.rsplit("@", 1)[1]
        if scorer.same_registrable_domain(domain, reference_domain):
            local = email.rsplit("@", 1)[0]
            try:
                priority = _CONTACT_PREFIXES.index(local)
            except ValueError:
                priority = len(_CONTACT_PREFIXES)
            ranked_emails.append(((0 if priority < len(_CONTACT_PREFIXES) else 1, priority, index), email, "SITE"))
        elif scorer.sibling_brand_domain(domain, reference_domain):
            local = email.rsplit("@", 1)[0]
            try:
                priority = _CONTACT_PREFIXES.index(local)
            except ValueError:
                priority = len(_CONTACT_PREFIXES)
            ranked_emails.append(((2, priority, index), email, "SITE_SIBLING"))
        elif domain in FREE_MAIL_DOMAINS:
            ranked_emails.append(((3, 0, index), email, "SITE_FREEMAIL"))
    ranked_emails.sort(key=lambda item: item[0])
    selected_email = ranked_emails[0][1] if ranked_emails else ""
    email_source = ranked_emails[0][2] if ranked_emails else ""

    site_phones = _unique([
        normalized for value in obs.get("phones", [])
        if (normalized := phone.normalize_phone(value))
    ])
    normalized_listed = phone.normalize_phone(listed_phone)
    listed_match = next((value for value in site_phones if normalized_listed and _digits(value) == _digits(normalized_listed)), "")
    tr_phones = [value for value in site_phones if value.startswith("0") and len(_digits(value)) == 10]
    fixed = next((value for value in tr_phones if value[1:2] in {"2", "3", "4"}), "")
    mobile = next((value for value in tr_phones if value[1:2] == "5"), "")
    selected_phone = listed_match or fixed or mobile
    phone_source = "SITE" if selected_phone else ""
    if not selected_phone and normalized_listed:
        selected_phone = normalized_listed
        phone_source = "REFERENCE_LISTING"
    if not selected_email:
        selected_email = str(obs.get("listed_email", "") or "").strip().casefold()
        if selected_email:
            email_source = "REFERENCE_LISTING"
    return {
        "email": selected_email,
        "email_source_tier": email_source,
        "phone": selected_phone,
        "phone_source_tier": phone_source,
        "alternative_phones": "; ".join(value for value in site_phones if value != selected_phone),
    }


def _fill_listing_contacts(row: dict, metadata: dict | None) -> None:
    metadata = metadata or {}
    phone_value, _ = reference_inputs.normalize_reference_phone(metadata.get("listed_phone"))
    email_value = str(metadata.get("listed_email", "") or "").strip().casefold()
    if not row.get("phone") and phone_value:
        row["phone"] = phone_value
        row["phone_source_tier"] = "REFERENCE_LISTING"
    if not row.get("email") and email_value:
        row["email"] = email_value
        row["email_source_tier"] = "REFERENCE_LISTING"


def complete_with_reference(index, company, logger, stage_a_row, metadata, *, evaluate_fn) -> dict:
    del index, logger
    metadata = metadata or {}
    ref = reference_inputs.reference_website(metadata)
    row_a = dict(stage_a_row or {})
    a_ok = row_a.get("status") in report.OK_STATUSES and bool(row_a.get("website"))
    if not ref:
        row = row_a
        _fill_listing_contacts(row, metadata)
        row["reference_tier"] = "NO_REFERENCE"
        row["reference_signals"] = ""
        row["reference_website"] = ""
        row["reference_phone"] = metadata.get("listed_phone", "")
        return row
    if a_ok and scorer.same_registrable_domain(row_a.get("website", ""), ref):
        row = row_a
        row["website_source"] = "OWN_SEARCH+REFERENCE"
        _fill_listing_contacts(row, metadata)
        row["reference_tier"] = "REFERENCE_MATCHES_OWN_SEARCH"
        row["reference_signals"] = ""
        row["reference_website"] = ref
        row["reference_phone"] = metadata.get("listed_phone", "")
        return row

    candidate = {
        "domain": scorer.normalize_domain(ref), "url": ref,
        "score": config.PRE_CRAWL_SCORE_CAP, "title": "", "snippet": "",
        "query": "reference_website", "rank": 0,
        "reason": "reference_website", "role": "company_candidate",
    }
    evaluation = evaluate_fn(company, candidate, metadata)
    obs = observe(evaluation, ref, metadata)
    decision = decide(obs, company)
    tier = decision["tier"]
    signals = decision.get("signals", [])
    if a_ok and tier != "REFERENCE_VERIFIED":
        row = row_a
        row["reference_tier"] = tier
        row["reference_conflict_domain"] = ref
    elif tier in {"REFERENCE_VERIFIED", "REFERENCE_ACCEPTED"}:
        row = dict(row_a)
        contacts = select_contacts(obs, obs["reference_domain"], metadata.get("listed_phone", ""))
        first_page = (evaluation.get("crawl_result", {}).get("pages", []) or [{}])[0]
        final_url = str(first_page.get("final_url", "") or "")
        selected_website = ref
        if final_url and obs.get("final_domain") and obs["final_domain"] != obs.get("reference_domain"):
            selected_website = final_url
        row.update({
            "website": selected_website,
            "website_source": tier,
            **contacts,
            "status": "OK_HIGH_CONFIDENCE" if tier == "REFERENCE_VERIFIED" else "OK_MEDIUM_CONFIDENCE",
        })
        row["reason"] = f"reference:{tier}:{','.join(signals)}; {row.get('reason', '')}".rstrip()
        row["reference_tier"] = tier
    elif tier in {"REFERENCE_UNREACHABLE", "REFERENCE_THIN"}:
        row = row_a
        if not a_ok:
            row["website"] = ref
            row["website_source"] = tier
            row["selected_website"] = ref
            _fill_listing_contacts(row, metadata)
        row["reference_tier"] = tier
        if a_ok:
            row["reference_conflict_domain"] = ref
    else:
        row = row_a
        if not a_ok:
            row["website"] = ""
            row["selected_website"] = ""
            row["website_source"] = ""
            _fill_listing_contacts(row, metadata)
        row["reference_tier"] = tier
        if a_ok:
            row["reference_conflict_domain"] = ref
    row["reference_website"] = ref
    row["reference_tier"] = tier
    row["reference_signals"] = ",".join(signals)
    row["reference_phone"] = metadata.get("listed_phone", "")
    return row
