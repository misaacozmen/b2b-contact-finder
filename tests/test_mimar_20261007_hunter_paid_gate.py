"""Talimat 38: the paid Hunter email fill obeys the paid Hunter switch, and the key tool turns that switch on."""

from __future__ import annotations

import json
import sqlite3

import config
import main
from modules import api_configuration, hunter
from petzoo_fixture_support import ReplayPaidTransport, install_harness, load_fixture, write_input_book


def test_email_gap_fill_follows_the_paid_hunter_switch(monkeypatch):
    monkeypatch.setattr(config, "PAID_ENABLED", True)
    monkeypatch.setattr(config, "ENABLE_HUNTER_EMAIL_GAP_FILL", True)
    monkeypatch.setattr(config, "HUNTER_API_KEY", "synthetic-hunter-key")
    monkeypatch.setattr(config, "ENABLE_HUNTER_DOMAIN_FINDER", False)
    assert not hunter.email_gap_fill_enabled()
    monkeypatch.setattr(config, "ENABLE_HUNTER_DOMAIN_FINDER", True)
    assert hunter.email_gap_fill_enabled()


def test_paid_run_with_the_hunter_switch_off_completes_without_hunter_work(tmp_path, monkeypatch):
    _manifest, fixture, _sha = load_fixture()
    records = [dict(row) for row in fixture if row["group"] == "F"][:3]
    for index, row in enumerate(records):
        row["item_index"] = index
    input_path = tmp_path / "hunter-switch-off.xlsx"
    write_input_book(input_path, records)
    router = ReplayPaidTransport(records, tmp_path / "transport.jsonl")
    runs_dir, _router, _session = install_harness(monkeypatch, tmp_path, records, workers=1, router=router)
    monkeypatch.setattr(config, "ENABLE_HUNTER_DOMAIN_FINDER", False)

    outcome = main.run(input_path, allow_paid=True)

    assert str(getattr(getattr(outcome, "status", None), "value", outcome)) == "COMPLETE"
    run_root = next(runs_dir.iterdir())
    with sqlite3.connect(f"file:{(run_root / 'state' / 'progress.sqlite3').resolve().as_posix()}?mode=ro", uri=True) as db:
        hunter_work = db.execute("SELECT COUNT(*) FROM provider_work_items WHERE provider='hunter'").fetchone()[0]
        hunter_calls = db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='hunter'").fetchone()[0]
        brightdata_calls = db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='brightdata'").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM run_items WHERE paid_state='PENDING'").fetchone()[0]
    assert brightdata_calls > 0
    assert (hunter_work, hunter_calls, pending) == (0, 0, 0)


def test_key_tool_turns_the_hunter_switch_on_but_keeps_a_switch_turned_off(tmp_path, monkeypatch):
    from tools import api_anahtarlari

    monkeypatch.setattr(config, "SAVED_API_KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(config, "RESOLVER_SETTINGS_FILE", tmp_path / "company_resolvers.json")
    secret = "synthetic-key-0123456789abcdef"

    def run(answers):
        replies = iter(answers)
        api_anahtarlari.configure(input_fn=lambda _: next(replies), secret_fn=lambda _: secret)
        return api_configuration.load_resolver_settings().get("hunter_domain_finder")

    assert run(["h", "h", "e"]) is True
    config.RESOLVER_SETTINGS_FILE.write_text(json.dumps({"version": 1, "hunter_domain_finder": False}), encoding="utf-8")
    assert run(["h", "h", "h"]) is False
    assert run(["h", "h", "e"]) is True
    config.RESOLVER_SETTINGS_FILE.unlink()
    assert run(["h", "h", "h"]) is True
