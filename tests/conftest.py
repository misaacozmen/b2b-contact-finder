"""Pytest configuration and network isolation audit hook."""

from __future__ import annotations

import os
import gc
import hashlib
import json
import re
import sys
import tempfile
import time
import threading
from pathlib import Path

import pytest

import config as app_config
config = app_config
_TIMING_LOG = None
_REPORT_LOCK = threading.Lock()
_REPORT_ORDINAL = 0
_TRACE_LOCK = threading.Lock()
_ACTIVE_TRACE: list[str] | None = None
_ACTIVE_TRACE_SET: set[str] | None = None
_ACTIVE_TRACE_TARGETS: set[str] = set()
_ACTIVE_NODEID = ""
_TRACE_REPO_PREFIX = str(Path(__file__).resolve().parents[1]).replace("\\", "/").casefold().rstrip("/") + "/"
_TRACE_ENTRY_LIMIT = 1024
_K6_RECEIPT_SCHEMA_VERSION = 2


def pytest_runtest_logreport(report):
    global _REPORT_ORDINAL
    target = os.environ.get("B2B_PYTEST_REPORT_JSONL", "").strip()
    if not target:
        return
    skip_reason = ""
    if report.skipped:
        skip_reason = str(report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr)
    with _REPORT_LOCK:
        _REPORT_ORDINAL += 1
        ordinal = _REPORT_ORDINAL
    payload = {
        "command_id": os.environ.get("B2B_COMMAND_ID", ""),
        "report_ordinal": ordinal,
        "nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
        "skip_reason": skip_reason, "duration": float(report.duration),
        "wasxfail": str(getattr(report, "wasxfail", "") or ""),
        "subtest": str(getattr(report, "context", "") or ""),
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _REPORT_LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _k6_artifact_manifest(root: Path) -> list[dict]:
    manifest = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort(); files.sort()
        current_path = Path(current)
        for name in files:
            if name.endswith(("-wal", "-shm")):
                continue
            path = current_path / name
            data = path.read_bytes()
            stat = path.stat()
            manifest.append({
                "path": path.relative_to(root).as_posix(),
                "absolute_path": str(path.resolve()),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mtime_ns": int(stat.st_mtime_ns),
            })
    return manifest


def _k6_recorder_journal() -> list:
    from modules import runtime

    journal = getattr(getattr(runtime, "_PAID_TRANSPORT", None), "journal", [])
    return json.loads(json.dumps(list(journal), ensure_ascii=False, default=str))


def _k6_network_records(path: str, nodeid: str) -> list[dict]:
    if not path or not Path(path).is_file():
        return []
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("nodeid") == nodeid:
            records.append(row)
    return records


def _k6_snapshot(root: Path, network_path: str, nodeid: str) -> dict:
    artifacts = _k6_artifact_manifest(root)
    recorder = _k6_recorder_journal()
    network_records = _k6_network_records(network_path, nodeid)
    network_material = _canonical_json(network_records).encode("utf-8")
    return {
        "artifact_root": str(root.resolve()),
        "artifact_manifest": artifacts,
        "artifact_sha256": hashlib.sha256(_canonical_json(artifacts).encode("utf-8")).hexdigest(),
        "recorder_journal": recorder,
        "recorder_count": len(recorder),
        "recorder_sha256": hashlib.sha256(_canonical_json(recorder).encode("utf-8")).hexdigest(),
        "network_receipt_path": str(Path(network_path).resolve()) if network_path else "",
        "network_receipt_count": len(network_records),
        "network_receipt_sha256": hashlib.sha256(network_material).hexdigest(),
        "socket_blocked": sum(row.get("kind") == "blocked_network" for row in network_records),
    }


def _k6_exception_classes(expected) -> list[str]:
    values = expected if isinstance(expected, tuple) else (expected,)
    return [f"{value.__module__}.{value.__qualname__}" for value in values]


def _k6_normalize_reason(value: BaseException, root: Path) -> str:
    reason = str(value).strip() or type(value).__name__
    for token in {str(root), str(root).replace("\\", "/")}:
        if token:
            reason = reason.replace(token, "<DISPOSABLE_ROOT>")
    return re.sub(r"0x[0-9a-fA-F]+", "0x<ADDR>", reason)[:500]


class _K6RaisesProxy:
    def __init__(self, inner, context: dict, expected: dict):
        self._inner = inner
        self._context = context
        self._expected = expected

    def __enter__(self):
        return self._inner.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        handled = self._inner.__exit__(exc_type, exc_value, traceback)
        if handled and exc_type is not None and exc_value is not None:
            tail = traceback
            while tail and tail.tb_next:
                tail = tail.tb_next
            self._context["actual_rejections"].append({
                "class": f"{exc_type.__module__}.{exc_type.__qualname__}",
                "actual_mro": [f"{base.__module__}.{base.__qualname__}" for base in exc_type.__mro__],
                "reason": _k6_normalize_reason(exc_value, self._context["root"]),
                "source": "" if tail is None else f"{Path(tail.tb_frame.f_code.co_filename).name}:{tail.tb_frame.f_code.co_name}:{tail.tb_lineno}",
                "expected_classes": list(self._expected["classes"]),
                "expected_reason_pattern": self._expected["reason_pattern"],
            })
        return handled


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    context = getattr(item, "_k6_receipt_context", None)
    if context is None:
        return
    if report.when == "call":
        context["call_report_outcome"] = report.outcome
        context["call_wasxfail"] = str(getattr(report, "wasxfail", "") or "")
        return
    if report.when != "teardown" or "call_report_outcome" not in context:
        return
    gc.collect()
    after = _k6_snapshot(context["root"], context["network_path"], item.nodeid)
    before = context["before"]
    mutation_evidence = {
        "artifact_changed": before["artifact_sha256"] != after["artifact_sha256"],
        "recorder_changed": before["recorder_sha256"] != after["recorder_sha256"],
        "socket_blocked_delta": int(after["socket_blocked"]) - int(before["socket_blocked"]),
        "matched_rejections": len(context["actual_rejections"]),
        "cli_exit_observed": bool(context["cli_exits"]),
    }
    mutation_observed = bool(
        mutation_evidence["artifact_changed"] or mutation_evidence["recorder_changed"]
        or mutation_evidence["socket_blocked_delta"] > 0 or mutation_evidence["matched_rejections"]
        or mutation_evidence["cli_exit_observed"]
    )
    if context["actual_rejections"]:
        stable_reason = ";".join(f"{row['class']}:{row['reason']}" for row in context["actual_rejections"])
    elif context["cli_exits"]:
        stable_reason = "cli_exit:" + ",".join(map(str, context["cli_exits"]))
    elif mutation_evidence["socket_blocked_delta"] > 0:
        stable_reason = f"socket_guard_blocked:{mutation_evidence['socket_blocked_delta']}"
    elif mutation_evidence["recorder_changed"]:
        stable_reason = "recorder_journal_changed"
    elif mutation_evidence["artifact_changed"]:
        stable_reason = "artifact_state_changed"
    else:
        stable_reason = "no_mutation_evidence"
    expected_exception_class = ["|".join(row["classes"]) for row in context["expected_rejections"]] or ["NONE"]
    actual_exception_class = [row["class"] for row in context["actual_rejections"]] or ["NONE"]
    expected = {
        "pytest_outcome": "passed", "mutation_observed": True,
        "exception_class": expected_exception_class,
        "reason_pattern": [row["reason_pattern"] for row in context["expected_rejections"]],
        "cli_exit": context["expected_cli_exits"] or None,
    }
    actual = {
        "pytest_outcome": context["call_report_outcome"], "mutation_observed": mutation_observed,
        "exception_class": actual_exception_class,
        "reason": [row["reason"] for row in context["actual_rejections"]],
        "cli_exit": context["cli_exits"] or None,
    }
    callspec = getattr(item, "callspec", None)
    parameters = {} if callspec is None else json.loads(json.dumps(callspec.params, ensure_ascii=False, default=str))
    command_id = os.environ.get("B2B_COMMAND_ID", "")
    case_id = hashlib.sha256(item.nodeid.encode("utf-8")).hexdigest()
    receipt = {
        "schema_version": _K6_RECEIPT_SCHEMA_VERSION,
        "command_id": command_id,
        "nodeid": item.nodeid,
        "mutation_id": item.nodeid.split("[")[-1].rstrip("]") if "[" in item.nodeid else item.nodeid.split("::")[-1],
        "case_id": case_id,
        "parameters": parameters,
        "disposable_root": str(context["root"]),
        "before": before,
        "after": after,
        "expected": expected,
        "actual": actual,
        "rejections": context["actual_rejections"],
        "stable_reason": stable_reason,
        "mutation_evidence": mutation_evidence,
        "report_outcome": context["call_report_outcome"],
        "wasxfail": context["call_wasxfail"],
        "evidence_path": f"k6_receipts/{case_id}.json",
    }
    target = Path(context["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with _REPORT_LOCK, target.open("a", encoding="utf-8") as handle:
        handle.write(_canonical_json(receipt) + "\n")
    case_path = target.parent / receipt["evidence_path"]
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(_canonical_json(receipt), encoding="utf-8")


def _production_profiler(frame, event, _arg):
    if _ACTIVE_TRACE is None or _ACTIVE_TRACE_SET is None:
        sys.setprofile(None)
        return None
    if event != "call":
        return _production_profiler
    if _ACTIVE_TRACE_TARGETS and _ACTIVE_TRACE_TARGETS.intersection(_ACTIVE_TRACE_SET):
        sys.setprofile(None)
        return None
    if len(_ACTIVE_TRACE) >= _TRACE_ENTRY_LIMIT:
        sys.setprofile(None)
        return None
    filename = str(frame.f_code.co_filename).replace("\\", "/")
    folded = filename.casefold()
    if not folded.startswith(_TRACE_REPO_PREFIX):
        return _production_profiler
    relative = filename[len(_TRACE_REPO_PREFIX):]
    if relative == "main.py" or relative.startswith("modules/") or relative in {"tools/closure_audit_runner.py", "tests/conftest.py"}:
        entry = f"{relative}:{frame.f_code.co_name}"
        if entry not in _ACTIVE_TRACE_SET:
            with _TRACE_LOCK:
                if entry not in _ACTIVE_TRACE_SET:
                    _ACTIVE_TRACE_SET.add(entry); _ACTIVE_TRACE.append(entry)
                    if entry in _ACTIVE_TRACE_TARGETS:
                        sys.setprofile(None); threading.setprofile(None)
                        return None
                    if len(_ACTIVE_TRACE) >= _TRACE_ENTRY_LIMIT:
                        threading.setprofile(None)
    return _production_profiler


def _start_production_trace(item) -> None:
    global _ACTIVE_TRACE, _ACTIVE_TRACE_SET, _ACTIVE_TRACE_TARGETS, _ACTIVE_NODEID
    selected = {value for value in os.environ.get("B2B_PRODUCTION_TRACE_NODES", "").split(",") if value}
    node_base = item.nodeid.split("::")[-1].split("[")[0]
    if os.environ.get("B2B_PRODUCTION_TRACE_JSONL", "").strip() and (not selected or node_base in selected):
        try:
            target_map = json.loads(os.environ.get("B2B_PRODUCTION_TRACE_TARGETS", "{}"))
        except (TypeError, ValueError):
            target_map = {}
        _ACTIVE_TRACE_TARGETS = set(map(str, target_map.get(node_base, ())))
        _ACTIVE_TRACE, _ACTIVE_TRACE_SET, _ACTIVE_NODEID = [], set(), item.nodeid
        sys.setprofile(_production_profiler)
        threading.setprofile(_production_profiler)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    global _ACTIVE_NODEID
    _ACTIVE_NODEID = item.nodeid
    node_base = item.nodeid.split("::")[-1].split("[")[0]
    late = {value for value in os.environ.get("B2B_PRODUCTION_TRACE_LATE_NODES", "").split(",") if value}
    if node_base not in late:
        _start_production_trace(item)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    global _ACTIVE_NODEID
    _ACTIVE_NODEID = item.nodeid
    node_base = item.nodeid.split("::")[-1].split("[")[0]
    late = {value for value in os.environ.get("B2B_PRODUCTION_TRACE_LATE_NODES", "").split(",") if value}
    if node_base not in late and _ACTIVE_TRACE is None:
        _start_production_trace(item)


def pytest_runtest_teardown(item, nextitem):
    global _ACTIVE_TRACE, _ACTIVE_TRACE_SET, _ACTIVE_TRACE_TARGETS, _ACTIVE_NODEID
    target = os.environ.get("B2B_PRODUCTION_TRACE_JSONL", "").strip()
    if not target or _ACTIVE_TRACE is None:
        _ACTIVE_NODEID = ""
        return
    sys.setprofile(None); threading.setprofile(None)
    with _TRACE_LOCK:
        entries = list(_ACTIVE_TRACE); _ACTIVE_TRACE = None; _ACTIVE_TRACE_SET = None
    path = Path(target); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"command_id": os.environ.get("B2B_COMMAND_ID", ""), "nodeid": _ACTIVE_NODEID or item.nodeid, "function_entries": entries}, ensure_ascii=False, sort_keys=True) + "\n")
    _ACTIVE_NODEID = ""; _ACTIVE_TRACE_TARGETS = set()


@pytest.fixture
def production_trace_call(request):
    """Start audit profiling immediately before a late production boundary."""
    def invoke(function, *args, **kwargs):
        if _ACTIVE_TRACE is None:
            _start_production_trace(request.node)
        return function(*args, **kwargs)
    return invoke


def _log_timing(event: str, **values) -> None:
    if _TIMING_LOG is None:
        return
    details = ";".join(f"{key}={value}" for key, value in values.items())
    with _TIMING_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{event};{details}\n")


def pytest_configure(config):
    """Create all synthetic fixtures below one disposable session root."""
    global _TIMING_LOG
    network_receipt = os.environ.get("B2B_SOCKET_DENY_JSONL", "").strip()
    if network_receipt:
        network_path = Path(network_receipt)
        network_path.parent.mkdir(parents=True, exist_ok=True)
        with network_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "guard_armed", "command_id": os.environ.get("B2B_COMMAND_ID", ""), "pid": os.getpid()}, ensure_ascii=False, sort_keys=True) + "\n")
    _TIMING_LOG = Path(
        os.environ.get(
            "B2B_TEST_TIMING_LOG",
            str(Path(tempfile.gettempdir()) / "b2b_test_timing.log"),
        )
    )
    _TIMING_LOG.parent.mkdir(parents=True, exist_ok=True)
    fixture_root = Path(tempfile.mkdtemp(prefix="b2b_test_fixtures_"))
    os.environ["B2B_TEST_FIXTURE_ROOT"] = str(fixture_root)
    runtime_root = Path(tempfile.mkdtemp(prefix="b2b_test_runtime_"))
    output_dir = runtime_root / "output"
    state_dir = runtime_root / "state"
    data_dir = runtime_root / "data"
    input_dir = runtime_root / "input"
    for path in (output_dir, state_dir, data_dir, input_dir):
        path.mkdir(parents=True, exist_ok=True)
    app_config.INPUT_DIR = input_dir
    app_config.OUTPUT_DIR = output_dir
    app_config.STATE_DIR = state_dir
    app_config.DATA_DIR = data_dir
    app_config.RUNS_DIR = runtime_root / "runs"
    app_config.CONTACTS_FILE = output_dir / "contacts.xlsx"
    app_config.ALL_RESULTS_FILE = output_dir / "all_results.xlsx"
    app_config.VERIFIED_CONTACTS_FILE = output_dir / "verified_contacts.xlsx"
    app_config.REVIEW_QUEUE_FILE = output_dir / "review_queue.xlsx"
    app_config.FAILED_FILE = output_dir / "failed.xlsx"
    app_config.CANDIDATES_FILE = output_dir / "website_candidates.xlsx"
    app_config.REPORT_FILE = output_dir / "report.txt"
    app_config.LOG_FILE = output_dir / "logs.txt"
    app_config.EVIDENCE_FILE = output_dir / "evidence.jsonl"
    app_config.ENTITY_RELATIONSHIPS_FILE = output_dir / "entity_relationships.jsonl"
    app_config.TELEMETRY_FILE = output_dir / "telemetry.json"
    app_config.DISCOVERY_COVERAGE_FILE = output_dir / "discovery_coverage.json"
    app_config.QUALITY_AUDIT_FILE = output_dir / "quality_audit.json"
    app_config.REPLAY_SNAPSHOT_FILE = output_dir / "replay_snapshot.json.gz"
    app_config.MANIFEST_FILE = output_dir / "manifest.json"
    app_config.PROGRESS_FILE = state_dir / "progress.json"
    app_config.PROGRESS_DB_FILE = state_dir / "progress.sqlite3"
    app_config.SEARCH_CACHE_DIR = state_dir / "search_cache"
    app_config.CRAWL_CACHE_DIR = state_dir / "crawl_cache"
    app_config.EMAIL_CACHE_DIR = state_dir / "email_cache"
    app_config.SAVED_API_KEYS_FILE = state_dir / "api_keys.json"
    app_config.RESOLVER_SETTINGS_FILE = state_dir / "company_resolvers.json"
    app_config.VERIFIED_ENTITY_MEMORY_FILE = data_dir / "verified_entity_memory.jsonl"
    app_config.COMPANY_ALIASES_FILE = data_dir / "company_aliases.json"
    app_config.ENTITY_REGISTRY_FILE = data_dir / "entity_registry.json"
    app_config.OFFICIAL_REGISTRY_FILE = data_dir / "official_registry.json"
    sys.path.insert(0, str(Path(__file__).resolve().parent))

_OUTBOUND_NETWORK_EVENTS = {
    "socket.getaddrinfo", "socket.gethostbyaddr", "socket.gethostbyname", "socket.gethostbyname_ex",
    "socket.getnameinfo", "socket.connect", "socket.connect_ex", "socket.sendto",
    "urllib.Request", "http.client.connect",
}


def _network_audit_hook(event: str, args: tuple) -> None:
    if event in _OUTBOUND_NETWORK_EVENTS:
        receipt = os.environ.get("B2B_SOCKET_DENY_JSONL", "").strip()
        if receipt:
            with Path(receipt).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"kind": "blocked_network", "command_id": os.environ.get("B2B_COMMAND_ID", ""), "nodeid": _ACTIVE_NODEID, "event": event, "args": [str(value)[:200] for value in args]}, ensure_ascii=False, sort_keys=True) + "\n")
        raise RuntimeError(f"Network access disabled during tests: {event}")


sys.addaudithook(_network_audit_hook)


_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_PROTECTED_TEST_ROOTS = tuple(
    (_REPO / name).resolve() for name in ("input", "state", "data", "runs")
)
_PROTECTED_TEST_ROOTS = _DEFAULT_PROTECTED_TEST_ROOTS


def _is_reaudit_evidence(path: Path) -> bool:
    configured = os.environ.get("B2B_REAUDIT_EVIDENCE_DIR", "").strip()
    if not configured:
        return False
    evidence = Path(configured).resolve()
    return path == evidence or evidence in path.parents


def _write_path_from_audit(args: tuple):
    if not args:
        return None
    raw = args[0]
    if isinstance(raw, int):
        return None
    if len(args) > 1 and isinstance(args[1], str):
        mode = args[1]
        if not any(flag in mode for flag in ("w", "a", "x", "+")):
            return None
    try:
        path = Path(os.fspath(raw)).resolve()
    except (TypeError, ValueError, OSError):
        return None
    return path


def _test_write_audit_hook(event: str, args: tuple) -> None:
    if event in {"open", "os.open", "sqlite3.connect"}:
        if event == "sqlite3.connect":
            raw = args[0] if args else None
            try:
                path = Path(os.fspath(raw)).resolve()
            except (TypeError, ValueError, OSError):
                path = None
            if path and not _is_reaudit_evidence(path) and any(path == root or root in path.parents for root in _PROTECTED_TEST_ROOTS):
                raise AssertionError(f"test attempted to connect production SQLite: {path}")
            return
        path = _write_path_from_audit(args)
        if event == "os.open" and args:
            try:
                flags = int(args[1])
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    path = Path(os.fspath(args[0])).resolve()
            except (TypeError, ValueError, OSError, IndexError):
                pass
        if path and not _is_reaudit_evidence(path) and any(path == root or root in path.parents for root in _PROTECTED_TEST_ROOTS):
            raise AssertionError(f"test attempted to write production path: {path}")
    if event in {"os.rename", "os.replace", "os.remove", "os.unlink", "os.mkdir", "os.makedirs", "os.rmdir"}:
        for raw in args[:2]:
            try:
                path = Path(os.fspath(raw)).resolve()
            except (TypeError, ValueError, OSError):
                continue
            if not _is_reaudit_evidence(path) and any(path == root or root in path.parents for root in _PROTECTED_TEST_ROOTS):
                raise AssertionError(f"test attempted filesystem mutation: {event} {path}")


sys.addaudithook(_test_write_audit_hook)


def _protected_manifest() -> dict[str, str]:
    manifest = {}
    if _PROTECTED_TEST_ROOTS == _DEFAULT_PROTECTED_TEST_ROOTS:
        for root in _PROTECTED_TEST_ROOTS:
            if not root.exists():
                continue
            for path in sorted(root.iterdir()):
                stat = path.lstat()
                manifest[str(path.relative_to(_REPO))] = f"guard:mode={stat.st_mode}:bytes={stat.st_size}:mtime_ns={stat.st_mtime_ns}:dev={stat.st_dev}:ino={stat.st_ino}"
        return manifest
    def reparse_metadata(path: Path, stat) -> str:
        attributes = int(getattr(stat, "st_file_attributes", 0))
        tag = int(getattr(stat, "st_reparse_tag", 0))
        try:
            target = os.path.normcase(os.path.normpath(os.readlink(path)))
            target_field = f"target:{target}"
        except (OSError, NotImplementedError):
            target_field = "target_unavailable"
        return f"reparse:attributes={attributes}:tag={tag}:dev={stat.st_dev}:ino={stat.st_ino}:{target_field}"
    for root in _PROTECTED_TEST_ROOTS:
        if not root.exists():
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            retained = []
            for name in sorted(directories):
                path = current_path / name
                if _is_reaudit_evidence(path):
                    continue
                stat = path.lstat()
                is_reparse = bool(getattr(stat, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                if path.is_symlink() or is_reparse:
                    manifest[str(path.relative_to(_REPO))] = reparse_metadata(path, stat)
                else:
                    retained.append(name)
            directories[:] = retained
            for name in sorted(files):
                path = current_path / name
                if _is_reaudit_evidence(path):
                    continue
                stat = path.lstat()
                is_reparse = bool(getattr(stat, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                if path.is_symlink() or is_reparse:
                    manifest[str(path.relative_to(_REPO))] = reparse_metadata(path, stat)
                    continue
                if stat.st_size <= 16 * 1024 * 1024:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    manifest[str(path.relative_to(_REPO))] = f"sha256:{digest}"
                else:
                    manifest[str(path.relative_to(_REPO))] = (
                        f"large:bytes={stat.st_size}:mtime_ns={stat.st_mtime_ns}:dev={stat.st_dev}:ino={stat.st_ino}"
                    )
    return manifest


@pytest.fixture(scope="session", autouse=True)
def protected_production_paths_unchanged():
    """Hash protected production trees once around the complete test session."""
    started = time.perf_counter()
    _log_timing("session_hash_start.begin", monotonic=f"{started:.6f}")
    before_manifest = _protected_manifest()
    shared_reference = os.environ.get("B2B_PROTECTED_MANIFEST_REFERENCE", "").strip()
    if shared_reference:
        reference_path = Path(shared_reference)
        if reference_path.is_file():
            assert json.loads(reference_path.read_text(encoding="utf-8")) == before_manifest, "protected production reference hash mismatch"
        else:
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            reference_path.write_text(json.dumps(before_manifest, sort_keys=True), encoding="utf-8")
    _log_timing(
        "session_hash_start.end",
        monotonic=f"{time.perf_counter():.6f}",
        elapsed=f"{time.perf_counter() - started:.3f}",
        files=len(before_manifest),
    )
    try:
        yield
    finally:
        if not shared_reference:
            started = time.perf_counter()
            _log_timing("session_hash_end.begin", monotonic=f"{started:.6f}")
            after_manifest = _protected_manifest()
            _log_timing(
                "session_hash_end.end",
                monotonic=f"{time.perf_counter():.6f}",
                elapsed=f"{time.perf_counter() - started:.3f}",
                files=len(after_manifest),
            )
            assert after_manifest == before_manifest, "tests modified protected production paths"


def _reset_runtime_globals() -> None:
    from modules import discovery_coverage, google_places, linkedin_company, llm_arbiter, replay_snapshot, runtime, search

    runtime.reset()
    discovery_coverage.reset()
    replay_snapshot.reset()
    linkedin_company.reset()
    google_places.reset()
    llm_arbiter.reset()
    search.reset_run_state()
    search.reset_candidate_host_observations()


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path):
    """Keep every test's files, config and mutable module state disposable."""
    original_config = {
        name: getattr(config, name)
        for name in dir(config)
        if name.isupper()
    }
    _reset_runtime_globals()
    try:
        yield
    finally:
        _reset_runtime_globals()
        for name, value in original_config.items():
            setattr(config, name, value)


@pytest.fixture(autouse=True)
def k6_case_receipt_context(request, tmp_path, isolated_runtime, monkeypatch):
    """Capture independently replayable evidence for every selected K6 case."""
    target = os.environ.get("B2B_K6_RECEIPT_JSONL", "").strip()
    if target:
        network_path = os.environ.get("B2B_SOCKET_DENY_JSONL", "").strip()
        node_base = request.node.nodeid.split("::")[-1].split("[")[0]
        try:
            expected_cli_map = json.loads(os.environ.get("B2B_K6_EXPECTED_CLI_EXITS", "{}"))
        except (TypeError, ValueError):
            expected_cli_map = {}
        context = {
            "target": target,
            "root": tmp_path,
            "network_path": network_path,
            "before": _k6_snapshot(tmp_path, network_path, request.node.nodeid),
            "expected_rejections": [],
            "actual_rejections": [],
            "expected_cli_exits": list(expected_cli_map.get(node_base, ())),
            "cli_exits": [],
        }
        request.node._k6_receipt_context = context
        original_raises = pytest.raises

        def tracked_raises(expected_exception, *args, **kwargs):
            pattern = kwargs.get("match", "")
            pattern = str(getattr(pattern, "pattern", pattern) or "")
            expected = {"classes": _k6_exception_classes(expected_exception), "reason_pattern": pattern}
            context["expected_rejections"].append(expected)
            if args:
                info = original_raises(expected_exception, *args, **kwargs)
                value = info.value
                context["actual_rejections"].append({
                    "class": f"{info.type.__module__}.{info.type.__qualname__}",
                    "actual_mro": [f"{base.__module__}.{base.__qualname__}" for base in info.type.__mro__],
                    "reason": _k6_normalize_reason(value, tmp_path),
                    "source": "functional_pytest_raises",
                    "expected_classes": list(expected["classes"]),
                    "expected_reason_pattern": pattern,
                })
                return info
            return _K6RaisesProxy(original_raises(expected_exception, **kwargs), context, expected)

        monkeypatch.setattr(pytest, "raises", tracked_raises)
        import main as app_main
        original_cli = app_main.cli

        def tracked_cli(*args, **kwargs):
            result = original_cli(*args, **kwargs)
            context["cli_exits"].append(int(result))
            return result

        monkeypatch.setattr(app_main, "cli", tracked_cli)
    yield
