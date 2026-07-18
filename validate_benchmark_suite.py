"""Validate Dev/Validation/Blind benchmark isolation and optional run outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from modules import scorer
from validate_golden_xlsx import FIELDS, _sheet_rows, assertion_coverage, evaluate, readiness_issues


BASE_DIR = Path(__file__).resolve().parent


def _companies(path: Path) -> set[str]:
    return {
        scorer.normalize_text(str(row.get("Company") or "")).strip()
        for row in _sheet_rows(path, "Manual Report")
        if str(row.get("Company") or "").strip()
    }


def validate_manifest(path: Path) -> tuple[list[dict], list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    sets = payload.get("sets", [])
    issues: list[str] = []
    seen_roles = set()
    company_sets: dict[str, set[str]] = {}
    for item in sets:
        role = item.get("role", "")
        if role in seen_roles:
            issues.append(f"duplicate benchmark role: {role}")
        seen_roles.add(role)
        expected_text = item.get("expected", "")
        if not expected_text:
            if role != "blind":
                issues.append(f"{role}: expected workbook missing")
            continue
        expected = (BASE_DIR / expected_text).resolve()
        if not expected.exists():
            issues.append(f"{role}: workbook not found: {expected}")
            continue
        if item.get("readiness_mode") != "legacy" and item.get("status") != "manual_validation_pending":
            issues.extend(f"{role}: {value}" for value in readiness_issues(expected))
        company_sets[role] = _companies(expected)
    for first_role, first in company_sets.items():
        for second_role, second in company_sets.items():
            if first_role >= second_role:
                continue
            overlap = first & second
            if overlap:
                issues.append(f"company overlap {first_role}/{second_role}: {len(overlap)}")
    return sets, issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=BASE_DIR / "data" / "benchmark_splits.json")
    parser.add_argument("--actual", action="append", default=[], help="role=contacts.xlsx")
    args = parser.parse_args()
    sets, issues = validate_manifest(args.manifest)
    actuals = dict(value.split("=", 1) for value in args.actual)
    for item in sets:
        role = item.get("role", "")
        expected_text = item.get("expected", "")
        if role not in actuals or not expected_text:
            continue
        expected = (BASE_DIR / expected_text).resolve()
        metrics, complete = evaluate(expected, Path(actuals[role]))
        print(f"[{role}] complete={len(complete)}")
        print(f"  coverage: {assertion_coverage(expected)}")
        for field in FIELDS:
            print(f"  {field}: {metrics[field]}")
    if issues:
        print("Benchmark suite issues:")
        for issue in issues:
            print(f"- {issue}")
        raise SystemExit(2)
    print("Benchmark suite manifest: OK")


if __name__ == "__main__":
    main()
