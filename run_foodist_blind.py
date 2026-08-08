from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "input" / "foodist_expo_turkiye_blind_226.xlsx"
OUTPUT = ROOT / "output" / "foodist_expo_turkiye_blind"
STATE = ROOT / "state" / "foodist_expo_turkiye_blind"

BRIGHTDATA_BUDGET = 2_000
GOOGLE_PLACES_BUDGET = 226
BRANDFETCH_BUDGET = 226
HUNTER_DOMAIN_BUDGET = 226
DDGS_FALLBACK_BUDGET = 500
CRAWLER_BUDGET = 8_000

for offset in (
    "BRIGHTDATA_REQUEST_OFFSET",
    "GOOGLE_PLACES_REQUEST_OFFSET",
    "BRANDFETCH_REQUEST_OFFSET",
    "HUNTER_REQUEST_OFFSET",
):
    os.environ[offset] = "0"

import config
import main


def configure_blind_run(state_dir: Path = STATE) -> None:
    """Use only company/country input and fresh run-local evidence."""
    # Credentials are reusable configuration, not evidence. Load them before
    # switching every cache/registry path to this fresh blind run.
    saved_keys = main._load_saved_api_keys()
    required_keys = {
        "brightdata": "Bright Data",
        "google_places": "Google Places",
        "brandfetch": "Brandfetch",
        "hunter": "Hunter",
    }
    missing = [label for key, label in required_keys.items() if not saved_keys.get(key)]
    if missing:
        raise SystemExit("Eksik kayıtlı API anahtarı: " + ", ".join(missing))

    main._set_run_state_dir(state_dir)

    config.SEARCH_PROVIDER = "brightdata"
    config.SEARCH_CACHE_MODE = "use"
    config.CRAWL_CACHE_MODE = "use"
    config.REPLAY_SNAPSHOT_INPUT = None

    # Do not reuse aliases, registries, entity memory, API keys, or resolver
    # choices learned from the fair-backed runs.
    config.COMPANY_ALIASES_FILE = state_dir / "company_aliases.json"
    config.ENTITY_REGISTRY_FILE = state_dir / "entity_registry.json"
    config.OFFICIAL_REGISTRY_FILE = state_dir / "official_registry.json"
    config.VERIFIED_ENTITY_MEMORY_FILE = state_dir / "verified_entity_memory.jsonl"
    config.SAVED_API_KEYS_FILE = state_dir / "api_keys.json"
    config.RESOLVER_SETTINGS_FILE = state_dir / "company_resolvers.json"

    config.BRIGHTDATA_API_KEY = saved_keys["brightdata"]
    config.GOOGLE_PLACES_API_KEY = saved_keys["google_places"]
    config.BRANDFETCH_CLIENT_ID = saved_keys["brandfetch"]
    config.HUNTER_API_KEY = saved_keys["hunter"]
    config.ENABLE_GOOGLE_PLACES = True
    config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = True
    config.ENABLE_HUNTER_DOMAIN_FINDER = True
    # Hunter e-mail results are third-party data. Keep them out of publication;
    # use Hunter only to discover a candidate company domain.
    config.ENABLE_HUNTER_FALLBACK = False
    config.BRIGHTDATA_REQUEST_BUDGET = BRIGHTDATA_BUDGET
    config.GOOGLE_PLACES_REQUEST_BUDGET = GOOGLE_PLACES_BUDGET
    config.BRANDFETCH_REQUEST_BUDGET = BRANDFETCH_BUDGET
    config.HUNTER_REQUEST_BUDGET = HUNTER_DOMAIN_BUDGET

    # Let the core size fallback capacity from the actual input row count.
    config.SEARCH_HTTP_REQUEST_BUDGET = 0
    config.CRAWLER_HTTP_REQUEST_BUDGET = CRAWLER_BUDGET
    config.MAX_WORKERS = 24
    # Bounded rendering is required for first-party SPA contact pages. The
    # browser request guard keeps active data on the official domain.
    config.ENABLE_JS_FALLBACK = True
    config.ENABLE_JS_PROFILE_FALLBACK = False
    config.MAX_BROWSER_RENDER_WORKERS = 1
    config.GLOBAL_REQUESTS_PER_SECOND = 8.0
    config.MIN_DELAY_SEC = 0
    config.MAX_DELAY_SEC = 0
    config.MAX_SEARCH_BRIDGE_FETCHES = 0


def main_cli() -> None:
    parser = argparse.ArgumentParser(
        description="Foodist 226 firma için fuar referanssız kör arama",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Bütçe özetini kabul edip canlı koşuyu başlat",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Eski cache'i kullanmadan zaman damgalı yeni state/output ile koş",
    )
    args = parser.parse_args()

    output_dir = OUTPUT
    state_dir = STATE
    if args.fresh:
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = OUTPUT.with_name(f"{OUTPUT.name}_{suffix}")
        state_dir = STATE.with_name(f"{STATE.name}_{suffix}")

    if not INPUT.exists():
        raise SystemExit(f"Input bulunamadı: {INPUT}")

    print("Foodist kör arama preflight")
    print(f"Input: {INPUT}")
    print(f"Output: {output_dir}")
    print(f"State/cache: {state_dir}")
    print(f"Bright Data arama bütçesi: {BRIGHTDATA_BUDGET}")
    print(f"Google Places bütçesi: {GOOGLE_PLACES_BUDGET}")
    print(f"Brandfetch domain discovery bütçesi: {BRANDFETCH_BUDGET}")
    print(f"Hunter Domain Finder bütçesi: {HUNTER_DOMAIN_BUDGET}")
    print(f"DDGS ücretsiz fallback bütçesi: {DDGS_FALLBACK_BUDGET}")
    print(f"Resmî-site crawler HTTP bütçesi: {CRAWLER_BUDGET}")
    print("Browser/Playwright render: kapalı")
    print("Hunter e-posta fallback: kapalı (yalnız domain discovery)")
    print("Ücretli API çağrı üst sınırı: 2678")
    print("Parasal maliyet: sağlayıcı hesap tarifelerine bağlı")

    if not args.yes:
        raise SystemExit(
            "Canlı koşu başlatılmadı. Başlatmak için aynı komuta --yes ekleyin.",
        )

    configure_blind_run(state_dir)
    print(main.run(INPUT, output_dir))


if __name__ == "__main__":
    main_cli()
