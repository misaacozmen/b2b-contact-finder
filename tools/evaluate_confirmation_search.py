"""Measure confirmation queries on a labelled truth set once (free search, no run state)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from modules import calibration, confirmation_search, scorer  # noqa: E402
from tools import calibrate_acceptance  # noqa: E402


def _fingerprint(query: str) -> str:
    from modules import search
    return search._query_fingerprint(query)


def _counted_fingerprints(record: dict, domain: str) -> set[str]:
    return {
        str(item.get("query_fingerprint") or "")
        for item in record.get("raw_results", [])
        if isinstance(item, dict) and scorer.same_registrable_domain(str(item.get("domain") or ""), domain)
    }


def evaluate(records: list[dict], adjudication: dict | None, search_fn: Callable[[str], list[dict]]) -> dict:
    rule = config.CALIBRATED_ACCEPTANCE_RULE
    summary = {"labelled": 0, "no_prediction": 0, "near_miss_firms": 0, "queries": 0, "added": 0, "added_correct": 0}
    details: list[dict] = []
    for record in records:
        if not record.get("labelled"):
            continue
        summary["labelled"] += 1
        if calibrate_acceptance.predict(record, rule):
            continue
        summary["no_prediction"] += 1
        candidates = [item for item in record.get("stage_a_candidates", []) if isinstance(item, dict)]
        if not any(confirmation_search.near_miss(item, rule) for item in candidates):
            continue
        queries = confirmation_search.confirmation_queries(str(record.get("company") or ""))
        if not queries:
            continue
        summary["near_miss_firms"] += 1
        results = {query: list(search_fn(query) or []) for query in queries}
        summary["queries"] += len(queries)
        updated = []
        for item in candidates:
            if confirmation_search.near_miss(item, rule):
                domain = str(item.get("domain") or "")
                counted = _counted_fingerprints(record, domain)
                ranks = [
                    confirmation_search.domain_rank(values, domain)
                    for query, values in results.items()
                    if _fingerprint(query) not in counted
                ]
                item = confirmation_search.apply_confirmation(item, ranks)
            updated.append(item)
        top3 = calibration.brand_prefix_top3(updated)
        accepted = sorted(
            (item for item in updated if calibration.rule_accepts(item, top3, rule)),
            key=calibration.acceptance_sort_key,
        )
        if not accepted:
            continue
        prediction = str(accepted[0].get("domain") or "")
        correct = calibration.truth_match(prediction, record, adjudication)
        summary["added"] += 1
        summary["added_correct"] += int(correct)
        details.append({
            "source_record_id": record.get("source_record_id"),
            "company": record.get("company"),
            "prediction": prediction,
            "truth_domain": record.get("truth_domain"),
            "correct": bool(correct),
        })
    summary["added_precision"] = summary["added_correct"] / summary["added"] if summary["added"] else 0.0
    return {"summary": summary, "details": details}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--adjudication", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    records = [json.loads(line) for line in args.truth.read_text(encoding="utf-8").splitlines() if line.strip()]
    adjudication = (
        json.loads(args.adjudication.read_text(encoding="utf-8"))
        if args.adjudication and args.adjudication.is_file() else None
    )
    from modules import search
    result = evaluate(records, adjudication, search._safe_search_text)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
