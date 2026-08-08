import json
import os
import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import Workbook

import config
import main
from modules import aliases, cache_store, checkpoint, crawler, entity_registry, network_guard, runtime
from validate_golden_xlsx import assertion_coverage, evaluate, readiness_issues


class ScaleAndValidationPackageTests(unittest.TestCase):
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
        final = Mock(status_code=200, headers={}, url="https://www.brand.com.tr/")
        final.raise_for_status.return_value = None
        with patch("modules.crawler.network_guard.validate_public_http_url", return_value=(True, "public")), patch.object(
            crawler.SESSION, "get", side_effect=[redirect, final]
        ), patch("modules.crawler.runtime.wait_for_request_slot"):
            response = crawler._request_with_safe_redirects("https://brand.com.tr", verify=True)
        self.assertEqual(response._b2b_final_url, "https://www.brand.com.tr/")

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
