"""Structured A8 ground-truth reviewer.

This entrypoint deliberately owns its JSON/source-field extraction.  It does
not import the production search, scorer, publication, checkpoint, actual, or
quality modules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from bs4 import BeautifulSoup

from modules.source_normalizer import is_website_label, normalize_url


ALLOWED_HOSTS = {
    "hometex.com.tr", "www.hometex.com.tr", "texhibitionist.com", "www.texhibitionist.com",
    "zuchex.com", "www.zuchex.com", "ambiente.messefrankfurt.com",
    "exhibitorsearch.messefrankfurt.com", "api.messefrankfurt.com",
}
NON_COMPANY_HOSTS = {"facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "pinterest.com", "informa.com", "tobb.org.tr", "xing.com"}
EVENT_HOSTS = {"hometex.com.tr", "texhibitionist.com", "zuchex.com", "messefrankfurt.com"}
FREE_EMAIL_HOSTS = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "proton.me", "protonmail.com"}
WEBSITE_VALUE_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:web\s*site|website|web sitesi|internet sitesi|official website|official site|web)\s*[:：]?\s*"
    r"((?:https?://|//|www\.)[^\s<>,;]+)"
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
CONTRACT = b"A8-structured-review-v3"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _records(selection: dict) -> list[dict]:
    if isinstance(selection.get("records"), list):
        return [dict(row) for row in selection["records"]]
    records = []
    for source in ("hometex", "ambiente"):
        records.extend(dict(row) for row in (selection.get(source) or {}).get("selected", []))
    return records


def _get(session: requests.Session, url: str, receipts: list[dict]) -> tuple[str, bytes]:
    host = (urlparse(url).hostname or "").casefold()
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(f"structured reviewer host is not allowlisted: {host}")
    started = time.monotonic()
    response = session.get(url, timeout=20)
    body = response.content
    response.raise_for_status()
    text = body.decode("utf-8", "strict")
    if "\ufffd" in text:
        raise RuntimeError(f"replacement character in structured review response: {url}")
    receipts.append({"url": url, "status": response.status_code, "bytes": len(body), "response_sha256": _sha256_bytes(body), "elapsed_seconds": time.monotonic() - started})
    return text, body


def _host_is_company(candidate: str) -> bool:
    host = (urlparse(candidate).hostname or "").casefold().removeprefix("www.")
    return bool(host and host not in NON_COMPANY_HOSTS and host not in {item.removeprefix("www.") for item in ALLOWED_HOSTS})


def _site_key(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    labels = [part for part in host.split(".") if part]
    if len(labels) >= 3 and labels[-2] in {"com", "net", "org", "gov", "edu"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _source_website_candidates(record: dict, soup: BeautifulSoup | None, profile_url: str) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    raw_source = _clean(record.get("source_listed_website") or record.get("listed_website"))
    if raw_source:
        candidates.append((raw_source, "source_listed_website", "/source_listed_website"))
    if soup is not None:
        text = soup.get_text(" ", strip=True)
        for match in WEBSITE_VALUE_RE.finditer(text):
            candidates.append((match.group(1).rstrip(".,;)]}"), "official_profile", "visible_text/website_label"))
        for email in sorted({value.casefold() for value in EMAIL_RE.findall(text)}):
            host = email.split("@", 1)[1]
            if host not in FREE_EMAIL_HOSTS and host not in EVENT_HOSTS:
                candidates.append((f"https://{host}", "official_profile_email_domain", "visible_text/email_domain"))
        for node in soup.find_all(string=lambda value: is_website_label(value)):
            container = node.parent
            if container is not None:
                for link in container.find_all("a", href=True):
                    candidates.append((_clean(link.get("href")), "official_profile", "explicit_label/a[href]"))
    return candidates


def _website_from_structured_fields(record: dict, soup: BeautifulSoup | None, profile_url: str) -> tuple[str, list[dict]]:
    evidence = []
    for raw, source, pointer in _source_website_candidates(record, soup, profile_url):
        normalized = normalize_url(raw, source_url=profile_url)
        parsed = urlparse(normalized["normalized_value"])
        if normalized["status"] == "present" and _host_is_company(normalized["normalized_value"]):
            evidence.append({"source": source, "candidate": normalized["normalized_value"], "accepted": True, "selector_or_json_pointer": pointer, "status": "present"})
            return normalized["normalized_value"], evidence
        evidence.append({"source": source, "candidate": raw, "accepted": False, "selector_or_json_pointer": pointer, "status": normalized["status"], "rejection_reason": normalized["rejection_reason"], "host": parsed.hostname or ""})
    return "", evidence


def _fetch_first_party(session: requests.Session, candidate: str, source_url: str, receipts: list[dict]) -> tuple[BeautifulSoup | None, str, dict]:
    normalized = normalize_url(candidate, source_url=source_url)
    value = normalized["normalized_value"]
    parsed = urlparse(value)
    evidence = {"source": "first_party_fetch", "candidate": candidate, "normalized_value": value, "status": normalized["status"], "rejection_reason": normalized["rejection_reason"]}
    if normalized["status"] != "present" or not _host_is_company(value):
        evidence["status"] = "rejected"
        evidence["rejection_reason"] = evidence["rejection_reason"] or "non_company_or_invalid_candidate"
        return None, "", evidence
    try:
        started = time.monotonic()
        response = session.get(value, timeout=20, allow_redirects=False)
        body = response.content
        receipts.append({"url": value, "status": response.status_code, "bytes": len(body), "response_sha256": _sha256_bytes(body), "elapsed_seconds": time.monotonic() - started, "role": "first_party_candidate"})
        if response.is_redirect or response.is_permanent_redirect:
            target = normalize_url(response.headers.get("location", ""), source_url=value)
            if target["status"] != "present" or _site_key(target["normalized_value"]) != _site_key(value):
                evidence.update(status="rejected", rejection_reason="cross_domain_redirect")
                return None, "", evidence
            response = session.get(target["normalized_value"], timeout=20, allow_redirects=False)
            body = response.content
            value = target["normalized_value"]
        text = body.decode("utf-8", "strict")
        if "\ufffd" in text:
            evidence.update(status="rejected", rejection_reason="replacement_character")
            return None, "", evidence
        evidence.update(status="present", normalized_value=value, source_url=value)
        return BeautifulSoup(text, "html.parser"), value, evidence
    except (requests.RequestException, UnicodeDecodeError) as exc:
        evidence.update(status="unknown", rejection_reason=type(exc).__name__)
        return None, "", evidence


def _emails(record: dict, soup: BeautifulSoup | None) -> tuple[list[str], list[dict]]:
    values = []
    listed = _clean(record.get("listed_email"))
    if listed:
        values.append(listed.casefold())
    if soup is not None:
        for link in soup.find_all("a", href=True):
            href = _clean(link.get("href"))
            if href.casefold().startswith("mailto:"):
                values.append(href[7:].split("?", 1)[0].casefold())
        values.extend(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", soup.get_text(" ", strip=True), re.I))
    values = sorted({value for value in values if re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", value.casefold())})
    return values, [{"source": "structured_field", "value": value, "accepted": True} for value in values]


def _phones(record: dict, soup: BeautifulSoup | None) -> tuple[list[str], list[dict]]:
    values = []
    listed = _clean(record.get("listed_phone"))
    if listed:
        values.append(listed)
    if soup is not None:
        values.extend(_clean(link.get("href"))[4:] for link in soup.find_all("a", href=True) if _clean(link.get("href")).casefold().startswith("tel:"))
    values = sorted({re.sub(r"[^+0-9]", "", value) for value in values if len(re.sub(r"\D", "", value)) >= 9})
    return values, [{"source": "structured_field", "value": value, "accepted": True} for value in values]


def _field(value: object, status: str, evidence: list[dict]) -> dict:
    return {"value": value if isinstance(value, list) else _clean(value), "status": status, "field_evidence": evidence}


def review_record(record: dict, session: requests.Session, receipts: list[dict]) -> dict:
    source_id = _clean(record.get("source_record_id"))
    profile_url = _clean(record.get("official_profile_url"))
    soup = None
    content_hash = ""
    if profile_url:
        try:
            text, body = _get(session, profile_url, receipts)
            soup = BeautifulSoup(text, "html.parser")
            content_hash = _sha256_bytes(body)
        except requests.RequestException as exc:
            receipts.append({"url": profile_url, "status": "error", "reason": type(exc).__name__})
    website, website_evidence = _website_from_structured_fields(record, soup, profile_url)
    first_party_soup = None
    first_party_url = ""
    for raw, source, pointer in _source_website_candidates(record, soup, profile_url):
        first_party_soup, first_party_url, candidate_evidence = _fetch_first_party(session, raw, profile_url, receipts)
        website_evidence.append({**candidate_evidence, "source": source, "selector_or_json_pointer": pointer})
        if first_party_soup is not None:
            website = first_party_url
            break
    emails, email_evidence = _emails(record, soup)
    phones, phone_evidence = _phones(record, soup)
    if first_party_soup is not None:
        extra_emails, extra_email_evidence = _emails({}, first_party_soup)
        extra_phones, extra_phone_evidence = _phones({}, first_party_soup)
        emails = sorted(set(emails + extra_emails))
        phones = sorted(set(phones + extra_phones))
        email_evidence.extend(extra_email_evidence)
        phone_evidence.extend(extra_phone_evidence)
    display = _clean(record.get("display_name") or record.get("Company"))
    legal = _clean(record.get("legal_name") or record.get("listed_legal_name"))
    identity = "known" if display or legal else "unknown"
    website_status = "present" if website else "unknown"
    email_status = "present" if emails else "unknown"
    phone_status = "present" if phones else "unknown"
    publication = "publishable" if identity == "known" and website and (emails or phones) else "unknown"
    evidence_url = profile_url or _clean(record.get("listing_url"))
    return {
        "schema_version": 3,
        "reviewer_execution_id": "",
        "reviewer_method": "structured_json_and_source_fields",
        "source_record_id": source_id,
        "source": _clean(record.get("source")),
        "display_name_observed": display,
        "legal_name_observed": legal,
        "listed_legal_name": legal,
        "listed_address": _clean(record.get("listed_address")),
        "source_listed_website": _clean(record.get("source_listed_website") or record.get("listed_website")),
        "source_listed_website_status": "present" if _clean(record.get("source_listed_website") or record.get("listed_website")) else "absent",
        "identity_status": identity,
        "evidence_url": evidence_url,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "content_sha256": content_hash,
        "fields": {
            "website": _field(website, website_status, website_evidence),
            "email": _field(emails, email_status, email_evidence),
            "phone": _field(phones, phone_status, phone_evidence),
            "expected_publication": _field(publication, "present" if publication != "unknown" else "unknown", [{"source": "structured_decision", "value": publication}]),
        },
        "rationale": f"Structured source fields and official JSON/HTML field containers reviewed for {source_id}.",
        "label_status": "frozen",
    }


def run(selection_path: Path, output_path: Path, manifest_path: Path | None, expected_count: int | None) -> dict:
    selection_payload = json.loads(selection_path.read_text(encoding="utf-8"))
    records = _records(selection_payload)
    if expected_count is not None and len(records) != expected_count:
        raise RuntimeError(f"selection count mismatch: {len(records)} != {expected_count}")
    ids = [_clean(row.get("source_record_id")) for row in records]
    if not all(ids) or len(ids) != len(set(ids)):
        raise RuntimeError("structured review selection IDs must be nonempty and unique")
    entrypoint = Path(__file__).resolve()
    execution_id = f"structured-{uuid.uuid4().hex}"
    execution_started = datetime.now(timezone.utc).isoformat()
    receipts: list[dict] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "A8-structured-review/3"})
    rows = [review_record(record, session, receipts) for record in records]
    entrypoint_hash = _sha256_file(entrypoint)
    bundle_hash = _sha256_bytes(entrypoint.read_bytes() + CONTRACT)
    for row in rows:
        row.update({
            "reviewer_execution_id": execution_id,
            "input_manifest_sha256": _sha256_file(selection_path),
            "review_contract_sha256": _sha256_bytes(CONTRACT),
            "reviewer_entrypoint_sha256": entrypoint_hash,
            "reviewer_bundle_sha256": bundle_hash,
            "tool_or_prompt_sha256": entrypoint_hash,
        })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    manifest = {
        "schema_version": 3, "execution_id": execution_id, "method": "structured_json_and_source_fields",
        "input_selection_sha256": _sha256_file(selection_path), "allowed_input_paths": [{"path": str(selection_path.resolve()), "sha256": _sha256_file(selection_path)}],
        "entrypoint": str(entrypoint), "entrypoint_sha256": entrypoint_hash, "review_contract_sha256": _sha256_bytes(CONTRACT),
        "reviewer_bundle_sha256": bundle_hash, "started_at": execution_started, "finished_at": datetime.now(timezone.utc).isoformat(),
        "http_receipts": receipts, "source_record_count": len(rows),
    }
    target_manifest = manifest_path or output_path.with_name("review_execution_manifest.json")
    target_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"rows": len(rows), "execution_id": execution_id, "reviewer_bundle_sha256": bundle_hash, "manifest": str(target_manifest)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execution-manifest", type=Path)
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args()
    print(json.dumps(run(args.selection, args.output, args.execution_manifest, args.expected_count), sort_keys=True))


if __name__ == "__main__":
    main()
