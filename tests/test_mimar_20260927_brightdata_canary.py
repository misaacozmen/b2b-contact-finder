"""Erratum 07: the Bright Data canary must read the saved key store like main.py."""

from __future__ import annotations

import config
from modules import api_configuration
from tools import brightdata_canary


_CONFIG_NAMES = (
    "BRIGHTDATA_API_KEY",
    "GOOGLE_PLACES_API_KEY",
    "BRANDFETCH_CLIENT_ID",
    "HUNTER_API_KEY",
    "ENABLE_BRANDFETCH_DOMAIN_SEARCH",
    "ENABLE_HUNTER_DOMAIN_FINDER",
)


class _FakeResponse:
    status_code = 200
    headers: dict = {}

    def json(self):
        return {"organic": [{"link": "https://example.com/"}]}

    def close(self):
        pass


def _isolate_config(monkeypatch, saved):
    for name in _CONFIG_NAMES:
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(config, "BRIGHTDATA_API_KEY", "")
    monkeypatch.setattr(api_configuration, "load_saved_api_keys", lambda: dict(saved))
    monkeypatch.setattr(api_configuration, "load_resolver_settings", lambda: {})


def test_canary_uses_saved_key_store_when_environment_key_is_missing(monkeypatch, capsys):
    _isolate_config(monkeypatch, {"brightdata": "saved-test-key"})
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(headers["Authorization"])
        return _FakeResponse()

    monkeypatch.setattr(brightdata_canary.requests, "post", fake_post)
    assert brightdata_canary.main() == 0
    assert calls == ["Bearer saved-test-key"] * 5
    assert "organic_queries=5/5 captcha_queries=0/5" in capsys.readouterr().out


def test_canary_without_any_key_exits_2_without_http(monkeypatch, capsys):
    _isolate_config(monkeypatch, {})
    calls = []
    monkeypatch.setattr(brightdata_canary.requests, "post", lambda *args, **kwargs: calls.append(1))
    assert brightdata_canary.main() == 2
    assert calls == []
    assert "BRIGHTDATA_API_KEY is not set" in capsys.readouterr().out
