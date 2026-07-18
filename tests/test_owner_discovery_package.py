import unittest
from unittest.mock import Mock, patch

import config
import main
from modules import extractor, identity, search


class OwnerDiscoveryPackageTests(unittest.TestCase):
    def test_intrinsic_official_domain_wins_before_profile_path_heuristics(self):
        self.assertEqual(
            search._candidate_role(
                "Example Brand", "https://examplebrand.com/company/profile",
                "Example Brand", "Company profile",
            ),
            "company_candidate",
        )

    def test_slug_entity_page_is_discovery_only_when_domain_is_not_the_brand(self):
        self.assertEqual(
            search._candidate_role(
                "Example Brand", "https://catalog.example/tmi?slug=example-brand",
                "Example Brand", "",
            ),
            "directory",
        )

    def test_discovery_bridges_do_not_displace_company_candidates_in_ranking(self):
        ranked = search.rank_candidates([
            {"url": "https://fair.example/brand/x", "score": 99, "role": "fair_profile"},
            {"url": "https://news.example/press/x", "score": 98, "role": "news"},
            {"url": "https://brand.example", "score": 70, "role": "company_candidate"},
        ])
        self.assertEqual(ranked[0]["url"], "https://brand.example")

    def test_identity_paths_include_owner_disclosure_pages(self):
        for path in ("/kvkk", "/gizlilik-politikasi", "/privacy-policy", "/legal"):
            self.assertIn(path, config.IDENTITY_PAGE_PATHS)

    def test_structured_brand_and_parent_relationships_are_extracted(self):
        html = '''
        <script type="application/ld+json">
        {"@type":"Organization","name":"Parent Holding",
         "brand":{"@type":"Brand","name":"Example Brand","url":"https://brand.example"},
         "parentOrganization":{"@type":"Organization","legalName":"Parent Holding A.S."}}
        </script>
        '''
        evidence = extractor.extract_organization_evidence(html)
        self.assertIn("Example Brand", evidence["brand_names"])
        self.assertIn("Parent Holding A.S.", evidence["related_organizations"])
        self.assertIn("https://brand.example", evidence["same_as"])

    def test_literal_nbsp_escape_is_not_part_of_email_local_name(self):
        html = (
            r'<a href="mailto:\u00a0info@example.com">Email</a> '
            r'\ninfo@example.com info@example.comt 2043004info@example.com'
        )
        self.assertEqual(extractor.extract_emails(html), ["info@example.com"])

    def test_profile_bridge_remains_neutral_identity_evidence(self):
        candidate = {
            "url": "https://alias.example", "query": "source_profile",
            "_source_profile_evidence": 1, "role": "company_candidate",
        }
        assessment = identity.assess(
            "Example Brand", candidate,
            ["page_identity_strong:2/2", "structured_identity_strong:2/2", "country_identity_tr_text"],
        )
        self.assertNotIn("authority", assessment["support_keys"])
        self.assertFalse(assessment["publishable"])

    def test_first_party_alias_link_enters_pool_without_becoming_authority(self):
        evaluation = {
            "crawl_result": {"url": "https://brand.com.tr"},
            "structured_identity": {
                "same_as": ["https://brandshop.com", "https://linkedin.com/company/brand"],
                "urls": ["https://brand.com.tr"],
            },
            "identity_assessment": {"support_keys": ["first_party_identity"], "conflicts": []},
        }
        aliases = main._first_party_alias_candidates("Brand", [evaluation], {"brand.com.tr"})
        self.assertEqual([item["domain"] for item in aliases], ["brandshop.com"])
        self.assertEqual(aliases[0]["query"], "first_party_alias")
        self.assertEqual(aliases[0]["_official_query_evidence"], 0)

    def test_conflicted_homonym_loses_to_clean_publishable_candidate(self):
        clean = {
            "candidate": {"url": "https://example.com.tr", "reason": "domain_hits:1/1", "role": "company_candidate"},
            "reasons": ["page_identity_strong:1/1", "structured_identity_strong:1/1"],
            "identity_assessment": {"conflicts": [], "provisionally_publishable": True},
            "structured_identity": {},
        }
        conflicted = {
            "candidate": {"url": "https://example.com", "reason": "domain_hits:1/1", "role": "company_candidate"},
            "reasons": ["page_identity_strong:1/1", "structured_identity_unmatched:0/1"],
            "identity_assessment": {"conflicts": [{"kind": "structured_owner_mismatch"}], "provisionally_publishable": False},
            "structured_identity": {},
        }
        result = main._homonym_conflict("Example", clean, conflicted)
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["reason"], "identity_conflict_resolution")

    def test_explicit_different_owner_qualifiers_block_short_brand_homonym(self):
        candidate = {
            "url": "https://example.com.tr", "query": "search",
            "reason": "domain_hits:1/1", "role": "company_candidate",
        }
        assessment = identity.assess(
            "Example Makine", candidate,
            [
                "page_identity_strong:1/1@scope=public_brand,pages=2",
                "structured_identity_strong:1/1@scope=public_brand",
                "email_domain_match", "country_identity_tr_tld",
            ],
            {"names": ["Example Ambalaj"]},
        )
        self.assertIn(
            "structured_owner_context_mismatch",
            {item["kind"] for item in assessment["conflicts"]},
        )
        self.assertFalse(assessment["publishable"])

    def test_missing_owner_qualifier_does_not_create_context_conflict(self):
        candidate = {
            "url": "https://example.com.tr", "query": "search",
            "reason": "domain_hits:1/1", "role": "company_candidate",
        }
        assessment = identity.assess(
            "Example Makine", candidate,
            [
                "page_identity_strong:1/1@scope=public_brand,pages=2",
                "structured_identity_strong:1/1@scope=public_brand",
                "email_domain_match", "country_identity_tr_tld",
            ],
            {"names": ["Example"]},
        )
        self.assertNotIn(
            "structured_owner_context_mismatch",
            {item["kind"] for item in assessment["conflicts"]},
        )

    def test_profile_plain_domain_needs_nearby_website_label_for_explicit_flag(self):
        search.reset_source_health()
        response = Mock(
            status_code=200,
            text=(
                "<div>Partner: unrelated.com</div><div>" + ("x" * 300)
                + "</div><div>Web Sitesi: official.com</div>"
            ),
            url="https://bridge-source.example/brand/x",
        )
        response._b2b_final_url = response.url
        with patch.object(config, "SEARCH_CACHE_MODE", "off"), patch(
            "modules.search.crawler._request_with_safe_redirects", return_value=response
        ):
            records = search._profile_external_websites(response.url)
        flags = {item["url"]: item["explicit_website"] for item in records}
        self.assertFalse(flags["https://unrelated.com"])
        self.assertTrue(flags["https://official.com"])


if __name__ == "__main__":
    unittest.main()
