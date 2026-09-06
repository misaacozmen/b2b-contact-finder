"""Third, source-only adjudication pass for two A8 reviewer outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phonenumbers

from modules.scorer import registrable_domain


CONTRACT = b"A8-adjudicator-v3"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        source_id = str(row.get("source_record_id") or "").strip()
        if not source_id or source_id in result:
            raise RuntimeError(f"duplicate/empty source ID: {source_id}")
        result[source_id] = row
    if not result:
        raise RuntimeError(f"empty review pass: {path}")
    return result


def _value(row: dict, field: str) -> Any:
    return (row.get("fields") or {}).get(field, {}).get("value", "")


def _status(row: dict, field: str) -> str:
    return str((row.get("fields") or {}).get(field, {}).get("status", "unknown") or "unknown").casefold()


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return sorted({str(item).strip().casefold() for item in value if str(item).strip()})
    return [str(value).strip().casefold()] if str(value or "").strip() else []


_EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")


def _email_values(value: Any) -> list[str]:
    return sorted({item for item in _list(value) if _EMAIL_RE.fullmatch(item)})


def _e164_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else ([value] if value not in (None, "") else [])
    result = set()
    for raw in values:
        candidate = str(raw or "").strip()
        if not candidate:
            continue
        try:
            parsed = phonenumbers.parse(candidate, "TR")
            if parsed.country_code == 90 and phonenumbers.is_valid_number(parsed):
                result.add(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))
        except phonenumbers.NumberParseException:
            continue
    return sorted(result)


def _evidence(row: dict, field: str) -> list[dict]:
    value = (row.get("fields") or {}).get(field, {}).get("field_evidence", [])
    evidence = [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    if evidence:
        return evidence
    return [{
        "source": "reviewer_field_observation",
        "field": field,
        "status": _status(row, field),
        "value": _value(row, field),
        "evidence_url": row.get("evidence_url", ""),
        "observed_at": row.get("observed_at", ""),
        "content_sha256": row.get("content_sha256", ""),
        "rationale": row.get("rationale", ""),
    }]


def _website_value(first: Any, second: Any) -> tuple[str, bool]:
    first_value = str(first or "").strip()
    second_value = str(second or "").strip()
    if first_value and second_value:
        first_domain = registrable_domain(first_value)
        second_domain = registrable_domain(second_value)
        if first_domain and first_domain == second_domain:
            return first_value, False
        return "", True
    return first_value or second_value, False


def _row(source_id: str, first: dict, second: dict, execution_id: str) -> dict:
    disagreements = []
    fields: dict[str, tuple[Any, str]] = {}
    website, website_disagreement = _website_value(_value(first, "website"), _value(second, "website"))
    if website_disagreement:
        disagreements.append("website")
        fields["website"] = ("", "unknown")
    else:
        website_status = "present" if website else "unknown"
        fields["website"] = (website, website_status)
    emails = sorted(set(_email_values(_value(first, "email")) + _email_values(_value(second, "email"))))
    phones = sorted(set(_e164_values(_value(first, "phone")) + _e164_values(_value(second, "phone"))))
    fields["email"] = (emails, "present" if emails else "unknown")
    fields["phone"] = (phones, "present" if phones else "unknown")
    identity = str(first.get("identity_status") or "unknown").casefold()
    if identity != str(second.get("identity_status") or "unknown").casefold():
        disagreements.append("identity_status")
        identity = "unknown"
    legal = str(first.get("listed_legal_name") or "").strip()
    if legal != str(second.get("listed_legal_name") or "").strip():
        disagreements.append("listed_legal_name")
        legal = ""
    website = str(fields["website"][0] or "")
    emails = _list(fields["email"][0])
    phones = _e164_values(fields["phone"][0])
    if identity == "known" and website and (emails or phones) and not website_disagreement:
        publication = "publishable"
    elif identity == "known" and not disagreements:
        publication = "abstain"
    else:
        publication = "unknown"
    reasons = ["adjudication_unresolved"] if publication == "unknown" else ["adjudicated_abstain"] if publication == "abstain" else []
    provenance = []
    for row in (first, second):
        provenance.append({
            "execution_id": row.get("reviewer_execution_id"), "method": row.get("reviewer_method"),
            "entrypoint_sha256": row.get("reviewer_entrypoint_sha256"), "bundle_sha256": row.get("reviewer_bundle_sha256"),
            "evidence_url": row.get("evidence_url"), "content_sha256": row.get("content_sha256"),
        })
    provenance.append({"execution_id": execution_id, "method": "third_adjudication", "reason": "source-only adjudication of pass outputs"})
    return {
        "schema_version": 3, "source_record_id": source_id, "Company": first.get("display_name_observed") or second.get("display_name_observed") or "",
        "source": first.get("source") or second.get("source"), "official_profile_url": first.get("evidence_url") or second.get("evidence_url") or "",
        "listed_legal_name": legal, "listed_address": first.get("listed_address") if first.get("listed_address") == second.get("listed_address") else "",
        "listed_phone": phones[0] if phones else "", "source_listed_website": first.get("source_listed_website", ""),
        "source_listed_website_status": first.get("source_listed_website_status", "absent"),
        "expected_website": website, "website_verified": fields["website"][1],
        "expected_email": emails[0] if emails else "", "email_verified": fields["email"][1],
        "expected_phone": phones[0] if phones else "", "phone_verified": fields["phone"][1] if phones else "unknown",
        "expected_publication": publication,
        "expected_website_domains_json": json.dumps(sorted({registrable_domain(website)} - {""}), separators=(",", ":")),
        "expected_emails_json": json.dumps(emails, separators=(",", ":")),
        "expected_phones_e164_json": json.dumps(phones, separators=(",", ":")),
        "expected_publication_reason_codes_json": json.dumps(reasons, separators=(",", ":")),
        "website_field_evidence_json": json.dumps(_evidence(first, "website") + _evidence(second, "website"), sort_keys=True, separators=(",", ":")),
        "email_field_evidence_json": json.dumps(_evidence(first, "email") + _evidence(second, "email"), sort_keys=True, separators=(",", ":")),
        "phone_field_evidence_json": json.dumps(_evidence(first, "phone") + _evidence(second, "phone"), sort_keys=True, separators=(",", ":")),
        "reviewer_provenance_json": json.dumps(provenance, sort_keys=True, separators=(",", ":")),
        "identity_evidence_urls": first.get("evidence_url") or second.get("evidence_url") or "",
        "contact_evidence_urls": first.get("evidence_url") or second.get("evidence_url") or "",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "evidence_content_sha256": _sha((str(first.get("content_sha256", "")) + "\n" + str(second.get("content_sha256", ""))).encode()),
        "reviewer_pass_1": first.get("reviewer_execution_id", ""), "reviewer_pass_2": second.get("reviewer_execution_id", ""),
        "disagreement_reason": ";".join(disagreements), "label_status": "frozen",
        "identity_status": identity,
        "reviewer_methods": [first.get("reviewer_method"), second.get("reviewer_method"), "third_adjudication"],
        "tool_or_prompt_sha256": [first.get("tool_or_prompt_sha256"), second.get("tool_or_prompt_sha256"), _sha(CONTRACT)],
        "review_evidence": {"pass_1": _evidence(first, "expected_publication"), "pass_2": _evidence(second, "expected_publication"), "adjudication": {"execution_id": execution_id, "disagreements": disagreements}},
    }


def run(pass_1: Path, pass_2: Path, output: Path, manifest_path: Path | None, queue_path: Path | None) -> dict:
    first, second = _load(pass_1), _load(pass_2)
    if set(first) != set(second):
        raise RuntimeError("review source ID sets differ")
    execution_id = f"adjudicator-{uuid.uuid4().hex}"
    rows = [_row(source_id, first[source_id], second[source_id], execution_id) for source_id in first]
    queue = [{"source_record_id": row["source_record_id"], "disagreement_fields": row["disagreement_reason"].split(";") if row["disagreement_reason"] else [], "status": "resolved" if not row["disagreement_reason"] else "adjudicated_unresolved"} for row in rows]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    queue_target = queue_path or output.with_name("adjudication_queue.jsonl")
    queue_target.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in queue), encoding="utf-8")
    entrypoint = Path(__file__).resolve()
    manifest = {
        "schema_version": 3, "execution_id": execution_id, "method": "third_adjudication",
        "allowed_input_paths": [{"path": str(path.resolve()), "sha256": _sha(path.read_bytes())} for path in (pass_1, pass_2)],
        "entrypoint": str(entrypoint), "entrypoint_sha256": _sha(entrypoint.read_bytes()), "review_contract_sha256": _sha(CONTRACT),
        "reviewer_bundle_sha256": _sha(entrypoint.read_bytes() + CONTRACT), "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(), "adjudication_count": len(rows), "unresolved_count": sum(bool(row["disagreement_reason"]) for row in rows),
    }
    manifest_target = manifest_path or output.with_name("adjudication_execution_manifest.json")
    manifest_target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"rows": len(rows), "execution_id": execution_id, "unresolved_count": manifest["unresolved_count"], "manifest": str(manifest_target)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pass-1", type=Path, required=True)
    parser.add_argument("--pass-2", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execution-manifest", type=Path)
    parser.add_argument("--adjudication-queue", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.pass_1, args.pass_2, args.output, args.execution_manifest, args.adjudication_queue), sort_keys=True))


if __name__ == "__main__":
    main()
