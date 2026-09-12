"""One deterministic input-selection path shared by CLI and pipeline runs."""

from __future__ import annotations

import json
from pathlib import Path

from modules import checkpoint, excel, run_context


def deduplicate_company_records(records: list[dict]) -> tuple[list[dict], int]:
    """Keep distinct source records; only an identical source ID may merge."""
    unique: dict[str, dict] = {}
    result: list[dict] = []
    for record in records:
        current = dict(record)
        source_id, quality = run_context.source_record_identity(current)
        current["source_record_id"] = source_id
        current["source_record_id_quality"] = quality
        existing = unique.get(source_id)
        if existing is None:
            unique[source_id] = current
            result.append(current)
            continue
        for field, value in current.items():
            if not existing.get(field) and value:
                existing[field] = value
    return result, len(records) - len(result)


def _artifact_statuses(from_run_manifest: Path) -> dict[str, str]:
    manifest_path = Path(from_run_manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("complete") or not manifest.get("files"):
        raise ValueError("source run manifest is not complete")
    source_root = manifest_path.parent
    artifact_dir = source_root / "output" / "artifacts" / str(manifest.get("artifact_set_sha256", ""))
    artifact = artifact_dir / "all_results.xlsx"
    info = manifest.get("files", {}).get("all_results.xlsx", {})
    if not artifact.is_file() or not info or checkpoint.file_hash(artifact) != info.get("sha256"):
        raise ValueError("source run manifest artifact hash mismatch")
    statuses = excel.read_result_statuses_by_source_id(artifact)
    if not statuses:
        raise ValueError("source run all_results has no source_record_id statuses")
    return statuses


def select_company_records(
    input_file: Path,
    *,
    companies: set[str] | None = None,
    only_statuses: set[str] | None = None,
    from_run_manifest: Path | None = None,
    require_nonempty: bool = False,
) -> tuple[list[dict], int]:
    """Read, deduplicate, filter and identity-order records exactly once."""
    records = excel.read_company_records(Path(input_file))
    for original_index, record in enumerate(records):
        record.setdefault("original_index", original_index)
    records, duplicate_count = deduplicate_company_records(records)

    if companies:
        wanted = {str(value).strip().casefold() for value in companies if str(value).strip()}
        records = [
            record for record in records
            if str(record.get("company", "")).strip().casefold() in wanted
        ]

    if only_statuses:
        if not from_run_manifest:
            raise ValueError("--only-status requires --from-run-manifest")
        statuses = _artifact_statuses(Path(from_run_manifest))
        allowed = {str(value).strip().casefold() for value in only_statuses if str(value).strip()}
        records = [
            record for record in records
            if statuses.get(str(record.get("source_record_id", "")), "").casefold() in allowed
        ]

    if require_nonempty and not records:
        raise RuntimeError(f"No companies matched the requested selection in {input_file}")
    return records, duplicate_count
