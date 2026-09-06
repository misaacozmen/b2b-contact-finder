from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "tools" / "run_timeboxed.py"


def _run(tmp_path: Path, timeout: float, *child_args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    report = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(WRAPPER), "--timeout-seconds", str(timeout), "--report", str(report), "--", sys.executable, "-c", *child_args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=45,
    )
    return result, json.loads(report.read_text(encoding="utf-8"))


def test_normal_exit_and_atomic_report(tmp_path: Path) -> None:
    result, report = _run(tmp_path, 5, "import sys; print('ok'); sys.exit(7)")
    assert result.returncode == 7
    assert report["timed_out"] is False
    assert report["child_exit_code"] == 7
    assert report["stdout_sha256"]
    assert Path(report["stdout_path"]).read_text(encoding="utf-8").strip() == "ok"


def test_sleep_timeout_returns_124_and_child_is_gone(tmp_path: Path) -> None:
    result, report = _run(tmp_path, 0.2, "import time; time.sleep(60)")
    assert result.returncode == 124
    assert report["timed_out"] is True
    assert report["child_exit_code"] is not None
    with __import__("contextlib").suppress(OSError):
        os.kill(report["pid"], 0)
        raise AssertionError("timed-out child is still alive")


def test_spaces_and_turkish_arguments_are_preserved(tmp_path: Path) -> None:
    marker = tmp_path / "argument with boşluk.txt"
    result, report = _run(tmp_path, 5, "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')", str(marker), "İstanbul & $HOME")
    assert result.returncode == 0
    assert report["command"][0] == sys.executable
    assert marker.read_text(encoding="utf-8") == "İstanbul & $HOME"


def test_timeout_interrupt_does_not_leave_process_group(tmp_path: Path) -> None:
    result, report = _run(tmp_path, 0.2, "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)")
    assert result.returncode == 124
    assert report["timed_out"] is True
    assert report["interrupt_terminate_actions"]


def test_shell_interpolation_is_never_used(tmp_path: Path) -> None:
    result, report = _run(tmp_path, 5, "import sys; assert sys.argv[1] == 'a&b|c'; print(sys.argv[1])", "a&b|c")
    assert result.returncode == 0
    assert "a&b|c" in Path(report["stdout_path"]).read_text(encoding="utf-8")
