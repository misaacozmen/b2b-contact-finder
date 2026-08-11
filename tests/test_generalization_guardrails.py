import unittest
from unittest.mock import Mock, patch

import main
from modules import scorer, search


class GeneralizationGuardrailTests(unittest.TestCase):
    def test_marketplace_subdomain_is_never_an_official_company_domain(self):
        self.assertTrue(scorer.is_excluded_domain("29981-tr.all.biz"))
        self.assertTrue(scorer.is_excluded_domain("www.alibaba.com.tr"))

    def test_company_shaped_mirror_subdomain_is_rejected(self):
        self.assertTrue(scorer.is_mirror_directory_domain(
            "BIRLESIK TEMIZLIK", "birlesiktemizlik.com.siteindices.com",
        ))
        self.assertFalse(scorer.is_mirror_directory_domain(
            "BASKA FIRMA", "birlesiktemizlik.com.siteindices.com",
        ))
        self.assertFalse(scorer.is_mirror_directory_domain(
            "BIRLESIK TEMIZLIK", "birlesiktemizlik.com",
        ))

    def test_search_drops_company_shaped_mirror_before_scoring(self):
        candidates = {}
        search._add_search_results(
            candidates,
            "BIRLESIK TEMIZLIK",
            '"BIRLESIK TEMIZLIK" official website',
            [{
                "href": "https://birlesiktemizlik.com.siteindices.com/",
                "title": "Birlesik Temizlik",
                "body": "Birlesik Temizlik resmi web sitesi",
            }],
            {"sector": "Temizlik"},
        )
        self.assertEqual(candidates, {})

    def test_legal_name_phrase_requires_words_together(self):
        company = "ARPACK MAKİNE SANAYİ VE TİCARET AŞ"
        self.assertTrue(scorer.legal_name_phrase_match(company, "Arpack Makine iletişim bilgileri"))
        self.assertFalse(scorer.legal_name_phrase_match(company, "Arpack Ambalaj için çeşitli makine ürünleri"))

    def test_structured_owner_mismatch_overrides_search_query_and_contact(self):
        candidate = {
            "url": "https://retailer.example",
            "query": "brand Turkiye official website",
            "role": "unknown",
            "_official_query_evidence": 4,
            "reason": "search_text_identity:1/2",
        }
        reasons = [
            "page_identity_strong:2/2",
            "email_domain_match",
            "structured_identity_unmatched:0/2",
            "legal_name_phrase_missing:0/2",
        ]
        self.assertFalse(main._has_trusted_website_evidence(candidate, reasons))

    def test_search_legal_name_and_same_site_name_are_not_independent(self):
        candidate = {
            "url": "https://publicbrand.example",
            "query": "legal company Turkiye official website",
            "role": "unknown",
            "_official_query_evidence": 1,
            "reason": "search_legal_name_identity:2",
        }
        reasons = [
            "page_identity_medium:1/2",
            "structured_identity_absent",
            "legal_name_phrase_match:2",
        ]
        self.assertFalse(main._has_trusted_website_evidence(candidate, reasons))

    def test_exact_brand_needs_multiple_page_hits_and_full_context(self):
        candidate = {
            "url": "https://brand.com.tr", "query": "brand official website",
            "role": "company_candidate", "_exact_brand_domain": True,
        }
        self.assertTrue(main._has_trusted_website_evidence(candidate, [
            "page_identity_medium:2/3", "context_match:2/2", "structured_identity_absent",
        ]))
        self.assertFalse(main._has_trusted_website_evidence(candidate, [
            "page_identity_strong:1/1", "context_match:1/2", "structured_identity_strong:1/1",
        ]))

    def test_foreign_cross_domain_redirect_is_rejected_for_tr_only_search(self):
        crawl = {"url": "https://brand.com", "pages": [], "error": "", "redirect_target": "https://other.es/"}
        candidate = {
            "url": "https://brand.com", "score": 90, "query": "brand official website",
            "reason": "domain_hits:1/1", "role": "company_candidate",
        }
        with patch("main.crawler.fetch_site", return_value=crawl):
            evaluation = main._evaluate_candidate("BRAND MAKİNA", candidate, {"sector": "Machinery"})
        self.assertEqual(evaluation["final_score"], 0)
        self.assertIn("foreign_country_redirect_rejected", evaluation["reasons"][0])

    def test_non_tr_search_candidate_needs_a_turkish_footprint(self):
        candidate = {
            "url": "https://brand.com", "query": "brand Turkiye official website",
            "role": "company_candidate", "_exact_brand_domain": True,
            "_official_query_evidence": 3,
        }
        reasons = [
            "page_identity_strong:2/2", "context_match:1/1",
            "legal_name_phrase_match:2", "country_identity_unproven",
        ]
        self.assertFalse(main._has_trusted_website_evidence(candidate, reasons))

    def test_unverified_review_candidate_is_not_published_in_primary_contacts(self):
        candidate = {
            "url": "https://retailer.example", "score": 86,
            "query": "brand Turkiye official website", "reason": "search_text_identity:1/2",
            "role": "unknown", "_official_query_evidence": 3,
        }
        evaluation = {
            "candidate": candidate,
            "crawl_result": {"url": candidate["url"], "pages": [{"url": candidate["url"], "html": "Brand products"}]},
            "email": "info@retailer.example", "email_source": "website", "email_source_url": candidate["url"],
            "alternative_emails": [], "email_verification": "verified", "email_verification_reason": "mx_present",
            "phone": "02125550000", "phone_source": "website", "phone_source_url": candidate["url"],
            "phone_label": "general", "alternative_phones": [],
            "final_score": 100,
            "reasons": ["page_identity_strong:2/2", "structured_identity_unmatched:0/2", "legal_name_phrase_missing:0/2"],
            "has_contact": True, "context_failed": False, "email_failed": False, "structured_identity": {},
        }
        candidates = [candidate]
        with patch("main.search.find_candidate_domains", return_value=candidates), patch(
            "main._evaluate_candidate", return_value=evaluation
        ), patch("main.random_delay"):
            _, row = main.process_company(0, "BRAND MAKİNA", Mock())
        self.assertEqual(row["website"], "")
        self.assertEqual(row["email"], "")
        self.assertEqual(row["phone"], "")
        self.assertEqual(row["candidate_1_url"], candidate["url"])

    def test_search_legal_name_evidence_outranks_loose_brand_product_page(self):
        candidates = {}
        search._add_search_results(
            candidates,
            "OXY TEMİZLİK ÜRÜNLERİ VE KOZMETİK SANAYİ TİCARET AŞ",
            "oxy temizlik Turkiye official website",
            [
                {"href": "https://retailer.example/oxy", "title": "Oxy Temizlik Ürünleri", "body": "Satın alın"},
                {"href": "https://corporate.example/about", "title": "Sektör Kimya", "body": "OXY Temizlik Ürünleri ve Kozmetik Sanayi Ticaret AŞ"},
            ],
            {"sector": "Temizlik ve hijyen ürünleri"},
        )
        ranked = sorted(candidates.values(), key=search._candidate_rank_key, reverse=True)
        self.assertEqual(ranked[0]["domain"], "corporate.example")
        self.assertEqual(ranked[0]["_legal_name_evidence"], 1)

    def test_single_brand_sector_homonym_cannot_end_search_early(self):
        candidate = {
            "domain": "arpack.com.tr", "query": "arpack Turkiye official website",
            "_official_query_evidence": 1, "_metadata_context_matches": 1,
            "_exact_brand_domain": True,
        }
        self.assertFalse(search._can_early_stop(
            "ARPACK MAKİNE", candidate, {"sector": "Packaging Machinery"}
        ))


if __name__ == "__main__":
    unittest.main()
