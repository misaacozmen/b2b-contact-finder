import unittest

import main
from modules import scorer, search


class MetadataContextTests(unittest.TestCase):
    def test_food_sector_maps_to_gida(self) -> None:
        metadata = {"sector": "Beverages, Biscuits, Chocolate, Frozen Cake", "description": ""}
        self.assertEqual(scorer.metadata_contexts(metadata), ["gida"])
        self.assertEqual(search._metadata_query_terms(metadata), ["gida"])

    def test_olive_pickle_sector_maps_to_food_context(self) -> None:
        metadata = {"sector": "Olives, Pepper Paste, Pickles, Sauces, Puree", "description": ""}
        self.assertIn("gida", scorer.metadata_contexts(metadata))
        self.assertTrue(scorer.page_matches_metadata_context("Turkish pickles and olive products", "gida"))

    def test_packaging_sector_maps_to_ambalaj(self) -> None:
        metadata = {"sector": "Label, Shrink-Sleeve, Carton Box", "description": ""}
        self.assertEqual(scorer.metadata_contexts(metadata), ["ambalaj"])
        self.assertEqual(search._metadata_query_terms(metadata), ["ambalaj"])

    def test_personal_care_sector_maps_to_kozmetik(self) -> None:
        metadata = {"sector": "Kişisel Bakım Ürünleri", "description": ""}
        self.assertEqual(scorer.metadata_contexts(metadata), ["kozmetik"])
        self.assertTrue(scorer.page_matches_metadata_context("Professional cosmetics and skin care", "kozmetik"))

    def test_laboratory_sector_maps_to_laboratuvar(self) -> None:
        metadata = {"sector": "Laboratory Services", "description": ""}
        self.assertEqual(scorer.metadata_contexts(metadata), ["laboratuvar"])
        self.assertEqual(search._metadata_query_terms(metadata), ["laboratuvar"])

    def test_unknown_metadata_does_not_create_context(self) -> None:
        metadata = {"sector": "", "description": "BEAUTYEURASIA.COM"}
        self.assertEqual(scorer.metadata_contexts(metadata), [])
        self.assertEqual(search._metadata_query_terms(metadata), [])

    def test_missing_discovery_context_is_never_a_hard_failure(self) -> None:
        evaluation = {
            "context_failed": True,
            "reasons": ["metadata_context_missing:0/1", "page_identity_strong:1/1", "email_domain_match"],
            "has_contact": True,
            "candidate": {"query": "input_website"},
        }
        self.assertFalse(main._is_hard_context_failure(evaluation))
        evaluation["candidate"] = {"query": "search"}
        self.assertFalse(main._is_hard_context_failure(evaluation))

    def test_metadata_context_absence_has_no_score_penalty(self) -> None:
        score, reason = main._page_context_score(
            "Example", [{"html": "Example endustriyel urunler"}],
            {"sector": "Laboratory Services"},
        )
        self.assertEqual(score, 0)
        self.assertEqual(reason, "metadata_context_not_observed:0/1")

    def test_explicit_first_party_context_conflict_remains_hard(self) -> None:
        evaluation = {
            "context_failed": True,
            "reasons": ["metadata_context_conflict:laboratuvar/tekstil"],
            "candidate": {"query": "search"},
        }
        self.assertTrue(main._is_hard_context_failure(evaluation))


if __name__ == "__main__":
    unittest.main()
