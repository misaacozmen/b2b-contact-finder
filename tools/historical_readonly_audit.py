from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


RUN_IDS = (
    "c301ac49204a145aabe8452e1420bef75eb69df5d50412cbfa9691c53a9620ca",
    "ff793bbf477194928303a18eea97cc6de82e942655fe044a1c136be797532fa1",
)


def receipt(path: Path) -> dict:
    data = path.read_bytes()
    stat = path.stat()
    return {"path": str(path.resolve()), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mtime_ns": stat.st_mtime_ns}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = []
    before = {}
    for run_id in RUN_IDS:
        root = args.root / "runs" / run_id
        db = root / "state" / "progress.sqlite3"
        files = [db, root / "manifest.json"]
        files.extend(path for path in (root / "output" / "logs.txt", root / "logs.txt") if path.is_file())
        if not db.is_file() or not (root / "manifest.json").is_file():
            raise SystemExit(f"historical run incomplete: {run_id}")
        before[run_id] = [receipt(path) for path in files]
        uri = f"file:{db.resolve().as_posix()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            item_count = connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=?", (run_id,)).fetchone()[0]
        records.append({"run_id": run_id, "sqlite_uri_mode": "ro&immutable=1", "integrity": integrity, "item_count": item_count})
    after = {run_id: [receipt(Path(item["path"])) for item in items] for run_id, items in before.items()}
    unchanged = before == after
    payload = {"runs": records, "before": before, "after": after, "unchanged": unchanged}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if unchanged and all(row["integrity"] == "ok" for row in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
