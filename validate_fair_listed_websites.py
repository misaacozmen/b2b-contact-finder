"""Validate fair-listed websites against company/brand identity metadata."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from modules import scorer
from modules.excel import read_company_records
from modules.exhibitor_scraper import _fold, _profile_company_key


_ACRONYM_STOPWORDS = {
    "anonim", "as", "limited", "ltd", "san", "sanayi", "sti", "sirketi", "ve",
}


def _identity_values(record: dict) -> list[str]:
    values = [str(record.get("company", "") or "")]
    for field in ("brands", "representations"):
        values.extend(
            part.strip()
            for part in str(record.get(field, "") or "").replace(",", ";").split(";")
            if part.strip()
        )
    return values


def _acronym_match(company: str, domain_core: str) -> str:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", _fold(company))
        if len(token) >= 2 and token not in _ACRONYM_STOPWORDS
    ][:4]
    if not 2 <= len(tokens) <= 4:
        return ""

    candidates = {""}
    for token in tokens:
        candidates = {
            prefix + token[:width]
            for prefix in candidates
            for width in range(1, min(2, len(token)) + 1)
        }
    matches = [value for value in candidates if len(value) >= 3 and value == domain_core]
    return max(matches, key=len, default="")


def meaningful_domain_token(record: dict) -> str:
    domain = scorer.normalize_domain(str(record.get("listed_website", "") or ""))
    core = scorer.compact_domain_core(domain)
    if not core:
        return ""
    for value in _identity_values(record):
        for token in scorer.domain_identity_tokens(value):
            if len(token) >= 3 and (token in core or core in token):
                return token
    return _acronym_match(str(record.get("company", "") or ""), core)


def validate_records(records: list[dict], listing_metadata: list[dict]) -> dict:
    metadata_by_profile = {
        str(item.get("profile_url", "") or ""): item
        for item in listing_metadata
        if item.get("profile_url")
    }
    results: list[dict] = []
    for source in records:
        if not source.get("listed_website"):
            continue
        record = dict(source)
        metadata = metadata_by_profile.get(str(record.get("profile_url", "") or ""))
        profile_aligned = bool(
            metadata
            and _profile_company_key(str(metadata.get("company", "") or ""))
            == _profile_company_key(str(record.get("company", "") or ""))
        )
        if profile_aligned and metadata.get("brands"):
            record["brands"] = metadata["brands"]

        token = meaningful_domain_token(record)
        proof = "meaningful_identity_token" if token else ""
        if not proof and profile_aligned:
            # Some fairs omit public-brand text while keeping the external
            # website inside the correctly titled company profile. The parser
            # heading guard makes that local association explicit and testable.
            proof = "profile_local_website"
        results.append(
            {
                "company": record.get("company", ""),
                "domain": scorer.normalize_domain(str(record.get("listed_website", "") or "")),
                "proof": proof or "unproven",
                "matched_token": token,
                "brands": record.get("brands", ""),
            }
        )

    proof_counts: dict[str, int] = {}
    for result in results:
        proof_counts[result["proof"]] = proof_counts.get(result["proof"], 0) + 1
    return {
        "checked_count": len(results),
        "passed_count": sum(result["proof"] != "unproven" for result in results),
        "failed_count": sum(result["proof"] == "unproven" for result in results),
        "proof_counts": proof_counts,
        "failures": [result for result in results if result["proof"] == "unproven"],
        "results": results,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--listing-metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main_cli() -> None:
    args = _parse_args()
    listing_metadata = json.loads(args.listing_metadata.read_text(encoding="utf-8"))
    report = validate_records(read_company_records(args.workbook), listing_metadata)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if report["failed_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main_cli()
