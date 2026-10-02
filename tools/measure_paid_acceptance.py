"""One-time Bright Data measurement on labelled firms without a FREE pick (Talimat 19).

build-input: select labelled truth records that the calibrated rule leaves
without a prediction, and write a names-only input workbook plus a truth map.
evaluate: score a completed run of that input against the truth map.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from modules import calibration, field_merge, scorer  # noqa: E402
from tools import adjudicate_same_entity, calibrate_acceptance  # noqa: E402


CATEGORIES = ("FREE", "PAID_LEGACY", "PAID_CALIBRATED", "PAID_CALIBRATED_2", "NONE")


def _named_paths(values: list[str]) -> dict[str, Path]:
    named: dict[str, Path] = {}
    for value in values or ():
        name, separator, path = str(value).partition("=")
        if not separator or not name or not path:
            raise ValueError(f"expected NAME=PATH: {value!r}")
        named[name] = Path(path)
    return named


def _company_key(company: object) -> str:
    return " ".join(str(company or "").split()).casefold()


def select_firms(truth_sets: dict[str, list[dict]]) -> list[dict]:
    """Labelled firms for which the calibrated rule makes no FREE prediction."""
    rule = config.CALIBRATED_ACCEPTANCE_RULE
    firms: list[dict] = []
    seen: set[str] = set()
    for name, records in truth_sets.items():
        for record in records:
            if not record.get("labelled") or calibrate_acceptance.predict(record, rule):
                continue
            key = _company_key(record.get("company"))
            if not key or key in seen:
                raise ValueError(f"duplicate or empty company name: {record.get('company')!r}")
            seen.add(key)
            firms.append({
                "company": str(record.get("company") or "").strip(),
                "set": name,
                "source_record_id": str(record.get("source_record_id") or ""),
                "truth_domain": str(record.get("truth_domain") or ""),
            })
    return firms


def write_input(firms: list[dict], path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "firms"
    sheet.append(["company"])
    for firm in firms:
        sheet.append([firm["company"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _connect(run_dir: Path) -> sqlite3.Connection:
    database = run_dir / "state" / "progress.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"run database not found: {database}")
    return sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)


def read_run_rows(run_dir: Path) -> list[dict]:
    connection = _connect(run_dir)
    try:
        return [
            json.loads(payload)
            for (payload,) in connection.execute(
                "SELECT payload FROM results WHERE run_id=? ORDER BY item_index", (run_dir.name,),
            )
        ]
    finally:
        connection.close()


def brightdata_usage(run_dir: Path) -> dict:
    connection = _connect(run_dir)
    try:
        calls = dict(connection.execute(
            "SELECT state, COUNT(*) FROM provider_calls WHERE run_id=? AND provider='brightdata' GROUP BY state",
            (run_dir.name,),
        ).fetchall())
        flights = dict(connection.execute(
            "SELECT state, COUNT(*) FROM provider_query_flights WHERE run_id=? AND provider='brightdata' GROUP BY state",
            (run_dir.name,),
        ).fetchall())
    finally:
        connection.close()
    return {"calls": calls, "flights": flights}


def category(row: dict) -> str:
    if field_merge.field_confidence(row, "website") not in field_merge.CONFIDENT:
        return "NONE"
    source = str(row.get("website_source") or "")
    if source == "PAID_BRIGHTDATA_CALIBRATED":
        second = f"calibrated_acceptance:{config.PAID_SECOND_RULE_ID};"
        return "PAID_CALIBRATED_2" if str(row.get("reason") or "").startswith(second) else "PAID_CALIBRATED"
    if source == "PAID_BRIGHTDATA":
        return "PAID_LEGACY"
    return "FREE"


def evaluate(
    rows: list[dict], firms: list[dict], adjudication: dict | None,
    fetch: Callable[[str], dict] | None = None,
) -> dict:
    by_key: dict[str, dict] = {}
    for row in rows:
        by_key.setdefault(_company_key(row.get("company")), row)
    details: list[dict] = []
    for firm in firms:
        row = by_key.get(_company_key(firm["company"]))
        if row is None:
            details.append({**firm, "matched": False, "category": "MISSING", "prediction": ""})
            continue
        kind = category(row)
        prediction = scorer.registrable_domain(str(row.get("website") or "")) if kind != "NONE" else ""
        paid_features = row.get("paid_stage_a_candidates")
        details.append({
            **firm,
            "matched": True,
            "category": kind,
            "prediction": prediction or "",
            "paid_reached": isinstance(paid_features, list),
            "truth_in_paid_candidates": isinstance(paid_features, list) and any(
                calibration.brand_match(str(item.get("domain") or ""), firm["truth_domain"])
                for item in paid_features if isinstance(item, dict)
            ),
            "correct_strict": bool(prediction) and calibration.brand_match(prediction, firm["truth_domain"]),
        })
    verdicts = dict((adjudication or {}).get("verdicts", {}))
    pairs = []
    for detail in details:
        if not detail.get("prediction") or detail.get("correct_strict"):
            continue
        key = calibration.adjudication_key(detail["source_record_id"], detail["prediction"])
        if key in verdicts or any(pair["key"] == key for pair in pairs):
            continue
        pairs.append({
            "key": key,
            "source_record_id": detail["source_record_id"],
            "company": detail["company"],
            "predicted_domain": scorer.normalize_domain(detail["prediction"]),
            "truth_domain": scorer.normalize_domain(detail["truth_domain"]),
        })
    new = (
        adjudicate_same_entity.adjudicate(pairs, fetch=fetch or adjudicate_same_entity.fetch_site_contacts)
        if pairs else {"verdicts": {}, "summary": {}}
    )
    verdicts.update(new["verdicts"])
    for detail in details:
        record = {"source_record_id": detail["source_record_id"], "truth_domain": detail["truth_domain"]}
        detail["correct"] = bool(detail.get("prediction")) and calibration.truth_match(
            detail["prediction"], record, {"verdicts": verdicts},
        )
    summary: dict = {
        "firms": len(firms),
        "matched": sum(1 for detail in details if detail["matched"]),
        "paid_reached": sum(1 for detail in details if detail.get("paid_reached")),
        "truth_in_paid_candidates": sum(1 for detail in details if detail.get("truth_in_paid_candidates")),
        "new_adjudication_pairs": len(pairs),
        "new_adjudication": new["summary"],
    }
    for kind in CATEGORIES:
        items = [detail for detail in details if detail["category"] == kind]
        summary[kind] = {
            "count": len(items),
            "correct": sum(1 for detail in items if detail["correct"]),
            "correct_strict": sum(1 for detail in items if detail.get("correct_strict")),
        }
    calibrated = summary["PAID_CALIBRATED"]
    summary["paid_calibrated_precision"] = calibrated["correct"] / calibrated["count"] if calibrated["count"] else 0.0
    second = summary["PAID_CALIBRATED_2"]
    summary["paid_second_precision"] = second["correct"] / second["count"] if second["count"] else 0.0
    summary["paid_calibrated_precision_strict"] = (
        calibrated["correct_strict"] / calibrated["count"] if calibrated["count"] else 0.0
    )
    return {"summary": summary, "details": details, "new_adjudication": new.get("pairs", [])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-input")
    build.add_argument("--truth", action="append", required=True, help="NAME=PATH")
    build.add_argument("--out-input", required=True, type=Path)
    build.add_argument("--out-map", required=True, type=Path)
    score = commands.add_parser("evaluate")
    score.add_argument("--run-dir", required=True, type=Path)
    score.add_argument("--map", required=True, type=Path)
    score.add_argument("--adjudication", action="append", default=[], type=Path)
    score.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "build-input":
        truth_sets = {
            name: calibrate_acceptance._read_truth(path)
            for name, path in _named_paths(args.truth).items()
        }
        firms = select_firms(truth_sets)
        write_input(firms, args.out_input)
        args.out_map.parent.mkdir(parents=True, exist_ok=True)
        args.out_map.write_text(json.dumps({
            "rule_id": config.CALIBRATED_ACCEPTANCE_RULE_ID, "firms": firms,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        by_set: dict[str, int] = {}
        for firm in firms:
            by_set[firm["set"]] = by_set.get(firm["set"], 0) + 1
        print(json.dumps({"firms": len(firms), "by_set": by_set}, ensure_ascii=False))
        return 0
    import urllib3

    urllib3.disable_warnings()
    firms = json.loads(args.map.read_text(encoding="utf-8"))["firms"]
    verdicts: dict[str, str] = {}
    for path in args.adjudication:
        verdicts.update(json.loads(path.read_text(encoding="utf-8")).get("verdicts", {}))
    result = evaluate(read_run_rows(args.run_dir), firms, {"verdicts": verdicts})
    result["summary"]["brightdata"] = brightdata_usage(args.run_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
