"""Build an offline publication risk/coverage report from labelled JSON/JSONL.

Required record fields:
  publication_safety_score: integer 0..100
  correct: boolean (or true/false, yes/no, 1/0)
  split_role: calibration or validation when recommending a threshold
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from modules import risk_calibration


def load_records(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        value = json.loads(text)
        if not isinstance(value, list):
            raise ValueError("JSON calibration input must be an array")
        return value
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-precision", type=float, default=0.99)
    parser.add_argument("--minimum-accepted", type=int, default=20)
    parser.add_argument(
        "--evaluate-only",
        action="store_true",
        help="Emit a risk/coverage curve without fitting a threshold; safe for holdout audit.",
    )
    args = parser.parse_args()
    records = load_records(args.records)
    if args.evaluate_only:
        result = {
            "mode": "evaluate_only",
            "curve": risk_calibration.risk_coverage_curve(records),
        }
    else:
        result = {
            "mode": "recommendation",
            **risk_calibration.recommend_threshold(
                records,
                target_precision=args.target_precision,
                minimum_accepted=args.minimum_accepted,
            ),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
