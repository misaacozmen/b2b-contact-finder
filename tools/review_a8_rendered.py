"""Rendered/browser A8 ground-truth reviewer.

The extraction and candidate decision code is intentionally separate from the
structured reviewer.  It only consumes the selection and rendered official
page text; it never reads actuals, expected workbooks, or production scores.
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

from bs4 import BeautifulSoup

from modules.source_normalizer import normalize_url


ALLOWED_HOSTS = {
    "hometex.com.tr", "www.hometex.com.tr", "texhibitionist.com", "www.texhibitionist.com",
    "zuchex.com", "www.zuchex.com", "ambiente.messefrankfurt.com",
    "exhibitorsearch.messefrankfurt.com", "api.messefrankfurt.com",
}
CONTRACT = b"A8-rendered-browser-review-v3"
NON_COMPANY_HOSTS = {"facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "pinterest.com", "informa.com", "tobb.org.tr", "xing.com"}
EVENT_HOSTS = {"hometex.com.tr", "texhibitionist.com", "zuchex.com", "messefrankfurt.com"}
FREE_EMAIL_HOSTS = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "proton.me", "protonmail.com"}
WEBSITE_VALUE_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:web\s*site|website|web sitesi|internet sitesi|official website|official site|web)\s*[:：]?\s*"
    r"((?:https?://|//|www\.)[^\s<>,;]+)"
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    return _sha(path.read_bytes())


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _records(payload: dict) -> list[dict]:
    if isinstance(payload.get("records"), list):
        return [dict(item) for item in payload["records"]]
    return [dict(item) for name in ("hometex", "ambiente") for item in (payload.get(name) or {}).get("selected", [])]


def _label(value: object) -> str:
    value = _text(value).rstrip(":： ").casefold()
    return value


def _site_key(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    labels = [part for part in host.split(".") if part]
    if len(labels) >= 3 and labels[-2] in {"com", "net", "org", "gov", "edu"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _company_candidate(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    return bool(host and host not in NON_COMPANY_HOSTS and host not in EVENT_HOSTS)


def _render(url: str, receipts: list[dict], *, allow_company_host: bool = False) -> tuple[str, bytes, str]:
    host = (urlparse(url).hostname or "").casefold()
    if host not in ALLOWED_HOSTS and not (allow_company_host and _company_candidate(url)):
        raise RuntimeError(f"rendered reviewer host is not allowlisted: {host}")
    started = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(f"browser_dependency_unavailable:{type(exc).__name__}") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        response = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        rendered = page.content().encode("utf-8")
        status = response.status if response is not None else None
        browser.close()
    decoded = rendered.decode("utf-8", "strict")
    if "\ufffd" in decoded:
        raise RuntimeError(f"replacement character in rendered response: {url}")
    receipts.append({"url": url, "status": status, "bytes": len(rendered), "response_sha256": _sha(rendered), "elapsed_seconds": time.monotonic() - started, "retrieval": "playwright_rendered_dom"})
    return decoded, rendered, "playwright"


def _render_first_party(url: str, source_url: str, receipts: list[dict]) -> tuple[BeautifulSoup | None, str, dict]:
    normalized = normalize_url(url, source_url=source_url)
    value = normalized["normalized_value"]
    evidence = {"source": "first_party_render", "candidate": url, "normalized_value": value, "status": normalized["status"], "rejection_reason": normalized["rejection_reason"]}
    if normalized["status"] != "present" or not _company_candidate(value):
        evidence.update(status="rejected", rejection_reason=evidence["rejection_reason"] or "non_company_or_invalid_candidate")
        return None, "", evidence
    try:
        html, body, _ = _render(value, receipts, allow_company_host=True)
        evidence.update(status="present", normalized_value=value, response_sha256=_sha(body))
        return BeautifulSoup(html, "html.parser"), value, evidence
    except Exception as exc:
        evidence.update(status="unknown", rejection_reason=type(exc).__name__)
        return None, "", evidence


def _visible_website(soup: BeautifulSoup, source_url: str) -> tuple[str, list[dict]]:
    evidence = []
    text = soup.get_text(" ", strip=True)
    for match in WEBSITE_VALUE_RE.finditer(text):
        raw = _text(match.group(1)).rstrip(".,;)]}")
        normalized = normalize_url(raw, source_url=source_url)
        host = (urlparse(normalized["normalized_value"]).hostname or "").casefold().removeprefix("www.")
        accepted = normalized["status"] == "present" and _company_candidate(normalized["normalized_value"])
        evidence.append({"source": "rendered_visible_text", "raw_value": raw, "normalized_value": normalized["normalized_value"], "accepted": bool(accepted), "label": match.group(0)[:80], "status": normalized["status"], "rejection_reason": normalized["rejection_reason"], "host": host})
        if accepted:
            return normalized["normalized_value"], evidence
    for node in soup.find_all(["dt", "th", "label", "strong", "b", "span", "div", "p"]):
        if _label(node.get_text(" ", strip=True)) not in {"web", "website", "web site", "web sitesi", "internet sitesi", "official website", "official site"}:
            continue
        parent = node.parent
        if parent is None:
            continue
        for link in parent.find_all("a", href=True):
            raw = _text(link.get("href"))
            normalized = normalize_url(raw, source_url=source_url)
            host = (urlparse(normalized["normalized_value"]).hostname or "").casefold().removeprefix("www.")
            accepted = normalized["status"] == "present" and host and _company_candidate(normalized["normalized_value"])
            evidence.append({"source": "rendered_visible_label", "raw_value": raw, "normalized_value": normalized["normalized_value"], "accepted": bool(accepted), "label": node.get_text(" ", strip=True), "status": normalized["status"], "rejection_reason": normalized["rejection_reason"]})
            if accepted:
                return normalized["normalized_value"], evidence
    return "", evidence


def _visible_contacts(soup: BeautifulSoup) -> tuple[list[str], list[str], list[dict]]:
    visible = soup.get_text(" ", strip=True)
    emails = sorted(set(value.casefold() for value in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", visible, re.I)))
    phones = sorted(set(re.sub(r"[^+0-9]", "", value) for value in re.findall(r"(?:\+|00)?[0-9][0-9 .()/-]{7,}[0-9]", visible) if len(re.sub(r"\D", "", value)) >= 9))
    evidence = [{"source": "rendered_visible_text", "field": "email", "value": value} for value in emails]
    evidence.extend({"source": "rendered_visible_text", "field": "phone", "value": value} for value in phones)
    return emails, phones, evidence


def _candidate_urls(record: dict, soup: BeautifulSoup, profile_url: str) -> list[str]:
    values = []
    listed = _text(record.get("source_listed_website") or record.get("listed_website"))
    if listed:
        values.append(listed)
    text = soup.get_text(" ", strip=True)
    values.extend(match.group(1).rstrip(".,;)]}") for match in WEBSITE_VALUE_RE.finditer(text))
    values.extend(f"https://{email.split('@', 1)[1].casefold()}" for email in sorted(set(EMAIL_RE.findall(text))) if email.split("@", 1)[1].casefold() not in FREE_EMAIL_HOSTS | EVENT_HOSTS)
    return list(dict.fromkeys(values))


def review_record(record: dict, receipts: list[dict]) -> dict:
    url = _text(record.get("official_profile_url"))
    html, body, _ = _render(url, receipts) if url else ("", b"", "none")
    soup = BeautifulSoup(html, "html.parser")
    website, website_evidence = _visible_website(soup, url)
    emails, phones, contact_evidence = _visible_contacts(soup)
    first_party_soup = None
    for candidate in _candidate_urls(record, soup, url):
        first_party_soup, first_party_url, candidate_evidence = _render_first_party(candidate, url, receipts)
        website_evidence.append(candidate_evidence)
        if first_party_soup is not None:
            website = first_party_url
            extra_emails, extra_phones, extra_evidence = _visible_contacts(first_party_soup)
            emails = sorted(set(emails + extra_emails))
            phones = sorted(set(phones + extra_phones))
            contact_evidence.extend(extra_evidence)
            break
    heading = soup.find(["h1", "h2"]) or soup.title
    display = _text(heading.get_text(" ", strip=True) if heading else record.get("display_name"))
    listed = _text(record.get("display_name") or record.get("Company"))
    identity = "known" if display or listed else "unknown"
    website_status = "present" if website else "unknown"
    email_status = "present" if emails else "unknown"
    phone_status = "present" if phones else "unknown"
    publication = "publishable" if identity == "known" and website and (emails or phones) else "unknown"
    return {
        "schema_version": 3, "reviewer_execution_id": "", "reviewer_method": "rendered_browser_visible_fields",
        "source_record_id": _text(record.get("source_record_id")), "source": _text(record.get("source")),
        "display_name_observed": display, "legal_name_observed": "", "listed_legal_name": "",
        "listed_address": _text(record.get("listed_address")), "identity_status": identity,
        "source_listed_website": _text(record.get("source_listed_website") or record.get("listed_website")),
        "source_listed_website_status": "present" if _text(record.get("source_listed_website") or record.get("listed_website")) else "absent",
        "evidence_url": url or _text(record.get("listing_url")), "observed_at": datetime.now(timezone.utc).isoformat(),
        "content_sha256": _sha(body),
        "fields": {
            "website": {"value": website, "status": website_status, "field_evidence": website_evidence},
            "email": {"value": emails, "status": email_status, "field_evidence": [item for item in contact_evidence if item.get("field") == "email"]},
            "phone": {"value": phones, "status": phone_status, "field_evidence": [item for item in contact_evidence if item.get("field") == "phone"]},
            "expected_publication": {"value": publication, "status": "present" if publication != "unknown" else "unknown", "field_evidence": [{"source": "rendered_decision", "value": publication}]},
        },
        "rationale": f"Rendered visible labels, containers, identity heading, and contact text independently reviewed for {_text(record.get('source_record_id'))}.",
        "label_status": "frozen",
    }


def run(selection_path: Path, output_path: Path, manifest_path: Path | None, expected_count: int | None) -> dict:
    payload = json.loads(selection_path.read_text(encoding="utf-8"))
    records = _records(payload)
    if expected_count is not None and len(records) != expected_count:
        raise RuntimeError(f"selection count mismatch: {len(records)} != {expected_count}")
    ids = [_text(item.get("source_record_id")) for item in records]
    if not all(ids) or len(ids) != len(set(ids)):
        raise RuntimeError("rendered review selection IDs must be nonempty and unique")
    entrypoint = Path(__file__).resolve()
    execution_id = f"rendered-{uuid.uuid4().hex}"
    receipts: list[dict] = []
    rows = [review_record(item, receipts) for item in records]
    entrypoint_hash = _file_sha(entrypoint)
    bundle_hash = _sha(entrypoint.read_bytes() + CONTRACT)
    selection_hash = _file_sha(selection_path)
    for row in rows:
        row.update({"reviewer_execution_id": execution_id, "input_manifest_sha256": selection_hash, "review_contract_sha256": _sha(CONTRACT), "reviewer_entrypoint_sha256": entrypoint_hash, "reviewer_bundle_sha256": bundle_hash, "tool_or_prompt_sha256": entrypoint_hash})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    manifest = {
        "schema_version": 3, "execution_id": execution_id, "method": "rendered_browser_visible_fields",
        "input_selection_sha256": selection_hash, "allowed_input_paths": [{"path": str(selection_path.resolve()), "sha256": selection_hash}],
        "entrypoint": str(entrypoint), "entrypoint_sha256": entrypoint_hash, "review_contract_sha256": _sha(CONTRACT), "reviewer_bundle_sha256": bundle_hash,
        "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
        "http_browser_receipts": receipts, "source_record_count": len(rows),
    }
    target = manifest_path or output_path.with_name("review_execution_manifest.json")
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"rows": len(rows), "execution_id": execution_id, "reviewer_bundle_sha256": bundle_hash, "manifest": str(target)}


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
