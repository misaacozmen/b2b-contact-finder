"""Run main.py against a Golden6 replay snapshot with a process-wide audit guard."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.free_only_contract import validate_database as validate_free_only_database
from tools.free_only_contract import validate_manifest as validate_free_only_manifest
from tools.free_only_contract import expected_offline_run_config
from tools.free_only_contract import validate_manifest_config_sha
from tools.free_only_contract import compare_run_configs
from tools.free_only_contract import inspect_budget_config
from tools.free_only_contract import inspect_database
from tools.free_only_contract import classify_replay_receipt


NETWORK_EVENT_PREFIXES = (
    "http.client.", "urllib.", "ssl.",
)
NETWORK_EVENTS = {
    "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
    "socket.gethostbyaddr", "socket.getnameinfo", "socket.getfqdn",
    "socket.connect", "socket.create_connection", "socket.sendto", "socket.sendmsg",
    "http.client.connect",
    "http.client.send", "http.client.putrequest", "http.client.request",
    "urllib.Request", "urllib.urlopen", "ssl.wrap_socket",
    "ssl.SSLContext.wrap_socket",
}


class ReplayNetworkViolation(RuntimeError):
    """Raised on the first audited external-network attempt."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_not_object:{path}")
    return value


def _complete_run(repo: Path, input_hash: str, before: set[str]) -> Path | None:
    runs = repo / "runs"
    matches = []
    if not runs.is_dir():
        return None
    for path in sorted(runs.glob("*/manifest.json")):
        if path.parent.name in before:
            continue
        try:
            manifest = _json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if manifest.get("input_sha256") == input_hash:
            matches.append(path.parent)
    return matches[0] if len(matches) == 1 else None


def _audit_factory(receipts: list[dict]):
    def audit(event: str, args: tuple) -> None:
        if event not in NETWORK_EVENTS and not any(event.startswith(prefix) for prefix in NETWORK_EVENT_PREFIXES):
            return
        record = {
            "event": event,
            "args": [str(value)[:200] for value in args],
        }
        receipts.append(record)
        raise ReplayNetworkViolation(f"offline_replay_network_event:{event}")
    return audit


def run(repo_root: Path, input_path: Path, package_dir: Path, receipt_path: Path) -> dict:
    repo = Path(repo_root).resolve()
    input_path = Path(input_path).resolve()
    package_dir = Path(package_dir).resolve()
    package_manifest_path = package_dir / "package_manifest.json"
    package_manifest = _json(package_manifest_path)
    package_budget = inspect_budget_config(package_manifest)
    preflight_error = ""
    execution_blocked = package_budget.get("paid_budget_nonzero") is True
    expected_config: dict = {}
    try:
        if package_budget.get("failure_reason") == "CONFIG_INVALID":
            raise ValueError("free_only_budget_config_invalid")
        try:
            validate_free_only_manifest(package_manifest, require_complete=False)
        except ValueError as exc:
            if str(exc) != "free_only_paid_budget_nonzero":
                raise
            execution_blocked = True
        validate_manifest_config_sha(package_manifest)
        expected_config = expected_offline_run_config(package_manifest["run_config"])
    except Exception as exc:
        preflight_error = f"{type(exc).__name__}:{exc}"
    replay_snapshot = package_dir / str(package_manifest.get("replay", {}).get("snapshot", ""))
    if not replay_snapshot.is_file():
        raise ValueError("replay_snapshot_missing")
    if _sha256(replay_snapshot) != package_manifest.get("replay", {}).get("snapshot_sha256"):
        raise ValueError("replay_snapshot_hash_mismatch")
    if hashlib.sha256(input_path.read_bytes()).hexdigest() != package_manifest.get("input", {}).get("sha256"):
        raise ValueError("replay_input_hash_mismatch")

    before = {path.name for path in (repo / "runs").iterdir() if path.is_dir()} if (repo / "runs").is_dir() else set()
    receipts: list[dict] = []
    sys.addaudithook(_audit_factory(receipts))
    old_argv = sys.argv
    old_env = os.environ.copy()
    stdout = io.StringIO()
    stderr = io.StringIO()
    pipeline_exit_code = 1
    exception = ""
    try:
        os.environ.pop("B2B_TEST_OFFLINE", None)
        os.environ.pop("ENABLE_JS_FALLBACK", None)
        sys.argv = [
            str(repo / "main.py"), "--input", str(input_path), "--rerank-cache",
            "--replay-snapshot", str(replay_snapshot), "--no-allow-paid",
            "--finalize-without-paid", "--non-interactive",
        ]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if preflight_error or execution_blocked:
                exception = preflight_error or "free_only_paid_budget_nonzero"
            else:
                try:
                    runpy.run_path(str(repo / "main.py"), run_name="__main__")
                    pipeline_exit_code = 0
                except SystemExit as exc:
                    pipeline_exit_code = int(exc.code) if isinstance(exc.code, int) else 1
                except ReplayNetworkViolation as exc:
                    exception = str(exc)
                    pipeline_exit_code = 2
                except BaseException as exc:
                    exception = f"{type(exc).__name__}:{exc}"
                    pipeline_exit_code = 1
    finally:
        sys.argv = old_argv
        for key in list(os.environ):
            if key not in old_env:
                os.environ.pop(key, None)
        os.environ.update(old_env)

    receipt = {
        "schema_version": 2,
        "status": "FAIL",
        "exit_code": pipeline_exit_code,
        "pipeline_exit_code": pipeline_exit_code,
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "package_manifest_sha256": _sha256(package_manifest_path),
        "replay_snapshot_sha256": _sha256(replay_snapshot),
        "replay_network_events": len(receipts),
        "network_events": receipts,
        "exception": exception,
        "stdout_sha256": hashlib.sha256(stdout.getvalue().encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.getvalue().encode("utf-8")).hexdigest(),
    }
    run_root = _complete_run(repo, receipt["input_sha256"], before)
    complete_run_missing = run_root is None
    paid_activity: dict[str, int] | None = None
    run_budget: dict | None = None
    db_observation: dict = {}
    db_validation_error = ""
    config_validation_error = ""
    threshold_info = {
        "threshold_mismatch": False,
        "threshold_mismatch_details": [],
        "config_mismatch": False,
        "config_mismatch_details": [],
    }
    if run_root is not None:
        manifest = _json(run_root / "manifest.json")
        complete_run_missing = not (
            manifest.get("complete") is True
            and manifest.get("phase") == "COMPLETE"
            and manifest.get("finalized") is True
            and manifest.get("status") == "complete_free_only"
        )
        artifact_dir = run_root / "output" / "artifacts" / str(manifest.get("artifact_set_sha256", ""))
        receipt.update({
            "run_id": str(manifest.get("run_id", run_root.name)),
            "run_dir": str(run_root),
            "artifact_dir": str(artifact_dir),
            "artifact_set_sha256": str(manifest.get("artifact_set_sha256", "")),
        })
        run_budget = inspect_budget_config(manifest)
        actual_config = manifest.get("run_config") if isinstance(manifest.get("run_config"), dict) else {}
        if expected_config:
            threshold_info = compare_run_configs(expected_config, actual_config)
        else:
            threshold_info = {
                "threshold_mismatch": False,
                "threshold_mismatch_details": [],
                "config_mismatch": True,
                "config_mismatch_details": [{"field": "package_preflight", "expected": "valid", "actual": preflight_error}],
            }
        try:
            validate_free_only_manifest(manifest, require_complete=False)
            validate_manifest_config_sha(manifest)
        except Exception as exc:
            text = str(exc)
            if text not in {"free_only_paid_budget_nonzero", "free_only_budget_config_invalid"}:
                config_validation_error = f"{type(exc).__name__}:{exc}"
        db_path = run_root / "state" / "progress.sqlite3"
        db_observation = inspect_database(
            db_path,
            str(manifest.get("run_id", run_root.name)),
            int(package_manifest.get("input", {}).get("record_count", 0)),
        )
        if db_observation.get("paid_activity") is not None:
            paid_activity = db_observation["paid_activity"]
        try:
            validate_free_only_database(
                db_path,
                str(manifest.get("run_id", run_root.name)),
                int(package_manifest.get("input", {}).get("record_count", 0)),
            )
        except ValueError as exc:
            db_validation_error = str(exc)
    else:
        db_observation = {
            "check_completed": False,
            "budget_check_completed": False,
            "paid_activity": None,
            "paid_activity_nonzero": None,
            "paid_provider_calls": None,
            "budget_offenders": [],
        }

    classification = classify_replay_receipt(
        budget_checks=[package_budget] + ([run_budget] if run_budget else []),
        threshold_info=threshold_info,
        database=db_observation,
        run_present=run_root is not None,
        complete_run_missing=complete_run_missing,
        pipeline_exit_code=pipeline_exit_code,
        network_event_count=len(receipts),
        config_invalid=(preflight_error != "" or config_validation_error != ""),
        config_invalid_details=([
            {"field": "package_validation", "reason": preflight_error}
            for _ in [1] if preflight_error
        ] + [
            {"field": "run_validation", "reason": config_validation_error}
            for _ in [1] if config_validation_error
        ]),
        config_validation_error=config_validation_error,
        db_validation_error=db_validation_error,
        base=receipt,
    )
    if classification["status"] != "PASS" and not receipt.get("exception"):
        classification["exception"] = db_validation_error or config_validation_error or preflight_error or classification["failure_reason"]
    classification["free_only_config_valid"] = classification["status"] == "PASS"
    classification["network_events"] = receipts
    classification["replay_network_events"] = len(receipts)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt_path.with_name(f".{receipt_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(classification, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(receipt_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return classification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a network-blocked Golden6 offline replay.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.repo_root, args.input, args.package, args.receipt)
    except Exception as exc:
        print(f"OFFLINE_REPLAY_BLOCKED:{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
