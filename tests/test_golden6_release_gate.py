import copy
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import evaluate_release_gate
from tools.evaluate_release_gate import evaluate_evidence


def _valid_evidence() -> dict:
    return {
        "behavioral": {
            "package_valid": True,
            "replay_miss_count": 0,
            "replay_network_events": 0,
            "provider_http_calls": 0,
            "provider_call_count": 0,
            "free_only_config_valid": True,
            "all_results_unique_ids": 20,
            "expected_all_results_order_match": True,
            "actual_is_subset": True,
            "validator_status": "PASS",
            "issues": [],
            "positive_denominators": True,
            "live_replay_metrics_equal": True,
            "live_artifact_hash": "artifact",
            "offline_artifact_hash": "artifact",
        },
        "checks": {
            "js_default_unset_true": True,
            "browser_smoke": True,
            "command_results": {
                name: {"returncode": 0}
                for name in ("compileall", "pytest", "benchmark", "pip_check", "help", "diff_check")
            },
            "offline_test_env_used": True,
            "offline_test_env_removed": True,
            "tests_intact": True,
            "tracked_clean": True,
            "feature_remote_exact": True,
            "ci_token_masked": True,
            "ci": {"head_sha": "head", "jobs_success": True},
        },
        "git": {
            "branch": "codex/release-hardening",
            "head_sha": "head",
            "origin_feature_sha": "head",
            "gate_base_sha": "base",
            "origin_main_sha": "base",
            "origin_main_ancestor": True,
            "no_divergence_or_force": True,
        },
    }


class Golden6ReleaseGateTests(unittest.TestCase):
    def test_valid_evidence_sets_all_required_flags(self):
        result = evaluate_evidence(_valid_evidence())
        self.assertTrue(result["behavioral_recall_validated"])
        self.assertTrue(result["release_ready"])
        self.assertTrue(result["merge_allowed"])
        self.assertEqual(result["failure_reasons"], [])

    def test_missing_id_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["all_results_unique_ids"] = 19
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_bad_package_hash_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["package_valid"] = False
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_replay_miss_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["replay_miss_count"] = 1
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_network_event_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["replay_network_events"] = 1
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_zero_denominator_blocks_all_flags(self):
        evidence = _valid_evidence()
        evidence["behavioral"]["positive_denominators"] = False
        result = evaluate_evidence(evidence)
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_wrong_ci_head_or_job_blocks_release_and_merge(self):
        evidence = _valid_evidence()
        evidence["checks"]["ci"]["head_sha"] = "other"
        result = evaluate_evidence(evidence)
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

        evidence = _valid_evidence()
        evidence["checks"]["ci"]["jobs_success"] = False
        result = evaluate_evidence(evidence)
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_advanced_or_diverged_main_blocks_merge(self):
        evidence = _valid_evidence()
        evidence["git"]["origin_main_ancestor"] = False
        result = evaluate_evidence(evidence)
        self.assertTrue(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

        evidence = _valid_evidence()
        evidence["git"]["no_divergence_or_force"] = False
        result = evaluate_evidence(evidence)
        self.assertTrue(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_manual_flag_injection_is_ignored(self):
        evidence = _valid_evidence()
        evidence.update({
            "behavioral_recall_validated": True,
            "release_ready": True,
            "merge_allowed": True,
        })
        evidence["behavioral"]["package_valid"] = False
        result = evaluate_evidence(copy.deepcopy(evidence))
        self.assertFalse(result["behavioral_recall_validated"])
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["merge_allowed"])

    def test_ci_auth_falls_back_to_git_credential_without_exposing_secret(self):
        token = "credential-token-not-evidence"
        gh = subprocess.CompletedProcess(["gh"], 1, "", "")
        credential = subprocess.CompletedProcess(
            ["git"], 0, f"protocol=https\nhost=github.com\nusername=user\npassword={token}\n", "",
        )
        with patch.dict(evaluate_release_gate.os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}, clear=False), patch.object(
            evaluate_release_gate.subprocess, "run", side_effect=[gh, credential],
        ) as run:
            resolved, method = evaluate_release_gate._ci_auth_token(Path.cwd())
        self.assertEqual(resolved, token)
        self.assertEqual(method, "git_credential")
        self.assertNotIn(token, repr(run.call_args_list))


if __name__ == "__main__":
    unittest.main()
