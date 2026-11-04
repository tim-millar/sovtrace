"""Bounded schema metadata and deterministic admissibility of untrusted proposals."""

from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal, Protocol, Self

from openpyxl.worksheet.worksheet import Worksheet
from pydantic import Field, model_validator

from . import resources
from .discovery import Structure, populated
from .errors import FailureCategory, IngestionError
from .manifest import (
    FIELD_DEFINITIONS,
    ConceptMapping,
    Contract,
    Diagnostic,
    SemanticField,
    exact_mappings,
)

ColumnIndex = Annotated[int, Field(gt=0, strict=True)]


class ColumnProposal(Contract):
    status: Literal["mapped", "absent", "ambiguous"]
    column: ColumnIndex | None
    candidates: tuple[ColumnIndex, ...]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("Candidates must be distinct")
        if self.status == "mapped" and (self.column is None or self.candidates != (self.column,)):
            raise ValueError("Mapped proposal requires exactly its selected candidate")
        if self.status == "absent" and (self.column is not None or self.candidates):
            raise ValueError("Absent proposal must have no candidates")
        if self.status == "ambiguous" and (self.column is not None or len(self.candidates) < 2):
            raise ValueError("Ambiguous proposal requires at least two candidates")
        return self


class SchemaProposal(Contract):
    asset_id: ColumnProposal
    property_name: ColumnProposal
    city: ColumnProposal
    state: ColumnProposal
    total_insurable_value_micro_usd: ColumnProposal
    construction_type: ColumnProposal


class TypeProfile(Contract):
    blank: int = 0
    text: int = 0
    number: int = 0
    boolean: int = 0
    date_time: int = 0
    formula: int = 0
    error: int = 0
    other: int = 0


class InferenceColumn(Contract):
    index: int
    letter: str
    label: str | None
    profile: TypeProfile


class InferenceRequest(Contract):
    columns: tuple[InferenceColumn, ...]


class InferenceUsage(Contract):
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cached_input_tokens: int


class InferenceResult(Contract):
    proposal: SchemaProposal
    model: str | None = None
    usage: InferenceUsage | None = None


class SchemaInferenceProvider(Protocol):
    def infer(self, request: InferenceRequest) -> InferenceResult: ...


def mapping_error(prefix: str, field: SemanticField | None = None) -> IngestionError:
    return IngestionError(FailureCategory.MAPPING, f"{prefix}: {field or 'schema'}")


def make_request(sheet: Worksheet, structure: Structure) -> InferenceRequest:
    resources.check_sheet_bounds(sheet)
    columns = structure.columns
    if (
        len(columns) > resources.MAX_INFERENCE_COLUMNS
        or any(len(c.label or "") > 256 for c in columns)
        or sum(len(c.label or "") for c in columns) > 32768
    ):
        raise IngestionError(FailureCategory.INFERENCE, "schema_inference_metadata_limit")
    rows: list[int] = []
    for r in range(structure.header_row + 1, sheet.max_row + 1):
        if any(populated(c.value) for c in sheet[r][: len(columns)]):
            rows.append(r)
        else:
            break
        if len(rows) == 32:
            break
    result = []
    for column in columns:
        counts = dict.fromkeys(TypeProfile.model_fields, 0)
        for r in rows:
            cell = sheet.cell(r, column.index)
            value = cell.value
            if not populated(value):
                kind = "blank"
            elif cell.data_type == "f":
                kind = "formula"
            elif cell.data_type == "e":
                kind = "error"
            elif isinstance(value, bool):
                kind = "boolean"
            elif isinstance(value, (datetime, date, time, timedelta)):
                kind = "date_time"
            elif isinstance(value, (int, float)):
                kind = "number"
            elif isinstance(value, str):
                kind = "text"
            else:
                kind = "other"
            counts[kind] += 1
        result.append(
            InferenceColumn(
                index=column.index,
                letter=column.letter,
                label=column.label,
                profile=TypeProfile(**counts),
            )
        )
    return InferenceRequest(columns=tuple(result))


def normalized_label(label: str | None) -> str:
    return " ".join((label or "").split()).casefold()


def check_competitors(structure: Structure) -> None:
    for group in (
        {"total insurable values", "total insurable value", "total insured value", "tiv"},
        {"construction type", "building construction", "construction category"},
    ):
        if sum(normalized_label(c.label) in group for c in structure.columns) > 1:
            raise mapping_error("mapping_ambiguous")


def resolve(
    structure: Structure,
    request: InferenceRequest,
    proposal: SchemaProposal,
) -> tuple[dict[SemanticField, ConceptMapping], tuple[Diagnostic, ...], list[str]]:
    exact, _ = exact_mappings(structure.columns, structure.worksheet)
    concepts = {}
    diagnostics = []
    inferred = []
    selected: set[int] = set()
    prohibited = {
        SemanticField.TIV: {
            "building",
            "building value",
            "contents",
            "contents value",
            "rental income",
            "replacement cost",
        },
        SemanticField.CONSTRUCTION: {"construction quality", "cladding type"},
    }
    for field, (_, dtype, required) in FIELD_DEFINITIONS.items():
        item: ColumnProposal = getattr(proposal, field.value)
        if any(c > len(structure.columns) for c in item.candidates):
            raise mapping_error("mapping_invalid_column", field)
        known = exact[field]
        if known.status == "mapped" and (item.status != "mapped" or item.column != known.column):
            raise mapping_error("mapping_conflict", field)
        if item.status == "ambiguous":
            raise mapping_error("mapping_ambiguous", field)
        if required and item.status == "absent":
            raise mapping_error("mapping_missing_required", field)
        code = "concept_absent"
        if item.column is not None:
            if item.column in selected:
                raise mapping_error("mapping_conflict", field)
            selected.add(item.column)
            column = request.columns[item.column - 1]
            if not (column.label or "").strip():
                raise mapping_error("mapping_invalid_column", field)
            if normalized_label(column.label) in prohibited.get(field, set()):
                raise mapping_error("mapping_incompatible", field)
            code = "concept_exact"
            if known.status != "mapped":
                profile = column.profile
                total = sum(profile.model_dump().values()) - profile.blank
                allowed = profile.text + profile.formula
                if field == SemanticField.TIV:
                    allowed += profile.number
                    incompatible = profile.boolean + profile.date_time + profile.error
                else:
                    incompatible = (
                        profile.number + profile.boolean + profile.date_time + profile.error
                    )
                if total and not allowed and incompatible == total:
                    raise mapping_error("mapping_incompatible", field)
                inferred.append(field.value)
                code = "concept_inferred"
        concepts[field] = ConceptMapping(
            status=item.status,
            column=item.column,
            candidates=item.candidates,
            normalized_type=dtype,
        )
        diagnostics.append(
            Diagnostic(
                code=code,
                message=f"{field}: {item.status}",
                worksheet=structure.worksheet,
                field=field,
                column=item.column,
            )
        )
    return concepts, tuple(diagnostics), inferred
