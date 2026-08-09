import unittest
from unittest.mock import Mock, patch

import config
import main
from modules import entity_resolution, linkedin_company, runtime


def _evaluation(url: str = "https://ornek.com.tr") -> dict:
    return {
        "candidate": {"url": url, "role": "company_candidate"},
        "crawl_result": {
            "url": url,
            "pages": [{"url": url, "html": "first-party"}],
        },
        "reasons": ["page_identity_weak:1/2", "country_identity_tr_tld"],
        "structured_identity": {},
        "identity_assessment": {
            "support_keys": [],
            "publishable": False,
            "provisionally_publishable": False,
            "conflicts": [],
        },
        "email_source_url": "",
        "phone_source_url": "",
        "has_contact": False,
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.text = ""

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class LinkedinCompanyPackageTests(unittest.TestCase):
    def setUp(self):
        runtime.reset()
        linkedin_company.reset()

    def test_verified_linkedin_website_is_an_additive_resolution_route(self):
        evaluation = _evaluation()
        before = entity_resolution.resolve_candidates("ORNEK GIDA", [evaluation])
        self.assertEqual(before.status, "unresolved")
        evaluation["linkedin_company_evidence"] = {
            "verified": True,
            "website": "https://www.ornek.com.tr",
        }
        after = entity_resolution.resolve_candidates("ORNEK GIDA", [evaluation])
        self.assertEqual(after.status, "resolved")
        self.assertEqual(
            after.reason, "candidate_resolved_by_linkedin_website_match"
        )

    def test_declared_linkedin_url_needs_only_one_separate_budget_request(self):
        evaluation = _evaluation()
        evaluation["structured_identity"] = {
            "same_as": ["https://linkedin.com/company/ornek-gida"]
        }
        response = FakeResponse([{
            "name": "Ornek Gida",
            "website": "https://ornek.com.tr/about",
            "industries": "Food",
        }])
        with patch.object(config, "ENABLE_LINKEDIN_COMPANY_LOOKUP", True), patch.object(
            config, "BRIGHTDATA_API_KEY", "secret"
        ), patch.object(config, "LINKEDIN_COMPANY_REQUEST_BUDGET", 3), patch(
            "modules.linkedin_company.requests.post", return_value=response
        ) as post:
            evidence = linkedin_company.corroborate("ORNEK GIDA", evaluation)
        self.assertTrue(evidence["verified"])
        self.assertEqual(post.call_count, 1)
        counters = runtime.snapshot()["counters"]
        self.assertEqual(counters["api.linkedin_company.requests"], 1)
        self.assertNotIn("api.brightdata.requests", {
            key: value for key, value in counters.items() if value
        })

    def test_replay_mode_never_calls_linkedin_api(self):
        with patch.object(config, "ENABLE_LINKEDIN_COMPANY_LOOKUP", True), patch.object(
            config, "BRIGHTDATA_API_KEY", "secret"
        ), patch.object(config, "SEARCH_CACHE_MODE", "replay"), patch(
            "modules.linkedin_company.requests.post"
        ) as post:
            evidence = linkedin_company.corroborate("ORNEK GIDA", _evaluation())
        self.assertIsNone(evidence)
        post.assert_not_called()

    def test_serp_discovery_and_scrape_use_linkedin_budget(self):
        evaluation = _evaluation()
        responses = [
            FakeResponse({"organic": [{
                "link": "https://www.linkedin.com/company/ornek-gida/",
                "title": "Ornek Gida | LinkedIn",
            }]}),
            FakeResponse([{
                "name": "Ornek Gida",
                "website": "https://ornek.com.tr",
            }]),
        ]
        with patch.object(config, "ENABLE_LINKEDIN_COMPANY_LOOKUP", True), patch.object(
            config, "BRIGHTDATA_API_KEY", "secret"
        ), patch.object(config, "LINKEDIN_COMPANY_REQUEST_BUDGET", 3), patch(
            "modules.linkedin_company.requests.post", side_effect=responses
        ):
            evidence = linkedin_company.corroborate("ORNEK GIDA", evaluation)
        self.assertTrue(evidence["verified"])
        counters = runtime.snapshot()["counters"]
        self.assertEqual(counters["api.linkedin_company.requests"], 2)
        self.assertEqual(counters["api.linkedin_company.serp_requests"], 1)
        self.assertEqual(counters["api.linkedin_company.scrape_requests"], 1)

    def test_serp_query_does_not_require_domain_in_indexed_snippet(self):
        evaluation = _evaluation()
        responses = [
            FakeResponse({"organic": [{
                "link": "https://linkedin.com/company/ornek-gida",
                "title": "Ornek Gida | LinkedIn",
            }]}),
            FakeResponse([{
                "name": "Ornek Gida",
                "website": "https://ornek.com.tr",
            }]),
        ]
        with patch.object(config, "ENABLE_LINKEDIN_COMPANY_LOOKUP", True), patch.object(
            config, "BRIGHTDATA_API_KEY", "secret"
        ), patch.object(config, "LINKEDIN_COMPANY_REQUEST_BUDGET", 3), patch(
            "modules.linkedin_company.requests.post", side_effect=responses
        ) as post:
            self.assertTrue(
                linkedin_company.corroborate("ORNEK GIDA", evaluation)["verified"]
            )
        search_url = post.call_args_list[0].kwargs["json"]["url"]
        self.assertNotIn("ornek.com.tr", search_url)

    def test_pipeline_fallback_is_not_called_for_a_ready_candidate(self):
        evaluation = _evaluation("https://ornekgida.com.tr")
        evaluation["reasons"] = [
            "page_identity_strong:2/2",
            "country_identity_tr_tld",
        ]
        evaluation["candidate"]["_official_query_evidence"] = 1
        resolution = entity_resolution.resolve_candidates(
            "ORNEK GIDA", [evaluation]
        )
        self.assertEqual(resolution.status, "resolved")
        with patch.object(
            linkedin_company, "corroborate", Mock()
        ) as corroborate:
            main._try_linkedin_company_corroboration(
                "ORNEK GIDA", [evaluation], resolution
            )
        corroborate.assert_not_called()

    def test_pipeline_can_corroborate_a_lower_ranked_existing_candidate(self):
        wrong = _evaluation("https://wrong-brand.com.tr")
        correct = _evaluation("https://ornek.com.tr")
        resolution = entity_resolution.resolve_candidates(
            "ORNEK GIDA", [wrong, correct]
        )

        def evidence(_company, evaluation):
            matched = "ornek.com.tr" in evaluation["candidate"]["url"]
            return {"verified": matched, "website_match": matched}

        with patch.object(linkedin_company, "corroborate", side_effect=evidence):
            result = main._try_linkedin_company_corroboration(
                "ORNEK GIDA", [wrong, correct], resolution
            )
        self.assertEqual(result.status, "resolved")
        self.assertIs(result.selected, correct)
        self.assertEqual(
            result.reason, "candidate_resolved_by_linkedin_website_match"
        )


if __name__ == "__main__":
    unittest.main()
