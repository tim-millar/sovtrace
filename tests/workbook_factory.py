"""Non-sensitive workbook builders, including explicit cached formula results."""

from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

HEADERS = [
    "EPIC Code",
    "Property Name",
    "City",
    "State",
    "Total Insurable Values",
    "Construction Type",
    "Building",
    "Contents",
    "Rental Income",
    "Street Level Address",
    "Construction Quality",
    "Cladding Type",
    "Cladding Type",
    "Construction Quality",
]


def asset(
    identifier: Any = "TEST-1", amount: Any = "10000000.000001", state: Any = "AZ"
) -> list[Any]:
    return [
        identifier,
        "Synthetic property",
        "Test City",
        state,
        amount,
        "Wood Frame - Low Rise",
        100,
        20,
        3,
        "1 Synthetic Street",
        "First",
        "A",
        "B",
        "Second",
    ]


def write_workbook(
    path: Path,
    *,
    rows: list[list[Any]] | None = None,
    header: int = 1,
    headers: list[str] | None = None,
    summary: bool = True,
    trailing: int = 2,
    caches: dict[str, Any] | None = None,
    extra_table: bool = False,
    formats: dict[str, str] | None = None,
    iso_dates: bool = False,
    preamble: dict[str, Any] | None = None,
) -> Path:
    wb = Workbook(iso_dates=iso_dates)
    ws = wb.active
    assert ws is not None
    ws.title = "Statement of Values"
    labels = HEADERS if headers is None else headers
    data = [asset()] if rows is None else rows
    if header == 5:
        for r, label in enumerate(
            ["Synthetic company", "Statement of Values", "Synthetic period"], 1
        ):
            ws.cell(r, 1, label)
        ws.cell(4, 7, "Pool")
    for coordinate, value in (preamble or {}).items():
        ws[coordinate] = value
    for column, label in enumerate(labels, 1):
        ws.cell(header, column, label)
    for r, values in enumerate(data, header + 1):
        for column, value in enumerate(values, 1):
            ws.cell(r, column, value)
    last = header + len(data)
    if summary:
        for column in [
            i for i, h in enumerate(labels, 1) if h in {"Building", "Total Insurable Values"}
        ]:
            letter = get_column_letter(column)
            ws.cell(last + 1, column, f"=SUM({letter}{header + 1}:{letter}{last})")
        last += 1
    if trailing:
        ws.cell(last + trailing, 1).number_format = "@"
    if extra_table:
        wb.copy_worksheet(ws).title = "Second table"
    for coordinate, number_format in (formats or {}).items():
        ws[coordinate].number_format = number_format
    wb.save(path)
    wb.close()
    if caches is not None:
        patch_caches(path, caches)
    return path


def patch_caches(path: Path, caches: dict[str, Any]) -> None:
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(members["xl/worksheets/sheet1.xml"])
    for cell in root.findall(".//s:c", namespace):
        if cell.attrib["r"] not in caches:
            continue
        cached = cell.find("s:v", namespace)
        assert cached is not None
        value = caches[cell.attrib["r"]]
        if isinstance(value, str) and value.startswith("#"):
            cell.set("t", "e")
        elif isinstance(value, str):
            cell.set("t", "str")
        else:
            cell.set("t", "n")
        cached.text = None if value is None else str(value)
    members["xl/worksheets/sheet1.xml"] = ET.tostring(root)
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    path.write_bytes(stream.getvalue())


def patch_numbers(path: Path, numbers: dict[str, str]) -> None:
    """Write precise synthetic numeric XML without a binary-float round trip."""
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(members["xl/worksheets/sheet1.xml"])
    for cell in root.findall(".//s:c", namespace):
        if cell.attrib["r"] not in numbers:
            continue
        cell.set("t", "n")
        value = cell.find("s:v", namespace)
        if value is None:
            value = ET.SubElement(cell, "{" + namespace["s"] + "}v")
        value.text = numbers[cell.attrib["r"]]
    members["xl/worksheets/sheet1.xml"] = ET.tostring(root)
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    path.write_bytes(stream.getvalue())
