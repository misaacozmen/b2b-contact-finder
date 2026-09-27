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
