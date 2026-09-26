"""Regression tests for terminal query-flight work item reconciliation."""

from __future__ import annotations

import sqlite3

import pytest

import config
from modules import checkpoint, runtime


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch):
    original_db = config.PROGRESS_DB_FILE
    runtime.reset()
    yield
    runtime.reset()
    config.PROGRESS_DB_FILE = original_db


def _failed_retry_flight(tmp_path, *, foreign_call: bool = False):
    db_path = tmp_path / "state" / "progress.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config.PROGRESS_DB_FILE = db_path
    checkpoint._SCHEMA_READY.clear()
    budgets = {
        "brightdata": 2, "google_places": 0, "brandfetch": 0,
        "hunter": 0, "linkedin": 0, "llm": 0,
    }
    checkpoint.initialize_schema(db_path)
    checkpoint.initialize_run(
        run_id="reconcile-test-run", input_hash="input", run_signature="signature",
        context={"phase": "FREE"}, budgets=budgets,
        items=[{
            "item_index": 0, "source_record_id": "src-0", "company": "COMPANY",
            "free_state": "DONE", "paid_required": True, "paid_state": "PENDING",
        }],
    )
    checkpoint.transition_phase("reconcile-test-run", "PAID", expected_count=1)
    runtime.configure_durable_run("reconcile-test-run", budgets)
    runtime.set_phase("PAID")
    assert checkpoint.claim_item(run_id="reconcile-test-run", item_index=0, phase="PAID")
    checkpoint.begin_paid_attempt(
        run_id="reconcile-test-run", item_index=0, attempt_number=1,
        provider_plan=("brightdata",),
    )
    runtime.set_item_context(0, "search")
    runtime.set_source_record_id("src-0")

    job = checkpoint.ensure_provider_work_item(
        run_id="reconcile-test-run", item_index=0, source_record_id="src-0",
        provider="brightdata", operation="search", request_fingerprint="same-request",
        query_fingerprint="query-fingerprint", need_class="website",
    )
    candidates = checkpoint.ready_provider_dispatch_candidates(
        "reconcile-test-run", "brightdata", item_indexes=[0],
    )
    assert checkpoint.reserve_provider_dispatch_round(
        run_id="reconcile-test-run", provider="brightdata", round_ordinal=0,
        candidates=candidates, cap=1,
    )
    runtime.set_provider_dispatch_rounds({"brightdata": 0})
    owner = "flight-owner"
    checkpoint.claim_provider_query_flight(
        run_id="reconcile-test-run", provider="brightdata",
        query_fingerprint="query-fingerprint", owner_token=owner,
    )

    call_ids = []
    for attempt_ordinal in (1, 2):
        reservation = runtime.reserve_api(
            "brightdata", operation="search", request_fingerprint="same-request",
            flight_fingerprint="query-fingerprint", execution_generation=1,
        )
        assert reservation.accepted, reservation
        runtime.start_api(reservation)
        runtime.mark_api_http_started(reservation, attempt_ordinal, "query-fingerprint")
        checkpoint.bind_provider_query_flight_call(
            run_id="reconcile-test-run", provider="brightdata",
            query_fingerprint="query-fingerprint", owner_token=owner,
            provider_call_id=reservation.call_id,
        )
        runtime.complete_api(reservation, "FAILED")
        call_ids.append(reservation.call_id)

    checkpoint.finish_provider_query_flight(
        run_id="reconcile-test-run", provider="brightdata",
        query_fingerprint="query-fingerprint", owner_token=owner,
        state="FAILED", result={"result_reason": "upstream_failed"}, call_ids=call_ids,
    )
    with sqlite3.connect(db_path) as connection:
        if foreign_call:
            connection.execute(
                "UPDATE provider_work_items SET call_id=? WHERE run_id=? AND job_fingerprint=?",
                ("foreign-call", "reconcile-test-run", job["job_fingerprint"]),
            )
            connection.commit()
        linked = connection.execute(
            "SELECT call_id,state FROM provider_work_items WHERE run_id=? AND job_fingerprint=?",
            ("reconcile-test-run", job["job_fingerprint"]),
        ).fetchone()
    return db_path, job["job_fingerprint"], call_ids, linked


def test_reconcile_accepts_work_item_linked_to_retry_call(tmp_path):
    _db_path, job_fingerprint, call_ids, linked = _failed_retry_flight(tmp_path)
    assert linked == (call_ids[1], "FAILED")

    assert checkpoint.reconcile_provider_work_item_to_flight(
        run_id="reconcile-test-run", job_fingerprint=job_fingerprint,
    )

    with sqlite3.connect(config.PROGRESS_DB_FILE) as connection:
        assert connection.execute(
            "SELECT call_id,state FROM provider_work_items WHERE run_id=? AND job_fingerprint=?",
            ("reconcile-test-run", job_fingerprint),
        ).fetchone() == (call_ids[1], "FAILED")


def test_reconcile_still_rejects_foreign_call(tmp_path):
    _db_path, job_fingerprint, _call_ids, linked = _failed_retry_flight(
        tmp_path, foreign_call=True,
    )
    assert linked == ("foreign-call", "FAILED")

    with pytest.raises(
        checkpoint.EvidenceInvariant,
        match="provider work call link conflicts with terminal flight",
    ):
        checkpoint.reconcile_provider_work_item_to_flight(
            run_id="reconcile-test-run", job_fingerprint=job_fingerprint,
        )
