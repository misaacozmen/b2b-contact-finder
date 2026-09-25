from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _tree_hash(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    if path.is_file():
        return _sha256(path)
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256(child)))
    return digest.hexdigest()


def _ignore_copy(source: Path, names: list[str], delivery: Path) -> set[str]:
    ignored = {name for name in names if name in {"__pycache__", ".pytest_cache"}}
    if source.name == "attempts":
        ignored.update(names)
    if source.resolve() == (delivery / "evidence").resolve() and "verifier_negatives" in names:
        ignored.add("verifier_negatives")
    return ignored


def _copy_delivery(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=lambda path, names: _ignore_copy(Path(path), names, source))


def _safe_environment(command_id: str) -> dict[str, str]:
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "COMSPEC",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update({"B2B_TEST_OFFLINE": "1", "B2B_COMMAND_ID": command_id})
    return environment


def _run_verifier(delivery: Path, target_delivery: Path, command_id: str, log_dir: Path, label: str) -> dict[str, Any]:
    runtime = Path(str(_json(target_delivery / "source_manifest.json")["workspace"])) / ".runtime" / "python3147-sqlite3534" / "python.exe"
    verifier = target_delivery / "source_snapshot" / "tools" / "verify_petzoo_delivery.py"
    command = [str(runtime), str(verifier), "--delivery", str(target_delivery), "--no-write"]
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{label}_stdout.txt"
    stderr_path = log_dir / f"{label}_stderr.txt"
    started = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    clock = time.perf_counter()
    process = subprocess.Popen(
        command, cwd=verifier.parent.parent, env=_safe_environment(command_id),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=900)
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
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        result = {}
    relative_stdout = stdout_path.relative_to(delivery).as_posix()
    relative_stderr = stderr_path.relative_to(delivery).as_posix()
    return {
        "argv": command, "cwd": str(verifier.parent.parent.resolve()),
        "command_id": command_id, "started_at": started,
        "ended_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "duration_seconds": time.perf_counter() - clock,
        "pid": process.pid, "timed_out": timed_out, "exit_code": process.returncode,
        "stdout_file": relative_stdout, "stderr_file": relative_stderr,
        "artifact_sha256": {
            relative_stdout: _sha256(stdout_path), relative_stderr: _sha256(stderr_path),
        },
        "rejection_errors": list(result.get("integrity_errors", [])) + list(result.get("acceptance_errors", [])),
        "overall_decision": result.get("overall_decision", ""),
    }


def _proof_seed(clone: Path) -> dict[str, Any]:
    evidence = clone / "evidence" / "verifier_negatives"
    bootstrap = evidence / "bootstrap_only.txt"
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    bootstrap.write_text("Temporary verifier calibration receipt; never copied to the final package.\n", encoding="utf-8")
    rel = bootstrap.relative_to(clone).as_posix()
    proof = {"exit_code": 0, "artifact_sha256": {rel: _sha256(bootstrap)}}
    cases = {}
    fragments = {
        "V01": "P11 workers_3:", "V02": "P11 workers_1:",
        "V03": "P11 workers_1:", "V04": "fixture hash mismatch",
        "V05": "P05:", "V06": "P11 workers_1:",
        "V07": "P12:", "V08": "JUnit reports 1 failures",
    }
    for name, fragment in fragments.items():
        receipt = {"case": name, "bootstrap_calibration_only": True}
        canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cases[name] = {
            "mutation_receipt": receipt,
            "mutation_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "expected_error_fragment": fragment,
            "control_before": proof,
            "negative_verifier": {**proof, "exit_code": 1, "rejection_errors": [fragment]},
            "control_after": proof,
        }
    return {
        "schema_version": 1, "positive_control_exit_code": 0,
        "final_positive_control": proof, "cases": cases,
    }


def _mutate(clone: Path, case_name: str) -> tuple[dict[str, Any], str]:
    changed: list[Path] = []
    before_hashes: dict[Path, str] = {}

    def track(path: Path) -> None:
        if path not in before_hashes:
            before_hashes[path] = _tree_hash(path)
            changed.append(path)

    if case_name == "V01":
        target = clone / "evidence" / "P11" / "workers_3"
        track(target)
        if target.is_dir():
            shutil.rmtree(target)
        expected = "P11 workers_3:"
    elif case_name == "V02":
        folder = clone / "evidence" / "P11" / "workers_1"
        transport = folder / "transport.jsonl"
        command_path = folder / "command.json"
        rows = [json.loads(line) for line in transport.read_text(encoding="utf-8").splitlines() if line.strip()]
        track(transport)
        track(command_path)
        kept = [row for row in rows if row.get("provider") != "hunter"]
        transport.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in kept), encoding="utf-8")
        command = _json(command_path)
        command.setdefault("artifact_sha256", {})["transport.jsonl"] = _sha256(transport)
        _write_json(command_path, command)
        expected = "P11 workers_1:"
    elif case_name == "V03":
        folder = clone / "evidence" / "P11" / "workers_1"
        result_path = folder / "result.json"
        command_path = folder / "command.json"
        result = _json(result_path)
        track(result_path)
        track(command_path)
        providers = sorted(result.get("physical_provider_calls", {}))
        result["work"] = [
            {**row, "state": "NOT_REQUIRED", "terminal_reason": "unverified_no_call_mutation", "call_id": ""}
            for row in result.get("work", [])
        ]
        result["physical_provider_calls"] = {provider: 0 for provider in providers}
        result["work_state_counts"] = {"NOT_REQUIRED": len(result["work"])}
        for budget in result.get("provider_budgets", {}).values():
            budget["physical_http_attempts"] = 0
            budget["reserved_total"] = 0
        _write_json(result_path, result)
        command = _json(command_path)
        command.setdefault("artifact_sha256", {})["result.json"] = _sha256(result_path)
        _write_json(command_path, command)
        expected = "P11 workers_1:"
    elif case_name == "V04":
        target = clone / "fixture_manifest.json"
        track(target)
        fixture = _json(target)
        fixture["fixture_sha256"] = "0" * 64
        _write_json(target, fixture)
        expected = "fixture hash mismatch"
    elif case_name == "V05":
        target = clone / "evidence" / "P05"
        track(target)
        if target.is_dir():
            shutil.rmtree(target)
        expected = "P05:"
    elif case_name == "V06":
        target = clone / "evidence" / "P11" / "workers_1" / "command.json"
        track(target)
        command = _json(target)
        command["timed_out"] = True
        _write_json(target, command)
        expected = "P11 workers_1: child command failed/timed out"
    elif case_name == "V07":
        base = clone / "evidence" / "P12"
        candidates = sorted(path for path in base.glob("allocation_before_verified_*") if path.is_dir())
        if not candidates:
            raise FileNotFoundError("P12 allocation_before evidence is missing before V07 mutation")
        for target in candidates:
            track(target)
        for target in candidates:
            shutil.rmtree(target)
        expected = "P12: allocation_before evidence missing"
    elif case_name == "V08":
        target = clone / "full_offline_junit.xml"
        track(target)
        xml_root = ET.parse(target).getroot()
        suite = xml_root if xml_root.tag.endswith("testsuite") else next(xml_root.iter("testsuite"))
        suite.set("failures", str(int(suite.attrib.get("failures", "0")) + 1))
        testcase = next(xml_root.iter("testcase"))
        ET.SubElement(testcase, "failure", {"message": "V08 controlled teardown failure"}).text = "negative verifier mutation"
        target.write_bytes(ET.tostring(xml_root, encoding="utf-8", xml_declaration=True))
        expected = "JUnit reports 1 failures"
    else:
        raise ValueError(case_name)

    receipt = {
        "case": case_name,
        "changes": [{
            "path": path.relative_to(clone).as_posix(),
            "before_sha256": before_hashes[path],
            "after_sha256": _tree_hash(path),
        } for path in changed],
    }
    return receipt, expected


def _clone_and_seed(source: Path, destination: Path) -> None:
    _copy_delivery(source, destination)
    _write_json(destination / "evidence" / "verifier_negatives" / "results.json", _proof_seed(destination))


def run_matrix(delivery: Path) -> dict[str, Any]:
    delivery = delivery.resolve()
    contract = _json(delivery / "acceptance_contract.json")
    runtime = Path(str(_json(delivery / "source_manifest.json")["workspace"])) / ".runtime" / "python3147-sqlite3534" / "python.exe"
    if not runtime.is_file():
        raise FileNotFoundError(runtime)
    negative_root = delivery / "evidence" / "verifier_negatives"
    negative_root.mkdir(parents=True, exist_ok=True)
    if (negative_root / "results.json").is_file():
        archive = negative_root / "attempts" / datetime.now(timezone.utc).strftime("prior_matrix_%Y%m%dT%H%M%S%fZ")
        archive.mkdir(parents=True, exist_ok=False)
        shutil.move(str(negative_root / "results.json"), str(archive / "results.json"))
    run_id = datetime.now(timezone.utc).strftime("matrix_%Y%m%dT%H%M%S%fZ")
    proof_root = negative_root / "runs" / run_id
    proof_root.mkdir(parents=True, exist_ok=False)
    cases: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="petzoo-verifier-", dir=str(delivery.parent)) as temporary:
        temporary_root = Path(temporary)
        positive_clone = temporary_root / "positive"
        _clone_and_seed(delivery, positive_clone)

        for index in range(1, 9):
            name = f"V{index:02d}"
            case_logs = proof_root / name
            control_before = _run_verifier(
                delivery, positive_clone, f"{run_id}-{name}-positive-before",
                case_logs, "control_before",
            )
            if control_before["timed_out"] or control_before["exit_code"] != 0:
                raise RuntimeError(f"{name}: clean positive control before mutation failed: {control_before['rejection_errors'][:4]}")

            mutated_clone = temporary_root / f"mutated_{name}"
            _clone_and_seed(delivery, mutated_clone)
            mutation, expected_fragment = _mutate(mutated_clone, name)
            canonical_mutation = json.dumps(mutation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            mutation_hash = hashlib.sha256(canonical_mutation.encode("utf-8")).hexdigest()
            negative = _run_verifier(
                delivery, mutated_clone, f"{run_id}-{name}-negative",
                case_logs, "negative",
            )
            matching = [value for value in negative["rejection_errors"] if expected_fragment in value]
            if negative["timed_out"] or negative["exit_code"] == 0 or not matching:
                raise RuntimeError(f"{name}: verifier did not reject the intended mutation: {negative['rejection_errors'][:8]}")

            control_after = _run_verifier(
                delivery, positive_clone, f"{run_id}-{name}-positive-after",
                case_logs, "control_after",
            )
            if control_after["timed_out"] or control_after["exit_code"] != 0:
                raise RuntimeError(f"{name}: clean positive control after mutation failed: {control_after['rejection_errors'][:4]}")
            cases[name] = {
                "mutation_receipt": mutation,
                "mutation_sha256": mutation_hash,
                "expected_error_fragment": expected_fragment,
                "control_before": control_before,
                "negative_verifier": negative,
                "control_after": control_after,
            }
            shutil.rmtree(mutated_clone)

        results = {
            "schema_version": 1,
            "run_id": run_id,
            "source_tree_sha256": contract["source_tree_sha256"],
            "fixture_sha256": contract["fixture_sha256"],
            "positive_control_exit_code": 0,
            "final_positive_control": cases["V08"]["control_after"],
            "cases": cases,
            "scope_note": "Each V01-V08 mutation ran in a fresh disposable copy; controls used an identical temporary seed receipt only to break verifier self-reference. The seed was not written to the final delivery.",
        }
        results_path = negative_root / "results.json"
        _write_json(results_path, results)

        final_clone = temporary_root / "final_positive"
        _clone_and_seed(delivery, final_clone)
        shutil.copyfile(results_path, final_clone / "evidence" / "verifier_negatives" / "results.json")
        proofs = [results["final_positive_control"]]
        for case in cases.values():
            proofs.extend((case["control_before"], case["negative_verifier"], case["control_after"]))
        for proof in proofs:
            for relative in proof.get("artifact_sha256", {}):
                source = delivery / relative
                target = final_clone / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        final_positive = _run_verifier(
            delivery, final_clone, f"{run_id}-final-positive",
            proof_root / "final_positive", "final_positive",
        )
        if final_positive["timed_out"] or final_positive["exit_code"] != 0:
            raise RuntimeError(f"final positive verifier control failed: {final_positive['rejection_errors'][:10]}")
        results["positive_control_exit_code"] = int(final_positive["exit_code"])
        results["final_positive_control"] = final_positive
        _write_json(results_path, results)

        final_check = _run_verifier(
            delivery, delivery, f"{run_id}-final-package-selfcheck",
            proof_root / "final_selfcheck", "final_selfcheck",
        )
        if final_check["timed_out"] or final_check["exit_code"] != 0:
            raise RuntimeError(f"final package verifier self-check failed: {final_check['rejection_errors'][:10]}")
        _write_json(proof_root / "final_package_selfcheck_command.json", final_check)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delivery", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run_matrix(args.delivery), ensure_ascii=False, indent=2, sort_keys=True))
