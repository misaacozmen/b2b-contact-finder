from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any


GATES = tuple(f"P{index:02d}" for index in range(1, 13))
PROVIDERS = ("brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _runtime_source_hash(snapshot: Path) -> str:
    files = [snapshot / "config.py", snapshot / "main.py", snapshot / "scrape_exhibitors.py"]
    files.extend(sorted(snapshot.glob("requirements*.txt")))
    files.extend(sorted((snapshot / "modules").glob("*.py")))
    digest = hashlib.sha256()
    for path in sorted({candidate.resolve() for candidate in files if candidate.is_file()}):
        relative = path.relative_to(snapshot.resolve()).as_posix().encode("utf-8")
        body_hash = hashlib.sha256(path.read_bytes()).digest()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        digest.update(body_hash)
    return digest.hexdigest()


def _source_errors(root: Path, workspace: Path) -> list[str]:
    errors: list[str] = []
    snapshot = root / "source_snapshot"
    manifest_path = root / "source_manifest.json"
    if not snapshot.is_dir() or not manifest_path.is_file():
        return ["source snapshot/manifest missing"]
    manifest = _json(manifest_path)
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        return ["source_manifest.files missing"]
    expected = {str(row.get("path", "")): row for row in entries if isinstance(row, dict)}
    actual = {
        path.relative_to(snapshot).as_posix(): path
        for path in snapshot.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if set(actual) != set(expected):
        errors.append("source snapshot file set differs from source manifest")
    for relative, path in actual.items():
        entry = expected.get(relative, {})
        if entry.get("bytes") != path.stat().st_size or entry.get("sha256") != _sha256(path):
            errors.append(f"source snapshot hash/size mismatch: {relative}")
        workspace_relative = str(entry.get("workspace_path", relative))
        workspace_path = workspace / workspace_relative
        if not workspace_path.is_file() or _sha256(workspace_path) != _sha256(path):
            errors.append(f"workspace/source snapshot mismatch: {workspace_relative}")
    required = {"requirements-browser.txt", "requirements-dev.txt", "requirements-ocr.txt"}
    if not required.issubset(actual):
        errors.append("runtime dependency snapshot omits required requirements files")
    expected_runtime = str(manifest.get("runtime_source_tree_sha256", ""))
    actual_runtime = _runtime_source_hash(snapshot)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_runtime) or actual_runtime != expected_runtime:
        errors.append("snapshot runtime_source_tree_sha256 mismatch")
    if manifest.get("workspace_runtime_source_tree_sha256") != _runtime_source_hash(workspace):
        errors.append("workspace runtime source hash differs from snapshot")
    return errors


def _pytest_rows(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = root / "pytest_reports_final.jsonl"
    if not path.is_file():
        return [], ["pytest_reports_final.jsonl missing"]
    rows = []
    errors = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"pytest report line {number} is invalid JSON")
            continue
        if isinstance(value, dict):
            rows.append(value)
    if not rows:
        errors.append("pytest report is empty")
    if any(str(row.get("outcome", "")) in {"failed", "error"} for row in rows):
        errors.append("pytest report contains failed/error outcomes")
    if any(row.get("phase") == "teardown" and row.get("outcome") != "passed" for row in rows):
        errors.append("pytest teardown failure present")
    contract_path = root / "acceptance_contract.json"
    contract = _json(contract_path) if contract_path.is_file() else {}
    requirements = contract.get("global_requirements", {}) if isinstance(contract, dict) else {}
    allowed_skips = set(requirements.get("known_preexisting_skip_nodeids", [])) if isinstance(requirements, dict) else set()
    unexpected_skips = {
        str(row.get("nodeid", "")) for row in rows
        if row.get("outcome") == "skipped" and str(row.get("nodeid", "")) not in allowed_skips
    }
    if unexpected_skips:
        errors.append("full acceptance run contains new skipped nodeids: " + ", ".join(sorted(unexpected_skips)))
    if any(row.get("outcome") in {"deselected", "xfailed", "xpassed"} or row.get("wasxfail") for row in rows):
        errors.append("full acceptance run contains xfail or deselection")
    return rows, errors


def _nodeid_passes(rows: list[dict[str, Any]], nodeid: str, command_id: str) -> bool:
    selected = [row for row in rows if row.get("nodeid") == nodeid and row.get("command_id") == command_id]
    call = [row for row in selected if row.get("phase") == "call"]
    teardown = [row for row in selected if row.get("phase") == "teardown"]
    return bool(call) and all(row.get("outcome") == "passed" for row in call) and all(row.get("outcome") == "passed" for row in teardown)


def _junit_errors(path: Path, *, allowed_skip_count: int = 0) -> list[str]:
    if not path.is_file():
        return [f"JUnit report missing: {path.name}"]
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return [f"JUnit report unreadable: {type(exc).__name__}"]
    suites = [root] if root.tag.endswith("testsuite") else list(root.iter("testsuite"))
    errors = []
    for suite in suites:
        for key in ("failures", "errors"):
            try:
                count = int(suite.attrib.get(key, "0"))
            except ValueError:
                count = 1
            if count:
                errors.append(f"JUnit reports {count} {key}")
        try:
            skipped = int(suite.attrib.get("skipped", "0"))
        except ValueError:
            skipped = allowed_skip_count + 1
        if skipped > allowed_skip_count:
            errors.append(f"JUnit reports {skipped} unapproved skipped tests")
    return errors


def _gate_errors(root: Path, rows: list[dict[str, Any]], errors: list[str]) -> list[str]:
    contract_path = root / "acceptance_contract.json"
    if not contract_path.is_file():
        return ["acceptance_contract.json missing"]
    contract = _json(contract_path)
    gates = contract.get("gates") if isinstance(contract, dict) else None
    if not isinstance(gates, dict) or set(gates) != set(GATES):
        return ["acceptance contract must enumerate P01-P12 exactly"]
    out: list[str] = []
    command_id = str(contract.get("command_id", ""))
    for gate in GATES:
        entry = gates[gate]
        if not isinstance(entry, dict):
            out.append(f"{gate}: malformed contract entry")
            continue
        nodeids = entry.get("nodeids")
        if not isinstance(nodeids, list) or not nodeids:
            out.append(f"{gate}: required nodeids missing")
        else:
            for nodeid in nodeids:
                if not _nodeid_passes(rows, str(nodeid), command_id):
                    out.append(f"{gate}: required nodeid not passed with clean teardown: {nodeid}")
        evidence = root / "evidence" / gate
        for name in ("command.json", "stdout.txt", "stderr.txt", "junit.xml", "measurement.json"):
            if not (evidence / name).is_file():
                out.append(f"{gate}: evidence missing {name}")
        command_path = evidence / "command.json"
        if command_path.is_file():
            record = _json(command_path)
            if record.get("timed_out") is not False or record.get("exit_code") not in (0, entry.get("expected_exit_code", 0)):
                out.append(f"{gate}: command timed out or unexpected exit")
            if record.get("source_tree_sha256") != contract.get("source_tree_sha256"):
                out.append(f"{gate}: command source hash mismatch")
            if record.get("fixture_sha256") != contract.get("fixture_sha256"):
                out.append(f"{gate}: command fixture hash mismatch")
            out.extend(_artifact_hash_errors(evidence, record.get("artifact_sha256"), f"{gate} gate command"))
        out.extend(f"{gate}: {message}" for message in _junit_errors(evidence / "junit.xml"))
    return out


def _p11_errors(root: Path, expected_fixture_sha256: str) -> list[str]:
    errors: list[str] = []
    results = {}
    for workers in (1, 3):
        folder = root / "evidence" / "P11" / f"workers_{workers}"
        required = ("command.json", "result.json", "transport.jsonl", "http.jsonl", "network.jsonl", "checkpoint.sqlite3", "run_manifest.json")
        if any(not (folder / name).is_file() for name in required):
            errors.append(f"P11 workers_{workers}: raw process evidence incomplete")
            continue
        result = _json(folder / "result.json")
        command = _json(folder / "command.json")
        if command.get("timed_out") is not False or command.get("exit_code") != 0:
            errors.append(f"P11 workers_{workers}: child command failed/timed out")
        if command.get("runtime_source_tree_sha256") != _runtime_source_hash(root / "source_snapshot"):
            errors.append(f"P11 workers_{workers}: source hash mismatch")
        errors.extend(_artifact_hash_errors(folder, command.get("artifact_sha256"), f"P11 workers_{workers}"))
        if result.get("fixture_sha256") != expected_fixture_sha256:
            errors.append(f"P11 workers_{workers}: fixture hash mismatch")
        if result.get("worker_count") != workers or result.get("source_count") != 137:
            errors.append(f"P11 workers_{workers}: wrong worker or source count")
        sources = result.get("source_record_ids", [])
        if len(sources) != 137 or len(set(sources)) != 137:
            errors.append(f"P11 workers_{workers}: source identities are not 137 unique records")
        if set(result.get("physical_provider_calls", {})) != set(PROVIDERS) or any(
            int(result.get("physical_provider_calls", {}).get(provider, 0)) <= 0 for provider in PROVIDERS
        ):
            errors.append(f"P11 workers_{workers}: one or more paid providers had zero physical calls")
        transport_events = [json.loads(line) for line in (folder / "transport.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        transport = [row for row in transport_events if row.get("kind") == "paid_transport"]
        terminal_events = [row for row in transport_events if row.get("kind") == "paid_transport_terminal"]
        if len(transport) != len(terminal_events) or {row.get("call_id") for row in transport} != {row.get("call_id") for row in terminal_events}:
            errors.append(f"P11 workers_{workers}: transport start/terminal receipt mismatch")
        by_call = {row.get("call_id"): row for row in result.get("calls", [])}
        if any(not row.get("request_fingerprint") or not row.get("query_fingerprint") or not row.get("query_fingerprint_basis") or int(row.get("attempt_ordinal", 0)) < 1 or row.get("execution_generation") is None for row in transport):
            errors.append(f"P11 workers_{workers}: transport request/attempt identity missing")
        if any(row.get("call_id") not in by_call or row.get("provider") != by_call[row.get("call_id")].get("provider") for row in transport):
            errors.append(f"P11 workers_{workers}: recorder call IDs do not bind to provider_calls")
        if any(
            (row.get("outcome") == "response" and not re.fullmatch(r"[0-9a-f]{64}", str(row.get("response_sha256", ""))))
            or (row.get("outcome") == "exception" and not row.get("exception_type"))
            for row in terminal_events
        ):
            errors.append(f"P11 workers_{workers}: response/exception terminal receipt incomplete")
        item_sources = result.get("source_record_ids", [])
        if any(
            row.get("item_index") is None or int(row["item_index"]) >= len(item_sources)
            or row.get("source_record_id") != item_sources[int(row["item_index"])]
            for row in transport
        ):
            errors.append(f"P11 workers_{workers}: source/item transport correlation mismatch")
        group_h = {source for source in item_sources if str(source).startswith("petzoo:H:")}
        if any(not any(row.get("provider") == provider and row.get("source_record_id") in group_h for row in transport) for provider in ("linkedin", "llm")):
            errors.append(f"P11 workers_{workers}: H did not exercise LinkedIn and LLM wrappers")
        if any(row.get("source_record_id", "").startswith("petzoo:A:") for row in transport):
            errors.append(f"P11 workers_{workers}: supplied-site no-call group made a paid transport call")
        budget_rows = result.get("provider_budgets", {})
        for provider in PROVIDERS:
            budget = budget_rows.get(provider, {})
            attempts = int(budget.get("physical_http_attempts", 0))
            if attempts > int(budget.get("effective_limit", -1)):
                errors.append(f"P11 workers_{workers}: {provider} exceeded effective limit")
            if sum(row.get("provider") == provider for row in transport) != attempts:
                errors.append(f"P11 workers_{workers}: {provider} transport/DB count mismatch")
        network = [json.loads(line) for line in (folder / "network.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        if not any(row.get("kind") == "guard_armed" and row.get("child") is True for row in network):
            errors.append(f"P11 workers_{workers}: child network guard not armed")
        blocked = [row for row in network if row.get("kind") == "blocked_network"]
        if any(row.get("event") not in {"socket.getaddrinfo", "socket.sendto"} for row in blocked):
            errors.append(f"P11 workers_{workers}: non-DNS socket attempt was blocked")
        if any(
            provider_host in json.dumps(row, ensure_ascii=False).casefold()
            for row in blocked
            for provider_host in ("brightdata", "googleapis", "brandfetch", "hunter", "openrouter", "linkedin")
        ):
            errors.append(f"P11 workers_{workers}: provider endpoint DNS was attempted")
        db_uri = f"file:{(folder / 'checkpoint.sqlite3').resolve().as_posix()}?mode=ro&immutable=1"
        try:
            with sqlite3.connect(db_uri, uri=True) as db:
                db_sources = db.execute("SELECT COUNT(*),COUNT(DISTINCT source_record_id) FROM run_items").fetchone()
                db_calls = db.execute("SELECT COUNT(*) FROM provider_calls WHERE http_started_at<>''").fetchone()[0]
                round_rows = db.execute(
                    "SELECT round_ordinal,kind,created_at FROM scheduler_progress_snapshots "
                    "WHERE run_id=? AND phase='PAID' AND kind IN ('START','END')",
                    (str(result.get("run_id", "")),),
                ).fetchall()
                heartbeat_rows = db.execute(
                    "SELECT round_ordinal,sequence,snapshot_json,snapshot_sha256,created_at "
                    "FROM scheduler_heartbeat_events WHERE run_id=? AND phase='PAID' "
                    "ORDER BY round_ordinal,sequence",
                    (str(result.get("run_id", "")),),
                ).fetchall()
        except sqlite3.Error as exc:
            errors.append(f"P11 workers_{workers}: scheduler heartbeat DB evidence unavailable ({type(exc).__name__})")
            round_rows, heartbeat_rows = [], []
            db_sources, db_calls = (0, 0), 0
        if db_sources != (137, 137):
            errors.append(f"P11 workers_{workers}: checkpoint source count mismatch")
        if db_calls != len({row.get("call_id") for row in transport}):
            errors.append(f"P11 workers_{workers}: physical call ledger/transport mismatch")
        round_timestamps: dict[tuple[int, str], datetime] = {}
        for round_ordinal, kind, created_at in round_rows:
            try:
                round_timestamps[(int(round_ordinal), str(kind))] = datetime.fromisoformat(str(created_at))
            except (TypeError, ValueError):
                errors.append(f"P11 workers_{workers}: malformed scheduler round timestamp")
        heartbeat_timestamps: dict[int, list[datetime]] = {}
        heartbeat_sequences: dict[int, list[int]] = {}
        for round_ordinal, sequence, payload_text, digest, created_at in heartbeat_rows:
            try:
                payload = json.loads(str(payload_text))
                canonical_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest() != str(digest):
                    errors.append(f"P11 workers_{workers}: heartbeat payload hash mismatch")
                if payload.get("heartbeat") is not True or int(payload.get("sequence", -1)) != int(sequence):
                    errors.append(f"P11 workers_{workers}: malformed heartbeat event")
                ordinal = int(round_ordinal)
                heartbeat_timestamps.setdefault(ordinal, []).append(datetime.fromisoformat(str(created_at)))
                heartbeat_sequences.setdefault(ordinal, []).append(int(sequence))
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                errors.append(f"P11 workers_{workers}: malformed heartbeat event")
        for ordinal, sequences in heartbeat_sequences.items():
            if sequences != list(range(1, len(sequences) + 1)):
                errors.append(f"P11 workers_{workers}: heartbeat sequence gap in round {ordinal}")
        for ordinal in sorted({key[0] for key in round_timestamps}):
            started_at = round_timestamps.get((ordinal, "START"))
            ended_at = round_timestamps.get((ordinal, "END"))
            if started_at is None or ended_at is None:
                errors.append(f"P11 workers_{workers}: round {ordinal} lacks START/END snapshots")
                continue
            if (ended_at - started_at).total_seconds() <= 60:
                continue
            timeline = [
                started_at,
                *[stamp for stamp in heartbeat_timestamps.get(ordinal, []) if started_at < stamp < ended_at],
                ended_at,
            ]
            gaps = [(right - left).total_seconds() for left, right in zip(timeline, timeline[1:])]
            if len(timeline) < 3 or max(gaps) > 60:
                errors.append(f"P11 workers_{workers}: round {ordinal} heartbeat gap exceeds 60 seconds")
        g000 = [row for row in transport if row.get("source_record_id") == "petzoo:G:000" and row.get("provider") == "brightdata"]
        g_call = [row for row in result.get("calls", []) if int(row.get("item_index", -1)) < len(item_sources) and item_sources[int(row.get("item_index", -1))] == "petzoo:G:000" and row.get("provider") == "brightdata"]
        g_terminal = [row for row in terminal_events if row.get("source_record_id") == "petzoo:G:000" and row.get("provider") == "brightdata"]
        if len(g000) != 1 or len(g_call) != 1 or g_call[0].get("state") != "UNKNOWN" or len(g_terminal) != 1 or g_terminal[0].get("outcome") != "exception":
            errors.append(f"P11 workers_{workers}: post-send UNKNOWN retry/correlation contract failed")
        results[str(workers)] = result
    if set(results) == {"1", "3"}:
        def logical(result: dict[str, Any]) -> list[tuple]:
            return sorted((
                row.get("source_record_id"), row.get("provider"), row.get("operation"),
                row.get("request_fingerprint"), row.get("state"), row.get("terminal_reason"),
                row.get("dependency_job_fingerprint"),
            ) for row in result.get("work", []))
        def dispositions(result: dict[str, Any]) -> list[tuple]:
            return sorted((row.get("source_record_id"), row.get("free_state"), row.get("paid_state"), bool(row.get("paid_required"))) for row in result.get("items", []))
        if logical(results["1"]) != logical(results["3"]):
            errors.append("P11 worker logical work/disposition sets differ")
        if dispositions(results["1"]) != dispositions(results["3"]):
            errors.append("P11 worker item disposition sets differ")
    return errors


def _latest_evidence_dir(parent: Path, prefix: str) -> Path | None:
    candidates = sorted(
        path for path in parent.iterdir()
        if path.is_dir() and path.name.startswith(prefix)
    ) if parent.is_dir() else []
    return candidates[-1] if candidates else None


def _artifact_hash_errors(folder: Path, hashes: Any, label: str) -> list[str]:
    errors = []
    if not isinstance(hashes, dict):
        return [f"{label}: artifact hash map missing"]
    for name, digest in hashes.items():
        relative = Path(str(name))
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"{label}: unsafe artifact path")
            continue
        target = (folder / relative).resolve()
        try:
            target.relative_to(folder.resolve())
        except ValueError:
            errors.append(f"{label}: unsafe artifact path")
            continue
        if not target.is_file() or _sha256(target) != str(digest):
            errors.append(f"{label}: artifact hash mismatch: {name}")
    return errors


def _p07_errors(root: Path, expected_fixture_sha256: str) -> list[str]:
    errors: list[str] = []
    base = root / "evidence" / "P07"
    fresh = _latest_evidence_dir(base, "fresh_process_verified_")
    active = _latest_evidence_dir(base, "active_owner_verified_")
    replay = next((
        path for path in reversed(sorted(base.glob("replay_matrix_verified_*")))
        if path.is_dir() and (path / "workers_1").is_dir() and (path / "workers_3").is_dir()
    ), None) if base.is_dir() else None
    if fresh is None or active is None or replay is None:
        return ["P07: fresh-process, active-owner, or worker replay evidence missing"]
    for name in ("command.json", "result.json", "checkpoint.sqlite3", "network.jsonl", "stdout.txt", "stderr.txt"):
        if not (fresh / name).is_file():
            errors.append(f"P07: fresh-process evidence missing {name}")
    if (fresh / "command.json").is_file():
        command = _json(fresh / "command.json")
        if command.get("timed_out") is not False or command.get("exit_code") != 0:
            errors.append("P07: fresh-process command failed or timed out")
    if (fresh / "result.json").is_file():
        result = _json(fresh / "result.json")
        if result.get("physical_transport_called") is not False or result.get("result") != [None, "http_404"]:
            errors.append("P07: fresh-process negative retrieval was not reused")
    try:
        active_result = _json(active / "result.json")
        if (
            active_result.get("fixture_sha256") != expected_fixture_sha256
            or active_result.get("waiter_result") != "retrieval_owner_wait_timeout"
            or active_result.get("same_run_physical_attempts") != 1
            or active_result.get("new_run_physical_attempts") != 1
        ):
            errors.append("P07: active-owner/new-run retry evidence mismatch")
    except (OSError, json.JSONDecodeError):
        errors.append("P07: active-owner result missing or invalid")
    expected_cases = {
        "http_403_browser", "http_404", "http_410", "http_429", "http_503",
        "timeout", "transient_then_success", "unsupported",
    }
    for workers in (1, 3):
        worker_root = replay / f"workers_{workers}"
        observed_cases = {path.name for path in worker_root.iterdir() if path.is_dir()}
        if observed_cases != expected_cases:
            errors.append(f"P07: workers_{workers} retrieval variant set incomplete")
        for case in expected_cases:
            folder = worker_root / case
            for name in ("result.json", "checkpoint.sqlite3", "http.jsonl"):
                if not (folder / name).is_file():
                    errors.append(f"P07: workers_{workers}/{case} missing {name}")
                    continue
            result_path = folder / "result.json"
            if result_path.is_file():
                try:
                    result = _json(result_path)
                    if (
                        result.get("workers") != workers or result.get("case") != case
                        or result.get("fixture_sha256") != expected_fixture_sha256
                        or result.get("repeat_count") != 8
                        or result.get("observed_attempts") != result.get("expected_attempts")
                    ):
                        errors.append(f"P07: workers_{workers}/{case} measurement mismatch")
                except (OSError, json.JSONDecodeError):
                    errors.append(f"P07: workers_{workers}/{case} result invalid")
    return errors


def _p08_errors(root: Path, expected_fixture_sha256: str, expected_source_sha256: str) -> list[str]:
    base = root / "evidence" / "P08"
    folder = _latest_evidence_dir(base, "redirect_matrix_verified_")
    if folder is None:
        return ["P08: redirect lifecycle matrix missing"]
    errors = []
    required = (
        "command.json", "completed_checkpoint.sqlite3", "discovery_attempts.json",
        "hard_kill_checkpoint.sqlite3", "hard_kill_network.jsonl", "hard_kill_stderr.txt", "hard_kill_stdout.txt",
    )
    for name in required:
        if not (folder / name).is_file():
            errors.append(f"P08: redirect matrix missing {name}")
    command_path = folder / "command.json"
    if command_path.is_file():
        command = _json(command_path)
        if (
            command.get("timed_out") is not False or command.get("exit_code") != 86
            or command.get("expected_exit_code") != 86
            or command.get("fixture_sha256") != expected_fixture_sha256
            or command.get("runtime_source_tree_sha256") != expected_source_sha256
        ):
            errors.append("P08: hard-kill child command identity or exit mismatch")
        errors.extend(_artifact_hash_errors(folder, command.get("artifact_sha256"), "P08"))
    for name, expected_started in (("completed_checkpoint.sqlite3", 0), ("hard_kill_checkpoint.sqlite3", 1)):
        database = folder / name
        if not database.is_file():
            continue
        try:
            uri = f"file:{database.resolve().as_posix()}?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True) as db:
                started = int(db.execute("SELECT COUNT(*) FROM discovery_executions WHERE state='STARTED'").fetchone()[0])
            if (started > 0) != bool(expected_started):
                errors.append(f"P08: {name} execution lifecycle state mismatch")
        except sqlite3.Error:
            errors.append(f"P08: {name} unreadable")
    return errors


def _p12_errors(root: Path, expected_fixture_sha256: str, expected_source_sha256: str) -> list[str]:
    base = root / "evidence" / "P12"
    matrix_path = next(iter(reversed(sorted(base.glob("matrix_result_*.json")))), None) if base.is_dir() else None
    if matrix_path is None:
        return ["P12: six-boundary matrix receipt missing"]
    errors = []
    try:
        matrix = _json(matrix_path)
        boundaries = matrix.get("boundaries", {})
    except (OSError, json.JSONDecodeError):
        return ["P12: six-boundary matrix receipt invalid"]
    expected = {
        "allocation_before", "allocation_after", "call_reserved_before_http",
        "http_started_before_response", "terminal_call_before_company_save",
        "finalization_after_memory_plan",
    }
    if matrix.get("fixture_sha256") != expected_fixture_sha256 or set(boundaries) != expected:
        errors.append("P12: boundary names or fixture identity mismatch")
    for boundary in sorted(expected):
        folders = sorted(path for path in base.glob(f"{boundary}_verified_*") if path.is_dir())
        if not folders:
            errors.append(f"P12: {boundary} evidence missing")
            continue
        folder = folders[-1]
        required = (
            "after_resume_state.json", "before_state.json", "checkpoint.sqlite3", "command.json",
            "crash_stderr.txt", "crash_stdout.txt", "faults.jsonl", "http.jsonl", "network.jsonl",
            "result.json", "resume_stderr.txt", "resume_stdout.txt", "run_manifest.json", "transport.jsonl",
        )
        if any(not (folder / name).is_file() for name in required):
            errors.append(f"P12: {boundary} raw evidence incomplete")
            continue
        command = _json(folder / "command.json")
        processes = (command.get("crash_process", {}), command.get("resume_process", {}))
        if (
            command.get("fixture_sha256") != expected_fixture_sha256
            or command.get("source_sha256") != expected_source_sha256
            or any(process.get("timed_out") is not False for process in processes)
            or processes[0].get("exit_code") != 86 or processes[0].get("expected_exit_code") != 86
            or processes[1].get("exit_code") != 0 or processes[1].get("expected_exit_code") != 0
        ):
            errors.append(f"P12: {boundary} child process receipt mismatch")
        errors.extend(_artifact_hash_errors(folder, command.get("artifact_sha256"), f"P12 {boundary}"))
        try:
            result = _json(folder / "result.json")
            observed = boundaries[boundary]
            if (
                result.get("run_id") != observed.get("run_id")
                or result.get("outcome") != observed.get("outcome")
                or result.get("manifest_complete") != observed.get("manifest_complete")
                or observed.get("crash_exit") != 86 or observed.get("resume_exit") != 0
            ):
                errors.append(f"P12: {boundary} resumed state differs from matrix receipt")
        except (OSError, json.JSONDecodeError):
            errors.append(f"P12: {boundary} result invalid")
    return errors


def _negative_errors(root: Path) -> list[str]:
    path = root / "evidence" / "verifier_negatives" / "results.json"
    if not path.is_file():
        return ["V01-V08 verifier-negative results missing"]
    result = _json(path)
    errors = []
    if result.get("positive_control_exit_code") != 0:
        errors.append("verifier positive control did not pass")
    final_positive = result.get("final_positive_control", {})
    if final_positive.get("exit_code") != 0:
        errors.append("final verifier positive-control command did not pass")
    for proof in (final_positive,):
        errors.extend(_negative_proof_file_errors(root, proof, "final positive control"))
    cases = result.get("cases", {})
    if set(cases) != {f"V{i:02d}" for i in range(1, 9)}:
        return errors + ["verifier negative matrix is incomplete"]
    for name, case in cases.items():
        mutation = case.get("mutation_receipt")
        if not isinstance(mutation, dict):
            errors.append(f"{name}: mutation receipt missing")
        else:
            canonical = json.dumps(mutation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != case.get("mutation_sha256"):
                errors.append(f"{name}: mutation receipt hash mismatch")
        before = case.get("control_before", {})
        negative = case.get("negative_verifier", {})
        after = case.get("control_after", {})
        if before.get("exit_code") != 0 or after.get("exit_code") != 0:
            errors.append(f"{name}: positive control did not pass before and after the negative")
        if negative.get("exit_code") in (None, 0):
            errors.append(f"{name}: mutation is unbound or verifier did not reject it")
        expected_fragment = str(case.get("expected_error_fragment", ""))
        rejection_errors = [str(value) for value in negative.get("rejection_errors", [])]
        if not expected_fragment or not any(expected_fragment in value for value in rejection_errors):
            errors.append(f"{name}: verifier rejection did not identify the intended gate")
        for label, proof in (("control before", before), ("negative", negative), ("control after", after)):
            errors.extend(_negative_proof_file_errors(root, proof, f"{name} {label}"))
    return errors


def _negative_proof_file_errors(root: Path, proof: dict[str, Any], label: str) -> list[str]:
    errors = []
    hashes = proof.get("artifact_sha256", {}) if isinstance(proof, dict) else {}
    if not isinstance(hashes, dict) or not hashes:
        return [f"{label}: stdout/stderr proof hashes missing"]
    for name, digest in hashes.items():
        relative = Path(str(name))
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"{label}: unsafe proof path")
            continue
        target = (root / relative).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            errors.append(f"{label}: unsafe proof path")
            continue
        if not target.is_file() or _sha256(target) != str(digest):
            errors.append(f"{label}: proof artifact missing or hash mismatch: {name}")
    return errors


def verify(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    workspace = Path(str(_json(root / "source_manifest.json").get("workspace", ""))).resolve() if (root / "source_manifest.json").is_file() else Path.cwd().resolve()
    integrity_errors = _source_errors(root, workspace)
    rows, report_errors = _pytest_rows(root)
    acceptance_errors = list(report_errors)
    acceptance_errors.extend(_gate_errors(root, rows, report_errors))
    fixture_path = root / "fixture_manifest.json"
    fixture_hash = ""
    if not fixture_path.is_file():
        integrity_errors.append("fixture_manifest.json missing")
    else:
        fixture = _json(fixture_path)
        fixture_hash = str(fixture.get("fixture_sha256", ""))
        if fixture.get("input_count") != 137 or not re.fullmatch(r"[0-9a-f]{64}", fixture_hash):
            integrity_errors.append("fixture manifest count/hash invalid")
    if fixture_hash:
        acceptance_errors.extend(_p11_errors(root, fixture_hash))
        source_hash = _runtime_source_hash(root / "source_snapshot")
        acceptance_errors.extend(_p07_errors(root, fixture_hash))
        acceptance_errors.extend(_p08_errors(root, fixture_hash, source_hash))
        acceptance_errors.extend(_p12_errors(root, fixture_hash, source_hash))
    command_path = root / "full_offline_command.json"
    if not command_path.is_file():
        acceptance_errors.append("full_offline_command.json missing")
    else:
        command = _json(command_path)
        if command.get("exit_code") != 0 or command.get("timed_out") is not False:
            acceptance_errors.append("full offline collection failed or timed out")
        if command.get("source_tree_sha256") != _runtime_source_hash(root / "source_snapshot"):
            acceptance_errors.append("full offline command source hash mismatch")
    known_skipped = {
        str(row.get("nodeid", "")) for row in rows if row.get("outcome") == "skipped"
    }
    acceptance_errors.extend(_junit_errors(
        root / "full_offline_junit.xml", allowed_skip_count=len(known_skipped),
    ))
    acceptance_errors.extend(_negative_errors(root))
    network_path = root / "network_guard_final.jsonl"
    if not network_path.is_file():
        integrity_errors.append("network_guard_final.jsonl missing")
    else:
        network_rows = [json.loads(line) for line in network_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        blocked = [row for row in network_rows if row.get("kind") == "blocked_network"]
        if any(row.get("event") not in {"socket.getaddrinfo", "socket.sendto"} for row in blocked):
            acceptance_errors.append("network guard blocked a non-DNS socket attempt")
        if any(
            provider_host in json.dumps(row, ensure_ascii=False).casefold()
            for row in blocked
            for provider_host in ("brightdata", "googleapis", "brandfetch", "hunter", "openrouter", "linkedin")
        ):
            acceptance_errors.append("paid provider host DNS was attempted")
    legacy_result: dict[str, Any]
    legacy_path = Path(__file__).with_name("delivery_verifier.py")
    try:
        spec = importlib.util.spec_from_file_location("petzoo_legacy_delivery_verifier", legacy_path)
        if not spec or not spec.loader:
            raise ImportError("legacy verifier loader unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        legacy_errors = module.verify_delivery(root)
        legacy_result = {"status": "PASS" if not legacy_errors else "FAIL", "errors": legacy_errors}
        if legacy_errors:
            integrity_errors.append("K01-K15 verifier: " + "; ".join(legacy_errors[:8]))
    except Exception as exc:
        legacy_result = {"status": "ERROR", "errors": [f"{type(exc).__name__}:{exc}"]}
        integrity_errors.append("K01-K15 verifier invocation failed")
    all_errors = integrity_errors + acceptance_errors
    decision = "READY_FOR_ARCHITECT_REVIEW" if not all_errors else "BLOCKED_WITH_EVIDENCE"
    return {
        "delivery_integrity_status": "PASS" if not integrity_errors else "FAIL",
        "petzoo_acceptance_status": "PASS" if not acceptance_errors else "FAIL",
        "overall_decision": decision,
        "legacy_k01_k15_verifier": legacy_result,
        "integrity_errors": integrity_errors,
        "acceptance_errors": acceptance_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delivery", required=True, type=Path)
    parser.add_argument("--no-write", action="store_true", help="verify without writing delivery_verification.json")
    args = parser.parse_args()
    result = verify(args.delivery)
    if not args.no_write:
        output_path = args.delivery.resolve() / "delivery_verification.json"
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["overall_decision"] == "READY_FOR_ARCHITECT_REVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
