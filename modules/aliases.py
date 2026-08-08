"""Human-approved company aliases and official websites.

This file is deliberately never populated automatically. A verified alias can
help discovery, but it must still pass the normal page and contact checks.
"""

import json
import logging
from functools import lru_cache

import config
from modules import entity_registry, official_registry, scorer


LOGGER = logging.getLogger("contact_finder")


@lru_cache(maxsize=1)
def _entries() -> dict:
    if not config.COMPANY_ALIASES_FILE.exists():
        return {}
    try:
        data = json.loads(config.COMPANY_ALIASES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read company aliases: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def get(company: str) -> dict:
    normalized = scorer.normalize_text(company).strip()
    for key, value in _entries().items():
        if scorer.normalize_text(key).strip() == normalized and isinstance(value, dict):
            return value
    return {}


def search_terms(company: str) -> list[str]:
    entry = get(company)
    values = [
        term.strip() for term in entry.get("aliases", [])
        if isinstance(term, str) and term.strip()
    ]
    values.extend(entity_registry.search_terms(company))
    return list(dict.fromkeys(values))


def verified_websites(company: str) -> list[dict]:
    entry = get(company)
    records: list[dict] = []
    legacy = entry.get("website", "")
    if isinstance(legacy, str) and legacy.strip():
        records.append({"url": legacy.strip(), "relationship": "official", "confidence": "verified", "source": "company_aliases"})
    for value in entry.get("websites", []):
        record = {"url": value} if isinstance(value, str) else value
        if not isinstance(record, dict) or not isinstance(record.get("url"), str):
            continue
        if record.get("confidence", "verified") != "verified":
            continue
        records.append({"relationship": "official", "source": "company_aliases", "confidence": "verified", **record})
    records.extend(entity_registry.verified_domains(company))
    records.extend(official_registry.verified_domains(company))
    by_domain = {}
    for record in records:
        domain = scorer.normalize_domain(record.get("url", ""))
        if domain:
            by_domain[domain] = record
    return list(by_domain.values())


def has_no_website(company: str) -> bool:
    return get(company).get("no_website") is True
