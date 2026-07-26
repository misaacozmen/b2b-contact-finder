import unittest
from unittest.mock import patch

import main
from modules import contact_publication, identity


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


def _phone(value, source_url, **extra):
    return {
        "value": value,
        "label": "general",
        "source_url": source_url,
        "retrieval_method": "http",
        **extra,
    }


class FieldPublicationPolicyTests(unittest.TestCase):
    def test_same_domain_email_survives_temporary_dns_uncertainty(self):
        decision = contact_publication.evaluate_email(
            "https://official.example",
            _email(
                "sales@official.example",
                "https://official.example/contact",
                "unverified",
            ),
        )
        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["reason"], "verified_first_party_source")

    def test_cross_domain_email_requires_dns_but_stays_allowed_when_verified(self):
        source = "https://official.example/contact"
        blocked = contact_publication.evaluate_email(
            "https://official.example",
            _email("sales@mail-provider.example", source, "unverified"),
        )
        allowed = contact_publication.evaluate_email(
            "https://official.example",
            _email("sales@mail-provider.example", source, "verified"),
        )
        self.assertFalse(blocked["eligible"])
        self.assertIn("cross_domain_email_dns_unverified", blocked["reason"])
        self.assertTrue(allowed["eligible"])

    def test_external_source_is_suppressed_per_field(self):
        result = contact_publication.filter_records(
            "https://official.example",
            [_email(
                "sales@official.example",
                "https://directory.example/profile",
            )],
            [_phone("02125550000", "https://official.example/contact")],
        )
        self.assertEqual(result["eligible_email_records"], [])
        self.assertEqual(
            [item["value"] for item in result["eligible_phone_records"]],
            ["02125550000"],
        )

    def test_verified_official_family_source_is_explicitly_audited(self):
        decision = contact_publication.evaluate_phone(
            "https://brand.example",
            _phone(
                "02125550000",
                "https://turkey-entity.example/contact",
                official_family_verified=True,
            ),
        )
        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["reason"], "verified_official_family_source")


class FieldPolicyIntegrationTests(unittest.TestCase):
    def test_one_bad_field_does_not_remove_safe_phone(self):
        crawl_result = {
            "url": "https://official.example",
            "pages": [{
                "url": "https://official.example/contact",
                "html": "fixture",
                "retrieval_method": "http",
            }],
            "error": "",
        }
        records = {
            "emails": [_email(
                "info@official.example",
                "https://directory.example/profile",
            )],
            "phones": [_phone(
                "0212 555 00 00",
                "https://official.example/contact",
            )],
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
                "OFFICIAL MAKINE",
                {
                    "url": "https://official.example",
                    "score": 80,
                    "reason": "domain_hits:1/1",
                },
            )
        self.assertEqual(result["email"], "")
        self.assertEqual(result["email_publication_status"], "suppressed")
        self.assertEqual(result["phone"], "02125550000")
        self.assertEqual(result["phone_publication_status"], "allowed")
        self.assertTrue(result["has_contact"])

    def test_contact_output_keeps_source_url_for_every_alternative(self):
        fields = main._contact_output_fields({
            "alternative_emails": ["sales@official.example"],
            "alternative_email_records": [{
                "value": "sales@official.example",
                "source_url": "https://official.example/contact",
            }],
            "alternative_phones": [{
                "value": "02125550000",
                "label": "general",
                "source_url": "https://official.example/locations",
            }],
            "contact_publication": {
                "policy_version": contact_publication.POLICY_VERSION,
            },
        })
        self.assertIn(
            "sales@official.example | https://official.example/contact",
            fields["alternative_email_sources"],
        )
        self.assertIn(
            "02125550000 | https://official.example/locations",
            fields["alternative_phone_sources"],
        )

    def test_family_merge_does_not_repeat_promoted_primary_contacts(self):
        primary = {
            "candidate": {"url": "https://brand.example"},
            "email": "",
            "phone": "",
            "alternative_emails": [],
            "alternative_email_records": [],
            "alternative_phones": [],
            "reasons": [],
        }
        related = [{
            "candidate": {"url": "https://entity.example"},
            "email": "info@entity.example",
            "email_source_url": "https://entity.example/contact",
            "email_publication_status": "allowed",
            "phone": "02125550000",
            "phone_source_url": "https://entity.example/contact",
            "phone_publication_status": "allowed",
            "alternative_emails": [],
            "alternative_email_records": [],
            "alternative_phones": [],
            "reasons": ["legal_name_match"],
        }]
        with patch("main._same_official_family", return_value=True), patch(
            "main._has_trusted_website_evidence", return_value=True,
        ):
            main._merge_official_family_contacts(primary, related)
        self.assertEqual(primary["email"], "info@entity.example")
        self.assertEqual(primary["phone"], "02125550000")
        self.assertEqual(primary["alternative_emails"], [])
        self.assertEqual(primary["alternative_email_records"], [])
        self.assertEqual(primary["alternative_phones"], [])


class PartialIdentityPrecisionTests(unittest.TestCase):
    def test_unanchored_partial_context_needs_third_first_party_component(self):
        assessment = identity.assess(
            "MAVI DUNYA MAKINE",
            {
                "url": "https://shop.example",
                "role": "company_candidate",
                "reason": "search_text_identity:2/3",
            },
            [
                "page_identity_strong:2/3",
                "structured_identity_medium:2/3",
                "context_match:1/2",
                "country_identity_tr_phone",
            ],
            {"names": ["Mavi Dunya Store"]},
        )
        self.assertEqual(assessment["first_party_bundle_components"], 2)
        self.assertEqual(assessment["required_first_party_bundle_components"], 3)
        self.assertTrue(assessment["partial_activity_identity"])
        self.assertFalse(assessment["provisionally_publishable"])

    def test_legal_anchor_resolves_partial_public_brand_context(self):
        assessment = identity.assess(
            "MAVI DUNYA MAKINE",
            {
                "url": "https://shop.example",
                "role": "company_candidate",
                "reason": "search_text_identity:2/3",
            },
            [
                "page_identity_strong:2/3",
                "structured_identity_medium:2/3",
                "legal_name_ownership_match:4",
                "context_match:1/2",
                "country_identity_tr_phone",
            ],
            {"names": ["Mavi Dunya Store"]},
        )
        self.assertTrue(assessment["provisionally_publishable"])


if __name__ == "__main__":
    unittest.main()
