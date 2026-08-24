import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from openpyxl import Workbook

import config
import main
from modules import aliases, cache_store, checkpoint, crawler, entity_registry, network_guard, runtime, scorer
from validate_golden_xlsx import assertion_coverage, evaluate, readiness_issues


class ScaleAndValidationPackageTests(unittest.TestCase):
    def test_network_guard_rejects_mixed_public_and_private_dns_answers(self) -> None:
        def resolver(_host, port, type):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
            ]

        self.assertEqual(
            network_guard.validate_public_http_url("https://company.example", resolver),
            (False, "dns_non_public_address"),
        )

    def test_pinned_adapter_uses_validated_ip_but_origin_tls_hostname(self) -> None:
        target = network_guard.ResolvedTarget(
            "https", "company.example", 443, "93.184.216.34",
        )
        adapter = network_guard._PinnedHTTPAdapter(target)
        poolmanager = Mock()
        adapter.poolmanager = poolmanager
        request = requests.Request("GET", "https://company.example/contact").prepare()
        adapter.get_connection_with_tls_context(request, True)
        call = poolmanager.connection_from_host.call_args
        self.assertEqual(call.kwargs["host"], "93.184.216.34")
        self.assertEqual(call.kwargs["pool_kwargs"]["server_hostname"], "company.example")
        self.assertEqual(call.kwargs["pool_kwargs"]["assert_hostname"], "company.example")

    def test_hardened_session_disables_environment_proxies(self) -> None:
        session = network_guard.harden_session(requests.Session())
        self.assertFalse(session.trust_env)
        self.assertIsInstance(
            session.get_adapter("https://company.example"),
            network_guard.PublicOnlyHTTPAdapter,
        )

    def test_network_guard_rejects_private_dns_answer(self) -> None:
        def resolver(_host, port, type):
            self.assertEqual(type, socket.SOCK_STREAM)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        self.assertEqual(
            network_guard.validate_public_http_url("https://company.example", resolver),
            (False, "dns_non_public_address"),
        )

    def test_safe_redirect_rejects_cross_domain_target(self) -> None:
        response = Mock(status_code=302, headers={"location": "https://evil.example/contact"})
        with patch("modules.crawler.network_guard.validate_public_http_url", return_value=(True, "public")), patch.object(
            crawler.SESSION, "get", return_value=response
        ), patch("modules.crawler.runtime.wait_for_request_slot"):
            with self.assertRaisesRegex(Exception, "cross_domain_redirect"):
                crawler._request_with_safe_redirects("https://brand.example", verify=True)

    def test_safe_redirect_allows_same_registrable_domain(self) -> None:
        redirect = Mock(status_code=301, headers={"location": "https://www.brand.com.tr/"})
        final = Mock(status_code=200, headers={"content-length": "7"}, url="https://www.brand.com.tr/")
        final.raise_for_status.return_value = None
        final.iter_content.return_value = [b"content"]
        with patch("modules.crawler.network_guard.validate_public_http_url", return_value=(True, "public")), patch.object(
            crawler.SESSION, "get", side_effect=[redirect, final]
        ), patch("modules.crawler.runtime.wait_for_request_slot"):
            response = crawler._request_with_safe_redirects("https://brand.com.tr", verify=True)
        self.assertEqual(response._b2b_final_url, "https://www.brand.com.tr/")

    def test_crawler_rejects_declared_oversized_response_before_reading(self) -> None:
        response = Mock(
            status_code=200,
            headers={"content-length": "2048"},
        )
        response.raise_for_status.return_value = None
        with patch(
            "modules.crawler.network_guard.validate_public_http_url",
            return_value=(True, "public"),
        ), patch.object(crawler.SESSION, "get", return_value=response), patch(
            "modules.crawler.runtime.wait_for_request_slot"
        ):
            with self.assertRaises(crawler.ResponseTooLarge):
                crawler._request_with_safe_redirects(
                    "https://brand.example", verify=True, max_bytes=1024,
                )
        response.iter_content.assert_not_called()
        response.close.assert_called_once_with()

    def test_public_suffix_boundary_rejects_unrelated_multilevel_domains(self) -> None:
        self.assertFalse(
            scorer.same_registrable_domain("https://alpha.co.jp", "https://beta.co.jp")
        )
        self.assertTrue(
            scorer.same_registrable_domain(
                "https://www.alpha.co.jp", "https://contact.alpha.co.jp"
            )
        )
        self.assertFalse(
            scorer.same_registrable_domain("8.8.8.8", "1.1.8.8")
        )

    def test_sitemap_skips_malformed_urls(self) -> None:
        sitemap = (
            "<urlset><url><loc>https://[broken/contact</loc></url>"
            "<url><loc>https://brand.example/contact</loc></url></urlset>"
        )
        with patch.object(crawler, "_try_fetch", return_value=(sitemap, None)):
            self.assertEqual(
                crawler._sitemap_contact_urls(
                    "https://brand.example", ["https://brand.example/sitemap.xml"]
                ),
                ["https://brand.example/contact"],
            )

    def test_sqlite_checkpoint_saves_rows_individually_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.xlsx"
            input_path.write_bytes(b"input")
            with patch.object(config, "PROGRESS_FILE", root / "progress.json"), patch.object(
                config, "PROGRESS_DB_FILE", root / "progress.sqlite3"
            ):
                checkpoint.save_result(input_path, 1, {"company": "B", "__index": 1}, "sig")
                checkpoint.save_result(input_path, 0, {"company": "A", "__index": 0}, "sig")
                loaded = checkpoint.load_progress(input_path, "sig")
                self.assertEqual(loaded["last_completed_index"], 1)
                self.assertEqual([row["company"] for row in loaded["results_so_far"]], ["A", "B"])
                self.assertTrue(checkpoint.has_progress())
                checkpoint.clear_progress()
                self.assertFalse(checkpoint.has_progress())

    def test_completed_run_cleanup_preserves_other_run_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_input = root / "first.xlsx"
            second_input = root / "second.xlsx"
            first_input.write_bytes(b"first")
            second_input.write_bytes(b"second")
            with patch.object(config, "PROGRESS_FILE", root / "progress.json"), patch.object(
                config, "PROGRESS_DB_FILE", root / "progress.sqlite3"
            ):
                checkpoint.save_result(first_input, 0, {"company": "A"}, "first")
                checkpoint.save_result(second_input, 0, {"company": "B"}, "second")
                checkpoint.clear_run_progress(first_input, "first")

                self.assertIsNone(checkpoint.load_progress(first_input, "first"))
                remaining = checkpoint.load_progress(second_input, "second")
                self.assertEqual(remaining["results_so_far"][0]["company"], "B")

    def test_duplicate_company_rows_merge_metadata(self) -> None:
        rows, removed = main._deduplicate_company_records([
            {"company": "Örnek A.Ş.", "website": "", "sector": "kozmetik", "source": "fair1"},
            {"company": "ORNEK A.S.", "website": "https://ornek.com.tr", "sector": "", "source": "fair2"},
        ])
        self.assertEqual(removed, 1)
        self.assertEqual(rows[0]["website"], "https://ornek.com.tr")
        self.assertEqual(rows[0]["source"], "fair1;fair2")

    def test_cache_is_written_compressed_and_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_store.save(root, "crawl", "key", {"html": "x" * 1000}, 1)
            self.assertTrue(list((root / "crawl").glob("*.json.gz")))
            self.assertEqual(cache_store.load(root, "crawl", "key", 30, 1)["html"], "x" * 1000)

    def test_parallel_cache_writes_to_same_key_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ThreadPoolExecutor(max_workers=16) as executor:
                list(executor.map(
                    lambda value: cache_store.save(
                        root, "crawl", "shared", {"value": value}, 1,
                    ),
                    range(64),
                ))
            loaded = cache_store.load(root, "crawl", "shared", 30, 1)
            self.assertIn(loaded["value"], range(64))

    def test_cache_replace_retries_transient_windows_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source, target):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("transient lock")
                return real_replace(source, target)

            with patch("modules.cache_store.os.replace", side_effect=flaky_replace), patch(
                "modules.cache_store.time.sleep",
            ):
                cache_store.save(root, "crawl", "key", {"ok": True}, 1)

            self.assertEqual(cache_store.load(root, "crawl", "key", 30, 1), {"ok": True})
            self.assertEqual(attempts, 2)

    def test_paid_api_budget_is_atomic(self) -> None:
        runtime.reset()
        self.assertTrue(runtime.reserve_api("test", 1))
        self.assertFalse(runtime.reserve_api("test", 1))
        self.assertEqual(runtime.snapshot()["counters"]["api.test.requests"], 1)

    def test_crawler_http_budget_is_atomic(self) -> None:
        runtime.reset()
        self.assertTrue(runtime.reserve_crawler_http(1))
        self.assertFalse(runtime.reserve_crawler_http(1))
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["counters"]["http.crawler.requests"], 1)
        self.assertEqual(snapshot["counters"]["http.crawler.budget_blocked"], 1)

    def test_free_search_query_budget_is_atomic(self) -> None:
        runtime.reset()
        self.assertTrue(runtime.reserve_search_query(1))
        self.assertFalse(runtime.reserve_search_query(1))
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["counters"]["http.search.requests"], 1)
        self.assertEqual(snapshot["counters"]["http.search.budget_blocked"], 1)

    def test_entity_registry_supports_multiple_verified_domains_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps({"entities": [{
                "entity_id": "entity-1",
                "legal_names": ["Example Gıda A.Ş."],
                "brands": ["Example Brand"],
                "official_domains": [
                    {"url": "https://example.com.tr", "confidence": "verified", "relationship": "corporate"},
                    {"url": "https://examplebrand.com", "confidence": "verified", "relationship": "brand"},
                    {"url": "https://unreviewed.example", "confidence": "observed"}
                ]
            }]}), encoding="utf-8")
            with patch.object(config, "ENTITY_REGISTRY_FILE", path):
                entity_registry._entities.cache_clear()
                records = entity_registry.verified_domains("Example Brand")
                entity_registry._entities.cache_clear()
        self.assertEqual({record["relationship"] for record in records}, {"corporate", "brand"})
        self.assertTrue(all(record["entity_id"] == "entity-1" for record in records))

    def test_unknown_golden_state_is_complete_but_excluded_from_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            actual = root / "actual.xlsx"
            self._write(expected, "Manual Report", [
                ["Company", "Expected Website", "Website Verified", "Expected Email", "Email Verified", "Expected Phone", "Phone Verified"],
                ["Example", "", "unknown", "info@example.com", "present", "", "absent"],
            ])
            self._write(actual, "Sheet", [
                ["company", "website", "email", "phone"],
                ["Example", "https://anything.example", "info@example.com", ""],
            ])
            self.assertEqual(readiness_issues(expected), [])
            metrics, _ = evaluate(expected, actual)
            coverage = assertion_coverage(expected)
        self.assertEqual(metrics["website"], {"tp": 0, "fp": 0, "fn": 0})
        self.assertEqual(metrics["email"]["tp"], 1)
        self.assertEqual(coverage["website"]["unknown"], 1)

    def test_golden_evaluation_can_be_limited_to_selected_companies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.xlsx"
            actual = root / "actual.xlsx"
            self._write(expected, "Manual Report", [
                ["Company", "Expected Website", "Website Verified", "Expected Email", "Email Verified", "Expected Phone", "Phone Verified"],
                ["Selected", "selected.com", "present", "", "absent", "", "absent"],
                ["Other", "other.com", "present", "", "absent", "", "absent"],
            ])
            self._write(actual, "Sheet", [["company", "website", "email", "phone"], ["Selected", "https://selected.com", "", ""]])
            metrics, complete = evaluate(expected, actual, {"Selected"})
        self.assertEqual(metrics["website"], {"tp": 1, "fp": 0, "fn": 0})
        self.assertEqual(complete, ["Selected"])

    def test_benchmark_validator_public_mode_success(self) -> None:
        import validate_benchmark_suite
        sets, issues = validate_benchmark_suite.validate_manifest(
            Path("data/benchmark_splits.json"),
            private_seen_workbook=None,
        )
        self.assertEqual(issues, [])
        self.assertTrue(len(sets) > 0)

    def test_benchmark_validator_private_missing_file_error(self) -> None:
        import validate_benchmark_suite
        _, issues = validate_benchmark_suite.validate_manifest(
            Path("data/benchmark_splits.json"),
            private_seen_workbook=Path("non_existent_firms.xlsx"),
        )
        self.assertTrue(any("private seen workbook not found" in issue for issue in issues))

    def test_benchmark_validator_private_seen_exact_counts_and_overlap(self) -> None:
        import validate_benchmark_suite
        from modules import excel
        with tempfile.TemporaryDirectory() as directory:
            # 1. Header-only (0 companies) -> mismatch
            wb_empty = Path(directory) / "empty.xlsx"
            excel.write_company_records(wb_empty, [])
            _, issues = validate_benchmark_suite.validate_manifest(
                Path("data/benchmark_splits.json"),
                private_seen_workbook=wb_empty,
            )
            self.assertTrue(any("count mismatch: actual 0 != expected 71" in issue for issue in issues))

            # 2. 70 companies -> mismatch
            wb_70 = Path(directory) / "seen_70.xlsx"
            excel.write_company_records(wb_70, [{"company": f"Synthetic Unseen Co {i}"} for i in range(70)])
            _, issues_70 = validate_benchmark_suite.validate_manifest(
                Path("data/benchmark_splits.json"),
                private_seen_workbook=wb_70,
            )
            self.assertTrue(any("count mismatch: actual 70 != expected 71" in issue for issue in issues_70))

            # 3. 72 companies -> mismatch
            wb_72 = Path(directory) / "seen_72.xlsx"
            excel.write_company_records(wb_72, [{"company": f"Synthetic Unseen Co {i}"} for i in range(72)])
            _, issues_72 = validate_benchmark_suite.validate_manifest(
                Path("data/benchmark_splits.json"),
                private_seen_workbook=wb_72,
            )
            self.assertTrue(any("count mismatch: actual 72 != expected 71" in issue for issue in issues_72))

            # 4. 71 companies and 0 overlap -> OK
            wb_71 = Path(directory) / "seen_71.xlsx"
            excel.write_company_records(wb_71, [{"company": f"Synthetic Clean Co {i}"} for i in range(71)])
            _, issues_71 = validate_benchmark_suite.validate_manifest(
                Path("data/benchmark_splits.json"),
                private_seen_workbook=wb_71,
            )
            self.assertEqual(issues_71, [])

            # 5. 71 companies with 1 overlap in golden 4 (cormind) -> overlap detected without name leak
            wb_overlap = Path(directory) / "seen_overlap.xlsx"
            overlap_rows = [{"company": f"Synthetic Clean Co {i}"} for i in range(70)] + [{"company": "cormind"}]
            excel.write_company_records(wb_overlap, overlap_rows)
            _, issues_ov = validate_benchmark_suite.validate_manifest(
                Path("data/benchmark_splits.json"),
                private_seen_workbook=wb_overlap,
            )
            self.assertTrue(any("private seen overlap: 1" in issue for issue in issues_ov))
            for issue in issues_ov:
                self.assertNotIn("cormind", issue)

            # 6. Multi-role overlap: overlap in golden 4 (cormind) and golden 5 (bioanalytic diagnostik)
            wb_multi = Path(directory) / "seen_multi_overlap.xlsx"
            multi_rows = (
                [{"company": f"Synthetic Clean Co {i}"} for i in range(69)]
                + [{"company": "cormind"}]
                + [{"company": "bioanalytic diagnostik kimya sanayi ve ticaret limited sirketi"}]
            )
            excel.write_company_records(wb_multi, multi_rows)
            _, issues_multi = validate_benchmark_suite.validate_manifest(
                Path("data/benchmark_splits.json"),
                private_seen_workbook=wb_multi,
            )
            self.assertTrue(any("development_golden4: private seen overlap: 1" in issue for issue in issues_multi))
            self.assertTrue(any("blind: private seen overlap: 1" in issue for issue in issues_multi))
            for issue in issues_multi:
                self.assertNotIn("cormind", issue)
                self.assertNotIn("bioanalytic", issue)

    def test_benchmark_validator_strict_boolean_and_policy_checks(self) -> None:
        import validate_benchmark_suite
        with tempfile.TemporaryDirectory() as directory:
            # 1. Non-boolean string "false"
            manifest_str = Path(directory) / "str_flag.json"
            manifest_str.write_text(json.dumps({
                "version": 1,
                "policy": {"private_seen_expected_unique_companies": 71},
                "sets": [{"name": "g1", "role": "blind", "expected": "", "private_seen_check": "false"}]
            }), encoding="utf-8")
            _, issues = validate_benchmark_suite.validate_manifest(manifest_str)
            self.assertTrue(any("private_seen_check must be a boolean" in issue for issue in issues))

            # 2. Non-boolean string "true"
            manifest_str_true = Path(directory) / "str_true_flag.json"
            manifest_str_true.write_text(json.dumps({
                "version": 1,
                "policy": {"private_seen_expected_unique_companies": 71},
                "sets": [{"name": "g1", "role": "blind", "expected": "", "private_seen_check": "true"}]
            }), encoding="utf-8")
            _, issues_st = validate_benchmark_suite.validate_manifest(manifest_str_true)
            self.assertTrue(any("private_seen_check must be a boolean" in issue for issue in issues_st))

            # 3. Non-boolean integer 1
            manifest_int = Path(directory) / "int_flag.json"
            manifest_int.write_text(json.dumps({
                "version": 1,
                "policy": {"private_seen_expected_unique_companies": 71},
                "sets": [{"name": "g1", "role": "blind", "expected": "", "private_seen_check": 1}]
            }), encoding="utf-8")
            _, issues_int = validate_benchmark_suite.validate_manifest(manifest_int)
            self.assertTrue(any("private_seen_check must be a boolean" in issue for issue in issues_int))

            # 4. Policy count mismatch (e.g. 0, 70, or string "71")
            manifest_bad_policy = Path(directory) / "bad_policy.json"
            manifest_bad_policy.write_text(json.dumps({
                "version": 1,
                "policy": {"private_seen_expected_unique_companies": 0},
                "sets": [{"name": "g1", "role": "dev", "expected": "", "private_seen_check": True}]
            }), encoding="utf-8")
            _, issues_bp = validate_benchmark_suite.validate_manifest(manifest_bad_policy)
            self.assertTrue(any("private_seen_expected_unique_companies must be 71" in issue for issue in issues_bp))

            # 5. No private_seen_check=True sets in manifest
            manifest_none = Path(directory) / "none_flag.json"
            manifest_none.write_text(json.dumps({
                "version": 1,
                "policy": {"private_seen_expected_unique_companies": 71},
                "sets": [{"name": "g1", "role": "dev", "expected": ""}]
            }), encoding="utf-8")
            _, issues_none = validate_benchmark_suite.validate_manifest(manifest_none)
            self.assertTrue(any("manifest policy error: at least one set must have private_seen_check=true" in issue for issue in issues_none))

    def test_benchmark_validator_cli_exit_codes(self) -> None:
        # Public CLI run -> exit code 0
        r_pub = subprocess.run([sys.executable, "validate_benchmark_suite.py"], capture_output=True, text=True)
        self.assertEqual(r_pub.returncode, 0)
        self.assertIn("private_seen_gate: NOT_REQUESTED", r_pub.stdout)

        # Non-existent file -> exit code 2
        r_bad = subprocess.run([sys.executable, "validate_benchmark_suite.py", "--private-seen-workbook", "missing.xlsx"], capture_output=True, text=True)
        self.assertEqual(r_bad.returncode, 2)

    def test_repository_has_no_public_seen_hashes_fixture_or_tool(self) -> None:
        self.assertFalse(Path("data/benchmark_seen_company_hashes.json").exists())
        self.assertFalse(Path("build_benchmark_seen_hashes.py").exists())
        for path in Path("data").rglob("*seen*.json"):
            self.fail(f"Found forbidden seen fixture in data/: {path}")

    @staticmethod
    def _write(path: Path, title: str, rows: list[list]) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = title
        for row in rows:
            sheet.append(row)
        workbook.save(path)


if __name__ == "__main__":
    unittest.main()
