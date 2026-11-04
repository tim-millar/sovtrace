"""Offline regression coverage of the ingestion contracts using synthetic workbooks only."""

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook
from openpyxl.utils.datetime import to_excel
from pydantic import ValidationError
from workbook_factory import HEADERS, asset, write_workbook

from sovtrace.ingestion import IngestionError, ingest_workbook, persistence
from sovtrace.ingestion.errors import FailureCategory
from sovtrace.ingestion.manifest import ConceptMapping, SchemaManifest, SemanticField
from sovtrace.ingestion.normalize import (
    INT64_MAX,
    INT64_MIN,
    equivalent,
    money,
    read_cells,
    source_json,
)
from sovtrace.ingestion.numeric_source import numeric_values


def connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db)


def saved_manifest(db: Path) -> dict:
    with connect(db) as connection:
        return json.loads(connection.execute("SELECT manifest_json FROM dataset").fetchone()[0])


@pytest.mark.parametrize("header", [1, 5])
def test_discovery_projection_and_provenance(tmp_path: Path, header: int) -> None:
    source = write_workbook(tmp_path / "synthetic.xlsx", header=header, rows=[asset(), asset()])
    db = tmp_path / "result.sqlite"
    report = ingest_workbook(source, db)
    assert report["physical_records"] == 2
    assert report["logical_assets"] == 1
    assert report["verified_duplicate_groups"] == 1
    assert report["header_row"] == header
    assert report["excluded_rows"] == {
        "metadata": 0 if header == 1 else 4,
        "summary": 1,
        "blank": 2,
    }
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = SchemaManifest.model_validate(saved_manifest(db))
    assert manifest.data_start_row == header + 1
    assert manifest.data_end_row == header + 2
    assert manifest.concepts[SemanticField.TIV].column == 5
    with connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM source_rows").fetchone() == (2,)
        assert c.execute("SELECT COUNT(*) FROM source_cells").fetchone() == (28,)
        assert c.execute(
            "SELECT asset_id, state, total_insurable_value_micro_usd, "
            "typeof(total_insurable_value_micro_usd) FROM assets"
        ).fetchone() == ("TEST-1", "AZ", 10000000000001, "integer")
        assert c.execute(
            "SELECT workbook_sha256, worksheet, row_number FROM asset_sources ORDER BY row_number"
        ).fetchall() == [
            (digest, "Statement of Values", header + 1),
            (digest, "Statement of Values", header + 2),
        ]
        assert c.execute(
            "SELECT column_index, excel_letter, original_label FROM source_columns "
            "WHERE original_label = 'Construction Quality' ORDER BY column_index"
        ).fetchall() == [(11, "K", "Construction Quality"), (14, "N", "Construction Quality")]
        assert c.execute(
            "SELECT column_index, original_value_json FROM source_cells "
            "WHERE row_number=? AND column_index IN (11,14) ORDER BY column_index",
            (header + 1,),
        ).fetchall() == [(11, '"First"'), (14, '"Second"')]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT monetary_scale,currency FROM dataset").fetchone() == (
            1000000,
            "USD",
        )
        assumptions = json.loads(c.execute("SELECT assumptions_json FROM dataset").fetchone()[0])
        assert "freshness" in assumptions["formula_cache"]
        assert c.execute(
            "SELECT COUNT(*) FROM diagnostics WHERE code='duplicate_consolidated'"
        ).fetchone() == (1,)
    # Database remains useful with no workbook at the original path.
    source.unlink()
    with connect(db) as c:
        assert c.execute("SELECT SUM(total_insurable_value_micro_usd) FROM assets").fetchone() == (
            10000000000001,
        )


@pytest.mark.parametrize("missing", ["Property Name", "City", "Construction Type"])
def test_absent_optional_concept(tmp_path: Path, missing: str) -> None:
    indices = [i for i, label in enumerate(HEADERS) if label != missing]
    source = write_workbook(
        tmp_path / "optional.xlsx",
        headers=[HEADERS[i] for i in indices],
        rows=[[asset()[i] for i in indices]],
    )
    db = tmp_path / "result.sqlite"
    ingest_workbook(source, db)
    manifest = saved_manifest(db)
    field = {
        "Property Name": "property_name",
        "City": "city",
        "Construction Type": "construction_type",
    }[missing]
    assert manifest["concepts"][field]["status"] == "absent"
    with connect(db) as c:
        assert c.execute(f"SELECT {field} FROM assets").fetchone() == (None,)
        assert c.execute(
            "SELECT availability, logical_non_null FROM field_completeness WHERE semantic_field=?",
            (field,),
        ).fetchone() == ("absent", 0)


@pytest.mark.parametrize("label", ["EPIC Code", "State", "Total Insurable Values"])
def test_missing_required_mapping(tmp_path: Path, label: str) -> None:
    headers = ["Unknown field" if x == label else x for x in HEADERS]
    source = write_workbook(tmp_path / "missing.xlsx", headers=headers)
    with pytest.raises(IngestionError, match="required concept is absent") as exc:
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert exc.value.category == FailureCategory.MAPPING
    assert not (tmp_path / "no.sqlite").exists()


def test_ambiguous_mapping(tmp_path: Path) -> None:
    source = write_workbook(
        tmp_path / "ambiguous.xlsx", headers=[*HEADERS, "State"], rows=[[*asset(), "AZ"]]
    )
    with pytest.raises(IngestionError, match="ambiguous mapping") as exc:
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert exc.value.category == FailureCategory.MAPPING


def test_manifest_validates_proposals_and_explicit_resolution(tmp_path: Path) -> None:
    source = write_workbook(
        tmp_path / "proposal.xlsx", headers=[*HEADERS, "Other state"], rows=[[*asset(), "AZ"]]
    )
    db = tmp_path / "result.sqlite"
    ingest_workbook(source, db)
    proposal = saved_manifest(db)
    proposal["concepts"]["state"]["column"] = 999
    proposal["concepts"]["state"]["candidates"] = [999]
    with pytest.raises(ValidationError, match="nonexistent source column"):
        SchemaManifest.model_validate(proposal)
    proposal["concepts"]["state"].update(column=4, candidates=[4, 15])
    with pytest.raises(ValidationError, match="explicit resolution"):
        SchemaManifest.model_validate(proposal)
    proposal["concepts"]["state"]["resolution"] = "Synthetic documented selection"
    assert SchemaManifest.model_validate(proposal).concepts[SemanticField.STATE].column == 4
    proposal["concepts"]["state"].update(column=None, status="ambiguous", resolution=None)
    with pytest.raises(ValidationError, match="ambiguous mapping"):
        SchemaManifest.model_validate(proposal)
    proposal = saved_manifest(db)
    proposal["columns"][0]["letter"] = "Z"
    with pytest.raises(ValidationError, match="letter must match"):
        SchemaManifest.model_validate(proposal)
    proposal = saved_manifest(db)
    proposal["concepts"]["state"]["normalized_type"] = "float"
    with pytest.raises(ValidationError, match="expected normalized type"):
        SchemaManifest.model_validate(proposal)


def test_mapping_contract_rejects_silent_multiple_mapping() -> None:
    with pytest.raises(ValidationError, match="Absent concepts"):
        ConceptMapping(status="absent", column=1, normalized_type="string")
    with pytest.raises(ValidationError, match="one candidate"):
        ConceptMapping(status="mapped", column=1, candidates=(2,), normalized_type="string")


@pytest.mark.parametrize(
    "kind",
    [
        "none",
        "two",
        "unexpected_header",
        "merged",
        "interior_blank",
        "after_summary",
        "outside_columns",
        "unknown_preamble",
        "unknown_row",
    ],
)
def test_structural_failures(tmp_path: Path, kind: str) -> None:
    source = write_workbook(
        tmp_path / "structure.xlsx",
        extra_table=kind == "two",
        header=11 if kind == "unexpected_header" else 5 if kind == "unknown_preamble" else 1,
    )
    wb = load_workbook(source)
    ws = wb["Statement of Values"]
    if kind == "none":
        ws.delete_rows(1, ws.max_row)
        ws["A1"] = "Not an SOV"
    elif kind == "merged":
        ws.merge_cells("B2:C2")
    elif kind == "interior_blank":
        ws.insert_rows(2)
    elif kind == "after_summary":
        ws["A5"] = "Unexpected record"
    elif kind == "outside_columns":
        ws["Z2"] = "Unlabelled business value"
    elif kind == "unknown_preamble":
        ws["B2"] = "Unsupported metadata"
    elif kind == "unknown_row":
        ws["G3"] = "Unclassified total"
    wb.save(source)
    wb.close()
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert exc.value.category in {FailureCategory.STRUCTURE, FailureCategory.DATA}
    assert not (tmp_path / "no.sqlite").exists()


@pytest.mark.parametrize("identifier", [None, "", "   ", 123, True, "=A9"])
def test_invalid_identifiers_never_disappear(tmp_path: Path, identifier: object) -> None:
    source = write_workbook(tmp_path / "id.xlsx", rows=[asset(identifier)])
    with pytest.raises(IngestionError, match="identifier") as exc:
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert exc.value.category == FailureCategory.DATA


@pytest.mark.parametrize(
    ("state", "normalized"),
    [
        (" az ", "AZ"),
        ("Arizona", "AZ"),
        (" new york ", "NY"),
        ("District of Columbia", "DC"),
        (None, None),
        ("ZZ", None),
        ("Georgia / Alabama", None),
        (1, None),
        ("#VALUE!", None),
    ],
)
def test_state_values(tmp_path: Path, state: object, normalized: str | None) -> None:
    source = write_workbook(tmp_path / "state.xlsx", rows=[asset(state=state)])
    db = tmp_path / "result.sqlite"
    ingest_workbook(source, db)
    with connect(db) as c:
        assert c.execute("SELECT state FROM assets").fetchone() == (normalized,)
        assert c.execute(
            "SELECT availability,logical_non_null FROM field_completeness "
            "WHERE semantic_field='state'"
        ).fetchone() == ("mapped", int(normalized is not None))
        if normalized is None:
            assert c.execute(
                "SELECT column_index FROM diagnostics WHERE semantic_field='state' "
                "AND severity='warning'"
            ).fetchone() == (4,)


@pytest.mark.parametrize(
    ("value", "expected", "code"),
    [
        (0, 0, None),
        ("0.000001", 1, None),
        ("1.234567", 1234567, None),
        ("1.2345675", 1234568, "precision_reduced"),
        ("-1.2345675", -1234568, "precision_reduced"),
        ("0.00000049", 0, "precision_reduced"),
        ("0.0000005", 1, "precision_reduced"),
        (None, None, "missing_value"),
        ("", None, "missing_value"),
        ("broken", None, "invalid_value"),
        (True, None, "invalid_value"),
        ("NaN", None, "invalid_value"),
        ("Infinity", None, "invalid_value"),
        ("9223372036854.775807", INT64_MAX, None),
        ("-9223372036854.775808", INT64_MIN, None),
    ],
)
def test_decimal_contract(value: object, expected: int | None, code: str | None) -> None:
    assert money(value) == (expected, code)


@pytest.mark.parametrize("amount", [None, "malformed", "=G2+H2+I2", "#VALUE!"])
def test_missing_or_invalid_tiv_is_null_without_component_fallback(
    tmp_path: Path, amount: object
) -> None:
    source = write_workbook(tmp_path / "missing_tiv.xlsx", rows=[asset(amount=amount)])
    db = tmp_path / "result.sqlite"
    report = ingest_workbook(source, db)
    assert report["valuation_complete"] is False
    assert saved_manifest(db)["concepts"]["total_insurable_value_micro_usd"]["status"] == "mapped"
    with connect(db) as c:
        assert c.execute("SELECT total_insurable_value_micro_usd FROM assets").fetchone() == (None,)
        assert c.execute(
            "SELECT physical_non_null,logical_non_null FROM field_completeness "
            "WHERE semantic_field='total_insurable_value_micro_usd'"
        ).fetchone() == (0, 0)
        assert c.execute(
            "SELECT COUNT(*) FROM diagnostics WHERE semantic_field="
            "'total_insurable_value_micro_usd' AND severity='warning'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "amount", ["9223372036854.775808", "-9223372036854.775809", "1e1001", "1e-1001"]
)
def test_unrepresentable_amount_fails(tmp_path: Path, amount: str) -> None:
    source = write_workbook(tmp_path / "overflow.xlsx", rows=[asset(amount=amount)])
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert exc.value.category == FailureCategory.DATA
    assert not (tmp_path / "no.sqlite").exists()


@pytest.mark.parametrize("sign", [1, -1])
def test_filtered_sum_overflow_cannot_hide_behind_cancellation(tmp_path: Path, sign: int) -> None:
    amounts = [sign * 5_000_000_000_000, sign * 5_000_000_000_000, -sign * 5_000_000_000_000]
    source = write_workbook(
        tmp_path / "sum.xlsx", rows=[asset(f"TEST-{i}", n) for i, n in enumerate(amounts)]
    )
    with pytest.raises(IngestionError, match="SUM may overflow"):
        ingest_workbook(source, tmp_path / "no.sqlite")


def test_formula_cache_duplicates_and_source_preservation(tmp_path: Path) -> None:
    first, second = asset(amount="=G2+H2+I2"), asset(amount="=G3+H3+I3")
    first[6], second[6] = "=1+2", "=2+1"
    source = write_workbook(
        tmp_path / "cached.xlsx",
        rows=[first, second],
        caches={"E2": 123.1234565, "E3": 123.1234565, "G2": 3, "G3": 3},
    )
    db = tmp_path / "result.sqlite"
    report = ingest_workbook(source, db)
    assert report["logical_assets"] == 1
    with connect(db) as c:
        assert c.execute("SELECT total_insurable_value_micro_usd FROM assets").fetchone() == (
            123123457,
        )
        assert c.execute(
            "SELECT row_number, coordinate, formula, original_type, cached_type, "
            "cached_value_json FROM source_cells WHERE column_index=5 ORDER BY row_number"
        ).fetchall() == [
            (2, "E2", "=G2+H2+I2", "f", "n", "123.1234565"),
            (3, "E3", "=G3+H3+I3", "f", "n", "123.1234565"),
        ]
        assert c.execute(
            "SELECT row_number,column_index FROM diagnostics WHERE code='precision_reduced' "
            "ORDER BY row_number"
        ).fetchall() == [(2, 5), (3, 5)]


@pytest.mark.parametrize("cache", [None, "#DIV/0!"])
def test_formula_without_usable_cache_is_preserved(tmp_path: Path, cache: object) -> None:
    source = write_workbook(
        tmp_path / "no-cache.xlsx", rows=[asset(amount="=G2+H2+I2")], caches={"E2": cache}
    )
    db = tmp_path / "result.sqlite"
    report = ingest_workbook(source, db)
    assert report["valuation_complete"] is False
    with connect(db) as c:
        assert c.execute("SELECT total_insurable_value_micro_usd FROM assets").fetchone() == (None,)
        assert c.execute(
            "SELECT row_number,column_index FROM diagnostics WHERE code='formula_cache_unusable'"
        ).fetchone() == (2, 5)


@pytest.mark.parametrize("column", [4, 9, 10, 13])
def test_duplicate_conflicts_include_unmapped_and_repeated_columns(
    tmp_path: Path, column: int
) -> None:
    first, second = asset(), asset()
    second[column] = "Conflicting synthetic value"
    source = write_workbook(tmp_path / "conflict.xlsx", rows=[first, second])
    with pytest.raises(IngestionError, match="rows 2 and 3; columns") as exc:
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert exc.value.category == FailureCategory.DUPLICATE
    assert not (tmp_path / "no.sqlite").exists()


def test_duplicate_missing_cache_cannot_establish_equality(tmp_path: Path) -> None:
    source = write_workbook(
        tmp_path / "unknown.xlsx", rows=[asset(amount="=G2"), asset(amount="=G3")]
    )
    with pytest.raises(IngestionError, match="unverifiable formula caches"):
        ingest_workbook(source, tmp_path / "no.sqlite")


def test_same_address_distinct_ids_and_thresholds(tmp_path: Path) -> None:
    source = write_workbook(
        tmp_path / "thresholds.xlsx",
        rows=[
            asset("  BELOW  ", "9999999.999999"),
            asset("EXACT", "10000000"),
            asset("ABOVE", "10000000.000001"),
            asset("ZERO", 0),
            asset("NEGATIVE", "-0.000001"),
        ],
    )
    db = tmp_path / "result.sqlite"
    ingest_workbook(source, db)
    with connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM assets").fetchone() == (5,)
        assert c.execute(
            "SELECT asset_id FROM assets WHERE total_insurable_value_micro_usd > 10000000000000"
        ).fetchall() == [("ABOVE",)]
        assert c.execute(
            "SELECT asset_id FROM assets WHERE total_insurable_value_micro_usd = 10000000000000"
        ).fetchall() == [("EXACT",)]
        assert c.execute("SELECT SUM(total_insurable_value_micro_usd) FROM assets").fetchone() == (
            29999999999999,
        )
        assert c.execute(
            "SELECT original_value_json FROM source_cells WHERE row_number=2 AND column_index=1"
        ).fetchone() == ('"  BELOW  "',)


def test_aggregation_rounds_after_sum_not_to_cents_per_record(tmp_path: Path) -> None:
    source = write_workbook(
        tmp_path / "fractions.xlsx", rows=[asset("A", "0.005"), asset("B", "0.005")]
    )
    db = tmp_path / "result.sqlite"
    ingest_workbook(source, db)
    with connect(db) as c:
        assert c.execute("SELECT SUM(total_insurable_value_micro_usd) FROM assets").fetchone() == (
            10000,
        )


def test_repeated_imports_are_semantically_equivalent(tmp_path: Path) -> None:
    source = write_workbook(tmp_path / "repeat.xlsx", rows=[asset(), asset()])
    databases = [tmp_path / "a.sqlite", tmp_path / "b.sqlite"]
    for db in databases:
        ingest_workbook(source, db)
    with connect(databases[0]) as a, connect(databases[1]) as b:
        for relation in [
            "assets",
            "source_rows",
            "source_columns",
            "source_cells",
            "asset_sources",
            "field_completeness",
            "dataset",
            "diagnostics",
        ]:
            assert (
                a.execute(f"SELECT * FROM {relation}").fetchall()
                == b.execute(f"SELECT * FROM {relation}").fetchall()
            )


@pytest.mark.parametrize("extension", [".xls", ".csv", ".xlsx"])
def test_bad_workbook_error_category(tmp_path: Path, extension: str) -> None:
    source = tmp_path / ("broken" + extension)
    source.write_text("Not a workbook")
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, tmp_path / "no.sqlite")
    assert exc.value.category == FailureCategory.WORKBOOK


def test_nonexistent_source(tmp_path: Path) -> None:
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(tmp_path / "absent.xlsx", tmp_path / "no.sqlite")
    assert exc.value.category == FailureCategory.WORKBOOK


def test_existing_output_and_failed_replacement(tmp_path: Path) -> None:
    source = write_workbook(tmp_path / "valid.xlsx")
    db = tmp_path / "existing.sqlite"
    ingest_workbook(source, db)
    original = db.read_bytes()
    with pytest.raises(IngestionError, match="use --replace"):
        ingest_workbook(source, db)
    invalid = write_workbook(tmp_path / "invalid.xlsx", rows=[asset(), asset(amount=2)])
    with pytest.raises(IngestionError):
        ingest_workbook(invalid, db, replace=True)
    assert db.read_bytes() == original
    replacement = write_workbook(tmp_path / "replacement.xlsx", rows=[asset("NEW")])
    ingest_workbook(replacement, db, replace=True)
    with connect(db) as c:
        assert c.execute("SELECT asset_id FROM assets").fetchone() == ("NEW",)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["write", "publish"])
def test_persistence_failure_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, failure: str
) -> None:
    source = write_workbook(tmp_path / "valid.xlsx")
    db = tmp_path / "result.sqlite"
    if existing:
        ingest_workbook(source, db)
    original = db.read_bytes() if existing else None
    if failure == "write":
        monkeypatch.setattr(
            persistence,
            "_write_database",
            Mock(side_effect=sqlite3.OperationalError("simulated disk failure")),
        )
    else:
        monkeypatch.setattr(
            persistence.os,
            "replace" if existing else "link",
            Mock(side_effect=OSError("simulated publication failure")),
        )
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, db, replace=existing)
    assert exc.value.category == FailureCategory.PERSISTENCE
    assert (db.read_bytes() if db.exists() else None) == original
    assert list(tmp_path.glob(".sov-import-*")) == []


def test_source_cannot_be_replaced_by_database(tmp_path: Path) -> None:
    source = write_workbook(tmp_path / "keep.xlsx")
    original = source.read_bytes()
    with pytest.raises(IngestionError, match="cannot be the output"):
        ingest_workbook(source, source, replace=True)
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "value",
    [
        date(2024, 1, 2),
        datetime(2024, 1, 2, 3, 4, 5),
        time(3, 4, 5),
        timedelta(days=2, microseconds=3),
    ],
)
def test_source_temporal_values_keep_types(value: object) -> None:
    data = json.loads(source_json(value))
    assert data["python_type"] == type(value).__name__


def test_ingest_cli_success_and_failure(tmp_path: Path) -> None:
    source = write_workbook(tmp_path / "synthetic.xlsx")
    db = tmp_path / "result.sqlite"
    command = ["sovtrace", "ingest", "--format", "json", str(source), "--db", str(db)]
    success = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
    assert success.returncode == 0
    assert json.loads(success.stdout)["logical_assets"] == 1
    failure = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
    assert failure.returncode == 1
    assert failure.stdout == ""
    assert "use --replace" in failure.stderr
    assert "Traceback" not in failure.stderr
    invalid = subprocess.run(
        [
            "sovtrace",
            "ingest",
            str(tmp_path / "missing.xlsx"),
            "--db",
            str(tmp_path / "new.sqlite"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert invalid.returncode == 1
    assert "Reason: workbook" in invalid.stderr
    assert "Traceback" not in invalid.stderr


def test_numeric_xml_precision_and_rounding_boundary(tmp_path: Path) -> None:
    from workbook_factory import patch_numbers

    source = write_workbook(
        tmp_path / "precise.xlsx", rows=[asset("BELOW_HALF", "=G2"), asset("ABOVE_HALF", "=G3")]
    )
    lexemes = {"E2": "10000000.0000004999", "E3": "10000000.0000005001"}
    patch_numbers(source, lexemes)
    db = tmp_path / "result.sqlite"
    ingest_workbook(source, db)
    with connect(db) as c:
        assert c.execute(
            "SELECT asset_id,total_insurable_value_micro_usd FROM assets ORDER BY asset_id"
        ).fetchall() == [("ABOVE_HALF", 10000000000001), ("BELOW_HALF", 10000000000000)]
        assert c.execute(
            "SELECT cached_value_json FROM source_cells WHERE column_index=5 ORDER BY row_number"
        ).fetchall() == [(lexemes["E2"],), (lexemes["E3"],)]


def test_duplicate_comparison_preserves_sub_float_numeric_differences(tmp_path: Path) -> None:
    from workbook_factory import patch_numbers

    source = write_workbook(tmp_path / "precise-conflict.xlsx", rows=[asset(), asset()])
    values = ["0.123456789012345671", "0.123456789012345672"]
    assert float(values[0]) == float(
        values[1]
    )  # A float-only comparison would silently merge these.
    patch_numbers(source, {"G2": values[0], "G3": values[1]})
    with pytest.raises(IngestionError, match="columns G"):
        ingest_workbook(source, tmp_path / "no.sqlite")


def test_non_formula_numeric_source_is_exact(tmp_path: Path) -> None:
    from workbook_factory import patch_numbers

    source = write_workbook(tmp_path / "precise-value.xlsx")
    number = "0.123456789012345671"
    patch_numbers(source, {"G2": number})
    db = tmp_path / "result.sqlite"
    ingest_workbook(source, db)
    with connect(db) as c:
        assert c.execute(
            "SELECT original_value_json FROM source_cells WHERE column_index=7"
        ).fetchone() == (number,)


def test_atomic_no_clobber_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = write_workbook(tmp_path / "race.xlsx")
    db = tmp_path / "race.sqlite"
    link = persistence.os.link

    def competing_publisher(temporary: Path, output: Path) -> None:
        output.write_bytes(b"another completed output")
        link(temporary, output)

    monkeypatch.setattr(persistence.os, "link", competing_publisher)
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, db)
    assert exc.value.category == FailureCategory.PERSISTENCE
    assert db.read_bytes() == b"another completed output"
    assert list(tmp_path.glob(".sov-import-*")) == []


def test_malformed_xml_has_clean_cli_error(tmp_path: Path) -> None:
    from io import BytesIO
    from zipfile import ZipFile

    source = write_workbook(tmp_path / "malformed.xlsx")
    with ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["xl/worksheets/sheet1.xml"] = b"<worksheet><bad>"
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    source.write_bytes(stream.getvalue())
    result = subprocess.run(
        ["sovtrace", "ingest", str(source), "--db", str(tmp_path / "no.sqlite")],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Reason: workbook" in result.stderr
    assert "Traceback" not in result.stderr


def test_formula_in_non_numeric_column_is_not_assumed_summary(tmp_path: Path) -> None:
    source = write_workbook(tmp_path / "not-summary.xlsx", summary=False, trailing=0)
    wb = load_workbook(source)
    wb["Statement of Values"]["B3"] = "=SUM(B2:B2)"
    wb.save(source)
    wb.close()
    with pytest.raises(IngestionError, match="missing asset identifier"):
        ingest_workbook(source, tmp_path / "no.sqlite")


def test_reordered_known_mappings_are_consumed_from_manifest(tmp_path: Path) -> None:
    order = list(reversed(range(len(HEADERS))))
    original = asset("SYNTHETIC", "1.234567", " colorado ")
    source = write_workbook(
        tmp_path / "reordered.xlsx",
        headers=[HEADERS[i] for i in order],
        rows=[[original[i] for i in order]],
        summary=False,
    )
    db = tmp_path / "result.sqlite"
    ingest_workbook(source, db)
    with connect(db) as c:
        assert c.execute("SELECT * FROM assets").fetchone() == (
            "SYNTHETIC",
            "Synthetic property",
            "Test City",
            "CO",
            1234567,
            "Wood Frame - Low Rise",
        )


def test_formatting_does_not_change_duplicate_identity(tmp_path: Path) -> None:
    from openpyxl.styles import Font

    source = write_workbook(tmp_path / "formatted.xlsx", rows=[asset(), asset()])
    wb = load_workbook(source)
    ws = wb["Statement of Values"]
    ws["B3"].font = Font(bold=True)
    ws["G3"].number_format = '"$"#,##0.00'
    wb.save(source)
    wb.close()
    report = ingest_workbook(source, tmp_path / "result.sqlite")
    assert report["logical_assets"] == 1
    assert report["physical_records"] == 2


def test_present_optional_column_with_missing_value_is_not_absent(tmp_path: Path) -> None:
    row = asset()
    row[1] = None
    row[5] = 123  # Invalid individual categorical cell.
    source = write_workbook(tmp_path / "optional-quality.xlsx", rows=[row])
    db = tmp_path / "result.sqlite"
    ingest_workbook(source, db)
    with connect(db) as c:
        assert c.execute("SELECT property_name,construction_type FROM assets").fetchone() == (
            None,
            None,
        )
        assert c.execute(
            "SELECT availability,logical_non_null FROM field_completeness "
            "WHERE semantic_field IN ('property_name','construction_type')"
        ).fetchall() == [("mapped", 0), ("mapped", 0)]
        assert c.execute(
            "SELECT code,column_index FROM diagnostics WHERE severity='warning' "
            "ORDER BY column_index"
        ).fetchall() == [("missing_value", 2), ("invalid_value", 6)]


@pytest.mark.parametrize(
    "value",
    [
        date(2024, 1, 2),
        datetime(2024, 1, 2, 3, 4, 5),
        time(3, 4, 5),
        timedelta(days=2, seconds=3),
        True,
        False,
        "not numeric",
        None,
    ],
)
def test_numeric_typed_decoded_values_are_not_blindly_overlaid(
    tmp_path: Path, value: object
) -> None:
    source = write_workbook(tmp_path / "decoded-types.xlsx")
    db = tmp_path / "baseline.sqlite"
    ingest_workbook(source, db)
    manifest = SchemaManifest.model_validate(saved_manifest(db))
    formulas = load_workbook(source)
    cached = load_workbook(source, data_only=True)
    try:
        original = formulas[manifest.worksheet]["G2"]
        original.value = value
        original.data_type = "n"  # Deliberately bypass openpyxl's usual temporal classification.
        formulas[manifest.worksheet]["H2"] = "=G2"
        calculated = cached[manifest.worksheet]["H2"]
        calculated.value = value
        calculated.data_type = "n"
        overlay = {"G2": Decimal("123.456789012345671"), "H2": Decimal("123.456789012345671")}
        cells = read_cells(
            formulas[manifest.worksheet], cached[manifest.worksheet], 2, manifest, overlay
        )
        assert cells[6].data_type == cells[7].cached_type == "n"
        assert cells[6].value is value
        assert cells[7].cached_value is value
        assert cells[7].effective_value is value
        if isinstance(value, (date, datetime, time, timedelta)):
            assert json.loads(source_json(cells[6].value))["python_type"] == type(value).__name__
            assert (
                json.loads(source_json(cells[7].cached_value))["python_type"]
                == type(value).__name__
            )
            assert equivalent(cells[6], cells[7])
            assert not equivalent(
                cells[6],
                replace(cells[7], cached_value=overlay["H2"]),
            )
    finally:
        formulas.close()
        cached.close()


@pytest.mark.parametrize(
    "value, number_format, iso_dates, expected",
    [
        (date(2024, 1, 2), "yyyy-mm-dd", True, {"python_type": "date", "iso": "2024-01-02"}),
        (
            date(2024, 1, 2),
            "yyyy-mm-dd",
            False,
            {"python_type": "datetime", "iso": "2024-01-02T00:00:00"},
        ),
        (
            datetime(2024, 1, 2, 3, 4, 5),
            "yyyy-mm-dd hh:mm:ss",
            False,
            {"python_type": "datetime", "iso": "2024-01-02T03:04:05"},
        ),
        (time(3, 4, 5), "hh:mm:ss", False, {"python_type": "time", "iso": "03:04:05"}),
        (
            timedelta(days=2, seconds=3),
            "[hh]:mm:ss",
            False,
            {"python_type": "timedelta", "days": 2, "seconds": 3, "microseconds": 0},
        ),
    ],
)
def test_temporal_source_cells_preserved_through_ingestion(
    tmp_path: Path,
    value: object,
    number_format: str,
    iso_dates: bool,
    expected: dict,
) -> None:
    row = asset()
    row[6] = value
    source = write_workbook(
        tmp_path / "temporal.xlsx",
        rows=[row, row],
        iso_dates=iso_dates,
        formats={"G2": number_format, "G3": number_format},
    )
    numeric = numeric_values(source.read_bytes(), "Statement of Values")
    assert ("G2" in numeric) is (not iso_dates)
    db = tmp_path / "result.sqlite"
    report = ingest_workbook(source, db)
    assert (report["physical_records"], report["logical_assets"]) == (2, 1)
    with connect(db) as c:
        stored = c.execute(
            "SELECT row_number, coordinate, original_type, original_value_json "
            "FROM source_cells WHERE column_index=7 ORDER BY row_number"
        ).fetchall()
        assert [(r, coord, kind, json.loads(v)) for r, coord, kind, v in stored] == [
            (2, "G2", "d", expected),
            (3, "G3", "d", expected),
        ]
        assert c.execute("SELECT row_number FROM asset_sources ORDER BY row_number").fetchall() == [
            (2,),
            (3,),
        ]


@pytest.mark.parametrize(
    "value, number_format, expected",
    [
        (date(2024, 1, 2), "yyyy-mm-dd", {"python_type": "datetime", "iso": "2024-01-02T00:00:00"}),
        (
            datetime(2024, 1, 2, 3, 4, 5),
            "yyyy-mm-dd hh:mm:ss",
            {"python_type": "datetime", "iso": "2024-01-02T03:04:05"},
        ),
        (time(3, 4, 5), "hh:mm:ss", {"python_type": "time", "iso": "03:04:05"}),
        (
            timedelta(days=2, seconds=3),
            "[hh]:mm:ss",
            {"python_type": "timedelta", "days": 2, "seconds": 3, "microseconds": 0},
        ),
    ],
)
def test_temporal_formula_caches_preserved_through_ingestion(
    tmp_path: Path,
    value: date | datetime | time | timedelta,
    number_format: str,
    expected: dict,
) -> None:
    first, second = asset(), asset()
    first[6], second[6] = "=H2", "=H3"
    serial = to_excel(value)
    source = write_workbook(
        tmp_path / "temporal-cache.xlsx",
        rows=[first, second],
        formats={"G2": number_format, "G3": number_format},
        caches={"G2": serial, "G3": serial},
    )
    assert "G2" in numeric_values(source.read_bytes(), "Statement of Values")
    db = tmp_path / "result.sqlite"
    report = ingest_workbook(source, db)
    assert (report["physical_records"], report["logical_assets"]) == (2, 1)
    with connect(db) as c:
        stored = c.execute(
            "SELECT coordinate, formula, cached_type, cached_value_json "
            "FROM source_cells WHERE column_index=7 ORDER BY row_number"
        ).fetchall()
        assert [(coord, formula, kind, json.loads(v)) for coord, formula, kind, v in stored] == [
            ("G2", "=H2", "d", expected),
            ("G3", "=H3", "d", expected),
        ]
    # A different decoded temporal cache must conflict despite the row-relative formulas.
    from workbook_factory import patch_caches

    patch_caches(source, {"G2": serial, "G3": serial + 1})
    with pytest.raises(IngestionError, match="columns G"):
        ingest_workbook(source, tmp_path / "conflict.sqlite")
