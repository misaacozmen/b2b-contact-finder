import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_progress(input_path: Path) -> dict[str, Any] | None:
    if not config.PROGRESS_FILE.exists():
        return None
    try:
        data = json.loads(config.PROGRESS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("input_file_hash") != file_hash(input_path):
        return None
    return data


def save_progress(input_path: Path, last_completed_index: int, results_so_far: list[dict[str, Any]]) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "input_file_hash": file_hash(input_path),
        "last_completed_index": last_completed_index,
        "results_so_far": results_so_far,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    tmp_path = config.PROGRESS_FILE.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(config.PROGRESS_FILE)


def clear_progress() -> None:
    if config.PROGRESS_FILE.exists():
        config.PROGRESS_FILE.unlink()

