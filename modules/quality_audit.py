"""Machine-readable publication precision and autonomy audit."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from modules import runtime, scorer


POLICY_VERSION = "autonomous-quality-v1"


def payload(rows: list[dict]) -> dict:
    statuses = Counter(str(row.get("status", "")) for row in rows)
    terminal_reasons: Counter = Counter()
    gap_counts: Counter = Counter()
    invalid_publications: list[dict] = []
    for row in rows:
        evaluation = row.get("__evaluation", {})
        resolution = (
            evaluation.get("identity_resolution", {})
            if isinstance(evaluation, dict) else {}
        )
        if not isinstance(resolution, dict):
            resolution = {"reason": str(resolution)}
        reason = str(
            evaluation.get("automation_terminal_reason", "")
            if isinstance(evaluation, dict) else ""
        ) or str(resolution.get("reason", ""))
        if reason:
            terminal_reasons[reason] += 1
        for gap in (
            evaluation.get("remaining_evidence_gaps", [])
            if isinstance(evaluation, dict) else []
        ):
            gap_counts[str(gap)] += 1
        if not str(row.get("status", "")).startswith("OK_"):
            continue
        website_domain = scorer.normalize_domain(row.get("website", ""))
        if scorer.is_excluded_domain(website_domain):
            invalid_publications.append({
                "company": row.get("company", ""),
                "reason": "published_excluded_third_party_domain",
            })
        source_domains = {
            scorer.normalize_domain(row.get("email_source_url", "")),
            scorer.normalize_domain(row.get("phone_source_url", "")),
        }
        source_domains.discard("")
        if not website_domain or not any(
            scorer.same_registrable_domain(website_domain, domain)
            for domain in source_domains
        ):
            invalid_publications.append({
                "company": row.get("company", ""),
                "reason": "published_without_same_site_contact_source",
            })
        assessment = (
            evaluation.get("identity_assessment", {})
            if isinstance(evaluation, dict) else {}
        )
        if assessment.get("conflicts"):
            invalid_publications.append({
                "company": row.get("company", ""),
                "reason": "published_with_identity_conflict",
            })
    published = sum(
        count for status, count in statuses.items()
        if status.startswith("OK_")
    )
    return {
        "policy_version": POLICY_VERSION,
        "company_count": len(rows),
        "published_count": published,
        "abstain_count": len(rows) - published,
        "status_counts": dict(sorted(statuses.items())),
        "terminal_reason_counts": dict(sorted(terminal_reasons.items())),
        "remaining_gap_counts": dict(sorted(gap_counts.items())),
        "invalid_publication_count": len(invalid_publications),
        "invalid_publications": invalid_publications,
        "calibration": {
            "blind_threshold_change": False,
            "status": "fixed_policy_requires_independent_labeled_outcomes",
            "reason": (
                "Unlabeled live/replay rows audit coverage and invariants; "
                "they are not ground truth for threshold tuning."
            ),
        },
        "runtime": runtime.snapshot(),
    }


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload(rows), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
