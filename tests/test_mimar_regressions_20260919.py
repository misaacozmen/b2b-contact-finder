"""Offline regressions for the 2026-09-19 architecture changes."""

from __future__ import annotations

import unittest

from modules import company_resolvers, discovery_coverage, discovery_rules, entity_resolution, publication_policy


class MimarRegressionTests(unittest.TestCase):
    def test_canonical_input_identity_preserves_source_fields_and_brand_boundaries(self):
        record = {
            "source_record_id": "aymod:17",
            "company": "Alpha / Beta & Co.",
            "listed_legal_name": "Alpha Beta Sanayi A.S.",
            "brands": "Alpha / Beta & Co.",
            "country": "Türkiye",
            "sector": "Footwear",
            "city": "Istanbul",
            "listed_email": "info@example.test",
            "source_evidence": "row:17",
        }
        identity = entity_resolution.canonical_target_identity(record)
        self.assertEqual(identity["source_record_id"], "aymod:17")
        self.assertEqual(identity["legal_name"], "Alpha Beta Sanayi A.S.")
        self.assertEqual(identity["brands"], ["Alpha", "Beta & Co"])
        self.assertEqual(identity["city"], "Istanbul")
        self.assertEqual(identity["source_evidence"], ["row:17"])

    def test_serp_normalization_unwraps_only_explicit_wrappers(self):
        values, counts = discovery_rules.normalize_serp_results([
            {"href": "https://www.google.com/url?q=https%253A%252F%252Facme.test%252Fcontact", "title": "ACME"},
            {"href": "https://www.google.com/url?q=CAESIopaque-token", "title": "Opaque"},
        ], query_id="q-1")
        self.assertEqual(values[0]["resolved_url"], "https://acme.test/contact")
        self.assertEqual(values[0]["resolution_method"], "wrapper_unwrapped")
        self.assertEqual(values[1]["resolution_status"], "unresolved")
        self.assertEqual(counts["resolved_result_count"], 1)
        self.assertEqual(counts["unresolved_redirect_count"], 1)

    def test_serp_all_opaque_results_are_typed_unusable(self):
        values, counts = discovery_rules.normalize_serp_results([
            {"href": "https://www.google.com/goto?url=CAESopaque"},
        ])
        self.assertEqual(counts["result_state"], "UNUSABLE_RESULTS")
        self.assertEqual(values[0]["resolution_method"], "wrapper_opaque")

    def test_content_decision_is_scheduler_independent(self):
        independent_id = "a" * 64
        candidate_id = "b" * 64
        relation_id = "c" * 64
        country_id = "d" * 64
        evidence = {
            "company": "ACME",
            "source_record_id": "fixture:acme",
            "website": "https://acme.test",
            "candidate": {"url": "https://acme.test"},
            "identity_assessment": {"publishable": True, "conflicts": []},
            "identity_verified": True,
            "identity_route": "TARGET_ANCHOR",
            "country_supported": True,
            "country_evidence_ids": [country_id],
            "evidence_records": [{
                "evidence_id": independent_id,
                "target_source_record_id": "fixture:acme",
                "evidence_source_record_id": "fixture:fair",
                "observed_business": "ACME",
                "url": "https://fair.test/acme",
                "final_url": "https://fair.test/acme",
                "content_sha256": "a" * 64,
                "retrieval_method": "official_link_reference",
                "observation_type": "phone",
                "observation_value": "+902120000000",
                "location": {"kind": "source_record", "field": "listed_phone"},
                "relation": "target_anchor",
                "identity_route": "TARGET_ANCHOR",
                "match_kind": "phone",
                "matched": True,
                "name_supported": True,
                "role": "independent_observation",
                "independent_source_evidence_id": independent_id,
                "candidate_evidence_id": candidate_id,
                "relation_evidence_id": relation_id,
            }, {
                "evidence_id": candidate_id,
                "target_source_record_id": "fixture:acme",
                "evidence_source_record_id": "fixture:page",
                "observed_business": "ACME",
                "url": "https://acme.test/contact",
                "final_url": "https://acme.test/contact",
                "content_sha256": "b" * 64,
                "retrieval_method": "http",
                "observation_type": "phone",
                "observation_value": "+902120000000",
                "location": {"kind": "page_text", "selector": "body"},
                "relation": "target_anchor",
                "identity_route": "TARGET_ANCHOR",
                "first_party": True,
                "role": "candidate_first_party_observation",
                "name_supported": True,
                "anchor_observation_type": "phone",
                "anchor_observation_value": "+902120000000",
                "contact_fields": ["email"],
                "observed_contacts": {"email": "info@acme.test"},
            }, {
                "evidence_id": relation_id,
                "target_source_record_id": "fixture:acme",
                "evidence_source_record_id": "fixture:relation",
                "observed_business": "ACME",
                "url": "https://acme.test/contact",
                "final_url": "https://acme.test/contact",
                "content_sha256": "c" * 64,
                "retrieval_method": "http",
                "observation_type": "target_anchor_relation",
                "observation_value": "+902120000000",
                "location": {"kind": "explicit_relation", "source": independent_id, "candidate": candidate_id},
                "relation": "target_anchor",
                "identity_route": "TARGET_ANCHOR",
                "match_kind": "phone",
                "matched": True,
                "name_supported": True,
                "role": "anchor_relation",
                "independent_source_evidence_id": independent_id,
                "candidate_evidence_id": candidate_id,
                "relation_evidence_id": relation_id,
                "first_party": True,
            }, {
                "evidence_id": country_id,
                "target_source_record_id": "fixture:acme",
                "evidence_source_record_id": "fixture:country",
                "observed_business": "ACME",
                "url": "https://acme.test/contact",
                "final_url": "https://acme.test/contact",
                "content_sha256": "d" * 64,
                "retrieval_method": "http",
                "observation_type": "country",
                "observation_value": "TR",
                "location": {"kind": "page_text", "selector": "body"},
                "relation": "country",
            }],
            "email": "info@acme.test",
            "email_publication_status": "allowed",
            "support_evidence_ids": [independent_id, candidate_id, relation_id],
        }
        content = publication_policy.evaluate_content(evidence)
        pending = publication_policy.finalize_publication(content, {
            "free_state": "PENDING", "paid_required": False, "paid_state": "NOT_REQUIRED",
        })
        done = publication_policy.finalize_publication(content, {
            "free_state": "DONE", "paid_required": False, "paid_state": "NOT_REQUIRED",
        })
        self.assertFalse(pending.publishable)
        self.assertTrue(done.publishable)
        self.assertEqual(pending.content_decision, done.content_decision)

    def test_single_token_content_requires_target_anchor(self):
        evidence = {
            "company": "ACME", "website": "https://acme.test",
            "identity_assessment": {"publishable": True, "conflicts": []},
            "identity_verified": True, "email": "info@acme.test",
            "email_publication_status": "allowed",
        }
        decision = publication_policy.evaluate_content(evidence)
        self.assertFalse(decision.website_allowed)
        self.assertIn("single_token_target_anchor_missing", decision.reason_codes)

    def test_resolver_skips_one_bad_row_without_poisoning_valid_rows(self):
        rows = company_resolvers._clean_results([
            {"domain": {"bad": True}, "name": "ACME"},
            {"domain": "acme.test", "name": "ACME", "claimed": False},
        ], "brandfetch")
        self.assertEqual([row["domain"] for row in rows], ["acme.test"])

    def test_coverage_acquisition_plan_is_source_record_scoped(self):
        discovery_coverage.reset()
        discovery_coverage.record_query(
            "Same Name", '"Same Name" contact', "primary", "replay_miss", 0,
            source_record_id="source:2", original_index=2,
        )
        discovery_coverage.finalize_company(
            "Same Name", resolved=False, candidate_count=0,
            source_record_id="source:2", original_index=2,
            terminal_reason="SEARCH_EXHAUSTED",
        )
        plan = discovery_coverage.payload()["acquisition_plan"]
        self.assertEqual(plan[0]["source_record_id"], "source:2")


if __name__ == "__main__":
    unittest.main()
