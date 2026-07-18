"""One-time secure setup for free company-name-to-domain resolvers."""

from __future__ import annotations

import getpass
import json

import config
import main


def _save_settings(values: dict[str, bool]) -> None:
    config.RESOLVER_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "brandfetch_domain_search": bool(values.get("brandfetch_domain_search")),
        "hunter_domain_finder": bool(values.get("hunter_domain_finder")),
    }
    config.RESOLVER_SETTINGS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def configure(input_fn=input, secret_fn=getpass.getpass) -> dict[str, bool]:
    saved = main._load_saved_api_keys()

    brandfetch_active = main._prompt_api_state("Brandfetch Brand Search", input_fn)
    if brandfetch_active:
        config.BRANDFETCH_CLIENT_ID = main._saved_or_prompted_key(
            "Brandfetch Client ID",
            "brandfetch",
            config.BRANDFETCH_CLIENT_ID,
            saved,
            input_fn,
            secret_fn,
        )
        saved["brandfetch"] = config.BRANDFETCH_CLIENT_ID

    hunter_active = main._prompt_api_state("Hunter Domain Finder", input_fn)
    if hunter_active:
        config.HUNTER_API_KEY = main._saved_or_prompted_key(
            "Hunter",
            "hunter",
            config.HUNTER_API_KEY,
            saved,
            input_fn,
            secret_fn,
        )
        saved["hunter"] = config.HUNTER_API_KEY

    if saved:
        main._save_api_keys(saved)
    states = {
        "brandfetch_domain_search": brandfetch_active,
        "hunter_domain_finder": hunter_active,
    }
    _save_settings(states)
    main._apply_saved_resolver_configuration(saved)

    print("\nResolver kurulumu tamamlandi.")
    print(f"Brandfetch: {'aktif' if brandfetch_active else 'deaktif'}")
    print(f"Hunter Domain Finder: {'aktif' if hunter_active else 'deaktif'}")
    print(f"Gizli anahtar kasasi: {config.SAVED_API_KEYS_FILE}")
    print(f"Acik/kapali ayarlari: {config.RESOLVER_SETTINGS_FILE}")
    print("Anahtarlar ekrana veya komut gecmisine yazilmadi.")
    return states


if __name__ == "__main__":
    configure()
