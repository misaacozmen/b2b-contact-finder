import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
import main
from modules import (
    crawler,
    entity_memory,
    entity_resolution,
    entity_semantics,
    evidence_acquisition,
    identity,
    official_registry,
    quality_audit,
    replay_snapshot,
    relationship_graph,
    search,
    site_mapper,
    site_recovery,
)


def _evaluation(
    url: str,
    reasons: list[str],
    *,
    semantic: str = "unknown",
    relationships: list[dict] | None = None,
    has_contact: bool = True,
) -> dict:
    return {
        "candidate": {"url": url, "role": "company_candidate"},
        "crawl_result": {
            "url": url,
            "pages": [{"url": url, "html": "first party"}],
        },
        "reasons": reasons,
        "structured_identity": {"relationships": relationships or []},
        "semantic_identity": {"decision": semantic},
        "identity_assessment": {
            "support_keys": ["first_party_identity", "domain_identity"],
            "publishable": True,
            "provisionally_publishable": True,
            "conflicts": [],
        },
        "email_source_url": url if has_contact else "",
        "phone_source_url": "",
        "has_contact": has_contact,
        "context_failed": False,
        "email_failed": False,
        "final_score": 80,
    }


class EvidenceAcquisitionTests(unittest.TestCase):
    def test_missing_legal_identity_produces_legal_first_plan(self):
        evaluation = _evaluation(
            "https://ornek.example",
            ["page_identity_medium:1/1", "country_identity_tr_text"],
        )
        state = evidence_acquisition.analyze(
            "ORNEK METAL SANAYI",
            [evaluation],
            metadata={"sector": "celik uretimi"},
        )
        self.assertIn("missing_legal_identity", state.gaps)
        self.assertEqual(state.crawl_scopes[0], "legal")
        self.assertTrue(state.search_queries[0].startswith('"ORNEK METAL SANAYI"'))

    def test_no_progress_stops_bounded_loop(self):
        state = evidence_acquisition.analyze("ORNEK METAL", [])
        self.assertFalse(
            evidence_acquisition.should_continue(state, state, 1, 3)
        )

    def test_evidence_from_different_candidates_does_not_fill_one_fingerprint(self):
        identity_only = _evaluation(
            "https://holding.example",
            [
                "page_identity_strong:2/2",
                "legal_name_full_match:2",
                "country_identity_tr_text",
            ],
            has_contact=False,
        )
        contact_only = _evaluation(
            "https://other.example",
            ["context_match:1/1", "country_identity_tr_text"],
        )
        contact_only["identity_assessment"] = {
            "support_keys": [],
            "provisionally_publishable": False,
            "conflicts": [],
        }

        state = evidence_acquisition.analyze(
            "ORNEK METAL", [identity_only, contact_only],
        )

        self.assertIn("missing_contact", state.gaps)

    def test_unresolved_state_never_reports_complete_without_ready_candidate(self):
        evaluation = _evaluation(
            "https://holding.example",
            [
                "page_identity_strong:2/2",
                "legal_name_full_match:2",
                "context_match:1/1",
                "country_identity_tr_text",
            ],
        )
        evaluation["identity_assessment"]["provisionally_publishable"] = False
        evaluation["identity_assessment"]["publishable"] = False

        state = evidence_acquisition.analyze(
            "ORNEK METAL", [evaluation], resolution_status="unresolved",
        )

        self.assertEqual(state.gaps, frozenset({"missing_identity_coherence"}))

    def test_targeted_page_order_obeys_current_gap(self):
        discovered = [
            {"url": "https://x.example/contact", "kind": "contact"},
            {"url": "https://x.example/kvkk", "kind": "legal"},
            {"url": "https://x.example/about", "kind": "about"},
        ]
        selected = site_mapper.balanced_urls(
            discovered, [], 2, preferred_kinds=("legal", "about"),
        )
        self.assertEqual(selected, [
            "https://x.example/kvkk",
            "https://x.example/about",
        ])

    def test_missing_legal_plan_spends_second_slot_on_another_legal_page(self):
        discovered = [
            {"url": "https://x.example/kvkk", "kind": "legal"},
            {"url": "https://x.example/ticari-bilgiler", "kind": "legal"},
            {"url": "https://x.example/privacy", "kind": "privacy"},
            {"url": "https://x.example/about", "kind": "about"},
        ]
        selected = site_mapper.balanced_urls(
            discovered,
            [],
            3,
            preferred_kinds=("legal", "privacy", "about"),
        )
        self.assertEqual(selected, [
            "https://x.example/kvkk",
            "https://x.example/privacy",
            "https://x.example/ticari-bilgiler",
        ])


class SemanticEntityTypeTests(unittest.TestCase):
    def test_metal_target_rejects_bank_candidate_only_on_strong_site_type(self):
        result = entity_semantics.assess(
            "DILER DEMIR CELIK",
            {"sector": "metal uretimi"},
            [{"html": "<p>Bankacilik finans kredi hizmetleri</p>"}],
        )
        self.assertEqual(result["decision"], "conflict")
        self.assertIn("metal_industry:finance", result["conflicts"])

    def test_weak_unrelated_word_stays_unknown(self):
        result = entity_semantics.assess(
            "ORNEK MAKINE",
            {},
            [{"html": "<p>Finans bilgileri</p>"}],
        )
        self.assertEqual(result["decision"], "unknown")

    def test_matching_manufacturing_site_supports_candidate(self):
        result = entity_semantics.assess(
            "ORNEK MAKINE",
            {},
            [{"html": "<p>Endustriyel makine ve otomasyon sistemleri</p>"}],
        )
        self.assertEqual(result["decision"], "match")

    def test_matching_activity_prevents_group_page_false_conflict(self):
        result = entity_semantics.assess(
            "ORNEK CELIK",
            {},
            [{
                "html": (
                    "<p>Demir celik metal profil uretimi</p>"
                    "<p>Grup finans ve yatirim hizmetleri</p>"
                ),
            }],
        )
        self.assertEqual(result["decision"], "match")
        self.assertEqual(result["conflicts"], [])


class CandidateTournamentTests(unittest.TestCase):
    def test_stronger_candidate_eliminates_weaker_candidate(self):
        weak = _evaluation(
            "https://ornek.com",
            [
                "page_identity_medium:1/1",
                "legal_name_phrase_match:1",
                "country_identity_tr_text",
            ],
        )
        strong = _evaluation(
            "https://ornekmetal.com.tr",
            [
                "page_identity_strong:2/2",
                "structured_identity_strong:2/2",
                "legal_name_full_match:2",
                "context_match:1/1",
                "country_identity_tr_tld",
            ],
            semantic="match",
        )
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [weak, strong],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, strong)


class DiscoveryAndMemoryTests(unittest.TestCase):
    def test_full_legal_name_is_first_primary_query(self):
        queries = search._primary_queries(
            "ORNEK METAL SANAYI LIMITED SIRKETI", {},
        )
        self.assertEqual(
            queries[0],
            '"ORNEK METAL SANAYI LIMITED SIRKETI" Turkiye official website',
        )
        self.assertTrue(search._query_covers_full_identity(
            "ORNEK METAL SANAYI LIMITED SIRKETI",
            queries[0],
        ))
        self.assertFalse(search._query_covers_full_identity(
            "ORNEK METAL SANAYI LIMITED SIRKETI",
            "ornek Turkiye official website",
        ))

    def test_official_registry_exposes_identity_but_not_contacts(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "registry.json"
            path.write_text(json.dumps({"entities": [{
                "legal_name": "Ornek Metal",
                "official_domain": "https://ornek.example",
                "email": "do-not-publish@registry.example",
                "phone": "+90 555 000 00 00",
                "source_class": "official_government_registry",
                "verification_status": "verified",
                "registry_url": "https://registry.example/entity/1",
            }]}), encoding="utf-8")
            official_registry._records.cache_clear()
            with patch.object(config, "OFFICIAL_REGISTRY_FILE", path):
                found = official_registry.find("ORNEK METAL")
            official_registry._records.cache_clear()
        self.assertEqual(found[0]["official_domain"], "ornek.example")
        self.assertNotIn("email", found[0])
        self.assertNotIn("phone", found[0])

    def test_memory_requires_published_same_site_contact_and_revalidates(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "memory.jsonl"
            row = {
                "company": "ORNEK METAL",
                "status": "OK_MEDIUM_CONFIDENCE",
                "website": "https://ornek.example",
                "email_source_url": "https://ornek.example/contact",
                "phone_source_url": "",
                "__evaluation": {
                    "_identity_resolution": "candidate_resolved_by_target_fingerprint",
                    "identity_assessment": {"conflicts": []},
                    "crawl": {
                        "pages": ["https://ornek.example/legal"],
                    },
                },
            }
            with patch.object(config, "VERIFIED_ENTITY_MEMORY_FILE", path):
                self.assertEqual(entity_memory.remember([row]), 1)
                found = entity_memory.candidates("ORNEK METAL")
                candidates = {}
                search._add_entity_memory_candidates(candidates, "ORNEK METAL")
        self.assertEqual(found[0]["authority"], "discovery_hint_revalidate_every_run")
        self.assertEqual(candidates["ornek.example"]["role"], "company_candidate")
        self.assertIn("requires_first_party_revalidation", candidates["ornek.example"]["reason"])

    def test_site_recovery_stays_in_same_registrable_domain(self):
        self.assertEqual(site_recovery.root_variants("https://ornek.example"), [
            "https://ornek.example",
            "http://ornek.example",
            "https://www.ornek.example",
            "http://www.ornek.example",
        ])

    def test_accumulated_legal_identity_allows_same_domain_contact_seed(self):
        candidates = {}
        search._add_search_results(
            candidates,
            "ORNEK METAL",
            "ORNEK METAL Turkiye official website",
            [
                {
                    "href": "https://ornek.example/",
                    "title": "Ornek Metal",
                    "body": "Ornek Metal resmi web sitesi",
                },
                {
                    "href": "https://ornek.example/iletisim",
                    "title": "Iletisim",
                    "body": "Telefon ve e-posta",
                },
            ],
            {},
        )
        self.assertEqual(
            candidates["ornek.example"]["_contact_seed_urls"],
            ["https://ornek.example/iletisim"],
        )

    def test_legal_search_result_becomes_same_domain_identity_seed(self):
        candidates = {}
        search._add_search_results(
            candidates,
            "ORNEK METAL SANAYI A.S.",
            "ORNEK METAL Turkiye official website",
            [{
                "href": "https://ornek.example/kvkk-aydinlatma-metni",
                "title": "KVKK Aydinlatma Metni",
                "body": "Unvan: Ornek Metal Sanayi Anonim Sirketi",
            }],
            {},
        )
        self.assertEqual(
            candidates["ornek.example"]["_identity_seed_urls"],
            ["https://ornek.example/kvkk-aydinlatma-metni"],
        )

    def test_identity_seeds_reject_other_domains_and_product_pages(self):
        safe = crawler._safe_identity_seed_urls("https://ornek.example", [
            "https://ornek.example/kvkk",
            "https://ornek.example/products",
            "https://evil.example/legal",
        ])
        self.assertEqual(safe, ["https://ornek.example/kvkk"])

    def test_identity_seeds_remove_search_tracking_parameters(self):
        safe = crawler._safe_identity_seed_urls("https://ornek.example", [
            "https://ornek.example/kvkk?srsltid=abc&utm_source=search&id=7#x",
        ])
        self.assertEqual(safe, ["https://ornek.example/kvkk?id=7"])

    def test_incomparable_candidates_use_deterministic_identity_key(self):
        legal = _evaluation(
            "https://ornekmetal.com",
            [
                "page_identity_medium:1/1",
                "legal_name_full_match:2",
                "country_identity_tr_text",
            ],
        )
        context = _evaluation(
            "https://ornek-metal.com.tr",
            [
                "page_identity_strong:2/2",
                "legal_name_phrase_match:1",
                "context_match:1/1",
                "country_identity_tr_tld",
            ],
        )
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [legal, context],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, legal)

    def test_direct_entity_site_outranks_parent_ownership_page(self):
        direct = _evaluation(
            "https://ornekmetal.example",
            [
                "page_identity_strong:2/2",
                "structured_identity_strong:2/2",
                "legal_name_phrase_match:2",
                "country_identity_tr_text",
            ],
        )
        parent = _evaluation(
            "https://holding.example",
            [
                "page_identity_strong:2/2",
                "structured_identity_strong:2/2",
                "legal_name_ownership_match:2",
                "country_identity_tr_text",
            ],
        )
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [parent, direct],
        )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, direct)

    def test_explicit_first_party_family_is_not_ambiguous(self):
        first = _evaluation(
            "https://global.example",
            [
                "page_identity_strong:2/2",
                "legal_name_full_match:2",
                "country_identity_tr_text",
            ],
            relationships=[{
                "kind": "subOrganization",
                "url": "https://turkey.example",
            }],
        )
        second = _evaluation(
            "https://turkey.example",
            [
                "page_identity_strong:2/2",
                "legal_name_full_match:2",
                "country_identity_tr_text",
            ],
        )
        components = relationship_graph.connected_domain_components(
            [first, second],
        )
        self.assertTrue(relationship_graph.same_official_family(
            "global.example", "turkey.example", components,
        ))
        result = entity_resolution.resolve_candidates(
            "ORNEK METAL", [first, second],
        )
        self.assertEqual(result.status, "resolved")


class AutonomousOrchestrationTests(unittest.TestCase):
    def test_contact_crawl_requires_plausible_first_party_identity(self):
        weak = _evaluation(
            "https://unrelated.example",
            ["page_identity_medium:1/1", "country_identity_tr_text"],
        )
        weak["identity_assessment"] = {
            "support_keys": [],
            "publishable": False,
            "provisionally_publishable": False,
            "conflicts": [],
        }
        strong = _evaluation(
            "https://ornekmetal.com.tr",
            [
                "page_identity_strong:2/2",
                "legal_name_phrase_match:2",
                "country_identity_tr_tld",
            ],
        )

        self.assertFalse(main._full_crawl_worthy("ORNEK METAL", weak))
        self.assertTrue(main._full_crawl_worthy("ORNEK METAL", strong))

    def test_contact_crawl_can_acquire_country_evidence_from_plausible_site(self):
        plausible = _evaluation(
            "https://akkentlokum.com",
            ["page_identity_medium:2/4", "country_identity_unproven"],
            has_contact=False,
        )
        plausible["identity_assessment"]["provisionally_publishable"] = False
        plausible["identity_assessment"]["publishable"] = False
        plausible["identity_assessment"]["conflicts"] = []

        self.assertTrue(main._full_crawl_worthy(
            "AKKENT SEKERLEME GIDA SANAYI", plausible,
        ))

    def test_search_consensus_can_trigger_bounded_evidence_crawl(self):
        plausible = _evaluation(
            "https://agrozan.com",
            ["page_identity_weak:2/7", "country_identity_unproven"],
            has_contact=False,
        )
        plausible["candidate"]["_official_query_evidence"] = 3
        plausible["identity_assessment"].update({
            "provisionally_publishable": False,
            "publishable": False,
            "conflicts": [],
        })
        self.assertTrue(main._full_crawl_worthy(
            "AGROZAN TARIM GIDA", plausible,
        ))

    def test_missing_country_is_an_evidence_gap_not_an_identity_conflict(self):
        assessment = identity.assess(
            "AKKENT SEKERLEME GIDA SANAYI",
            {"url": "https://akkentlokum.com", "role": "company_candidate"},
            ["page_identity_medium:2/4", "country_identity_unproven"],
            {},
        )

        self.assertFalse(assessment["conflicts"])
        self.assertIn(
            "target_country_unproven",
            {item["kind"] for item in assessment["neutral"]},
        )

    def test_gap_round_rechecks_candidate_and_can_resolve(self):
        candidate = {
            "url": "https://unrelated.example",
            "role": "company_candidate",
            "score": 70,
        }
        weak = _evaluation(
            candidate["url"],
            ["page_identity_medium:1/1", "country_identity_tr_text"],
        )
        weak["candidate"] = candidate
        strong = _evaluation(
            candidate["url"],
            [
                "page_identity_strong:2/2",
                "structured_identity_strong:2/2",
                "legal_name_full_match:2",
                "country_identity_tr_text",
            ],
        )
        strong["candidate"] = candidate
        initial = entity_resolution.resolve_candidates(
            "ORNEK METAL", [weak],
        )
        with patch.object(
            config, "MAX_AUTONOMOUS_RESOLUTION_ROUNDS", 1,
        ), patch(
            "main.search.find_targeted_candidates", return_value=[],
        ), patch(
            "main._evaluate_candidate_with_stage", return_value=strong,
        ):
            evaluations, resolution, state = main._complete_resolution_evidence(
                "ORNEK METAL", {}, [candidate], [weak], initial,
            )
        self.assertEqual(resolution.status, "resolved")
        self.assertFalse(state.gaps)
        self.assertEqual(len(evaluations[0]["_automation"]["rounds"]), 1)

    def test_quality_audit_flags_no_valid_publication(self):
        rows = [{
            "company": "ORNEK",
            "status": "OK_MEDIUM_CONFIDENCE",
            "website": "https://ornek.example",
            "email_source_url": "https://directory.example/profile",
            "phone_source_url": "",
            "__evaluation": {"identity_assessment": {"conflicts": []}},
        }]
        audit = quality_audit.payload(rows)
        self.assertEqual(audit["invalid_publication_count"], 1)
        self.assertEqual(audit["published_count"], 1)

    def test_quality_audit_flags_excluded_third_party_website(self):
        rows = [{
            "company": "ORNEK",
            "status": "OK_MEDIUM_CONFIDENCE",
            "website": "https://tradeatlas.com",
            "email_source_url": "https://tradeatlas.com/company/ornek",
            "phone_source_url": "https://tradeatlas.com/company/ornek",
            "__evaluation": {"identity_assessment": {"conflicts": []}},
        }]
        audit = quality_audit.payload(rows)
        self.assertEqual(audit["invalid_publication_count"], 1)
        self.assertEqual(
            audit["invalid_publications"][0]["reason"],
            "published_excluded_third_party_domain",
        )

    def test_quality_audit_accepts_compact_resolution_string(self):
        audit = quality_audit.payload([{
            "company": "ORNEK",
            "status": "REVIEW_NEEDED",
            "__evaluation": {
                "identity_resolution": "no_candidate_proved_target_fingerprint",
            },
        }])
        self.assertEqual(
            audit["terminal_reason_counts"],
            {"no_candidate_proved_target_fingerprint": 1},
        )

    def test_replay_prefix_chooses_richest_same_url_crawl(self):
        replay_snapshot.reset()
        replay_snapshot.record(
            "crawl_cache", "site",
            "https://ornek.example|pages=6|seeds=/contact",
            3, {"pages": [{"url": "home"}]},
        )
        replay_snapshot.record(
            "crawl_cache", "site",
            "https://ornek.example|pages=6|seeds=/iletisim",
            3, {"pages": [{"url": "home"}, {"url": "contact"}]},
        )
        hit, value = replay_snapshot.lookup_prefix(
            "crawl_cache", "site",
            "https://ornek.example|pages=6|",
            3,
        )
        replay_snapshot.reset()
        self.assertTrue(hit)
        self.assertEqual(len(value["pages"]), 2)


if __name__ == "__main__":
    unittest.main()
