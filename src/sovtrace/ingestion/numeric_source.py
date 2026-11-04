"""Retain numeric XML lexemes that openpyxl would otherwise decode through float.

openpyxl remains responsible for workbook structure, types and formula handling.
This supplement preserves exact original/cached decimal numbers for source fidelity.
"""

from decimal import Decimal, InvalidOperation
from io import BytesIO
from zipfile import ZipFile

from openpyxl.utils import get_column_letter

from .errors import FailureCategory, IngestionError
from .resources import SHEET_NS, inspect_workbook, xml_events


def numeric_values(content: bytes, worksheet: str) -> dict[str, Decimal]:
    # The helper also defends direct callers. It never builds a worksheet tree.
    sheets = inspect_workbook(content)
    with ZipFile(BytesIO(content)) as archive:
        result: dict[str, Decimal] = {}
        row = column = 0
        reference = ""
        numeric = False
        with xml_events(archive, sheets[worksheet]) as events:
            for event, element in events:
                if event == "start" and element.tag == SHEET_NS + "row":
                    row = int(element.attrib.get("r", row + 1))
                    column = 0
                elif event == "start" and element.tag == SHEET_NS + "c":
                    reference = element.attrib.get("r", f"{get_column_letter(column + 1)}{row}")
                    from openpyxl.utils.cell import column_index_from_string, coordinate_from_string

                    letter, _ = coordinate_from_string(reference)
                    column = column_index_from_string(letter)
                    numeric = element.attrib.get("t", "n") == "n"
                elif (
                    event == "end"
                    and element.tag == SHEET_NS + "v"
                    and numeric
                    and element.text is not None
                ):
                    try:
                        number = Decimal(element.text)
                    except InvalidOperation as exc:
                        raise IngestionError(
                            FailureCategory.DATA, f"{worksheet}!{reference}: invalid source number"
                        ) from exc
                    if not number.is_finite():
                        raise IngestionError(
                            FailureCategory.DATA,
                            f"{worksheet}!{reference}: non-finite source number",
                        )
                    result[reference] = number
                elif event == "end" and element.tag == SHEET_NS + "c":
                    numeric = False
        return result
