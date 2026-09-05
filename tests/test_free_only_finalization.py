from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import config
import main
from modules import checkpoint, run_context


def _seed_free_run(db: Path) -> None:
    items = [
        {"item_index": 0, "source_record_id": "source:0", "free_state": "DONE", "paid_required": True, "paid_state": "PENDING"},
        {"item_index": 1, "source_record_id": "source:1", "free_state": "DONE", "paid_required": False, "paid_state": "NOT_REQUIRED"},
    ]
    results = [
        {"item_index": 0, "payload": json.dumps({"source_record_id": "source:0", "publication_eligible": False, "publication_blockers": "review"})},
        {"item_index": 1, "payload": json.dumps({"source_record_id": "source:1", "publication_eligible": False})},
    ]
    checkpoint.seed_recovered_run(
        path=db, run_id="free-only", input_hash="h", run_signature="s",
        context={"phase": "FREE"},
        budgets={provider: 0 for provider in checkpoint.CANONICAL_PROVIDERS},
        items=items, results=results,
    )


def test_free_only_mode_is_in_run_config_and_paid_is_forced_off():
    with patch.object(config, "PAID_ENABLED", False, create=True):
        cfg = run_context.RunConfig.from_config(paid_enabled=False, free_only_finalization=True)
    assert cfg.free_only_finalization is True
    assert cfg.paid_enabled is False
    assert cfg.as_dict()["free_only_finalization"] is True
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_context.RunConfig.from_config(paid_enabled=True, free_only_finalization=True)


def test_free_only_finalization_atomically_skips_recommended_paid_queue(tmp_path: Path):
    db = tmp_path / "state.sqlite3"
    with patch.object(config, "PROGRESS_DB_FILE", db):
        _seed_free_run(db)
        result = checkpoint.finalize_free_only_queue("free-only")
        assert result == {"paid_recommended": 1, "paid_provider_calls": 0}
        items = checkpoint.load_run_items("free-only")
        rows = checkpoint.load_results_by_id("free-only")
    assert all(not item["paid_required"] and item["paid_state"] == "NOT_REQUIRED" for item in items)
    assert rows[0]["paid_recommended"] is True
    assert rows[0]["paid_skipped_reason"] == "disabled_by_explicit_free_only_finalization"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paid_attempts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 0


def test_cli_mode_conflict_is_rejected():
    with pytest.raises(SystemExit, match="mutually exclusive"):
        main.resolve_cli_run_config(["--allow-paid", "--finalize-without-paid"])


def test_default_no_allow_paid_does_not_become_free_only():
    args = main.parse_args(["--no-allow-paid"])
    assert args.allow_paid is False
    assert args.finalize_without_paid is False
