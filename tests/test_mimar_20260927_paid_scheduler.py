"""Erratum 08: paid rounds close executor-declined work instead of stalling."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import config
from modules import checkpoint, pipeline_runner, runtime, search


PROVIDERS = ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")
RUN_ID = "declined-run"


@pytest.fixture(autouse=True)
def _clean_runtime():
    old_db = config.PROGRESS_DB_FILE
    runtime.reset()
    yield
    runtime.reset()
    config.PROGRESS_DB_FILE = old_db


def _paid_run(tmp_path, count=3):
    db = tmp_path / "state" / "progress.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    config.PROGRESS_DB_FILE = db
    budgets = {provider: 0 for provider in PROVIDERS} | {"brightdata": 10}
    checkpoint._SCHEMA_READY.clear()
    checkpoint.initialize_schema(db)
    checkpoint.initialize_run(
        run_id=RUN_ID, input_hash="input", run_signature="signature",
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
    checkpoint.transition_phase(RUN_ID, "PAID", expected_count=count)
    runtime.configure_durable_run(RUN_ID, budgets)
    runtime.set_phase("PAID")
    return RUN_ID


def _job(run_id, item_index, request):
    return checkpoint.ensure_provider_work_item(
        run_id=run_id, item_index=item_index, source_record_id=f"src-{item_index}",
        provider="brightdata", operation="search", request_fingerprint=request,
        need_class="website_discovery",
    )


def _allocate(run_id, round_ordinal, item_indexes):
    candidates = checkpoint.ready_provider_dispatch_candidates(
        run_id, "brightdata", item_indexes=item_indexes,
    )
    reserved = checkpoint.reserve_provider_dispatch_round(
        run_id=run_id, provider="brightdata", round_ordinal=round_ordinal,
        candidates=candidates, cap=len(candidates),
    )
    assert len(reserved) == len(item_indexes)


def _decline(run_id, round_ordinal, item_index):
    assert checkpoint.release_provider_dispatch_allocation(
        run_id=run_id, provider="brightdata", round_ordinal=round_ordinal,
        item_index=item_index, source_record_id=f"src-{item_index}",
        reason="no_physical_dispatch",
    )


def test_only_jobs_declined_in_their_own_latest_allocation_are_closed(tmp_path):
    run_id = _paid_run(tmp_path)
    declined = _job(run_id, 0, "declined-query")
    faulted = _job(run_id, 1, "faulted-query")
    _allocate(run_id, 0, [0, 1])
    with sqlite3.connect(config.PROGRESS_DB_FILE) as db:
        db.execute(
            "UPDATE provider_dispatch_allocations SET job_fingerprint=? WHERE run_id=? AND item_index=1",
            ("f" * 64, run_id),
        )
        db.commit()
    _decline(run_id, 0, 0)
    _decline(run_id, 0, 1)
    untried = _job(run_id, 2, "untried-query")

    closed = checkpoint.close_executor_declined_work(run_id=run_id)

    assert closed == [{"job_fingerprint": declined["job_fingerprint"], "item_index": 0, "provider": "brightdata"}]
    states = {
        row["job_fingerprint"]: (row["state"], row["terminal_reason"])
        for row in checkpoint.load_provider_work_items(run_id)
    }
    assert states[declined["job_fingerprint"]] == ("NOT_REQUIRED", "executor_declined_after_allocation")
    assert states[faulted["job_fingerprint"]][0] == "READY"
    assert states[untried["job_fingerprint"]][0] == "READY"
    assert checkpoint.close_executor_declined_work(run_id=run_id) == []


def test_decline_from_an_older_round_is_not_closed(tmp_path):
    run_id = _paid_run(tmp_path)
    old = _job(run_id, 0, "old-query")
    _allocate(run_id, 0, [0])
    _decline(run_id, 0, 0)
    _job(run_id, 1, "newer-query")
    _allocate(run_id, 1, [1])

    assert checkpoint.close_executor_declined_work(run_id=run_id) == []
    assert checkpoint.load_provider_work_items(run_id, item_index=0)[0]["job_fingerprint"] == old["job_fingerprint"]
    assert checkpoint.load_provider_work_items(run_id, item_index=0)[0]["state"] == "READY"


def test_paid_round_progress_rule():
    ready = {"work": [{"job_fingerprint": "a", "state": "READY"}], "physical_calls": [], "terminal_jobs": []}
    assert pipeline_runner._paid_round_made_progress(ready, ready) is False
    assert pipeline_runner._paid_round_made_progress(ready, {**ready, "physical_calls": ["call-1"]}) is True
    closed = {"work": [{"job_fingerprint": "a", "state": "NOT_REQUIRED"}], "physical_calls": [], "terminal_jobs": ["a"]}
    assert pipeline_runner._paid_round_made_progress(ready, closed) is True
    waiting = {"work": [{"job_fingerprint": "a", "state": "WAITING_DEPENDENCY"}], "physical_calls": [], "terminal_jobs": []}
    assert pipeline_runner._paid_round_made_progress(waiting, ready) is True


def test_validate_paid_evidence_collects_zero_call_unknown(tmp_path):
    from test_search_phase_regressions import init_run

    db = tmp_path / "zero-call.sqlite3"
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

    result = checkpoint.validate_paid_evidence("run", collect=True)

    assert [violation["item_index"] for violation in result["violations"]] == [0]
    assert "zero-call UNKNOWN" in result["violations"][0]["error"]
    with pytest.raises(checkpoint.OutcomeInvariant, match="zero-call UNKNOWN"):
        checkpoint.validate_paid_evidence("run", collect=False)


def test_brightdata_retry_wait_respects_failed_query_cooldown(monkeypatch):
    monkeypatch.setattr(config, "BRIGHTDATA_FAILED_QUERY_COOLDOWN_SEC", 20.0)
    monkeypatch.setattr(config, "MAX_RETRY_AFTER_SEC", 30)
    assert search._brightdata_retry_wait(SimpleNamespace(headers={}), 0) == 20.0
    assert search._brightdata_retry_wait(SimpleNamespace(headers={"Retry-After": "25"}), 0) == 25.0
    assert search._brightdata_retry_wait(SimpleNamespace(headers={"Retry-After": "90"}), 1) == 30.0
