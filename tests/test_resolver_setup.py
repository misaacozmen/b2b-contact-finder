import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import main
import setup_company_resolvers


class ResolverSetupTests(unittest.TestCase):
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
