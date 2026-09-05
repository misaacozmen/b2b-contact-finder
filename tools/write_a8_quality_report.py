"""Write the auditable A8 quality report for a v2 benchmark manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate_benchmark_suite import evaluate_source_review, resolve_item_paths


def write_report(manifest_path: Path, json_path: Path, markdown_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    roles: dict[str, dict] = {}
    failures: list[str] = []
    structural_issues: list[str] = []
    for item in manifest.get("sets", []):
        role = str(item.get("role") or "")
        paths = resolve_item_paths(item, manifest_path)
        actual_manifest_text = item.get("actual_manifest")
        if paths["expected"] is None or paths["actual"] is None or not actual_manifest_text:
            structural_issues.append(f"{role}: expected/actual/actual_manifest is missing")
            continue
        actual_manifest = (manifest_path.resolve().parent / str(actual_manifest_text)).resolve()
        try:
            metrics = evaluate_source_review(paths["expected"], paths["actual"], actual_manifest)
        except Exception as exc:
            structural_issues.append(f"{role}: {exc}")
            continue
        roles[role] = metrics
        if metrics["false_publication"]:
            failures.append(f"{role}: confirmed false publication={metrics['false_publication']}")
        if metrics["published_unknown_identity"]:
            failures.append(f"{role}: published unknown identity={metrics['published_unknown_identity']}")
        if metrics["denominators"]["known_identity"] <= 0:
            failures.append(f"{role}: zero expected or known-identity denominator")
        if metrics["denominators"]["publication_precision"] <= 0:
            failures.append(f"{role}: zero published denominator")
    if structural_issues:
        status = "structural_failed"
        exit_code = 2
    elif failures:
        status = "failed"
        exit_code = 3
    else:
        status = "pass"
        exit_code = 0
    report = {
        "schema_version": 1,
        "benchmark_manifest": str(manifest_path.resolve()),
        "benchmark_manifest_sha256": manifest.get("manifest_sha256"),
        "quality_status": status,
        "validator_exit_code": exit_code,
        "acceptance": manifest.get("acceptance"),
        "roles": roles,
        "failures": failures,
        "structural_issues": structural_issues,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# A8 quality report",
        "",
        f"- quality_status: `{status}`",
        f"- validator_exit_code: `{exit_code}`",
        "",
    ]
    for role, metrics in roles.items():
        lines.extend([
            f"## {role}",
            "",
            f"- total_expected_records: `{metrics['total_expected_records']}`",
            f"- expected_publishable_count: `{metrics['expected_publishable_count']}`",
            f"- unknown_record_count: `{metrics['unknown_record_count']}`",
            f"- publication_coverage: `{metrics['publication_coverage']}`",
            f"- publication_precision: `{metrics['publication_precision']}`",
            f"- provider_calls: `{metrics['provider_calls']}`",
            f"- physical_http_requests: `{metrics['physical_http_requests']}`",
            f"- cost: `{metrics['cost']}`",
            "",
        ])
    if failures or structural_issues:
        lines.append("## Gate findings")
        lines.append("")
        lines.extend(f"- {value}" for value in [*structural_issues, *failures])
        lines.append("")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args()
    report = write_report(args.manifest, args.json, args.markdown)
    print(json.dumps({"quality_status": report["quality_status"], "validator_exit_code": report["validator_exit_code"]}))


if __name__ == "__main__":
    main()
