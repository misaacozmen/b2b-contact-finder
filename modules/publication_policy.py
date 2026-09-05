"""Conservative, evidence-based policy for the publication surface.

The legacy pipeline already contains many hard safety gates.  This module does
not replace or relax them: it may only allow a legacy publication to stand or
downgrade it to review.  The numeric safety score is an auditable ordering
signal for offline risk/coverage analysis, not a probability.
"""

from __future__ import annotations

import re
import hashlib
import json
from typing import Any

import config

from modules import identity, scorer


POLICY_VERSION = "evidence-risk-v1"
DECISION_SCHEMA_VERSION = 1
OK_STATUSES = {"OK_HIGH_CONFIDENCE", "OK_MEDIUM_CONFIDENCE"}
EXCLUDED_ROLES = {
    "directory", "fair_profile", "shared_listing", "marketplace", "news",
    "public_body",
}
LEGAL_NAME_REASON_PREFIXES = (
    "legal_name_phrase_match:",
    "legal_name_full_match:",
    "legal_name_ownership_match:",
)
CONTEXT_CONFLICT_OVERRIDE = "metadata_context_conflict_overridden_by_exact_compound_identity"
CONFLICT_TOKENS = {
    "sector_conflict",
    "context_conflict",
    "country_conflict",
    "country_mismatch",
    "foreign_country",
}


def _legacy_is_publishable_row(row: dict) -> bool:
    persisted_scheduler_row = all(
        key in row for key in ("free_state", "paid_state")
    )
    if (
        row.get("quarantine_state")
        or row.get("quarantine_status")
        or "legacy_recovery_provisional" in str(row.get("publication_blockers", ""))
        or str(row.get("source_record_id_quality", "")).casefold() == "legacy_recovery"
    ):
        return False
    if (
        not row.get("source_record_id")
        or not row.get("free_state")
        or not row.get("paid_state")
        or "paid_required" not in row
    ):
        # Legacy/unit-level policy calls may omit scheduler metadata; only
        # require it when the field is present in a persisted run payload.
        if persisted_scheduler_row:
            return False
    if str(row.get("free_state", "")).upper() in {"PENDING", "RUNNING", "UNKNOWN", "BLOCKED_BUDGET"}:
        return False
    if bool(row.get("paid_required")) and str(row.get("paid_state", "")).upper() != "DONE":
        return False
    if persisted_scheduler_row and str(row.get("paid_state", "")).upper() not in {"DONE", "NOT_REQUIRED"}:
        return False
    eligible = (
        row.get("status") in OK_STATUSES
        and row.get("publication_eligible") is True
    )
    if not eligible or not persisted_scheduler_row:
        return eligible
    # Older direct memory callers do not carry the persisted scoring/contact
    # fields.  The strict publication contract applies once those fields are
    # present in a scheduler payload.
    if not any(key in row for key in ("score", "email_publication_status", "phone_publication_status")):
        return eligible
    if int(row.get("score") or 0) < int(getattr(config, "MIN_ACCEPT_SCORE", 65)):
        return False
    evaluation = dict(row.get("__evaluation") or {})
    if row.get("identity_resolution") and "_identity_resolution" not in evaluation:
        evaluation["_identity_resolution"] = row.get("identity_resolution")
    assessment = row.get("identity_assessment") or evaluation.get("identity_assessment") or {}
    if not bool(assessment.get("publishable")) or assessment.get("conflicts"):
        return False
    reasons = " ".join(str(value) for value in (
        row.get("reason", ""), row.get("publication_blockers", ""),
        evaluation.get("reasons", []) if isinstance(evaluation, dict) else "",
    )).casefold()
    reason_tokens = _normalized_reason_tokens(row.get("reason", ""))
    blocker_tokens = _normalized_reason_tokens(row.get("publication_blockers", ""))
    evaluation_tokens = _normalized_reason_tokens(
        evaluation.get("reasons", []) if isinstance(evaluation, dict) else "",
    )
    conflict_tokens = (reason_tokens | blocker_tokens | evaluation_tokens) - {CONTEXT_CONFLICT_OVERRIDE}
    if any(marker in token for token in conflict_tokens for marker in CONFLICT_TOKENS):
        return False
    if "cross_domain_email_accepted_from_verified_official_page" in reasons and not evaluation.get("structured_domain_relation"):
        return False
    if len(scorer.legal_identity_tokens(str(row.get("company", "")))) <= 1:
        if not (
            scorer.normalize_domain(str(row.get("website", "")))
            and scorer.domain_identity_match(str(row.get("company", "")), str(row.get("website", "")))[0]
            and _has_reason(str(row.get("reason", "")).split(";"), LEGAL_NAME_REASON_PREFIXES)
            and "country_identity_tr_" in reasons
            and "context_match:" in reasons
        ):
            return False
    valid_email = bool(row.get("email")) and str(row.get("email_publication_status", "")).casefold() == "allowed"
    valid_phone = bool(row.get("phone")) and str(row.get("phone_publication_status", "")).casefold() == "allowed"
    return valid_email or valid_phone


def _reason_values(row: dict, evaluation: dict) -> set[str]:
    return (
        _normalized_reason_tokens(row.get("reason", ""))
        | _normalized_reason_tokens(row.get("publication_blockers", ""))
        | _normalized_reason_tokens(evaluation.get("reasons", []))
    )


def _context_resolution(row: dict, evaluation: dict) -> dict:
    value = row.get("context_resolution")
    if not isinstance(value, dict):
        value = evaluation.get("context_resolution")
    return value if isinstance(value, dict) else {}


def _context_resolution_is_verified(context: dict) -> bool:
    """Require ownership, activity compatibility, and one independent match."""
    ownership = any(bool(context.get(key)) for key in (
        "legal_ownership_verified", "brand_ownership_verified",
        "target_legal_name_verified", "brand_owner_verified",
        "legal_name_verified", "ownership_verified",
    ))
    compatibility = any(bool(context.get(key)) for key in (
        "candidate_sector_compatible", "candidate_product_compatible",
        "site_sector_product_compatible", "sector_compatible",
    ))
    matches = context.get("independent_matches") or context.get("independent_identity_matches") or []
    if isinstance(matches, dict):
        matches = [matches]
    match_kinds = {"phone", "full_address", "address", "company_number", "company_registration_number"}
    independent_match = False
    for match in matches:
        if not isinstance(match, dict):
            continue
        kind = str(match.get("kind") or match.get("type") or "").casefold().replace(" ", "_")
        url = str(match.get("url") or match.get("source_url") or "").strip()
        content_hash = str(match.get("content_sha256") or match.get("content_hash") or "").casefold()
        source_record_id = str(match.get("source_record_id") or "").strip()
        if kind in match_kinds and url and re.fullmatch(r"[0-9a-f]{64}", content_hash) and source_record_id:
            independent_match = True
            break
    return ownership and compatibility and independent_match and not context.get("conflicts")


def _website_identity_verified(row: dict, evaluation: dict) -> bool:
    assessment = row.get("identity_assessment") or evaluation.get("identity_assessment") or {}
    candidate = evaluation.get("candidate") or {}
    if assessment.get("conflicts") or assessment.get("publishable") is False:
        return False
    website = str(row.get("website") or candidate.get("url") or "").strip()
    if not website:
        return False
    if _context_resolution_is_verified(_context_resolution(row, evaluation)):
        return True
    reasons = _reason_values(row, evaluation)
    identity_evidence = any(token.startswith((
        "page_identity_strong:", "page_identity_medium:", "structured_identity_",
        "legal_name_", "country_identity_tr_", "context_match:",
    )) for token in reasons)
    resolution = str(row.get("identity_resolution") or evaluation.get("identity_resolution") or evaluation.get("_identity_resolution") or "")
    return bool(assessment.get("publishable") is True and (identity_evidence or resolution or candidate))


def decide_row(row: dict, evaluation: dict | None = None) -> dict:
    """Return the one explainable final publication decision for a row."""
    evaluation = evaluation if isinstance(evaluation, dict) else (
        row.get("__evaluation") if isinstance(row.get("__evaluation"), dict) else {}
    )
    persisted = all(key in row for key in ("source_record_id", "free_state", "paid_state", "paid_required"))
    strict_persisted = persisted and any(key in row for key in ("score", "email_publication_status", "phone_publication_status"))
    advisory = bool(row.get("publication_advisory_eligible", row.get("advisory_eligible", row.get("publication_eligible") is True)))
    allowed_contact_fields = []
    if row.get("email") and str(row.get("email_publication_status", "")).casefold() == "allowed":
        allowed_contact_fields.append("email")
    if row.get("phone") and str(row.get("phone_publication_status", "")).casefold() == "allowed":
        allowed_contact_fields.append("phone")
    blockers: list[str] = []
    if row.get("quarantine_state") or row.get("quarantine_status") or "legacy_recovery_provisional" in str(row.get("publication_blockers", "")):
        blockers.append("quarantined")
    if strict_persisted:
        if not row.get("source_record_id"):
            blockers.append("source_record_id_missing")
        if str(row.get("free_state", "")).upper() in {"PENDING", "RUNNING", "UNKNOWN", "BLOCKED_BUDGET"}:
            blockers.append("free_scheduler_not_terminal")
        if bool(row.get("paid_required")) and str(row.get("paid_state", "")).upper() != "DONE":
            blockers.append("paid_scheduler_not_terminal")
        if str(row.get("paid_state", "")).upper() not in {"DONE", "NOT_REQUIRED"}:
            blockers.append("paid_scheduler_not_terminal")
        if str(row.get("status", "")) not in OK_STATUSES:
            blockers.append("status_not_publishable")
        if int(row.get("score") or 0) < int(getattr(config, "MIN_ACCEPT_SCORE", 65)):
            blockers.append("score_below_minimum")
        if not _website_identity_verified(row, evaluation):
            blockers.append("website_identity_unverified")
        assessment = row.get("identity_assessment") or evaluation.get("identity_assessment") or {}
        if assessment.get("conflicts"):
            blockers.append("identity_conflict")
        reasons = _reason_values(row, evaluation)
        context = _context_resolution(row, evaluation)
        override_present = any(token == CONTEXT_CONFLICT_OVERRIDE for token in reasons)
        real_conflicts = {
            token for token in reasons
            if any(marker in token for marker in CONFLICT_TOKENS)
            and token != CONTEXT_CONFLICT_OVERRIDE
        }
        if real_conflicts:
            blockers.append("context_conflict")
        if override_present and not _context_resolution_is_verified(context):
            blockers.append("context_resolution_unverified")
        if "cross_domain_email_accepted_from_verified_official_page" in reasons and not evaluation.get("structured_domain_relation"):
            blockers.append("cross_domain_email_unresolved")
        if not allowed_contact_fields:
            blockers.append("no_allowed_contact_field")
        if not _legacy_is_publishable_row(row):
            blockers.append("publication_gate_failed")
        publishable = not blockers
    else:
        publishable = _legacy_is_publishable_row(row)
        if not publishable and advisory:
            blockers.append("publication_gate_failed")
    if not publishable and not blockers:
        blockers.append("publication_gate_failed")
    return {
        "publishable": bool(publishable),
        "blockers": list(dict.fromkeys(blockers)),
        "website_identity_verified": _website_identity_verified(row, evaluation),
        "allowed_contact_fields": allowed_contact_fields,
        "policy_version": POLICY_VERSION,
        "advisory_eligible": advisory,
    }


_DECISION_INPUT_FIELDS = (
    "source_record_id", "run_id", "free_state", "paid_state", "paid_required",
    "status", "score", "publication_eligible", "publication_advisory_eligible",
    "email_publication_status", "phone_publication_status", "website",
    "identity_resolution", "reason", "publication_blockers", "config_sha256",
)


def _hash_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _safe_decision_input(
    row: dict,
    evaluation: dict,
    evaluation_sha256: str,
    config_sha256: str,
) -> dict:
    """Build the auditable decision input without HTML, secrets, or payload blobs."""
    assessment = row.get("identity_assessment") or evaluation.get("identity_assessment") or {}
    context = _context_resolution(row, evaluation)
    candidate = evaluation.get("candidate") if isinstance(evaluation.get("candidate"), dict) else {}
    candidate_safe = {
        key: candidate.get(key, "")
        for key in ("url", "domain", "role", "query", "rank", "score", "reason", "_identity_company")
        if key in candidate
    }
    assessment_safe = {
        key: assessment.get(key)
        for key in (
            "publishable", "conflicts", "support_count", "first_party_bundle_components",
            "strong_first_party_bundle", "decision", "support_keys",
        )
        if key in assessment
    }
    context_safe = {
        key: context.get(key)
        for key in (
            "legal_ownership_verified", "brand_ownership_verified", "target_legal_name_verified",
            "brand_owner_verified", "legal_name_verified", "ownership_verified",
            "candidate_sector_compatible", "candidate_product_compatible", "site_sector_product_compatible",
            "sector_compatible", "conflicts", "independent_matches", "independent_identity_matches",
        )
        if key in context
    }
    payload = {key: row.get(key) for key in _DECISION_INPUT_FIELDS if key in row}
    payload["config_sha256"] = config_sha256
    payload.update({
        "reason_tokens": sorted(_reason_values(row, evaluation)),
        "evaluation_sha256": evaluation_sha256,
        "identity_assessment": assessment_safe,
        "context_resolution": context_safe,
        "candidate": candidate_safe,
        "allowed_contact_fields": [
            field for field in ("email", "phone")
            if row.get(field) and str(row.get(f"{field}_publication_status", "")).casefold() == "allowed"
        ],
    })
    return payload


def _evaluation_sha256(evaluation: dict) -> str:
    # Import lazily to keep publication_policy independent of the redaction
    # module's import order. The hash is over sanitized structured evidence;
    # raw HTML is never included in the decision input.
    from modules import redaction
    return _hash_json(redaction.sanitize(evaluation))


def _default_config_sha256(row: dict) -> str:
    value = str(row.get("config_sha256") or row.get("run_config_sha256") or "").strip()
    if value:
        return value
    try:
        import importlib
        run_context = importlib.import_module("modules.run_context")
        return run_context.RunConfig.from_config(
            paid_enabled=bool(row.get("paid_enabled", getattr(config, "PAID_ENABLED", False)))
        ).sha256
    except Exception:
        return ""


def freeze_publication_decision(
    row: dict,
    evaluation: dict | None = None,
    *,
    config_sha256: str | None = None,
) -> dict:
    """Compute and persist exactly one immutable final decision envelope."""
    existing = row.get("publication_decision")
    if isinstance(existing, dict):
        expected_config = str(config_sha256 or row.get("config_sha256") or row.get("run_config_sha256") or "")
        verify_publication_decision(
            existing,
            source_record_id=str(row.get("source_record_id", "") or "") or None,
            run_id=str(row.get("run_id", "") or "") or None,
            config_sha256=expected_config or None,
        )
        return existing
    source_record_id = str(row.get("source_record_id", "") or "").strip()
    if not source_record_id:
        raise RuntimeError("cannot freeze publication decision without source_record_id")
    run_id = str(row.get("run_id", "") or "")
    resolved_config_sha256 = str(config_sha256 or _default_config_sha256(row) or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", resolved_config_sha256):
        raise RuntimeError("cannot freeze publication decision without a valid config_sha256")
    evaluation = evaluation if isinstance(evaluation, dict) else (
        row.get("__evaluation") if isinstance(row.get("__evaluation"), dict) else {}
    )
    evaluation_hash = _evaluation_sha256(evaluation)
    decision = decide_row(row, evaluation)
    if not decision["publishable"]:
        existing_blockers = [
            value.strip()
            for value in str(row.get("publication_blockers", "") or "").replace(",", ";").split(";")
            if value.strip()
        ]
        decision["blockers"] = list(dict.fromkeys([*existing_blockers, *decision["blockers"]]))
    decision_input = _safe_decision_input(row, evaluation, evaluation_hash, resolved_config_sha256)
    envelope = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "policy_version": decision["policy_version"],
        "source_record_id": source_record_id,
        "run_id": run_id,
        "publishable": bool(decision["publishable"]),
        "blockers": list(dict.fromkeys(str(value) for value in decision.get("blockers", []) if str(value))),
        "advisory_eligible": bool(decision.get("advisory_eligible", False)),
        "website_identity_verified": bool(decision.get("website_identity_verified", False)),
        "allowed_contact_fields": list(decision.get("allowed_contact_fields", [])),
        "decision_input_sha256": _hash_json(decision_input),
        "evaluation_sha256": evaluation_hash,
        "config_sha256": resolved_config_sha256,
        "action": str((evaluation.get("publication_policy") or {}).get("action", "")),
        "decision_input": decision_input,
    }
    envelope["publication_decision_sha256"] = _hash_json(envelope)
    row["publication_decision"] = envelope
    row["publication_decision_sha256"] = envelope["publication_decision_sha256"]
    return envelope


def verify_publication_decision(
    envelope: dict,
    *,
    source_record_id: str | None = None,
    run_id: str | None = None,
    config_sha256: str | None = None,
) -> dict:
    """Verify an immutable decision and, when supplied, bind it to its row."""
    required = {
        "schema_version", "policy_version", "source_record_id", "run_id", "publishable",
        "blockers", "advisory_eligible", "website_identity_verified", "allowed_contact_fields",
        "decision_input_sha256", "evaluation_sha256", "config_sha256", "decision_input",
        "publication_decision_sha256",
    }
    if not isinstance(envelope, dict) or not required.issubset(envelope):
        raise RuntimeError("publication_decision envelope is incomplete")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != DECISION_SCHEMA_VERSION:
        raise RuntimeError("publication_decision schema_version is invalid")
    if not isinstance(envelope["policy_version"], str) or not envelope["policy_version"].strip():
        raise RuntimeError("publication_decision policy_version is invalid")
    if not isinstance(envelope["source_record_id"], str) or not envelope["source_record_id"].strip():
        raise RuntimeError("publication_decision source_record_id is invalid")
    if not isinstance(envelope["run_id"], str):
        raise RuntimeError("publication_decision run_id is invalid")
    for field in ("publishable", "advisory_eligible", "website_identity_verified"):
        if type(envelope[field]) is not bool:
            raise RuntimeError(f"publication_decision {field} is invalid")
    if not isinstance(envelope["blockers"], list) or any(
        not isinstance(value, str) or not value.strip() for value in envelope["blockers"]
    ) or len(envelope["blockers"]) != len(set(envelope["blockers"])):
        raise RuntimeError("publication_decision blockers are invalid")
    if not isinstance(envelope["allowed_contact_fields"], list) or any(
        value not in {"email", "phone"} for value in envelope["allowed_contact_fields"]
    ) or len(envelope["allowed_contact_fields"]) != len(set(envelope["allowed_contact_fields"])):
        raise RuntimeError("publication_decision allowed_contact_fields are invalid")
    for field in ("decision_input_sha256", "evaluation_sha256", "config_sha256", "publication_decision_sha256"):
        if not isinstance(envelope[field], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", envelope[field]):
            raise RuntimeError(f"publication_decision {field} is invalid")
    if not isinstance(envelope["decision_input"], dict):
        raise RuntimeError("publication_decision decision_input is invalid")
    if envelope["decision_input"].get("source_record_id") != envelope["source_record_id"]:
        raise RuntimeError("publication_decision source_record_id binding mismatch")
    if envelope["decision_input"].get("run_id", "") != envelope["run_id"]:
        raise RuntimeError("publication_decision run_id binding mismatch")
    if envelope["decision_input"].get("config_sha256") != envelope["config_sha256"]:
        raise RuntimeError("publication_decision config_sha256 binding mismatch")
    input_hash = _hash_json(envelope["decision_input"])
    if input_hash != str(envelope["decision_input_sha256"]):
        raise RuntimeError("publication_decision decision_input hash mismatch")
    unsigned = {key: value for key, value in envelope.items() if key != "publication_decision_sha256"}
    if _hash_json(unsigned) != str(envelope["publication_decision_sha256"]):
        raise RuntimeError("publication_decision envelope hash mismatch")
    for field, expected in (
        ("source_record_id", source_record_id),
        ("run_id", run_id),
        ("config_sha256", config_sha256),
    ):
        if expected is not None and str(envelope[field]) != str(expected):
            raise RuntimeError(f"publication_decision {field} does not match row")
    return envelope


def apply_frozen_decision_fields(row: dict, envelope: dict) -> dict:
    """Project an already-verified envelope onto flat output fields."""
    verify_publication_decision(
        envelope,
        source_record_id=str(row.get("source_record_id", "") or "") or None,
        run_id=str(row.get("run_id", "") or "") or None,
    )
    if "publication_eligible" in row and type(row["publication_eligible"]) is bool and row["publication_eligible"] != envelope["publishable"]:
        raise RuntimeError("flat publication_eligible conflicts with frozen decision")
    if "allowed_contact_fields" in row:
        current = row["allowed_contact_fields"]
        if isinstance(current, str):
            current = [value.strip() for value in current.replace(",", ";").split(";") if value.strip()]
        if current not in (None, "", []) and set(current) != set(envelope["allowed_contact_fields"]):
            raise RuntimeError("flat allowed_contact_fields conflicts with frozen decision")
    for field in ("publication_decision_sha256", "decision_input_sha256", "evaluation_sha256", "config_sha256"):
        if field in row and row[field] not in (None, "") and str(row[field]) != str(envelope[field]):
            raise RuntimeError(f"flat {field} conflicts with frozen decision")
    if "publication_blockers" in row:
        current_blockers = {
            value.strip() for value in str(row.get("publication_blockers") or "").replace(",", ";").split(";") if value.strip()
        }
        if current_blockers and current_blockers != set(envelope["blockers"]):
            raise RuntimeError("flat publication_blockers conflicts with frozen decision")
    row["publication_decision"] = envelope
    row["publication_decision_sha256"] = envelope["publication_decision_sha256"]
    row["publication_eligible"] = bool(envelope["publishable"])
    row["publication_policy_version"] = envelope["policy_version"]
    row["publication_advisory_eligible"] = bool(envelope["advisory_eligible"])
    row["advisory_eligible"] = bool(envelope["advisory_eligible"])
    row["website_identity_verified"] = bool(envelope["website_identity_verified"])
    row["allowed_contact_fields"] = "; ".join(envelope["allowed_contact_fields"])
    row["decision_input_sha256"] = envelope["decision_input_sha256"]
    row["evaluation_sha256"] = envelope["evaluation_sha256"]
    row["config_sha256"] = envelope["config_sha256"]
    if not envelope["publishable"]:
        row["publication_blockers"] = "; ".join(dict.fromkeys(
            value for value in [str(row.get("publication_blockers", "")), *envelope["blockers"]] if value
        ))
    return row


def with_blocker(envelope: dict, blocker: str) -> dict:
    """Derive a new deterministic review envelope without re-running policy."""
    verify_publication_decision(envelope)
    derived = dict(envelope)
    derived["blockers"] = list(dict.fromkeys([*envelope["blockers"], str(blocker)]))
    derived["publishable"] = False
    derived["decision_input"] = dict(envelope["decision_input"], reconciliation_blocker=str(blocker))
    derived["decision_input_sha256"] = _hash_json(derived["decision_input"])
    derived.pop("publication_decision_sha256", None)
    derived["publication_decision_sha256"] = _hash_json(derived)
    return derived


def rebind_publication_decision(
    envelope: dict,
    *,
    run_id: str,
    config_sha256: str,
) -> dict:
    """Rebind an already-frozen decision to a continuation run without policy recomputation."""
    verify_publication_decision(envelope)
    if not isinstance(run_id, str):
        raise RuntimeError("publication_decision continuation run_id is invalid")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(config_sha256)):
        raise RuntimeError("publication_decision continuation config_sha256 is invalid")
    derived = dict(envelope)
    derived["run_id"] = run_id
    derived["config_sha256"] = str(config_sha256)
    derived["decision_input"] = dict(
        envelope["decision_input"], run_id=run_id, config_sha256=str(config_sha256)
    )
    derived["decision_input_sha256"] = _hash_json(derived["decision_input"])
    derived.pop("publication_decision_sha256", None)
    derived["publication_decision_sha256"] = _hash_json(derived)
    verify_publication_decision(derived, run_id=run_id, config_sha256=str(config_sha256))
    return derived


def frozen_publishable(row: dict, *, require: bool = False) -> bool:
    envelope = row.get("publication_decision")
    if isinstance(envelope, dict):
        verify_publication_decision(envelope)
        return bool(envelope["publishable"])
    if require:
        raise RuntimeError(f"frozen publication_decision missing for {row.get('source_record_id', '')}")
    return is_publishable_row(row)


def is_publishable_row(row: dict) -> bool:
    """Boolean compatibility wrapper around :func:`decide_row`."""
    if isinstance(row.get("publication_decision"), dict):
        return frozen_publishable(row, require=True)
    decision = decide_row(row)
    if all(key in row for key in ("source_record_id", "free_state", "paid_state", "paid_required")):
        row["publication_eligible"] = decision["publishable"]
        if not decision["publishable"]:
            row["publication_blockers"] = "; ".join(dict.fromkeys(
                value for value in [str(row.get("publication_blockers", "")), *decision["blockers"]] if value
            ))
    return bool(decision["publishable"])


def _has_reason(reasons: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(str(reason).strip().startswith(prefixes) for reason in reasons)


def _normalized_reason_tokens(value: Any) -> set[str]:
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    tokens: set[str] = set()
    for value_item in values:
        tokens.update(
            token.strip().casefold()
            for token in str(value_item).split(";")
            if token.strip()
        )
    return tokens


def _bounded_score(value: int) -> int:
    return max(0, min(100, int(value)))


def evaluate(
    company: str,
    evaluation: dict,
    proposed_status: str,
    *,
    minimum_safety_score: int,
) -> dict:
    """Return a downgrade-only publication decision.

    ``safety_score`` deliberately remains distinct from a calibrated
    probability.  Only a disjoint labelled set may turn this ordering into a
    deployable threshold.
    """
    minimum_safety_score = _bounded_score(minimum_safety_score)
    candidate = evaluation.get("candidate", {})
    reasons = list(evaluation.get("reasons", []))
    assessment = evaluation.get("identity_assessment") or identity.assess(
        company,
        candidate,
        reasons,
        evaluation.get("structured_identity", {}),
    )
    conflicts = assessment.get("conflicts", [])
    blockers: list[str] = []

    role = str(candidate.get("role", ""))
    if role in EXCLUDED_ROLES:
        blockers.append(f"excluded_candidate_role:{role}")
    exact_domain_resolution = str(
        evaluation.get("_identity_resolution", "") or ""
    ).endswith("_exact_full_name_domain")
    fingerprint_resolution = str(
        evaluation.get("_identity_resolution", "") or ""
    ).startswith("candidate_resolved_by_")
    legal_or_ownership_evidence = _has_reason(reasons, ("legal_name_phrase_match:", "legal_name_full_match:", "legal_name_ownership_match:")) or bool(evaluation.get("structured_domain_relation"))
    if len(scorer.legal_identity_tokens(company)) <= 1 and not (
        scorer.normalize_domain(candidate.get("url", ""))
        and scorer.domain_identity_match(company, candidate.get("url", ""))[0]
        and legal_or_ownership_evidence
        and _has_reason(reasons, ("country_identity_tr_",))
        and _has_reason(reasons, ("context_match:", "page_identity_strong:"))
    ):
        blockers.append("generic_single_token_identity_not_verified")
    if not assessment.get("publishable"):
        # A fingerprint/fast-path resolution is useful evidence, but never a
        # publication authorization by itself.
        blockers.append("identity_resolution_not_publishable" if fingerprint_resolution else "identity_not_publishable")
    blockers.extend(
        f"identity_conflict:{item.get('kind', 'unknown')}"
        for item in conflicts
    )
    if not evaluation.get("has_contact"):
        blockers.append("no_first_party_contact")
    cross_domain_email_resolved = (
        "cross_domain_email_accepted_from_verified_official_page" in reasons
        and bool(evaluation.get("structured_domain_relation"))
    )
    if evaluation.get("email_failed") and not cross_domain_email_resolved:
        blockers.append("email_gate_failed")
    if _has_reason(reasons, (
        "foreign_country_redirect_rejected",
        "unsafe_context_identity",
        "context_gate_failed",
        "unsupported_search_text_candidate_rejected",
    )):
        blockers.append("identity_or_context_safety_gate")
    if "tls_insecure_transport" in reasons:
        blockers.append("tls_certificate_unverified")

    if CONTEXT_CONFLICT_OVERRIDE in _normalized_reason_tokens(reasons) and not _context_resolution_is_verified(
        _context_resolution({}, evaluation)
    ):
        blockers.append("context_resolution_unverified")

    support_count = int(assessment.get("support_count", 0) or 0)
    bundle_components = int(assessment.get("first_party_bundle_components", 0) or 0)
    score = 25
    score += min(support_count, 3) * 18
    score += min(bundle_components, 4) * 8
    if assessment.get("strong_first_party_bundle"):
        score += 18
    if assessment.get("publishable"):
        score += 10
    if _has_reason(reasons, ("country_identity_tr_",)):
        score += 7
    if evaluation.get("has_contact"):
        score += 5
    if evaluation.get("email") and evaluation.get("email_verification") == "verified":
        score += 3
    if evaluation.get("phone"):
        score += 2
    if scorer.domain_identity_match(company, candidate.get("url", ""))[0]:
        score += 5
    score -= min(len(conflicts), 2) * 35
    if evaluation.get("email_failed") and not cross_domain_email_resolved:
        score -= 20
    if role in EXCLUDED_ROLES:
        score -= 40
    safety_score = _bounded_score(score)

    legacy_publishable = proposed_status in OK_STATUSES
    risk_eligible = not blockers and safety_score >= minimum_safety_score
    # ``eligible`` is the actual publication decision, not an advisory risk
    # score. A legacy review row remains withheld until its identity status is
    # resolved, even when its standalone safety score is high.
    eligible = legacy_publishable and risk_eligible
    if not legacy_publishable:
        action = "retain_legacy_abstention"
    elif risk_eligible:
        action = "allow_legacy_publication"
    else:
        action = "downgrade_to_review"

    if blockers:
        risk_tier = "blocked"
    elif safety_score >= 90:
        risk_tier = "low"
    elif safety_score >= minimum_safety_score:
        risk_tier = "controlled"
    else:
        risk_tier = "elevated"

    return {
        "policy_version": POLICY_VERSION,
        "mode": "downgrade_only",
        "proposed_status": proposed_status,
        "action": action,
        "eligible": eligible,
        "publishable": bool(eligible),
        "blockers": list(dict.fromkeys(blockers)),
        "website_identity_verified": _website_identity_verified({"website": candidate.get("url", ""), "identity_assessment": assessment}, evaluation),
        "allowed_contact_fields": [
            field for field in ("email", "phone")
            if evaluation.get(field) and str(evaluation.get(f"{field}_publication_status", "")).casefold() == "allowed"
        ],
        "advisory_eligible": eligible,
        "risk_eligible": risk_eligible,
        "safety_score": safety_score,
        "risk_index": 100 - safety_score,
        "risk_tier": risk_tier,
        "minimum_safety_score": minimum_safety_score,
        "hard_blockers": list(dict.fromkeys([
            *blockers,
        ])),
        "evidence_summary": {
            "identity_decision": assessment.get("decision", ""),
            "independent_support_count": support_count,
            "first_party_bundle_components": bundle_components,
            "strong_first_party_bundle": bool(assessment.get("strong_first_party_bundle")),
            "exact_full_name_domain_resolution": exact_domain_resolution,
            "has_contact": bool(evaluation.get("has_contact")),
            "risk_eligible": risk_eligible,
        },
    }


def enforce(decision: dict, status: str, confidence: str, reasons: list[str]) -> tuple[str, str]:
    """Apply only a downgrade; never promote a legacy review/abstention."""
    if status not in OK_STATUSES or decision.get("action") != "downgrade_to_review":
        return status, confidence
    reason = (
        f"publication_policy_downgrade:{decision.get('policy_version')}:"
        f"safety={decision.get('safety_score', 0)}"
    )
    blockers = decision.get("hard_blockers", [])
    if blockers:
        reason += f":blockers={','.join(blockers)}"
    if reason not in reasons:
        reasons.append(reason)
    return "REVIEW_NEEDED", "review"
