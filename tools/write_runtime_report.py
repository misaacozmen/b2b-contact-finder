from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from modules.run_context import source_tree_sha256


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    lock_path = root / "runtime-lock.txt"
    smoke = subprocess.run(
        [sys.executable, str(root / "tools" / "runtime_smoke.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    smoke_data = json.loads(smoke.stdout)
    report = {
        "interpreter": str(Path(sys.executable).resolve()),
        "python_version": sys.version,
        "sqlite_version": sqlite3.sqlite_version,
        "runtime_source_tree_sha256": source_tree_sha256(root),
        "runtime_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "runtime_lock_path": str(lock_path.resolve()),
        "browser_smoke": smoke_data["browser"],
        "ocr_smoke": smoke_data["ocr"],
        "runtime_guard_3_50_4_rejected": sqlite3.sqlite_version != "3.50.4",
    }
    (root / "runtime_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return smoke.returncode


if __name__ == "__main__":
    raise SystemExit(main())
