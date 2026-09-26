"""Subprocess entry point for process-isolated PETZOO acceptance runs."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))


class _Setter:
    @staticmethod
    def setattr(target, name, value, raising=True):
        del raising
        setattr(target, name, value)


def main_entry() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--transport-journal", required=True)
    parser.add_argument("--http-journal", required=True)
    parser.add_argument("--network-journal", required=True)
    args = parser.parse_args()

    os.environ["B2B_SOCKET_DENY_JSONL"] = str(Path(args.network_journal).resolve())
    os.environ["B2B_COMMAND_ID"] = f"petzoo-p11-workers-{args.workers}"
    os.environ.update({
        "BRIGHTDATA_API_KEY": "fake-petzoo-brightdata",
        "GOOGLE_PLACES_API_KEY": "fake-petzoo-places",
        "BRANDFETCH_CLIENT_ID": "fake-petzoo-brandfetch",
        "HUNTER_API_KEY": "fake-petzoo-hunter",
        "OPENROUTER_API_KEY": "fake-petzoo-llm",
    })

    # Importing conftest arms a process-level audit hook. The fixture resolver
    # later replaces only DNS for the explicit .example fixture zone; all
    # socket connects, HTTP fallthroughs, and unapproved DNS remain denied.
    import conftest  # noqa: F401
    from petzoo_fixture_support import (
        _append_jsonl, install_harness, load_fixture,
        petzoo_group_c_transport, write_input_book,
    )
    import main as pipeline_main
    from modules import checkpoint, runtime

    _append_jsonl(Path(args.network_journal), {
        "kind": "guard_armed", "command_id": os.environ["B2B_COMMAND_ID"],
        "pid": os.getpid(), "child": True,
    })
    manifest, records, fixture_sha256 = load_fixture()
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    source = workspace / "fixture.xlsx"
    write_input_book(source, records)
    transport_path = Path(args.transport_journal).resolve()
    http_path = Path(args.http_journal).resolve()
    router, group_c_route_report = petzoo_group_c_transport(records, transport_path)
    install_harness(
        _Setter(), workspace, records, workers=int(args.workers), router=router,
        http_journal=http_path,
    )
    outcome = pipeline_main.run(source, allow_paid=True)
    run_dirs = sorted((workspace / "runs").iterdir())
    if len(run_dirs) != 1:
        raise RuntimeError(f"expected exactly one run directory, got {len(run_dirs)}")
    run_root = run_dirs[0]
    manifest_path = run_root / "manifest.json"
    run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    db_path = run_root / "state" / "progress.sqlite3"
    import config
    config.PROGRESS_DB_FILE = db_path
    run_id = str(run_manifest["run_id"])
    receipt = checkpoint.canonical_scheduler_receipt(run_id)
    db_uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        work_rows = [dict(row) for row in db.execute(
            "SELECT item_index,source_record_id,provider,operation,request_fingerprint,"
            "query_fingerprint,job_fingerprint,state,terminal_reason,call_id,dependency_job_fingerprint "
            "FROM provider_work_items WHERE run_id=? ORDER BY item_index,provider,operation,request_fingerprint",
            (run_id,),
        )]
        allocation_rows = [dict(row) for row in db.execute(
            "SELECT provider,round_ordinal,item_index,source_record_id,need_class,state,job_fingerprint,consumed_call_id,terminal_state "
            "FROM provider_dispatch_allocations WHERE run_id=? ORDER BY provider,round_ordinal,item_index",
            (run_id,),
        )]
        item_rows = [dict(row) for row in db.execute(
            "SELECT item_index,source_record_id,free_state,paid_state,paid_required "
            "FROM run_items WHERE run_id=? ORDER BY item_index",
            (run_id,),
        )]
        call_rows = [dict(row) for row in db.execute(
            "SELECT call_id,item_index,provider,operation,request_fingerprint,flight_fingerprint,"
            "state,http_started_at FROM provider_calls WHERE run_id=? ORDER BY provider,item_index,call_id",
            (run_id,),
        )]
        attempt_call_rows = [dict(row) for row in db.execute(
            "SELECT item_index,attempt_number,call_id,provider,query_fingerprint,relation "
            "FROM paid_attempt_calls WHERE run_id=? ORDER BY item_index,attempt_number,call_id",
            (run_id,),
        )]
        attempt_rows = [dict(row) for row in db.execute(
            "SELECT item_index,attempt_number,result,evidence_kind,input_snapshot_sha256 "
            "FROM paid_attempts WHERE run_id=? ORDER BY item_index,attempt_number",
            (run_id,),
        )]
        no_call_rows = [dict(row) for row in db.execute(
            "SELECT item_index,evidence_kind,input_snapshot_sha256,normalized_input_website,"
            "evaluation_payload_sha256,result_payload_sha256,publication_eligible,evaluator_schema_version "
            "FROM paid_no_call_evidence WHERE run_id=? ORDER BY item_index,paid_attempt_id",
            (run_id,),
        )]
    result = {
        "fixture_id": manifest["fixture_id"],
        "fixture_sha256": fixture_sha256,
        "worker_count": int(args.workers),
        "pid": os.getpid(),
        "run_id": run_id,
        "run_root": str(run_root),
        "outcome": getattr(getattr(outcome, "status", None), "value", str(outcome)),
        "manifest_complete": bool(run_manifest.get("complete")),
        "manifest_phase": str(run_manifest.get("phase", "")),
        "transport_journal": str(transport_path),
        "http_journal": str(http_path),
        "network_journal": str(Path(args.network_journal).resolve()),
        "provider_budgets": receipt.get("provider_budgets", {}),
        "source_count": len(item_rows),
        "source_record_ids": [row["source_record_id"] for row in item_rows],
        "work_state_counts": {},
        "physical_provider_calls": {},
        "provider_work_count": len(work_rows),
        "work": work_rows,
        "allocations": allocation_rows,
        "group_c_route_report": group_c_route_report,
        "items": item_rows,
        "calls": call_rows,
        "attempt_call_relations": attempt_call_rows,
        "attempts": attempt_rows,
        "no_call_receipts": no_call_rows,
    }
    for row in work_rows:
        key = f"{row['state']}"
        result["work_state_counts"][key] = result["work_state_counts"].get(key, 0) + 1
    for provider, budget in receipt.get("provider_budgets", {}).items():
        result["physical_provider_calls"][provider] = int(budget.get("physical_http_attempts", 0))
    result_path = Path(args.result).resolve()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    runtime.reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main_entry())
