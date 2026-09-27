"""Read-only check: apply the declined-work closure to a copy of a stalled run."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from modules import checkpoint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args()
    run_root = Path(args.run_root).resolve()
    source = run_root / "state" / "progress.sqlite3"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        copy = Path(directory) / "progress.sqlite3"
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as reader, closing(sqlite3.connect(copy)) as writer:
            reader.backup(writer)
        config.PROGRESS_DB_FILE = copy
        checkpoint._SCHEMA_READY.clear()
        checkpoint.initialize_schema(copy)
        run_id = run_root.name
        closed = checkpoint.close_executor_declined_work(run_id=run_id)
        states = {}
        for item_index in sorted({row["item_index"] for row in closed}):
            reconciled = checkpoint.reconcile_paid_item_state_after_work_materialization(
                run_id=run_id, item_index=item_index,
            )
            states[item_index] = (reconciled or {}).get("paid_state")
        pending = checkpoint.pending_provider_work(run_id)
        evidence = checkpoint.validate_paid_evidence(run_id, collect=True)
        print(json.dumps({
            "closed_jobs": len(closed),
            "closed_items": sorted(states),
            "paid_state_after": states,
            "pending_jobs_after": len(pending),
            "evidence_violations": evidence.get("violations", []),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
