import argparse
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import config
from modules import excel, run_context
from modules.exhibitor_scraper import (
    dedupe_rows,
    scrape_beauty_eurasia,
    scrape_foodist,
    scrape_idos,
    scrape_ifco,
    scrape_maktek,
    scrape_metalexpo,
    scrape_texhibition,
    scrape_zuchex,
)
from modules.utils import ensure_directories


SCRAPERS = {
    "ifco": scrape_ifco,
    "idos": scrape_idos,
    "beauty": scrape_beauty_eurasia,
    "foodist": scrape_foodist,
    "maktek": scrape_maktek,
    "metalexpo": scrape_metalexpo,
    "texhibition": scrape_texhibition,
    "zuchex": scrape_zuchex,
}


def _source_detail_index(rows: list[dict], key: str) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for row in rows:
        value = str(row.get(key, "") or "").strip()
        if value:
            indexed[value] = row
    return indexed


def _texhibition_snapshot_payload(rows: list[dict]) -> dict:
    records = {}
    for row in rows:
        source_id = str(row.get("source_record_id", "") or "").strip()
        profile_url = str(row.get("profile_url", "") or "").strip()
        content_sha256 = str(row.get("source_detail_content_sha256", "") or "").strip()
        if not source_id or not profile_url or not content_sha256:
            continue
        records[source_id] = {
            "source_record_id": source_id,
            "profile_url": profile_url,
            "source_detail_content_sha256": content_sha256,
        }
    ordered = [records[key] for key in sorted(records)]
    canonical = run_context.canonical_json(ordered).encode("utf-8")
    return {
        "schema_version": 1,
        "record_count": len(ordered),
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "records": ordered,
    }


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def enrich_existing_workbook(
    input_path: Path | None = None,
    output_path: Path | None = None,
    *,
    texhibition_rows: list[dict] | None = None,
    zuchex_rows: list[dict] | None = None,
    delay: float = 0.4,
    source_snapshot_mode: str = "live",
    source_snapshot_input_path: Path | None = None,
) -> Path:
    """Enrich an existing input by immutable source keys only.

    Texhibition joins on the complete profile URL. Zuchex joins on its
    upstream ``_id``/``source_record_id``; company-name joins are forbidden.
    """
    input_path = Path(input_path or config.INPUT_FILE)
    if output_path is None:
        output_path = input_path.with_name("firms_enriched.xlsx")
    output_path = Path(output_path)
    if output_path.resolve() == input_path.resolve():
        raise ValueError("enrichment output must not overwrite input workbook")

    source_rows = excel.read_company_records(input_path)
    input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()

    rows = [copy.deepcopy(row) for row in source_rows]
    for row in rows:
        if not row.get("source_record_id"):
            source_id, quality = run_context.source_record_identity(row)
            row["source_record_id"] = source_id
            row["source_record_id_quality"] = quality
    source_ids = [str(row.get("source_record_id", "")) for row in rows]
    if not all(source_ids) or len(source_ids) != len(set(source_ids)):
        raise ValueError("enriched input requires unique source_record_id values")

    source_errors: dict[str, dict[str, str]] = {}
    source_timestamps: dict[str, str] = {}
    if texhibition_rows is None:
        source_timestamps["texhibition"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            texhibition_rows = scrape_texhibition(fetch_details=True, delay_sec=delay)
        except Exception as exc:
            texhibition_rows = []
            source_errors["texhibition"] = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
    else:
        source_timestamps["texhibition"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    tex_snapshot = _texhibition_snapshot_payload(texhibition_rows)
    snapshot_path = input_path.with_name("texhibition_source_snapshot.json")
    expected_snapshot = None
    if snapshot_path.is_file():
        try:
            expected_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"SOURCE_DRIFT:invalid_snapshot:{snapshot_path}") from exc
        expected_hash = str(expected_snapshot.get("sha256", ""))
        if expected_hash != tex_snapshot["sha256"]:
            raise RuntimeError(
                f"SOURCE_DRIFT:expected={expected_hash}:actual={tex_snapshot['sha256']}"
            )

    if zuchex_rows is None:
        source_timestamps["zuchex"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            zuchex_rows = scrape_zuchex(fetch_details=True, delay_sec=delay)
        except Exception as exc:
            zuchex_rows = []
            source_errors["zuchex"] = {
                "error_type": (
                    "ZUCHEX_CONFIGURATION"
                    if str(exc) == "zuchex_missing_required_ids"
                    else type(exc).__name__
                ),
                "error": str(exc),
            }
    else:
        source_timestamps["zuchex"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    by_profile = _source_detail_index(texhibition_rows, "profile_url")
    by_source_id = _source_detail_index(zuchex_rows, "source_record_id")
    by_upstream_id = _source_detail_index(zuchex_rows, "_id")

    source_match_counts = {"texhibition": 0, "zuchex": 0}
    source_covered_counts = {"texhibition": 0, "zuchex": 0}
    source_unavailable_counts = {"texhibition": {}, "zuchex": {}}
    for row in rows:
        source = str(row.get("source", "")).casefold()
        has_texhibition = "texhibition" in source
        has_zuchex = "zuchex" in source
        detail = None
        source_name = "texhibition" if has_texhibition else "zuchex" if has_zuchex else ""
        if has_texhibition:
            profile_url = str(row.get("profile_url", "") or "").strip()
            detail = by_profile.get(profile_url) if profile_url else None
        elif has_zuchex:
            detail = by_source_id.get(str(row.get("source_record_id", "") or "").strip())
            if detail is None:
                detail = by_upstream_id.get(str(row.get("_id", "") or "").strip())
        if detail is None:
            if source_name == "zuchex" and source_errors.get("zuchex", {}).get("error_type") == "ZUCHEX_CONFIGURATION":
                status = "UNAVAILABLE_ZUCHEX_CONFIGURATION"
            elif source_name in source_errors:
                status = f"UNAVAILABLE_{source_name.upper()}_ERROR"
            elif not str(row.get("profile_url", "") or "").strip():
                status = "UNAVAILABLE_NO_PROFILE_URL"
            else:
                status = "UNAVAILABLE_NO_SOURCE_KEY_MATCH"
            row["source_detail_status"] = status
            if source_name:
                source_unavailable_counts[source_name][status] = source_unavailable_counts[source_name].get(status, 0) + 1
            continue
        if source_name:
            source_match_counts[source_name] += 1
            if source_name == "texhibition" and has_zuchex:
                source_covered_counts["zuchex"] += 1
        # Only source metadata fields may be copied from the detail record;
        # the original company/order/source population remains untouched.
        for field in (
            "listed_website", "website", "country", "listed_phone", "listed_email",
            "listed_phone_status", "listed_address", "listed_address_status", "brands", "representations", "listed_legal_name",
            "description", "source_detail_status", "source_detail_url",
            "source_detail_content_sha256", "source_evidence",
        ):
            if field in detail and detail[field] not in (None, ""):
                value = detail[field]
                if field == "source_evidence":
                    try:
                        claims = json.loads(str(value))
                        if isinstance(claims, list):
                            for claim in claims:
                                if isinstance(claim, dict):
                                    claim["source_record_id"] = row["source_record_id"]
                            value = json.dumps(claims, ensure_ascii=False, sort_keys=True)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                row[field] = value

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}.", suffix=output_path.suffix,
        dir=output_path.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        excel.write_company_records(temporary, rows)
        validated = excel.read_company_records(temporary)
        if len(validated) != len(rows) or [r["source_record_id"] for r in validated] != source_ids:
            raise ValueError("enriched input changed source population or order")
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    output_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
    input_source_counts = {
        "texhibition": sum(1 for row in rows if "texhibition" in str(row.get("source", "")).casefold()),
        "zuchex": sum(1 for row in rows if "zuchex" in str(row.get("source", "")).casefold()),
    }
    accounted_counts = {
        source: source_match_counts[source] + source_covered_counts[source] + sum(source_unavailable_counts[source].values())
        for source in ("texhibition", "zuchex")
    }
    if accounted_counts != input_source_counts:
        raise ValueError(f"source_accounting_mismatch:{accounted_counts}:{input_source_counts}")
    manifest = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": {
            "path": str(input_path.resolve()),
            "sha256": input_sha256,
            "rows": len(source_rows),
        },
        "output": {
            "path": str(output_path.resolve()),
            "sha256": output_sha256,
            "rows": len(rows),
            "order_preserved": [row["source_record_id"] for row in rows] == source_ids,
        },
        "source_snapshot": {
            "mode": source_snapshot_mode,
            "path": str(snapshot_path.resolve()),
            "input_path": str(source_snapshot_input_path.resolve()) if source_snapshot_input_path else None,
            "sha256": tex_snapshot["sha256"],
        },
        "sources": {
            "texhibition": {
                "source_row_count": len(texhibition_rows),
                "input_row_count": sum(1 for row in rows if "texhibition" in str(row.get("source", "")).casefold()),
            "matched_row_count": source_match_counts["texhibition"],
            "covered_by_other_source_count": source_covered_counts["texhibition"],
            "accounted_row_count": accounted_counts["texhibition"],
            "snapshot_sha256": tex_snapshot["sha256"],
                "error_type": source_errors.get("texhibition", {}).get("error_type"),
                "error": source_errors.get("texhibition", {}).get("error"),
                "unavailable_status_counts": source_unavailable_counts["texhibition"],
                "timestamp": source_timestamps.get("texhibition"),
            },
            "zuchex": {
                "source_row_count": len(zuchex_rows),
                "input_row_count": sum(1 for row in rows if "zuchex" in str(row.get("source", "")).casefold()),
            "matched_row_count": source_match_counts["zuchex"],
            "covered_by_texhibition_count": source_covered_counts["zuchex"],
            "accounted_row_count": accounted_counts["zuchex"],
                "error_type": source_errors.get("zuchex", {}).get("error_type"),
                "error": source_errors.get("zuchex", {}).get("error"),
                "unavailable_status_counts": source_unavailable_counts["zuchex"],
                "timestamp": source_timestamps.get("zuchex"),
            },
        },
    }
    manifest_path = output_path.with_suffix(".manifest.json")
    manifest_temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    try:
        manifest_temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_temporary.replace(manifest_path)
    finally:
        manifest_temporary.unlink(missing_ok=True)
    if expected_snapshot is None:
        _write_json_atomic(snapshot_path, tex_snapshot)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fuar katilimci listelerinden firma adi ve website ceker.")
    parser.add_argument(
        "--source",
        choices=["all", *SCRAPERS.keys()],
        default="all",
        help="Cekilecek kaynak. Varsayilan: all",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=config.INPUT_FILE,
        help="Olusacak Excel dosyasi. Varsayilan: input/firms.xlsx",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="Detay sayfalarina girip website arama. Daha hizli calisir.",
    )
    parser.add_argument(
        "--enrich-existing", action="store_true",
        help="Enrich input/firms.xlsx into input/firms_enriched.xlsx using source keys",
    )
    parser.add_argument(
        "--replay-source-snapshot", type=Path, default=None,
        help="Replay a locked Texhibition source-row snapshot instead of fetching live details",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Sayfalar arasi bekleme suresi. Varsayilan: 0.4 saniye",
    )
    return parser.parse_args()


def run(source: str, output: Path, fetch_details: bool, delay: float) -> list[dict]:
    ensure_directories()
    rows: list[dict] = []
    selected_sources = SCRAPERS.keys() if source == "all" else [source]

    for selected_source in selected_sources:
        scraper = SCRAPERS[selected_source]
        source_rows = scraper(fetch_details=fetch_details, delay_sec=delay)
        print(f"{selected_source}: {len(source_rows)} firma")
        rows.extend(source_rows)

    rows = dedupe_rows(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=output.suffix + ".staging", dir=output.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        excel.write_company_records(temporary, rows)
        # Re-open before publication so a partial/invalid workbook is never
        # allowed to replace the last known-good input.
        excel.read_company_records(temporary)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Toplam benzersiz firma: {len(rows)}")
    print(f"Website bulunan: {sum(1 for row in rows if row.get('website'))}")
    print(f"Yazildi: {output}")
    return rows


if __name__ == "__main__":
    args = parse_args()
    if args.enrich_existing:
        target = args.output if args.output != config.INPUT_FILE else config.INPUT_FILE.with_name("firms_enriched.xlsx")
        replay_rows = None
        replay_mode = "live"
        if args.replay_source_snapshot is not None:
            snapshot_payload = json.loads(args.replay_source_snapshot.read_text(encoding="utf-8"))
            replay_rows = snapshot_payload.get("rows") if isinstance(snapshot_payload, dict) else snapshot_payload
            if not isinstance(replay_rows, list):
                raise ValueError("source snapshot must contain a rows list")
            replay_mode = "locked_replay"
        print(enrich_existing_workbook(
            config.INPUT_FILE, target, delay=args.delay,
            texhibition_rows=replay_rows,
            source_snapshot_mode=replay_mode,
            source_snapshot_input_path=args.replay_source_snapshot,
        ))
    else:
        run(args.source, args.output, fetch_details=not args.no_details, delay=args.delay)
