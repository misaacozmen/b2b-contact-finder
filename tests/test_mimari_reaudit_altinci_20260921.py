from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from pathlib import Path

from openpyxl import load_workbook
import pytest

import config
import main
from modules import checkpoint, output_artifacts, pipeline_runner, publication_policy, runtime, search
from tests.test_search_phase_regressions import (
    Response, RecordingPaidTransport, _input_book, _real_run_setup, init_run,
)


def _empty_primary_run(tmp_path: Path, monkeypatch, *, companies: list[str], workers: int, query_limit: int, budget: int):
    source = tmp_path / "input.xlsx"
    _input_book(source, companies)
    transport = RecordingPaidTransport([
        Response(200, {"organic": []}) for _ in range(budget)
    ])
    runs = _real_run_setup(tmp_path, monkeypatch, transport, workers=workers)
    monkeypatch.setattr(config, "MAX_SEARCH_QUERIES_PER_COMPANY", query_limit)
    monkeypatch.setattr(config, "DEFAULT_PAID_SEARCH_QUERY_LIMIT", query_limit)
    monkeypatch.setattr(config, "BRIGHTDATA_REQUEST_BUDGET", budget)
    monkeypatch.setattr(config, "BRIGHTDATA_REQUEST_HARD_CAP", budget)
    monkeypatch.setattr(config, "DEFAULT_FREE_SEARCH_QUERY_LIMIT_PER_COMPANY", 1)
    monkeypatch.setattr(config, "SEARCH_HTTP_REQUEST_BUDGET", len(companies))
    monkeypatch.setattr(config, "MAX_AUTONOMOUS_RESOLUTION_ROUNDS", 0)
    return source, transport, runs


def _primary_linkage_metrics(root: Path) -> dict:
    database = root / "state" / "progress.sqlite3"
    with sqlite3.connect(database) as db:
        run_id = str(db.execute("SELECT run_id FROM runs ORDER BY updated_at DESC LIMIT 1").fetchone()[0])
        source_rows = db.execute(
            "SELECT item_index,source_record_id FROM run_items WHERE run_id=? ORDER BY item_index",
            (run_id,),
        ).fetchall()
        plan_rows = db.execute(
            "SELECT item_index,query_ordinal,normalized_query,query_sha256 "
            "FROM paid_query_plan_entries WHERE run_id=? AND query_kind='primary' "
            "ORDER BY item_index,query_ordinal",
            (run_id,),
        ).fetchall()
        calls = db.execute(
            "SELECT call_id,item_index,flight_fingerprint,state,http_started_at "
            "FROM provider_calls WHERE run_id=? AND provider='brightdata' ORDER BY item_index,call_id",
            (run_id,),
        ).fetchall()
        flights = {
            str(fingerprint): (str(state), int(generation))
            for fingerprint, state, generation in db.execute(
                "SELECT query_fingerprint,state,execution_generation "
                "FROM provider_query_flights WHERE run_id=? AND provider='brightdata'",
                (run_id,),
            ).fetchall()
        }
        terminals = {
            (str(fingerprint), int(generation)): json.loads(str(call_ids_json))
            for fingerprint, generation, call_ids_json in db.execute(
                "SELECT query_fingerprint,execution_generation,call_ids_json "
                "FROM provider_query_flight_terminals WHERE run_id=? AND provider='brightdata'",
                (run_id,),
            ).fetchall()
        }
        call_by_item_flight = {}
        for call_id, item_index, flight, state, http_started_at in calls:
            call_by_item_flight.setdefault((int(item_index), str(flight)), []).append({
                "call_id": str(call_id), "state": str(state),
                "http_started": bool(str(http_started_at or "")),
            })
        source_by_item = {int(item): str(source) for item, source in source_rows}
        source_queries = {source: set() for source in source_by_item.values()}
        linkage = []
        for item_index, query_ordinal, query, query_sha in plan_rows:
            query = str(query)
            assert hashlib.sha256(query.encode("utf-8")).hexdigest() == str(query_sha)
            flight = search._brightdata_flight_fingerprint(query)
            flight_row = flights.get(flight)
            assert flight_row is not None, (item_index, query)
            assert str(flight_row[0]) == "DONE", (item_index, query, flight_row)
            terminal = terminals.get((flight, int(flight_row[1])))
            assert terminal is not None, (item_index, query, flight)
            matched = call_by_item_flight.get((int(item_index), flight), [])
            assert len(matched) == 1, (item_index, query, matched)
            assert matched[0]["state"] == "DONE" and matched[0]["http_started"]
            assert matched[0]["call_id"] in terminal
            source_queries[source_by_item[int(item_index)]].add(query)
            linkage.append({
                "item_index": int(item_index), "source_record_id": source_by_item[int(item_index)],
                "query_ordinal": int(query_ordinal), "query": query,
                "flight_fingerprint": flight, "call_id": matched[0]["call_id"],
            })
        assert len(source_queries) == 100
        assert all(len(queries) == 3 for queries in source_queries.values())
        assert len(plan_rows) == 300
        assert len(calls) == 300
        assert len({row[0] for row in calls}) == 300
        return {
            "run_id": run_id,
            "source_count": len(source_queries),
            "primary_plan_count": len(plan_rows),
            "provider_call_count": len(calls),
            "done_call_count": sum(row[3] == "DONE" for row in calls),
            "source_query_sets": {source: sorted(queries) for source, queries in sorted(source_queries.items())},
            "linkage_count": len(linkage),
        }


def test_h01_default_pipeline_100_firms_three_workers_has_one_call_per_primary_query(tmp_path, monkeypatch):
    measurements = {}
    for workers in (1, 3):
        variant = tmp_path / f"workers-{workers}"
        variant.mkdir()
        source, transport, runs = _empty_primary_run(
            variant, monkeypatch,
            companies=[f"Company {index:03d}" for index in range(100)],
            workers=workers, query_limit=3, budget=300,
        )
        outcome = main.run(source, allow_paid=True)
        root = next(runs.iterdir())
        assert outcome.status is pipeline_runner.PipelineOutcomeStatus.COMPLETE
        assert len(transport.journal) == 300
        measurements[str(workers)] = {
            **_primary_linkage_metrics(root),
            "worker_count": workers,
            "transport_call_count": len(transport.journal),
        }
    assert measurements["1"]["source_query_sets"] == measurements["3"]["source_query_sets"]
    target = os.environ.get("B2B_K08_MEASUREMENTS", "").strip()
    if target:
        baseline = measurements["1"]
        Path(target).write_text(json.dumps({
            **baseline,
            "distinct_queries_per_source": 3,
            "duplicate_count": 0,
            "loss_count": 0,
            "worker_sets_equal": True,
            "worker_variants": measurements,
        }, indent=2, sort_keys=True), encoding="utf-8")


def test_k09_budget_100_gives_each_firm_only_first_primary_right(tmp_path, monkeypatch):
    # Keep the three-query workload frozen while the real dispatch allocator
    # enforces the 100-call global budget.  This makes the two denied rights
    # observable instead of letting the fair-share planner pre-trim them.
    search.reset_run_state()
    monkeypatch.setattr(
        search, "configure_run_budget",
        lambda _company_count: search.configure_frozen_paid_query_limit(3),
    )
    source, transport, runs = _empty_primary_run(
        tmp_path, monkeypatch,
        companies=[f"Company {index:03d}" for index in range(100)],
        workers=3, query_limit=3, budget=100,
    )
    outcome = main.run(source, allow_paid=True)
    root = next(runs.iterdir())
    assert outcome.status is pipeline_runner.PipelineOutcomeStatus.PAID_MANUAL_AUTHORIZATION_REVIEW_REQUIRED
    assert len(transport.journal) == 100
    database = root / "state" / "progress.sqlite3"
    with sqlite3.connect(database) as db:
        run_id = str(db.execute("SELECT run_id FROM runs ORDER BY updated_at DESC LIMIT 1").fetchone()[0])
        plan_rows = db.execute(
            "SELECT item_index,query_ordinal,normalized_query FROM paid_query_plan_entries "
            "WHERE run_id=? AND query_kind='primary' ORDER BY item_index,query_ordinal",
            (run_id,),
        ).fetchall()
        assert len(plan_rows) == 300
        first_query = {}
        for item_index, query_ordinal, query in plan_rows:
            if int(query_ordinal) == 0:
                first_query[int(item_index)] = str(query)
        call_rows = db.execute(
            "SELECT item_index,flight_fingerprint,state FROM provider_calls "
            "WHERE run_id=? AND provider='brightdata' ORDER BY item_index",
            (run_id,),
        ).fetchall()
        assert len(call_rows) == 100
        assert {int(row[0]) for row in call_rows} == set(first_query)
        assert all(row[2] == "DONE" for row in call_rows)
        assert all(row[1] == search._brightdata_flight_fingerprint(first_query[int(row[0])]) for row in call_rows)
        allocation_rows = db.execute(
            "SELECT round_ordinal,item_index,source_record_id,state,consumed_call_id "
            "FROM provider_dispatch_allocations WHERE run_id=? AND provider='brightdata' "
            "ORDER BY round_ordinal,item_index",
            (run_id,),
        ).fetchall()
        assert len(allocation_rows) == 100
        assert {int(row[1]) for row in allocation_rows} == set(first_query)
        assert all(str(row[0]) == "0" and str(row[3]) == "DONE" and str(row[4]) for row in allocation_rows)
        result_rows = db.execute("SELECT item_index,payload FROM results WHERE run_id=? ORDER BY item_index", (run_id,)).fetchall()
        terminal_reasons = []
        first_trace_queries = {}
        for item_index, payload in result_rows:
            row = json.loads(payload)
            traces = [entry for entry in row.get("__search_trace", []) if isinstance(entry, dict) and entry.get("phase") == "primary"]
            called = [entry for entry in traces if entry.get("call_ids")]
            pending = [entry for entry in traces if str(entry.get("result_reason", "")).casefold() == "dispatch_not_allocated"]
            assert len(called) == 1 and len(pending) == 2, (item_index, traces)
            assert called[0].get("query") == first_query[int(item_index)]
            first_trace_queries[int(item_index)] = called[0].get("query")
            terminal_reasons.extend(str(entry.get("result_reason", "")) for entry in pending)
        assert len(terminal_reasons) == 200
        assert all(reason == "dispatch_not_allocated" for reason in terminal_reasons)
        assert db.execute(
            "SELECT COUNT(*) FROM run_items WHERE run_id=? AND paid_state='BLOCKED_BUDGET'",
            (run_id,),
        ).fetchone()[0] == 100
    measurement = {
        "run_id": run_id,
        "configured_budget": 100,
        "source_count": 100,
        "primary_plan_count": len(plan_rows),
        "provider_call_count": len(call_rows),
        "first_right_count": len(first_trace_queries),
        "second_right_consumed": 0,
        "remaining_primary_query_count": len(terminal_reasons),
        "remaining_terminal_reasons": {reason: terminal_reasons.count(reason) for reason in sorted(set(terminal_reasons))},
        "dispatch_allocation_count": len(allocation_rows),
        "budget_terminal_count": len(terminal_reasons),
        "run_complete": outcome.status in {
            pipeline_runner.PipelineOutcomeStatus.COMPLETE,
            pipeline_runner.PipelineOutcomeStatus.PAID_MANUAL_AUTHORIZATION_REVIEW_REQUIRED,
        },
    }
    target = os.environ.get("B2B_K09_MEASUREMENTS", "").strip()
    if target:
        Path(target).write_text(json.dumps(measurement, indent=2, sort_keys=True), encoding="utf-8")


def test_h01_owner_relation_is_monotone_and_same_attempt_conflict_rolls_back(tmp_path, monkeypatch):
    source, transport, runs = _empty_primary_run(
        tmp_path, monkeypatch,
        companies=["Duplicate Company", "Duplicate Company"],
        workers=2, query_limit=1, budget=2,
    )
    outcome = main.run(source, allow_paid=True)
    assert outcome.status is pipeline_runner.PipelineOutcomeStatus.COMPLETE
    root = next(runs.iterdir())
    database = root / "state" / "progress.sqlite3"
    with sqlite3.connect(database) as db:
        rows = db.execute(
            "SELECT provider_call_id,item_index,relation FROM provider_query_flight_consumers ORDER BY provider_call_id,item_index"
        ).fetchall()
        assert all(relation in {"OWNER", "INHERITED"} for _call_id, _item, relation in rows)
        for candidate in {call_id for call_id, _item, _relation in rows}:
            relations = [relation for call_id, _item, relation in rows if call_id == candidate]
            assert relations.count("OWNER") == 1


def test_h01_conflicting_owner_insert_is_atomic(tmp_path, monkeypatch):
    database = tmp_path / "h01-atomic.sqlite3"
    monkeypatch.setattr(config, "PROGRESS_DB_FILE", database)
    init_run(database, count=2, budget=2)
    source_ids = {0: "s0", 1: "s1"}
    jobs = {
        item_index: checkpoint.ensure_provider_work_item(
            run_id="run", item_index=item_index, source_record_id=source_id,
            provider="brightdata", operation="h01", request_fingerprint="h01-request",
            query_fingerprint="h01-fp", need_class="identity",
        )
        for item_index, source_id in source_ids.items()
    }
    checkpoint.reserve_provider_dispatch_round(
        run_id="run", provider="brightdata", round_ordinal=0,
        candidates=[
            {
                "item_index": item_index, "source_record_id": source_id,
                "need_class": "identity", "job_fingerprint": jobs[item_index]["job_fingerprint"],
                "operation": "h01", "request_fingerprint": "h01-request",
                "query_fingerprint": "h01-fp", "plan_version": 1,
            }
            for item_index, source_id in source_ids.items()
        ],
        cap=2,
    )
    runtime.set_provider_dispatch_rounds({"brightdata": 0})
    runtime.set_item_context(0, "paid")
    runtime.set_source_record_id("s0")
    checkpoint.begin_paid_attempt(run_id="run", item_index=0, attempt_number=1)
    claim = checkpoint.claim_provider_query_flight(
        run_id="run", provider="brightdata", query_fingerprint="h01-fp", owner_token="owner",
    )
    reservation = runtime.reserve_api(
        "brightdata", operation="h01", request_fingerprint="h01-request",
        flight_fingerprint="h01-fp", execution_generation=claim["execution_generation"],
    )
    assert reservation.accepted
    runtime.start_api(reservation)
    runtime.mark_api_http_started(reservation, 1, "h01-fp")
    checkpoint.bind_provider_query_flight_call(
        run_id="run", provider="brightdata", query_fingerprint="h01-fp",
        owner_token="owner", provider_call_id=reservation.call_id,
    )
    runtime.complete_api(reservation, "FAILED")
    checkpoint.finish_provider_query_flight(
        run_id="run", provider="brightdata", query_fingerprint="h01-fp",
        owner_token="owner", state="FAILED", result={"values": []},
        call_ids=[reservation.call_id],
    )
    checkpoint.record_paid_attempt(
        run_id="run", item_index=0, attempt_number=1, result="FAILED",
        call_ids=[reservation.call_id], call_relations={reservation.call_id: "OWNER"},
    )
    runtime.set_provider_dispatch_rounds({"brightdata": 0})
    runtime.set_item_context(1, "paid")
    runtime.set_source_record_id("s1")
    checkpoint.begin_paid_attempt(run_id="run", item_index=1, attempt_number=1)
    checkpoint.record_paid_attempt(
        run_id="run", item_index=1, attempt_number=1, result="FAILED",
        call_ids=[reservation.call_id], call_relations={reservation.call_id: "INHERITED"},
    )
    checkpoint.begin_paid_attempt(run_id="run", item_index=1, attempt_number=2)
    with pytest.raises(checkpoint.EvidenceInvariant):
        checkpoint.record_paid_attempt(
            run_id="run", item_index=1, attempt_number=2, result="FAILED",
            call_ids=[reservation.call_id], call_relations={reservation.call_id: "OWNER"},
        )
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM paid_attempt_calls WHERE item_index=1 AND attempt_number=2"
        ).fetchone()[0] == 0


def test_h02_three_primary_queries_are_three_physical_calls_and_round_independent(tmp_path, monkeypatch):
    source, transport, runs = _empty_primary_run(
        tmp_path, monkeypatch, companies=["Single Company"], workers=1, query_limit=3, budget=3,
    )
    outcome = main.run(source, allow_paid=True)
    root = next(runs.iterdir())
    assert outcome.status is pipeline_runner.PipelineOutcomeStatus.COMPLETE
    assert len(transport.journal) == 3
    with sqlite3.connect(root / "state" / "progress.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='brightdata'").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(DISTINCT flight_fingerprint) FROM provider_calls WHERE provider='brightdata'").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM provider_dispatch_rounds WHERE provider='brightdata' AND state='COMPLETE'").fetchone()[0] == 3


def _content_fixture(source_id: str) -> tuple[dict, list[dict]]:
    website = "https://projection.example"
    evidence_id = "evidence-projection"
    records = [{
        "evidence_id": evidence_id,
        "target_source_record_id": source_id,
        "evidence_source_record_id": "page:projection",
        "observed_business": "Projection Company",
        "url": website,
        "final_url": website,
        "content_sha256": "a" * 64,
        "retrieval_method": "http",
        "observation_type": "legal_name",
        "observation_value": "Projection Company",
        "location": {"kind": "page_text"},
        "relation": "first_party_identity",
    }]
    content = publication_policy.ContentDecision(
        verified=True,
        website_allowed=True,
        email_allowed=True,
        phone_allowed=False,
        support_evidence_ids=(evidence_id,),
        identity_status="verified",
        identity_route="LEGAL_NAME",
        missing_evidence=("phone_evidence_missing",),
        target_source_record_id=source_id,
        evidence_fingerprint=publication_policy.evidence_fingerprint(
            records, target_source_record_id=source_id, website=website,
            email="info@projection.example", phone="+999",
        ),
        bound_website="projection.example",
        bound_email="info@projection.example",
        bound_phone="+999",
    ).as_dict()
    return content, records


def test_h03_projection_keeps_valid_email_suppresses_invalid_phone_and_preserves_raw_evidence(tmp_path, monkeypatch):
    source_id = "source:projection"
    content, records = _content_fixture(source_id)
    raw = {
        "company": "Projection Company",
        "source_record_id": source_id,
        "free_state": "DONE",
        "paid_required": False,
        "paid_state": "NOT_REQUIRED",
        "status": "OK_HIGH_CONFIDENCE",
        "score": 95,
        "website": "https://projection.example",
        "email": "info@projection.example",
        "phone": "+999",
        "email_publication_status": "allowed",
        "phone_publication_status": "suppressed",
        "identity_assessment": {"publishable": True},
        "content_decision": content,
        "content_evidence_records": records,
    }
    projected = output_artifacts.project_publication_row(raw)
    assert raw["phone"] == "+999"
    assert projected["email"] == "info@projection.example"
    assert projected["phone"] == ""
    assert projected["contact_status"] == "partial"
    assert output_artifacts.validate_publication_projection(projected)
    projected["email"] = "forged@example.invalid"
    assert not output_artifacts.validate_publication_projection(projected)
