"""Field-level provenance records for first-party company evidence."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from modules import scorer


def page_scope(source_url: str) -> str:
    value = scorer.normalize_text(urlparse(source_url or "").path)
    scopes = (
        ("legal", ("kvkk", "legal", "ticari bilgi", "sirket bilgi", "imprint")),
        ("privacy", ("privacy", "gizlilik", "veri koruma", "data protection")),
        ("contact", ("contact", "iletisim", "bize ulas")),
        ("locations", ("location", "lokasyon", "sube", "branch", "factory", "fabrika")),
        ("about", ("about", "hakkimizda", "kurumsal", "corporate")),
        ("document", ("pdf", "catalog", "catalogue", "katalog", "brochure", "brosur")),
    )
    for scope, markers in scopes:
        if any(marker in value for marker in markers):
            return scope
    return "homepage" if not value.strip(" /") else "other"


def _normalized_value(field: str, value: object) -> str:
    text = str(value or "").strip()
    if field in {"telephone", "phone"}:
        return re.sub(r"\D", "", text)
    if field in {"email", "kep"}:
        return text.casefold()
    return scorer.normalize_text(text)


def build_claim(
    field: str,
    value: object,
    source_url: str,
    method: str,
    *,
    html_text: str = "",
    source_kind: str = "first_party",
    relation: str = "",
    retrieval_method: str = "http",
) -> dict:
    """Create a normalized, auditable claim without granting independence."""
    domain = scorer.normalize_domain(urlparse(source_url or "").netloc or source_url)
    claim = {
        "field": field,
        "value": str(value or "").strip(),
        "normalized_value": _normalized_value(field, value),
        "source_url": source_url,
        "source_domain": domain,
        "source_kind": source_kind,
        "page_scope": page_scope(source_url),
        "method": method,
        "retrieval_method": retrieval_method or "unknown",
        # Every page on the same official domain deliberately shares a key.
        "independence_key": f"first_party_domain:{domain}" if source_kind == "first_party" else source_kind,
    }
    if relation:
        claim["relation"] = relation
    if html_text:
        claim["source_content_sha256"] = hashlib.sha256(
            html_text.encode("utf-8", errors="ignore")
        ).hexdigest()
    return claim


def deduplicate(claims: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for claim in claims:
        marker = (
            str(claim.get("field", "")),
            str(claim.get("normalized_value", "")),
            str(claim.get("source_url", "")),
            str(claim.get("method", "")),
            str(claim.get("relation", "")),
        )
        if marker in seen or not marker[0] or not marker[1]:
            continue
        seen.add(marker)
        result.append(claim)
    return result


def evaluation_claims(evaluation: dict) -> list[dict]:
    """Return identity and selected-contact claims for the evidence artifact."""
    claims = list(evaluation.get("structured_identity", {}).get("claims", []))
    for field in ("email", "phone"):
        value = evaluation.get(field, "")
        source_url = evaluation.get(f"{field}_source_url", "")
        if value and source_url:
            claim = build_claim(
                field, value, source_url, "contact_extraction",
                retrieval_method=evaluation.get(f"{field}_retrieval_method", "unknown"),
            )
            claim["selected"] = True
            selection_reason = evaluation.get(f"{field}_selection_reason", "")
            if selection_reason:
                claim["selection_reason"] = selection_reason
            if field == "phone" and evaluation.get("phone_label"):
                claim["contact_role"] = evaluation["phone_label"]
            if field == "email":
                claim["dns_status"] = evaluation.get("email_verification", "not_checked")
                claim["dns_reason"] = evaluation.get("email_verification_reason", "")
            claim["publication_status"] = evaluation.get(
                f"{field}_publication_status", "suppressed",
            )
            claim["publication_reason"] = evaluation.get(
                f"{field}_publication_reason", "",
            )
            claim["publication_policy_version"] = evaluation.get(
                "contact_publication", {},
            ).get("policy_version", "")
            claims.append(claim)
    for field, records_key in (
        ("email", "alternative_email_records"),
        ("phone", "alternative_phones"),
    ):
        for record in evaluation.get(records_key, []):
            value = record.get("value", "")
            source_url = record.get("source_url", "")
            if not value or not source_url:
                continue
            claim = build_claim(
                field, value, source_url, "contact_extraction",
                retrieval_method=record.get("retrieval_method", "unknown"),
            )
            claim["selected"] = False
            if record.get("label"):
                claim["contact_role"] = record["label"]
            if record.get("selection_reason"):
                claim["selection_reason"] = record["selection_reason"]
            claim["publication_status"] = "allowed"
            claim["publication_policy_version"] = evaluation.get(
                "contact_publication", {},
            ).get("policy_version", "")
            claims.append(claim)
    return deduplicate(claims)
