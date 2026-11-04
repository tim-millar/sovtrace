"""Fixed synthetic corpus and oracle. No imports from the application under evaluation.

Coordinates use declared layouts, never a discovered manifest or evidence. Business
expectations are literals; raw fixture dollars and normalized oracle micro-USD are
separately specified. This is deliberately a fixed corpus, not an SOV interpreter.
"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

VERSION = "synthetic-corpus-oracle-v1"
SOURCE_WORKSHEET = "Statement of Values"
FIELDS = (
    "asset_id",
    "property_name",
    "city",
    "state",
    "total_insurable_value_micro_usd",
    "construction_type",
)
TIV = FIELDS[4]
HEADERS = (
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
)
RENAMED = (
    "Location Reference",
    "Premises",
    "Municipality",
    "US State",
    "Total Insured Value",
    "Building Construction",
)
# Independently declared complete semantic population, in semantic-field order.
POPULATION = (
    ("SYN-001", "Synthetic One", "Phoenix", "AZ", 12000000000000, "Wood Frame - Podium"),
    ("SYN-002", "Synthetic Two", "Tucson", "AZ", 8000000000000, "Wood Frame - Low Rise"),
    ("SYN-003", "Synthetic Three", "New York", "NY", 3000000000000, "Wood Frame - Low Rise"),
    ("SYN-004", "Synthetic Four", "Los Angeles", "CA", 5000000000000, "Wood Frame - Wrap"),
)
ORDER = ("SYN-001", "SYN-002", "SYN-003", "SYN-004", "SYN-001")
RAW_AMOUNTS = (12000000, 8000000, 3000000, 5000000)
USD_TEXT = ("$12,000,000.00", "$8,000,000.00", "$3,000,000.00", "$5,000,000.00")


@dataclass(frozen=True)
class Fixture:
    id: str
    header: int = 1
    infer: bool = False
    columns: tuple[int | None, ...] = (1, 2, 3, 4, 5, 6)
    failure_category: str | None = None
    failure_code: str | None = None
    failure_contains: str | None = None
    failure_prefix: str | None = None

    @property
    def mappings(self) -> dict[str, int | None]:
        return dict(zip(FIELDS, self.columns, strict=True))

    @property
    def population(self) -> dict[str, dict]:
        result: dict[str, dict] = {
            row[0]: dict(zip(FIELDS, row, strict=True)) for row in POPULATION
        }
        if self.id == "E5":
            for record in result.values():
                record["construction_type"] = None
        if self.id == "E9":
            result["SYN-003"][TIV] = None
        return result

    @property
    def membership(self) -> dict[str, list[int]]:
        return {
            key: [self.header + i for i, asset in enumerate(ORDER, 1) if asset == key]
            for key in self.population
        }

    @property
    def exclusions(self) -> dict[int, str]:
        metadata = (
            {1, 2, 3, 4}
            if self.header == 5
            else ({1, 3, 5, 6} if self.header == 8 else ({1} if self.header == 3 else set()))
        )
        return {
            **{
                r: "excluded_metadata" if r in metadata else "excluded_blank"
                for r in range(1, self.header)
            },
            self.header + 6: "excluded_summary",
            self.header + 7: "excluded_blank",
            self.header + 8: "excluded_blank",
        }

    def source_value(self, asset: str, field: str):
        index = int(asset[-3:]) - 1
        if field == TIV:
            if self.id == "E9" and asset == "SYN-003":
                return "12,34,567.00"
            return USD_TEXT[index] if self.id in ("E8", "E9", "E11") else RAW_AMOUNTS[index]
        values: dict[str, str | int] = dict(zip(FIELDS, POPULATION[index], strict=True))
        return values[field]

    def cell(self, asset: str, row: int, field: str) -> dict:
        column = self.mappings[field]
        assert column is not None
        letter = get_column_letter(column)
        return {
            "semantic_field": field,
            "column_index": column,
            "column_letter": letter,
            "cell_coordinate": f"{letter}{row}",
            "source_header": RENAMED[FIELDS.index(field)]
            if self.id in ("E1", "E2", "E11")
            else HEADERS[FIELDS.index(field)],
            "original_value": self.source_value(asset, field),
            "normalized_value": self.population[asset][field],
        }


FIXTURES = (
    Fixture("E0a"),
    Fixture("E0b", header=5),
    Fixture("E1", infer=True),
    Fixture("E2", infer=True, columns=(14, 13, 12, 11, 10, 9)),
    Fixture("E3", header=3),
    Fixture("E4", header=8),
    Fixture("E5", infer=True, columns=(1, 2, 3, 4, 5, None)),
    Fixture(
        "E6",
        infer=True,
        columns=(1, 2, 3, None, 4, 5),
        failure_category="mapping",
        failure_code="mapping_missing_required",
        failure_contains="state",
    ),
    Fixture(
        "E7",
        infer=True,
        columns=(1, 2, 3, 4, None, 6),
        failure_category="mapping",
        failure_code="mapping_ambiguous",
    ),
    Fixture("E8"),
    Fixture("E9"),
    Fixture(
        "E10a",
        header=3,
        failure_category="structure",
        failure_contains="unsupported document metadata",
    ),
    Fixture(
        "E10b",
        failure_category="structure",
        failure_prefix="Expected one SOV table; found 2 candidate headers",
    ),
    Fixture("E11", header=8, infer=True, columns=(3, 2, 4, 6, 11, 13)),
)
BY_ID = {f.id: f for f in FIXTURES}


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    operation: Literal["exists", "count", "sum", "list"]
    sql: str
    value: bool | int | tuple[str, ...]
    members: tuple[str, ...]
    required: tuple[str, ...]


AZ = "SELECT COUNT(*) FROM assets WHERE state = 'AZ'"
THRESHOLD = "state = 'AZ' AND total_insurable_value_micro_usd > 10000000000000"
QUESTIONS = (
    Question(
        "Q1",
        "Are there assets in Arizona?",
        "exists",
        AZ,
        True,
        ("SYN-001", "SYN-002"),
        ("asset_id", "state"),
    ),
    Question(
        "Q2",
        "How many assets are in Arizona?",
        "count",
        AZ,
        2,
        ("SYN-001", "SYN-002"),
        ("asset_id", "state"),
    ),
    Question(
        "Q3",
        "What is the aggregate TIV of Arizona assets?",
        "sum",
        "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'AZ'",
        20000000000000,
        ("SYN-001", "SYN-002"),
        ("asset_id", "state", TIV),
    ),
    Question(
        "Q4",
        "How many Arizona assets are worth more than $10 million?",
        "count",
        f"SELECT COUNT(*) FROM assets WHERE {THRESHOLD}",
        1,
        ("SYN-001",),
        ("asset_id", "state", TIV),
    ),
    Question(
        "Q5",
        "What is the TIV of Arizona assets worth over $10 million?",
        "sum",
        f"SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE {THRESHOLD}",
        12000000000000,
        ("SYN-001",),
        ("asset_id", "state", TIV),
    ),
    Question(
        "Q6",
        "Which assets have Wood Frame - Podium construction?",
        "list",
        "SELECT asset_id, construction_type FROM assets WHERE construction_type = 'Wood Frame - Podium'",
        ("SYN-001",),
        ("SYN-001",),
        ("asset_id", "construction_type"),
    ),
    Question(
        "Q7",
        "Are there assets in Florida?",
        "exists",
        "SELECT COUNT(*) FROM assets WHERE state = 'FL'",
        False,
        (),
        ("asset_id", "state"),
    ),
    Question(
        "Q8",
        "What is the aggregate TIV of all assets?",
        "sum",
        "SELECT SUM(total_insurable_value_micro_usd) FROM assets",
        28000000000000,
        ("SYN-001", "SYN-002", "SYN-003", "SYN-004"),
        ("asset_id", TIV),
    ),
)


def expected_failure(fixture: Fixture, question: Question) -> tuple[str, ...]:
    if fixture.id == "E5" and question.id == "Q6":
        return ("unsupported_question", "unavailable_field")
    if fixture.id == "E9" and question.id in ("Q4", "Q5", "Q8"):
        return ("data_incomplete",)
    return ()


def workbook_identity(source: Path) -> dict[str, str]:
    """Independent identity from fixture bytes/name and the declared worksheet."""
    digest = sha256(source.read_bytes()).hexdigest()
    return {
        "workbook_sha256": digest,
        "workbook_filename": source.name,
        "worksheet": SOURCE_WORKSHEET,
        "dataset_id": f"{digest}:{SOURCE_WORKSHEET}",
    }


def write_fixture(path: Path, fixture: Fixture) -> None:
    """Use the existing workbook_factory's row/header/summary generation pattern."""
    labels: list[str] = list(HEADERS)
    if fixture.id in ("E1", "E2", "E11"):
        labels[:6] = RENAMED
    data = []
    for i in (0, 1, 2, 3, 0):
        row = list(POPULATION[i])
        row[4] = USD_TEXT[i] if fixture.id in ("E8", "E9", "E11") else RAW_AMOUNTS[i]
        if fixture.id == "E9" and i == 2:
            row[4] = "12,34,567.00"
        row.extend((100, 20, 3, "1 Synthetic Street", "First", "A", "B", "Second"))
        data.append(row)
    permutation = list(range(14))
    if fixture.id == "E2":
        permutation.reverse()
    elif fixture.id == "E11":
        permutation = [6, 1, 0, 2, 7, 3, 8, 9, 10, 11, 4, 12, 5, 13]
    elif fixture.id == "E5":
        permutation.remove(5)
    elif fixture.id == "E6":
        permutation.remove(3)
    labels = [labels[i] for i in permutation]
    data = [[row[i] for i in permutation] for row in data]
    if fixture.id == "E7":
        labels[4] = "Total Insured Value"
        labels.append("TIV")
        for row in data:
            row.append(11000000)
    wb = Workbook()
    wb.properties.created = wb.properties.modified = datetime(2026, 1, 1)
    ws = wb.active
    assert ws is not None
    ws.title = "Statement of Values"
    preamble = {
        3: {"A1": "Synthetic Company"},
        5: {
            "A1": "Synthetic Company",
            "A2": "Statement of Values",
            "A3": "Synthetic Reporting Period",
            "G4": "Pool",
        },
        8: {
            "A1": "Synthetic Company",
            "A3": "Statement of Values",
            "A5": "Synthetic Reporting Period",
            "G6": "Pool",
        },
    }.get(fixture.header, {})
    if fixture.id == "E10a":
        preamble = {"A1": "Reporting Period", "B1": "Synthetic October"}
    for coordinate, value in preamble.items():
        ws[coordinate] = value
    for column, label in enumerate(labels, 1):
        ws.cell(fixture.header, column, label)
    for row, values in enumerate(data, fixture.header + 1):
        for column, value in enumerate(values, 1):
            ws.cell(row, column, value)
    # The TIV formula moves with its physical heading, including renamed headings.
    for column, label in enumerate(labels, 1):
        if label in ("Building", "Total Insurable Values", "Total Insured Value"):
            letter = get_column_letter(column)
            ws.cell(
                fixture.header + 6,
                column,
                f"=SUM({letter}{fixture.header + 1}:{letter}{fixture.header + 5})",
            )
    ws.cell(fixture.header + 8, 1).number_format = "@"
    if fixture.id == "E10b":
        wb.copy_worksheet(ws).title = "Second table"
    wb.save(path)
    wb.close()
    # Fix ZIP timestamps too: byte-identical corpus and application dataset identities.
    with ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    core = ET.fromstring(members["docProps/core.xml"])
    for name in ("created", "modified"):
        node = core.find("{http://purl.org/dc/terms/}" + name)
        assert node is not None
        node.text = "2026-01-01T00:00:00Z"
    members["docProps/core.xml"] = ET.tostring(core)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            info = ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
