from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import sqlite3
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import config
from modules import (
    checkpoint,
    discovery_coverage,
    excel,
    google_places,
    linkedin_company,
    output_artifacts,
    replay_snapshot,
    report,
    runtime,
    runtime_paths,
    run_context,
    scorer,
    search,
)
from modules.utils import ensure_directories, setup_logging


_PAID_ESCALATION_STATUSES = {
    "REVIEW_NEEDED", "WEBSITE_NOT_FOUND", "WEBSITE_AMBIGUOUS",
    "WEBSITE_FETCH_FAILED",
}
PAID_ATTEMPT_RESULTS = frozenset({"COMPLETED", "NO_CALL_NEEDED", "BLOCKED_BUDGET", "FAILED", "UNKNOWN"})
_SUCCESSFUL_PROVIDER_STATES = {"COMPLETED", "EMPTY", "CACHE_HIT", "DONE"}
_NONTERMINAL_PROVIDER_STATES = {"RESERVED", "RUNNING"}
FREE_FAILURE_STATUSES = frozenset({"SEARCH_FAILED", "PROCESSING_FAILED"})
SAFE_SQLITE_MINIMUM = (3, 51, 3)
PATCHED_SAFE_SQLITE_BUILDS = frozenset({(3, 50, 7), (3, 44, 6)})


def require_safe_sqlite_for_live() -> None:
    """Reject live runs on SQLite builds without the required handoff fixes."""
    version = tuple(int(part) for part in sqlite3.sqlite_version.split(".")[:3])
    if version >= SAFE_SQLITE_MINIMUM or version in PATCHED_SAFE_SQLITE_BUILDS:
        return
    raise RuntimeError(
        "live run requires SQLite >= 3.51.3 or patched 3.50.7/3.44.6; "
        f"runtime is {sqlite3.sqlite_version}"
    )


def require_complete_manifest_phase(manifest: dict) -> None:
    if manifest.get("complete") and manifest.get("phase") != "COMPLETE":
        raise RuntimeError("complete manifest phase must be exactly COMPLETE")


def paid_attempt_result(row: dict | None = None, *, exception: BaseException | None = None) -> str:
    """Normalize a paid worker outcome before changing durable scheduler state."""
    if exception is not None:
        exception_name = exception.__class__.__name__.casefold()
        return "UNKNOWN" if isinstance(exception, (TimeoutError, ConnectionError)) or "timeout" in exception_name else "FAILED"
    row = row or {}
    provider_results = row.get("provider_results", row.get("provider_result", ""))
    if isinstance(provider_results, dict):
        provider_results = [provider_results]
    if isinstance(provider_results, (list, tuple)):
        states_by_call: dict[str, str] = {}
        anonymous_states: list[str] = []
        for value in provider_results:
            if isinstance(value, dict):
                state = str(value.get("result_state", value.get("provider_result", value.get("state", "")))).upper()
                ids = value.get("call_ids", value.get("provider_call_ids", [])) or []
            else:
                state = str(getattr(value, "result_state", getattr(value, "state", value))).upper()
                ids = getattr(value, "call_ids", ())
            ids = [str(call_id) for call_id in ids if call_id]
            if ids:
                for call_id in ids:
                    states_by_call[call_id] = state
            else:
                anonymous_states.append(state)
        states = list(states_by_call.values()) + anonymous_states
        if any(state in _NONTERMINAL_PROVIDER_STATES for state in states):
            return "UNKNOWN"
        if "UNKNOWN" in states:
            return "UNKNOWN"
        if "BLOCKED_BUDGET" in states:
            return "BLOCKED_BUDGET"
        if any(state in _SUCCESSFUL_PROVIDER_STATES for state in states):
            return "COMPLETED"
        if "FAILED" in states:
            return "FAILED"
        if states and all(state in _SUCCESSFUL_PROVIDER_STATES | {"NO_CALL_NEEDED"} for state in states):
            return "COMPLETED"
    elif str(provider_results).upper() in PAID_ATTEMPT_RESULTS:
        return str(provider_results).upper()
    explicit = str(row.get("paid_attempt_result", "")).upper()
    if explicit in PAID_ATTEMPT_RESULTS:
        return explicit
    if str(row.get("reason", "")).lower() in {"budget_blocked", "budget_exhausted"}:
        return "BLOCKED_BUDGET"
    if row.get("status") in {"NO_CALL_NEEDED", "NOT_REQUIRED"}:
        return "NO_CALL_NEEDED"
    if row.get("status") in report.OK_STATUSES:
        return "FAILED"
    return "FAILED"


def classify_scheduler_states(row: dict, *, attempt_number: int, publication_gate: bool | None = None) -> dict[str, object]:
    """Produce the three scheduler fields from one durable-free outcome."""
    status = str(row.get("status", "")).upper()
    if status in FREE_FAILURE_STATUSES:
        if int(attempt_number) < 2:
            return {"free_state": "PENDING", "paid_state": "NOT_REQUIRED", "paid_required": False}
        return {"free_state": "FAILED", "paid_state": "PENDING", "paid_required": True}
    if publication_gate is None:
        publication_gate = row.get("publication_eligible") is True
    if status in report.OK_STATUSES and not publication_gate:
        return {"free_state": "DONE", "paid_state": "PENDING", "paid_required": True}
    if status in report.OK_STATUSES and publication_gate:
        return {"free_state": "DONE", "paid_state": "NOT_REQUIRED", "paid_required": False}
    if needs_paid_escalation(row):
        return {"free_state": "DONE", "paid_state": "PENDING", "paid_required": True}
    return {"free_state": "DONE", "paid_state": "NOT_REQUIRED", "paid_required": False}


def classify_free_scheduler_result(row: dict, *, attempt_number: int) -> str:
    """Compatibility view of the joint scheduler classifier."""
    return str(classify_scheduler_states(row, attempt_number=attempt_number)["free_state"])


def _inherited_paid_result(row: dict) -> tuple[str, str]:
    """Return a durable reference for cache/duplicate results with no new call."""
    values = row.get("provider_results", row.get("provider_result", []))
    if isinstance(values, dict):
        values = [values]
    for value in values if isinstance(values, (list, tuple)) else []:
        state = str(value.get("result_state", value.get("state", ""))).upper() if isinstance(value, dict) else str(getattr(value, "result_state", getattr(value, "state", ""))).upper()
        reason = str(value.get("result_reason", value.get("reason", ""))) if isinstance(value, dict) else str(getattr(value, "result_reason", getattr(value, "reason", "")))
        ids = value.get("call_ids", value.get("provider_call_ids", [])) if isinstance(value, dict) else getattr(value, "call_ids", ())
        reference = str((ids or [""])[0])
        if state == "CACHE_HIT" or ("duplicate" in reason.casefold() and state in {"COMPLETED", "DONE"}):
            return "NO_CALL_NEEDED", reference or ("cache:" + reason if state == "CACHE_HIT" else reason)
    return "", ""


def _verify_complete_artifacts(run_root: Path, manifest: dict) -> None:
    artifact_dir = run_root / "output" / "artifacts" / str(manifest.get("artifact_set_sha256", ""))
    if not artifact_dir.is_dir() or not manifest.get("files"):
        raise RuntimeError("complete run manifest has no immutable artifact set")
    for name, info in manifest["files"].items():
        path = artifact_dir / str(name)
        if not path.is_file() or checkpoint.file_hash(path) != info.get("sha256"):
            raise RuntimeError(f"complete artifact hash mismatch: {name}")


def _verify_artifact_metadata(run_root: Path, artifacts: dict) -> None:
    artifact_set = str(artifacts.get("artifact_set_sha256", ""))
    files = artifacts.get("files")
    artifact_dir = run_root / "output" / "artifacts" / artifact_set
    if not artifact_set or not isinstance(files, dict) or not files or not artifact_dir.is_dir():
        raise RuntimeError("finalization artifact metadata is invalid")
    aggregate = []
    for name, info in sorted(files.items()):
        path = artifact_dir / str(name)
        if path.parent != artifact_dir or not path.is_file():
            raise RuntimeError(f"finalization artifact is missing: {name}")
        digest = checkpoint.file_hash(path)
        if digest != str(info.get("sha256", "")) or int(info.get("bytes", -1)) != path.stat().st_size:
            raise RuntimeError(f"finalization artifact hash mismatch: {name}")
        aggregate.append(f"{path.name}:{digest}\n")
    if hashlib.sha256("".join(aggregate).encode("utf-8")).hexdigest() != artifact_set:
        raise RuntimeError("finalization aggregate artifact hash mismatch")


def _call_writer(writer: Callable[..., Any], rows: list[dict], elapsed: float,
                 telemetry_snapshot: dict[str, Any], config_sha256: str = "") -> Any:
    parameters = inspect.signature(writer).parameters
    accepts_snapshot = "telemetry_snapshot" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    accepts_config = "config_sha256" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs = {}
    if accepts_snapshot:
        kwargs["telemetry_snapshot"] = telemetry_snapshot
    if accepts_config:
        kwargs["config_sha256"] = config_sha256
    if kwargs:
        return writer(rows, elapsed, **kwargs)
    return writer(rows, elapsed)


def _validate_resume_identity(*, run_root: Path, manifest: dict, input_hash: str,
                              run_config: run_context.RunConfig,
                              ordered_source_record_ids: list[str],
                              lineage: dict) -> None:
    expected = {
        "run_id": run_root.name,
        "input_sha256": input_hash,
        "config_sha256": run_config.sha256,
        "runtime_source_tree_sha256": run_context.source_tree_sha256(),
        "item_count": len(ordered_source_record_ids),
        "ordered_source_record_ids": ordered_source_record_ids,
        "lineage": lineage,
    }
    db_path = run_root / "state" / "progress.sqlite3"
    resume_phase = None
    if db_path.exists():
        with closing(sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)) as connection:
            phase_row = connection.execute("SELECT phase FROM runs WHERE run_id=?", (run_root.name,)).fetchone()
            resume_phase = str(phase_row[0]) if phase_row else None
    validation_profile = "COMPLETE" if manifest.get("complete") and resume_phase != "FINALIZING" else "FINALIZING" if resume_phase == "FINALIZING" else "ACTIVE_RESUME"
    run_context.validate_run_bundle(
        run_root,
        expected_input_hash=input_hash,
        expected_config_hash=run_config.sha256,
        expected_source_ids=ordered_source_record_ids,
        profile=validation_profile,
    )
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"resume identity mismatch: {key}")
    if not db_path.exists():
        raise RuntimeError("resume SQLite is missing")
    uri = f"file:{db_path.resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute("SELECT run_id,input_hash FROM runs WHERE run_id=?", (run_root.name,)).fetchone()
        if not row or row[0] != run_root.name or row[1] != input_hash:
            raise RuntimeError("resume SQLite identity mismatch")
        db_ids = [row[0] for row in connection.execute("SELECT source_record_id FROM run_items WHERE run_id=? ORDER BY item_index", (run_root.name,))]
        if db_ids != ordered_source_record_ids:
            raise RuntimeError("resume source_record_id list mismatch")
        if connection.execute("SELECT COUNT(*) FROM run_items WHERE run_id=?", (run_root.name,)).fetchone()[0] != len(ordered_source_record_ids):
            raise RuntimeError("resume item count mismatch")


def resolve_run_config(manifest: dict | None = None, *, allow_paid: bool | None = None,
                       search_cache: str | None = None, crawl_cache: str | None = None,
                       brightdata_budget: int | None = None, google_places_budget: int | None = None,
                       linkedin_budget: int | None = None, rerank_cache: bool = False,
                       finalize_without_paid: bool | None = None,
                       input_count: int | None = None,
                       unique_profile_host_count: int | None = None) -> run_context.RunConfig:
    if manifest is None:
        return run_context.RunConfig.from_config(
            paid_enabled=bool(allow_paid),
            free_only_finalization=bool(finalize_without_paid),
            input_count=input_count,
            unique_profile_host_count=unique_profile_host_count,
        )
    recorded = run_context.RunConfig.from_dict(manifest.get("run_config", {}))
    if allow_paid is not None and bool(allow_paid) != recorded.paid_enabled:
        raise ValueError("resume rejects behavioral paid-mode override")
    if finalize_without_paid is not None and bool(finalize_without_paid) != recorded.free_only_finalization:
        raise ValueError("resume rejects behavioral free-only-finalization override")
    if search_cache is not None and search_cache != recorded.search_cache_mode:
        raise ValueError("resume rejects behavioral search-cache override")
    if crawl_cache is not None and crawl_cache != recorded.crawl_cache_mode:
        raise ValueError("resume rejects behavioral crawl-cache override")
    if rerank_cache and (recorded.search_cache_mode != "replay" or recorded.crawl_cache_mode != "replay"):
        raise ValueError("resume rejects behavioral replay override")
    requested_budgets = {
        "brightdata": brightdata_budget, "google_places": google_places_budget,
        "linkedin": linkedin_budget,
    }
    recorded_budgets = recorded.as_dict()["budgets"]
    for provider, value in requested_budgets.items():
        if value is not None and int(value) != int(recorded_budgets[provider]):
            raise ValueError(f"resume rejects behavioral budget override: {provider}")
    recorded.apply_effective_settings()
    return recorded


def validate_resume_before_credentials(input_file: Path, resume_run_dir: Path, *, allow_paid: bool | None = None,
                                       companies: set[str] | None = None, only_statuses: set[str] | None = None,
                                       from_run_manifest: Path | None = None, search_cache: str | None = None,
                                       crawl_cache: str | None = None, brightdata_budget: int | None = None,
                                       google_places_budget: int | None = None, linkedin_budget: int | None = None,
                                       rerank_cache: bool = False,
                                       finalize_without_paid: bool | None = None) -> None:
    """Read-only identity gate used before any credential setup or provider code."""
    run_root = Path(resume_run_dir).resolve()
    manifest_path = run_root / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("resume requires run_root/manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_config = resolve_run_config(
        manifest, allow_paid=allow_paid, search_cache=search_cache,
        crawl_cache=crawl_cache, brightdata_budget=brightdata_budget,
        google_places_budget=google_places_budget, linkedin_budget=linkedin_budget,
        rerank_cache=rerank_cache, finalize_without_paid=finalize_without_paid,
    )
    if manifest.get("paid_enabled") is False and bool(allow_paid):
        raise PermissionError("paid-disabled parent requires prepare_paid_continuation.py")
    records, _ = deduplicate_company_records(excel.read_company_records(Path(input_file)))
    if companies:
        wanted = {value.casefold() for value in companies}
        records = [record for record in records if record["company"].casefold() in wanted]
    if only_statuses:
        if not from_run_manifest:
            raise ValueError("--only-status requires --from-run-manifest")
        source_manifest = json.loads(Path(from_run_manifest).read_text(encoding="utf-8"))
        source_root = Path(from_run_manifest).resolve().parent
        artifact_dir = source_root / "output" / "artifacts" / str(source_manifest.get("artifact_set_sha256", ""))
        artifact = artifact_dir / "all_results.xlsx"
        info = source_manifest.get("files", {}).get("all_results.xlsx", {})
        if not source_manifest.get("complete") or not artifact.is_file() or checkpoint.file_hash(artifact) != info.get("sha256"):
            raise ValueError("source run manifest artifact hash mismatch")
        statuses = excel.read_result_statuses_by_source_id(artifact)
        allowed = {value.casefold() for value in only_statuses}
        records = [record for record in records if statuses.get(record["source_record_id"], "").casefold() in allowed]
    input_hash = checkpoint.file_hash(Path(input_file))
    _validate_resume_identity(
        run_root=run_root, manifest=manifest, input_hash=input_hash,
        run_config=run_config,
        ordered_source_record_ids=[record["source_record_id"] for record in records],
        lineage=manifest.get("lineage", {"type": "fresh"}),
    )


def deduplicate_company_records(records: list[dict]) -> tuple[list[dict], int]:
    """Keep distinct source records; only an identical source ID may merge."""
    unique: dict[str, dict] = {}
    result: list[dict] = []
    for index, record in enumerate(records):
        current = dict(record)
        source_id, quality = run_context.source_record_identity(current)
        current["source_record_id"] = source_id
        current["source_record_id_quality"] = quality
        existing = unique.get(source_id)
        if existing is None:
            unique[source_id] = current
            result.append(current)
            continue
        for field, value in current.items():
            if not existing.get(field) and value:
                existing[field] = value
    return result, len(records) - len(result)


def needs_paid_escalation(row: dict) -> bool:
    return bool(
        not row.get("__paid_escalation_complete")
        and (
            row.get("status") in _PAID_ESCALATION_STATUSES
            or row.get("reason") == "no_candidate_proved_target_fingerprint"
        )
    )


def result_quality_key(row: dict) -> tuple[int, ...]:
    status = str(row.get("status", ""))
    return (
        int(status in report.OK_STATUSES),
        int(bool(row.get("publication_eligible"))),
        int(bool(row.get("website"))),
        int(bool(row.get("email"))) + int(bool(row.get("phone"))),
        int(row.get("score") or 0),
        -int(row.get("reason") == "no_candidate_proved_target_fingerprint"),
    )


def _durable_output_rows(run_id: str, fallback: dict[int, dict] | None = None) -> list[dict]:
    """Use the transactionally persisted payload plus its item scheduler row."""
    persisted = checkpoint.load_results_by_id(run_id)
    item_state = {item["item_index"]: item for item in checkpoint.load_run_items(run_id)}
    rows = []
    for index in sorted(persisted):
        row = dict(persisted[index])
        row.setdefault("run_id", run_id)
        state = item_state.get(index)
        if state:
            row.update({
                "source_record_id": state["source_record_id"],
                "free_state": state["free_state"],
                "paid_required": bool(state["paid_required"]),
                "paid_state": state["paid_state"],
                "free_attempts": state["free_attempts"],
                "paid_attempts": state["paid_attempts"],
                "last_error": state["last_error"],
                "quarantine_state": state.get("quarantine_state", ""),
                "quarantine_status": state.get("quarantine_status", ""),
                "publication_blockers": state.get("publication_blockers", ""),
            })
        if output_artifacts.is_quarantined_row(row):
            row["publication_eligible"] = False
        rows.append(row)
    return rows


def _drain_memory_outbox(run_id: str, *, replay: bool) -> None:
    from modules import entity_memory
    if replay:
        checkpoint.skip_memory_outbox(run_id)
        return
    for entry in checkpoint.load_memory_outbox(run_id):
        payload = dict(entry["payload"])
        payload["__memory_receipt_key"] = entry["receipt_key"]
        entity_memory.remember([payload])
        checkpoint.complete_memory_receipt(run_id, entry["receipt_key"])


def run_pipeline(
    input_file: Path,
    output_dir: Path | None = None,
    companies: set[str] | None = None,
    only_statuses: set[str] | None = None,
    *,
    process_company_fn: Callable[..., Any],
    write_outputs_fn: Callable[..., Any],
    set_output_dir_fn: Callable[..., Any],
    empty_result_fn: Callable[..., dict],
    allow_paid: bool | None = None,
    run_dir: Path | None = None,
    resume_run_dir: Path | None = None,
    from_run_manifest: Path | None = None,
    finalize_without_paid: bool = False,
) -> str:
    previous_paid_limit = search._RUN_PAID_QUERY_LIMIT
    try:
        return _run_pipeline_impl(
            input_file,
            output_dir=output_dir,
            companies=companies,
            only_statuses=only_statuses,
            process_company_fn=process_company_fn,
            write_outputs_fn=write_outputs_fn,
            set_output_dir_fn=set_output_dir_fn,
            empty_result_fn=empty_result_fn,
            allow_paid=allow_paid,
            run_dir=run_dir,
            resume_run_dir=resume_run_dir,
            from_run_manifest=from_run_manifest,
            finalize_without_paid=finalize_without_paid,
        )
    finally:
        search._RUN_PAID_QUERY_LIMIT = previous_paid_limit


def _run_pipeline_impl(
    input_file: Path,
    output_dir: Path | None = None,
    companies: set[str] | None = None,
    only_statuses: set[str] | None = None,
    *,
    process_company_fn: Callable[..., Any],
    write_outputs_fn: Callable[..., Any],
    set_output_dir_fn: Callable[..., Any],
    empty_result_fn: Callable[..., dict],
    allow_paid: bool | None = None,
    run_dir: Path | None = None,
    resume_run_dir: Path | None = None,
    from_run_manifest: Path | None = None,
    finalize_without_paid: bool = False,
) -> str:
    owned_lease: dict[str, Any] = {}
    try:
        return _run_pipeline_impl_body(
            input_file, output_dir=output_dir, companies=companies,
            only_statuses=only_statuses,
            process_company_fn=process_company_fn,
            write_outputs_fn=write_outputs_fn,
            set_output_dir_fn=set_output_dir_fn,
            empty_result_fn=empty_result_fn,
            allow_paid=allow_paid, run_dir=run_dir,
            resume_run_dir=resume_run_dir,
            from_run_manifest=from_run_manifest,
            finalize_without_paid=finalize_without_paid,
            _owned_lease=owned_lease,
        )
    finally:
        lease = owned_lease.get("lease")
        if lease is not None:
            lease.release()


def _run_pipeline_impl_body(
    input_file: Path,
    output_dir: Path | None = None,
    companies: set[str] | None = None,
    only_statuses: set[str] | None = None,
    *,
    process_company_fn: Callable[..., Any],
    write_outputs_fn: Callable[..., Any],
    set_output_dir_fn: Callable[..., Any],
    empty_result_fn: Callable[..., dict],
    allow_paid: bool | None = None,
    run_dir: Path | None = None,
    resume_run_dir: Path | None = None,
    from_run_manifest: Path | None = None,
    finalize_without_paid: bool = False,
    _owned_lease: dict[str, Any] | None = None,
) -> str:
    previous_paid_enabled = bool(getattr(config, "PAID_ENABLED", True))
    if os.getenv("B2B_TEST_OFFLINE") != "1":
        require_safe_sqlite_for_live()
    if resume_run_dir:
        run_dir = Path(resume_run_dir)
    runtime.reset()
    logger = logging.getLogger("contact_finder")
    discovery_coverage.reset()
    replay_snapshot.reset()
    search.reset_candidate_host_observations()
    linkedin_company.reset()
    google_places.reset()
    start_time = time.monotonic()
    company_records = excel.read_company_records(input_file)
    for original_index, record in enumerate(company_records):
        record.setdefault("original_index", original_index)
    company_records, duplicate_count = deduplicate_company_records(company_records)
    for record in company_records:
        if not record.get("source_record_id"):
            source_id, quality = run_context.source_record_identity(record)
            record["source_record_id"] = source_id
            record["source_record_id_quality"] = quality
        discovery_coverage.register_source(
            record["source_record_id"],
            company=record.get("company", ""),
            original_index=record.get("original_index"),
            stage="input",
        )
    if duplicate_count:
        runtime.record("input.duplicates_removed", duplicate_count)
        logger.info("Removed %s duplicate company rows before processing", duplicate_count)
    if companies:
        wanted = {value.casefold() for value in companies}
        company_records = [record for record in company_records if record["company"].casefold() in wanted]
    if only_statuses:
        if not from_run_manifest:
            raise ValueError("--only-status requires a verified --from-run-manifest")
        manifest = json.loads(Path(from_run_manifest).read_text(encoding="utf-8"))
        if not manifest.get("complete") or not manifest.get("files"):
            raise ValueError("source run manifest is not complete")
        source_root = Path(from_run_manifest).resolve().parent
        artifact_dir = source_root / "output" / "artifacts" / str(manifest.get("artifact_set_sha256", ""))
        all_results_source = artifact_dir / "all_results.xlsx"
        file_info = manifest.get("files", {}).get("all_results.xlsx", {})
        if not all_results_source.exists() or not file_info or checkpoint.file_hash(all_results_source) != file_info.get("sha256"):
            raise ValueError("source run manifest artifact hash mismatch")
        previous_statuses = excel.read_result_statuses_by_source_id(
            all_results_source
        )
        allowed = {value.casefold() for value in only_statuses}
        company_records = [
            record for record in company_records
            if previous_statuses.get(record.get("source_record_id", ""), "").casefold() in allowed
        ]
    if not company_records:
        raise RuntimeError(f"No companies matched the requested selection in {input_file}")
    scorer.configure_company_token_frequencies([
        record["company"] for record in company_records
    ])
    resume_manifest = None
    if resume_run_dir:
        resume_manifest_path = Path(resume_run_dir) / "manifest.json"
        if not resume_manifest_path.exists():
            raise ValueError("resume requires run_root/manifest.json")
        resume_manifest = json.loads(resume_manifest_path.read_text(encoding="utf-8"))
        if resume_manifest.get("paid_enabled") is False and bool(allow_paid):
            raise PermissionError("complete paid-disabled runs require prepare_paid_continuation.py")
        run_config = resolve_run_config(
            resume_manifest, allow_paid=allow_paid,
            finalize_without_paid=finalize_without_paid,
        )
        allow_paid = run_config.paid_enabled
    else:
        allow_paid = bool(allow_paid)
        profile_hosts = {
            (urlparse(str(record.get("profile_url") or "")).hostname or "").casefold()
            for record in company_records
            if (urlparse(str(record.get("profile_url") or "")).hostname or "").strip()
        }
        # The effective per-run crawler cap is part of the frozen RunConfig.
        # Apply it to the live reservation gate before freezing that config;
        # otherwise the manifest and the physical limiter can disagree and a
        # later item can be starved by an inherited environment value.
        config.CRAWLER_HTTP_REQUEST_BUDGET = (
            19 * len(company_records) + 2 * len(profile_hosts)
        )
        run_config = resolve_run_config(
            None, allow_paid=allow_paid,
            finalize_without_paid=finalize_without_paid,
            input_count=len(company_records),
            unique_profile_host_count=len(profile_hosts),
        )
    paid_query_limit = search.configure_run_budget(len(company_records))
    paid_settings = {
        "search_provider": config.SEARCH_PROVIDER,
        "google_places": config.ENABLE_GOOGLE_PLACES,
        "brandfetch": config.ENABLE_BRANDFETCH_DOMAIN_SEARCH,
        "hunter_domain": config.ENABLE_HUNTER_DOMAIN_FINDER,
        "linkedin_enabled": config.ENABLE_LINKEDIN_COMPANY_LOOKUP,
        "llm_enabled": config.ENABLE_LLM_ARBITER,
        "brightdata_budget": config.BRIGHTDATA_REQUEST_BUDGET,
        "google_places_budget": config.GOOGLE_PLACES_REQUEST_BUDGET,
        "brandfetch_budget": config.BRANDFETCH_REQUEST_BUDGET,
        "hunter_budget": config.HUNTER_REQUEST_BUDGET,
        "linkedin_budget": config.LINKEDIN_COMPANY_REQUEST_BUDGET,
        "llm_budget": config.LLM_ARBITER_BUDGET,
    }
    paid_escalation_enabled = bool(
        allow_paid
        and config.SEARCH_CACHE_MODE != "replay"
        and (
            paid_settings["search_provider"] == "brightdata"
            or (paid_settings["google_places"] and config.GOOGLE_PLACES_API_KEY)
            or paid_settings["brandfetch"]
            or paid_settings["hunter_domain"]
            or paid_settings["linkedin_enabled"]
            or paid_settings["llm_enabled"]
        )
    )

    run_signature = json.dumps(
        {
            "companies": sorted(record["company"] for record in company_records),
            "search_cache": config.SEARCH_CACHE_MODE,
            "crawl_cache": config.CRAWL_CACHE_MODE,
            "brightdata_budget": config.BRIGHTDATA_REQUEST_BUDGET,
            "linkedin_company_budget": config.LINKEDIN_COMPANY_REQUEST_BUDGET,
            "linkedin_company_enabled": config.ENABLE_LINKEDIN_COMPANY_LOOKUP,
            "paid_query_limit_per_company": paid_query_limit,
            "google_places_budget": config.GOOGLE_PLACES_REQUEST_BUDGET,
            "brandfetch_budget": config.BRANDFETCH_REQUEST_BUDGET,
            "hunter_budget": config.HUNTER_REQUEST_BUDGET,
            "replay_snapshot": str(config.REPLAY_SNAPSHOT_INPUT or ""),
            "two_pass_paid_escalation": paid_escalation_enabled,
            "allow_paid": bool(allow_paid),
            "finalize_without_paid": bool(finalize_without_paid),
            "run_config_sha256": run_config.sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    input_hash = checkpoint.file_hash(input_file)
    context = run_context.new_context(
        input_hash, run_config,
        ordered_source_record_ids=[record.get("source_record_id", f"input:{idx}") for idx, record in enumerate(company_records)],
        lineage=(resume_manifest or {}).get("lineage", {"type": "fresh"}),
    )
    if run_dir is None:
        run_root = Path(config.RUNS_DIR) / context.run_id
        set_output_dir_fn(run_root / "output")
        runtime_paths.set_run_state_dir(run_root / "state")
    else:
        run_root = Path(run_dir)
        if run_root.name != context.run_id:
            raise ValueError("--run-dir must end with the canonical run_id")
    manifest_path = run_root / "manifest.json"
    lease = run_context.RunLease(run_root)
    lease.acquire()
    if _owned_lease is not None:
        _owned_lease["lease"] = lease
    if run_dir is not None:
        set_output_dir_fn(run_root / "output")
        runtime_paths.set_run_state_dir(run_root / "state")
    elif output_dir:
        set_output_dir_fn(output_dir)
    # Durable host preflight is the first network-capable operation after the
    # run lease.  This also makes the free-only handoff safe before approval.
    checkpoint.initialize_schema(run_root / "state" / "progress.sqlite3")
    runtime.configure_durable_run(context.run_id, run_config.as_dict().get("budgets", {}))
    # Resume identity is a local, fail-closed check and must precede every
    # physical source probe or provider-capable operation.
    if resume_manifest:
        require_complete_manifest_phase(resume_manifest)
        _validate_resume_identity(
            run_root=run_root, manifest=resume_manifest, input_hash=input_hash,
            run_config=run_config,
            ordered_source_record_ids=[record["source_record_id"] for record in company_records],
            lineage=(resume_manifest or {}).get("lineage", {"type": "fresh"}),
        )
    # Load/configure replay identity before source preflight.  A replay miss
    # must fail locally before any source probe can become physical I/O.
    if config.REPLAY_SNAPSHOT_INPUT:
        replay_snapshot.load(Path(config.REPLAY_SNAPSHOT_INPUT), max_uncompressed_bytes=config.REPLAY_SNAPSHOT_MAX_UNCOMPRESSED_BYTES)
    replay_snapshot.configure_run_store(run_root / "state" / "progress.sqlite3", context.run_id)
    if config.REPLAY_MANIFEST_INPUT:
        replay_snapshot.load_shards(Path(config.REPLAY_MANIFEST_INPUT), expected_run_id=context.run_id, expected_config_hash=run_config.sha256, max_uncompressed_bytes=config.REPLAY_SNAPSHOT_MAX_UNCOMPRESSED_BYTES)
    source_preflight = []
    if not (resume_manifest and resume_manifest.get("complete")):
        source_preflight = search.preflight_source_profiles(company_records, run_id=context.run_id)
        for health in source_preflight:
            logger.info(
                "Source profile preflight: host=%s status=%s server_errors=%s circuit_open=%s",
                health.get("host", ""), health.get("status", "unknown"),
                health.get("server_errors", 0), health.get("circuit_open", False),
            )
    if resume_manifest:
        if resume_manifest.get("complete"):
            _verify_complete_artifacts(run_root, resume_manifest)
            _verify_artifact_metadata(
                run_root,
                {
                    "artifact_set_sha256": resume_manifest.get("artifact_set_sha256", ""),
                    "files": resume_manifest.get("files", {}),
                },
            )
            intent = checkpoint.load_finalization_intent(context.run_id)
            if intent and intent.get("artifact_set_sha256") and intent.get("artifact_set_sha256") != resume_manifest.get("artifact_set_sha256"):
                raise RuntimeError("complete finalization intent artifact identity mismatch")
            if not intent or not intent.get("memory_plan_committed"):
                checkpoint.mark_legacy_no_memory_plan(
                    context.run_id,
                    artifact_set_sha256=str(resume_manifest.get("artifact_set_sha256", "")),
                    manifest_sha256=checkpoint.file_hash(manifest_path),
                )
                intent = checkpoint.load_finalization_intent(context.run_id)
            if intent and intent.get("status") in {"STARTED", "ARTIFACT_READY"} and intent.get("memory_plan_committed"):
                checkpoint.complete_finalization_intent(
                    run_id=context.run_id,
                    artifact_set_sha256=str(resume_manifest.get("artifact_set_sha256", "")),
                    manifest_sha256=checkpoint.file_hash(manifest_path),
                )
            if resume_manifest.get("phase") == "COMPLETE":
                state = checkpoint.load_run_state_by_id(context.run_id)
                if state and state.get("phase") == "FINALIZING":
                    checkpoint.validate_finalization_contract(context.run_id, run_root, require_complete=False)
                    checkpoint.complete_finalization_phase(context.run_id, expected_count=len(company_records))
                if intent and intent.get("memory_plan_committed"):
                    entries = checkpoint.load_memory_outbox_entries(context.run_id)
                    if len(entries) != int(intent.get("memory_plan_count", 0)):
                        raise RuntimeError("complete resume memory plan is incomplete")
                checkpoint.validate_finalization_contract(context.run_id, run_root)
                if intent and intent.get("memory_plan_committed"):
                    _drain_memory_outbox(
                        context.run_id,
                        replay=config.SEARCH_CACHE_MODE == "replay" or config.CRAWL_CACHE_MODE == "replay",
                    )
                    entries = checkpoint.load_memory_outbox_entries(context.run_id)
                    if any(entry["state"] not in {"DONE", "SKIPPED_REPLAY"} for entry in entries):
                        raise RuntimeError("complete resume left memory outbox entries")
                checkpoint.validate_finalization_contract(context.run_id, run_root)
                _verify_artifact_metadata(
                    run_root,
                    {
                        "artifact_set_sha256": resume_manifest.get("artifact_set_sha256", ""),
                        "files": resume_manifest.get("files", {}),
                    },
                )
                run_context.validate_run_bundle(run_root, expected_input_hash=input_hash, expected_config_hash=run_config.sha256, expected_source_ids=[record["source_record_id"] for record in company_records], profile="COMPLETE")
            lease.release()
            return "COMPLETE_RESUME_VERIFIED"
        # FINALIZING resumes reconcile the immutable artifact boundary below;
        # they must not enter either provider phase.
        resume_state = checkpoint.load_run_state_by_id(context.run_id)
        resume_intent = checkpoint.load_finalization_intent(context.run_id)
        if resume_state and resume_state.get("phase") == "FINALIZING" and resume_intent and resume_intent.get("status") == "ARTIFACT_READY":
            prepared = json.loads(resume_intent.get("output_context_json") or "{}")
            prepared_artifacts = prepared.get("artifacts") or {}
            _verify_artifact_metadata(run_root, prepared_artifacts)
            if not resume_intent.get("memory_plan_committed"):
                prepared_memory_rows = prepared.get("memory_rows")
                if not isinstance(prepared_memory_rows, list):
                    raise RuntimeError("prepared finalization has no recoverable memory rows")
                checkpoint.enqueue_memory_rows(context.run_id, prepared_memory_rows)
                checkpoint.commit_finalization_memory_plan(
                    run_id=context.run_id,
                    generation=str(resume_intent.get("generation", "")),
                    result_snapshot_sha256=str(resume_intent.get("result_snapshot_sha256", "")),
                    telemetry_snapshot=json.loads(resume_intent.get("telemetry_snapshot_json") or "{}"),
                )
                resume_intent = checkpoint.load_finalization_intent(context.run_id) or resume_intent
            run_context.validate_run_bundle(
                run_root, expected_input_hash=input_hash,
                expected_config_hash=run_config.sha256,
                expected_source_ids=[record["source_record_id"] for record in company_records],
                profile="FINALIZING", require_artifacts=False,
            )
            output_artifacts.publish_manifest(
                path=manifest_path, run_id=context.run_id, input_hash=input_hash,
                config_sha256=run_config.sha256,
                counts=prepared.get("counts", {"input_count": len(company_records), "result_count": len(company_records)}),
                artifacts=prepared_artifacts,
                telemetry=checkpoint.derive_telemetry(context.run_id),
            )
            checkpoint.complete_finalization_intent(
                run_id=context.run_id,
                artifact_set_sha256=str(prepared_artifacts["artifact_set_sha256"]),
                manifest_sha256=checkpoint.file_hash(manifest_path),
            )
            checkpoint.validate_finalization_contract(context.run_id, run_root, require_complete=False)
            checkpoint.complete_finalization_phase(context.run_id, expected_count=len(company_records))
            if not resume_intent.get("memory_plan_committed"):
                raise RuntimeError("prepared finalization has no committed memory plan")
            if len(checkpoint.load_memory_outbox_entries(context.run_id)) != int(resume_intent.get("memory_plan_count", 0)):
                raise RuntimeError("prepared finalization memory plan is incomplete")
            checkpoint.validate_finalization_contract(context.run_id, run_root)
            _drain_memory_outbox(
                context.run_id,
                replay=config.SEARCH_CACHE_MODE == "replay" or config.CRAWL_CACHE_MODE == "replay",
            )
            if any(entry["state"] not in {"DONE", "SKIPPED_REPLAY"} for entry in checkpoint.load_memory_outbox_entries(context.run_id)):
                raise RuntimeError("prepared finalization left memory outbox entries")
            lease.release()
            return "FINALIZATION_RESUME_RECONCILED"
    ensure_directories()
    logger = setup_logging()
    prior_state = checkpoint.load_run_state_by_id(context.run_id)
    budgets = run_config.as_dict().get("budgets", {})
    config.PAID_ENABLED = bool(allow_paid)
    runtime.configure_durable_run(context.run_id, budgets)
    if prior_state:
        checkpoint.recover_interrupted_items(context.run_id)
    if prior_state and prior_state.get("context"):
        saved_context = prior_state["context"]
        context = run_context.RunContext(
            run_id=context.run_id,
            input_hash=input_hash,
            code_revision=str(saved_context.get("code_revision", context.code_revision)),
            phase=str(prior_state.get("phase", saved_context.get("phase", "FREE"))),
            started_at=str(saved_context.get("started_at", context.started_at)),
            resume_history=tuple(saved_context.get("resume_history", ())),
        )
    else:
        checkpoint.initialize_run(
            run_id=context.run_id, input_hash=input_hash, run_signature=run_signature,
            context=context.as_dict(), budgets=budgets,
            items=[{"item_index": idx, "source_record_id": record["source_record_id"], "paid_required": False}
                   for idx, record in enumerate(company_records)],
        )
    run_context.write_manifest(
        manifest_path, context, run_config, complete=False,
        extra={
            "run_signature": run_signature,
            "input_count": len(company_records),
            "item_count": len(company_records),
            "ordered_source_record_ids": [record["source_record_id"] for record in company_records],
            "runtime_source_tree_sha256": run_context.source_tree_sha256(),
            "lineage": (resume_manifest or {}).get("lineage", {"type": "fresh"}),
            "phase": context.phase,
            "paid_enabled": run_config.paid_enabled,
            "finalization_mode": "free_only" if finalize_without_paid else "normal",
        },
    )
    if prior_state and prior_state.get("runtime_snapshot"):
        runtime.restore(prior_state["runtime_snapshot"])
    results_by_index: dict[int, dict] = {}
    results_by_index.update(checkpoint.load_results_by_id(context.run_id))

    item_states = {item["item_index"]: item for item in checkpoint.load_run_items(context.run_id)}
    checkpoint.reconcile_unknown_provider_calls(context.run_id)
    pending = [
        (idx, record)
        for idx, record in enumerate(company_records)
        if item_states.get(idx, {}).get("free_state") == "PENDING"
    ]
    resume_phase = str((prior_state or {}).get("phase", "FREE"))
    def execute_phase(items: list[tuple[int, dict]], *, paid_phase: bool) -> None:
        if not items:
            return
        phase_name = "PAID" if paid_phase else "FREE"
        runtime.set_phase(phase_name)
        if paid_phase:
            runtime.record("pipeline.paid_total", len(items))
        else:
            runtime.record("pipeline.free_total", len(items))
        def process_one(idx: int, record: dict):
            runtime.set_item_context(idx, phase_name.lower(), record.get("source_record_id", ""))
            return process_company_fn(idx, record["company"], logger, record.get("website", ""), record)
        def run_one(idx: int, record: dict):
            if not checkpoint.claim_item(run_id=context.run_id, item_index=idx, phase=phase_name):
                return None
            if paid_phase:
                checkpoint.begin_paid_attempt(
                    run_id=context.run_id, item_index=idx,
                    attempt_number=int(item_states.get(idx, {}).get("paid_attempts", 0)) + 1,
                )
            provider_token = runtime.begin_provider_attempt() if paid_phase else None
            calls_before = {call["call_id"] for call in checkpoint.provider_calls_for_item(context.run_id, idx)} if paid_phase else set()
            processing_error = None
            try:
                result = process_one(idx, record)
            except Exception as exc:
                processing_error = exc
                exc.__dict__["_provider_call_ids_before"] = sorted(calls_before)
                raise
            finally:
                provider_outcomes = runtime.end_provider_attempt(provider_token) if paid_phase else []
                if processing_error is not None:
                    processing_error.__dict__["_provider_results"] = provider_outcomes
            if paid_phase and result is not None:
                result[1]["__provider_call_ids_before"] = sorted(calls_before)
                result[1]["provider_results"] = provider_outcomes
            return result
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            futures = {
                executor.submit(run_one, idx, record): idx
                for idx, record in items
            }
            for future in as_completed(futures):
                retried = False
                try:
                    result = future.result()
                    if result is None:
                        continue
                    idx, row = result
                except Exception as exc:
                    idx = futures[future]
                    company = company_records[idx]["company"]
                    logger.exception("Unhandled processing failure for %s", company)
                    if not paid_phase:
                        # A transient free-pass failure gets exactly one retry;
                        # a second failure is terminal and remains auditable.
                        retried = True
                        try:
                            idx, row = process_one(idx, company_records[idx])
                            row["attempt_number"] = 2
                        except Exception as retry_exc:
                            logger.exception("Free retry failed for %s", company)
                            row = empty_result_fn(
                                company, "PROCESSING_FAILED",
                                f"attempt_2:{retry_exc.__class__.__name__}: {retry_exc}",
                            )
                            row["attempt_number"] = 2
                    else:
                        row = empty_result_fn(
                            company, "PROCESSING_FAILED",
                            f"{exc.__class__.__name__}: {exc}",
                        )
                        row["attempt_number"] = 1
                        row["paid_attempt_result"] = paid_attempt_result(exception=exc)
                        if paid_phase:
                            row["__provider_call_ids_before"] = getattr(exc, "_provider_call_ids_before", [])
                            row["provider_results"] = getattr(exc, "_provider_results", [])
                if not paid_phase and not retried and str(row.get("status", "")).upper() in FREE_FAILURE_STATUSES:
                    try:
                        retry_idx, retry_row = process_one(idx, company_records[idx])
                        idx, row = retry_idx, retry_row
                        row["attempt_number"] = 2
                        retried = True
                    except Exception as retry_exc:
                        row = empty_result_fn(
                            company_records[idx]["company"], "PROCESSING_FAILED",
                            f"attempt_2:{retry_exc.__class__.__name__}: {retry_exc}",
                        )
                        row["attempt_number"] = 2
                row["__index"] = idx
                row.setdefault("source_record_id", company_records[idx].get("source_record_id", ""))
                row.setdefault("original_index", company_records[idx].get("original_index", idx))
                if company_records[idx].get("_id"):
                    row.setdefault("_id", company_records[idx]["_id"])
                existing_item = item_states.get(idx, {})
                attempt_reason = str(row.get("reason", ""))
                if paid_phase:
                    before_ids = set(row.pop("__provider_call_ids_before", []))
                    provider_calls = [
                        call for call in checkpoint.provider_calls_for_item(context.run_id, idx)
                        if not before_ids or call["call_id"] not in before_ids
                    ]
                    durable_outcomes = [
                        {"result_state": {"DONE": "COMPLETED", "FAILED": "FAILED", "UNKNOWN": "UNKNOWN"}.get(call["state"], call["state"]),
                         "result_reason": call["state"], "call_ids": [call["call_id"]]}
                        for call in provider_calls
                    ]
                    row["provider_results"] = list(row.get("provider_results", [])) + durable_outcomes
                attempt_result = paid_attempt_result(row) if paid_phase else "NO_CALL_NEEDED"
                if paid_phase:
                    explicit_no_call = str(row.get("paid_attempt_result", "")).upper() == "NO_CALL_NEEDED" or str(row.get("status", "")).upper() == "NO_CALL_NEEDED"
                    inherited_result, inherited_ref = _inherited_paid_result(row)
                    if inherited_result and not provider_calls:
                        attempt_result = inherited_result
                        row["paid_result_ref"] = inherited_ref
                    if attempt_result == "COMPLETED" and not provider_calls and not explicit_no_call:
                        attempt_result = "FAILED"
                        row["reason"] = "; ".join(filter(None, [str(row.get("reason", "")), "paid_provider_execution_missing"]))
                        attempt_reason = str(row.get("reason", ""))
                    single_call = provider_calls[0] if len(provider_calls) == 1 else {"call_id": "", "request_fingerprint": ""}
                    checkpoint.record_paid_attempt(
                        run_id=context.run_id, item_index=idx,
                        attempt_number=int(existing_item.get("paid_attempts", 0)) + 1,
                        result=attempt_result, reason=attempt_reason,
                        call_id=str(row.get("call_id") or single_call["call_id"]),
                        request_fingerprint=str(row.get("request_fingerprint") or single_call["request_fingerprint"]),
                        call_ids=[call["call_id"] for call in provider_calls],
                    )
                if paid_phase and idx in results_by_index:
                    previous = results_by_index[idx]
                    if result_quality_key(previous) > result_quality_key(row):
                        row = previous
                if existing_item.get("quarantine_state"):
                    output_artifacts.apply_quarantine(
                        row, state=str(existing_item["quarantine_state"]),
                        status=str(existing_item.get("quarantine_status", "")),
                        blockers=str(existing_item.get("publication_blockers", "")),
                    )
                row["__paid_escalation_complete"] = bool(
                    (paid_phase and attempt_result in {"COMPLETED", "NO_CALL_NEEDED"})
                    or (not paid_phase and (allow_paid and not paid_escalation_enabled or row.get("status") in report.OK_STATUSES))
                )
                results_by_index[idx] = row
                paid_result = attempt_result
                row["paid_attempt_result"] = paid_result if paid_phase else row.get("paid_attempt_result", "NO_CALL_NEEDED")
                if paid_phase:
                    row["paid_attempt_reason"] = attempt_reason
                if paid_phase:
                    free_state = existing_item.get("free_state", "DONE")
                    paid_state = {"COMPLETED": "DONE", "NO_CALL_NEEDED": "DONE", "FAILED": "FAILED", "UNKNOWN": "UNKNOWN", "BLOCKED_BUDGET": "BLOCKED_BUDGET"}[paid_result]
                    scheduler_states = {"free_state": free_state, "paid_state": paid_state, "paid_required": True}
                else:
                    scheduler_states = classify_scheduler_states(row, attempt_number=int(row.get("attempt_number", 1)), publication_gate=output_artifacts.is_publishable_row(row))
                    free_state = str(scheduler_states["free_state"])
                    paid_state = str(scheduler_states["paid_state"])
                checkpoint.save_item_transaction(
                    run_id=context.run_id, item_index=idx,
                    source_record_id=company_records[idx]["source_record_id"], payload=row,
                    free_state=free_state,
                    paid_state=paid_state,
                    paid_required=bool(scheduler_states["paid_required"]),
                    free_attempts=int(row.get("attempt_number", 1)),
                    paid_attempts=(int(existing_item.get("paid_attempts", 0)) + 1) if paid_phase else int(existing_item.get("paid_attempts", 0)),
                    last_error=attempt_reason if paid_phase else str(row.get("reason", "")),
                )
                durable_telemetry = checkpoint.derive_telemetry(context.run_id)
                counter = "paid_completed" if paid_phase else "free_completed"
                total_counter = "paid_total" if paid_phase else "free_total"
                runtime.record(f"pipeline.{counter}")
                logger.info(
                    "%s=%s %s=%s attempt_number=%s input_index=%s company=%s",
                    counter, durable_telemetry[counter], total_counter,
                    durable_telemetry["paid_required" if paid_phase else "total_items"], row.get("attempt_number", 1), idx, row["company"],
                )

    try:
        config.SEARCH_PROVIDER = "ddgs"
        config.ENABLE_GOOGLE_PLACES = False
        config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = False
        config.ENABLE_HUNTER_DOMAIN_FINDER = False
        config.ENABLE_LINKEDIN_COMPANY_LOOKUP = False
        config.ENABLE_LLM_ARBITER = False
        config.BRIGHTDATA_REQUEST_BUDGET = 0
        config.GOOGLE_PLACES_REQUEST_BUDGET = 0
        config.BRANDFETCH_REQUEST_BUDGET = 0
        config.HUNTER_REQUEST_BUDGET = 0
        config.LINKEDIN_COMPANY_REQUEST_BUDGET = 0
        config.LLM_ARBITER_BUDGET = 0
        runtime.record("pipeline.free_pass_companies", len(pending))
        if resume_phase not in {"PAID", "FINALIZING", "COMPLETE"}:
            execute_phase(pending, paid_phase=False)

        config.SEARCH_PROVIDER = paid_settings["search_provider"] if allow_paid else "ddgs"
        config.ENABLE_GOOGLE_PLACES = paid_settings["google_places"] if allow_paid else False
        config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = paid_settings["brandfetch"] if allow_paid else False
        config.ENABLE_HUNTER_DOMAIN_FINDER = paid_settings["hunter_domain"] if allow_paid else False
        config.ENABLE_LINKEDIN_COMPANY_LOOKUP = paid_settings["linkedin_enabled"] if allow_paid else False
        config.ENABLE_LLM_ARBITER = paid_settings["llm_enabled"] if allow_paid else False
        config.BRIGHTDATA_REQUEST_BUDGET = paid_settings["brightdata_budget"] if allow_paid else 0
        config.GOOGLE_PLACES_REQUEST_BUDGET = paid_settings["google_places_budget"] if allow_paid else 0
        config.BRANDFETCH_REQUEST_BUDGET = paid_settings["brandfetch_budget"] if allow_paid else 0
        config.HUNTER_REQUEST_BUDGET = paid_settings["hunter_budget"] if allow_paid else 0
        config.LINKEDIN_COMPANY_REQUEST_BUDGET = paid_settings["linkedin_budget"] if allow_paid else 0
        config.LLM_ARBITER_BUDGET = paid_settings["llm_budget"] if allow_paid else 0
        paid_indexes = checkpoint.freeze_paid_queue(context.run_id)
        if finalize_without_paid and resume_phase == "FREE":
            if allow_paid:
                raise RuntimeError("free-only finalization cannot enable paid providers")
            checkpoint.finalize_free_only_queue(context.run_id)
            paid_indexes = []
        if resume_phase == "FREE" and not allow_paid and paid_indexes and not finalize_without_paid:
            telemetry = checkpoint.derive_telemetry(context.run_id)
            expected = len(company_records)
            if not (
                telemetry["item_terminal"] == expected
                and telemetry["result_count"] == expected
                and telemetry["manifest_count"] == expected
            ):
                raise RuntimeError(f"handoff telemetry is not an exact terminal snapshot: {telemetry}")
            checkpoint.mark_handoff_pending(
                run_id=context.run_id, expected_count=expected,
            )
        escalation = [(idx, company_records[idx]) for idx in paid_indexes if idx < len(company_records)]
        if escalation and not allow_paid and not finalize_without_paid:
            if resume_phase == "FREE":
                checkpoint.transition_phase(context.run_id, "PAID", expected_count=len(company_records))
            # Derive the last mutable telemetry view before sealing.  From this
            # point through manifest publication no application-level reads or
            # writes are allowed to alter the handoff state.
            handoff_telemetry = checkpoint.derive_telemetry(context.run_id)
            checkpoint_path = run_root / "state" / "progress.sqlite3"
            sealed = checkpoint.seal_checkpoint_for_handoff(
                checkpoint_path,
                run_id=context.run_id,
                expected_count=len(company_records),
            )
            artifact_dir = run_root / "output" / "artifacts" / sealed["sha256"]
            artifact_dir.mkdir(parents=True, exist_ok=True)
            frozen_checkpoint = artifact_dir / "recovery_state.sqlite3"
            handoff_snapshot = checkpoint.create_handoff_snapshot(
                checkpoint_path,
                frozen_checkpoint,
                run_id=context.run_id,
                expected_count=len(company_records),
            )
            frozen_hash = str(handoff_snapshot["sha256"])
            frozen_bytes = int(handoff_snapshot["bytes"])
            artifact_set_sha256 = hashlib.sha256(f"recovery_state.sqlite3:{frozen_hash}\n".encode()).hexdigest()
            target_artifact_dir = run_root / "output" / "artifacts" / artifact_set_sha256
            if target_artifact_dir != artifact_dir:
                target_artifact_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(frozen_checkpoint, target_artifact_dir / frozen_checkpoint.name)
                shutil.rmtree(artifact_dir, ignore_errors=True)
                artifact_dir = target_artifact_dir
            # The sanitized handoff snapshot is the run's frozen checkpoint;
            # make the live checkpoint byte-identical before publishing the
            # manifest so FROZEN_RECOVERY validation has one immutable source.
            checkpoint.replace_handoff_checkpoint(
                artifact_dir / frozen_checkpoint.name, checkpoint_path,
                expected_sha256=frozen_hash, expected_bytes=frozen_bytes,
            )
            files = {"recovery_state.sqlite3": {"sha256": frozen_hash, "bytes": frozen_bytes}}
            run_context.write_manifest(
                manifest_path, context.with_phase("PAID"), run_config, complete=False,
                extra={
                    "run_signature": run_signature,
                    "input_count": len(company_records),
                    "item_count": len(company_records),
                    "ordered_source_record_ids": [record["source_record_id"] for record in company_records],
                    "runtime_source_tree_sha256": run_context.source_tree_sha256(),
                    "lineage": (resume_manifest or {}).get("lineage", {"type": "fresh"}),
                    "phase": "PAID",
                    "paid_enabled": False,
                    "paid_pending": len(escalation),
                    "artifact_set_sha256": artifact_set_sha256,
                    "files": files,
                    "checkpoint_sha256": frozen_hash,
                    "handoff": True,
                    "telemetry": handoff_telemetry,
                },
            )
            lease.release()
            return "PAID_PENDING_APPROVAL"
        if resume_phase != "FINALIZING" and paid_escalation_enabled and escalation:
            runtime.record("pipeline.paid_escalation_companies", len(escalation))
            if resume_phase != "PAID":
                checkpoint.transition_phase(context.run_id, "PAID", expected_count=len(company_records))
                search.scale_paid_api_budgets(len(escalation))
                search.configure_run_budget(len(escalation))
            execute_phase(escalation, paid_phase=True)
    except KeyboardInterrupt:
        logger.warning("Interrupted. Progress checkpoint was saved.")
        raise
    finally:
        config.PAID_ENABLED = previous_paid_enabled
        config.SEARCH_PROVIDER = paid_settings["search_provider"]
        config.ENABLE_GOOGLE_PLACES = paid_settings["google_places"]
        config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = paid_settings["brandfetch"]
        config.ENABLE_HUNTER_DOMAIN_FINDER = paid_settings["hunter_domain"]
        config.ENABLE_LINKEDIN_COMPANY_LOOKUP = paid_settings["linkedin_enabled"]
        config.ENABLE_LLM_ARBITER = paid_settings["llm_enabled"]
        config.BRIGHTDATA_REQUEST_BUDGET = paid_settings["brightdata_budget"]
        config.GOOGLE_PLACES_REQUEST_BUDGET = paid_settings["google_places_budget"]
        config.BRANDFETCH_REQUEST_BUDGET = paid_settings["brandfetch_budget"]
        config.HUNTER_REQUEST_BUDGET = paid_settings["hunter_budget"]
        config.LINKEDIN_COMPANY_REQUEST_BUDGET = paid_settings["linkedin_budget"]
        config.LLM_ARBITER_BUDGET = paid_settings["llm_budget"]
        pass

    manual_review_items = [
        item for item in checkpoint.load_run_items(context.run_id)
        if item.get("paid_required") and item.get("paid_state") in {"UNKNOWN", "BLOCKED_BUDGET"}
    ]
    if manual_review_items:
        context = context.with_phase("PAID")
        run_context.write_manifest(
            manifest_path, context, run_config, complete=False,
            extra={
                "phase": "PAID",
                "paid_pending": 0,
                "manual_authorization_review_required": True,
                "manual_review_item_indexes": [item["item_index"] for item in manual_review_items],
            },
        )
        lease.release()
        return "PAID_MANUAL_AUTHORIZATION_REVIEW_REQUIRED"

    if any(str(item.get("quarantine_state", "")) == "HANDOFF_PENDING" for item in checkpoint.load_run_items(context.run_id)):
        checkpoint.release_handoff_pending(context.run_id, expected_count=len(company_records))

    if resume_phase != "FINALIZING":
        checkpoint.transition_phase(context.run_id, "FINALIZING", expected_count=len(company_records))
    runtime.set_phase("FINALIZING")
    existing_intent = checkpoint.load_finalization_intent(context.run_id)
    if existing_intent and existing_intent.get("status") == "STARTED":
        result_snapshot = str(existing_intent.get("result_snapshot_sha256", ""))
        generation = str(existing_intent.get("generation", ""))
        telemetry_snapshot = json.loads(existing_intent.get("telemetry_snapshot_json") or "{}")
        if not result_snapshot or not generation or not telemetry_snapshot:
            raise RuntimeError("finalization intent has no frozen telemetry snapshot")
        output_context = json.loads(existing_intent.get("output_context_json") or "{}")
        finalization_elapsed = float(output_context.get("elapsed_seconds", telemetry_snapshot.get("elapsed_seconds", 0)))
    else:
        result_snapshot = checkpoint.result_snapshot_sha256(context.run_id)
        generation = hashlib.sha256(f"{context.run_id}:{result_snapshot}".encode("utf-8")).hexdigest()
        telemetry_snapshot = {"durable_scheduler": checkpoint.derive_telemetry(context.run_id)}
        finalization_elapsed = float(telemetry_snapshot.get("elapsed_seconds", time.monotonic() - start_time))
        checkpoint.begin_finalization_intent(
            run_id=context.run_id, generation=generation,
            input_snapshot_sha256=result_snapshot,
            result_snapshot_sha256=result_snapshot,
            output_context={"elapsed_seconds": finalization_elapsed},
            telemetry_snapshot=telemetry_snapshot,
        )
    checkpoint.validate_run_invariants(context.run_id, expected_count=len(company_records), require_payloads=True)
    rows = _durable_output_rows(context.run_id)
    intent_context = checkpoint.load_finalization_intent(context.run_id) or {}
    output_context = json.loads(intent_context.get("output_context_json") or "{}")
    frozen_telemetry = json.loads(intent_context.get("telemetry_snapshot_json") or "{}") or telemetry_snapshot
    report_text = _call_writer(
        write_outputs_fn, rows,
        float(output_context.get("elapsed_seconds", finalization_elapsed)),
        frozen_telemetry,
        run_config.sha256,
    )
    artifact_result = report_text
    artifacts = getattr(artifact_result, "artifacts", None)
    if not artifacts:
        lease.release()
        raise RuntimeError("writer must return immutable artifact metadata")
    memory_rows = getattr(artifact_result, "entity_memory_rows", [])
    _verify_artifact_metadata(run_root, artifacts)
    run_context.validate_run_bundle(
        run_root, expected_input_hash=input_hash,
        expected_config_hash=run_config.sha256,
        expected_source_ids=[record["source_record_id"] for record in company_records],
        profile="FINALIZING", require_artifacts=False,
    )
    checkpoint.mark_finalization_artifact(
        run_id=context.run_id,
        artifact_set_sha256=str(artifacts.get("artifact_set_sha256", "")),
        output_context={
            "artifacts": artifacts,
            "memory_rows": memory_rows,
            "counts": {"input_count": len(company_records), "result_count": len(rows)},
            "telemetry_snapshot": frozen_telemetry,
            "elapsed_seconds": float(output_context.get("elapsed_seconds", finalization_elapsed)),
        },
    )
    checkpoint.enqueue_memory_rows(context.run_id, memory_rows)
    checkpoint.commit_finalization_memory_plan(
        run_id=context.run_id,
        generation=generation,
        result_snapshot_sha256=result_snapshot,
        telemetry_snapshot=frozen_telemetry,
    )
    output_artifacts.publish_manifest(
        path=manifest_path, run_id=context.run_id, input_hash=input_hash,
        config_sha256=run_config.sha256,
        counts={"input_count": len(company_records), "result_count": len(rows)},
        artifacts=artifacts,
        telemetry=checkpoint.derive_telemetry(context.run_id),
    )
    checkpoint.complete_finalization_intent(
        run_id=context.run_id,
        artifact_set_sha256=str(artifacts.get("artifact_set_sha256", "")),
        manifest_sha256=checkpoint.file_hash(manifest_path),
    )
    checkpoint.validate_finalization_contract(context.run_id, run_root, require_complete=False)
    checkpoint.transition_phase(context.run_id, "COMPLETE", expected_count=len(company_records))
    context = context.with_phase("COMPLETE")
    runtime.set_phase("COMPLETE")
    checkpoint.validate_finalization_contract(context.run_id, run_root)
    _drain_memory_outbox(
        context.run_id,
        replay=config.SEARCH_CACHE_MODE == "replay" or config.CRAWL_CACHE_MODE == "replay",
    )
    if any(entry["state"] not in {"DONE", "SKIPPED_REPLAY"} for entry in checkpoint.load_memory_outbox_entries(context.run_id)):
        raise RuntimeError("finalization left memory outbox entries")
    lease.release()
    return str(report_text)
