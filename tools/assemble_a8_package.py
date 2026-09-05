"""Assemble an immutable, finalized A8 benchmark package (V3)."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import excel
from modules.run_context import source_tree_sha256


ACCEPTANCE = {
    "max_false_publication": 0,
    "max_published_unknown_identity": 0,
    "require_full_source_id_coverage": True,
    "require_all_rows_review_frozen": True,
    "require_nonzero_known_identity_denominator": True,
}
PACKAGE_ID_ALGORITHM = "sha256(canonical-json(manifest_without_package_id_sha256))"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def source_ids(path: Path) -> list[str]:
    rows = excel.read_company_records(path)
    ids = [str(row.get("source_record_id") or "").strip() for row in rows]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise RuntimeError(f"source workbook IDs are not unique/nonempty: {path}")
    return ids


def file_spec(path: Path, relative: str) -> dict[str, Any]:
    return {"path": relative.replace("\\", "/"), "sha256": sha256(path), "bytes": path.stat().st_size}


def copy_tree_file(source: Path, destination_root: Path, relative: str) -> dict[str, Any]:
    if not source.is_file():
        raise RuntimeError(f"required package source is missing: {source}")
    destination = destination_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return file_spec(destination, relative)


def _git_identity(root: Path) -> dict[str, Any]:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True).stdout
    if status.strip():
        raise RuntimeError("immutable release package requires a clean worktree")
    return {"base_commit": head, "uncommitted": False, "source_tree_sha256": source_tree_sha256(root)}


def _validate_actual_source(actual_dir: Path, role: str) -> dict[str, Any]:
    manifest_path = actual_dir / "actual_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("status") not in {"complete", "complete_free_only"} or payload.get("finalized") is not True or payload.get("complete") is not True or payload.get("phase") != "COMPLETE":
        raise RuntimeError(f"{role} actual is not a finalized production artifact")
    if payload.get("status") == "handoff_checkpoint_materialized" or payload.get("checkpoint_materialized"):
        raise RuntimeError(f"{role} handoff/checkpoint snapshot cannot be packaged as actual")
    for name in ("all_results.xlsx", "actual_manifest.json"):
        if not (actual_dir / name).is_file():
            raise RuntimeError(f"{role} actual is missing {name}")
    return payload


def assemble(args: argparse.Namespace) -> Path:
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parents[1]
    git = _git_identity(repo_root)
    if not getattr(args, "quality_report", None) or not getattr(args, "quality_report_md", None):
        raise RuntimeError("V3 assembly requires staged JSON and Markdown quality reports")
    base = json.loads(args.selection_base.read_text(encoding="utf-8"))
    acquisition = json.loads(args.acquisition.read_text(encoding="utf-8"))
    diagnostic_expected_ids = source_ids(args.diagnostic_expected)
    independent_expected_ids = source_ids(args.independent_expected)
    if len(diagnostic_expected_ids) != 96 or len(independent_expected_ids) != 120:
        raise RuntimeError("expected workbook counts are not 96 and 120")
    diagnostic_actual = _validate_actual_source(args.diagnostic_actual, "diagnostic_96")
    independent_actual = _validate_actual_source(args.independent_actual, "independent_120")

    staging = output_root / f".a8_staging_{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        specs: list[dict[str, Any]] = []
        specs.append(copy_tree_file(args.diagnostic_queue, staging, "selection/diagnostic_96_review_queue.xlsx"))
        specs.append(copy_tree_file(args.diagnostic_expected, staging, "expected/diagnostic_96.xlsx"))
        specs.append(copy_tree_file(args.independent_expected, staging, "expected/independent_120.xlsx"))
        for sources, relative in (
            ([args.diagnostic_pass_1, args.independent_pass_1], "evidence/review_pass_1.jsonl"),
            ([args.diagnostic_pass_2, args.independent_pass_2], "evidence/review_pass_2.jsonl"),
            ([args.diagnostic_merged, args.independent_merged], "evidence/review_evidence.jsonl"),
        ):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"".join(source.read_bytes() for source in sources))
            specs.append(file_spec(target, relative))
        specs.append(copy_tree_file(args.runtime_lock, staging, "runtime-lock.txt"))
        specs.append(copy_tree_file(args.runtime_report, staging, "runtime_report.json"))
        specs.append(copy_tree_file(args.quality_report, staging, "quality_report.json"))
        specs.append(copy_tree_file(args.quality_report_md, staging, "quality_report.md"))
        for actual_dir, role in ((args.diagnostic_actual, "diagnostic_96"), (args.independent_actual, "independent_120")):
            for name in ("all_results.xlsx", "actual_manifest.json", "checkpoint_results.jsonl"):
                specs.append(copy_tree_file(actual_dir / name, staging, f"actual/{role}/{name}"))

        selection_manifest = {
            "schema_version": 3,
            "benchmark": "A8",
            "selection_algorithm": "SHA256(source_record_id + architect-independent-v1)",
            "original_input": base["parent_artifacts"]["original_input"],
            "original_source_id_order": base["original_source_id_order"],
            "original_source_id_order_sha256": canonical_hash(base["original_source_id_order"]),
            "diagnostic_96": {
                "source_record_ids": diagnostic_expected_ids,
                "source_record_ids_sha256": canonical_hash(diagnostic_expected_ids),
                "review_queue": "selection/diagnostic_96_review_queue.xlsx",
                "expected_sha256": sha256(args.diagnostic_expected),
            },
            "independent_120": {
                "source_record_ids": independent_expected_ids,
                "source_record_ids_sha256": canonical_hash(independent_expected_ids),
                "expected_sha256": sha256(args.independent_expected),
            },
            "independent_acquisition": {
                "sha256": sha256(args.acquisition),
                "hometex_detail_slug_count": acquisition["hometex"]["unique_slug_count"],
                "hometex_selected_count": len(acquisition["hometex"]["selected"]),
                "ambiente_hits_total": acquisition["ambiente"]["hits_total"],
                "ambiente_selected_count": len(acquisition["ambiente"]["selected"]),
                "exclusion_count": len(acquisition["exclusion_manifest"]),
                "request_telemetry_count": len(acquisition.get("request_telemetry", [])),
            },
            "review_evidence": {
                "diagnostic_pass_1_sha256": sha256(args.diagnostic_pass_1),
                "diagnostic_pass_2_sha256": sha256(args.diagnostic_pass_2),
                "diagnostic_merged_sha256": sha256(args.diagnostic_merged),
                "independent_pass_1_sha256": sha256(args.independent_pass_1),
                "independent_pass_2_sha256": sha256(args.independent_pass_2),
                "independent_merged_sha256": sha256(args.independent_merged),
            },
        }
        selection_path = staging / "selection_manifest.json"
        selection_path.write_text(json.dumps(selection_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        specs.append(file_spec(selection_path, "selection_manifest.json"))

        expected_sha = {"diagnostic_96": sha256(args.diagnostic_expected), "independent_120": sha256(args.independent_expected)}
        descriptors = []
        for role, expected_relative, actual_dir, actual_payload in (
            ("diagnostic_96", "expected/diagnostic_96.xlsx", args.diagnostic_actual, diagnostic_actual),
            ("independent_120", "expected/independent_120.xlsx", args.independent_actual, independent_actual),
        ):
            descriptors.append({
                "schema_version": 3,
                "role": role,
                "expected": expected_relative,
                "expected_sha256": expected_sha[role],
                "actual": f"actual/{role}/all_results.xlsx",
                "actual_output_sha256": sha256(actual_dir / "all_results.xlsx"),
                "actual_manifest": f"actual/{role}/actual_manifest.json",
                "actual_manifest_sha256": sha256(actual_dir / "actual_manifest.json"),
                "source_record_ids_sha256": actual_payload["source_record_ids_sha256"],
                "config_sha256": actual_payload["config_sha256"],
                "runtime_source_tree_sha256": actual_payload["runtime_source_tree_sha256"],
                "acceptance": ACCEPTANCE,
            })

        manifest_without_id = {
            "schema_version": 3,
            "benchmark": "A8",
            "package_id_algorithm": PACKAGE_ID_ALGORITHM,
            **git,
            "acceptance": ACCEPTANCE,
            "sets": descriptors,
            "selection_manifest_sha256": file_spec(selection_path, "selection_manifest.json")["sha256"],
            "runtime_lock_sha256": sha256(args.runtime_lock),
            "runtime_report_sha256": sha256(args.runtime_report),
            "quality_report_sha256": sha256(args.quality_report),
            "quality_report_md_sha256": sha256(args.quality_report_md),
            "files": sorted(specs, key=lambda item: item["path"]),
        }
        package_id = canonical_hash(manifest_without_id)
        package_dir = output_root / f"a8_benchmark_{package_id}"
        if package_dir.exists():
            raise RuntimeError(f"final package destination already exists: {package_dir}")
        manifest = {**manifest_without_id, "package_id_sha256": package_id}
        (staging / "benchmark_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.replace(package_dir)
        manifest_path = package_dir / "benchmark_manifest.json"
        print(json.dumps({
            "package": str(package_dir),
            "package_id_sha256": package_id,
            "benchmark_manifest_file_sha256": sha256(manifest_path),
            "file_count": len(specs),
        }, sort_keys=True))
        return package_dir
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in (
        "selection-base", "diagnostic-selection", "independent-selection", "acquisition", "diagnostic-queue",
        "diagnostic-expected", "independent-expected", "diagnostic-pass-1", "diagnostic-pass-2", "diagnostic-merged",
        "independent-pass-1", "independent-pass-2", "runtime-lock", "runtime-report", "quality-report", "quality-report-md",
        "diagnostic-actual", "independent-actual", "output-root",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    assemble(parser.parse_args())


if __name__ == "__main__":
    main()
