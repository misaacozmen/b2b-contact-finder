"""Optional Google Places API (New) lookup for official website and phone evidence."""

import logging
import json

import requests

import config
from modules import cache_store, runtime, scorer


LOGGER = logging.getLogger("contact_finder")
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id,places.displayName,places.websiteUri,places.internationalPhoneNumber,places.businessStatus"
_BUDGET_LATCHED = False


def reset() -> None:
    global _BUDGET_LATCHED
    _BUDGET_LATCHED = False


def is_enabled() -> bool:
    return config.ENABLE_GOOGLE_PLACES and bool(config.GOOGLE_PLACES_API_KEY)


def search_company(company: str) -> list[dict]:
    """Return Google-maintained business records without making them trusted candidates yet."""
    global _BUDGET_LATCHED
    if not is_enabled():
        return runtime.provider_result([], state="NOT_ENABLED", reason="provider_disabled")
    variants = scorer.search_name_variants(company)
    query_name = (variants[0] if variants else company).strip()[:100]
    cache_key = json.dumps({"query": query_name, "region": "TR", "fields": FIELD_MASK}, sort_keys=True)
    if config.SEARCH_CACHE_MODE in {"use", "replay"}:
        cached = cache_store.load(
            config.SEARCH_CACHE_DIR, "google_places", cache_key,
            config.SEARCH_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION,
        )
        if cached is not None:
            return runtime.provider_result(cached, state="CACHE_HIT", reason="cached")
        if config.SEARCH_CACHE_MODE == "replay":
            LOGGER.warning("Google Places replay cache miss: %s", company)
            return runtime.provider_result([], state="REPLAY_MISS", reason="replay_cache_miss")
    if _BUDGET_LATCHED:
        runtime.record("api.google_places.budget_latched")
        return runtime.provider_result([], state="BLOCKED_BUDGET", reason="budget_latched")
    try:
        reservation = runtime.reserve_api("google_places", operation="text_search", request_fingerprint=runtime.request_fingerprint("google_places", "text_search", {"query": query_name, "region": "TR"}))
        if not reservation:
            if reservation.reason in {"budget_exhausted", "budget_disabled"} and not _BUDGET_LATCHED:
                LOGGER.warning("Google Places run budget exhausted; disabling remaining calls")
                _BUDGET_LATCHED = True
                runtime.record("api.google_places.budget_exhausted_logged")
            elif reservation.reason in {"budget_exhausted", "budget_disabled"}:
                runtime.record("api.google_places.budget_exhausted_latched")
            return runtime.rejected_provider_result(reservation)
        runtime.wait_for_request_slot()
        response = requests.post(
            TEXT_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": FIELD_MASK,
            },
            json={"textQuery": f"{query_name} Turkey", "languageCode": "tr", "regionCode": "TR", "maxResultCount": 5},
            timeout=config.GOOGLE_PLACES_TIMEOUT_SEC,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Google Places response is not a JSON object")
        runtime.complete_api(reservation, "DONE")
    except Exception as exc:
        if "reservation" in locals():
            state = "UNKNOWN" if runtime.is_unknown_transport_error(exc) else "FAILED"
            runtime.complete_api(reservation, state)
            return runtime.provider_result([], state=state, reason=f"{type(exc).__name__}:{exc}", call_ids=(getattr(reservation, "call_id", ""),))
        LOGGER.warning("Google Places lookup failed for %s: %s", company, exc)
        return runtime.provider_result([], state="FAILED", reason=f"{type(exc).__name__}:{exc}")

    places = []
    for place in payload.get("places", []):
        if place.get("businessStatus") == "CLOSED_PERMANENTLY":
            continue
        website = place.get("websiteUri", "")
        if not website:
            continue
        places.append(
            {
                "website": website,
                "phone": place.get("internationalPhoneNumber", ""),
                "name": (place.get("displayName") or {}).get("text", ""),
                "place_id": place.get("id", ""),
            }
        )
    if config.SEARCH_CACHE_MODE in {"use", "refresh"}:
        cache_store.save(
            config.SEARCH_CACHE_DIR, "google_places", cache_key, places,
            config.CACHE_SCHEMA_VERSION,
        )
    return runtime.provider_result(places, state="COMPLETED" if places else "EMPTY", reason="results" if places else "empty_response", call_ids=(getattr(reservation, "call_id", ""),))


def find_phone_for_website(company: str, website: str) -> str:
    """Return a Places phone only when Places confirms the exact website domain."""
    domain = scorer.normalize_domain(website)
    if not domain:
        return ""
    for place in search_company(company):
        if scorer.normalize_domain(place["website"]) == domain and place.get("phone"):
            return place["phone"]
    return ""
