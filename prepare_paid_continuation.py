"""Offline, approval-gated creation of a paid continuation run."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from modules import checkpoint, run_context


PROVIDERS = tuple(sorted(checkpoint.CANONICAL_PROVIDERS))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_parent(parent: Path) -> tuple[dict, list[str], list[dict], list[dict]]:
    manifest_path = parent / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("parent run manifest is missing or has the wrong run ID")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if parent.name != manifest.get("run_id"):
        raise ValueError("parent run manifest is missing or has the wrong run ID")
    if manifest.get("complete") or manifest.get("paid_enabled") is not False:
        raise ValueError("parent must be an incomplete paid-disabled run")
    if manifest.get("provisional") or (manifest.get("lineage") or {}).get("type") == "legacy_recovery":
        raise ValueError("legacy recovery runs remain quarantined and cannot be continued")
    run_context.validate_run_bundle(parent, profile="FROZEN_RECOVERY")
    artifact_dir = parent / "output" / "artifacts" / str(manifest.get("artifact_set_sha256", ""))
    if not artifact_dir.is_dir() or not manifest.get("files"):
        raise ValueError("parent artifact set is missing")
    for name, info in manifest["files"].items():
        artifact = artifact_dir / str(name)
        if not artifact.is_file() or _sha256(artifact) != info.get("sha256"):
            raise ValueError(f"parent artifact hash mismatch: {name}")
    checkpoint_name = "recovery_state.sqlite3"
    checkpoint_info = manifest.get("files", {}).get(checkpoint_name)
    db_path = artifact_dir / checkpoint_name
    if not checkpoint_info or not db_path.exists():
        raise ValueError("parent manifest does not point to an immutable checkpoint artifact")
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("parent SQLite integrity check failed")
        run = connection.execute("SELECT run_id,input_hash FROM runs WHERE run_id=?", (parent.name,)).fetchone()
        items = [dict(zip(("item_index", "source_record_id", "free_state", "paid_required", "paid_state", "free_attempts", "paid_attempts", "last_error", "payload_sha256", "quarantine_state", "quarantine_status", "publication_blockers"), row)) for row in connection.execute("SELECT item_index,source_record_id,free_state,paid_required,paid_state,free_attempts,paid_attempts,last_error,payload_sha256,quarantine_state,quarantine_status,publication_blockers FROM run_items ORDER BY item_index")]
        results = [{"item_index": row[0], "payload": row[1]} for row in connection.execute("SELECT item_index,payload FROM results ORDER BY item_index")]
    if not run or run[1] != manifest.get("input_sha256") or len(items) == 0 or len(results) != len(items):
        raise ValueError("parent run identity/count validation failed")
    ids = [item["source_record_id"] for item in items]
    if ids != manifest.get("ordered_source_record_ids") or len(set(ids)) != len(items):
        raise ValueError("parent source ID list mismatch")
    result_by_index = {int(result["item_index"]): result for result in results}
    if set(result_by_index) != set(range(len(items))):
        raise ValueError("parent result index list mismatch")
    if any(_sha256_bytes(result_by_index[item["item_index"]]["payload"]) != item["payload_sha256"] for item in items):
        raise ValueError("parent payload hash mismatch")
    if any(item["free_state"] not in {"DONE", "FAILED"} for item in items):
        raise ValueError("all free work must be terminal before paid continuation")
    return manifest, ids, items, results


def _sha256_bytes(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _ensure_authorization_schema(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS authorizations(auth_sha256 TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL DEFAULT '', child_run_id TEXT NOT NULL, target_dir TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'PREPARING', created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '', used_at TEXT NOT NULL DEFAULT '')")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(authorizations)")}
        for name, definition in {"parent_run_id": "TEXT NOT NULL DEFAULT ''", "target_dir": "TEXT NOT NULL DEFAULT ''", "status": "TEXT NOT NULL DEFAULT 'PREPARING'", "created_at": "TEXT NOT NULL DEFAULT ''", "updated_at": "TEXT NOT NULL DEFAULT ''"}.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE authorizations ADD COLUMN {name} {definition}")
        connection.execute("UPDATE authorizations SET status='PUBLISHED' WHERE status='' AND used_at<>''")
        connection.commit()


def _claim_authorization(path: Path, *, auth_sha256: str, parent_run_id: str, child_run_id: str, target_dir: str) -> str:
    _ensure_authorization_schema(path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT parent_run_id,child_run_id,target_dir,status FROM authorizations WHERE auth_sha256=?", (auth_sha256,)).fetchone()
        if row:
            if str(row[0]) != parent_run_id or str(row[1]) != child_run_id or str(row[2]) != target_dir:
                raise RuntimeError("authorization is bound to another target or child")
            return str(row[3] or "PREPARING")
        connection.execute("INSERT INTO authorizations(auth_sha256,parent_run_id,child_run_id,target_dir,status,created_at,updated_at,used_at) VALUES(?,?,?,?,?,?,?,?)", (auth_sha256, parent_run_id, child_run_id, target_dir, "PREPARING", now, now, ""))
        connection.commit()
    return "PREPARING"


def _mark_authorization_published(path: Path, auth_sha256: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE authorizations SET status='PUBLISHED',updated_at=?,used_at=? WHERE auth_sha256=? AND status='PREPARING'", (datetime.now(timezone.utc).isoformat(timespec="seconds"), datetime.now(timezone.utc).isoformat(timespec="seconds"), auth_sha256))
        connection.commit()


def _validate_authorization(path: Path, *, parent_root: Path, parent: dict, source_ids: list[str], paid_source_ids: list[str]) -> tuple[dict, str]:
    auth_sha256 = _sha256(path)
    approval = json.loads(path.read_text(encoding="utf-8"))
    if approval.get("approved") is not True:
        raise PermissionError("authorization JSON must contain approved=true")
    required = {"brightdata", "google_places", "brandfetch", "hunter", "linkedin", "llm"}
    limits_value = approval.get("limits")
    if not isinstance(limits_value, dict) or set(limits_value) != required:
        raise ValueError("authorization limits must contain exactly the six canonical providers")
    if any(type(value) is not int or value < 0 for value in limits_value.values()):
        raise ValueError("authorization limits must be non-negative integers")
    bindings = {
        "parent_run_id": parent.get("run_id"),
        "parent_manifest_sha256": _sha256(parent_root / "manifest.json"),
        "checkpoint_sha256": parent.get("checkpoint_sha256") or parent.get("files", {}).get("recovery_state.sqlite3", {}).get("sha256"),
        "paid_source_ids_sha256": hashlib.sha256(run_context.canonical_json(paid_source_ids).encode("utf-8")).hexdigest(),
    }
    for key, expected in bindings.items():
        if approval.get(key) != expected:
            raise ValueError(f"authorization binding mismatch: {key}")
    return approval, auth_sha256


def prepare_paid_continuation(parent_run_dir: Path, authorization: Path, destination: Path) -> dict:
    parent = Path(parent_run_dir).resolve()
    manifest, source_ids, parent_items, results = _read_parent(parent)
    paid_source_ids = [item["source_record_id"] for item in parent_items if item["paid_required"] and item["paid_state"] == "PENDING"]
    if not paid_source_ids:
        raise ValueError("parent has no PENDING paid work")
    approval, auth_sha256 = _validate_authorization(Path(authorization), parent_root=parent, parent=manifest, source_ids=source_ids, paid_source_ids=paid_source_ids)
    authorization_db = parent / "state" / "continuation_authorizations.sqlite3"
    authorization_db.parent.mkdir(parents=True, exist_ok=True)
    limits = {provider: approval["limits"][provider] for provider in PROVIDERS}
    base = run_context.RunConfig.from_dict(manifest.get("run_config", {}))
    effective = base.__class__(
        search_provider=base.search_provider, search_cache_mode=base.search_cache_mode,
        crawl_cache_mode=base.crawl_cache_mode, paid_enabled=True,
        brightdata_budget=limits["brightdata"], google_places_budget=limits["google_places"],
        brandfetch_budget=limits["brandfetch"], hunter_budget=limits["hunter"],
        linkedin_budget=limits["linkedin"], llm_budget=limits["llm"], model=base.model,
        thresholds=base.thresholds, policy_versions=base.policy_versions,
        effective_settings=base.effective_settings,
    )
    lineage = {
        "type": "paid_continuation",
        "parent_run_id": parent.name,
        "parent_manifest_sha256": _sha256(parent / "manifest.json"),
        "authorization_sha256": auth_sha256,
    }
    run_id = run_context.canonical_run_id(
        input_sha256=manifest["input_sha256"], ordered_source_record_ids=source_ids,
        effective_config=effective.as_dict(), runtime_source_tree_hash=run_context.source_tree_sha256(),
        lineage=lineage,
    )
    final_root = Path(destination).resolve() / "runs" / run_id
    target_dir = str(Path(destination).resolve())
    authorization_state = _claim_authorization(
        authorization_db, auth_sha256=auth_sha256, parent_run_id=parent.name,
        child_run_id=run_id, target_dir=target_dir,
    )
    if authorization_state == "PUBLISHED" and not final_root.exists():
        raise RuntimeError("authorization is already published but child is missing")
    if final_root.exists():
        existing = json.loads((final_root / "manifest.json").read_text(encoding="utf-8"))
        if existing.get("lineage") != lineage:
            raise RuntimeError("different continuation already exists at canonical run ID")
        run_context.validate_run_bundle(final_root, expected_input_hash=manifest["input_sha256"], expected_config_hash=effective.sha256, expected_source_ids=source_ids)
        _mark_authorization_published(authorization_db, auth_sha256)
        return existing
    runs_root = final_root.parent
    runs_root.mkdir(parents=True, exist_ok=True)
    for prepared in sorted(runs_root.glob(f".{run_id}.*.staging")):
        if (prepared / "manifest.json").is_file() and (prepared / "state" / "progress.sqlite3").is_file():
            # A crash after staging but before rename can be completed without
            # consuming a second authorization or creating a new child ID.
            try:
                prepared.replace(final_root)
                run_context.validate_run_bundle(final_root, expected_input_hash=manifest["input_sha256"], expected_config_hash=effective.sha256, expected_source_ids=source_ids)
                _mark_authorization_published(authorization_db, auth_sha256)
                return json.loads((final_root / "manifest.json").read_text(encoding="utf-8"))
            except Exception:
                if final_root.exists():
                    raise
    staging = runs_root / f".{run_id}.{uuid.uuid4().hex}.staging"
    state_dir = staging / "state"
    output_dir = staging / "output"
    artifact_stage = staging / ".artifact_staging"
    state_dir.mkdir(parents=True)
    output_dir.mkdir()
    artifact_stage.mkdir()
    items = []
    for item in parent_items:
        copied = dict(item)
        if copied["paid_required"] and copied["paid_state"] == "PENDING":
            copied["paid_state"] = "PENDING"
        items.append(copied)
    checkpoint.seed_recovered_run(
        path=state_dir / "progress.sqlite3", run_id=run_id,
        input_hash=manifest["input_sha256"], run_signature=f"continuation:{parent.name}:{auth_sha256}",
        context={"run_id": run_id, "input_hash": manifest["input_sha256"], "phase": "PAID", "lineage": lineage, "paid_queue_frozen": True, "seed_timestamp": "2000-01-01T00:00:00+00:00"},
        budgets=limits, items=items, results=results,
    )
    parent_artifact_dir = parent / "output" / "artifacts" / str(manifest["artifact_set_sha256"])
    if (parent_artifact_dir / "all_results.xlsx").exists():
        shutil.copy2(parent_artifact_dir / "all_results.xlsx", artifact_stage / "all_results.xlsx")
    shutil.copy2(state_dir / "progress.sqlite3", artifact_stage / "recovery_state.sqlite3")
    artifact_files = [artifact_stage / "recovery_state.sqlite3"]
    if (artifact_stage / "all_results.xlsx").exists():
        artifact_files.insert(0, artifact_stage / "all_results.xlsx")
    artifact_set_sha256 = hashlib.sha256("".join(f"{p.name}:{_sha256(p)}\n" for p in sorted(artifact_files)).encode()).hexdigest()
    artifact_dir = output_dir / "artifacts" / artifact_set_sha256
    artifact_dir.mkdir(parents=True)
    files = {}
    for source in artifact_files:
        target = artifact_dir / source.name
        source.replace(target)
        files[target.name] = {"sha256": _sha256(target), "bytes": target.stat().st_size}
    if (artifact_dir / "all_results.xlsx").exists():
        shutil.copy2(artifact_dir / "all_results.xlsx", output_dir / "all_results.xlsx")
    child_manifest = {
        "manifest_schema_version": 4, "complete": False, "provisional": bool(manifest.get("provisional", False)), "quarantine_state": manifest.get("quarantine_state"), "phase": "PAID", "paid_enabled": True,
        "run_id": run_id, "input_sha256": manifest["input_sha256"], "config_sha256": effective.sha256,
        "run_config": effective.as_dict(), "runtime_source_tree_sha256": run_context.source_tree_sha256(), "item_count": len(source_ids),
        "ordered_source_record_ids": source_ids, "lineage": lineage,
        "counts": {"input": len(source_ids), "paid_pending": sum(1 for item in items if item["paid_required"] and item["paid_state"] == "PENDING")},
        "artifact_set_sha256": artifact_set_sha256, "files": files,
        "checkpoint_sha256": files["recovery_state.sqlite3"]["sha256"],
    }
    (staging / "manifest.json").write_text(json.dumps(child_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(artifact_dir / "recovery_state.sqlite3", state_dir / "progress.sqlite3")
    staging.replace(final_root)
    run_context.validate_run_bundle(final_root, expected_input_hash=manifest["input_sha256"], expected_config_hash=effective.sha256, expected_source_ids=source_ids)
    _mark_authorization_published(authorization_db, auth_sha256)
    return child_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a paid continuation without running providers")
    parser.add_argument("--parent-run-dir", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(prepare_paid_continuation(args.parent_run_dir, args.authorization, args.destination), ensure_ascii=False, indent=2))
