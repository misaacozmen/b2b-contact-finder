from __future__ import annotations

from pathlib import Path

import config


def set_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_DIR = output_dir
    config.CONTACTS_FILE = output_dir / "contacts.xlsx"
    config.ALL_RESULTS_FILE = output_dir / "all_results.xlsx"
    config.MANIFEST_FILE = output_dir / "manifest.json"
    config.VERIFIED_CONTACTS_FILE = output_dir / "verified_contacts.xlsx"
    config.REVIEW_QUEUE_FILE = output_dir / "review_queue.xlsx"
    config.FAILED_FILE = output_dir / "failed.xlsx"
    config.CANDIDATES_FILE = output_dir / "website_candidates.xlsx"
    config.REPORT_FILE = output_dir / "report.txt"
    config.LOG_FILE = output_dir / "logs.txt"
    config.EVIDENCE_FILE = output_dir / "evidence.jsonl"
    config.ENTITY_RELATIONSHIPS_FILE = output_dir / "entity_relationships.jsonl"
    config.TELEMETRY_FILE = output_dir / "telemetry.json"
    config.DISCOVERY_COVERAGE_FILE = output_dir / "discovery_coverage.json"
    config.QUALITY_AUDIT_FILE = output_dir / "quality_audit.json"
    config.REPLAY_SNAPSHOT_FILE = output_dir / "replay_snapshot.json.gz"


def set_run_state_dir(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    config.STATE_DIR = state_dir
    config.PROGRESS_FILE = state_dir / "progress.json"
    config.PROGRESS_DB_FILE = state_dir / "progress.sqlite3"
    config.SEARCH_CACHE_DIR = state_dir / "search_cache"
    config.CRAWL_CACHE_DIR = state_dir / "crawl_cache"
    config.EMAIL_CACHE_DIR = state_dir / "email_cache"
