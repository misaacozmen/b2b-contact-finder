import tempfile
import unittest
import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import config
import main
from scrape_exhibitors import enrich_existing_workbook
from validate_benchmark_suite import _population_check
from validate_golden_xlsx import evaluate, evaluate_stages, validate_artifact_contract
from modules import crawler, excel, identity, phone, run_budget, runtime, run_context, scorer, selection
from modules.exhibitor_scraper import _texhibition_profile_details, scrape_ifco


def _workbook(path: Path, headers: list[str], rows: list[dict], *, sheet: str = "Sheet") -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(headers)
    for row in rows:
        worksheet.append([row.get(header, "") for header in headers])
    workbook.save(path)
    workbook.close()


class BenchmarkPopulationGateTests(unittest.TestCase):
    def test_sparse_contacts_count_missing_expected_as_fn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            contacts = root / "contacts.xlsx"
            all_results = root / "all_results.xlsx"
            headers = ["Company", "source_record_id", "Expected Website", "Website Verified", "Expected Email", "Email Verified", "Expected Phone", "Phone Verified"]
            rows = [
                {"Company": "Alpha", "source_record_id": "s:a", "Expected Website": "alpha.example", "Website Verified": "yes", "Expected Email": "a@alpha.example", "Email Verified": "yes", "Expected Phone": "02125550001", "Phone Verified": "yes"},
                {"Company": "Beta", "source_record_id": "s:b", "Expected Website": "beta.example", "Website Verified": "yes", "Expected Email": "b@beta.example", "Email Verified": "yes", "Expected Phone": "02125550002", "Phone Verified": "yes"},
            ]
            _workbook(expected, headers, rows, sheet="Manual Report")
            _workbook(all_results, ["company", "source_record_id", "website"], [
                {"company": "Alpha", "source_record_id": "s:a", "website": "https://alpha.example"},
                {"company": "Beta", "source_record_id": "s:b", "website": "https://beta.example"},
            ])
            _workbook(contacts, ["company", "source_record_id", "website", "email", "phone"], [{
                "company": "Alpha", "source_record_id": "s:a", "website": "https://alpha.example",
                "email": "a@alpha.example", "phone": "02125550001",
            }])
            contract = validate_artifact_contract(expected, contacts, all_results)
            metrics, _ = evaluate(expected, contacts)
        self.assertEqual(contract["published"], 1)
        self.assertEqual(contract["abstained"], 1)
        self.assertEqual(metrics["website"], {"tp": 1, "fp": 0, "fn": 1})

    def test_unexpected_or_duplicate_artifact_ids_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            contacts = root / "contacts.xlsx"
            all_results = root / "all_results.xlsx"
            _workbook(expected, ["Company", "source_record_id"], [{"Company": "Alpha", "source_record_id": "s:a"}], sheet="Manual Report")
            _workbook(all_results, ["company", "source_record_id"], [{"company": "Alpha", "source_record_id": "s:a"}])
            _workbook(contacts, ["company", "source_record_id"], [{"company": "Extra", "source_record_id": "s:x"}])
            with self.assertRaisesRegex(ValueError, "unexpected"):
                validate_artifact_contract(expected, contacts, all_results)
            _workbook(contacts, ["company", "source_record_id"], [])
            for ids in (["s:a", "s:x"], ["s:a", "s:a"], []):
                _workbook(all_results, ["company", "source_record_id"], [
                    {"company": f"Row {index}", "source_record_id": source_id}
                    for index, source_id in enumerate(ids)
                ])
                with self.assertRaises(ValueError):
                    validate_artifact_contract(expected, contacts, all_results)

    def test_all_results_is_exact_population_and_empty_contacts_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            all_results = root / "all_results.xlsx"
            contacts = root / "contacts.xlsx"
            expected_rows = [
                {"Company": "Alpha", "source_record_id": "texhibition_2026:a"},
                {"Company": "Beta", "source_record_id": "zuchex_2026:b"},
            ]
            _workbook(expected, ["Company", "source_record_id"], expected_rows, sheet="Manual Report")
            _workbook(
                all_results,
                ["company", "source_record_id"],
                [{"company": row["Company"], "source_record_id": row["source_record_id"]} for row in expected_rows],
            )
            _workbook(contacts, ["company", "source_record_id"], [])

            structural, quality = _population_check(expected, all_results, contacts, "blind")

            self.assertEqual(structural, [])
            self.assertEqual(quality, [])

    def test_population_gap_and_unexpected_contact_are_structural_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            all_results = root / "all_results.xlsx"
            contacts = root / "contacts.xlsx"
            _workbook(
                expected,
                ["Company", "source_record_id"],
                [{"Company": "Alpha", "source_record_id": "source:a"}],
                sheet="Manual Report",
            )
            _workbook(all_results, ["company", "source_record_id"], [])
            _workbook(
                contacts,
                ["company", "source_record_id"],
                [{"company": "Unexpected", "source_record_id": "source:x"}],
            )

            structural, quality = _population_check(expected, all_results, contacts, "blind")

            self.assertTrue(any("missing" in issue or "no evaluated" in issue for issue in structural))
            self.assertTrue(any("unexpected" in issue for issue in structural))
            self.assertEqual(quality, [])


class BudgetAndLegalRuleTests(unittest.TestCase):
    def test_pinned_project_runtime_has_safe_sqlite(self):
        runtime_python = Path(main.__file__).resolve().parent / ".runtime" / "python3147-sqlite3534" / "python.exe"
        self.assertTrue(runtime_python.is_file())
        version = subprocess.check_output(
            [str(runtime_python), "-c", "import sqlite3; print(sqlite3.sqlite_version)"],
            text=True,
        ).strip()
        self.assertGreaterEqual(tuple(map(int, version.split("."))), (3, 51, 3))

    def test_identity_depth_changes_semantic_config_hash(self):
        with patch.object(config, "MAX_IDENTITY_PAGES", 4):
            four = run_context.RunConfig.from_config(paid_enabled=False).sha256
        with patch.object(config, "MAX_IDENTITY_PAGES", 6):
            six = run_context.RunConfig.from_config(paid_enabled=False).sha256
        self.assertNotEqual(four, six)

    def test_population_ratios_use_890_population_and_zero_disabled(self):
        with patch.object(config, "BRIGHTDATA_REQUEST_RATIO", 3.9), patch.object(
            config, "GOOGLE_PLACES_REQUEST_RATIO", 0.25
        ), patch.object(config, "HUNTER_REQUEST_RATIO", 0.10), patch.object(
            config, "BRANDFETCH_REQUEST_RATIO", 0.25
        ):
            budgets = run_budget.calculate_paid_api_budgets(
                890,
                {"brightdata": None, "google_places": None, "hunter": None, "brandfetch": 100},
            )

        self.assertEqual(budgets, {
            "brightdata": 3471,
            "google_places": 223,
            "hunter": 89,
            "brandfetch": 100,
        })

    def test_budget_boundaries_and_893_matrix(self):
        none_caps = {provider: None for provider in run_budget.PAID_API_PROVIDERS}
        self.assertEqual(
            run_budget.calculate_paid_api_budgets(0, none_caps),
            {provider: 0 for provider in run_budget.PAID_API_PROVIDERS},
        )
        self.assertEqual(run_budget.calculate_paid_api_budgets(1, none_caps), {
            "brightdata": 4, "google_places": 1, "hunter": 1, "brandfetch": 1,
        })
        self.assertEqual(run_budget.calculate_paid_api_budgets(893, none_caps), {
            "brightdata": 3483, "google_places": 224, "hunter": 90, "brandfetch": 224,
        })
        self.assertEqual(
            run_budget.calculate_paid_api_budgets(893, {**none_caps, "brightdata": 0})["brightdata"], 0,
        )
        self.assertEqual(
            run_budget.calculate_paid_api_budgets(893, {**none_caps, "brightdata": 100})["brightdata"], 100,
        )

    def test_zero_budget_rejects_paid_call_without_durable_run(self):
        with patch.object(config, "PAID_ENABLED", True), patch.object(
            config, "BRIGHTDATA_REQUEST_BUDGET", 0
        ), patch.object(config, "BRIGHTDATA_REQUEST_HARD_CAP", None):
            runtime.reset()
            reservation = runtime.reserve_api("brightdata")
        self.assertFalse(reservation.accepted)
        self.assertEqual(reservation.reason, "budget_disabled")

    def test_zero_durable_budget_rejects_with_budget_disabled(self):
        with patch.object(config, "PAID_ENABLED", True):
            runtime.reset()
            runtime.configure_durable_run("run", {"brightdata": 0})
            runtime.set_phase("PAID")
            reservation = runtime.reserve_api("brightdata")
        self.assertFalse(reservation.accepted)
        self.assertEqual(reservation.reason, "budget_disabled")

    def test_unique_telemetry_is_hashed_and_restore_has_no_synthetic_key(self):
        runtime.reset()
        runtime.record_unique("test.hosts", "example.com")
        snapshot = runtime.snapshot()
        self.assertNotIn("example.com", snapshot["unique_keys"]["test.hosts"])
        self.assertEqual(snapshot["unique_counts"]["test.hosts"], 1)
        runtime.restore({"counters": {}, "unique_counts": {"test.hosts": 4}})
        self.assertEqual(runtime.snapshot()["unique_counts"], {})

    def test_legal_company_words_are_centralized_and_deduplicated(self):
        self.assertEqual(len(config.LEGAL_COMPANY_WORDS), len(set(config.LEGAL_COMPANY_WORDS)))
        self.assertTrue({"sirket", "sirketi", "ve", "and"}.issubset(config.LEGAL_COMPANY_WORDS))
        self.assertEqual(
            scorer.distinctive_tokens("ABC Sanayi ve Ticaret Limited Şirketi"),
            ["abc"],
        )
        self.assertNotIn("ve", scorer.legal_identity_tokens("ABC Sanayi ve Ticaret"))
        self.assertNotIn("sirket", scorer.legal_identity_tokens("ABC Şirket"))
        self.assertNotIn("sirketi", scorer.legal_identity_tokens("ABC Şirketi"))


class EnrichedInputTests(unittest.TestCase):
    @patch("modules.exhibitor_scraper.time.sleep")
    @patch("modules.exhibitor_scraper._get")
    def test_ifco_merges_only_labelled_address_from_matching_profile(self, get_mock, _sleep):
        listing = '<main><a href="/tr/fuar/exhibitors/alpha"><img alt="Alpha Tekstil"></a></main>'
        detail = '''<main><h1>Alpha Tekstil</h1><dl>
            <dt>Firma Adresi</dt><dd>Merkez Mah. 1, İstanbul</dd>
            <dt>Telefon</dt><dd>+90 212 555 00 00</dd>
        </dl></main><footer>Organizatör +90 212 999 00 00</footer>'''
        get_mock.side_effect = [listing, detail]
        rows = scrape_ifco(fetch_details=True, delay_sec=0)
        self.assertEqual(rows[0]["listed_address"], "Merkez Mah. 1, İstanbul")
        self.assertEqual(rows[0]["listed_phone"], "")
        self.assertEqual(rows[0]["listed_address_status"], "OBSERVED_PRESENT")
        self.assertEqual(rows[0]["listed_phone_status"], "OBSERVED_ABSENT")
        evidence = json.loads(rows[0]["source_evidence"])
        self.assertTrue(all(item["source_record_id"] for item in evidence))

    def test_texhibition_parser_and_keyed_enrichment_preserve_population(self):
        profile_url = "https://www.texhibitionist.com/en/exhibitors/alpha"
        details = _texhibition_profile_details(
            """
            <main>
              <dl>
                <dt>Company Name</dt><dd>Alpha Tekstil A.S.</dd>
                <dt>Website</dt><dd><a href="https://alpha.example">alpha.example</a></dd>
                <dt>Address</dt><dd>Organize Sanayi Bölgesi 12, Istanbul</dd>
              </dl>
            </main>
            <footer>footer@example.com</footer>
            """,
            profile_url,
        )
        self.assertEqual(details["listed_legal_name"], "Alpha Tekstil A.S.")
        self.assertEqual(details["listed_website"], "https://alpha.example")
        self.assertTrue(details["source_detail_content_sha256"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "firms.xlsx"
            output_path = root / "firms_enriched.xlsx"
            source_rows = [
                {
                    "company": "Alpha",
                    "source": "texhibition_2026",
                    "profile_url": profile_url,
                    "listing_url": "https://www.texhibitionist.com/en/exhibitors?v=1",
                },
                {
                    "company": "Beta",
                    "source": "zuchex_2026",
                    "_id": "z1",
                    "source_record_id": "zuchex_2026:z1",
                },
            ]
            excel.write_company_records(input_path, source_rows)
            before = excel.read_company_records(input_path)
            enriched = enrich_existing_workbook(
                input_path,
                output_path,
                texhibition_rows=[{**details, "profile_url": profile_url}],
                zuchex_rows=[],
            )
            after_input = excel.read_company_records(input_path)
            after_output = excel.read_company_records(enriched)

        self.assertEqual(before, after_input)
        self.assertEqual([row["company"] for row in after_output], ["Alpha", "Beta"])
        self.assertEqual(after_output[0]["listed_website"], "https://alpha.example")
        self.assertEqual(after_output[1]["source_detail_status"], "UNAVAILABLE_NO_PROFILE_URL")

    def test_production_item_fixture_maps_website_without_authorized_person_leak(self):
        fixture = Path(__file__).parent / "fixtures" / "texhibition_detail_ait.html"
        details = _texhibition_profile_details(
            fixture.read_text(encoding="utf-8"),
            "https://www.texhibitionist.com/en/exhibitors/ait-ai-tools",
        )
        self.assertEqual(details["listed_website"], "https://ai.ait.com.tr")
        self.assertNotIn("Melih Yıldız", details["listed_website"])

    def test_unlabelled_iso_capacity_and_postal_values_do_not_become_phone(self):
        details = _texhibition_profile_details(
            """
            <main><article>
              <div class="item"><div class="key">ISO Standards</div><div class="value">9001-14001-45001</div></div>
              <div class="item"><div class="key">Capacity</div><div class="value">3.000.000</div></div>
              <div class="item"><div class="key">Postal Code</div><div class="value">34000</div></div>
            </article></main>
            """,
            "https://www.texhibitionist.com/en/exhibitors/no-phone",
        )
        self.assertEqual(details["listed_phone"], "")
        self.assertEqual(details["listed_phone_status"], "OBSERVED_ABSENT")

    def test_unlabelled_certificate_or_image_link_does_not_become_website(self):
        details = _texhibition_profile_details(
            """
            <main><article>
              <a href="https://example.com/certificates/oeko-tex.pdf">Certificate</a>
              <a href="https://example.com/company-logo.png">Logo</a>
            </article></main>
            """,
            "https://www.texhibitionist.com/en/exhibitors/no-website",
        )
        self.assertEqual(details["listed_website"], "")

    def test_labelled_phone_tel_link_and_website_are_accepted(self):
        details = _texhibition_profile_details(
            """
            <main><article>
              <div class="item"><div class="key">Phone</div><div class="value">
                <a href="tel:%2B90-212-555-12-34">call</a>
              </div></div>
              <div class="item"><div class="key">Web Site</div><div class="value">
                <a href="https://example.com">example.com</a>
              </div></div>
            </article></main>
            """,
            "https://www.texhibitionist.com/en/exhibitors/labelled",
        )
        self.assertEqual(details["listed_phone"], "02125551234")
        self.assertEqual(details["listed_phone_status"], "OBSERVED_PRESENT")
        self.assertEqual(details["listed_website"], "https://example.com")

    def test_invalid_labelled_phone_is_absent_not_present(self):
        details = _texhibition_profile_details(
            """
            <main><article>
              <div class="item"><div class="key">Phone</div><div class="value">9001-14001-45001</div></div>
            </article></main>
            """,
            "https://www.texhibitionist.com/en/exhibitors/invalid-phone",
        )
        self.assertEqual(details["listed_phone"], "")
        self.assertNotEqual(details["listed_phone_status"], "OBSERVED_PRESENT")


class BrowserRecoveryTelemetryTests(unittest.TestCase):
    def setUp(self):
        runtime.reset()

    def test_enabled_browser_preflight_runs_before_network(self):
        with patch.object(config, "ENABLE_JS_FALLBACK", True), patch(
            "modules.crawler._preflight_js_fallback",
            side_effect=RuntimeError("preflight"),
        ), patch("modules.crawler._try_fetch") as fetch:
            with self.assertRaisesRegex(RuntimeError, "preflight"):
                crawler._fetch_site_live("https://example.com")
        fetch.assert_not_called()

    def test_failed_identity_or_root_render_does_not_publish_original_shell(self):
        shell = '<html><body><div id="app"></div><script src="main.js"></script></body></html>'
        with patch.object(config, "ENABLE_JS_FALLBACK", True), patch.object(
            crawler, "_preflight_js_fallback"
        ), patch.object(crawler, "_try_fetch", return_value=(shell, None)), patch.object(
            crawler, "_try_render", return_value=(shell, None)
        ):
            result = crawler._fetch_site_live("https://example.com")
        self.assertEqual(result["pages"], [])

    def test_interstitial_host_count_is_unique_but_page_count_is_not(self):
        crawler._record_security_interstitial("https://example.com/a")
        crawler._record_security_interstitial("https://example.com/b")
        crawler._record_security_interstitial("https://example.com/cache", source="cache")
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["counters"]["live.site.security_interstitial_rejected"], 2)
        self.assertEqual(snapshot["counters"]["cache.site.security_interstitial_rejected"], 1)
        self.assertEqual(snapshot["unique_counts"]["recovery.security_interstitial_hosts"], 1)


class SourceMetadataCorroborationTests(unittest.TestCase):
    def test_source_phone_and_address_are_only_supporting_reasons(self):
        source_id = "texhibition_2026:source-1"
        source_url = "https://www.texhibitionist.com/en/exhibitors/source-1"
        content_hash = "a" * 64
        phone_value = "+90 555 111 22 33"
        address_value = "Istanbul Ataturk Organize Sanayi 34000"
        reasons = main._source_metadata_match_reasons(
            {
                "source_record_id": source_id,
                "source_detail_status": "COMPLETED",
                "source_detail_url": source_url,
                "source_detail_content_sha256": content_hash,
                "listed_phone": phone_value,
                "listed_address": address_value,
                "source_evidence": json.dumps([
                    {
                        "source_record_id": source_id, "field": "listed_phone",
                        "value": phone_value, "normalized_value": phone.normalize_phone(phone_value),
                        "url": source_url, "content_sha256": content_hash, "observed_at": "2026-09-07T00:00:00+00:00",
                    },
                    {
                        "source_record_id": source_id, "field": "listed_address",
                        "value": address_value, "normalized_value": main._normalized_source_field("listed_address", address_value),
                        "url": source_url, "content_sha256": content_hash, "observed_at": "2026-09-07T00:00:00+00:00",
                    },
                ]),
            },
            [{"url": "https://official.example/contact", "address": "Istanbul Ataturk Organize Sanayi 34000"}],
            ["05551112233"],
            {"addresses": ["Istanbul Ataturk Organize Sanayi 34000"]},
            ["page_identity_strong:2/2"],
        )
        self.assertEqual(
            set(reasons),
            {"source_listing_phone_match", "source_listing_address_match", f"source_profile:{source_id}"},
        )

    def test_plain_source_metadata_without_completed_evidence_is_ignored(self):
        reasons = main._source_metadata_match_reasons(
            {"listed_phone": "+90 555 111 22 33", "listed_address": "Istanbul"},
            [{"url": "https://official.example/contact", "address": "Istanbul"}],
            ["05551112233"], {"addresses": ["Istanbul"]}, ["page_identity_strong:2/2"],
        )
        self.assertEqual(reasons, [])

    def test_each_verified_source_field_mints_one_package_and_mismatch_mints_none(self):
        source_id = "texhibition_2026:source-2"
        source_url = "https://www.texhibitionist.com/en/exhibitors/source-2"
        content_hash = "b" * 64
        phone_value = "+90 555 111 22 33"
        address_value = "Istanbul Ataturk Organize Sanayi 34000"
        base = {
            "source_record_id": source_id,
            "source_detail_status": "COMPLETED",
            "source_detail_url": source_url,
            "source_detail_content_sha256": content_hash,
            "listed_phone": phone_value,
            "listed_address": address_value,
            "source_evidence": json.dumps([
                {
                    "source_record_id": source_id, "field": "listed_phone",
                    "value": phone_value, "normalized_value": phone.normalize_phone(phone_value),
                    "url": source_url, "content_sha256": content_hash, "observed_at": "2026-09-07T00:00:00+00:00",
                },
                {
                    "source_record_id": source_id, "field": "listed_address",
                    "value": address_value, "normalized_value": main._normalized_source_field("listed_address", address_value),
                    "url": source_url, "content_sha256": content_hash, "observed_at": "2026-09-07T00:00:00+00:00",
                },
            ]),
        }
        phone_reasons = main._source_metadata_match_reasons(
            base, [], ["05551112233"], {"addresses": []}, ["page_identity_strong:2/2"]
        )
        address_reasons = main._source_metadata_match_reasons(
            base, [], [], {"addresses": [address_value]}, ["page_identity_strong:2/2"]
        )
        mismatch_reasons = main._source_metadata_match_reasons(
            base, [], ["05441112233"], {"addresses": ["Ankara Organize Sanayi 06000"]}, ["page_identity_strong:2/2"]
        )
        package_reason = f"source_profile:{source_id}"
        self.assertEqual(set(phone_reasons), {"source_listing_phone_match", package_reason})
        self.assertEqual(set(address_reasons), {"source_listing_address_match", package_reason})
        self.assertEqual(mismatch_reasons, [])

        assessment = identity.assess(
            "Alpha Tekstil",
            {"_identity_company": "Alpha Tekstil"},
            ["source_listing_phone_match", "source_listing_address_match", package_reason],
            {},
        )
        source_signals = [signal for signal in assessment["signals"] if signal["source"] == "source_profile"]
        self.assertEqual(len(source_signals), 1)
        self.assertEqual(source_signals[0]["independence_key"], package_reason)
        self.assertFalse(assessment["publishable"])


class GoldenSourceIdentityTests(unittest.TestCase):
    def test_same_company_different_source_ids_are_aligned_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            actual = root / "actual.xlsx"
            _workbook(
                expected,
                ["Company", "source_record_id", "Expected Website", "Website Verified", "Expected Email", "Email Verified", "Expected Phone", "Phone Verified"],
                [
                    {"Company": "Same", "source_record_id": "source:a", "Expected Website": "a.example", "Website Verified": "yes", "Email Verified": "unknown", "Phone Verified": "unknown"},
                    {"Company": "Same", "source_record_id": "source:b", "Expected Website": "b.example", "Website Verified": "yes", "Email Verified": "unknown", "Phone Verified": "unknown"},
                ],
                sheet="Manual Report",
            )
            _workbook(
                actual,
                ["company", "source_record_id", "website"],
                [
                    {"company": "Same", "source_record_id": "source:b", "website": "https://b.example"},
                    {"company": "Same", "source_record_id": "source:a", "website": "https://a.example"},
                ],
            )
            metrics, _ = evaluate(expected, actual)
        self.assertEqual(metrics["website"], {"tp": 2, "fp": 0, "fn": 0})

    def test_partial_source_ids_are_structural_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            actual = root / "actual.xlsx"
            _workbook(expected, ["Company", "source_record_id"], [{"Company": "Same", "source_record_id": "source:a"}], sheet="Manual Report")
            _workbook(actual, ["company", "website"], [{"company": "Same", "website": "https://a.example"}])
            with self.assertRaisesRegex(ValueError, "source_record_id"):
                evaluate(expected, actual)


class SharedSelectionAndBudgetTests(unittest.TestCase):
    def test_cli_and_pipeline_use_same_population_budget_and_canonical_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "firms.xlsx"
            _workbook(
                input_path,
                ["company", "source", "source_record_id"],
                [
                    {"company": "Alpha", "source": "texhibition_2026", "source_record_id": "texhibition_2026:a"},
                    {"company": "Beta", "source": "zuchex_2026", "source_record_id": "zuchex_2026:b"},
                    {"company": "Ignored", "source": "texhibition_2026", "source_record_id": "texhibition_2026:c"},
                ],
            )
            with patch.object(config, "BRIGHTDATA_REQUEST_HARD_CAP", None), patch.object(
                config, "GOOGLE_PLACES_REQUEST_HARD_CAP", None
            ), patch.object(config, "HUNTER_REQUEST_HARD_CAP", None), patch.object(
                config, "BRANDFETCH_REQUEST_HARD_CAP", None
            ):
                cli_config = main.resolve_cli_run_config([
                    "--input", str(input_path), "--companies", "Alpha,Beta", "--allow-paid",
                ])
                records, _ = selection.select_company_records(
                    input_path, companies={"Alpha", "Beta"}, require_nonempty=True,
                )
                calculated = run_budget.calculate_paid_api_budgets(
                    len(records), run_budget.explicit_paid_api_caps(),
                )
                pipeline_config = run_context.RunConfig.from_config(
                    paid_enabled=True,
                    budgets={
                        **calculated,
                        "linkedin": config.LINKEDIN_COMPANY_REQUEST_BUDGET,
                        "llm": config.LLM_ARBITER_BUDGET,
                    },
                )
                ordered_ids = [record["source_record_id"] for record in records]
                input_hash = __import__("hashlib").sha256(input_path.read_bytes()).hexdigest()
                cli_run_id = run_context.canonical_run_id(
                    input_sha256=input_hash, ordered_source_record_ids=ordered_ids,
                    effective_config=cli_config.as_dict(),
                    runtime_source_tree_hash=run_context.source_tree_sha256(),
                )
                pipeline_run_id = run_context.canonical_run_id(
                    input_sha256=input_hash, ordered_source_record_ids=ordered_ids,
                    effective_config=pipeline_config.as_dict(),
                    runtime_source_tree_hash=run_context.source_tree_sha256(),
                )
        self.assertEqual(cli_config.as_dict()["budgets"], pipeline_config.as_dict()["budgets"])
        self.assertEqual(cli_run_id, pipeline_run_id)


if __name__ == "__main__":
    unittest.main()
