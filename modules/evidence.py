"""Write field-level discovery evidence without leaking API credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path

from modules import evidence_ledger, redaction


def _json_safe(value):
    return redaction.sanitize(value)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for row in rows:
                source_evidence = row.get("source_evidence", [])
                if isinstance(source_evidence, str):
                    try:
                        source_evidence = json.loads(source_evidence)
                    except json.JSONDecodeError:
                        source_evidence = []
                allowed_fields = row.get("allowed_contact_fields", [])
                if isinstance(allowed_fields, str):
                    allowed_fields = [
                        value.strip() for value in allowed_fields.replace(",", ";").split(";")
                        if value.strip()
                    ]
                record = {
                    # Run identity is carried by the manifest; keeping it out
                    # of the portable evidence rows makes live/replay
                    # artifacts byte-identical while preserving row lineage.
                    "run_id": "",
                    "company": row.get("company", ""),
                    "source_record_id": row.get("source_record_id", ""),
                    "original_index": row.get("original_index", row.get("__index")),
                    "publication_decision": {
                        "publishable": bool(row.get("publication_eligible", False)),
                        "advisory_eligible": bool(row.get("publication_advisory_eligible", False)),
                        "website_identity_verified": bool(row.get("website_identity_verified", False)),
                        "allowed_contact_fields": allowed_fields,
                        "blockers": row.get("publication_blockers", ""),
                        "policy_version": row.get("publication_policy_version", ""),
                    },
                    "source_detail": {
                        "listed_legal_name": row.get("listed_legal_name", ""),
                        "source_detail_status": row.get("source_detail_status", ""),
                        "source_detail_url": row.get("source_detail_url", ""),
                        "content_sha256": row.get("source_detail_content_sha256", ""),
                        "evidence": source_evidence,
                    },
                    "source_evidence": source_evidence,
                    "selected": {
                        "website": row.get("website", ""),
                        "website_source": row.get("website_source", ""),
                        "email": row.get("email", ""),
                        "email_source_url": row.get("email_source_url", ""),
                        "phone": row.get("phone", ""),
                        "phone_source_url": row.get("phone_source_url", ""),
                        "status": row.get("status", ""),
                        "confidence": row.get("confidence", ""),
                        "score": row.get("score", 0),
                        "publication_policy": {
                            "version": row.get("publication_policy_version", ""),
                            "action": row.get("publication_policy_action", ""),
                            "eligible": row.get("publication_eligible", False),
                            "safety_score": row.get("publication_safety_score", 0),
                            "risk_index": row.get("publication_risk_index", 100),
                            "risk_tier": row.get("publication_risk_tier", ""),
                            "blockers": row.get("publication_blockers", ""),
                        },
                        "reason": row.get("reason", ""),
                    },
                    "candidates": row.get("__candidates", []),
                    "search_trace": row.get("__search_trace", []),
                    "source_health": row.get("__source_health", {}),
                    "evaluation": row.get("__evaluation", {}),
                    "candidate_evaluations": row.get("__candidate_evaluations", []),
                    "field_evidence": evidence_ledger.evaluation_claims(row.get("__evaluation", {})),
                }
                handle.write(json.dumps(_json_safe(record), ensure_ascii=False, sort_keys=True) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
