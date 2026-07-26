"""Offline risk/coverage calibration for publication decisions.

This module never mutates runtime configuration.  It produces an auditable
candidate threshold from a labelled calibration split and reports whether the
sample is statistically strong enough for deployment.
"""

from __future__ import annotations

import math


ALLOWED_CALIBRATION_ROLES = {"calibration", "validation"}


def wilson_lower_bound(correct: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    probability = correct / total
    denominator = 1.0 + (z * z / total)
    centre = probability + (z * z / (2.0 * total))
    margin = z * math.sqrt(
        (probability * (1.0 - probability) / total)
        + (z * z / (4.0 * total * total))
    )
    return max(0.0, (centre - margin) / denominator)


def _coerce_label(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "correct", "1"}:
            return True
        if normalized in {"false", "no", "incorrect", "0"}:
            return False
    raise ValueError(f"invalid calibration label: {value!r}")


def _labelled(records: list[dict], score_key: str, label_key: str) -> list[tuple[int, bool]]:
    labelled: list[tuple[int, bool]] = []
    for record in records:
        if label_key not in record or record.get(label_key) is None:
            continue
        labelled.append((
            max(0, min(100, int(record.get(score_key, 0) or 0))),
            _coerce_label(record.get(label_key)),
        ))
    return labelled


def risk_coverage_curve(
    records: list[dict],
    *,
    score_key: str = "publication_safety_score",
    label_key: str = "correct",
) -> list[dict]:
    labelled = _labelled(records, score_key, label_key)
    if not labelled:
        return []
    thresholds = sorted({score for score, _ in labelled}, reverse=True)
    curve: list[dict] = []
    for threshold in thresholds:
        accepted = [correct for score, correct in labelled if score >= threshold]
        correct_count = sum(accepted)
        count = len(accepted)
        precision = correct_count / count
        curve.append({
            "threshold": threshold,
            "accepted": count,
            "labelled_total": len(labelled),
            "coverage": count / len(labelled),
            "correct": correct_count,
            "false_positives": count - correct_count,
            "precision": precision,
            "risk": 1.0 - precision,
            "precision_wilson_lower": wilson_lower_bound(correct_count, count),
        })
    return curve


def recommend_threshold(
    records: list[dict],
    *,
    target_precision: float = 0.99,
    minimum_accepted: int = 20,
    score_key: str = "publication_safety_score",
    label_key: str = "correct",
    role_key: str = "split_role",
) -> dict:
    """Choose maximum coverage satisfying empirical precision.

    Blind/holdout/dev rows are rejected to prevent calibration leakage.
    ``deployable`` additionally requires the Wilson lower bound to meet the
    target, so small perfect samples remain useful diagnostics but cannot
    silently change production thresholds.
    """
    roles = {
        str(record.get(role_key, "")).strip().casefold()
        for record in records
        if record.get(label_key) is not None
    }
    invalid_roles = sorted(role for role in roles if role not in ALLOWED_CALIBRATION_ROLES)
    if invalid_roles:
        raise ValueError(
            "calibration requires only calibration/validation rows; "
            f"found: {', '.join(invalid_roles)}"
        )
    curve = risk_coverage_curve(records, score_key=score_key, label_key=label_key)
    eligible = [
        point for point in curve
        if point["accepted"] >= int(minimum_accepted)
        and point["precision"] >= float(target_precision)
    ]
    if not eligible:
        return {
            "candidate_threshold": None,
            "deployable": False,
            "reason": "no_threshold_meets_empirical_target_and_minimum_sample",
            "target_precision": float(target_precision),
            "minimum_accepted": int(minimum_accepted),
            "labelled_total": curve[0]["labelled_total"] if curve else 0,
            "curve": curve,
        }
    candidate = max(eligible, key=lambda point: (point["coverage"], -point["threshold"]))
    deployable = candidate["precision_wilson_lower"] >= float(target_precision)
    return {
        "candidate_threshold": candidate["threshold"],
        "deployable": deployable,
        "reason": (
            "statistical_precision_bound_met"
            if deployable else "empirical_target_met_but_statistical_support_insufficient"
        ),
        "target_precision": float(target_precision),
        "minimum_accepted": int(minimum_accepted),
        "labelled_total": candidate["labelled_total"],
        "accepted": candidate["accepted"],
        "coverage": candidate["coverage"],
        "precision": candidate["precision"],
        "precision_wilson_lower": candidate["precision_wilson_lower"],
        "curve": curve,
    }
