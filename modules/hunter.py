"""Optional Hunter domain search used only after first-party site extraction fails."""

import logging
import re

import requests

import config
from modules import checkpoint, runtime


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
        runtime.start_api(reservation)
        runtime.wait_for_request_slot()
        runtime.mark_api_http_started(reservation, 1)
        endpoint = DOMAIN_SEARCH_URL
        params = {"domain": domain, "api_key": config.HUNTER_API_KEY, "limit": 10}
        envelope = runtime.transport_envelope(reservation, endpoint=endpoint, request_shape={"method": "GET", "params": {"domain": domain, "limit": 10}}, timeout=config.HUNTER_TIMEOUT_SEC)
        response = runtime.invoke_paid_transport(envelope, lambda: requests.get(
            endpoint,
            params=params,
            timeout=config.HUNTER_TIMEOUT_SEC,
        ))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Hunter response is not a JSON object")
        if "data" not in payload:
            raise ValueError("Hunter response lacks data")
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("Hunter response data is not a JSON object")
        raw_emails = data.get("emails", [])
        if not isinstance(raw_emails, list) or any(not isinstance(item, dict) for item in raw_emails):
            raise ValueError("Hunter emails is not a list of objects")
        emails = []
        for item in raw_emails:
            value = item.get("value", "")
            confidence = item.get("confidence", 0)
            sources = item.get("sources", [])
            if not isinstance(value, str) or isinstance(confidence, bool) or not isinstance(confidence, int) or confidence < 0 or not isinstance(sources, list):
                raise ValueError("Hunter email fields have invalid types")
            value = value.strip().lower()
            if value and confidence >= config.HUNTER_MIN_CONFIDENCE:
                emails.append({"email": value, "confidence": confidence, "sources": sources})
    except Exception as exc:
        if isinstance(exc, checkpoint.SchedulerInvariantError):
            raise
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

    runtime.complete_api(reservation, "DONE")
    return runtime.provider_result(emails, state="COMPLETED" if emails else "EMPTY", reason="results" if emails else "empty_response", call_ids=(getattr(reservation, "call_id", ""),))
