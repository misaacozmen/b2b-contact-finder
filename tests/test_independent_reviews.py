from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.adjudicate_a8_reviews import run as adjudicate
from tools.merge_source_reviews import merge


def _review(source_id: str, execution: str, method: str, bundle: str, *, email_order: list[str] | None = None) -> dict:
    emails = email_order or ["info@example.test", "sales@example.test"]
    return {
        "schema_version": 3,
        "reviewer_execution_id": execution,
        "reviewer_method": method,
        "reviewer_entrypoint_sha256": bundle,
        "reviewer_bundle_sha256": bundle,
        "tool_or_prompt_sha256": bundle,
        "input_manifest_sha256": "a" * 64,
        "review_contract_sha256": "b" * 64,
        "source_record_id": source_id,
        "source": "hometex_2026",
        "display_name_observed": "Example Textile",
        "listed_legal_name": "Example Textile Ltd",
        "listed_address": "Istanbul",
        "source_listed_website": "www.example.test",
        "source_listed_website_status": "present",
        "identity_status": "known",
        "evidence_url": "https://hometex.com.tr/en/example",
        "observed_at": "2026-09-05T00:00:00Z",
        "content_sha256": "c" * 64,
        "fields": {
            "website": {"value": "https://www.example.test", "status": "present", "field_evidence": [{"source": method, "value": "https://www.example.test"}]},
            "email": {"value": emails, "status": "present", "field_evidence": [{"source": method, "value": value} for value in emails]},
            "phone": {"value": ["+90 212 555 0101"], "status": "present", "field_evidence": [{"source": method, "value": "+90 212 555 0101"}]},
            "expected_publication": {"value": "publishable", "status": "present", "field_evidence": [{"source": method, "value": "publishable"}]},
        },
        "rationale": f"independent {method} rationale for {source_id}",
        "label_status": "frozen",
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_merge_requires_distinct_method_entrypoint_and_bundle_and_uses_set_semantics(tmp_path: Path):
    first = tmp_path / "structured.jsonl"
    second = tmp_path / "rendered.jsonl"
    queue = tmp_path / "queue.jsonl"
    _write(first, [_review("src:1", "structured-run", "structured", "1" * 64, email_order=["sales@example.test", "info@example.test"])])
    _write(second, [_review("src:1", "rendered-run", "rendered", "2" * 64)])
    merged = merge(first, second, queue)
    assert merged[0]["expected_emails_json"] == '["info@example.test","sales@example.test"]'
    assert merged[0]["expected_phones_e164_json"] == '["+902125550101"]'
    assert not queue.read_text(encoding="utf-8").strip()

    same = tmp_path / "same.jsonl"
    _write(same, [_review("src:1", "other-run", "rendered", "1" * 64)])
    with pytest.raises(RuntimeError, match="not independent"):
        merge(first, same)


def test_merge_writes_adjudication_queue_for_disagreement(tmp_path: Path):
    first, second, queue = tmp_path / "a.jsonl", tmp_path / "b.jsonl", tmp_path / "queue.jsonl"
    a = _review("src:1", "a", "structured", "1" * 64)
    b = _review("src:1", "b", "rendered", "2" * 64)
    b["fields"]["website"]["value"] = "https://other.test"
    _write(first, [a])
    _write(second, [b])
    merged = merge(first, second, queue)
    assert merged[0]["label_status"] == "frozen"
    assert merged[0]["expected_publication"] == "unknown"
    assert json.loads(queue.read_text(encoding="utf-8").splitlines()[0])["status"] == "needs_adjudication"


def test_adjudicator_is_a_third_execution_and_preserves_unresolved_unknown(tmp_path: Path):
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _write(first, [_review("src:1", "a", "structured", "1" * 64)])
    row = _review("src:1", "b", "rendered", "2" * 64)
    row["fields"]["website"]["value"] = "https://other.test"
    _write(second, [row])
    output = tmp_path / "expected.jsonl"
    result = adjudicate(first, second, output, None, None)
    expected = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert result["unresolved_count"] == 1
    assert expected["label_status"] == "frozen"
    assert expected["expected_publication"] == "unknown"
    assert len(json.loads(expected["reviewer_provenance_json"])) == 3
    assert Path(result["manifest"]).exists()
