"""Best-effort, atomic run summaries alongside the immutable run artifacts."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import uuid

from openpyxl import Workbook

import config
from modules import excel, redaction, scorer


logger = logging.getLogger(__name__)

HEADERS = [
    "Firma", "Web sitesi", "Web kaynağı", "Web güven", "E-posta",
    "E-posta kaynağı", "E-posta güven", "Telefon", "Telefon kaynağı",
    "Telefon güven", "Diğer telefonlar", "Referans web sitesi",
    "Referans durumu", "Referans sinyalleri", "Referans telefonu",
    "Girdi web durumu", "Eksik alanlar", "Yayına hazır", "Durum",
    "Ücretli durum", "Not",
]
ALLOWED_STATUSES = {
    "TAMAMLANDI", "KISMI_UCRETLI_ONAY_BEKLIYOR", "KISMI_ZAMANLAYICI_DURDU",
    "KISMI_KOSU_HATASI", "KISMI_KULLANICI_DURDURDU",
}


def _safe(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return excel._safe_cell_value(value)
    return excel._safe_cell_value(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _table_row(row: dict) -> dict[str, object]:
    last_error = str(row.get("last_error", "") or "")
    paid_status = str(row.get("paid_state", "") or "")
    if last_error:
        paid_status = f"{paid_status} — {last_error}" if paid_status else last_error
    gaps = row.get("field_gaps", "")
    if isinstance(gaps, (list, tuple, set)):
        gaps = ";".join(sorted(str(value) for value in gaps))
    if not gaps:
        gaps = ";".join(
            field for field in ("website", "email", "phone") if not row.get(field)
        )
    ready = row.get("ready_for_publication", row.get("publication_eligible", False))
    return {
        "Firma": row.get("company", ""),
        "Web sitesi": row.get("website", ""),
        "Web kaynağı": row.get("website_source", ""),
        "Web güven": row.get("website_confidence", ""),
        "E-posta": row.get("email", ""),
        "E-posta kaynağı": row.get("email_source_tier", row.get("email_source", "")),
        "E-posta güven": row.get("email_confidence", ""),
        "Telefon": row.get("phone", ""),
        "Telefon kaynağı": row.get("phone_source_tier", row.get("phone_source", "")),
        "Telefon güven": row.get("phone_confidence", ""),
        "Diğer telefonlar": row.get("alternative_phones", row.get("alternative_phone", "")),
        "Referans web sitesi": row.get("listed_website", row.get("reference_website", "")),
        "Referans durumu": row.get("reference_tier", ""),
        "Referans sinyalleri": row.get("reference_signals", ""),
        "Referans telefonu": row.get("listed_phone", ""),
        "Girdi web durumu": row.get("listed_website_status", ""),
        "Eksik alanlar": gaps,
        "Yayına hazır": bool(ready),
        "Durum": row.get("status", ""),
        "Ücretli durum": paid_status,
        "Not": str(row.get("reason", "") or "")[:300],
    }


def _atomic_text(path: Path, value: str, *, encoding: str = "utf-8") -> None:
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp{path.suffix}")
    try:
        temporary.write_text(value, encoding=encoding, newline="")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _sheet_value(stage: dict, field: str) -> object:
    return stage.get(field, "") if isinstance(stage, dict) else ""


def _confidence(stage: dict, field: str) -> str:
    value = _sheet_value(stage, f"{field}_confidence")
    return str(value or "").upper()


def _ready(stage: dict) -> bool:
    if not isinstance(stage, dict):
        return False
    return bool(stage.get("ready_for_publication", stage.get("publication_eligible", False)))


def _stage_summary(rows: list[dict]) -> list[dict[str, object]]:
    stages = (
        ("A — Bağımsız arama (referanssız)", "stage_a"),
        ("A + Referans", "stage_ab"),
        ("Final (A + Referans + Ücretli)", "final"),
    )
    fields = ("website", "email", "phone", "ready")
    result: list[dict[str, object]] = []
    total = len(rows)
    for stage_label, stage_key in stages:
        values = []
        for field in fields:
            count = 0
            for row in rows:
                stage = row if stage_key == "final" else row.get(stage_key, {})
                if field == "ready":
                    count += int(_ready(stage))
                    continue
                count += int(bool(_sheet_value(stage, field)) and _confidence(stage, field) in {"HIGH", "MEDIUM"})
            values.append(f"{count} ({count / total * 100:.1f}%)" if total else "0 (0.0%)")
        result.append({"stage": stage_label, **dict(zip(fields, values))})
    return result


def _md(value: object) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _build_report(
    rows: list[dict], *, run_id: str, run_status: str, status_detail: str,
    elapsed_seconds: float | None, telemetry: dict | None, generated_at: str,
    file_names: list[str],
) -> str:
    started_at = "bilinmiyor"
    try:
        from modules import checkpoint
        state = checkpoint.load_run_state_by_id(run_id)
        if state:
            context = state.get("context") or {}
            started_at = str(context.get("started_at", started_at))
    except Exception:
        logger.exception("run start time could not be read for report run_id=%s", run_id)
    elapsed = f"{float(elapsed_seconds):.1f} sn" if elapsed_seconds is not None else "bilinmiyor"
    table = _stage_summary(rows)
    lines = [
        "# Koşu raporu", "", f"- Koşu kimliği: `{_md(run_id)}`",
        f"- Durum: `{_md(run_status)}`", f"- Durum ayrıntısı: {_md(status_detail)}",
        f"- Başlangıç: {_md(started_at)}", f"- Bitiş: {_md(generated_at)}",
        f"- Süre: {_md(elapsed)}", "", "## Oran tablosu", "",
        "| Aşama | Web sitesi | E-posta | Telefon | Yayına hazır |",
        "|---|---:|---:|---:|---:|",
    ]
    lines.extend(
        f"| {_md(row['stage'])} | {_md(row['website'])} | {_md(row['email'])} | {_md(row['phone'])} | {_md(row['ready'])} |"
        for row in table
    )
    matches = 0
    comparable = 0
    for row in rows:
        stage_a = row.get("stage_a") if isinstance(row.get("stage_a"), dict) else {}
        site = str(stage_a.get("website", "") or "")
        reference = str(row.get("listed_website", row.get("reference_website", "")) or "")
        if site and reference:
            comparable += 1
            try:
                matches += int(scorer.same_registrable_domain(site, reference))
            except Exception:
                pass
    hit_rate = f"{matches / comparable * 100:.1f}%" if comparable else "0.0%"
    lines.extend(["", "## Bağımsız arama isabet vekili", "", f"A isabeti (referansla aynı alan adı): {matches} / {comparable} ({hit_rate})"])

    confidence_fields = ("website", "email", "phone")
    lines.extend(["", "## Güven dağılımı", "", "| Alan | HIGH | MEDIUM | LOW | NONE |", "|---|---:|---:|---:|---:|"])
    for field in confidence_fields:
        counts = {level: 0 for level in ("HIGH", "MEDIUM", "LOW", "NONE")}
        for row in rows:
            level = str(row.get(f"{field}_confidence", "NONE") or "NONE").upper()
            counts[level if level in counts else "NONE"] += 1
        lines.append(f"| {_md(field)} | {counts['HIGH']} | {counts['MEDIUM']} | {counts['LOW']} | {counts['NONE']} |")

    lines.extend(["", "## Kaynak dağılımı", ""])
    for field, key in (("Web sitesi", "website_source"), ("E-posta", "email_source_tier"), ("Telefon", "phone_source_tier")):
        counts: dict[str, int] = {}
        for row in rows:
            source = str(row.get(key, "") or "NONE")
            counts[source] = counts.get(source, 0) + 1
        lines.append(f"- {field}: " + ", ".join(f"{_md(name)}={count}" for name, count in sorted(counts.items())))

    lines.extend(["", "## Referans durumu ve girdi temizliği", ""])
    for key, label in (("reference_tier", "Referans"), ("listed_website_status", "Girdi web")):
        counts: dict[str, int] = {}
        for row in rows:
            value = str(row.get(key, "") or "NONE")
            counts[value] = counts.get(value, 0) + 1
        lines.append(f"- {label}: " + (", ".join(f"{_md(name)}={count}" for name, count in sorted(counts.items())) or "veri yok"))

    paid_indexes = sum(bool(row.get("paid_required")) and int(row.get("paid_attempts", 0) or 0) > 0 for row in rows)
    skipped = sum(bool(row.get("paid_required")) and int(row.get("paid_attempts", 0) or 0) == 0 for row in rows)
    lines.extend(["", "## Ücretli aşama", "", f"- İşlem gören firma: {paid_indexes}", f"- Atlanan ücretli firma: {skipped}"])
    provider_budgets = (telemetry or {}).get("provider_budgets", {}) if isinstance(telemetry, dict) else {}
    if isinstance(provider_budgets, dict) and provider_budgets:
        lines.extend(["", "| Sağlayıcı | Çağrı | Başarılı | Başarısız | Belirsiz | Bütçe |", "|---|---:|---:|---:|---:|---:|"])
        for provider, values in sorted(provider_budgets.items()):
            if not isinstance(values, dict):
                continue
            total_calls = sum(int(values.get(key, 0) or 0) for key in ("done", "failed", "unknown", "reserved"))
            lines.append(f"| {_md(provider)} | {total_calls} | {values.get('done', 0)} | {values.get('failed', 0)} | {values.get('unknown', 0)} | {values.get('effective_limit', 0)} |")
    else:
        lines.append("- Sağlayıcı bütçe ve çağrı verisi: ölçülmedi")

    lines.extend(["", "## Ücretsiz arama canary ve motor sağlığı", ""])
    health = {
        key: value for key, value in ((telemetry or {}).get("counters", {}) if isinstance(telemetry, dict) else {}).items()
        if "canary" in str(key).casefold() or "engine" in str(key).casefold()
    }
    lines.append(json.dumps(health, ensure_ascii=False, sort_keys=True) if health else "Canary/motor ölçümleri: ölçülmedi")

    lines.extend(["", "## Hatalar", ""])
    categories = (
        ("İzole edilen firmalar", lambda row: str(row.get("last_error", "")).startswith("invariant:")),
        ("PROCESSING_FAILED firmalar", lambda row: str(row.get("status", "")).upper() == "PROCESSING_FAILED"),
        ("Belirsiz/bütçesi yetmeyen ücretli firmalar", lambda row: str(row.get("last_error", "")) in {"provider_call_unknown_outcome", "paid_budget_exhausted"}),
    )
    for label, predicate in categories:
        lines.append(f"### {label}")
        matches_rows = [row for row in rows if predicate(row)]
        if matches_rows:
            lines.extend(f"- {_md(row.get('company', ''))}: {_md(str(row.get('last_error') or row.get('reason') or '')[:200])}" for row in matches_rows)
        else:
            lines.append("- Yok")
    lines.extend(["", "## Dosyalar", "", *[f"- `{_md(name)}`" for name in file_names], ""])
    return redaction.redact_text("\n".join(lines))


def write_run_report(
    rows: list[dict],
    *,
    output_root: Path,
    run_status: str,
    status_detail: str,
    elapsed_seconds: float | None,
    telemetry: dict | None = None,
) -> dict[str, str]:
    """Write all available summary files; collect every filesystem error."""
    errors: dict[str, str] = {}
    files: dict[str, str] = {}
    safe_status = str(run_status) if str(run_status) in ALLOWED_STATUSES else "KISMI_KOSU_HATASI"
    target_root = Path(output_root)
    normalized_rows = [dict(row) for row in rows if isinstance(row, dict)]
    table_rows = [_table_row(row) for row in normalized_rows]
    try:
        target_root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.exception("run report output directory could not be created: %s", target_root)
        errors["output_root"] = f"{type(exc).__name__}:{str(exc)[:300]}"

    workbook_path = target_root / "sonuclar.xlsx"
    try:
        book = Workbook()
        all_sheet = book.active
        all_sheet.title = "Tüm firmalar"
        for sheet, sheet_rows in (
            (all_sheet, table_rows),
            (book.create_sheet("Yayına hazır"), [row for row in table_rows if row.get("Yayına hazır") is True]),
            (book.create_sheet("Eksikler"), [row for row in table_rows if row.get("Eksik alanlar")]),
        ):
            sheet.append(HEADERS)
            for row in sheet_rows:
                sheet.append([_safe(row.get(header, "")) for header in HEADERS])
        summary = book.create_sheet("Özet")
        summary.append(["Aşama", "Web sitesi", "E-posta", "Telefon", "Yayına hazır"])
        for item in _stage_summary(normalized_rows):
            summary.append([item[key] for key in ("stage", "website", "email", "phone", "ready")])
        temporary = target_root / f".sonuclar.{uuid.uuid4().hex}.tmp.xlsx"
        try:
            book.save(temporary)
            temporary.replace(workbook_path)
        finally:
            temporary.unlink(missing_ok=True)
        files[workbook_path.name] = hashlib.sha256(workbook_path.read_bytes()).hexdigest()
    except Exception as exc:
        logger.exception("run summary workbook could not be written")
        errors["sonuclar.xlsx"] = f"{type(exc).__name__}:{str(exc)[:300]}"
        csv_path = target_root / "sonuclar.csv"
        try:
            temporary = target_root / f".sonuclar.{uuid.uuid4().hex}.tmp.csv"
            try:
                with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=HEADERS, delimiter=";", extrasaction="ignore")
                    writer.writeheader()
                    for row in table_rows:
                        writer.writerow({key: _safe(row.get(key, "")) for key in HEADERS})
                temporary.replace(csv_path)
            finally:
                temporary.unlink(missing_ok=True)
            files[csv_path.name] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        except Exception as csv_exc:
            logger.exception("run summary CSV fallback could not be written")
            errors["sonuclar.csv"] = f"{type(csv_exc).__name__}:{str(csv_exc)[:300]}"

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = str(normalized_rows[0].get("run_id", "") or target_root.parent.name)
    output_names = list(files)
    output_names.append("rapor.md")
    report_path = target_root / "rapor.md"
    try:
        markdown = _build_report(
            normalized_rows, run_id=run_id, run_status=safe_status,
            status_detail=str(status_detail)[:300], elapsed_seconds=elapsed_seconds,
            telemetry=telemetry, generated_at=generated_at, file_names=output_names + ["run_status.json"],
        )
        _atomic_text(report_path, markdown)
        files[report_path.name] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    except Exception as exc:
        logger.exception("run markdown report could not be written")
        errors["rapor.md"] = f"{type(exc).__name__}:{str(exc)[:300]}"

    status_payload = {
        "run_status": safe_status,
        "status_detail": str(status_detail)[:300],
        "generated_at": generated_at,
        "row_count": len(normalized_rows),
        "files": files,
    }
    status_path = target_root / "run_status.json"
    try:
        _atomic_text(status_path, json.dumps(status_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        files[status_path.name] = hashlib.sha256(status_path.read_bytes()).hexdigest()
    except Exception as exc:
        logger.exception("run status file could not be written")
        errors["run_status.json"] = f"{type(exc).__name__}:{str(exc)[:300]}"
    return errors
