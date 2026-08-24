from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import config
from modules import (
    checkpoint,
    discovery_coverage,
    excel,
    linkedin_company,
    replay_snapshot,
    report,
    runtime,
    scorer,
    search,
)
from modules.utils import ensure_directories, setup_logging


_PAID_ESCALATION_STATUSES = {
    "REVIEW_NEEDED", "WEBSITE_NOT_FOUND", "WEBSITE_AMBIGUOUS",
    "WEBSITE_FETCH_FAILED",
}


def deduplicate_company_records(records: list[dict]) -> tuple[list[dict], int]:
    """Merge duplicate fair rows without losing the richer metadata record."""
    unique: dict[str, dict] = {}
    duplicate_count = 0
    for record in records:
        key = scorer.normalize_text(record.get("company", "")).strip()
        if not key:
            continue
        if key not in unique:
            unique[key] = dict(record)
            continue
        duplicate_count += 1
        current = unique[key]
        for field, value in record.items():
            if not current.get(field) and value:
                current[field] = value
        sources = list(dict.fromkeys(filter(None, [current.get("source", ""), record.get("source", "")])))
        if sources:
            current["source"] = ";".join(sources)
    return list(unique.values()), duplicate_count


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
) -> str:
    if output_dir:
        set_output_dir_fn(output_dir)
    ensure_directories()
    runtime.reset()
    discovery_coverage.reset()
    replay_snapshot.reset()
    if config.REPLAY_SNAPSHOT_INPUT:
        replay_snapshot.load(
            Path(config.REPLAY_SNAPSHOT_INPUT),
            max_uncompressed_bytes=config.REPLAY_SNAPSHOT_MAX_UNCOMPRESSED_BYTES,
        )
    search.reset_source_health()
    search.reset_candidate_host_observations()
    linkedin_company.reset()
    logger = setup_logging()
    start_time = time.monotonic()
    company_records = excel.read_company_records(input_file)
    company_records, duplicate_count = deduplicate_company_records(company_records)
    if duplicate_count:
        runtime.record("input.duplicates_removed", duplicate_count)
        logger.info("Removed %s duplicate company rows before processing", duplicate_count)
    if companies:
        wanted = {value.casefold() for value in companies}
        company_records = [record for record in company_records if record["company"].casefold() in wanted]
    if only_statuses:
        previous_statuses = excel.read_result_statuses(config.CONTACTS_FILE)
        allowed = {value.casefold() for value in only_statuses}
        company_records = [
            record for record in company_records
            if previous_statuses.get(record["company"].casefold(), "").casefold() in allowed
        ]
    if not company_records:
        raise RuntimeError(f"No companies matched the requested selection in {input_file}")
    scorer.configure_company_token_frequencies([
        record["company"] for record in company_records
    ])
    paid_query_limit = search.configure_run_budget(len(company_records))
    paid_settings = {
        "search_provider": config.SEARCH_PROVIDER,
        "google_places": config.ENABLE_GOOGLE_PLACES,
        "brandfetch": config.ENABLE_BRANDFETCH_DOMAIN_SEARCH,
        "hunter_domain": config.ENABLE_HUNTER_DOMAIN_FINDER,
    }
    paid_escalation_enabled = bool(
        config.SEARCH_CACHE_MODE != "replay"
        and (
            paid_settings["search_provider"] == "brightdata"
            or (paid_settings["google_places"] and config.GOOGLE_PLACES_API_KEY)
            or paid_settings["brandfetch"]
            or paid_settings["hunter_domain"]
        )
    )

    source_preflight = search.preflight_source_profiles(company_records)
    for health in source_preflight:
        logger.info(
            "Source profile preflight: host=%s status=%s server_errors=%s circuit_open=%s",
            health.get("host", ""), health.get("status", "unknown"),
            health.get("server_errors", 0), health.get("circuit_open", False),
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
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    progress = checkpoint.load_progress(input_file, run_signature)
    results_by_index: dict[int, dict] = {}
    start_index = 0
    if progress:
        # Keep interrupted runs portable as well: the snapshot is checkpointed
        # beside the output and merged only after the matching progress
        # signature has been accepted.
        if not config.REPLAY_SNAPSHOT_INPUT and config.REPLAY_SNAPSHOT_FILE.exists():
            replay_snapshot.load(
                config.REPLAY_SNAPSHOT_FILE,
                max_uncompressed_bytes=config.REPLAY_SNAPSHOT_MAX_UNCOMPRESSED_BYTES,
            )
        results_so_far = progress.get("results_so_far", [])
        for offset, row in enumerate(results_so_far):
            idx = int(row.get("__index", offset))
            results_by_index[idx] = row
        start_index = int(progress.get("last_completed_index", -1)) + 1
        logger.info("Resuming from index %s", start_index)

    pending = [
        (idx, record)
        for idx, record in enumerate(company_records)
        if idx not in results_by_index and idx >= start_index
    ]
    def execute_phase(items: list[tuple[int, dict]], *, paid_phase: bool) -> None:
        if not items:
            return
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_company_fn, idx, record["company"], logger, record.get("website", ""), record): idx
                for idx, record in items
            }
            for future in as_completed(futures):
                try:
                    idx, row = future.result()
                except Exception as exc:
                    idx = futures[future]
                    company = company_records[idx]["company"]
                    logger.exception("Unhandled processing failure for %s", company)
                    row = empty_result_fn(
                        company,
                        "PROCESSING_FAILED",
                        f"{exc.__class__.__name__}: {exc}",
                    )
                row["__index"] = idx
                row["__paid_escalation_complete"] = bool(
                    paid_phase or not paid_escalation_enabled
                    or row.get("status") in report.OK_STATUSES
                )
                if paid_phase and idx in results_by_index:
                    previous = results_by_index[idx]
                    if result_quality_key(previous) > result_quality_key(row):
                        previous["__paid_escalation_complete"] = True
                        row = previous
                results_by_index[idx] = row
                checkpoint.save_result(input_file, idx, row, run_signature)
                if (
                    len(results_by_index)
                    % config.REPLAY_SNAPSHOT_CHECKPOINT_INTERVAL
                    == 0
                ):
                    replay_snapshot.write(config.REPLAY_SNAPSHOT_FILE)
                logger.info("Completed %s/%s: %s", len(results_by_index), len(company_records), row["company"])

    try:
        if paid_escalation_enabled:
            config.SEARCH_PROVIDER = "ddgs"
            config.ENABLE_GOOGLE_PLACES = False
            config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = False
            config.ENABLE_HUNTER_DOMAIN_FINDER = False
            runtime.record("pipeline.free_pass_companies", len(pending))
        execute_phase(pending, paid_phase=False)

        config.SEARCH_PROVIDER = paid_settings["search_provider"]
        config.ENABLE_GOOGLE_PLACES = paid_settings["google_places"]
        config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = paid_settings["brandfetch"]
        config.ENABLE_HUNTER_DOMAIN_FINDER = paid_settings["hunter_domain"]
        escalation = [
            (idx, company_records[idx])
            for idx, row in sorted(results_by_index.items())
            if needs_paid_escalation(row)
        ]
        if paid_escalation_enabled and escalation:
            runtime.record("pipeline.paid_escalation_companies", len(escalation))
            search.scale_paid_api_budgets(len(escalation))
            search.configure_run_budget(len(escalation))
            execute_phase(escalation, paid_phase=True)
    except KeyboardInterrupt:
        replay_snapshot.write(config.REPLAY_SNAPSHOT_FILE)
        logger.warning("Interrupted. Progress checkpoint was saved.")
        raise
    finally:
        config.SEARCH_PROVIDER = paid_settings["search_provider"]
        config.ENABLE_GOOGLE_PLACES = paid_settings["google_places"]
        config.ENABLE_BRANDFETCH_DOMAIN_SEARCH = paid_settings["brandfetch"]
        config.ENABLE_HUNTER_DOMAIN_FINDER = paid_settings["hunter_domain"]

    rows = [results_by_index[i] for i in range(len(company_records))]
    report_text = write_outputs_fn(rows, time.monotonic() - start_time)
    checkpoint.clear_run_progress(input_file, run_signature)
    return report_text
