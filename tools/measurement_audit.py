"""Deterministic, offline field-level measurement for the B2B pipeline.

The tool deliberately treats ``unknown`` as an observed outcome.  It remains
in the accuracy denominator and cannot silently become a pass or disappear
from the coverage denominator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


FIELDS = ("website", "email", "phone")
STATES = {"present", "absent", "unknown"}
PREDICTION_STATES = STATES | {"missing_prediction"}


def _sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Return a Wilson score interval without changing the supplied denominator."""
    successes = int(successes)
    total = int(total)
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denominator
    radius = z * ((p * (1.0 - p) / total + z * z / (4.0 * total * total)) ** 0.5) / denominator
    return (max(0.0, centre - radius), min(1.0, centre + radius))


def build_synthetic_fixture(count: int = 100) -> dict[str, Any]:
    """Build a frozen two-source fixture with explicit alternatives and unknowns."""
    count = int(count)
    if count <= 0:
        raise ValueError("count must be positive")
    companies = []
    for index in range(count):
        source_id = f"synthetic:company-{index + 1:03d}"
        domain = f"company-{index + 1:03d}.example"
        email_domain = domain
        phone = f"+90212000{index:04d}"
        records = [
            {
                "source_record_id": f"{source_id}:registry",
                "source_type": "registry",
                "company": f"Company {index + 1:03d}",
                "website": {"state": "present", "value": f"https://{domain}", "accepted_alternatives": [f"https://www.{domain}"]},
                "email": {"state": "present", "value": f"info@{email_domain}", "accepted_alternatives": [f"sales@{email_domain}"]},
                "phone": {"state": "present", "value": phone, "accepted_alternatives": []},
                "predicted_website": {"state": "present", "value": f"https://{domain}"},
                "predicted_email": {"state": "present", "value": f"info@{email_domain}"},
                "predicted_phone": {"state": "present", "value": phone},
            },
            {
                "source_record_id": f"{source_id}:directory",
                "source_type": "directory",
                "company": f"Company {index + 1:03d}",
                "website": {"state": "present", "value": f"https://{domain}", "accepted_alternatives": [f"https://www.{domain}"]},
                "email": {"state": "absent", "value": "", "accepted_alternatives": []},
                "phone": {"state": "absent", "value": "", "accepted_alternatives": []},
            },
        ]
        companies.append({"source_record_id": source_id, "company": f"Company {index + 1:03d}", "records": records})
    return {
        "schema_version": 1,
        "fixture_kind": "synthetic_two_source_100",
        "companies": companies,
        "labels_frozen": True,
    }


def _normalize_observation(value: Any, *, missing: bool = False) -> dict[str, Any]:
    if missing:
        return {"state": "missing_prediction", "value": "", "accepted_alternatives": []}
    if not isinstance(value, dict):
        return {"state": "unknown", "value": "", "accepted_alternatives": []}
    state = str(value.get("state", "unknown")).casefold()
    if state not in STATES:
        state = "unknown"
    alternatives = value.get("accepted_alternatives", [])
    if not isinstance(alternatives, list):
        alternatives = [alternatives]
    return {
        "state": state,
        "value": str(value.get("value", "") or ""),
        "accepted_alternatives": [str(item) for item in alternatives if str(item)],
    }


class MeasurementInputError(ValueError):
    """The frozen measurement input has contradictory observations."""


def _observation_key(field: str, value: dict[str, Any]) -> tuple:
    raw = str(value.get("value") or "").strip()
    if field == "website":
        raw = raw.casefold().removeprefix("https://").removeprefix("http://").rstrip("/")
    else:
        raw = raw.casefold()
    return (value.get("state"), raw)


def _prediction_value(row: dict[str, Any], field: str) -> tuple[bool, Any]:
    values = []
    key = f"predicted_{field}"
    if key in row:
        values.append(row[key])
    if isinstance(row.get("prediction"), dict) and field in row["prediction"]:
        values.append(row["prediction"][field])
    if f"observed_{field}" in row:
        values.append(row[f"observed_{field}"])
    if not values:
        return False, None
    normalized = [_normalize_observation(value) for value in values]
    keys = {_observation_key(field, value) for value in normalized}
    if len(keys) != 1:
        raise MeasurementInputError(f"conflicting prediction for {field}")
    return True, normalized[0]


def _merge_truth(field: str, values: list[dict[str, Any]]) -> dict[str, Any]:
    present = [item for item in values if item["state"] == "present"]
    if present:
        keys = {_observation_key(field, item) for item in present}
        if len(keys) != 1:
            raise MeasurementInputError(f"conflicting truth for {field}")
        merged = dict(present[0])
        alternatives = {str(value) for item in present for value in item.get("accepted_alternatives", [])}
        alternatives.update(str(value) for item in present for value in (item.get("value", ""),))
        alternatives.discard(merged.get("value", ""))
        merged["accepted_alternatives"] = sorted(alternatives)
        return merged
    if any(item["state"] == "absent" for item in values):
        return {"state": "absent", "value": "", "accepted_alternatives": []}
    return {"state": "unknown", "value": "", "accepted_alternatives": []}


def evaluate_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one immutable observation per dataset/view/company key."""
    dataset_version = str(fixture.get("dataset_version") or fixture.get("schema_version") or "unknown")
    normalized: dict[tuple[str, str, str], dict[str, Any]] = {}
    source_count = 0
    for company in fixture.get("companies", []):
        if not isinstance(company, dict):
            continue
        company_id = str(company.get("source_record_id") or company.get("company") or "").strip()
        if not company_id:
            continue
        view = str(company.get("input_view") or "default")
        key = (dataset_version, view, company_id)
        bucket = normalized.setdefault(key, {"truth": {field: [] for field in FIELDS}, "predictions": {field: [] for field in FIELDS}, "contact_verified": False, "rows": 0})
        bucket["contact_verified"] = bucket["contact_verified"] or bool(company.get("contact_target_verified") or company.get("independent_target_evidence"))
        for record in company.get("records", []):
            if not isinstance(record, dict):
                continue
            source_count += 1
            bucket["rows"] += 1
            bucket["contact_verified"] = bucket["contact_verified"] or bool(record.get("contact_target_verified") or record.get("independent_target_evidence"))
            for field in FIELDS:
                bucket["truth"][field].append(_normalize_observation(record.get(field)))
                present, prediction = _prediction_value(record, field)
                if present:
                    bucket["predictions"][field].append(prediction)
    company_results = []
    rows = []
    for (view_dataset, view, company_id), bucket in sorted(normalized.items()):
        per_field = {}
        for field in FIELDS:
            truth = _merge_truth(field, bucket["truth"][field])
            predictions = bucket["predictions"][field]
            prediction = _normalize_observation(None, missing=True) if not predictions else predictions[0]
            if predictions and len({_observation_key(field, item) for item in predictions}) != 1:
                raise MeasurementInputError(f"conflicting prediction for {field}")
            accepted = {truth["value"], *truth["accepted_alternatives"]}
            correct = bool(
                (truth["state"] == "absent" and prediction["state"] == "absent")
                or (truth["state"] == "present" and prediction["state"] == "present" and prediction["value"] in accepted)
            )
            per_field[field] = {"truth": truth, "prediction": prediction, "correct": correct, "correct_published": correct}
        # Contact values from a wrong website are not target-correct absent an
        # explicit frozen relationship edge.
        if not per_field["website"]["correct"] and not bucket["contact_verified"]:
            for field in ("email", "phone"):
                per_field[field]["correct"] = False
                per_field[field]["correct_published"] = False
        full = all(per_field[f]["correct_published"] for f in FIELDS)
        partial = per_field["website"]["correct_published"] and any(per_field[f]["correct_published"] for f in ("email", "phone"))
        company_results.append({"source_record_id": company_id, "dataset_version": view_dataset, "input_view": view, "full": full, "partial": partial, "fields": per_field})
        rows.append((view_dataset, view, company_id, bucket, per_field))
    fields = {}
    for field in FIELDS:
        total = present_truth = correct = unknown = missing_prediction = published = correct_published = incorrect_published = published_truth_unknown = wrong_company_contact = 0
        for _dataset, _view, _company_id, bucket, per_field in rows:
            truth = per_field[field]["truth"]
            prediction = per_field[field]["prediction"]
            total += 1
            present_truth += int(truth["state"] == "present")
            unknown += int(prediction["state"] == "unknown")
            # Keep the diagnostic count of missing source observations, while
            # all accuracy/coverage denominators remain one per company key.
            missing_prediction += int(prediction["state"] == "missing_prediction") * max(1, int(bucket.get("rows", 1)))
            correct += int(per_field[field]["correct"])
            is_published = prediction["state"] in {"present", "unknown"}
            published += int(is_published)
            good = bool(is_published and per_field[field]["correct_published"])
            correct_published += int(good)
            incorrect_published += int(is_published and not good)
            published_truth_unknown += int(is_published and truth["state"] == "unknown")
            wrong_company_contact += int(field in {"email", "phone"} and prediction["state"] == "present" and not per_field[field]["correct"])
        precision = correct_published / published if published else None
        recall = correct_published / present_truth if present_truth else None
        low, high = wilson_interval(correct_published, published)
        recall_low, recall_high = wilson_interval(correct_published, present_truth)
        fields[field] = {"total": total, "correct": correct, "unknown": unknown, "missing_prediction": missing_prediction, "wrong_company_contact": wrong_company_contact, "truth_present": present_truth, "published": published, "correct_published": correct_published, "incorrect_published": incorrect_published, "published_truth_unknown": published_truth_unknown, "precision": precision, "recall": recall, "full_coverage": correct_published / total if total else None, "partial_coverage": correct_published / total if total else None, "accuracy_wilson_95": [low, high], "recall_wilson_95": [recall_low, recall_high]}
    company_count = len(company_results)
    full_count = sum(bool(item["full"]) for item in company_results)
    partial_count = sum(bool(item["partial"]) for item in company_results)
    return {
        "schema_version": 2,
        "measurement_kind": "offline_synthetic_fixture",
        "row_count": source_count,
        "company_count": company_count,
        "source_count": source_count,
        "fields": fields,
        "company_metrics": {
            "full_coverage_count": full_count,
            "partial_coverage_count": partial_count,
            "full_coverage": full_count / company_count if company_count else None,
            "partial_coverage": partial_count / company_count if company_count else None,
            "denominator": company_count,
        },
        "unknown_is_pass": False,
        "denominator_policy": "explicit_predictions; firms_deduplicated_by_dataset_version_input_view_company_id",
        "dataset_version": dataset_version,
        "input_views": sorted({key[1] for key in normalized}),
    }


def write_measurement_bundle(output_dir: Path, fixture: dict[str, Any] | None = None) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fixture = fixture or build_synthetic_fixture(100)
    result = evaluate_fixture(fixture)
    manifest = {
        "schema_version": 2,
        "measurement_kind": result["measurement_kind"],
        "truth_input_sha256": _sha256({"companies": [{"source_record_id": c.get("source_record_id"), "records": [{k: v for k, v in r.items() if not str(k).startswith(("predicted_", "observed_")) and k != "prediction"} for r in c.get("records", [])]} for c in fixture.get("companies", [])]}),
        "prediction_input_sha256": _sha256({"companies": [{"source_record_id": c.get("source_record_id"), "records": [{k: v for k, v in r.items() if str(k).startswith("predicted_") or k == "prediction"} for r in c.get("records", [])]} for c in fixture.get("companies", [])]}),
        "config_sha256": _sha256({"fields": FIELDS, "denominator_policy": result["denominator_policy"]}),
        "labels_sha256": _sha256(fixture.get("companies", [])),
        "row_count": result["row_count"],
    }
    (output_dir / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "measurement.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"manifest": manifest, "measurement": result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=100)
    args = parser.parse_args()
    write_measurement_bundle(args.output, build_synthetic_fixture(args.count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
