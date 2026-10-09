"""Rebuild run summary artifacts from a completed run without touching its state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from modules import checkpoint, field_merge, opt_out, run_report  # noqa: E402


def _read_json(path: Path, *, required: bool = True) -> dict:
    if not path.is_file():
        if required:
            raise FileNotFoundError(path)
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _load_database(db_path: Path, run_id: str) -> tuple[list[dict], dict, dict, float | None]:
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        run = connection.execute(
            "SELECT phase,context_json,runtime_json FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ValueError(f"run not found in database: {run_id}")
        context = json.loads(run["context_json"] or "{}")
        runtime = json.loads(run["runtime_json"] or "{}")
        finalization = connection.execute(
            "SELECT output_context_json FROM finalization_intent WHERE run_id=?",
            (run_id,),
        ).fetchone()
        output_context = json.loads(finalization["output_context_json"] or "{}") if finalization else {}
        query = """
            SELECT r.item_index,r.payload,
                   i.free_state,i.paid_required,i.paid_state,i.free_attempts,i.paid_attempts,
                   i.last_error,i.quarantine_state,i.quarantine_status,i.publication_blockers,
                   s.snapshot_json
            FROM results AS r
            JOIN run_items AS i USING(run_id,item_index)
            LEFT JOIN immutable_input_snapshots AS s USING(run_id,item_index)
            WHERE r.run_id=?
            ORDER BY r.item_index
        """
        rows: list[dict] = []
        for record in connection.execute(query, (run_id,)):
            row = json.loads(record["payload"])
            snapshot = json.loads(record["snapshot_json"] or "{}")
            row.update({
                "run_id": run_id,
                "free_state": record["free_state"],
                "paid_required": bool(record["paid_required"]),
                "paid_state": record["paid_state"],
                "free_attempts": int(record["free_attempts"] or 0),
                "paid_attempts": int(record["paid_attempts"] or 0),
                "last_error": record["last_error"] or "",
                "quarantine_state": record["quarantine_state"] or "",
                "quarantine_status": record["quarantine_status"] or "",
                "publication_blockers": record["publication_blockers"] or "",
                "listed_website_status": snapshot.get("listed_website_status", ""),
                "website_input_status": snapshot.get("website_input_status", ""),
                "listing_source": snapshot.get("source", ""),
                "listing_hall": snapshot.get("hall", ""),
                "listing_stand": snapshot.get("stand", ""),
            })
            stage_a = row.get("stage_a")
            if isinstance(stage_a, dict):
                stage_a = dict(stage_a)
                # Historical stage-A snapshots omit website_source. They are
                # reference-blind search results, so let field_merge infer OWN_SEARCH.
                if stage_a.get("website"):
                    field_merge.annotate(stage_a, None)
                row["stage_a"] = stage_a
            rows.append(row)
    if not rows:
        raise ValueError(f"run has no result rows: {run_id}")
    elapsed = output_context.get("elapsed_seconds", runtime.get("elapsed_seconds"))
    return rows, context, output_context, float(elapsed) if elapsed is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    out_dir = args.out.resolve()
    if out_dir == run_dir or run_dir in out_dir.parents:
        parser.error("--out must be outside the source run directory")
    run_id = run_dir.name
    db_path = run_dir / "state" / "progress.sqlite3"
    rows, context, _output_context, elapsed = _load_database(db_path, run_id)

    status_payload = _read_json(run_dir / "output" / "run_status.json")
    telemetry = _read_json(run_dir / "output" / "telemetry.json", required=False)
    run_status = str(status_payload.get("run_status") or "")
    status_detail = str(status_payload.get("status_detail") or "recomputed")
    if not run_status:
        run_status = "TAMAMLANDI" if str(context.get("phase", "")).upper() == "COMPLETE" else "KISMI_KOSU_HATASI"

    state = {"run_id": run_id, "context": context}
    with patch.object(
        checkpoint,
        "load_run_state_by_id",
        side_effect=lambda requested_id: state if requested_id == run_id else None,
    ):
        errors = run_report.write_run_report(
            rows,
            output_root=out_dir,
            run_status=run_status,
            status_detail=status_detail,
            elapsed_seconds=elapsed,
            telemetry=telemetry,
            opt_out_entries=opt_out.load(config.OPT_OUT_FILE),
        )
    if errors:
        print(json.dumps({"run_id": run_id, "out": str(out_dir), "errors": errors}, ensure_ascii=False))
        return 1
    print(json.dumps({
        "run_id": run_id,
        "out": str(out_dir),
        "report": str(out_dir / "rapor.md"),
        "rows": len(rows),
        "source_database_read_only": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
