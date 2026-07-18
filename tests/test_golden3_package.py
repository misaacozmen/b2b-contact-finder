import unittest
from pathlib import Path

from modules import scorer
from validate_golden_xlsx import _sheet_rows, readiness_issues


ROOT = Path(__file__).resolve().parents[1]
GOLDEN3_DIR = ROOT / "outputs" / "golden_3_20260715"


class Golden3PackageTests(unittest.TestCase):
    def test_golden3_has_fifteen_unique_companies_and_no_prior_overlap(self):
        golden3 = _sheet_rows(GOLDEN3_DIR / "golden_3_manual_validation_15.xlsx", "Manual Report")
        current = {
            scorer.normalize_text(str(row.get("Company") or "")).strip()
            for row in golden3
            if row.get("Company")
        }
        prior = set()
        for path in (
            ROOT / "outputs/golden_manual_validation_20260713/golden_manual_validation_30.xlsx",
            ROOT / "outputs/golden_2_20260714/golden_2_manual_validation_30.xlsx",
        ):
            prior.update(
                scorer.normalize_text(str(row.get("Company") or "")).strip()
                for row in _sheet_rows(path, "Manual Report")
                if row.get("Company")
            )
        self.assertEqual(len(current), 15)
        self.assertFalse(current & prior)

    def test_golden3_is_ready_after_manual_validation(self):
        issues = readiness_issues(GOLDEN3_DIR / "golden_3_manual_validation_15.xlsx")
        self.assertEqual(issues, [])

    def test_golden3_uses_three_turkish_fair_sources(self):
        rows = _sheet_rows(GOLDEN3_DIR / "golden_3_pipeline_input_15.xlsx")
        counts = {}
        for row in rows:
            counts[row["source"]] = counts.get(row["source"], 0) + 1
            self.assertEqual(row["country"], "Türkiye")
        self.assertEqual(counts, {"ifco": 5, "idos_f_istanbul": 5, "beauty_eurasia": 5})


if __name__ == "__main__":
    unittest.main()
