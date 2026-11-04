"""Bounded physical discovery for a single SOV table with a row-1–10 header."""

import re
from dataclasses import dataclass

from openpyxl.cell.cell import Cell
from openpyxl.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from . import resources
from .errors import FailureCategory, IngestionError
from .manifest import Diagnostic, SourceColumn

SIGNATURE = {
    "EPIC Code",
    "Property Name",
    "State",
    "Building",
    "Contents",
    "Rental Income",
    "Total Insurable Values",
    "Construction Type",
}
SECTION_HEADINGS = {
    "Pool",
    "Security Guards",
    "Hurricane Exposed Locations - Secondary Characteristics",
    "Earthquake  Exposed Locations",
}

SUMMARY_LABELS = {
    "Building",
    "Contents",
    "Rental Income",
    "Total Insurable Values",
    "Number Of Units (for Apartments)",
    "Square Footage (Apartments)",
    "Square Footage (Commercial)",
    "Total Square Footage",
}


@dataclass(frozen=True)
class Structure:
    worksheet: str
    header_row: int
    columns: tuple[SourceColumn, ...]
    asset_rows: tuple[int, ...]
    excluded: dict[str, int]
    diagnostics: tuple[Diagnostic, ...]


def populated(value: object) -> bool:
    return value is not None and value != ""


def preamble_kind(sheet: Worksheet, row: int) -> str | None:
    cells = [c for c in sheet[row] if isinstance(c, Cell) and populated(c.value)]
    if not cells:
        return "blank"
    if all(c.data_type in {"s", "inlineStr"} and isinstance(c.value, str) for c in cells) and (
        (len(cells) == 1 and cells[0].column == 1)
        or all(c.value in SECTION_HEADINGS for c in cells)
    ):
        return "metadata"
    return None


def _validate_merges(sheet: Worksheet, header: int) -> None:
    """Allow only single-row, recognized section-heading merges before this header."""
    for merged in sheet.merged_cells.ranges:
        anchor = sheet.cell(merged.min_row, merged.min_col)
        cells = [c for c in sheet[merged.min_row] if isinstance(c, Cell) and populated(c.value)]
        if (
            merged.min_row == merged.max_row < header
            and anchor.data_type in {"s", "inlineStr"}
            and isinstance(anchor.value, str)
            and anchor.value in SECTION_HEADINGS
            and all(
                c.data_type in {"s", "inlineStr"}
                and isinstance(c.value, str)
                and c.value in SECTION_HEADINGS
                for c in cells
            )
        ):
            continue
        raise IngestionError(
            FailureCategory.STRUCTURE, f"{sheet.title}!{merged}: unsupported merged cells"
        )


def discover_header(workbook: Workbook, *, infer_schema: bool = False) -> Structure:
    resources.check_loaded_workbook(workbook)
    candidates: list[tuple[Worksheet, int]] = []
    for sheet in workbook.worksheets:
        assert isinstance(sheet, Worksheet)
        for row_number, row in enumerate(sheet, 1):
            labels = {c.value for c in row if isinstance(c.value, str)}
            if len(labels & SIGNATURE) >= 3:
                candidates.append((sheet, row_number))
    if infer_schema:
        for sheet in workbook.worksheets:
            for row_number in range(1, min(10, sheet.max_row) + 1):
                if (sheet, row_number) in candidates:
                    continue
                # Recognized section rows are preamble, even with three or more
                # distinct labels immediately above the real header.
                if preamble_kind(sheet, row_number) == "metadata":
                    continue
                row = sheet[row_number]
                labels = {c.value for c in row if isinstance(c.value, str) and populated(c.value)}
                populated_headers = [c for c in row if isinstance(c, Cell) and populated(c.value)]
                if len(labels) < 3 or not populated_headers:
                    continue
                if min(c.column for c in populated_headers) != 1:
                    continue
                if any(
                    populated(c.value)
                    and (not isinstance(c.value, str) or c.data_type not in {"s", "inlineStr"})
                    for c in row
                ):
                    continue
                width = max(c.column for c in populated_headers)
                if sum(populated(c.value) for c in sheet[row_number + 1][:width]) < 3:
                    continue
                # Invalid merges must fail, not hide an otherwise credible table.
                _validate_merges(sheet, row_number)
                if any(preamble_kind(sheet, r) is None for r in range(1, row_number)):
                    continue
                if width > resources.MAX_INFERENCE_COLUMNS:
                    raise IngestionError(
                        FailureCategory.STRUCTURE, "Unsupported inference column span"
                    )
                candidates.append((sheet, row_number))
    if len(candidates) != 1:
        raise IngestionError(
            FailureCategory.STRUCTURE,
            f"Expected one SOV table; found {len(candidates)} candidate headers"
            + (": " + ", ".join(f"{s.title}!{r}" for s, r in candidates) if candidates else ""),
        )
    sheet, header = candidates[0]
    if not 1 <= header <= 10:
        raise IngestionError(
            FailureCategory.STRUCTURE, f"{sheet.title}!{header}: supported header rows are 1–10"
        )
    _validate_merges(sheet, header)
    header_cells = [c for c in sheet[header] if isinstance(c, Cell) and populated(c.value)]
    width = max(c.column for c in header_cells)
    columns: list[SourceColumn] = []
    for cell in sheet[header][:width]:
        if not isinstance(cell, Cell):
            raise IngestionError(FailureCategory.STRUCTURE, "Merged header cells are unsupported")
        if cell.value is not None and (
            not isinstance(cell.value, str) or cell.data_type not in {"s", "inlineStr"}
        ):
            raise IngestionError(FailureCategory.STRUCTURE, f"Non-text header at {cell.coordinate}")
        columns.append(SourceColumn(index=cell.column, letter=cell.column_letter, label=cell.value))
    excluded = {"metadata": 0, "summary": 0, "blank": 0}
    diagnostics: list[Diagnostic] = []

    def exclude(row: int, kind: str) -> None:
        excluded[kind] += 1
        diagnostics.append(
            Diagnostic(
                code=f"excluded_{kind}",
                message=f"Excluded {kind} row",
                worksheet=sheet.title,
                row=row,
            )
        )

    for r in range(1, header):
        kind = preamble_kind(sheet, r)
        if kind is None:
            raise IngestionError(
                FailureCategory.STRUCTURE, f"{sheet.title}!{r}: unsupported document metadata"
            )
        exclude(r, kind)
    # Reject width and blank-boundary violations before any semantic/model operation.
    ended = False
    seen = False
    for r in range(header + 1, sheet.max_row + 1):
        cells = [c for c in sheet[r] if isinstance(c, Cell) and populated(c.value)]
        if not cells:
            ended = True
        elif ended or any(c.column > width for c in cells):
            raise IngestionError(
                FailureCategory.STRUCTURE,
                f"{sheet.title}!{r}: populated row outside table boundary",
            )
        else:
            seen = True
    if not seen:
        raise IngestionError(FailureCategory.STRUCTURE, f"{sheet.title}: no asset records")
    return Structure(sheet.title, header, tuple(columns), (), excluded, tuple(diagnostics))


def finalize_rows(
    workbook: Workbook,
    structure: Structure,
    identifier_column: int | None,
    tiv_column: int | None,
    *,
    inferred: bool = False,
) -> Structure:
    resources.check_loaded_workbook(workbook)
    sheet = workbook[structure.worksheet]
    header, columns = structure.header_row, structure.columns
    width = len(columns)
    excluded = dict(structure.excluded)
    diagnostics = list(structure.diagnostics)

    def exclude(row: int, kind: str) -> None:
        excluded[kind] += 1
        diagnostics.append(
            Diagnostic(
                code=f"excluded_{kind}",
                message=f"Excluded {kind} row",
                worksheet=sheet.title,
                row=row,
            )
        )

    id_columns = [] if identifier_column is None else [identifier_column]
    asset_rows: list[int] = []
    ended = False
    for r in range(header + 1, sheet.max_row + 1):
        cells = [c for c in sheet[r] if isinstance(c, Cell) and populated(c.value)]
        if not cells:
            ended = bool(asset_rows)
            exclude(r, "blank")
            continue
        if any(c.column > width for c in cells):
            raise IngestionError(
                FailureCategory.STRUCTURE,
                f"{sheet.title}!{r}: populated cells outside source columns",
            )
        # Only a formula-only SUM row spanning precisely the preceding data is recognized.
        is_summary = (
            bool(asset_rows)
            and not excluded["summary"]
            and (
                not inferred
                or (
                    identifier_column is not None
                    and not populated(sheet.cell(r, identifier_column).value)
                    and any(c.column == tiv_column for c in cells)
                )
            )
            and all(
                (
                    columns[c.column - 1].label in SUMMARY_LABELS
                    or (inferred and c.column == tiv_column)
                )
                and c.data_type == "f"
                and isinstance(c.value, str)
                and re.fullmatch(
                    rf"=SUM\(\$?{c.column_letter}\$?{header + 1}:"
                    rf"\$?{c.column_letter}\$?{asset_rows[-1]}\)",
                    c.value,
                    re.IGNORECASE,
                )
                for c in cells
            )
        )
        if is_summary:
            ended = True
            exclude(r, "summary")
            continue
        if ended:
            raise IngestionError(
                FailureCategory.STRUCTURE, f"{sheet.title}!{r}: populated row after table boundary"
            )
        if len(id_columns) == 1 and not populated(sheet.cell(r, id_columns[0]).value):
            # A partial asset must never disappear as an assumed summary or blank.
            raise IngestionError(
                FailureCategory.DATA,
                f"{sheet.title}!{sheet.cell(r, id_columns[0]).coordinate}: "
                "missing asset identifier in populated row",
            )
        asset_rows.append(r)
    if not asset_rows:
        raise IngestionError(FailureCategory.STRUCTURE, f"{sheet.title}: no asset records")
    return Structure(
        sheet.title, header, tuple(columns), tuple(asset_rows), excluded, tuple(diagnostics)
    )


def discover(workbook: Workbook) -> Structure:
    """source-label compatibility entry point; the same seam is used with inference."""
    structure = discover_header(workbook)
    ids = [c.index for c in structure.columns if c.label == "EPIC Code"]
    return finalize_rows(workbook, structure, ids[0] if len(ids) == 1 else None, None)
