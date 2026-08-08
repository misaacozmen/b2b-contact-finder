"""Read-only adapter for government/official-registry identity assertions.

Registry records may seed a domain and legal identifiers. Contact values are
intentionally neither parsed nor returned.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

import config
from modules import scorer


LOGGER = logging.getLogger("contact_finder")
ALLOWED_SOURCE_CLASSES = {
    "official_government_registry",
    "government_company_registry",
}


@lru_cache(maxsize=1)
def _records() -> list[dict]:
    try:
        payload = json.loads(
            config.OFFICIAL_REGISTRY_FILE.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read official registry identities: %s", exc)
        return []
    values = payload.get("entities", []) if isinstance(payload, dict) else payload
    return [
        value for value in values
        if isinstance(value, dict)
        and value.get("source_class") in ALLOWED_SOURCE_CLASSES
        and value.get("verification_status") == "verified"
    ] if isinstance(values, list) else []


def find(company: str) -> list[dict]:
    normalized = scorer.normalize_text(company).strip()
    result: list[dict] = []
    for record in _records():
        names = [
            record.get("legal_name", ""),
            *record.get("legal_names", []),
            *record.get("aliases", []),
        ]
        if not any(
            scorer.normalize_text(str(name)).strip() == normalized
            for name in names if name
        ):
            continue
        result.append({
            "legal_name": record.get("legal_name", ""),
            "legal_names": [
                str(value) for value in record.get("legal_names", []) if value
            ],
            "identifiers": [
                str(value) for value in record.get("identifiers", []) if value
            ],
            "official_domain": scorer.normalize_domain(
                record.get("official_domain", "")
            ),
            "registry_url": record.get("registry_url", ""),
            "source_class": record.get("source_class", ""),
            "verification_status": "verified",
        })
    return result


def verified_domains(company: str) -> list[dict]:
    return [
        {
            "url": record["official_domain"],
            "confidence": "verified",
            "relationship": "official_registry_identity",
            "evidence_url": record.get("registry_url", ""),
            "source": record.get("source_class", ""),
        }
        for record in find(company)
        if record.get("official_domain")
    ]
