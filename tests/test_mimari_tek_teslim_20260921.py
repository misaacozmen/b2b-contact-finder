from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from modules import output_artifacts, publication_policy, report
from tests.test_mimari_reaudit_altinci_20260921 import _content_fixture


def _partial_row() -> dict:
    source_id = "source:projection-integrity"
    content, records = _content_fixture(source_id)
    return {
        "company": "Projection Company",
        "source_record_id": source_id,
        "free_state": "DONE",
        "paid_required": False,
        "paid_state": "NOT_REQUIRED",
        "status": "OK_HIGH_CONFIDENCE",
        "score": 95,
        "website": "https://projection.example",
        "email": "info@projection.example",
        "phone": "+999",
        "email_publication_status": "allowed",
        "phone_publication_status": "suppressed",
        "identity_assessment": {"publishable": True},
        "content_decision": content,
        "content_evidence_records": records,
    }


def test_projection_receipt_uses_raw_snapshot_and_rejects_evidence_tampering():
    projected = output_artifacts.project_publication_row(_partial_row())
    assert output_artifacts.validate_publication_projection(projected)
    assert publication_policy.is_publishable_row(projected)
    assert output_artifacts.partition_output_rows([projected])[0] == [projected]
    assert report.failed_rows([projected]) == []

    for mutate in (
        lambda receipt: receipt["raw_snapshot"].pop("content_evidence_records"),
        lambda receipt: receipt["raw_snapshot"]["content_evidence_records"][0].update(content_sha256="b" * 64),
        lambda receipt: receipt["raw_snapshot"]["content_evidence_records"][0].update(target_source_record_id="source:other"),
        lambda receipt: receipt.update(raw_snapshot_sha256="f" * 64, raw_decision_sha256="f" * 64),
    ):
        tampered = deepcopy(projected)
        mutate(tampered["publication_projection_receipt"])
        assert output_artifacts.validate_publication_projection(tampered) is False
        assert publication_policy.is_publishable_row(tampered) is False


def test_projection_validation_is_fail_closed_for_malformed_receipt():
    projected = output_artifacts.project_publication_row(_partial_row())
    for value in (None, [], "malformed", {"schema_version": "bad"}):
        candidate = deepcopy(projected)
        candidate["publication_projection_receipt"] = value
        assert output_artifacts.validate_publication_projection(candidate) is False
        assert publication_policy.is_publishable_row(candidate) is False


@pytest.mark.parametrize(
    ("allowed_field", "suppressed_field"),
    (("email", "phone"), ("phone", "email")),
)
def test_projection_rejects_every_readded_suppressed_contact_carrier(
    allowed_field, suppressed_field,
):
    raw = _partial_row()
    raw.update({
        "email_source": "raw-email-source",
        "email_source_url": "https://raw.example/email",
        "alternative_emails": "backup@example.invalid",
        "alternative_email_sources": "raw-email-alternative-source",
        "phone_source": "raw-phone-source",
        "phone_source_url": "https://raw.example/phone",
        "phone_label": "switchboard",
        "alternative_phones": "+998000000000",
        "alternative_phone_sources": "raw-phone-alternative-source",
        "email_publication_status": "allowed" if allowed_field == "email" else "suppressed",
        "phone_publication_status": "allowed" if allowed_field == "phone" else "suppressed",
    })
    content = dict(raw["content_decision"])
    content["email_allowed"] = allowed_field == "email"
    content["phone_allowed"] = allowed_field == "phone"
    raw["content_decision"] = content
    before = deepcopy(raw)

    projected = output_artifacts.project_publication_row(raw)
    assert output_artifacts.validate_publication_projection(projected)
    assert raw == before
    assert projected["publication_projection_receipt"]["raw_snapshot_sha256"] == publication_policy._canonical_hash(raw)

    for carrier in publication_policy.CONTACT_PROJECTION_FIELDS[suppressed_field]:
        candidate = deepcopy(projected)
        candidate[carrier] = "+999"
        assert output_artifacts.validate_publication_projection(candidate) is False, carrier

    for carrier in publication_policy.CONTACT_PROJECTION_FIELDS[allowed_field]:
        assert projected.get(carrier, "") == raw.get(carrier, "")

    target = os.environ.get("B2B_K03_MEASUREMENTS", "").strip()
    if target:
        path = Path(target)
        measurements = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        measurements.append({
            "allowed_field": allowed_field,
            "suppressed_field": suppressed_field,
            "valid_partial": True,
            "raw_unchanged": raw == before,
            "disallowed_carrier_count": len(publication_policy.CONTACT_PROJECTION_FIELDS[suppressed_field]),
            "disallowed_carriers_rejected": len(publication_policy.CONTACT_PROJECTION_FIELDS[suppressed_field]),
        })
        path.write_text(json.dumps(measurements, indent=2, sort_keys=True), encoding="utf-8")
