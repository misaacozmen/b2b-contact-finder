"""Five-query paid Bright Data canary; run only after explicit user approval."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.parse import quote_plus

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from modules import api_configuration


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
            try:
                code = str(response.headers.get("x-brd-err-code", "") or "-")
                error_text = " ".join(str(response.headers.get(key, "") or "") for key in (
                    "x-brd-err-code", "x-brd-error", "proxy-status",
                )).casefold()
                if "captcha" in error_text:
                    captcha_count += 1
                try:
                    response_payload = response.json()
                except (ValueError, json.JSONDecodeError):
                    response_payload = {}
                organic = _organic_count(response_payload)
                successes += int(organic > 0)
                print(f"query={query} http={response.status_code} x-brd-err-code={code} organic={organic}")
            finally:
                response.close()
        except requests.RequestException as exc:
            print(f"query={query} http=ERROR x-brd-err-code=- organic=0 error={type(exc).__name__}")
    print(f"organic_queries={successes}/5 captcha_queries={captcha_count}/5")
    if captcha_count >= 2:
        print("CAPTCHA_THRESHOLD_REACHED: stop and report; do not change zone/domain settings")
    return 0 if successes >= 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
