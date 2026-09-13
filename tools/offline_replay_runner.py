"""Run main.py against a Golden6 replay snapshot with a process-wide audit guard."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.free_only_contract import validate_database as validate_free_only_database
from tools.free_only_contract import validate_manifest as validate_free_only_manifest


NETWORK_EVENT_PREFIXES = (
    "socket.", "http.client.", "urllib.", "ssl.", "asyncio.",
)
NETWORK_EVENTS = {
    "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
    "socket.gethostbyaddr", "socket.getnameinfo", "socket.getfqdn",
    "socket.connect", "socket.create_connection", "http.client.connect",
    "http.client.send", "http.client.putrequest", "http.client.request",
    "urllib.Request", "urllib.urlopen", "ssl.wrap_socket",
    "ssl.SSLContext.wrap_socket",
}


class ReplayNetworkViolation(RuntimeError):
    """Raised on the first audited external-network attempt."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_not_object:{path}")
    return value


def _complete_run(repo: Path, input_hash: str, before: set[str]) -> Path | None:
    runs = repo / "runs"
    matches = []
    if not runs.is_dir():
        return None
    for path in sorted(runs.glob("*/manifest.json")):
        if path.parent.name in before:
            continue
        try:
            manifest = _json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if manifest.get("complete") is True and manifest.get("input_sha256") == input_hash:
            matches.append(path.parent)
    return matches[0] if len(matches) == 1 else None


def _audit_factory(receipts: list[dict]):
    def audit(event: str, args: tuple) -> None:
        if event not in NETWORK_EVENTS and not any(event.startswith(prefix) for prefix in NETWORK_EVENT_PREFIXES):
            return
        record = {
            "event": event,
            "args": [str(value)[:200] for value in args],
        }
        receipts.append(record)
        raise ReplayNetworkViolation(f"offline_replay_network_event:{event}")
    return audit


def run(repo_root: Path, input_path: Path, package_dir: Path, receipt_path: Path) -> dict:
    repo = Path(repo_root).resolve()
    input_path = Path(input_path).resolve()
    package_dir = Path(package_dir).resolve()
    package_manifest_path = package_dir / "package_manifest.json"
    package_manifest = _json(package_manifest_path)
    try:
        validate_free_only_manifest(package_manifest, require_complete=False)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    replay_snapshot = package_dir / str(package_manifest.get("replay", {}).get("snapshot", ""))
    if not replay_snapshot.is_file():
        raise ValueError("replay_snapshot_missing")
    if _sha256(replay_snapshot) != package_manifest.get("replay", {}).get("snapshot_sha256"):
        raise ValueError("replay_snapshot_hash_mismatch")
    if hashlib.sha256(input_path.read_bytes()).hexdigest() != package_manifest.get("input", {}).get("sha256"):
        raise ValueError("replay_input_hash_mismatch")

    before = {path.parent.name for path in (repo / "runs").glob("*/manifest.json")} if (repo / "runs").is_dir() else set()
    receipts: list[dict] = []
    sys.addaudithook(_audit_factory(receipts))
    old_argv = sys.argv
    old_env = os.environ.copy()
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 1
    exception = ""
    try:
        os.environ.pop("B2B_TEST_OFFLINE", None)
        os.environ.pop("ENABLE_JS_FALLBACK", None)
        sys.argv = [
            str(repo / "main.py"), "--input", str(input_path), "--rerank-cache",
            "--replay-snapshot", str(replay_snapshot), "--no-allow-paid",
            "--finalize-without-paid", "--non-interactive",
        ]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                runpy.run_path(str(repo / "main.py"), run_name="__main__")
                exit_code = 0
            except SystemExit as exc:
                exit_code = int(exc.code) if isinstance(exc.code, int) else 1
            except ReplayNetworkViolation as exc:
                exception = str(exc)
                exit_code = 2
            except BaseException as exc:
                exception = f"{type(exc).__name__}:{exc}"
                exit_code = 1
    finally:
        sys.argv = old_argv
        for key in list(os.environ):
            if key not in old_env:
                os.environ.pop(key, None)
        os.environ.update(old_env)

    receipt = {
        "schema_version": 1,
        "status": "PASS" if exit_code == 0 and not receipts else "FAIL",
        "exit_code": exit_code,
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "package_manifest_sha256": _sha256(package_manifest_path),
        "replay_snapshot_sha256": _sha256(replay_snapshot),
        "replay_network_events": len(receipts),
        "network_events": receipts,
        "exception": exception,
        "stdout_sha256": hashlib.sha256(stdout.getvalue().encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.getvalue().encode("utf-8")).hexdigest(),
    }
    run_root = _complete_run(repo, receipt["input_sha256"], before)
    paid_activity: dict[str, int] = {}
    free_only_error = ""
    if run_root is not None:
        manifest = _json(run_root / "manifest.json")
        artifact_dir = run_root / "output" / "artifacts" / str(manifest.get("artifact_set_sha256", ""))
        receipt.update({
            "run_id": str(manifest.get("run_id", run_root.name)),
            "run_dir": str(run_root),
            "artifact_dir": str(artifact_dir),
            "artifact_set_sha256": str(manifest.get("artifact_set_sha256", "")),
        })
        try:
            validate_free_only_manifest(manifest)
            paid_activity = validate_free_only_database(
                run_root / "state" / "progress.sqlite3",
                str(manifest.get("run_id", run_root.name)),
                int(package_manifest.get("input", {}).get("record_count", 0)),
            )
        except ValueError as exc:
            free_only_error = str(exc)
    else:
        free_only_error = "free_only_complete_run_missing"
    if free_only_error:
        receipt["exception"] = free_only_error
        receipt["status"] = "FAIL"
        exit_code = 3
    receipt.update({
        "free_only_config_valid": not free_only_error,
        "paid_provider_calls": int(paid_activity.get("provider_calls", -1)) if not free_only_error else -1,
        "paid_budget_nonzero": bool(free_only_error),
        "paid_activity": paid_activity,
    })
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt_path.with_name(f".{receipt_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(receipt_path)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a network-blocked Golden6 offline replay.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.repo_root, args.input, args.package, args.receipt)
    except Exception as exc:
        print(f"OFFLINE_REPLAY_BLOCKED:{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
