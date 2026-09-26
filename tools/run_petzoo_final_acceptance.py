from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_environment(delivery: Path, command_id: str, reports: str, network: str, timing: str, receipts: str) -> dict[str, str]:
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "COMSPEC",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({
        "B2B_TEST_OFFLINE": "1",
        "B2B_COMMAND_ID": command_id,
        "B2B_PYTEST_REPORT_JSONL": str(delivery / reports),
        "B2B_SOCKET_DENY_JSONL": str(delivery / network),
        "B2B_TEST_TIMING_LOG": str(delivery / timing),
        "B2B_K6_RECEIPT_JSONL": str(delivery / receipts),
        "BRIGHTDATA_API_KEY": "fake-petzoo-brightdata",
        "GOOGLE_PLACES_API_KEY": "fake-petzoo-places",
        "BRANDFETCH_CLIENT_ID": "fake-petzoo-brandfetch",
        "HUNTER_API_KEY": "fake-petzoo-hunter",
        "OPENROUTER_API_KEY": "fake-petzoo-llm",
    })
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str], delivery: Path,
         record_name: str, stdout_name: str, stderr_name: str,
         command_id: str, fixture_sha256: str, source_sha256: str) -> dict:
    started_at = _utc()
    started = time.perf_counter()
    timed_out = False
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = process.communicate(timeout=28800)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            process.kill()
        stdout, stderr = process.communicate()
    duration = time.perf_counter() - started
    (delivery / stdout_name).write_text(stdout, encoding="utf-8")
    (delivery / stderr_name).write_text(stderr, encoding="utf-8")
    record = {
        "argv": command,
        "cwd": str(cwd.resolve()),
        "runtime": str(Path(command[0]).resolve()),
        "command_id": command_id,
        "environment": {
            key: ("<masked>" if any(word in key.casefold() for word in ("key", "token", "secret", "password")) else value)
            for key, value in env.items()
            if key.startswith("B2B_") or any(word in key.casefold() for word in ("key", "token", "secret", "password"))
        },
        "source_tree_sha256": source_sha256,
        "fixture_sha256": fixture_sha256,
        "started_at": started_at,
        "ended_at": _utc(),
        "duration_seconds": duration,
        "pid": process.pid,
        "timed_out": timed_out,
        "exit_code": process.returncode,
        "stdout_file": stdout_name,
        "stderr_file": stderr_name,
    }
    record["artifact_sha256"] = {
        name: _sha256(delivery / name)
        for name in (stdout_name, stderr_name)
    }
    (delivery / record_name).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def run_final_acceptance(delivery: Path) -> dict:
    delivery = delivery.resolve()
    snapshot = delivery / "source_snapshot"
    manifest = json.loads((delivery / "source_manifest.json").read_text(encoding="utf-8"))
    fixture = json.loads((delivery / "fixture_manifest.json").read_text(encoding="utf-8"))
    contract_path = delivery / "acceptance_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    source_sha256 = str(manifest["runtime_source_tree_sha256"])
    fixture_sha256 = str(fixture["fixture_sha256"])
    if contract.get("source_tree_sha256") != source_sha256 or contract.get("fixture_sha256") != fixture_sha256:
        raise RuntimeError("frozen acceptance contract hash differs from the source/fixture manifests")
    runtime = Path(manifest["workspace"]) / ".runtime" / "python3147-sqlite3534" / "python.exe"
    if not runtime.is_file():
        raise FileNotFoundError(runtime)

    p11 = delivery / "evidence" / "P11"
    attempts_root = p11 / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)
    previous_workers = [p11 / f"workers_{workers}" for workers in (1, 3)]
    if any(path.is_dir() for path in previous_workers):
        preserved = attempts_root / f"pre_final_acceptance_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
        preserved.mkdir(parents=True, exist_ok=False)
        for workers, path in zip((1, 3), previous_workers):
            if path.is_dir():
                shutil.move(str(path), str(preserved / f"workers_{workers}"))

    target_id = f"petzoo-target-acceptance-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    target_environment = _safe_environment(
        delivery, target_id, "acceptance_reports_final.jsonl",
        "network_guard_target_acceptance.jsonl", "timing_target_acceptance.jsonl",
        "k6_receipts_target_acceptance.jsonl",
    )
    target_junit = delivery / "acceptance_junit.xml"
    target_command = [
        str(runtime), "-m", "pytest", "-q", "tests/test_petzoo_pipeline_acceptance.py",
        "--disable-warnings", f"--junitxml={target_junit}",
    ]
    target = _run(
        target_command, cwd=snapshot, env=target_environment, delivery=delivery,
        record_name="acceptance_command.json", stdout_name="acceptance_stdout.txt",
        stderr_name="acceptance_stderr.txt", command_id=target_id,
        fixture_sha256=fixture_sha256, source_sha256=source_sha256,
    )
    if target["timed_out"] or target["exit_code"] != 0:
        return {"acceptance": target, "full_offline": None, "decision": "FAIL_TARGET_ACCEPTANCE"}

    attempts = attempts_root / f"target_acceptance_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
    attempts.mkdir(parents=True, exist_ok=False)
    for workers in (1, 3):
        source = p11 / f"workers_{workers}"
        if source.is_dir():
            shutil.copytree(source, attempts / f"workers_{workers}")

    full_id = str(contract["command_id"])
    full_environment = _safe_environment(
        delivery, full_id, "pytest_reports_final.jsonl",
        "network_guard_final.jsonl", "test_timing_final.jsonl",
        "k6_receipt_final.jsonl",
    )
    full_junit = delivery / "full_offline_junit.xml"
    full_command = [
        str(runtime), "-m", "pytest", "-q", "--disable-warnings",
        f"--junitxml={full_junit}",
    ]
    full = _run(
        full_command, cwd=snapshot, env=full_environment, delivery=delivery,
        record_name="full_offline_command.json", stdout_name="full_offline_stdout.txt",
        stderr_name="full_offline_stderr.txt", command_id=full_id,
        fixture_sha256=fixture_sha256, source_sha256=source_sha256,
    )
    if full["timed_out"] or full["exit_code"] != 0:
        return {"acceptance": target, "full_offline": full, "decision": "FAIL_FULL_OFFLINE"}

    collector_id = f"petzoo-gate-evidence-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    collector_environment = _safe_environment(
        delivery, collector_id, "pytest_reports_final.jsonl",
        "network_guard_final.jsonl", "test_timing_final.jsonl", "k6_receipt_final.jsonl",
    )
    collector = _run(
        [str(runtime), str(snapshot / "tools" / "collect_petzoo_gate_evidence.py"), "--delivery", str(delivery)],
        cwd=snapshot, env=collector_environment, delivery=delivery,
        record_name="gate_evidence_collection_command.json",
        stdout_name="gate_evidence_collection_stdout.txt",
        stderr_name="gate_evidence_collection_stderr.txt",
        command_id=collector_id, fixture_sha256=fixture_sha256,
        source_sha256=source_sha256,
    )
    if collector["timed_out"] or collector["exit_code"] != 0:
        return {
            "acceptance": target, "full_offline": full,
            "gate_evidence_collection": collector,
            "decision": "FAIL_GATE_EVIDENCE_COLLECTION",
        }
    return {
        "acceptance": target, "full_offline": full,
        "gate_evidence_collection": collector, "decision": "PASS_TEST_COMMANDS",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delivery", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run_final_acceptance(args.delivery), indent=2, sort_keys=True))
