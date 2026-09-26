from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.delivery_verifier import verify_matrix


def _write_valid_matrix(root: Path) -> None:
    evidence = root / "evidence.json"
    evidence.write_text("raw evidence\n", encoding="utf-8")
    evidence_hash = hashlib.sha256(evidence.read_bytes()).hexdigest()
    measurements = {
        "k03_projection_measurements.json": [
            {"allowed_field": "email", "suppressed_field": "phone", "valid_partial": True, "raw_unchanged": True, "disallowed_carrier_count": 6, "disallowed_carriers_rejected": 6},
            {"allowed_field": "phone", "suppressed_field": "email", "valid_partial": True, "raw_unchanged": True, "disallowed_carrier_count": 5, "disallowed_carriers_rejected": 5},
        ],
        "k08_pipeline_measurements.json": {
            "source_count": 100, "primary_plan_count": 300, "provider_call_count": 300, "done_call_count": 300, "linkage_count": 300,
            "distinct_queries_per_source": 3, "duplicate_count": 0, "loss_count": 0, "worker_sets_equal": True,
            "worker_variants": {str(worker): {"source_count": 100, "primary_plan_count": 300, "provider_call_count": 300, "done_call_count": 300, "linkage_count": 300, "source_query_sets": {f"s{i}": [f"q{i}-a", f"q{i}-b", f"q{i}-c"] for i in range(100)}} for worker in (1, 3)},
        },
        "k09_budget_measurements.json": {
            "configured_budget": 100, "source_count": 100, "provider_call_count": 100, "first_right_count": 100,
            "second_right_consumed": 0, "remaining_primary_query_count": 200, "budget_terminal_count": 200,
            "run_complete": True, "remaining_terminal_reasons": {"dispatch_not_allocated": 200},
        },
    }
    measurement_hashes = {}
    for name, payload in measurements.items():
        path = root / name
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        measurement_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    gates = {}
    for gate in (f"K{index:02d}" for index in range(1, 16)):
        observed = {"case_passed": True}
        expected = {"case_passed": True}
        if gate == "K03":
            observed.update({
                "valid_partial_count": 2,
                "raw_unchanged_count": 2,
                "disallowed_negative_count": 11,
                "disallowed_negative_rejected": 11,
            })
        elif gate == "K08":
            observed.update({
                "source_count": 100, "primary_plan_count": 300,
                "provider_call_count": 300, "done_call_count": 300,
                "linkage_count": 300, "distinct_queries_per_source": 3,
                "duplicate_count": 0, "loss_count": 0, "worker_sets_equal": True,
            })
        elif gate == "K09":
            observed.update({
                "configured_budget": 100, "source_count": 100,
                "provider_call_count": 100, "first_right_count": 100,
                "second_right_consumed": 0, "remaining_primary_query_count": 200,
                "budget_terminal_count": 200, "run_complete": True,
                "remaining_terminal_reasons": {"dispatch_not_allocated": 200},
            })
        raw_evidence = [{"path": "evidence.json", "sha256": evidence_hash}]
        if gate in measurement_hashes:
            raw_evidence.append({"path": gate.lower().replace("k03", "k03_projection_measurements").replace("k08", "k08_pipeline_measurements").replace("k09", "k09_budget_measurements") + ".json", "sha256": measurement_hashes[{"K03": "k03_projection_measurements.json", "K08": "k08_pipeline_measurements.json", "K09": "k09_budget_measurements.json"}[gate]]})
        gates[gate] = {
            "status": "PASS",
            "test_nodeids": ["tests/test_gate.py::test_gate"],
            "run_id": "run",
            "expected": expected,
            "observed": observed,
            "raw_evidence": raw_evidence,
        }
    (root / "pytest_reports_final.jsonl").write_text(
        json.dumps({"command_id": "run", "nodeid": "tests/test_gate.py::test_gate", "phase": "call", "outcome": "passed"}) + "\n",
        encoding="utf-8",
    )
    (root / "kapanis_matrisi.json").write_text(
        json.dumps({"pytest_command_id": "run", "gates": gates}, indent=2), encoding="utf-8",
    )


@pytest.mark.parametrize("mutation", [
    "missing_path", "299_calls", "source_distribution", "negative_true", "hash_mismatch",
])
def test_delivery_verifier_rejects_tampered_gate_evidence(tmp_path: Path, mutation: str):
    _write_valid_matrix(tmp_path)
    matrix_path = tmp_path / "kapanis_matrisi.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    if mutation == "missing_path":
        matrix["gates"]["K04"]["raw_evidence"] = [{"path": "", "sha256": ""}]
    elif mutation == "299_calls":
        matrix["gates"]["K08"]["observed"]["provider_call_count"] = 299
    elif mutation == "source_distribution":
        matrix["gates"]["K08"]["observed"]["distinct_queries_per_source"] = 2
    elif mutation == "negative_true":
        matrix["gates"]["K03"]["observed"]["restored_disallowed_phone_alternative"] = True
    elif mutation == "hash_mismatch":
        matrix["gates"]["K09"]["raw_evidence"][0]["sha256"] = "0" * 64
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    errors = verify_matrix(tmp_path)
    assert errors, mutation
