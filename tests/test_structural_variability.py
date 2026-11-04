"""structural variability S0–S12: bounded structure, strict money, and original-coordinate evidence."""

import json
import sqlite3
from datetime import date
from pathlib import Path
from runpy import run_path
from typing import Any

import pytest
from openpyxl import load_workbook
from test_ingestion import saved_manifest
from test_qa import FakeGenerator
from test_schema_inference import EXPECTED, RENAMED, FakeInference, proposal
from workbook_factory import HEADERS, asset, write_workbook

from sovtrace.ingestion import IngestionError, ingest_workbook
from sovtrace.ingestion.errors import FailureCategory
from sovtrace.ingestion.normalize import INT64_MAX, INT64_MIN, money
from sovtrace.qa import Failure, Success, ask
from sovtrace.qa.contracts import ListValue

PREAMBLE = {
    "A1": "Synthetic Company",
    "A3": "Statement of Values",
    "A5": "Synthetic Reporting Period",
    "G6": "Pool",
    "H6": "Security Guards",
}
# Fixed physical positions, independent of provider output and persisted mappings.
ORDER = [6, 7, 0, 1, 2, 3, 8, 9, 10, 11, 4, 12, 5, 13]
POSITIONS = dict(zip(EXPECTED, [3, 4, 5, 6, 11, 13], strict=True))
LETTERS = dict(zip(EXPECTED, ["C", "D", "E", "F", "K", "M"], strict=True))
TIV = "total_insurable_value_micro_usd"


def business_workbook(
    path: Path, header: int, *, renamed: bool = False, merged_section: bool = False
) -> Path:
    rows = [
        asset("SYN-001", "$12,000,000.00", " Arizona "),
        asset("SYN-002", "USD 8,000,000.00", "AZ"),
        asset("SYN-003", "3,000,000.00", "New York"),
        asset("SYN-004", "5000000.00", "CA"),
        asset("SYN-001", "$12,000,000.00", " Arizona "),
    ]
    for row, construction in zip(
        rows,
        [
            "Wood Frame - Podium",
            "Wood Frame - Low Rise",
            "Wood Frame - Low Rise",
            "Wood Frame - Wrap",
            "Wood Frame - Podium",
        ],
        strict=True,
    ):
        row[5] = construction
    labels = RENAMED if renamed else HEADERS
    order = ORDER if renamed else list(range(len(labels)))
    source = write_workbook(
        path,
        header=header,
        headers=[labels[i] for i in order],
        rows=[[row[i] for i in order] for row in rows],
        summary=False,
        trailing=0,
        preamble=PREAMBLE if header == 8 else None,
    )
    wb = load_workbook(source)
    ws = wb["Statement of Values"]
    if merged_section:
        section_row = 4 if header == 5 else 6
        ws[f"BG{section_row}"] = "Pool"
        ws.merge_cells(f"BG{section_row}:BY{section_row}")
        ws[f"D{section_row}"] = "Security Guards"
        ws.merge_cells(f"D{section_row}:F{section_row}")
    letter = "K" if renamed else "E"
    ws[f"{letter}{header + 6}"] = f"=sum(${letter}${header + 1}:{letter}${header + 5})"
    ws.cell(header + 8, 1).number_format = "@"
    wb.save(source)
    wb.close()
    return source


@pytest.mark.parametrize(
    ("header", "merged_section"),
    [(header, False) for header in range(1, 11)] + [(5, True), (8, True)],
)
@pytest.mark.parametrize("renamed", [False, True])
def test_s0_s5_s10_s12_business_pipeline(
    tmp_path: Path, header: int, renamed: bool, merged_section: bool
) -> None:
    source = business_workbook(
        tmp_path / "business.xlsx", header, renamed=renamed, merged_section=merged_section
    )
    db = tmp_path / "business.sqlite"
    positions = POSITIONS if renamed else EXPECTED
    letters = LETTERS if renamed else dict(zip(EXPECTED, "ABCDEF", strict=True))
    fake = FakeInference(proposal(positions))
    report: dict = ingest_workbook(
        source, db, infer_schema=renamed, inference_provider=fake if renamed else None
    )
    assert len(fake.requests) == int(renamed)
    assert (
        report["physical_records"],
        report["logical_assets"],
        report["verified_duplicate_groups"],
    ) == (5, 4, 1)
    assert report["valuation_complete"] is True
    metadata = 4 if header in (5, 8) else 0
    assert report["excluded_rows"] == {
        "metadata": metadata,
        "summary": 1,
        "blank": header - 1 - metadata + 2,
    }
    manifest = saved_manifest(db)
    assert (manifest["header_row"], manifest["data_start_row"], manifest["data_end_row"]) == (
        header,
        header + 1,
        header + 5,
    )
    assert {f: m["column"] for f, m in manifest["concepts"].items()} == positions
    for quality in report["field_completeness"].values():
        assert quality == dict(
            availability="mapped",
            physical_non_null=5,
            physical_total=5,
            logical_non_null=4,
            logical_total=4,
        )
    if merged_section:
        labels = RENAMED if renamed else HEADERS
        order = ORDER if renamed else list(range(len(labels)))
        assert [c["label"] for c in manifest["columns"]] == [labels[i] for i in order]
    with sqlite3.connect(db) as c:
        if merged_section:
            metadata_rows = [1, 2, 3, 4] if header == 5 else [1, 3, 5, 6]
            assert c.execute(
                "SELECT worksheet, row_number FROM diagnostics WHERE code='excluded_metadata' "
                "ORDER BY row_number"
            ).fetchall() == [("Statement of Values", row) for row in metadata_rows]
            assert c.execute("SELECT COUNT(*) FROM source_cells").fetchone() == (70,)
        assert c.execute(
            "SELECT asset_id, state, total_insurable_value_micro_usd FROM assets ORDER BY asset_id"
        ).fetchall() == [
            ("SYN-001", "AZ", 12000000000000),
            ("SYN-002", "AZ", 8000000000000),
            ("SYN-003", "NY", 3000000000000),
            ("SYN-004", "CA", 5000000000000),
        ]
        assert c.execute("SELECT row_number FROM source_rows ORDER BY row_number").fetchall() == [
            (header + i,) for i in range(1, 6)
        ]
        if header == 8:
            assert c.execute(
                "SELECT code, row_number FROM diagnostics WHERE code LIKE 'excluded_%' "
                "ORDER BY row_number"
            ).fetchall() == [
                ("excluded_metadata", 1),
                ("excluded_blank", 2),
                ("excluded_metadata", 3),
                ("excluded_blank", 4),
                ("excluded_metadata", 5),
                ("excluded_metadata", 6),
                ("excluded_blank", 7),
                ("excluded_summary", 14),
                ("excluded_blank", 15),
                ("excluded_blank", 16),
            ]
    source.unlink()
    demo = run_path(str(Path(__file__).parents[1] / "scripts/demo.py"))
    contributors = [
        {"SYN-001", "SYN-002"},
        {"SYN-001", "SYN-002"},
        {"SYN-001", "SYN-002"},
        {"SYN-001"},
        {"SYN-001"},
        {"SYN-001"},
        set(),
        {"SYN-001", "SYN-002", "SYN-003", "SYN-004"},
    ]
    offsets = {"SYN-001": [1, 5], "SYN-002": [2], "SYN-003": [3], "SYN-004": [4]}
    original_amounts = {
        "SYN-001": "$12,000,000.00",
        "SYN-002": "USD 8,000,000.00",
        "SYN-003": "3,000,000.00",
        "SYN-004": "5000000.00",
    }
    normalized_amounts = {
        "SYN-001": 12000000000000,
        "SYN-002": 8000000000000,
        "SYN-003": 3000000000000,
        "SYN-004": 5000000000000,
    }
    for case, ids in zip(demo["CASES"], contributors, strict=True):
        answer = ask(db, case.question, demo["FixedSQLGenerator"](case))
        assert isinstance(answer, Success)
        result = (
            len(answer.result.records)
            if isinstance(answer.result, ListValue)
            else answer.result.value
        )
        assert result == case.expected
        assert answer.matching_count == case.matching_count
        assert answer.evidence.source_record_count == case.physical_count
        assert answer.verification.reconciled and answer.verification.complete
        assert answer.verification.same_snapshot
        assert {c.asset_id for c in answer.evidence.contributors} == ids
        for contributor in answer.evidence.contributors:
            assert [r.row for r in contributor.source_records] == [
                header + offset for offset in offsets[contributor.asset_id]
            ]
            for record in contributor.source_records:
                for cell in record.cells:
                    assert cell.cell_coordinate == f"{letters[cell.semantic_field]}{record.row}"
                    labels = RENAMED if renamed else HEADERS
                    assert cell.source_header == labels[EXPECTED[cell.semantic_field] - 1]
                    if cell.semantic_field == TIV:
                        assert cell.original_value == original_amounts[contributor.asset_id]
                        assert cell.normalized_value == normalized_amounts[contributor.asset_id]


@pytest.mark.parametrize(
    "preamble",
    [
        {"A2": "Late title", "J1": "Pool", "M1": "Security Guards"},
        {"K3": "Earthquake  Exposed Locations", "A6": "Title"},
        {"A7": "Title", "G2": "Pool", "A1": "Title"},
    ],
)
def test_s4_preamble_order_and_positions(tmp_path: Path, preamble: dict[str, Any]) -> None:
    source = write_workbook(tmp_path / "preamble.xlsx", header=8, preamble=preamble)
    report: dict = ingest_workbook(source, tmp_path / "preamble.sqlite")
    occupied = len({int(k.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) for k in preamble})
    assert report["excluded_rows"] == dict(metadata=occupied, blank=7 - occupied + 2, summary=1)


@pytest.mark.parametrize(
    "kind",
    [
        "interior_blank",
        "leading_data_blank",
        "after_summary",
        "second_summary",
        "wrong_range",
        "wrong_column",
        "footer",
        "multi_metadata",
        "numeric_metadata",
        "date_metadata",
        "bool_metadata",
        "formula_metadata",
        "error_metadata",
        "merged_metadata",
        "merged_header",
        "merged_data",
        "row11",
        "two_exact",
        "exact_and_renamed",
        "two_renamed",
        "late_second",
    ],
)
def test_s6_s9_structure_rejection_preserves_replacement(tmp_path: Path, kind: str) -> None:
    db = tmp_path / "existing.sqlite"
    ingest_workbook(write_workbook(tmp_path / "valid.xlsx"), db)
    original = db.read_bytes()
    source = business_workbook(tmp_path / "bad.xlsx", 11 if kind == "row11" else 8)
    wb = load_workbook(source)
    ws = wb["Statement of Values"]
    if kind in {"interior_blank", "leading_data_blank"}:
        ws.insert_rows(11 if kind == "interior_blank" else 9)
    elif kind in {"after_summary", "second_summary", "footer"}:
        ws["A15" if kind != "footer" else "B14"] = (
            "=SUM(A9:A13)" if kind == "second_summary" else "Unexpected"
        )
    elif kind == "wrong_range":
        ws["E14"] = "=SUM(E8:E13)"
    elif kind == "wrong_column":
        ws["E14"] = "=SUM(D9:D13)"
    elif kind == "multi_metadata":
        ws["B3"] = "Unsupported metadata"
    elif kind.endswith("_metadata") and kind != "merged_metadata":
        ws["A3"] = {
            "numeric_metadata": 1,
            "date_metadata": date(2026, 1, 1),
            "bool_metadata": True,
            "formula_metadata": "=1+1",
            "error_metadata": "#VALUE!",
        }[kind]
    elif kind.startswith("merged_"):
        ws.merge_cells(
            {"merged_metadata": "A1:B1", "merged_header": "B8:C8", "merged_data": "B9:C9"}[kind]
        )
    elif kind in {"two_exact", "exact_and_renamed", "two_renamed", "late_second"}:
        other = wb.copy_worksheet(ws)
        other.title = "Other"
        if kind in {"exact_and_renamed", "two_renamed"}:
            for col, label in enumerate(RENAMED, 1):
                other.cell(8, col, label)
        if kind == "two_renamed":
            for col, label in enumerate(RENAMED, 1):
                ws.cell(8, col, label)
        if kind == "late_second":
            other.insert_rows(1, 3)
    wb.save(source)
    wb.close()
    fake = FakeInference()
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, db, replace=True, infer_schema=True, inference_provider=fake)
    # Unrecognized formula/footer rows with blank IDs retain ingestion's fatal data category.
    assert exc.value.category in {FailureCategory.STRUCTURE, FailureCategory.DATA}
    assert not fake.requests
    assert db.read_bytes() == original
    assert not list(tmp_path.glob(".sov-import-*"))


@pytest.mark.parametrize("identifier", [None, "", "   ", 123, True, "=A9"])
def test_s11_shifted_invalid_identifier(tmp_path: Path, identifier: Any) -> None:
    source = write_workbook(
        tmp_path / "bad-id.xlsx", header=8, rows=[asset(identifier)], preamble=PREAMBLE
    )
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert exc.value.category == FailureCategory.DATA
    assert not (tmp_path / "no.sqlite").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_deterministic_wide_table_preserves_exact_label_compatibility(
    tmp_path: Path, enabled: bool
) -> None:
    labels = [*HEADERS, *[f"Extra {i}" for i in range(130)]]
    extras = [f"Extra value {i}" for i in range(130)]
    # All 144 data cells are text: row 9 looks like a header until its preamble
    # is checked, and row 10 independently supplies a qualifying following row.
    first = [str(value) for value in asset("WIDE-A", "$1,234.50")] + extras
    second = [str(value) for value in asset("WIDE-B", "2.000001")] + extras
    source = write_workbook(
        tmp_path / "wide.xlsx", header=8, headers=labels, rows=[first, first, second]
    )
    db = tmp_path / "wide.sqlite"
    fake = FakeInference()
    report: dict = ingest_workbook(
        source, db, infer_schema=enabled, inference_provider=fake if enabled else None
    )
    assert not fake.requests
    assert report["schema_inference"]["status"] == ("not_needed" if enabled else "disabled")
    assert (
        report["header_row"],
        report["physical_records"],
        report["logical_assets"],
        report["verified_duplicate_groups"],
        report["valuation_complete"],
    ) == (8, 3, 2, 1, True)
    for quality in report["field_completeness"].values():
        assert quality == dict(
            availability="mapped",
            physical_non_null=3,
            physical_total=3,
            logical_non_null=2,
            logical_total=2,
        )
    manifest = saved_manifest(db)
    assert len(manifest["columns"]) == 144
    assert [c["label"] for c in manifest["columns"]] == labels
    assert manifest["columns"][128] == dict(index=129, letter="DY", label="Extra 114")
    assert manifest["columns"][143] == dict(index=144, letter="EN", label="Extra 129")
    assert {f: m["column"] for f, m in manifest["concepts"].items()} == EXPECTED
    assert (manifest["data_start_row"], manifest["data_end_row"]) == (9, 11)
    with sqlite3.connect(db) as c:
        assert c.execute(
            "SELECT asset_id, state, total_insurable_value_micro_usd FROM assets ORDER BY asset_id"
        ).fetchall() == [("WIDE-A", "AZ", 1234500000), ("WIDE-B", "AZ", 2000001)]
        assert c.execute("SELECT row_number FROM source_rows ORDER BY row_number").fetchall() == [
            (9,),
            (10,),
            (11,),
        ]
        assert c.execute("SELECT COUNT(*) FROM source_cells").fetchone() == (432,)
        assert c.execute(
            "SELECT coordinate, original_value_json FROM source_cells "
            "WHERE column_index IN (129,144) ORDER BY row_number,column_index"
        ).fetchall() == [
            ("DY9", '"Extra value 114"'),
            ("EN9", '"Extra value 129"'),
            ("DY10", '"Extra value 114"'),
            ("EN10", '"Extra value 129"'),
            ("DY11", '"Extra value 114"'),
            ("EN11", '"Extra value 129"'),
        ]
    source.unlink()
    answer = ask(
        db,
        "Total TIV?",
        FakeGenerator("SELECT SUM(total_insurable_value_micro_usd) FROM assets", "sum"),
    )
    assert isinstance(answer, Success)
    assert not isinstance(answer.result, ListValue)
    assert answer.result.value == 1236500001
    assert answer.evidence.contributing_asset_count == 2
    assert answer.evidence.source_record_count == 3
    assert answer.verification.reconciled and answer.verification.complete
    assert answer.verification.same_snapshot
    assert {
        c.asset_id: [r.row for r in c.source_records] for c in answer.evidence.contributors
    } == {"WIDE-A": [9, 10], "WIDE-B": [11]}
    tiv_cells = [
        cell
        for contributor in answer.evidence.contributors
        for record in contributor.source_records
        for cell in record.cells
        if cell.semantic_field == TIV
    ]
    assert [(c.cell_coordinate, c.original_value, c.normalized_value) for c in tiv_cells] == [
        ("E9", "$1,234.50", 1234500000),
        ("E10", "$1,234.50", 1234500000),
        ("E11", "2.000001", 2000001),
    ]


@pytest.mark.parametrize("width", [129, 144])
def test_genuine_inference_header_width_limit(tmp_path: Path, width: int) -> None:
    source = write_workbook(
        tmp_path / "inferred-wide.xlsx",
        header=8,
        headers=[*RENAMED, *[f"Extra {i}" for i in range(width - len(RENAMED))]],
        rows=[asset()],
        summary=False,
        preamble=PREAMBLE,
    )
    fake = FakeInference()
    with pytest.raises(IngestionError, match="Unsupported inference column span") as exc:
        ingest_workbook(source, tmp_path / "no.sqlite", infer_schema=True, inference_provider=fake)
    assert exc.value.category == FailureCategory.STRUCTURE
    assert not fake.requests
    assert not (tmp_path / "no.sqlite").exists()
    assert not list(tmp_path.glob(".sov-import-*"))


@pytest.mark.parametrize(
    ("value", "expected", "code"),
    [
        (1200000, 1200000000000, None),
        ("1200000.00", 1200000000000, None),
        ("1,200,000.00", 1200000000000, None),
        ("$1,200,000.00", 1200000000000, None),
        ("USD 1,200,000.00", 1200000000000, None),
        (" $1,200,000.00 ", 1200000000000, None),
        ("1,234", 1234000000, None),
        ("12,345", 12345000000, None),
        ("123,456", 123456000000, None),
        ("12,345,678.90", 12345678900000, None),
        ("$1234.50", 1234500000, None),
        ("-1,234.50", -1234500000, None),
        ("-$1,234.50", -1234500000, None),
        ("-USD 1,234.50", -1234500000, None),
        ("usd   1.000001", 1000001, None),
        ("USD 123", 123000000, None),
        ("usd 123", 123000000, None),
        ("UsD 1,234.50", 1234500000, None),
        ("-uSd 1,234.50", -1234500000, None),
        ("$0.000001", 1, None),
        ("-$0", 0, None),
        ("$1,234.123456", 1234123456, None),
        ("$1,234.1234567", 1234123457, "precision_reduced"),
        ("-$1,234.1234565", -1234123457, "precision_reduced"),
        ("$9,223,372,036,854.775807", INT64_MAX, None),
        ("-USD 9,223,372,036,854.775808", INT64_MIN, None),
        ("1e-6", 1, None),
        ("+1.5", 1500000, None),
        (".5", 500000, None),
    ],
)
def test_money_accepted(value: Any, expected: int, code: str | None) -> None:
    assert money(value) == (expected, code)


@pytest.mark.parametrize(
    "value",
    [
        "12,34",
        "1234,567",
        "1,23,456",
        "1,234,56",
        "1,,234",
        "1,234,",
        "1,234.56.78",
        "12,34,567.00",
        "£1,200,000.00",
        "EUR 1,200,000.00",
        "1.200.000,00",
        "1.2m",
        "($1,200.00)",
        "$-1,200.00",
        "USD -1,200.00",
        "1,200 USD",
        "$1,2,3",
        "$ 123",
        "USD\t123",
        "USD123",
        "uſd 123",  # U+017F is not an ASCII s.
        "UſD 1,234.50",
        "-uſd 123",
        "uſd 1,234,567.89",
        "$$123",
        "USD $123",
        "+$123",
        "--$123",
        "$1e3",
        "$NaN",
        "NaN",
        "Infinity",
        "-Infinity",
        "$１２３",
        "1,234.",
    ],
)
def test_money_rejected(value: str) -> None:
    assert money(value) == (None, "invalid_value")


@pytest.mark.parametrize(
    "value",
    [
        "$9,223,372,036,854.775808",
        "-USD 9,223,372,036,854.775809",
        "$" + "1" * 1001,
        "$0." + "0" * 1001 + "1",
    ],
)
def test_formatted_money_fatal_precision_and_range_preserve_database(
    tmp_path: Path, value: str
) -> None:
    db = tmp_path / "existing.sqlite"
    ingest_workbook(write_workbook(tmp_path / "valid.xlsx"), db)
    original = db.read_bytes()
    source = write_workbook(tmp_path / "bad.xlsx", header=8, rows=[asset(amount=value)])
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, db, replace=True)
    assert exc.value.category == FailureCategory.DATA
    assert db.read_bytes() == original


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("$1,23", "invalid_value"),
        ("uſd 123", "invalid_value"),
        (None, "missing_value"),
        ("  ", "missing_value"),
    ],
)
def test_incomplete_money_retained_and_unverified(tmp_path: Path, value: Any, code: str) -> None:
    source = write_workbook(
        tmp_path / "incomplete.xlsx", header=8, rows=[asset("bad", value), asset("good", "$1.00")]
    )
    db = tmp_path / "incomplete.sqlite"
    report: dict = ingest_workbook(source, db)
    assert report["valuation_complete"] is False
    assert saved_manifest(db)["concepts"][TIV]["status"] == "mapped"
    assert report["field_completeness"][TIV] == dict(
        availability="mapped",
        physical_non_null=1,
        physical_total=2,
        logical_non_null=1,
        logical_total=2,
    )
    with sqlite3.connect(db) as c:
        assert (
            json.loads(
                c.execute(
                    "SELECT original_value_json FROM source_cells WHERE coordinate='E9'"
                ).fetchone()[0]
            )
            == value
        )
        assert c.execute(
            "SELECT total_insurable_value_micro_usd FROM assets WHERE asset_id='bad'"
        ).fetchone() == (None,)
        assert c.execute(
            "SELECT row_number,column_index FROM diagnostics WHERE code=? AND semantic_field=?",
            (code, TIV),
        ).fetchall() == [(9, 5)]
    source.unlink()
    answer = ask(
        db,
        "Total TIV?",
        FakeGenerator("SELECT SUM(total_insurable_value_micro_usd) FROM assets", "sum"),
    )
    assert isinstance(answer, Failure) and answer.category == "data_incomplete"


@pytest.mark.parametrize(
    ("cache", "expected", "code"),
    [
        ("$1,234.1234565", 1234123457, "precision_reduced"),
        ("-USD 1,234.50", -1234500000, None),
        (123, 123000000, None),
        (None, None, "formula_cache_unusable"),
        ("#VALUE!", None, "formula_cache_unusable"),
        ("$1,23", None, "invalid_value"),
    ],
)
def test_shifted_formula_cache_and_original_expression(
    tmp_path: Path, cache: Any, expected: int | None, code: str | None
) -> None:
    source = write_workbook(
        tmp_path / "cache.xlsx", header=8, rows=[asset(amount="=G9+H9")], caches={"E9": cache}
    )
    db = tmp_path / "cache.sqlite"
    report: dict = ingest_workbook(source, db)
    assert report["valuation_complete"] is (expected is not None)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT total_insurable_value_micro_usd FROM assets").fetchone() == (
            expected,
        )
        formula, kind, cached = c.execute(
            "SELECT formula,cached_type,cached_value_json FROM source_cells WHERE coordinate='E9'"
        ).fetchone()
        assert formula == "=G9+H9"
        assert kind == (
            "n"
            if cache is None or isinstance(cache, int)
            else "e"
            if str(cache).startswith("#")
            else "s"
        )
        assert json.loads(cached) == cache
        if code:
            assert c.execute(
                "SELECT row_number,column_index FROM diagnostics WHERE code=?", (code,)
            ).fetchall() == [(9, 5)]


@pytest.mark.parametrize("other", ["1234.50", "USD 1,234.50", 1234.5])
def test_equal_normalized_money_is_not_duplicate_identity(tmp_path: Path, other: Any) -> None:
    source = write_workbook(
        tmp_path / "conflict.xlsx", header=8, rows=[asset(amount="$1,234.50"), asset(amount=other)]
    )
    assert money(other) == money("$1,234.50")
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert exc.value.category == FailureCategory.DUPLICATE
    assert not (tmp_path / "no.sqlite").exists()


@pytest.mark.parametrize("renamed", [False, True])
def test_section_row_is_not_a_competing_header(tmp_path: Path, renamed: bool) -> None:
    source = write_workbook(
        tmp_path / "sections.xlsx",
        header=8,
        headers=RENAMED if renamed else HEADERS,
        preamble={"A7": "Pool", "B7": "Security Guards", "C7": "Earthquake  Exposed Locations"},
        summary=False,
    )
    report: dict = ingest_workbook(
        source, tmp_path / "sections.sqlite", infer_schema=True, inference_provider=FakeInference()
    )
    assert report["header_row"] == 8
    assert report["excluded_rows"] == dict(metadata=1, blank=8, summary=0)


def test_shifted_identical_formatted_formula_caches_consolidate_and_reconcile(
    tmp_path: Path,
) -> None:
    source = write_workbook(
        tmp_path / "formula-duplicates.xlsx",
        header=8,
        rows=[asset(amount="=G9+H9"), asset(amount="=G10+H10")],
        caches={"E9": "$1,234.123456", "E10": "$1,234.123456"},
    )
    db = tmp_path / "formula.sqlite"
    report = ingest_workbook(source, db)
    assert report["verified_duplicate_groups"] == 1
    source.unlink()
    answer = ask(
        db,
        "Total TIV?",
        FakeGenerator("SELECT SUM(total_insurable_value_micro_usd) FROM assets", "sum"),
    )
    assert isinstance(answer, Success)
    assert not isinstance(answer.result, ListValue)
    assert answer.result.value == 1234123456
    assert answer.verification.evidence_sum_micro_usd == 1234123456
    assert answer.verification.reconciled and answer.verification.complete
    assert answer.evidence.source_record_count == 2
    records = answer.evidence.contributors[0].source_records
    assert [r.row for r in records] == [9, 10]
    for record, formula, coordinate in zip(
        records, ["=G9+H9", "=G10+H10"], ["E9", "E10"], strict=True
    ):
        cell = next(c for c in record.cells if c.semantic_field == TIV)
        assert cell.formula == formula
        assert cell.cell_coordinate == coordinate
        assert cell.cached_value == "$1,234.123456"
        assert cell.normalized_value == 1234123456


@pytest.mark.parametrize("sign", ["", "-"])
def test_formatted_aggregate_overflow_stays_fatal(tmp_path: Path, sign: str) -> None:
    source = write_workbook(
        tmp_path / "sum-overflow.xlsx",
        header=8,
        rows=[asset("a", sign + "$5,000,000,000,000"), asset("b", sign + "$5,000,000,000,000")],
    )
    with pytest.raises(IngestionError, match="SUM may overflow"):
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert not (tmp_path / "no.sqlite").exists()


@pytest.mark.parametrize("renamed", [False, True])
@pytest.mark.parametrize(
    "kind",
    [
        "title",
        "unknown",
        "number",
        "date",
        "boolean",
        "error",
        "formula",
        "multirow",
        "cross_header",
        "header",
        "data",
        "summary",
        "empty",
        "other_title",
        "other_number",
    ],
)
def test_unsupported_merges_preserve_replacement_and_reject_before_inference(
    tmp_path: Path, renamed: bool, kind: str
) -> None:
    source = business_workbook(tmp_path / "bad-merge.xlsx", 5, renamed=renamed)
    wb = load_workbook(source)
    ws = wb["Statement of Values"]
    merge = {
        "title": "A1:B1",
        "multirow": "BG3:BY4",
        "cross_header": "BG4:BY5",
        "header": "E5:F5",
        "data": "G6:H6",
        "summary": "E11:F11",
    }.get(kind, "BG4:BY4")
    if kind not in {"title", "header", "data", "summary"}:
        ws[merge.split(":")[0]] = {
            "unknown": "Unrecognized section",
            "number": 123,
            "date": date(2026, 1, 1),
            "boolean": True,
            "error": "#VALUE!",
            "formula": '="Pool"',
            "empty": None,
        }.get(kind, "Pool")
    if kind == "other_title":
        ws["A4"] = "Arbitrary title beside recognized merge"
    elif kind == "other_number":
        ws["D4"] = 123
    ws.merge_cells(merge)
    wb.save(source)
    wb.close()
    db = tmp_path / "existing.sqlite"
    ingest_workbook(write_workbook(tmp_path / "valid.xlsx"), db)
    original = db.read_bytes()
    fake = FakeInference(proposal(POSITIONS))
    for output, replace in [(db, True), (tmp_path / "no.sqlite", False)]:
        with pytest.raises(IngestionError) as exc:
            ingest_workbook(
                source, output, replace=replace, infer_schema=True, inference_provider=fake
            )
        assert exc.value.category == FailureCategory.STRUCTURE
        assert "Statement of Values" in str(exc.value) and merge in str(exc.value)
        assert not fake.requests
        assert db.read_bytes() == original
        assert not (tmp_path / "no.sqlite").exists()
        assert not list(tmp_path.glob(".sov-import-*"))


@pytest.mark.parametrize("kind", ["title", "unknown", "empty", "data", "recognized"])
def test_invalid_merge_cannot_hide_second_renamed_candidate(tmp_path: Path, kind: str) -> None:
    source = business_workbook(tmp_path / "two-merge.xlsx", 5)
    wb = load_workbook(source)
    ws = wb["Statement of Values"]
    other = wb.copy_worksheet(ws)
    other.title = "Other"
    for column, label in enumerate(RENAMED, 1):
        other.cell(5, column, label)
    if kind == "title":
        merge = "A1:B1"
    elif kind == "data":
        merge = "G6:H6"
    else:
        merge = "BG4:BY4"
        other["BG4"] = (
            "Unknown section" if kind == "unknown" else "Pool" if kind == "recognized" else None
        )
    other.merge_cells(merge)
    wb.save(source)
    wb.close()
    fake = FakeInference()
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, tmp_path / "no.sqlite", infer_schema=True, inference_provider=fake)
    assert exc.value.category == FailureCategory.STRUCTURE
    if kind == "recognized":
        assert "found 2 candidate headers" in str(exc.value)
    else:
        assert f"Other!{merge}" in str(exc.value)
    assert not fake.requests
    assert not (tmp_path / "no.sqlite").exists()


def test_merged_section_does_not_relax_mapping_validation(tmp_path: Path) -> None:
    source = business_workbook(
        tmp_path / "mapping-merge.xlsx", 5, renamed=True, merged_section=True
    )
    fake = FakeInference(proposal({**POSITIONS, "state": None}))
    with pytest.raises(IngestionError, match="mapping_missing_required") as exc:
        ingest_workbook(source, tmp_path / "no.sqlite", infer_schema=True, inference_provider=fake)
    assert exc.value.category == FailureCategory.MAPPING
    assert len(fake.requests) == 1
    assert not (tmp_path / "no.sqlite").exists()
