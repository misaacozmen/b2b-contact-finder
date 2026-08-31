"""Deterministic, disposable benchmark fixtures for the offline test session."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import config
from modules import checkpoint, run_context


def root() -> Path:
    value = os.environ.get("B2B_TEST_FIXTURE_ROOT")
    if not value:
        raise RuntimeError("B2B_TEST_FIXTURE_ROOT is not configured")
    return Path(value)


def _write(path: Path, headers: list[str], rows: list[dict], *, sheet: str = "Sheet") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(headers)
    for row in rows:
        worksheet.append([row.get(header, "") for header in headers])
    workbook.save(path)
    workbook.close()
    return path


def _manual(path: Path, names: list[str]) -> Path:
    headers = ["Company", "Expected Website", "Website Verified", "Expected Email", "Email Verified", "Expected Phone", "Phone Verified"]
    rows = [{"Company": name, "Expected Website": f"https://{name.casefold().replace(' ', '-')}.example", "Website Verified": "present", "Expected Email": f"info@{name.casefold().replace(' ', '-')}.example", "Email Verified": "present", "Expected Phone": "02125550000", "Phone Verified": "present"} for name in names]
    return _write(path, headers, rows, sheet="Manual Report")


def _pipeline(path: Path, names: list[str], source: str, profile_host: str, profile_prefix: str = "", description: str = "") -> Path:
    rows = [{"company": name, "source": source, "country": "Türkiye", "website": "", "sector": "synthetic sector", "profile_url": f"https://{profile_host}/{profile_prefix}{index}", "description": description} for index, name in enumerate(names)]
    return _write(path, ["company", "source", "country", "website", "sector", "profile_url", "description"], rows)


def ensure() -> Path:
    base = root()
    marker = base / ".ready"
    if marker.exists():
        return base
    fair_rows = [{"company": f"Synthetic Company {index}", "listed_website": f"https://synthetic{index}.example", "brands": f"Synthetic{index}", "profile_url": f"https://foodist.example/profile/{index}"} for index in range(44)]
    _write(base / "outputs/task2_validation_20260808/validation_subset_seed_314365_60.xlsx", ["company", "listed_website", "brands", "profile_url"], fair_rows)
    (base / "tests/fixtures").mkdir(parents=True, exist_ok=True)
    (base / "tests/fixtures/task2_foodist_listing_20260808.json").write_text(json.dumps(fair_rows, ensure_ascii=False), encoding="utf-8")

    prior1 = [f"Prior One {index}" for index in range(30)]
    prior2 = [f"Prior Two {index}" for index in range(30)]
    _manual(base / "outputs/golden_manual_validation_20260713/golden_manual_validation_30.xlsx", prior1)
    _manual(base / "outputs/golden_2_20260714/golden_2_manual_validation_30.xlsx", prior2)
    g3 = [f"Golden Three {index}" for index in range(15)]
    g4 = ["cormind"] + [f"Golden Four {index}" for index in range(14)]
    g5 = ["bioanalytic diagnostik kimya sanayi ve ticaret limited sirketi"] + [f"Golden Five {index}" for index in range(19)]
    g6 = [f"Golden Six {index}" for index in range(20)]
    _manual(base / "outputs/golden_3_20260715/golden_3_manual_validation_15.xlsx", g3)
    g3_rows = []
    for index, name in enumerate(g3):
        source = ("ifco", "idos_f_istanbul", "beauty_eurasia")[index // 5]
        g3_rows.append({"company": name, "source": source, "country": "Türkiye", "website": "", "sector": "synthetic sector", "profile_url": f"https://{source}.example/{index}", "description": ""})
    _write(base / "outputs/golden_3_20260715/golden_3_pipeline_input_15.xlsx", ["company", "source", "country", "website", "sector", "profile_url", "description"], g3_rows)
    _manual(base / "outputs/golden_4_20260715/golden_4_manual_validation_15.xlsx", g4)
    _pipeline(base / "outputs/golden_4_20260715/golden_4_pipeline_input_15.xlsx", g4, "win_eurasia", "platform.win-eurasia.com")
    _manual(base / "outputs/golden_5_20260716/golden_5_manual_validation_20_ready.xlsx", g5)
    _pipeline(base / "outputs/golden_5_20260716/golden_5_pipeline_input_20.xlsx", g5, "expomed_eurasia_2026", "expomedistanbul.com", "en/brand/", "Expomed Eurasia 2026")
    _write(base / "outputs/golden_5_20260716/golden_5_discovery_blind_input_20.xlsx", ["company", "source", "country", "website", "sector", "profile_url", "description"], [{"company": name, "source": "expomed_eurasia_2026", "country": "Türkiye", "website": "", "sector": "synthetic sector", "profile_url": "", "description": ""} for name in g5])
    _manual(base / "outputs/golden_6_20260718/golden_6_manual_validation_20_ready.xlsx", g6)
    _pipeline(base / "outputs/golden_6_20260718/golden_6_pipeline_input_20.xlsx", g6, "automechanika_istanbul_2026", "www.automechanikaistanbulplus.com", "company/", "Automechanika Istanbul 2026")
    _write(base / "outputs/golden_6_20260718/golden_6_discovery_blind_input_20.xlsx", ["company", "source", "country", "website", "sector", "profile_url", "description"], [{"company": name, "source": "automechanika_istanbul_2026", "country": "Türkiye", "website": "", "sector": "synthetic sector", "profile_url": "", "description": ""} for name in g6])
    manifest = {"version": 1, "policy": {"private_seen_expected_unique_companies": 71}, "sets": [
        {"name": "golden_3", "role": "golden_3", "expected": "outputs/golden_3_20260715/golden_3_manual_validation_15.xlsx", "status": "manual_validation_pending"},
        {"name": "golden_4", "role": "development_golden4", "expected": "outputs/golden_4_20260715/golden_4_manual_validation_15.xlsx", "private_seen_check": True, "status": "manual_validation_pending"},
        {"name": "golden_5", "role": "blind", "expected": "outputs/golden_5_20260716/golden_5_manual_validation_20_ready.xlsx", "private_seen_check": True, "status": "manual_validation_pending"},
        {"name": "golden_6", "role": "blind_golden6", "expected": "outputs/golden_6_20260718/golden_6_manual_validation_20_ready.xlsx", "private_seen_check": True, "status": "manual_validation_pending"},
    ]}
    (base / "data").mkdir(parents=True, exist_ok=True)
    (base / "data/benchmark_splits.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    marker.write_text("ready", encoding="utf-8")
    return base


def path(*parts: str) -> Path:
    return ensure().joinpath(*parts)


def benchmark_manifest() -> Path:
    return path("data", "benchmark_splits.json")


def fair_subset() -> Path:
    return path("outputs", "task2_validation_20260808", "validation_subset_seed_314365_60.xlsx")


def fair_metadata() -> Path:
    return path("tests", "fixtures", "task2_foodist_listing_20260808.json")


def golden(number: int) -> Path:
    names = {3: ("golden_3_20260715", "golden_3_manual_validation_15.xlsx"), 4: ("golden_4_20260715", "golden_4_manual_validation_15.xlsx"), 5: ("golden_5_20260716", "golden_5_manual_validation_20_ready.xlsx"), 6: ("golden_6_20260718", "golden_6_manual_validation_20_ready.xlsx")}
    directory, filename = names[number]
    return path("outputs", directory, filename)


def remaining_run_sources() -> dict[str, Path]:
    """Create a disposable 893-row parent/recovery package for continuation tests."""
    base = root() / "remaining_run_sources"
    original_input = base / "input" / "firms.xlsx"
    run_id = "synthetic_remaining_run_20260831"
    run_root = base / "runs" / run_id
    recovery_db = run_root / "output" / "artifacts" / "fixture" / "recovery_state.sqlite3"
    parent_manifest = run_root / "manifest.json"
    source_mapping = recovery_db.with_name("source_id_mapping.json")
    ready = base / ".ready"
    if ready.exists():
        return {
            "original_input": original_input,
            "recovery_db": recovery_db,
            "parent_manifest": parent_manifest,
            "source_mapping": source_mapping,
        }

    headers = [
        "company", "source", "country", "website", "listed_website", "sector",
        "profile_url", "description", "listing_url", "brands", "representations",
        "listed_phone", "listed_email", "listed_address", "hall", "stand",
    ]
    rows = []
    mappings = []
    for index in range(893):
        source_id = f"synthetic_2026:{index:04d}"
        rows.append({
            "company": f"Synthetic Firm {index:04d}",
            "source": "synthetic_fixture_2026",
            "country": "Türkiye",
            "website": "",
            "listed_website": "",
            "sector": "textile",
            "profile_url": f"https://synthetic.example/profile/{index:04d}",
            "description": "Synthetic continuation fixture",
            "listing_url": f"https://synthetic.example/listing/{index:04d}",
            "brands": "",
            "representations": "",
            "listed_phone": "",
            "listed_email": "",
            "listed_address": "",
            "hall": "",
            "stand": "",
        })
    _write(original_input, headers, rows)
    input_records = __import__("modules.excel", fromlist=["read_company_records"]).read_company_records(original_input)
    for index, record in enumerate(input_records):
        mappings.append({
            "legacy_index": index,
            "canonical_source_record_id": f"synthetic_2026:{index:04d}",
            "input_row_sha256": hashlib.sha256(run_context.canonical_json(record).encode("utf-8")).hexdigest(),
        })
    mapping_payload = {"schema_version": 1, "mappings": mappings}
    mapping_payload["mapping_sha256"] = hashlib.sha256(
        run_context.canonical_json(mapping_payload).encode("utf-8")
    ).hexdigest()
    source_mapping.parent.mkdir(parents=True, exist_ok=True)
    source_mapping.write_text(json.dumps(mapping_payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    items = []
    results = []
    for index in range(893):
        selected_free = index < 2
        selected_paid = 2 <= index < 159
        source_id = f"synthetic_2026:{index:04d}"
        payload = json.dumps({
            "company": f"Synthetic Firm {index:04d}",
            "source_record_id": source_id,
            "publication_eligible": False,
            "publication_blockers": "synthetic_fixture_pending",
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        items.append({
            "item_index": index,
            "source_record_id": source_id,
            "free_state": "PENDING" if selected_free else "DONE",
            "paid_required": 1 if selected_paid else 0,
            "paid_state": "PENDING" if selected_paid else "NOT_REQUIRED",
            "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "publication_blockers": "synthetic_fixture_pending",
        })
        results.append({"item_index": index, "payload": payload})
    budgets = {provider: 0 for provider in checkpoint.CANONICAL_PROVIDERS}
    with patch.object(config, "PROGRESS_DB_FILE", recovery_db):
        checkpoint.seed_recovered_run(
            path=recovery_db,
            run_id=run_id,
            input_hash=hashlib.sha256(original_input.read_bytes()).hexdigest(),
            run_signature="synthetic_remaining_fixture",
            context={"phase": "FREE", "seed_timestamp": "2026-08-31T00:00:00+00:00"},
            budgets=budgets,
            items=items,
            results=results,
        )
    manifest = {
        "version": 1,
        "run_id": run_id,
        "input_sha256": hashlib.sha256(original_input.read_bytes()).hexdigest(),
        "checkpoint_sha256": hashlib.sha256(recovery_db.read_bytes()).hexdigest(),
        "artifact_set_sha256": "synthetic-artifact-set-sha256",
    }
    parent_manifest.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    ready.write_text("ready", encoding="utf-8")
    return {
        "original_input": original_input,
        "recovery_db": recovery_db,
        "parent_manifest": parent_manifest,
        "source_mapping": source_mapping,
    }
