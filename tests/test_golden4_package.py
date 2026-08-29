import json
import unittest
from pathlib import Path
from urllib.parse import urlparse

from modules import scorer
from validate_benchmark_suite import validate_manifest
from validate_golden_xlsx import _sheet_rows
from fixture_factory import benchmark_manifest, golden, path as fixture_path


ROOT = fixture_path()
GOLDEN4_DIR = golden(4).parent


def _names(path: Path, sheet: str | None = None, column: str = "Company") -> set[str]:
    return {
        scorer.normalize_text(str(row.get(column) or "")).strip()
        for row in _sheet_rows(path, sheet)
        if str(row.get(column) or "").strip()
    }


class Golden4PackageTests(unittest.TestCase):
    def test_golden4_has_fifteen_unique_companies_with_no_prior_overlap_and_private_gate(self):
        manual = GOLDEN4_DIR / "golden_4_manual_validation_15.xlsx"
        current = _names(manual, "Manual Report")
        prior = set()
        for path in (
            fixture_path("outputs/golden_manual_validation_20260713/golden_manual_validation_30.xlsx"),
            fixture_path("outputs/golden_2_20260714/golden_2_manual_validation_30.xlsx"),
            golden(3),
        ):
            prior.update(_names(path, "Manual Report"))
        self.assertEqual(len(current), 15)
        self.assertFalse(current & prior)

        splits = json.loads(benchmark_manifest().read_text(encoding="utf-8"))
        g4_set = next(s for s in splits["sets"] if s.get("name") == "golden_4")
        self.assertIs(g4_set.get("private_seen_check"), True)

    def test_golden4_uses_only_turkey_rows_from_the_unseen_fair(self):
        rows = _sheet_rows(GOLDEN4_DIR / "golden_4_pipeline_input_15.xlsx")
        self.assertEqual(len(rows), 15)
        profiles = set()
        for row in rows:
            self.assertEqual(row["source"], "win_eurasia")
            self.assertEqual(row["country"], "Türkiye")
            self.assertFalse(row.get("website"))
            self.assertTrue(row.get("sector"))
            profile = str(row.get("profile_url") or "")
            self.assertEqual(urlparse(profile).hostname, "platform.win-eurasia.com")
            profiles.add(profile)
        self.assertEqual(len(profiles), 15)

    def test_manual_report_and_pipeline_input_have_identical_companies(self):
        manual = _names(GOLDEN4_DIR / "golden_4_manual_validation_15.xlsx", "Manual Report")
        pipeline = _names(GOLDEN4_DIR / "golden_4_pipeline_input_15.xlsx", column="company")
        self.assertEqual(manual, pipeline)

    def test_pending_blind_set_keeps_benchmark_manifest_valid(self):
        _, issues = validate_manifest(benchmark_manifest())
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
