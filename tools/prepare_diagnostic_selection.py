"""Materialize the frozen 96-row diagnostic review selection from the preserved manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.selection_manifest.read_text(encoding="utf-8"))
    rows = payload.get("diagnostic_rows") or []
    if len(rows) != 96 or len({row.get("source_record_id") for row in rows}) != 96:
        raise RuntimeError("diagnostic selection must contain exactly 96 unique source IDs")
    records = []
    for row in rows:
        records.append({
            "source_record_id": row["source_record_id"],
            "source": row["source"],
            "display_name": row.get("Company", ""),
            "official_profile_url": row.get("official_profile_url", ""),
            "listing_url": row.get("listing_url", ""),
        })
    result = {
        "schema_version": 1,
        "selection_kind": "diagnostic_96",
        "source_manifest_sha256": payload.get("manifest_sha256"),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"records": len(records)}))


if __name__ == "__main__":
    main()
