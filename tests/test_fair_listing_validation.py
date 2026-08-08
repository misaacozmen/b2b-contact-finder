import json
import unittest
from pathlib import Path

from modules.excel import read_company_records
from validate_fair_listed_websites import meaningful_domain_token, validate_records


ROOT = Path(__file__).parents[1]
SUBSET = ROOT / "outputs" / "task2_validation_20260808" / "validation_subset_seed_314365_60.xlsx"
LISTING_METADATA = ROOT / "tests" / "fixtures" / "task2_foodist_listing_20260808.json"


class FairListingValidationTests(unittest.TestCase):
    def test_named_findings_match_their_public_brand_tokens(self):
        self.assertEqual(
            meaningful_domain_token(
                {
                    "company": "4EL GIDA SAN. VE TİC. LTD. ŞTİ.",
                    "brands": "Torita Tortillas",
                    "listed_website": "https://www.torita.com.tr",
                }
            ),
            "torita",
        )
        self.assertEqual(
            meaningful_domain_token(
                {
                    "company": "A.AKSULAR GIDA TİC.VE SAN. A.Ş.",
                    "brands": "aly",
                    "listed_website": "https://alyfoods.com",
                }
            ),
            "aly",
        )

    def test_fixed_seed_subset_has_no_unproven_fair_website_pair(self):
        metadata = json.loads(LISTING_METADATA.read_text(encoding="utf-8"))
        report = validate_records(read_company_records(SUBSET), metadata)

        self.assertEqual(report["checked_count"], 44)
        self.assertEqual(report["passed_count"], 44)
        self.assertEqual(report["failed_count"], 0)
        self.assertGreaterEqual(report["proof_counts"]["meaningful_identity_token"], 43)
        self.assertLessEqual(report["proof_counts"].get("profile_local_website", 0), 1)


if __name__ == "__main__":
    unittest.main()
