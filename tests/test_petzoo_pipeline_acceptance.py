from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import requests

import config
import main
from modules import checkpoint, crawler, pipeline_runner, publication_policy, runtime, search
from modules import run_context
from prepare_paid_continuation import prepare_paid_continuation
from petzoo_fixture_support import (
    FakeResponse, PROVIDER_BUDGETS, ReplayPaidTransport, _append_jsonl, install_harness, load_fixture,
    petzoo_group_c_transport, write_input_book,
)


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[2] if ROOT.name == "source_snapshot" else ROOT
RUNTIME = Path(sys.executable)
DELIVERY = PROJECT_ROOT / "outputs" / "petzoo_incident_fix_20260923_tek_teslim"


def _evidence_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


@pytest.fixture(autouse=True)
def _reset_process_state(monkeypatch, request, tmp_path):
    runtime.reset()
    crawler.clear_page_store()
    yield
    test_name = request.node.name
    parts = test_name.split("_", 2)
    gate = parts[1].upper() if len(parts) > 1 else ""
    if gate in {f"P{index:02d}" for index in range(1, 11)}:
        case_root = DELIVERY / "evidence" / gate / "cases" / test_name / _evidence_stamp()
        case_root.mkdir(parents=True, exist_ok=True)
        databases = []
        for index, source in enumerate(sorted(Path(tmp_path).rglob("progress.sqlite3"))):
            if index >= 12 or not source.is_file():
                break
            destination = case_root / "checkpoints" / f"checkpoint_{index:02d}.sqlite3"
            destination.parent.mkdir(parents=True, exist_ok=True)
            _backup_sqlite(source, destination)
            with sqlite3.connect(f"file:{destination.resolve().as_posix()}?mode=ro", uri=True) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                counts = {
                    table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in ("run_items", "provider_work_items", "provider_calls", "retrieval_receipts", "discovery_executions")
                    if table in tables
                }
                integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
            databases.append({
                "path": destination.relative_to(case_root).as_posix(),
                "sha256": _sha256(destination), "counts": counts,
                "integrity_check": integrity, "foreign_key_errors": len(foreign_key_errors),
            })
        raw_files = []
        raw_root = case_root / "transport"
        for source in sorted(Path(tmp_path).rglob("*.jsonl")):
            if source.name not in {"transport.jsonl", "http.jsonl", "network.jsonl", "faults.jsonl"}:
                continue
            relative = source.relative_to(tmp_path)
            destination = raw_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            raw_files.append({
                "path": destination.relative_to(case_root).as_posix(),
                "sha256": _sha256(destination), "bytes": destination.stat().st_size,
            })
        (case_root / "raw_measurement.json").write_text(json.dumps({
            "nodeid": request.node.nodeid,
            "database_snapshots": databases,
            "transport_artifacts": raw_files,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    runtime.reset()
    crawler.clear_page_store()


def _init_run(tmp_path: Path, monkeypatch, budgets: dict[str, int], *, run_id="petzoo-acceptance"):
    db_path = tmp_path / "state" / "progress.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "PROGRESS_DB_FILE", db_path)
    checkpoint._SCHEMA_READY.clear()
    checkpoint.initialize_schema(db_path)
    checkpoint.initialize_run(
        run_id=run_id, input_hash="fixture-input", run_signature="fixture-signature",
        context={"phase": "FREE"}, budgets=budgets,
        items=[{
            "item_index": 0, "source_record_id": "petzoo:test:000",
            "company": "PETZOO TEST", "free_state": "DONE",
            "paid_required": True, "paid_state": "PENDING",
        }],
    )
    checkpoint.transition_phase(run_id, "PAID", expected_count=1)
    runtime.configure_durable_run(run_id, budgets)
    runtime.set_phase("PAID")
    return run_id, db_path


def _job(run_id: str, provider: str, request: str, *, state="READY", dependency=""):
    return checkpoint.ensure_provider_work_item(
        run_id=run_id, item_index=0, source_record_id="petzoo:test:000",
        provider=provider, operation="search", request_fingerprint=request,
        state=state, dependency_job_fingerprint=dependency,
    )


def test_p03_budget_terminalization_is_provider_specific_and_never_closes_waiting_work(tmp_path, monkeypatch):
    budgets = {provider: 0 for provider in PROVIDER_BUDGETS}
    budgets.update({"brightdata": 1, "google_places": 1, "brandfetch": 1})
    run_id, db_path = _init_run(tmp_path, monkeypatch, budgets)
    exhausted = _job(run_id, "brightdata", "bd-exhausted")
    live = _job(run_id, "google_places", "places-live")
    parent = _job(run_id, "brandfetch", "bf-parent", state="DONE")
    dependent = _job(run_id, "brandfetch", "bf-dependent", state="WAITING_DEPENDENCY", dependency=parent["job_fingerprint"])
    missing = _job(run_id, "hunter", "hunter-missing", state="READY")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE provider_usage SET reserved_total=1 WHERE run_id=? AND provider='brightdata'", (run_id,))
        db.execute(
            "UPDATE provider_work_items SET state='WAITING_DEPENDENCY',dependency_job_fingerprint='' WHERE run_id=? AND job_fingerprint=?",
            (run_id, missing["job_fingerprint"]),
        )
        db.commit()

    checkpoint.terminalize_pending_paid_budget(run_id=run_id, item_index=0, reason="fixture-capacity-check")
    report = checkpoint.resolve_provider_work_dependencies(run_id)
    jobs = {row["job_fingerprint"]: row for row in checkpoint.load_provider_work_items(run_id)}
    assert jobs[exhausted["job_fingerprint"]]["state"] == "BLOCKED_BUDGET"
    assert jobs[live["job_fingerprint"]]["state"] == "READY"
    assert jobs[dependent["job_fingerprint"]]["state"] == "READY"
    assert jobs[missing["job_fingerprint"]]["state"] == "WAITING_DEPENDENCY"
    assert report["missing"] == [{
        "job_fingerprint": missing["job_fingerprint"],
        "dependency_job_fingerprint": "",
    }]
    item = checkpoint.load_run_items(run_id)[0]
    assert item["paid_state"] == "PENDING"
    with sqlite3.connect(db_path) as db:
        blocked = db.execute(
            "SELECT provider FROM provider_budget_blocks WHERE run_id=? ORDER BY provider",
            (run_id,),
        ).fetchall()
    assert blocked == [("brightdata",)]


def test_p03_dependency_resolution_and_invalid_dependency_matrix(tmp_path, monkeypatch):
    run_id, db_path = _init_run(
        tmp_path, monkeypatch,
        {**{provider: 0 for provider in PROVIDER_BUDGETS}, "brightdata": 1},
    )
    parent = _job(run_id, "brandfetch", "dependency-parent", state="DONE")
    valid = _job(
        run_id, "google_places", "dependency-valid", state="WAITING_DEPENDENCY",
        dependency=parent["job_fingerprint"],
    )
    first = checkpoint.resolve_provider_work_dependencies(run_id)
    second = checkpoint.resolve_provider_work_dependencies(run_id)
    valid_after = {
        row["job_fingerprint"]: row
        for row in checkpoint.load_provider_work_items(run_id)
    }[valid["job_fingerprint"]]
    assert first["released"] == [valid["job_fingerprint"]]
    assert second["released"] == []
    assert valid_after["state"] == "READY"
    assert valid_after["terminal_reason"] == f"dependency_satisfied:{parent['job_fingerprint']}"

    left = _job(run_id, "brightdata", "dependency-left")
    right = _job(run_id, "hunter", "dependency-right")
    self_ref = _job(run_id, "llm", "dependency-self")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE provider_work_items SET state='WAITING_DEPENDENCY',dependency_job_fingerprint=? "
            "WHERE run_id=? AND job_fingerprint=?",
            (right["job_fingerprint"], run_id, left["job_fingerprint"]),
        )
        db.execute(
            "UPDATE provider_work_items SET state='WAITING_DEPENDENCY',dependency_job_fingerprint=? "
            "WHERE run_id=? AND job_fingerprint=?",
            (left["job_fingerprint"], run_id, right["job_fingerprint"]),
        )
        db.execute(
            "UPDATE provider_work_items SET state='WAITING_DEPENDENCY',dependency_job_fingerprint=? "
            "WHERE run_id=? AND job_fingerprint=?",
            (self_ref["job_fingerprint"], run_id, self_ref["job_fingerprint"]),
        )
        db.commit()
    invalid = checkpoint.resolve_provider_work_dependencies(run_id)
    cycle_sets = {frozenset(values) for values in invalid["cycles"]}
    assert frozenset({left["job_fingerprint"], right["job_fingerprint"]}) in cycle_sets
    assert frozenset({self_ref["job_fingerprint"]}) in cycle_sets
    assert set(invalid["waiting"]) >= {left["job_fingerprint"], right["job_fingerprint"], self_ref["job_fingerprint"]}
    final_states = {
        row["job_fingerprint"]: row["state"]
        for row in checkpoint.load_provider_work_items(run_id)
    }
    assert all(final_states[key] == "WAITING_DEPENDENCY" for key in (left["job_fingerprint"], right["job_fingerprint"], self_ref["job_fingerprint"]))


def test_search_job_materialization_deduplicates_overlapping_primary_and_adaptive_trace(tmp_path, monkeypatch):
    run_id, _db_path = _init_run(
        tmp_path, monkeypatch,
        {**{provider: 0 for provider in PROVIDER_BUDGETS}, "brightdata": 2},
    )
    query = '"PETZOO G-004" web sitesi'
    request_fingerprint = search.brightdata_request_fingerprint(query)
    query_fingerprint = search._brightdata_flight_fingerprint(query)
    primary = checkpoint.ensure_provider_work_item(
        run_id=run_id, item_index=0, source_record_id="petzoo:test:000",
        provider="brightdata", operation="search",
        request_fingerprint=request_fingerprint,
        query_fingerprint=query_fingerprint, need_class="website",
    )
    pipeline_runner._materialize_observed_search_jobs(
        run_id=run_id,
        company_records=[{"source_record_id": "petzoo:test:000"}],
        results_by_index={0: {"__search_trace": [
            {"source": "adaptive_discovery", "execution_phase": "PAID",
             "planned_queries": [query]},
            {"source": "brightdata", "phase": "primary", "query": query},
        ]}},
    )

    items = checkpoint.load_provider_work_items(run_id, item_index=0)
    assert len(items) == 1
    assert items[0]["job_fingerprint"] == primary["job_fingerprint"]
    assert items[0]["need_class"] == "website"


def test_p01_hunter_last_slot_only_reaches_next_eligible_company(tmp_path, monkeypatch):
    _manifest, fixture, fixture_sha256 = load_fixture()
    records = [dict(row) for row in fixture if row["group"] == "F"][:11]
    assert len({row["source_record_id"] for row in records}) == 11
    input_path = tmp_path / "p01-eleven-companies.xlsx"
    transport_path = tmp_path / "transport.jsonl"
    write_input_book(input_path, records)
    router = ReplayPaidTransport(records, transport_path)
    runs_dir, _router, _session = install_harness(
        monkeypatch, tmp_path, records, workers=1, router=router,
        http_journal=tmp_path / "http.jsonl",
    )
    monkeypatch.setattr(config, "HUNTER_REQUEST_HARD_CAP", 2)
    monkeypatch.setattr(config, "HUNTER_REQUEST_BUDGET", 2)

    initialize_schema = checkpoint.initialize_schema

    def install_p01_pending_search_fault(path=None):
        result = initialize_schema(path)
        with sqlite3.connect(Path(path or config.PROGRESS_DB_FILE)) as db:
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS p01_fault_defer_adaptive_search "
                "AFTER INSERT ON provider_dispatch_allocations "
                "WHEN NEW.provider='brightdata' AND NEW.state='RESERVED' "
                "AND NEW.round_ordinal>=1 AND NEW.item_index=0 "
                "BEGIN UPDATE provider_dispatch_allocations SET job_fingerprint='" + "f" * 64 + "' "
                "WHERE run_id=NEW.run_id AND provider=NEW.provider "
                "AND round_ordinal=NEW.round_ordinal AND item_index=NEW.item_index; END"
            )
            db.commit()
        return result

    monkeypatch.setattr(checkpoint, "initialize_schema", install_p01_pending_search_fault)
    outcome = main.run(input_path, allow_paid=True)
    run_root = next(runs_dir.iterdir())
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    db_path = run_root / "state" / "progress.sqlite3"
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        hunter_calls = [dict(row) for row in db.execute(
            "SELECT c.call_id,c.item_index,i.source_record_id,c.operation,c.request_fingerprint,c.state "
            "FROM provider_calls c JOIN run_items i ON i.run_id=c.run_id AND i.item_index=c.item_index "
            "WHERE c.provider='hunter' ORDER BY c.item_index,c.call_id"
        )]
        hunter_jobs = [dict(row) for row in db.execute(
            "SELECT item_index,source_record_id,job_fingerprint,state,call_id "
            "FROM provider_work_items WHERE provider='hunter' ORDER BY item_index,job_fingerprint"
        )]
        brightdata_jobs = [dict(row) for row in db.execute(
            "SELECT item_index,source_record_id,job_fingerprint,state,need_class "
            "FROM provider_work_items WHERE provider='brightdata' ORDER BY item_index,job_fingerprint"
        )]
        hunter_allocations = [dict(row) for row in db.execute(
            "SELECT item_index,job_fingerprint,state,released_reason FROM provider_dispatch_allocations "
            "WHERE provider='hunter' ORDER BY round_ordinal,item_index"
        )]
        source_ids = [row[0] for row in db.execute(
            "SELECT source_record_id FROM run_items ORDER BY item_index"
        )]

    transport_rows = _jsonl(transport_path)
    physical_call_ids = {
        row["call_id"] for row in transport_rows
        if row.get("kind") == "paid_transport" and row.get("provider") == "hunter"
    }
    physical = [row for row in hunter_calls if row["call_id"] in physical_call_ids]
    assert len(source_ids) == 11 and len(set(source_ids)) == 11
    assert len(physical) == 2
    assert len({row["source_record_id"] for row in physical}) == 2
    assert all(row["state"] == "DONE" for row in physical)
    assert len(hunter_jobs) == 11
    completed_job_ids = {row["job_fingerprint"] for row in hunter_jobs if row["state"] == "DONE"}
    assert len(completed_job_ids) == 2
    allocated_jobs = [row["job_fingerprint"] for row in hunter_allocations if row["job_fingerprint"]]
    assert len(allocated_jobs) == len(set(allocated_jobs)) == 2
    assert set(allocated_jobs) == completed_job_ids
    allocated_sources = {row["source_record_id"] for row in physical}
    assert len(allocated_sources & {row["source_record_id"] for row in records[1:]}) >= 1
    brightdata_pending_sources = {
        row["source_record_id"] for row in brightdata_jobs
        if row["state"] in {"READY", "ALLOCATED", "WAITING_DEPENDENCY"}
    }
    assert allocated_sources & brightdata_pending_sources
    assert str(getattr(getattr(outcome, "status", None), "value", str(outcome))).startswith("SCHEDULER_STALLED")

    evidence_dir = DELIVERY / "evidence" / "P01" / f"hunter_last_slot_verified_{_evidence_stamp()}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    _backup_sqlite(db_path, evidence_dir / "checkpoint.sqlite3")
    shutil.copyfile(transport_path, evidence_dir / "transport.jsonl")
    (evidence_dir / "result.json").write_text(json.dumps({
        "fixture_sha256": fixture_sha256,
        "source_record_ids": source_ids,
        "physical_hunter_calls": physical,
        "hunter_jobs": hunter_jobs,
        "brightdata_jobs": brightdata_jobs,
        "hunter_allocations": hunter_allocations,
        "pipeline_outcome": getattr(getattr(outcome, "status", None), "value", str(outcome)),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_p02_dispatch_release_does_not_infer_no_call_from_success_status(tmp_path, monkeypatch):
    run_id, _db_path = _init_run(tmp_path, monkeypatch, {**{p: 0 for p in PROVIDER_BUDGETS}, "brightdata": 1})
    job = _job(run_id, "brightdata", "fingerprint:real-query")
    candidates = checkpoint.ready_provider_dispatch_candidates(run_id, "brightdata", item_indexes=[0])
    reserved = checkpoint.reserve_provider_dispatch_round(
        run_id=run_id, provider="brightdata", round_ordinal=0,
        candidates=candidates, cap=1,
    )
    assert len(reserved) == 1
    assert checkpoint.release_provider_dispatch_allocation(
        run_id=run_id, provider="brightdata", round_ordinal=0,
        item_index=0, source_record_id="petzoo:test:000", reason="no_physical_dispatch",
    )
    current = checkpoint.load_provider_work_items(run_id, item_index=0)
    assert len(current) == 1 and current[0]["job_fingerprint"] == job["job_fingerprint"]
    assert current[0]["state"] == "READY"
    assert checkpoint.load_run_items(run_id)[0]["paid_state"] == "PENDING"


def test_p02_terminal_follower_then_new_query_uses_only_current_physical_dispatch(tmp_path, monkeypatch):
    _manifest, fixture, fixture_sha256 = load_fixture()
    company = "PETZOO C-REPLAY"
    records = [
        {"item_index": 0, "source_record_id": "petzoo:C:900", "company": company,
         "group": "C", "scenario": "terminal_follower_new_adaptive_query", "subcase": "",
         "website": "", "sector": "pet food and animal nutrition", "legal_name": company},
        {"item_index": 1, "source_record_id": "petzoo:C:901", "company": company,
         "group": "C", "scenario": "terminal_follower_new_adaptive_query", "subcase": "",
         "website": "", "sector": "industrial packaging machinery", "legal_name": company},
        {"item_index": 2, "source_record_id": "petzoo:C:902", "company": "PETZOO C-NEXT",
         "group": "C", "scenario": "terminal_follower_new_adaptive_query", "subcase": "",
         "website": "", "sector": "pet products", "legal_name": "PETZOO C-NEXT"},
    ]
    primary_query = search._primary_queries(company, {
        "legal_name": company, "sector": records[0]["sector"],
    })[0]
    primary_request = search.brightdata_request_fingerprint(primary_query)
    primary_query_fingerprint = search._brightdata_flight_fingerprint(primary_query)
    next_query = search._primary_queries(records[2]["company"], {
        "legal_name": records[2]["legal_name"], "sector": records[2]["sector"],
    })[0]
    next_query_fingerprint = search._brightdata_flight_fingerprint(next_query)
    input_path = tmp_path / "p02-terminal-follower.xlsx"
    transport_path = tmp_path / "transport.jsonl"
    write_input_book(input_path, records)
    router = ReplayPaidTransport(
        records, transport_path,
        route_overrides={
            ("brightdata", primary_request, 1): FakeResponse(200, {"organic": []}),
        },
    )
    runs_dir, _router, _session = install_harness(
        monkeypatch, tmp_path, records, workers=1, router=router,
        http_journal=tmp_path / "http.jsonl",
    )
    outcome = main.run(input_path, allow_paid=True)
    run_root = next(runs_dir.iterdir())
    db_path = run_root / "state" / "progress.sqlite3"
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        calls = [dict(row) for row in db.execute(
            "SELECT c.call_id,c.item_index,i.source_record_id,c.provider,c.operation,"
            "c.request_fingerprint,c.flight_fingerprint,c.state,c.http_started_at "
            "FROM provider_calls c JOIN run_items i ON i.run_id=c.run_id AND i.item_index=c.item_index "
            "WHERE c.provider='brightdata' ORDER BY c.created_at,c.call_id"
        )]
        consumers = [dict(row) for row in db.execute(
            "SELECT i.source_record_id,x.item_index,x.query_fingerprint,x.provider_call_id,x.relation "
            "FROM provider_query_flight_consumers x JOIN run_items i "
            "ON i.run_id=x.run_id AND i.item_index=x.item_index "
            "WHERE x.query_fingerprint=? ORDER BY x.item_index",
            (primary_query_fingerprint,),
        )]
        relations = [dict(row) for row in db.execute(
            "SELECT item_index,attempt_number,provider_call_id,query_fingerprint,execution_generation,relation "
            "FROM paid_attempt_calls WHERE provider='brightdata' ORDER BY item_index,attempt_number,provider_call_id"
        )]
        dispatches = [dict(row) for row in db.execute(
            "SELECT round_ordinal,consumed_call_id,job_fingerprint,state "
            "FROM provider_dispatch_allocations WHERE provider='brightdata' AND consumed_call_id<>''"
        )]
        plans = [dict(row) for row in db.execute(
            "SELECT i.source_record_id,q.query_kind,q.round_ordinal,q.normalized_query "
            "FROM paid_query_plan_entries q JOIN run_items i "
            "ON i.run_id=q.run_id AND i.item_index=q.item_index "
            "WHERE i.source_record_id IN ('petzoo:C:900','petzoo:C:901','petzoo:C:902') "
            "ORDER BY q.item_index,q.query_kind,q.round_ordinal,q.query_ordinal"
        )]
        search_traces = {
            int(item_index): [entry for entry in json.loads(payload).get("__search_trace", []) if isinstance(entry, dict)]
            for item_index, payload in db.execute(
                "SELECT item_index,payload FROM results WHERE item_index IN (0,1)"
            )
        }
        source_ids = [row[0] for row in db.execute(
            "SELECT source_record_id FROM run_items ORDER BY item_index"
        )]

    transport = _jsonl(transport_path)
    physical = [row for row in transport if row.get("kind") == "paid_transport" and row.get("provider") == "brightdata"]
    primary_physical = [row for row in physical if row.get("query_fingerprint") == primary_query_fingerprint]
    follower_receipts = [row for row in consumers if row["source_record_id"] == "petzoo:C:901"]
    new_calls = [row for row in physical if row.get("source_record_id") == "petzoo:C:902" and row.get("query_fingerprint") == next_query_fingerprint]
    new_call_ids = {row["call_id"] for row in new_calls}
    owner_relations = [row for row in relations if row["item_index"] == 2 and row["provider_call_id"] in new_call_ids]
    owner_relations_only = [row for row in owner_relations if row["relation"] == "OWNER"]
    unique_owner_call_ids = {row["provider_call_id"] for row in owner_relations_only}
    round_by_call = {row["consumed_call_id"]: row["round_ordinal"] for row in dispatches}
    primary_owner_ids = {row["call_id"] for row in primary_physical}
    assert len(source_ids) == 3 and len(set(source_ids)) == 3
    assert len(primary_physical) == 1 and primary_physical[0]["source_record_id"] == "petzoo:C:900"
    assert follower_receipts and all(row["relation"] == "INHERITED" for row in follower_receipts)
    assert not any(row.get("source_record_id") == "petzoo:C:901" and row.get("query_fingerprint") == primary_query_fingerprint for row in physical)
    assert len(new_calls) == 1
    assert unique_owner_call_ids == new_call_ids
    assert all(row["execution_generation"] >= 1 for row in owner_relations_only)
    owner_attempts = {
        (row["attempt_number"], row["provider_call_id"]) for row in owner_relations_only
    }
    assert not any(
        row["relation"] == "INHERITED"
        and (row["attempt_number"], row["provider_call_id"]) in owner_attempts
        for row in owner_relations
    )
    assert round_by_call[next(iter(new_call_ids))] == round_by_call[next(iter(primary_owner_ids))]
    assert any(row["source_record_id"] == "petzoo:C:900" and row["query_kind"] == "primary" for row in plans)
    assert any(row["source_record_id"] == "petzoo:C:902" and row["query_kind"] == "targeted" for row in plans)
    assert any(row.get("source") == "adaptive_discovery" for row in search_traces[0])
    assert any(row.get("source") == "adaptive_discovery" for row in search_traces[1])

    evidence_dir = DELIVERY / "evidence" / "P02" / f"terminal_follower_new_query_verified_{_evidence_stamp()}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    _backup_sqlite(db_path, evidence_dir / "checkpoint.sqlite3")
    shutil.copyfile(transport_path, evidence_dir / "transport.jsonl")
    (evidence_dir / "result.json").write_text(json.dumps({
        "fixture_sha256": fixture_sha256,
        "source_record_ids": source_ids,
        "primary_query_fingerprint": primary_query_fingerprint,
        "primary_physical_count": len(primary_physical),
        "follower_receipts": follower_receipts,
        "new_primary_calls": new_calls,
        "owner_relations": owner_relations,
        "dispatches": dispatches,
        "outcome": getattr(getattr(outcome, "status", None), "value", str(outcome)),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_p07_negative_http_receipt_survives_a_fresh_process(tmp_path, monkeypatch):
    _fixture_manifest, _fixture_records, fixture_sha256 = load_fixture()
    run_id, db_path = _init_run(tmp_path, monkeypatch, {provider: 0 for provider in PROVIDER_BUDGETS}, run_id="petzoo-retrieval-resume")
    runtime.set_source_record_id("petzoo:test:000")
    hits = {"count": 0}

    def http_404(_url):
        hits["count"] += 1
        response = requests.Response()
        response.status_code = 404
        error = requests.HTTPError("404")
        error.response = response
        raise error

    monkeypatch.setattr(crawler, "_fetch", http_404)
    assert crawler._try_fetch("https://petzoo-a-000.example/") == (None, "http_404")
    assert hits["count"] == 1
    with sqlite3.connect(db_path) as db:
        saved = db.execute(
            "SELECT state,method,error,attempts FROM retrieval_receipts WHERE run_id=?",
            (run_id,),
        ).fetchall()
    assert len(saved) == 1 and saved[0][:3] == ("FAILED", "http", "http_404")

    child = ROOT / "tests" / "petzoo_receipt_resume.py"
    network_log = tmp_path / "receipt-child-network.jsonl"
    child_output = tmp_path / "receipt-child.json"
    env = dict(os.environ)
    for key in list(env):
        if any(token in key.casefold() for token in ("api_key", "token", "secret", "password")):
            env.pop(key, None)
    env.update({
        "B2B_SOCKET_DENY_JSONL": str(network_log),
        "B2B_COMMAND_ID": "petzoo-p07-receipt-resume",
        "PETZOO_DB": str(db_path), "PETZOO_RUN_ID": run_id,
        "PETZOO_RECEIPT_URL": "https://petzoo-a-000.example/",
        "PETZOO_RESULT": str(child_output),
    })
    command = [str(RUNTIME), str(child)]
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.perf_counter()
    child_process = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    timed_out = False
    try:
        child_stdout, child_stderr = child_process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        timed_out = True
        child_process.kill()
        child_stdout, child_stderr = child_process.communicate()
    duration_seconds = time.perf_counter() - started
    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    evidence_dir = DELIVERY / "evidence" / "P07" / f"fresh_process_verified_{_evidence_stamp()}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "stdout.txt").write_text(child_stdout, encoding="utf-8")
    (evidence_dir / "stderr.txt").write_text(child_stderr, encoding="utf-8")
    if child_output.is_file():
        shutil.copyfile(child_output, evidence_dir / "result.json")
    if network_log.is_file():
        shutil.copyfile(network_log, evidence_dir / "network.jsonl")
    _backup_sqlite(db_path, evidence_dir / "checkpoint.sqlite3")
    from modules.run_context import source_tree_sha256

    command_record = {
        "argv": command, "cwd": str(ROOT.resolve()), "runtime": str(RUNTIME.resolve()),
        "started_at": started_at, "finished_at": finished_at,
        "duration_seconds": duration_seconds, "parent_pid": os.getpid(),
        "child_pid": child_process.pid, "timeout_seconds": 120,
        "exit_code": child_process.returncode, "timed_out": timed_out,
        "fixture_sha256": fixture_sha256,
        "runtime_source_tree_sha256": source_tree_sha256(),
        "environment": {key: "<masked>" for key in env if any(token in key.casefold() for token in ("key", "token", "secret", "password"))},
        "stdout_file": "stdout.txt", "stderr_file": "stderr.txt",
        "result_file": "result.json", "database_file": "checkpoint.sqlite3",
        "network_file": "network.jsonl",
    }
    command_record["artifact_sha256"] = {
        path.name: _sha256(path)
        for path in evidence_dir.iterdir()
        if path.is_file()
    }
    (evidence_dir / "command.json").write_text(json.dumps(command_record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert not timed_out
    assert child_process.returncode == 0, child_stderr
    child_result = json.loads(child_output.read_text(encoding="utf-8"))
    assert child_result == {"result": [None, "http_404"], "physical_transport_called": False}
    assert hits["count"] == 1
    network_rows = [json.loads(line) for line in network_log.read_text(encoding="utf-8").splitlines()]
    assert all(row.get("kind") != "blocked_network" for row in network_rows)


def test_p07_eight_retrieval_replays_are_durable_for_workers_1_and_3(tmp_path, monkeypatch):
    _manifest, fixture, fixture_sha256 = load_fixture()
    cases = [
        ("http_404", "E", "http_404", 1),
        ("http_410", "E", "http_410", 1),
        ("unsupported", "E", "unsupported_content_type:image/png", 1),
        ("http_403_browser", "D", "http_403", 1),
        ("timeout", "E", "timeout", 1 + config.MAX_RETRIES),
        ("http_429", "E", "http_429", 1 + config.MAX_RETRIES),
        ("http_503", "E", "http_503", 1 + config.MAX_RETRIES),
        ("transient_then_success", "E", "success", 2),
    ]
    evidence_root = DELIVERY / "evidence" / "P07" / f"replay_matrix_verified_{_evidence_stamp()}"
    evidence_root.mkdir(parents=True, exist_ok=True)
    for workers in (1, 3):
        for case_id, group, expected, expected_attempts in cases:
            source = next((
                row for row in fixture
                if row["group"] == group
                and (case_id == "http_403_browser" or row["subcase"] == case_id)
            ), None)
            if source is None and case_id in {"http_410", "unsupported", "transient_then_success"}:
                source = {**next(row for row in fixture if row["group"] == "E"), "subcase": case_id}
            assert source is not None, f"P07 case is absent from its independent retrieval matrix: {case_id}"
            record = {**source, "item_index": 0}
            case_root = evidence_root / f"workers_{workers}" / case_id
            case_root.mkdir(parents=True, exist_ok=True)
            work_root = tmp_path / f"workers_{workers}" / case_id
            work_root.mkdir(parents=True, exist_ok=True)
            with monkeypatch.context() as scoped:
                runtime.reset()
                crawler.clear_page_store()
                runs_dir, _router, session = install_harness(
                    scoped, work_root, [record], workers=workers,
                    http_journal=work_root / "http.jsonl",
                )
                run_id, db_path = _init_run(
                    work_root, scoped,
                    {provider: 0 for provider in PROVIDER_BUDGETS},
                    run_id=f"petzoo-p07-{workers}-{case_id}",
                )
                if workers == 3:
                    claim_audit = case_root / "receipt_claim_audit.jsonl"
                    original_claim = checkpoint.claim_retrieval_receipt
                    original_owner_alive = checkpoint._retrieval_owner_is_alive

                    def audited_owner_alive(pid):
                        alive = original_owner_alive(pid)
                        _append_jsonl(claim_audit, {
                            "kind": "owner_liveness", "pid": int(pid),
                            "process_pid": os.getpid(), "alive": alive,
                        })
                        return alive

                    def audited_claim(**kwargs):
                        receipt = original_claim(**kwargs)
                        _append_jsonl(claim_audit, {
                            "kind": "claim", "claim": receipt.get("claim"),
                            "owner_pid": receipt.get("owner_pid"),
                            "owner_token": receipt.get("owner_token"),
                            "attempts": receipt.get("attempts"),
                        })
                        return receipt

                    scoped.setattr(checkpoint, "_retrieval_owner_is_alive", audited_owner_alive)
                    scoped.setattr(checkpoint, "claim_retrieval_receipt", audited_claim)
                runtime.set_source_record_id(record["source_record_id"])
                url = f"https://petzoo-{group.lower()}-{int(record['source_record_id'].rsplit(':', 1)[1]):03d}.example/"
                if case_id == "http_403_browser":
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        http_results = list(executor.map(crawler._try_fetch, [url] * 8))
                    render_calls = {"count": 0}
                    original_render = crawler._try_render

                    def counted_render(render_url):
                        render_calls["count"] += 1
                        return original_render(render_url)

                    scoped.setattr(crawler, "_try_render", counted_render)
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        browser_results = list(executor.map(crawler._try_render_persistent, [url] * 8))
                    assert all(result == (None, "http_403") for result in http_results)
                    assert all(result[0] and result[1] is None for result in browser_results)
                    assert render_calls["count"] == 1
                    observed_error = "http_403"
                else:
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        results = list(executor.map(crawler._try_fetch, [url] * 8))
                    if expected == "success":
                        assert all(result[0] and result[1] is None for result in results)
                        observed_error = ""
                    else:
                        assert all(result == (None, expected) for result in results)
                        observed_error = expected

                root_key = f"{urlparse(url).hostname}/"
                assert session.counts[root_key] == expected_attempts
                with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
                    receipts = db.execute(
                        "SELECT method,state,attempts,error FROM retrieval_receipts "
                        "WHERE run_id=? ORDER BY method",
                        (run_id,),
                    ).fetchall()
                methods = {row[0]: row[1:] for row in receipts}
                assert methods["http"][:2] == (
                    "SUCCEEDED" if expected == "success" else "FAILED",
                    expected_attempts,
                )
                assert methods["http"][2] == observed_error
                if case_id == "http_403_browser":
                    assert methods["browser_render"][:2] == ("SUCCEEDED", 1)
                _backup_sqlite(db_path, case_root / "checkpoint.sqlite3")
                if (work_root / "http.jsonl").is_file():
                    shutil.copyfile(work_root / "http.jsonl", case_root / "http.jsonl")
                (case_root / "result.json").write_text(json.dumps({
                    "run_id": run_id, "workers": workers, "case": case_id,
                    "fixture_sha256": fixture_sha256,
                    "repeat_count": 8, "expected_attempts": expected_attempts,
                    "observed_attempts": session.counts[root_key],
                    "receipts": [list(row) for row in receipts],
                }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_p07_active_leader_waiter_times_out_and_new_run_can_retry(tmp_path, monkeypatch):
    _manifest, fixture, fixture_sha256 = load_fixture()
    source = next(row for row in fixture if row["group"] == "E" and row["subcase"] == "http_404")
    record = {**source, "item_index": 0}
    url = f"https://petzoo-e-{int(source['source_record_id'].rsplit(':', 1)[1]):03d}.example/"
    evidence_dir = DELIVERY / "evidence" / "P07" / f"active_owner_verified_{_evidence_stamp()}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    leader_root = tmp_path / "leader-run"
    leader_root.mkdir()
    entered_transport = threading.Event()
    release_transport = threading.Event()
    clock_state = {"now": 0.0}

    with monkeypatch.context() as scoped:
        runtime.reset()
        crawler.clear_page_store()
        _runs_dir, _router, session = install_harness(
            scoped, leader_root, [record], workers=3,
            http_journal=leader_root / "http.jsonl",
        )
        run_id, db_path = _init_run(
            leader_root, scoped,
            {provider: 0 for provider in PROVIDER_BUDGETS},
            run_id="petzoo-p07-active-owner",
        )
        runtime.set_source_record_id(source["source_record_id"])
        original_get = session.get

        def blocking_get(target_url, **kwargs):
            entered_transport.set()
            if not release_transport.wait(timeout=15):
                raise requests.Timeout("P07 controlled leader timeout")
            return original_get(target_url, **kwargs)

        original_time = crawler.time

        def virtual_monotonic():
            return clock_state["now"]

        def virtual_sleep(seconds):
            clock_state["now"] += max(1.0, float(seconds))

        scoped.setattr(session, "get", blocking_get)
        scoped.setattr(crawler, "time", SimpleNamespace(
            monotonic=virtual_monotonic, sleep=virtual_sleep, time=original_time.time,
        ))
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                leader = executor.submit(crawler._try_fetch, url)
                assert entered_transport.wait(timeout=5)
                waiter = executor.submit(crawler._try_fetch, url)
                assert waiter.result(timeout=5) == (None, "retrieval_owner_wait_timeout")
                release_transport.set()
                assert leader.result(timeout=5) == (None, "http_404")
        finally:
            release_transport.set()

        assert session.counts[f"{urlparse(url).hostname}/"] == 1
        with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
            receipt = db.execute(
                "SELECT state,attempts,error,owner_token,owner_pid FROM retrieval_receipts WHERE run_id=?",
                (run_id,),
            ).fetchone()
        assert receipt == ("FAILED", 1, "http_404", "", 0)
        _backup_sqlite(db_path, evidence_dir / "leader_checkpoint.sqlite3")
        shutil.copyfile(leader_root / "http.jsonl", evidence_dir / "leader_http.jsonl")

    new_run_root = tmp_path / "new-run"
    new_run_root.mkdir()
    with monkeypatch.context() as scoped:
        runtime.reset()
        crawler.clear_page_store()
        _runs_dir, _router, new_session = install_harness(
            scoped, new_run_root, [record], workers=1,
            http_journal=new_run_root / "http.jsonl",
        )
        new_run_id, new_db_path = _init_run(
            new_run_root, scoped,
            {provider: 0 for provider in PROVIDER_BUDGETS},
            run_id="petzoo-p07-new-run",
        )
        runtime.set_source_record_id(source["source_record_id"])
        assert crawler._try_fetch(url) == (None, "http_404")
        assert new_session.counts[f"{urlparse(url).hostname}/"] == 1
        _backup_sqlite(new_db_path, evidence_dir / "new_run_checkpoint.sqlite3")
        shutil.copyfile(new_run_root / "http.jsonl", evidence_dir / "new_run_http.jsonl")
    (evidence_dir / "result.json").write_text(json.dumps({
        "fixture_sha256": fixture_sha256,
        "leader_run_id": run_id,
        "new_run_id": new_run_id,
        "same_run_physical_attempts": session.counts[f"{urlparse(url).hostname}/"],
        "waiter_result": "retrieval_owner_wait_timeout",
        "new_run_physical_attempts": new_session.counts[f"{urlparse(url).hostname}/"],
        "leader_receipt": list(receipt),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_p08_redirect_execution_closure_and_hard_kill_recovery_matrix(tmp_path, monkeypatch):
    _manifest, _fixture, fixture_sha256 = load_fixture()
    run_id, db_path = _init_run(
        tmp_path, monkeypatch, {provider: 0 for provider in PROVIDER_BUDGETS},
        run_id="petzoo-p08-redirect-matrix",
    )
    monkeypatch.setattr(config, "SEARCH_CACHE_MODE", "off")
    source_record_id = "petzoo:p08:000"
    runtime.set_source_record_id(source_record_id)
    evidence_dir = DELIVERY / "evidence" / "P08" / f"redirect_matrix_verified_{_evidence_stamp()}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    original_validate = search.network_guard.validate_public_http_url

    def validate_synthetic_wrapper(url):
        host = (urlparse(str(url)).hostname or "").casefold()
        if host in {"google.com", "www.google.com"} or host.endswith(".example"):
            return True, "synthetic_wrapper_dns"
        return original_validate(url)

    monkeypatch.setattr(search.network_guard, "validate_public_http_url", validate_synthetic_wrapper)

    def response(status, url, headers=None):
        value = requests.Response()
        value.status_code = status
        value.url = url
        value.headers.update(headers or {})
        value.close = lambda: None
        return value

    resolved_chain = [
        response(302, "https://www.google.com/url?opaque=1", {"location": "/out/one"}),
        response(302, "https://www.google.com/out/one", {"location": "https://target.example/final"}),
        response(200, "https://target.example/final"),
    ]
    observed_urls = []

    def route_chain(url, *, timeout):
        del timeout
        observed_urls.append(str(url))
        return resolved_chain.pop(0)

    monkeypatch.setattr(search, "_serp_redirect_transport", route_chain)
    search._add_search_results({}, "PETZOO Redirect", "verified redirect query", [{
        "link": "https://www.google.com/url?opaque=1",
        "provider": "brightdata", "title": "PETZOO Redirect official",
    }])
    assert len(observed_urls) == 3
    assert observed_urls[-1] == "https://target.example/final"

    monkeypatch.setattr(search, "_serp_redirect_transport", lambda _url, *, timeout: response(503, _url))
    search._add_search_results({}, "PETZOO Redirect", "unresolved redirect query", [{
        "link": "https://www.google.com/url?opaque=2", "provider": "brightdata",
    }])

    direct_count_before = checkpoint.load_discovery_attempts(run_id, source_record_id)
    search._add_search_results({}, "PETZOO Redirect", "direct normalized query", [{
        "link": "https://www.google.com/url?q=https%3A%2F%2Ftarget.example%2Fdirect",
        "provider": "brightdata",
    }])
    cached_count_before = checkpoint.load_discovery_attempts(run_id, source_record_id)
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        execution_count_before_replay = db.execute(
            "SELECT COUNT(*) FROM discovery_executions WHERE run_id=?", (run_id,),
        ).fetchone()[0]
    search._add_search_results({}, "PETZOO Redirect", "cached observation query", [{
        "link": "https://www.google.com/url?opaque=cached",
        "resolved_url": "https://target.example/cached",
        "resolution_status": "resolved", "resolution_method": "replay_cached",
        "provider": "brightdata",
    }])
    replay_attempts = checkpoint.load_discovery_attempts(run_id, source_record_id)
    assert len(replay_attempts) == len(cached_count_before) + 1
    assert not replay_attempts[-1].get("reservation", {}).get("execution_id")
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM discovery_executions WHERE run_id=?", (run_id,),
        ).fetchone()[0] == execution_count_before_replay
    assert len(cached_count_before) >= len(direct_count_before)

    monkeypatch.setattr(
        search, "_serp_redirect_transport",
        lambda _url, *, timeout: (_ for _ in ()).throw(RuntimeError("fixture transport exception")),
    )
    search._add_search_results({}, "PETZOO Redirect", "exception redirect query", [{
        "link": "https://www.google.com/url?opaque=exception", "provider": "brightdata",
    }])

    def cancel_route(_url, *, timeout):
        del timeout
        raise KeyboardInterrupt("fixture cancellation")

    monkeypatch.setattr(search, "_serp_redirect_transport", cancel_route)
    with pytest.raises(KeyboardInterrupt):
        search._add_search_results({}, "PETZOO Redirect", "cancel redirect query", [{
            "link": "https://www.google.com/url?opaque=cancel", "provider": "brightdata",
        }])

    attempts = checkpoint.load_discovery_attempts(run_id, source_record_id)
    executions = []
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        executions = db.execute(
            "SELECT execution_id,state,execution_kind FROM discovery_executions "
            "WHERE run_id=? AND source_record_id=? ORDER BY ordinal",
            (run_id, source_record_id),
        ).fetchall()
    assert len(executions) == 4
    assert sorted(row[1] for row in executions) == sorted(["DONE", "FAILED", "FAILED", "UNKNOWN"])
    assert not any(row[1] == "STARTED" for row in executions)
    assert len({row[0] for row in executions}) == 4
    execution_attempt_ids = {
        str(row.get("reservation", {}).get("execution_id") or "")
        for row in attempts if row.get("reservation", {}).get("execution_id")
    }
    assert execution_attempt_ids == {row[0] for row in executions}
    assert any(row.get("semantic_result") == "INCOMPLETE_EXECUTION" for row in attempts)

    crash_root = tmp_path / "hard_kill"
    crash_run_id, crash_db = _init_run(
        crash_root, monkeypatch, {provider: 0 for provider in PROVIDER_BUDGETS},
        run_id="petzoo-p08-hard-kill",
    )
    network_log = evidence_dir / "hard_kill_network.jsonl"
    child_code = r'''\
import os, sys
from pathlib import Path
sys.path.insert(0, "tests")
os.environ["B2B_SOCKET_DENY_JSONL"] = os.environ["PETZOO_NETWORK_LOG"]
os.environ["B2B_COMMAND_ID"] = "petzoo-p08-hard-kill-child"
import conftest
import config
from petzoo_fixture_support import install_harness
from modules import checkpoint, runtime, search
class Setter:
    @staticmethod
    def setattr(target, name, value, raising=True):
        del raising
        setattr(target, name, value)
root = Path(os.environ["PETZOO_CRASH_ROOT"])
config.PROGRESS_DB_FILE = Path(os.environ["PETZOO_CRASH_DB"])
manifest, records, _ = __import__("petzoo_fixture_support").load_fixture()
record = {"item_index": 0, "source_record_id": "petzoo:test:000", "company": "PETZOO crash", "group": "E", "scenario": "hard_kill", "subcase": "", "website": ""}
install_harness(Setter(), root, [record], workers=1, http_journal=root / "http.jsonl")
config.PROGRESS_DB_FILE = Path(os.environ["PETZOO_CRASH_DB"])
checkpoint._SCHEMA_READY.clear()
checkpoint.initialize_schema(config.PROGRESS_DB_FILE)
runtime.configure_durable_run(os.environ["PETZOO_CRASH_RUN_ID"], {p: 0 for p in ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")})
runtime.set_phase("PAID")
runtime.set_source_record_id(record["source_record_id"])
with open(os.environ["PETZOO_NETWORK_LOG"], "a", encoding="utf-8") as stream:
    stream.write('{"kind":"guard_armed","child":true}\n')
def hard_kill(_url, *, timeout):
    del timeout
    os._exit(86)
search._serp_redirect_transport = hard_kill
search._resolve_redirect_item({"raw_url":"https://redirect.example/wrapped","resolution_method":"wrapper_opaque","resolution_status":"unresolved","provider":"brightdata"}, "hard kill query", 0)
os._exit(99)
'''
    child_env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "COMSPEC"}
    }
    child_env.update({
        "B2B_TEST_OFFLINE": "1", "PETZOO_NETWORK_LOG": str(network_log),
        "PETZOO_CRASH_ROOT": str(crash_root), "PETZOO_CRASH_DB": str(crash_db),
        "PETZOO_CRASH_RUN_ID": crash_run_id,
    })
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    child_started = time.perf_counter()
    child = subprocess.Popen(
        [str(RUNTIME), "-c", child_code], cwd=ROOT, env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    timed_out = False
    try:
        child_stdout, child_stderr = child.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        timed_out = True
        child.kill()
        child_stdout, child_stderr = child.communicate()
    duration = time.perf_counter() - child_started
    assert not timed_out, "P08 hard-kill child exceeded its outer timeout"
    assert child.returncode == 86, child_stderr
    recovered = checkpoint.load_discovery_attempts(crash_run_id, "petzoo:test:000")
    assert len(recovered) == 1
    assert recovered[0]["transport_outcome"] == "UNKNOWN"
    assert recovered[0]["semantic_result"] == "INCOMPLETE_EXECUTION"
    assert recovered[0]["reason"] == "restart_incomplete_execution"
    with sqlite3.connect(f"file:{crash_db.resolve().as_posix()}?mode=ro", uri=True) as db:
        open_executions = db.execute(
            "SELECT state FROM discovery_executions WHERE run_id=?", (crash_run_id,),
        ).fetchall()
    assert open_executions == [("STARTED",)]
    _backup_sqlite(db_path, evidence_dir / "completed_checkpoint.sqlite3")
    _backup_sqlite(crash_db, evidence_dir / "hard_kill_checkpoint.sqlite3")
    (evidence_dir / "discovery_attempts.json").write_text(
        json.dumps({"completed": attempts, "hard_kill_recovery": recovered}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for name, content in (("hard_kill_stdout.txt", child_stdout), ("hard_kill_stderr.txt", child_stderr)):
        (evidence_dir / name).write_text(content, encoding="utf-8")
    from modules.run_context import source_tree_sha256
    command_record = {
        "argv": [str(RUNTIME), "-c", child_code], "cwd": str(ROOT.resolve()),
        "runtime": str(RUNTIME.resolve()), "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": duration, "parent_pid": os.getpid(), "child_pid": child.pid,
        "timeout_seconds": 60, "timed_out": timed_out, "exit_code": child.returncode,
        "expected_exit_code": 86, "fixture_sha256": fixture_sha256,
        "runtime_source_tree_sha256": source_tree_sha256(),
        "environment": {key: "<masked>" for key in child_env if any(token in key.casefold() for token in ("key", "token", "secret", "password"))},
    }
    command_record["artifact_sha256"] = {
        path.name: _sha256(path) for path in evidence_dir.iterdir()
        if path.is_file() and path.name != "command.json"
    }
    (evidence_dir / "command.json").write_text(json.dumps(command_record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_p05_real_cli_stalls_after_first_no_progress_round(tmp_path, monkeypatch):
    _manifest, fixture, fixture_sha256 = load_fixture()
    row = next(item for item in fixture if item["group"] == "B")
    record = {**row, "item_index": 0}
    input_path = tmp_path / "petzoo-smoke.xlsx"
    write_input_book(input_path, [record])
    transport_log = tmp_path / "paid-transport.jsonl"
    http_log = tmp_path / "crawler-http.jsonl"
    router = ReplayPaidTransport([record], transport_log)
    runs_dir, _router, _session = install_harness(
        monkeypatch, tmp_path, [record], workers=1, router=router, http_journal=http_log,
    )

    initialize_schema = checkpoint.initialize_schema

    def install_scheduler_fault(path=None):
        result = initialize_schema(path)
        target = Path(path or config.PROGRESS_DB_FILE)
        fault_fingerprint = "f" * 64
        with sqlite3.connect(target) as db:
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS p05_fault_dispatch_job_binding "
                "AFTER INSERT ON provider_dispatch_allocations "
                "WHEN NEW.provider='brightdata' AND NEW.state='RESERVED' AND NEW.round_ordinal>=1 "
                "BEGIN UPDATE provider_dispatch_allocations SET job_fingerprint='" + fault_fingerprint + "' "
                "WHERE run_id=NEW.run_id AND provider=NEW.provider "
                "AND round_ordinal=NEW.round_ordinal AND item_index=NEW.item_index; END"
            )
            db.commit()
        return result

    monkeypatch.setattr(checkpoint, "initialize_schema", install_scheduler_fault)

    monkeypatch.setattr(main, "_ensure_safe_project_runtime", lambda _argv: None)
    monkeypatch.setattr(main, "_apply_saved_resolver_configuration", lambda: None)
    monkeypatch.setattr(main, "configure_apis_interactively", lambda: None)
    cli_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cli_started_epoch = time.perf_counter()
    cli_exit = main.cli(["--input", str(input_path), "--allow-paid", "--non-interactive"])
    cli_duration_seconds = time.perf_counter() - cli_started_epoch
    cli_finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1
    run_root = run_dirs[0]
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    db_path = run_root / "state" / "progress.sqlite3"
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        run_id = str(db.execute("SELECT run_id FROM runs").fetchone()[0])
        physical = db.execute(
            "SELECT provider,COUNT(*) FROM provider_calls WHERE run_id=? AND http_started_at<>'' GROUP BY provider ORDER BY provider",
            (run_id,),
        ).fetchall()
        work = db.execute(
            "SELECT COUNT(*),COUNT(DISTINCT job_fingerprint) FROM provider_work_items WHERE run_id=?",
            (run_id,),
        ).fetchone()
        nonflight_links = db.execute(
            "SELECT c.call_id,COUNT(DISTINCT a.attempt_number) FROM provider_calls c "
            "JOIN paid_attempt_calls a ON a.run_id=c.run_id AND a.call_id=c.call_id "
            "WHERE c.run_id=? AND c.flight_fingerprint='' GROUP BY c.call_id",
            (run_id,),
        ).fetchall()
        run_state = db.execute(
            "SELECT phase,termination_reason,stopped_at FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        source_count = db.execute(
            "SELECT COUNT(*) FROM run_items WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        ready_work = db.execute(
            "SELECT provider,state,job_fingerprint FROM provider_work_items "
            "WHERE run_id=? AND state='READY'",
            (run_id,),
        ).fetchall()
        faulted_allocations = db.execute(
            "SELECT provider,state,job_fingerprint,released_reason FROM provider_dispatch_allocations "
            "WHERE run_id=? AND provider='brightdata'",
            (run_id,),
        ).fetchall()
        snapshots = db.execute(
            "SELECT round_ordinal,kind,snapshot_json FROM scheduler_progress_snapshots "
            "WHERE run_id=? AND phase='PAID' ORDER BY round_ordinal,kind",
            (run_id,),
        ).fetchall()
    journal = [json.loads(line) for line in transport_log.read_text(encoding="utf-8").splitlines()]
    starts = [entry for entry in journal if entry.get("kind") == "paid_transport"]
    terminals = [entry for entry in journal if entry.get("kind") == "paid_transport_terminal"]
    assert cli_exit == 23
    assert manifest["complete"] is False
    assert manifest["scheduler_status"] == pipeline_runner.PipelineOutcomeStatus.SCHEDULER_STALLED.value
    assert manifest["runtime_source_tree_sha256"]
    assert len(physical) >= 1 and sum(count for _provider, count in physical) == len(starts)
    assert len(starts) == len(terminals) == len({entry["call_id"] for entry in starts})
    assert work[0] == work[1]
    assert nonflight_links and all(attempt_count == 1 for _call_id, attempt_count in nonflight_links)
    assert all(entry["request_fingerprint"] and entry["attempt_ordinal"] >= 1 for entry in journal)
    assert run_state[0] == "PAID" and run_state[1] and run_state[2]
    assert source_count == 1
    assert ready_work and all(row[0] == "brightdata" and row[1] == "READY" for row in ready_work)
    blocked_round_allocations = [row for row in faulted_allocations if row[1] == "RELEASED"]
    assert blocked_round_allocations and all(
        row[2] == "f" * 64 and row[3] == "no_physical_dispatch"
        for row in blocked_round_allocations
    )
    starts_by_round = {int(row[0]) for row in snapshots if row[1] == "START"}
    ends_by_round = {int(row[0]) for row in snapshots if row[1] == "END"}
    assert starts_by_round == ends_by_round and starts_by_round
    last_round = max(starts_by_round)
    end_payloads = {
        int(row[0]): json.loads(row[2])
        for row in snapshots
        if row[1] == "END"
    }
    progress_keys = (
        "new_physical_calls",
        "terminal_job_delta",
        "resolved_dependency_jobs",
        "state_changes",
    )
    assert all(any(end_payloads[ordinal][key] for key in progress_keys) for ordinal in ends_by_round if ordinal < last_round)
    last_end = json.loads(next(row[2] for row in snapshots if int(row[0]) == last_round and row[1] == "END"))
    assert all(not last_end[key] for key in progress_keys)
    assert last_end["pending_jobs"]
    assert any(int(budget["effective_limit"]) > int(budget["reserved_total"]) for budget in last_end["budgets"])
    assert run_state[1] == "no_durable_scheduler_progress_after_paid_round"
    partial_dir = Path(manifest["partial_recovery"]["directory"])
    partial = json.loads((partial_dir / "partial_results.json").read_text(encoding="utf-8"))
    assert partial["partial"] is True and partial["accepted_as_complete"] is False
    assert len(partial["rows"]) == 1
    assert partial["rows"][0]["source_record_id"] == record["source_record_id"]
    assert (partial_dir / "partial_manifest.json").is_file()
    assert fixture_sha256

    gate_dir = DELIVERY / "evidence" / "P05" / "cli_runs" / run_id
    gate_dir.mkdir(parents=True, exist_ok=False)
    _backup_sqlite(db_path, gate_dir / "progress.sqlite3")
    shutil.copyfile(run_root / "manifest.json", gate_dir / "manifest.json")
    shutil.copyfile(input_path, gate_dir / "synthetic_input.xlsx")
    shutil.copyfile(transport_log, gate_dir / "transport.jsonl")
    shutil.copyfile(http_log, gate_dir / "crawler_http.jsonl")
    partial_evidence = gate_dir / "partial_recovery"
    partial_evidence.mkdir()
    shutil.copyfile(partial_dir / "partial_results.json", partial_evidence / "partial_results.json")
    shutil.copyfile(partial_dir / "partial_manifest.json", partial_evidence / "partial_manifest.json")
    round_evidence = {
        str(int(row[0])): json.loads(row[2])
        for row in snapshots
        if row[1] == "END"
    }
    (gate_dir / "scheduler_rounds.json").write_text(
        json.dumps(round_evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    evidence_files = sorted(path for path in gate_dir.rglob("*") if path.is_file())
    (gate_dir / "evidence_hashes.json").write_text(
        json.dumps({path.relative_to(gate_dir).as_posix(): _sha256(path) for path in evidence_files}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    from modules.run_context import source_tree_sha256

    (gate_dir / "command.json").write_text(
        json.dumps({
            "kind": "in_process_main_cli_acceptance",
            "pytest_argv": [sys.executable, *sys.argv],
            "process_id": os.getpid(),
            "actual_cli_argv": ["main.cli", "--input", str(input_path), "--allow-paid", "--non-interactive"],
            "cwd": str(PROJECT_ROOT.resolve()),
            "runtime": str(Path(sys.executable).resolve()),
            "environment": {key: "<masked>" for key in os.environ if any(
                term in key.casefold() for term in ("key", "token", "secret", "password")
            )},
            "runtime_source_tree_sha256": source_tree_sha256(),
            "fixture_sha256": fixture_sha256,
            "run_id": run_id,
            "cli_exit_code": cli_exit,
            "started_at": cli_started_at,
            "finished_at": cli_finished_at,
            "duration_seconds": cli_duration_seconds,
            "timed_out": False,
            "source_count": source_count,
            "physical_transport_count": len(starts),
            "first_no_progress_round": last_round,
            "no_later_round": True,
            "artifacts": "evidence_hashes.json",
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(destination) as backup_db:
            source_db.backup(backup_db)


def test_p06_lower_quality_paid_result_preserves_content_and_durable_work_across_fresh_process(tmp_path):
    _manifest, fixture, fixture_sha256 = load_fixture()
    source_record = next(row for row in fixture if row["source_record_id"] == "petzoo:A:000")
    record = {**source_record, "item_index": 0}
    input_path = tmp_path / "p06-input.xlsx"
    write_input_book(input_path, [record])
    gate_root = DELIVERY / "evidence" / "P06"
    gate_root.mkdir(parents=True, exist_ok=True)
    case_root = gate_root / f"fresh_resume_{_evidence_stamp()}"
    case_root.mkdir(parents=True, exist_ok=False)
    safe_environment_names = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "COMSPEC",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in safe_environment_names}
    environment.update({
        "B2B_TEST_OFFLINE": "1",
        "BRIGHTDATA_API_KEY": "fake-petzoo-brightdata",
        "GOOGLE_PLACES_API_KEY": "fake-petzoo-places",
        "BRANDFETCH_CLIENT_ID": "fake-petzoo-brandfetch",
        "HUNTER_API_KEY": "fake-petzoo-hunter",
        "OPENROUTER_API_KEY": "fake-petzoo-llm",
    })

    def run_child(mode: str, run_root: Path | None, suffix: str) -> dict:
        transport_path = case_root / f"{suffix}_transport.jsonl"
        http_path = case_root / f"{suffix}_http.jsonl"
        network_path = case_root / f"{suffix}_network.jsonl"
        result_path = case_root / f"{suffix}_result.json"
        command_id = f"petzoo-p06-{_evidence_stamp()}-{suffix}"
        child_environment = {
            **environment, "B2B_COMMAND_ID": command_id,
            "B2B_SOCKET_DENY_JSONL": str(network_path),
        }
        command = [
            str(RUNTIME), str(ROOT / "tests" / "petzoo_p06_child_runner.py"),
            "--mode", mode, "--workspace", str(tmp_path), "--input", str(input_path),
            "--transport-journal", str(transport_path), "--http-journal", str(http_path),
            "--network-journal", str(network_path), "--result", str(result_path),
        ]
        if run_root is not None:
            command.extend(["--run-root", str(run_root)])
        started = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        started_clock = time.perf_counter()
        process = subprocess.Popen(
            command, cwd=ROOT, env=child_environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            else:
                process.kill()
            stdout, stderr = process.communicate()
        stdout_path = case_root / f"{suffix}_stdout.txt"
        stderr_path = case_root / f"{suffix}_stderr.txt"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        record_path = case_root / f"{suffix}_command.json"
        command_record = {
            "argv": command, "cwd": str(ROOT.resolve()), "runtime": str(RUNTIME.resolve()),
            "command_id": command_id, "fixture_sha256": fixture_sha256,
            "environment": {key: "<masked>" for key in child_environment if any(
                term in key.casefold() for term in ("key", "token", "secret", "password")
            )},
            "started_at": started, "ended_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "duration_seconds": time.perf_counter() - started_clock,
            "pid": process.pid, "timed_out": timed_out, "exit_code": process.returncode,
            "artifact_sha256": {
                path.name: _sha256(path) for path in (
                    stdout_path, stderr_path, transport_path, http_path, network_path, result_path,
                ) if path.is_file()
            },
        }
        record_path.write_text(json.dumps(command_record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        assert not timed_out and process.returncode == 0, f"P06 {mode} child failed: {stderr}"
        return json.loads(result_path.read_text(encoding="utf-8"))

    free = run_child("free", None, "free")
    assert free["outcome"] == pipeline_runner.PipelineOutcomeStatus.PAID_PENDING_APPROVAL.value
    assert free["fixture_sha256"] == fixture_sha256
    assert free["payload"].get("website") and free["payload"].get("publication_eligible") is False
    parent = Path(free["run_root"])
    parent_manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    source_id = record["source_record_id"]
    approval = {
        "approved": True,
        "limits": {provider: (1 if provider == "brightdata" else 0) for provider in checkpoint.CANONICAL_PROVIDERS},
        "parent_run_id": parent_manifest["run_id"],
        "parent_manifest_sha256": _sha256(parent / "manifest.json"),
        "checkpoint_sha256": parent_manifest.get("_continuation_checkpoint_sha256") or parent_manifest["files"]["recovery_state.sqlite3"]["sha256"],
        "paid_source_ids_sha256": hashlib.sha256(run_context.canonical_json([source_id]).encode("utf-8")).hexdigest(),
    }
    approval_path = case_root / "approval.json"
    approval_path.write_text(json.dumps(approval, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    child_manifest = prepare_paid_continuation(parent, approval_path, tmp_path / "p06-continuation")
    child_root = tmp_path / "p06-continuation" / "runs" / child_manifest["run_id"]
    resumed = run_child("resume", child_root, "resume")
    assert resumed["fixture_sha256"] == fixture_sha256
    assert resumed["payload"].get("website") == free["payload"].get("website")
    assert resumed["payload"].get("score") == free["payload"].get("score")
    assert resumed["paid_brightdata_calls"] >= 1
    assert resumed["paid_brightdata_work_done"] >= 1
    assert resumed["paid_attempt_call_links"] >= 1
    assert resumed["http_404_count"] >= 1, "fresh-process paid execution did not observe the controlled lower-quality retrieval"
    assert resumed["run_id"] == child_manifest["run_id"]


def test_p11_group_a_supplied_site_is_real_no_call(tmp_path, monkeypatch):
    _manifest, fixture, _fixture_sha256 = load_fixture()
    source = next(row for row in fixture if row["group"] == "A")
    record = {**source, "item_index": 0}
    input_path = tmp_path / "group-a.xlsx"
    write_input_book(input_path, [record])
    router = ReplayPaidTransport([record], tmp_path / "transport.jsonl")
    runs_dir, _router, _session = install_harness(
        monkeypatch, tmp_path, [record], workers=1, router=router,
        http_journal=tmp_path / "http.jsonl",
    )
    outcome = main.run(input_path, allow_paid=True)
    run_root = next(runs_dir.iterdir())
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    with sqlite3.connect(f"file:{(run_root / 'state' / 'progress.sqlite3').resolve()}?mode=ro", uri=True) as db:
        item = db.execute("SELECT paid_required,paid_state FROM run_items").fetchone()
        paid_calls = db.execute("SELECT COUNT(*) FROM provider_calls WHERE phase='PAID'").fetchone()[0]
        no_call = db.execute(
            "SELECT COUNT(*) FROM paid_no_call_evidence WHERE evidence_kind='supplied_website_publishable_at_paid_entry' AND publication_eligible=1"
        ).fetchone()[0]
        attempts = db.execute(
            "SELECT result,evidence_kind FROM paid_attempts WHERE phase='PAID'"
        ).fetchall()
        work_states = db.execute(
            "SELECT DISTINCT state FROM provider_work_items"
        ).fetchall()
        results = db.execute("SELECT payload FROM results").fetchone()
    payload = json.loads(results[0])
    assert getattr(outcome.status, "value", str(outcome.status)) == pipeline_runner.PipelineOutcomeStatus.COMPLETE.value
    assert manifest["complete"] is True
    assert item == (1, "DONE")
    assert paid_calls == 0
    assert no_call == 1
    assert attempts == [("NO_CALL_NEEDED", "supplied_website_publishable_at_paid_entry")]
    assert work_states and set(work_states) == {("NOT_REQUIRED",)}
    assert payload["publication_eligible"] is True
    assert payload["website"].rstrip("/") == source["website"].rstrip("/")
    known_evaluation = payload["known_website_evaluation"]
    assert publication_policy.decide_supplied_website_no_call(
        known_evaluation, source_record_id=source["source_record_id"],
    )["publishable"] is True
    assert publication_policy.decide_supplied_website_no_call(
        known_evaluation, source_record_id=source["source_record_id"] + ":wrong",
    )["publishable"] is False


def test_p11_group_h_reaches_linkedin_and_llm_through_pipeline(tmp_path, monkeypatch):
    _manifest, fixture, _fixture_sha256 = load_fixture()
    records = [
        {**row, "item_index": index}
        for index, row in enumerate(row for row in fixture if row["group"] == "H")
    ]
    input_path = tmp_path / "group-h.xlsx"
    transport_path = tmp_path / "transport.jsonl"
    write_input_book(input_path, records)
    router = ReplayPaidTransport(records, transport_path)
    runs_dir, _router, _session = install_harness(
        monkeypatch, tmp_path, records, workers=1, router=router,
        http_journal=tmp_path / "http.jsonl",
    )
    # This acceptance case exercises the LinkedIn and LLM gates specifically.
    # Keep unrelated enrichment providers out so their population-scaled
    # sub-budgets cannot leave unrelated BLOCKED_BUDGET jobs in this mini-run.
    monkeypatch.setattr(config, "ENABLE_GOOGLE_PLACES", False)
    monkeypatch.setattr(config, "ENABLE_BRANDFETCH_DOMAIN_SEARCH", False)
    monkeypatch.setattr(config, "ENABLE_HUNTER_DOMAIN_FINDER", False)
    outcome = main.run(input_path, allow_paid=True)
    run_root = next(runs_dir.iterdir())
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    db_path = run_root / "state" / "progress.sqlite3"
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        providers = {
            row[0] for row in db.execute(
                "SELECT DISTINCT provider FROM provider_calls WHERE http_started_at IS NOT NULL"
            )
        }
        work_states = [row[0] for row in db.execute("SELECT state FROM provider_work_items")]
        item_states = [row[0] for row in db.execute("SELECT paid_state FROM run_items")]
    transport_rows = _jsonl(transport_path)
    assert {"linkedin", "llm"} <= providers
    assert {"linkedin", "llm"} <= {row["provider"] for row in transport_rows}
    assert getattr(outcome.status, "value", str(outcome.status)) == pipeline_runner.PipelineOutcomeStatus.COMPLETE.value
    assert manifest["complete"] is True
    assert work_states and set(work_states) <= {"DONE", "FAILED", "NOT_REQUIRED"}
    assert set(item_states) == {"DONE"}


def test_p11_group_c_real_terminal_follower_under_small_fixture_budget(tmp_path, monkeypatch):
    _manifest, fixture, _fixture_sha256 = load_fixture()
    records = [
        {**row, "item_index": index}
        for index, row in enumerate(sorted(
            (row for row in fixture if row["group"] == "C" and row["source_record_id"] in {"petzoo:C:000", "petzoo:C:001"}),
            key=lambda row: row["source_record_id"],
        ))
    ]
    queries = [search._primary_queries(record["company"], record) for record in records]
    assert queries[0][0] == queries[1][0] and queries[0][1] != queries[1][1]
    input_path = tmp_path / "group-c.xlsx"
    transport_path = tmp_path / "transport.jsonl"
    write_input_book(input_path, records)
    router, route_report = petzoo_group_c_transport(records, transport_path)
    runs_dir, _router, _session = install_harness(
        monkeypatch, tmp_path, records, workers=1, router=router,
        http_journal=tmp_path / "http.jsonl",
    )
    outcome = main.run(input_path, allow_paid=True)
    run_root = next(runs_dir.iterdir())
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    db_path = run_root / "state" / "progress.sqlite3"
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        calls = db.execute(
            "SELECT call_id,item_index,provider,request_fingerprint,flight_fingerprint,state,http_started_at FROM provider_calls WHERE run_id=(SELECT run_id FROM runs) ORDER BY created_at,call_id"
        ).fetchall()
        relations = db.execute(
            "SELECT item_index,provider_call_id,provider,query_fingerprint,relation FROM paid_attempt_calls WHERE run_id=(SELECT run_id FROM runs) ORDER BY item_index,provider_call_id"
        ).fetchall()
        followers = db.execute(
            "SELECT item_index,provider_call_id,relation FROM provider_query_flight_consumers WHERE run_id=(SELECT run_id FROM runs) AND relation='INHERITED'"
        ).fetchall()
        adaptive_plan = db.execute(
            "SELECT normalized_query FROM paid_query_plan_entries WHERE run_id=(SELECT run_id FROM runs) "
            "AND item_index=1 AND query_kind='adaptive'"
        ).fetchall()
        allocations = db.execute(
            "SELECT round_ordinal,state,consumed_call_id FROM provider_dispatch_allocations "
            "WHERE run_id=(SELECT run_id FROM runs) AND provider='brightdata' AND item_index=1"
        ).fetchall()
    transport_rows = [row for row in _jsonl(transport_path) if row.get("kind") == "paid_transport"]
    call_by_id = {row[0]: row for row in calls}
    assert route_report["shared_terminal_fingerprint_count"] > 0
    assert followers and any(row[0] == 1 and row[2] == "INHERITED" for row in followers)
    assert any(row["item_index"] == 0 and row["request_fingerprint"] == search.brightdata_request_fingerprint(queries[0][0]) for row in transport_rows)
    adaptive_fingerprints = {
        search.brightdata_request_fingerprint(row[0]) for row in adaptive_plan
    }
    assert any(
        row["item_index"] == 1 and row["request_fingerprint"] in adaptive_fingerprints
        and any(allocation[0] == 0 and allocation[1] == "DONE" and allocation[2] == row["call_id"] for allocation in allocations)
        for row in transport_rows
    ), "terminal follower did not dispatch its distinct durably planned query in the same round"
    assert all(row["call_id"] in call_by_id and call_by_id[row["call_id"]][2] == row["provider"] for row in transport_rows)
    assert getattr(outcome.status, "value", str(outcome.status)) == pipeline_runner.PipelineOutcomeStatus.PAID_MANUAL_AUTHORIZATION_REVIEW_REQUIRED.value
    assert manifest["manual_review_item_indexes"] == [1]


def test_p11_group_f_uses_last_hunter_slots_for_distinct_eligible_sources(tmp_path, monkeypatch):
    _manifest, fixture, _fixture_sha256 = load_fixture()
    records = [
        {**row, "item_index": index}
        for index, row in enumerate(sorted(
            (row for row in fixture if row["group"] == "F"),
            key=lambda row: row["source_record_id"],
        ))
    ]
    input_path = tmp_path / "group-f.xlsx"
    transport_path = tmp_path / "transport.jsonl"
    write_input_book(input_path, records)
    router = ReplayPaidTransport(records, transport_path)
    runs_dir, _router, _session = install_harness(
        monkeypatch, tmp_path, records, workers=1, router=router,
        http_journal=tmp_path / "http.jsonl",
    )
    outcome = main.run(input_path, allow_paid=True)
    run_root = next(runs_dir.iterdir())
    db_path = run_root / "state" / "progress.sqlite3"
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        hunter_jobs = db.execute(
            "SELECT item_index,source_record_id,state,job_fingerprint FROM provider_work_items WHERE run_id=(SELECT run_id FROM runs) AND provider='hunter' ORDER BY source_record_id"
        ).fetchall()
        hunter_calls = db.execute(
            "SELECT item_index,call_id,state FROM provider_calls WHERE run_id=(SELECT run_id FROM runs) AND provider='hunter' ORDER BY item_index,call_id"
        ).fetchall()
        hunter_budget = db.execute(
            "SELECT effective_limit,reserved_total,reserved,completed,failed,unknown FROM provider_usage WHERE run_id=(SELECT run_id FROM runs) AND provider='hunter'"
        ).fetchone()
        allocations = db.execute(
            "SELECT item_index,source_record_id,state,job_fingerprint FROM provider_dispatch_allocations WHERE run_id=(SELECT run_id FROM runs) AND provider='hunter' ORDER BY round_ordinal,item_index"
        ).fetchall()
        owner_index_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT provider_call_id FROM paid_attempt_calls "
            "WHERE run_id=? AND provider=? AND provider_call_id=? AND query_fingerprint=? "
            "AND execution_generation=? AND relation='OWNER'",
            ("", "hunter", "", "", 1),
        ).fetchall()
    hunter_transport = [row for row in _jsonl(transport_path) if row.get("kind") == "paid_transport" and row["provider"] == "hunter"]
    assert len(hunter_jobs) >= 15
    effective_limit = hunter_budget[0]
    assert effective_limit > 0
    assert len(hunter_calls) == len(hunter_transport) == effective_limit
    assert len({row["source_record_id"] for row in hunter_transport}) == effective_limit
    assert hunter_budget == (effective_limit, effective_limit, 0, effective_limit, 0, 0)
    assert len({row[3] for row in allocations}) == len(allocations)
    assert any("ix_paid_attempt_calls_generation_owner" in str(row[3]) for row in owner_index_plan)
    called_sources = {row["source_record_id"] for row in hunter_transport}
    assert called_sources == {row["source_record_id"] for row in records[:effective_limit]}
    assert len(set(row[1] for row in hunter_jobs) - called_sources) >= 1
    assert getattr(outcome.status, "value", str(outcome.status)) == pipeline_runner.PipelineOutcomeStatus.COMPLETE.value


def _run_single_fixture_source(tmp_path, monkeypatch, source):
    record = {**source, "item_index": 0}
    input_path = tmp_path / "single-source.xlsx"
    transport_path = tmp_path / "transport.jsonl"
    write_input_book(input_path, [record])
    router = ReplayPaidTransport([record], transport_path)
    runs_dir, _router, _session = install_harness(
        monkeypatch, tmp_path, [record], workers=1, router=router,
        http_journal=tmp_path / "http.jsonl",
    )
    outcome = main.run(input_path, allow_paid=True)
    run_root = next(runs_dir.iterdir())
    db_path = run_root / "state" / "progress.sqlite3"
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        calls = db.execute(
            "SELECT provider,state,http_started_at,result_ref FROM provider_calls ORDER BY created_at,call_id"
        ).fetchall()
        work = db.execute(
            "SELECT provider,state,terminal_reason,call_id FROM provider_work_items ORDER BY provider,request_fingerprint"
        ).fetchall()
        items = db.execute("SELECT paid_state FROM run_items").fetchall()
        flights = db.execute(
            "SELECT provider,state,execution_generation,result_json FROM provider_query_flights ORDER BY provider,query_fingerprint"
        ).fetchall()
    return outcome, json.loads((run_root / "manifest.json").read_text(encoding="utf-8")), calls, work, items, _jsonl(transport_path), flights


def test_p04_real_pipeline_post_send_unknown_is_not_retried_or_replaced_by_ddgs(tmp_path, monkeypatch):
    _manifest, fixture, _fixture_sha256 = load_fixture()
    source = next(row for row in fixture if row["source_record_id"] == "petzoo:G:000")
    outcome, manifest, calls, work, items, transport, _flights = _run_single_fixture_source(tmp_path, monkeypatch, source)
    starts = [row for row in transport if row.get("kind") == "paid_transport"]
    assert len(starts) == 1
    assert starts[0]["provider"] == "brightdata"
    assert len(calls) == 1 and calls[0][0:2] == ("brightdata", "UNKNOWN") and calls[0][2]
    assert any(row[0] == "brightdata" and row[1] == "UNKNOWN" for row in work)
    assert all(row[0] == "brightdata" and row[1] in {"UNKNOWN", "READY"} for row in work)
    assert all(not row[3] for row in work if row[1] == "READY")
    assert items == [("UNKNOWN",)]
    assert manifest["complete"] is False
    assert getattr(outcome.status, "value", str(outcome.status)) != pipeline_runner.PipelineOutcomeStatus.COMPLETE.value
    assert len([row for row in transport if row.get("kind") == "paid_transport_terminal"]) == 1


def test_p09_provider_receipt_inheritance_and_no_call_proof_matrix(tmp_path, monkeypatch):
    from modules import company_resolvers, google_places

    record = {
        "item_index": 0, "source_record_id": "petzoo:test:000",
        "company": "ACME Pet Products", "group": "F",
        "scenario": "provider_wrapper_duplicate", "subcase": "", "website": "",
    }
    runs_dir, router, _session = install_harness(
        monkeypatch, tmp_path / "wrappers", [record], workers=1,
        http_journal=tmp_path / "wrappers" / "http.jsonl",
    )
    budgets = {provider: 0 for provider in PROVIDER_BUDGETS}
    budgets.update({"brandfetch": 1, "google_places": 1, "hunter": 1})
    run_id, db_path = _init_run(
        tmp_path / "wrappers", monkeypatch, budgets,
        run_id="petzoo-p09-wrapper-reuse",
    )
    assert checkpoint.claim_item(run_id=run_id, item_index=0, phase="PAID")
    checkpoint.begin_paid_attempt(
        run_id=run_id, item_index=0, attempt_number=1,
        provider_plan=("brandfetch", "google_places", "hunter"),
    )
    runtime.set_item_context(0, "P09 wrapper duplicate")
    runtime.set_source_record_id(record["source_record_id"])
    runtime.set_phase("PAID")
    wrappers = {
        "brandfetch": lambda: company_resolvers.brandfetch_domains(record["company"]),
        "google_places": lambda: google_places.search_company(record["company"]),
        "hunter": lambda: company_resolvers.hunter_domains(record["company"]),
    }
    places_variants = google_places.scorer.search_name_variants(record["company"])
    places_query = (places_variants[0] if places_variants else record["company"]).strip()[:100]
    requests_by_provider = {
        "brandfetch": ("domain_search", runtime.request_fingerprint(
            "brandfetch", "domain_search", {"company": record["company"]},
        )),
        "google_places": ("text_search", runtime.request_fingerprint(
            "google_places", "text_search", {"query": places_query, "region": "TR"},
        )),
        "hunter": ("domain_search", runtime.request_fingerprint(
            "hunter", "domain_search", {"company": record["company"]},
        )),
    }
    for provider, (operation, fingerprint) in requests_by_provider.items():
        job = checkpoint.ensure_provider_work_item(
            run_id=run_id, item_index=0, source_record_id=record["source_record_id"],
            provider=provider, operation=operation, request_fingerprint=fingerprint,
            need_class="identity",
        )
        candidates = checkpoint.ready_provider_dispatch_candidates(
            run_id, provider, item_indexes=[0],
        )
        assert [row["job_fingerprint"] for row in candidates] == [job["job_fingerprint"]]
        allocations = checkpoint.reserve_provider_dispatch_round(
            run_id=run_id, provider=provider, round_ordinal=0,
            candidates=candidates, cap=1,
        )
        assert len(allocations) == 1
    runtime.set_provider_dispatch_rounds({provider: 0 for provider in wrappers})
    first_results = {}
    second_results = {}
    for provider, call in wrappers.items():
        first_results[provider] = call()
        second_results[provider] = call()
        assert first_results[provider].call_ids, (
            f"{provider} did not dispatch: state={first_results[provider].state}, "
            f"reason={first_results[provider].reason}"
        )
        first_id = first_results[provider].call_ids[0]
        assert first_results[provider].state in {"COMPLETED", "EMPTY"}
        assert second_results[provider].call_ids == (first_id,)
        assert second_results[provider].call_relations == {first_id: "INHERITED"}
    owner_call_ids = sorted({result.call_ids[0] for result in first_results.values()})
    checkpoint.record_paid_attempt(
        run_id=run_id, item_index=0, attempt_number=1, result="COMPLETED",
        reason="controlled wrapper responses completed",
        call_ids=owner_call_ids,
        call_relations={call_id: "OWNER" for call_id in owner_call_ids},
    )
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        wrapper_calls = db.execute(
            "SELECT provider,COUNT(*),SUM(http_started_at<>'') FROM provider_calls "
            "WHERE run_id=? GROUP BY provider ORDER BY provider", (run_id,),
        ).fetchall()
        relations = db.execute(
            "SELECT provider,relation,provider_call_id FROM paid_attempt_calls "
            "WHERE run_id=? AND item_index=0 ORDER BY provider", (run_id,),
        ).fetchall()
    assert wrapper_calls == [("brandfetch", 1, 1), ("google_places", 1, 1), ("hunter", 1, 1)]
    assert [(row[0], row[1]) for row in relations] == [
        ("brandfetch", "OWNER"), ("google_places", "OWNER"), ("hunter", "OWNER"),
    ]
    transport_rows = [row for row in _jsonl(router.journal_path) if row.get("kind") == "paid_transport"]
    assert {row["provider"] for row in transport_rows} == set(wrappers)
    assert len(transport_rows) == 3

    _fixture_manifest, fixture, fixture_sha256 = load_fixture()
    supplied = {**next(row for row in fixture if row["group"] == "A"), "item_index": 0}
    monkeypatch.undo()
    runtime.reset()
    crawler.clear_page_store()
    no_call_root = tmp_path / "no_call"
    no_call_root.mkdir(parents=True, exist_ok=True)
    no_call_runs, _no_call_router, _no_call_session = install_harness(
        monkeypatch, no_call_root, [supplied], workers=1,
        http_journal=no_call_root / "http.jsonl",
    )
    input_path = no_call_root / "supplied_site.xlsx"
    write_input_book(input_path, [supplied])
    outcome = main.run(input_path, allow_paid=True)
    no_call_run_root = next(no_call_runs.iterdir())
    no_call_manifest = json.loads((no_call_run_root / "manifest.json").read_text(encoding="utf-8"))
    no_call_run_id = str(no_call_manifest["run_id"])
    no_call_db = no_call_run_root / "state" / "progress.sqlite3"
    monkeypatch.setattr(config, "PROGRESS_DB_FILE", no_call_db)
    assert getattr(outcome.status, "value", str(outcome.status)) == pipeline_runner.PipelineOutcomeStatus.COMPLETE.value
    checkpoint.validate_paid_evidence(no_call_run_id)

    with sqlite3.connect(f"file:{no_call_db.resolve().as_posix()}?mode=ro", uri=True) as db:
        attempt_id = db.execute(
            "SELECT paid_attempt_id FROM paid_attempts WHERE run_id=? AND item_index=0 AND phase='PAID'",
            (no_call_run_id,),
        ).fetchone()[0]
    negatives = {
        "receipt_missing": lambda db: db.execute(
            "DELETE FROM paid_no_call_evidence WHERE run_id=? AND item_index=0 AND paid_attempt_id=?",
            (no_call_run_id, attempt_id),
        ),
        "wrong_source_snapshot": lambda db: db.execute(
            "UPDATE paid_no_call_evidence SET input_snapshot_sha256=? WHERE run_id=? AND item_index=0 AND paid_attempt_id=?",
            ("0" * 64, no_call_run_id, attempt_id),
        ),
        "wrong_evaluation_hash": lambda db: db.execute(
            "UPDATE paid_no_call_evidence SET evaluation_payload_sha256=? WHERE run_id=? AND item_index=0 AND paid_attempt_id=?",
            ("1" * 64, no_call_run_id, attempt_id),
        ),
        "wrong_result_hash": lambda db: db.execute(
            "UPDATE paid_no_call_evidence SET result_payload_sha256=? WHERE run_id=? AND item_index=0 AND paid_attempt_id=?",
            ("2" * 64, no_call_run_id, attempt_id),
        ),
        "wrong_job_attempt": lambda db: db.execute(
            "UPDATE paid_no_call_evidence SET paid_attempt_id=? WHERE run_id=? AND item_index=0 AND paid_attempt_id=?",
            ("f" * 64, no_call_run_id, attempt_id),
        ),
        "wrong_call_reference": lambda db: db.execute(
            "INSERT INTO paid_attempt_calls(run_id,item_index,attempt_number,phase,call_id,paid_attempt_id,provider_call_id,provider,query_fingerprint,execution_generation,relation) "
            "SELECT run_id,item_index,attempt_number,'PAID','forged-call',paid_attempt_id,'forged-call','brightdata','',1,'OWNER' "
            "FROM paid_attempts WHERE run_id=? AND item_index=0 AND paid_attempt_id=?",
            (no_call_run_id, attempt_id),
        ),
        "untyped_no_call_shortcut": lambda db: db.execute(
            "UPDATE paid_attempts SET evidence_kind='NO_CALL_NEEDED' WHERE run_id=? AND item_index=0 AND paid_attempt_id=?",
            (no_call_run_id, attempt_id),
        ),
    }
    negative_root = tmp_path / "no_call_negative_copies"
    negative_root.mkdir(parents=True, exist_ok=True)
    observed_rejections = {}
    for case_name, mutate in negatives.items():
        bad_db = negative_root / f"{case_name}.sqlite3"
        _backup_sqlite(no_call_db, bad_db)
        rejected_by_constraint = False
        try:
            with sqlite3.connect(bad_db) as db:
                mutate(db)
                db.commit()
        except sqlite3.IntegrityError:
            rejected_by_constraint = True
        if not rejected_by_constraint:
            monkeypatch.setattr(config, "PROGRESS_DB_FILE", bad_db)
            with pytest.raises(checkpoint.EvidenceInvariant):
                checkpoint.validate_paid_evidence(no_call_run_id)
        observed_rejections[case_name] = {
            "database_sha256": hashlib.sha256(bad_db.read_bytes()).hexdigest(),
            "rejected_by": "sqlite_constraint" if rejected_by_constraint else "paid_evidence_validator",
        }
    monkeypatch.setattr(config, "PROGRESS_DB_FILE", no_call_db)
    checkpoint.validate_paid_evidence(no_call_run_id)

    evidence_dir = DELIVERY / "evidence" / "P09" / f"receipt_matrix_verified_{_evidence_stamp()}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    _backup_sqlite(no_call_db, evidence_dir / "valid_no_call_checkpoint.sqlite3")
    _backup_sqlite(db_path, evidence_dir / "wrapper_reuse_checkpoint.sqlite3")
    (evidence_dir / "result.json").write_text(json.dumps({
        "fixture_sha256": fixture_sha256,
        "wrapper_physical_calls": {row[0]: row[1] for row in wrapper_calls},
        "wrapper_relations": [list(row) for row in relations],
        "duplicate_results_inherited": {
            provider: second_results[provider].call_relations for provider in wrappers
        },
        "no_call_positive_control": "PASS",
        "no_call_negative_rejections": observed_rejections,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_p10_real_brightdata_header_parser_retains_captcha_provider_stop(tmp_path, monkeypatch):
    _manifest, fixture, _fixture_sha256 = load_fixture()
    source = next(row for row in fixture if row["source_record_id"] == "petzoo:G:001")
    _outcome, _manifest, calls, work, _items, transport, flights = _run_single_fixture_source(tmp_path, monkeypatch, source)
    starts = [row for row in transport if row.get("kind") == "paid_transport" and row["provider"] == "brightdata"]
    assert len(starts) == 1
    brightdata_calls = [row for row in calls if row[0] == "brightdata"]
    assert len(brightdata_calls) == 1 and brightdata_calls[0][1] == "FAILED" and brightdata_calls[0][2]
    assert "brightdata_header:CAPTCHA:captcha challenge" in brightdata_calls[0][3]
    assert any(row[0] == "brightdata" and row[1] == "FAILED" and "captcha" in row[2].casefold() for row in work)
    brightdata_flights = [row for row in flights if row[0] == "brightdata"]
    captcha_flights = [
        row for row in brightdata_flights
        if "CAPTCHA" in str(json.loads(row[3]).get("result_reason", ""))
    ]
    assert len(captcha_flights) == 1 and captcha_flights[0][1:3] == ("FAILED", 1)
    assert all(
        json.loads(row[3]).get("result_reason") == "dispatch_not_allocated"
        and not json.loads(row[3]).get("call_ids")
        for row in brightdata_flights if row not in captcha_flights
    )
    terminals = [row for row in transport if row.get("kind") == "paid_transport_terminal"]
    assert len([row for row in terminals if row["provider"] == "brightdata"]) == 1
    assert all(row["outcome"] == "response" for row in terminals if row["provider"] == "brightdata")


def test_p11_real_137_company_pipeline_worker_1_and_3(tmp_path):
    fixture_manifest, fixture, fixture_sha256 = load_fixture()
    expected_sources = sorted(row["source_record_id"] for row in fixture)
    from collections import Counter

    assert Counter(row["group"] for row in fixture) == {
        "A": 20, "B": 20, "C": 20, "D": 20, "E": 15, "F": 15, "G": 15, "H": 12,
    }
    assert Counter(row["subcase"] for row in fixture if row["group"] == "E") == {
        "http_404": 5, "timeout": 5, "http_429": 3, "http_503": 2,
    }
    assert Counter(row["subcase"] for row in fixture if row["group"] == "H") == {
        "linkedin_resolution": 5, "first_party_verified": 1,
        "llm_context_conflict": 6,
    }
    gate_root = DELIVERY / "evidence" / "P11"
    gate_root.mkdir(parents=True, exist_ok=True)
    variants = {}
    safe_environment_names = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "COMSPEC",
    }
    for workers in (1, 3):
        variant_root = gate_root / f"workers_{workers}"
        variant_root.mkdir(parents=True, exist_ok=True)
        work_root = tmp_path / f"p11_workers_{workers}"
        work_root.mkdir(parents=True, exist_ok=True)
        result_path = work_root / "result.json"
        transport_path = work_root / "transport.jsonl"
        http_path = work_root / "http.jsonl"
        network_path = work_root / "network.jsonl"
        command = [
            str(RUNTIME), str(ROOT / "tests" / "petzoo_child_runner.py"),
            "--workspace", str(work_root), "--workers", str(workers),
            "--result", str(result_path),
            "--transport-journal", str(transport_path),
            "--http-journal", str(http_path),
            "--network-journal", str(network_path),
        ]
        environment = {key: value for key, value in os.environ.items() if key.upper() in safe_environment_names}
        environment.update({
            "B2B_SOCKET_DENY_JSONL": str(network_path),
            "B2B_COMMAND_ID": f"petzoo-p11-workers-{workers}",
            "B2B_TEST_OFFLINE": "1",
            "BRIGHTDATA_API_KEY": "fake-petzoo-brightdata",
            "GOOGLE_PLACES_API_KEY": "fake-petzoo-places",
            "BRANDFETCH_CLIENT_ID": "fake-petzoo-brandfetch",
            "HUNTER_API_KEY": "fake-petzoo-hunter",
            "OPENROUTER_API_KEY": "fake-petzoo-llm",
        })
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started_epoch = time.time()
        process = subprocess.Popen(
            command, cwd=ROOT, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=1800)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            else:
                process.kill()
            stdout, stderr = process.communicate()
        command_record = {
            "argv": command, "cwd": str(ROOT.resolve()), "runtime": str(RUNTIME.resolve()),
            "environment": {key: "<masked>" for key in environment if any(
                term in key.casefold() for term in ("key", "token", "secret", "password")
            )},
            "fixture_sha256": fixture_sha256,
            "runtime_source_tree_sha256": __import__("modules.run_context", fromlist=["source_tree_sha256"]).source_tree_sha256(),
            "started_at": started, "duration_seconds": time.time() - started_epoch,
            "child_pid": process.pid, "timed_out": timed_out,
            "exit_code": process.returncode,
            "stdout_file": "stdout.txt", "stderr_file": "stderr.txt",
            "result_file": "result.json", "transport_file": "transport.jsonl",
            "http_file": "http.jsonl", "network_file": "network.jsonl",
        }
        (variant_root / "stdout.txt").write_text(stdout, encoding="utf-8")
        (variant_root / "stderr.txt").write_text(stderr, encoding="utf-8")
        for source, name in ((transport_path, "transport.jsonl"), (http_path, "http.jsonl"), (network_path, "network.jsonl")):
            if source.is_file():
                shutil.copyfile(source, variant_root / name)
        run_roots = [path for path in (work_root / "runs").glob("*") if path.is_dir()]
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            (variant_root / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result_run_root = Path(result.get("run_root", ""))
            if result_run_root.is_dir() and result_run_root not in run_roots:
                run_roots.append(result_run_root)
        for run_root in run_roots:
            manifest_source = run_root / "manifest.json"
            database_source = run_root / "state" / "progress.sqlite3"
            if manifest_source.is_file():
                shutil.copyfile(manifest_source, variant_root / "run_manifest.json")
            if database_source.is_file():
                _backup_sqlite(database_source, variant_root / "checkpoint.sqlite3")
        command_record["ended_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        command_record["artifact_sha256"] = {
            path.name: _sha256(path)
            for path in variant_root.iterdir()
            if path.is_file() and path.name != "command.json"
        }
        (variant_root / "command.json").write_text(json.dumps(command_record, indent=2) + "\n", encoding="utf-8")
        assert not timed_out, f"P11 worker={workers} outer timeout after 1800 seconds"
        assert process.returncode == 0, stderr
        assert result_path.is_file(), f"P11 worker={workers} missing result.json"
        assert all((variant_root / name).is_file() for name in ("transport.jsonl", "http.jsonl", "network.jsonl", "checkpoint.sqlite3", "run_manifest.json"))
        run_root = Path(result["run_root"])
        db_path = run_root / "state" / "progress.sqlite3"

        with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
            round_rows = db.execute(
                "SELECT round_ordinal,kind,created_at FROM scheduler_progress_snapshots "
                "WHERE run_id=? AND phase='PAID' AND kind IN ('START','END') "
                "ORDER BY round_ordinal,kind",
                (result["run_id"],),
            ).fetchall()
            heartbeat_rows = db.execute(
                "SELECT round_ordinal,created_at FROM scheduler_heartbeat_events "
                "WHERE run_id=? AND phase='PAID' ORDER BY round_ordinal,created_at",
                (result["run_id"],),
            ).fetchall()
        round_timestamps = {
            (int(round_ordinal), str(kind)): datetime.fromisoformat(str(created_at))
            for round_ordinal, kind, created_at in round_rows
        }
        heartbeats_by_round = {}
        for round_ordinal, created_at in heartbeat_rows:
            heartbeats_by_round.setdefault(int(round_ordinal), []).append(
                datetime.fromisoformat(str(created_at))
            )
        for round_ordinal in sorted({key[0] for key in round_timestamps}):
            started_at = round_timestamps[(round_ordinal, "START")]
            ended_at = round_timestamps[(round_ordinal, "END")]
            if (ended_at - started_at).total_seconds() > 60:
                in_round = [
                    timestamp for timestamp in heartbeats_by_round.get(round_ordinal, [])
                    if started_at < timestamp < ended_at
                ]
                timeline = [started_at, *in_round, ended_at]
                gaps = [
                    (right - left).total_seconds()
                    for left, right in zip(timeline, timeline[1:])
                ]
                assert in_round and max(gaps) <= 60, (
                    f"P11 worker={workers} round={round_ordinal} heartbeat gaps {gaps}"
                )

        assert result["fixture_sha256"] == fixture_sha256
        assert result["source_count"] == 137
        assert sorted(result["source_record_ids"]) == expected_sources
        assert len(set(result["source_record_ids"])) == 137
        assert result["worker_count"] == workers
        assert set(result["physical_provider_calls"]) == set(PROVIDER_BUDGETS)
        assert all(int(result["physical_provider_calls"][provider]) > 0 for provider in PROVIDER_BUDGETS)
        network_rows = _jsonl(network_path)
        assert any(row.get("kind") == "guard_armed" and row.get("child") is True for row in network_rows)
        blocked = [row for row in network_rows if row.get("kind") == "blocked_network"]
        assert all(row.get("event") in {"socket.getaddrinfo", "socket.sendto"} for row in blocked)
        assert not any(
            provider_host in json.dumps(row, ensure_ascii=False).casefold()
            for row in blocked
            for provider_host in ("brightdata", "googleapis", "brandfetch", "hunter", "openrouter", "linkedin")
        )
        transport_events = _jsonl(transport_path)
        transport_rows = [row for row in transport_events if row.get("kind") == "paid_transport"]
        transport_terminal_rows = [row for row in transport_events if row.get("kind") == "paid_transport_terminal"]
        assert len(transport_rows) == len(transport_terminal_rows)
        assert {row.get("call_id") for row in transport_rows} == {row.get("call_id") for row in transport_terminal_rows}
        assert all(
            row.get("query_fingerprint") and row.get("execution_generation") is not None
            and row.get("query_fingerprint_basis")
            and row.get("terminal_event_id")
            and (row.get("outcome") == "exception" or row.get("response_sha256"))
            for row in transport_terminal_rows
        )
        call_by_id = {row["call_id"]: row for row in result["calls"]}
        group_a = {source for source in result["source_record_ids"] if source.startswith("petzoo:A:")}
        group_a_items = [row for row in result["items"] if row["source_record_id"] in group_a]
        assert len(group_a) == len(group_a_items) == 20
        assert all(bool(row["paid_required"]) and row["paid_state"] == "DONE" for row in group_a_items)
        group_a_indexes = {row["item_index"] for row in group_a_items}
        group_a_work = [row for row in result["work"] if row["source_record_id"] in group_a]
        assert group_a_work and all(
            row["state"] == "NOT_REQUIRED"
            and row["terminal_reason"] == "supplied_website_publishable_at_paid_entry"
            for row in group_a_work
        )
        group_a_attempts = [row for row in result["attempts"] if row["item_index"] in group_a_indexes]
        assert len(group_a_attempts) == 20 and all(
            row["result"] == "NO_CALL_NEEDED"
            and row["evidence_kind"] == "supplied_website_publishable_at_paid_entry"
            and row["input_snapshot_sha256"]
            for row in group_a_attempts
        )
        group_a_receipts = [row for row in result["no_call_receipts"] if row["item_index"] in group_a_indexes]
        assert len(group_a_receipts) == 20 and {row["item_index"] for row in group_a_receipts} == group_a_indexes
        assert all(
            row["evidence_kind"] == "supplied_website_publishable_at_paid_entry"
            and row["publication_eligible"] == 1
            and row["evaluator_schema_version"] >= 1
            and row["input_snapshot_sha256"]
            and row["normalized_input_website"].startswith("petzoo-a-")
            and row["evaluation_payload_sha256"]
            and row["result_payload_sha256"]
            for row in group_a_receipts
        )
        assert not any(row["source_record_id"] in group_a for row in transport_rows)
        assert len(transport_rows) == sum(
            int(value.get("physical_http_attempts", 0))
            for value in result["provider_budgets"].values()
        )
        assert all(
            row["call_id"] in call_by_id
            and row["provider"] == call_by_id[row["call_id"]]["provider"]
            and row["source_record_id"] == result["source_record_ids"][row["item_index"]]
            for row in transport_rows
        )
        group_h_sources = {source for source in result["source_record_ids"] if source.startswith("petzoo:H:")}
        assert group_h_sources and all(
            any(row["provider"] == provider and row["source_record_id"] in group_h_sources for row in transport_rows)
            for provider in ("linkedin", "llm")
        )
        group_c_sources = {source for source in result["source_record_ids"] if source.startswith("petzoo:C:")}
        group_c_indexes = {row["item_index"] for row in result["items"] if row["source_record_id"] in group_c_sources}
        owner_call_by_id = {row["call_id"]: row for row in result["calls"]}
        c_inherited = [
            row for row in result["attempt_call_relations"]
            if row["item_index"] in group_c_indexes and row["relation"] == "INHERITED"
        ]
        c_follower_indexes = {row["item_index"] for row in c_inherited}
        assert c_inherited, "P11 C never observed a real terminal query follower"
        assert any(
            owner["item_index"] == inherited["item_index"]
            and owner["attempt_number"] == inherited["attempt_number"]
            and owner["provider"] == inherited["provider"] == "brightdata"
            and owner["relation"] == "OWNER"
            and owner["query_fingerprint"] != inherited["query_fingerprint"]
            and owner_call_by_id[owner["call_id"]]["item_index"] == inherited["item_index"]
            and any(transport["call_id"] == owner["call_id"] for transport in transport_rows)
            for inherited in c_inherited
            for owner in result["attempt_call_relations"]
        ), "P11 C follower did not dispatch a distinct Bright Data query"
        assert result["group_c_route_report"]["shared_terminal_fingerprint_count"] > 0
        group_f_sources = {source for source in result["source_record_ids"] if source.startswith("petzoo:F:")}
        group_f_transport = [row for row in transport_rows if row["source_record_id"] in group_f_sources]
        assert all(
            any(row["provider"] == provider for row in group_f_transport)
            for provider in ("brandfetch", "google_places", "hunter")
        )
        group_f_hunter_sources = {
            row["source_record_id"] for row in group_f_transport if row["provider"] == "hunter"
        }
        assert len(group_f_hunter_sources) == 14
        assert group_f_hunter_sources == {f"petzoo:F:{ordinal:03d}" for ordinal in range(14)}
        assert result["provider_budgets"]["hunter"]["physical_http_attempts"] == 14
        group_f_hunter_jobs = [row for row in result["work"] if row["source_record_id"] in group_f_sources and row["provider"] == "hunter"]
        assert len(group_f_hunter_jobs) >= 15
        unserved_f_jobs = [row for row in group_f_hunter_jobs if row["source_record_id"] not in group_f_hunter_sources]
        assert unserved_f_jobs and all(row["state"] in {"READY", "BLOCKED_BUDGET"} for row in unserved_f_jobs)
        group_g_unknown = next(index for index, source in enumerate(result["source_record_ids"]) if source == "petzoo:G:000")
        assert sum(
            row["provider"] == "brightdata" and row["source_record_id"] == "petzoo:G:000"
            for row in transport_rows
        ) == 1
        assert any(
            row["provider"] == "brightdata" and row["item_index"] == group_g_unknown
            and row["state"] == "UNKNOWN" and row["http_started_at"]
            for row in result["calls"]
        )
        for provider, budget in result["provider_budgets"].items():
            assert int(budget["physical_http_attempts"]) <= int(budget["effective_limit"])
            assert sum(row["provider"] == provider for row in transport_rows) == int(budget["physical_http_attempts"])
        variants[str(workers)] = result

    def logical_work(result):
        return sorted((
            row["source_record_id"], row["provider"], row["operation"],
            row["request_fingerprint"], row.get("query_fingerprint", ""),
            row["state"], row["terminal_reason"],
            row["dependency_job_fingerprint"],
        ) for row in result["work"])

    def dispositions(result):
        return sorted((row["source_record_id"], row["free_state"], row["paid_state"], bool(row["paid_required"])) for row in result["items"])

    assert logical_work(variants["1"]) == logical_work(variants["3"])
    assert dispositions(variants["1"]) == dispositions(variants["3"])


def test_p12_six_real_process_crash_boundaries_resume_from_durable_state(tmp_path):
    fixture_manifest, _fixture, fixture_sha256 = load_fixture()
    boundaries = (
        "allocation_before", "allocation_after", "call_reserved_before_http",
        "http_started_before_response", "terminal_call_before_company_save",
        "finalization_after_memory_plan",
    )
    evidence_root = DELIVERY / "evidence" / "P12"
    evidence_root.mkdir(parents=True, exist_ok=True)
    safe_environment_names = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "COMSPEC",
    }
    observed = {}

    def state_snapshot(run_root: Path, target: Path) -> dict:
        database = run_root / "state" / "progress.sqlite3"
        with sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True) as db:
            calls = db.execute(
                "SELECT call_id,provider,item_index,request_fingerprint,state,http_started_at FROM provider_calls ORDER BY created_at,call_id"
            ).fetchall()
            allocations = db.execute(
                "SELECT provider,round_ordinal,item_index,job_fingerprint,state,consumed_call_id FROM provider_dispatch_allocations ORDER BY provider,round_ordinal,item_index"
            ).fetchall()
            work = db.execute(
                "SELECT provider,job_fingerprint,state,call_id FROM provider_work_items ORDER BY provider,job_fingerprint"
            ).fetchall()
            items = db.execute(
                "SELECT item_index,source_record_id,free_state,paid_state,paid_attempts FROM run_items ORDER BY item_index"
            ).fetchall()
            budgets = db.execute(
                "SELECT provider,effective_limit,reserved_total,reserved,completed,failed,unknown FROM provider_usage ORDER BY provider"
            ).fetchall()
            recoveries = db.execute(
                "SELECT call_id,provider,item_index,reason,http_started_at FROM provider_call_recovery_receipts ORDER BY call_id"
            ).fetchall()
            intent = db.execute(
                "SELECT status,memory_plan_committed,memory_plan_count FROM finalization_intent"
            ).fetchall()
            foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
        manifest_path = run_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        snapshot = {
            "run_root": str(run_root), "manifest": manifest,
            "calls": calls, "allocations": allocations, "work": work,
            "items": items, "budgets": budgets, "recoveries": recoveries,
            "finalization_intent": intent, "foreign_key_errors": foreign_key_errors,
        }
        target.write_text(json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        _backup_sqlite(database, target.with_name("checkpoint.sqlite3"))
        if manifest_path.is_file():
            shutil.copyfile(manifest_path, target.with_name("run_manifest.json"))
        return snapshot

    for boundary in boundaries:
        case_root = evidence_root / f"{boundary}_verified_{_evidence_stamp()}"
        case_root.mkdir(parents=True, exist_ok=True)
        work_root = tmp_path / "p12" / boundary
        work_root.mkdir(parents=True, exist_ok=True)
        transport_path = work_root / "transport.jsonl"
        http_path = work_root / "http.jsonl"
        network_path = work_root / "network.jsonl"
        fault_path = work_root / "faults.jsonl"
        result_path = work_root / "result.json"
        for journal in (transport_path, http_path, network_path, fault_path):
            journal.write_text("", encoding="utf-8")
        runner = ROOT / "tests" / "petzoo_p12_child_runner.py"
        safe_env = {key: value for key, value in os.environ.items() if key.upper() in safe_environment_names}
        safe_env.update({
            "B2B_SOCKET_DENY_JSONL": str(network_path),
            "B2B_COMMAND_ID": f"petzoo-p12-{boundary}",
            "B2B_TEST_OFFLINE": "1",
            "BRIGHTDATA_API_KEY": "fake-petzoo-brightdata",
            "GOOGLE_PLACES_API_KEY": "fake-petzoo-places",
            "BRANDFETCH_CLIENT_ID": "fake-petzoo-brandfetch",
            "HUNTER_API_KEY": "fake-petzoo-hunter",
            "OPENROUTER_API_KEY": "fake-petzoo-llm",
        })

        def invoke(*, resume_run: Path | None, crash_boundary: str):
            command = [
                str(RUNTIME), str(runner), "--workspace", str(work_root),
                "--boundary", crash_boundary, "--result", str(result_path),
                "--transport-journal", str(transport_path), "--http-journal", str(http_path),
                "--network-journal", str(network_path), "--fault-journal", str(fault_path),
            ]
            if resume_run is not None:
                command.extend(["--resume-run", str(resume_run)])
            started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            epoch = time.time()
            process = subprocess.Popen(
                command, cwd=ROOT, env=safe_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            timed_out = False
            try:
                stdout, stderr = process.communicate(timeout=240)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                stdout, stderr = process.communicate()
            return {
                "argv": command, "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "duration_seconds": time.time() - epoch, "pid": process.pid,
                "exit_code": process.returncode, "timed_out": timed_out,
                "stdout": stdout, "stderr": stderr,
                "expected_exit_code": 86 if crash_boundary != "none" else 0,
            }

        initial = invoke(resume_run=None, crash_boundary=boundary)
        (case_root / "crash_stdout.txt").write_text(initial["stdout"], encoding="utf-8")
        (case_root / "crash_stderr.txt").write_text(initial["stderr"], encoding="utf-8")
        assert not initial["timed_out"], f"P12 {boundary} crash child timed out"
        assert initial["exit_code"] == 86, f"P12 {boundary} did not hard-kill at its hook: {initial['stderr']}"
        fault_rows = _jsonl(fault_path)
        fault = next((row for row in fault_rows if row.get("boundary") == boundary), None)
        assert fault and fault.get("observed_checkpoint"), f"P12 {boundary} hook was not observed"
        run_roots = [path for path in (work_root / "runs").glob("*") if path.is_dir()]
        assert len(run_roots) == 1, f"P12 {boundary} expected one durable run root, found {run_roots}"
        run_root = run_roots[0]
        before = state_snapshot(run_root, case_root / "before_state.json")
        assert before["foreign_key_errors"] == []

        resumed = invoke(resume_run=run_root, crash_boundary="none")
        (case_root / "resume_stdout.txt").write_text(resumed["stdout"], encoding="utf-8")
        (case_root / "resume_stderr.txt").write_text(resumed["stderr"], encoding="utf-8")
        assert not resumed["timed_out"], f"P12 {boundary} resume child timed out"
        assert resumed["exit_code"] == 0, f"P12 {boundary} resume failed: {resumed['stderr']}"
        assert result_path.is_file(), f"P12 {boundary} resumed child omitted result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["fixture_sha256"] == fixture_sha256
        assert result["run_id"] == run_root.name
        after = state_snapshot(run_root, case_root / "after_resume_state.json")
        assert after["foreign_key_errors"] == []
        fault_call_id = str(fault.get("call_id") or "")
        transport_rows = [row for row in _jsonl(transport_path) if row.get("kind") == "paid_transport"] if transport_path.is_file() else []
        if boundary == "call_reserved_before_http":
            assert fault_call_id and any(row[0] == fault_call_id for row in after["recoveries"])
            request_fingerprint = next(row[3] for row in before["calls"] if row[0] == fault_call_id)
            assert sum(row["request_fingerprint"] == request_fingerprint for row in transport_rows) == 1
            assert not any(row[0] == fault_call_id for row in after["calls"])
            brightdata_budget = next(row for row in after["budgets"] if row[0] == "brightdata")
            assert brightdata_budget[2] == brightdata_budget[3] + brightdata_budget[4] + brightdata_budget[5] + brightdata_budget[6]
        elif boundary == "http_started_before_response":
            call = next(row for row in after["calls"] if row[0] == fault_call_id)
            assert call[4] == "UNKNOWN" and call[5]
            assert not any(row["call_id"] == fault_call_id for row in transport_rows)
            assert not any(row["request_fingerprint"] == call[3] for row in transport_rows)
        elif boundary == "terminal_call_before_company_save":
            call = next(row for row in after["calls"] if row[0] == fault_call_id)
            assert call[4] == "DONE" and call[5]
            assert sum(row["call_id"] == fault_call_id for row in transport_rows) == 1
        elif boundary == "finalization_after_memory_plan":
            before_calls = {row[0] for row in before["calls"]}
            assert before["finalization_intent"] and before["finalization_intent"][0][1] == 1
            assert {row[0] for row in after["calls"]} == before_calls
            assert result["manifest_complete"] is True
            assert result["finalization_intent"][0]["status"] == "COMPLETE"
        else:
            assert result["allocations"]
            assert len({row["job_fingerprint"] for row in result["allocations"]}) == len(result["allocations"])
        network_rows = _jsonl(network_path)
        assert sum(row.get("kind") == "guard_armed" and row.get("child") is True for row in network_rows) == 2
        blocked = [row for row in network_rows if row.get("kind") == "blocked_network"]
        assert all(row.get("event") in {"socket.getaddrinfo", "socket.sendto"} for row in blocked)
        for source, name in ((transport_path, "transport.jsonl"), (http_path, "http.jsonl"), (network_path, "network.jsonl"), (fault_path, "faults.jsonl")):
            if source.is_file():
                shutil.copyfile(source, case_root / name)
        shutil.copyfile(result_path, case_root / "result.json")
        command_record = {
            "fixture_sha256": fixture_sha256,
            "fixture_id": fixture_manifest["fixture_id"],
            "runtime": str(RUNTIME.resolve()), "cwd": str(ROOT.resolve()),
            "source_sha256": __import__("modules.run_context", fromlist=["source_tree_sha256"]).source_tree_sha256(),
            "crash_process": {key: value for key, value in initial.items() if key not in {"stdout", "stderr"}},
            "resume_process": {key: value for key, value in resumed.items() if key not in {"stdout", "stderr"}},
            "environment": {key: "<masked>" for key in safe_env if any(term in key.casefold() for term in ("key", "token", "secret", "password"))},
            "artifact_sha256": {path.name: _sha256(path) for path in case_root.iterdir() if path.is_file() and path.name != "command.json"},
        }
        (case_root / "command.json").write_text(json.dumps(command_record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        observed[boundary] = {
            "run_id": result["run_id"], "outcome": result["outcome"],
            "manifest_complete": result["manifest_complete"],
            "crash_exit": initial["exit_code"], "resume_exit": resumed["exit_code"],
            "fault_call_id": fault_call_id,
        }

    (evidence_root / f"matrix_result_{_evidence_stamp()}.json").write_text(
        json.dumps({"fixture_sha256": fixture_sha256, "boundaries": observed}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
