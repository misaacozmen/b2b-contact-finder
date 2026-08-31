from __future__ import annotations

import hashlib
import json
from pathlib import Path

from modules import publication_policy


def test_manual_fixture_integrity_and_hash_manifest():
    fixture = Path(__file__).parent / "fixtures" / "manual_verified_tex_zuchex_fixture.json"
    manifest = json.loads((fixture.parent / "manual_fixture_manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][fixture.name]["bytes"] == fixture.stat().st_size
    assert manifest["files"][fixture.name]["sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert payload["manual_verification"]
    assert {row["source_record_id"] for row in payload["records"]} == {
        "texhibition_2026:fixture-001", "zuchex_2026:fixture-001", "texhibition_2026:fixture-002",
    }
    assert sum(bool(row.get("expected_publication_eligible")) for row in payload["records"]) == 0
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    assert len(digest) == 64


def test_manual_fixture_integrity_is_separate_from_actual_publication_gate():
    fixture = Path(__file__).parent / "fixtures" / "manual_verified_tex_zuchex_fixture.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    actual = []
    for row in payload["records"]:
        evaluated = publication_policy.decide_row({
            **row,
            "publication_eligible": row.get("expected_publication_eligible"),
        })
        actual.append(evaluated)
    assert len(actual) == len(payload["records"])
    assert all(item["publishable"] is False for item in actual)
    assert all(item["blockers"] for item in actual)
