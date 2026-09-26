"""Replay provider work item reconciliation against a private DB copy."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from modules import checkpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    source_db = args.run_dir / "state" / "progress.sqlite3"
    if not source_db.is_file():
        parser.error(f"progress database not found: {source_db}")

    exception_counts: Counter[tuple[str, str]] = Counter()
    with tempfile.TemporaryDirectory(prefix="replay-reconcile-") as temp_dir:
        copied_db = Path(temp_dir) / "progress.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{source_db}{suffix}")
            if source.is_file():
                shutil.copy2(source, Path(f"{copied_db}{suffix}"))
        config.PROGRESS_DB_FILE = copied_db

        with closing(sqlite3.connect(copied_db)) as connection:
            run_ids = [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT run_id FROM provider_work_items WHERE query_fingerprint<>'' ORDER BY run_id"
            )]
            jobs = [
                (run_id, str(row[0]))
                for run_id in run_ids
                for row in connection.execute(
                    "SELECT job_fingerprint FROM provider_work_items WHERE run_id=? AND query_fingerprint<>''",
                    (run_id,),
                )
            ]
            connection.commit()

        for run_id, job_fingerprint in jobs:
            try:
                checkpoint.reconcile_provider_work_item_to_flight(
                    run_id=run_id, job_fingerprint=job_fingerprint,
                )
            except Exception as exc:
                exception_counts[(type(exc).__name__, str(exc))] += 1

    for (exception_class, message), count in sorted(exception_counts.items()):
        print(f"{exception_class}:{message}: {count}")
    total = sum(exception_counts.values())
    print(f"exceptions={total}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
