"""Optional Hunter domain search used only after first-party site extraction fails."""

import logging
import re

import requests

import config
from modules import runtime


LOGGER = logging.getLogger("contact_finder")
DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"


def _safe_request_error(exc: Exception) -> str:
    """Keep credentials embedded in provider URLs out of run logs."""
    message = re.sub(
        r"([?&](?:api_key|access_token|token)=)[^&\s]+",
        r"\1[REDACTED]",
        str(exc),
        flags=re.IGNORECASE,
    )
    if config.HUNTER_API_KEY:
        message = message.replace(config.HUNTER_API_KEY, "[REDACTED]")
    return message


def is_enabled() -> bool:
    return (
        bool(getattr(config, "PAID_ENABLED", True))
        and config.ENABLE_HUNTER_FALLBACK
        and bool(config.HUNTER_API_KEY)
    )


def find_domain_emails(domain: str) -> list[dict]:
    if not is_enabled() or not domain:
        return runtime.provider_result([], state="NOT_ENABLED", reason="provider_disabled")
    if not runtime.paid_access_allowed("hunter"):
        return runtime.provider_result([], state="NOT_ENABLED", reason="paid_not_authorized")
    try:
        reservation = runtime.reserve_api("hunter", operation="domain_search", request_fingerprint=runtime.request_fingerprint("hunter", "domain_search", {"domain": domain}))
        if not reservation:
            LOGGER.warning("Hunter run budget exhausted")
            return runtime.rejected_provider_result(reservation)
        runtime.wait_for_request_slot()
        response = requests.get(
            DOMAIN_SEARCH_URL,
            params={"domain": domain, "api_key": config.HUNTER_API_KEY, "limit": 10},
            timeout=config.HUNTER_TIMEOUT_SEC,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Hunter response is not a JSON object")
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("Hunter response data is not a JSON object")
        runtime.complete_api(reservation, "DONE")
    except Exception as exc:
        if "reservation" in locals():
            state = "UNKNOWN" if runtime.is_unknown_transport_error(exc) else "FAILED"
            runtime.complete_api(reservation, state)
            LOGGER.warning("Hunter domain search failed for %s: %s", domain, _safe_request_error(exc))
            return runtime.provider_result([], state=state, reason=f"{type(exc).__name__}:{exc}", call_ids=(getattr(reservation, "call_id", ""),))
        LOGGER.warning(
            "Hunter domain search failed for %s: %s",
            domain,
            _safe_request_error(exc),
        )
        return runtime.provider_result([], state="FAILED", reason=f"{type(exc).__name__}:{exc}")

    emails = []
    for item in data.get("emails", []):
        value = (item.get("value") or "").strip().lower()
        confidence = int(item.get("confidence") or 0)
        if value and confidence >= config.HUNTER_MIN_CONFIDENCE:
            emails.append({"email": value, "confidence": confidence, "sources": item.get("sources") or []})
    return runtime.provider_result(emails, state="COMPLETED" if emails else "EMPTY", reason="results" if emails else "empty_response", call_ids=(getattr(reservation, "call_id", ""),))
