"""Shared correctness metrics for offline search calibration."""

from __future__ import annotations

import math
import re

from modules import phone, scorer

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_PATTERN = re.compile(
    r"(?:\+?90[\s\-.()]*)?\(?0?\s*[2-5]\d{2}\)?[\s\-.]*\d{3}[\s\-.]*\d{2}[\s\-.]*\d{2}"
)
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def brand_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if scorer.same_registrable_domain(a, b):
        return True
    return scorer.compact_domain_core(scorer.normalize_domain(a)) == scorer.compact_domain_core(scorer.normalize_domain(b))


def wilson_lower_bound(successes: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0:
        return 0.0
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (centre - margin) / denominator


def is_placeholder_phone(number: str) -> bool:
    """Template numbers such as 0312 000 00 00 prove nothing about ownership."""
    tail = re.sub(r"\D", "", str(number or ""))[-7:]
    return len(tail) < 7 or len(set(tail)) == 1


def extract_contacts(text: str) -> dict[str, set[str]]:
    emails = {
        value.lower().strip(".")
        for value in _EMAIL_PATTERN.findall(text or "")
        if not value.lower().endswith(_IMAGE_SUFFIXES)
    }
    phones: set[str] = set()
    for raw in _PHONE_PATTERN.findall(text or ""):
        normalized = phone.normalize_phone(raw)
        if normalized and not is_placeholder_phone(normalized):
            phones.add(normalized)
    return {"emails": emails, "phones": phones}


def same_entity_verdict(
    predicted_domain: str, truth_domain: str, predicted_site: dict, truth_site: dict,
) -> dict:
    """Decide from public contacts whether two domains belong to one firm."""
    if not predicted_site.get("reachable") or not truth_site.get("reachable"):
        return {"verdict": "UNKNOWN", "reason": "unreachable", "evidence": []}
    shared_phones = sorted(set(predicted_site.get("phones") or ()) & set(truth_site.get("phones") or ()))
    if shared_phones:
        return {"verdict": "SAME_ENTITY", "reason": "shared_phone", "evidence": shared_phones}
    shared_emails = sorted(set(predicted_site.get("emails") or ()) & set(truth_site.get("emails") or ()))
    if shared_emails:
        return {"verdict": "SAME_ENTITY", "reason": "shared_email", "evidence": shared_emails}
    cross_emails = sorted(
        {email for email in predicted_site.get("emails") or () if brand_match(email.rsplit("@", 1)[-1], truth_domain)}
        | {email for email in truth_site.get("emails") or () if brand_match(email.rsplit("@", 1)[-1], predicted_domain)}
    )
    if cross_emails:
        return {"verdict": "SAME_ENTITY", "reason": "cross_domain_email", "evidence": cross_emails}
    return {"verdict": "DIFFERENT", "reason": "no_shared_contact", "evidence": []}


def adjudication_key(source_record_id: str, prediction: str) -> str:
    return f"{source_record_id}|{scorer.normalize_domain(prediction)}"


def truth_match(prediction: str, record: dict, adjudication: dict | None = None) -> bool:
    """Brand match, or an adjudicated same-firm alternate domain."""
    if brand_match(prediction, str(record.get("truth_domain") or "")):
        return True
    verdicts = (adjudication or {}).get("verdicts", {})
    key = adjudication_key(str(record.get("source_record_id") or ""), prediction)
    return verdicts.get(key) == "SAME_ENTITY"


def rule_accepts(features: dict, firm_brand_prefix_top3: int, rule: dict) -> bool:
    """Apply the calibrated Stage A admission rule to one candidate."""
    if not isinstance(features, dict) or not isinstance(rule, dict):
        return False
    domain = str(features.get("domain") or "")
    if not domain or features.get("reachable") is not True or features.get("s3") is not True:
        return False
    if features.get("conflict") is not False or features.get("parked") is not False:
        return False
    if scorer.is_excluded_domain(domain):
        return False
    try:
        if int(features.get("brand_prefix_len")) < int(rule["L"]):
            return False
        if int(features.get("rank_best")) > int(rule["R"]):
            return False
        if int(features.get("query_hits")) < int(rule["H"]):
            return False
        contact = bool(features.get("tr_phone") or features.get("same_domain_email"))
        contact_mode = str(rule["C"])
        if contact_mode == "contact" and not contact:
            return False
        if contact_mode == "email" and features.get("same_domain_email") is not True:
            return False
        if contact_mode not in {"none", "contact", "email"}:
            return False
        if int(rule["N"]) == 1 and features.get("s4") is not True:
            return False
        if int(rule["U"]) == 1 and firm_brand_prefix_top3 != 1:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return True
