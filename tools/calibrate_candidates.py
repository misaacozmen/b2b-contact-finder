"""Measure brand-prefix candidate admission rules on a labelled truth set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from modules import calibration, scorer


def _read_truth(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _eligible_results(record: dict) -> list[dict]:
    eligible = []
    for result in record.get("raw_results", []):
        domain = str(result.get("domain") or "")
        url = str(result.get("url") or "")
        if not domain or not url or scorer.is_excluded_domain(domain):
            continue
        eligible.append(result)
    return eligible


def _g0(record: dict, result: dict) -> bool:
    details = scorer.score_domain_details(
        str(record.get("company") or ""),
        str(result.get("url") or ""),
        title=str(result.get("title") or ""),
        snippet="",
    )
    return int(details.get("score", 0)) > 0


def _prefix(record: dict, result: dict, minimum_token_len: int, maximum_rank: int) -> bool:
    tokens = scorer.distinctive_tokens(str(record.get("company") or ""))
    if not tokens:
        return False
    token = tokens[0]
    try:
        rank = int(result.get("rank", 99))
    except (TypeError, ValueError):
        return False
    return (
        len(token) >= minimum_token_len
        and scorer.compact_domain_core(str(result.get("domain") or "")).startswith(token)
        and rank <= maximum_rank
    )


def _metrics(records: list[dict], rule: tuple[int, int] | None) -> dict[str, float | int]:
    labelled = [record for record in records if record.get("labelled")]
    correct_firms = 0
    negative_counts = []
    for record in labelled:
        truth = str(record.get("truth_domain") or "")
        accepted = []
        for result in _eligible_results(record):
            accepted_by_rule = _g0(record, result) if rule is None else (
                _g0(record, result) or _prefix(record, result, rule[0], rule[1])
            )
            if accepted_by_rule:
                domain = str(result.get("domain") or "")
                accepted.append(domain)
        distinct = set(accepted)
        if any(calibration.brand_match(domain, truth) for domain in distinct):
            correct_firms += 1
        negative_counts.append(sum(not calibration.brand_match(domain, truth) for domain in distinct))
    trials = len(labelled)
    return {
        "labelled": trials,
        "correct_firms": correct_firms,
        "recall": correct_firms / trials if trials else 0.0,
        "mean_negatives": sum(negative_counts) / trials if trials else 0.0,
    }


def calibrate(records: list[dict]) -> dict:
    labelled = [record for record in records if record.get("labelled")]
    rows = []
    for minimum_token_len in (3, 4, 5):
        for maximum_rank in (3, 8):
            row = {"L": minimum_token_len, "K": maximum_rank}
            for split in ("cal", "hold"):
                row[split] = _metrics(
                    [record for record in labelled if record.get("split") == split],
                    (minimum_token_len, maximum_rank),
                )
            rows.append(row)
    eligible = [row for row in rows if row["cal"]["mean_negatives"] <= 1.5]
    selected = None
    if eligible:
        chosen = sorted(
            eligible,
            key=lambda row: (-row["cal"]["recall"], -row["L"], row["K"]),
        )[0]
        selected = {"L": chosen["L"], "K": chosen["K"]}
    return {
        "selected": selected,
        "rows": rows,
        "baseline_G0": {
            split: _metrics(
                [record for record in labelled if record.get("split") == split], None,
            )
            for split in ("cal", "hold")
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = calibrate(_read_truth(Path(args.truth)))
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"selected": result["selected"], "baseline_G0": result["baseline_G0"]}, ensure_ascii=False))
    return 0 if result["selected"] is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
