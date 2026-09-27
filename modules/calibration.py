"""Shared correctness metrics for offline search calibration."""

from __future__ import annotations

import math

from modules import scorer


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
