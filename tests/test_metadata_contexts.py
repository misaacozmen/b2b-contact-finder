import unittest
from pathlib import Path

import main
from modules import entity_resolution, exhibitor_scraper, scorer, search


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

    def test_texhibition_source_sector_maps_accessory_and_digital_contexts(self) -> None:
        self.assertEqual(
            scorer.metadata_contexts({"source": "texhibition_2026", "sector": "Accessory"}),
            ["textile_accessories"],
        )
        self.assertEqual(
            scorer.metadata_contexts({"source": "texhibition_2026", "sector": "Digital"}),
            ["textile_digital"],
        )

    def test_non_texhibition_generic_terms_do_not_create_textile_context(self) -> None:
        self.assertEqual(
            scorer.metadata_contexts({"source": "other_source", "sector": "Accessory", "description": "Digital Agency"}),
            [],
        )

    def test_texhibition_non_textile_labels_keep_required_contexts(self) -> None:
        self.assertEqual(scorer.metadata_contexts({"source": "other_source", "sector": "Digital Agency"}), [])
        self.assertEqual(scorer.metadata_contexts({"source": "other_source", "sector": "Accessory Store"}), [])
        self.assertEqual(scorer.metadata_contexts({"source": "other_source", "sector": "Label Manufacturer"}), ["ambalaj"])

    def test_combined_source_context_evidence_keeps_provenance_separate(self) -> None:
        evidence = scorer.metadata_context_evidence({
            "source": "texhibition_2026;zuchex_2026",
            "sector": "Label Manufacturer",
            "listed_legal_name": "AKEL",
        })
        assert [item["source"] for item in evidence] == ["texhibition_2026", "zuchex_2026"]
        assert evidence[0]["contexts"] == ["ambalaj"]
        assert evidence[1]["status"] == "unknown"
        assert evidence[1]["reason"] == "source_specific_metadata_unavailable_after_legacy_merge"

    def test_zuchex_graphql_fixture_preserves_display_legal_and_profile_fields(self) -> None:
        import json
        fixture = json.loads((Path(__file__).parent / "fixtures" / "zuchex_public_detail.json").read_text(encoding="utf-8"))
        node = fixture["data"]["view"]["exhibitors"]["nodes"][0]
        details = exhibitor_scraper._zuchex_node_details(node, listing_url="https://www.zuchex.com/list")
        assert details["source_id"] == "zuchex-public-001"
        assert details["display_name"] == "AKEL"
        assert details["legal_name"] == ""
        assert details["profile_url"].endswith("zuchex-public-001")
        assert details["first_party_fields"]["website"] == "https://akel.com.tr"

    def test_blank_and_unmapped_source_sectors_are_explicit(self) -> None:
        assert scorer.metadata_context_status({"source": "texhibition_2026", "sector": ""}) == ("unknown", "source_sector_blank")
        assert scorer.metadata_context_status({"source": "texhibition_2026", "sector": "Unmapped sector"}) == ("unknown", "unknown_unmapped_label:unmapped sector")
        profile = entity_resolution.build_target_profile("Example", {"source": "texhibition_2026", "sector": ""})
        assert profile.metadata_context_status == "unknown"
        assert profile.metadata_context_reason == "source_sector_blank"
        assert profile.metadata_source_fields_sha256

    def test_clean_texhibition_detail_fixture_excludes_footer_and_keeps_fields(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures"
        adenza = exhibitor_scraper._texhibition_profile_details(
            (fixture_root / "texhibitionist_adenza_profile.html").read_text(encoding="utf-8"),
            "https://www.texhibitionist.com/en/exhibitors/adenza",
        )
        assert adenza["listed_legal_name"].startswith("ADENZA")
        assert "Merter" in adenza["listed_address"]
        assert adenza["listed_website"] == "https://adenza.com.tr"
        assert "organizer" not in adenza["description"].casefold()

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
