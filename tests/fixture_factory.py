"""Deterministic, disposable benchmark fixtures for the offline test session."""

from __future__ import annotations

import json
import os
from pathlib import Path

from openpyxl import Workbook


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
