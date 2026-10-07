"""Talimat 35: a firm interrupted by a stop gets its free search allowance back when the run resumes."""

from __future__ import annotations

import pytest

import config
from modules import checkpoint, runtime


@pytest.fixture
def free_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROGRESS_DB_FILE", tmp_path / "progress.sqlite3")
    checkpoint.initialize_schema(config.PROGRESS_DB_FILE)
    checkpoint.initialize_run(
        run_id="run", input_hash="input", run_signature="sig",
        context={"phase": "FREE", "paid_query_limit_per_company": 0, "budget_details": {}},
        budgets={},
        items=[
            {"item_index": 0, "source_record_id": "s0", "free_state": "RUNNING", "paid_state": "NOT_REQUIRED"},
            {"item_index": 1, "source_record_id": "s1", "free_state": "DONE", "paid_state": "NOT_REQUIRED"},
            {"item_index": 2, "source_record_id": "s2", "free_state": "RUNNING", "paid_state": "NOT_REQUIRED"},
        ],
    )
    runtime.reset()
    runtime.configure_durable_run("run", {})
    runtime.set_phase("FREE")
    runtime.set_search_bucket("discovery")
    yield
    runtime.reset()


def _search(item_index: int, count: int) -> list[bool]:
    runtime.set_item_context(item_index)
    accepted = []
    for number in range(count):
        fingerprint = f"query-{item_index}-{number}"
        logical = runtime.reserve_free_logical_query("discovery", fingerprint)
        accepted.append(bool(logical))
        if logical:
            physical = runtime.reserve_free_physical_attempt("discovery", fingerprint, "yandex")
            runtime.complete_free_physical_attempt(physical.attempt_id, True)
    return accepted


def test_interrupted_firm_searches_again_after_resume(free_run):
    assert _search(0, 7) == [True] * 6 + [False]
    assert _search(1, 2) == [True, True]

    assert checkpoint.recover_interrupted_items("run") == {
        "free_reset": 2, "paid_reset": 0, "paid_unknown": 0, "pre_http_calls_recovered": 0,
    }

    assert _search(0, 7) == [True] * 6 + [False]
    runtime.set_item_context(1)
    assert runtime.free_search_capacity("discovery").logical_used == 2
    checkpoint.validate_ledger_equations("run")


def test_released_allowance_is_kept_as_a_receipt(free_run):
    _search(0, 7)
    checkpoint.recover_interrupted_items("run")
    receipts = checkpoint.free_usage_recovery_receipts("run")
    assert [receipt["item_index"] for receipt in receipts] == [0]
    assert len(receipts[0]["attempts"]) == 6
    assert receipts[0]["usage"][0]["discovery_logical_used"] == 6
    assert [block["block_kind"] for block in receipts[0]["blocks"]] == ["logical"]


def test_finished_firms_and_unused_allowance_are_left_alone(free_run):
    _search(1, 3)
    checkpoint.recover_interrupted_items("run")
    assert checkpoint.free_usage_recovery_receipts("run") == []
    runtime.set_item_context(1)
    assert runtime.free_search_capacity("discovery").logical_used == 3
    assert [item["free_state"] for item in checkpoint.load_run_items("run")] == ["PENDING", "DONE", "PENDING"]
