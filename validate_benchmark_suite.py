"""Validate Dev/Validation/Blind benchmark isolation and optional run outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from modules import excel, scorer
from validate_golden_xlsx import FIELDS, _sheet_rows, assertion_coverage, evaluate, readiness_issues


BASE_DIR = Path(__file__).resolve().parent
EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES = 71


def _companies(path: Path) -> set[str]:
    return {
        scorer.normalize_text(str(row.get("Company") or "")).strip()
        for row in _sheet_rows(path, "Manual Report")
        if str(row.get("Company") or "").strip()
    }


def validate_manifest(
    path: Path,
    private_seen_workbook: Path | None = None,
) -> tuple[list[dict], list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    issues: list[str] = []
    sets = payload.get("sets", [])
    policy = payload.get("policy", {})

    policy_count = policy.get("private_seen_expected_unique_companies")
    if type(policy_count) is not int or policy_count != EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES:
        issues.append(
            f"manifest policy error: private_seen_expected_unique_companies must be {EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES}"
        )

    seen_roles = set()
    company_sets: dict[str, set[str]] = {}
    has_private_flag = False

    for item in sets:
        role = item.get("role", "")
        if role in seen_roles:
            issues.append(f"duplicate benchmark role: {role}")
        seen_roles.add(role)

        if "private_seen_check" in item:
            val = item["private_seen_check"]
            if type(val) is not bool:
                issues.append(f"{role}: private_seen_check must be a boolean")

        is_private_checked = (item.get("private_seen_check") is True)
        if is_private_checked:
            has_private_flag = True

        if role == "blind" or role.startswith("blind_"):
            if not is_private_checked:
                issues.append(f"{role}: blind role requires private_seen_check=true")

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

    if not has_private_flag:
        issues.append("manifest policy error: at least one set must have private_seen_check=true")

    for first_role, first in company_sets.items():
        for second_role, second in company_sets.items():
            if first_role >= second_role:
                continue
            overlap = first & second
            if overlap:
                issues.append(f"company overlap {first_role}/{second_role}: {len(overlap)}")

    if private_seen_workbook is not None:
        wb_path = Path(private_seen_workbook).resolve()
        if not wb_path.exists():
            issues.append(f"private seen workbook not found: {wb_path}")
        else:
            try:
                records = excel.read_company_records(wb_path)
                private_seen = {
                    scorer.normalize_text(str(r.get("company") or "")).strip()
                    for r in records
                    if str(r.get("company") or "").strip()
                }
                if len(private_seen) != EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES:
                    issues.append(
                        f"private seen workbook count mismatch: actual {len(private_seen)} != expected {EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES}"
                    )

                checked_private_roles: list[str] = []
                for item in sets:
                    if item.get("private_seen_check") is True:
                        role = item.get("role", "")
                        if role in company_sets and company_sets[role]:
                            checked_private_roles.append(role)
                            overlap = company_sets[role] & private_seen
                            if overlap:
                                issues.append(f"{role}: private seen overlap: {len(overlap)}")
                        else:
                            issues.append(f"{role}: private seen check target workbook missing or empty")

                if not checked_private_roles:
                    issues.append("private seen gate error: no private_seen_check sets evaluated")

            except Exception:
                issues.append("private seen workbook read error")

    return sets, issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=BASE_DIR / "data" / "benchmark_splits.json")
    parser.add_argument("--actual", action="append", default=[], help="role=contacts.xlsx")
    parser.add_argument("--private-seen-workbook", type=Path, default=None, help="Path to private seen firms workbook")
    args = parser.parse_args()

    sets, issues = validate_manifest(
        args.manifest,
        private_seen_workbook=args.private_seen_workbook,
    )
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
    if args.private_seen_workbook is not None:
        print("private_seen_gate: OK")
    else:
        print("private_seen_gate: NOT_REQUESTED")


if __name__ == "__main__":
    main()
