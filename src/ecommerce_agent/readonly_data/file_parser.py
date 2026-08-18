from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree

from .adapters import ReportFileFormat


MAX_REPORT_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_XLSX_BYTES = 50 * 1024 * 1024
MAX_REPORT_ROWS = 50_000
MAX_REPORT_COLUMNS = 128

_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REFERENCE = re.compile(r"^([A-Z]+)[1-9][0-9]*$")


@dataclass(frozen=True, slots=True)
class ParsedReportRow:
    row_number: int
    values: dict[str, Any]
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedReport:
    headers: tuple[str, ...]
    rows: tuple[ParsedReportRow, ...]
    sheet_name: str | None
    excel_date_system: int | None


def parse_report_file(
    content: bytes,
    file_format: ReportFileFormat,
    *,
    sheet_name: str | None = None,
) -> ParsedReport:
    if not isinstance(content, bytes) or not content:
        raise ValueError("readonly_report_content_required")
    if len(content) > MAX_REPORT_BYTES:
        raise ValueError("readonly_report_too_large")
    if file_format is ReportFileFormat.CSV:
        return _parse_csv(content)
    return _parse_xlsx(content, sheet_name=sheet_name)


def _parse_csv(content: bytes) -> ParsedReport:
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("readonly_csv_utf8_required") from exc
    try:
        records = csv.reader(io.StringIO(text, newline=""), strict=True)
        header_row = next(records)
    except StopIteration as exc:
        raise ValueError("readonly_report_empty") from exc
    except csv.Error as exc:
        raise ValueError("readonly_csv_invalid") from exc
    headers = _validate_headers(header_row)
    parsed: list[ParsedReportRow] = []
    try:
        for row_number, row in enumerate(records, start=1):
            if row_number > MAX_REPORT_ROWS:
                raise ValueError("readonly_report_row_limit_exceeded")
            error_code = None
            if len(row) != len(headers):
                error_code = "report_row_width_invalid"
            values = {
                header: row[index] if index < len(row) else None
                for index, header in enumerate(headers)
            }
            parsed.append(
                ParsedReportRow(
                    row_number=row_number,
                    values=values,
                    error_code=error_code,
                )
            )
    except csv.Error as exc:
        raise ValueError("readonly_csv_invalid") from exc
    if not parsed:
        raise ValueError("readonly_report_empty")
    return ParsedReport(
        headers=headers,
        rows=tuple(parsed),
        sheet_name=None,
        excel_date_system=None,
    )


def _parse_xlsx(content: bytes, *, sheet_name: str | None) -> ParsedReport:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError("readonly_xlsx_invalid") from exc
    with archive:
        members = archive.infolist()
        names = {member.filename for member in members}
        if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts for name in names):
            raise ValueError("readonly_xlsx_path_invalid")
        if sum(member.file_size for member in members) > MAX_UNCOMPRESSED_XLSX_BYTES:
            raise ValueError("readonly_xlsx_uncompressed_limit_exceeded")
        if "xl/vbaProject.bin" in names:
            raise ValueError("xlsx_macro_forbidden")
        if any(name.startswith("xl/externalLinks/") for name in names):
            raise ValueError("xlsx_external_link_forbidden")
        required = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
        if not required <= names:
            raise ValueError("readonly_xlsx_workbook_missing")

        workbook = _xml(archive.read("xl/workbook.xml"))
        relationships = _xml(archive.read("xl/_rels/workbook.xml.rels"))
        workbook_properties = workbook.find(f"{{{_SPREADSHEET_NS}}}workbookPr")
        date_1904 = (
            workbook_properties is not None
            and workbook_properties.attrib.get("date1904", "").lower() in {"1", "true"}
        )
        relationship_targets = {
            relationship.attrib.get("Id", ""): relationship.attrib.get("Target", "")
            for relationship in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
        }
        sheets: list[tuple[str, str]] = []
        for sheet in workbook.findall(f".//{{{_SPREADSHEET_NS}}}sheet"):
            name = sheet.attrib.get("name", "")
            relationship_id = sheet.attrib.get(f"{{{_OFFICE_REL_NS}}}id", "")
            target = relationship_targets.get(relationship_id, "")
            if name and target:
                sheets.append((name, _xlsx_target(target)))
        if not sheets:
            raise ValueError("readonly_xlsx_sheet_missing")
        if sheet_name is None:
            if len(sheets) != 1:
                raise ValueError("xlsx_sheet_selection_required")
            selected_name, selected_path = sheets[0]
        else:
            selected = [item for item in sheets if item[0] == sheet_name]
            if len(selected) != 1:
                raise ValueError("xlsx_sheet_not_found")
            selected_name, selected_path = selected[0]
        if selected_path not in names:
            raise ValueError("readonly_xlsx_sheet_missing")

        shared_strings = _shared_strings(archive, names)
        worksheet = _xml(archive.read(selected_path))
        if worksheet.find(f".//{{{_SPREADSHEET_NS}}}hyperlink") is not None:
            raise ValueError("xlsx_hyperlink_forbidden")
        rows = worksheet.findall(f".//{{{_SPREADSHEET_NS}}}sheetData/{{{_SPREADSHEET_NS}}}row")
        if not rows:
            raise ValueError("readonly_report_empty")
        materialized = [
            _xlsx_row(row, shared_strings=shared_strings)
            for row in rows
        ]
        headers = _validate_headers(materialized[0])
        parsed: list[ParsedReportRow] = []
        for row_number, row in enumerate(materialized[1:], start=1):
            if row_number > MAX_REPORT_ROWS:
                raise ValueError("readonly_report_row_limit_exceeded")
            error_code = None
            if len(row) > len(headers):
                error_code = "report_row_width_invalid"
            values = {
                header: row[index] if index < len(row) else None
                for index, header in enumerate(headers)
            }
            parsed.append(
                ParsedReportRow(
                    row_number=row_number,
                    values=values,
                    error_code=error_code,
                )
            )
        if not parsed:
            raise ValueError("readonly_report_empty")
        return ParsedReport(
            headers=headers,
            rows=tuple(parsed),
            sheet_name=selected_name,
            excel_date_system=1904 if date_1904 else 1900,
        )


def _validate_headers(value: list[str | None]) -> tuple[str, ...]:
    if not value or len(value) > MAX_REPORT_COLUMNS:
        raise ValueError("readonly_report_header_invalid")
    headers = tuple(item.strip() if isinstance(item, str) else "" for item in value)
    if any(not header for header in headers):
        raise ValueError("readonly_report_header_invalid")
    return headers


def _xml(content: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError("readonly_xlsx_xml_invalid") from exc


def _xlsx_target(target: str) -> str:
    base = target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target)
    normalized = posixpath.normpath(base)
    if normalized == "xl" or not normalized.startswith("xl/") or ".." in PurePosixPath(normalized).parts:
        raise ValueError("readonly_xlsx_path_invalid")
    return normalized


def _shared_strings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in names:
        return []
    root = _xml(archive.read(path))
    return [
        "".join(text.text or "" for text in item.findall(f".//{{{_SPREADSHEET_NS}}}t"))
        for item in root.findall(f"{{{_SPREADSHEET_NS}}}si")
    ]


def _xlsx_row(row: ElementTree.Element, *, shared_strings: list[str]) -> list[str | None]:
    values: dict[int, str | None] = {}
    for cell in row.findall(f"{{{_SPREADSHEET_NS}}}c"):
        if cell.find(f"{{{_SPREADSHEET_NS}}}f") is not None:
            raise ValueError("xlsx_formula_forbidden")
        reference = cell.attrib.get("r", "")
        match = _CELL_REFERENCE.fullmatch(reference)
        if match is None:
            raise ValueError("readonly_xlsx_cell_reference_invalid")
        column_index = _column_index(match.group(1))
        if column_index >= MAX_REPORT_COLUMNS:
            raise ValueError("readonly_report_column_limit_exceeded")
        cell_type = cell.attrib.get("t")
        if cell_type == "inlineStr":
            value = "".join(
                text.text or ""
                for text in cell.findall(f".//{{{_SPREADSHEET_NS}}}t")
            )
        else:
            node = cell.find(f"{{{_SPREADSHEET_NS}}}v")
            value = node.text if node is not None else None
            if cell_type == "s" and value is not None:
                try:
                    value = shared_strings[int(value)]
                except (IndexError, ValueError) as exc:
                    raise ValueError("readonly_xlsx_shared_string_invalid") from exc
            elif cell_type == "b" and value is not None:
                value = "true" if value == "1" else "false"
        values[column_index] = value
    if not values:
        return []
    return [values.get(index) for index in range(max(values) + 1)]


def _column_index(letters: str) -> int:
    value = 0
    for character in letters:
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


__all__ = [
    "ParsedReport",
    "ParsedReportRow",
    "parse_report_file",
]
