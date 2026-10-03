import unittest
from unittest.mock import patch

import main
from modules import field_merge, reference_resolution, scorer


def _email(value, source_url, status="verified", **extra):
    return {
        "value": value,
        "label": "general",
        "source_url": source_url,
        "retrieval_method": "http",
        "verification_status": status,
        "verification_reason": "fixture",
        **extra,
    }


class SiblingBrandDomainTests(unittest.TestCase):
    def test_same_core_with_other_suffix_is_sibling(self):
        self.assertTrue(scorer.sibling_brand_domain("ornekfirma.com", "https://www.ornekfirma.com.tr/"))

    def test_leading_core_of_four_or_more_characters_is_sibling(self):
        self.assertTrue(scorer.sibling_brand_domain("ornek.com", "ornekmarin.com"))
        self.assertTrue(scorer.sibling_brand_domain("ornekmarin.com.tr", "ornek.com"))

    def test_short_core_unrelated_core_and_same_domain_are_not_sibling(self):
        self.assertFalse(scorer.sibling_brand_domain("abcgroup.com", "abc.com.tr"))
        self.assertFalse(scorer.sibling_brand_domain("baskafirma.com", "ornekfirma.com"))
        self.assertFalse(scorer.sibling_brand_domain("marinornek.com", "ornek.com"))
        self.assertFalse(scorer.sibling_brand_domain("ornekfirma.com", "www.ornekfirma.com"))

    def test_kep_and_public_body_domains_are_never_sibling(self):
        self.assertFalse(scorer.sibling_brand_domain("ornekfirma.hs01.kep.tr", "ornekfirma.com.tr"))
        self.assertFalse(scorer.sibling_brand_domain("ornekfirma.edu.tr", "ornekfirmalab.com"))


class ReferenceContactSelectionTests(unittest.TestCase):
    def _obs(self, emails):
        return {"emails": emails, "phones": [], "listed_email": ""}

    def test_sibling_mailbox_beats_free_mailbox(self):
        contacts = reference_resolution.select_contacts(
            self._obs(["kisi.ornek@gmail.com", "satis@ornek.com"]), "ornekmarin.com.tr", "",
        )
        self.assertEqual(contacts["email"], "satis@ornek.com")
        self.assertEqual(contacts["email_source_tier"], "SITE_SIBLING")

    def test_same_domain_mailbox_beats_sibling_mailbox(self):
        contacts = reference_resolution.select_contacts(
            self._obs(["info@ornek.com", "satis@ornekmarin.com.tr"]), "ornekmarin.com.tr", "",
        )
        self.assertEqual(contacts["email"], "satis@ornekmarin.com.tr")
        self.assertEqual(contacts["email_source_tier"], "SITE")

    def test_kep_mailbox_is_not_selected(self):
        contacts = reference_resolution.select_contacts(
            self._obs(["ornekmarin@hs01.kep.tr"]), "ornekmarin.com.tr", "",
        )
        self.assertEqual(contacts["email"], "")


class SiblingConfidenceTests(unittest.TestCase):
    def test_sibling_email_follows_website_confidence_up_to_medium(self):
        confident = {
            "website": "https://ornekmarin.com.tr", "website_source": "OWN_SEARCH+REFERENCE",
            "email": "info@ornek.com", "email_source_tier": "SITE_SIBLING",
        }
        weak = {**confident, "website_source": "REFERENCE_THIN"}
        self.assertEqual(field_merge.field_confidence(confident, "email"), "MEDIUM")
        self.assertEqual(field_merge.field_confidence(weak, "email"), "LOW")


class EmailCompletionTests(unittest.TestCase):
    def test_sibling_record_fills_empty_email_before_free_mailbox(self):
        row = {"website": "https://ornekmarin.com.tr", "email": ""}
        evaluation = {"email_completion_records": [
            _email("kisi.ornek@gmail.com", "https://ornekmarin.com.tr/iletisim"),
            _email("info@ornek.com", "https://ornekmarin.com.tr/iletisim"),
        ]}
        main._complete_email_from_official_site(row, evaluation)
        self.assertEqual(row["email"], "info@ornek.com")
        self.assertEqual(row["email_source_tier"], "SITE_SIBLING")
        self.assertEqual(row["email_source_url"], "https://ornekmarin.com.tr/iletisim")

    def test_free_mailbox_fills_when_no_sibling_exists(self):
        row = {"website": "https://ornekmarin.com.tr", "email": ""}
        evaluation = {"email_completion_records": [
            _email("baska@baskafirma.com", "https://ornekmarin.com.tr/iletisim"),
            _email("kisi.ornek@gmail.com", "https://ornekmarin.com.tr/iletisim"),
        ]}
        main._complete_email_from_official_site(row, evaluation)
        self.assertEqual(row["email"], "kisi.ornek@gmail.com")
        self.assertEqual(row["email_source_tier"], "SITE_FREEMAIL")

    def test_unrelated_record_leaves_email_empty(self):
        row = {"website": "https://ornekmarin.com.tr", "email": ""}
        main._complete_email_from_official_site(row, {"email_completion_records": [
            _email("baska@baskafirma.com", "https://ornekmarin.com.tr/iletisim"),
        ]})
        self.assertEqual(row["email"], "")
        self.assertNotIn("email_source_tier", row)

    def test_evaluation_keeps_sibling_out_of_selection_and_offers_it_for_completion(self):
        crawl_result = {
            "url": "https://ornekmarin.com.tr",
            "pages": [{
                "url": "https://ornekmarin.com.tr/iletisim",
                "html": "fixture",
                "retrieval_method": "http",
            }],
            "error": "",
        }
        records = {
            "emails": [
                _email("info@ornekmarin.com", "https://ornekmarin.com.tr/iletisim"),
                _email("info@baskafirma.com", "https://ornekmarin.com.tr/iletisim"),
            ],
            "phones": [],
        }
        with patch("main.crawler.fetch_site", return_value=crawl_result), patch(
            "main.extractor.extract_contact_records", return_value=records,
        ), patch(
            "main.email_verifier.verify_email",
            return_value={"status": "verified", "reason": "mx_present"},
        ), patch(
            "main._score_candidate_with_site", return_value=(85, []),
        ), patch(
            "main._structured_identity_score", return_value=(0, "", {}),
        ), patch(
            "main.identity.assess",
            return_value={
                "support_count": 2,
                "decision": "verified",
                "provisionally_publishable": True,
                "conflicts": [],
            },
        ):
            result = main._evaluate_candidate(
                "ORNEK MARIN",
                {
                    "url": "https://ornekmarin.com.tr",
                    "score": 80,
                    "reason": "domain_hits:1/1",
                },
            )
        self.assertEqual(result["email"], "")
        self.assertEqual(
            [record["value"] for record in result["email_completion_records"]],
            ["info@ornekmarin.com"],
        )


if __name__ == "__main__":
    unittest.main()
