"""Run one child command in an isolated process group with an atomic report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _interrupt_process_group(process: subprocess.Popen[bytes], actions: list[dict]) -> None:
    if os.name == "nt":
        actions.append({"action": "CTRL_BREAK_EVENT", "at": _utc_now()})
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        actions.append({"action": "SIGINT_PROCESS_GROUP", "at": _utc_now()})
        os.killpg(os.getpgid(process.pid), signal.SIGINT)


def _terminate_process_group(process: subprocess.Popen[bytes], actions: list[dict]) -> None:
    if os.name == "nt":
        actions.append({"action": "TERMINATE_PROCESS_GROUP", "at": _utc_now(), "method": "taskkill_tree"})
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        actions.append({"action": "SIGTERM_PROCESS_GROUP", "at": _utc_now()})
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)


def run(command: list[str], timeout_seconds: float, report_path: Path) -> int:
    if not command:
        raise ValueError("a child command is required")
    if command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("a child command is required")

    report_path = report_path.resolve()
    stdout_path = report_path.with_name(f"{report_path.stem}.stdout.txt")
    stderr_path = report_path.with_name(f"{report_path.stem}.stderr.txt")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    command_sha256 = hashlib.sha256(
        json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    actions: list[dict] = []
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    popen_kwargs = {"start_new_session": os.name != "nt"}
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            command,
            shell=False,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            **popen_kwargs,
        )
        timed_out = False
        try:
            child_exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _interrupt_process_group(process, actions)
            try:
                child_exit_code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _terminate_process_group(process, actions)
                try:
                    child_exit_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child_exit_code = None
    finished_at = _utc_now()
    elapsed = time.monotonic() - started_monotonic
    report = {
        "schema_version": 1,
        "command": command,
        "command_sha256": command_sha256,
        "pid": process.pid,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "monotonic_elapsed_seconds": elapsed,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "child_exit_code": child_exit_code,
        "interrupt_terminate_actions": actions,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
    }
    _write_atomic(report_path, report)
    if timed_out:
        return 124
    return int(child_exit_code if child_exit_code is not None else 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        return run(args.command, args.timeout_seconds, args.report)
    except Exception as exc:
        print(f"run_timeboxed error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
