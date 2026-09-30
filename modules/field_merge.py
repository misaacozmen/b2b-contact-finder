"""One confidence model for field publication, reporting, and paid filling."""
from __future__ import annotations

from modules import scorer


CONFIDENT = frozenset({"HIGH", "MEDIUM"})
_FIELDS = ("website", "email", "phone")
_CONFIDENCE_ORDER = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
_FIELD_COMPANIONS = {
    "website": (
        "website_source", "website_discovery_query", "website_confidence", "website_source_url",
        "selected_website", "status",
    ),
    "email": (
        "email_source_tier", "email_confidence", "email_source",
        "email_source_url", "email_selection_reason", "email_retrieval_method",
        "email_verification", "email_verification_reason", "alternative_emails",
        "alternative_email_records",
    ),
    "phone": (
        "phone_source_tier", "phone_confidence", "phone_source",
        "phone_source_url", "phone_label", "phone_selection_reason",
        "phone_retrieval_method", "phone_publication_status",
        "phone_publication_reason", "alternative_phones",
    ),
}


def _website_confidence(row: dict) -> str:
    source = str(row.get("website_source", "") or "")
    is_known_source = (
        source in {"OWN_SEARCH", "OWN_SEARCH+REFERENCE", "OWN_SEARCH_CALIBRATED", "PAID_BRIGHTDATA"}
        or source.startswith("REFERENCE_")
    )
    if row.get("website") and not is_known_source:
        source = "OWN_SEARCH"
    status = str(row.get("status", "") or "")
    if source in {"OWN_SEARCH", "PAID_BRIGHTDATA"}:
        return {
            "OK_HIGH_CONFIDENCE": "HIGH",
            "OK_MEDIUM_CONFIDENCE": "MEDIUM",
        }.get(status, "NONE")
    return {
        "OWN_SEARCH+REFERENCE": "HIGH",
        "OWN_SEARCH_CALIBRATED": "MEDIUM",
        "REFERENCE_VERIFIED": "HIGH",
        "REFERENCE_ACCEPTED": "MEDIUM",
        "REFERENCE_THIN": "LOW",
        "REFERENCE_UNREACHABLE": "LOW",
    }.get(source, "NONE")


def field_confidence(row: dict, field: str) -> str:
    value = str(row.get(field, "") or "").strip()
    if not value:
        return "NONE"
    if field == "website":
        return _website_confidence(row)
    website_confidence = _website_confidence(row)
    source_key = f"{field}_source_tier"
    source = str(row.get(source_key, "") or "")
    if not source:
        source = "SITE"
    if field == "email":
        if source == "SITE":
            if scorer.same_registrable_domain(value.rsplit("@", 1)[-1], row.get("website", "")):
                return website_confidence
            return "NONE"
        return {
            "SITE_FREEMAIL": "LOW", "REFERENCE_LISTING": "MEDIUM",
            "PAID_HUNTER": "MEDIUM",
        }.get(source, "NONE")
    if field == "phone":
        if source == "SITE":
            return "MEDIUM" if website_confidence == "LOW" else website_confidence
        return {
            "REFERENCE_LISTING": "MEDIUM",
            "PAID_GOOGLE_PLACES": "MEDIUM",
        }.get(source, "NONE")
    raise ValueError(f"unknown field: {field}")


def annotate(row: dict, metadata: dict | None) -> dict:
    del metadata  # Reference contacts are assigned by reference_resolution.
    if row.get("website") and not row.get("website_source"):
        row["website_source"] = "OWN_SEARCH"
    if row.get("email") and not row.get("email_source_tier"):
        row["email_source_tier"] = "SITE"
    if row.get("phone") and not row.get("phone_source_tier"):
        row["phone_source_tier"] = "SITE"
    for field in _FIELDS:
        row[f"{field}_confidence"] = field_confidence(row, field)
    gaps = [field for field in _FIELDS if row[f"{field}_confidence"] not in CONFIDENT]
    row["field_gaps"] = ";".join(gaps)
    row["ready_for_publication"] = (
        row["website_confidence"] in CONFIDENT
        and any(row[f"{field}_confidence"] in CONFIDENT for field in ("email", "phone"))
    )
    return row


def field_gaps(row: dict) -> set[str]:
    """Return fields whose confidence is below MEDIUM."""
    return {field for field in _FIELDS if field_confidence(row, field) not in CONFIDENT}


def ready_for_publication(row: dict) -> bool:
    """Require a confident website and at least one confident contact."""
    return (
        field_confidence(row, "website") in CONFIDENT
        and any(field_confidence(row, field) in CONFIDENT for field in ("email", "phone"))
    )


def stage_snapshot(row: dict) -> dict:
    annotated = dict(row)
    annotate(annotated, None)
    keys = (
        "website", "email", "phone", "status", "website_confidence",
        "email_confidence", "phone_confidence",
    )
    return {key: annotated.get(key, "") for key in keys}


def merge(previous: dict, current: dict) -> dict:
    """Merge fields by confidence; empty values and ties never displace prior data."""
    previous_row = dict(previous or {})
    current_row = dict(current or {})
    annotate(previous_row, None)
    annotate(current_row, None)
    result = {**previous_row, **current_row}
    for field in _FIELDS:
        previous_value = previous_row.get(field)
        current_value = current_row.get(field)
        if not current_value:
            winner = previous_row
        elif not previous_value:
            winner = current_row
        elif (
            _CONFIDENCE_ORDER[field_confidence(current_row, field)]
            > _CONFIDENCE_ORDER[field_confidence(previous_row, field)]
        ):
            winner = current_row
        else:
            winner = previous_row
        if field in winner:
            result[field] = winner[field]
        else:
            result.pop(field, None)
        for key in _FIELD_COMPANIONS[field]:
            if key in winner:
                result[key] = winner[key]
            else:
                result.pop(key, None)
    annotate(result, None)
    return result
