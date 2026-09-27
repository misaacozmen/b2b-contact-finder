"""Adjudicate predicted-vs-truth domain disagreements from public site contacts.

Free HTTP only: homepage plus up to three same-host contact pages per domain.
No paid provider is involved.  The verdict rule lives in
``calibration.same_entity_verdict`` so tests can exercise it offline.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlparse

import requests
import urllib3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import calibration, scorer
from tools import calibrate_acceptance


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)
CONTACT_HINT = re.compile(r"iletisim|iletişim|contact|kontakt|bize-ulasin|hakkimizda|about", re.I)
HREF = re.compile(r"href=[\"']([^\"'#]+)[\"']", re.I)
MAX_CONTACT_PAGES = 3
TIMEOUT_SEC = 15
MAX_BYTES = 600_000


def disagreement_pairs(records: list[dict], rules: list[dict | None]) -> list[dict]:
    """Every labelled (firm, prediction) pair that is not a brand match."""
    pairs: dict[str, dict] = {}
    for record in records:
        if not record.get("labelled"):
            continue
        truth = str(record.get("truth_domain") or "")
        for rule in rules:
            prediction = calibrate_acceptance.predict(record, rule)
            if not prediction or calibration.brand_match(prediction, truth):
                continue
            key = calibration.adjudication_key(str(record.get("source_record_id") or ""), prediction)
            pairs.setdefault(key, {
                "key": key,
                "source_record_id": str(record.get("source_record_id") or ""),
                "company": str(record.get("company") or ""),
                "predicted_domain": scorer.normalize_domain(prediction),
                "truth_domain": scorer.normalize_domain(truth),
            })
    return [pairs[key] for key in sorted(pairs)]


def _fetch(url: str) -> tuple[str, str]:
    try:
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SEC,
            verify=False, allow_redirects=True,
        )
    except requests.RequestException:
        return "", url
    try:
        if response.status_code >= 400:
            return "", str(response.url)
        response.encoding = response.apparent_encoding or response.encoding
        return response.text[:MAX_BYTES], str(response.url)
    finally:
        response.close()


def fetch_site_contacts(domain: str) -> dict:
    home, final_url = "", ""
    for base in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
        home, final_url = _fetch(base)
        if home:
            break
    if not home:
        return {"reachable": False, "phones": [], "emails": [], "pages": 0}
    host = urlparse(final_url).netloc.lower().removeprefix("www.")
    links: list[str] = []
    for href in HREF.findall(home):
        url = urljoin(final_url, href)
        if (
            urlparse(url).netloc.lower().removeprefix("www.") == host
            and CONTACT_HINT.search(url)
            and url not in links
        ):
            links.append(url)
    texts = [home]
    for url in links[:MAX_CONTACT_PAGES]:
        text, _ = _fetch(url)
        if text:
            texts.append(text)
    contacts = calibration.extract_contacts("\n".join(texts))
    return {
        "reachable": True,
        "phones": sorted(contacts["phones"]),
        "emails": sorted(contacts["emails"]),
        "pages": len(texts),
    }


def adjudicate(pairs: list[dict], fetch=fetch_site_contacts) -> dict:
    sites: dict[str, dict] = {}
    for pair in pairs:
        for domain in (pair["predicted_domain"], pair["truth_domain"]):
            if domain not in sites:
                sites[domain] = fetch(domain)
    verdicts: dict[str, str] = {}
    for pair in pairs:
        pair.update(calibration.same_entity_verdict(
            pair["predicted_domain"], pair["truth_domain"],
            sites[pair["predicted_domain"]], sites[pair["truth_domain"]],
        ))
        verdicts[pair["key"]] = pair["verdict"]
    summary = {
        verdict: sum(1 for pair in pairs if pair["verdict"] == verdict)
        for verdict in ("SAME_ENTITY", "DIFFERENT", "UNKNOWN")
    }
    return {"pairs": pairs, "verdicts": verdicts, "sites": sites, "summary": summary}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--rule-id", default="", help="limit pairs to one rule (plus the existing path)")
    args = parser.parse_args()
    urllib3.disable_warnings()
    records = calibrate_acceptance._read_truth(Path(args.truth))
    grid = calibrate_acceptance.acceptance_grid()
    if args.rule_id:
        rules = [rule for rule in grid if calibrate_acceptance.rule_id(rule) == args.rule_id]
        if not rules:
            parser.error(f"unknown --rule-id: {args.rule_id!r}")
    else:
        rules = grid
    result = adjudicate(disagreement_pairs(records, [None, *rules]))
    result = {"truth": str(args.truth), "rule_scope": args.rule_id or "ALL_GRID", **result}
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"pairs": len(result["pairs"]), **result["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
