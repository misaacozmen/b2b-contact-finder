"""Tests for per-item invariant isolation and run-report persistence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from openpyxl import Workbook

import config
import main
from modules import checkpoint, output_artifacts, pipeline_runner, runtime, search
from modules import run_report
from strict_fixtures import publishable_content_decision


PROVIDERS = {name: 0 for name in checkpoint.CANONICAL_PROVIDERS}


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    runtime.reset()
    yield
    runtime.reset()


def _input_book(path: Path, companies: list[str]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.append(["company", "source_record_id", "website"])
    for index, company in enumerate(companies):
        sheet.append([company, f"source:{index}", f"https://company{index}.example"])
    book.save(path)
    book.close()


def _configure_pipeline(tmp_path: Path, monkeypatch, companies: list[str]) -> Path:
    source = tmp_path / "input.xlsx"
    _input_book(source, companies)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "MAX_WORKERS", 1)
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "brightdata")
    monkeypatch.setattr(config, "SEARCH_CACHE_MODE", "off")
    monkeypatch.setattr(config, "CRAWL_CACHE_MODE", "off")
    monkeypatch.setattr(config, "BRIGHTDATA_API_KEY", "fake")
    monkeypatch.setattr(config, "BRIGHTDATA_REQUEST_BUDGET", 12)
    monkeypatch.setattr(config, "BRIGHTDATA_REQUEST_HARD_CAP", 12)
    monkeypatch.setattr(config, "BRIGHTDATA_REQUESTS_PER_MINUTE", 0)
    monkeypatch.setattr(config, "GLOBAL_REQUESTS_PER_SECOND", 0)
    monkeypatch.setattr(config, "MAX_RETRIES", 0)
    monkeypatch.setattr(config, "MAX_SEARCH_QUERIES_PER_COMPANY", 1)
    monkeypatch.setattr(config, "DEFAULT_PAID_SEARCH_QUERY_LIMIT", 1)
    for name in (
        "ENABLE_GOOGLE_PLACES", "ENABLE_BRANDFETCH_DOMAIN_SEARCH",
        "ENABLE_HUNTER_DOMAIN_FINDER", "ENABLE_LINKEDIN_COMPANY_LOOKUP",
        "ENABLE_LLM_ARBITER",
    ):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, "MIN_DELAY_SEC", 0)
    monkeypatch.setattr(config, "MAX_DELAY_SEC", 0)
    monkeypatch.setattr(search, "preflight_source_profiles", lambda *_args, **_kwargs: [])
    return source


def _writer(rows, elapsed, *, telemetry_snapshot=None, operational_metrics=None):
    return output_artifacts.write_outputs(
        rows, elapsed, telemetry_snapshot=telemetry_snapshot,
        operational_metrics=operational_metrics,
    )


def _free_needs_paid(index: int, company: str, record: dict) -> tuple[int, dict]:
    return index, {
        "company": company, "source_record_id": record["source_record_id"],
        "status": "REVIEW_NEEDED", "reason": "needs_paid",
        "publication_eligible": False,
    }


def _supplied_site_success(index: int, company: str, record: dict) -> tuple[int, dict]:
    website = str(record["website"])
    return index, {
        "company": company, "source_record_id": record["source_record_id"],
        "status": "OK_HIGH_CONFIDENCE", "reason": "supplied site validated",
        "website": website, "publication_eligible": True,
        "content_decision": publishable_content_decision(
            source_record_id=record["source_record_id"], website=website,
            email="", phone="",
        ),
        "known_website_evaluation": {
            "status": "OK_HIGH_CONFIDENCE", "website": website,
        },
        "paid_attempt_result": "NO_CALL_NEEDED",
        "paid_attempt_reason": "supplied_website_publishable_at_paid_entry",
        "paid_evidence_ref": "test:supplied-site",
    }


def test_invariant_in_paid_phase_still_writes_report(tmp_path, monkeypatch):
    source = _configure_pipeline(tmp_path, monkeypatch, ["Resume Company"])

    def worker(index, company, _logger, _website, record, *, execution_phase="FREE"):
        if execution_phase == "FREE":
            return _free_needs_paid(index, company, record)
        raise checkpoint.ResumeInvariant("paid resume evidence conflict")

    with pytest.raises(checkpoint.ResumeInvariant, match="paid resume evidence conflict"):
        pipeline_runner.run_pipeline(
            source, allow_paid=True, process_company_fn=worker,
            write_outputs_fn=_writer,
            set_output_dir_fn=lambda path: setattr(config, "OUTPUT_DIR", Path(path)),
            empty_result_fn=lambda company, status, reason: {
                "company": company, "status": status, "reason": reason,
                "publication_eligible": False,
            },
        )

    run_root = next((tmp_path / "runs").iterdir())
    output = run_root / "output"
    assert (output / "rapor.md").is_file()
    assert (output / "sonuclar.xlsx").is_file() or (output / "sonuclar.csv").is_file()
    status = json.loads((output / "run_status.json").read_text(encoding="utf-8"))
    assert status["run_status"] == "KISMI_KOSU_HATASI"


def test_item_invariant_is_isolated(tmp_path, monkeypatch):
    source = _configure_pipeline(tmp_path, monkeypatch, ["First", "Broken", "Third"])

    def worker(index, company, _logger, _website, record, *, execution_phase="FREE"):
        if execution_phase == "FREE":
            return _free_needs_paid(index, company, record)
        if index == 1:
            raise checkpoint.EvidenceInvariant("item evidence contradiction")
        return _supplied_site_success(index, company, record)

    outcome = pipeline_runner.run_pipeline(
        source, allow_paid=True, process_company_fn=worker,
        write_outputs_fn=_writer,
        set_output_dir_fn=lambda path: setattr(config, "OUTPUT_DIR", Path(path)),
        empty_result_fn=lambda company, status, reason: {
            "company": company, "status": status, "reason": reason,
            "publication_eligible": False,
        },
    )
    assert outcome.status is pipeline_runner.PipelineOutcomeStatus.COMPLETE
    run_root = next((tmp_path / "runs").iterdir())
    output = run_root / "output"
    status = json.loads((output / "run_status.json").read_text(encoding="utf-8"))
    assert status["run_status"] == "TAMAMLANDI"
    with sqlite3.connect(run_root / "state" / "progress.sqlite3") as connection:
        states = connection.execute(
            "SELECT item_index,paid_state,last_error FROM run_items ORDER BY item_index",
        ).fetchall()
    assert states[1][1] == "FAILED"
    assert states[1][2].startswith("invariant:EvidenceInvariant:")
    assert states[0][1] == states[2][1] == "DONE"


def test_unknown_paid_state_no_longer_blocks_finalization(tmp_path, monkeypatch):
    source = _configure_pipeline(tmp_path, monkeypatch, ["Uncertain Company"])

    def worker(index, company, _logger, _website, record, *, execution_phase="FREE"):
        if execution_phase == "FREE":
            return _free_needs_paid(index, company, record)
        query = checkpoint.load_paid_query_plan(runtime.durable_run_id(), index)[0]
        reservation = runtime.reserve_api(
            "brightdata", operation="search",
            request_fingerprint=search.brightdata_request_fingerprint(query),
        )
        assert reservation.accepted, reservation
        runtime.start_api(reservation)
        runtime.complete_api(reservation, "UNKNOWN", "simulated transport uncertainty")
        return index, {
            "company": company, "source_record_id": record["source_record_id"],
            "status": "REVIEW_NEEDED", "reason": "simulated uncertainty",
            "publication_eligible": False,
        }

    outcome = pipeline_runner.run_pipeline(
        source, allow_paid=True, process_company_fn=worker,
        write_outputs_fn=_writer,
        set_output_dir_fn=lambda path: setattr(config, "OUTPUT_DIR", Path(path)),
        empty_result_fn=lambda company, status, reason: {
            "company": company, "status": status, "reason": reason,
            "publication_eligible": False,
        },
    )
    assert outcome.status is pipeline_runner.PipelineOutcomeStatus.COMPLETE
    run_root = next((tmp_path / "runs").iterdir())
    output = run_root / "output"
    status = json.loads((output / "run_status.json").read_text(encoding="utf-8"))
    assert status["run_status"] == "TAMAMLANDI"
    assert "provider_call_unknown_outcome" in (output / "rapor.md").read_text(encoding="utf-8")
    with sqlite3.connect(run_root / "state" / "progress.sqlite3") as connection:
        assert connection.execute(
            "SELECT paid_state,last_error FROM run_items",
        ).fetchone() == ("FAILED", "provider_call_unknown_outcome")


def test_cli_prints_exception_class_and_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "_ensure_safe_project_runtime", lambda _argv: None)
    monkeypatch.setattr(main, "_apply_cli_options", lambda _args: None)
    monkeypatch.setattr(main, "resolve_cli_run_config", lambda _args: None)
    monkeypatch.setattr(main, "_cli_selected_values", lambda _args: (set(), set()))
    monkeypatch.setattr(main, "_apply_saved_resolver_configuration", lambda: {})

    def fail(*_args, **_kwargs):
        raise checkpoint.EvidenceInvariant("x-detail")

    monkeypatch.setattr(main, "run", fail)
    result = main.cli(["--input", str(tmp_path / "input.xlsx"), "--non-interactive"])
    assert result == 22
    assert "SCHEDULER_INVARIANT_VIOLATION:EvidenceInvariant:x-detail" in capsys.readouterr().out


def test_validate_paid_evidence_collect_mode(tmp_path, monkeypatch):
    db_path = tmp_path / "progress.sqlite3"
    monkeypatch.setattr(config, "PROGRESS_DB_FILE", db_path)
    checkpoint._SCHEMA_READY.clear()
    checkpoint.initialize_schema(db_path)
    checkpoint.initialize_run(
        run_id="validation-run", input_hash="input", run_signature="signature",
        context={"phase": "FREE"}, budgets=PROVIDERS,
        items=[{
            "item_index": 0, "source_record_id": "source:0",
            "free_state": "DONE", "paid_required": True, "paid_state": "PENDING",
        }],
    )
    checkpoint.transition_phase("validation-run", "PAID", expected_count=1)
    assert checkpoint.claim_item(run_id="validation-run", item_index=0, phase="PAID")
    checkpoint.begin_paid_attempt(
        run_id="validation-run", item_index=0, attempt_number=1,
        provider_plan=(),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE run_items SET paid_state='DONE' WHERE run_id='validation-run' AND item_index=0",
        )
        connection.commit()

    result = checkpoint.validate_paid_evidence("validation-run", collect=True)
    assert len(result["violations"]) == 1
    assert result["violations"][0]["item_index"] == 0
    with pytest.raises(checkpoint.EvidenceInvariant):
        checkpoint.validate_paid_evidence("validation-run", collect=False)


def test_report_writer_never_raises(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    errors = run_report.write_run_report(
        [{"company": "Example"}], output_root=blocker,
        run_status="TAMAMLANDI", status_detail="test",
        elapsed_seconds=0.1, telemetry={},
    )
    assert errors
    assert "output_root" in errors
