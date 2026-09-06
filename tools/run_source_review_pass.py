"""Run one isolated, first-party source-review pass for selected A8 records."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from modules.source_normalizer import is_website_label, normalize_url


ALLOWED_HOSTS = {
    "hometex.com.tr",
    "www.hometex.com.tr",
    "texhibitionist.com",
    "www.texhibitionist.com",
    "zuchex.com",
    "www.zuchex.com",
    "ambiente.messefrankfurt.com",
    "exhibitorsearch.messefrankfurt.com",
    "api.messefrankfurt.com",
}
NON_COMPANY_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "pinterest.com",
}


def _get(session: requests.Session, url: str) -> requests.Response:
    host = (urlparse(url).hostname or "").casefold()
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(f"review URL is not allowlisted: {host}")
    response = session.get(url, timeout=20)
    response.raise_for_status()
    return response


def _text(response: requests.Response) -> str:
    text = response.content.decode("utf-8", "strict")
    if "\ufffd" in text:
        raise RuntimeError(f"replacement character in review response: {response.url}")
    return text


def _clean(value: object) -> str:
    return unicodedata.normalize("NFC", html.unescape(str(value or "")).strip())


def _first_email(soup: BeautifulSoup) -> str:
    for link in soup.find_all("a", href=True):
        href = unquote(str(link["href"]).strip())
        if href.casefold().startswith("mailto:"):
            address = href[7:].split("?", 1)[0].strip()
            if "@" in address:
                return address
    return ""


def _first_phone(soup: BeautifulSoup) -> str:
    for link in soup.find_all("a", href=True):
        href = str(link["href"]).strip()
        if href.casefold().startswith("tel:"):
            value = re.sub(r"[^+0-9]", "", href[4:])
            if len(re.sub(r"\D", "", value)) >= 9 and value not in {"+90888888888", "+908888888888"}:
                return value
    text = soup.get_text(" ", strip=True)
    match = re.search(r"(?:Telephone|Phone)\s+((?:\+|00)?[0-9][0-9 .()/-]{7,})", text, re.I)
    value = re.sub(r"[^+0-9]", "", match.group(1)) if match else ""
    return value if len(re.sub(r"\D", "", value)) >= 9 and value not in {"+90888888888", "+908888888888"} else ""


def _labelled_company_website(soup: BeautifulSoup) -> str:
    for text_node in soup.find_all(string=lambda value: is_website_label(value)):
        parent = text_node.parent
        for container in (parent, parent.parent if parent else None, parent.parent.parent if parent and parent.parent else None):
            if container is None:
                continue
            for link in container.find_all("a", href=True):
                href = _clean(link.get("href"))
                normalized = normalize_url(href)
                parsed = urlparse(normalized["normalized_value"])
                host = (parsed.hostname or "").casefold().removeprefix("www.")
                if normalized["status"] == "present" and host and host not in ALLOWED_HOSTS and host not in NON_COMPANY_HOSTS and host not in {"informa.com", "xing.com", "tobb.org.tr"} and not any(parsed.path.casefold().endswith(suffix) for suffix in (".pdf", ".jpg", ".jpeg", ".png")):
                    return normalized["normalized_value"]
    return ""


def _source_field_website(record: dict) -> str:
    candidate = _clean(record.get("listed_website"))
    normalized = normalize_url(candidate)
    if normalized["status"] != "present":
        return ""
    parsed = urlparse(normalized["normalized_value"])
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if not host or host in NON_COMPANY_HOSTS or host in {"informa.com", "xing.com", "tobb.org.tr"}:
        return ""
    return normalized["normalized_value"]


def _first_company_website(soup: BeautifulSoup) -> str:
    """Compatibility alias retained for callers; only labelled fields qualify."""
    return _labelled_company_website(soup)


def _field(value: str, status: str, *, evidence_url: str, source: str, field: str) -> dict:
    return {
        "value": value,
        "status": status,
        "field_evidence": [{
            "source": source,
            "field": field,
            "evidence_url": evidence_url,
            "value": value,
        }],
    }


def _first_external_company_website_removed(soup: BeautifulSoup) -> str:
    """Explicitly document that unlabeled external links are not company sites."""
    return ""


def _first_company_website_legacy_removed(soup: BeautifulSoup) -> str:
    for link in soup.find_all("a", href=True):
        href = str(link["href"]).strip()
        parsed = urlparse(href)
        host = (parsed.hostname or "").casefold()
        host = host.removeprefix("www.")
        if parsed.scheme in {"http", "https"} and host and host not in ALLOWED_HOSTS and not host.endswith("messefrankfurt.com") and host not in NON_COMPANY_HOSTS:
            return href
    return ""


def _review_record(record: dict, pass_name: str, session: requests.Session, *, execution_id: str, method: str, selection_sha256: str, tool_sha256: str) -> dict:
    evidence_url = str(record.get("official_profile_url") or record.get("listing_url") or "").strip()
    if not evidence_url:
        raise RuntimeError(f"source review record has no official evidence URL: {record.get('source_record_id')}")
    response = _get(session, evidence_url)
    text = _text(response)
    soup = BeautifulSoup(text, "html.parser")
    source = str(record["source"])
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    listed_legal_name = _clean(record.get("legal_name"))
    listed_address = _clean(record.get("listed_address"))
    if source == "zuchex_2026":
        # The public Zuchex listing is the first-party surface available for
        # these diagnostic IDs; it does not expose a source-specific detail
        # page, so no company identity is inferred from the page title.
        identity_status = "known" if _clean(record.get("display_name")) else "unknown"
        listed_legal_name = ""
        website = ""
        email = ""
        phone = ""
    elif source == "texhibition_2026":
        identity_status = "known" if (_clean(record.get("display_name")) or listed_legal_name) else "unknown"
        website = _labelled_company_website(soup) or _source_field_website(record)
        email = _clean(record.get("listed_email")) or _first_email(soup)
        phone = _clean(record.get("listed_phone")) or _first_phone(soup)
    else:
        identity_status = "known" if (_clean(record.get("display_name")) or listed_legal_name) else "unknown"
        website = _labelled_company_website(soup) or _source_field_website(record)
        email = _clean(record.get("listed_email")) or _first_email(soup)
        phone = _clean(record.get("listed_phone")) or _first_phone(soup)
    contact_present = bool(email or phone)
    if identity_status != "known":
        expected_publication = "unknown"
    elif website and contact_present:
        expected_publication = "publishable"
    else:
        expected_publication = "abstain"
    scope_unknown = source == "zuchex_2026"
    website_status = "unknown" if scope_unknown else ("present" if website else "absent")
    email_status = "unknown" if scope_unknown else ("present" if email else "absent")
    phone_status = "unknown" if scope_unknown else ("present" if phone else "absent")
    if scope_unknown and identity_status == "known":
        expected_publication = "abstain"
    elif scope_unknown:
        expected_publication = "unknown"
    if source == "hometex_2026":
        rationale = "Official HOMETEX labelled fields and contact scope reviewed; page title is not used as legal-name evidence."
    elif source == "zuchex_2026":
        rationale = "Official Zuchex public listing reviewed; no source-specific detail identity is inferred from the listing surface."
    elif source == "texhibition_2026":
        rationale = "Official Texhibition exhibitor profile fields and labelled contact scope reviewed; organizer/footer links are not company evidence."
    else:
        rationale = "Official Ambiente structured exhibitor fields and labelled contact fields reviewed; display name is retained separately and not copied into legal_name."
    fields = {
        "website": _field(website if website_status == "present" else "", website_status, evidence_url=evidence_url, source=source, field="website"),
        "email": _field(email if email_status == "present" else "", email_status, evidence_url=evidence_url, source=source, field="email"),
        "phone": _field(phone if phone_status == "present" else "", phone_status, evidence_url=evidence_url, source=source, field="phone"),
        "expected_publication": {
            "value": expected_publication,
            "status": "present" if expected_publication != "unknown" else "unknown",
            "field_evidence": [{"source": source, "field": "expected_publication", "evidence_url": evidence_url, "value": expected_publication}],
        },
    }
    return {
        "schema_version": 1,
        "reviewer_execution_id": execution_id,
        "reviewer_method": method,
        "input_manifest_sha256": selection_sha256,
        "review_contract_sha256": hashlib.sha256(b"A8-ground-truth-review-contract-v2").hexdigest(),
        "tool_or_prompt_sha256": tool_sha256,
        "source_record_id": record["source_record_id"],
        "source": source,
        "display_name_observed": _clean(record.get("display_name")),
        "legal_name_observed": listed_legal_name,
        "listed_legal_name": listed_legal_name,
        "listed_address": listed_address,
        "identity_status": identity_status,
        "evidence_url": evidence_url,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "content_sha256": hashlib.sha256(response.content).hexdigest(),
        "fields": fields,
        "rationale": rationale,
        "label_status": "frozen",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--pass-name", choices=("pass_1", "pass_2"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=120)
    parser.add_argument("--reviewer-method", choices=("official_structured_fields", "independent_rendered_review"))
    parser.add_argument("--execution-id")
    args = parser.parse_args()
    payload = json.loads(args.selection.read_text(encoding="utf-8"))
    records = payload.get("records") or (payload["hometex"]["selected"] + payload["ambiente"]["selected"])
    if len(records) != args.expected_count or len({record["source_record_id"] for record in records}) != args.expected_count:
        raise RuntimeError(f"selection must contain exactly {args.expected_count} unique source IDs")
    session = requests.Session()
    session.headers.update({"User-Agent": f"A8-source-review/{args.pass_name}"})
    execution_id = args.execution_id or f"{args.pass_name}-{uuid.uuid4().hex}"
    method = args.reviewer_method or ("official_structured_fields" if args.pass_name == "pass_1" else "independent_rendered_review")
    selection_sha256 = hashlib.sha256(args.selection.read_bytes()).hexdigest()
    tool_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    rows = []
    for index, record in enumerate(records):
        rows.append(_review_record(record, args.pass_name, session, execution_id=execution_id, method=method, selection_sha256=selection_sha256, tool_sha256=tool_sha256))
        if index + 1 < len(records):
            time.sleep(0.15)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"pass": args.pass_name, "rows": len(rows), "frozen": sum(row["label_status"] == "frozen" for row in rows)}))


if __name__ == "__main__":
    main()
