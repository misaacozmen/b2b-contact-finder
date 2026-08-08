import unittest
from unittest.mock import patch

import main
from modules import crawler, extractor, phone, scorer


def _evaluation(url, reasons, **overrides):
    item = {
        "candidate": {"url": url, "query": "brand Turkiye official website", "_official_query_evidence": 1},
        "reasons": reasons,
        "context_failed": False,
        "email_failed": False,
        "has_contact": True,
        "final_score": 100,
        "email": "",
        "phone": "",
        "structured_identity": {},
    }
    item.update(overrides)
    return item


class NextAccuracyPackageTests(unittest.TestCase):
    def test_crawled_identity_and_context_outrank_search_snippet_metadata(self):
        correct = _evaluation(
            "https://brand.com.tr",
            ["page_identity_strong:2/2", "context_match:2/2", "structured_identity_strong:2/2"],
        )
        noisy = _evaluation(
            "https://brandgroup.net",
            ["page_identity_strong:2/2", "context_match:1/2", "structured_identity_absent"],
        )
        noisy["candidate"]["_official_query_evidence"] = 4
        noisy["candidate"]["_metadata_context_matches"] = 3
        ranked = sorted([noisy, correct], key=lambda item: main._evaluation_rank_key("Brand", item), reverse=True)
        self.assertIs(ranked[0], correct)

    def test_real_page_brand_hit_outranks_directory_structured_noise(self):
        official = _evaluation(
            "https://brand.com.tr",
            ["page_identity_weak:1/3", "context_match:2/2", "structured_identity_weak:1/3"],
        )
        directory = _evaluation(
            "https://tr.pinterest.com",
            ["page_identity_missing:0/3", "context_match:1/2", "structured_identity_medium:1/3"],
        )
        ranked = sorted([directory, official], key=lambda item: main._evaluation_rank_key("Brand Food", item), reverse=True)
        self.assertIs(ranked[0], official)
        self.assertTrue(scorer.is_excluded_domain("tr.pinterest.com"))
        self.assertTrue(scorer.is_excluded_domain("www.yelp.com"))

    def test_intrinsic_company_candidate_outranks_chamber_company_listing(self):
        official = _evaluation(
            "https://brand.store",
            ["page_identity_weak:1/3", "context_missing:0/1", "structured_identity_absent"],
        )
        official["candidate"]["role"] = "company_candidate"
        listing = _evaluation(
            "https://www.ito.org.tr",
            ["page_identity_medium:2/3", "context_missing:0/1", "structured_identity_absent"],
        )
        listing["candidate"]["role"] = "unknown"
        ranked = sorted([listing, official], key=lambda item: main._evaluation_rank_key("Brand", item), reverse=True)
        self.assertIs(ranked[0], official)
        self.assertTrue(scorer.is_excluded_domain("www.ito.org.tr"))

    def test_irregularly_grouped_turkish_phone_is_extracted_and_normalized(self):
        values = extractor.extract_phones("Telefon: 0 216 669 0 555")
        self.assertIn("02166690555", [phone.normalize_phone(value) for value in values])

    def test_whatsapp_link_on_official_page_is_a_phone_source(self):
        html = '<a href="https://wa.me/905334778560">WhatsApp</a>'
        records = extractor.extract_contact_records(html, "https://official.example/contact")
        normalized = [phone.normalize_phone(item["value"]) for item in records["phones"]]
        self.assertIn("05334778560", normalized)
        record = next(item for item in records["phones"] if phone.normalize_phone(item["value"]) == "05334778560")
        self.assertEqual(record["label"], "whatsapp")

    def test_shopify_contact_path_is_part_of_default_crawl(self):
        self.assertIn("/pages/contact", __import__("config").CONTACT_PAGE_PATHS)

    def test_bilingual_brand_token_can_match_turkish_domain(self):
        self.assertTrue(scorer.domain_identity_match("Adel Chocolate & Candy", "adelcikolata.com")[0])

    def test_unsupported_search_text_result_is_not_published(self):
        evaluation = _evaluation(
            "https://unrelated-retailer.com.tr",
            ["page_identity_missing:0/2", "context_match:1/1", "structured_identity_unmatched:0/2"],
        )
        evaluation["candidate"]["role"] = "unknown"
        self.assertTrue(main._unsupported_search_text_candidate("Example Brand", evaluation))
        evaluation["_identity_resolution"] = "candidate_resolved_by_target_fingerprint"
        self.assertFalse(main._unsupported_search_text_candidate("Example Brand", evaluation))

    def test_trusted_low_score_site_is_preserved_for_review(self):
        reasons = []
        self.assertEqual(main._confidence_status(48, True, reasons, True), ("REVIEW_NEEDED", "review"))
        self.assertIn("trusted_website_below_score_preserved_for_review", reasons)

    def test_profile_brand_domain_stays_unpublished_when_unreachable(self):
        unrelated = {"url": "https://parenttextile.com.tr", "query": "source_profile", "score": 92, "_source_profile_evidence": 1}
        brand = {"url": "https://brandcollection.com.tr", "query": "source_profile", "score": 92, "_source_profile_evidence": 1}
        self.assertIsNone(main._authoritative_unreachable_candidate([unrelated, brand], "Brand"))

    def test_first_party_cross_domain_email_and_shared_phone_join_official_family(self):
        corporate = _evaluation(
            "https://brand.com.tr",
            ["page_identity_strong:2/2", "structured_identity_strong:2/2", "legal_name_phrase_match:1"],
            email="info@brand.com.tr",
            phone="02125550000",
        )
        shop = _evaluation(
            "https://brandshop.com",
            ["page_identity_strong:2/2", "structured_identity_strong:2/2", "legal_name_phrase_match:1"],
            email="sales@brand.com.tr",
            phone="02125550000",
        )
        self.assertTrue(main._same_official_family(corporate, shop, "Brand"))

    def test_nested_corporate_page_can_reveal_contact_page(self):
        pages = {
            "https://example.com": '<a href="/about">About</a>',
            "https://example.com/about": '<a href="/reach-us">Contact</a>',
            "https://example.com/reach-us": "info@example.com",
        }

        def fake_fetch(url):
            return (pages[url], None) if url in pages else (None, "http_404")

        with patch("modules.crawler._try_fetch", side_effect=fake_fetch), patch(
            "modules.crawler._robots_and_sitemaps", return_value=(None, [])
        ):
            result = crawler._fetch_site_live("https://example.com")
        self.assertIn("https://example.com/reach-us", {page["url"] for page in result["pages"]})


if __name__ == "__main__":
    unittest.main()
