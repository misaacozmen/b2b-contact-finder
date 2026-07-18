import unittest
from pathlib import Path
from urllib.parse import urlparse

from modules import scorer
import run_golden_5
from validate_benchmark_suite import validate_manifest
from validate_golden_xlsx import _sheet_rows, readiness_issues


ROOT = Path(__file__).resolve().parents[1]
GOLDEN5_DIR = ROOT / "outputs" / "golden_5_20260716"


def _names(path: Path, sheet: str | None = None, column: str = "Company") -> set[str]:
    return {
        scorer.normalize_text(str(row.get(column) or "")).strip()
        for row in _sheet_rows(path, sheet)
        if str(row.get(column) or "").strip()
    }


class Golden5PackageTests(unittest.TestCase):
    def test_golden5_has_twenty_unique_companies_with_no_seen_overlap(self):
        manual = GOLDEN5_DIR / "golden_5_manual_validation_20_ready.xlsx"
        current = _names(manual, "Manual Report")
        prior = set()
        for path in (
            ROOT / "outputs/golden_manual_validation_20260713/golden_manual_validation_30.xlsx",
            ROOT / "outputs/golden_2_20260714/golden_2_manual_validation_30.xlsx",
            ROOT / "outputs/golden_3_20260715/golden_3_manual_validation_15.xlsx",
            ROOT / "outputs/golden_4_20260715/golden_4_manual_validation_15.xlsx",
        ):
            prior.update(_names(path, "Manual Report"))
        prior.update(_names(ROOT / "input/firms.xlsx", column="company"))
        self.assertEqual(len(current), 20)
        self.assertFalse(current & prior)

    def test_source_assisted_input_keeps_official_fair_profiles_for_diagnostics(self):
        rows = _sheet_rows(GOLDEN5_DIR / "golden_5_pipeline_input_20.xlsx")
        self.assertEqual(len(rows), 20)
        profiles = set()
        for row in rows:
            self.assertEqual(row["source"], "expomed_eurasia_2026")
            self.assertEqual(row["country"], "Türkiye")
            self.assertFalse(row.get("website"))
            self.assertTrue(row.get("sector"))
            self.assertIn("Expomed Eurasia 2026", str(row.get("description") or ""))
            profile = str(row.get("profile_url") or "")
            self.assertEqual(urlparse(profile).hostname, "expomedistanbul.com")
            self.assertTrue(urlparse(profile).path.startswith("/en/brand/"))
            profiles.add(profile)
        self.assertEqual(len(profiles), 20)

    def test_discovery_blind_input_removes_every_profile_shortcut(self):
        rows = _sheet_rows(GOLDEN5_DIR / "golden_5_discovery_blind_input_20.xlsx")
        self.assertEqual(len(rows), 20)
        for row in rows:
            self.assertEqual(row["source"], "expomed_eurasia_2026")
            self.assertEqual(row["country"], "Türkiye")
            self.assertFalse(row.get("website"))
            self.assertFalse(row.get("profile_url"))
            self.assertTrue(row.get("sector"))

    def test_manual_report_and_discovery_blind_input_have_identical_companies(self):
        manual = _names(GOLDEN5_DIR / "golden_5_manual_validation_20_ready.xlsx", "Manual Report")
        pipeline = _names(GOLDEN5_DIR / "golden_5_discovery_blind_input_20.xlsx", column="company")
        self.assertEqual(manual, pipeline)

    def test_golden5_runner_uses_discovery_blind_input(self):
        self.assertEqual(
            run_golden_5.GOLDEN_INPUT.name,
            "golden_5_discovery_blind_input_20.xlsx",
        )

    def test_completed_manual_workbook_is_ready_for_blind_run(self):
        issues = readiness_issues(
            GOLDEN5_DIR / "golden_5_manual_validation_20_ready.xlsx"
        )
        self.assertEqual(issues, [])

    def test_pending_blind_set_keeps_benchmark_manifest_valid(self):
        _, issues = validate_manifest(ROOT / "data/benchmark_splits.json")
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
