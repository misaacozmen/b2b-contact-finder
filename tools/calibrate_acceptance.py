"""Select a calibrated own-search acceptance rule from an offline truth set."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import calibration


FEATURE_KEYS = (
    "domain", "url", "crawl_profile", "reachable", "brand_prefix_len", "rank_best",
    "query_hits", "admission", "s1", "s2", "s3", "s4", "parked", "thin",
    "conflict", "same_domain_email", "tr_phone", "legacy_final_score",
    "legacy_publishable",
)


def _read_truth(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def acceptance_grid() -> list[dict]:
    return [
        {"L": length, "R": rank, "H": hits, "C": contact, "N": country, "U": top3}
        for length in (3, 4, 5)
        for rank in (1, 3)
        for hits in (1, 2)
        for contact in ("none", "contact", "email")
        for country in (0, 1)
        for top3 in (0, 1)
    ]


def rule_id(rule: dict) -> str:
    return (
        f"L{rule['L']}_R{rule['R']}_H{rule['H']}_C{rule['C']}"
        f"_N{rule['N']}_U{rule['U']}"
    )


def _numeric(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def predict(record: dict, rule: dict | None) -> str | None:
    stage_a = record.get("stage_a") if isinstance(record.get("stage_a"), dict) else {}
    if str(stage_a.get("status") or "").startswith("OK_"):
        website = str(stage_a.get("website") or "").strip()
        return website or None
    if rule is None:
        return None
    top3 = record.get("stage_a_brand_prefix_top3")
    candidates = [
        candidate
        for candidate in record.get("stage_a_candidates", [])
        if isinstance(candidate, dict)
        and calibration.rule_accepts(candidate, top3, rule)
    ]
    candidates.sort(key=lambda candidate: (
        _numeric(candidate.get("rank_best"), 99),
        -_numeric(candidate.get("query_hits"), -1),
        -_numeric(candidate.get("legacy_final_score"), float("-inf")),
        str(candidate.get("domain") or ""),
    ))
    return str(candidates[0].get("domain") or "") or None if candidates else None


def metrics(records: list[dict], rule: dict | None, adjudication: dict | None = None) -> dict[str, float | int]:
    labelled = [record for record in records if record.get("labelled")]
    predicted = correct = 0
    for record in labelled:
        prediction = predict(record, rule)
        if prediction:
            predicted += 1
            if calibration.truth_match(prediction, record, adjudication):
                correct += 1
    total = len(labelled)
    return {
        "labelled": total,
        "predicted": predicted,
        "correct": correct,
        "coverage": predicted / total if total else 0.0,
        "precision": correct / predicted if predicted else 0.0,
        "wilson": calibration.wilson_lower_bound(correct, predicted),
    }


def feature_none_counts(records: list[dict]) -> dict[str, int]:
    counts = {key: 0 for key in (*FEATURE_KEYS, "stage_a_brand_prefix_top3")}
    for record in records:
        if record.get("stage_a_brand_prefix_top3") is None:
            counts["stage_a_brand_prefix_top3"] += 1
        for candidate in record.get("stage_a_candidates", []):
            if not isinstance(candidate, dict):
                continue
            for key in FEATURE_KEYS:
                if candidate.get(key) is None:
                    counts[key] += 1
    return counts


def _row(rule: dict, records: list[dict], split: str) -> dict:
    scoped = [record for record in records if record.get("split") == split]
    measured = metrics(scoped, rule)
    return {"rule_id": rule_id(rule), **rule, "split": split, **measured}


def calibrate(records: list[dict]) -> tuple[dict, list[dict]]:
    rules = acceptance_grid()
    cal_rows = [_row(rule, records, "cal") for rule in rules]
    eligible = [
        row for row in cal_rows
        if row["precision"] >= 0.92 and row["wilson"] >= 0.80
    ]
    winner_row = sorted(
        eligible, key=lambda row: (-row["coverage"], -row["precision"], row["rule_id"]),
    )[0] if eligible else None
    selected_rule = next(
        (rule for rule in rules if winner_row and rule_id(rule) == winner_row["rule_id"]),
        None,
    )

    # Holdout is measured only after the calibration split has fixed a winner.
    hold_rows = [_row(rule, records, "hold") for rule in rules] if selected_rule else []
    table_rows = []
    hold_by_id = {row["rule_id"]: row for row in hold_rows}
    for row in cal_rows:
        table_rows.append(row)
        if row["rule_id"] in hold_by_id:
            table_rows.append(hold_by_id[row["rule_id"]])

    baseline = {
        split: metrics(
            records if split == "all" else [record for record in records if record.get("split") == split],
            None,
        )
        for split in ("cal", "hold", "all")
    }
    if winner_row and selected_rule:
        selected_hold = hold_by_id[rule_id(selected_rule)]
        selected_all = metrics(records, selected_rule)
        selected = {"rule_id": rule_id(selected_rule), **selected_rule}
        result = {
            "selected": selected,
            "cal": {key: winner_row[key] for key in ("labelled", "predicted", "correct", "coverage", "precision", "wilson")},
            "hold": {key: selected_hold[key] for key in ("labelled", "predicted", "correct", "coverage", "precision", "wilson")},
            "all": selected_all,
            "baseline": baseline,
            "feature_none_counts": feature_none_counts(records),
            "selection_constraints": {"cal_precision_min": 0.92, "cal_wilson_min": 0.80, "hold_precision_min": 0.90},
            "holdout_pass": selected_hold["precision"] >= 0.90,
        }
    else:
        result = {
            "selected": None,
            "cal": None,
            "hold": None,
            "all": None,
            "baseline": baseline,
            "feature_none_counts": feature_none_counts(records),
            "selection_constraints": {"cal_precision_min": 0.92, "cal_wilson_min": 0.80, "hold_precision_min": 0.90},
            "holdout_pass": False,
        }
    return result, table_rows


def wrong_predictions(records: list[dict], rule: dict | None, adjudication: dict | None = None) -> list[dict]:
    wrong = []
    for record in records:
        if not record.get("labelled"):
            continue
        prediction = predict(record, rule)
        if prediction and not calibration.truth_match(prediction, record, adjudication):
            wrong.append({
                "source_record_id": record.get("source_record_id"),
                "company": record.get("company"),
                "prediction": prediction,
                "truth_domain": record.get("truth_domain"),
            })
    return wrong


def calibrate_all(records: list[dict], adjudication: dict | None) -> tuple[dict, list[dict]]:
    """Select on every labelled record; the independent test set is evaluated separately."""
    rules = acceptance_grid()
    rows = [
        {"rule_id": rule_id(rule), **rule, "split": "all", **metrics(records, rule, adjudication)}
        for rule in rules
    ]
    eligible = [row for row in rows if row["precision"] >= 0.92 and row["wilson"] >= 0.80]
    winner = sorted(
        eligible, key=lambda row: (-row["coverage"], -row["precision"], row["rule_id"]),
    )[0] if eligible else None
    selected_rule = next((rule for rule in rules if winner and rule_id(rule) == winner["rule_id"]), None)
    result = {
        "mode": "all",
        "selected": {"rule_id": rule_id(selected_rule), **selected_rule} if selected_rule else None,
        "all": metrics(records, selected_rule, adjudication) if selected_rule else None,
        "strict_all": metrics(records, selected_rule, None) if selected_rule else None,
        "baseline": metrics(records, None, adjudication),
        "wrong": wrong_predictions(records, selected_rule, adjudication) if selected_rule else [],
        "selection_constraints": {"precision_min": 0.92, "wilson_min": 0.80},
    }
    return result, rows


def evaluate(records: list[dict], rule: dict, adjudication: dict | None) -> dict:
    return {
        "mode": "evaluate",
        "rule": {"rule_id": rule_id(rule), **rule},
        "metrics": metrics(records, rule, adjudication),
        "strict": metrics(records, rule, None),
        "baseline": metrics(records, None, adjudication),
        "wrong": wrong_predictions(records, rule, adjudication),
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = (
        "rule_id", "L", "R", "H", "C", "N", "U", "split", "labelled",
        "predicted", "correct", "coverage", "precision", "wilson",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--table", default="")
    parser.add_argument("--mode", choices=("holdout", "all", "evaluate"), default="holdout")
    parser.add_argument("--adjudication", default="")
    parser.add_argument("--rule-id", default="")
    args = parser.parse_args()
    records = _read_truth(Path(args.truth))
    adjudication = (
        json.loads(Path(args.adjudication).read_text(encoding="utf-8")) if args.adjudication else None
    )
    if args.mode == "all":
        if not args.table:
            parser.error("--table is required for --mode all")
        result, rows = calibrate_all(records, adjudication)
        _write_json(Path(args.out), result)
        _write_csv(Path(args.table), rows)
        print(json.dumps({"selected": result["selected"], "all": result["all"], "strict_all": result["strict_all"]}, ensure_ascii=False))
        return 0 if result["selected"] else 2
    if args.mode == "evaluate":
        rule = next((rule for rule in acceptance_grid() if rule_id(rule) == args.rule_id), None)
        if rule is None:
            parser.error(f"unknown --rule-id: {args.rule_id!r}")
        result = evaluate(records, rule, adjudication)
        _write_json(Path(args.out), result)
        print(json.dumps({"rule": result["rule"]["rule_id"], "metrics": result["metrics"], "strict": result["strict"]}, ensure_ascii=False))
        return 0
    if not args.table:
        parser.error("--table is required for --mode holdout")
    result, rows = calibrate(_read_truth(Path(args.truth)))
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_csv(Path(args.table), rows)
    print(json.dumps({
        "selected": result["selected"], "cal": result["cal"],
        "hold": result["hold"], "holdout_pass": result["holdout_pass"],
    }, ensure_ascii=False))
    if result["selected"] is None:
        return 2
    return 0 if result["holdout_pass"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
