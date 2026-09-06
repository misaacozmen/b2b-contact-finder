"""Merge two source-only review passes by source_record_id."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phonenumbers

from modules.scorer import registrable_domain

FIELDS = ("website", "email", "phone", "expected_publication")


def _load(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        source_id = str(row.get("source_record_id") or "").strip()
        if not source_id or source_id in rows:
            raise RuntimeError(f"duplicate or empty source ID in {path}: {source_id}")
        if row.get("label_status") != "frozen":
            raise RuntimeError(f"review row is not frozen in {path}: {source_id}")
        rows[source_id] = row
    if not rows:
        raise RuntimeError(f"review pass is empty: {path}")
    return rows


def _field_value(row: dict[str, Any], field: str) -> Any:
    return row.get("fields", {}).get(field, {}).get("value", "")


def _field_status(row: dict[str, Any], field: str) -> str:
    value = row.get("fields", {}).get(field, {}).get("status", "unknown")
    return str(value or "unknown").strip().casefold()


def _canonical_field_value(value: Any, field: str) -> Any:
    if field == "website":
        return str(value or "").strip()
    if field in {"email", "phone"}:
        values = value if isinstance(value, list) else ([str(value).strip()] if str(value or "").strip() else [])
        return sorted({str(item).strip().casefold() for item in values if str(item).strip()})
    return str(value or "").strip()


def _e164_values(value: Any) -> list[str]:
    raw_values = value if isinstance(value, list) else ([value] if value not in (None, "") else [])
    normalized: set[str] = set()
    for raw in raw_values:
        candidate = str(raw or "").strip()
        try:
            parsed = phonenumbers.parse(candidate, "TR")
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_possible_number(parsed) and phonenumbers.is_valid_number(parsed):
            normalized.add(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))
    return sorted(normalized)


def _field_evidence(row: dict[str, Any], field: str) -> list[dict[str, Any]]:
    value = row.get("fields", {}).get(field, {}).get("field_evidence", [])
    evidence = [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    if evidence:
        return evidence
    return [{
        "source": "reviewer_field_observation",
        "field": field,
        "status": _field_status(row, field),
        "value": _field_value(row, field),
        "evidence_url": row.get("evidence_url", ""),
        "observed_at": row.get("observed_at", ""),
        "content_sha256": row.get("content_sha256", ""),
        "rationale": row.get("rationale", ""),
    }]


def _same_or_unknown(first: object, second: object) -> tuple[str, bool]:
    a = str(first or "").strip()
    b = str(second or "").strip()
    return (a, False) if a == b else ("", True)


def _semantic_validate(rows: list[dict[str, Any]]) -> None:
    forbidden = (
        "apps.apple.com", "xing.com/spi/shares/new", "informa.com",
        "old.texhibitionist.com/storage/", "http://-", "tobb.org.tr/Sayfalar/Eng/AnaSayfa.php",
    )
    for row in rows:
        source = str(row.get("source") or "")
        rationale = str(row.get("rationale") or "")
        flat_fields = {
            "website": (row.get("expected_website"), row.get("website_verified")),
            "email": (row.get("expected_email"), row.get("email_verified")),
            "phone": (row.get("expected_phone"), row.get("phone_verified")),
        }
        for field, (raw_value, raw_status) in flat_fields.items():
            value = str(raw_value or "").strip()
            status = str(raw_status or "unknown").casefold()
            if any(token.casefold() in value.casefold() for token in forbidden):
                raise RuntimeError(f"forbidden non-company value in {field}: {row['source_record_id']}")
            if status == "present" and not value:
                raise RuntimeError(f"present field is empty: {row['source_record_id']}:{field}")
            if status == "absent" and value:
                raise RuntimeError(f"absent field is populated: {row['source_record_id']}:{field}")
        if source == "texhibition_2026" and "Ambiente" in rationale:
            raise RuntimeError(f"source/rationale mismatch: {row['source_record_id']}")
        identity = str(row.get("identity_status") or "unknown").casefold()
        publication = str(row.get("expected_publication") or "unknown").casefold()
        if publication == "publishable" and (identity != "known" or str(row.get("website_verified") or "").casefold() != "present" or not (str(row.get("email_verified") or "").casefold() == "present" or str(row.get("phone_verified") or "").casefold() == "present")):
            raise RuntimeError(f"unsafe publishable label: {row['source_record_id']}")


def merge(pass_1: Path, pass_2: Path, adjudication_queue: Path | None = None) -> list[dict[str, Any]]:
    first = _load(pass_1)
    second = _load(pass_2)
    if set(first) != set(second):
        raise RuntimeError(f"review source ID sets differ: pass1={len(first)} pass2={len(second)}")
    first_meta = next(iter(first.values()))
    second_meta = next(iter(second.values()))
    identity_fields = ("reviewer_method", "reviewer_entrypoint_sha256", "reviewer_bundle_sha256", "tool_or_prompt_sha256")
    for field in identity_fields:
        first_value = {str(row.get(field) or "") for row in first.values()}
        second_value = {str(row.get(field) or "") for row in second.values()}
        if not first_value or not second_value or "" in first_value or "" in second_value:
            raise RuntimeError(f"reviewer {field} is missing")
        if first_value == second_value:
            raise RuntimeError(f"reviewer {field} is not independent")
    execution_ids = {str(row.get("reviewer_execution_id") or "") for row in (*first.values(), *second.values())}
    if len(execution_ids) != 2:
        raise RuntimeError("reviewer execution IDs are not exactly two independent executions")
    merged = []
    for source_id in first:
        a = first[source_id]
        b = second[source_id]
        fields: dict[str, dict[str, str]] = {}
        disagreements: list[str] = []
        for field in FIELDS:
            first_value = _canonical_field_value(_field_value(a, field), field)
            second_value = _canonical_field_value(_field_value(b, field), field)
            if first_value != second_value:
                disagreements.append(field)
                fields[field] = {"value": [] if field in {"email", "phone"} else "", "status": "unknown"}
            else:
                first_status = _field_status(a, field)
                second_status = _field_status(b, field)
                fields[field] = {
                    "value": first_value,
                    "status": first_status if first_status == second_status else "unknown",
                }
        identity_status, identity_disagreement = _same_or_unknown(a.get("identity_status"), b.get("identity_status"))
        legal_name, legal_disagreement = _same_or_unknown(a.get("listed_legal_name", a.get("legal_name_observed")), b.get("listed_legal_name", b.get("legal_name_observed")))
        address, address_disagreement = _same_or_unknown(a.get("listed_address"), b.get("listed_address"))
        for name, disagreement in (("identity_status", identity_disagreement), ("listed_legal_name", legal_disagreement), ("listed_address", address_disagreement)):
            if disagreement:
                disagreements.append(name)
        if identity_disagreement:
            identity_status = "unknown"
        if legal_disagreement:
            legal_name = ""
        if address_disagreement:
            address = ""
        execution_1 = str(a.get("reviewer_execution_id") or "").strip()
        execution_2 = str(b.get("reviewer_execution_id") or "").strip()
        if not execution_1 or not execution_2 or execution_1 == execution_2:
            raise RuntimeError(f"reviewer executions are not independent: {source_id}")
        merged_publication = str(fields["expected_publication"]["value"] or "unknown").casefold()
        if merged_publication == "publishable" and (identity_status != "known" or fields["website"]["status"] != "present" or not (fields["email"]["status"] == "present" or fields["phone"]["status"] == "present")):
            merged_publication = "unknown"
        website_value = str(fields["website"]["value"] or "")
        email_values = list(fields["email"]["value"] or [])
        phone_values = _e164_values(fields["phone"]["value"])
        if fields["phone"]["status"] == "present" and not phone_values:
            disagreements.append("phone_invalid")
            fields["phone"] = {"value": [], "status": "unknown"}
        if merged_publication == "publishable" and not (email_values or phone_values):
            merged_publication = "unknown"
        reasons = ["review_disagreement"] if disagreements else []
        if merged_publication == "abstain" and not reasons:
            reasons = ["insufficient_verified_evidence"]
        if merged_publication == "unknown" and not reasons:
            reasons = ["evidence_insufficient"]
        listed_source_website, listed_source_disagreement = _same_or_unknown(a.get("source_listed_website"), b.get("source_listed_website"))
        if listed_source_disagreement:
            listed_source_website = ""
        expected_domains = sorted({registrable_domain(website_value)} - {""})
        expected_emails = sorted({str(value).casefold() for value in email_values if str(value).strip()})
        expected_phones = sorted(phone_values)
        evidence_hash = hashlib.sha256(
            f"{a.get('content_sha256', '')}\n{b.get('content_sha256', '')}".encode("utf-8")
        ).hexdigest()
        merged.append({
            "schema_version": 3,
            "source_record_id": source_id,
            "source": a.get("source"),
            "Company": a.get("display_name_observed") or b.get("display_name_observed") or "",
            "official_profile_url": a.get("evidence_url") or b.get("evidence_url") or "",
            "listed_legal_name": legal_name,
            "listed_address": address,
            "listed_phone": fields["phone"]["value"],
            "source_listed_website": listed_source_website,
            "source_listed_website_status": "present" if listed_source_website else "absent",
            "expected_website": website_value,
            "website_verified": fields["website"]["status"],
            "expected_email": expected_emails[0] if expected_emails else "",
            "email_verified": fields["email"]["status"],
            "expected_phone": expected_phones[0] if expected_phones else "",
            "phone_verified": fields["phone"]["status"],
            "expected_publication": merged_publication,
            "expected_website_domains_json": json.dumps(expected_domains, ensure_ascii=False, separators=(",", ":")),
            "expected_emails_json": json.dumps(expected_emails, ensure_ascii=False, separators=(",", ":")),
            "expected_phones_e164_json": json.dumps(expected_phones, ensure_ascii=False, separators=(",", ":")),
            "expected_publication_reason_codes_json": json.dumps(sorted(reasons), ensure_ascii=False, separators=(",", ":")),
            "website_field_evidence_json": json.dumps(_field_evidence(a, "website") + _field_evidence(b, "website"), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "email_field_evidence_json": json.dumps(_field_evidence(a, "email") + _field_evidence(b, "email"), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "phone_field_evidence_json": json.dumps(_field_evidence(a, "phone") + _field_evidence(b, "phone"), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "reviewer_provenance_json": json.dumps([
                {"execution_id": execution_1, "method": a.get("reviewer_method"), "entrypoint_sha256": a.get("reviewer_entrypoint_sha256"), "bundle_sha256": a.get("reviewer_bundle_sha256"), "evidence_url": a.get("evidence_url"), "content_sha256": a.get("content_sha256")},
                {"execution_id": execution_2, "method": b.get("reviewer_method"), "entrypoint_sha256": b.get("reviewer_entrypoint_sha256"), "bundle_sha256": b.get("reviewer_bundle_sha256"), "evidence_url": b.get("evidence_url"), "content_sha256": b.get("content_sha256")},
            ], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "identity_evidence_urls": a.get("evidence_url") or b.get("evidence_url") or "",
            "contact_evidence_urls": a.get("evidence_url") or b.get("evidence_url") or "",
            "observed_at": max(str(a.get("observed_at") or ""), str(b.get("observed_at") or "")),
            "evidence_content_sha256": evidence_hash,
            "reviewer_pass_1": execution_1,
            "reviewer_pass_2": execution_2,
            "disagreement_reason": ";".join(disagreements),
            "label_status": "frozen",
            "review_evidence": {
                "pass_1": {
                    "source_record_id": source_id,
                    "evidence_url": a.get("evidence_url"),
                    "observed_at": a.get("observed_at"),
                    "content_sha256": a.get("content_sha256"),
                    "rationale": a.get("rationale"),
                },
                "pass_2": {
                    "source_record_id": source_id,
                    "evidence_url": b.get("evidence_url"),
                    "observed_at": b.get("observed_at"),
                    "content_sha256": b.get("content_sha256"),
                    "rationale": b.get("rationale"),
                },
            },
            "identity_status": identity_status,
            "reviewer_methods": [a.get("reviewer_method"), b.get("reviewer_method")],
            "input_manifest_sha256": [a.get("input_manifest_sha256"), b.get("input_manifest_sha256")],
            "review_contract_sha256": a.get("review_contract_sha256"),
            "tool_or_prompt_sha256": [a.get("tool_or_prompt_sha256"), b.get("tool_or_prompt_sha256")],
            "rationale": f"{a.get('rationale', '')} | {b.get('rationale', '')}",
        })
    _semantic_validate(merged)
    if adjudication_queue is not None:
        queue = [
            {
                "source_record_id": row["source_record_id"],
                "disagreement_fields": [value for value in row.get("disagreement_reason", "").split(";") if value],
                "pass_1_execution_id": row.get("reviewer_pass_1"),
                "pass_2_execution_id": row.get("reviewer_pass_2"),
                "pass_1_bundle_sha256": (row.get("tool_or_prompt_sha256") or ["", ""])[0],
                "pass_2_bundle_sha256": (row.get("tool_or_prompt_sha256") or ["", ""])[-1],
                "evidence": row.get("review_evidence", {}),
                "status": "needs_adjudication" if row.get("disagreement_reason") else "no_disagreement",
            }
            for row in merged
            if row.get("disagreement_reason")
        ]
        adjudication_queue.parent.mkdir(parents=True, exist_ok=True)
        adjudication_queue.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in queue), encoding="utf-8")
    return merged


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pass-1", type=Path, required=True)
    parser.add_argument("--pass-2", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adjudication-queue", type=Path)
    args = parser.parse_args()
    rows = merge(args.pass_1, args.pass_2, args.adjudication_queue)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"rows": len(rows), "frozen": sum(row["label_status"] == "frozen" for row in rows)}))


if __name__ == "__main__":
    main()
