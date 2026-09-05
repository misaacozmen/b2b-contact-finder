from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acquisition", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.acquisition.read_text(encoding="utf-8"))
    records = payload["hometex"]["selected"] + payload["ambiente"]["selected"]
    if len(records) != 120 or len({row["source_record_id"] for row in records}) != 120:
        raise RuntimeError("independent selection must be exactly 120 unique records")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"schema_version": 1, "records": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"records": len(records)}))


if __name__ == "__main__":
    main()
