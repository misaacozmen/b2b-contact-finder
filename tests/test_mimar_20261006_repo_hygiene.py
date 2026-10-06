"""Talimat 30: no API key and no private business data in the repository."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from modules import secrets_store

ROOT = Path(__file__).resolve().parents[1]
IGNORE_RULES = (
    ".runtime/", "state/", "/runs/", "/teslim/", "data/truth/", ".env", "input/*.xlsx",
    "/MIMAR_*.md", "/FUAR_KOSU_KILAVUZU.md",
)
PRIVATE_PREFIXES = (".runtime/", "state/", "runs/", "teslim/", "data/truth/", "output/", "outputs/")
GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_-]{35}")


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is not available")
    return [path for path in out.decode("utf-8").split("\0") if path]


def _texts(tracked: list[str]):
    for path in tracked:
        try:
            yield path, (ROOT / path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue


def test_private_folders_are_ignored_and_untracked(tracked):
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert [rule for rule in IGNORE_RULES if rule not in rules] == []
    assert [path for path in tracked if path.startswith(PRIVATE_PREFIXES)] == []
    assert [path for path in tracked if path.startswith("input/") and path != "input/.gitkeep"] == []
    assert [path for path in tracked if Path(path).name == ".env" or Path(path).name.startswith(".env.")] == []


def test_no_google_api_key_in_tracked_files(tracked):
    assert [path for path, text in _texts(tracked) if GOOGLE_KEY.search(text)] == []


def test_no_saved_api_key_in_tracked_files(tracked):
    store = ROOT / "state" / "api_keys.json"
    if not store.is_file():
        pytest.skip("no saved API keys on this machine")
    try:
        saved = secrets_store.decode(json.loads(store.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pytest.skip("saved API keys cannot be read by this user")
    values = [value for value in saved.values() if isinstance(value, str) and len(value.strip()) >= 12]
    leaked = [path for path, text in _texts(tracked) if any(value.strip() in text for value in values)]
    # Only file names are reported; key values are never printed.
    assert leaked == [], f"a saved API key appears in: {leaked}"


def test_api_key_tool_saves_keys_encrypted_and_never_prints_them(capsys):
    import config
    from modules import api_configuration
    from tools import api_anahtarlari

    secret = "synthetic-key-0123456789abcdef"
    answers = iter(["e", "h", "e"])
    result = api_anahtarlari.configure(input_fn=lambda _: next(answers), secret_fn=lambda _: secret)
    assert result == {"brightdata": True, "google_places": False, "hunter": True}
    assert api_configuration.load_saved_api_keys() == {"brightdata": secret, "hunter": secret}
    assert secret not in config.SAVED_API_KEYS_FILE.read_text(encoding="utf-8")
    assert secret not in capsys.readouterr().out
