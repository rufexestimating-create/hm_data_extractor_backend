"""
Export extracted data to CSV and Excel files.
"""

import csv
import os

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

import config


HEADERS = [
    "Name",
    "Website",
    "Contact Page",
    "Email(s)",
    "Phone(s)",
    "Address",
    "State",
    "License Number",
    "Facebook",
    "Instagram",
    "Twitter / X",
    "LinkedIn",
    "YouTube",
    "Source URL",
    "Website Source",
]

HEADERS_FAST = [
    "Name",
    "Website",
    "Email(s)",
    "Phone",
    "State",
    "License Number",
    "Source URL",
    "Website Source",
]


def _is_fast(records: list[dict]) -> bool:
    """Detect if records came from fast extraction (have 'phone' str key, no 'phones' list)."""
    if not records:
        return False
    return "phone" in records[0] and "phones" not in records[0]


def _flatten_record(record: dict) -> list[str]:
    """Turn an extraction dict into a flat row for export."""
    socials = record.get("socials", {})
    phones = record.get("phones", [])
    if isinstance(phones, str):
        phones = [phones] if phones else []
    return [
        record.get("name") or "",
        record.get("website") or "",
        record.get("contact_page") or record.get("website_status") or "",
        " | ".join(record.get("emails", [])),
        " | ".join(phones),
        record.get("address") or "",
        record.get("state") or "",
        record.get("license") or "",
        socials.get("facebook", ""),
        socials.get("instagram", ""),
        socials.get("twitter", ""),
        socials.get("linkedin", ""),
        socials.get("youtube", ""),
        record.get("page_url") or "",
        record.get("website_source") or "",
    ]


def _flatten_record_fast(record: dict) -> list[str]:
    """Turn a fast-mode extraction dict into a flat row."""
    return [
        record.get("name") or "",
        record.get("website") or "",
        " | ".join(record.get("emails", [])),
        record.get("phone") or "",
        record.get("state") or "",
        record.get("license") or "",
        record.get("page_url") or "",
        record.get("website_source") or "",
    ]


def save_csv(records: list[dict], filename: str | None = None) -> str:
    """Save records to a CSV file. Returns the file path."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    filename = filename or config.DEFAULT_FILENAME
    path = os.path.join(config.OUTPUT_DIR, f"{filename}.csv")

    fast = _is_fast(records)
    headers = HEADERS_FAST if fast else HEADERS
    flatten = _flatten_record_fast if fast else _flatten_record

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for rec in records:
            writer.writerow(flatten(rec))

    return os.path.abspath(path)


def save_excel(records: list[dict], filename: str | None = None) -> str:
    """Save records to a styled Excel file. Returns the file path."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    filename = filename or config.DEFAULT_FILENAME
    path = os.path.join(config.OUTPUT_DIR, f"{filename}.xlsx")

    fast = _is_fast(records)
    headers = HEADERS_FAST if fast else HEADERS
    flatten = _flatten_record_fast if fast else _flatten_record

    wb = Workbook()
    ws = wb.active
    ws.title = "Extracted Data"

    # --- Styles ---
    header_font = Font(bold=True, color="FFFFFF", size=12)
    header_fill = PatternFill(start_color="2B579A", end_color="2B579A", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    # Write header row
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border

    # Write data rows
    for row_idx, rec in enumerate(records, 2):
        row_data = flatten(rec)
        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border = thin_border
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    # Auto-width columns
    for col_idx, header in enumerate(headers, 1):
        max_len = len(header)
        for row_idx in range(2, len(records) + 2):
            val = ws.cell(row=row_idx, column=col_idx).value or ""
            max_len = max(max_len, min(len(str(val)), 50))
        ws.column_dimensions[chr(64 + col_idx) if col_idx <= 26 else "A" + chr(64 + col_idx - 26)].width = max_len + 4

    # Freeze header row
    ws.freeze_panes = "A2"

    # Auto-filter
    ws.auto_filter.ref = ws.dimensions

    wb.save(path)
    return os.path.abspath(path)


class IncrementalCSVWriter:
    """Append records to CSV one at a time — crash-safe incremental saving."""

    def __init__(self, filename: str | None = None, fast: bool = False):
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        filename = filename or config.DEFAULT_FILENAME
        self.path = os.path.join(config.OUTPUT_DIR, f"{filename}.csv")
        self.fast = fast
        self._flatten = _flatten_record_fast if fast else _flatten_record
        self._file = open(self.path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(HEADERS_FAST if fast else HEADERS)
        self._file.flush()

    def append(self, record: dict):
        """Write one record and flush to disk immediately."""
        self._writer.writerow(self._flatten(record))
        self._file.flush()

    def close(self):
        if not self._file.closed:
            self._file.flush()
            self._file.close()
