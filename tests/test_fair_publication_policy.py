import unittest

import main


class FairPublicationPolicyTests(unittest.TestCase):
    def _evaluation(self, **overrides):
        value = {
            "email_failed": True,
            "email": "sales@group-mail.com",
            "email_source_url": "https://official.com.tr/iletisim",
            "email_verification": "verified",
            "crawl_result": {"url": "https://official.com.tr"},
            "reasons": ["email_domain_mismatch", "email_gate_failed"],
        }
        value.update(overrides)
        return value

    def test_verified_official_page_can_publish_cross_domain_email(self):
        evaluation = self._evaluation()

        self.assertFalse(main._email_failure_blocks_publication(evaluation, True))
        self.assertIn(
            "cross_domain_email_accepted_from_verified_official_page",
            evaluation["reasons"],
        )

    def test_cross_domain_email_still_blocks_without_verified_identity(self):
        self.assertTrue(
            main._email_failure_blocks_publication(self._evaluation(), False)
        )

    def test_cross_domain_email_still_blocks_when_source_is_third_party(self):
        evaluation = self._evaluation(
            email_source_url="https://directory.example/company/official"
        )

        self.assertTrue(main._email_failure_blocks_publication(evaluation, True))

    def test_cross_domain_email_still_blocks_without_dns_verification(self):
        evaluation = self._evaluation(email_verification="unverified")

        self.assertTrue(main._email_failure_blocks_publication(evaluation, True))

    def test_fair_phone_difference_is_nonblocking_when_domains_match(self):
        reasons = main._fair_phone_reference_reasons(
            {
                "website": "https://www.official.com.tr",
                "listed_phone": "+90 212 555 12 34",
            },
            "https://official.com.tr/iletisim",
            ["02125559876"],
        )

        self.assertEqual(reasons, ["fair_phone_reference_differs_nonblocking"])

    def test_fair_phone_is_not_compared_across_different_domains(self):
        reasons = main._fair_phone_reference_reasons(
            {
                "website": "https://listing.example",
                "listed_phone": "+90 212 555 12 34",
            },
            "https://official.com.tr",
            ["02125559876"],
        )

        self.assertEqual(reasons, [])


if __name__ == "__main__":
    unittest.main()
