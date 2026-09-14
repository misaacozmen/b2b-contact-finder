"""Create immutable v2 superseding records for historical replay receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.free_only_contract import classify_replay_receipt
from tools.free_only_contract import compare_run_configs
from tools.free_only_contract import expected_offline_run_config
from tools.free_only_contract import inspect_budget_config
from tools.free_only_contract import inspect_database
from tools.free_only_contract import validate_manifest
from tools.free_only_contract import validate_manifest_config_sha


TARGETS = {
    "b2b-live-84ba890-replay-receipt.json": "4ea381359ad07c806c2a53e86a47273085fc483783d19319bf39eba339f0a1b0",
    "b2b-live-60f47a8-threshold-75-receipt.json": "028d45aa7ab8f52908289332fa26271ce5798dae5608803590cbdb50cd113cee",
    "b2b-live-60f47a8-threshold-clean-70-receipt.json": "fe46577374aef5685160ac9cd1914c9f3446999c081346fcee7e6ede7d23ebaf",
    "b2b-live-60f47a8-threshold-clean-72-receipt.json": "90b7f1605c40e240011a7f6a9d35d5ff3e5f881d98640104a61870697d70e046",
}
SUPERSEDES_MANIFEST_SHA256 = "3a27d8f753247c059c46a2394ca23d3ba2412a5aea5cedb9b5707ae5dd0e6820"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_not_object:{path}")
    return value


def _package_manifest(parent: Path, receipt: dict) -> Path | None:
    wanted = str(receipt.get("package_manifest_sha256") or "")
    for path in sorted(parent.glob("*-package/package_manifest.json")):
        if wanted and sha256(path) == wanted:
            return path.resolve()
    return None


def _manifest_evidence(path: Path, manifest: dict) -> dict:
    return {
        "path": str(path),
        "sha256": sha256(path),
        "run_id": manifest.get("run_id"),
        "complete": manifest.get("complete"),
        "phase": manifest.get("phase"),
        "status": manifest.get("status"),
        "config_sha256": manifest.get("config_sha256"),
        "artifact_set_sha256": manifest.get("artifact_set_sha256"),
    }


def _safe_original_fields(original: dict) -> dict:
    """Keep receipt metadata only; never copy arbitrary receipt values."""
    hash_fields = (
        "input_sha256", "package_manifest_sha256", "replay_snapshot_sha256",
        "stdout_sha256", "stderr_sha256", "artifact_set_sha256",
    )
    result = {
        key: value for key in hash_fields
        if type(value := original.get(key)) is str
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    }
    replay_count = original.get("replay_network_events")
    result["replay_network_events"] = replay_count if type(replay_count) is int and replay_count >= 0 else 0
    result["network_events"] = []
    return result


def _database_evidence(path: Path, observation: dict) -> dict:
    return {
        "path": str(path),
        "sha256": sha256(path) if path.is_file() else None,
        "phase": observation.get("phase"),
        "item_count": observation.get("item_count"),
        "result_count": observation.get("result_count"),
        "expected_count": observation.get("expected_count"),
        "item_status_violation_count": observation.get("item_status_violation_count", 0),
        "result_status_violation_count": observation.get("result_status_violation_count", 0),
        "provider_usage": observation.get("provider_usage", {}),
        "paid_activity": observation.get("paid_activity"),
        "paid_provider_calls": observation.get("paid_provider_calls"),
        "paid_activity_nonzero": observation.get("paid_activity_nonzero"),
        "paid_budget_nonzero": observation.get("paid_budget_nonzero", False),
        "budget_check_completed": observation.get("budget_check_completed", False),
        "database_validation_failed": observation.get("database_validation_failed", True),
        "check_completed": observation.get("check_completed", False),
    }


def _network_count(original: dict) -> int:
    count = original.get("replay_network_events")
    count = count if type(count) is int and count >= 0 else 0
    events = original.get("network_events")
    return max(count, len(events) if isinstance(events, list) else 0)


def _classify_missing(
    original: dict,
    original_path: Path,
    original_sha256: str,
    package_path: Path | None,
) -> dict:
    package_budget = inspect_budget_config(_json(package_path)) if package_path else {
        "budget_check_completed": True,
        "paid_budget_nonzero": False,
        "budget_offenders": [],
        "config_invalid_details": [],
    }
    package_invalid_details: list[dict] = []
    if package_path is not None:
        package = _json(package_path)
        try:
            validate_manifest(package, require_complete=False)
        except ValueError as exc:
            if str(exc) != "free_only_paid_budget_nonzero":
                package_invalid_details.append({"field": "package_semantic", "reason": str(exc)})
        try:
            validate_manifest_config_sha(package)
        except Exception as exc:
            package_invalid_details.append({"field": "package_config_hash", "reason": type(exc).__name__})
    result = classify_replay_receipt(
        budget_checks=[package_budget],
        threshold_info={
            "threshold_mismatch": False, "threshold_mismatch_details": [],
            "config_mismatch": False, "config_mismatch_details": [],
        },
        database={
            "check_completed": False, "budget_check_completed": False,
            "paid_activity": None, "paid_activity_nonzero": None,
            "paid_provider_calls": None, "paid_budget_nonzero": False,
            "budget_offenders": [], "database_validation_failed": True,
        },
        run_present=False,
        complete_run_missing=True,
        pipeline_exit_code=original.get("pipeline_exit_code", original.get("exit_code", 1)),
        network_event_count=_network_count(original),
        config_invalid=bool(package_invalid_details),
        config_invalid_details=package_invalid_details,
        base=_safe_original_fields(original),
    )
    result["status"] = "FAIL"
    result["schema_version"] = 2
    result["exception"] = result["failure_reason"]
    result["free_only_config_valid"] = False
    result["original_path"] = str(original_path)
    result["original_sha256"] = original_sha256
    result["supersedes_sha256"] = original_sha256
    evidence = {
        "basis": "no complete run manifest was available from the receipt",
        "receipt": {"path": str(original_path), "sha256": original_sha256},
    }
    if package_path is not None:
        evidence["package_manifest"] = {"path": str(package_path), "sha256": sha256(package_path)}
    result["classification_evidence"] = evidence
    return result


def classify_receipt(original_path: Path) -> dict:
    original_path = Path(original_path).resolve()
    original_sha256 = sha256(original_path)
    expected_sha256 = TARGETS.get(original_path.name)
    if expected_sha256 and original_sha256 != expected_sha256:
        raise ValueError(f"original_sha256_mismatch:{original_path.name}")
    original = _json(original_path)
    package_path = _package_manifest(original_path.parent, original)
    run_dir_text = original.get("run_dir")
    run_dir = Path(str(run_dir_text)).resolve() if run_dir_text else None
    manifest_path = run_dir / "manifest.json" if run_dir else None
    if manifest_path is None or not manifest_path.is_file():
        return _classify_missing(original, original_path, original_sha256, package_path)

    manifest = _json(manifest_path)
    complete_run_missing = not (
        manifest.get("complete") is True
        and manifest.get("phase") == "COMPLETE"
        and manifest.get("finalized") is True
        and manifest.get("status") == "complete_free_only"
    )

    package_invalid_details: list[dict] = []
    package = _json(package_path) if package_path else {}
    package_budget = inspect_budget_config(package)
    run_budget = inspect_budget_config(manifest)
    package_config = package.get("run_config") if isinstance(package.get("run_config"), dict) else {}
    expected_config: dict = {}
    try:
        expected_config = expected_offline_run_config(package_config)
    except Exception as exc:
        package_invalid_details.append({"field": "package_run_config", "reason": type(exc).__name__})
    for label, candidate in (("package", package), ("run", manifest)):
        try:
            validate_manifest(candidate, require_complete=False)
        except ValueError as exc:
            if str(exc) != "free_only_paid_budget_nonzero":
                package_invalid_details.append({"field": f"{label}_semantic", "reason": str(exc)})
        try:
            validate_manifest_config_sha(candidate)
        except Exception as exc:
            package_invalid_details.append({"field": f"{label}_config_hash", "reason": type(exc).__name__})
    config_info = compare_run_configs(expected_config, manifest.get("run_config", {})) if expected_config else {}
    db_path = run_dir / "state" / "progress.sqlite3"
    observation = inspect_database(
        db_path,
        str(manifest.get("run_id", run_dir.name)),
        int(package.get("input", {}).get("record_count", 0)),
    )
    db_validation_error = "" if not observation.get("database_validation_failed") else "DATABASE_VALIDATION_FAILED"
    result = classify_replay_receipt(
        budget_checks=[package_budget, run_budget],
        threshold_info=config_info,
        database=observation,
        run_present=True,
        complete_run_missing=complete_run_missing,
        pipeline_exit_code=original.get("pipeline_exit_code", original.get("exit_code", 1)),
        network_event_count=_network_count(original),
        config_invalid=bool(package_invalid_details),
        config_invalid_details=package_invalid_details,
        db_validation_error=db_validation_error,
        base={
            **_safe_original_fields(original),
            "run_id": str(manifest.get("run_id", run_dir.name)),
            "run_dir": str(run_dir),
            "artifact_set_sha256": str(manifest.get("artifact_set_sha256") or ""),
            "artifact_dir": str(run_dir / "output" / "artifacts" / str(manifest.get("artifact_set_sha256") or "")),
        },
    )
    result["schema_version"] = 2
    result["exception"] = "" if result["status"] == "PASS" else result["failure_reason"]
    result["free_only_config_valid"] = result["status"] == "PASS"
    result["original_path"] = str(original_path)
    result["original_sha256"] = original_sha256
    result["supersedes_sha256"] = original_sha256
    result["classification_evidence"] = {
        "basis": "manifest and SQLite evidence; no receipt text classification",
        "receipt": {"path": str(original_path), "sha256": original_sha256},
        "package_manifest": {"path": str(package_path), "sha256": sha256(package_path)} if package_path else None,
        "run_manifest": _manifest_evidence(manifest_path, manifest),
        "database": _database_evidence(db_path, observation),
    }
    return result


def superseding_path(original_path: Path) -> Path:
    return Path(original_path).with_suffix(".v2.json")


def _atomic_write_if_changed(path: Path, payload: str) -> None:
    data = payload.encode("utf-8")
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_superseding_record(original_path: Path, *, output_path: Path | None = None) -> dict:
    original_path = Path(original_path).resolve()
    result = classify_receipt(original_path)
    output_path = Path(output_path or superseding_path(original_path)).resolve()
    _atomic_write_if_changed(output_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def migrate(paths: list[Path], manifest_path: Path | None = None) -> dict:
    records = []
    for path in paths:
        record = write_superseding_record(path)
        new_path = superseding_path(path)
        records.append({
            "original_path": str(Path(path).resolve()),
            "original_sha256": record["original_sha256"],
            "new_path": str(new_path.resolve()),
            "new_sha256": sha256(new_path),
            "supersedes_sha256": record["supersedes_sha256"],
            "failure_reason": record["failure_reason"],
            "evidence_class": record["evidence_class"],
            "replay_evidence_eligible": record["replay_evidence_eligible"],
            "classification_evidence": record["classification_evidence"],
        })
    result = {
        "schema_version": 2,
        "supersedes_manifest_sha256": SUPERSEDES_MANIFEST_SHA256,
        "records": records,
    }
    if manifest_path is not None:
        manifest_path = Path(manifest_path).resolve()
        _atomic_write_if_changed(manifest_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reclassify historical offline replay receipts without touching originals.")
    parser.add_argument("--documents", type=Path, default=Path(r"C:\Users\ISAAC\Documents"))
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    paths = [args.documents / name for name in TARGETS]
    try:
        migrate(paths, args.manifest or args.documents / "b2b-replay-receipt-reclassification-v2.json")
    except Exception as exc:
        print(f"RECLASSIFICATION_BLOCKED:{type(exc).__name__}:{exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
