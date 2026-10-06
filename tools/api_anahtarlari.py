"""Save the paid API keys once: Bright Data, Google Places and Hunter (Talimat 30).

Keys are typed hidden and stored encrypted for this Windows user in
state/api_keys.json (DPAPI); they never appear on screen, in logs or in git.
Usage:  .runtime\\python3147-sqlite3534\\python.exe tools\\api_anahtarlari.py
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from modules import api_configuration  # noqa: E402

PROVIDERS = (
    ("brightdata", "Bright Data (SERP API)"),
    ("google_places", "Google Places (Places API - New)"),
    ("hunter", "Hunter"),
)
YES = {"e", "evet", "y", "yes"}
NO = {"h", "hayir", "hayır", "n", "no", ""}


def _ask(question: str, input_fn) -> bool:
    while True:
        answer = input_fn(f"{question} [e/h]: ").strip().casefold()
        if answer in YES:
            return True
        if answer in NO:
            return False
        print("Lütfen 'e' veya 'h' yazın.")


def configure(input_fn=input, secret_fn=getpass.getpass) -> dict[str, bool]:
    """Ask for each provider; return which providers have a saved key afterwards."""
    saved = api_configuration.load_saved_api_keys()
    changed = False
    for name, label in PROVIDERS:
        state = "kayıtlı" if saved.get(name) else "kayıtlı değil"
        question = f"{label}: anahtar {state}. Yeni anahtar girilsin mi?"
        if not _ask(question, input_fn):
            continue
        saved[name] = api_configuration.prompt_api_key(label, secret_fn)
        changed = True
    if changed:
        api_configuration.save_api_keys(saved)
    result = {name: bool(saved.get(name)) for name, _ in PROVIDERS}
    print(f"Anahtar kasası: {config.SAVED_API_KEYS_FILE}")
    for name, label in PROVIDERS:
        print(f"  {label}: {'kayıtlı' if result[name] else 'yok'}")
    print(f"Bright Data zone adı: {config.BRIGHTDATA_ZONE} (farklıysa BRIGHTDATA_ZONE ortam değişkeniyle değiştirin)")
    return result


if __name__ == "__main__":
    configure()
