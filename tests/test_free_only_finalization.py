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
from tools.free_only_contract import (
    classify_replay_receipt, compare_run_configs, inspect_budget_config,
    validate_manifest_config_sha,
)
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
    assert result["schema_version"] == 2
    assert result["budget_check_completed"] is False
    assert result["paid_budget_nonzero"] is False
    assert result["failure_reason"] == "COMPLETE_RUN_MISSING"


def test_runner_ignores_preexisting_manifestless_run_directory(tmp_path):
    runs = tmp_path / "runs"
    existing = runs / "existing"
    new = runs / "new"
    existing.mkdir(parents=True)
    before = {path.name for path in runs.iterdir() if path.is_dir()}
    (existing / "manifest.json").write_text(json.dumps({"input_sha256": "a" * 64}), encoding="utf-8")
    new.mkdir()
    (new / "manifest.json").write_text(json.dumps({"input_sha256": "a" * 64}), encoding="utf-8")
    assert offline_replay_runner._complete_run(tmp_path, "a" * 64, before) == new


def test_classifier_rejects_malformed_and_contradictory_observations():
    activity = {
        "provider_calls": 0, "paid_attempts": 0, "paid_attempt_calls": 0,
        "provider_query_flights": 0, "flight_consumers": 0, "paid_budget_blocks": 0,
    }
    database = {
        "check_completed": True, "budget_check_completed": True,
        "paid_activity": activity, "paid_activity_nonzero": False,
        "paid_provider_calls": 0, "paid_budget_nonzero": False,
        "budget_offenders": [], "database_validation_failed": False,
    }
    budget = {
        "budget_check_completed": True, "paid_budget_nonzero": False,
        "budget_offenders": [], "config_invalid_details": [],
    }
    clean = {
        "threshold_mismatch": False, "threshold_mismatch_details": [],
        "config_mismatch": False, "config_mismatch_details": [],
    }
    cases = [
        (dict(clean, threshold_mismatch="false"), database, budget, "CONFIG_INVALID"),
        (clean, dict(database, database_validation_failed="true"), budget, "DATABASE_VALIDATION_FAILED"),
        (dict(clean, threshold_mismatch=False, threshold_mismatch_details=[{"field": "x"}]), database, budget, "CONFIG_INVALID"),
        (clean, database, dict(budget, budget_offenders=[{"provider": "llm", "value": 1}]), "PAID_BUDGET_NONZERO"),
    ]
    for threshold_info, db, budget_check, expected in cases:
        result = classify_replay_receipt(
            budget_checks=[budget_check], threshold_info=threshold_info, database=db,
            run_present=True, pipeline_exit_code=0, network_event_count=0,
        )
        assert result["status"] == "FAIL"
        assert result["replay_evidence_eligible"] is False
        assert result["failure_reason"] == expected
        assert result["failure_reasons"]
        assert result["evidence_class"] == "REPLAY_FAILURE"


def test_offline_replay_runconfig_invariant_still_writes_config_invalid_receipt(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    input_path.write_bytes(b"golden-input")
    snapshot = tmp_path / "replay_snapshot.json.gz"
    with gzip.open(snapshot, "wt", encoding="utf-8") as handle:
        json.dump({"entries": [], "entry_count": 0}, handle)
    package = tmp_path / "package"
    package.mkdir()
    with patch.object(config, "SEARCH_CACHE_MODE", "refresh"), patch.object(config, "CRAWL_CACHE_MODE", "refresh"):
        run_config = run_context.RunConfig.from_config(
            paid_enabled=False,
            budgets={provider: 0 for provider in ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")},
            finalize_without_paid=True,
        ).as_dict()
    package_manifest = {
        "run_config": run_config,
        "config_sha256": run_context.RunConfig.from_dict(run_config).sha256,
        "input": {"sha256": hashlib.sha256(input_path.read_bytes()).hexdigest()},
        "replay": {"snapshot": snapshot.name, "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()},
    }
    (package / "package_manifest.json").write_text(json.dumps(package_manifest), encoding="utf-8")
    snapshot.replace(package / snapshot.name)
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(offline_replay_runner.sys, "addaudithook", lambda _hook: None)
    monkeypatch.setattr(offline_replay_runner, "validate_manifest_config_sha", lambda _manifest: (_ for _ in ()).throw(checkpoint.SchedulerInvariantError("injected")))
    monkeypatch.setattr(offline_replay_runner, "runpy", type("Runpy", (), {"run_path": staticmethod(lambda *_args, **_kwargs: None)})())
    result = offline_replay_runner.run(tmp_path, input_path, package, receipt_path)
    assert receipt_path.is_file()
    assert result["failure_reason"] == "CONFIG_INVALID"
    assert result["evidence_class"] == "REPLAY_FAILURE"
    assert "SchedulerInvariantError" in result["exception"]


def test_budget_classification_is_independent_of_exception_and_activity():
    base = {"budgets": {provider: 0 for provider in ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")}}
    assert inspect_budget_config(base)["paid_budget_nonzero"] is False
    assert inspect_budget_config({"budgets": {**base["budgets"], "llm": 1}})["paid_budget_nonzero"] is True
    for malformed in (True, -1, "1", None):
        result = inspect_budget_config({"budgets": {**base["budgets"], "llm": malformed}})
        assert result["paid_budget_nonzero"] is False
        assert result["budget_offenders"] == []
        assert result["failure_reason"] == "CONFIG_INVALID"
    missing = inspect_budget_config({"budgets": {key: value for key, value in base["budgets"].items() if key != "llm"}})
    assert missing["failure_reason"] == "CONFIG_INVALID"
    assert missing["budget_offenders"] == []
    mixed = inspect_budget_config({"budgets": {**base["budgets"], "llm": 1, "hunter": "bad"}})
    assert mixed["paid_budget_nonzero"] is True
    assert mixed["budget_offenders"] == [{"provider": "llm", "value": 1, "source": "run_config.budgets"}]
    assert mixed["failure_reason"] == "CONFIG_INVALID"


def test_replay_classification_priority_preserves_independent_flags():
    zero_db = {
        "check_completed": True,
        "budget_check_completed": True,
        "paid_activity": {
            "provider_calls": 0, "paid_attempts": 0, "paid_attempt_calls": 0,
            "provider_query_flights": 0, "flight_consumers": 0, "paid_budget_blocks": 0,
        },
        "paid_activity_nonzero": False,
        "paid_provider_calls": 0,
        "paid_budget_nonzero": False,
        "budget_offenders": [],
        "database_validation_failed": False,
    }
    zero_budget = {
        "budget_check_completed": True, "paid_budget_nonzero": False,
        "budget_offenders": [], "config_invalid_details": [],
    }
    thresholds = {"threshold_mismatch": True, "threshold_mismatch_details": [{"field": "x"}], "config_mismatch": False, "config_mismatch_details": []}
    result = classify_replay_receipt(
        budget_checks=[zero_budget], threshold_info=thresholds, database=zero_db,
        run_present=True, pipeline_exit_code=0, network_event_count=0,
    )
    assert result["failure_reason"] == "REJECTED_THRESHOLD_CANDIDATE"
    assert result["evidence_class"] == "REJECTED_THRESHOLD_CANDIDATE"
    failing = dict(zero_db, paid_activity_nonzero=True, paid_budget_nonzero=True)
    result = classify_replay_receipt(
        budget_checks=[{
            "budget_check_completed": True, "paid_budget_nonzero": True,
            "budget_offenders": [{"provider": "llm", "value": 1}],
            "config_invalid_details": [],
        }],
        threshold_info=dict(thresholds, config_mismatch=True), database=failing,
        run_present=False, pipeline_exit_code=7, network_event_count=1,
        config_invalid=True, config_invalid_details=[{"field": "llm", "reason": "invalid_value"}],
    )
    assert result["failure_reason"] == "PAID_BUDGET_NONZERO"
    assert result["threshold_mismatch"] is True
    assert result["config_mismatch"] is True
    assert result["paid_activity_nonzero"] is True
    assert result["evidence_class"] == "REPLAY_FAILURE"
    zero_budget_activity = classify_replay_receipt(
        budget_checks=[zero_budget], threshold_info=thresholds,
        database=dict(zero_db, paid_activity_nonzero=True), run_present=True,
        pipeline_exit_code=0, network_event_count=0,
    )
    assert zero_budget_activity["failure_reason"] == "PAID_ACTIVITY_NONZERO"
    assert zero_budget_activity["paid_budget_nonzero"] is False
    incomplete = classify_replay_receipt(
        budget_checks=[dict(zero_budget, budget_check_completed=False)], threshold_info={
            "threshold_mismatch": False, "threshold_mismatch_details": [],
            "config_mismatch": False, "config_mismatch_details": [],
        },
        database=zero_db, run_present=True, pipeline_exit_code=0, network_event_count=0,
    )
    assert incomplete["status"] == "FAIL"
    assert incomplete["replay_evidence_eligible"] is False
    config_error = classify_replay_receipt(
        budget_checks=[zero_budget], threshold_info={
            "threshold_mismatch": False, "threshold_mismatch_details": [],
            "config_mismatch": False, "config_mismatch_details": [],
        }, database=zero_db,
        run_present=True, pipeline_exit_code=0, network_event_count=0,
        config_validation_error="ResumeInvariant:injected",
    )
    assert config_error["config_invalid"] is True
    assert config_error["failure_reason"] == "CONFIG_INVALID"


def test_non_threshold_config_drift_is_not_a_threshold_candidate():
    expected = {
        "search_cache_mode": "replay", "crawl_cache_mode": "replay", "budgets": {},
        "thresholds": {"MEDIUM_CONFIDENCE_SCORE": 75},
        "effective_settings": {"publication_policy_min_safety_score": 75, "review_score": 60},
    }
    actual = json.loads(json.dumps(expected))
    actual["crawl_cache_mode"] = "refresh"
    result = compare_run_configs(expected, actual)
    assert result["threshold_mismatch"] is False
    assert result["config_mismatch"] is True


def test_threshold_drift_is_not_config_or_budget_drift():
    expected = {
        "search_cache_mode": "replay", "crawl_cache_mode": "replay", "budgets": {},
        "thresholds": {"MEDIUM_CONFIDENCE_SCORE": 75},
        "effective_settings": {"publication_policy_min_safety_score": 75, "review_score": 60},
    }
    actual = json.loads(json.dumps(expected))
    for score in (70, 72):
        actual["effective_settings"]["publication_policy_min_safety_score"] = score
        result = compare_run_configs(expected, actual)
        assert result["threshold_mismatch"] is True
        assert result["config_mismatch"] is False


def test_run_config_comparison_is_recursive_and_exact_type():
    expected = {
        "thresholds": {"MEDIUM_CONFIDENCE_SCORE": 75},
        "effective_settings": {
            "publication_policy_min_safety_score": 75,
            "enable_llm": True,
            "nested": {"value": 1},
        },
    }
    threshold = json.loads(json.dumps(expected))
    threshold["effective_settings"]["publication_policy_min_safety_score"] = 70
    assert compare_run_configs(expected, threshold) == {
        "threshold_mismatch": True,
        "threshold_mismatch_details": [{
            "field": "run_config.effective_settings.publication_policy_min_safety_score",
            "expected": 75,
            "actual": 70,
        }],
        "config_mismatch": False,
        "config_mismatch_details": [],
    }

    for mutate in (
        lambda value: value["thresholds"].update(MEDIUM_CONFIDENCE_SCORE=75.0),
        lambda value: value["effective_settings"].update(enable_llm=1),
        lambda value: value["effective_settings"].update(nested={"value": 1.0}),
        lambda value: value["effective_settings"].pop("enable_llm"),
        lambda value: value["effective_settings"].update(enable_llm=None),
        lambda value: value.update(thresholds=[]),
    ):
        actual = json.loads(json.dumps(expected))
        mutate(actual)
        result = compare_run_configs(expected, actual)
        assert result["threshold_mismatch"] is False
        assert result["config_mismatch"] is True


def test_dotted_config_key_cannot_be_reclassified_as_threshold():
    expected = {"effective_settings.publication_policy_min_safety_score": 75}
    actual = {"effective_settings.publication_policy_min_safety_score": 70}
    result = compare_run_configs(expected, actual)
    assert result["threshold_mismatch"] is False
    assert result["config_mismatch"] is True


def test_manifest_config_requires_exact_round_trip_and_effective_types():
    with patch.object(config, "SEARCH_CACHE_MODE", "refresh"), patch.object(config, "CRAWL_CACHE_MODE", "refresh"):
        run_config = run_context.RunConfig.from_config(
            paid_enabled=False,
            budgets={provider: 0 for provider in ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")},
            finalize_without_paid=True,
        ).as_dict()
    manifest = {"run_config": run_config, "config_sha256": run_context.RunConfig.from_dict(run_config).sha256}
    validate_manifest_config_sha(manifest)
    for mutate in (
        lambda value: value["run_config"]["effective_settings"].update(enable_llm=1),
        lambda value: value["run_config"]["effective_settings"].pop("enable_llm"),
        lambda value: value["run_config"].update(extra_field=True),
        lambda value: value["run_config"]["effective_settings"].update(publication_policy_min_safety_score=None),
    ):
        candidate = json.loads(json.dumps(manifest))
        mutate(candidate)
        try:
            candidate["config_sha256"] = run_context.RunConfig.from_dict(candidate["run_config"]).sha256
        except Exception:
            pass
        with pytest.raises(ValueError, match="free_only_config_sha_mismatch"):
            validate_manifest_config_sha(candidate)

    for thresholds, settings in ((60, 70), (101, 101), (-1, -1)):
        candidate = json.loads(json.dumps(manifest))
        candidate["run_config"]["thresholds"]["REVIEW_SCORE"] = thresholds
        candidate["run_config"]["effective_settings"]["review_score"] = settings
        candidate["config_sha256"] = run_context.RunConfig.from_dict(candidate["run_config"]).sha256
        with pytest.raises(ValueError, match="free_only_config_semantic_invalid"):
            validate_manifest_config_sha(candidate)


def test_budget_observation_false_with_threshold_drift_is_replay_failure():
    activity = {
        "provider_calls": 0, "paid_attempts": 0, "paid_attempt_calls": 0,
        "provider_query_flights": 0, "flight_consumers": 0, "paid_budget_blocks": 0,
    }
    database = {
        "check_completed": True, "budget_check_completed": True,
        "paid_activity": activity, "paid_activity_nonzero": False,
        "paid_provider_calls": 0, "paid_budget_nonzero": False,
        "budget_offenders": [], "database_validation_failed": False,
    }
    result = classify_replay_receipt(
        budget_checks=[{
            "budget_check_completed": False, "paid_budget_nonzero": False,
            "budget_offenders": [], "config_invalid_details": [],
        }],
        threshold_info={
            "threshold_mismatch": True, "threshold_mismatch_details": [{"field": "threshold"}],
            "config_mismatch": False, "config_mismatch_details": [],
        }, database=database, run_present=True, pipeline_exit_code=0, network_event_count=0,
    )
    assert result["config_invalid"] is True
    assert result["failure_reason"] == "CONFIG_INVALID"
    assert result["evidence_class"] == "REPLAY_FAILURE"
    assert result["replay_evidence_eligible"] is False
    assert result["failure_reasons"]


def test_classifier_budget_observations_fail_closed_and_preserve_valid_offenders():
    activity = {
        "provider_calls": 0, "paid_attempts": 0, "paid_attempt_calls": 0,
        "provider_query_flights": 0, "flight_consumers": 0, "paid_budget_blocks": 0,
    }
    database = {
        "check_completed": True, "budget_check_completed": True,
        "paid_activity": activity, "paid_activity_nonzero": False,
        "paid_provider_calls": 0, "paid_budget_nonzero": False,
        "budget_offenders": [], "database_validation_failed": False,
    }
    threshold_info = {
        "threshold_mismatch": False, "threshold_mismatch_details": [],
        "config_mismatch": False, "config_mismatch_details": [],
    }
    valid = {
        "budget_check_completed": True, "paid_budget_nonzero": False,
        "budget_offenders": [], "config_invalid_details": [],
    }

    class BudgetList(list):
        pass

    for malformed in (BudgetList([valid]), (valid,), {"check": valid}, None, []):
        result = classify_replay_receipt(
            budget_checks=malformed, threshold_info=threshold_info, database=database,
            run_present=True, pipeline_exit_code=0, network_event_count=0,
        )
        assert result["status"] == "FAIL"
        assert result["config_invalid"] is True
        assert result["failure_reason"] == "CONFIG_INVALID"
        assert result["failure_reasons"]
        assert result["evidence_class"] == "REPLAY_FAILURE"
        assert result["replay_evidence_eligible"] is False
        assert result["database_validation_failed"] is False

    mixed = {
        "budget_check_completed": True, "paid_budget_nonzero": True,
        "budget_offenders": [
            {"provider": "llm", "value": 2},
            {"provider": "not-canonical", "value": 9},
            {"provider": "hunter", "value": "3"},
        ],
        "config_invalid_details": [],
    }
    result = classify_replay_receipt(
        budget_checks=[mixed], threshold_info=threshold_info, database=database,
        run_present=True, pipeline_exit_code=0, network_event_count=0,
    )
    assert result["config_invalid"] is True
    assert result["failure_reason"] == "PAID_BUDGET_NONZERO"
    assert result["failure_reasons"] == ["PAID_BUDGET_NONZERO", "CONFIG_INVALID"]
    assert result["paid_budget_nonzero"] is True
    assert result["budget_offenders"] == [{"provider": "llm", "value": 2}]
    assert result["database_validation_failed"] is False
