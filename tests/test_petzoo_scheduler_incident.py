"""Offline acceptance probes for the PETZOO scheduler incident fix.

The fixtures exercise the durable checkpoint, real dispatch allocator, search
fingerprints, crawler negative cache, and recovery publication without opening
an external provider connection.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import config
import main
from modules import checkpoint, crawler, output_artifacts, pipeline_runner, runtime, search


PROVIDERS = ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch):
    old_db = config.PROGRESS_DB_FILE
    runtime.reset()
    crawler.clear_page_store()
    yield
    runtime.reset()
    crawler.clear_page_store()
    config.PROGRESS_DB_FILE = old_db


def _run(tmp_path: Path, *, count: int = 3, budgets: dict[str, int] | None = None) -> str:
    db = tmp_path / "state" / "progress.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    config.PROGRESS_DB_FILE = db
    budgets = {provider: 0 for provider in PROVIDERS} | (budgets or {})
    run_id = "petzoo-test-run"
    checkpoint._SCHEMA_READY.clear()
    checkpoint.initialize_schema(db)
    checkpoint.initialize_run(
        run_id=run_id, input_hash="input", run_signature="signature",
        context={"phase": "FREE"}, budgets=budgets,
        items=[
            {
                "item_index": index, "source_record_id": f"src-{index}",
                "company": f"COMPANY {index}", "free_state": "DONE",
                "paid_required": True, "paid_state": "PENDING",
            }
            for index in range(count)
        ],
    )
    checkpoint.transition_phase(run_id, "PAID", expected_count=count)
    runtime.configure_durable_run(run_id, budgets)
    runtime.set_phase("PAID")
    return run_id


def test_checkpoint_connections_preserve_wal_mode(tmp_path):
    db_path = tmp_path / "state" / "progress.sqlite3"
    first = checkpoint._open_connection(db_path)
    try:
        assert first.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
        first.execute("CREATE TABLE wal_probe (value INTEGER NOT NULL)")
        first.execute("INSERT INTO wal_probe VALUES (7)")
        first.commit()
    finally:
        first.close()

    second = checkpoint._open_connection(db_path)
    try:
        assert second.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
        assert second.execute("SELECT value FROM wal_probe").fetchone()[0] == 7
    finally:
        second.close()


def test_generation_validation_cache_rechecks_after_relation_mutations(tmp_path, monkeypatch):
    _run(tmp_path, count=1)
    db_path = config.PROGRESS_DB_FILE
    with sqlite3.connect(db_path) as db:
        query_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM paid_attempt_calls "
            "WHERE run_id='r' AND provider='brightdata' AND provider_call_id='c' "
            "AND query_fingerprint='q' AND execution_generation=1 AND relation='OWNER' "
            "AND item_index=0 AND paid_attempt_id='a' LIMIT 1"
        ).fetchall()
    assert any("ix_paid_attempt_calls_generation_audit" in str(row[3]) for row in query_plan)
    identity = checkpoint._db_identity(db_path)
    with checkpoint._GENERATION_VALIDATION_CACHE_LOCK:
        checkpoint._GENERATION_VALIDATION_CACHE.pop(identity, None)

    validation_count = 0
    original_validation = checkpoint._generation_relation_violations

    def count_validation(connection):
        nonlocal validation_count
        validation_count += 1
        return original_validation(connection)

    monkeypatch.setattr(checkpoint, "_generation_relation_violations", count_validation)
    for _ in range(2):
        connection = checkpoint._connect()
        connection.close()
    assert validation_count == 1

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO provider_query_flights(run_id,provider,query_fingerprint,state,owner_token,lease_expires_at,created_at,updated_at) "
            "VALUES('petzoo-test-run','brightdata',?,'IN_PROGRESS','owner','later','now','now')",
            ("a" * 64,),
        )
    connection = checkpoint._connect()
    connection.close()
    assert validation_count == 2

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE provider_query_flights SET state='DONE',provider_call_id='forged-call' "
            "WHERE run_id='petzoo-test-run' AND provider='brightdata' AND query_fingerprint=?",
            ("a" * 64,),
        )
    with pytest.raises(checkpoint.EvidenceInvariant):
        checkpoint._connect()
    assert validation_count == 3


def test_scheduler_heartbeat_retries_transient_write_and_keeps_append_only_history(tmp_path, monkeypatch):
    run_id = _run(tmp_path, count=1, budgets={"brightdata": 1})
    original = checkpoint.record_scheduler_heartbeat
    persisted_twice = threading.Event()
    calls = 0

    def flaky_write(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("injected transient lock")
        result = original(**kwargs)
        if int(result["sequence"]) >= 2:
            persisted_twice.set()
        return result

    monkeypatch.setattr(checkpoint, "record_scheduler_heartbeat", flaky_write)
    heartbeat = pipeline_runner._SchedulerProgressHeartbeat(
        run_id=run_id, round_ordinal=1, interval_seconds=0.01, retry_delay_seconds=0.01,
    ).start()
    assert persisted_twice.wait(1.0)
    heartbeat.stop()

    with sqlite3.connect(config.PROGRESS_DB_FILE) as db:
        rows = db.execute(
            "SELECT sequence,snapshot_json,snapshot_sha256 FROM scheduler_heartbeat_events "
            "WHERE run_id=? AND round_ordinal=1 AND phase='PAID' ORDER BY sequence",
            (run_id,),
        ).fetchall()
        latest = db.execute(
            "SELECT snapshot_json FROM scheduler_progress_snapshots "
            "WHERE run_id=? AND round_ordinal=1 AND phase='PAID' AND kind='HEARTBEAT'",
            (run_id,),
        ).fetchone()
    assert calls >= 3
    assert [row[0] for row in rows] == [1, 2]
    assert all(json.loads(row[1])["heartbeat"] for row in rows)
    assert all(len(row[2]) == 64 for row in rows)
    assert json.loads(latest[0])["sequence"] == 2


def _claim(run_id: str, item_index: int = 0, provider_plan: tuple[str, ...] = ("brightdata",)) -> None:
    assert checkpoint.claim_item(run_id=run_id, item_index=item_index, phase="PAID")
    checkpoint.begin_paid_attempt(
        run_id=run_id, item_index=item_index, attempt_number=1,
        provider_plan=provider_plan,
    )
    runtime.set_item_context(item_index, "test")
    runtime.set_source_record_id(f"src-{item_index}")


def _job(run_id: str, item_index: int, provider: str, operation: str, request: str, *, state: str = "READY", need: str = "website"):
    return checkpoint.ensure_provider_work_item(
        run_id=run_id, item_index=item_index,
        source_record_id=f"src-{item_index}", provider=provider,
        operation=operation, request_fingerprint=request,
        state=state, need_class=need,
    )


def _dispatch(run_id: str, job: dict, *, cap: int = 1):
    candidates = checkpoint.ready_provider_dispatch_candidates(run_id, job["provider"], item_indexes=[job["item_index"]])
    assert candidates
    selected = checkpoint.reserve_provider_dispatch_round(
        run_id=run_id, provider=job["provider"], round_ordinal=0,
        candidates=candidates, cap=cap,
    )
    assert selected
    runtime.set_provider_dispatch_rounds({job["provider"]: 0})
    runtime.set_item_context(job["item_index"], job["operation"])
    runtime.set_source_record_id(job["source_record_id"])
    reservation = runtime.reserve_api(
        job["provider"], operation=job["operation"],
        request_fingerprint=job["request_fingerprint"],
    )
    assert reservation.accepted, reservation
    return reservation


def test_p01_last_hunter_right_goes_to_next_ready_company(tmp_path):
    run_id = _run(tmp_path, budgets={"hunter": 1, "brightdata": 1})
    _job(run_id, 0, "hunter", "domain_search", "hunter-0", state="DONE", need="identity")
    _job(run_id, 0, "brightdata", "search", "bd-0", need="website")
    _job(run_id, 1, "hunter", "domain_search", "hunter-1", need="identity")
    _job(run_id, 2, "hunter", "domain_search", "hunter-2", need="identity")
    candidates = checkpoint.ready_provider_dispatch_candidates(run_id, "hunter", item_indexes=[0, 1, 2])
    assert [row["item_index"] for row in candidates] == [1, 2]
    selected = checkpoint.reserve_provider_dispatch_round(
        run_id=run_id, provider="hunter", round_ordinal=0,
        candidates=candidates, cap=1,
    )
    assert [row["item_index"] for row in selected] == [1]
    assert checkpoint.provider_work_item_for_request(
        run_id=run_id, item_index=0, provider="hunter",
        operation="domain_search", request_fingerprint="hunter-0",
    )["state"] == "DONE"


def test_p02_terminal_follower_does_not_consume_next_query_budget(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 2})
    old = _job(run_id, 0, "brightdata", "search", search.brightdata_request_fingerprint("first"), state="DONE")
    new = _job(run_id, 0, "brightdata", "search", search.brightdata_request_fingerprint("second"))
    assert old["job_fingerprint"] != new["job_fingerprint"]
    assert [job["request_fingerprint"] for job in checkpoint.ready_provider_dispatch_candidates(run_id, "brightdata", item_indexes=[0])] == [new["request_fingerprint"]]
    inherited = runtime.rejected_provider_result(runtime.Reservation(
        False, "brightdata", "search", 0, "PAID", call_id="old-call",
        reason="duplicate_request", inherited_state="DONE",
    ))
    assert inherited.call_relations == {"old-call": "INHERITED"}


def test_p02_no_dispatch_job_yields_to_untried_query_and_retries_fairly(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 4})
    stale = _job(run_id, 0, "brightdata", "search", "stale-query")

    def release_selected(round_ordinal: int, expected_request: str) -> None:
        candidates = checkpoint.ready_provider_dispatch_candidates(
            run_id, "brightdata", item_indexes=[0],
        )
        assert [row["request_fingerprint"] for row in candidates] == [expected_request]
        reserved = checkpoint.reserve_provider_dispatch_round(
            run_id=run_id, provider="brightdata", round_ordinal=round_ordinal,
            candidates=candidates, cap=1,
        )
        assert len(reserved) == 1
        assert checkpoint.release_provider_dispatch_allocation(
            run_id=run_id, provider="brightdata", round_ordinal=round_ordinal,
            item_index=0, source_record_id="src-0", reason="no_physical_dispatch",
        )

    release_selected(0, "stale-query")
    _job(run_id, 0, "brightdata", "search", "fresh-query")
    release_selected(1, "fresh-query")
    release_selected(2, "stale-query")
    release_selected(3, "fresh-query")


def test_p03_terminal_provider_does_not_create_pending_work(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 1, "brandfetch": 0, "google_places": 0, "hunter": 0})
    _job(run_id, 0, "brandfetch", "domain_search", "bf-0", state="BLOCKED_BUDGET", need="identity")
    _job(run_id, 0, "google_places", "text_search", "places-0", state="DONE", need="contact")
    _job(run_id, 0, "brightdata", "search", "bd-0")
    assert checkpoint.pending_provider_names(run_id, 0) == ["brightdata"]


def test_p04_unknown_has_priority_and_free_ddgs_is_not_paid_work(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 1})
    _job(run_id, 0, "brightdata", "search", "bd-unknown", state="UNKNOWN")
    assert checkpoint.provider_work_has_unknown(run_id, 0)
    assert checkpoint.pending_provider_names(run_id, 0) == []
    assert "ddgs" not in checkpoint.CANONICAL_PROVIDERS


def test_p05_stall_is_durable_and_partial_output_is_not_complete(tmp_path, monkeypatch):
    run_id = _run(tmp_path, budgets={"brightdata": 1})
    before = checkpoint.scheduler_progress_snapshot(run_id)
    checkpoint.record_scheduler_progress_snapshot(
        run_id=run_id, round_ordinal=1, phase="PAID", kind="START", snapshot=before,
    )
    after = checkpoint.scheduler_progress_snapshot(run_id)
    assert before["work_hash"] == after["work_hash"]
    stall = checkpoint.mark_scheduler_stalled(run_id=run_id, reason="fault_injected_no_progress")
    assert stall["phase"] == "PAID"
    written = output_artifacts.write_partial_recovery(
        [{"source_record_id": "src-0", "paid_state": "PENDING"}],
        output_root=tmp_path / "output", metadata={"status": "SCHEDULER_STALLED", "termination": stall},
    )
    manifest = json.loads((Path(written["directory"]) / "partial_manifest.json").read_text(encoding="utf-8"))
    assert manifest["partial"] is True and manifest["accepted_as_complete"] is False
    args = SimpleNamespace(
        input="input.xlsx", finalize_without_paid=None, run_dir=None,
        resume_run=None, from_run_manifest=None, search_cache=None,
        crawl_cache=None, brightdata_budget=None, google_places_budget=None,
        hunter_budget=None, brandfetch_budget=None,
        linkedin_company_budget=None, llm_budget=None, rerank_cache=None,
        non_interactive=True, allow_paid=True,
    )
    monkeypatch.setattr(main, "parse_args", lambda _argv=None: args)
    monkeypatch.setattr(main, "_ensure_safe_project_runtime", lambda _argv: None)
    monkeypatch.setattr(main, "_apply_cli_options", lambda _args: None)
    monkeypatch.setattr(main, "resolve_cli_run_config", lambda _args: None)
    monkeypatch.setattr(main, "_cli_selected_values", lambda _args: ([], []))
    monkeypatch.setattr(main, "_apply_saved_resolver_configuration", lambda: None)
    monkeypatch.setattr(main, "run", lambda *_a, **_k: main.pipeline_runner.PipelineOutcome(
        main.pipeline_runner.PipelineOutcomeStatus.SCHEDULER_STALLED,
        "SCHEDULER_STALLED",
    ))
    assert main.cli([]) == 23


def test_p06_terminal_quality_history_is_not_reopened(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 2})
    first = _job(run_id, 0, "brightdata", "search", "quality-old", state="DONE")
    second = _job(run_id, 0, "brightdata", "search", "quality-new", state="READY")
    assert first["state"] == "DONE"
    assert second["state"] == "READY"
    with pytest.raises(checkpoint.StateTransitionInvariant):
        checkpoint.transition_provider_work_item(run_id=run_id, job_fingerprint=first["job_fingerprint"], state="READY")


def test_p13_paid_materializer_ignores_free_plans_and_reopens_for_targeted_plan(tmp_path):
    run_id = _run(tmp_path, count=1, budgets={"brightdata": 3})
    with sqlite3.connect(config.PROGRESS_DB_FILE) as db:
        db.execute(
            "UPDATE run_items SET paid_state='DONE' WHERE run_id=? AND item_index=0",
            (run_id,),
        )
        db.execute(
            "INSERT INTO results(run_id,item_index,payload) VALUES(?,0,?)",
            (run_id, json.dumps({"source_record_id": "src-0", "company": "ACME", "paid_state": "DONE"})),
        )
        db.commit()
    checkpoint.freeze_paid_query_plan(
        run_id=run_id, item_index=0, query_kind="targeted", round_ordinal=2,
        queries=['"ACME" official site'],
    )
    checkpoint.freeze_paid_query_plan(
        run_id=run_id, item_index=0, query_kind="adaptive", round_ordinal=1,
        queries=['"ACME" paid adaptive query'],
    )
    pipeline_runner._materialize_observed_search_jobs(
        run_id=run_id,
        company_records=[{"source_record_id": "src-0", "company": "ACME"}],
        results_by_index={0: {"__search_trace": [{
            "source": "adaptive_discovery", "execution_phase": "FREE",
            "planned_queries": ['"ACME" free planner query'],
        }, {
            "source": "adaptive_discovery", "execution_phase": "PAID",
            "planned_queries": [
                '"ACME" paid adaptive query', '"ACME" unplanned trace-only query',
            ],
        }]}},
    )
    work = checkpoint.load_provider_work_items(run_id, item_index=0)
    assert {row["request_fingerprint"] for row in work} == {
        search.brightdata_request_fingerprint('"ACME" official site'),
        search.brightdata_request_fingerprint('"ACME" paid adaptive query'),
    }
    assert checkpoint.load_run_items(run_id)[0]["paid_state"] == "PENDING"
    assert checkpoint.load_results_by_id(run_id)[0]["dispatch_pending_providers"] == ["brightdata"]


def test_p14_targeted_durable_plan_materializes_without_search_trace(tmp_path):
    run_id = _run(tmp_path, count=1, budgets={"brightdata": 1})
    checkpoint.freeze_paid_query_plan(
        run_id=run_id, item_index=0, query_kind="targeted", round_ordinal=1,
        queries=['"ACME" official site'],
    )
    pipeline_runner._materialize_observed_search_jobs(
        run_id=run_id,
        company_records=[{"source_record_id": "src-0", "company": "ACME"}],
        results_by_index={0: {"__search_trace": None}},
    )
    work = checkpoint.load_provider_work_items(run_id, item_index=0)
    assert len(work) == 1
    assert work[0]["request_fingerprint"] == search.brightdata_request_fingerprint('"ACME" official site')
    assert work[0]["state"] == "READY"


class _NotFoundResponse:
    status_code = 404
    url = "https://negative.example/"
    headers = {"content-type": "text/html"}
    text = "not found"
    content = b"not found"

    def raise_for_status(self):
        raise requests.HTTPError("404")


def test_p07_negative_page_cache_blocks_repeat_same_run(tmp_path, monkeypatch):
    run_id = _run(tmp_path, budgets={"brightdata": 1})
    runtime.set_source_record_id("src-0")
    monkeypatch.setattr(config, "CRAWL_CACHE_MODE", "off")
    monkeypatch.setattr(crawler, "_preflight_js_fallback", lambda: None)
    count = {"root": 0}

    def fetch(url):
        if str(url).rstrip("/") == "https://negative.example":
            count["root"] += 1
        response = _NotFoundResponse()
        error = requests.HTTPError("404")
        error.response = response
        raise error

    monkeypatch.setattr(crawler, "_fetch", fetch)
    crawler.fetch_site("https://negative.example/", profile="identity")
    crawler.fetch_site("https://negative.example/", profile="identity")
    assert count["root"] == 1
    assert runtime.durable_run_id() == run_id


def test_p08_redirect_execution_has_typed_terminal_or_recovery_state(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 1})
    execution = checkpoint.reserve_discovery_execution(
        run_id=run_id, source_record_id="src-0", stage="redirect", execution_kind="resolved",
    )
    checkpoint.record_discovery_attempt(
        run_id=run_id, source_record_id="src-0", attempt_id="redirect-ok",
        execution_id=execution["execution_id"], stage="redirect", transport_outcome="DONE",
        semantic_result="REDIRECT_RESOLVED",
    )
    with sqlite3.connect(config.PROGRESS_DB_FILE) as db:
        assert db.execute("SELECT COUNT(*) FROM discovery_executions WHERE state='STARTED'").fetchone()[0] == 0


def test_p09_duplicate_done_binds_inherited_call_evidence(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 2})
    job = _job(run_id, 0, "brightdata", "search", "same-request")
    _claim(run_id, 0)
    reservation = _dispatch(run_id, job)
    runtime.start_api(reservation)
    runtime.mark_api_http_started(reservation, 1)
    runtime.complete_api(reservation, "DONE")
    duplicate = runtime.reserve_api(
        "brightdata", operation="search", request_fingerprint="same-request",
    )
    result = runtime.rejected_provider_result(duplicate)
    assert duplicate.reason == "duplicate_request"
    assert result.call_relations == {reservation.call_id: "INHERITED"}


def test_p10_terminal_failure_stop_scope_survives_replay_classification(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 1})
    job = _job(run_id, 0, "brightdata", "search", "captcha-request")
    checkpoint.transition_provider_work_item(
        run_id=run_id, job_fingerprint=job["job_fingerprint"], state="FAILED",
        terminal_reason="provider_header:captcha",
    )
    row = checkpoint.load_provider_work_items(run_id, provider="brightdata")[0]
    assert row["terminal_reason"] == "provider_header:captcha"
    assert search.SearchResults([], "replay", "brightdata", result_state="FAILED", stop_scope="PAID_PROVIDER").stop_scope.value == "PAID_PROVIDER"


def test_p11_137_fixture_has_unique_identity_and_no_unexplained_pending(tmp_path):
    run_id = _run(tmp_path, count=137, budgets={"brightdata": 535, "google_places": 35, "brandfetch": 35, "hunter": 14, "linkedin": 1085, "llm": 478})
    for index in range(137):
        _job(run_id, index, "brightdata", "search", f"request-{index}", state="NOT_REQUIRED")
    rows = checkpoint.load_provider_work_items(run_id)
    assert len(rows) == 137
    assert len({row["source_record_id"] for row in rows}) == 137
    assert checkpoint.pending_provider_work(run_id) == []


def test_p12_interrupted_call_reconciles_to_unknown_without_reallocation(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 1})
    job = _job(run_id, 0, "brightdata", "search", "resume-request")
    _claim(run_id, 0)
    reservation = _dispatch(run_id, job)
    runtime.start_api(reservation)
    runtime.mark_api_http_started(reservation, 1)
    assert checkpoint.reconcile_unknown_provider_calls(run_id) == 1
    assert checkpoint.provider_work_has_unknown(run_id, 0)
    assert checkpoint.ready_provider_dispatch_candidates(run_id, "brightdata", item_indexes=[0]) == []


def test_p12_reserved_call_before_http_recovers_allocation_and_budget(tmp_path):
    run_id = _run(tmp_path, budgets={"brightdata": 1})
    job = _job(run_id, 0, "brightdata", "search", "reserved-before-http")
    _claim(run_id, 0)
    reservation = _dispatch(run_id, job)
    runtime.start_api(reservation)

    assert checkpoint.recover_interrupted_items(run_id) == {
        "free_reset": 0, "paid_reset": 1, "paid_unknown": 0,
        "pre_http_calls_recovered": 1,
    }
    assert checkpoint.provider_call_recovery_receipts(run_id)[0]["call_id"] == reservation.call_id
    assert checkpoint.provider_calls_for_item(run_id, 0) == []
    ready = checkpoint.ready_provider_dispatch_candidates(run_id, "brightdata", item_indexes=[0])
    assert len(ready) == 1 and ready[0]["job_fingerprint"] == job["job_fingerprint"]
    with sqlite3.connect(config.PROGRESS_DB_FILE) as db:
        assert db.execute("SELECT reserved,reserved_total,unknown FROM provider_usage WHERE run_id=? AND provider='brightdata'", (run_id,)).fetchone() == (0, 0, 0)
