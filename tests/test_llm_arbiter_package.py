import unittest
from unittest.mock import patch

import requests

import config
import main
from modules import entity_resolution, llm_arbiter, runtime


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result or {
            "verdict": "match",
            "reason": "Firma ve faaliyet alanı örtüşüyor.",
            "detected_sector": "Mutfak ekipmanları üretimi",
            "expected_sector": "Ev ve mutfak eşyaları",
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 18,
                "total_tokens": 138,
            },
        }
        self.error = error
        self.calls = []

    def generate(self, prompt, response_schema):
        self.calls.append((prompt, response_schema))
        if self.error:
            raise self.error
        return self.result


def _evaluation(url="https://alfabalik.com"):
    return {
        "candidate": {
            "url": url,
            "role": "company_candidate",
            "query": "search",
            "_identity_company": "ALFA",
            "_official_query_evidence": 2,
        },
        "crawl_result": {
            "url": url,
            "pages": [{
                "url": url,
                "html": "<html><title>Alfa</title><body>Alfa üretim iletişim</body></html>",
            }],
        },
        "reasons": [
            "page_identity_medium:1/1",
            "structured_identity_medium:1/1",
            "email_domain_match",
            "country_identity_tr_text",
            "metadata_context_conflict:ev_mutfak/gida",
        ],
        "structured_identity": {"names": ["Alfa"]},
        "identity_assessment": {
            "support_keys": ["first_party_identity"],
            "publishable": False,
            "provisionally_publishable": True,
            "support_count": 2,
            "conflicts": [{"kind": "context"}],
        },
        "email_source_url": "https://alfabalik.com/contact",
        "phone_source_url": "",
        "has_contact": True,
        "context_failed": True,
    }


class LlmArbiterPackageTests(unittest.TestCase):
    def setUp(self):
        runtime.reset()

    def test_mock_client_returns_structured_verdict_and_records_cost(self):
        client = FakeClient()
        with patch.object(config, "LLM_ARBITER_BUDGET", 5), patch.object(
            config, "GLOBAL_REQUESTS_PER_SECOND", 10000,
        ):
            result = llm_arbiter.arbitrate(
                "ALFA", "ALFA LTD", "Ev ve mutfak", "alfamutfak.com",
                "Alfa mutfak ekipmanları üretir.", client=client,
            )
        self.assertEqual(result["verdict"], "match")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            client.calls[0][1]["properties"]["verdict"]["enum"],
            ["match", "no_match", "uncertain"],
        )
        self.assertEqual(result["detected_sector"], "Mutfak ekipmanları üretimi")
        counters = runtime.snapshot()["counters"]
        self.assertEqual(counters["api.llm_arbiter.requests"], 1)
        self.assertEqual(counters["api.llm_arbiter.total_tokens"], 138)

    def test_provider_failure_is_fail_open_uncertain(self):
        client = FakeClient(error=requests.Timeout("timeout"))
        with patch.object(config, "LLM_ARBITER_BUDGET", 5), patch.object(
            config, "GLOBAL_REQUESTS_PER_SECOND", 10000,
        ):
            result = llm_arbiter.arbitrate(
                "ALFA", "ALFA LTD", "Ev ve mutfak", "alfa.com", "özet",
                client=client,
            )
        self.assertEqual(result["verdict"], "uncertain")
        self.assertIn("provider_failure", result["reason"])

    def test_missing_key_never_reserves_or_calls_api(self):
        with patch.object(config, "GROQ_API_KEY", ""), patch.object(
            config, "ENABLE_LLM_ARBITER", True,
        ):
            result = llm_arbiter.arbitrate(
                "ALFA", "ALFA LTD", "Ev ve mutfak", "alfa.com", "özet",
            )
        self.assertEqual(result["verdict"], "uncertain")
        self.assertEqual(
            runtime.snapshot()["counters"]["api.llm_arbiter.requests"], 0
        )

    def test_match_adds_independent_candidate_ready_route(self):
        evaluation = _evaluation()
        before = entity_resolution.resolve_candidates("ALFA", [evaluation])
        self.assertEqual(before.status, "unresolved")
        evaluation["llm_arbiter_evidence"] = {
            "verdict": "match", "reason": "Faaliyet ve unvan örtüşüyor."
        }
        after = entity_resolution.resolve_candidates("ALFA", [evaluation])
        self.assertEqual(after.status, "resolved")
        self.assertEqual(after.reason, "candidate_resolved_by_llm_arbiter_match")
        self.assertFalse(main._is_hard_context_failure(evaluation))

    def test_no_match_rejects_candidate_and_reason_is_auditable(self):
        evaluation = _evaluation()
        resolution = entity_resolution.resolve_candidates("ALFA", [evaluation])
        verdict = {
            "verdict": "no_match",
            "reason": "Site balıkçılık şirketini anlatıyor.",
            "model": "llama-3.3-70b-versatile",
        }
        with patch.object(llm_arbiter, "available", return_value=True), patch.object(
            llm_arbiter, "arbitrate", return_value=verdict,
        ) as arbitrate:
            after = main._try_llm_arbitration(
                "ALFA", {"sector": "Ev ve Mutfak Eşyaları"},
                [evaluation], resolution,
            )
        self.assertEqual(arbitrate.call_count, 1)
        self.assertEqual(after.status, "unresolved")
        self.assertTrue(evaluation["_llm_arbiter_rejected"])
        self.assertEqual(
            evaluation["llm_arbiter_evidence"]["triggers"],
            ["sector_context_conflict_with_identity_evidence"],
        )
        self.assertIn("balıkçılık", evaluation["reasons"][-1])
        self.assertEqual(
            main._evaluation_evidence(evaluation)["llm_arbiter_evidence"]["verdict"],
            "no_match",
        )

    def test_close_identity_margin_conflict_arbitrates_both_candidates(self):
        left = _evaluation("https://alfa-one.com")
        right = _evaluation("https://alfa-two.com")
        for evaluation in (left, right):
            evaluation["context_failed"] = False
            evaluation["reasons"] = [
                "page_identity_medium:1/1", "country_identity_tr_text"
            ]
            evaluation["identity_assessment"] = {
                "support_keys": ["first_party_identity"],
                "publishable": True,
                "provisionally_publishable": True,
                "support_count": 2,
                "conflicts": [],
            }
            evaluation["final_score"] = 90
        resolution = entity_resolution.resolve_candidates("ALFA", [left, right])
        with patch.object(llm_arbiter, "available", return_value=True), patch.object(
            llm_arbiter, "arbitrate", return_value={
                "verdict": "uncertain", "reason": "İki aday da benzer görünüyor."
            },
        ) as arbitrate:
            main._try_llm_arbitration(
                "ALFA", {"sector": "Genel üretim"}, [left, right], resolution,
            )
        self.assertEqual(arbitrate.call_count, 2)
        self.assertEqual(
            left["llm_arbiter_evidence"]["triggers"],
            ["close_identity_margin_conflict"],
        )

    def test_uncertain_leaves_existing_review_path_unchanged(self):
        evaluation = _evaluation()
        resolution = entity_resolution.resolve_candidates("ALFA", [evaluation])
        with patch.object(llm_arbiter, "available", return_value=True), patch.object(
            llm_arbiter, "arbitrate", return_value={
                "verdict": "uncertain", "reason": "Kanıt yetersiz."
            },
        ):
            after = main._try_llm_arbitration(
                "ALFA", {"sector": "Ev ve Mutfak Eşyaları"},
                [evaluation], resolution,
            )
        self.assertEqual(after.status, resolution.status)
        self.assertFalse(evaluation.get("_llm_arbiter_rejected", False))

    def test_generic_name_with_unobserved_sector_is_arbitrated(self):
        evaluation = _evaluation("https://arasmetal.com.tr")
        evaluation["context_failed"] = False
        evaluation["reasons"] = [
            "page_identity_strong:1/1",
            "email_domain_match",
            "country_identity_tr_tld",
            "metadata_context_not_observed:0/1",
        ]
        self.assertTrue(main._llm_context_conflict_candidate(
            "ARAS METAL", {"sector": "Ev ve Mutfak Eşyaları"}, evaluation,
        ))

    def test_explicit_keyword_match_still_triggers_generic_name_route(self):
        evaluation = _evaluation("https://ornekmutfak.com.tr")
        evaluation["context_failed"] = False
        evaluation["reasons"] = [
            "page_identity_strong:1/1",
            "email_domain_match",
            "country_identity_tr_tld",
            "metadata_context_not_observed:0/1",
            "context_match:1/1",
        ]
        self.assertTrue(main._llm_context_conflict_candidate(
            "ORNEK", {"sector": "Ev ve Mutfak Eşyaları"}, evaluation,
        ))

    def test_prompt_rejects_generic_ecommerce_text_as_sector_evidence(self):
        prompt = llm_arbiter._prompt(
            "AZRA GRUP", "AZRA GRUP", "Ev ve Mutfak Eşyaları",
            "servis.azragroup.com", "Sepet Mesafeli Satış Sözleşmesi Teslimat",
        )
        self.assertIn("e-ticaret ifadeleri sektör kanıtı değildir", prompt)
        self.assertIn("ürün/hizmet kategorisinden", prompt)

    def test_page_summary_is_bounded_and_excludes_scripts(self):
        summary = llm_arbiter.summarize_pages([{
            "html": "<script>ignore me</script><body>" + "ürün " * 500 + "</body>"
        }])
        self.assertLessEqual(len(summary.split()), 320)
        self.assertNotIn("ignore me", summary)


if __name__ == "__main__":
    unittest.main()
