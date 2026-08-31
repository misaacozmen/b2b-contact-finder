"""Pytest configuration and network isolation audit hook."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

import config as app_config
config = app_config
_TIMING_LOG = None


def _log_timing(event: str, **values) -> None:
    if _TIMING_LOG is None:
        return
    details = ";".join(f"{key}={value}" for key, value in values.items())
    with _TIMING_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{event};{details}\n")


def pytest_configure(config):
    """Create all synthetic fixtures below one disposable session root."""
    global _TIMING_LOG
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

def _network_audit_hook(event: str, args: tuple) -> None:
    if event.startswith("socket.") or event in {"urllib.Request", "http.client.connect"}:
        raise RuntimeError(f"Network access disabled during tests: {event}")


sys.addaudithook(_network_audit_hook)


_REPO = Path(__file__).resolve().parents[1]
_PROTECTED_TEST_ROOTS = tuple(
    (_REPO / name).resolve() for name in ("input", "output", "state", "data", "runs")
)


def _write_path_from_audit(args: tuple):
    if not args:
        return None
    raw = args[0]
    if isinstance(raw, int):
        return None
    try:
        path = Path(os.fspath(raw)).resolve()
    except (TypeError, ValueError, OSError):
        return None
    if len(args) > 1 and isinstance(args[1], str):
        mode = args[1]
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            return path
    return None


def _test_write_audit_hook(event: str, args: tuple) -> None:
    if event in {"open", "os.open", "sqlite3.connect"}:
        if event == "sqlite3.connect":
            raw = args[0] if args else None
            try:
                path = Path(os.fspath(raw)).resolve()
            except (TypeError, ValueError, OSError):
                path = None
            if path and any(path == root or root in path.parents for root in _PROTECTED_TEST_ROOTS):
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
        if path and any(path == root or root in path.parents for root in _PROTECTED_TEST_ROOTS):
            raise AssertionError(f"test attempted to write production path: {path}")
    if event in {"os.rename", "os.replace", "os.remove", "os.unlink", "os.mkdir", "os.makedirs", "os.rmdir"}:
        for raw in args[:2]:
            try:
                path = Path(os.fspath(raw)).resolve()
            except (TypeError, ValueError, OSError):
                continue
            if any(path == root or root in path.parents for root in _PROTECTED_TEST_ROOTS):
                raise AssertionError(f"test attempted filesystem mutation: {event} {path}")


sys.addaudithook(_test_write_audit_hook)


def _protected_manifest() -> dict[str, str]:
    manifest = {}
    for root in _PROTECTED_TEST_ROOTS:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            manifest[str(path.relative_to(_REPO))] = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    return manifest


@pytest.fixture(scope="session", autouse=True)
def protected_production_paths_unchanged():
    """Hash protected production trees once around the complete test session."""
    started = time.perf_counter()
    _log_timing("session_hash_start.begin", monotonic=f"{started:.6f}")
    before_manifest = _protected_manifest()
    _log_timing(
        "session_hash_start.end",
        monotonic=f"{time.perf_counter():.6f}",
        elapsed=f"{time.perf_counter() - started:.3f}",
        files=len(before_manifest),
    )
    try:
        yield
    finally:
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
    search.reset_source_health()
    search.reset_candidate_host_observations()
    search._RUN_PAID_QUERY_LIMIT = None


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
