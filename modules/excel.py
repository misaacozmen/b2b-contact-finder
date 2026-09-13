from pathlib import Path
from typing import Iterable
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED, ZipInfo
from datetime import datetime
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

import config
from modules import redaction


_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _safe_cell_value(value: object) -> object:
    """Prevent credentials from persisting and user strings from being formulas."""
    if isinstance(value, str):
        value = redaction.redact_text(value)
        if value.lstrip().startswith(_FORMULA_PREFIXES):
            return f"'{value}"
    return value


def _unescape_cell_value(value: object) -> object:
    """Restore text escaped by this module when reading its own workbooks."""
    if (
        isinstance(value, str)
        and value.startswith("'")
        and value[1:].lstrip().startswith(_FORMULA_PREFIXES)
    ):
        return value[1:]
    return value


def _read_rows(path: Path) -> list[tuple]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    if path.stat().st_size > config.MAX_WORKBOOK_FILE_BYTES:
        raise ValueError(f"Workbook exceeds file size limit: {path}")
    try:
        with ZipFile(path) as archive:
            expanded_size = sum(item.file_size for item in archive.infolist())
    except BadZipFile as exc:
        raise ValueError(f"Invalid XLSX archive: {path}") from exc
    if expanded_size > config.MAX_WORKBOOK_UNCOMPRESSED_BYTES:
        raise ValueError(f"Workbook exceeds expanded size limit: {path}")

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows: list[tuple] = []
        for index, row in enumerate(workbook.active.iter_rows(values_only=True)):
            if index >= config.MAX_WORKBOOK_ROWS + 1:
                raise ValueError(f"Workbook exceeds row limit: {path}")
            if any(
                isinstance(value, str)
                and len(value) > config.MAX_WORKBOOK_CELL_CHARS
                for value in row
            ):
                raise ValueError(f"Workbook contains an oversized cell: {path}")
            rows.append(row)
        return rows
    finally:
        workbook.close()


def read_companies(path: Path) -> list[str]:
    rows = _read_rows(path)
    if not rows:
        return []

    first_cell = str(_unescape_cell_value(rows[0][0] or "")).strip().lower()
    start_index = 1 if first_cell == "company" else 0
    companies: list[str] = []
    for row in rows[start_index:]:
        value = row[0] if row else None
        if value is None:
            continue
        value = _unescape_cell_value(value)
        company = str(value).strip()
        if company:
            companies.append(company)
    return companies


def read_company_records(path: Path) -> list[dict]:
    rows = _read_rows(path)
    if not rows:
        return []

    first_row = [
        str(_unescape_cell_value(value or "")).strip().lower()
        for value in rows[0]
    ]
    has_header = "company" in first_row
    headers = first_row if has_header else []
    start_index = 1 if has_header else 0

    def value_for(row: tuple, names: tuple[str, ...], fallback_index: int | None = None) -> str:
        for name in names:
            if name in headers:
                idx = headers.index(name)
                if idx < len(row) and row[idx] is not None:
                    return str(_unescape_cell_value(row[idx])).strip()
        if (
            not has_header
            and fallback_index is not None
            and fallback_index < len(row)
            and row[fallback_index] is not None
        ):
            return str(_unescape_cell_value(row[fallback_index])).strip()
        return ""

    records: list[dict] = []
    for row in rows[start_index:]:
        company = value_for(row, ("company", "firma", "firma adi", "firma adı"), 0)
        if not company:
            continue
        records.append(
            {
                "company": company,
                "website": value_for(row, ("website", "web sitesi", "websitesi", "site"), 1),
                "listed_website": value_for(
                    row, ("listed_website", "fair_website", "fuar web sitesi"), None
                ),
                "source": value_for(row, ("source", "kaynak"), None),
                "source_record_id": value_for(row, ("source_record_id", "source id", "record_id"), None),
                "country": value_for(row, ("country", "ulke", "ülke"), None),
                "profile_url": value_for(row, ("profile_url", "profil", "profile"), None),
                "listing_url": value_for(row, ("listing_url", "liste_url", "liste url"), None),
                "listed_phone": value_for(row, ("listed_phone", "fair_phone", "fuar telefonu"), None),
                "listed_phone_status": value_for(row, ("listed_phone_status", "phone_observation"), None),
                "listed_email": value_for(row, ("listed_email", "fair_email", "fuar e-posta"), None),
                "listed_address": value_for(row, ("listed_address", "fair_address", "fuar adresi"), None),
                "listed_address_status": value_for(row, ("listed_address_status", "address_observation"), None),
                "listed_legal_name": value_for(row, ("listed_legal_name", "legal_name", "ticari unvan", "ticari unvanı"), None),
                "source_detail_status": value_for(row, ("source_detail_status", "detail_status"), None),
                "source_detail_url": value_for(row, ("source_detail_url", "detail_url"), None),
                "source_detail_content_sha256": value_for(row, ("source_detail_content_sha256", "detail_content_sha256"), None),
                "source_evidence": value_for(row, ("source_evidence", "source_evidence_json"), None),
                "hall": value_for(row, ("hall", "salon"), None),
                "stand": value_for(row, ("stand", "stant"), None),
                "brands": value_for(row, ("brands", "markalar"), None),
                "representations": value_for(row, ("representations", "temsilcilikler"), None),
                "sector": value_for(row, ("sector", "sektor", "sektör", "urun grubu", "ürün grubu"), None),
                "description": value_for(row, ("description", "aciklama", "açıklama"), None),
                "_id": value_for(row, ("_id", "id"), None),
            }
        )
    return records


def read_result_statuses(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    rows = _read_rows(path)
    if not rows:
        return {}
    headers = [
        str(_unescape_cell_value(value or "")).strip().casefold()
        for value in rows[0]
    ]
    if "company" not in headers or "status" not in headers:
        return {}
    company_idx = headers.index("company")
    status_idx = headers.index("status")
    return {
        str(_unescape_cell_value(row[company_idx])).strip().casefold():
        str(_unescape_cell_value(row[status_idx] or "")).strip()
        for row in rows[1:]
        if len(row) > max(company_idx, status_idx) and row[company_idx]
    }


def read_result_statuses_by_source_id(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    rows = _read_rows(path)
    if not rows:
        return {}
    headers = [str(_unescape_cell_value(value or "")).strip().casefold() for value in rows[0]]
    if "source_record_id" not in headers or "status" not in headers:
        return {}
    source_idx, status_idx = headers.index("source_record_id"), headers.index("status")
    return {
        str(_unescape_cell_value(row[source_idx])).strip(): str(_unescape_cell_value(row[status_idx] or "")).strip()
        for row in rows[1:]
        if len(row) > max(source_idx, status_idx) and row[source_idx]
    }


_FROZEN_XLSX_TIMESTAMP = "2000-01-01T00:00:00+00:00"


def _normalize_xlsx_zip(path: Path) -> None:
    temporary = path.with_name(f".{path.name}.normalized")
    with ZipFile(path, "r") as source, ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=9) as target:
        for name in sorted(source.namelist()):
            info = ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0
            info.comment = b""
            info.extra = b""
            data = source.read(name)
            if name == "docProps/core.xml":
                data = re.sub(
                    rb"(<dcterms:modified[^>]*>).*?(</dcterms:modified>)",
                    rb"\g<1>2000-01-01T00:00:00Z\g<2>", data,
                )
            target.writestr(info, data)
    temporary.replace(path)


def _write_rows(path: Path, headers: list[str], rows: Iterable[dict], *, frozen_timestamp: str = _FROZEN_XLSX_TIMESTAMP) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    try:
        frozen_datetime = datetime.fromisoformat(str(frozen_timestamp).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        frozen_datetime = datetime(2000, 1, 1)
    workbook.properties.created = frozen_datetime
    workbook.properties.modified = frozen_datetime
    workbook.properties.lastModifiedBy = "B2B Contact Finder"
    sheet = workbook.active
    sheet.append([_safe_cell_value(header) for header in headers])
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in rows:
        sheet.append([_safe_cell_value(row.get(header, "")) for header in headers])

    for column in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 60)

    workbook.save(path)
    _normalize_xlsx_zip(path)


def write_contacts(path: Path, rows: Iterable[dict], *, frozen_timestamp: str = _FROZEN_XLSX_TIMESTAMP) -> None:
    _write_rows(
        path,
        [
            "company",
            "entity_id",
            "legacy_index",
            "source_record_id",
            "source_record_id_quality",
            "free_state",
            "paid_required",
            "paid_state",
            "paid_recommended",
            "paid_skipped_reason",
            "delivery_state",
            "website",
            "website_source",
            "website_status",
            "email",
            "email_source",
            "email_source_url",
            "alternative_emails",
            "alternative_email_sources",
            "email_verification",
            "email_verification_reason",
            "email_publication_status",
            "email_publication_reason",
            "phone",
            "phone_source",
            "phone_source_url",
            "phone_label",
            "alternative_phones",
            "alternative_phone_sources",
            "phone_publication_status",
            "phone_publication_reason",
            "contact_policy_version",
            "contact_status",
            "status",
            "confidence",
            "score",
            "publication_policy_version",
            "publication_policy_action",
            "publication_eligible",
            "publication_safety_score",
            "publication_risk_index",
            "publication_risk_tier",
            "publication_blockers",
            "collision_reason",
            "reason",
            "quarantine_state",
            "quarantine_status",
        ],
        rows,
        frozen_timestamp=frozen_timestamp,
    )


def write_failed(path: Path, rows: Iterable[dict], *, frozen_timestamp: str = _FROZEN_XLSX_TIMESTAMP) -> None:
    _write_rows(path, ["company", "status", "reason"], rows, frozen_timestamp=frozen_timestamp)


def write_website_candidates(path: Path, rows: Iterable[dict], *, frozen_timestamp: str = _FROZEN_XLSX_TIMESTAMP) -> None:
    headers = [
        "company",
        "source_record_id",
        "selected_website",
        "status",
        "paid_recommended",
        "paid_skipped_reason",
        "confidence",
        "publication_policy_version",
        "publication_policy_action",
        "publication_eligible",
        "publication_safety_score",
        "publication_risk_index",
        "publication_risk_tier",
        "publication_blockers",
        "candidate_1_url",
        "candidate_1_score",
        "candidate_1_reason",
        "candidate_1_query",
        "candidate_1_role",
        "candidate_2_url",
        "candidate_2_score",
        "candidate_2_reason",
        "candidate_2_query",
        "candidate_2_role",
        "candidate_3_url",
        "candidate_3_score",
        "candidate_3_reason",
        "candidate_3_query",
        "candidate_3_role",
    ]
    _write_rows(path, headers, rows, frozen_timestamp=frozen_timestamp)


def write_company_records(path: Path, rows: Iterable[dict]) -> None:
    _write_rows(
        path,
        [
            "company", "website", "listed_website", "source", "country",
            "profile_url", "listing_url", "listed_phone", "listed_email", "listed_address",
            "hall", "stand", "brands", "representations",
            "listed_legal_name", "source_detail_status", "source_detail_url",
            "source_detail_content_sha256", "source_evidence", "sector", "description",
            "source_record_id", "_id", "listed_phone_status", "listed_address_status",
        ],
        rows,
    )
