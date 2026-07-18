"""Optional Hunter domain search used only after first-party site extraction fails."""

import logging

import requests

import config
from modules import runtime


LOGGER = logging.getLogger("contact_finder")
DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"


def is_enabled() -> bool:
    return config.ENABLE_HUNTER_FALLBACK and bool(config.HUNTER_API_KEY)


def find_domain_emails(domain: str) -> list[dict]:
    if not is_enabled() or not domain:
        return []
    try:
        if not runtime.reserve_api("hunter", config.HUNTER_REQUEST_BUDGET):
            LOGGER.warning("Hunter run budget exhausted")
            return []
        runtime.wait_for_request_slot()
        response = requests.get(
            DOMAIN_SEARCH_URL,
            params={"domain": domain, "api_key": config.HUNTER_API_KEY, "limit": 10},
            timeout=config.HUNTER_TIMEOUT_SEC,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.warning("Hunter domain search failed for %s: %s", domain, exc)
        return []

    emails = []
    for item in response.json().get("data", {}).get("emails", []):
        value = (item.get("value") or "").strip().lower()
        confidence = int(item.get("confidence") or 0)
        if value and confidence >= config.HUNTER_MIN_CONFIDENCE:
            emails.append({"email": value, "confidence": confidence, "sources": item.get("sources") or []})
    return emails
