from __future__ import annotations

import getpass
import json
import uuid

import config
from modules import secrets_store


def prompt_api_state(label: str, input_fn=input) -> bool:
    active_answers = {"y", "yes", "aktif", "a", "evet", "e", "1"}
    inactive_answers = {"n", "no", "deaktif", "pasif", "d", "hayir", "hayır", "h", "0"}
    while True:
        answer = input_fn(f"{label} aktif mi? [y/n]: ").strip().casefold()
        if answer in active_answers:
            return True
        if answer in inactive_answers:
            return False
        print("Lütfen 'y' veya 'n' yazın.")


def prompt_use_saved_key(label: str, input_fn=input) -> bool:
    while True:
        answer = input_fn(f"{label}: kayıtlı API anahtarı kullanılsın mı? [y/n]: ").strip().casefold()
        if answer in {"y", "yes", "evet", "e", "1"}:
            return True
        if answer in {"n", "no", "hayir", "hayır", "h", "0"}:
            return False
        print("Lütfen 'y' veya 'n' yazın.")


def prompt_api_key(label: str, secret_fn=getpass.getpass) -> str:
    while True:
        api_key = secret_fn(f"{label} API anahtarı (gizli): ").strip()
        if api_key:
            return api_key
        print("API aktifken anahtar boş bırakılamaz.")


def load_saved_api_keys() -> dict[str, str]:
    try:
        payload = json.loads(config.SAVED_API_KEYS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    try:
        payload = secrets_store.decode(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def save_api_keys(api_keys: dict[str, str]) -> None:
    clean = {key: value for key, value in api_keys.items() if value}
    payload = secrets_store.encode(clean)
    config.SAVED_API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.SAVED_API_KEYS_FILE.with_name(
        f".{config.SAVED_API_KEYS_FILE.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(config.SAVED_API_KEYS_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def load_resolver_settings() -> dict[str, bool]:
    """Load non-secret persisted switches for optional company resolvers."""
    try:
        payload = json.loads(config.RESOLVER_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if key in {"brandfetch_domain_search", "hunter_domain_finder"}
        and isinstance(value, bool)
    }


def apply_saved_resolver_configuration(saved: dict[str, str] | None = None) -> dict[str, bool]:
    """Apply persisted resolver switches and DPAPI-protected credentials.

    Environment-provided credentials remain the first choice. Persisted switches
    only enable discovery resolvers when a corresponding credential is present.
    """
    saved = saved if saved is not None else load_saved_api_keys()
    settings = load_resolver_settings()
    config.GOOGLE_PLACES_API_KEY = config.GOOGLE_PLACES_API_KEY or saved.get("google_places", "")
    config.BRIGHTDATA_API_KEY = config.BRIGHTDATA_API_KEY or saved.get("brightdata", "")
    config.BRANDFETCH_CLIENT_ID = config.BRANDFETCH_CLIENT_ID or saved.get("brandfetch", "")
    config.HUNTER_API_KEY = config.HUNTER_API_KEY or saved.get("hunter", "")

    brandfetch_requested = settings.get(
        "brandfetch_domain_search", config.ENABLE_BRANDFETCH_DOMAIN_SEARCH,
    )
    hunter_requested = settings.get(
        "hunter_domain_finder", config.ENABLE_HUNTER_DOMAIN_FINDER,
    )
    config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = bool(
        brandfetch_requested and config.BRANDFETCH_CLIENT_ID
    )
    config.ENABLE_HUNTER_DOMAIN_FINDER = bool(
        hunter_requested and config.HUNTER_API_KEY
    )
    return {
        "brandfetch_domain_search": config.ENABLE_BRANDFETCH_DOMAIN_SEARCH,
        "hunter_domain_finder": config.ENABLE_HUNTER_DOMAIN_FINDER,
    }


def saved_or_prompted_key(
    label: str,
    key_name: str,
    current_value: str,
    saved: dict[str, str],
    input_fn=input,
    secret_fn=getpass.getpass,
) -> str:
    api_key = current_value or saved.get(key_name, "")
    if api_key and prompt_use_saved_key(label, input_fn):
        print(f"{label}: kayıtlı API anahtarı kullanılacak.")
        return api_key
    if api_key:
        print(f"{label}: yeni API anahtarı girildiğinde kayıtlı anahtar değiştirilecek.")
    api_key = prompt_api_key(label, secret_fn)
    saved[key_name] = api_key
    return api_key


def configure_apis_interactively(input_fn=input, secret_fn=getpass.getpass) -> None:
    saved = load_saved_api_keys()
    resolver_states = apply_saved_resolver_configuration(saved)
    google_active = prompt_api_state("Google Places API", input_fn)
    config.ENABLE_GOOGLE_PLACES = google_active
    config.GOOGLE_PLACES_API_KEY = (
        saved_or_prompted_key(
            "Google Places", "google_places", config.GOOGLE_PLACES_API_KEY, saved, input_fn, secret_fn
        )
        if google_active
        else ""
    )

    brightdata_active = prompt_api_state("Bright Data API", input_fn)
    config.SEARCH_PROVIDER = "brightdata" if brightdata_active else "ddgs"
    config.BRIGHTDATA_API_KEY = (
        saved_or_prompted_key(
            "Bright Data", "brightdata", config.BRIGHTDATA_API_KEY, saved, input_fn, secret_fn
        )
        if brightdata_active
        else ""
    )
    if google_active:
        saved["google_places"] = config.GOOGLE_PLACES_API_KEY
    if brightdata_active:
        saved["brightdata"] = config.BRIGHTDATA_API_KEY
    if saved:
        save_api_keys(saved)

    print(
        "Koşu ayarları: "
        f"Google Places={'aktif' if google_active else 'deaktif'}, "
        f"Bright Data={'aktif' if brightdata_active else 'deaktif'}, "
        f"Brandfetch={'aktif' if resolver_states['brandfetch_domain_search'] else 'deaktif'}, "
        f"Hunter Domain Finder={'aktif' if resolver_states['hunter_domain_finder'] else 'deaktif'}"
    )


# Compatibility aliases with leading underscore
_prompt_api_state = prompt_api_state
_prompt_use_saved_key = prompt_use_saved_key
_prompt_api_key = prompt_api_key
_load_saved_api_keys = load_saved_api_keys
_save_api_keys = save_api_keys
_load_resolver_settings = load_resolver_settings
_apply_saved_resolver_configuration = apply_saved_resolver_configuration
_saved_or_prompted_key = saved_or_prompted_key


