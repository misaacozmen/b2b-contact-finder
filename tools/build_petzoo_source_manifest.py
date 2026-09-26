from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}
REQUIRED_REQUIREMENTS = {
    "requirements-browser.txt",
    "requirements-dev.txt",
    "requirements-ocr.txt",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def runtime_source_hash(root: Path) -> str:
    candidates = [root / "config.py", root / "main.py", root / "scrape_exhibitors.py"]
    candidates.extend(root.glob("requirements*.txt"))
    candidates.extend((root / "modules").glob("*.py"))
    digest = hashlib.sha256()
    for path in sorted({candidate.resolve() for candidate in candidates if candidate.is_file()}):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        body_hash = hashlib.sha256(path.read_bytes()).digest()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        digest.update(body_hash)
    return digest.hexdigest()


def source_files(snapshot: Path) -> list[Path]:
    return sorted(
        path for path in snapshot.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.parts)
        and path.suffix not in {".pyc", ".pyo"}
    )


def build(workspace: Path, delivery: Path) -> dict:
    workspace = workspace.resolve()
    delivery = delivery.resolve()
    snapshot = delivery / "source_snapshot"
    if not snapshot.is_dir():
        raise FileNotFoundError(f"source snapshot missing: {snapshot}")
    paths = source_files(snapshot)
    relative_paths = {path.relative_to(snapshot).as_posix() for path in paths}
    missing_requirements = sorted(REQUIRED_REQUIREMENTS - relative_paths)
    if missing_requirements:
        raise RuntimeError(f"snapshot requirements missing: {missing_requirements}")
    entries = []
    mismatches = []
    for path in paths:
        relative = path.relative_to(snapshot).as_posix()
        source = workspace / Path(relative)
        digest = sha256(path)
        if not source.is_file() or sha256(source) != digest:
            mismatches.append(relative)
        entries.append({
            "path": relative,
            "workspace_path": relative,
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
    if mismatches:
        raise RuntimeError(f"workspace/snapshot source mismatch: {mismatches[:20]}")
    manifest = {
        "schema_version": 1,
        "workspace": str(workspace),
        "snapshot": str(snapshot),
        "runtime_source_tree_sha256": runtime_source_hash(snapshot),
        "workspace_runtime_source_tree_sha256": runtime_source_hash(workspace),
        "excluded_generated_content": sorted(EXCLUDED_PARTS | {"*.pyc", "*.pyo"}),
        "files": entries,
    }
    if manifest["runtime_source_tree_sha256"] != manifest["workspace_runtime_source_tree_sha256"]:
        raise RuntimeError("workspace and snapshot runtime source hashes differ")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--delivery", required=True, type=Path)
    args = parser.parse_args()
    manifest = build(args.workspace, args.delivery)
    output = args.delivery.resolve() / "source_manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "manifest": str(output),
        "file_count": len(manifest["files"]),
        "runtime_source_tree_sha256": manifest["runtime_source_tree_sha256"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
