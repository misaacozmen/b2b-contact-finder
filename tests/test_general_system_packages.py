import unittest
from unittest.mock import patch

import main
from modules import crawler, extractor, identity, query_planner, relationship_graph, site_mapper


class AdaptiveQueryPlannerTests(unittest.TestCase):
    def test_new_representation_hint_enters_bounded_plan_without_duplicates(self):
        queries = query_planner.adaptive_queries(
            "VEKTOR MAKINE SANAYI LIMITED SIRKETI",
            {
                "representations": "Atlas Robotics; Atlas Robotics",
                "brands": "Vektor Pro",
                "listed_address": "OSB 4. Cadde, Konya, Turkiye",
                "sector": "endustriyel otomasyon",
            },
            already_run={'"vektor" Turkiye resmi sitesi'},
            limit=6,
        )
        self.assertEqual(len(queries), len({value.casefold() for value in queries}))
        self.assertTrue(any("Atlas Robotics" in value and "distributor" in value for value in queries))
        self.assertTrue(any("kvkk" in value for value in queries))

    def test_listing_hint_is_query_input_not_identity_authority(self):
        queries = query_planner.adaptive_queries(
            "ORBITA TEKNOLOJI", None, related_name_hints=["Nova Sistem"], limit=1,
        )
        self.assertEqual(queries, ['"Nova Sistem" Turkiye official website'])


class FirstPartyIdentityEngineTests(unittest.TestCase):
    def test_short_brand_needs_third_first_party_component(self):
        reasons = [
            "page_identity_strong:1/1", "structured_identity_strong:1/1",
            "country_identity_tr_text",
        ]
        candidate = {"url": "https://unrelated-domain.example", "role": "company_candidate", "reason": "search_text_identity:1/1"}
        weak = identity.assess("NOVA", candidate, reasons, {"names": ["NOVA"]})
        strong = identity.assess(
            "NOVA", candidate, reasons,
            {"names": ["NOVA"], "corroborated_addresses": ["Ankara"]},
        )
        self.assertFalse(weak["strong_first_party_bundle"])
        self.assertTrue(strong["strong_first_party_bundle"])

    def test_explicit_legal_company_type_mismatch_blocks_identity(self):
        assessment = identity.assess(
            "ORBITA MAKINE LIMITED SIRKETI",
            {"url": "https://orbita.example", "role": "company_candidate", "reason": "domain_hits:1/1"},
            ["page_identity_strong:1/1", "structured_identity_strong:1/1", "country_identity_tr_text"],
            {"legal_names": ["Orbita Makine Anonim Sirketi"], "names": ["Orbita Makine"]},
        )
        self.assertIn("structured_company_type_mismatch", {item["kind"] for item in assessment["conflicts"]})
        self.assertFalse(assessment["provisionally_publishable"])

    def test_first_party_legal_identifier_is_a_bundle_component(self):
        assessment = identity.assess(
            "NOVA",
            {"url": "https://nova.example", "role": "company_candidate", "reason": "search_text_identity:1/1"},
            ["page_identity_strong:1/1", "structured_identity_strong:1/1", "country_identity_tr_text"],
            {"names": ["NOVA"], "identifiers": ["mersis:0123456789012345"]},
        )
        self.assertTrue(assessment["strong_first_party_bundle"])

    def test_multiple_same_kind_legal_identifiers_block_identity(self):
        assessment = identity.assess(
            "ORBITA MAKINE",
            {"url": "https://orbita.example", "role": "company_candidate", "reason": "domain_hits:2/2"},
            ["page_identity_strong:2/2", "structured_identity_strong:2/2", "country_identity_tr_text"],
            {"names": ["Orbita Makine"], "identifiers": [
                "mersis:0123456789012345", "mersis:9876543210987654",
            ]},
        )
        self.assertIn("multiple_first_party_legal_identifiers", {item["kind"] for item in assessment["conflicts"]})
        self.assertFalse(assessment["provisionally_publishable"])


class FirstPartyLegalExtractionTests(unittest.TestCase):
    def test_microdata_and_labeled_legal_identifiers_keep_provenance(self):
        html = """
        <div itemscope itemtype="https://schema.org/Organization">
          <span itemprop="legalName">Orbita Makine Limited Sirketi</span>
          <meta itemprop="taxID" content="1234567890">
          <address itemprop="address">OSB 1. Cadde No: 5 Ankara</address>
        </div>
        <p>MERSİS No: 0123456789012345</p>
        <p>VKN: 1234567890</p>
        <p>KEP Adresi: orbita@hs01.kep.tr</p>
        """
        found = extractor.extract_organization_evidence(
            html, "https://orbita.example/kvkk",
        )
        self.assertIn("Orbita Makine Limited Sirketi", found["legal_names"])
        self.assertIn("mersis:0123456789012345", found["identifiers"])
        self.assertIn("vkn:1234567890", found["identifiers"])
        self.assertIn("kep:orbita@hs01.kep.tr", found["identifiers"])
        self.assertTrue(any(value.startswith("taxID:") for value in found["identifiers"]))
        self.assertTrue(found["claims"])
        self.assertTrue(all(claim["source_url"] == "https://orbita.example/kvkk" for claim in found["claims"]))
        self.assertTrue(all(claim["page_scope"] == "legal" for claim in found["claims"]))
        self.assertEqual({claim["independence_key"] for claim in found["claims"]}, {
            "first_party_domain:orbita.example",
        })

    def test_trade_registry_heading_is_not_an_identifier(self):
        found = extractor.extract_organization_evidence(
            "<p>Ticaret Sicil Tarihi: 01.01.2020</p>",
            "https://official.example/kvkk",
        )
        self.assertFalse(any(value.startswith("trade_registry:") for value in found["identifiers"]))

    def test_labeled_visible_legal_name_becomes_a_sourced_claim(self):
        found = extractor.extract_organization_evidence(
            "<p>Ticari Unvan: Orbita Makine Sanayi Limited Sirketi</p>",
            "https://orbita.example/legal",
        )
        self.assertIn("Orbita Makine Sanayi Limited Sirketi", found["legal_names"])
        claims = [claim for claim in found["claims"] if claim["field"] == "legal_name"]
        self.assertEqual({claim["method"] for claim in claims}, {"visible_labeled"})
        self.assertEqual({claim["source_url"] for claim in claims}, {"https://orbita.example/legal"})

    def test_labeled_legal_name_is_identity_not_declared_relationship(self):
        pages = [{
            "url": "https://product.example/legal",
            "html": (
                "<html><title>Product</title><body>"
                "Ticari Unvan: Delta Otomotiv Sanayi ve Ticaret A.S."
                "</body></html>"
            ),
        }]

        _, reason, structured = main._structured_identity_score(
            "Delta Otomotiv Sanayi ve Ticaret A.S.", pages,
        )

        self.assertEqual(reason, "structured_identity_strong:1/1@scope=legal_name")
        self.assertEqual(
            structured["legal_names"],
            ["Delta Otomotiv Sanayi ve Ticaret A.S"],
        )


class RelationshipGraphTests(unittest.TestCase):
    def test_explicit_first_party_branch_url_creates_typed_edge(self):
        edges = relationship_graph.typed_domain_edges(
            "global.example", "turkey.example",
            {"relationships": [{"kind": "subOrganization", "name": "Turkey Entity", "url": "https://turkey.example"}]},
            {},
        )
        self.assertEqual(edges, ["first_party_subOrganization"])

    def test_name_only_global_brand_claim_does_not_make_local_site_family(self):
        edges = relationship_graph.typed_domain_edges(
            "global.example", "turkey.example",
            {"relationships": [{"kind": "brand", "name": "Example", "url": ""}]},
            {},
        )
        self.assertEqual(edges, [])


class SmartOfficialSiteMappingTests(unittest.TestCase):
    def test_balanced_map_keeps_legal_location_and_contact_but_rejects_external(self):
        html = """
        <a href='/contact'>Contact</a><a href='/kvkk'>KVKK</a>
        <a href='/locations'>Locations</a><a href='https://directory.example/contact'>Contact</a>
        """
        mapped = site_mapper.discover(html, "https://official.example")
        urls = site_mapper.balanced_urls(mapped, [], 4)
        self.assertIn("https://official.example/contact", urls)
        self.assertIn("https://official.example/kvkk", urls)
        self.assertIn("https://official.example/locations", urls)
        self.assertFalse(any("directory.example" in value for value in urls))

    def test_noise_mailboxes_are_not_publishable_and_marketing_loses_to_info(self):
        records = [
            {"value": "privacy@official.example", "source_url": "https://official.example/privacy", "label": "general"},
            {"value": "marketing@official.example", "source_url": "https://official.example/contact", "label": "marketing"},
            {"value": "info@official.example", "source_url": "https://official.example/contact", "label": "general"},
        ]
        selected = main._select_best_email_record("Official Makine", "https://official.example", records)
        self.assertFalse(main._email_is_usable("webmaster.tr@official.example"))
        self.assertEqual(selected["value"], "info@official.example")

    def test_primary_contacts_output_receives_only_publishable_rows(self):
        rows = [
            {"company": "Verified", "status": "OK_HIGH_CONFIDENCE", "website": "https://verified.example"},
            {"company": "Review", "status": "REVIEW_NEEDED", "website": "https://review.example"},
        ]
        with patch("main.evidence.write_jsonl"), patch(
            "main.entity_registry.write_observations"
        ), patch("main.excel.write_contacts") as write_contacts, patch(
            "main.excel.write_failed"
        ), patch("main.excel.write_website_candidates"), patch(
            "main.report.build_report", return_value="report"
        ), patch("pathlib.Path.write_text"), patch("main.runtime.write"):
            main._write_outputs(rows, 0)
        published = list(write_contacts.call_args_list[0].args[1])
        self.assertEqual([row["company"] for row in published], ["Verified"])


class ControlledRecoveryPipelineTests(unittest.TestCase):
    def test_static_sitemap_page_prevents_browser_fallback(self):
        def fake_fetch(url):
            if url == "https://official.example/kvkk":
                return "Official Makine Limited Sirketi Ankara", None
            return None, "http_403"

        with patch("modules.crawler._try_fetch", side_effect=fake_fetch), patch(
            "modules.crawler._robots_and_sitemaps",
            return_value=(None, ["https://official.example/sitemap.xml"]),
        ), patch(
            "modules.crawler._sitemap_contact_urls",
            return_value=["https://official.example/kvkk"],
        ), patch("modules.crawler._try_render") as render:
            result = crawler.fetch_site("https://official.example", profile="identity")
        self.assertTrue(result["pages"])
        render.assert_not_called()

    def test_cross_company_redirect_remains_a_safe_abstention(self):
        with patch("modules.crawler._try_fetch", return_value=(None, "cross_domain_redirect:https://other.example")), patch(
            "modules.crawler._robots_and_sitemaps", return_value=(None, [])
        ), patch("modules.crawler._sitemap_contact_urls", return_value=[]), patch(
            "modules.crawler._try_render", return_value=(None, "js_fallback_disabled")
        ):
            result = crawler.fetch_site("https://official.example", profile="identity")
        self.assertFalse(result["pages"])
        self.assertNotEqual(result["url"], "https://other.example")


if __name__ == "__main__":
    unittest.main()
