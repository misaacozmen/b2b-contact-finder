"""Talimat 21: launcher logic behind the desktop run panel."""

from __future__ import annotations

from datetime import date
import json
import os
import time

from openpyxl import Workbook
import pytest

import config
from modules import run_launcher, run_report
from tools import kosu_paneli


def _workbook(path, rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


@pytest.fixture
def ratios(monkeypatch):
    monkeypatch.setattr(config, "BRIGHTDATA_REQUEST_RATIO", 3.9)
    monkeypatch.setattr(config, "GOOGLE_PLACES_REQUEST_RATIO", 0.25)
    monkeypatch.setattr(config, "HUNTER_REQUEST_RATIO", 0.10)
    monkeypatch.setattr(config, "BRANDFETCH_REQUEST_RATIO", 0.25)


def test_inspect_input_counts_firms_and_columns(tmp_path):
    path = _workbook(tmp_path / "fuar.xlsx", [
        ["company", "website", "listed_phone"],
        ["Alfa Makina", "https://alfamakina.com.tr", ""],
        ["Beta Gıda", "", "0212 555 00 00"],
    ])
    info = run_launcher.inspect_input(path)
    assert info == {"ok": True, "error": "", "company_count": 2, "has_website": True, "has_phone": True, "has_email": False}


def test_inspect_input_reports_problems(tmp_path):
    assert run_launcher.inspect_input(tmp_path / "yok.xlsx")["error"] == "Dosya bulunamadı."
    (tmp_path / "liste.csv").write_text("company\nAlfa\n", encoding="utf-8")
    assert run_launcher.inspect_input(tmp_path / "liste.csv")["error"] == "Yalnız .xlsx dosyası seçin."
    no_header = _workbook(tmp_path / "baslik.xlsx", [["firma"], ["Alfa Makina"]])
    assert run_launcher.inspect_input(no_header)["error"] == 'İlk satırda "company" başlığı yok.'
    empty = _workbook(tmp_path / "bos.xlsx", [["company"]])
    assert run_launcher.inspect_input(empty)["error"] == "Dosyada firma yok."


def test_free_command_matches_guide():
    command, env = run_launcher.build_command(run_launcher.MODE_FREE, "C:/liste/fuar.xlsx", 10)
    assert command[:2] == [str(run_launcher.RUNTIME_PYTHON), str(run_launcher.PROJECT_ROOT / "main.py")]
    assert command[2:] == ["--input", str(os.path.normpath("C:/liste/fuar.xlsx")), "--non-interactive", "--no-allow-paid", "--finalize-without-paid"]
    assert env == {}


def test_paid_commands_match_guide():
    command, env = run_launcher.build_command(run_launcher.MODE_PLACES_HUNTER, "fuar.xlsx", 10)
    assert command[3:] == ["fuar.xlsx", "--non-interactive", "--allow-paid", "--brightdata-budget", "0",
                           "--brandfetch-budget", "0", "--linkedin-company-budget", "0", "--llm-budget", "0"]
    assert env == {"ENABLE_LLM_ARBITER": "0", "ENABLE_LINKEDIN_COMPANY_LOOKUP": "0"}
    command, env = run_launcher.build_command(run_launcher.MODE_BRIGHTDATA, "fuar.xlsx", 150)
    assert command[command.index("--brightdata-budget") + 1] == "600"
    assert env["SEARCH_PROVIDER"] == "brightdata"
    with pytest.raises(ValueError):
        run_launcher.build_command("bilinmeyen", "fuar.xlsx", 1)


def test_child_env_drops_inherited_run_switches():
    env = run_launcher.child_env({"ENABLE_LLM_ARBITER": "0"}, base={"PATH": "x", "SEARCH_PROVIDER": "brightdata", "B2B_TEST_OFFLINE": "1"})
    assert env == {"PATH": "x", "ENABLE_LLM_ARBITER": "0", "PYTHONIOENCODING": "utf-8"}


def test_paid_limits_follow_run_budget(ratios):
    assert run_launcher.paid_limits(run_launcher.MODE_FREE, 162) == {}
    assert run_launcher.paid_limits(run_launcher.MODE_PLACES_HUNTER, 162) == {"google_places": 41, "hunter": 17}
    assert run_launcher.paid_limits(run_launcher.MODE_BRIGHTDATA, 150) == {"brightdata": 585, "google_places": 38, "hunter": 15}
    assert run_launcher.brightdata_cost_ceiling(600) == 1.8
    text = run_launcher.paid_confirmation_text(run_launcher.MODE_BRIGHTDATA, 150)
    assert "Bright Data: en çok 585 çağrı" in text and text.endswith("Devam edilsin mi?")


def test_parse_progress_tracks_phase_and_counts():
    text = "\n".join([
        "2026-10-02 10:00:00,001 | INFO | === AŞAMA 1/3: ÜCRETSİZ ARAMA — 3 firma ===",
        "2026-10-02 10:00:20,001 | INFO | free_completed=1 free_total=3 attempt_number=1 input_index=0 company=ALFA MAKİNA",
        "2026-10-02 10:00:40,001 | INFO | free_completed=2 free_total=3 attempt_number=1 input_index=2 company=BETA GIDA",
    ])
    state = run_launcher.parse_progress(text)
    assert (state["stage"], state["phase"], state["done"], state["total"], state["company"]) == (1, "ÜCRETSİZ ARAMA", 2, 3, "BETA GIDA")
    assert state["phase_started"] > 0
    later = run_launcher.parse_progress(
        "2026-10-02 10:01:00,001 | INFO | === AŞAMA 2/3: ÜCRETLİ TAMAMLAMA — 1 firma (eksiksiz olduğu için atlanan: 2) ===", state,
    )
    assert (later["stage"], later["phase"], later["done"], later["total"]) == (2, "ÜCRETLİ TAMAMLAMA", 0, 0)
    assert run_launcher.phase_label(later["phase"]) == "Ücretli tamamlama"


def test_estimate_remaining():
    assert run_launcher.estimate_remaining(100.0, 2, 10) == 400.0
    assert run_launcher.estimate_remaining(100.0, 0, 10) is None
    assert run_launcher.estimate_remaining(100.0, 10, 10) is None


def test_find_new_run_dir_prefers_created_then_touched(tmp_path):
    (tmp_path / "eski").mkdir()
    started = time.time() - 5
    assert run_launcher.find_new_run_dir(tmp_path, {"eski"}, started) is None
    (tmp_path / "eski" / "output").mkdir()
    (tmp_path / "eski" / "output" / "logs.txt").write_text("x", encoding="utf-8")
    assert run_launcher.find_new_run_dir(tmp_path, {"eski"}, started) == tmp_path / "eski"
    (tmp_path / "yeni").mkdir()
    assert run_launcher.find_new_run_dir(tmp_path, {"eski"}, started) == tmp_path / "yeni"


def _run_dir(tmp_path):
    output = tmp_path / "run" / "output"
    output.mkdir(parents=True)
    (output / "run_status.json").write_text(json.dumps({"run_status": "TAMAMLANDI", "row_count": 3}), encoding="utf-8")
    (output / "rapor.md").write_text("# Rapor\n", encoding="utf-8")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = run_report.CONTACT_SHEET
    sheet.append(run_report.CONTACT_HEADERS)
    sheet.append(["Alfa Makina", "https://alfamakina.com.tr", "info@alfamakina.com.tr", "0212 555 00 00"])
    sheet.append(["Beta Gıda", "https://betagida.com.tr", "", ""])
    sheet.append(["Gama Ltd", "", "", "0216 555 00 00"])
    workbook.save(output / "sonuclar.xlsx")
    return tmp_path / "run"


def test_status_and_results_summary(tmp_path):
    run_dir = _run_dir(tmp_path)
    assert run_launcher.read_run_status(run_dir)["run_status"] == "TAMAMLANDI"
    assert run_launcher.read_run_status(tmp_path / "yok") == {}
    assert run_launcher.summarize_results(run_dir) == {"firms": 3, "website": 2, "email": 1, "phone": 2}


def test_archive_copies_without_overwriting(tmp_path):
    run_dir = _run_dir(tmp_path)
    source = _workbook(tmp_path / "liste.xlsx", [["company"], ["Alfa Makina"]])
    archive = tmp_path / "arsiv"
    target = run_launcher.archive_run(run_dir, source, "Test  Fuar", archive_root=archive, today=date(2026, 10, 2))
    assert target == archive / "2026-10 Test Fuar"
    assert sorted(path.name for path in target.iterdir()) == ["TestFuar_2026_girdi.xlsx", "TestFuar_2026_rapor.md", "TestFuar_2026_sonuclar.xlsx"]
    assert source.is_file()
    with pytest.raises(FileExistsError):
        run_launcher.archive_run(run_dir, source, "Test Fuar", archive_root=archive, today=date(2026, 10, 2))
    with pytest.raises(ValueError):
        run_launcher.archive_run(run_dir, source, "a/b", archive_root=archive)
    (run_dir / "output" / "rapor.md").unlink()
    with pytest.raises(FileNotFoundError):
        run_launcher.archive_run(run_dir, source, "Diger", archive_root=archive)


class _FakeProcess:
    pid = 4321

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


def test_run_session_starts_guide_command_and_reads_progress(tmp_path):
    calls = []
    process = _FakeProcess()

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    runs = tmp_path / "runs"
    runs.mkdir()
    session = run_launcher.RunSession(run_launcher.MODE_FREE, tmp_path / "fuar.xlsx", 3, runs_root=runs, log_dir=tmp_path / "panel", popen=fake_popen)
    session.start()
    command, kwargs = calls[0]
    assert "--finalize-without-paid" in command
    assert kwargs["cwd"] == str(run_launcher.PROJECT_ROOT)
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert session.console_path.parent == tmp_path / "panel"
    assert session.poll()["running"] is True
    output = runs / "abc" / "output"
    output.mkdir(parents=True)
    log = output / "logs.txt"
    log.write_bytes(
        "2026-10-02 10:00:00,001 | INFO | === AŞAMA 1/3: ÜCRETSİZ ARAMA — 3 firma ===\n"
        "2026-10-02 10:00:20,001 | INFO | free_completed=1 free_total=3 attempt_number=1 input_index=0 company=ALFA\n"
        "2026-10-02 10:00:40,001 | INFO | free_completed=2 free_t".encode("utf-8")
    )
    state = session.poll()
    assert (state["run_dir"], state["done"], state["total"]) == (str(runs / "abc"), 1, 3)
    with open(log, "ab") as stream:
        stream.write("otal=3 attempt_number=1 input_index=1 company=BETA\n".encode("utf-8"))
    process.code = 0
    state = session.poll()
    assert (state["done"], state["company"], state["running"], state["exit_code"]) == (2, "BETA", False, 0)


def test_panel_duration_text():
    assert kosu_paneli._duration(None) == "hesaplanıyor"
    assert kosu_paneli._duration(20) == "1 dk'dan az"
    assert kosu_paneli._duration(600) == "10 dk"
    assert kosu_paneli._duration(125 * 60) == "2 sa 5 dk"
