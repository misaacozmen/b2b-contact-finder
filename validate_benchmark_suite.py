"""Validate Dev/Validation/Blind benchmark isolation and optional run outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import config
from modules import excel, run_context, scorer
from validate_golden_xlsx import FIELDS, _sheet_rows, assertion_coverage, evaluate, readiness_issues


BASE_DIR = Path(__file__).resolve().parent
EXPECTED_PRIVATE_SEEN_UNIQUE_COMPANIES = 71
_SOURCE_ID_FIELDS = (
    "source_record_id", "Source Record ID", "sourceRecordId", "source id",
    "source_id", "record_id", "record id", "kaynak_kayit_id",
)
_COMPANY_FIELDS = ("company", "Company")


def _companies(path: Path) -> set[str]:
    return {
        scorer.normalize_text(str(row.get("Company") or "")).strip()
        for row in _sheet_rows(path, "Manual Report")
        if str(row.get("Company") or "").strip()
    }


def _row_value(row: dict, fields: tuple[str, ...]) -> str:
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _identity_map(rows: list[dict], *, role: str, mode: str) -> tuple[dict[str, dict], list[str]]:
    """Build a fail-closed record map for structural population checks."""
    records: dict[str, dict] = {}
    issues: list[str] = []
    for index, row in enumerate(rows, start=2):
        company = _row_value(row, _COMPANY_FIELDS)
        source_id = _row_value(row, _SOURCE_ID_FIELDS)
        if not company:
            issues.append(f"{role}: row {index} has no company")
            continue
        key = source_id if mode == "id" else scorer.normalize_text(company).strip()
        if mode == "id" and not source_id:
            issues.append(f"{role}: row {index} has no source_record_id")
            continue
        if not key:
            issues.append(f"{role}: row {index} has no usable identity")
            continue
        if key in records:
            issues.append(f"{role}: duplicate record identity")
            continue
        records[key] = row
    return records, issues


def _population_check(
    expected: Path, all_results: Path, contacts: Path, role: str
) -> tuple[list[str], list[str]]:
    """Validate all-results population and the contacts publication subset."""
    issues: list[str] = []
    quality_issues: list[str] = []
    try:
        expected_rows = _sheet_rows(expected, "Manual Report")
        all_rows = _sheet_rows(all_results)
        contact_rows = _sheet_rows(contacts)
    except Exception as exc:
        return [f"{role}: output unreadable: {exc.__class__.__name__}"], quality_issues

    expected_has_any_id = any(_row_value(row, _SOURCE_ID_FIELDS) for row in expected_rows)
    expected_has_all_ids = bool(expected_rows) and all(
        _row_value(row, _SOURCE_ID_FIELDS) for row in expected_rows
    )
    if expected_has_any_id and not expected_has_all_ids:
        issues.append(f"{role}: expected source_record_id coverage is incomplete")
    mode = "id" if expected_has_all_ids else "name"

    expected_map, expected_issues = _identity_map(expected_rows, role=f"{role}: expected", mode=mode)
    all_map, all_issues = _identity_map(all_rows, role=f"{role}: all_results", mode=mode)
    issues.extend(expected_issues)
    issues.extend(all_issues)
    if not all_rows:
        issues.append(f"{role}: all_results output has no evaluated rows")

    missing = set(expected_map) - set(all_map)
    extra = set(all_map) - set(expected_map)
    if missing:
        issues.append(f"{role}: all_results is missing {len(missing)} expected records")
    if extra:
        issues.append(f"{role}: all_results has {len(extra)} unexpected records")

    if mode == "id" and any(not _row_value(row, _SOURCE_ID_FIELDS) for row in contact_rows):
        issues.append(f"{role}: contacts row has no source_record_id")
    contact_map, contact_issues = _identity_map(contact_rows, role=f"{role}: contacts", mode=mode)
    issues.extend(contact_issues)
    unexpected_publications = set(contact_map) - set(all_map)
    if unexpected_publications:
        issues.append(
            f"{role}: contacts publishes {len(unexpected_publications)} unexpected records"
        )
    return issues, quality_issues


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _artifact_inventory(root: Path) -> dict:
    files = {}
    digest = hashlib.sha256()
    for path in sorted(item for item in Path(root).rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        file_hash = _sha256_file(path)
        files[relative] = {"sha256": file_hash, "bytes": path.stat().st_size}
        digest.update(relative.encode("utf-8"))
        digest.update(file_hash.encode("ascii"))
    return {"root": str(Path(root).resolve()), "files": files, "aggregate_sha256": digest.hexdigest()}


def _ordered_input_ids(input_path: Path) -> list[str]:
    records = excel.read_company_records(Path(input_path))
    unique: dict[str, dict] = {}
    for record in records:
        source_id, _quality = run_context.source_record_identity(record)
        if source_id not in unique:
            unique[source_id] = record
    return list(unique)


def _ordered_artifact_ids(path: Path) -> list[str]:
    return [_row_value(row, _SOURCE_ID_FIELDS) for row in _sheet_rows(path) if _row_value(row, _SOURCE_ID_FIELDS)]


def _required_source_record_id_issues(
    item: dict, expected: Path, manifest_base: Path, role: str,
) -> list[str]:
    """Require a complete, ordered ID envelope for a benchmark set."""
    issues: list[str] = []
    targets: list[tuple[str, Path, str | None]] = [("expected", expected, "Manual Report")]
    for key in ("pipeline_input", "source_assisted_input"):
        value = item.get(key)
        if value:
            targets.append((key, (manifest_base / str(value)).resolve(), None))
        else:
            issues.append(f"{role}: {key} required when requires_source_record_id=true")

    ordered: dict[str, list[str]] = {}
    companies: dict[str, list[str]] = {}
    for label, path, sheet in targets:
        if not path.is_file():
            issues.append(f"{role}: {label} workbook not found: {path}")
            continue
        try:
            rows = _sheet_rows(path, sheet)
        except Exception as exc:
            issues.append(f"{role}: {label} workbook unreadable: {exc.__class__.__name__}")
            continue
        ids = [_row_value(row, _SOURCE_ID_FIELDS) for row in rows]
        if len(rows) != 20:
            issues.append(f"{role}: {label} must contain exactly 20 records")
        if any(not value for value in ids):
            issues.append(f"{role}: {label} has incomplete source_record_id coverage")
        if len(ids) != len(set(ids)):
            issues.append(f"{role}: {label} has duplicate source_record_id")
        ordered[label] = ids
        companies[label] = [_row_value(row, _COMPANY_FIELDS) for row in rows]

    expected_ids = ordered.get("expected")
    if expected_ids is not None:
        for label, ids in ordered.items():
            if label != "expected" and ids != expected_ids:
                issues.append(f"{role}: {label} source_record_id order does not match expected")
    expected_companies = companies.get("expected")
    if expected_companies is not None:
        for label, names in companies.items():
            if label != "expected" and names != expected_companies:
                issues.append(f"{role}: {label} company order does not match expected")
    return issues


def _ordered_ids_hash(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def _read_json_if_present(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _artifact_readiness_issues(artifact_dir: Path) -> list[str]:
    manifest_path = Path(artifact_dir) / "delivery_manifest.json"
    if not manifest_path.is_file():
        return ["delivery_manifest.json missing"]
    manifest = _read_json_if_present(manifest_path)
    issues = []
    if manifest.get("complete") is not True:
        issues.append("complete is not true")
    if manifest.get("remediation_complete") is not True:
        issues.append("remediation_complete is not true")
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    pending = counts.get("pending", manifest.get("pending", 0))
    try:
        pending_count = int(pending or 0)
    except (TypeError, ValueError):
        pending_count = 1
    if pending_count > 0:
        issues.append(f"pending={pending_count}")
    return issues


def _field_metrics_from_labels(expected: Path | None, actual: Path) -> dict:
    if expected is None:
        return {field: {"status": "UNVALIDATED", "tp": None, "fp": None, "fn": None, "precision": None, "recall": None} for field in FIELDS}
    try:
        metrics, _complete = evaluate(expected, actual)
    except ValueError as exc:
        return {field: {"status": "UNVALIDATED", "tp": None, "fp": None, "fn": None, "precision": None, "recall": None} for field in FIELDS} | {"structural_error": str(exc)}
    result = {}
    for field, values in metrics.items():
        denominator_precision = values["tp"] + values["fp"]
        denominator_recall = values["tp"] + values["fn"]
        result[field] = {
            "status": "ACTUAL_EVALUATED",
            **values,
            "precision": round(values["tp"] / denominator_precision, 4) if denominator_precision else 0.0,
            "recall": round(values["tp"] / denominator_recall, 4) if denominator_recall else 0.0,
        }
    return result


def _stage_metrics_from_labels(expected: Path | None, actual: Path, candidates: Path) -> dict:
    if expected is None:
        return {"status": "UNVALIDATED"}
    try:
        return {"status": "ACTUAL_EVALUATED", **evaluate_stages(expected, actual, candidates)}
    except ValueError as exc:
        return {"status": "UNVALIDATED", "structural_error": str(exc)}


def _provider_budget_metrics(artifact_dir: Path, population: int, counters: dict) -> dict:
    validation_run = _read_json_if_present(artifact_dir / "validation_run.json")
    recorded = validation_run.get("budgets", {}) if isinstance(validation_run.get("budgets"), dict) else {}
    ratios = {
        "brightdata": float(config.BRIGHTDATA_REQUEST_RATIO),
        "google_places": float(config.GOOGLE_PLACES_REQUEST_RATIO),
        "hunter": float(config.HUNTER_REQUEST_RATIO),
        "brandfetch": float(config.BRANDFETCH_REQUEST_RATIO),
    }
    caps = {
        "brightdata": config.BRIGHTDATA_REQUEST_HARD_CAP,
        "google_places": config.GOOGLE_PLACES_REQUEST_HARD_CAP,
        "hunter": config.HUNTER_REQUEST_HARD_CAP,
        "brandfetch": config.BRANDFETCH_REQUEST_HARD_CAP,
    }
    counter_names = {"brightdata": "brightdata", "google_places": "google_places", "hunter": "hunter", "brandfetch": "brandfetch"}
    result = {}
    for provider, ratio in ratios.items():
        effective = recorded.get(provider)
        if effective is None:
            effective = math.ceil(population * ratio)
            if caps[provider] is not None:
                effective = min(effective, max(0, int(caps[provider])))
        provider_prefix = f"api.{counter_names[provider]}"
        result[provider] = {
            "population": population,
            "ratio": ratio if not recorded else None,
            "cap": caps[provider] if not recorded else None,
            "effective": int(effective),
            "reserved": counters.get(f"{provider_prefix}.reserved"),
            "completed": counters.get(f"{provider_prefix}.completed"),
            "blocked": counters.get(f"{provider_prefix}.budget_blocked", 0),
            "status": "UNVALIDATED" if not recorded else "RECORDED_EFFECTIVE_ONLY",
        }
    return result


def _real_artifact_run(artifact_dir: Path, input_path: Path, expected: Path | None) -> dict:
    artifact_dir = Path(artifact_dir).resolve()
    readiness = _artifact_readiness_issues(artifact_dir)
    if readiness:
        raise ValueError(f"artifact_not_ready:{'; '.join(readiness)}")
    all_results = artifact_dir / "all_results.xlsx"
    contacts = artifact_dir / "contacts.xlsx"
    candidates = artifact_dir / "website_candidates.xlsx"
    if not all_results.is_file() or not contacts.is_file() or not candidates.is_file():
        raise FileNotFoundError(f"real artifact set incomplete: {artifact_dir}")
    input_ids = _ordered_input_ids(input_path)
    result_ids = _ordered_artifact_ids(all_results)
    all_rows = _sheet_rows(all_results)
    contact_rows = _sheet_rows(contacts)
    audit = _read_json_if_present(artifact_dir / "quality_audit.json")
    telemetry = _read_json_if_present(artifact_dir / "telemetry.json")
    counters = telemetry.get("counters", {}) if isinstance(telemetry.get("counters"), dict) else {}
    status_counts = audit.get("status_counts", {}) if isinstance(audit.get("status_counts"), dict) else {}
    if not status_counts:
        for row in all_rows:
            status = str(row.get("status") or "")
            if status:
                status_counts[status] = status_counts.get(status, 0) + 1
    high = int(status_counts.get("OK_HIGH_CONFIDENCE", 0))
    medium = int(status_counts.get("OK_MEDIUM_CONFIDENCE", 0))
    complete_contacts = sum(
        bool(row.get("website")) and bool(row.get("email")) and bool(row.get("phone"))
        for row in contact_rows
    )
    interstitial = {
        "live_pages": counters.get("live.site.security_interstitial_rejected", 0),
        "cache_pages": counters.get("cache.site.security_interstitial_rejected", 0),
        "unique_hosts": (telemetry.get("unique_counts", {}) or {}).get("recovery.security_interstitial_hosts", 0),
    }
    return {
        "path": str(artifact_dir),
        "artifacts": _artifact_inventory(artifact_dir),
        "population": {
            "input_rows": len(input_ids),
            "all_results_rows": len(all_rows),
            "contacts_rows": len(contact_rows),
            "ordered_input_source_ids_sha256": _ordered_ids_hash(input_ids),
            "ordered_all_results_source_ids_sha256": _ordered_ids_hash(result_ids),
            "exact_all_results_population": result_ids == input_ids,
        },
        "field_metrics": _field_metrics_from_labels(expected, all_results),
        "stage_metrics": _stage_metrics_from_labels(expected, all_results, candidates),
        "publication": {
            "published_count": len(contact_rows),
            "abstained_count": max(0, len(input_ids) - len(contact_rows)),
            "precision": None,
            "status": "UNVALIDATED_NO_GROUND_TRUTH" if expected is None else "LABEL_DEPENDENT",
        },
        "verified_full_contacts": complete_contacts,
        "ok_high_count": high,
        "ok_medium_count": medium,
        "provider_budgets": _provider_budget_metrics(artifact_dir, len(input_ids), counters),
        "browser": {
            "attempts": counters.get("recovery.browser_attempts", 0),
            "successes": counters.get("recovery.browser_successes", 0),
            "errors": counters.get("recovery.browser.errors", 0),
        },
        "interstitial": interstitial,
        "recorded_run": _read_json_if_present(artifact_dir / "validation_run.json"),
        "telemetry_sha256": _sha256_file(artifact_dir / "telemetry.json") if (artifact_dir / "telemetry.json").is_file() else None,
    }


def build_ab_report(
    baseline_dir: Path,
    candidate_dir: Path,
    input_path: Path,
    output_path: Path,
    *,
    expected_path: Path | None = None,
    commands: list[str] | None = None,
    paid_cost_authorized: bool = False,
) -> dict:
    """Generate a hash- and artifact-backed A/B report; never invent labels."""
    def load_artifact(path: Path) -> dict:
        if not Path(path).is_dir():
            return {
                "status": "UNVALIDATED",
                "path": str(Path(path).resolve()),
                "error": "current_artifact_not_available",
                "artifacts": None,
            }
        return _real_artifact_run(path, input_path, expected_path)

    baseline = load_artifact(baseline_dir)
    candidate = load_artifact(candidate_dir)
    config_path = BASE_DIR / "config.py"
    expected_coverage = {}
    if expected_path is not None and Path(expected_path).is_file():
        expected_coverage = assertion_coverage(Path(expected_path))
    has_ground_truth = bool(expected_coverage) and all(
        expected_coverage.get(field, {}).get("asserted", 0) > 0
        for field in FIELDS
    )
    actual_evaluated = has_ground_truth and all(
        run.get("field_metrics", {}).get(field, {}).get("status") == "ACTUAL_EVALUATED"
        and run.get("stage_metrics", {}).get("status") == "ACTUAL_EVALUATED"
        for run in (baseline, candidate)
        for field in FIELDS
    )
    try:
        git_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=BASE_DIR, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        git_commit = None
    report = {
        "schema_version": 2,
        "quality_status": "actual_evaluated" if actual_evaluated else "unvalidated_no_ground_truth",
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "source_tree": {
            "git_commit_sha": git_commit,
            "working_tree_sha256": run_context.source_tree_sha256(BASE_DIR),
            "config_sha256": _sha256_file(config_path),
        },
        "input": {
            "path": str(Path(input_path).resolve()),
            "sha256": _sha256_file(Path(input_path)),
            "rows": len(_ordered_input_ids(Path(input_path))),
            "ordered_source_ids_sha256": _ordered_ids_hash(_ordered_input_ids(Path(input_path))),
        },
        "ground_truth": {
            "path": str(Path(expected_path).resolve()) if expected_path else None,
            "coverage": expected_coverage,
            "labelled_complete": has_ground_truth,
        },
        "commands": list(commands or []),
        "argv": list(sys.argv),
        "run_config": {
            "baseline": baseline.get("recorded_run", {}),
            "candidate": candidate.get("recorded_run", {}),
        },
        "cost_authorization": {
            "explicit": bool(paid_cost_authorized),
            "status": "AUTHORIZED" if paid_cost_authorized else "UNVALIDATED",
        },
        "baseline": baseline,
        "candidate": candidate,
        "quality_gates": {
            "no_candidate_new_fp": "UNVALIDATED",
            "no_precision_lower": "UNVALIDATED",
            "counts_not_lower": "UNVALIDATED",
            "definite_improvement": "UNVALIDATED",
            "minimum_threshold_75": (
                "PASS" if actual_evaluated and config.PUBLICATION_POLICY_MIN_SAFETY_SCORE >= 75
                else "UNVALIDATED" if not has_ground_truth else "FAIL"
            ),
            "paid_live_ab_cost_effect": "UNVALIDATED" if not paid_cost_authorized else "REQUIRES_LABELLED_COST_AUDIT",
        },
        "limitations": [
            "No complete labelled golden workbook was supplied for the 893-row live artifact set; TP/FP/FN and precision/recall remain UNVALIDATED.",
            "Provider ratios/caps are UNVALIDATED when the artifact manifest does not record the run budget calculation.",
        ],
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output_path)
    return report


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
        manifest_base = path.resolve().parent.parent if path.resolve().name == "benchmark_splits.json" else BASE_DIR
        expected = (manifest_base / expected_text).resolve()
        if not expected.exists():
            issues.append(f"{role}: workbook not found: {expected}")
            continue
        if item.get("readiness_mode") != "legacy" and item.get("status") != "manual_validation_pending":
            issues.extend(f"{role}: {value}" for value in readiness_issues(expected))
        if item.get("requires_source_record_id") is True:
            issues.extend(_required_source_record_id_issues(item, expected, manifest_base, role))
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
    parser.add_argument("--all-results", action="append", default=[], help="role=all_results.xlsx")
    parser.add_argument(
        "--require-actual", action="store_true",
        help="Fail unless every manifest role with expected labels has an evaluated actual output.",
    )
    parser.add_argument("--private-seen-workbook", type=Path, default=None, help="Path to private seen firms workbook")
    parser.add_argument("--ab-baseline-dir", type=Path, default=None)
    parser.add_argument("--ab-candidate-dir", type=Path, default=None)
    parser.add_argument("--ab-input", type=Path, default=None)
    parser.add_argument("--ab-expected", type=Path, default=None)
    parser.add_argument("--ab-output", type=Path, default=BASE_DIR / "outputs" / "before_after.json")
    parser.add_argument("--ab-command", action="append", default=[])
    parser.add_argument("--ab-paid-cost-authorized", action="store_true")
    parser.add_argument(
        "--json-output", type=Path,
        default=BASE_DIR / "outputs" / "benchmark_validation.json",
    )
    args = parser.parse_args()

    json_report = {
        "status": "FAIL",
        "files": {},
        "code_sha256": run_context.source_tree_sha256(),
        "config_sha256": _sha256_file(Path(config.__file__)),
        "roles": {},
        "gates": {},
        "issues": [],
    }
    if args.manifest.is_file():
        json_report["files"]["manifest"] = {
            "path": str(args.manifest.resolve()),
            "sha256": _sha256_file(args.manifest),
        }

    sets, issues = validate_manifest(
        args.manifest,
        private_seen_workbook=args.private_seen_workbook,
    )
    actuals = {}
    for value in args.actual:
        if "=" not in value:
            issues.append(f"invalid --actual value: {value}")
            continue
        role, actual_path = value.split("=", 1)
        actuals[role] = actual_path
    all_results = {}
    for value in args.all_results:
        if "=" not in value:
            issues.append(f"invalid --all-results value: {value}")
            continue
        role, all_results_path = value.split("=", 1)
        all_results[role] = all_results_path

    manifest_base = args.manifest.resolve().parent.parent if args.manifest.resolve().name == "benchmark_splits.json" else BASE_DIR
    expected_by_role = {}
    # Resolve expected before reading any actual output.  Besides keeping the
    # per-role paths stable, this prevents --require-actual from using an
    # unbound expected variable.
    for item in sets:
        role = item.get("role", "")
        expected_text = item.get("expected", "")
        if expected_text:
            expected_by_role[role] = (manifest_base / expected_text).resolve()

    quality_issues: list[str] = []
    if args.require_actual:
        for item in sets:
            role = item.get("role", "")
            if not item.get("expected", ""):
                continue
            actual_path = actuals.get(role)
            if not actual_path:
                issues.append(f"{role}: actual output required (--require-actual)")
            all_results_path = all_results.get(role)
            if not all_results_path:
                issues.append(f"{role}: all_results output required (--require-actual)")
            expected = expected_by_role.get(role)
            actual_file = Path(actual_path) if actual_path else None
            all_results_file = Path(all_results_path) if all_results_path else None
            if actual_file is not None and not actual_file.exists():
                issues.append(f"{role}: actual output not found: {actual_file}")
            if all_results_file is not None and not all_results_file.exists():
                issues.append(f"{role}: all_results output not found: {all_results_file}")
            if expected and actual_file and all_results_file and actual_file.exists() and all_results_file.exists():
                population_issues, publication_issues = _population_check(
                    expected, all_results_file, actual_file, role
                )
                issues.extend(population_issues)
                quality_issues.extend(publication_issues)
    for item in sets:
        role = item.get("role", "")
        expected_text = item.get("expected", "")
        if role not in actuals or not expected_text or not Path(actuals[role]).exists():
            continue
        expected = expected_by_role[role]
        metrics, complete = evaluate(expected, Path(actuals[role]))
        role_report = {
            "records": {"complete_matches": len(complete)},
            "fields": {},
        }
        print(f"[{role}] complete={len(complete)}")
        print(f"  coverage: {assertion_coverage(expected)}")
        for field in FIELDS:
            print(f"  {field}: {metrics[field]}")
            values = metrics[field]
            precision_denominator = values["tp"] + values["fp"]
            recall_denominator = values["tp"] + values["fn"]
            role_report["fields"][field] = {
                **values,
                "precision": values["tp"] / precision_denominator if precision_denominator else None,
                "recall": values["tp"] / recall_denominator if recall_denominator else None,
            }
            if not precision_denominator or not recall_denominator:
                quality_issues.append(f"{role}: {field} zero metric denominator")
            if metrics[field]["fp"]:
                quality_issues.append(
                    f"{role}: verified {field} false positives: {metrics[field]['fp']}"
                )
        json_report["roles"][role] = role_report
    ab_requested = any(
        value is not None
        for value in (args.ab_baseline_dir, args.ab_candidate_dir, args.ab_input)
    )
    if ab_requested and not all(
        value is not None
        for value in (args.ab_baseline_dir, args.ab_candidate_dir, args.ab_input)
    ):
        issues.append("A/B report requires --ab-baseline-dir, --ab-candidate-dir and --ab-input")
    if issues:
        json_report["issues"] = issues
        json_report["gates"]["structural"] = "FAIL"
        _write_json_report(args.json_output, json_report)
        print("Benchmark suite issues:")
        for issue in issues:
            print(f"- {issue}")
        raise SystemExit(2)
    if quality_issues:
        json_report["issues"] = quality_issues
        json_report["gates"] = {"structural": "PASS", "quality": "FAIL"}
        _write_json_report(args.json_output, json_report)
        print("Benchmark suite quality issues:")
        for issue in quality_issues:
            print(f"- {issue}")
        raise SystemExit(3)
    ab_report = None
    if ab_requested:
        try:
            ab_report = build_ab_report(
                args.ab_baseline_dir,
                args.ab_candidate_dir,
                args.ab_input,
                args.ab_output,
                expected_path=args.ab_expected,
                commands=args.ab_command,
                paid_cost_authorized=args.ab_paid_cost_authorized,
            )
        except Exception as exc:
            json_report["issues"] = [str(exc)]
            json_report["gates"] = {"structural": "PASS", "quality": "PASS", "ab": "FAIL"}
            _write_json_report(args.json_output, json_report)
            print(f"A/B report error: {exc}")
            raise SystemExit(4)
        print(f"ab_report: {args.ab_output.resolve()}")
    json_report["status"] = "PASS" if args.require_actual else "SCHEMA_ONLY"
    json_report["gates"] = {
        "structural": "PASS",
        "quality": "PASS" if args.require_actual else "NOT_EVALUATED",
    }
    _write_json_report(args.json_output, json_report)
    print("Benchmark suite manifest: OK")
    if ab_report is not None:
        print(f"quality_status: {ab_report['quality_status']}")
    elif args.require_actual:
        print("quality_status: actual_evaluated")
    else:
        print("quality_status: schema_only; quality_not_evaluated")
    if args.private_seen_workbook is not None:
        print("private_seen_gate: OK")
    else:
        print("private_seen_gate: NOT_REQUESTED")


if __name__ == "__main__":
    main()
