from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import pytest


EVIDENCE = Path(os.environ["PETZOO_EVIDENCE_DIR"]).resolve()
PHASE = os.environ.get("PETZOO_PHASE", "unknown")
DELIVERY_MODULE = Path(os.environ["PETZOO_DELIVERY_MODULE"]).resolve()
WORKSPACE_ROOT = Path(os.environ["PETZOO_WORKSPACE_ROOT"]).resolve()
SOURCE_SNAPSHOT = Path(os.environ["PETZOO_SOURCE_SNAPSHOT"]).resolve()
TLDEXTRACT_CACHE = (EVIDENCE / "dependency_cache" / "tldextract").resolve()
DEFERRED = tuple(json.loads(os.environ.get("PETZOO_DEFERRED_NODEIDS", "[]")))
_EVENT_LOCK = threading.Lock()
_NODE_STARTS: dict[str, float] = {}
_ORIGINAL_POPEN = None
_DELIVERY_ROUTED = False
_BENCHMARK_NODEID = "tests/test_scale_and_validation_package.py::ScaleAndValidationPackageTests::test_benchmark_validator_cli_exit_codes"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8")
    with _EVENT_LOCK:
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _resolve_socket_deny_target(value: str, child_cwd: object, child_env: dict[str, str]) -> tuple[str, str, str]:
    cwd = Path(child_cwd) if child_cwd is not None else Path.cwd()
    if not cwd.is_absolute():
        cwd = Path.cwd() / cwd
    cwd = cwd.resolve()
    requested = value.strip()
    source = "explicit" if requested else "default"
    candidate = Path(requested) if requested else (EVIDENCE / "network_guard.jsonl")
    if not candidate.is_absolute():
        candidate = cwd / candidate
    target = candidate.resolve(strict=False)
    allowed_roots = {EVIDENCE}
    for name in ("TEMP", "TMP", "TMPDIR"):
        raw_root = child_env.get(name, "").strip()
        if raw_root:
            root = Path(raw_root)
            if not root.is_absolute():
                root = cwd / root
            resolved_root = root.resolve(strict=False)
            if resolved_root.is_relative_to(EVIDENCE):
                allowed_roots.add(resolved_root)
    protected = target.is_relative_to(SOURCE_SNAPSHOT) or target in {
        EVIDENCE / "run_offline_regression.py",
        EVIDENCE / "petzoo_offline_plugin.py",
        EVIDENCE / "sitecustomize.py",
    }
    if protected or not any(target.is_relative_to(root) for root in allowed_roots):
        raise RuntimeError(
            "refusing B2B_SOCKET_DENY_JSONL outside this task's evidence/temp roots: "
            f"requested={requested!r}, child_cwd={str(cwd)!r}, resolved={str(target)!r}"
        )
    return str(target), source, str(cwd)


def _route_delivery(items) -> None:
    global _DELIVERY_ROUTED
    if _DELIVERY_ROUTED:
        return
    matches = {}
    for item in items:
        module = getattr(item, "module", None)
        filename = getattr(module, "__file__", None) if module is not None else None
        if filename and Path(filename).resolve() == DELIVERY_MODULE:
            matches[id(module)] = module
    if not matches and PHASE in {"preflight", "long_h01", "long_k09"}:
        return
    if len(matches) != 1:
        raise pytest.UsageError(
            f"expected exactly one PETZOO acceptance module after collection; found {len(matches)}"
        )
    module = next(iter(matches.values()))
    old_path = str(Path(module.DELIVERY).resolve())
    module.DELIVERY = EVIDENCE
    _DELIVERY_ROUTED = True
    _append_jsonl(EVIDENCE / "delivery_route.jsonl", {
        "event": "post_collection_delivery_route",
        "phase": PHASE,
        "module": str(DELIVERY_MODULE),
        "from": old_path,
        "to": str(EVIDENCE),
        "timestamp_utc": _utc(),
    })


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    _route_delivery(items)


def pytest_collection_finish(session):
    nodeids = [item.nodeid for item in session.items]
    if PHASE == "preflight":
        return
    if not _DELIVERY_ROUTED and PHASE not in {"long_h01", "long_k09"}:
        raise pytest.UsageError("PETZOO DELIVERY output routing was not applied after collection")
    if PHASE == "collect":
        payload = {
            "phase": PHASE,
            "count": len(nodeids),
            "nodeids": nodeids,
            "timestamp_utc": _utc(),
        }
        (EVIDENCE / "collection_nodes.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return
    if PHASE == "run":
        full_path = EVIDENCE / "collection_nodes.json"
        full = json.loads(full_path.read_text(encoding="utf-8"))["nodeids"]
        missing_deferred = [nodeid for nodeid in DEFERRED if full.count(nodeid) != 1]
        expected = [nodeid for nodeid in full if nodeid not in DEFERRED]
        if missing_deferred or nodeids != expected:
            raise pytest.UsageError(
                "run collection differs from full collection minus the three explicit deferred nodes; "
                f"deferred_identity_errors={missing_deferred}, selected={len(nodeids)}, expected={len(expected)}"
            )
        payload = {
            "phase": PHASE,
            "count": len(nodeids),
            "nodeids": nodeids,
            "deselected_nodeids": list(DEFERRED),
            "timestamp_utc": _utc(),
        }
        (EVIDENCE / "selected_nodes.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def pytest_configure(config):
    global _ORIGINAL_POPEN
    if _ORIGINAL_POPEN is not None:
        return
    _ORIGINAL_POPEN = subprocess.Popen

    def guarded_popen(*args, **kwargs):
        supplied = kwargs.get("env")
        supplied_env_snapshot = dict(supplied) if supplied is not None else None
        child_env = dict(os.environ if supplied is None else supplied)
        if not TLDEXTRACT_CACHE.is_relative_to(EVIDENCE) or TLDEXTRACT_CACHE.is_relative_to(SOURCE_SNAPSHOT):
            raise RuntimeError("TLDEXTRACT_CACHE must be inside evidence and outside source_snapshot")
        offline_present = "B2B_TEST_OFFLINE" in child_env
        offline_value = child_env.get("B2B_TEST_OFFLINE")
        requested_socket_deny_path = child_env.get("B2B_SOCKET_DENY_JSONL", "")
        effective_socket_deny_path, socket_deny_path_source, resolved_child_cwd = _resolve_socket_deny_target(
            requested_socket_deny_path, kwargs.get("cwd"), child_env,
        )
        child_env["TLDEXTRACT_CACHE"] = str(TLDEXTRACT_CACHE)
        child_env["PETZOO_NETWORK_GUARD"] = "1"
        child_env["PETZOO_FILESYSTEM_GUARD"] = "1"
        child_env["PETZOO_NETWORK_GUARD_LOG"] = str(EVIDENCE / "network_guard.jsonl")
        child_env["PETZOO_FILESYSTEM_GUARD_LOG"] = str(EVIDENCE / "filesystem_guard.jsonl")
        child_env["PETZOO_WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
        child_env["PETZOO_SOURCE_SNAPSHOT"] = str(SOURCE_SNAPSHOT)
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["B2B_SOCKET_DENY_JSONL"] = effective_socket_deny_path
        child_env["PETZOO_EVIDENCE_DIR"] = str(EVIDENCE)
        child_env["PETZOO_PHASE"] = PHASE
        child_env["PETZOO_CHILD_PROCESS"] = "1"
        child_env["PETZOO_ACTIVE_NODEID"] = os.environ.get("PETZOO_ACTIVE_NODEID", "")
        extra_path = str(EVIDENCE)
        existing_path = child_env.get("PYTHONPATH", "")
        path_parts = []
        for part in existing_path.split(os.pathsep):
            if not part:
                continue
            try:
                candidate = Path(part).resolve()
                candidate.relative_to(WORKSPACE_ROOT)
            except (OSError, ValueError):
                path_parts.append(part)
            else:
                if candidate == SOURCE_SNAPSHOT or candidate == EVIDENCE:
                    path_parts.append(part)
        if os.path.normcase(extra_path) not in {os.path.normcase(part) for part in path_parts}:
            child_env["PYTHONPATH"] = os.pathsep.join([extra_path, *path_parts])
        else:
            child_env["PYTHONPATH"] = os.pathsep.join(path_parts)
        kwargs["env"] = child_env
        child = _ORIGINAL_POPEN(*args, **kwargs)
        command = args[0] if args else kwargs.get("args", "")
        command0 = command[0] if isinstance(command, (list, tuple)) and command else command
        _append_jsonl(EVIDENCE / "child_processes.jsonl", {
            "event": "spawned_with_offline_guard",
            "pid": child.pid,
            "parent_pid": os.getpid(),
            "phase": PHASE,
            "nodeid": child_env.get("PETZOO_ACTIVE_NODEID", ""),
            "executable_basename": Path(str(command0)).name,
            "offline": offline_value,
            "b2b_test_offline_present": offline_present,
            "b2b_test_offline_value": offline_value,
            "command_id": child_env.get("B2B_COMMAND_ID", ""),
            "caller_env_unchanged": supplied is None or supplied == supplied_env_snapshot,
            "network_guard": child_env["PETZOO_NETWORK_GUARD"],
            "filesystem_guard": child_env["PETZOO_FILESYSTEM_GUARD"],
            "tldextract_cache_path": child_env["TLDEXTRACT_CACHE"],
            "socket_deny_path": child_env["B2B_SOCKET_DENY_JSONL"],
            "requested_socket_deny_path": requested_socket_deny_path,
            "socket_deny_path_source": socket_deny_path_source,
            "network_guard_log_path": child_env["PETZOO_NETWORK_GUARD_LOG"],
            "child_cwd": resolved_child_cwd,
            "sitecustomize_path_inherited": extra_path in child_env.get("PYTHONPATH", "").split(os.pathsep),
            "timestamp_utc": _utc(),
        })
        return child

    subprocess.Popen = guarded_popen


def pytest_sessionstart(session):
    _append_jsonl(EVIDENCE / "plugin_events.jsonl", {
        "event": "session_start",
        "phase": PHASE,
        "pid": os.getpid(),
        "timestamp_utc": _utc(),
        "monotonic": time.monotonic(),
        "pytest_version": pytest.__version__,
    })


def pytest_runtest_logstart(nodeid, location):
    started = time.monotonic()
    _NODE_STARTS[nodeid] = started
    os.environ["PETZOO_ACTIVE_NODEID"] = nodeid
    _append_jsonl(EVIDENCE / "node_events.jsonl", {
        "event": "node_start",
        "phase": PHASE,
        "nodeid": nodeid,
        "timestamp_utc": _utc(),
        "monotonic": started,
        "pid": os.getpid(),
        "location": location,
    })


def pytest_runtest_logreport(report):
    longrepr = ""
    if report.longrepr:
        longrepr = str(report.longrepr)
    started = _NODE_STARTS.get(report.nodeid)
    _append_jsonl(EVIDENCE / "node_events.jsonl", {
        "event": "phase_report",
        "pytest_phase": PHASE,
        "phase": report.when,
        "nodeid": report.nodeid,
        "outcome": report.outcome,
        "duration_seconds": float(report.duration),
        "elapsed_since_node_start": max(0.0, time.monotonic() - started) if started is not None else None,
        "wasxfail": str(getattr(report, "wasxfail", "") or ""),
        "longrepr": longrepr,
        "timestamp_utc": _utc(),
        "monotonic": time.monotonic(),
        "pid": os.getpid(),
    })


def pytest_runtest_logfinish(nodeid, location):
    started = _NODE_STARTS.pop(nodeid, None)
    finished = time.monotonic()
    os.environ.pop("PETZOO_ACTIVE_NODEID", None)
    _append_jsonl(EVIDENCE / "node_events.jsonl", {
        "event": "node_finish",
        "phase": PHASE,
        "nodeid": nodeid,
        "elapsed_seconds": max(0.0, finished - started) if started is not None else None,
        "timestamp_utc": _utc(),
        "monotonic": finished,
        "pid": os.getpid(),
        "location": location,
    })


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    if item.nodeid != _BENCHMARK_NODEID:
        yield
        return

    original_run = subprocess.run
    captured = []

    def capture_run(*args, **kwargs):
        result = original_run(*args, **kwargs)
        command = args[0] if args else kwargs.get("args", [])
        argv = [str(part) for part in command] if isinstance(command, (list, tuple)) else [str(command)]
        label = "private_missing" if "--private-seen-workbook" in argv else "public"

        def text_output(value):
            if value is None:
                return ""
            if isinstance(value, bytes):
                return value.decode(kwargs.get("encoding") or "utf-8", errors=kwargs.get("errors") or "replace")
            return str(value)

        stdout_name = f"benchmark_cli_{label}_stdout.txt"
        stderr_name = f"benchmark_cli_{label}_stderr.txt"
        (EVIDENCE / stdout_name).write_text(text_output(result.stdout), encoding="utf-8")
        (EVIDENCE / stderr_name).write_text(text_output(result.stderr), encoding="utf-8")
        record = {
            "label": label,
            "argv": argv,
            "cwd": str(Path(kwargs.get("cwd") or os.getcwd()).resolve()),
            "returncode": result.returncode,
            "stdout_file": stdout_name,
            "stderr_file": stderr_name,
            "stdout": text_output(result.stdout),
            "stderr": text_output(result.stderr),
        }
        captured.append(record)
        _append_jsonl(EVIDENCE / "benchmark_cli_results.jsonl", record)
        return result

    subprocess.run = capture_run
    try:
        yield
    finally:
        subprocess.run = original_run
        (EVIDENCE / "benchmark_cli_results.json").write_text(
            json.dumps(captured, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def pytest_sessionfinish(session, exitstatus):
    _append_jsonl(EVIDENCE / "plugin_events.jsonl", {
        "event": "session_finish",
        "phase": PHASE,
        "pid": os.getpid(),
        "exitstatus": int(exitstatus),
        "timestamp_utc": _utc(),
        "monotonic": time.monotonic(),
    })
