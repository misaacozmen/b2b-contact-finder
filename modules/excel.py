from pathlib import Path
from typing import Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font


def read_companies(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []

    first_cell = str(rows[0][0] or "").strip().lower()
    start_index = 1 if first_cell == "company" else 0
    companies: list[str] = []
    for row in rows[start_index:]:
        value = row[0] if row else None
        if value is None:
            continue
        company = str(value).strip()
        if company:
            companies.append(company)
    return companies


def _write_rows(path: Path, headers: list[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in rows:
        sheet.append([row.get(header, "") for header in headers])

    for column in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 60)

    workbook.save(path)


def write_contacts(path: Path, rows: Iterable[dict]) -> None:
    _write_rows(path, ["company", "website", "email", "phone", "status", "confidence", "score", "reason"], rows)


def write_failed(path: Path, rows: Iterable[dict]) -> None:
    _write_rows(path, ["company", "status", "reason"], rows)


def write_website_candidates(path: Path, rows: Iterable[dict]) -> None:
    headers = [
        "company",
        "selected_website",
        "status",
        "confidence",
        "candidate_1_url",
        "candidate_1_score",
        "candidate_1_reason",
        "candidate_2_url",
        "candidate_2_score",
        "candidate_2_reason",
        "candidate_3_url",
        "candidate_3_score",
        "candidate_3_reason",
    ]
    _write_rows(path, headers, rows)
