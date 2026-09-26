from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


REQUIRED_GATES = tuple(f"K{index:02d}" for index in range(1, 16))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _reports(root: Path) -> list[dict[str, Any]]:
    path = root / "pytest_reports_final.jsonl"
    if not path.is_file():
        raise FileNotFoundError(path)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _test_passed(rows: list[dict[str, Any]], nodeid: str, command_id: str) -> tuple[bool, str]:
    selected = [row for row in rows if str(row.get("nodeid", "")) == nodeid]
    if not selected:
        return False, f"pytest nodeid missing: {nodeid}"
    if command_id and any(str(row.get("command_id", "")) != command_id for row in selected):
        return False, f"pytest nodeid belongs to a different run: {nodeid}"
    if any(str(row.get("outcome", "")) in {"skipped", "deselected"} or row.get("wasxfail") for row in selected):
        return False, f"pytest nodeid skipped/xfail/deselected: {nodeid}"
    call = [row for row in selected if str(row.get("phase", "")) == "call"]
    if not call or any(str(row.get("outcome", "")) != "passed" for row in call):
        return False, f"pytest nodeid did not pass in call phase: {nodeid}"
    teardown = [row for row in selected if str(row.get("phase", "")) == "teardown"]
    if any(str(row.get("outcome", "")) != "passed" for row in teardown):
        return False, f"pytest teardown failed: {nodeid}"
    return True, ""


def _evidence_errors(root: Path, entry: dict[str, Any]) -> list[str]:
    errors = []
    evidence = entry.get("raw_evidence")
    if not isinstance(evidence, list) or not evidence:
        return [f"{entry.get('gate')}: raw_evidence is empty"]
    for item in evidence:
        if not isinstance(item, dict):
            errors.append(f"{entry.get('gate')}: malformed raw evidence entry")
            continue
        relative = str(item.get("path", "")).strip()
        expected = str(item.get("sha256", "")).strip()
        if not relative:
            errors.append(f"{entry.get('gate')}: empty evidence path")
            continue
        path = (root / relative).resolve()
        if root.resolve() not in path.parents:
            errors.append(f"{entry.get('gate')}: evidence escapes delivery: {relative}")
        elif not path.is_file():
            errors.append(f"{entry.get('gate')}: evidence missing: {relative}")
        elif not re.fullmatch(r"[0-9a-f]{64}", expected) or digest(path) != expected:
            errors.append(f"{entry.get('gate')}: evidence hash mismatch: {relative}")
    return errors


def _measurement(root: Path, entry: dict[str, Any], filename: str) -> tuple[Any | None, list[str]]:
    candidates = [
        str(item.get("path", ""))
        for item in entry.get("raw_evidence", [])
        if isinstance(item, dict) and Path(str(item.get("path", ""))).name == filename
    ]
    if not candidates:
        return None, [f"{entry.get('gate')}: raw measurement missing: {filename}"]
    path = (root / candidates[0]).resolve()
    try:
        return read_json(path), []
    except Exception as exc:
        return None, [f"{entry.get('gate')}: raw measurement unreadable: {filename}:{type(exc).__name__}"]


def _condition_errors(root: Path, entry: dict[str, Any]) -> list[str]:
    gate = str(entry.get("gate", ""))
    expected = entry.get("expected")
    observed = entry.get("observed")
    errors = []
    if not isinstance(expected, dict) or not expected:
        return [f"{gate}: expected measurements missing"]
    if not isinstance(observed, dict) or not observed:
        return [f"{gate}: observed measurements missing"]
    if gate == "K03":
        raw, raw_errors = _measurement(root, entry, "k03_projection_measurements.json")
        errors.extend(raw_errors)
        rows = raw if isinstance(raw, list) else []
        orientations = {(row.get("allowed_field"), row.get("suppressed_field")) for row in rows if isinstance(row, dict)}
        actual = {
            "valid_partial_count": sum(row.get("valid_partial") is True for row in rows if isinstance(row, dict)),
            "raw_unchanged_count": sum(row.get("raw_unchanged") is True for row in rows if isinstance(row, dict)),
            "disallowed_negative_count": sum(int(row.get("disallowed_carrier_count", -1)) for row in rows if isinstance(row, dict)),
            "disallowed_negative_rejected": sum(int(row.get("disallowed_carriers_rejected", -1)) for row in rows if isinstance(row, dict)),
        }
        if len(rows) != 2 or orientations != {("email", "phone"), ("phone", "email")}:
            errors.append("K03: raw projection orientations are incomplete")
        if actual["valid_partial_count"] != 2 or actual["raw_unchanged_count"] != 2:
            errors.append("K03: valid partial/raw fingerprint measurement failed")
        if actual["disallowed_negative_count"] != 11 or actual["disallowed_negative_rejected"] != 11:
            errors.append("K03: suppressed carrier negative measurement failed")
        if any(row.get("restored_disallowed_phone_alternative") is True or row.get("restored_disallowed_email_alternative") is True for row in rows if isinstance(row, dict)):
            errors.append("K03: disallowed alternative was accepted")
        for key, value in actual.items():
            if observed.get(key) != value:
                errors.append(f"K03: matrix/raw mismatch for {key}")
    elif gate == "K08":
        raw, raw_errors = _measurement(root, entry, "k08_pipeline_measurements.json")
        errors.extend(raw_errors)
        required = {
            "source_count": 100, "primary_plan_count": 300,
            "provider_call_count": 300, "done_call_count": 300,
            "linkage_count": 300,
        }
        raw_variants = raw.get("worker_variants") if isinstance(raw, dict) else None
        if not isinstance(raw_variants, dict) or set(raw_variants) != {"1", "3"}:
            errors.append("K08: raw worker variants missing")
        else:
            variant_sets = []
            for worker in ("1", "3"):
                variant = raw_variants[worker]
                source_sets = variant.get("source_query_sets") if isinstance(variant, dict) else None
                if not isinstance(source_sets, dict) or len(source_sets) != 100:
                    errors.append(f"K08: raw worker {worker} source distribution missing")
                    continue
                if any(not isinstance(queries, list) or len(queries) != 3 or len(set(queries)) != 3 for queries in source_sets.values()):
                    errors.append(f"K08: raw worker {worker} has 2/4 or duplicate source distribution")
                variant_sets.append(source_sets)
                for key, value in required.items():
                    if variant.get(key) != value:
                        errors.append(f"K08: raw worker {worker} {key}={variant.get(key)!r}, expected {value!r}")
            if len(variant_sets) == 2 and variant_sets[0] != variant_sets[1]:
                errors.append("K08: raw worker 1/3 source-query sets differ")
        for key, value in required.items():
            if not isinstance(raw, dict) or raw.get(key) != value or observed.get(key) != value:
                errors.append(f"K08: {key}={observed.get(key)!r}, expected {value!r}")
        if not isinstance(raw, dict) or raw.get("distinct_queries_per_source") != 3 or raw.get("duplicate_count") != 0 or raw.get("loss_count") != 0:
            errors.append("K08: raw distribution measurement failed")
        if observed.get("distinct_queries_per_source") != 3 or observed.get("duplicate_count") != 0 or observed.get("loss_count") != 0:
            errors.append("K08: per-source distribution measurement failed")
        if not isinstance(raw, dict) or raw.get("worker_sets_equal") is not True or observed.get("worker_sets_equal") is not True:
            errors.append("K08: worker 1/3 source-query sets differ")
    elif gate == "K09":
        raw, raw_errors = _measurement(root, entry, "k09_budget_measurements.json")
        errors.extend(raw_errors)
        required = {
            "configured_budget": 100, "source_count": 100,
            "provider_call_count": 100, "first_right_count": 100,
            "second_right_consumed": 0, "remaining_primary_query_count": 200,
            "budget_terminal_count": 200,
        }
        for key, value in required.items():
            if not isinstance(raw, dict) or raw.get(key) != value or observed.get(key) != value:
                errors.append(f"K09: {key}={observed.get(key)!r}, expected {value!r}")
        if not isinstance(raw, dict) or raw.get("run_complete") is not True or observed.get("run_complete") is not True:
            errors.append("K09: run did not terminate cleanly")
        raw_reasons = raw.get("remaining_terminal_reasons") if isinstance(raw, dict) else {}
        if not isinstance(raw_reasons, dict) or raw_reasons != {"dispatch_not_allocated": 200}:
            errors.append("K09: raw remaining work lacks exact budget-terminal distribution")
        if any("budget" not in str(key).casefold() and str(key) != "dispatch_not_allocated" for key in (observed.get("remaining_terminal_reasons") or {})):
            errors.append("K09: remaining work lacks a budget-terminal reason")
    else:
        condition = str(entry.get("condition", "case_passed"))
        if observed.get(condition) is not True:
            errors.append(f"{gate}: measured condition {condition!r} is not true")
    return errors


def verify_matrix(root: Path) -> list[str]:
    root = Path(root).resolve()
    errors: list[str] = []
    matrix_path = root / "kapanis_matrisi.json"
    if not matrix_path.is_file():
        return ["kapanis_matrisi.json missing"]
    matrix = read_json(matrix_path)
    gates = matrix.get("gates") if isinstance(matrix, dict) else None
    if not isinstance(gates, dict) or set(gates) != set(REQUIRED_GATES):
        return ["kapanis_matrisi.gates must contain exactly K01-K15"]
    try:
        reports = _reports(root)
    except Exception as exc:
        return [f"pytest evidence unreadable: {type(exc).__name__}:{exc}"]
    command_id = str(matrix.get("pytest_command_id", ""))
    if not command_id:
        errors.append("pytest_command_id missing")
    for gate in REQUIRED_GATES:
        entry = gates[gate]
        if not isinstance(entry, dict) or entry.get("status") != "PASS":
            errors.append(f"{gate}: status is not PASS")
            continue
        nodeids = entry.get("test_nodeids")
        if isinstance(nodeids, str):
            nodeids = [nodeids]
        if not isinstance(nodeids, list) or not nodeids or any(not str(value).strip() for value in nodeids):
            errors.append(f"{gate}: test_nodeids missing")
        else:
            for nodeid in nodeids:
                passed, reason = _test_passed(reports, str(nodeid), command_id)
                if not passed:
                    errors.append(f"{gate}: {reason}")
        errors.extend(_evidence_errors(root, entry))
        errors.extend(_condition_errors(root, {"gate": gate, **entry}))
    return errors


def _verify_source(root: Path, errors: list[str]) -> None:
    snapshot = root / "source_snapshot"
    manifest_path = root / "source_manifest.json"
    if not snapshot.is_dir() or not manifest_path.is_file():
        errors.append("source snapshot or manifest missing")
        return
    manifest = read_json(manifest_path)
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        errors.append("source_manifest.files missing")
        return
    expected = {str(item.get("path")): item for item in entries if isinstance(item, dict)}
    actual = {
        path.relative_to(snapshot).as_posix(): path
        for path in snapshot.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if set(actual) != set(expected):
        errors.append("source snapshot file set differs from manifest")
    for relative, path in actual.items():
        item = expected.get(relative, {})
        if int(item.get("bytes", -1)) != path.stat().st_size or str(item.get("sha256", "")) != digest(path):
            errors.append(f"source manifest mismatch: {relative}")
    for relative in ("input", "state", "data", "runs"):
        if (snapshot / relative).exists():
            errors.append(f"mutable source tree copied: {relative}")
    for relative in ("input/firms.xlsx", "data/benchmark_seen_company_hashes.json", "build_benchmark_seen_hashes.py"):
        if (snapshot / relative).exists():
            errors.append(f"forbidden source copied: {relative}")


def _verify_delivery_manifest(root: Path, errors: list[str]) -> None:
    path = root / "delivery_manifest.json"
    if not path.is_file():
        errors.append("delivery_manifest.json missing")
        return
    payload = read_json(path)
    entries = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        errors.append("delivery_manifest.files missing")
        return
    for item in entries:
        relative = str(item.get("path", ""))
        target = root / relative
        if not relative or relative == "delivery_manifest.json" or not target.is_file():
            errors.append(f"delivery artifact missing: {relative}")
        elif int(item.get("bytes", -1)) != target.stat().st_size or str(item.get("sha256", "")) != digest(target):
            errors.append(f"delivery artifact hash mismatch: {relative}")


def verify_delivery(root: Path) -> list[str]:
    root = Path(root).resolve()
    errors: list[str] = []
    _verify_source(root, errors)
    _verify_delivery_manifest(root, errors)
    for name in ("full_offline_command.json", "k14_command.json"):
        path = root / name
        if not path.is_file():
            errors.append(f"command receipt missing: {name}")
    full_path = root / "full_offline_command.json"
    if full_path.is_file():
        full = read_json(full_path)
        if int(full.get("exit_code", -1)) != 0:
            errors.append("full offline command was nonzero")
        if Path(str(full.get("cwd", ""))).resolve() != (root / "source_snapshot").resolve():
            errors.append("full offline cwd is not source_snapshot")
        if not Path(str(full.get("runtime", ""))).is_file():
            errors.append("full offline runtime missing")
    k14 = root / "k14_command.json"
    if k14.is_file():
        value = read_json(k14)
        for key in ("compileall_exit", "pip_check_exit", "git_diff_check_exit"):
            if int(value.get(key, -1)) != 0:
                errors.append(f"{key} failed")
    report_path = root / "pytest_reports_final.jsonl"
    if not report_path.is_file():
        errors.append("pytest_reports_final.jsonl missing")
    else:
        try:
            rows = _reports(root)
            if not rows or any(str(row.get("outcome", "")) in {"failed", "error"} for row in rows):
                errors.append("pytest report is empty or has failures")
            if any(str(row.get("phase", "")) == "teardown" and str(row.get("outcome", "")) != "passed" for row in rows):
                errors.append("pytest teardown is not clean")
        except Exception as exc:
            errors.append(f"pytest report unreadable: {type(exc).__name__}:{exc}")
    errors.extend(verify_matrix(root))
    return errors


def main(root: Path | None = None) -> int:
    root = Path(root or Path(__file__).resolve().parent).resolve()
    errors = verify_delivery(root)
    if errors:
        print(json.dumps({"status": "FAIL", "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "PASS", "delivery": str(root), "gates": 15, "full_suite_exit": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
