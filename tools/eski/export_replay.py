"""Explicit portable replay export; pipeline runs never create this export."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from modules import replay_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export replay cache shards")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--directory", type=Path, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    manifest_path = args.run_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("run_root/manifest.json is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_id = str(manifest.get("run_id", ""))
    config_hash = str(manifest.get("config_sha256", ""))
    if not run_id or not config_hash:
        raise SystemExit("run manifest must contain run_id and config_sha256")
    directory = args.directory or (args.run_dir / "output" / "replay")
    replay_snapshot.configure_run_store(args.run_dir / "state" / "progress.sqlite3", run_id, read_only=True)
    replay_snapshot.export_shards(
        directory, run_id=run_id, config_hash=config_hash,
    )
