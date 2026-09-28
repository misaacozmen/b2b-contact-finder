"""Erratum 14: Bright Data uses google.com, and the canary retries once after the cooldown."""

from __future__ import annotations

import config
from modules import api_configuration
from tools import brightdata_canary


class _Response:
    def __init__(self, organic: int, headers: dict | None = None):
        self.status_code = 200
        self.headers = dict(headers or {})
        self._organic = organic

    def json(self):
        return {"organic": [{"link": f"https://example{i}.com/"} for i in range(self._organic)]}

    def close(self):
        pass


_CAPTCHA = {"x-brd-error-code": "captcha", "x-brd-error": "redirect location was rejected"}


def _prepare(monkeypatch, responses):
    monkeypatch.setattr(config, "BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setattr(api_configuration, "apply_saved_resolver_configuration", lambda: {})
    urls, sleeps = [], []
    queue = list(responses)

    def fake_post(url, json=None, headers=None, timeout=None):
        urls.append(json["url"])
        return queue.pop(0) if queue else _Response(3)

    monkeypatch.setattr(brightdata_canary.requests, "post", fake_post)
    monkeypatch.setattr(brightdata_canary.time, "sleep", lambda seconds: sleeps.append(seconds))
    return urls, sleeps


def test_brightdata_google_domain_is_google_com():
    assert config.BRIGHTDATA_GOOGLE_DOMAIN == "www.google.com"
    assert config.BRIGHTDATA_GOOGLE_GL == "tr"
    assert config.BRIGHTDATA_GOOGLE_HL == "tr"


def test_canary_retries_retryable_header_error_once_after_cooldown(monkeypatch, capsys):
    urls, sleeps = _prepare(monkeypatch, [_Response(0, _CAPTCHA)])
    assert brightdata_canary.main() == 0
    assert len(urls) == 6
    assert urls[0] == urls[1]
    assert all(url.startswith("https://www.google.com/search?") for url in urls)
    assert sleeps == [config.BRIGHTDATA_FAILED_QUERY_COOLDOWN_SEC]
    out = capsys.readouterr().out
    assert "attempt=1 http=200 x-brd-err-code=captcha organic=0" in out
    assert "organic_queries=5/5 captcha_queries=0/5" in out


def test_canary_does_not_retry_empty_response_without_error_header(monkeypatch, capsys):
    urls, sleeps = _prepare(monkeypatch, [_Response(0) for _ in range(5)])
    assert brightdata_canary.main() == 1
    assert len(urls) == 5
    assert sleeps == []
    assert "organic_queries=0/5 captcha_queries=0/5" in capsys.readouterr().out


def test_canary_counts_captcha_only_when_retry_also_fails(monkeypatch, capsys):
    urls, sleeps = _prepare(monkeypatch, [_Response(0, _CAPTCHA) for _ in range(10)])
    assert brightdata_canary.main() == 1
    assert len(urls) == 10
    assert len(sleeps) == 5
    out = capsys.readouterr().out
    assert "organic_queries=0/5 captcha_queries=5/5" in out
    assert "CAPTCHA_THRESHOLD_REACHED" in out
