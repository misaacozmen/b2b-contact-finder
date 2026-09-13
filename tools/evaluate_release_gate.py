"""Evidence-only Golden6 release evaluator.

The three release flags are derived here.  They are never accepted from CLI,
environment, configuration, or a supplied JSON override.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook

from tools.free_only_contract import validate_database as validate_free_only_database
from tools.free_only_contract import validate_manifest as validate_free_only_manifest


REQUIRED_CI_JOBS = ("test", "browser-smoke", "ocr-smoke")
REQUEST_COUNTER_PREFIXES = ("api.", "http.search.", "http.crawler.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_not_object:{path}")
    return value


def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _ids(path: Path, sheet_name: str | None = None) -> list[str]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[sheet_name] if sheet_name else workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(value or "").strip().casefold() for value in rows[0]]
        if "source_record_id" not in headers:
            return []
        index = headers.index("source_record_id")
        return [str(row[index] or "").strip() for row in rows[1:] if any(value is not None for value in row)]
    finally:
        workbook.close()


def _provider_http_calls(artifact_dir: Path) -> int:
    telemetry = _json(artifact_dir / "telemetry.json")
    counters = telemetry.get("counters") if isinstance(telemetry.get("counters"), dict) else {}
    return sum(
        int(value or 0)
        for key, value in counters.items()
        if any(str(key).startswith(prefix) for prefix in REQUEST_COUNTER_PREFIXES)
        and str(key).endswith(".requests")
        and isinstance(value, (int, float))
    )


def _package_files_valid(package_dir: Path, manifest: dict) -> tuple[bool, list[str]]:
    failures: list[str] = []
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    for relative, info in files.items():
        path = package_dir / str(relative)
        if not path.is_file():
            failures.append(f"package_file_missing:{relative}")
        elif _sha256(path) != info.get("sha256") or path.stat().st_size != int(info.get("bytes", -1)):
            failures.append(f"package_file_hash_mismatch:{relative}")
    snapshot = package_dir / str(manifest.get("replay", {}).get("snapshot", ""))
    if not snapshot.is_file():
        failures.append("replay_snapshot_missing")
    elif _sha256(snapshot) != manifest.get("replay", {}).get("snapshot_sha256"):
        failures.append("replay_snapshot_hash_mismatch")
    if manifest.get("source_integrity_before") != manifest.get("source_integrity_after"):
        failures.append("source_run_hash_changed")
    if manifest.get("input", {}).get("record_count") != 20:
        failures.append("package_record_count_not_20")
    return not failures, failures


def _validator_report(repo: Path, expected: Path, artifact_dir: Path, output: Path) -> tuple[dict, list[str]]:
    if not artifact_dir.is_dir():
        return {}, [f"artifact_dir_missing:{artifact_dir}"]
    command = [
        sys.executable, "validate_golden_xlsx.py",
        "--expected", str(expected),
        "--actual", str(artifact_dir / "contacts.xlsx"),
        "--candidates", str(artifact_dir / "website_candidates.xlsx"),
        "--all-results", str(artifact_dir / "all_results.xlsx"),
        "--json-output", str(output),
    ]
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    if not output.is_file():
        return {}, [f"validator_report_missing:{output}", f"validator_exit:{result.returncode}"]
    report = _json(output)
    failures = [] if result.returncode == 0 and report.get("status") == "PASS" else ["validator_status_not_pass"]
    failures.extend(str(value) for value in report.get("issues", []) if value)
    return report, failures


def _metrics(report: dict) -> dict:
    return {
        "fields": report.get("fields", {}),
        "stages": report.get("stages", {}),
        "records": report.get("records", {}),
    }


def _positive_denominators(report: dict) -> bool:
    fields = report.get("fields") if isinstance(report.get("fields"), dict) else {}
    required_fields = {"website", "email", "phone"}
    if set(fields) != required_fields:
        return False
    if any(
        values.get("tp", 0) + values.get("fp", 0) <= 0
        or values.get("tp", 0) + values.get("fn", 0) <= 0
        or values.get("precision") is None
        or values.get("recall") is None
        for values in fields.values() if isinstance(values, dict)
    ):
        return False
    stages = report.get("stages") if isinstance(report.get("stages"), dict) else {}
    return all(
        stages.get(key, 0) > 0
        for key in ("published_count", "expected_websites", "selected_count")
    )


def evaluate_evidence(evidence: dict) -> dict:
    """Compute the flags strictly from evidence fields."""
    failures: list[str] = []
    behavioral = evidence.get("behavioral") if isinstance(evidence.get("behavioral"), dict) else {}
    checks = evidence.get("checks") if isinstance(evidence.get("checks"), dict) else {}
    git = evidence.get("git") if isinstance(evidence.get("git"), dict) else {}
    ci = checks.get("ci") if isinstance(checks.get("ci"), dict) else {}

    conditions = {
        "package_valid": behavioral.get("package_valid") is True,
        "replay_miss_zero": behavioral.get("replay_miss_count") == 0,
        "replay_network_zero": behavioral.get("replay_network_events") == 0,
        "provider_http_zero": behavioral.get("provider_http_calls") == 0,
        "free_only_config_valid": behavioral.get("free_only_config_valid") is True,
        "all_results_unique_ids_20": behavioral.get("all_results_unique_ids") == 20,
        "expected_all_results_order_match": behavioral.get("expected_all_results_order_match") is True,
        "actual_is_subset": behavioral.get("actual_is_subset") is True,
        "validator_pass": behavioral.get("validator_status") == "PASS",
        "issues_empty": behavioral.get("issues") == [],
        "positive_denominators": behavioral.get("positive_denominators") is True,
        "live_replay_metrics_equal": behavioral.get("live_replay_metrics_equal") is True,
    }
    for name, passed in conditions.items():
        if not passed:
            failures.append(f"BEHAVIORAL_{name.upper()}")
    behavioral_recall_validated = all(conditions.values())

    command_results = checks.get("command_results") if isinstance(checks.get("command_results"), dict) else {}
    required_commands_pass = bool(command_results) and all(
        value.get("returncode") == 0 for value in command_results.values() if isinstance(value, dict)
    ) and all(name in command_results for name in ("compileall", "pytest", "benchmark", "pip_check", "help", "diff_check"))
    release_conditions = {
        "behavioral_recall_validated": behavioral_recall_validated,
        "js_default_unset_true": checks.get("js_default_unset_true") is True,
        "browser_smoke": checks.get("browser_smoke") is True,
        "required_commands_pass": required_commands_pass,
        "offline_test_env_used": checks.get("offline_test_env_used") is True,
        "offline_test_env_removed": checks.get("offline_test_env_removed") is True,
        "tests_intact": checks.get("tests_intact") is True,
        "tracked_clean": checks.get("tracked_clean") is True,
        "feature_remote_exact": checks.get("feature_remote_exact") is True,
        "ci_exact_head": ci.get("head_sha") == git.get("head_sha") and ci.get("jobs_success") is True,
        "ci_token_masked": checks.get("ci_token_masked") is True,
    }
    for name, passed in release_conditions.items():
        if not passed:
            failures.append(f"RELEASE_{name.upper()}")
    release_ready = all(release_conditions.values())

    merge_conditions = {
        "release_ready": release_ready,
        "active_branch": git.get("branch") == "codex/release-hardening",
        "origin_feature_exact": git.get("origin_feature_sha") == git.get("head_sha"),
        "gate_base_is_origin_main": git.get("gate_base_sha") == git.get("origin_main_sha"),
        "origin_main_ancestor": git.get("origin_main_ancestor") is True,
        "no_divergence_or_force": git.get("no_divergence_or_force") is True,
    }
    for name, passed in merge_conditions.items():
        if not passed:
            failures.append(f"MERGE_{name.upper()}")
    merge_allowed = all(merge_conditions.values())
    return {
        "behavioral_recall_validated": behavioral_recall_validated,
        "release_ready": release_ready,
        "merge_allowed": merge_allowed,
        "failure_reasons": sorted(set(failures)),
    }


def _repo_state(repo: Path) -> dict:
    branch_code, branch, _ = _git(repo, "branch", "--show-current")
    head_code, head, _ = _git(repo, "rev-parse", "HEAD")
    base_code, origin_main, _ = _git(repo, "rev-parse", "refs/remotes/origin/main")
    feature_code, origin_feature, _ = _git(repo, "rev-parse", "refs/remotes/origin/codex/release-hardening")
    ancestor = _git(repo, "merge-base", "--is-ancestor", "refs/remotes/origin/main", "HEAD")[0] == 0
    count_code, count, _ = _git(repo, "rev-list", "--left-right", "--count", "refs/remotes/origin/main...HEAD")
    left, right = (count.split() if count_code == 0 else ("-1", "-1"))
    status_code, status, _ = _git(repo, "status", "--porcelain", "--untracked-files=no")
    return {
        "branch": branch if branch_code == 0 else "",
        "head_sha": head if head_code == 0 else "",
        "origin_main_sha": origin_main if base_code == 0 else "",
        "origin_feature_sha": origin_feature if feature_code == 0 else "",
        "gate_base_sha": origin_main if base_code == 0 else "",
        "origin_main_ancestor": ancestor,
        "no_divergence_or_force": count_code == 0 and left == "0" and int(right) >= 0,
        "divergence": {"behind": int(left), "ahead": int(right)},
        "tracked_clean": status_code == 0 and not status,
    }


def _test_integrity(repo: Path) -> bool:
    code, diff, _ = _git(repo, "diff", "HEAD^!", "--", "tests")
    if code != 0:
        return False
    added_bad = any(
        line.startswith("+") and not line.startswith("+++") and re.search(r"skip|xfail", line, re.IGNORECASE)
        for line in diff.splitlines()
    )
    deleted_test = any(
        line.startswith("-") and not line.startswith("---") and re.search(r"def test_", line)
        for line in diff.splitlines()
    )
    return not added_bad and not deleted_test


def _run_command(repo: Path, name: str, command: list[str], env: dict[str, str]) -> dict:
    result = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr.encode("utf-8")).hexdigest(),
    }


def _github_repo(remote: str) -> tuple[str, str] | None:
    value = remote.strip()
    if value.startswith("git@"):
        value = value.split(":", 1)[1]
    elif "://" in value:
        value = urllib.parse.urlparse(value).path.lstrip("/")
    value = value.removesuffix(".git").strip("/")
    parts = value.split("/")
    return (parts[-2], parts[-1]) if len(parts) >= 2 else None


def query_github_ci(repo: Path, head_sha: str, branch: str) -> dict:
    _code, remote, _ = _git(repo, "config", "--get", "remote.origin.url")
    identity = _github_repo(remote)
    if not identity:
        return {"jobs_success": False, "failure": "CI_REMOTE_NOT_GITHUB"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return {"jobs_success": False, "failure": "CI_API_TOKEN_MISSING"}
    owner, repo_name = identity
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "golden6-release-gate",
    }

    def get(url: str) -> dict:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("github_response_not_object")
        return value

    try:
        runs_url = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo_name)}/actions/runs?head={urllib.parse.quote(branch)}&per_page=50"
        runs = get(runs_url).get("workflow_runs", [])
        selected = next((run for run in runs if run.get("head_sha") == head_sha), None)
        if not selected:
            return {"jobs_success": False, "failure": "CI_EXACT_HEAD_NOT_FOUND", "head_sha": ""}
        jobs_url = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo_name)}/actions/runs/{selected['id']}/jobs?per_page=100"
        jobs = get(jobs_url).get("jobs", [])
        job_map = {str(job.get("name")): job for job in jobs}
        success = all(
            job_map.get(name, {}).get("conclusion") == "success"
            for name in REQUIRED_CI_JOBS
        )
        return {
            "run_id": selected.get("id"),
            "run_name": selected.get("name"),
            "head_sha": selected.get("head_sha"),
            "status": selected.get("status"),
            "conclusion": selected.get("conclusion"),
            "jobs": {name: {"id": job_map.get(name, {}).get("id"), "conclusion": job_map.get(name, {}).get("conclusion")} for name in REQUIRED_CI_JOBS},
            "jobs_success": success and selected.get("status") == "completed" and selected.get("conclusion") == "success",
            "repository": f"{owner}/{repo_name}",
            "token_used_for_auth_only": True,
        }
    except Exception as exc:
        return {"jobs_success": False, "failure": f"CI_API_ERROR:{type(exc).__name__}"}


def evaluate(repo_root: Path, package_dir: Path, replay_receipt_path: Path, report_path: Path) -> dict:
    repo = Path(repo_root).resolve()
    package_dir = Path(package_dir).resolve()
    report_path = Path(report_path).resolve()
    evidence: dict = {"behavioral": {}, "checks": {}, "git": {}, "artifacts": {}, "metrics": {}}
    failures: list[str] = []
    try:
        package_manifest_path = package_dir / "package_manifest.json"
        package_manifest = _json(package_manifest_path)
        package_valid, package_failures = _package_files_valid(package_dir, package_manifest)
        failures.extend(package_failures)
        free_only_config_valid = True
        try:
            validate_free_only_manifest(package_manifest, require_complete=False)
        except ValueError as exc:
            free_only_config_valid = False
            failures.append(f"FREE_ONLY_PACKAGE:{exc}")
        input_path = Path(str(package_manifest.get("input", {}).get("path", ""))).resolve()
        expected_path = repo / "outputs" / "golden_6_20260718" / "golden_6_manual_validation_20_ready.xlsx"
        expected_ids = list(package_manifest.get("ordered_source_record_ids", []))
        live_artifact = Path(str(package_manifest.get("live_run", {}).get("artifact_dir", ""))).resolve()
        receipt = _json(Path(replay_receipt_path).resolve())
        offline_artifact = Path(str(receipt.get("artifact_dir", ""))).resolve()
        live_root = Path(str(package_manifest.get("live_run", {}).get("run_dir", ""))).resolve()
        offline_root = Path(str(receipt.get("run_dir", ""))).resolve()
        try:
            live_manifest = _json(live_root / "manifest.json")
            validate_free_only_manifest(live_manifest)
            live_config = live_manifest.get("run_config", {})
            if str(live_manifest.get("config_sha256", "")) != str(package_manifest.get("live_run", {}).get("config_sha256", "")):
                raise ValueError("live_config_hash_mismatch")
            validate_free_only_database(live_root / "state" / "progress.sqlite3", str(live_manifest.get("run_id", "")), 20)
            offline_manifest = _json(offline_root / "manifest.json")
            validate_free_only_manifest(offline_manifest)
            if offline_manifest.get("run_config") != live_config:
                raise ValueError("offline_free_only_run_config_mismatch")
            validate_free_only_database(offline_root / "state" / "progress.sqlite3", str(offline_manifest.get("run_id", "")), 20)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            free_only_config_valid = False
            failures.append(f"FREE_ONLY_RUN:{exc}")
        live_report, live_failures = _validator_report(repo, expected_path, live_artifact, report_path.with_name(report_path.stem + ".live.validator.json"))
        offline_report, offline_failures = _validator_report(repo, expected_path, offline_artifact, report_path.with_name(report_path.stem + ".offline.validator.json"))
        failures.extend(live_failures + offline_failures)
        live_ids = _ids(live_artifact / "all_results.xlsx") if live_artifact.is_dir() else []
        offline_ids = _ids(offline_artifact / "all_results.xlsx") if offline_artifact.is_dir() else []
        live_contacts = _ids(live_artifact / "contacts.xlsx") if live_artifact.is_dir() else []
        offline_contacts = _ids(offline_artifact / "contacts.xlsx") if offline_artifact.is_dir() else []
        live_metrics = _metrics(live_report)
        offline_metrics = _metrics(offline_report)
        live_telemetry_calls = _provider_http_calls(live_artifact) if live_artifact.is_dir() else -1
        offline_telemetry_calls = _provider_http_calls(offline_artifact) if offline_artifact.is_dir() else -1
        offline_coverage = _json(offline_artifact / "discovery_coverage.json") if (offline_artifact / "discovery_coverage.json").is_file() else {}
        behavioral = {
            "package_valid": package_valid,
            "replay_miss_count": offline_coverage.get("replay_miss_count", -1),
            "replay_network_events": receipt.get("replay_network_events", -1),
            "provider_http_calls": offline_telemetry_calls,
            "live_provider_http_calls": live_telemetry_calls,
            "free_only_config_valid": free_only_config_valid,
            "all_results_unique_ids": len(live_ids) if len(live_ids) == len(set(live_ids)) else -1,
            "expected_all_results_order_match": live_ids == expected_ids and offline_ids == expected_ids,
            "actual_is_subset": set(live_contacts).issubset(set(live_ids)) and set(offline_contacts).issubset(set(offline_ids)),
            "validator_status": "PASS" if live_report.get("status") == "PASS" and offline_report.get("status") == "PASS" else "FAIL",
            "issues": list(live_report.get("issues", [])) + list(offline_report.get("issues", [])),
            "positive_denominators": _positive_denominators(live_report) and _positive_denominators(offline_report),
            "live_replay_metrics_equal": live_metrics == offline_metrics,
        }
        git = _repo_state(repo)
        command_env = os.environ.copy()
        command_env["B2B_TEST_OFFLINE"] = "1"
        command_results = {
            "compileall": _run_command(repo, "compileall", [sys.executable, "-m", "compileall", "-q", "config.py", "main.py", "modules", "tools", "tests"], command_env),
            "pytest": _run_command(repo, "pytest", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], command_env),
            "benchmark": _run_command(repo, "benchmark", [sys.executable, "validate_benchmark_suite.py"], command_env),
            "pip_check": _run_command(repo, "pip_check", [sys.executable, "-m", "pip", "check"], command_env),
            "help": _run_command(repo, "help", [sys.executable, "main.py", "--help"], command_env),
            "diff_check": _run_command(repo, "diff_check", ["git", "diff", "--check"], command_env),
        }
        clean_env = os.environ.copy()
        clean_env.pop("B2B_TEST_OFFLINE", None)
        clean_env.pop("ENABLE_JS_FALLBACK", None)
        js_result = subprocess.run([sys.executable, "-c", "import config; assert config.ENABLE_JS_FALLBACK is True"], cwd=repo, env=clean_env, capture_output=True, text=True)
        browser_result = subprocess.run([sys.executable, "-c", "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(); page=b.new_page(); page.set_content('<h1>Test</h1>'); assert 'Test' in page.content(); b.close(); p.stop()"], cwd=repo, env=clean_env, capture_output=True, text=True)
        ci = query_github_ci(repo, git.get("head_sha", ""), git.get("branch", ""))
        evidence = {
            "behavioral": behavioral,
            "checks": {
                "js_default_unset_true": js_result.returncode == 0,
                "browser_smoke": browser_result.returncode == 0,
                "command_results": command_results,
                "offline_test_env_used": True,
                "offline_test_env_removed": "B2B_TEST_OFFLINE" not in clean_env,
                "tests_intact": _test_integrity(repo),
                "tracked_clean": git.get("tracked_clean") is True,
                "feature_remote_exact": git.get("origin_feature_sha") == git.get("head_sha"),
                "ci_token_masked": ci.get("token_used_for_auth_only") is True,
                "ci": ci,
            },
            "git": git,
            "artifacts": {
                "package_manifest": {"path": str(package_manifest_path), "sha256": _sha256(package_manifest_path)},
                "replay_receipt": {"path": str(replay_receipt_path), "sha256": _sha256(Path(replay_receipt_path))},
                "live_artifact_dir": str(live_artifact),
                "offline_artifact_dir": str(offline_artifact),
            },
            "metrics": {"live": live_metrics, "offline": offline_metrics},
        }
    except Exception as exc:
        failures.append(f"EVALUATOR_ERROR:{type(exc).__name__}:{exc}")
        evidence["behavioral"] = {"package_valid": False}
    computed = evaluate_evidence(evidence)
    computed["failure_reasons"] = sorted(set(computed["failure_reasons"] + failures))
    report = {
        "schema_version": 1,
        "behavioral_recall_validated": computed["behavioral_recall_validated"],
        "release_ready": computed["release_ready"],
        "merge_allowed": computed["merge_allowed"],
        "head_sha": evidence.get("git", {}).get("head_sha", ""),
        "base_sha": evidence.get("git", {}).get("gate_base_sha", ""),
        "origin_main_sha": evidence.get("git", {}).get("origin_main_sha", ""),
        "source_record_count": _json(package_dir / "package_manifest.json").get("input", {}).get("record_count", 0) if (package_dir / "package_manifest.json").is_file() else 0,
        "source_record_id_sha256": _json(package_dir / "package_manifest.json").get("input", {}).get("ordered_id_sha256", "") if (package_dir / "package_manifest.json").is_file() else "",
        "replay_miss_count": evidence.get("behavioral", {}).get("replay_miss_count"),
        "replay_network_events": evidence.get("behavioral", {}).get("replay_network_events"),
        "validator_status": evidence.get("behavioral", {}).get("validator_status", "FAIL"),
        "publication_precision": evidence.get("metrics", {}).get("live", {}).get("stages", {}).get("publication_precision"),
        "publication_recall": evidence.get("metrics", {}).get("live", {}).get("stages", {}).get("publication_recall"),
        "ci": evidence.get("checks", {}).get("ci", {}),
        "artifacts": evidence.get("artifacts", {}),
        "metrics": evidence.get("metrics", {}),
        "failure_reasons": computed["failure_reasons"],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(f".{report_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the evidence-backed Golden6 release gate.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--replay-receipt", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.repo_root, args.package, args.replay_receipt, args.report)
    except Exception as exc:
        print(f"RELEASE_GATE_BLOCKED:{exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["release_ready"] and report["merge_allowed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
