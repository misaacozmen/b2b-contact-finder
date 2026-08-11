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

    def test_home_and_kitchen_sector_maps_to_ev_mutfak(self) -> None:
        metadata = {"sector": "ZUCHEX - Ev ve Mutfak Eşyaları", "description": ""}
        self.assertEqual(scorer.metadata_contexts(metadata), ["ev_mutfak"])
        self.assertEqual(search._metadata_query_terms(metadata), ["ev mutfak esyalari"])
        self.assertTrue(scorer.page_matches_metadata_context("Porcelain tableware and cookware", "ev_mutfak"))

    def test_explicit_other_sector_is_a_hard_metadata_conflict(self) -> None:
        score, reason = main._page_context_score(
            "ALKAR",
            [{"html": "Balık ürünleri ve seafood fish processing"}],
            {"sector": "Ev ve Mutfak Eşyaları"},
        )
        self.assertEqual(score, -20)
        self.assertEqual(reason, "metadata_context_conflict:ev_mutfak/gida")
        self.assertTrue(main._is_hard_context_failure({
            "context_failed": True,
            "reasons": [reason],
            "candidate": {"_identity_company": "ALKAR"},
            "structured_identity": {},
            "identity_assessment": {"strong_first_party_bundle": True},
        }))

    def test_unobserved_sector_remains_nonblocking(self) -> None:
        score, reason = main._page_context_score(
            "ABDIK", [{"html": "ABDIK kurumsal iletişim"}],
            {"sector": "Ev ve Mutfak Eşyaları"},
        )
        self.assertEqual(score, 0)
        self.assertEqual(reason, "metadata_context_not_observed:0/1")

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

    def test_context_conflict_does_not_override_exact_compound_legal_identity(self) -> None:
        evaluation = {
            "context_failed": True,
            "reasons": [
                "metadata_context_conflict:ev_mutfak/ambalaj",
                "page_identity_strong:2/2",
                "legal_name_phrase_match:2",
            ],
            "candidate": {
                "domain": "adfankastre.com.tr",
                "url": "https://adfankastre.com.tr",
                "_identity_company": "ADF ANKASTRE",
            },
        }
        self.assertFalse(main._is_hard_context_failure(evaluation))

    def test_exact_compound_legal_identity_neutralizes_context_conflict_before_assessment(self) -> None:
        score, reasons = main._score_candidate_with_site(
            "ADF ANKASTRE",
            {"url": "https://adfankastre.com.tr", "score": 82, "reason": "test"},
            {
                "url": "https://adfankastre.com.tr",
                "pages": [{
                    "url": "https://adfankastre.com.tr",
                    "html": "ADF Ankastre ambalaj ambalaj ambalaj",
                }],
            },
            "",
            [],
            {"sector": "Ev ve Mutfak EÅŸyalarÄ±"},
        )
        self.assertGreaterEqual(score, 82)
        self.assertIn("metadata_context_conflict_overridden_by_exact_compound_identity", reasons)
        self.assertNotIn("context_gate_failed", reasons)


if __name__ == "__main__":
    unittest.main()
