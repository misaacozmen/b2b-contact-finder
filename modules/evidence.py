"""Write field-level discovery evidence without leaking API credentials."""

from __future__ import annotations

import json
from pathlib import Path


def _json_safe(value):
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if not str(key).startswith("_secret")}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            record = {
                "company": row.get("company", ""),
                "selected": {
                    "website": row.get("website", ""),
                    "website_source": row.get("website_source", ""),
                    "email": row.get("email", ""),
                    "email_source_url": row.get("email_source_url", ""),
                    "phone": row.get("phone", ""),
                    "phone_source_url": row.get("phone_source_url", ""),
                    "status": row.get("status", ""),
                    "confidence": row.get("confidence", ""),
                    "score": row.get("score", 0),
                    "reason": row.get("reason", ""),
                },
                "candidates": row.get("__candidates", []),
                "search_trace": row.get("__search_trace", []),
                "source_health": row.get("__source_health", {}),
                "evaluation": row.get("__evaluation", {}),
                "candidate_evaluations": row.get("__candidate_evaluations", []),
            }
            handle.write(json.dumps(_json_safe(record), ensure_ascii=False) + "\n")
