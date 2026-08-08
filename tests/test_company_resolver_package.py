import unittest
from unittest.mock import patch

import config
import main
from modules import company_resolvers, scorer, search


class CompanyResolverPackageTests(unittest.TestCase):
    def test_hostname_validation_rejects_path_artifact(self):
        self.assertTrue(scorer.is_valid_hostname("yuce-tibbi.com"))
        self.assertFalse(scorer.is_valid_hostname("default.asp"))

    def test_bare_domain_is_extracted_from_pdf_or_listing_snippet(self):
        result = {
            "body": "YÜ-CE TIBBİ GEREÇLER SANAYİ resmi iletişim: yuce-tibbi.com",
        }
        websites = search._snippet_outbound_websites(
            result, "https://directory.example/files/catalog.pdf",
        )
        self.assertEqual(websites, ["https://yuce-tibbi.com"])

    def test_email_domain_is_not_mistaken_for_bare_outbound_site(self):
        result = {"body": "E-posta: sales@unrelated.com"}
        self.assertEqual(
            search._snippet_outbound_websites(result, "https://directory.example/item"),
            [],
        )

    def test_unlabelled_bare_domain_on_listing_page_is_ignored(self):
        result = {"body": "Company profile examplebrand.com"}
        self.assertEqual(
            search._snippet_outbound_websites(result, "https://directory.example/item"),
            [],
        )

    def test_resolver_candidate_is_explicitly_discovery_only(self):
        candidates = {}
        trace = []
        resolved = [{
            "domain": "examplebrand.com", "providers": ["brandfetch"],
            "resolved_name": "Example Brand", "rank": 1, "claimed": True,
        }]
        with patch.object(company_resolvers, "resolve_company_domains", return_value=resolved):
            search._add_resolver_candidates(candidates, "Example Brand", trace)
        candidate = candidates["examplebrand.com"]
        self.assertIn("discovery_only_not_identity_authority", candidate["reason"])
        self.assertEqual(candidate["_official_query_evidence"], 0)
        self.assertEqual(trace[0]["source"], "company_domain_resolvers")

    def test_resolver_union_preserves_both_providers(self):
        with patch.object(company_resolvers, "brandfetch_domains", return_value=[{
            "provider": "brandfetch", "domain": "example.com", "resolved_name": "Example",
            "rank": 1, "claimed": False,
        }]), patch.object(company_resolvers, "hunter_domains", return_value=[{
            "provider": "hunter_domain_finder", "domain": "example.com", "resolved_name": "Example",
            "rank": 1, "claimed": False,
        }]):
            results = company_resolvers.resolve_company_domains("Example")
        self.assertEqual(results[0]["providers"], ["brandfetch", "hunter_domain_finder"])

    def test_resolver_rejects_unrelated_resolved_company_name(self):
        with patch.object(company_resolvers, "brandfetch_domains", return_value=[]), patch.object(
            company_resolvers, "hunter_domains", return_value=[{
                "provider": "hunter_domain_finder", "domain": "woodeninteriors.com.tr",
                "resolved_name": "Wooden Interiors Dekorasyon A.S.",
                "rank": 1, "claimed": False,
            }],
        ):
            results = company_resolvers.resolve_company_domains(
                "Northstar Surgical Instruments Limited",
            )
        self.assertEqual(results, [])

    def test_resolver_accepts_long_public_brand_anchor(self):
        with patch.object(company_resolvers, "brandfetch_domains", return_value=[{
            "provider": "brandfetch", "domain": "northstaradvanced.com",
            "resolved_name": "Northstar Advanced Materials", "rank": 1,
            "claimed": True,
        }]), patch.object(company_resolvers, "hunter_domains", return_value=[]):
            results = company_resolvers.resolve_company_domains(
                "Northstar Ileri Malzemeler Sanayi A.S.",
            )
        self.assertEqual([item["domain"] for item in results], ["northstaradvanced.com"])

    def test_pdf_on_ordinary_host_can_create_discovery_only_outbound_candidate(self):
        candidates = {}
        result = {
            "href": "https://public-records.example/files/exporters.pdf",
            "title": "Exporter catalogue",
            "body": (
                "Northstar Precision Instruments Limited resmi iletisim "
                "northstar-instruments.com"
            ),
        }
        search._add_search_results(
            candidates,
            "Northstar Precision Instruments Limited",
            '"Northstar Precision Instruments Limited" official website',
            [result],
        )
        candidate = candidates["northstar-instruments.com"]
        self.assertEqual(candidate["query"], "snippet_outbound_discovery")
        self.assertEqual(candidate["_official_query_evidence"], 0)
        self.assertIn("discovery_only_not_identity_authority", candidate["reason"])

    def test_related_name_can_anchor_pdf_outbound_discovery(self):
        candidates = {}
        result = {
            "href": "https://industry-body.example/reports/members.pdf",
            "title": "Member register",
            "body": "Aurora Instruments Limited web sayfa aurora-instruments.com",
        }
        search._add_related_hint_results(
            candidates,
            "Northstar Medical Technologies A.S.",
            "Aurora Instruments Limited",
            '"Aurora Instruments Limited" Turkiye official website',
            [result],
        )
        candidate = candidates["aurora-instruments.com"]
        self.assertEqual(candidate["query"], "snippet_outbound_discovery")
        self.assertEqual(candidate["_official_query_evidence"], 0)
        self.assertEqual(
            candidate["_outbound_discovery_evidence"][0]["identity_name"],
            "Aurora Instruments Limited",
        )

    def test_disabled_resolvers_make_no_network_call(self):
        with patch.object(config, "ENABLE_BRANDFETCH_DOMAIN_SEARCH", False), patch.object(
            config, "ENABLE_HUNTER_DOMAIN_FINDER", False,
        ), patch("modules.company_resolvers.requests.get") as request:
            self.assertEqual(company_resolvers.resolve_company_domains("Example"), [])
        request.assert_not_called()

    def test_rare_company_token_adds_bounded_ranking_signal(self):
        scorer.configure_company_token_frequencies([
            "ZEPHYR MAKINE", "ALFA MAKINE", "BETA MAKINE", "GAMA MAKINE",
            "DELTA MAKINE", "OMEGA MAKINE", "NOVA MAKINE", "VEKTOR MAKINE",
            "PENTA MAKINE", "SIGMA MAKINE",
        ])
        bonus, tokens = scorer.rare_identity_token_bonus("ZEPHYR MAKINE", "zephyr.com")
        self.assertEqual(bonus, 4)
        self.assertEqual(tokens, ["zephyr"])

    def test_candidate_lifecycle_is_written_for_every_candidate(self):
        candidates = [
            {"domain": "chosen.com", "url": "https://chosen.com", "query": "search", "score": 90},
            {"domain": "other.com", "url": "https://other.com", "query": "resolver", "score": 70},
        ]
        row = main._attach_candidates({
            "website": "https://chosen.com", "status": "OK_FULL", "confidence": "high",
        }, candidates)
        stages = row["__candidate_evaluations"]
        self.assertEqual(stages[0]["stages"][-1]["stage"], "published")
        self.assertEqual(stages[1]["stages"][-1]["stage"], "not_evaluated")

    def test_close_publishable_aliases_trigger_ambiguity_gate(self):
        def evaluation(domain):
            return {
                "candidate": {"url": f"https://{domain}", "query": "search"},
                "identity_assessment": {
                    "provisionally_publishable": True, "support_count": 2, "conflicts": [],
                },
                "final_score": 91,
                "structured_identity": {}, "reasons": [], "email": "", "phone": "",
            }
        with patch.object(main, "_same_official_family", return_value=False):
            self.assertTrue(main._close_identity_margin_conflict(
                "Unusual Legal Name", evaluation("brand-one.com"), evaluation("brand-two.com"),
            ))

    def test_full_contact_crawl_cannot_erase_stronger_light_identity(self):
        candidate = {"url": "https://official.example", "_stage_history": [{"stage": "full_evaluated"}]}
        light = {
            "candidate": candidate,
            "final_score": 94,
            "reasons": ["page_identity_strong:3/3", "legal_name_phrase_match:3"],
            "structured_identity": {"legal_names": ["Example Legal A.S."]},
            "identity_assessment": {
                "publishable": True,
                "provisionally_publishable": True, "support_count": 2, "conflicts": [],
            },
        }
        full = {
            "candidate": candidate,
            "final_score": 88,
            "reasons": ["page_identity_medium:1/3", "email_domain_match"],
            "structured_identity": {},
            "identity_assessment": {
                "provisionally_publishable": False, "support_count": 1, "conflicts": [],
            },
        }
        merged = main._preserve_identity_phase_evidence(full, light)
        self.assertTrue(merged["identity_assessment"]["provisionally_publishable"])
        self.assertIn("email_domain_match", merged["reasons"])
        self.assertIn("page_identity_strong:3/3", merged["reasons"])
        self.assertNotIn("page_identity_medium:1/3", merged["reasons"])

    def test_full_crawl_hard_identity_conflict_is_never_overridden(self):
        light = {"identity_assessment": {"publishable": True, "provisionally_publishable": True, "support_count": 2}}
        full = {
            "identity_assessment": {
                "provisionally_publishable": False, "support_count": 0,
                "conflicts": [{"kind": "owner_mismatch"}],
            },
        }
        self.assertIs(main._preserve_identity_phase_evidence(full, light), full)
        self.assertEqual(full["identity_assessment"]["support_count"], 0)


if __name__ == "__main__":
    unittest.main()
