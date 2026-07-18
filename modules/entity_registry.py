"""Auditable legal-entity, brand and official-domain relationships.

Only records explicitly marked ``verified`` can become authoritative search
candidates. Runtime observations are written separately and never promote
themselves, preventing a bad crawl from poisoning later runs.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import config
from modules import scorer


LOGGER = logging.getLogger("contact_finder")


@lru_cache(maxsize=1)
def _entities() -> list[dict]:
    try:
        payload = json.loads(config.ENTITY_REGISTRY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read entity registry: %s", exc)
        return []
    values = payload.get("entities", []) if isinstance(payload, dict) else payload
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _names(entity: dict) -> list[str]:
    values = [entity.get("legal_name", ""), *entity.get("legal_names", []), *entity.get("brands", []), *entity.get("aliases", [])]
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def find(company: str) -> dict:
    normalized = scorer.normalize_text(company).strip()
    for entity in _entities():
        if any(scorer.normalize_text(name).strip() == normalized for name in _names(entity)):
            return entity
    return {}


def search_terms(company: str) -> list[str]:
    entity = find(company)
    normalized_company = scorer.normalize_text(company).strip()
    return [name for name in _names(entity) if scorer.normalize_text(name).strip() != normalized_company]


def verified_domains(company: str) -> list[dict]:
    entity = find(company)
    results: list[dict] = []
    for record in entity.get("official_domains", []):
        if isinstance(record, str):
            record = {"url": record, "confidence": "verified"}
        if not isinstance(record, dict) or record.get("confidence") != "verified":
            continue
        url = record.get("url", "")
        if not scorer.normalize_domain(url):
            continue
        results.append({
            **record,
            "url": url,
            "entity_id": entity.get("entity_id", ""),
            "relationship": record.get("relationship", "official"),
        })
    return results


def write_observations(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            evaluation = row.get("__evaluation", {})
            selected = evaluation.get("candidate", {}) if isinstance(evaluation, dict) else {}
            structured = evaluation.get("structured_identity", {}) if isinstance(evaluation, dict) else {}
            record = {
                "company": row.get("company", ""),
                "selected_domain": scorer.normalize_domain(row.get("website", "")),
                "status": row.get("status", ""),
                "entity_id": selected.get("_entity_id", ""),
                "relationship": selected.get("_entity_relationship", "observed_candidate"),
                "source": row.get("website_source", ""),
                "evidence_url": selected.get("_entity_evidence_url", selected.get("_profile_url", "")),
                "structured_urls": structured.get("urls", []),
                "structured_same_as": structured.get("same_as", []),
                "confidence": "observed_high" if str(row.get("status", "")).startswith("OK_") else "observed_review",
                "observed_at": observed_at,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
