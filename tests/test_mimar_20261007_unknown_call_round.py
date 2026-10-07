"""Talimat 37: one paid call with an unknown outcome no longer stops the paid phase for the other firms."""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from modules import checkpoint, runtime

PROVIDERS = ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")


@pytest.fixture(autouse=True)
def _clean_runtime():
    old_db = config.PROGRESS_DB_FILE
    runtime.reset()
    yield
    runtime.reset()
    config.PROGRESS_DB_FILE = old_db


def _paid_run(tmp_path: Path, count: int, budgets: dict[str, int]) -> str:
    db = tmp_path / "state" / "progress.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    config.PROGRESS_DB_FILE = db
    budgets = {provider: 0 for provider in PROVIDERS} | budgets
    run_id = "unknown-round-run"
    checkpoint._SCHEMA_READY.clear()
    checkpoint.initialize_schema(db)
    checkpoint.initialize_run(
        run_id=run_id, input_hash="input", run_signature="signature",
        context={"phase": "FREE"}, budgets=budgets,
        items=[
            {
                "item_index": index, "source_record_id": f"src-{index}", "company": f"COMPANY {index}",
                "free_state": "DONE", "paid_required": True, "paid_state": "PENDING",
            }
            for index in range(count)
        ],
    )
    checkpoint.transition_phase(run_id, "PAID", expected_count=count)
    runtime.configure_durable_run(run_id, budgets)
    runtime.set_phase("PAID")
    return run_id


def _job(run_id: str, item_index: int, request: str) -> dict:
    return checkpoint.ensure_provider_work_item(
        run_id=run_id, item_index=item_index, source_record_id=f"src-{item_index}",
        provider="brightdata", operation="search", request_fingerprint=request,
        state="READY", need_class="website",
    )


def _call_with_unknown_outcome(run_id: str, job: dict) -> None:
    assert checkpoint.claim_item(run_id=run_id, item_index=job["item_index"], phase="PAID")
    checkpoint.begin_paid_attempt(run_id=run_id, item_index=job["item_index"], attempt_number=1, provider_plan=("brightdata",))
    candidates = checkpoint.ready_provider_dispatch_candidates(run_id, "brightdata", item_indexes=[job["item_index"]])
    assert checkpoint.reserve_provider_dispatch_round(run_id=run_id, provider="brightdata", round_ordinal=0, candidates=candidates, cap=1)
    runtime.set_provider_dispatch_rounds({"brightdata": 0})
    runtime.set_item_context(job["item_index"], "search")
    runtime.set_source_record_id(job["source_record_id"])
    reservation = runtime.reserve_api("brightdata", operation="search", request_fingerprint=job["request_fingerprint"])
    assert reservation.accepted, reservation
    runtime.start_api(reservation)
    runtime.mark_api_http_started(reservation, 1)
    assert checkpoint.reconcile_unknown_provider_calls(run_id) == 1


def test_other_firms_get_a_new_round_after_an_unknown_call(tmp_path):
    run_id = _paid_run(tmp_path, 2, {"brightdata": 3})
    _call_with_unknown_outcome(run_id, _job(run_id, 0, "first-request"))
    _job(run_id, 1, "second-request")

    assert checkpoint.load_provider_dispatch_round(run_id, "brightdata", 0)["state"] == "BLOCKED_UNKNOWN"
    assert checkpoint.open_provider_dispatch_rounds(run_id, ["brightdata"]) == {}
    assert checkpoint.next_provider_dispatch_round(run_id, "brightdata") == 1

    candidates = checkpoint.ready_provider_dispatch_candidates(run_id, "brightdata", item_indexes=[0, 1])
    assert [row["item_index"] for row in candidates] == [1]
    selected = checkpoint.reserve_provider_dispatch_round(
        run_id=run_id, provider="brightdata", round_ordinal=1, candidates=candidates, cap=1,
    )
    assert [row["item_index"] for row in selected] == [1]


def test_unknown_call_keeps_its_budget_and_is_never_retried(tmp_path):
    run_id = _paid_run(tmp_path, 2, {"brightdata": 3})
    _call_with_unknown_outcome(run_id, _job(run_id, 0, "first-request"))
    assert checkpoint.provider_work_has_unknown(run_id, 0)
    assert checkpoint.ready_provider_dispatch_candidates(run_id, "brightdata", item_indexes=[0]) == []
    assert checkpoint.provider_dispatch_remaining_capacity(run_id, "brightdata") == 2
    assert [item["paid_state"] for item in checkpoint.load_run_items(run_id)] == ["UNKNOWN", "PENDING"]
