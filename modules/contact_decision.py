"""Field-specific contact ranking for verified first-party pages."""

from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlparse

import config
from modules import evidence_ledger, phone, scorer


PAGE_PRIORITY = {
    "contact": 6, "locations": 5, "document": 4, "about": 3,
    "homepage": 2, "legal": 1, "privacy": 0, "other": 1,
}
PHONE_ROLE_PRIORITY = {
    "headquarters": 100, "general": 90, "specialist": 85,
    "sales": 82, "export": 82, "istanbul": 78, "izmir": 78,
    "ankara": 78, "whatsapp": 75, "factory": 65, "branch": 60,
    "marketing": 45, "owner": 40, "fax": -100,
}
EMAIL_ROLE_PRIORITY = {
    "sales": 6, "export": 6, "office": 5, "headquarters": 5,
    "general": 4, "specialist": 3, "factory": 2, "branch": 2,
    "marketing": 0, "privacy": -10,
}


def _best_source(records: list[dict]) -> dict:
    return max(
        records,
        key=lambda item: (
            PAGE_PRIORITY.get(evidence_ledger.page_scope(item.get("source_url", "")), 0),
            1 if item.get("retrieval_method", "http") == "http" else 0,
            item.get("source_url", ""),
        ),
        default={},
    )


def _aggregate(records: list[dict], normalizer) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        value = normalizer(str(record.get("value", "") or ""))
        if value:
            grouped[value].append({**record, "value": value})
    result = []
    for value, observations in grouped.items():
        source = _best_source(observations)
        source_urls = list(dict.fromkeys(
            item.get("source_url", "") for item in observations if item.get("source_url")
        ))
        retrieval_methods = list(dict.fromkeys(
            item.get("retrieval_method", "http") for item in observations
        ))
        result.append({
            **source,
            "value": value,
            "source_url": source.get("source_url", ""),
            "source_urls": source_urls,
            "observation_count": len(source_urls),
            "retrieval_methods": retrieval_methods,
        })
    return result


def rank_phone_records(records: list[dict]) -> list[dict]:
    """Rank corporate switchboards and scoped first-party contacts separately."""
    candidates = [
        item for item in _aggregate(records, phone.normalize_phone)
        if item.get("label", "general") != "fax"
    ]

    def number_type_priority(value: str) -> int:
        digits = re.sub(r"\D", "", value)
        if re.fullmatch(r"0?444\d{4}", digits) or digits.startswith("0850"):
            return 3
        if digits.startswith(("02", "03", "04")):
            return 2
        if digits.startswith("05"):
            return 1
        return 0

    def rank(item: dict) -> tuple[int, int, int, int, int, str]:
        scope = evidence_ledger.page_scope(item.get("source_url", ""))
        return (
            PHONE_ROLE_PRIORITY.get(item.get("label", "general"), 0),
            number_type_priority(item["value"]),
            PAGE_PRIORITY.get(scope, 0),
            item.get("observation_count", 0),
            1 if item.get("retrieval_method", "http") == "http" else 0,
            item["value"],
        )

    ranked = sorted(candidates, key=rank, reverse=True)
    for item in ranked:
        item["selection_reason"] = (
            f"role={item.get('label', 'general')};"
            f"scope={evidence_ledger.page_scope(item.get('source_url', ''))};"
            f"observations={item.get('observation_count', 0)}"
        )
    return ranked


def rank_email_records(company: str, website: str, records: list[dict], is_usable) -> list[dict]:
    """Rank mailbox ownership, page scope and business role independently."""
    candidates = _aggregate(
        [record for record in records if is_usable(str(record.get("value", "")))],
        lambda value: value.strip().casefold(),
    )
    website_domain = scorer.normalize_domain(urlparse(website).netloc or website)
    website_root = scorer.compact_domain_core(website_domain)
    tokens = scorer.distinctive_tokens(company)

    def rank(item: dict) -> tuple[int, int, int, int, int, int, int, str]:
        email = item["value"]
        local, domain = email.split("@", 1)
        email_domain = scorer.normalize_domain(domain)
        email_root = scorer.compact_domain_core(email_domain)
        email_text = scorer.normalize_text(f"{local} {email_root}")
        website_family = bool(
            email_root and website_root
            and (email_root == website_root or email_root in website_root or website_root in email_root)
        )
        affinity = (80 if website_family else 0) + (
            35 if any(token in email_text for token in tokens) else 0
        ) + (5 if email_domain.endswith((".com.tr", ".tr")) else 0)
        source_path = urlparse(item.get("source_url", "")).path.casefold()
        localized_route = bool(re.search(
            r"(?:^|/)(?:tr|tr-tr|tr_tr|turkiye|turkey)(?:/|$)", source_path,
        ))
        localized_mailbox = bool(re.search(
            r"(?:^|[._-])(?:tr|turkiye|turkey)(?:[._-]|$)", local,
        ))
        prefix = re.split(r"[.-]", local, maxsplit=1)[0]
        try:
            prefix_priority = -config.EMAIL_PRIORITY_PREFIXES.index(prefix)
        except ValueError:
            prefix_priority = -len(config.EMAIL_PRIORITY_PREFIXES)
        scope = evidence_ledger.page_scope(item.get("source_url", ""))
        role = item.get("label", "general")
        return (
            1 if website_family else 0,
            affinity,
            PAGE_PRIORITY.get(scope, 0),
            EMAIL_ROLE_PRIORITY.get(role, 0),
            (2 if localized_route else 0) + (1 if localized_mailbox else 0),
            item.get("observation_count", 0),
            prefix_priority,
            email,
        )

    ranked = sorted(candidates, key=rank, reverse=True)
    accepted = []
    for item in ranked:
        values = rank(item)
        if values[1] < 15:
            continue
        item["selection_reason"] = (
            f"domain_family={values[0]};"
            f"scope={evidence_ledger.page_scope(item.get('source_url', ''))};"
            f"role={item.get('label', 'general')};"
            f"observations={item.get('observation_count', 0)}"
        )
        accepted.append(item)
    return accepted
