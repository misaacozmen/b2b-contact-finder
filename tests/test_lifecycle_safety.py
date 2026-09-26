import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from modules import checkpoint, output_artifacts, run_context, runtime


class LifecycleSafetyTests(unittest.TestCase):
    def _seed(self, path: Path, *, items: list[dict], budgets: dict[str, int] | None = None) -> str:
        run_id = hashlib.sha256(str(path).encode()).hexdigest()
        checkpoint.seed_recovered_run(
            path=path,
            run_id=run_id,
            input_hash="input-hash",
            run_signature="signature",
            context={"phase": "FREE"},
            budgets=budgets or {name: 0 for name in checkpoint.CANONICAL_PROVIDERS},
            items=items,
            results=[],
        )
        config.PROGRESS_DB_FILE = path
        return run_id

    def test_source_identity_preserves_qualified_and_prefixes_unqualified_once(self):
        self.assertEqual(
            run_context.source_record_identity({"source": "tex", "source_record_id": "tex:42"}),
            ("tex:42", "upstream"),
        )
        self.assertEqual(
            run_context.source_record_identity({"source": "tex", "source_record_id": "42"}),
            ("tex:42", "upstream"),
        )
        first = run_context.source_record_identity({"source": "tex", "company": "A", "profile_url": "https://x/a"})
        second = run_context.source_record_identity({"source": "tex", "company": "A", "profile_url": "https://x/b"})
        self.assertNotEqual(first[0], second[0])
        self.assertTrue(first[0].startswith("tex:"))

    def test_resume_recovery_resets_free_and_reopens_paid_work_without_http_evidence(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            self._test_resume_recovery(Path(directory) / "progress.sqlite3")

    def _test_resume_recovery(self, path: Path):
        run_id = self._seed(path, items=[
            {"item_index": 0, "source_record_id": "s:0", "free_state": "RUNNING", "paid_state": "NOT_REQUIRED"},
            {"item_index": 1, "source_record_id": "s:1", "free_state": "DONE", "paid_required": True, "paid_state": "RUNNING"},
        ])
        self.assertEqual(checkpoint.recover_interrupted_items(run_id), {
            "free_reset": 1, "paid_reset": 1, "paid_unknown": 0,
            "pre_http_calls_recovered": 0,
        })
        items = checkpoint.load_run_items(run_id)
        self.assertEqual([item["free_state"] for item in items], ["PENDING", "DONE"])
        self.assertEqual(items[1]["paid_state"], "PENDING")

    def test_item_claim_and_save_are_compare_and_set(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.sqlite3"
            run_id = self._seed(path, items=[{"item_index": 0, "source_record_id": "s:0", "free_state": "PENDING", "paid_state": "NOT_REQUIRED"}])
            self.assertTrue(checkpoint.claim_item(run_id=run_id, item_index=0, phase="FREE"))
            self.assertFalse(checkpoint.claim_item(run_id=run_id, item_index=0, phase="FREE"))
            checkpoint.save_item_transaction(
                run_id=run_id, item_index=0, source_record_id="s:0", payload={"company": "A"},
                free_state="DONE", paid_state="NOT_REQUIRED", paid_required=False,
                free_attempts=1, paid_attempts=0,
            )
            self.assertEqual(checkpoint.load_results_by_id(run_id)[0]["company"], "A")
            with self.assertRaisesRegex(RuntimeError, "scheduler CAS"):
                checkpoint.save_item_transaction(
                    run_id=run_id, item_index=0, source_record_id="s:0", payload={"company": "B"},
                    free_state="DONE", paid_state="NOT_REQUIRED", paid_required=False,
                    free_attempts=2, paid_attempts=0,
                )

    def test_phase_transition_rejects_nonterminal_free_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.sqlite3"
            run_id = self._seed(path, items=[{"item_index": 0, "source_record_id": "s:0", "free_state": "PENDING", "paid_state": "NOT_REQUIRED"}])
            with self.assertRaisesRegex(RuntimeError, "free work"):
                checkpoint.transition_phase(run_id, "PAID", expected_count=1)

    def test_provider_ledger_is_unique_and_atomic(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "progress.sqlite3"
            run_id = self._seed(
                path,
                items=[{"item_index": 0, "source_record_id": "s:0", "free_state": "DONE", "paid_required": True, "paid_state": "RUNNING"}],
                budgets={name: 1 if name == "brightdata" else 0 for name in checkpoint.CANONICAL_PROVIDERS},
            )
            with patch.object(config, "PROGRESS_DB_FILE", path):
                runtime.configure_durable_run(run_id, {name: 1 if name == "brightdata" else 0 for name in checkpoint.CANONICAL_PROVIDERS})
                runtime.set_phase("PAID")
                runtime.set_item_context(0, "search")
                runtime.set_source_record_id("s:0")
                checkpoint.begin_paid_attempt(run_id=run_id, item_index=0, attempt_number=1)
                job = checkpoint.ensure_provider_work_item(
                    run_id=run_id, item_index=0, source_record_id="s:0",
                    provider="brightdata", operation="search",
                    request_fingerprint="brightdata:0:search", need_class="website",
                )
                checkpoint.reserve_provider_dispatch_round(
                    run_id=run_id, provider="brightdata", round_ordinal=1,
                    candidates=[job], cap=1,
                )
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    def reserve(_):
                        runtime.set_phase("PAID")
                        runtime.set_item_context(0, "search")
                        runtime.set_source_record_id("s:0")
                        runtime.set_provider_dispatch_rounds({"brightdata": 1})
                        return runtime.reserve_api("brightdata", operation="search")
                    values = list(executor.map(reserve, range(8)))
                self.assertEqual(sum(bool(value) for value in values), 1)
                accepted = next(value for value in values if value)
                runtime.complete_api(accepted, "DONE")
                self.assertFalse(runtime.reserve_api("brightdata", operation="search"))
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("select count(*) from provider_calls").fetchone()[0], 1)
                self.assertEqual(connection.execute("select state from provider_calls").fetchone()[0], "DONE")

    def test_artifact_writer_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, "produced no file"):
            output_artifacts._atomic_excel(Path(directory) / "missing.xlsx", lambda _path, _rows: None, [])


if __name__ == "__main__":
    unittest.main()
