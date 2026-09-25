"""Offline network and workspace-write guard inherited by this isolated run."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote


_DENIED_NETWORK_EVENTS = {
    "socket.getaddrinfo", "socket.gethostbyaddr", "socket.gethostbyname",
    "socket.gethostbyname_ex", "socket.getnameinfo", "socket.connect",
    "socket.connect_ex", "socket.sendto", "urllib.Request", "http.client.connect",
}
_WORKSPACE = os.path.normcase(os.path.abspath(os.environ.get("PETZOO_WORKSPACE_ROOT", "")))
_EVIDENCE = os.path.normcase(os.path.abspath(os.environ.get("PETZOO_EVIDENCE_DIR", "")))
_SNAPSHOT = os.path.normcase(os.path.abspath(os.environ.get("PETZOO_SOURCE_SNAPSHOT", "")))
_SNAPSHOT_OUTPUTS = os.path.normcase(os.path.realpath(os.environ.get(
    "PETZOO_SNAPSHOT_OUTPUTS", os.path.join(_SNAPSHOT, "outputs"),
)))
_TESTS = os.path.normcase(os.path.join(_SNAPSHOT, "tests"))
_HARNESS_FILES = {
    os.path.normcase(os.path.join(_EVIDENCE, name))
    for name in ("run_offline_regression.py", "petzoo_offline_plugin.py", "sitecustomize.py")
}
_REENTRY = False
_EVENT_SEQUENCE = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _append(path_text: str, record: dict) -> None:
    global _REENTRY
    if _REENTRY or not path_text:
        return
    _REENTRY = True
    try:
        target = Path(path_text)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(target, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        _REENTRY = False


def _lexical_path(value) -> str | None:
    if isinstance(value, int):
        return None
    try:
        raw = os.fsdecode(os.fspath(value))
    except (TypeError, ValueError, OSError):
        return None
    if raw.startswith("file:"):
        raw = unquote(raw[5:].split("?", 1)[0])
        if os.name == "nt" and raw.startswith("/"):
            raw = raw.lstrip("/")
    if not raw:
        return None
    if raw == ":memory:":
        return None
    if not os.path.isabs(raw):
        raw = os.path.join(os.getcwd(), raw)
    return os.path.normcase(os.path.abspath(os.path.normpath(raw)))


def _path(value) -> str | None:
    lexical = _lexical_path(value)
    return os.path.normcase(os.path.realpath(lexical)) if lexical else None


def _log_target(path_text: str | None) -> str | None:
    if not path_text or not path_text.strip():
        return None
    raw = Path(path_text.strip())
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    return os.path.normcase(str(raw.resolve(strict=False)))


def _emit_network_event(record: dict) -> None:
    targets = []
    for path_text in (
        os.environ.get("PETZOO_NETWORK_GUARD_LOG", ""),
        os.environ.get("B2B_SOCKET_DENY_JSONL", ""),
    ):
        target = _log_target(path_text)
        if target and target not in targets:
            targets.append(target)
    for target in targets:
        _append(target, record)


def _within(path: str | None, root: str) -> bool:
    if not path or not root:
        return False
    try:
        return os.path.commonpath((path, root)) == root
    except (ValueError, OSError):
        return False


def _is_snapshot_write(path: str | None) -> bool:
    return _within(path, _SNAPSHOT) and not _within(path, _SNAPSHOT_OUTPUTS)


def _is_snapshot_output(path: str | None) -> bool:
    return _within(path, _SNAPSHOT_OUTPUTS) and _within(path, _SNAPSHOT)


def _is_snapshot_output_escape(value) -> bool:
    lexical = _lexical_path(value)
    if not _within(lexical, _SNAPSHOT_OUTPUTS):
        return False
    resolved = os.path.normcase(os.path.realpath(lexical))
    return not _is_snapshot_output(resolved)


def _snapshot_output_link_creation(event: str, args: tuple) -> bool:
    if event not in {"os.symlink", "os.link"} or len(args) < 2:
        return False
    link_path = _lexical_path(args[1])
    return _within(link_path, _SNAPSHOT_OUTPUTS)


def _snapshot_output_rename_escape(event: str, args: tuple) -> bool:
    if event != "os.rename" or len(args) < 2:
        return False
    source_lexical = _lexical_path(args[0])
    destination_lexical = _lexical_path(args[1])
    return (
        _within(source_lexical, _SNAPSHOT_OUTPUTS)
        and not _is_snapshot_output(_path(args[1]))
    ) or (
        source_lexical == _SNAPSHOT_OUTPUTS
        or destination_lexical == _SNAPSHOT_OUTPUTS
    )


def _snapshot_output_root_mutation(event: str, args: tuple) -> bool:
    if event not in {"os.remove", "os.unlink", "os.rmdir", "os.chmod", "os.chown", "os.utime", "os.truncate"}:
        return False
    return any(_lexical_path(value) == _SNAPSHOT_OUTPUTS for value in args[:2])


def _is_workspace_write(path: str | None) -> bool:
    if not _within(path, _WORKSPACE):
        return False
    if _within(path, _EVIDENCE) and not _is_snapshot_write(path):
        return False
    if _is_snapshot_output(path):
        return False
    return True


def _write_denied(path: str | None, requested_path=None, event: str = "", event_args: tuple = ()) -> bool:
    return (
        bool(path in _HARNESS_FILES)
        or _is_snapshot_write(path)
        or _is_workspace_write(path)
        or _is_snapshot_output_escape(requested_path if requested_path is not None else path)
        or _snapshot_output_link_creation(event, event_args)
        or _snapshot_output_rename_escape(event, event_args)
        or _snapshot_output_root_mutation(event, event_args)
    )


def _offline_state() -> dict:
    present = "B2B_TEST_OFFLINE" in os.environ
    return {
        "b2b_test_offline_present": present,
        "b2b_test_offline_value": os.environ.get("B2B_TEST_OFFLINE") if present else None,
    }


def _guard_event(kind: str, **values) -> dict:
    global _EVENT_SEQUENCE
    _EVENT_SEQUENCE += 1
    event_sequence = _EVENT_SEQUENCE
    pid = os.getpid()
    return {
        "kind": kind,
        "pid": pid,
        "parent_pid": os.getppid(),
        "phase": os.environ.get("PETZOO_PHASE", "unknown"),
        "nodeid": os.environ.get("PETZOO_ACTIVE_NODEID", ""),
        "probe_id": os.environ.get("PETZOO_GUARD_PROBE", ""),
        "guard_event_sequence": event_sequence,
        "guard_event_id": f"{pid}:{event_sequence}:{time.monotonic_ns()}",
        "timestamp_utc": _now(),
        **_offline_state(),
        **values,
    }


def _is_write_open(mode, flags) -> bool:
    if isinstance(mode, str) and any(letter in mode for letter in "wax+"):
        return True
    if isinstance(flags, int):
        mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & mask)
    return False


def _audit(event: str, args: tuple) -> None:
    if _REENTRY:
        return
    if event == "open" and args:
        path = _path(args[0])
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if _is_write_open(mode, flags) and _write_denied(path, args[0], event=event, event_args=args):
            _append(os.environ.get("PETZOO_FILESYSTEM_GUARD_LOG", ""), _guard_event(
                "workspace_write_denied", event=event, path=path, mode=str(mode), flags=flags,
            ))
            raise PermissionError(f"PETZOO isolated-run write denied: {path}")
        if path and _within(path, _TESTS) and not _is_write_open(mode, flags):
            _append(os.environ.get("PETZOO_FILESYSTEM_GUARD_LOG", ""), _guard_event(
                "snapshot_test_file_access", event=event,
                path=os.path.relpath(path, _SNAPSHOT).replace(os.sep, "/"),
            ))
    elif event in {
        "os.remove", "os.unlink", "os.rmdir", "os.mkdir", "os.chmod", "os.chown",
        "os.utime", "os.truncate", "os.link", "os.symlink", "os.rename",
        "shutil.copyfile", "shutil.copystat", "sqlite3.connect",
    }:
        candidates = []
        for value in args[:2]:
            path = _path(value)
            if path:
                candidates.append((path, value))
        denied = next(((path, value) for path, value in candidates if _write_denied(path, value, event=event, event_args=args)), None)
        if denied:
            denied_path, _requested_path = denied
            _append(os.environ.get("PETZOO_FILESYSTEM_GUARD_LOG", ""), _guard_event(
                "workspace_write_denied", event=event, path=denied_path,
            ))
            raise PermissionError(f"PETZOO isolated-run mutation denied: {denied_path}")
    if _network_guard and event in _DENIED_NETWORK_EVENTS:
        _emit_network_event(_guard_event(
            "blocked_network", event=event,
        ))
        raise RuntimeError(f"Network access disabled by PETZOO_NETWORK_GUARD: {event}")


_fs_guard = os.environ.get("PETZOO_FILESYSTEM_GUARD") == "1"
_network_guard_requested = os.environ.get("PETZOO_NETWORK_GUARD") == "1"
_network_guard = _network_guard_requested and os.environ.get("PETZOO_CHILD_PROCESS") == "1"
if _fs_guard or _network_guard:
    if _SNAPSHOT_OUTPUTS and not _within(_SNAPSHOT_OUTPUTS, _SNAPSHOT):
        raise RuntimeError("PETZOO_SNAPSHOT_OUTPUTS must resolve inside the isolated source snapshot")
    sys.addaudithook(_audit)
    if _fs_guard:
        _append(os.environ.get("PETZOO_FILESYSTEM_GUARD_LOG", ""), _guard_event(
            "filesystem_guard_hook_installed", guard="deny_workspace_writes_and_snapshot_writes",
        ))
    if _network_guard:
        _emit_network_event(_guard_event(
            "network_guard_hook_installed", guard="deny_socket_audit_events",
        ))
    _emit_network_event(_guard_event(
        "python_process_guard_armed", network_guard_requested=_network_guard_requested,
        network_guard=_network_guard, filesystem_guard=_fs_guard,
    ))
