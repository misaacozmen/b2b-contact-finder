from __future__ import annotations

import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _gate_junit(gate: str, nodeids: list[str], reports: list[dict]) -> bytes:
    suite = ET.Element("testsuite", {
        "name": gate, "tests": str(len(nodeids)), "failures": "0",
        "errors": "0", "skipped": "0",
    })
    for nodeid in nodeids:
        call = next(row for row in reports if row.get("nodeid") == nodeid and row.get("phase") == "call")
        teardown = [row for row in reports if row.get("nodeid") == nodeid and row.get("phase") == "teardown"]
        if call.get("outcome") != "passed" or any(row.get("outcome") != "passed" for row in teardown):
            raise RuntimeError(f"{gate}: failed call or teardown report cannot be packaged")
        module, _, test_name = nodeid.partition("::")
        case = ET.SubElement(suite, "testcase", {
            "classname": module, "name": test_name,
            "time": str(float(call.get("duration_seconds", call.get("duration", 0)) or 0)),
        })
        properties = ET.SubElement(case, "properties")
        ET.SubElement(properties, "property", {"name": "nodeid", "value": nodeid})
    return ET.tostring(suite, encoding="utf-8", xml_declaration=True)


def collect(delivery: Path) -> dict:
    delivery = delivery.resolve()
    contract = json.loads((delivery / "acceptance_contract.json").read_text(encoding="utf-8"))
    full_command = json.loads((delivery / "full_offline_command.json").read_text(encoding="utf-8"))
    report_path = delivery / "pytest_reports_final.jsonl"
    reports = [
        json.loads(line) for line in report_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    source_files = {
        "stdout.txt": delivery / "full_offline_stdout.txt",
        "stderr.txt": delivery / "full_offline_stderr.txt",
    }
    results = {}
    for gate, entry in contract["gates"].items():
        nodeids = [str(value) for value in entry["nodeids"]]
        gate_rows = [
            row for row in reports
            if row.get("command_id") == contract["command_id"] and row.get("nodeid") in nodeids
        ]
        call_rows = [row for row in gate_rows if row.get("phase") == "call"]
        teardown_rows = [row for row in gate_rows if row.get("phase") == "teardown"]
        if {row.get("nodeid") for row in call_rows} != set(nodeids):
            raise RuntimeError(f"{gate}: final full-suite report is missing a required nodeid")
        if any(row.get("outcome") != "passed" for row in call_rows + teardown_rows):
            raise RuntimeError(f"{gate}: failed call or teardown report cannot be packaged")
        folder = delivery / "evidence" / gate
        folder.mkdir(parents=True, exist_ok=True)
        for name, source in source_files.items():
            if not source.is_file():
                raise FileNotFoundError(source)
            shutil.copyfile(source, folder / name)
        junit_path = folder / "junit.xml"
        junit_path.write_bytes(_gate_junit(gate, nodeids, gate_rows))
        command = {
            **full_command,
            "scope_gate": gate,
            "required_nodeids": nodeids,
            "timed_out": full_command.get("timed_out"),
            "exit_code": full_command.get("exit_code"),
            "source_tree_sha256": full_command.get("source_tree_sha256"),
            "fixture_sha256": full_command.get("fixture_sha256"),
            "stdout_file": "stdout.txt",
            "stderr_file": "stderr.txt",
            "junit_file": "junit.xml",
        }
        (folder / "command.json").write_text(json.dumps(command, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raw = []
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.name in {"command.json", "stdout.txt", "stderr.txt", "junit.xml", "measurement.json"}:
                continue
            if "attempts" in path.parts or path.name.endswith(".lock"):
                continue
            raw.append({
                "path": path.relative_to(delivery).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            })
        measurement = {
            "gate": gate,
            "command_id": contract["command_id"],
            "source_tree_sha256": contract["source_tree_sha256"],
            "fixture_sha256": contract["fixture_sha256"],
            "expected_result_class": entry["expected_result_class"],
            "required_assertions": entry["required_assertions"],
            "nodeids": nodeids,
            "call_reports": call_rows,
            "teardown_reports": teardown_rows,
            "raw_evidence": raw,
        }
        (folder / "measurement.json").write_text(json.dumps(measurement, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        command["artifact_sha256"] = {
            name: _sha256(folder / name)
            for name in ("stdout.txt", "stderr.txt", "junit.xml", "measurement.json")
        }
        (folder / "command.json").write_text(json.dumps(command, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        results[gate] = {"nodeids": len(nodeids), "raw_artifacts": len(raw)}
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--delivery", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(collect(args.delivery), indent=2, sort_keys=True))
