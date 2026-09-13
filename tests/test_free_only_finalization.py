from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from openpyxl import Workbook

import config
import main
from modules import checkpoint, pipeline_runner, runtime, run_context
from tools import offline_replay_runner
from test_search_phase_regressions import _input_book, _real_run_setup


def test_zero_call_unknown_is_typed_finalization_invariant_and_cli_22(tmp_path, monkeypatch):
    db = tmp_path / "zero-call.sqlite3"
    from test_search_phase_regressions import init_run

    init_run(db)
    runtime.set_item_context(0, "paid")
    checkpoint.begin_paid_attempt(run_id="run", item_index=0, attempt_number=1)
    checkpoint.record_paid_attempt(
        run_id="run", item_index=0, attempt_number=1,
        result="UNKNOWN", reason="candidate_not_found",
    )
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE run_items SET paid_state='UNKNOWN' WHERE run_id='run' AND item_index=0")
        connection.commit()
    with pytest.raises(checkpoint.OutcomeInvariant, match="zero-call UNKNOWN"):
        checkpoint.validate_paid_evidence("run")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_calls WHERE run_id='run'").fetchone()[0] == 0

    args = argparse.Namespace(
        run_dir=None, resume_run=None, only_status="", from_run_manifest=None,
        non_interactive=True, input=Path("x"), allow_paid=False,
        search_cache=None, crawl_cache=None, brightdata_budget=None,
        google_places_budget=None, hunter_budget=None, brandfetch_budget=None,
        linkedin_company_budget=None, llm_budget=None, rerank_cache=False,
        companies="", replay_snapshot=None, replay_manifest=None,
        finalize_without_paid=None,
    )
    monkeypatch.setattr(main, "parse_args", lambda *_: args)
    monkeypatch.setattr(main, "_ensure_safe_project_runtime", lambda *_: None)
    monkeypatch.setattr(main, "_apply_cli_options", lambda *_: None)
    monkeypatch.setattr(main, "resolve_cli_run_config", lambda *_: None)
    monkeypatch.setattr(main, "_apply_saved_resolver_configuration", lambda: None)
    monkeypatch.setattr(main, "run", lambda *_a, **_k: (_ for _ in ()).throw(checkpoint.OutcomeInvariant("zero-call UNKNOWN")))
    assert main.cli([]) == 22


def test_free_only_fresh_run_completes_without_paid_activity(tmp_path, monkeypatch):
    source = tmp_path / "input.xlsx"
    _input_book(source, ["PAID CO"])
    runs = _real_run_setup(tmp_path, monkeypatch, transport=type("NoPaidTransport", (), {"__call__": lambda self, _envelope: (_ for _ in ()).throw(AssertionError("paid transport called")), "journal": []})())

    outcome = main.run(source, allow_paid=False, finalize_without_paid=True)
    root = next(runs.iterdir())
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert outcome.status is pipeline_runner.PipelineOutcomeStatus.COMPLETE
    assert manifest["phase"] == "COMPLETE"
    assert manifest["complete"] is True
    assert manifest["finalized"] is True
    assert manifest["status"] == "complete_free_only"
    assert manifest["paid_enabled"] is False
    assert manifest["run_config"]["finalize_without_paid"] is True
    assert set(manifest["run_config"]["budgets"].values()) == {0}
    with sqlite3.connect(root / "state" / "progress.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM paid_attempts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM run_items WHERE paid_required=0 AND paid_state='NOT_REQUIRED'").fetchone()[0] == 1
        payload = json.loads(connection.execute("SELECT payload FROM results WHERE item_index=0").fetchone()[0])
    assert payload["paid_required"] is False
    assert payload["paid_state"] == "NOT_REQUIRED"
    assert payload["paid_recommended"] is True
    assert payload["paid_skipped_reason"] == "disabled_by_explicit_free_only_finalization"


def test_free_only_crash_resume_is_idempotent(tmp_path, monkeypatch):
    source = tmp_path / "input.xlsx"
    _input_book(source, ["PAID CO"])
    runs = _real_run_setup(tmp_path, monkeypatch, transport=type("NoPaidTransport", (), {"__call__": lambda self, _envelope: (_ for _ in ()).throw(AssertionError("paid transport called")), "journal": []})())
    original_writer = main._write_outputs
    crashed = {"value": True}

    def crash_once(*args, **kwargs):
        if crashed["value"]:
            crashed["value"] = False
            raise RuntimeError("injected finalization crash")
        return original_writer(*args, **kwargs)

    monkeypatch.setattr(main, "_write_outputs", crash_once)
    with pytest.raises(RuntimeError, match="injected finalization crash"):
        main.run(source, allow_paid=False, finalize_without_paid=True)
    root = next(runs.iterdir())
    monkeypatch.setattr(main, "_write_outputs", original_writer)
    resumed = main.run(source, allow_paid=False, finalize_without_paid=True, resume_run=root)
    verified = main.run(source, allow_paid=False, finalize_without_paid=True, resume_run=root)
    assert resumed.status in {
        pipeline_runner.PipelineOutcomeStatus.COMPLETE,
        pipeline_runner.PipelineOutcomeStatus.FINALIZATION_RESUME_RECONCILED,
    }
    assert verified.status is pipeline_runner.PipelineOutcomeStatus.COMPLETE_RESUME_VERIFIED
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is True and manifest["status"] == "complete_free_only"
    with sqlite3.connect(root / "state" / "progress.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM paid_attempts").fetchone()[0] == 0


def test_allow_paid_and_finalize_without_paid_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        main.parse_args(["--allow-paid", "--finalize-without-paid"])


def test_offline_replay_command_is_explicitly_free_only_and_has_no_network(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    input_path.write_bytes(b"golden-input")
    snapshot = tmp_path / "replay_snapshot.json.gz"
    with gzip.open(snapshot, "wt", encoding="utf-8") as handle:
        json.dump({"entries": [], "entry_count": 0}, handle)
    package = tmp_path / "package"
    package.mkdir()
    with patch.object(config, "SEARCH_CACHE_MODE", "refresh"), patch.object(
        config, "CRAWL_CACHE_MODE", "refresh"
    ):
        run_config = run_context.RunConfig.from_config(
            paid_enabled=False,
            budgets={provider: 0 for provider in ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")},
            finalize_without_paid=True,
        ).as_dict()
    package_manifest = {
        "run_config": run_config,
        "input": {"sha256": hashlib.sha256(input_path.read_bytes()).hexdigest()},
        "replay": {"snapshot": snapshot.name, "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()},
    }
    package_manifest["config_sha256"] = run_context.RunConfig.from_dict(run_config).sha256
    (package / "package_manifest.json").write_text(json.dumps(package_manifest), encoding="utf-8")
    snapshot.replace(package / snapshot.name)
    receipt_path = tmp_path / "receipt.json"
    captured: list[str] = []
    monkeypatch.setattr(offline_replay_runner.sys, "addaudithook", lambda _hook: None)
    monkeypatch.setattr(offline_replay_runner, "runpy", type("Runpy", (), {"run_path": staticmethod(lambda _path, run_name=None: captured.extend(__import__("sys").argv))})())
    result = offline_replay_runner.run(tmp_path, input_path, package, receipt_path)
    assert result["replay_network_events"] == 0
    assert "--allow-paid" not in captured
    assert "--no-allow-paid" in captured
    assert "--finalize-without-paid" in captured
