"""Process-isolated crash/resume runner for real PETZOO P12 pipeline boundaries."""

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


def _exit_at(boundary: str, observed: str, journal: Path, *, call_id: str = "") -> None:
    from petzoo_fixture_support import _append_jsonl

    _append_jsonl(journal, {
        "kind": "p12_fault_injected", "boundary": boundary,
        "observed_checkpoint": observed, "call_id": str(call_id), "pid": os.getpid(),
    })
    os._exit(86)


def _install_fault(boundary: str, journal: Path) -> None:
    from modules import checkpoint

    if boundary in {"allocation_before", "allocation_after"}:
        original = checkpoint.reserve_provider_dispatch_round

        def wrapped(*args, **kwargs):
            provider = str(kwargs.get("provider", args[1] if len(args) > 1 else ""))
            candidates = kwargs.get("candidates", args[4] if len(args) > 4 else [])
            if boundary == "allocation_before" and provider == "brightdata" and candidates:
                _exit_at(boundary, "before_allocation_transaction", journal)
            result = original(*args, **kwargs)
            if boundary == "allocation_after" and provider == "brightdata" and candidates:
                _exit_at(boundary, "after_allocation_commit", journal)
            return result

        checkpoint.reserve_provider_dispatch_round = wrapped
    elif boundary == "call_reserved_before_http":
        original = checkpoint.reserve_provider_call

        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            provider = str(kwargs.get("provider", ""))
            if provider == "brightdata" and result:
                _exit_at(boundary, "after_call_reserved_commit", journal, call_id=str(result))
            return result

        checkpoint.reserve_provider_call = wrapped
    elif boundary == "http_started_before_response":
        original = checkpoint.mark_provider_call_http_started

        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            _exit_at(boundary, "after_http_started_commit_before_transport", journal, call_id=str(kwargs.get("call_id", "")))
            return result

        checkpoint.mark_provider_call_http_started = wrapped
    elif boundary == "terminal_call_before_company_save":
        original = checkpoint.complete_provider_call_and_flight_success

        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            _exit_at(boundary, "after_terminal_call_and_result_commit", journal, call_id=str(kwargs.get("provider_call_id", "")))
            return result

        checkpoint.complete_provider_call_and_flight_success = wrapped
    elif boundary == "finalization_after_memory_plan":
        original = checkpoint.commit_finalization_memory_plan

        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            _exit_at(boundary, "after_finalization_memory_plan_commit", journal)
            return result

        checkpoint.commit_finalization_memory_plan = wrapped


def main_entry() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--boundary", required=True)
    parser.add_argument("--resume-run", default="")
    parser.add_argument("--result", required=True)
    parser.add_argument("--transport-journal", required=True)
    parser.add_argument("--http-journal", required=True)
    parser.add_argument("--network-journal", required=True)
    parser.add_argument("--fault-journal", required=True)
    args = parser.parse_args()

    os.environ["B2B_SOCKET_DENY_JSONL"] = str(Path(args.network_journal).resolve())
    os.environ["B2B_COMMAND_ID"] = f"petzoo-p12-{args.boundary}"
    os.environ.update({
        "BRIGHTDATA_API_KEY": "fake-petzoo-brightdata",
        "GOOGLE_PLACES_API_KEY": "fake-petzoo-places",
        "BRANDFETCH_CLIENT_ID": "fake-petzoo-brandfetch",
        "HUNTER_API_KEY": "fake-petzoo-hunter",
        "OPENROUTER_API_KEY": "fake-petzoo-llm",
    })
    import conftest  # noqa: F401
    from petzoo_fixture_support import _append_jsonl, ReplayPaidTransport, install_harness, load_fixture, write_input_book
    import config
    import main as pipeline_main
    from modules import checkpoint, runtime

    _append_jsonl(Path(args.network_journal), {
        "kind": "guard_armed", "command_id": os.environ["B2B_COMMAND_ID"],
        "pid": os.getpid(), "child": True,
    })
    _manifest, fixture, fixture_sha256 = load_fixture()
    record = dict(next(row for row in fixture if row["group"] == "B"))
    record["item_index"] = 0
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    source = workspace / "fixture.xlsx"
    if not source.is_file():
        write_input_book(source, [record])
    transport_path = Path(args.transport_journal).resolve()
    http_path = Path(args.http_journal).resolve()
    install_harness(
        _Setter(), workspace, [record], workers=1,
        router=ReplayPaidTransport([record], transport_path),
        http_journal=http_path,
    )
    # install_harness chooses all six fixed budgets and routes only approved fixture traffic.
    if not args.resume_run:
        if args.boundary != "none":
            _install_fault(args.boundary, Path(args.fault_journal).resolve())
        outcome = pipeline_main.run(source, allow_paid=True)
    else:
        outcome = pipeline_main.run(
            source, allow_paid=True, resume_run=Path(args.resume_run).resolve(),
        )

    run_root = Path(args.resume_run).resolve() if args.resume_run else next((workspace / "runs").glob("*"))
    manifest_path = run_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    db_path = run_root / "state" / "progress.sqlite3"
    config.PROGRESS_DB_FILE = db_path
    run_id = str(manifest["run_id"])
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        calls = [dict(row) for row in db.execute(
            "SELECT call_id,item_index,provider,operation,request_fingerprint,flight_fingerprint,state,http_started_at FROM provider_calls WHERE run_id=? ORDER BY created_at,call_id",
            (run_id,),
        )]
        allocations = [dict(row) for row in db.execute(
            "SELECT provider,round_ordinal,item_index,source_record_id,state,job_fingerprint,consumed_call_id FROM provider_dispatch_allocations WHERE run_id=? ORDER BY provider,round_ordinal,item_index",
            (run_id,),
        )]
        work = [dict(row) for row in db.execute(
            "SELECT provider,operation,request_fingerprint,job_fingerprint,state,call_id FROM provider_work_items WHERE run_id=? ORDER BY provider,job_fingerprint",
            (run_id,),
        )]
        receipts = [dict(row) for row in db.execute(
            "SELECT call_id,provider,item_index,reason,http_started_at,attempt_ordinal FROM provider_call_recovery_receipts WHERE run_id=? ORDER BY call_id",
            (run_id,),
        )]
        intent_rows = [dict(row) for row in db.execute(
            "SELECT status,memory_plan_committed,memory_plan_count FROM finalization_intent WHERE run_id=?",
            (run_id,),
        )]
        outbox = [dict(row) for row in db.execute(
            "SELECT state,COUNT(*) AS count FROM memory_outbox WHERE run_id=? GROUP BY state ORDER BY state",
            (run_id,),
        )]
    result = {
        "boundary": args.boundary, "fixture_sha256": fixture_sha256,
        "pid": os.getpid(), "run_id": run_id, "run_root": str(run_root),
        "outcome": str(getattr(getattr(outcome, "status", None), "value", outcome)),
        "manifest_complete": bool(manifest.get("complete")),
        "manifest_phase": str(manifest.get("phase", "")),
        "calls": calls, "allocations": allocations, "work": work,
        "pre_http_recovery_receipts": receipts,
        "finalization_intent": intent_rows, "memory_outbox": outbox,
        "transport_journal": str(transport_path),
        "http_journal": str(http_path),
        "network_journal": str(Path(args.network_journal).resolve()),
    }
    result_path = Path(args.result).resolve()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    runtime.reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main_entry())
