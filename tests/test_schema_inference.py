"""schema inference offline semantic inference, failure atomicity and real downstream evidence."""

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from runpy import run_path
from typing import Any
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from pydantic import ValidationError
from test_ingestion import saved_manifest
from workbook_factory import HEADERS, asset, patch_caches, patch_numbers, write_workbook

from sovtrace.ingestion import IngestionError, ingest_workbook
from sovtrace.ingestion.errors import FailureCategory
from sovtrace.ingestion.inference import (
    ColumnProposal,
    InferenceRequest,
    InferenceResult,
    SchemaProposal,
)
from sovtrace.ingestion.manifest import SemanticField
from sovtrace.qa import Success, ask
from sovtrace.qa.contracts import ListValue

RENAMED = [
    "Location Reference",
    "Premises",
    "Municipality",
    "US State",
    "Total Insured Value",
    "Building Construction",
    *[
        "Structure Component"
        if h == "Building"
        else "Movable Component"
        if h == "Contents"
        else "Lease Component"
        if h == "Rental Income"
        else h
        for h in HEADERS[6:]
    ],
]
# References are independent of provider responses.
EXPECTED = dict(zip([f.value for f in SemanticField], range(1, 7), strict=True))


def proposal(columns: Mapping[str, int | None] | None = None) -> SchemaProposal:
    return SchemaProposal.model_validate(
        {
            field: {
                "status": "absent" if index is None else "mapped",
                "column": index,
                "candidates": [] if index is None else [index],
            }
            for field, index in (EXPECTED if columns is None else columns).items()
        }
    )


class FakeInference:
    def __init__(self, result: SchemaProposal | None = None, *, fail: bool = False):
        self.proposal = result or proposal()
        self.fail = fail
        self.requests: list[InferenceRequest] = []

    def infer(self, request: InferenceRequest) -> InferenceResult:
        self.requests.append(request)
        if self.fail:
            raise IngestionError(
                FailureCategory.INFERENCE, "schema_inference_provider_error: timeout"
            )
        return InferenceResult(proposal=self.proposal)


def import_variant(
    tmp_path: Path,
    *,
    labels: list[str] | None = None,
    provider: FakeInference | None = None,
    **kwargs: Any,
) -> tuple[Path, dict]:
    source = write_workbook(
        tmp_path / "variant.xlsx", headers=labels or RENAMED, summary=False, **kwargs
    )
    db = tmp_path / "variant.sqlite"
    report = ingest_workbook(
        source, db, infer_schema=True, inference_provider=provider or FakeInference()
    )
    return db, report


@pytest.mark.parametrize("header", [1, 5])
@pytest.mark.parametrize("permuted", [False, True])
def test_fully_renamed_and_permuted(tmp_path: Path, header: int, permuted: bool) -> None:
    order = list(reversed(range(len(RENAMED)))) if permuted else list(range(len(RENAMED)))
    expected = {field: order.index(index - 1) + 1 for field, index in EXPECTED.items()}
    fake = FakeInference(proposal(expected))
    db, report = import_variant(
        tmp_path,
        labels=[RENAMED[i] for i in order],
        rows=[[asset()[i] for i in order]] * 2,
        header=header,
        provider=fake,
    )
    assert report["physical_records"] == 2 and report["logical_assets"] == 1
    assert report["verified_duplicate_groups"] == 1
    assert report["schema_inference"] == {
        "status": "applied",
        "model": None,
        "inferred_fields": list(EXPECTED),
    }
    manifest = saved_manifest(db)
    assert {f: m["column"] for f, m in manifest["concepts"].items()} == expected
    assert [c["label"] for c in manifest["columns"]] == [RENAMED[i] for i in order]
    assert manifest["header_row"] == header
    assert len(fake.requests) == 1
    assert len(fake.requests[0].columns) == len(RENAMED)
    assert report["diagnostic_summary"]["concept_inferred"] == 6
    with sqlite3.connect(db) as c:
        persisted = json.loads(c.execute("SELECT report_json FROM dataset").fetchone()[0])
        assert persisted["schema_inference"] == report["schema_inference"]
        assert (
            c.execute("SELECT SUM(total_insurable_value_micro_usd) FROM assets").fetchone()[0]
            == 10000000000001
        )


def test_optional_renamed_even_when_required_are_exact(tmp_path: Path) -> None:
    labels = HEADERS.copy()
    labels[5] = RENAMED[5]
    fake = FakeInference()
    db, report = import_variant(tmp_path, labels=labels, provider=fake)
    assert len(fake.requests) == 1
    assert report["schema_inference"]["inferred_fields"] == ["construction_type"]
    assert saved_manifest(db)["concepts"]["construction_type"]["column"] == 6


def test_optional_absent(tmp_path: Path) -> None:
    expected = {**EXPECTED, "city": None}
    labels = RENAMED.copy()
    labels[2] = "Unrelated field"
    db, report = import_variant(tmp_path, labels=labels, provider=FakeInference(proposal(expected)))
    assert saved_manifest(db)["concepts"]["city"]["status"] == "absent"
    assert report["field_completeness"]["city"]["availability"] == "absent"
    assert report["diagnostic_summary"]["concept_absent"] == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_exact_path_needs_no_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    constructor = Mock(side_effect=AssertionError("Must not construct provider"))
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", constructor)
    source = write_workbook(tmp_path / "known.xlsx")
    report = ingest_workbook(source, tmp_path / "known.sqlite", infer_schema=enabled)
    assert report["schema_inference"] == {
        "status": "not_needed" if enabled else "disabled",
        "model": None,
        "inferred_fields": [],
    }
    constructor.assert_not_called()


def test_provider_requires_opt_in(tmp_path: Path) -> None:
    fake = FakeInference()
    with pytest.raises(IngestionError, match="inference_disabled"):
        ingest_workbook("unused.xlsx", tmp_path / "no.sqlite", inference_provider=fake)
    assert not fake.requests


@pytest.mark.parametrize(
    ("field", "item", "prefix"),
    [
        (
            "asset_id",
            {"status": "absent", "column": None, "candidates": []},
            "mapping_missing_required",
        ),
        (
            "state",
            {"status": "absent", "column": None, "candidates": []},
            "mapping_missing_required",
        ),
        (
            "total_insurable_value_micro_usd",
            {"status": "absent", "column": None, "candidates": []},
            "mapping_missing_required",
        ),
        (
            "city",
            {"status": "ambiguous", "column": None, "candidates": [2, 3]},
            "mapping_ambiguous",
        ),
        (
            "city",
            {"status": "mapped", "column": 999, "candidates": [999]},
            "mapping_invalid_column",
        ),
        ("city", {"status": "mapped", "column": 2, "candidates": [2]}, "mapping_conflict"),
    ],
)
def test_invalid_mapping_never_publishes(
    tmp_path: Path, field: str, item: dict, prefix: str
) -> None:
    body = proposal().model_dump()
    body[field] = item
    fake = FakeInference(SchemaProposal.model_validate(body))
    with pytest.raises(IngestionError, match=prefix) as exc:
        import_variant(tmp_path, provider=fake)
    assert exc.value.category == FailureCategory.MAPPING
    assert not (tmp_path / "variant.sqlite").exists()


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("total_insurable_value_micro_usd", "Building"),
        ("total_insurable_value_micro_usd", " building   VALUE "),
        ("total_insurable_value_micro_usd", "Contents"),
        ("total_insurable_value_micro_usd", "Contents Value"),
        ("total_insurable_value_micro_usd", "Rental Income"),
        ("total_insurable_value_micro_usd", "Replacement Cost"),
        ("construction_type", "Construction Quality"),
        ("construction_type", " Cladding   TYPE "),
    ],
)
def test_explicit_semantic_contradictions(tmp_path: Path, field: str, label: str) -> None:
    labels = RENAMED.copy()
    labels[EXPECTED[field] - 1] = label
    with pytest.raises(IngestionError, match="mapping_incompatible"):
        import_variant(tmp_path, labels=labels)
    assert not (tmp_path / "variant.sqlite").exists()


@pytest.mark.parametrize(
    "label", ["Total Insurable Value", "TIV", "Construction Category", "Building Construction"]
)
def test_competing_headings_rejected_before_model(tmp_path: Path, label: str) -> None:
    fake = FakeInference()
    with pytest.raises(IngestionError, match="mapping_ambiguous"):
        import_variant(
            tmp_path, labels=[*RENAMED, label], rows=[[*asset(), "Other"]], provider=fake
        )
    assert not fake.requests


def test_repeated_exact_heading_rejected_before_model(tmp_path: Path) -> None:
    fake = FakeInference()
    with pytest.raises(IngestionError, match="mapping_ambiguous"):
        import_variant(
            tmp_path, labels=[*HEADERS, "City"], rows=[[*asset(), "Other"]], provider=fake
        )
    assert not fake.requests


@pytest.mark.parametrize("override", ["move", "absent", "ambiguous"])
def test_exact_mapping_cannot_be_overridden_or_hidden_by_fallback(
    tmp_path: Path, override: str
) -> None:
    body = proposal().model_dump()
    body["state"] = (
        {"status": "mapped", "column": 3, "candidates": [3]}
        if override == "move"
        else {
            "status": override,
            "column": None,
            "candidates": [3, 4] if override == "ambiguous" else [],
        }
    )
    labels = RENAMED.copy()
    labels[3] = "State"
    with pytest.raises(IngestionError, match="mapping_conflict"):
        import_variant(
            tmp_path, labels=labels, provider=FakeInference(SchemaProposal.model_validate(body))
        )
    assert not (tmp_path / "variant.sqlite").exists()


def test_unlabelled_inferred_column(tmp_path: Path) -> None:
    labels = RENAMED.copy()
    labels[2] = ""
    with pytest.raises(IngestionError, match="mapping_invalid_column"):
        import_variant(tmp_path, labels=labels)


@pytest.mark.parametrize(
    "field", ["asset_id", "city", "state", "construction_type", "total_insurable_value_micro_usd"]
)
@pytest.mark.parametrize("value", [True, date(2026, 1, 1), "#VALUE!", 123])
def test_conclusive_type_contradictions(tmp_path: Path, field: str, value: Any) -> None:
    row = asset()
    row[EXPECTED[field] - 1] = value
    if field == "total_insurable_value_micro_usd" and value == 123:
        import_variant(tmp_path, rows=[row])
    else:
        with pytest.raises(IngestionError, match="mapping_incompatible"):
            import_variant(tmp_path, rows=[row])


@pytest.mark.parametrize("value", [None, "not a value", True, "=G2+H2"])
def test_occasional_invalid_cells_remain_normalization_responsibility(
    tmp_path: Path, value: Any
) -> None:
    db, report = import_variant(tmp_path, rows=[asset("a", value), asset("b", "1")])
    assert report["physical_records"] == 2
    assert report["valuation_complete"] is False
    assert saved_manifest(db)["concepts"]["total_insurable_value_micro_usd"]["column"] == 5


@pytest.mark.parametrize(
    "item",
    [
        {"status": "mapped", "column": 0, "candidates": [0]},
        {"status": "mapped", "column": -1, "candidates": [-1]},
        {"status": "mapped", "column": True, "candidates": [True]},
        {"status": "mapped", "column": 1, "candidates": [1, 2]},
        {"status": "mapped", "column": 2, "candidates": [1]},
        {"status": "absent", "column": 1, "candidates": []},
        {"status": "ambiguous", "column": None, "candidates": [1, 1]},
        {"status": "ambiguous", "column": None, "candidates": [1]},
        {"status": "mapped", "column": 1, "candidates": [1], "resolution": "trust me"},
    ],
)
def test_strict_proposal_invariants(item: dict) -> None:
    with pytest.raises(ValidationError):
        ColumnProposal.model_validate(item)


@pytest.mark.parametrize("mutation", ["extra", "missing", "unknown"])
def test_exact_six_concept_contract(mutation: str) -> None:
    body = proposal().model_dump()
    if mutation == "missing":
        body.pop("city")
    else:
        body["reasoning" if mutation == "extra" else "roof"] = "not allowed"
    with pytest.raises(ValidationError):
        SchemaProposal.model_validate(body)


@pytest.mark.parametrize("mandatory_exact", [False, True])
def test_technical_fallback_requires_independent_deterministic_manifest(
    tmp_path: Path, mandatory_exact: bool
) -> None:
    labels = HEADERS.copy() if mandatory_exact else RENAMED.copy()
    labels[5] = RENAMED[5]
    fake = FakeInference(fail=True)
    if mandatory_exact:
        db, report = import_variant(tmp_path, labels=labels, provider=fake)
        assert report["schema_inference"] == {
            "status": "fallback",
            "model": None,
            "inferred_fields": [],
        }
        assert saved_manifest(db)["concepts"]["construction_type"]["status"] == "absent"
        assert report["diagnostic_summary"]["schema_inference_fallback"] == 1
    else:
        with pytest.raises(IngestionError) as exc:
            import_variant(tmp_path, labels=labels, provider=fake)
        assert exc.value.category == FailureCategory.INFERENCE
        assert not (tmp_path / "variant.sqlite").exists()
    assert len(fake.requests) == 1


@pytest.mark.parametrize("technical", [False, True])
def test_failed_replacement_preserves_database(tmp_path: Path, technical: bool) -> None:
    db = tmp_path / "existing.sqlite"
    ingest_workbook(write_workbook(tmp_path / "known.xlsx"), db)
    original = db.read_bytes()
    source = write_workbook(tmp_path / "renamed.xlsx", headers=RENAMED, summary=False)
    fake = FakeInference(proposal({**EXPECTED, "state": None}), fail=technical)
    with pytest.raises(IngestionError):
        ingest_workbook(source, db, replace=True, infer_schema=True, inference_provider=fake)
    assert db.read_bytes() == original
    assert not list(tmp_path.glob(".sov-import-*"))


@pytest.mark.parametrize(
    "kind",
    ["row11", "preamble", "multiple", "merged_header", "merged_data", "interior_blank", "width"],
)
def test_structure_rejection_precedes_inference(tmp_path: Path, kind: str) -> None:
    source = write_workbook(
        tmp_path / "bad.xlsx",
        headers=RENAMED,
        summary=False,
        rows=[asset("a"), asset("b"), asset("c")],
        header=11 if kind == "row11" else 5 if kind == "preamble" else 1,
        extra_table=kind == "multiple",
    )
    wb = load_workbook(source)
    ws = wb["Statement of Values"]
    if kind == "preamble":
        ws["B2"] = "Unsupported"
    elif kind == "merged_header":
        ws.merge_cells("B1:C1")
    elif kind == "merged_data":
        ws.merge_cells("B2:C2")
    elif kind == "interior_blank":
        ws.delete_rows(3)
        ws.insert_rows(3)
    elif kind == "width":
        ws["Z2"] = "outside"
    wb.save(source)
    wb.close()
    fake = FakeInference()
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, tmp_path / "no.sqlite", infer_schema=True, inference_provider=fake)
    assert exc.value.category == FailureCategory.STRUCTURE
    assert not fake.requests
    assert not (tmp_path / "no.sqlite").exists()


@pytest.mark.parametrize("identifier", [None, "", "   ", 123, True, "=A9"])
def test_renamed_identifier_never_disappears(tmp_path: Path, identifier: Any) -> None:
    with pytest.raises(IngestionError):
        import_variant(tmp_path, rows=[asset(identifier)])
    assert not (tmp_path / "variant.sqlite").exists()


@pytest.mark.parametrize(
    "kind", ["valid", "wrong_range", "wrong_column", "no_tiv", "after", "literal", "identifier"]
)
def test_strict_mapped_terminal_summary(tmp_path: Path, kind: str) -> None:
    source = write_workbook(tmp_path / "summary.xlsx", headers=RENAMED, summary=False, trailing=0)
    wb = load_workbook(source)
    ws = wb["Statement of Values"]
    ws["G1"] = "Building"
    ws["E3"] = "=SUM(E2:E2)"
    ws["G3"] = "=SUM(G2:G2)"
    if kind == "wrong_range":
        ws["E3"] = "=SUM(E1:E2)"
    elif kind == "wrong_column":
        ws["F3"] = "=SUM(F2:F2)"
    elif kind == "no_tiv":
        ws["E3"] = None
    elif kind == "after":
        ws.append(asset("later"))
    elif kind == "literal":
        ws["G3"] = 123
    elif kind == "identifier":
        ws["A3"] = "=SUM(A2:A2)"
    wb.save(source)
    wb.close()
    db = tmp_path / "summary.sqlite"
    if kind == "valid":
        report = ingest_workbook(source, db, infer_schema=True, inference_provider=FakeInference())
        assert isinstance(report["excluded_rows"], dict)
        assert report["excluded_rows"]["summary"] == 1
        assert report["physical_records"] == 1
    else:
        with pytest.raises(IngestionError):
            ingest_workbook(source, db, infer_schema=True, inference_provider=FakeInference())
        assert not db.exists()


@pytest.mark.parametrize("kind", ["labels", "columns"])
def test_inference_metadata_limits_fail_before_provider(tmp_path: Path, kind: str) -> None:
    fake = FakeInference()
    labels = RENAMED.copy()
    if kind == "labels":
        labels[0] = "x" * 257
    else:
        labels += [f"Unrelated {i}" for i in range(129 - len(labels))]
    with pytest.raises(IngestionError):
        import_variant(tmp_path, labels=labels, provider=fake)
    assert not fake.requests


def test_bounded_private_type_profiles(tmp_path: Path) -> None:
    fake = FakeInference()
    rows = [asset(f"SECRET-ID-{i}", "1234567.891234") for i in range(40)]
    rows[0][2] = True
    rows[1][2] = date(2026, 1, 1)
    rows[2][2] = "#VALUE!"
    rows[3][2] = "=A2"
    rows[4][2] = None
    rows[5][2] = 123
    import_variant(tmp_path, provider=fake, rows=rows)
    request = fake.requests[0]
    assert request.columns[2].profile.model_dump() == dict(
        blank=1, text=26, number=1, boolean=1, date_time=1, formula=1, error=1, other=0
    )
    assert all(sum(c.profile.model_dump().values()) == 32 for c in request.columns)
    serialized = request.model_dump_json()
    for private in [
        "SECRET-ID",
        "1234567",
        "Synthetic property",
        "Test City",
        "=A2",
        "variant.xlsx",
        str(tmp_path),
        "Statement of Values",
        hashlib.sha256((tmp_path / "variant.xlsx").read_bytes()).hexdigest(),
    ]:
        assert private not in serialized


@pytest.mark.parametrize("header", [1, 5])
@pytest.mark.parametrize("permuted", [False, True])
def test_a4_business_results_and_complete_original_coordinates(
    tmp_path: Path, header: int, permuted: bool
) -> None:
    root = Path(__file__).parents[1]
    demo = run_path(str(root / "scripts/demo.py"))
    wb = load_workbook(root / "examples/synthetic-sov.xlsx", data_only=True)
    ws = wb["Statement of Values"]
    labels = [str(c.value) for c in ws[1]]
    original_rows = [[c.value for c in ws[r]] for r in range(2, 7)]
    wb.close()
    renamed = [RENAMED[HEADERS.index(label)] if label in HEADERS else label for label in labels]
    order = list(reversed(range(len(labels)))) if permuted else list(range(len(labels)))
    positions = {field: order.index(index - 1) + 1 for field, index in EXPECTED.items()}
    source = write_workbook(
        tmp_path / "business.xlsx",
        header=header,
        headers=[renamed[i] for i in order],
        rows=[[row[i] for i in order] for row in original_rows],
        summary=False,
    )
    db = tmp_path / "business.sqlite"
    report = ingest_workbook(
        source, db, infer_schema=True, inference_provider=FakeInference(proposal(positions))
    )
    assert (
        report["physical_records"],
        report["logical_assets"],
        report["verified_duplicate_groups"],
    ) == (5, 4, 1)
    assert report["valuation_complete"] is True
    source.unlink()  # All provenance must now come from the imported snapshot.
    expected_contributors = (
        {"SYN-001", "SYN-002"},
        {"SYN-001", "SYN-002"},
        {"SYN-001", "SYN-002"},
        {"SYN-001"},
        {"SYN-001"},
        {"SYN-001"},
        set(),
        {"SYN-001", "SYN-002", "SYN-003", "SYN-004"},
    )
    for case, expected_ids in zip(demo["CASES"], expected_contributors, strict=True):
        answer = ask(db, case.question, demo["FixedSQLGenerator"](case))
        assert isinstance(answer, Success)
        value = (
            len(answer.result.records)
            if isinstance(answer.result, ListValue)
            else answer.result.value
        )
        assert value == case.expected
        assert answer.matching_count == case.matching_count
        assert answer.evidence.source_record_count == case.physical_count
        assert (
            answer.verification.reconciled
            and answer.verification.complete
            and answer.verification.same_snapshot
        )
        actual_ids = {c.asset_id for c in answer.evidence.contributors}
        assert actual_ids == expected_ids
        if case.operation == "sum" and not case.sql.partition(" WHERE ")[2]:
            assert actual_ids == {str(r[0]).strip() for r in original_rows}
        for contributor in answer.evidence.contributors:
            expected_rows = [
                header + i + 1
                for i, row in enumerate(original_rows)
                if str(row[0]).strip() == contributor.asset_id
            ]
            assert [s.row for s in contributor.source_records] == expected_rows
            for record in contributor.source_records:
                original = original_rows[record.row - header - 1]
                for cell in record.cells:
                    field = cell.semantic_field
                    index = positions[field]
                    assert cell.cell_coordinate == f"{get_column_letter(index)}{record.row}"
                    assert cell.original_value == original[EXPECTED[field] - 1]
                    assert cell.source_header == renamed[EXPECTED[field] - 1]
        if case.physical_count > case.matching_count:
            assert any("duplicate" in q for q in answer.qualifications)


def test_inferred_money_retains_exact_xml_formula_cache_and_qualifications(tmp_path: Path) -> None:
    source = write_workbook(
        tmp_path / "precision.xlsx",
        headers=RENAMED,
        summary=False,
        rows=[asset("a", 1), asset("b", "=G3+H3")],
    )
    patch_numbers(source, {"E2": "10000000000.000001"})
    patch_caches(source, {"E3": "1.0050004"})
    db = tmp_path / "precision.sqlite"
    ingest_workbook(source, db, infer_schema=True, inference_provider=FakeInference())
    from test_qa import FakeGenerator

    answer = ask(
        db,
        "Total value?",
        FakeGenerator("SELECT SUM(total_insurable_value_micro_usd) FROM assets", "sum"),
    )
    assert isinstance(answer, Success)
    assert not isinstance(answer.result, ListValue)
    assert answer.result.value == 10000000001005001
    assert answer.verification.evidence_sum_micro_usd == 10000000001005001
    assert any("precision reduction" in q for q in answer.qualifications)
    cells = [
        cell
        for c in answer.evidence.contributors
        for r in c.source_records
        for cell in r.cells
        if cell.semantic_field == "total_insurable_value_micro_usd"
    ]
    assert cells[0].original_value == Decimal("10000000000.000001")
    assert cells[1].formula == "=G3+H3"
    assert cells[1].cached_value == "1.0050004"


@pytest.mark.parametrize("nullable", ["state", "total_insurable_value_micro_usd"])
def test_inferred_nullable_values_keep_material_completeness_failure(
    tmp_path: Path, nullable: str
) -> None:
    from test_qa import FakeGenerator

    from sovtrace.qa import Failure

    row = asset("missing")
    row[EXPECTED[nullable] - 1] = None
    db, report = import_variant(tmp_path, rows=[row, asset("present")])
    assert report["field_completeness"][nullable]["logical_non_null"] == 1
    sql = (
        "SELECT SUM(total_insurable_value_micro_usd) FROM assets"
        if nullable != "state"
        else "SELECT COUNT(*) FROM assets WHERE state='AZ'"
    )
    operation = "sum" if nullable != "state" else "count"
    answer = ask(db, "Total value of Arizona assets?", FakeGenerator(sql, operation))
    assert isinstance(answer, Failure) and answer.category == "data_incomplete"


def test_conflicting_duplicates_in_inferred_identifier_still_fail(tmp_path: Path) -> None:
    first, second = asset("same"), asset("same")
    second[-1] = "different unmapped business value"
    with pytest.raises(IngestionError) as exc:
        import_variant(tmp_path, rows=[first, second])
    assert exc.value.category == FailureCategory.DUPLICATE
    assert not (tmp_path / "variant.sqlite").exists()


@pytest.mark.parametrize("header", [1, 5])
def test_fallback_discovery_rejects_leading_blank_header_column(
    tmp_path: Path, header: int
) -> None:
    source = write_workbook(
        tmp_path / "shifted.xlsx",
        header=header,
        headers=["", *RENAMED],
        rows=[[None, *asset()]],
        summary=False,
    )
    fake = FakeInference(proposal({field: index + 1 for field, index in EXPECTED.items()}))
    db = tmp_path / "no.sqlite"
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, db, infer_schema=True, inference_provider=fake)
    assert exc.value.category == FailureCategory.STRUCTURE
    assert not fake.requests
    assert not db.exists()


@pytest.mark.parametrize("infer_schema", [False, True])
def test_exact_discovery_retains_leading_blank_header_column(
    tmp_path: Path, infer_schema: bool
) -> None:
    source = write_workbook(
        tmp_path / "exact-shifted.xlsx",
        headers=["", *HEADERS],
        rows=[[None, *asset()]],
        summary=False,
    )
    fake = FakeInference()
    db = tmp_path / "exact.sqlite"
    report = ingest_workbook(
        source,
        db,
        infer_schema=infer_schema,
        inference_provider=fake if infer_schema else None,
    )
    assert report["schema_inference"] == {
        "status": "not_needed" if infer_schema else "disabled",
        "model": None,
        "inferred_fields": [],
    }
    assert not fake.requests
    manifest = saved_manifest(db)
    assert manifest["columns"][0] == {"index": 1, "letter": "A", "label": None}
    assert {f: m["column"] for f, m in manifest["concepts"].items()} == {
        field: index + 1 for field, index in EXPECTED.items()
    }


def test_fallback_discovery_preserves_blank_source_column_and_128_column_limit(
    tmp_path: Path,
) -> None:
    labels = RENAMED.copy()
    labels[10] = ""
    labels += [f"Extra {i}" for i in range(128 - len(labels))]
    db, _ = import_variant(tmp_path, labels=labels)
    manifest = saved_manifest(db)
    assert len(manifest["columns"]) == 128
    assert manifest["columns"][10] == {"index": 11, "letter": "K", "label": None}
    assert [(c["index"], c["letter"]) for c in manifest["columns"]] == [
        (index, get_column_letter(index)) for index in range(1, 129)
    ]
    assert {f: m["column"] for f, m in manifest["concepts"].items()} == EXPECTED
    with sqlite3.connect(db) as connection:
        for index in (*EXPECTED.values(), 11):
            coordinate, original = connection.execute(
                "SELECT coordinate, original_value_json FROM source_cells "
                "WHERE row_number=2 AND column_index=?",
                (index,),
            ).fetchone()
            assert coordinate == f"{get_column_letter(index)}2"
            assert json.loads(original) == asset()[index - 1]


def test_exact_unsupported_row_cannot_be_bypassed_with_inference(tmp_path: Path) -> None:
    fake = FakeInference()
    source = write_workbook(tmp_path / "row11.xlsx", header=11)
    with pytest.raises(IngestionError, match="supported header rows"):
        ingest_workbook(source, tmp_path / "no.sqlite", infer_schema=True, inference_provider=fake)
    assert not fake.requests


@pytest.mark.parametrize("error_header", [False, True])
def test_invalid_fallback_header_types_do_not_reach_provider(
    tmp_path: Path, error_header: bool
) -> None:
    labels = RENAMED.copy()
    labels[2] = "#VALUE!" if error_header else "=1+1"
    fake = FakeInference()
    with pytest.raises(IngestionError) as exc:
        import_variant(tmp_path, labels=labels, provider=fake)
    assert exc.value.category == FailureCategory.STRUCTURE
    assert not fake.requests


def test_empty_known_table_with_unresolved_optional_field_has_no_model_call(tmp_path: Path) -> None:
    labels = HEADERS.copy()
    labels[5] = RENAMED[5]
    fake = FakeInference()
    with pytest.raises(IngestionError, match="no asset records"):
        import_variant(tmp_path, labels=labels, rows=[], provider=fake)
    assert not fake.requests


def test_cli_inference_flag_uses_same_offline_path_for_known_headers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sovtrace.cli import main

    source = write_workbook(tmp_path / "cli.xlsx")
    main(
        [
            "ingest",
            "--format",
            "json",
            str(source),
            "--db",
            str(tmp_path / "cli.sqlite"),
            "--infer-schema",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert report["schema_inference"]["status"] == "not_needed"
    assert report["logical_assets"] == 1


def test_python_api_default_remains_deterministic_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructor = Mock(side_effect=AssertionError("Must not construct provider"))
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", constructor)
    source = write_workbook(tmp_path / "renamed.xlsx", headers=RENAMED, summary=False)
    db = tmp_path / "no.sqlite"
    with pytest.raises(IngestionError) as exc:
        ingest_workbook(source, db)
    assert exc.value.category == FailureCategory.STRUCTURE
    constructor.assert_not_called()
    assert not db.exists()
