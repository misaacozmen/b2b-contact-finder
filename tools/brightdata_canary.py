"""Five-query paid Bright Data canary; run only after explicit user approval."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from urllib.parse import quote_plus

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from modules import api_configuration, search


QUERIES = (
    "airpak havalandırma filtre",
    "adeko grup bilişim",
    "akdiş havalandırma",
    "çetinmak makine",
    "abm makine taşlama",
)


def _organic_count(payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    for key in ("organic", "organic_results", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _attempt(query: str, headers: dict, attempt: int) -> tuple[int, bool, bool]:
    """One physical call; returns (organic count, retryable header error, captcha)."""
    search_url = (
        f"https://{config.BRIGHTDATA_GOOGLE_DOMAIN}/search"
        f"?q={quote_plus(query)}&hl={config.BRIGHTDATA_GOOGLE_HL}"
        f"&gl={config.BRIGHTDATA_GOOGLE_GL}&brd_json=1"
    )
    payload = {
        "zone": config.BRIGHTDATA_ZONE,
        "url": search_url,
        "format": "raw",
        "country": config.BRIGHTDATA_COUNTRY,
    }
    try:
        response = requests.post(
            config.BRIGHTDATA_ENDPOINT, json=payload, headers=headers,
            timeout=config.BRIGHTDATA_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        print(f"query={query} attempt={attempt} http=ERROR x-brd-err-code=- organic=0 error={type(exc).__name__}")
        return 0, False, False
    try:
        has_error, code, message, retryable = search._brightdata_header_error(response)
        try:
            response_payload = response.json()
        except (ValueError, json.JSONDecodeError):
            response_payload = {}
        organic = _organic_count(response_payload)
        print(f"query={query} attempt={attempt} http={response.status_code} x-brd-err-code={code or '-'} organic={organic}")
        return organic, has_error and retryable, "captcha" in f"{code} {message}".casefold()
    finally:
        response.close()


def main() -> int:
    api_configuration.apply_saved_resolver_configuration()
    if not config.BRIGHTDATA_API_KEY:
        print("BRIGHTDATA_API_KEY is not set (ortam değişkeni ve kayıtlı anahtar deposu boş)")
        return 2
    headers = {
        "Authorization": f"Bearer {config.BRIGHTDATA_API_KEY}",
        "Content-Type": "application/json",
    }
    successes = 0
    captcha_count = 0
    for query in QUERIES:
        for attempt in (1, 2):
            organic, retryable, captcha = _attempt(query, headers, attempt)
            if organic > 0 or not retryable or attempt == 2:
                break
            # Bright Data blocks a query that just failed for at least 15 s.
            time.sleep(config.BRIGHTDATA_FAILED_QUERY_COOLDOWN_SEC)
        successes += int(organic > 0)
        captcha_count += int(captcha)
    print(f"organic_queries={successes}/5 captcha_queries={captcha_count}/5")
    if captcha_count >= 2:
        print("CAPTCHA_THRESHOLD_REACHED: stop and report; do not change zone/domain settings")
    return 0 if successes >= 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
