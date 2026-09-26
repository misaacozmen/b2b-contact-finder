"""Field confidence annotations needed by the reference completion stage."""
from __future__ import annotations

from modules import scorer


CONFIDENT = frozenset({"HIGH", "MEDIUM"})
_FIELDS = ("website", "email", "phone")


def _website_confidence(row: dict) -> str:
    source = str(row.get("website_source", "") or "")
    status = str(row.get("status", "") or "")
    if source in {"OWN_SEARCH", "PAID_BRIGHTDATA"}:
        return {
            "OK_HIGH_CONFIDENCE": "HIGH",
            "OK_MEDIUM_CONFIDENCE": "MEDIUM",
        }.get(status, "NONE")
    return {
        "OWN_SEARCH+REFERENCE": "HIGH",
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
    if field == "email":
        if source == "SITE":
            if scorer.same_registrable_domain(value.rsplit("@", 1)[-1], row.get("website", "")):
                return website_confidence
            return "NONE"
        return {"SITE_FREEMAIL": "LOW", "REFERENCE_LISTING": "MEDIUM", "PAID_HUNTER": "MEDIUM"}.get(source, "NONE")
    if field == "phone":
        if source == "SITE":
            return "MEDIUM" if website_confidence == "LOW" else website_confidence
        return {"REFERENCE_LISTING": "MEDIUM", "PAID_GOOGLE_PLACES": "MEDIUM"}.get(source, "NONE")
    raise ValueError(f"unknown field: {field}")


def annotate(row: dict, metadata: dict | None) -> dict:
    del metadata  # Reserved for the full field merge/report stage.
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


def stage_snapshot(row: dict) -> dict:
    annotated = dict(row)
    annotate(annotated, None)
    keys = (
        "website", "email", "phone", "status", "website_confidence",
        "email_confidence", "phone_confidence",
    )
    return {key: annotated.get(key, "") for key in keys}
