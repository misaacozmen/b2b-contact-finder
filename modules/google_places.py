"""Optional Google Places API (New) lookup for official website and phone evidence."""

import logging
import json

import requests

import config
from modules import cache_store, runtime, scorer


LOGGER = logging.getLogger("contact_finder")
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id,places.displayName,places.websiteUri,places.internationalPhoneNumber,places.businessStatus"


def is_enabled() -> bool:
    return config.ENABLE_GOOGLE_PLACES and bool(config.GOOGLE_PLACES_API_KEY)


def search_company(company: str) -> list[dict]:
    """Return Google-maintained business records without making them trusted candidates yet."""
    if not is_enabled():
        return []
    variants = scorer.search_name_variants(company)
    query_name = (variants[0] if variants else company).strip()[:100]
    cache_key = json.dumps({"query": query_name, "region": "TR", "fields": FIELD_MASK}, sort_keys=True)
    if config.SEARCH_CACHE_MODE in {"use", "replay"}:
        cached = cache_store.load(
            config.SEARCH_CACHE_DIR, "google_places", cache_key,
            config.SEARCH_CACHE_TTL_DAYS, config.CACHE_SCHEMA_VERSION,
        )
        if cached is not None:
            return cached
        if config.SEARCH_CACHE_MODE == "replay":
            LOGGER.warning("Google Places replay cache miss: %s", company)
            return []
    try:
        if not runtime.reserve_api("google_places", config.GOOGLE_PLACES_REQUEST_BUDGET):
            LOGGER.warning("Google Places run budget exhausted")
            return []
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
    except requests.RequestException as exc:
        LOGGER.warning("Google Places lookup failed for %s: %s", company, exc)
        return []

    places = []
    for place in response.json().get("places", []):
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
    return places


def find_phone_for_website(company: str, website: str) -> str:
    """Return a Places phone only when Places confirms the exact website domain."""
    domain = scorer.normalize_domain(website)
    if not domain:
        return ""
    for place in search_company(company):
        if scorer.normalize_domain(place["website"]) == domain and place.get("phone"):
            return place["phone"]
    return ""
