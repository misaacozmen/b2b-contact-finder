from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE = ROOT / "outputs" / "petzoo_offline_kapanis_20260925"
EVIDENCE = Path(os.environ.get("PETZOO_EVIDENCE_DIR", DEFAULT_EVIDENCE)).expanduser().resolve()
EVIDENCE_ROOT = EVIDENCE.parent
ALLOWED_OUTPUT_ROOT = (ROOT / "outputs").resolve()
DEADLINE_FILE = Path(os.environ.get("PETZOO_DEADLINE_FILE", "")).expanduser()
TASK_DEADLINE_QPC = 0
TASK_DEADLINE_FREQUENCY = 0
TASK_START_UTC = ""
TEST_ROOT = EVIDENCE / "source_snapshot"
SNAPSHOT_OUTPUTS = TEST_ROOT / "outputs"
PREVIOUS_EVIDENCE = Path(os.environ.get(
    "PETZOO_PREVIOUS_EVIDENCE", ROOT / "outputs" / "petzoo_offline_regresyon_bolum1_r2_20260925",
)).expanduser().resolve()
SCOPE_MANIFEST = Path(os.environ.get(
    "PETZOO_SCOPE_MANIFEST", ROOT / "outputs" / "petzoo_incident_fix_20260923_tek_teslim" / "source_manifest.json",
)).expanduser().resolve()
RUNTIME = Path(os.environ.get("PETZOO_PYTHON", sys.executable)).expanduser().resolve()
DEPENDENCY_CACHE_DIR = EVIDENCE / "dependency_cache" / "tldextract"
INITIAL_RUNTIME_SHA256 = "ac7cea155eead34c4d462492a34348f40f98d8212e27ed741be7fe27c4144b2c"
EXPECTED_RUNTIME_SHA256 = "d63e5f982d680fb12db2935efebafb4c860b4fa58694448a6fe5cc22ff9750e2"
DEFERRED = [
    "tests/test_mimari_reaudit_altinci_20260921.py::test_h01_default_pipeline_100_firms_three_workers_has_one_call_per_primary_query",
    "tests/test_mimari_reaudit_altinci_20260921.py::test_k09_budget_100_gives_each_firm_only_first_primary_right",
    "tests/test_petzoo_pipeline_acceptance.py::test_p11_real_137_company_pipeline_worker_1_and_3",
]
COMMIT_SCOPE_PATHS = [
    ".github/workflows/tests.yml",
    "config.py", "main.py",
    "modules/checkpoint.py", "modules/company_resolvers.py", "modules/contact_publication.py",
    "modules/crawler.py", "modules/discovery_coverage.py", "modules/discovery_rules.py",
    "modules/entity_resolution.py", "modules/evidence.py", "modules/excel.py", "modules/extractor.py",
    "modules/linkedin_company.py", "modules/llm_arbiter.py", "modules/output_artifacts.py",
    "modules/pipeline_runner.py", "modules/publication_policy.py", "modules/redaction.py",
    "modules/report.py", "modules/resolution_orchestrator.py", "modules/run_budget.py",
    "modules/run_context.py", "modules/runtime.py", "modules/search.py",
    "tests/test_autonomous_resolution_package.py", "tests/test_behavioral_replay.py",
    "tests/test_company_resolver_package.py", "tests/test_general_system_packages.py",
    "tests/test_identity_architecture.py", "tests/test_lifecycle_safety.py",
    "tests/test_linkedin_company_package.py", "tests/test_live_run_go_contract.py",
    "tests/test_llm_arbiter_package.py", "tests/test_operational_contracts.py",
    "tests/test_p3_package.py", "tests/test_p6_package.py", "tests/test_publication_export_gate.py",
    "tests/test_publication_reason_parsing.py", "tests/test_remediation_runs.py",
    "tests/test_search_phase_regressions.py",
    "tests/petzoo_child_runner.py", "tests/petzoo_fixture_support.py",
    "tests/petzoo_p06_child_runner.py", "tests/petzoo_p12_child_runner.py",
    "tests/petzoo_receipt_resume.py", "tests/strict_fixtures.py",
    "tests/test_delivery_verifier_contract.py", "tests/test_mimar_regressions_20260919.py",
    "tests/test_mimar_regressions_20260920.py", "tests/test_mimari_reaudit_altinci_20260921.py",
    "tests/test_mimari_reaudit_besinci_20260921.py", "tests/test_mimari_reaudit_ucuncu_20260920.py",
    "tests/test_mimari_tek_teslim_20260921.py", "tests/test_petzoo_pipeline_acceptance.py",
    "tests/test_petzoo_scheduler_incident.py", "tests/fixtures/petzoo_acceptance/manifest.json",
    "tools/build_petzoo_source_manifest.py", "tools/collect_petzoo_gate_evidence.py",
    "tools/delivery_verifier.py", "tools/run_petzoo_final_acceptance.py",
    "tools/run_petzoo_verifier_negatives.py", "tools/verify_petzoo_delivery.py",
    "tools/petzoo_offline/run_offline_regression.py", "tools/petzoo_offline/petzoo_offline_plugin.py",
    "tools/petzoo_offline/sitecustomize.py", "docs/petzoo_offline_status.md",
]
COMMIT_SCOPE_PURPOSE = (
    "Bounded scheduler/ledger/provider-retrieval/evidence-publication implementation, its direct regression tests, "
    "synthetic PETZOO fixture and child helpers, reproducible offline harness, and the concise test-status record."
)
PROTECTED_ROOTS = ("input", "state", "data", "runs", "outputs")
TOTAL_BUDGET_SECONDS = 6 * 60 * 60
COLLECTION_LIMIT_SECONDS = 60
RUN_LIMIT_SECONDS = 30 * 60
NODE_LIMIT_SECONDS = 180
END_MARGIN_SECONDS = 90


def configure_paths() -> dict:
    global EVIDENCE, EVIDENCE_ROOT, ALLOWED_OUTPUT_ROOT, DEADLINE_FILE
    global TASK_DEADLINE_QPC, TASK_DEADLINE_FREQUENCY, TASK_START_UTC
    global TEST_ROOT, SNAPSHOT_OUTPUTS, PREVIOUS_EVIDENCE, SCOPE_MANIFEST, RUNTIME, DEPENDENCY_CACHE_DIR
    parser = argparse.ArgumentParser(description="Run the bounded PETZOO offline regression in an isolated snapshot.")
    parser.add_argument("--evidence-dir", default=os.environ.get("PETZOO_EVIDENCE_DIR", str(DEFAULT_EVIDENCE)))
    parser.add_argument("--scope-manifest", default=os.environ.get(
        "PETZOO_SCOPE_MANIFEST", str(ROOT / "outputs" / "petzoo_incident_fix_20260923_tek_teslim" / "source_manifest.json"),
    ))
    parser.add_argument("--previous-evidence", default=os.environ.get(
        "PETZOO_PREVIOUS_EVIDENCE", str(ROOT / "outputs" / "petzoo_offline_regresyon_bolum1_r2_20260925"),
    ))
    parser.add_argument("--python", dest="python_path", default=os.environ.get("PETZOO_PYTHON", sys.executable))
    parser.add_argument("--allowed-output-root", default=os.environ.get("PETZOO_ALLOWED_OUTPUT_ROOT", str(ROOT / "outputs")))
    parser.add_argument("--task-evidence-root", default=os.environ.get("PETZOO_TASK_EVIDENCE_ROOT", ""))
    parser.add_argument("--deadline-file", default=os.environ.get("PETZOO_DEADLINE_FILE", ""))
    args = parser.parse_args()

    outputs_root = Path(args.allowed_output_root).expanduser().resolve()
    outputs_item = Path(args.allowed_output_root).expanduser()
    if not outputs_item.is_dir() or outputs_item.is_symlink() or getattr(outputs_item.lstat(), "st_file_attributes", 0) & 0x400:
        raise RuntimeError("allowed outputs root must be a real directory, not a link/reparse point")
    ALLOWED_OUTPUT_ROOT = outputs_root
    EVIDENCE = Path(args.evidence_dir).expanduser().resolve()
    if EVIDENCE == outputs_root or not EVIDENCE.is_relative_to(outputs_root):
        raise RuntimeError(f"evidence-dir must be a child of the explicitly allowed outputs directory: {EVIDENCE}")
    EVIDENCE_ROOT = Path(args.task_evidence_root).expanduser().resolve() if args.task_evidence_root else EVIDENCE.parent
    if EVIDENCE_ROOT != outputs_root and not EVIDENCE_ROOT.is_relative_to(outputs_root):
        raise RuntimeError(f"task evidence root must be under the allowed outputs directory: {EVIDENCE_ROOT}")
    if EVIDENCE != EVIDENCE_ROOT and not EVIDENCE.is_relative_to(EVIDENCE_ROOT):
        raise RuntimeError(f"attempt evidence directory must be inside the task evidence root: {EVIDENCE}")
    if not args.deadline_file:
        raise RuntimeError("a persistent six-hour --deadline-file is required")
    DEADLINE_FILE = Path(args.deadline_file).expanduser().resolve()
    if not DEADLINE_FILE.is_file() or not DEADLINE_FILE.is_relative_to(EVIDENCE_ROOT):
        raise RuntimeError("deadline file must exist inside the persistent task evidence root")
    deadline = json.loads(DEADLINE_FILE.read_text(encoding="utf-8-sig"))
    TASK_DEADLINE_QPC = int(deadline["deadline_qpc"])
    TASK_DEADLINE_FREQUENCY = int(deadline["qpc_frequency"])
    TASK_START_UTC = str(deadline["start_utc"])
    if TASK_DEADLINE_FREQUENCY <= 0 or not TASK_START_UTC:
        raise RuntimeError("persistent deadline record is incomplete")
    TEST_ROOT = EVIDENCE / "source_snapshot"
    SNAPSHOT_OUTPUTS = TEST_ROOT / "outputs"
    DEPENDENCY_CACHE_DIR = EVIDENCE / "dependency_cache" / "tldextract"
    PREVIOUS_EVIDENCE = Path(args.previous_evidence).expanduser().resolve()
    SCOPE_MANIFEST = Path(args.scope_manifest).expanduser().resolve()
    RUNTIME = Path(args.python_path).expanduser().resolve()
    if not RUNTIME.is_file():
        raise RuntimeError(f"Python executable does not exist: {RUNTIME}")
    if not SCOPE_MANIFEST.is_file():
        raise RuntimeError(f"source scope manifest does not exist: {SCOPE_MANIFEST}")
    if TEST_ROOT.exists() or DEPENDENCY_CACHE_DIR.exists() or (EVIDENCE / "run_state.json").exists():
        raise RuntimeError("evidence run already initialized; refusing to reuse or overwrite it")
    if EVIDENCE.exists():
        allowed_existing = {"git_baseline.json"}
        existing = {item.name for item in EVIDENCE.iterdir()}
        if existing - allowed_existing or any(item.is_symlink() for item in EVIDENCE.iterdir()):
            raise RuntimeError(f"evidence root contains unexpected pre-existing artifacts: {sorted(existing)}")
    return {
        "evidence_dir": str(EVIDENCE),
        "scope_manifest": str(SCOPE_MANIFEST),
        "previous_evidence": str(PREVIOUS_EVIDENCE),
        "python": str(RUNTIME),
        "repository_outputs_root": str(outputs_root),
        "task_evidence_root": str(EVIDENCE_ROOT),
        "deadline_file": str(DEADLINE_FILE),
        "deadline_qpc": TASK_DEADLINE_QPC,
        "deadline_qpc_frequency": TASK_DEADLINE_FREQUENCY,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def monotonic_qpc() -> tuple[int, int]:
    if os.name != "nt":
        return time.monotonic_ns(), 1_000_000_000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    counter = ctypes.c_longlong()
    frequency = ctypes.c_longlong()
    if not kernel32.QueryPerformanceCounter(ctypes.byref(counter)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel32.QueryPerformanceFrequency(ctypes.byref(frequency)) or frequency.value <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counter.value), int(frequency.value)


def remaining_task_seconds() -> float:
    if not TASK_DEADLINE_QPC or not TASK_DEADLINE_FREQUENCY:
        return 0.0
    now, frequency = monotonic_qpc()
    if frequency != TASK_DEADLINE_FREQUENCY:
        return 0.0
    return max(0.0, (TASK_DEADLINE_QPC - now) / frequency)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_commit_scope_manifest(state: dict) -> dict:
    baseline_path = EVIDENCE / "git_baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8-sig"))

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=True,
        )
        return completed.stdout.strip()

    head = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    target_branch = str(baseline.get("target_branch", "codex/release-hardening"))
    expected_worktree_branch = str(baseline.get("worktree_branch", baseline.get("branch", target_branch)))
    index_tree = git("write-tree")
    staged = git("diff", "--cached", "--name-status")
    remotes = git("remote", "-v").splitlines()
    expected_remote = "https://github.com/misaacozmen/b2b-contact-finder.git"
    if (
        head != baseline.get("head")
        or branch != expected_worktree_branch
        or index_tree != baseline.get("index_tree")
        or staged
        or not any(line.startswith(f"origin\t{expected_remote} (fetch)") for line in remotes)
        or not any(line.startswith(f"origin\t{expected_remote} (push)") for line in remotes)
    ):
        raise RuntimeError("Git HEAD/branch/index/remote changed from the captured authorized baseline")

    forbidden_parts = {"input", "state", "data", "runs", "outputs", "tmp", ".runtime", ".venv"}
    records = []
    for relative in COMMIT_SCOPE_PATHS:
        parts = Path(relative).parts
        if Path(relative).is_absolute() or ".." in parts or any(part.lower() in forbidden_parts for part in parts):
            raise RuntimeError(f"unsafe path in explicit commit scope: {relative}")
        if any(part.lower() in {".env", ".env.local", ".env.production"} for part in parts):
            raise RuntimeError(f"credential path cannot enter commit scope: {relative}")
        path = ROOT / relative
        if not path.exists() and relative == "docs/petzoo_offline_status.md":
            records.append({"path": relative, "planned_after_success": True, "sha256": None, "bytes": None})
            continue
        if not path.is_file() or path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 0x400:
            raise RuntimeError(f"explicit commit candidate missing or redirected: {relative}")
        if not path.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError(f"commit candidate escapes repository root: {relative}")
        records.append({"path": relative, "planned_after_success": False, "sha256": file_sha256(path), "bytes": path.stat().st_size})
    manifest = {
        "purpose": COMMIT_SCOPE_PURPOSE,
        "head_before_test": head,
        "branch": branch,
        "target_branch": target_branch,
        "origin": expected_remote,
        "index_tree_before_test": index_tree,
        "staged_paths_before_test": [],
        "paths": records,
        "excluded_by_scope": [
            ".closure_audit_root", "MIMAR_*.md", "mimar_*.csv", "paid-approval*.json",
            "paid-budget*.json", "run_*.ps1", "tmp/", "woodtech_*.xlsx", "woodtech_*_raporu*.md",
            "tests/fixtures/zuchex_public_frontend_observation_20260907.json",
            "tools/build_mimar_audit.py", "tools/export_ankiros_input.py",
            "tools/final_audit_runner.py", "tools/measurement_audit.py",
        ],
    }
    write_json(EVIDENCE / "commit_scope.json", manifest)
    write_json(EVIDENCE / "candidate_hashes_start.json", {
        "captured_at_utc": utc_now(),
        "files": [row for row in records if row.get("sha256")],
        "planned_after_success": [row["path"] for row in records if row.get("planned_after_success")],
    })
    state["commit_scope_path_count"] = len(records)
    state["candidate_hashes_before_validation"] = sum(row.get("sha256") is not None for row in records)
    state["commit_scope_sha256"] = file_sha256(EVIDENCE / "commit_scope.json")
    return manifest


def compare_candidate_hashes() -> dict:
    start = json.loads((EVIDENCE / "candidate_hashes_start.json").read_text(encoding="utf-8"))
    end_rows = []
    changed = []
    missing = []
    for row in start.get("files", []):
        path = ROOT / row["path"]
        if not path.is_file():
            missing.append(row["path"])
            continue
        actual = file_sha256(path)
        end_rows.append({"path": row["path"], "sha256": actual, "bytes": path.stat().st_size})
        if actual != row.get("sha256"):
            changed.append({"path": row["path"], "expected_sha256": row.get("sha256"), "actual_sha256": actual})
    record = {
        "captured_at_utc": utc_now(),
        "unchanged": not changed and not missing,
        "files": end_rows,
        "changed": changed,
        "missing": missing,
        "planned_after_success": start.get("planned_after_success", []),
    }
    write_json(EVIDENCE / "candidate_hashes_end.json", record)
    return record


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def r2_copy_harness() -> dict:
    source_root = ROOT / "tools" / "petzoo_offline"
    records = {}
    for name in ("run_offline_regression.py", "petzoo_offline_plugin.py", "sitecustomize.py"):
        source = source_root / name
        destination = EVIDENCE / name
        if not source.is_file() or destination.exists():
            raise RuntimeError(f"canonical harness missing or evidence copy already exists: {source} -> {destination}")
        before = file_sha256(source)
        shutil.copyfile(source, destination)
        copied = file_sha256(destination)
        if before != copied or before != file_sha256(source):
            raise RuntimeError(f"canonical harness copy hash mismatch: {name}")
        records[name] = copied
    write_json(EVIDENCE / "harness_copy.json", {
        "canonical_root": str(source_root),
        "evidence_copy_root": str(EVIDENCE),
        "files": records,
        "identical": True,
    })
    return {"source_root": str(source_root), "files": records, "identical": True}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def r2_harness_hashes() -> dict[str, str]:
    names = ("run_offline_regression.py", "petzoo_offline_plugin.py", "sitecustomize.py")
    return {
        name: file_sha256(EVIDENCE / name) if (EVIDENCE / name).is_file() else "NOT_RUN"
        for name in names
    }


def runtime_sources() -> list[Path]:
    paths = [ROOT / "config.py", ROOT / "main.py", ROOT / "scrape_exhibitors.py"]
    paths.extend(ROOT.glob("requirements*.txt"))
    paths.extend((ROOT / "modules").glob("*.py"))
    return sorted({path.resolve() for path in paths if path.is_file()})


def runtime_tree_hash() -> str:
    digest = hashlib.sha256()
    for path in runtime_sources():
        relative = path.relative_to(ROOT.resolve()).as_posix().encode("utf-8")
        data_hash = bytes.fromhex(file_sha256(path))
        size = path.stat().st_size
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(size.to_bytes(8, "big"))
        digest.update(data_hash)
    return digest.hexdigest()


def source_test_paths() -> list[Path]:
    candidates = []
    test_root = ROOT / "tests"
    if test_root.exists():
        for current, directories, files in os.walk(test_root, followlinks=False):
            directories[:] = sorted(d for d in directories if d not in {"__pycache__", ".pytest_cache"})
            for name in sorted(files):
                path = Path(current) / name
                if path.suffix in {".pyc", ".pyo"}:
                    continue
                candidates.append(path.resolve())
    candidates.extend(path.resolve() for path in ROOT.glob("*.py") if path.is_file())
    tools = ROOT / "tools"
    if tools.exists():
        candidates.extend(path.resolve() for path in tools.rglob("*.py") if path.is_file() and "__pycache__" not in path.parts)
    for name in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"):
        path = ROOT / name
        if path.is_file():
            candidates.append(path.resolve())
    for name in ("run_offline_regression.py", "petzoo_offline_plugin.py", "sitecustomize.py"):
        candidates.append((EVIDENCE / name).resolve())
    return sorted(set(candidates))


def source_test_inventory() -> dict:
    rows = []
    tree = hashlib.sha256()
    total_bytes = 0
    for path in source_test_paths():
        relative = path.relative_to(ROOT.resolve()).as_posix()
        size = path.stat().st_size
        digest = bytes.fromhex(file_sha256(path))
        path_bytes = relative.encode("utf-8")
        tree.update(len(path_bytes).to_bytes(8, "big"))
        tree.update(path_bytes)
        tree.update(size.to_bytes(8, "big"))
        tree.update(digest)
        total_bytes += size
        rows.append({"path": relative, "bytes": size, "sha256": digest.hex()})
    return {
        "captured_at_utc": utc_now(),
        "runtime_source_tree_sha256": runtime_tree_hash(),
        "inventory_sha256": tree.hexdigest(),
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "excluded_generated_paths": ["**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**"],
        "files": rows,
    }


def _is_reparse(path: Path, stat_result) -> bool:
    reparse_flag = getattr(stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(stat_result, "st_file_attributes", 0) & reparse_flag)


def protected_inventory(progress_label: str, task_started: float) -> dict:
    evidence_resolved = EVIDENCE.resolve()
    rows: dict[str, dict] = {}
    files_done = 0
    total_bytes = 0
    next_log = time.monotonic() + 15
    for root_name in PROTECTED_ROOTS:
        root = ROOT / root_name
        if not root.exists():
            rows[f"{root_name}/<missing-root>"] = {"kind": "missing-root"}
            continue
        if root_name == "outputs":
            try:
                if evidence_resolved.parent == root.resolve():
                    # This run's own records are the only excluded output subtree.
                    excluded_output_name = evidence_resolved.name
                else:
                    excluded_output_name = ""
            except OSError:
                excluded_output_name = ""
        else:
            excluded_output_name = ""
        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            kept_directories = []
            for directory in sorted(directories):
                path = current_path / directory
                try:
                    if root_name == "outputs" and path.resolve() == evidence_resolved:
                        continue
                    st = path.lstat()
                except OSError as exc:
                    rows[path.relative_to(ROOT).as_posix()] = {"kind": "stat-error", "error": repr(exc)}
                    continue
                if _is_reparse(path, st):
                    try:
                        target = os.readlink(path)
                    except OSError:
                        target = "<unavailable>"
                    rows[path.relative_to(ROOT).as_posix()] = {
                        "kind": "reparse", "mode": st.st_mode, "device": st.st_dev,
                        "inode": st.st_ino, "target": target,
                    }
                else:
                    kept_directories.append(directory)
            directories[:] = kept_directories
            for filename in sorted(filenames):
                path = current_path / filename
                try:
                    st = path.lstat()
                    relative = path.relative_to(ROOT).as_posix()
                    if _is_reparse(path, st):
                        try:
                            target = os.readlink(path)
                        except OSError:
                            target = "<unavailable>"
                        rows[relative] = {
                            "kind": "reparse", "mode": st.st_mode, "device": st.st_dev,
                            "inode": st.st_ino, "target": target,
                        }
                        continue
                    sha = file_sha256(path)
                    size = st.st_size
                    rows[relative] = {"kind": "file", "bytes": size, "sha256": sha}
                    files_done += 1
                    total_bytes += size
                except OSError as exc:
                    rows[path.relative_to(ROOT).as_posix()] = {"kind": "read-error", "error": repr(exc)}
                now = time.monotonic()
                if now >= next_log:
                    append_jsonl(EVIDENCE / "watchdog_progress.jsonl", {
                        "phase": progress_label, "files_hashed": files_done,
                        "bytes_hashed": total_bytes,
                        "elapsed_seconds": round(now - task_started, 3),
                        "timestamp_utc": utc_now(),
                    })
                    print(f"[{progress_label}] {files_done} files, {total_bytes} bytes hashed", flush=True)
                    next_log = now + 15
    digest = hashlib.sha256()
    for relative, row in sorted(rows.items()):
        encoded = relative.encode("utf-8")
        material = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(material).to_bytes(8, "big"))
        digest.update(material)
    return {
        "captured_at_utc": utc_now(),
        "roots": list(PROTECTED_ROOTS),
        "excluded_evidence_subtree": str(evidence_resolved),
        "file_count": files_done,
        "entry_count_including_reparse": len(rows),
        "total_bytes": total_bytes,
        "inventory_sha256": digest.hexdigest(),
        "entries": rows,
    }


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInfo(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInfo(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInfo), ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInfo(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class ProcessJob:
    """A Windows Job Object confines and can terminate only this pytest tree."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            raise OSError("this bounded runner requires Windows Job Object process-tree control")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(info), ctypes.sizeof(info),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_count(self) -> int:
        info = _BasicAccountingInfo()
        returned = wintypes.DWORD()
        ok = self.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> None:
        if self.handle and not self.kernel.TerminateJobObject(self.handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def make_child_env(phase: str, command_id: str) -> tuple[dict[str, str], list[str]]:
    env, removed = r2_child_env(phase)
    env["B2B_COMMAND_ID"] = command_id
    return env, removed


def verify_job_object_before_pytest() -> dict:
    env, removed = make_child_env("supervision-preflight", "petzoo-job-control-preflight")
    env["PETZOO_CHILD_PROCESS"] = "1"
    argv = [str(RUNTIME), "-c", "pass"]
    result = {
        "argv": argv,
        "started_at_utc": utc_now(),
        "credential_variable_names_removed": removed,
        "offline": True,
        "process_tree_containment": "Windows Job Object kill-on-close",
    }
    try:
        job = ProcessJob()
    except OSError as exc:
        result.update({"available": False, "process_tree_closed": True, "error": repr(exc)})
        write_json(EVIDENCE / "job_object_preflight.json", result)
        append_jsonl(EVIDENCE / "process_supervision.jsonl", result)
        raise RuntimeError(f"private Windows Job Object unavailable: {exc}") from exc
    with (EVIDENCE / "job_preflight_stdout.txt").open("wb", buffering=0) as stdout, (EVIDENCE / "job_preflight_stderr.txt").open("wb", buffering=0) as stderr:
        process = subprocess.Popen(
            argv, cwd=ROOT, env=env, stdout=stdout, stderr=stderr,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        result["pid"] = process.pid
        try:
            job.assign(process)
            result["job_assignment"] = "success"
        except OSError as exc:
            result.update({"job_assignment": "failed", "error": repr(exc)})
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            job.close()
            result["process_tree_closed"] = process.poll() is not None
            write_json(EVIDENCE / "job_object_preflight.json", result)
            append_jsonl(EVIDENCE / "process_supervision.jsonl", result)
            raise RuntimeError(f"cannot contain pytest in a private Windows Job Object: {exc}") from exc
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            job.terminate(1)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        active = job.active_count()
        deadline = time.monotonic() + 3.0
        while active and time.monotonic() < deadline:
            time.sleep(0.1)
            active = job.active_count()
        result.update({
            "exit_code": process.poll(),
            "active_processes_after_wait": active,
            "process_tree_closed": process.poll() is not None and active == 0,
            "ended_at_utc": utc_now(),
        })
        job.close()
    write_json(EVIDENCE / "job_object_preflight.json", result)
    append_jsonl(EVIDENCE / "process_supervision.jsonl", result)
    if not result["process_tree_closed"] or result.get("timed_out") or result.get("exit_code") != 0:
        raise RuntimeError("private process-tree containment preflight did not close cleanly")
    return result


def read_node_events(path: Path, offset: int, state: dict) -> int:
    if not path.is_file():
        return offset
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read()
        new_offset = handle.tell()
    if not raw:
        return new_offset
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        state["last_event"] = event
        if event.get("event") == "node_start":
            state["active_node"] = event.get("nodeid", "")
            state["active_node_started_monotonic"] = float(event.get("monotonic", 0.0))
            state["last_completed_node"] = state.get("last_completed_node", "")
        elif event.get("event") == "phase_report":
            state["last_completed_stage"] = (
                f"{event.get('nodeid', '')}::{event.get('phase', '')}={event.get('outcome', '')}"
            )
        elif event.get("event") == "node_finish":
            state["last_completed_node"] = event.get("nodeid", "")
            if state.get("active_node") == event.get("nodeid"):
                state["active_node"] = ""
                state["active_node_started_monotonic"] = 0.0
    return new_offset


def _stop_process_tree(
    process: subprocess.Popen, job: ProcessJob | None, reason: str, *, grace_seconds: float = 10.0,
) -> dict:
    details = {"reason": reason, "graceful_signal_sent": False, "forced_tree_stop": False}
    started = time.monotonic()
    if process.poll() is None:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            details["graceful_signal_sent"] = True
        except (OSError, ValueError, ProcessLookupError):
            pass
    deadline = started + grace_seconds
    while time.monotonic() < deadline:
        process.poll()
        try:
            active = job.active_count() if job and job.handle else (0 if process.poll() is not None else 1)
        except OSError:
            active = 1
        if active == 0:
            break
        time.sleep(0.2)
    try:
        active = job.active_count() if job and job.handle else (0 if process.poll() is not None else 1)
    except OSError:
        active = 1
    if active > 0:
        details["forced_tree_stop"] = True
        if job and job.handle:
            details["job_active_processes_before_force"] = active
            try:
                job.terminate(1)
                force_deadline = time.monotonic() + 5.0
                while time.monotonic() < force_deadline and job.active_count() > 0:
                    time.sleep(0.1)
                details["active_processes_after_force"] = job.active_count()
            except OSError as exc:
                details["job_force_error"] = repr(exc)
            job.close()
        elif process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=8,
            )
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if job and job.handle:
            job.close()
        else:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            details["root_wait_failed"] = True
    details["elapsed_cleanup_seconds"] = round(time.monotonic() - started, 3)
    details["root_exit_code"] = process.poll()
    try:
        details["active_processes_after_cleanup"] = job.active_count() if job and job.handle else None
    except OSError as exc:
        details["active_processes_after_cleanup_error"] = repr(exc)
    return details


def supervise(stage: str, argv: list[str], timeout_seconds: float, env: dict[str, str], task_started: float) -> dict:
    stdout_path = EVIDENCE / f"{stage}_stdout.txt"
    stderr_path = EVIDENCE / f"{stage}_stderr.txt"
    node_events = EVIDENCE / "node_events.jsonl"
    state = {"active_node": "", "active_node_started_monotonic": 0.0, "last_completed_node": ""}
    offset = 0
    stage_started = time.monotonic()
    job = None
    process = None
    record = {
        "stage": stage,
        "argv": argv,
        "started_at_utc": utc_now(),
        "timeout_seconds": timeout_seconds,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "process_tree_containment": "windows_job_object_kill_on_close",
    }
    task_remaining = remaining_task_seconds()
    if task_remaining <= 0:
        record.update({"started": False, "exit_code": None, "timed_out": True, "termination_reason": "global_deadline"})
        write_json(EVIDENCE / f"{stage}_process.json", record)
        return record
    if timeout_seconds > task_remaining:
        record["global_deadline_limited_timeout_seconds"] = task_remaining
        timeout_seconds = task_remaining
        record["timeout_seconds"] = timeout_seconds
    try:
        job = ProcessJob()
    except OSError as exc:
        record.update({"started": False, "containment_error": repr(exc), "exit_code": None})
        write_json(EVIDENCE / f"{stage}_process.json", record)
        return record
    with stdout_path.open("wb", buffering=0) as stdout, stderr_path.open("wb", buffering=0) as stderr:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(argv, cwd=TEST_ROOT, env=env, stdout=stdout, stderr=stderr, creationflags=flags)
        record["pid"] = process.pid
        try:
            job.assign(process)
            record["job_assignment"] = "success"
        except OSError as exc:
            record["job_assignment"] = "failed"
            record["job_assignment_error"] = repr(exc)
            killed = _stop_process_tree(process, None, "job assignment failed")
            job.close()
            record.update({
                "started": True,
                "exit_code": process.poll(),
                "timed_out": False,
                "process_tree_closed": process.poll() is not None,
                "process_tree_cleanup": killed,
                "ended_at_utc": utc_now(),
            })
            write_json(EVIDENCE / f"{stage}_process.json", record)
            return record
        reason = ""
        next_progress = time.monotonic()
        last_job_count = None
        while process.poll() is None:
            offset = read_node_events(node_events, offset, state)
            now = time.monotonic()
            if remaining_task_seconds() <= 0:
                reason = "global_deadline"
            if now - stage_started >= timeout_seconds:
                reason = reason or "stage_wall_timeout"
            if state.get("active_node") and (stage == "run" or stage.startswith("long_")):
                active_node = str(state["active_node"])
                node_wall_limit = NODE_LIMIT_SECONDS if stage == "run" else LONG_NODE_WALL_LIMITS.get(active_node, timeout_seconds)
                node_elapsed = now - float(state.get("active_node_started_monotonic", now))
                if node_elapsed >= node_wall_limit:
                    reason = "node_wall_timeout"
                    state["active_node_elapsed_seconds"] = round(node_elapsed, 3)
                    state["active_node_wall_limit_seconds"] = node_wall_limit
            try:
                last_job_count = job.active_count()
            except OSError as exc:
                reason = reason or "job_accounting_error"
                record["job_accounting_error"] = repr(exc)
            if reason:
                break
            if now >= next_progress:
                progress = {
                    "stage": stage,
                    "pid": process.pid,
                    "elapsed_seconds": round(now - stage_started, 2),
                    "active_node": state.get("active_node", ""),
                    "active_node_elapsed_seconds": round(now - state["active_node_started_monotonic"], 2) if state.get("active_node") else 0,
                    "last_completed_node": state.get("last_completed_node", ""),
                    "job_active_processes": last_job_count,
                    "timestamp_utc": utc_now(),
                }
                append_jsonl(EVIDENCE / "watchdog_progress.jsonl", progress)
                print(json.dumps(progress, ensure_ascii=False), flush=True)
                next_progress = now + 5.0
            time.sleep(0.2)
        offset = read_node_events(node_events, offset, state)
        if reason:
            record["termination_reason"] = reason
            record["process_tree_cleanup"] = _stop_process_tree(process, job, reason)
        else:
            # A completed pytest root is not enough: wait for any subprocesses in its job.
            wait_deadline = time.monotonic() + 10.0
            while time.monotonic() < wait_deadline:
                try:
                    last_job_count = job.active_count()
                except OSError as exc:
                    record["job_accounting_error"] = repr(exc)
                    last_job_count = 1
                if last_job_count == 0:
                    break
                time.sleep(0.2)
            if last_job_count:
                record["orphaned_descendants_detected"] = True
                record["process_tree_cleanup"] = _stop_process_tree(
                    process, job, "pytest exited with descendants still active", grace_seconds=0.0,
                )
            else:
                job.close()
        if job and job.handle:
            job.close()
        record.update({
            "started": True,
            "ended_at_utc": utc_now(),
            "duration_seconds": round(time.monotonic() - stage_started, 3),
            "exit_code": process.poll(),
            "timed_out": bool(reason in {"stage_wall_timeout", "node_wall_timeout"}),
            "process_wait_completed": process.poll() is not None,
            "process_tree_closed": (
                process.poll() is not None
                and not record.get("job_accounting_error")
                and not record.get("process_tree_cleanup", {}).get("job_force_error")
                and not record.get("process_tree_cleanup", {}).get("root_wait_failed")
                and (
                    last_job_count == 0
                    or record.get("process_tree_cleanup", {}).get("active_processes_after_force") == 0
                    or record.get("process_tree_cleanup", {}).get("active_processes_after_cleanup") == 0
                )
            ),
            "job_active_processes_at_close": last_job_count,
            "active_node_at_stop": state.get("active_node", ""),
            "last_completed_node": state.get("last_completed_node", ""),
            "active_node_elapsed_seconds_at_stop": state.get("active_node_elapsed_seconds"),
        })
    write_json(EVIDENCE / f"{stage}_process.json", record)
    return record


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"malformed_line": line})
    return rows


def compare_inventories(before: dict, after: dict) -> dict:
    first = before.get("entries", {}) or {row["path"]: row for row in before.get("files", [])}
    last = after.get("entries", {}) or {row["path"]: row for row in after.get("files", [])}
    added = sorted(set(last) - set(first))
    removed = sorted(set(first) - set(last))
    changed = sorted(path for path in set(first) & set(last) if first[path] != last[path])
    errors = sorted(path for path, row in {**first, **last}.items() if row.get("kind", "").endswith("error"))
    return {
        "match": not (added or removed or changed or errors),
        "before_inventory_sha256": before.get("inventory_sha256"),
        "after_inventory_sha256": after.get("inventory_sha256"),
        "added_count": len(added), "removed_count": len(removed), "changed_count": len(changed),
        "read_error_count": len(errors),
        "added": added[:200], "removed": removed[:200], "changed": changed[:200], "read_errors": errors[:200],
    }


def classify_reports(report_rows: list[dict]) -> dict:
    by_node: dict[str, dict[str, dict]] = {}
    for row in report_rows:
        nodeid = row.get("nodeid")
        phase = row.get("phase")
        if nodeid and phase in {"setup", "call", "teardown"}:
            by_node.setdefault(nodeid, {})[phase] = row
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "completed_nodes": 0}
    skipped_nodes = []
    failed_nodes = []
    for nodeid, phases in by_node.items():
        if "teardown" in phases:
            counts["completed_nodes"] += 1
        failed = [(phase, row) for phase, row in phases.items() if row.get("outcome") == "failed"]
        if failed:
            if any(phase in {"setup", "teardown"} for phase, _ in failed):
                counts["errors"] += 1
            else:
                counts["failed"] += 1
            failed_nodes.append({"nodeid": nodeid, "phases": [phase for phase, _ in failed]})
            continue
        skip_phase = next((p for p, row in phases.items() if row.get("outcome") == "skipped"), None)
        if skip_phase:
            counts["skipped"] += 1
            skipped_nodes.append({
                "nodeid": nodeid,
                "phase": skip_phase,
                "reason": phases[skip_phase].get("skip_reason", ""),
                "wasxfail": phases[skip_phase].get("wasxfail", ""),
            })
            continue
        if phases.get("call", {}).get("outcome") == "passed" and all(
            phases.get(phase, {}).get("outcome") in {None, "passed"} for phase in ("setup", "teardown")
        ) and "teardown" in phases:
            counts["passed"] += 1
    counts["reported_nodes"] = len(by_node)
    counts["skipped_nodes"] = skipped_nodes
    counts["failed_nodes"] = failed_nodes
    return counts


def write_report(state: dict) -> None:
    collection = state.get("collection") or {}
    nodes = collection.get("nodeids", [])
    selected = state.get("selected_nodes") or state.get("selected_nodes_expected", [])
    counts = state.get("test_counts", {})
    n_total = len(nodes)
    n_deselected = len(state.get("deferred_nodeids", [])) if state.get("collection_verified") else 0
    n_selected = len(selected)
    completed = int(counts.get("completed_nodes", 0))
    outcome_total = sum(int(counts.get(name, 0)) for name in ("passed", "failed", "errors", "skipped"))
    unresolved = max(0, int(counts.get("unresolved_selected_count", n_selected - outcome_total)))
    never_started = int(counts.get("never_started_nodes", n_selected))
    started_not_finished = int(counts.get("started_not_finished_nodes", 0))
    status = state.get("section_status", "BLOCKED_WITH_EVIDENCE")
    lines = [
        "# PETZOO çevrimdışı regresyon — bölüm 1",
        "",
        f"**Bölüm sonucu:** `{status}`  ",
        "**Genel proje durumu:** `IMPLEMENTED_NOT_ACCEPTED`  ",
        f"**Başlangıç runtime hash doğrulaması:** `{state.get('runtime_start_hash', 'not-recorded')}`  ",
        f"**Bitiş runtime hash:** `{state.get('runtime_end_hash', 'not-recorded')}`",
        "",
        "## Kapsam ve sayımlar",
        "",
        f"- Collection toplamı: {n_total}",
        f"- Tam node kimliğiyle deselect: {n_deselected}",
        f"- Seçilen node: {n_selected}",
        f"- Tamamlanan node: {completed}",
        f"- Pass / fail / error / skip / sonuçlandırılamayan: {counts.get('passed', 0)} / {counts.get('failed', 0)} / {counts.get('errors', 0)} / {counts.get('skipped', 0)} / {unresolved}",
        f"- Seçilen ama hiç başlamayan node: {never_started}; başlayıp terminal node kaydı tamamlanmayan: {started_not_finished}",
        f"- Sayım denklemi: {n_selected} seçilen = {counts.get('passed', 0)} pass + {counts.get('failed', 0)} fail + {counts.get('errors', 0)} error + {counts.get('skipped', 0)} skip + {unresolved} sonuçlandırılamayan.",
        "- Ertelenen node: 3 (`DEFERRED_NOT_RUN`; skip sayısına dahil değildir)",
        "",
        "Ertelenen tam kimlikler:",
        *[f"- `DEFERRED_NOT_RUN` — `{nodeid}`" for nodeid in DEFERRED],
        "",
        "## Koşu ve süreç kontrolü",
        "",
        f"- Collection exit/timeout: {state.get('collection_process', {}).get('exit_code')} / {state.get('collection_process', {}).get('timed_out')}",
        f"- Test exit/timeout: {state.get('run_process', {}).get('exit_code')} / {state.get('run_process', {}).get('timed_out')}",
        f"- Son tamamlanan aşama: `{state.get('last_completed_stage', '')}`; son tamamlanan node: `{state.get('last_completed_node', '')}`",
        f"- Aktif node kesilme anında: `{state.get('active_node_at_stop', '')}`",
        f"- Süre aşımı nedeni: `{state.get('termination_reason', '')}`",
        f"- Süreç ağacı kapandı: {state.get('process_tree_closed', False)}; containment: Windows Job Object, kill-on-close",
        f"- Çevrimdışı: `B2B_TEST_OFFLINE=1`; gerçek sağlayıcı anahtarları temizlendi (yalnız değişken adları komut kaydında); alt süreçlere guard ve Python socket-deny hook'u aktarıldı.",
        f"- Ağ engel olayları: {state.get('network_blocked_events', 0)}; guard-armed kayıtları: {state.get('network_guard_armed', 0)}. Engellenen denemeler gerçek ağ çıkışı sayılmamıştır.",
        "",
        "## Bütünlük",
        "",
        f"- Test/kaynak/runner/plugin başlangıç-bitiş envanteri eşleşti: {state.get('source_inventory_match', False)}",
        f"- Protected roots `{', '.join(PROTECTED_ROOTS)}` ve eski teslimleri kapsayan dosya SHA-256 envanteri eşleşti: {state.get('protected_inventory_match', False)}",
        f"- Protected envanter: {state.get('protected_before', {}).get('file_count', 0)} dosya, {state.get('protected_before', {}).get('total_bytes', 0)} bayt; başlangıç `{state.get('protected_before', {}).get('inventory_sha256', '')}`, bitiş `{state.get('protected_after', {}).get('inventory_sha256', '')}`.",
        f"- Test/kaynak envanteri: {state.get('source_before', {}).get('file_count', 0)} dosya; başlangıç `{state.get('source_before', {}).get('inventory_sha256', '')}`, bitiş `{state.get('source_after', {}).get('inventory_sha256', '')}`.",
        "- Önceki teslim klasörü için geçmiş başlangıç hash algoritması/envanteri eksikliği geriye dönük kapatılmamıştır; bu rapor yalnız bu koşunun başlangıç-bitiş karşılaştırmasını kanıtlar.",
        "",
        "## Başarısızlıklar ve normal skip'ler",
        "",
    ]
    if counts.get("failed_nodes"):
        events = read_jsonl(EVIDENCE / "node_events.jsonl")
        for failure in counts["failed_nodes"]:
            nodeid = failure["nodeid"]
            lines.append(f"### `{nodeid}` — aşamalar: {', '.join(failure['phases'])}")
            for event in events:
                if event.get("event") == "phase_report" and event.get("nodeid") == nodeid and event.get("longrepr"):
                    lines.extend(["", "```text", event["longrepr"], "```", ""])
            if not any(event.get("event") == "phase_report" and event.get("nodeid") == nodeid and event.get("longrepr") for event in events):
                lines.append("Tam traceback `node_events.jsonl` içindeki ilgili `phase_report.longrepr` alanında saklıdır.")
    if counts.get("skipped_nodes"):
        for row in counts["skipped_nodes"]:
            lines.append(f"- `{row['nodeid']}` ({row['phase']}): {row['reason'] or '<reason not recorded>'}; wasxfail=`{row['wasxfail']}`")
    else:
        lines.append("Normal pytest skip'i kaydedilmedi.")
    if state.get("fatal_error"):
        lines.extend(["", "Runner/preflight hatası:", "", "```text", state["fatal_error"], "```"])
    if state.get("termination_reason"):
        raw_stderr = (EVIDENCE / "run_stderr.txt").read_text(encoding="utf-8", errors="replace") if (EVIDENCE / "run_stderr.txt").is_file() else ""
        trace_at = raw_stderr.rfind("Traceback (most recent call last):")
        if trace_at >= 0:
            trace = raw_stderr[trace_at:]
            lines.extend(["", "Watchdog kesişiminde stderr'de bulunan traceback:", "", "```text", trace[-120000:], "```"])
        else:
            lines.extend(["", "Watchdog kesişiminde Python traceback üretilemediyse tam ham stderr `run_stderr.txt`, son node/aşama ve olay anı kayıtları `node_events.jsonl` içindedir."])
    lines.extend([
        "",
        "## Kanıt dosyaları",
        "",
        "- Tam collection node listesi: `collection_nodes.json`",
        "- Run selection listesi: `selected_nodes.json`",
        "- Komut, argv/env politikası, deselect ve plugin hash'i: `run_command.json`",
        "- Başlangıç/bitiş kaynak-test-runner envanterleri: `source_test_inventory_before.json`, `source_test_inventory_after.json`, `source_test_inventory_comparison.json`",
        "- Korunan dizin dosya hash'leri: `protected_inventory_before.json`, `protected_inventory_after.json`, `protected_inventory_comparison.json`",
        "- Collection/test canlı stdout-stderr: `collect_stdout.txt`, `collect_stderr.txt`, `run_stdout.txt`, `run_stderr.txt`",
        "- Anlık node/aşama/traceback olayları: `node_events.jsonl`; pytest raporları: `pytest_reports.jsonl`; JUnit: `junit.xml` (varsa)",
        "- Ağ/alt süreç kanıtı: `network_guard.jsonl`, `child_processes.jsonl`, `plugin_events.jsonl`, `process_supervision.jsonl`, `collect_process.json`, `run_process.json`",
        "- Watchdog durumu ve pytest zamanlaması: `watchdog_progress.jsonl`, `pytest_timing.log`",
        "",
        f"Rapor yazım zamanı: {utc_now()} UTC.",
    ])
    (EVIDENCE / "regresyon_raporu.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    if os.name != "nt":
        raise SystemExit("Windows process-tree supervision is required for this run")
    if not EVIDENCE.is_dir():
        raise SystemExit(f"evidence directory must exist: {EVIDENCE}")
    started = time.monotonic()
    state = {
        "started_at_utc": utc_now(),
        "deferred_nodeids": DEFERRED,
        "section_status": "BLOCKED_WITH_EVIDENCE",
        "collection_verified": False,
        "source_inventory_match": False,
        "protected_inventory_match": False,
        "process_tree_closed": True,
        "fatal_error": "",
        "termination_reason": "",
        "last_completed_node": "",
        "active_node_at_stop": "",
        "test_counts": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "completed_nodes": 0, "reported_nodes": 0, "skipped_nodes": [], "failed_nodes": []},
    }
    write_json(EVIDENCE / "run_state.json", state)
    try:
        state["runtime_start_hash"] = runtime_tree_hash()
        state["source_before"] = source_test_inventory()
        state["runtime_source_files_before"] = [
            {"path": p.relative_to(ROOT).as_posix(), "bytes": p.stat().st_size, "sha256": file_sha256(p)}
            for p in runtime_sources()
        ]
        write_json(EVIDENCE / "source_test_inventory_before.json", state["source_before"])
        write_json(EVIDENCE / "runtime_source_manifest_before.json", state["runtime_source_files_before"])
        if state["runtime_start_hash"] != EXPECTED_RUNTIME_SHA256:
            raise RuntimeError(
                f"runtime source hash mismatch before collection: expected {EXPECTED_RUNTIME_SHA256}, got {state['runtime_start_hash']}"
            )

        protected_started = time.monotonic()
        state["protected_before"] = protected_inventory("protected_hash_start", started)
        state["protected_hash_start_seconds"] = round(time.monotonic() - protected_started, 3)
        write_json(EVIDENCE / "protected_inventory_before.json", state["protected_before"])
        state["job_object_preflight"] = verify_job_object_before_pytest()

        base_args = [
            str(RUNTIME), "-m", "pytest", "-q", "-p", "petzoo_offline_plugin",
            "-o", f"cache_dir={EVIDENCE / 'pytest_cache'}",
            "--basetemp", str(EVIDENCE / "pytest_tmp"),
        ]
        env_collect, removed_names = make_child_env("collect", "petzoo-offline-regression-part1-collect")
        state["removed_environment_names"] = removed_names
        collect_argv = [*base_args, "--collect-only"]
        state["collect_argv"] = collect_argv
        collect_result = supervise("collect", collect_argv, COLLECTION_LIMIT_SECONDS, env_collect, started)
        state["collection_process"] = collect_result
        state["process_tree_closed"] = state["process_tree_closed"] and bool(collect_result.get("process_tree_closed"))
        append_jsonl(EVIDENCE / "process_supervision.jsonl", collect_result)
        if collect_result.get("job_assignment") != "success":
            raise RuntimeError("collection process was not contained in a private Windows Job Object")
        if collect_result.get("timed_out") or collect_result.get("exit_code") != 0:
            state["termination_reason"] = collect_result.get("termination_reason", "collection_failed_or_timed_out")
            raise RuntimeError("collection failed or exceeded its 60-second wall-clock limit")
        collect_path = EVIDENCE / "collection_nodes.json"
        if not collect_path.is_file():
            raise RuntimeError("collection plugin did not save the full node list")
        state["collection"] = json.loads(collect_path.read_text(encoding="utf-8"))
        nodeids = state["collection"].get("nodeids", [])
        if not nodeids or len(set(nodeids)) != len(nodeids):
            raise RuntimeError("collection list is empty or contains duplicate full node IDs")
        identity_errors = [nodeid for nodeid in DEFERRED if nodeids.count(nodeid) != 1]
        if identity_errors:
            state["deferred_identity_errors"] = identity_errors
            raise RuntimeError("one or more explicit deferred node IDs were not present exactly once")
        state["collection_verified"] = True
        selected_nodes = [nodeid for nodeid in nodeids if nodeid not in DEFERRED]
        state["selected_nodes_expected"] = selected_nodes

        run_args = [
            *base_args,
            "--maxfail=1",
            f"--junitxml={EVIDENCE / 'junit.xml'}",
            *[f"--deselect={nodeid}" for nodeid in DEFERRED],
        ]
        env_run, removed_names_run = make_child_env("run", "petzoo-offline-regression-part1")
        state["removed_environment_names"] = sorted(set(removed_names) | set(removed_names_run))
        state["run_argv"] = run_args
        run_elapsed = time.monotonic() - started
        end_reserve = max(180.0, state["protected_hash_start_seconds"] + END_MARGIN_SECONDS)
        available = TOTAL_BUDGET_SECONDS - run_elapsed - end_reserve
        run_timeout = min(RUN_LIMIT_SECONDS, max(0.0, available))
        state["computed_run_timeout_seconds"] = run_timeout
        if run_timeout < 1:
            raise RuntimeError("task time budget leaves no safe window for the one bounded pytest run")
        run_result = supervise("run", run_args, run_timeout, env_run, started)
        state["run_process"] = run_result
        state["process_tree_closed"] = state["process_tree_closed"] and bool(run_result.get("process_tree_closed"))
        state["termination_reason"] = run_result.get("termination_reason", "")
        state["active_node_at_stop"] = run_result.get("active_node_at_stop", "")
        state["last_completed_node"] = run_result.get("last_completed_node", "")
        append_jsonl(EVIDENCE / "process_supervision.jsonl", run_result)
        if run_result.get("job_assignment") != "success":
            raise RuntimeError("test process was not contained in a private Windows Job Object")
        selected_path = EVIDENCE / "selected_nodes.json"
        if selected_path.is_file():
            state["selected_nodes"] = json.loads(selected_path.read_text(encoding="utf-8")).get("nodeids", [])
        else:
            state["selected_nodes"] = selected_nodes
        if state["selected_nodes"] != selected_nodes:
            raise RuntimeError("actual post-deselect node list differs from the verified expected selection")

        report_rows = read_jsonl(EVIDENCE / "pytest_reports.jsonl")
        state["test_counts"] = classify_reports(report_rows)
        network_paths = {EVIDENCE / "network_guard.jsonl"}
        network_paths.update(path for path in EVIDENCE.rglob("*network*.jsonl") if path.is_file())
        network_paths = {path for path in network_paths if path.is_file()}
        state["network_records"] = [
            row for path in sorted(network_paths)
            for row in read_jsonl(path)
            if row.get("kind") in {"blocked_network", "python_child_guard_armed", "guard_armed"}
        ]
        state["network_blocked_events"] = sum(row.get("kind") == "blocked_network" for row in state["network_records"])
        state["network_guard_armed"] = sum(row.get("kind") in {"python_child_guard_armed", "guard_armed"} for row in state["network_records"])
        state["network_guard_files"] = sorted(
            str(path.relative_to(EVIDENCE)) for path in network_paths
            if any(row.get("kind") in {"blocked_network", "python_child_guard_armed", "guard_armed"} for row in read_jsonl(path))
        )
        node_events = []
        if (EVIDENCE / "node_events.jsonl").is_file():
            node_events = read_jsonl(EVIDENCE / "node_events.jsonl")
        state["node_events_count"] = len(node_events)
        state["started_nodeids"] = [row.get("nodeid", "") for row in node_events if row.get("event") == "node_start"]
        state["finished_nodeids"] = [row.get("nodeid", "") for row in node_events if row.get("event") == "node_finish"]
        state["last_completed_node"] = next(
            (row.get("nodeid", "") for row in reversed(node_events) if row.get("event") == "node_finish"),
            state.get("last_completed_node", ""),
        )
        state["last_completed_stage"] = next(
            (f"{row.get('nodeid', '')}::{row.get('phase', '')}={row.get('outcome', '')}" for row in reversed(node_events) if row.get("event") == "phase_report"),
            state.get("last_completed_stage", ""),
        )
        outcome_total = sum(state["test_counts"].get(name, 0) for name in ("passed", "failed", "errors", "skipped"))
        state["test_counts"]["outcome_count"] = outcome_total
        state["test_counts"]["uncategorized_reported_nodes"] = max(0, state["test_counts"].get("reported_nodes", 0) - outcome_total)
        state["test_counts"]["unresolved_selected_count"] = max(0, len(state.get("selected_nodes_expected", [])) - outcome_total)
        started_set = set(state.get("started_nodeids", []))
        finished_set = set(state.get("finished_nodeids", []))
        state["test_counts"]["never_started_nodes"] = max(0, len(state.get("selected_nodes_expected", [])) - len(started_set))
        state["test_counts"]["started_not_finished_nodes"] = len(started_set - finished_set)
        if run_result.get("timed_out"):
            state["termination_reason"] = run_result.get("termination_reason", "watchdog_timeout")
        if run_result.get("exit_code") != 0:
            state["fatal_error"] = state.get("fatal_error") or f"pytest exited {run_result.get('exit_code')} (no retry; --maxfail=1)"
        if state["test_counts"].get("reported_nodes", 0) != len(selected_nodes):
            state["selected_unreported_count"] = max(0, len(selected_nodes) - state["test_counts"].get("reported_nodes", 0))
    except Exception as exc:
        state["fatal_error"] = f"{type(exc).__name__}: {exc}"
        if not state.get("termination_reason"):
            state["termination_reason"] = state.get("collection_process", {}).get("termination_reason", "")
    finally:
        try:
            state["source_after"] = source_test_inventory()
            write_json(EVIDENCE / "source_test_inventory_after.json", state["source_after"])
            write_json(EVIDENCE / "runtime_source_manifest_after.json", [
                {"path": p.relative_to(ROOT).as_posix(), "bytes": p.stat().st_size, "sha256": file_sha256(p)}
                for p in runtime_sources()
            ])
            state["runtime_end_hash"] = state["source_after"]["runtime_source_tree_sha256"]
            state["source_inventory_comparison"] = compare_inventories(state.get("source_before", {}), state["source_after"])
            state["source_inventory_match"] = bool(state["source_inventory_comparison"]["match"])
            write_json(EVIDENCE / "source_test_inventory_comparison.json", state["source_inventory_comparison"])
        except Exception as exc:
            state["source_inventory_error"] = repr(exc)
        try:
            state["protected_after"] = protected_inventory("protected_hash_end", started)
            write_json(EVIDENCE / "protected_inventory_after.json", state["protected_after"])
            state["protected_inventory_comparison"] = compare_inventories(state.get("protected_before", {}), state["protected_after"])
            state["protected_inventory_match"] = bool(state["protected_inventory_comparison"]["match"])
            write_json(EVIDENCE / "protected_inventory_comparison.json", state["protected_inventory_comparison"])
        except Exception as exc:
            state["protected_inventory_error"] = repr(exc)
        if not state.get("collection_process") or not state.get("run_process"):
            state["test_counts"] = state.get("test_counts", {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "completed_nodes": 0, "reported_nodes": 0, "skipped_nodes": [], "failed_nodes": []})
        if state.get("run_process", {}).get("process_tree_closed") is False or state.get("collection_process", {}).get("process_tree_closed") is False:
            state["process_tree_closed"] = False
        state["finished_at_utc"] = utc_now()
        state["duration_seconds"] = round(time.monotonic() - started, 3)
        successful_run = (
            state.get("run_process", {}).get("exit_code") == 0
            and not state.get("run_process", {}).get("timed_out")
            and not state.get("termination_reason")
            and state.get("test_counts", {}).get("failed", 0) == 0
            and state.get("test_counts", {}).get("errors", 0) == 0
            and state.get("test_counts", {}).get("reported_nodes", 0) == len(state.get("selected_nodes_expected", []))
            and state.get("source_inventory_match") is True
            and state.get("protected_inventory_match") is True
            and state.get("process_tree_closed") is True
            and not state.get("run_process", {}).get("orphaned_descendants_detected")
            and not state.get("fatal_error")
        )
        state["section_status"] = "OFFLINE_REGRESSION_PART1_PASS" if successful_run else "BLOCKED_WITH_EVIDENCE"
        write_json(EVIDENCE / "run_command.json", {
            "command_id": "petzoo-offline-regression-part1",
            "collect_argv": state.get("collect_argv", []),
            "run_argv": state.get("run_argv", []),
            "cwd": str(ROOT),
            "runtime_executable": str(RUNTIME),
            "environment_policy": {
                "B2B_TEST_OFFLINE": "1",
                "B2B_SOCKET_DENY_JSONL": str(EVIDENCE / "network_guard.jsonl"),
                "B2B_TEST_TIMING_LOG": str(EVIDENCE / "pytest_timing.log"),
                "B2B_PYTEST_REPORT_JSONL": str(EVIDENCE / "pytest_reports.jsonl"),
                "B2B_K6_RECEIPT_JSONL": str(EVIDENCE / "k6_receipts.jsonl"),
                "provider_credentials_inherited": False,
                "removed_environment_variable_names": state.get("removed_environment_names", []),
                "child_process_guard_injected": True,
                "child_process_mechanism": "PYTHONPATH sitecustomize.py plus B2B_TEST_OFFLINE and B2B_SOCKET_DENY_JSONL",
            },
            "source_runtime_sha256_at_start": state.get("runtime_start_hash"),
            "source_runtime_sha256_at_end": state.get("runtime_end_hash"),
            "plugin_sha256": {
                name: file_sha256(EVIDENCE / name)
                for name in ("petzoo_offline_plugin.py", "sitecustomize.py", "run_offline_regression.py")
                if (EVIDENCE / name).is_file()
            },
            "test_and_support_inventory_start_sha256": state.get("source_before", {}).get("inventory_sha256"),
            "test_and_support_inventory_end_sha256": state.get("source_after", {}).get("inventory_sha256"),
            "protected_inventory_start_sha256": state.get("protected_before", {}).get("inventory_sha256"),
            "protected_inventory_end_sha256": state.get("protected_after", {}).get("inventory_sha256"),
            "deferred_nodeids": DEFERRED,
            "collection_total": len(state.get("collection", {}).get("nodeids", [])),
            "deselected_count": len(DEFERRED) if state.get("collection_verified") else 0,
            "selected_count": len(state.get("selected_nodes_expected", [])),
            "test_counts": state.get("test_counts", {}),
            "collection_process": state.get("collection_process", {}),
            "run_process": state.get("run_process", {}),
            "state": state,
        })
        write_report(state)
        write_json(EVIDENCE / "run_state.json", state)
    print(json.dumps({
        "section_status": state["section_status"],
        "collection_total": len(state.get("collection", {}).get("nodeids", [])),
        "selected": len(state.get("selected_nodes_expected", [])),
        "test_counts": state.get("test_counts", {}),
        "duration_seconds": state.get("duration_seconds"),
        "report": str(EVIDENCE / "regresyon_raporu.md"),
    }, ensure_ascii=False), flush=True)
    return 0 if state["section_status"] == "OFFLINE_REGRESSION_PART1_PASS" else 1


PREFLIGHT_NODEIDS = [
    "tests/test_identity_architecture.py::IdentityArchitectureTests::test_js_fallback_defaults_true_when_environment_is_unset",
    "tests/test_identity_architecture.py::IdentityArchitectureTests::test_js_fallback_can_be_explicitly_disabled",
]
BENCHMARK_NODEID = "tests/test_scale_and_validation_package.py::ScaleAndValidationPackageTests::test_benchmark_validator_cli_exit_codes"
ACCURACY_NODEIDS = [
    "tests/test_accuracy_shield_package.py::AccuracyShieldPackageTests::test_contact_query_deep_url_is_kept_as_crawl_seed",
]
P07_NODEID = "tests/test_petzoo_pipeline_acceptance.py::test_p07_negative_http_receipt_survives_a_fresh_process"
PREVIOUS_R3_FAILURE_NODE = P07_NODEID
LONG_TESTS = [
    ("h01", "tests/test_mimari_reaudit_altinci_20260921.py::test_h01_default_pipeline_100_firms_three_workers_has_one_call_per_primary_query", 25 * 60),
    ("k09", "tests/test_mimari_reaudit_altinci_20260921.py::test_k09_budget_100_gives_each_firm_only_first_primary_right", 10 * 60),
    ("p11", "tests/test_petzoo_pipeline_acceptance.py::test_p11_real_137_company_pipeline_worker_1_and_3", 65 * 60),
]
LONG_NODE_WALL_LIMITS = {nodeid: limit for _name, nodeid, limit in LONG_TESTS}
TASK_LIMIT_SECONDS = 6 * 60 * 60
PREFLIGHT_LIMIT_SECONDS = 90
GUARD_PROBE_LIMIT_SECONDS = 4
R2_COLLECTION_LIMIT_SECONDS = 60
CACHE_CONTROL_LIMIT_SECONDS = 15
SHORT_GATE_LIMIT_SECONDS = 180


def r2_runtime_sources(root: Path) -> list[Path]:
    paths = [root / "config.py", root / "main.py", root / "scrape_exhibitors.py"]
    paths.extend(root.glob("requirements*.txt"))
    paths.extend((root / "modules").glob("*.py"))
    return sorted({path.resolve() for path in paths if path.is_file()})


def r2_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    root = root.resolve()
    for path in r2_runtime_sources(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(file_sha256(path)))
    return digest.hexdigest()


def r2_scope_paths() -> tuple[list[str], int]:
    manifest = json.loads(SCOPE_MANIFEST.read_text(encoding="utf-8"))
    paths = {str(row["path"]).replace("\\", "/") for row in manifest.get("files", [])}
    old_scope_count = len(paths)

    test_root = ROOT / "tests"
    for current, directories, filenames in os.walk(test_root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in {"__pycache__", ".pytest_cache"})
        for filename in sorted(filenames):
            if filename.endswith((".pyc", ".pyo")):
                continue
            paths.add((Path(current) / filename).relative_to(ROOT).as_posix())
    for path in ROOT.glob("*.py"):
        if path.is_file():
            paths.add(path.relative_to(ROOT).as_posix())
    for directory in (ROOT / "modules", ROOT / "tools"):
        if directory.exists():
            for path in directory.rglob("*.py"):
                if path.is_file() and "__pycache__" not in path.parts:
                    paths.add(path.relative_to(ROOT).as_posix())
    for pattern in ("requirements*.txt",):
        paths.update(path.relative_to(ROOT).as_posix() for path in ROOT.glob(pattern) if path.is_file())
    for name in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"):
        if (ROOT / name).is_file():
            paths.add(name)

    forbidden = {"input", "state", "data", "runs", "outputs", ".runtime", ".venv"}
    result = []
    for relative in sorted(paths):
        parts = Path(relative).parts
        if Path(relative).is_absolute() or ".." in parts or any(part.lower() in forbidden for part in parts):
            raise RuntimeError(f"unsafe snapshot scope path: {relative}")
        if any(part.lower() in {".env", ".env.local", ".env.production"} for part in parts):
            raise RuntimeError(f"credential/config path excluded from snapshot scope: {relative}")
        source = ROOT / relative
        if not source.is_file():
            raise RuntimeError(f"scope source missing: {relative}")
        attributes = getattr(source.stat(), "st_file_attributes", 0)
        if source.is_symlink() or attributes & 0x400:
            raise RuntimeError(f"reparse/symlink source is not copied: {relative}")
        if not source.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError(f"resolved source escaped workspace root: {relative}")
        result.append(relative)
    return result, old_scope_count


def r2_copy_snapshot() -> dict:
    TEST_ROOT.mkdir(parents=True, exist_ok=False)
    if any(TEST_ROOT.iterdir()):
        raise RuntimeError("source_snapshot is not empty; refusing to overwrite it")
    if not SCOPE_MANIFEST.is_file():
        raise RuntimeError(f"previous source manifest missing: {SCOPE_MANIFEST}")
    live_before = r2_tree_hash(ROOT)
    if live_before != EXPECTED_RUNTIME_SHA256:
        raise RuntimeError(f"live runtime hash mismatch before snapshot: {live_before}")
    relative_paths, manifest_scope_count = r2_scope_paths()
    rows = []
    for relative in relative_paths:
        source = ROOT / Path(relative)
        destination = TEST_ROOT / Path(relative)
        source_before = file_sha256(source)
        size_before = source.stat().st_size
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied_hash = file_sha256(destination)
        source_after = file_sha256(source)
        size_after = source.stat().st_size
        if source_before != source_after or copied_hash != source_before or size_before != size_after:
            raise RuntimeError(f"source changed during copy or snapshot mismatch: {relative}")
        rows.append({"path": relative, "bytes": size_before, "source_sha256": source_before, "snapshot_sha256": copied_hash})
    SNAPSHOT_OUTPUTS.mkdir(parents=False, exist_ok=False)
    output_attributes = getattr(SNAPSHOT_OUTPUTS.stat(), "st_file_attributes", 0)
    if SNAPSHOT_OUTPUTS.is_symlink() or output_attributes & 0x400:
        raise RuntimeError("source_snapshot/outputs must be a real directory, not a symlink/junction")
    if SNAPSHOT_OUTPUTS.resolve().parent != TEST_ROOT.resolve() or any(SNAPSHOT_OUTPUTS.iterdir()):
        raise RuntimeError("source_snapshot/outputs must resolve directly inside the new snapshot and start empty")
    changed_after_copy = [
        row["path"] for row in rows
        if file_sha256(ROOT / Path(row["path"])) != row["source_sha256"]
    ]
    if changed_after_copy:
        raise RuntimeError(f"scope source changed before copy completed: {changed_after_copy[:30]}")
    live_after = r2_tree_hash(ROOT)
    snapshot_runtime = r2_tree_hash(TEST_ROOT)
    if live_after != live_before or snapshot_runtime != EXPECTED_RUNTIME_SHA256:
        raise RuntimeError(
            f"runtime hash mismatch after snapshot: live={live_after}, snapshot={snapshot_runtime}"
        )
    record = {
        "created_at_utc": utc_now(),
        "scope_source_manifest": str(SCOPE_MANIFEST),
        "manifest_used_for": "relative path scope only; previous content hashes were not reused",
        "manifest_path_count": manifest_scope_count,
        "current_extra_path_count": max(0, len(relative_paths) - manifest_scope_count),
        "file_count": len(rows),
        "total_bytes": sum(row["bytes"] for row in rows),
        "source_copy_hashes_match": all(row["source_sha256"] == row["snapshot_sha256"] for row in rows),
        "live_runtime_sha256_before": live_before,
        "live_runtime_sha256_after_copy": live_after,
        "snapshot_runtime_sha256": snapshot_runtime,
        "snapshot_outputs_path": str(SNAPSHOT_OUTPUTS.resolve()),
        "snapshot_outputs_started_empty": True,
        "snapshot_outputs_excluded_from_source_manifest": True,
        "files": rows,
    }
    write_json(EVIDENCE / "snapshot_manifest.json", record)
    return record


def r2_inventory() -> dict:
    rows = []
    digest = hashlib.sha256()
    total_bytes = 0
    paths = []
    for current, directories, filenames in os.walk(TEST_ROOT, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if name not in {"__pycache__", ".pytest_cache"}
            and (Path(current) / name).resolve(strict=False) != SNAPSHOT_OUTPUTS.resolve(strict=False)
        )
        for filename in sorted(filenames):
            if filename.endswith((".pyc", ".pyo")):
                continue
            paths.append(Path(current) / filename)
    for name in ("run_offline_regression.py", "petzoo_offline_plugin.py", "sitecustomize.py"):
        paths.append(EVIDENCE / name)
    for path in sorted(paths, key=lambda item: item.relative_to(EVIDENCE).as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(EVIDENCE).as_posix()
        size = path.stat().st_size
        sha = file_sha256(path)
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(sha))
        rows.append({"path": relative, "bytes": size, "sha256": sha})
        total_bytes += size
    return {
        "captured_at_utc": utc_now(),
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "inventory_sha256": digest.hexdigest(),
        "runtime_tree_sha256": r2_tree_hash(TEST_ROOT),
        "snapshot_outputs_excluded": True,
        "files": rows,
    }


def r2_snapshot_outputs_manifest() -> dict:
    root = SNAPSHOT_OUTPUTS.resolve(strict=True)
    attrs = getattr(SNAPSHOT_OUTPUTS.stat(), "st_file_attributes", 0)
    if SNAPSHOT_OUTPUTS.is_symlink() or attrs & 0x400 or root.parent != TEST_ROOT.resolve():
        raise RuntimeError("snapshot outputs root was redirected or replaced")
    files = []
    unsafe = []
    for current, directories, filenames in os.walk(SNAPSHOT_OUTPUTS, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            child = current_path / name
            attributes = getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
            if child.is_symlink() or attributes & 0x400 or not child.resolve(strict=False).is_relative_to(root):
                unsafe.append(str(child.relative_to(SNAPSHOT_OUTPUTS)))
                directories.remove(name)
        for name in filenames:
            child = current_path / name
            attributes = getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
            if child.is_symlink() or attributes & 0x400 or not child.resolve(strict=False).is_relative_to(root):
                unsafe.append(str(child.relative_to(SNAPSHOT_OUTPUTS)))
                continue
            files.append({
                "path": child.relative_to(SNAPSHOT_OUTPUTS).as_posix(),
                "bytes": child.stat().st_size,
                "sha256": file_sha256(child),
            })
    return {
        "root": str(root),
        "outside_source_manifest": True,
        "files": sorted(files, key=lambda row: row["path"]),
        "unsafe_paths": unsafe,
        "file_count": len(files),
    }


def r2_child_env(phase: str, *, probe_id: str = "") -> tuple[dict[str, str], list[str]]:
    env = dict(os.environ)
    removed = []
    secret_tokens = ("API_KEY", "CLIENT_ID", "CLIENT_SECRET", "ACCESS_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "API_TOKEN", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "AUTHORIZATION")
    for name in list(env):
        upper = name.upper()
        if any(token in upper for token in secret_tokens):
            removed.append(name)
            env.pop(name, None)
    for name in (
        "PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "B2B_RUN_INCIDENT_RECONCILIATION",
        "B2B_TEST_FIXTURE_ROOT", "B2B_PROTECTED_MANIFEST_REFERENCE", "B2B_REAUDIT_EVIDENCE_DIR",
        "B2B_PRODUCTION_TRACE_NODES", "B2B_PRODUCTION_TRACE_JSONL", "B2B_PRODUCTION_TRACE_TARGETS",
        "B2B_PRODUCTION_TRACE_LATE_NODES",
    ):
        if name in env:
            removed.append(name)
            env.pop(name, None)
    # Keep tempfile outputs under this attempt, while pytest owns a disposable
    # child basetemp. Pytest clears basetemp between sessions; session fixtures
    # created by conftest must not live inside that directory. tmp_path remains
    # nested under tempfile.gettempdir() for artifact-boundary validation.
    temp_root = EVIDENCE
    temp_root.mkdir(parents=True, exist_ok=True)
    env.update({
        "B2B_TEST_OFFLINE": "1",
        "B2B_SOCKET_DENY_JSONL": str(EVIDENCE / "network_guard.jsonl"),
        "B2B_TEST_TIMING_LOG": str(EVIDENCE / f"pytest_timing_{phase}.log"),
        "B2B_PYTEST_REPORT_JSONL": str(EVIDENCE / ("preflight_reports.jsonl" if phase == "preflight" else "pytest_reports.jsonl")),
        "B2B_K6_RECEIPT_JSONL": str(EVIDENCE / f"k6_receipts_{phase}.jsonl"),
        "PETZOO_EVIDENCE_DIR": str(EVIDENCE),
        "PETZOO_WORKSPACE_ROOT": str(ROOT.resolve()),
        "PETZOO_SOURCE_SNAPSHOT": str(TEST_ROOT.resolve()),
        "PETZOO_SNAPSHOT_OUTPUTS": str(SNAPSHOT_OUTPUTS.resolve()),
        "PETZOO_DELIVERY_MODULE": str(TEST_ROOT / "tests" / "test_petzoo_pipeline_acceptance.py"),
        "PETZOO_DEFERRED_NODEIDS": json.dumps(DEFERRED if phase == "run" else []),
        "PETZOO_NETWORK_GUARD": "1",
        "PETZOO_FILESYSTEM_GUARD": "1",
        "PETZOO_NETWORK_GUARD_LOG": str(EVIDENCE / "network_guard.jsonl"),
        "PETZOO_FILESYSTEM_GUARD_LOG": str(EVIDENCE / "filesystem_guard.jsonl"),
        "TLDEXTRACT_CACHE": str(DEPENDENCY_CACHE_DIR.resolve()),
        "PETZOO_PHASE": phase,
        "PETZOO_CHILD_PROCESS": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(EVIDENCE),
        "TEMP": str(temp_root), "TMP": str(temp_root), "TMPDIR": str(temp_root),
    })
    if probe_id:
        env["PETZOO_CHILD_PROCESS"] = "1"
        env["PETZOO_GUARD_PROBE"] = probe_id
    return env, sorted(set(removed))


def r3_prepare_dependency_cache() -> dict:
    evidence = EVIDENCE.resolve()
    snapshot = TEST_ROOT.resolve()
    cache = DEPENDENCY_CACHE_DIR.resolve()
    if not cache.is_relative_to(evidence) or cache.is_relative_to(snapshot):
        raise RuntimeError(f"unsafe dependency cache location: {cache}")
    if cache.exists():
        raise RuntimeError(f"dependency cache already exists; refusing reuse or deletion: {cache}")
    cache.mkdir(parents=True, exist_ok=False)
    if any(cache.iterdir()):
        raise RuntimeError("new dependency cache was not empty")
    os.environ["TLDEXTRACT_CACHE"] = str(cache)
    return {
        "path": str(cache),
        "inside_evidence_root": True,
        "outside_source_snapshot": True,
        "started_empty": True,
        "started_file_count": 0,
    }


def r3_cache_files() -> list[dict]:
    rows = []
    if not DEPENDENCY_CACHE_DIR.is_dir():
        return rows
    for path in sorted(DEPENDENCY_CACHE_DIR.rglob("*")):
        if path.is_file():
            rows.append({
                "path": path.relative_to(EVIDENCE).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            })
    return rows


def r3_gate_remaining(gate_started: float) -> float:
    return max(0.0, SHORT_GATE_LIMIT_SECONDS - (time.monotonic() - gate_started))


def r3_cache_control(index: int, gate_started: float, task_started: float, state: dict) -> dict:
    probe_id = f"cache_control_{index}"
    env, _removed = r2_child_env("cache-control", probe_id=probe_id)
    if env.get("TLDEXTRACT_CACHE") != str(DEPENDENCY_CACHE_DIR.resolve()):
        raise RuntimeError("cache-control environment does not point to the isolated cache")
    code = "\n".join([
        "import json, os",
        "from pathlib import Path",
        "import modules.scorer as scorer",
        "snapshot = Path(os.environ['PETZOO_SOURCE_SNAPSHOT']).resolve()",
        "evidence = Path(os.environ['PETZOO_EVIDENCE_DIR']).resolve()",
        "expected = Path(os.environ['TLDEXTRACT_CACHE']).resolve()",
        "actual = Path(scorer._PSL_EXTRACTOR._cache.cache_dir).resolve()",
        "module = Path(scorer.__file__).resolve()",
        "if not module.is_relative_to(snapshot): raise SystemExit('scorer did not import from source_snapshot: ' + str(module))",
        "if actual != expected or not actual.is_relative_to(evidence) or actual.is_relative_to(snapshot): raise SystemExit('resolved extractor cache escaped isolated evidence root: ' + str(actual))",
        "value = scorer.domain_core('https://examplebrand.com.tr/iletisim')",
        "if value != 'examplebrand': raise SystemExit('unexpected domain_core result: ' + repr(value))",
        "print(json.dumps({'pid': os.getpid(), 'domain_core': value, 'extractor_cache_path': str(actual), 'scorer_path': str(module)}, sort_keys=True), flush=True)",
    ])
    remaining = SHORT_GATE_LIMIT_SECONDS - (time.monotonic() - gate_started)
    if remaining <= 0:
        raise RuntimeError("180-second short validation gate expired before cache control")
    result = supervise(
        probe_id, [str(RUNTIME), "-c", code], min(CACHE_CONTROL_LIMIT_SECONDS, remaining), env, task_started,
    )
    state.setdefault("processes", []).append(result)
    state.setdefault("cache_control_processes", []).append(result)
    state["all_process_trees_closed"] = state.get("all_process_trees_closed", True) and bool(result.get("process_tree_closed"))
    append_jsonl(EVIDENCE / "process_supervision.jsonl", result)
    stdout_path = EVIDENCE / f"{probe_id}_stdout.txt"
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
    parsed = None
    try:
        parsed = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        pass
    network = [row for row in read_jsonl(EVIDENCE / "network_guard.jsonl") if row.get("probe_id") == probe_id]
    filesystem = [row for row in read_jsonl(EVIDENCE / "filesystem_guard.jsonl") if row.get("probe_id") == probe_id]
    unexpected_network = [row for row in network if row.get("kind") == "blocked_network"]
    denied_writes = [row for row in filesystem if row.get("kind") == "workspace_write_denied"]
    installed_network = [row for row in network if row.get("kind") == "network_guard_hook_installed"]
    installed_filesystem = [row for row in filesystem if row.get("kind") == "filesystem_guard_hook_installed"]
    valid = (
        result.get("exit_code") == 0 and not result.get("timed_out")
        and result.get("job_assignment") == "success" and result.get("process_tree_closed") is True
        and isinstance(parsed, dict) and parsed.get("domain_core") == "examplebrand"
        and parsed.get("extractor_cache_path") == str(DEPENDENCY_CACHE_DIR.resolve())
        and parsed.get("scorer_path", "").startswith(str(TEST_ROOT.resolve()))
        and not unexpected_network and not denied_writes
        and len(installed_network) == 1 and len(installed_filesystem) == 1
    )
    record = {
        "probe_id": probe_id,
        "passed": valid,
        "result": parsed,
        "network_guard_installed": len(installed_network) == 1,
        "filesystem_guard_installed": len(installed_filesystem) == 1,
        "unexpected_network_blocks": unexpected_network,
        "protected_write_denials": denied_writes,
        "process": result,
    }
    state.setdefault("cache_controls", []).append(record)
    append_jsonl(EVIDENCE / "cache_control_results.jsonl", record)
    if not valid:
        raise RuntimeError(f"isolated tldextract cache control {index} failed; see {probe_id}_stderr.txt and cache_control_results.jsonl")
    return record


def r4_log_contract_probe(mode: str, gate_started: float, task_started: float, state: dict) -> dict:
    probe_id = f"log_contract_{mode}"
    env, _removed = r2_child_env("log-contract", probe_id=probe_id)
    central = (EVIDENCE / "network_guard.jsonl").resolve()
    separate = (EVIDENCE / "log_contract" / "separate_target.jsonl").resolve()
    if mode == "separate":
        if separate.exists():
            raise RuntimeError(f"separate log-contract target already exists; refusing reuse: {separate}")
        env["B2B_SOCKET_DENY_JSONL"] = os.path.relpath(separate, TEST_ROOT.resolve())
        expected_effective = str(separate)
    elif mode == "same":
        env["B2B_SOCKET_DENY_JSONL"] = str(central)
        expected_effective = str(central)
    elif mode == "unset":
        env.pop("B2B_SOCKET_DENY_JSONL", None)
        expected_effective = str(central)
    else:
        raise ValueError(f"unknown log-contract mode: {mode}")
    env["PETZOO_EXPECT_LOG_MODE"] = mode
    env["PETZOO_EXPECT_SOCKET_LOG"] = expected_effective
    code = "\n".join([
        "import json, os, socket, sys",
        "from pathlib import Path",
        "import sitecustomize",
        "mode = os.environ['PETZOO_EXPECT_LOG_MODE']",
        "cwd = Path.cwd().resolve()",
        "raw = os.environ.get('B2B_SOCKET_DENY_JSONL')",
        "actual = str((cwd / raw).resolve()) if raw and not Path(raw).is_absolute() else (str(Path(raw).resolve()) if raw else '')",
        "expected = os.environ['PETZOO_EXPECT_SOCKET_LOG']",
        "if Path(sitecustomize.__file__).resolve().parent != Path(os.environ['PETZOO_EVIDENCE_DIR']).resolve(): raise SystemExit('isolated sitecustomize was not loaded')",
        "if mode == 'unset' and 'B2B_SOCKET_DENY_JSONL' in os.environ: raise SystemExit('unset target unexpectedly present')",
        "if mode != 'unset' and actual != expected: raise SystemExit('explicit target was not resolved as expected: ' + repr(actual))",
        "blocked = False",
        "if mode == 'separate':",
        "    sock = socket.socket()",
        "    try:",
        "        sock.connect(('127.0.0.1', 1))",
        "    except RuntimeError as exc:",
        "        if 'PETZOO_NETWORK_GUARD' not in str(exc): raise",
        "        blocked = True",
        "    finally:",
        "        sock.close()",
        "    if not blocked: raise SystemExit('local socket.connect was not blocked')",
        "print(json.dumps({'pid': os.getpid(), 'mode': mode, 'cwd': str(cwd), 'raw_target': raw, 'effective_target': expected, 'socket_connect_blocked': blocked, 'sitecustomize': str(Path(sitecustomize.__file__).resolve())}, sort_keys=True), flush=True)",
    ])
    remaining = r3_gate_remaining(gate_started)
    if remaining <= 0:
        raise RuntimeError("180-second short gate expired before all log-contract controls")
    result = supervise(
        probe_id, [str(RUNTIME), "-c", code], min(GUARD_PROBE_LIMIT_SECONDS, remaining), env, task_started,
    )
    state.setdefault("processes", []).append(result)
    state.setdefault("log_contract_processes", []).append(result)
    state["all_process_trees_closed"] = state.get("all_process_trees_closed", True) and bool(result.get("process_tree_closed"))
    append_jsonl(EVIDENCE / "process_supervision.jsonl", result)
    stdout_path = EVIDENCE / f"{probe_id}_stdout.txt"
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
    parsed = None
    try:
        parsed = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        pass
    central_rows = [row for row in read_jsonl(central) if row.get("probe_id") == probe_id]
    separate_rows = [row for row in read_jsonl(separate) if row.get("probe_id") == probe_id] if separate.is_file() else []
    target_rows = separate_rows if mode == "separate" else central_rows
    event_kinds = [row.get("kind") for row in target_rows]
    ids = [row.get("guard_event_id") for row in target_rows]
    required_kinds = {"network_guard_hook_installed", "python_process_guard_armed"}
    if mode == "separate":
        required_kinds.add("blocked_network")
    unique_ids = len(ids) == len(set(ids)) and all(ids)
    dual_channel_match = False
    if mode == "separate":
        central_by_id = {row.get("guard_event_id"): row for row in central_rows if row.get("guard_event_id")}
        separate_by_id = {row.get("guard_event_id"): row for row in separate_rows if row.get("guard_event_id")}
        dual_channel_match = set(central_by_id) == set(separate_by_id) and central_by_id == separate_by_id
    no_unexpected_block = (
        sum(row.get("kind") == "blocked_network" for row in target_rows)
        == (1 if mode == "separate" else 0)
        and all(row.get("event") == "socket.connect" for row in target_rows if row.get("kind") == "blocked_network")
    )
    valid = (
        result.get("exit_code") == 0 and not result.get("timed_out")
        and result.get("job_assignment") == "success" and result.get("process_tree_closed") is True
        and isinstance(parsed, dict) and parsed.get("mode") == mode
        and parsed.get("effective_target") == expected_effective
        and parsed.get("socket_connect_blocked") is (mode == "separate")
        and required_kinds.issubset(set(event_kinds))
        and sum(row.get("kind") == "network_guard_hook_installed" for row in target_rows) == 1
        and sum(row.get("kind") == "python_process_guard_armed" for row in target_rows) == 1
        and unique_ids and no_unexpected_block
        and (mode != "separate" or dual_channel_match)
        and (mode != "same" or expected_effective == str(central))
        and (mode != "unset" or not parsed.get("raw_target"))
    )
    record = {
        "probe_id": probe_id,
        "mode": mode,
        "requested_target": env.get("B2B_SOCKET_DENY_JSONL"),
        "effective_target": expected_effective,
        "target_source": "default" if mode == "unset" else "explicit",
        "central_event_count": len(central_rows),
        "child_target_event_count": len(separate_rows) if mode == "separate" else len(central_rows),
        "same_target_event_ids_written_once": unique_ids,
        "dual_channel_events_identical": dual_channel_match if mode == "separate" else None,
        "socket_connect_blocked_before_os": parsed.get("socket_connect_blocked") if parsed else False,
        "passed": valid,
        "result": parsed,
        "central_events": central_rows,
        "target_events": target_rows,
        "process": result,
    }
    state.setdefault("log_contract_controls", []).append(record)
    append_jsonl(EVIDENCE / "log_contract_results.jsonl", record)
    return record


def r4_verify_p07(state: dict) -> dict:
    errors = []
    evidence_root = EVIDENCE / "evidence" / "P07"
    command_paths = sorted(evidence_root.rglob("command.json")) if evidence_root.is_dir() else []
    if len(command_paths) != 1:
        errors.append(f"expected one P07 command.json, found {len(command_paths)}")
        record = {"passed": False, "errors": errors, "command_record_count": len(command_paths)}
        write_json(EVIDENCE / "p07_log_contract.json", record)
        return record
    case_root = command_paths[0].parent
    try:
        command = json.loads(command_paths[0].read_text(encoding="utf-8"))
        result_path = case_root / "result.json"
        network_copy = case_root / "network.jsonl"
        result_payload = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
        network_rows = read_jsonl(network_copy)
    except (OSError, json.JSONDecodeError) as exc:
        command, result_payload, network_rows = {}, None, []
        errors.append(f"cannot read P07 child evidence: {type(exc).__name__}: {exc}")
        result_path, network_copy = case_root / "result.json", case_root / "network.jsonl"
    pid = command.get("child_pid")
    child_records = [row for row in read_jsonl(EVIDENCE / "child_processes.jsonl") if row.get("pid") == pid]
    if len(child_records) != 1:
        errors.append(f"expected one plugin child record for P07 pid={pid}, found {len(child_records)}")
    child_record = child_records[0] if len(child_records) == 1 else {}
    command_id = command.get("command_id") or child_record.get("command_id") or "petzoo-p07-receipt-resume"
    source_log = Path(child_record.get("socket_deny_path", "")) if child_record.get("socket_deny_path") else None
    if command.get("exit_code") != 0 or command.get("timed_out") is not False:
        errors.append("P07 fresh child did not exit successfully without timeout")
    if result_payload != {"result": [None, "http_404"], "physical_transport_called": False}:
        errors.append(f"unexpected P07 child result: {result_payload!r}")
    if child_record.get("command_id") != command_id or child_record.get("nodeid") != P07_NODEID:
        errors.append("plugin child PID/command_id/nodeid record does not match P07 command")
    if child_record.get("socket_deny_path_source") != "explicit":
        errors.append("P07 explicit B2B_SOCKET_DENY_JSONL target was not preserved as explicit")
    if child_record.get("network_guard_log_path") != str((EVIDENCE / "network_guard.jsonl").resolve()):
        errors.append("P07 central PETZOO_NETWORK_GUARD_LOG channel was not kept separate and central")
    if child_record.get("caller_env_unchanged") is not True:
        errors.append("guarded_popen did not preserve the caller's supplied env mapping")
    if not source_log or not source_log.is_file():
        errors.append("P07 effective child network log is missing")
    elif not source_log.resolve().is_relative_to(EVIDENCE.resolve()) or source_log.resolve().is_relative_to(TEST_ROOT.resolve()):
        errors.append("P07 effective child log target is outside the allowed evidence/temp roots")
    if child_record.get("requested_socket_deny_path") and source_log:
        requested_path = Path(child_record["requested_socket_deny_path"])
        if not requested_path.is_absolute():
            requested_path = Path(child_record.get("child_cwd", "")) / requested_path
        if requested_path.resolve(strict=False) != source_log.resolve(strict=False):
            errors.append("P07 requested and effective log targets differ")
    if source_log and network_copy.is_file() and file_sha256(source_log) != file_sha256(network_copy):
        errors.append("P07 copied network log differs from the child's effective log target")
    marker_rows = [row for row in network_rows if row.get("kind") == "guard_armed" and row.get("command_id") == command_id and row.get("pid") == pid and row.get("child") is True]
    if len(marker_rows) != 1:
        errors.append(f"expected one helper guard_armed record for pid={pid}/command_id={command_id}, found {len(marker_rows)}")
    blocked_rows = [row for row in network_rows if row.get("kind") == "blocked_network"]
    if blocked_rows:
        errors.append(f"P07 child log contains {len(blocked_rows)} blocked_network rows; expected zero")
    install_rows = [row for row in network_rows if row.get("kind") == "network_guard_hook_installed" and row.get("pid") == pid]
    central_install_rows = [row for row in read_jsonl(EVIDENCE / "network_guard.jsonl") if row.get("kind") == "network_guard_hook_installed" and row.get("pid") == pid]
    if len(install_rows) != 1 or len(central_install_rows) != 1 or install_rows[0].get("guard_event_id") != central_install_rows[0].get("guard_event_id"):
        errors.append("actual child guard-install event is not represented once in both central and explicit channels")
    artifact_hashes = command.get("artifact_sha256", {})
    if network_copy.is_file() and artifact_hashes.get("network.jsonl") != file_sha256(network_copy):
        errors.append("P07 command artifact hash does not match the child's copied network log")
    record = {
        "passed": not errors,
        "errors": errors,
        "nodeid": P07_NODEID,
        "command_id": command_id,
        "child_pid": pid,
        "requested_socket_deny_path": child_record.get("requested_socket_deny_path"),
        "effective_socket_deny_path": child_record.get("socket_deny_path"),
        "socket_deny_path_source": child_record.get("socket_deny_path_source"),
        "result": result_payload,
        "guard_armed_rows": marker_rows,
        "blocked_network_count": len(blocked_rows),
        "network_log_sha256": file_sha256(network_copy) if network_copy.is_file() else None,
        "network_guard_install_event_id": install_rows[0].get("guard_event_id") if install_rows else None,
        "central_guard_install_event_id": central_install_rows[0].get("guard_event_id") if central_install_rows else None,
        "command_record": command,
        "plugin_child_record": child_record,
    }
    write_json(EVIDENCE / "p07_log_contract.json", record)
    return record


def r4_deduplicate_guard_channels(state: dict) -> dict:
    candidates = [EVIDENCE / "network_guard.jsonl"]
    outside_paths = []
    for row in read_jsonl(EVIDENCE / "child_processes.jsonl"):
        value = row.get("socket_deny_path")
        if value:
            candidates.append(Path(value))
    for row in state.get("log_contract_controls", []):
        if row.get("effective_target"):
            candidates.append(Path(row["effective_target"]))
    p07 = state.get("p07_log_contract", {})
    if p07.get("effective_socket_deny_path"):
        candidates.append(Path(p07["effective_socket_deny_path"]))
    unique_paths = {}
    for path in candidates:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(EVIDENCE.resolve()) or resolved.is_relative_to(TEST_ROOT.resolve()):
            outside_paths.append(str(resolved))
            continue
        unique_paths[os.path.normcase(str(resolved))] = resolved
    unique_events = {}
    rows_scanned = 0
    missing_paths = []
    for path in unique_paths.values():
        if not path.is_file():
            missing_paths.append(str(path))
            continue
        for index, row in enumerate(read_jsonl(path)):
            rows_scanned += 1
            event_id = row.get("guard_event_id")
            pid = row.get("pid")
            if event_id and pid is not None:
                key = (str(pid), str(event_id))
            else:
                key = (str(path), str(index), str(pid))
            current = unique_events.setdefault(key, {"record": row, "channels": [], "occurrences": 0, "payload_mismatch": False})
            current["occurrences"] += 1
            current["channels"].append(str(path))
            current["payload_mismatch"] = current["payload_mismatch"] or current["record"] != row
    duplicate_rows = sum(max(0, item["occurrences"] - 1) for item in unique_events.values())
    duplicate_payloads = [key for key, item in unique_events.items() if item["payload_mismatch"]]
    record = {
        "dedupe_key": "(pid, guard_event_id); rows without an event id remain channel-local",
        "source_paths": [str(path) for path in unique_paths.values()],
        "rows_scanned": rows_scanned,
        "unique_events": len(unique_events),
        "duplicate_channel_rows_ignored": duplicate_rows,
        "payload_mismatch_event_keys": [list(key) for key in duplicate_payloads],
        "outside_allowed_paths": outside_paths,
        "missing_paths": missing_paths,
        "events": [
            {
                "pid": item["record"].get("pid"),
                "guard_event_id": item["record"].get("guard_event_id"),
                "guard_event_sequence": item["record"].get("guard_event_sequence"),
                "kind": item["record"].get("kind"),
                "event": item["record"].get("event"),
                "probe_id": item["record"].get("probe_id"),
                "phase": item["record"].get("phase"),
                "channel_count": item["occurrences"],
            }
            for item in unique_events.values()
        ],
    }
    write_json(EVIDENCE / "network_channel_dedup.json", record)
    return record


def r2_probe_guard(probe_id: str, offline_mode: str, started: float, state: dict, timeout_seconds: float = GUARD_PROBE_LIMIT_SECONDS) -> dict:
    env, _removed = r2_child_env("guard-probe", probe_id=probe_id)
    env.pop("B2B_TEST_OFFLINE", None)
    if offline_mode != "absent":
        env["B2B_TEST_OFFLINE"] = offline_mode
    env["PETZOO_EXPECT_OFFLINE_STATE"] = offline_mode
    code = r'''import os, socket, sys
from pathlib import Path
expected = os.environ["PETZOO_EXPECT_OFFLINE_STATE"]
actual = os.environ.get("B2B_TEST_OFFLINE", "absent")
if actual != expected:
    raise SystemExit("offline environment state mismatch: " + repr(actual))
import config
if not Path(config.__file__).resolve().is_relative_to(Path(os.environ["PETZOO_SOURCE_SNAPSHOT"]).resolve()):
    raise SystemExit("config did not import from source_snapshot")
print("CONFIG_IMPORTED_FROM_SOURCE_SNAPSHOT", flush=True)
probe_write = os.path.join(os.environ["PETZOO_SOURCE_SNAPSHOT"], "__guard_probe_" + os.environ["PETZOO_GUARD_PROBE"] + ".tmp")
try:
    with open(probe_write, "w", encoding="utf-8") as handle:
        handle.write("must never reach disk")
except PermissionError as exc:
    if "PETZOO isolated-run" not in str(exc):
        raise
    print("REAL_FILESYSTEM_AUDIT_HOOK_BLOCKED_WRITE", flush=True)
else:
    raise SystemExit("snapshot write was not blocked by the filesystem audit hook")
s = socket.socket()
try:
    s.connect(("127.0.0.1", 1))
except RuntimeError as exc:
    if "PETZOO_NETWORK_GUARD" not in str(exc):
        raise
    print("REAL_AUDIT_HOOK_BLOCKED_SOCKET_CONNECT", flush=True)
    raise SystemExit(0)
finally:
    s.close()
raise SystemExit("socket.connect was not blocked by the audit hook")
'''
    result = supervise(f"guard_probe_{offline_mode}", [str(RUNTIME), "-c", code], timeout_seconds, env, started)
    state.setdefault("processes", []).append(result)
    append_jsonl(EVIDENCE / "process_supervision.jsonl", result)
    events = read_jsonl(EVIDENCE / "network_guard.jsonl")
    probe_events = [row for row in events if row.get("probe_id") == probe_id]
    installed = [row for row in probe_events if row.get("kind") == "network_guard_hook_installed"]
    filesystem_events = read_jsonl(EVIDENCE / "filesystem_guard.jsonl")
    fs_installed = [row for row in filesystem_events if row.get("probe_id") == probe_id and row.get("kind") == "filesystem_guard_hook_installed"]
    fs_blocked = [row for row in filesystem_events if row.get("probe_id") == probe_id and row.get("kind") == "workspace_write_denied"]
    blocked = [row for row in probe_events if row.get("kind") == "blocked_network" and row.get("event") == "socket.connect"]
    probe_stdout_path = EVIDENCE / f"guard_probe_{offline_mode}_stdout.txt"
    probe_stdout = probe_stdout_path.read_text(encoding="utf-8", errors="replace") if probe_stdout_path.is_file() else ""
    expected_present = offline_mode != "absent"
    expected_value = None if offline_mode == "absent" else offline_mode
    valid = (
        result.get("exit_code") == 0 and not result.get("timed_out")
        and result.get("job_assignment") == "success" and result.get("process_tree_closed") is True
        and len(installed) == 1 and len(fs_installed) == 1 and len(fs_blocked) == 1 and len(blocked) == 1
        and "CONFIG_IMPORTED_FROM_SOURCE_SNAPSHOT" in probe_stdout
        and "REAL_FILESYSTEM_AUDIT_HOOK_BLOCKED_WRITE" in probe_stdout
        and "REAL_AUDIT_HOOK_BLOCKED_SOCKET_CONNECT" in probe_stdout
        and installed[0].get("b2b_test_offline_present") == expected_present
        and installed[0].get("b2b_test_offline_value") == expected_value
        and blocked[0].get("b2b_test_offline_present") == expected_present
        and blocked[0].get("b2b_test_offline_value") == expected_value
    )
    record = {
        "probe_id": probe_id,
        "b2b_test_offline_expected": offline_mode,
        "config_imported_from_snapshot": "CONFIG_IMPORTED_FROM_SOURCE_SNAPSHOT" in probe_stdout,
        "real_sitecustomize_loaded": bool(installed),
        "network_audit_hook_installed": len(installed) == 1,
        "filesystem_audit_hook_installed": len(fs_installed) == 1,
        "snapshot_write_blocked_before_disk": len(fs_blocked) == 1 and result.get("exit_code") == 0,
        "socket_connect_blocked_before_os": len(blocked) == 1 and result.get("exit_code") == 0,
        "process_tree_closed": result.get("process_tree_closed", False),
        "passed": valid,
        "process": result,
        "guard_events": probe_events,
    }
    append_jsonl(EVIDENCE / "guard_probe_results.jsonl", record)
    return record


def _is_reparse_junction(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    is_junction = getattr(path, "is_junction", None)
    return bool(getattr(metadata, "st_file_attributes", 0) & 0x400) and bool(is_junction and is_junction())


def r4_snapshot_outputs_probe(gate_started: float, task_started: float, state: dict) -> dict:
    junction = SNAPSHOT_OUTPUTS / "guard_escape_junction"
    snapshot_real = TEST_ROOT.resolve(strict=True)
    outputs_real = SNAPSHOT_OUTPUTS.resolve(strict=True)
    record = {
        "probe_id": "snapshot_output_boundary",
        "status": "NOT_RUN",
        "command_argv": [],
        "command_exit_code": None,
        "command_timeout_seconds": 15,
        "command_stdout": "",
        "command_stderr": "",
        "process_tree_closed": True,
        "junction_created_and_resolves_to_snapshot": False,
        "junction_removed": False,
        "passed": False,
    }
    process_result = None
    own_path_was_absent = False
    junction_created = False
    config_source = snapshot_real / "config.py"
    config_sha256_before = file_sha256(config_source)
    try:
        if outputs_real.parent != snapshot_real or not outputs_real.is_relative_to(snapshot_real):
            raise RuntimeError("snapshot outputs is not the direct physical outputs child of the isolated snapshot")
        if junction.parent.resolve(strict=True) != outputs_real or not outputs_real.is_relative_to(EVIDENCE.resolve(strict=True)):
            raise RuntimeError("junction path is outside the new snapshot-output temporary area")
        if not snapshot_real.is_relative_to(EVIDENCE.resolve(strict=True)):
            raise RuntimeError("junction target is outside the new evidence snapshot")
        if os.path.lexists(junction):
            raise RuntimeError(f"refusing pre-existing output-boundary junction path: {junction}")
        own_path_was_absent = True
        ps_script = (
            "$ErrorActionPreference = 'Stop'; "
            "New-Item -ItemType Junction -Path $env:PETZOO_PROBE_LINK "
            "-Target $env:PETZOO_PROBE_TARGET | Out-Null"
        )
        command_argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script]
        env, _removed = r2_child_env("snapshot-output-boundary", probe_id="snapshot_output_boundary")
        env["PETZOO_PROBE_LINK"] = str(junction)
        env["PETZOO_PROBE_TARGET"] = str(snapshot_real)
        record.update({
            "status": "RUNNING",
            "command_argv": command_argv,
            "command_env_paths": {"PETZOO_PROBE_LINK": str(junction), "PETZOO_PROBE_TARGET": str(snapshot_real)},
            "command_cwd": str(TEST_ROOT),
            "config_source_sha256_before": config_sha256_before,
            "allowed_temporary_path": str(outputs_real),
            "target_resolved_path": str(snapshot_real),
        })
        command_timeout = min(15.0, remaining_task_seconds(), r3_gate_remaining(gate_started))
        if command_timeout <= 0:
            raise RuntimeError("six-hour deadline or 180-second short gate expired before junction creation")
        record["command_timeout_seconds"] = command_timeout
        command_started = time.monotonic()
        try:
            junction_process = subprocess.run(
                command_argv, cwd=TEST_ROOT, env=env, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=command_timeout,
                check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            record.update({
                "command_exit_code": junction_process.returncode,
                "command_stdout": junction_process.stdout,
                "command_stderr": junction_process.stderr,
                "command_process_wait_completed": True,
            })
        except subprocess.TimeoutExpired as exc:
            def decode_output(value: str | bytes | None) -> str:
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return value or ""
            record.update({
                "command_exit_code": None,
                "command_stdout": decode_output(exc.stdout),
                "command_stderr": decode_output(exc.stderr),
                "command_timed_out": True,
                "command_process_wait_completed": True,
            })
            raise RuntimeError("PowerShell junction command exceeded its bounded timeout") from exc
        record["command_elapsed_seconds"] = round(time.monotonic() - command_started, 3)
        link_is_junction = _is_reparse_junction(junction)
        target_matches = link_is_junction and junction.resolve(strict=True) == snapshot_real
        junction_created = bool(
            junction_process.returncode == 0 and junction.is_dir()
            and link_is_junction and target_matches
        )
        record["junction_lstat_attributes"] = getattr(junction.lstat(), "st_file_attributes", 0) if os.path.lexists(junction) else None
        record["junction_is_reparse_point"] = link_is_junction
        record["junction_target_matches_snapshot"] = target_matches
        record["junction_created_and_resolves_to_snapshot"] = junction_created
        if not junction_created:
            raise RuntimeError("PowerShell returned without creating the verified snapshot junction")
        code = r'''import hashlib, json, os
from pathlib import Path
out = Path(os.environ["PETZOO_SNAPSHOT_OUTPUTS"])
snapshot = Path(os.environ["PETZOO_SOURCE_SNAPSHOT"])
config = snapshot / "config.py"
before = hashlib.sha256(config.read_bytes()).hexdigest()
allowed = out / "allowed_output_probe.txt"
allowed.write_text("allowed generated output\\n", encoding="utf-8")
if allowed.read_text(encoding="utf-8") != "allowed generated output\\n":
    raise SystemExit("write inside snapshot outputs did not persist")
denied = []
def write(path):
    with open(path, "ab") as handle:
        handle.write(b"must be blocked before disk")
def expect_denied(label, action):
    try:
        action()
    except PermissionError as exc:
        if "PETZOO isolated-run" not in str(exc):
            raise
        denied.append(label)
    else:
        raise SystemExit("protected output escape unexpectedly succeeded: " + label)
expect_denied("dotdot_open", lambda: write(out / ".." / "config.py"))
expect_denied("junction_open", lambda: write(out / "guard_escape_junction" / "config.py"))
rename_source = out / "rename_source_probe.txt"
rename_source.write_text("rename source remains on disk\\n", encoding="utf-8")
expect_denied("rename_target", lambda: os.rename(rename_source, out / ".." / "config.py"))
if not rename_source.is_file():
    raise SystemExit("rename escape moved the source before rejection")
after = hashlib.sha256(config.read_bytes()).hexdigest()
if after != before:
    raise SystemExit("protected config changed during boundary probes")
print(json.dumps({"pid": os.getpid(), "allowed_output_sha256": hashlib.sha256(allowed.read_bytes()).hexdigest(), "expected_denials": denied, "config_sha256_before": before, "config_sha256_after": after, "rename_source_retained": True}, sort_keys=True), flush=True)
'''
        remaining = r3_gate_remaining(gate_started)
        if remaining <= 0:
            raise RuntimeError("180-second short gate expired before snapshot output path probe")
        process_result = supervise(
            "snapshot_output_boundary", [str(RUNTIME), "-c", code],
            min(GUARD_PROBE_LIMIT_SECONDS, remaining), env, task_started,
        )
        state.setdefault("processes", []).append(process_result)
        append_jsonl(EVIDENCE / "process_supervision.jsonl", process_result)
        stdout_path = EVIDENCE / "snapshot_output_boundary_stdout.txt"
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
        parsed = None
        try:
            parsed = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            pass
        fs_rows = [row for row in read_jsonl(EVIDENCE / "filesystem_guard.jsonl") if row.get("probe_id") == "snapshot_output_boundary"]
        denials = [row for row in fs_rows if row.get("kind") == "workspace_write_denied"]
        expected_events = {"dotdot_open": "open", "junction_open": "open", "rename_target": "os.rename"}
        valid_denials = all(any(row.get("event") == expected_events[label] for row in denials) for label in expected_events)
        record.update({
            "passed": (
                process_result.get("exit_code") == 0 and not process_result.get("timed_out")
                and process_result.get("job_assignment") == "success"
                and process_result.get("process_tree_closed") is True
                and parsed is not None
                and parsed.get("expected_denials") == ["dotdot_open", "junction_open", "rename_target"]
                and parsed.get("config_sha256_before") == parsed.get("config_sha256_after")
                and parsed.get("rename_source_retained") is True
                and len(denials) == 3 and valid_denials
            ),
            "child_result": parsed,
            "write_denials": denials,
            "process": process_result,
        })
        state["expected_snapshot_output_escape_blocks"] = denials
    except Exception as exc:
        record["probe_error"] = f"{type(exc).__name__}: {exc}"
        if record.get("command_timed_out"):
            record["status"] = "TIMEOUT"
        elif process_result is not None:
            record["status"] = "PASS" if record.get("passed") else "FAIL"
        else:
            record["status"] = "NOT_RUN"
    finally:
        if os.path.lexists(junction):
            try:
                cleanup_is_ours = (
                    own_path_was_absent and _is_reparse_junction(junction)
                    and junction.resolve(strict=True) == snapshot_real
                )
                if cleanup_is_ours:
                    os.rmdir(junction)
                    record["junction_removed"] = not os.path.lexists(junction)
                else:
                    record["junction_removed"] = False
                    record["junction_cleanup_error"] = "retained: link identity/type/target was not the verified temporary junction"
            except OSError as exc:
                record["junction_removed"] = False
                record["junction_cleanup_error"] = repr(exc)
        else:
            record["junction_removed"] = True
        try:
            config_sha256_after = file_sha256(config_source)
            record["config_source_sha256_after"] = config_sha256_after
            record["config_source_sha256_unchanged"] = config_sha256_after == config_sha256_before
        except OSError as exc:
            record["config_source_sha256_after_error"] = repr(exc)
            record["config_source_sha256_unchanged"] = False
        if process_result is not None:
            record["process_tree_closed"] = process_result.get("process_tree_closed", False)
            state["all_process_trees_closed"] = state.get("all_process_trees_closed", True) and bool(process_result.get("process_tree_closed"))
        state["all_process_trees_closed"] = state.get("all_process_trees_closed", True) and bool(record.get("command_process_wait_completed", True))
        if record.get("passed") and record.get("junction_removed") and record.get("config_source_sha256_unchanged"):
            record["status"] = "PASS"
        elif record.get("status") == "RUNNING":
            record["status"] = "FAIL"
        state["snapshot_output_probe"] = record
        append_jsonl(EVIDENCE / "snapshot_output_probe_results.jsonl", record)
        write_json(EVIDENCE / "snapshot_output_probe.json", record)
    if not record.get("passed") or not record.get("junction_removed"):
        raise RuntimeError("snapshot outputs access/escape boundary probe failed")
    return record


def r4_verify_benchmark_cli(state: dict) -> dict:
    errors = []
    result_path = EVIDENCE / "benchmark_cli_results.json"
    try:
        rows = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        rows = []
        errors.append(f"benchmark subprocess output capture unavailable: {type(exc).__name__}: {exc}")
    labels = [row.get("label") for row in rows]
    expected_codes = {"public": 0, "private_missing": 2}
    if len(rows) != 2 or sorted(labels) != sorted(expected_codes):
        errors.append(f"expected both real benchmark CLI invocations, got labels={labels!r}")
    for row in rows:
        label = row.get("label")
        argv = row.get("argv", [])
        if label not in expected_codes:
            continue
        if row.get("returncode") != expected_codes[label]:
            errors.append(f"{label} benchmark CLI returned {row.get('returncode')}, expected {expected_codes[label]}")
        if len(argv) < 3 or argv[1] != "validate_benchmark_suite.py" or argv[2] != "--manifest":
            errors.append(f"{label} did not execute the expected validator CLI argv: {argv!r}")
        if label == "public" and "private_seen_gate: NOT_REQUESTED" not in row.get("stdout", ""):
            errors.append("public benchmark CLI output lacks its real NOT_REQUESTED result")
        if label == "private_missing" and (
            "--private-seen-workbook" not in argv or "missing.xlsx" not in argv
        ):
            errors.append("private-workbook negative benchmark invocation did not use its original expected arguments")
    output_file = SNAPSHOT_OUTPUTS / "benchmark_validation.json"
    if not output_file.is_file():
        errors.append(f"validator did not create its default JSON output: {output_file}")
        output_sha256 = None
    else:
        output_sha256 = file_sha256(output_file)
    record = {
        "passed": not errors,
        "errors": errors,
        "nodeid": BENCHMARK_NODEID,
        "calls": rows,
        "snapshot_output_path": str(output_file),
        "snapshot_output_sha256": output_sha256,
    }
    write_json(EVIDENCE / "benchmark_cli_verification.json", record)
    state["benchmark_cli_verification"] = record
    return record


def r2_pytest_argv(phase: str, extra: list[str]) -> list[str]:
    return [
        str(RUNTIME), "-m", "pytest", "-q", "-p", "petzoo_offline_plugin",
        "-c", str(TEST_ROOT / "pytest.ini"),
        "-o", f"cache_dir={EVIDENCE / 'pytest_cache'}",
        "--basetemp", str(EVIDENCE / "pytest_tmp"),
        *extra,
    ]


def r2_result_counts(report_path: Path) -> dict:
    return classify_reports(read_jsonl(report_path))


def r2_phase_result_counts(report_path: Path, phase: str) -> dict:
    rows = [row for row in read_jsonl(report_path) if row.get("pytest_phase") == phase]
    return classify_reports(rows)


def r2_write_report(state: dict) -> None:
    collection = state.get("collection", {}).get("nodeids", [])
    selected = state.get("selected_nodeids", [])
    counts = state.get("test_counts", {})
    preflight_counts = state.get("preflight_test_counts", {})
    cache_rows = state.get("cache_controls", [])
    log_rows = state.get("log_contract_controls", [])
    long_results = state.get("long_test_results", [])
    if not long_results:
        long_results = [
            {"name": name, "nodeid": nodeid, "status": "NOT_RUN"}
            for name, nodeid, _limit in LONG_TESTS
        ]
    preflight_summary = (
        f"{preflight_counts.get('passed', 0)} pass / {preflight_counts.get('failed', 0)} fail / "
        f"{preflight_counts.get('errors', 0)} error / {preflight_counts.get('skipped', 0)} skip "
        f"({preflight_counts.get('reported_nodes', 0)}/{len(ACCURACY_NODEIDS) + len(PREFLIGHT_NODEIDS) + 2} reported)"
        if state.get("preflight_process") else "NOT_RUN"
    )
    normal_process = state.get("run_process", {})
    normal_status = (
        "PASS" if state.get("normal_gate_passed") else
        "TIMEOUT" if normal_process.get("timed_out") else
        "FAIL" if normal_process else "NOT_RUN"
    )
    def control_status(rows: list[dict], expected: int) -> str:
        if not rows:
            return "NOT_RUN"
        if any(row.get("timed_out") or row.get("status") == "TIMEOUT" for row in rows):
            return "TIMEOUT"
        passed = sum(bool(row.get("passed")) for row in rows)
        if passed == expected and len(rows) == expected:
            return "PASS"
        return f"FAIL/PARTIAL ({passed}/{expected}; {len(rows)} executed)"

    def test_stage_status(process: dict, stage_counts: dict, expected: int) -> str:
        if not process:
            return "NOT_RUN"
        if process.get("timed_out"):
            return "TIMEOUT"
        if (
            process.get("exit_code") == 0 and stage_counts.get("passed") == expected
            and stage_counts.get("reported_nodes") == expected
            and not stage_counts.get("failed") and not stage_counts.get("errors")
            and not stage_counts.get("skipped")
        ):
            return "PASS"
        return "FAIL"

    accounted = sum(int(counts.get(key, 0)) for key in ("passed", "failed", "errors", "skipped"))
    unresolved = max(0, len(selected) - accounted)
    events = read_jsonl(EVIDENCE / "node_events.jsonl")
    phases = {"preflight": r2_result_counts(EVIDENCE / "preflight_reports.jsonl"), "run": counts}
    phases.update({
        str(result.get("name")): result.get("counts", {})
        for result in long_results if isinstance(result, dict)
    })
    failures = []
    for name, phase_counts in phases.items():
        for row in phase_counts.get("failed_nodes", []):
            failures.append((name, row))
    lines = [
        "# PETZOO offline kapanış — snapshot çıktısı izolasyonu",
        "",
        f"**Bölüm sonucu:** `{state.get('section_status', 'BLOCKED_WITH_EVIDENCE')}`  ",
        "**Genel proje durumu:** `IMPLEMENTED_NOT_ACCEPTED`  ",
        f"**Toplam süre:** {state.get('duration_seconds', 0):.3f} sn  ",
        f"**Altı saat deadline:** başlangıç `{state.get('task_started_at_utc', '')}`; bu denemede kalan `{state.get('task_remaining_seconds_at_start', 'NOT_RECORDED')}` sn  ",
        f"**Runtime hash (canlı başlangıç / snapshot / bitiş):** `{state.get('runtime_live_start', '')}` / `{state.get('runtime_snapshot_start', '')}` / `{state.get('runtime_snapshot_end', '')}`",
        "",
        "## Önceki engel ve bu turdaki dar düzeltme",
        "",
        f"R4'teki ilk geniş koşu `tests/test_scale_and_validation_package.py::ScaleAndValidationPackageTests::test_benchmark_validator_cli_exit_codes` içinde validator'ın varsayılan `{TEST_ROOT / 'outputs' / 'benchmark_validation.json'}` çıktısı için oluşturduğu `outputs` dizini genel snapshot yazma kuralınca engellendi. Bu üretim validator hatası değildi. R5 harness, yalnız yeni snapshot'ın boş `outputs/` altını izinli generated-output alanı yaptı; çözülmüş yol sınırı ve dotdot/junction/rename kaçışları guard ile reddedilir. Test assertion'ı ve gerçek CLI argv'si değiştirilmedi.",
        f"Harness canonical olarak `tools/petzoo_offline/` altında tutuldu; test koşusu aynı SHA-256'lı kopyayı kullandı. Önceki R3 cache izolasyonu, R4 child `B2B_SOCKET_DENY_JSONL` explicit hedefi, merkezi log ayrı kanalı/çift-kanal event-id tekilleştirmesi ve OFFLINE/JS ortam sözleşmesi korundu. Test alt süreç yorumlayıcısı `sys.executable`.",
        "",
        "## İzolasyon ve guard doğrulaması",
        "",
        f"- Snapshot: `{TEST_ROOT}`; eski manifest yalnız göreli dosya kapsamı olarak kullanıldı, eski hash'ler taşınmadı.",
        f"- Snapshot dosya sayısı/bayt: {state.get('snapshot_copy', {}).get('file_count', 0)} / {state.get('snapshot_copy', {}).get('total_bytes', 0)}.",
        f"- Güncel kaynak kopyası sırasında kaynak/snapshot hash eşleşmesi: {state.get('snapshot_copy', {}).get('source_copy_hashes_match', False)}.",
        f"- Snapshot `outputs/` ayrı ve başlangıçta boş: {state.get('snapshot_copy', {}).get('snapshot_outputs_started_empty', False)}; gerçek dizin / symlink-junction değil; source manifestinden ve source inventory'den hariç. Üretilen çıktıların ayrı manifesti `{state.get('snapshot_outputs_manifest', {}).get('file_count', 0)} dosya`, güvensiz yol={len(state.get('snapshot_outputs_manifest', {}).get('unsafe_paths', []))}; `snapshot_outputs_manifest.json`.",
        f"- Çıktı sınır probu: {state.get('snapshot_output_probe', {}).get('status', 'NOT_RUN')}; junction oluşturma={state.get('snapshot_output_probe', {}).get('junction_created_and_resolves_to_snapshot', False)}; kaçış reddi={len(state.get('expected_snapshot_output_escape_blocks', []))}/3 (probe çalışmadıysa `NOT_RUN`); korunan kaynak hash aynı={state.get('snapshot_output_probe', {}).get('config_source_sha256_unchanged', 'NOT_RUN')}; geçici junction kaldırıldı={state.get('snapshot_output_probe', {}).get('junction_removed', 'NOT_RUN')}.",
        f"- Cache: `{state.get('dependency_cache', {}).get('path', DEPENDENCY_CACHE_DIR)}`; başlangıçta boş: {state.get('dependency_cache', {}).get('started_empty', False)}; snapshot dışında/kanıt kökü içinde: {state.get('dependency_cache', {}).get('outside_source_snapshot', False)}/{state.get('dependency_cache', {}).get('inside_evidence_root', False)}.",
        f"- Guard kontrolleri (OFFLINE absent / 0 / 1): {control_status(state.get('guard_probes', []), 3)}.",
        "- Her kontrol gerçek `sitecustomize.py` yükleyip yerel `127.0.0.1:1` `socket.connect` audit olayını OS bağlantısından önce engellemeyi sınadı; bu üç beklenen engel genel ağ ihlali sayılmadı.",
        f"- Aynı kontrollerde snapshot yazma engeli: {len(state.get('expected_probe_workspace_blocks', []))}/3; guard probe yoksa `NOT_RUN` (kanıt `guard_probe_results.jsonl`).",
        f"- Python cache kontrolleri: {control_status(cache_rows, 2)}; sonuç eşitliği={cache_rows[0].get('result') == cache_rows[1].get('result') if len(cache_rows) == 2 and all(row.get('result') is not None for row in cache_rows) else 'NOT_RUN'}; süreç/PID/path kanıtı `cache_control_results.jsonl`.",
        f"- `B2B_SOCKET_DENY_JSONL` bootstrap kontrolleri: {control_status(log_rows, 3)}; yürütülen hedef kayıtları={[(row.get('mode'), row.get('requested_target'), row.get('effective_target'), row.get('target_source')) for row in log_rows] if log_rows else 'NOT_RUN'}; `log_contract_results.jsonl`.",
        f"- Ayrı hedefte gerçek `socket.connect` engeli: {len(state.get('expected_log_contract_network_events', []))}/1{'' if log_rows else ' — NOT_RUN'}; kanal birleştirme kanıtı={state.get('guard_channel_dedup', {}).get('unique_events', 'NOT_RUN')} tekil olay (dedupe yoksa `NOT_RUN`).",
        f"- P07 kısa doğrulaması `{P07_NODEID}`: {'PASS' if state.get('p07_log_contract', {}).get('passed') else ('FAIL' if state.get('p07_log_contract', {}).get('errors') else 'NOT_RUN')}; PID={state.get('p07_log_contract', {}).get('child_pid', 'NOT_RUN')}; command_id=`{state.get('p07_log_contract', {}).get('command_id', 'NOT_RUN')}`; istenen/effective log=`{state.get('p07_log_contract', {}).get('requested_socket_deny_path', 'NOT_RUN')}` / `{state.get('p07_log_contract', {}).get('effective_socket_deny_path', 'NOT_RUN')}`; sonuç `{state.get('p07_log_contract', {}).get('result', 'NOT_RUN')}`; blocked_network={state.get('p07_log_contract', {}).get('blocked_network_count', 'NOT_RUN')}.",
        f"- Ön test node'ları (yalnız yetkilendirilen beş): `{BENCHMARK_NODEID}`, `{P07_NODEID}`, `{PREFLIGHT_NODEIDS[0]}`, `{PREFLIGHT_NODEIDS[1]}`, `{ACCURACY_NODEIDS[0]}`.",
        f"- Kısa kapı: {'PASS' if state.get('short_gate_passed') else ('FAIL' if state.get('short_gate_seconds') is not None else 'NOT_RUN')}; süre={state.get('short_gate_seconds', 'NOT_RUN')} / 180 sn; preflight test sayımı={preflight_summary}.",
        f"- Benchmark public/missing-workbook CLI gerçek çağrıları: {('PASS' if state.get('benchmark_cli_verification', {}).get('passed') else ('FAIL' if state.get('benchmark_cli_verification', {}).get('errors') else 'NOT_RUN'))}; exit kodları={[(row.get('label'), row.get('returncode')) for row in state.get('benchmark_cli_verification', {}).get('calls', [])] if state.get('benchmark_cli_verification') else 'NOT_RUN'}; çıktı SHA-256=`{state.get('benchmark_cli_verification', {}).get('snapshot_output_sha256', 'NOT_RUN')}`.",
        f"- İlgili child env + gerçek network/filesystem hook doğrulamaları: {state.get('preflight_child_env_checks', [])}.",
        f"- Kısa kapıdaki beklenmeyen ağ blokları/korunan yazma engelleri: {len(state.get('unexpected_short_gate_network_blocks', []))}/{len(state.get('unexpected_short_gate_write_denials', []))}; izinli sınır testi dışında beklenmeyen yazma yok.",
        f"- Çocuk süreçlerde B2B_TEST_OFFLINE değer/yokluk ve bağımsız guard: `child_processes.jsonl`; ağ olayları: `{state.get('network_log_sha256', '')}` ({state.get('network_guard_counts', {})}).",
        f"- Korunan workspace yazma denemesi sayısı (engellendi): {len(state.get('blocked_workspace_writes', []))}; kayıt hash'i `{state.get('filesystem_log_sha256', '')}`.",
        f"- Snapshot test-tree file-access kayıtları: {state.get('snapshot_test_access_count', 0)}; log başlangıç/bitiş SHA-256 `{state.get('filesystem_log_start_sha256', '')}` / `{state.get('filesystem_log_sha256', '')}`.",
        "- Gerçek workspace input/state/data/runs/output ve önceki teslim klasörleri kopyalanmadı. Workspace çapında büyüyen DB/log dosyaları hash'lenmedi; kullanıcı koşusuna dokunulmadı veya PID taraması/sonlandırması yapılmadı.",
        "",
        "## Geniş regresyon sayımları",
        "",
        f"- Collection: {'PASS' if state.get('collection_process', {}).get('exit_code') == 0 else ('TIMEOUT' if state.get('collection_process', {}).get('timed_out') else ('FAIL' if state.get('collection_process') else 'NOT_RUN'))}; node={len(collection)}; baseline 1105 korunma/eşitlik={state.get('collection_matches_previous', 'NOT_RUN')}; eklenen={len(state.get('collection_diff', {}).get('added', []))}.",
        f"- Normal bölüm: {normal_status}; seçili={len(selected) if state.get('collection_process') else 'NOT_RUN'}; pass={counts.get('passed', 'NOT_RUN')}, fail={counts.get('failed', 'NOT_RUN')}, error={counts.get('errors', 'NOT_RUN')}, skip={counts.get('skipped', 'NOT_RUN')}, unresolved={unresolved if state.get('run_process') else 'NOT_RUN'}.",
        f"- Normal bölüm başlama/tamamlanma: {len(set(state.get('run_started_nodeids', []))) if state.get('run_process') else 'NOT_RUN'} / {counts.get('completed_nodes', 'NOT_RUN')}; son tamamlanan `{state.get('last_completed_node', 'NOT_RUN')}`.",
        *[
            f"- Zorunlu uzun test `{result.get('nodeid')}`: `{result.get('status', 'NOT_RUN')}`; limit={result.get('wall_limit_seconds', 'NOT_RUN')} sn; pass={result.get('counts', {}).get('passed', 'NOT_RUN')}, fail={result.get('counts', {}).get('failed', 'NOT_RUN')}, error={result.get('counts', {}).get('errors', 'NOT_RUN')}, skip={result.get('counts', {}).get('skipped', 'NOT_RUN')}."
            for result in long_results
        ],
        f"- Collection süresi/exit: {state.get('collection_process', {}).get('duration_seconds')} sn / {state.get('collection_process', {}).get('exit_code')}.",
        f"- Geniş test süresi/exit/timeout: {state.get('run_process', {}).get('duration_seconds')} sn / {state.get('run_process', {}).get('exit_code')} / {state.get('run_process', {}).get('timed_out')}.",
        f"- Son tamamlanan node: `{state.get('last_completed_node', '')}`; son aşama: `{state.get('last_completed_stage', '')}`; kesilme anındaki node: `{state.get('active_node_at_stop', '')}`.",
        f"- Bu koşunun Job Object süreç ağaçları kapandı: {state.get('all_process_trees_closed', False)}. Kullanıcıya ait PID'ler incelenmedi/durdurulmadı.",
        "",
        "## Bütünlük kanıtı",
        "",
        f"- Runtime SHA-256: başlangıç `{state.get('runtime_live_start', '')}`, snapshot başlangıç `{state.get('runtime_snapshot_start', '')}`, snapshot bitiş `{state.get('runtime_snapshot_end', '')}`.",
        f"- Snapshot kaynak/test/runner envanteri eşleşti: {state.get('source_inventory_match', False)}; başlangıç `{state.get('source_inventory_before', {}).get('inventory_sha256', '')}`, bitiş `{state.get('source_inventory_after', {}).get('inventory_sha256', '')}`.",
        f"- Runtime + pytest/test/helper + runner/plugin SHA envanteri başlangıç/son dosya sayısı: {state.get('source_inventory_before', {}).get('file_count', 0)} / {state.get('source_inventory_after', {}).get('file_count', 0)}.",
        f"- Harness SHA-256: `{r2_harness_hashes()}`.",
        f"- Kaynak/test/helper/harness workspace dosyaları snapshot kopyasından sonra değişmedi: {state.get('source_workspace_unchanged_after_copy', False)}; değişen={len(state.get('source_workspace_changes_after_copy', []))}, eksik={len(state.get('source_workspace_missing_after_copy', []))}. Aday hashleri `candidate_hashes_start.json`/`candidate_hashes_end.json`; açık commit listesi `commit_scope.json`.",
        f"- Üretilen tldextract cache dosyaları (kaynak manifestinden ayrı): {len(state.get('dependency_cache_manifest', {}).get('files', []))}; liste/hash `dependency_cache_manifest.json`.",
        f"- P07 log-contract doğrulama ayrıntısı: `p07_log_contract.json`; çoklu-kanal event dedupe: `network_channel_dedup.json`.",
        "- Kod değişiklikleri yalnız kanonik harness ve taşınabilir test-runtime yolu içindir; üretim mantığı değişmedi. Genel gerçek-firma başarı oranı bu testle kanıtlanmadı; genel durum `IMPLEMENTED_NOT_ACCEPTED`.",
        "",
        "## Hata ve skip ayrıntısı",
        "",
    ]
    if not state.get("collection_matches_previous", False) and state.get("collection_diff"):
        diff = state["collection_diff"]
        lines.extend([f"Collection listesi farklı: added={len(diff.get('added', []))}, removed={len(diff.get('removed', []))}, order_changed={diff.get('order_changed', False)}."])
        lines.extend([f"- added `{node}`" for node in diff.get("added", [])[:100]])
        lines.extend([f"- removed `{node}`" for node in diff.get("removed", [])[:100]])
    for phase_name, failure in failures:
        nodeid = failure["nodeid"]
        lines.extend(["", f"### {phase_name}: `{nodeid}` ({', '.join(failure['phases'])})"])
        matching = [row for row in events if row.get("event") == "phase_report" and row.get("pytest_phase") == phase_name and row.get("nodeid") == nodeid and row.get("longrepr")]
        if matching:
            for event in matching:
                lines.extend(["", "```text", event["longrepr"], "```"])
        else:
            lines.append("Tam traceback ilgili phase report JSONL ham kaydında mevcut.")
    for control in state.get("log_contract_controls", []):
        if not control.get("passed"):
            mode = control.get("mode", "unknown")
            stderr_path = EVIDENCE / f"log_contract_{mode}_stderr.txt"
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
            lines.extend(["", f"### Log sözleşmesi kontrol hatası: `{mode}`", "", "```text", stderr or json.dumps(control, ensure_ascii=False, indent=2), "```"])
    if state.get("p07_log_contract", {}).get("errors"):
        lines.extend(["", "### P07 log kanıtı doğrulama hataları", "", *[f"- {error}" for error in state["p07_log_contract"]["errors"]]])
    if state.get("benchmark_cli_verification", {}).get("errors"):
        lines.extend(["", "### Benchmark CLI doğrulama hataları", "", *[f"- {error}" for error in state["benchmark_cli_verification"]["errors"]]])
    if not state.get("snapshot_output_probe", {}).get("passed", False):
        lines.extend(["", "### Snapshot outputs sınır probu hatası", "", "```text", json.dumps(state.get("snapshot_output_probe", {}), ensure_ascii=False, indent=2), "```"])
    for control in state.get("cache_controls", []):
        if not control.get("passed"):
            stderr_path = EVIDENCE / f"{control.get('probe_id', 'cache_control')}_stderr.txt"
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
            lines.extend(["", f"### Cache kontrol hatası: `{control.get('probe_id')}`", "", "```text", stderr or json.dumps(control, ensure_ascii=False, indent=2), "```"])
    if not failures:
        lines.append("Koşulan pytest aşamalarında assertion traceback yok." if state.get("preflight_process") or state.get("run_process") else "Pytest aşamaları `NOT_RUN`; collection/çalıştırma başlamadı.")
    for phase_name, phase_counts in phases.items():
        for row in phase_counts.get("skipped_nodes", []):
            lines.append(f"- Normal skip `{phase_name}` `{row['nodeid']}` ({row['phase']}): {row.get('reason') or '<reason not recorded>'}; wasxfail=`{row.get('wasxfail', '')}`")
    other_network = state.get("non_probe_network_blocks", [])
    lines.extend(["", "Diğer (üç guard probu dışı) engellenen network audit olayları; gerçek çıkış değildir:"])
    if other_network:
        lines.extend([f"- `{row.get('phase')}` `{row.get('nodeid')}` event=`{row.get('event')}` pid={row.get('pid')}" for row in other_network[:300]])
    else:
        lines.append("- Yok.")
    if state.get("blocked_workspace_writes"):
        lines.extend(["", "Engellenen workspace/snapshot yazma denemeleri:"])
        lines.extend([f"- `{row.get('event')}` `{row.get('path')}` pid={row.get('pid')} node=`{row.get('nodeid')}`" for row in state["blocked_workspace_writes"][:200]])
    if state.get("fatal_error"):
        lines.extend(["", "Koşucu/ön kontrol hatası:", "", "```text", state["fatal_error"], "```"])
    lines.extend([
        "",
        "## Ham kayıtlar",
        "",
        "- `git_baseline.json`, `commit_scope.json`, `candidate_hashes_start.json`, `candidate_hashes_end.json`, `source_workspace_final_comparison.json`: başlangıç Git/index/remote, explicit kapsam ve çalışma kopyası hash uzlaşımı.",
        "- `snapshot_manifest.json`: eski manifest path-scope + güncel içerik kopyalama kaynak/hedef SHA-256.",
        "- `snapshot_outputs_initial_manifest.json`, `snapshot_outputs_manifest.json`, `snapshot_output_probe.json`, `snapshot_output_probe_results.jsonl`: ayrı generated-output alanı ve sınır/escape kanıtı.",
        "- `source_inventory_before.json`, `source_inventory_after.json`, `source_inventory_comparison.json`: snapshot ve runner/plugin bütünlüğü.",
        "- `guard_probe_results.jsonl`, `network_guard.jsonl`, `filesystem_guard.jsonl`, `child_processes.jsonl`: gerçek guard ve erişim/yazma kayıtları.",
        "- `dependency_cache_manifest.json`, `dependency_cache_after_controls.json`, `cache_control_results.jsonl`: başlangıç/son cache dosyaları ve iki yeni-process sonucu.",
        "- `log_contract_{separate,same,unset}_{stdout,stderr}.txt`, `log_contract_results.jsonl`, `p07_log_contract.json`, `network_channel_dedup.json`: log hedefi kontrolleri, P07 PID/command id/result ve tekilleştirilmiş guard olayları.",
        "- `benchmark_cli_*_stdout.txt`, `benchmark_cli_*_stderr.txt`, `benchmark_cli_results.json`, `benchmark_cli_verification.json`: iki gerçek validator CLI çağrısı; public exit 0, missing workbook exit 2.",
        "- `preflight_stdout.txt`, `preflight_stderr.txt`, `preflight_reports.jsonl`, `preflight_junit.xml`: benchmark, P07, iki JS node'u ve tek accuracy node'u.",
        "- `collection_nodes.json`, `collection_diff.json`, `selected_nodes.json`: collection sırası ve seçim.",
        "- `collect_stdout.txt`, `collect_stderr.txt`, `run_stdout.txt`, `run_stderr.txt`, `node_events.jsonl`, `pytest_reports.jsonl`, `junit.xml`: canlı geniş koşu çıktıları.",
        "- `run_command.json`, `process_supervision.jsonl`, `watchdog_progress.jsonl`, `run_state.json`: argv/env politikası, süreler ve süreç ağacı kanıtı.",
        "",
        f"Rapor yazım zamanı: {utc_now()} UTC.",
    ])
    report_text = "\n".join(lines) + "\n"
    (EVIDENCE / "regresyon_raporu.md").write_text(report_text, encoding="utf-8")
    if EVIDENCE_ROOT != EVIDENCE:
        (EVIDENCE_ROOT / "FINAL_RAPOR.md").write_text(report_text, encoding="utf-8")


def r2_main() -> int:
    if os.name != "nt":
        raise SystemExit("Windows Job Object is required")
    started = time.monotonic()
    state = {
        "started_at_utc": utc_now(),
        "task_started_at_utc": TASK_START_UTC,
        "task_deadline_qpc": TASK_DEADLINE_QPC,
        "task_deadline_qpc_frequency": TASK_DEADLINE_FREQUENCY,
        "task_budget_seconds": TOTAL_BUDGET_SECONDS,
        "task_remaining_seconds_at_start": round(remaining_task_seconds(), 3),
        "section_status": "BLOCKED_WITH_EVIDENCE",
        "fatal_error": "",
        "all_process_trees_closed": True,
        "guard_probes": [],
        "processes": [],
        "collection_matches_previous": False,
        "runtime_authorized_base_sha256": INITIAL_RUNTIME_SHA256,
        "runtime_expected_candidate_sha256": EXPECTED_RUNTIME_SHA256,
    }
    write_json(EVIDENCE / "run_state.json", state)
    try:
        if remaining_task_seconds() <= 0:
            raise RuntimeError("the persistent six-hour task deadline has expired")
        if not RUNTIME.is_file():
            raise RuntimeError(f"required runtime executable missing: {RUNTIME}")
        if r2_tree_hash(ROOT) != EXPECTED_RUNTIME_SHA256:
            raise RuntimeError("live runtime source hash does not match the authorized source")
        state["runtime_live_start"] = r2_tree_hash(ROOT)
        state["runtime_hash_change_reason"] = (
            "modules/checkpoint.py now refreshes affected dispatch rounds atomically when a provider query flight receives its terminal receipt"
            if EXPECTED_RUNTIME_SHA256 != INITIAL_RUNTIME_SHA256 else "none"
        )
        write_commit_scope_manifest(state)
        state["run_options"] = {
            "evidence_dir": str(EVIDENCE), "scope_manifest": str(SCOPE_MANIFEST),
            "previous_evidence": str(PREVIOUS_EVIDENCE), "python": str(RUNTIME),
            "local_time_limit_seconds": TASK_LIMIT_SECONDS,
            "persistent_deadline_file": str(DEADLINE_FILE),
            "persistent_deadline_qpc": TASK_DEADLINE_QPC,
            "task_remaining_seconds_at_run_start": round(remaining_task_seconds(), 3),
            "short_gate_limit_seconds": SHORT_GATE_LIMIT_SECONDS,
            "collection_limit_seconds": R2_COLLECTION_LIMIT_SECONDS,
            "broad_limit_seconds": RUN_LIMIT_SECONDS,
            "node_wall_limit_seconds": NODE_LIMIT_SECONDS,
        }
        state["harness_copy"] = r2_copy_harness()
        state["dependency_cache"] = r3_prepare_dependency_cache()
        state["snapshot_copy"] = r2_copy_snapshot()
        state["runtime_snapshot_start"] = r2_tree_hash(TEST_ROOT)
        state["snapshot_outputs_initial_manifest"] = r2_snapshot_outputs_manifest()
        write_json(EVIDENCE / "snapshot_outputs_initial_manifest.json", state["snapshot_outputs_initial_manifest"])
        if state["snapshot_outputs_initial_manifest"].get("file_count") != 0 or state["snapshot_outputs_initial_manifest"].get("unsafe_paths"):
            raise RuntimeError("isolated snapshot outputs was not newly created empty and unredirected")
        state["source_inventory_before"] = r2_inventory()
        write_json(EVIDENCE / "source_inventory_before.json", state["source_inventory_before"])
        for log_name in ("network_guard.jsonl", "filesystem_guard.jsonl"):
            (EVIDENCE / log_name).touch(exist_ok=True)
        state["network_log_start_sha256"] = file_sha256(EVIDENCE / "network_guard.jsonl")
        state["filesystem_log_start_sha256"] = file_sha256(EVIDENCE / "filesystem_guard.jsonl")

        gate_started = time.monotonic()
        state["short_gate_started_at_utc"] = utc_now()
        for name, offline in (("probe_absent", "absent"), ("probe_zero", "0"), ("probe_one", "1")):
            remaining = r3_gate_remaining(gate_started)
            if remaining <= 0:
                raise RuntimeError("180-second short validation gate expired before all three guard probes")
            row = r2_probe_guard(name, offline, started, state, min(GUARD_PROBE_LIMIT_SECONDS, remaining))
            state["guard_probes"].append(row)
            state["all_process_trees_closed"] = state["all_process_trees_closed"] and bool(row["process_tree_closed"])
            if not row["passed"]:
                raise RuntimeError(f"real network guard preflight failed for B2B_TEST_OFFLINE={offline}")

        state["snapshot_output_probe"] = r4_snapshot_outputs_probe(gate_started, started, state)

        for mode in ("separate", "same", "unset"):
            r4_log_contract_probe(mode, gate_started, started, state)
        if len(state.get("log_contract_controls", [])) != 3 or not all(row.get("passed") for row in state["log_contract_controls"]):
            raise RuntimeError("one or more real-bootstrap B2B_SOCKET_DENY_JSONL contract controls failed")

        cache_control_1 = r3_cache_control(1, gate_started, started, state)
        cache_control_2 = r3_cache_control(2, gate_started, started, state)
        cache_results = [cache_control_1.get("result"), cache_control_2.get("result")]
        if (
            cache_results[0] is None or cache_results[1] is None
            or cache_results[0].get("pid") == cache_results[1].get("pid")
            or cache_results[0].get("domain_core") != cache_results[1].get("domain_core")
            or cache_results[0].get("domain_core") != "examplebrand"
            or cache_results[0].get("extractor_cache_path") != cache_results[1].get("extractor_cache_path")
        ):
            raise RuntimeError("two isolated cache-control processes did not return identical domain/cache results")
        state["cache_control_files"] = r3_cache_files()
        write_json(EVIDENCE / "dependency_cache_after_controls.json", {
            "started_empty": state["dependency_cache"]["started_empty"],
            "cache_path": state["dependency_cache"]["path"],
            "controls": cache_results,
            "files": state["cache_control_files"],
        })

        preflight_env, removed_names = r2_child_env("preflight")
        state["removed_environment_names"] = removed_names
        state["preflight_tldextract_cache"] = preflight_env.get("TLDEXTRACT_CACHE")
        preflight_argv = r2_pytest_argv("preflight", [
            f"--junitxml={EVIDENCE / 'preflight_junit.xml'}",
            BENCHMARK_NODEID, P07_NODEID, *PREFLIGHT_NODEIDS, *ACCURACY_NODEIDS,
        ])
        state["preflight_argv"] = preflight_argv
        remaining = r3_gate_remaining(gate_started)
        if remaining <= 0:
            raise RuntimeError("180-second short validation gate expired before the five-node preflight")
        preflight_result = supervise("preflight", preflight_argv, min(PREFLIGHT_LIMIT_SECONDS, remaining), preflight_env, started)
        state["preflight_process"] = preflight_result
        state["processes"].append(preflight_result)
        state["all_process_trees_closed"] = state["all_process_trees_closed"] and bool(preflight_result.get("process_tree_closed"))
        append_jsonl(EVIDENCE / "process_supervision.jsonl", preflight_result)
        state["preflight_test_counts"] = r2_result_counts(EVIDENCE / "preflight_reports.jsonl")
        state["p07_log_contract"] = r4_verify_p07(state)
        state["benchmark_cli_verification"] = r4_verify_benchmark_cli(state)
        preflight_nodes = {row.get("nodeid") for row in read_jsonl(EVIDENCE / "preflight_reports.jsonl") if row.get("phase") == "call"}
        expected_preflight_nodes = set([BENCHMARK_NODEID, P07_NODEID] + PREFLIGHT_NODEIDS + ACCURACY_NODEIDS)
        child_rows = read_jsonl(EVIDENCE / "child_processes.jsonl")
        child_env_checks = []
        expected_child_offline = {
            PREFLIGHT_NODEIDS[0]: (False, None),
            PREFLIGHT_NODEIDS[1]: (True, "1"),
        }
        network_rows = read_jsonl(EVIDENCE / "network_guard.jsonl")
        fs_rows = read_jsonl(EVIDENCE / "filesystem_guard.jsonl")
        for nodeid, (expected_present, expected_value) in expected_child_offline.items():
            matches = [row for row in child_rows if row.get("nodeid") == nodeid]
            valid_child = False
            if len(matches) == 1:
                child = matches[0]
                pid = child.get("pid")
                valid_child = (
                    child.get("b2b_test_offline_present") is expected_present
                    and child.get("b2b_test_offline_value") == expected_value
                    and child.get("network_guard") == "1"
                    and child.get("filesystem_guard") == "1"
                    and child.get("tldextract_cache_path") == str(DEPENDENCY_CACHE_DIR.resolve())
                    and child.get("caller_env_unchanged") is True
                    and child.get("sitecustomize_path_inherited") is True
                    and any(row.get("pid") == pid and row.get("kind") == "network_guard_hook_installed" for row in network_rows)
                    and any(row.get("pid") == pid and row.get("kind") == "filesystem_guard_hook_installed" for row in fs_rows)
                )
            child_env_checks.append({"nodeid": nodeid, "expected_offline_present": expected_present, "expected_offline_value": expected_value, "child_count": len(matches), "passed": valid_child})
        state["preflight_child_env_checks"] = child_env_checks
        state["short_gate_seconds"] = round(time.monotonic() - gate_started, 3)
        state["preflight_expected_nodes"] = sorted(expected_preflight_nodes)
        network_rows = read_jsonl(EVIDENCE / "network_guard.jsonl")
        filesystem_rows = read_jsonl(EVIDENCE / "filesystem_guard.jsonl")
        expected_probe_ids = {"probe_absent", "probe_zero", "probe_one"}
        expected_write_probe_ids = expected_probe_ids | {"snapshot_output_boundary"}
        allowed_short_gate_network_ids = expected_probe_ids | {"log_contract_separate"}
        state["unexpected_short_gate_network_blocks"] = [
            row for row in network_rows
            if row.get("kind") == "blocked_network" and row.get("probe_id") not in allowed_short_gate_network_ids
        ]
        state["unexpected_short_gate_write_denials"] = [
            row for row in filesystem_rows
            if row.get("kind") == "workspace_write_denied" and row.get("probe_id") not in expected_write_probe_ids
        ]
        if (
            preflight_result.get("exit_code") != 0 or preflight_result.get("timed_out")
            or preflight_result.get("job_assignment") != "success"
            or preflight_nodes != expected_preflight_nodes
            or state["preflight_test_counts"].get("passed") != len(expected_preflight_nodes)
            or state["preflight_test_counts"].get("failed") or state["preflight_test_counts"].get("errors")
            or state["preflight_test_counts"].get("skipped")
            or any(not row["passed"] for row in child_env_checks)
            or not state.get("p07_log_contract", {}).get("passed")
            or not state.get("benchmark_cli_verification", {}).get("passed")
            or not state.get("snapshot_output_probe", {}).get("passed")
            or not state.get("snapshot_output_probe", {}).get("junction_removed")
            or not all(row.get("passed") for row in state.get("log_contract_controls", []))
            or state["short_gate_seconds"] > SHORT_GATE_LIMIT_SECONDS
            or state["unexpected_short_gate_network_blocks"]
            or state["unexpected_short_gate_write_denials"]
        ):
            raise RuntimeError("180-second five-node/cache/guard/output-boundary short validation gate did not pass exactly as authorized")

        state["short_gate_passed"] = True

        collect_env, _ = r2_child_env("collect")
        state["collection_tldextract_cache"] = collect_env.get("TLDEXTRACT_CACHE")
        collect_argv = r2_pytest_argv("collect", ["--collect-only"])
        state["collect_argv"] = collect_argv
        collect_result = supervise("collect", collect_argv, R2_COLLECTION_LIMIT_SECONDS, collect_env, started)
        state["collection_process"] = collect_result
        state["processes"].append(collect_result)
        state["all_process_trees_closed"] = state["all_process_trees_closed"] and bool(collect_result.get("process_tree_closed"))
        append_jsonl(EVIDENCE / "process_supervision.jsonl", collect_result)
        if collect_result.get("exit_code") != 0 or collect_result.get("timed_out") or collect_result.get("job_assignment") != "success":
            raise RuntimeError("full collection failed or exceeded its authorized 60-second wall-clock limit")
        current = json.loads((EVIDENCE / "collection_nodes.json").read_text(encoding="utf-8")).get("nodeids", [])
        previous = json.loads((PREVIOUS_EVIDENCE / "collection_nodes.json").read_text(encoding="utf-8")).get("nodeids", [])
        state["collection"] = {"nodeids": current, "count": len(current)}
        state["selected_nodeids"] = [node for node in current if node not in DEFERRED]
        if len(current) != 1105 or len(previous) != 1105 or len(set(current)) != len(current):
            raise RuntimeError(f"collection count/identity mismatch: current={len(current)}, previous={len(previous)}")
        state["collection_matches_previous"] = current == previous
        if not state["collection_matches_previous"]:
            state["collection_diff"] = {
                "added": [node for node in current if node not in set(previous)],
                "removed": [node for node in previous if node not in set(current)],
                "order_changed": current != previous and set(current) == set(previous),
            }
            write_json(EVIDENCE / "collection_diff.json", state["collection_diff"])
            raise RuntimeError("full collection differs from the previously recorded 1105-node list; broad run not started")
        missing_deferred = [node for node in DEFERRED if current.count(node) != 1]
        if missing_deferred:
            raise RuntimeError(f"deferred node IDs not present exactly once: {missing_deferred}")
        selected = [node for node in current if node not in DEFERRED]
        if len(selected) != 1102:
            raise RuntimeError(f"expected 1102 selected nodes, got {len(selected)}")
        state["selected_nodeids"] = selected
        write_json(EVIDENCE / "selected_nodes.json", {"count": len(selected), "nodeids": selected, "deferred_nodeids": DEFERRED})

        run_env, removed_names_run = r2_child_env("run")
        state["run_tldextract_cache"] = run_env.get("TLDEXTRACT_CACHE")
        state["removed_environment_names"] = sorted(set(removed_names) | set(removed_names_run))
        run_argv = r2_pytest_argv("run", [
            "--maxfail=10", f"--junitxml={EVIDENCE / 'junit.xml'}",
            *[f"--deselect={nodeid}" for nodeid in DEFERRED],
        ])
        state["run_argv"] = run_argv
        run_limit = min(RUN_LIMIT_SECONDS, max(0.0, remaining_task_seconds() - 12.0))
        state["computed_run_limit_seconds"] = run_limit
        if run_limit < 1:
            raise RuntimeError("persistent six-hour deadline leaves no safe window for the normal regression")
        run_result = supervise("run", run_argv, run_limit, run_env, started)
        state["run_process"] = run_result
        state["processes"].append(run_result)
        state["all_process_trees_closed"] = state["all_process_trees_closed"] and bool(run_result.get("process_tree_closed"))
        append_jsonl(EVIDENCE / "process_supervision.jsonl", run_result)
        selected_record_path = EVIDENCE / "selected_nodes.json"
        if selected_record_path.is_file():
            selected_record = json.loads(selected_record_path.read_text(encoding="utf-8"))
            shutil.copyfile(selected_record_path, EVIDENCE / "plugin_selected_nodes_run.json")
            state["actual_deselected_nodeids"] = selected_record.get("deselected_nodeids", [])
            state["actual_selected_nodeids"] = selected_record.get("nodeids", [])
            if state["actual_selected_nodeids"] != selected or state["actual_deselected_nodeids"] != DEFERRED:
                state["fatal_error"] = "pytest plugin's actual post-deselect list differs from the collection-verified normal/long partition"
        state["termination_reason"] = run_result.get("termination_reason", "")
        if run_result.get("exit_code") != 0:
            state["fatal_error"] = f"normal regression pytest exited {run_result.get('exit_code')} with --maxfail=10"
        state["test_counts"] = r2_phase_result_counts(EVIDENCE / "pytest_reports.jsonl", "run")
        run_events = [
            row for row in read_jsonl(EVIDENCE / "node_events.jsonl")
            if row.get("pytest_phase") == "run"
            or (row.get("event") in {"node_start", "node_finish"} and row.get("phase") == "run")
        ]
        state["run_started_nodeids"] = [row.get("nodeid", "") for row in run_events if row.get("event") == "node_start"]
        state["run_finished_nodeids"] = [row.get("nodeid", "") for row in run_events if row.get("event") == "node_finish"]
        state["last_completed_node"] = next((row.get("nodeid", "") for row in reversed(run_events) if row.get("event") == "node_finish"), "")
        state["last_completed_stage"] = next((f"{row.get('nodeid')}::{row.get('phase')}={row.get('outcome')}" for row in reversed(run_events) if row.get("event") == "phase_report"), "")
        result_total = sum(state["test_counts"].get(k, 0) for k in ("passed", "failed", "errors", "skipped"))
        state["test_counts"]["unresolved_selected_count"] = max(0, len(selected) - result_total)
        started_ids, finished_ids = set(state["run_started_nodeids"]), set(state["run_finished_nodeids"])
        state["test_counts"]["never_started_nodes"] = max(0, len(selected) - len(started_ids))
        state["test_counts"]["started_not_finished_nodes"] = len(started_ids - finished_ids)
        if state["test_counts"].get("reported_nodes") != len(selected):
            state["test_counts"]["selected_unreported_count"] = max(0, len(selected) - state["test_counts"].get("reported_nodes", 0))
        state["normal_gate_passed"] = bool(
            run_result.get("exit_code") == 0 and not run_result.get("timed_out")
            and run_result.get("job_assignment") == "success"
            and state["test_counts"].get("failed", 0) == 0
            and state["test_counts"].get("errors", 0) == 0
            and state["test_counts"].get("reported_nodes", 0) == len(selected)
            and state["test_counts"].get("unresolved_selected_count", 0) == 0
            and state.get("actual_selected_nodeids") == selected
            and state.get("actual_deselected_nodeids") == DEFERRED
        )
        if state["normal_gate_passed"]:
            long_results = []
            for short_name, nodeid, wall_limit in LONG_TESTS:
                if remaining_task_seconds() <= 5:
                    result = {
                        "name": short_name, "nodeid": nodeid, "status": "NOT_RUN",
                        "reason": "persistent six-hour deadline exhausted before this required long test",
                        "process": {"started": False, "timed_out": True, "termination_reason": "global_deadline"},
                        "counts": {},
                    }
                    long_results.append(result)
                    write_json(EVIDENCE / "long_test_results.json", long_results)
                    continue
                phase = f"long_{short_name}"
                long_env, long_removed = r2_child_env(phase)
                state["removed_environment_names"] = sorted(set(state.get("removed_environment_names", [])) | set(long_removed))
                long_argv = r2_pytest_argv(phase, [
                    "--maxfail=1", f"--junitxml={EVIDENCE / f'{phase}.xml'}", nodeid,
                ])
                long_process = supervise(phase, long_argv, wall_limit, long_env, started)
                state["processes"].append(long_process)
                state["all_process_trees_closed"] = state["all_process_trees_closed"] and bool(long_process.get("process_tree_closed"))
                append_jsonl(EVIDENCE / "process_supervision.jsonl", long_process)
                selected_path = EVIDENCE / "selected_nodes.json"
                if selected_path.is_file():
                    shutil.copyfile(selected_path, EVIDENCE / f"plugin_selected_nodes_{phase}.json")
                long_counts = r2_phase_result_counts(EVIDENCE / "pytest_reports.jsonl", phase)
                if long_process.get("timed_out"):
                    long_status = "TIMEOUT"
                elif (
                    long_process.get("exit_code") == 0 and long_process.get("job_assignment") == "success"
                    and long_counts.get("reported_nodes") == 1 and long_counts.get("passed") == 1
                    and long_counts.get("failed") == 0 and long_counts.get("errors") == 0
                    and long_counts.get("skipped") == 0
                ):
                    long_status = "PASS"
                else:
                    long_status = "FAIL" if long_process.get("started") else "NOT_RUN"
                long_results.append({
                    "name": short_name, "nodeid": nodeid, "status": long_status,
                    "wall_limit_seconds": wall_limit, "process": long_process, "counts": long_counts,
                })
                write_json(EVIDENCE / "long_test_results.json", long_results)
            state["long_test_results"] = long_results
        else:
            state["long_test_results"] = [
                {"name": name, "nodeid": nodeid, "status": "NOT_RUN", "reason": "normal regression did not pass on this source snapshot"}
                for name, nodeid, _limit in LONG_TESTS
            ]
            write_json(EVIDENCE / "long_test_results.json", state["long_test_results"])
        write_json(EVIDENCE / "selection_reconciliation.json", {
            "collection_total": len(current), "previous_collection_total": len(previous),
            "normal_selected_count": len(selected), "normal_selected_nodeids": selected,
            "deferred_from_normal_nodeids": DEFERRED,
            "required_long_nodeids": [nodeid for _name, nodeid, _limit in LONG_TESTS],
            "partition_union_matches_collection": (
                len(set(selected) | set(DEFERRED)) == len(current)
                and set(selected).isdisjoint(DEFERRED)
                and set(selected) | set(DEFERRED) == set(current)
            ),
            "normal_gate_passed": state.get("normal_gate_passed", False),
        })
        write_json(EVIDENCE / "selected_nodes.json", {
            "collection_count": len(current),
            "normal_selected_count": len(selected),
            "normal_nodeids": selected,
            "normal_deselected_nodeids": DEFERRED,
            "long_test_nodeids": [nodeid for _name, nodeid, _limit in LONG_TESTS],
            "partition_union_matches_collection": (
                len(set(selected) | set(DEFERRED)) == len(current)
                and set(selected).isdisjoint(DEFERRED)
                and set(selected) | set(DEFERRED) == set(current)
            ),
        })

    except Exception as exc:
        state["fatal_error"] = state.get("fatal_error") or f"{type(exc).__name__}: {exc}"
        gate_started_local = locals().get("gate_started")
        if gate_started_local is not None:
            state["short_gate_seconds"] = round(time.monotonic() - gate_started_local, 3)
            state["short_gate_passed"] = False
    finally:
        state["dependency_cache_manifest"] = {
            "cache_path": str(DEPENDENCY_CACHE_DIR.resolve()),
            "started_empty": state.get("dependency_cache", {}).get("started_empty", False),
            "outside_source_snapshot": state.get("dependency_cache", {}).get("outside_source_snapshot", False),
            "inside_evidence_root": state.get("dependency_cache", {}).get("inside_evidence_root", False),
            "files": r3_cache_files(),
        }
        write_json(EVIDENCE / "dependency_cache_manifest.json", state["dependency_cache_manifest"])
        try:
            state["source_inventory_after"] = r2_inventory()
            write_json(EVIDENCE / "source_inventory_after.json", state["source_inventory_after"])
            before = state.get("source_inventory_before", {})
            after = state["source_inventory_after"]
            state["source_inventory_comparison"] = compare_inventories(before, after)
            state["source_inventory_match"] = bool(state["source_inventory_comparison"]["match"])
            write_json(EVIDENCE / "source_inventory_comparison.json", state["source_inventory_comparison"])
            state["runtime_snapshot_end"] = after.get("runtime_tree_sha256", "")
        except Exception as exc:
            state["source_inventory_error"] = repr(exc)
        try:
            if SNAPSHOT_OUTPUTS.is_dir():
                state["snapshot_outputs_manifest"] = r2_snapshot_outputs_manifest()
                write_json(EVIDENCE / "snapshot_outputs_manifest.json", state["snapshot_outputs_manifest"])
            else:
                state["snapshot_outputs_manifest"] = {"root": str(SNAPSHOT_OUTPUTS), "root_missing": True, "files": [], "unsafe_paths": []}
            state["snapshot_outputs_integrity"] = (
                state.get("snapshot_outputs_initial_manifest", {}).get("file_count") == 0
                and not state["snapshot_outputs_manifest"].get("unsafe_paths")
                and state["snapshot_outputs_manifest"].get("root_missing") is not True
            )
        except Exception as exc:
            state["snapshot_outputs_manifest_error"] = repr(exc)
            state["snapshot_outputs_integrity"] = False
        try:
            copied_rows = state.get("snapshot_copy", {}).get("files", [])
            changed = []
            missing = []
            for row in copied_rows:
                source = ROOT / row["path"]
                if not source.is_file():
                    missing.append(row["path"])
                    continue
                actual = file_sha256(source)
                if actual != row["source_sha256"]:
                    changed.append({"path": row["path"], "expected_sha256": row["source_sha256"], "actual_sha256": actual})
            state["source_workspace_changes_after_copy"] = changed
            state["source_workspace_missing_after_copy"] = missing
            state["source_workspace_unchanged_after_copy"] = not changed and not missing
            write_json(EVIDENCE / "source_workspace_final_comparison.json", {
                "unchanged": state["source_workspace_unchanged_after_copy"],
                "changed": changed,
                "missing": missing,
                "scope_file_count": len(copied_rows),
            })
        except Exception as exc:
            state["source_workspace_comparison_error"] = repr(exc)
            state["source_workspace_unchanged_after_copy"] = False
        try:
            state["candidate_hash_comparison"] = compare_candidate_hashes()
        except Exception as exc:
            state["candidate_hash_comparison"] = {"unchanged": False, "error": repr(exc)}
        filesystem_events = read_jsonl(EVIDENCE / "filesystem_guard.jsonl")
        probe_ids = {"probe_absent", "probe_zero", "probe_one"}
        allowed_write_probe_ids = probe_ids | {"snapshot_output_boundary"}
        all_denials = [row for row in filesystem_events if row.get("kind") == "workspace_write_denied"]
        state["expected_probe_workspace_blocks"] = [row for row in all_denials if row.get("probe_id") in probe_ids]
        state["expected_snapshot_output_escape_blocks"] = [row for row in all_denials if row.get("probe_id") == "snapshot_output_boundary"]
        state["blocked_workspace_writes"] = [row for row in all_denials if row.get("probe_id") not in allowed_write_probe_ids]
        state["snapshot_test_access_count"] = sum(row.get("kind") == "snapshot_test_file_access" for row in filesystem_events)
        network = read_jsonl(EVIDENCE / "network_guard.jsonl")
        state["expected_probe_network_events"] = [row for row in network if row.get("probe_id") in {"probe_absent", "probe_zero", "probe_one"} and row.get("kind") == "blocked_network"]
        state["expected_log_contract_network_events"] = [row for row in network if row.get("probe_id") == "log_contract_separate" and row.get("kind") == "blocked_network"]
        expected_network_probe_ids = {"probe_absent", "probe_zero", "probe_one", "log_contract_separate"}
        state["non_probe_network_blocks"] = [row for row in network if row.get("kind") == "blocked_network" and row.get("probe_id") not in expected_network_probe_ids]
        state["network_guard_counts"] = {
            "hook_installed": sum(row.get("kind") == "network_guard_hook_installed" for row in network),
            "expected_probe_blocked": len(state["expected_probe_network_events"]),
            "non_probe_blocked": len(state["non_probe_network_blocks"]),
        }
        state["network_log_sha256"] = file_sha256(EVIDENCE / "network_guard.jsonl") if (EVIDENCE / "network_guard.jsonl").is_file() else ""
        state["filesystem_log_sha256"] = file_sha256(EVIDENCE / "filesystem_guard.jsonl") if (EVIDENCE / "filesystem_guard.jsonl").is_file() else ""
        state["source_snapshot_runtime_end"] = r2_tree_hash(TEST_ROOT) if TEST_ROOT.is_dir() else ""
        state["child_tldextract_cache_mismatches"] = [
            row for row in read_jsonl(EVIDENCE / "child_processes.jsonl")
            if row.get("tldextract_cache_path") != str(DEPENDENCY_CACHE_DIR.resolve())
        ]
        state["guard_channel_dedup"] = r4_deduplicate_guard_channels(state)
        state["filesystem_log_sha256"] = file_sha256(EVIDENCE / "filesystem_guard.jsonl") if (EVIDENCE / "filesystem_guard.jsonl").is_file() else ""
        if state.get("collection_process", {}).get("process_tree_closed") is False or state.get("preflight_process", {}).get("process_tree_closed") is False or state.get("run_process", {}).get("process_tree_closed") is False:
            state["all_process_trees_closed"] = False
        state["duration_seconds"] = round(time.monotonic() - started, 3)
        state["finished_at_utc"] = utc_now()
        all_probes_passed = len(state.get("guard_probes", [])) == 3 and all(row.get("passed") for row in state.get("guard_probes", []))
        expected_preflight_count = len(ACCURACY_NODEIDS) + len(PREFLIGHT_NODEIDS) + 2
        preflight_ok = (
            state.get("short_gate_passed") is True
            and state.get("preflight_test_counts", {}).get("passed") == expected_preflight_count
            and state.get("preflight_test_counts", {}).get("reported_nodes") == expected_preflight_count
            and state.get("preflight_process", {}).get("exit_code") == 0
            and state.get("benchmark_cli_verification", {}).get("passed") is True
            and state.get("snapshot_output_probe", {}).get("passed") is True
        )
        counts = state.get("test_counts", {})
        selected = state.get("selected_nodeids", [])
        broad_ok = (
            state.get("collection_matches_previous") is True
            and state.get("normal_gate_passed") is True
            and len(state.get("long_test_results", [])) == len(LONG_TESTS)
            and all(row.get("status") == "PASS" for row in state.get("long_test_results", []))
        )
        integrity_ok = (
            state.get("source_inventory_match") is True
            and state.get("source_workspace_unchanged_after_copy") is True
            and state.get("candidate_hash_comparison", {}).get("unchanged") is True
            and state.get("runtime_snapshot_end") == EXPECTED_RUNTIME_SHA256
            and state.get("snapshot_copy", {}).get("live_runtime_sha256_before") == EXPECTED_RUNTIME_SHA256
            and len(state.get("blocked_workspace_writes", [])) == 0
            and state.get("all_process_trees_closed") is True
            and len(state.get("expected_probe_network_events", [])) == 3
            and len(state.get("expected_probe_workspace_blocks", [])) == 3
            and len(state.get("expected_snapshot_output_escape_blocks", [])) == 3
            and state.get("snapshot_output_probe", {}).get("passed") is True
            and state.get("snapshot_output_probe", {}).get("junction_removed") is True
            and state.get("snapshot_outputs_integrity") is True
            and state.get("benchmark_cli_verification", {}).get("passed") is True
            and state.get("dependency_cache", {}).get("started_empty") is True
            and len([row for row in state.get("cache_controls", []) if row.get("passed")]) == 2
            and len([row for row in state.get("log_contract_controls", []) if row.get("passed")]) == 3
            and state.get("p07_log_contract", {}).get("passed") is True
            and len(state.get("expected_log_contract_network_events", [])) == 1
            and state.get("expected_log_contract_network_events", [{}])[0].get("event") == "socket.connect"
            and not state.get("guard_channel_dedup", {}).get("outside_allowed_paths")
            and not state.get("guard_channel_dedup", {}).get("payload_mismatch_event_keys")
            and not state.get("unexpected_short_gate_network_blocks")
            and not state.get("unexpected_short_gate_write_denials")
            and not state.get("child_tldextract_cache_mismatches")
        )
        state["section_status"] = "OFFLINE_REGRESSION_ALL_PASS" if all_probes_passed and preflight_ok and broad_ok and integrity_ok and not state.get("fatal_error") else "BLOCKED_WITH_EVIDENCE"
        state["command_record"] = {
            "cwd": str(TEST_ROOT),
            "runtime": str(RUNTIME),
            "source_snapshot": str(TEST_ROOT),
            "collection_argv": state.get("collect_argv", []),
            "preflight_argv": state.get("preflight_argv", []),
            "run_argv": state.get("run_argv", []),
            "environment_policy": {
                "B2B_TEST_OFFLINE": "1 in parent pytest; explicit child env value/presence preserved",
                "ENABLE_JS_FALLBACK": "explicit child env value/presence preserved",
                "PETZOO_NETWORK_GUARD": "1 independent of B2B_TEST_OFFLINE",
                "PETZOO_FILESYSTEM_GUARD": "1; workspace/snapshot sources denied except resolved isolated source_snapshot/outputs",
                "PETZOO_SNAPSHOT_OUTPUTS": str(SNAPSHOT_OUTPUTS.resolve()),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": "new evidence root only; workspace source root excluded",
                "TLDEXTRACT_CACHE": str(DEPENDENCY_CACHE_DIR.resolve()),
                "TLDEXTRACT_CACHE_propagation": "guard/cache controls, preflight, collection, broad pytest and every plugin-spawned child before Python import",
                "B2B_SOCKET_DENY_JSONL": "preserve explicit nonempty path after child-cwd resolution and evidence/temp allowlist; central default only when absent/empty",
                "PETZOO_NETWORK_GUARD_LOG": str(EVIDENCE / "network_guard.jsonl"),
                "evidence_root_selection": "--evidence-dir or PETZOO_EVIDENCE_DIR; safe repository outputs/petzoo_offline_kapanis_20260925 default",
                "provider_credentials_inherited": False,
                "removed_environment_variable_names": state.get("removed_environment_names", []),
                "environment_values_recorded": ["B2B_TEST_OFFLINE presence/value only", "TLDEXTRACT_CACHE isolated path", "B2B_SOCKET_DENY_JSONL requested/effective paths only"],
            },
            "plugin_sha256": r2_harness_hashes(),
            "runtime_hash_start": state.get("runtime_live_start"),
            "runtime_hash_snapshot_start": state.get("runtime_snapshot_start"),
            "runtime_hash_snapshot_end": state.get("runtime_snapshot_end"),
            "source_inventory_start": state.get("source_inventory_before", {}).get("inventory_sha256"),
            "source_inventory_end": state.get("source_inventory_after", {}).get("inventory_sha256"),
            "collection_count": len(state.get("collection", {}).get("nodeids", [])),
            "selected_count": len(state.get("selected_nodeids", [])),
            "phase_tldextract_cache": {
                "guard_and_controls": str(DEPENDENCY_CACHE_DIR.resolve()),
                "preflight": state.get("preflight_tldextract_cache"),
                "collection": state.get("collection_tldextract_cache"),
                "run": state.get("run_tldextract_cache"),
                "child_mismatches": state.get("child_tldextract_cache_mismatches", []),
            },
            "deferred_nodeids": DEFERRED,
            "long_test_nodeids": [nodeid for _name, nodeid, _limit in LONG_TESTS],
            "task_deadline_file": str(DEADLINE_FILE),
            "task_deadline_qpc": TASK_DEADLINE_QPC,
            "task_remaining_seconds_at_finish": round(remaining_task_seconds(), 3),
            "processes": state.get("processes", []),
        }
        write_json(EVIDENCE / "run_command.json", state["command_record"])
        write_json(EVIDENCE / "run_state.json", state)
        r2_write_report(state)
    print(json.dumps({
        "section_status": state["section_status"],
        "collection_total": len(state.get("collection", {}).get("nodeids", [])),
        "selected": len(state.get("selected_nodeids", [])),
        "preflight": state.get("preflight_test_counts", {}),
        "tests": state.get("test_counts", {}),
        "duration_seconds": state.get("duration_seconds"),
        "long_tests": [{"nodeid": row.get("nodeid"), "status": row.get("status")} for row in state.get("long_test_results", [])],
        "report": str(EVIDENCE_ROOT / "FINAL_RAPOR.md"),
    }, ensure_ascii=False), flush=True)
    return 0 if state["section_status"] == "OFFLINE_REGRESSION_ALL_PASS" else 1


if __name__ == "__main__":
    configure_paths()
    raise SystemExit(r2_main())
