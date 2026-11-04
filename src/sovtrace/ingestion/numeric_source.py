"""Retain numeric XML lexemes that openpyxl would otherwise decode through float.

openpyxl remains responsible for workbook structure, types and formula handling.
This supplement preserves exact original/cached decimal numbers for source fidelity.
"""

import posixpath
from decimal import Decimal, InvalidOperation
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .errors import FailureCategory, IngestionError

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def numeric_values(content: bytes, worksheet: str) -> dict[str, Decimal]:
    with ZipFile(BytesIO(content)) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet = next(
            s for s in workbook.findall("s:sheets/s:sheet", NS) if s.attrib["name"] == worksheet
        )
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = next(
            r.attrib["Target"] for r in relationships if r.attrib["Id"] == sheet.attrib[REL_ID]
        )
        member = (
            target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
        )
        root = ET.fromstring(archive.read(member))
        result: dict[str, Decimal] = {}
        for cell in root.findall("s:sheetData/s:row/s:c", NS):
            value = cell.find("s:v", NS)
            if cell.attrib.get("t", "n") != "n" or value is None or value.text is None:
                continue
            try:
                number = Decimal(value.text)
            except InvalidOperation as exc:
                raise IngestionError(
                    FailureCategory.DATA, f"{worksheet}!{cell.attrib['r']}: invalid source number"
                ) from exc
            if not number.is_finite():
                raise IngestionError(
                    FailureCategory.DATA,
                    f"{worksheet}!{cell.attrib['r']}: non-finite source number",
                )
            result[cell.attrib["r"]] = number
        return result
