"""Talimat 17 Erratum 1: fetch attempts ignore surrounding whitespace in page URLs."""

from __future__ import annotations

import config
from modules import checkpoint, crawler, runtime


def test_fetch_attempt_ignores_surrounding_whitespace_in_url(tmp_path, monkeypatch):
    database = tmp_path / "attempts.sqlite3"
    monkeypatch.setattr(config, "PROGRESS_DB_FILE", database)
    checkpoint.initialize_schema(database)
    checkpoint.initialize_run_items("run-a", [{"source_record_id": "source:target"}])
    monkeypatch.setattr(runtime, "durable_run_id", lambda: "run-a")
    monkeypatch.setattr(runtime, "current_source_record_id", lambda: "source:target")
    for url in ("https://ornek.com.tr/franchise", "https://ornek.com.tr/franchise ", " https://ornek.com.tr/franchise"):
        crawler._record_fetch_attempt(
            url, profile="full", transport_outcome="CACHE_HIT", semantic_result="FAILED", reason="http_404",
        )
    rows = checkpoint.load_discovery_attempts("run-a")
    assert [row["candidate_url"] for row in rows] == ["https://ornek.com.tr/franchise"]
