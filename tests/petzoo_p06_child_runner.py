"""Fresh-process harness for the P06 lower-quality paid-resume acceptance case."""

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
    parser.add_argument("--mode", choices=("free", "resume"), required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--transport-journal", type=Path, required=True)
    parser.add_argument("--http-journal", type=Path, required=True)
    parser.add_argument("--network-journal", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()

    os.environ["B2B_TEST_OFFLINE"] = "1"
    os.environ["B2B_SOCKET_DENY_JSONL"] = str(args.network_journal.resolve())
    os.environ.setdefault("B2B_COMMAND_ID", f"petzoo-p06-{args.mode}")
    os.environ.update({
        "BRIGHTDATA_API_KEY": "fake-petzoo-brightdata",
        "GOOGLE_PLACES_API_KEY": "fake-petzoo-places",
        "BRANDFETCH_CLIENT_ID": "fake-petzoo-brandfetch",
        "HUNTER_API_KEY": "fake-petzoo-hunter",
        "OPENROUTER_API_KEY": "fake-petzoo-llm",
    })
    import conftest  # noqa: F401
    import config
    import main as pipeline_main
    from modules import checkpoint, runtime
    from petzoo_fixture_support import (
        FakeResponse, ReplayPaidTransport, _append_jsonl, _company_html,
        install_harness, load_fixture,
    )

    _fixture_manifest, fixture, fixture_sha256 = load_fixture()
    record = {**next(row for row in fixture if row["source_record_id"] == "petzoo:A:000"), "item_index": 0}
    router = ReplayPaidTransport([record], args.transport_journal)
    runs_dir, _router, session = install_harness(
        _Setter(), args.workspace, [record], workers=1, router=router,
        http_journal=args.http_journal,
    )
    config.ENABLE_GOOGLE_PLACES = False
    config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = False
    config.ENABLE_HUNTER_DOMAIN_FINDER = False
    config.ENABLE_LINKEDIN_COMPANY_LOOKUP = False
    config.ENABLE_LLM_ARBITER = False

    request_count = 0

    def controlled_get(url: str, **_kwargs):
        nonlocal request_count
        request_count += 1
        _append_jsonl(args.http_journal, {
            "kind": "p06_controlled_http", "pid": os.getpid(),
            "mode": args.mode, "attempt_ordinal": request_count,
            "source_record_id": record["source_record_id"], "url": str(url),
            "http_status": 200 if args.mode == "free" else 404,
        })
        if args.mode == "free":
            body = _company_html(
                record["company"], website=str(url), include_email=False,
                include_contact=False,
            )
            return FakeResponse(200, body, {"content-type": "text/html"}, url=str(url))
        return FakeResponse(404, "controlled lower-quality resume retrieval", {
            "content-type": "text/plain",
        }, url=str(url))

    session.get = controlled_get
    if args.mode == "free":
        outcome = pipeline_main.run(args.input, allow_paid=False)
        run_roots = [path for path in runs_dir.iterdir() if path.is_dir()]
        if len(run_roots) != 1:
            raise RuntimeError(f"expected one initial run directory, found {len(run_roots)}")
        run_root = run_roots[0]
    else:
        if args.run_root is None:
            raise RuntimeError("resume mode requires --run-root")
        run_root = args.run_root.resolve()
        outcome = pipeline_main.run(args.input, allow_paid=True, resume_run=run_root)

    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    db_path = run_root / "state" / "progress.sqlite3"
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        payload_row = db.execute(
            "SELECT payload FROM results WHERE run_id=? ORDER BY item_index LIMIT 1",
            (manifest["run_id"],),
        ).fetchone()
        calls = db.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE run_id=? AND provider='brightdata' AND http_started_at IS NOT NULL",
            (manifest["run_id"],),
        ).fetchone()[0]
        work_done = db.execute(
            "SELECT COUNT(*) FROM provider_work_items WHERE run_id=? AND provider='brightdata' AND state='DONE'",
            (manifest["run_id"],),
        ).fetchone()[0]
        links = db.execute(
            "SELECT COUNT(*) FROM paid_attempt_calls WHERE run_id=? AND provider='brightdata' AND relation IN ('OWNER','INHERITED')",
            (manifest["run_id"],),
        ).fetchone()[0]
    payload = json.loads(payload_row[0]) if payload_row else {}
    http_rows = [
        json.loads(line) for line in args.http_journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if args.http_journal.is_file() else []
    summary = {
        "mode": args.mode, "fixture_sha256": fixture_sha256,
        "run_id": manifest["run_id"], "run_root": str(run_root),
        "outcome": getattr(getattr(outcome, "status", None), "value", str(outcome)),
        "manifest_complete": bool(manifest.get("complete")),
        "payload": payload, "paid_brightdata_calls": int(calls),
        "paid_brightdata_work_done": int(work_done),
        "paid_attempt_call_links": int(links),
        "http_404_count": sum(row.get("http_status") == 404 for row in http_rows),
        "controlled_http_count": sum(row.get("kind") == "p06_controlled_http" for row in http_rows),
        "pid": os.getpid(),
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "payload"}, sort_keys=True))
    runtime.reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main_entry())
