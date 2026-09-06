"""Split adjudicated source-review rows by frozen source-ID selections."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("records") or []
    values = [str(row.get("source_record_id") or "") for row in rows]
    if not values or any(not value for value in values) or len(values) != len(set(values)):
        raise RuntimeError(f"selection IDs are not exact: {path}")
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adjudicated", type=Path, required=True)
    parser.add_argument("--diagnostic-selection", type=Path, required=True)
    parser.add_argument("--independent-selection", type=Path, required=True)
    parser.add_argument("--diagnostic-output", type=Path, required=True)
    parser.add_argument("--independent-output", type=Path, required=True)
    args = parser.parse_args()
    diagnostic_ids = _ids(args.diagnostic_selection)
    independent_ids = _ids(args.independent_selection)
    rows = [json.loads(line) for line in args.adjudicated.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {str(row.get("source_record_id") or ""): row for row in rows}
    if len(rows) != len(by_id) or set(by_id) != set(diagnostic_ids) | set(independent_ids):
        raise RuntimeError("adjudicated IDs do not match frozen selections")
    for output, ids in ((args.diagnostic_output, diagnostic_ids), (args.independent_output, independent_ids)):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("".join(json.dumps(by_id[source_id], ensure_ascii=False, sort_keys=True) + "\n" for source_id in ids), encoding="utf-8")
    print(json.dumps({"diagnostic": len(diagnostic_ids), "independent": len(independent_ids)}))


if __name__ == "__main__":
    main()
