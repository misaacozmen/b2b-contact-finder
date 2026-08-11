import unittest
from unittest.mock import Mock, patch

import main
from modules import entity_resolution, publication_policy, search


def _evaluation(
    url: str,
    *,
    reasons: list[str],
    source_profile: bool = True,
    publishable: bool = True,
    conflicts: list | None = None,
    has_contact: bool = True,
) -> dict:
    return {
        "candidate": {
            "url": url,
            "query": "source_profile" if source_profile else "official website",
            "reason": "discovery_only_not_identity_authority",
            "role": "company_candidate",
            "_source_profile_evidence": int(source_profile),
        },
        "crawl_result": {
            "url": url,
            "pages": [{"url": url, "html": "first-party"}],
        },
        "reasons": reasons,
        "structured_identity": {},
        "identity_assessment": {
            "support_keys": ["first_party_identity", "domain_identity"],
            "publishable": publishable,
            "provisionally_publishable": publishable,
            "conflicts": conflicts or [],
        },
        "email_source_url": url if has_contact else "",
        "phone_source_url": "",
        "has_contact": has_contact,
    }


class EntityResolutionCoreTests(unittest.TestCase):
    def test_provisional_third_party_legal_phrase_is_not_resolved(self):
        evaluation = _evaluation(
            "https://hosted-directory.example",
            reasons=[
                "page_identity_strong:2/2",
                "legal_name_phrase_match:2",
                "country_identity_tr_phone",
            ],
        )
        evaluation["identity_assessment"].update({
            "publishable": False,
            "provisionally_publishable": True,
            "decision": "strong_first_party_needs_uniqueness",
        })
        result = entity_resolution.resolve_candidates("ORNEK GIDA", [evaluation])
        self.assertEqual(result.status, "unresolved")

    def test_generic_tld_phone_only_homonym_is_not_resolved(self):
        evaluation = _evaluation(
            "https://empiregroupusa.com",
            reasons=["page_identity_strong:3/3", "country_identity_tr_phone"],
        )
        evaluation["candidate"]["_official_query_evidence"] = 0
        result = entity_resolution.resolve_candidates("EMPIRE GIDA", [evaluation])
        self.assertEqual(result.status, "unresolved")

    def test_broadly_corroborated_legal_brand_route_can_resolve(self):
        evaluation = _evaluation(
            "https://brand.com.tr",
            reasons=[
                "page_identity_strong:3/3",
                "legal_name_full_match:3",
                "country_identity_tr_tld",
            ],
        )
        evaluation["candidate"]["_official_query_evidence"] = 4
        evaluation["identity_assessment"].update({
            "publishable": False,
            "provisionally_publishable": True,
            "decision": "strong_first_party_needs_uniqueness",
        })
        result = entity_resolution.resolve_candidates("ORNEK GIDA", [evaluation])
        self.assertEqual(result.status, "resolved")

    def test_unique_full_legal_title_route_does_not_need_two_sources(self):
        evaluation = _evaluation(
            "https://doganseed.com.tr",
            reasons=[
                "page_identity_strong:5/5",
                "legal_name_full_match:5",
                "country_identity_tr_tld",
            ],
        )
        evaluation["candidate"]["_official_query_evidence"] = 4
        evaluation["identity_assessment"].update({
            "publishable": False,
            "provisionally_publishable": False,
            "support_keys": ["first_party_identity"],
        })
        result = entity_resolution.resolve_candidates(
            "ELBISTAN DOGAN TOHUMCULUK TARIM URUNLERI", [evaluation],
        )
        self.assertEqual(result.status, "resolved")

    def test_full_legal_title_can_use_first_party_turkish_phone_for_country(self):
        evaluation = _evaluation(
            "https://brand.com",
            reasons=[
                "page_identity_strong:4/4",
                "legal_name_full_match:4",
                "country_identity_tr_phone",
            ],
        )
        evaluation["candidate"]["_official_query_evidence"] = 4
        evaluation["identity_assessment"].update({
            "publishable": False,
            "provisionally_publishable": False,
            "support_keys": ["first_party_identity"],
        })
        result = entity_resolution.resolve_candidates(
            "ORNEK GIDA SANAYI TICARET", [evaluation],
        )
        self.assertEqual(result.status, "resolved")

    def test_profile_provenance_never_changes_identity_fingerprint(self):
        reasons = [
            "page_identity_strong:2/2",
            "legal_name_phrase_match:2",
            "country_identity_tr_tld",
        ]
        profile = entity_resolution.build_target_profile("ORNEK METAL")
        listed = _evaluation(
            "https://ornekmetal.com.tr",
            reasons=reasons,
            source_profile=True,
        )
        search = _evaluation(
            "https://ornekmetal.com.tr",
            reasons=reasons,
            source_profile=False,
        )
        self.assertEqual(
            entity_resolution.fingerprint(profile, listed),
            entity_resolution.fingerprint(profile, search),
        )

    def test_first_party_legal_bundle_resolves_profile_route(self):
        evaluation = _evaluation(
            "https://ornekmetal.com.tr",
            reasons=[
                "page_identity_strong:2/2",
                "legal_name_phrase_match:2",
                "country_identity_tr_tld",
            ],
        )
        result = entity_resolution.resolve_profile_anchor(
            "ORNEK METAL", [evaluation],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, evaluation)

    def test_profile_link_without_first_party_bundle_is_unresolved(self):
        evaluation = _evaluation(
            "https://ornek.example",
            reasons=["country_identity_tr_tld"],
            publishable=False,
        )
        result = entity_resolution.resolve_profile_anchor(
            "ORNEK METAL", [evaluation],
        )
        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.selected)

    def test_equal_unrelated_profile_routes_remain_ambiguous(self):
        reasons = [
            "page_identity_strong:2/2",
            "legal_name_phrase_match:2",
            "country_identity_tr_tld",
        ]
        first = _evaluation("https://ornekmetal.com", reasons=reasons)
        second = _evaluation("https://ornekmetal.com.tr", reasons=reasons)
        result = entity_resolution.resolve_profile_anchor(
            "ORNEK METAL", [first, second],
        )
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.selected)

    def test_same_registrable_language_route_is_one_identity(self):
        reasons = [
            "page_identity_strong:2/2",
            "legal_name_phrase_match:2",
            "country_identity_tr_phone",
        ]
        first = _evaluation("https://example.com", reasons=reasons)
        second = _evaluation("https://en.example.com", reasons=reasons)
        result = entity_resolution.resolve_profile_anchor(
            "EXAMPLE METAL", [first, second],
        )
        self.assertEqual(result.status, "resolved")

    def test_verified_profile_anchor_stops_broad_search(self):
        candidate = {
            "url": "https://ornekmetal.com.tr",
            "domain": "ornekmetal.com.tr",
            "query": "source_profile",
            "reason": "discovery_only_not_identity_authority",
            "role": "company_candidate",
            "_source_profile_evidence": 1,
        }
        candidates = search.CandidateList([candidate])
        evaluation = _evaluation(
            candidate["url"],
            reasons=[
                "page_identity_strong:2/2",
                "legal_name_phrase_match:2",
                "country_identity_tr_tld",
            ],
        )
        evaluation["candidate"] = candidate
        with patch(
            "main.search.find_profile_candidates", return_value=candidates
        ), patch(
            "main._evaluate_candidate_with_stage", return_value=evaluation
        ), patch(
            "main._preserve_identity_phase_evidence",
            side_effect=lambda full, light: full,
        ), patch(
            "main._finalize_selected_evaluation",
            return_value={
                "company": "ORNEK METAL",
                "status": "OK_MEDIUM_CONFIDENCE",
                "reason": "verified",
            },
        ), patch(
            "main.search.find_candidate_domains",
            side_effect=AssertionError("broad search should be fallback-only"),
        ), patch("main.random_delay"):
            _, row = main.process_company(
                0, "ORNEK METAL", Mock(), metadata={}
            )
        self.assertEqual(row["status"], "OK_MEDIUM_CONFIDENCE")
        self.assertTrue(row["reason"].startswith("profile_route_resolved_by_"))

    def test_search_resolution_discards_candidate_without_target_fingerprint(self):
        official = _evaluation(
            "https://ornekmetal.com.tr",
            reasons=[
                "page_identity_strong:2/2",
                "legal_name_phrase_match:2",
                "country_identity_tr_tld",
            ],
            source_profile=False,
        )
        noise = _evaluation(
            "https://ornekotel.com.tr",
            reasons=[
                "page_identity_strong:1/1",
                "legal_name_phrase_missing:0/2",
                "country_identity_tr_tld",
            ],
            source_profile=False,
        )
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [noise, official],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, official)

    def test_two_equal_search_identities_abstain(self):
        reasons = [
            "page_identity_strong:2/2",
            "legal_name_phrase_match:2",
            "country_identity_tr_tld",
        ]
        first = _evaluation(
            "https://ornekmetal.com", reasons=reasons, source_profile=False
        )
        second = _evaluation(
            "https://ornekmetal.com.tr", reasons=reasons, source_profile=False
        )
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [first, second],
        )
        self.assertEqual(result.status, "ambiguous")

    def test_brand_substring_cannot_replace_full_target_fingerprint(self):
        noise = _evaluation(
            "https://stonehaventravelagency.com",
            reasons=[
                "page_identity_strong:1/1",
                "structured_identity_strong:1/1",
                "legal_name_phrase_missing:0/2",
                "country_identity_tr_phone",
            ],
            source_profile=False,
        )
        result = entity_resolution.resolve_candidates(
            "HAVEN CELIK", [noise],
        )
        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.selected)

    def test_exact_full_name_domain_uses_safe_fast_path(self):
        evaluation = _evaluation(
            "https://ornekmetal.com.tr",
            reasons=["country_identity_tr_tld"],
            source_profile=False,
            publishable=False,
        )
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [evaluation],
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(
            result.reason,
            "candidate_resolved_by_exact_full_name_domain",
        )

        evaluation["_identity_resolution"] = result.reason
        decision = publication_policy.evaluate(
            "ORNEK METAL",
            evaluation,
            "OK_MEDIUM_CONFIDENCE",
            minimum_safety_score=70,
        )
        self.assertNotIn("identity_not_publishable", decision["hard_blockers"])

    def test_partial_name_domain_does_not_use_fast_path(self):
        evaluation = _evaluation(
            "https://ornek.com.tr",
            reasons=["country_identity_tr_tld"],
            source_profile=False,
            publishable=False,
        )
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [evaluation],
        )
        self.assertEqual(result.status, "unresolved")

    def test_exact_full_name_website_resolves_without_contacts(self):
        evaluation = _evaluation(
            "https://ornekmetal.com.tr",
            reasons=["page_identity_weak:1/2", "country_identity_tr_tld"],
            source_profile=False,
            publishable=False,
            has_contact=False,
        )
        evaluation["candidate"]["_official_query_evidence"] = 2
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [evaluation],
        )
        self.assertEqual(result.status, "resolved")
        self.assertEqual(
            result.reason, "candidate_resolved_by_exact_full_name_domain",
        )

    def test_exact_public_brand_with_site_contact_resolves(self):
        evaluation = _evaluation(
            "https://ornekmetal.com.tr",
            reasons=["page_identity_weak:1/2", "country_identity_tr_tld"],
            source_profile=False,
            publishable=False,
        )
        evaluation["candidate"]["_official_query_evidence"] = 2
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL SANAYI TICARET", [evaluation],
        )
        self.assertEqual(result.status, "resolved")
        evaluation["_identity_resolution"] = result.reason
        decision = publication_policy.evaluate(
            "ORNEK METAL SANAYI TICARET",
            evaluation,
            "OK_MEDIUM_CONFIDENCE",
            minimum_safety_score=70,
        )
        self.assertNotIn("identity_not_publishable", decision["hard_blockers"])

    def test_long_public_brand_domain_with_first_party_contact_resolves(self):
        evaluation = _evaluation(
            "https://agronatural.tr",
            reasons=[
                "page_identity_medium:1/2@scope=public_brand,pages=3",
                "country_identity_tr_tld",
            ],
            source_profile=False,
        )
        result = entity_resolution.resolve_candidates(
            "AGRONATURAL KONSERVECILIK GIDA SANAYI", [evaluation],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, evaluation)

    def test_verified_intrinsic_domain_with_phone_only_country_resolves(self):
        evaluation = _evaluation(
            "https://longmarkproducts.com",
            reasons=[
                "page_identity_strong:2/2",
                "country_identity_tr_phone",
            ],
            source_profile=False,
        )
        result = entity_resolution.resolve_candidates(
            "LONGMARK PRODUCTS SANAYI TICARET", [evaluation],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, evaluation)

    def test_verified_first_party_route_requires_same_site_contact(self):
        evaluation = _evaluation(
            "https://longmarkproducts.com",
            reasons=[
                "page_identity_strong:2/2",
                "country_identity_tr_phone",
            ],
            source_profile=False,
        )
        evaluation["email_source_url"] = "https://directory.example/contact"
        evaluation["phone_source_url"] = "https://directory.example/contact"
        result = entity_resolution.resolve_candidates(
            "LONGMARK PRODUCTS SANAYI TICARET", [evaluation],
        )
        self.assertEqual(result.status, "unresolved")

    def test_short_brand_domain_still_requires_legal_identity(self):
        evaluation = _evaluation(
            "https://tat.com.tr",
            reasons=[
                "page_identity_strong:1/1",
                "country_identity_tr_tld",
            ],
            source_profile=False,
        )
        result = entity_resolution.resolve_candidates(
            "TAT BAKLIYAT SANAYI", [evaluation],
        )
        self.assertEqual(result.status, "unresolved")

    def test_exact_public_brand_beats_proved_descriptor_site(self):
        reasons = [
            "page_identity_strong:1/1",
            "legal_name_phrase_match:2",
            "country_identity_tr_phone",
        ]
        exact = _evaluation(
            "https://ornek.com", reasons=reasons, source_profile=False,
        )
        descriptor = _evaluation(
            "https://ornekurun.com", reasons=reasons, source_profile=False,
        )
        exact["candidate"]["_official_query_evidence"] = 3
        descriptor["candidate"]["_official_query_evidence"] = 3
        result = entity_resolution.resolve_candidates(
            "ORNEK TEKSTIL", [descriptor, exact],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, exact)

    def test_exact_requested_scope_beats_equally_proved_parent_domain(self):
        reasons = [
            "page_identity_strong:2/2",
            "legal_name_phrase_match:2",
            "country_identity_tr_phone",
        ]
        parent = _evaluation(
            "https://ornek.com", reasons=reasons, source_profile=False,
        )
        scoped = _evaluation(
            "https://ornekcelik.com.tr", reasons=reasons,
            source_profile=False,
        )
        parent["candidate"]["_official_query_evidence"] = 3
        scoped["candidate"]["_official_query_evidence"] = 3
        result = entity_resolution.resolve_candidates(
            "ORNEK CELIK", [parent, scoped],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, scoped)

    def test_specific_public_domain_does_not_require_legal_phrase(self):
        exact = _evaluation(
            "https://longmark.com",
            reasons=["page_identity_strong:1/1", "country_identity_tr_phone"],
            source_profile=False,
        )
        descriptor = _evaluation(
            "https://longmarkfabric.com",
            reasons=[
                "page_identity_strong:1/1",
                "structured_identity_strong:1/1",
                "country_identity_tr_phone",
            ],
            source_profile=False,
        )
        exact["candidate"]["_official_query_evidence"] = 3
        descriptor["candidate"]["_official_query_evidence"] = 3
        result = entity_resolution.resolve_candidates(
            "LONGMARK TEKSTIL", [descriptor, exact],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, exact)

    def test_primary_plus_later_identity_token_enters_specific_crawl_route(self):
        profile = entity_resolution.build_target_profile(
            "LONGMARK TEKSTIL TARIM SANAYI"
        )
        scoped = _evaluation(
            "https://longmarktarim.com.tr",
            reasons=["page_identity_strong:2/2", "country_identity_tr_tld"],
            source_profile=False,
            publishable=False,
            has_contact=False,
        )
        broad = _evaluation(
            "https://longmarklar.com",
            reasons=["page_identity_strong:2/2", "country_identity_tr_tld"],
            source_profile=False,
            publishable=False,
            has_contact=False,
        )
        self.assertEqual(
            entity_resolution.fingerprint(profile, scoped).domain_specificity,
            2,
        )
        self.assertEqual(
            entity_resolution.fingerprint(profile, broad).domain_specificity,
            0,
        )

    def test_mixed_alphanumeric_primary_domain_is_specific(self):
        evaluation = _evaluation(
            "https://3-s.com.tr",
            reasons=[
                "page_identity_strong:2/2",
                "legal_name_phrase_match:2",
                "country_identity_tr_tld",
            ],
            source_profile=False,
        )
        fingerprint = entity_resolution.fingerprint(
            entity_resolution.build_target_profile("3S MUHENDISLIK"),
            evaluation,
        )
        self.assertTrue(fingerprint.primary_domain_exact)

    def test_exact_translated_brand_uses_proved_search_tie_break(self):
        reasons = [
            "page_identity_strong:2/2",
            "legal_name_phrase_match:2",
            "country_identity_tr_tld",
        ]
        translated = _evaluation(
            "https://acartextile.com.tr", reasons=reasons,
            source_profile=False, has_contact=False,
        )
        descriptor = _evaluation(
            "https://acartextiledenim.com", reasons=reasons,
            source_profile=False, has_contact=False,
        )
        translated["candidate"]["_official_query_evidence"] = 3
        descriptor["candidate"]["_official_query_evidence"] = 2
        result = entity_resolution.resolve_candidates(
            "ACAR TEKSTIL", [descriptor, translated],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, translated)

    def test_public_body_brand_domain_cannot_resolve(self):
        evaluation = _evaluation(
            "https://avrasya.edu.tr",
            reasons=[
                "page_identity_strong:1/1",
                "country_identity_tr_tld",
            ],
            source_profile=False,
        )
        result = entity_resolution.resolve_candidates(
            "AVRASYA GIDA SANAYI", [evaluation],
        )
        self.assertEqual(result.status, "unresolved")

    def test_cross_domain_redirect_blocks_fast_path(self):
        evaluation = _evaluation(
            "https://ornekmetal.com.tr",
            reasons=["country_identity_tr_tld"],
            source_profile=False,
            publishable=False,
        )
        evaluation["crawl_result"]["url"] = "https://different.example"
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [evaluation],
        )
        self.assertEqual(result.status, "unresolved")

    def test_single_token_exact_domain_still_needs_page_identity(self):
        evaluation = _evaluation(
            "https://ornekmarka.com.tr",
            reasons=["country_identity_tr_tld"],
            source_profile=False,
            publishable=False,
        )
        result = entity_resolution.resolve_candidates(
            "ORNEKMARKA", [evaluation],
        )
        self.assertEqual(result.status, "unresolved")

    def test_places_phone_match_can_resolve_one_strong_first_party_site(self):
        evaluation = _evaluation(
            "https://marka.com.tr",
            reasons=[
                "page_identity_medium:2/3",
                "country_identity_tr_tld",
                "google_places_first_party_phone_match",
            ],
            source_profile=False,
        )
        evaluation["candidate"].update({
            "_google_places_evidence": [{
                "name": "ORNEK GIDA", "phone": "+902121112233",
                "website": "https://marka.com.tr",
            }],
            "_official_query_evidence": 1,
        })
        evaluation["identity_assessment"].update({
            "publishable": True,
            "support_keys": ["first_party_identity", "places_identity"],
        })
        result = entity_resolution.resolve_candidates("ORNEK GIDA", [evaluation])
        self.assertEqual(result.status, "resolved")

    def test_places_phone_match_keeps_equal_distinct_sites_ambiguous(self):
        evaluations = []
        for domain in ("marka.com.tr", "markagida.com.tr"):
            evaluation = _evaluation(
                f"https://{domain}",
                reasons=[
                    "page_identity_medium:2/3",
                    "country_identity_tr_tld",
                    "google_places_first_party_phone_match",
                ],
                source_profile=False,
            )
            evaluation["candidate"].update({
                "_google_places_evidence": [{
                    "name": "ORNEK GIDA", "phone": "+902121112233",
                }],
                "_official_query_evidence": 2,
            })
            evaluation["identity_assessment"].update({
                "publishable": True,
                "support_keys": ["first_party_identity", "places_identity"],
            })
            evaluations.append(evaluation)
        result = entity_resolution.resolve_candidates("ORNEK GIDA", evaluations)
        self.assertEqual(result.status, "ambiguous")

    def test_places_business_name_and_domain_can_corroborate_official_site(self):
        evaluation = _evaluation(
            "https://ornek.com.tr",
            reasons=["page_identity_medium:2/3", "country_identity_tr_tld"],
            source_profile=False,
        )
        evaluation["candidate"].update({
            "_google_places_evidence": [{
                "name": "ORNEK GIDA",
                "website": "https://www.ornek.com.tr/",
            }],
            "_official_query_evidence": 2,
        })
        evaluation["identity_assessment"] = entity_resolution.identity.assess(
            "ORNEK GIDA", evaluation["candidate"], evaluation["reasons"], {},
        )
        result = entity_resolution.resolve_candidates("ORNEK GIDA", [evaluation])
        self.assertEqual(result.status, "resolved")

    def test_places_name_match_on_different_domain_is_not_identity_support(self):
        evaluation = _evaluation(
            "https://marka.com.tr",
            reasons=["page_identity_medium:2/3", "country_identity_tr_tld"],
            source_profile=False,
        )
        evaluation["candidate"].update({
            "_google_places_evidence": [{
                "name": "ORNEK GIDA", "website": "https://different.com.tr",
            }],
            "_official_query_evidence": 2,
        })
        evaluation["identity_assessment"] = entity_resolution.identity.assess(
            "ORNEK GIDA", evaluation["candidate"], evaluation["reasons"], {},
        )
        result = entity_resolution.resolve_candidates("ORNEK GIDA", [evaluation])
        self.assertEqual(result.status, "unresolved")

    def test_structured_public_brand_with_safe_suffix_variant_resolves(self):
        evaluation = _evaluation(
            "https://tatmakarna.com",
            reasons=[
                "page_identity_medium:2/2",
                "structured_identity_medium:1/2",
                "country_identity_tr_phone",
            ],
            source_profile=False,
        )
        evaluation["candidate"]["_official_query_evidence"] = 2
        evaluation["structured_identity"] = {"names": ["TAT MAKARNA"]}
        evaluation["identity_assessment"].update({
            "publishable": True,
            "support_keys": ["domain_identity", "first_party_identity"],
        })
        result = entity_resolution.resolve_candidates(
            "TAT MAKARNACILIK SANAYI", [evaluation],
        )
        self.assertEqual(result.status, "resolved")

    def test_structured_brand_missing_second_distinctive_token_stays_unresolved(self):
        evaluation = _evaluation(
            "https://anka.com",
            reasons=[
                "page_identity_medium:2/2",
                "structured_identity_medium:1/2",
                "country_identity_tr_phone",
            ],
            source_profile=False,
        )
        evaluation["candidate"]["_official_query_evidence"] = 3
        evaluation["structured_identity"] = {"names": ["TOKAT ANKA GIDA"]}
        evaluation["identity_assessment"].update({
            "publishable": True,
            "support_keys": ["domain_identity", "first_party_identity"],
        })
        result = entity_resolution.resolve_candidates(
            "ANKA SEKER GIDA", [evaluation],
        )
        self.assertEqual(result.status, "unresolved")

    def test_strong_structured_split_brand_resolves_after_broad_consensus(self):
        evaluation = _evaluation(
            "https://yagizefe.com.tr",
            reasons=[
                "page_identity_strong:4/5",
                "structured_identity_medium:3/5",
                "country_identity_tr_tld",
            ],
            source_profile=False,
        )
        evaluation["candidate"]["_official_query_evidence"] = 4
        evaluation["structured_identity"] = {
            "names": ["Genç Yağız Efe Tarım Ürünleri Ltd. Şti."],
        }
        evaluation["identity_assessment"].update({
            "publishable": False,
            "provisionally_publishable": True,
            "support_keys": ["first_party_identity"],
        })
        result = entity_resolution.resolve_candidates(
            "GENÇ YAĞIZEFE TARIM ÜRÜNLERİ FERMANTASYON LTD. ŞTİ.",
            [evaluation],
        )
        self.assertEqual(result.status, "resolved")

    def test_short_exact_brand_domain_needs_context_and_broad_consensus(self):
        evaluation = _evaluation(
            "https://lark.com.tr",
            reasons=[
                "page_identity_strong:3/3",
                "context_match:1/2",
                "country_identity_tr_tld",
            ],
            source_profile=False,
        )
        evaluation["candidate"]["_official_query_evidence"] = 4
        result = entity_resolution.resolve_candidates(
            "LARK GIDA AMBALAJ", [evaluation],
        )
        self.assertEqual(result.status, "resolved")

    def test_short_exact_brand_without_context_stays_unresolved(self):
        evaluation = _evaluation(
            "https://lark.com.tr",
            reasons=["page_identity_strong:3/3", "country_identity_tr_tld"],
            source_profile=False,
        )
        evaluation["candidate"]["_official_query_evidence"] = 5
        result = entity_resolution.resolve_candidates(
            "LARK GIDA AMBALAJ", [evaluation],
        )
        self.assertEqual(result.status, "unresolved")

if __name__ == "__main__":
    unittest.main()
