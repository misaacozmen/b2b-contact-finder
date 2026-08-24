import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import main
import setup_company_resolvers


class ResolverSetupTests(unittest.TestCase):
    def test_cli_help_exits_without_starting_interactive_setup(self):
        with patch("sys.stdout", new=io.StringIO()), patch.object(
            setup_company_resolvers, "configure"
        ) as configure, self.assertRaises(SystemExit) as raised:
            setup_company_resolvers.cli(["--help"])
        self.assertEqual(raised.exception.code, 0)
        configure.assert_not_called()

    def test_setup_persists_keys_encrypted_and_enables_both_resolvers(self):
        answers = iter(["y", "y"])
        secrets = iter(["brand-client-id", "hunter-secret"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keys_file = root / "api_keys.json"
            settings_file = root / "company_resolvers.json"
            with patch.object(config, "SAVED_API_KEYS_FILE", keys_file), patch.object(
                config, "RESOLVER_SETTINGS_FILE", settings_file,
            ), patch.object(config, "BRANDFETCH_CLIENT_ID", ""), patch.object(
                config, "HUNTER_API_KEY", "",
            ), patch.object(config, "ENABLE_BRANDFETCH_DOMAIN_SEARCH", False), patch.object(
                config, "ENABLE_HUNTER_DOMAIN_FINDER", False,
            ):
                states = setup_company_resolvers.configure(
                    input_fn=lambda _prompt: next(answers),
                    secret_fn=lambda _prompt: next(secrets),
                )
                self.assertEqual(states, {
                    "brandfetch_domain_search": True,
                    "hunter_domain_finder": True,
                })
                self.assertTrue(config.ENABLE_BRANDFETCH_DOMAIN_SEARCH)
                self.assertTrue(config.ENABLE_HUNTER_DOMAIN_FINDER)
                self.assertEqual(main._load_saved_api_keys()["brandfetch"], "brand-client-id")
                self.assertEqual(main._load_saved_api_keys()["hunter"], "hunter-secret")
                raw = keys_file.read_text(encoding="utf-8")
                self.assertNotIn("brand-client-id", raw)
                self.assertNotIn("hunter-secret", raw)
                settings = json.loads(settings_file.read_text(encoding="utf-8"))
                self.assertTrue(settings["brandfetch_domain_search"])
                self.assertTrue(settings["hunter_domain_finder"])

    def test_save_api_keys_fails_closed_when_encryption_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            keys_file = Path(directory) / "api_keys.json"
            keys_file.write_text('{"encrypted":"existing"}', encoding="utf-8")
            with patch.object(config, "SAVED_API_KEYS_FILE", keys_file), patch.object(
                main.secrets_store, "encode", side_effect=OSError("DPAPI unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "DPAPI unavailable"):
                    main._save_api_keys({"hunter": "plaintext-secret"})
            self.assertEqual(
                keys_file.read_text(encoding="utf-8"),
                '{"encrypted":"existing"}',
            )
            self.assertFalse(any(Path(directory).glob("*.tmp")))

    def test_save_api_keys_replaces_from_same_directory_temporary_file(self):
        replace_calls = []
        original_replace = Path.replace

        def tracked_replace(source, target):
            replace_calls.append((source, Path(target)))
            return original_replace(source, target)

        with tempfile.TemporaryDirectory() as directory:
            keys_file = Path(directory) / "api_keys.json"
            encrypted = {
                "version": 1,
                "storage": "windows_dpapi_user",
                "encrypted": "ciphertext",
            }
            with patch.object(config, "SAVED_API_KEYS_FILE", keys_file), patch.object(
                main.secrets_store, "encode", return_value=encrypted,
            ), patch.object(Path, "replace", tracked_replace):
                main._save_api_keys({"hunter": "plaintext-secret"})
            self.assertEqual(json.loads(keys_file.read_text(encoding="utf-8")), encrypted)
            self.assertEqual(len(replace_calls), 1)
            source, target = replace_calls[0]
            self.assertEqual(source.parent, keys_file.parent)
            self.assertEqual(target, keys_file)
            self.assertFalse(source.exists())

    def test_saved_resolver_configuration_never_enables_without_key(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "company_resolvers.json"
            settings_file.write_text(json.dumps({
                "brandfetch_domain_search": True,
                "hunter_domain_finder": True,
            }), encoding="utf-8")
            with patch.object(config, "RESOLVER_SETTINGS_FILE", settings_file), patch.object(
                config, "BRANDFETCH_CLIENT_ID", "",
            ), patch.object(config, "HUNTER_API_KEY", ""), patch.object(
                config, "ENABLE_BRANDFETCH_DOMAIN_SEARCH", False,
            ), patch.object(config, "ENABLE_HUNTER_DOMAIN_FINDER", False):
                states = main._apply_saved_resolver_configuration({})
                self.assertEqual(states, {
                    "brandfetch_domain_search": False,
                    "hunter_domain_finder": False,
                })


if __name__ == "__main__":
    unittest.main()
