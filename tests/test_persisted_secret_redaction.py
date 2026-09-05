"""Tests for secret redaction in logs, cache, replay snapshots, and persisted artifact surfaces."""

from __future__ import annotations

import copy
import gzip
import html
import io
import json
import logging
import sqlite3
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
from modules import (
    cache_store,
    checkpoint,
    company_resolvers,
    discovery_coverage,
    entity_registry,
    excel,
    quality_audit,
    redaction,
    redaction_scanner,
    replay_snapshot,
    runtime,
    utils,
)
from decision_fixtures import frozen_row


class PersistedSecretRedactionTests(unittest.TestCase):
    def test_query_secret_variations_and_idempotence(self):
        cases = [
            ("https://example.com/api?token=VERYSECRET_LITERAL&safe=123", "https://example.com/api?token=[REDACTED]&safe=123"),
            ("https://example.com/api&sig=VERYSECRET_SIG&safe=123", "https://example.com/api&sig=[REDACTED]&safe=123"),
            ("https://example.com/api%3Ftoken%3DVERYSECRET_ENC%26safe%3D123", "https://example.com/api%3Ftoken%3D[REDACTED]%26safe%3D123"),
            ("https://example.com/api%26sig%3DVERYSECRET_ENC%26safe%3D123", "https://example.com/api%26sig%3D[REDACTED]%26safe%3D123"),
            ("https://example.com/api%253Ftoken%253DVERYSECRET_DOUBLE%2526safe%253D123", "https://example.com/api%253Ftoken%253D[REDACTED]%2526safe%253D123"),
            ("https://x.test/api%25253Ftoken%25253DTRIPLE%252526safe%25253D1", "https://x.test/api%25253Ftoken%25253D[REDACTED]%252526safe%25253D1"),
            ("https://example.com/api&amp;token=VERYSECRET_AMP&safe=123", "https://example.com/api&amp;token=[REDACTED]&safe=123"),
            ("https://x.test/api&amp;amp;token=DOUBLE_ENTITY&amp;amp;safe=1", "https://x.test/api&amp;amp;token=[REDACTED]&amp;amp;safe=1"),
            ("https://x.test/api&amp;amp;amp;token=TRIPLE_ENTITY&amp;amp;amp;safe=1", "https://x.test/api&amp;amp;amp;token=[REDACTED]&amp;amp;amp;safe=1"),
            ("https://example.com/api&#38;token=VERYSECRET_DEC&safe=123", "https://example.com/api&#38;token=[REDACTED]&safe=123"),
            ("https://example.com/api&#x26;token=VERYSECRET_HEX&safe=123", "https://example.com/api&#x26;token=[REDACTED]&safe=123"),
            ("https://example.com/api&amp;%253Ftoken%3DMIXED&amp;safe=1", "https://example.com/api&amp;%253Ftoken%3D[REDACTED]&amp;safe=1"),
            ("Bearer VERYSECRET_BEARER", "Bearer [REDACTED]"),
            ("https://user:VERYSECRET_PASS@example.com/api", "https://[REDACTED]@example.com/api"),
            ("token=LITERAL_SECRET", "token=[REDACTED]"),
            ("api_key=LITERAL_SECRET", "api_key=[REDACTED]"),
            ("client_id=CLIENT_SECRET", "client_id=[REDACTED]"),
            ("accessToken: LITERAL_SECRET", "accessToken: [REDACTED]"),
            ("X-Api-Key: HEADER_SECRET", "X-Api-Key: [REDACTED]"),
            ("https://x/#access_token=FRAGMENT_SECRET", "https://x/#access_token=[REDACTED]"),
            ("https://x/path;token=MATRIX_SECRET", "https://x/path;token=[REDACTED]"),
            ("https://x/?token=[REDACTED]STILL_SECRET&safe=1", "https://x/?token=[REDACTED]&safe=1"),
            ('Authorization: Digest username="u", nonce="N", response="R"', "Authorization: [REDACTED]"),
            ("Proxy-Authorization: Negotiate BASE64SECRET== more", "Proxy-Authorization: [REDACTED]"),
            ("Authorization: AWS4-HMAC-SHA256 Credential=AKIA/..., SignedHeaders=host, Signature=SECRET", "Authorization: [REDACTED]"),
            ("Cookie: session=SECRET_COOKIE", "Cookie: [REDACTED]"),
            ("Set-Cookie: session=SECRET_COOKIE", "Set-Cookie: [REDACTED]"),
            ('<script>window.x={cookie:"COOKIE_SECRET",safe:"keep"}</script>', '<script>window.x={cookie:"[REDACTED]",safe:"keep"}</script>'),
            ('<script>window.x={authorization:"AUTH_SECRET",safe:"keep"}</script>', '<script>window.x={authorization:"[REDACTED]",safe:"keep"}</script>'),
            ('<div title="Cookie: chocolate" data-safe="keep">ok</div>', '<div title="Cookie: chocolate" data-safe="keep">ok</div>'),
            ("https://x.test/?%74oken=ENCODED_KEY_SECRET&safe=1", "[REDACTED]"),
            ("https://x.test/?api%4Bey=ENCODED_API_SECRET", "[REDACTED]"),
            ('<script>{"access\\u0054oken":"UNICODE_KEY_SECRET","safe":"keep"}</script>', "[REDACTED]"),
            ('<div data-api&#45;key="ENTITY_ATTR_SECRET" class="safe">x</div>', "[REDACTED]"),
            ('<script>window.x={"accessToken":"PREFIX\\"SECRET_SUFFIX","safe":"keep"}</script>', '<script>window.x={"accessToken":"[REDACTED]","safe":"keep"}</script>'),
            ("accessToken:`BACKTICK_SECRET`", "accessToken:`[REDACTED]`"),
            ('<div data-api-key="SECRET_ATTR" class="safe">hello</div>', '<div data-api-key="[REDACTED]" class="safe">hello</div>'),
            ('<div data-api-key=SECRET_UNQUOTED class="safe">hello</div>', '<div data-api-key=[REDACTED] class="safe">hello</div>'),
            ('{"apiKey":SECRET_VAL, "safe": true}', '{"apiKey":[REDACTED], "safe": true}'),
            ('{"clientSecret":"MY_CLIENT_SECRET"}', '{"clientSecret":"[REDACTED]"}'),
            ('{"betoken":"keep_val"}', '{"betoken":"keep_val"}'),
        ]
        for original, expected in cases:
            redacted = redaction.redact_text(original)
            self.assertEqual(redacted, expected, f"Failed for {original}")
            redacted_twice = redaction.redact_text(redacted)
            self.assertEqual(redacted_twice, redacted, f"Not idempotent for {original}")

    def test_pretty_printed_js_and_container_fail_closed(self):
        # 1. Pretty printed JS with authorization and cookie
        js_auth = '<script>{\n  authorization:"AUTH_SECRET",safe:"KEEP_ME"\n}</script>'
        red_auth = redaction.redact_text(js_auth)
        self.assertNotIn("AUTH_SECRET", red_auth)
        self.assertIn("KEEP_ME", red_auth)
        self.assertIn('authorization:"[REDACTED]"', red_auth)

        js_cookie = '<script>{\n  cookie:"COOKIE_SECRET",safe:"KEEP_ME"\n}</script>'
        red_cookie = redaction.redact_text(js_cookie)
        self.assertNotIn("COOKIE_SECRET", red_cookie)
        self.assertIn("KEEP_ME", red_cookie)
        self.assertIn('cookie:"[REDACTED]"', red_cookie)

        # 2. Container values fail-closed completely to [REDACTED]
        self.assertEqual(
            redaction.redact_text('{"authorization":["Basic ARRAY_BASIC_SECRET"],"safe":1}'),
            "[REDACTED]",
        )
        self.assertEqual(
            redaction.redact_text('{"accessToken":["ARRAY_TOKEN_SECRET"],"safe":1}'),
            "[REDACTED]",
        )
        self.assertEqual(
            redaction.redact_text('{"token":{"nested":"OBJECT_TOKEN_SECRET"},"safe":1}'),
            "[REDACTED]",
        )

    def test_mixed_alternating_encoding_redaction(self):
        def hp(value: str) -> str:
            return value.replace("%", "&#37;")

        # Mixed PoC from section 2
        mixed = hp(
            urllib.parse.quote(
                hp("%74oken=MIXED_PERSIST_SECRET"),
                safe="",
            )
        )
        url = f"https://x/?{mixed}&safe=1"
        res = redaction.redact_text(url)
        self.assertNotIn("MIXED_PERSIST_SECRET", res)

        # Alternating H/P/H and P/H/P for layers 3, 9, 17, 65, 256
        for layers in (3, 9, 17, 65, 256):
            curr = f"%74oken=ALT_SECRET_{layers}"
            for i in range(layers):
                if i % 2 == 0:
                    curr = urllib.parse.quote(curr, safe="")
                else:
                    curr = html.escape(curr)
            res_alt = redaction.redact_text(f"https://x.test/?{curr}&safe=1")
            self.assertNotIn(f"ALT_SECRET_{layers}", res_alt)

        # Config secret with '/' and '?' characters
        alt_secret = "CONF/ALT?MIXED_CONFIG_SECRET"
        with patch.object(config, "BRIGHTDATA_API_KEY", alt_secret):
            enc_sec = alt_secret
            for i in range(5):
                enc_sec = urllib.parse.quote(enc_sec, safe="") if i % 2 == 0 else html.escape(enc_sec)
            res_sec = redaction.redact_known_values(f"url=https://x.test/?data={enc_sec}&safe=1")
            self.assertNotIn("CONF/ALT?MIXED_CONFIG_SECRET", res_sec)
            self.assertNotIn("CONF%2FALT%3FMIXED_CONFIG_SECRET", res_sec)

    def test_sanitize_depth_and_cycle_prevention(self):
        # 1. Recursive cycle detection
        cyclic_dict: dict = {"a": 1}
        cyclic_dict["self"] = cyclic_dict
        sanitized_cycle = redaction.sanitize(cyclic_dict)
        self.assertEqual(sanitized_cycle["self"], "[REDACTED]")

        # 2. Deep nested list exceeding MAX_SANITIZE_DEPTH (64)
        deep_list: list = ["NESTED_SECRET_BOTTOM"]
        for _ in range(100):
            deep_list = [deep_list]
        sanitized_deep = redaction.sanitize(deep_list)
        # Deepest accessible node should be replaced with [REDACTED]
        curr = sanitized_deep
        while isinstance(curr, list) and curr:
            curr = curr[0]
        self.assertEqual(curr, "[REDACTED]")

    def test_cache_store_deep_nested_and_cyclic_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            namespace = "test_deep"
            key = "query:deep_nested"
            replay_snapshot.reset()

            comp_path = cache_store._path(cache_dir, namespace, key, compressed=True)
            comp_path.parent.mkdir(parents=True, exist_ok=True)

            # 500-level nested list in value
            deep_val: list = ["NESTED_CACHE_SECRET"]
            for _ in range(500):
                deep_val = [deep_val]

            payload = {
                "schema_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "value": deep_val,
            }
            with gzip.open(comp_path, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle)

            loaded = cache_store.load(cache_dir, namespace, key, ttl_days=30, schema_version=1)
            self.assertIsNotNone(loaded)
            # Inspect file on disk to ensure NO sentinel remains
            with gzip.open(comp_path, "rt", encoding="utf-8") as handle:
                disk_text = handle.read()
            self.assertNotIn("NESTED_CACHE_SECRET", disk_text)

            # Second load must be stable and not crash
            loaded2 = cache_store.load(cache_dir, namespace, key, ttl_days=30, schema_version=1)
            self.assertIsNotNone(loaded2)

    def test_sensitive_key_retention_and_filtering(self):
        payload = {
            "independence_key": "valid_independence_proof_123",
            "cache_key": "valid_cache_hash_456",
            "api_key": "VERYSECRET_API_KEY",
            "secret_key": "VERYSECRET_KEY",
            "token": "VERYSECRET_TOKEN",
            "custom_secret": "VERYSECRET_CUSTOM",
            "brandfetch_client_id": "VERYSECRET_CLIENT_ID",
            "google_places_credential": "VERYSECRET_CREDENTIAL",
            "cookie": "VERYSECRET_COOKIE",
            "proxy_authorization": "VERYSECRET_PROXY",
            "private_key": "VERYSECRET_PRIV_KEY",
            "sub_dict": {
                "independence_key": "nested_proof",
                "client_secret": "VERYSECRET_CLIENT",
            },
        }
        sanitized = redaction.sanitize(payload)
        self.assertIn("independence_key", sanitized)
        self.assertEqual(sanitized["independence_key"], "valid_independence_proof_123")
        self.assertIn("cache_key", sanitized)
        self.assertEqual(sanitized["cache_key"], "valid_cache_hash_456")
        self.assertEqual(sanitized["sub_dict"]["independence_key"], "nested_proof")
        self.assertNotIn("api_key", sanitized)
        self.assertNotIn("secret_key", sanitized)
        self.assertNotIn("token", sanitized)
        self.assertNotIn("custom_secret", sanitized)
        self.assertNotIn("brandfetch_client_id", sanitized)
        self.assertNotIn("google_places_credential", sanitized)
        self.assertNotIn("cookie", sanitized)
        self.assertNotIn("proxy_authorization", sanitized)
        self.assertNotIn("private_key", sanitized)
        self.assertNotIn("client_secret", sanitized["sub_dict"])

    def test_central_logging_formatter_all_headers_and_tracebacks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "run.log"
            fake_key = "FAKE_SUPER_SECRET_KEY_789"
            with patch.object(config, "LOG_FILE", str(log_file)), patch.object(
                config, "BRIGHTDATA_API_KEY", fake_key
            ):
                logger = utils.setup_logging()
                logger.info("Connecting with Authorization: Digest username=\"user1\", nonce=\"NONCE_123\", response=\"RESP_456\"")
                logger.info("Proxy connection: Proxy-Authorization: Negotiate NEGOTIATE_TOKEN_789")
                logger.info("AWS request: Authorization: AWS4-HMAC-SHA256 Credential=AKIA_MOCK_CRED/2026, SignedHeaders=host, Signature=AWS_SIG_SECRET")
                logger.info("Session header: Cookie: session_id=COOKIE_SESS_123; user_auth=USER_AUTH_456")
                logger.info("Response header: Set-Cookie: tracking_token=SET_COOKIE_SECRET; Path=/; Secure")
                try:
                    raise ValueError(f"Traceback error with Authorization: Bearer {fake_key}")
                except ValueError:
                    logger.exception("Failed request with exception")

                utils.close_logging()
                log_content = log_file.read_text(encoding="utf-8")

                # Verify NO credential sentinels exist in log file
                self.assertNotIn("NONCE_123", log_content)
                self.assertNotIn("RESP_456", log_content)
                self.assertNotIn("NEGOTIATE_TOKEN_789", log_content)
                self.assertNotIn("AWS_SIG_SECRET", log_content)
                self.assertNotIn("COOKIE_SESS_123", log_content)
                self.assertNotIn("USER_AUTH_456", log_content)
                self.assertNotIn("SET_COOKIE_SECRET", log_content)
                self.assertNotIn(fake_key, log_content)
                self.assertIn("[REDACTED]", log_content)

    def test_root_logger_propagation_leak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "run.log"
            fake_key = "FAKE_ROOT_LEAK_SECRET_456"
            root_stream = io.StringIO()
            root_handler = logging.StreamHandler(root_stream)
            root_logger = logging.getLogger()
            root_logger.addHandler(root_handler)

            try:
                with patch.object(config, "LOG_FILE", str(log_file)), patch.object(
                    config, "HUNTER_API_KEY", fake_key
                ):
                    app_logger = utils.setup_logging()
                    self.assertFalse(app_logger.propagate)
                    app_logger.info("App logger message with secret: %s", fake_key)
                    try:
                        raise RuntimeError(f"Error containing {fake_key}")
                    except RuntimeError:
                        app_logger.exception("App exception")
                    utils.close_logging()

                    app_log = log_file.read_text(encoding="utf-8")
                    self.assertNotIn(fake_key, app_log)
                    self.assertIn("[REDACTED]", app_log)

                    root_output = root_stream.getvalue()
                    self.assertNotIn(fake_key, root_output)
                    self.assertNotIn("App logger message with secret", root_output)
            finally:
                root_logger.removeHandler(root_handler)
                root_handler.close()
                utils.close_logging()

    def test_replay_snapshot_immutability_and_key_hint_redaction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_file = Path(tmpdir) / "replay_snapshot.json.gz"
            replay_snapshot.reset()
            fake_key = "FAKE_CONFIG_SECRET_XYZ"

            with patch.object(config, "BRIGHTDATA_API_KEY", fake_key):
                # 1. Record with source mutation
                source_dict = {"safe": "value1", "api_key": "RECORD_SECRET"}
                key_with_secret = f"search:https://test.com?token={fake_key}|sub"
                replay_snapshot.record("search", "google", key_with_secret, 1, source_dict)
                source_dict["safe"] = "MUTATED"
                source_dict["api_key"] = "MUTATED_SECRET"

                # 2. Lookup immutability
                hit, val = replay_snapshot.lookup("search", "google", key_with_secret, 1)
                self.assertTrue(hit)
                self.assertEqual(val["safe"], "value1")
                self.assertNotIn("api_key", val)

                val["safe"] = "MUTATED_LOOKUP"
                val["api_key"] = "MUTATION_SECRET"

                hit2, val2 = replay_snapshot.lookup("search", "google", key_with_secret, 1)
                self.assertTrue(hit2)
                self.assertEqual(val2["safe"], "value1")
                self.assertNotIn("api_key", val2)

                # 3. Lookup prefix immutability
                hit_p, val_p = replay_snapshot.lookup_prefix("search", "google", key_with_secret[:key_with_secret.index("|")+1], 1)
                self.assertTrue(hit_p)
                self.assertEqual(val_p["safe"], "value1")
                val_p["safe"] = "MUTATED_PREFIX"

                hit_p2, _ = replay_snapshot.lookup_prefix("search", "google", key_with_secret[:key_with_secret.index("|")+1], 1)
                self.assertTrue(hit_p2)

                # 4. Write snapshot to gzip and inspect raw text
                replay_snapshot.write(snap_file)
                with gzip.open(snap_file, "rt", encoding="utf-8") as handle:
                    raw_snap = handle.read()

                self.assertNotIn(fake_key, raw_snap)
                self.assertNotIn("RECORD_SECRET", raw_snap)
                self.assertNotIn("MUTATION_SECRET", raw_snap)
                self.assertNotIn("MUTATED_SECRET", raw_snap)
                self.assertNotIn("https://test.com", raw_snap)
                self.assertIn('"key_hint":"[REDACTED]"', raw_snap)

                # 5. Load snapshot back and verify integrity
                replay_snapshot.reset()
                meta = replay_snapshot.load(snap_file, max_uncompressed_bytes=10_000_000)
                self.assertEqual(meta["entry_count"], 1)

    def test_cache_store_full_canonicalization_and_corrupt_payload_handling(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            namespace = "test_canon"
            key = "query:canon_company"
            replay_snapshot.reset()

            comp_path = cache_store._path(cache_dir, namespace, key, compressed=True)
            comp_path.parent.mkdir(parents=True, exist_ok=True)

            # 1. Non-dict payload: root string with secret
            with gzip.open(comp_path, "wt", encoding="utf-8") as handle:
                json.dump("https://x/?token=MALFORMED_SECRET", handle)

            replay_snapshot.record("cache", namespace, key, 1, {"safe": "snap_safe"})
            loaded = cache_store.load(cache_dir, namespace, key, ttl_days=30, schema_version=1)
            self.assertEqual(loaded, {"safe": "snap_safe"})
            self.assertFalse(comp_path.exists())

            # 2. Extra top-level secret in payload
            replay_snapshot.reset()
            extra_payload = {
                "schema_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "value": {"safe": "canon_value"},
                "api_key": "TOP_SECRET_EXTRA_KEY",
            }
            with gzip.open(comp_path, "wt", encoding="utf-8") as handle:
                json.dump(extra_payload, handle)

            loaded_extra = cache_store.load(cache_dir, namespace, key, ttl_days=30, schema_version=1)
            self.assertEqual(loaded_extra, {"safe": "canon_value"})
            with gzip.open(comp_path, "rt", encoding="utf-8") as handle:
                disk_text = handle.read()
            self.assertNotIn("TOP_SECRET_EXTRA_KEY", disk_text)
            self.assertNotIn("api_key", disk_text)

            # 3. Corrupt gzip file
            replay_snapshot.reset()
            comp_path.write_bytes(b"corrupt non-gzip data")
            replay_snapshot.record("cache", namespace, key, 1, {"safe": "snap_corrupt_recovery"})
            loaded_corrupt = cache_store.load(cache_dir, namespace, key, ttl_days=30, schema_version=1)
            self.assertEqual(loaded_corrupt, {"safe": "snap_corrupt_recovery"})
            self.assertFalse(comp_path.exists())

    def test_cache_store_expired_and_wrong_schema_migration_sanitization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            namespace = "test_ns2"
            replay_snapshot.reset()

            # 1. Expired cache file
            exp_key = "query:expired_company"
            exp_path = cache_store._path(cache_dir, namespace, exp_key, compressed=True)
            exp_path.parent.mkdir(parents=True, exist_ok=True)
            exp_payload = {
                "schema_version": 1,
                "created_at": "2020-01-01T00:00:00+00:00",
                "value": {
                    "api_key": "EXPIRED_SECRET_111",
                    "safe": "keep",
                },
            }
            with gzip.open(exp_path, "wt", encoding="utf-8") as handle:
                json.dump(exp_payload, handle)

            loaded_exp = cache_store.load(cache_dir, namespace, exp_key, ttl_days=30, schema_version=1, allow_stale=False)
            self.assertIsNone(loaded_exp)
            with gzip.open(exp_path, "rt", encoding="utf-8") as handle:
                disk_exp = json.load(handle)
            self.assertNotIn("api_key", disk_exp["value"])
            self.assertEqual(disk_exp["created_at"], "2020-01-01T00:00:00+00:00")
            self.assertEqual(disk_exp["value"]["safe"], "keep")

            # 2. Wrong schema cache file
            wrong_key = "query:wrong_schema_company"
            wrong_path = cache_store._path(cache_dir, namespace, wrong_key, compressed=True)
            wrong_payload = {
                "schema_version": 99,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "value": {
                    "api_key": "WRONG_SCHEMA_SECRET_222",
                    "safe": "keep_wrong",
                },
            }
            with gzip.open(wrong_path, "wt", encoding="utf-8") as handle:
                json.dump(wrong_payload, handle)

            loaded_wrong = cache_store.load(cache_dir, namespace, wrong_key, ttl_days=30, schema_version=1)
            self.assertIsNone(loaded_wrong)
            with gzip.open(wrong_path, "rt", encoding="utf-8") as handle:
                disk_wrong = json.load(handle)
            self.assertNotIn("api_key", disk_wrong["value"])
            self.assertEqual(disk_wrong["schema_version"], 99)
            self.assertEqual(disk_wrong["value"]["safe"], "keep_wrong")

    def test_cache_store_sibling_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            namespace = "test_siblings"
            key = "query:sibling_company"

            comp_path = cache_store._path(cache_dir, namespace, key, compressed=True)
            plain_path = cache_store._path(cache_dir, namespace, key, compressed=False)
            comp_path.parent.mkdir(parents=True, exist_ok=True)

            cache_store.save(cache_dir, namespace, key, {"safe": "value"}, schema_version=1)
            plain_path.write_text("dummy_stale_plaintext", encoding="utf-8")
            self.assertTrue(comp_path.exists())
            self.assertTrue(plain_path.exists())

            replay_snapshot.reset()

            loaded = cache_store.load(cache_dir, namespace, key, ttl_days=30, schema_version=1)
            self.assertIsNotNone(loaded)
            self.assertTrue(comp_path.exists())
            self.assertFalse(plain_path.exists())

            plain_path.write_text("dummy_stale_plaintext2", encoding="utf-8")
            cache_store.save(cache_dir, namespace, key, {"safe": "value2"}, schema_version=1)
            self.assertTrue(comp_path.exists())
            self.assertFalse(plain_path.exists())

    def test_quality_audit_and_discovery_coverage_redaction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_file = Path(tmpdir) / "quality_audit.json"
            coverage_file = Path(tmpdir) / "discovery_coverage.json"

            rows = [
                frozen_row({
                    "company": "Audit Corp",
                    "status": "OK_HIGH_CONFIDENCE",
                    "publication_eligible": True,
                    "independence_key": "valid_indep",
                    "api_key": "SECRET_AUDIT_KEY",
                    "email_source_url": "https://audit.com?token=SECRET_AUDIT_TOKEN",
                }, "test:entity-secret", publishable=True)
            ]
            quality_audit.write(audit_file, rows)
            audit_text = audit_file.read_text(encoding="utf-8")
            self.assertNotIn("SECRET_AUDIT_KEY", audit_text)
            self.assertNotIn("SECRET_AUDIT_TOKEN", audit_text)

            discovery_coverage.record_query(
                "Audit Corp",
                "https://example.com/search?token=SECRET_COV_TOKEN",
                "primary",
                "cache_hit",
                1,
                {"no_candidates"},
            )
            discovery_coverage.write(coverage_file)
            cov_text = coverage_file.read_text(encoding="utf-8")
            self.assertNotIn("SECRET_COV_TOKEN", cov_text)
            self.assertIn("[REDACTED]", cov_text)

    def test_brandfetch_and_hunter_error_redaction(self):
        fake_secret = "VERYSECRET_12345"
        with patch.object(config, "BRANDFETCH_CLIENT_ID", fake_secret), patch.object(
            config, "HUNTER_API_KEY", fake_secret
        ):
            exc = RuntimeError(f"HTTP 401 Client Error for https://api.brandfetch.io/v2/search/test?c={fake_secret}")
            safe_msg = company_resolvers._safe_request_error(exc)
            self.assertNotIn(fake_secret, safe_msg)
            self.assertIn("[REDACTED]", safe_msg)

    def test_excel_cell_value_redaction_and_formula_protection(self):
        secret_url = "https://example.com/api?token=VERYSECRET_URL_TOKEN&sig=VERYSECRET_SIG"
        redacted = excel._safe_cell_value(secret_url)
        self.assertNotIn("VERYSECRET_URL_TOKEN", redacted)
        self.assertNotIn("VERYSECRET_SIG", redacted)
        self.assertIn("[REDACTED]", redacted)

        enc_url = "https://example.com/api%3Ftoken%3DVERYSECRET_ENC%26sig%3DVERYSECRET_SIG_ENC"
        redacted_enc = excel._safe_cell_value(enc_url)
        self.assertNotIn("VERYSECRET_ENC", redacted_enc)
        self.assertNotIn("VERYSECRET_SIG_ENC", redacted_enc)
        self.assertIn("[REDACTED]", redacted_enc)

        formula_val = "=1+1"
        escaped_formula = excel._safe_cell_value(formula_val)
        self.assertEqual(escaped_formula, "'=1+1")
        self.assertEqual(excel._unescape_cell_value(escaped_formula), "=1+1")

        combo = "=SUM(1,2)?token=VERYSECRET_COMBO"
        escaped_combo = excel._safe_cell_value(combo)
        self.assertNotIn("VERYSECRET_COMBO", escaped_combo)
        self.assertTrue(escaped_combo.startswith("'"))

    def test_excel_persisted_artifact_secret_redaction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "contacts.xlsx"
            rows = [
                frozen_row({
                    "company": "Secret Corp",
                    "status": "OK_HIGH_CONFIDENCE",
                    "publication_eligible": True,
                    "website": "https://secretcorp.com",
                    "email": "info@secretcorp.com",
                    "email_source_url": "https://secretcorp.com/contact?token=VERYSECRET_EMAIL_TOKEN",
                    "phone": "+902123334455",
                    "phone_source_url": "https://secretcorp.com/phone%3Fapi_key%3DVERYSECRET_API_KEY",
                }, "test:audit-secret", publishable=True)
            ]
            excel.write_contacts(file_path, rows)
            read_back = excel._read_rows(file_path)
            content_dump = str(read_back)
            self.assertNotIn("VERYSECRET_EMAIL_TOKEN", content_dump)
            self.assertNotIn("VERYSECRET_API_KEY", content_dump)
            self.assertIn("[REDACTED]", content_dump)

    def test_entity_relationships_persisted_secret_redaction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "entity_relationships.jsonl"
            rows = [
                frozen_row({
                    "company": "Entity Corp",
                    "status": "OK_HIGH_CONFIDENCE",
                    "publication_eligible": True,
                    "website": "https://entitycorp.com",
                    "website_source": "https://source.com?token=VERYSECRET_SOURCE_TOKEN",
                    "__evaluation": {
                        "candidate": {
                            "_profile_url": "https://profile.com%3Faccess_token%3DVERYSECRET_PROFILE_TOKEN",
                        }
                    },
                }, "test:entity-registry-secret", publishable=True)
            ]
            entity_registry.write_observations(file_path, rows)
            raw_text = file_path.read_text(encoding="utf-8")
            self.assertNotIn("VERYSECRET_SOURCE_TOKEN", raw_text)
            self.assertNotIn("VERYSECRET_PROFILE_TOKEN", raw_text)
            self.assertIn("[REDACTED]", raw_text)

    def test_checkpoint_sqlite_payload_redaction_and_resume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = Path(tmpdir) / "progress.db"
            marker_file = Path(tmpdir) / "progress.json"
            input_file = Path(tmpdir) / "firms.xlsx"
            input_file.write_text("dummy", encoding="utf-8")

            with patch.object(config, "PROGRESS_DB_FILE", db_file), patch.object(
                config, "PROGRESS_FILE", marker_file
            ):
                row = {
                    "company": "Checkpoint Corp",
                    "status": "OK_HIGH_CONFIDENCE",
                    "publication_eligible": True,
                    "independence_key": "checkpoint_indep_key",
                    "website": "https://checkpoint.com?token=VERYSECRET_CHECKPOINT_TOKEN",
                    "details": {
                        "auth_header": "Bearer VERYSECRET_BEARER_TOKEN",
                        "nested": [
                            {
                                "secret_key": "VERYSECRET_NESTED_KEY",
                                "url": "https://api.com%26sig%3DVERYSECRET_SIG",
                            }
                        ],
                    },
                }
                checkpoint.save_result(input_file, 0, row, run_signature="sig123")

                conn = sqlite3.connect(db_file)
                cursor = conn.cursor()
                payload_str = cursor.execute("SELECT payload FROM results WHERE item_index=0").fetchone()[0]
                conn.close()

                self.assertNotIn("VERYSECRET_CHECKPOINT_TOKEN", payload_str)
                self.assertNotIn("VERYSECRET_BEARER_TOKEN", payload_str)
                self.assertNotIn("VERYSECRET_NESTED_KEY", payload_str)
                self.assertNotIn("VERYSECRET_SIG", payload_str)
                self.assertIn("[REDACTED]", payload_str)
                self.assertIn("checkpoint_indep_key", payload_str)

                loaded = checkpoint.load_progress(input_file, run_signature="sig123")
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["last_completed_index"], 0)
                self.assertEqual(len(loaded["results_so_far"]), 1)
                self.assertEqual(loaded["results_so_far"][0]["company"], "Checkpoint Corp")
                self.assertEqual(loaded["results_so_far"][0]["independence_key"], "checkpoint_indep_key")


if __name__ == "__main__":
    unittest.main()
