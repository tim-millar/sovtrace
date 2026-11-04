"""Provider-independent schema contract and the source-label label interpreter."""

from enum import StrEnum
from typing import Literal, Self

from openpyxl.utils import get_column_letter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import FailureCategory, IngestionError


class SemanticField(StrEnum):
    ASSET_ID = "asset_id"
    PROPERTY_NAME = "property_name"
    CITY = "city"
    STATE = "state"
    TIV = "total_insurable_value_micro_usd"
    CONSTRUCTION = "construction_type"


# Label, normalized type, required schema concept (individual cells may still be invalid).
FIELD_DEFINITIONS: dict[SemanticField, tuple[str, str, bool]] = {
    SemanticField.ASSET_ID: ("EPIC Code", "string", True),
    SemanticField.PROPERTY_NAME: ("Property Name", "nullable_string", False),
    SemanticField.CITY: ("City", "nullable_string", False),
    SemanticField.STATE: ("State", "nullable_state", True),
    SemanticField.TIV: ("Total Insurable Values", "nullable_int64_micro_usd", True),
    SemanticField.CONSTRUCTION: ("Construction Type", "nullable_string", False),
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Diagnostic(Contract):
    code: str
    message: str
    worksheet: str
    row: int | None = None
    column: int | None = None
    field: SemanticField | None = None
    severity: Literal["info", "warning"] = "info"


class SourceColumn(Contract):
    index: int = Field(gt=0)
    letter: str
    label: str | None

    @model_validator(mode="after")
    def valid_coordinate(self) -> Self:
        if self.letter != get_column_letter(self.index):
            raise ValueError("Column letter must match its physical index")
        return self


class ConceptMapping(Contract):
    status: Literal["mapped", "absent", "ambiguous"]
    column: int | None = None
    candidates: tuple[int, ...] = ()
    normalized_type: str
    resolution: str | None = None

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("Candidate columns must be distinct")
        if self.status == "absent" and (self.column is not None or self.candidates):
            raise ValueError("Absent concepts cannot reference columns")
        if self.status == "ambiguous" and (self.column is not None or len(self.candidates) < 2):
            raise ValueError("Ambiguous concepts require multiple candidates and no selection")
        if self.status == "mapped":
            if self.column is None or self.column not in self.candidates:
                raise ValueError("Mapped concept must select one candidate column")
            if len(self.candidates) > 1 and not (self.resolution and self.resolution.strip()):
                raise ValueError("Multiple candidates require an explicit resolution")
        return self


class SchemaManifest(Contract):
    workbook_filename: str = Field(min_length=1)
    workbook_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    worksheet: str = Field(min_length=1)
    header_row: int = Field(gt=0)
    data_start_row: int = Field(gt=0)
    data_end_row: int = Field(gt=0)
    columns: tuple[SourceColumn, ...]
    concepts: dict[SemanticField, ConceptMapping]
    diagnostics: tuple[Diagnostic, ...] = ()

    @model_validator(mode="after")
    def usable_schema(self) -> Self:
        indices = [c.index for c in self.columns]
        if not indices or indices != list(range(1, len(indices) + 1)):
            raise ValueError("Source columns must retain distinct contiguous physical positions")
        if not self.header_row < self.data_start_row <= self.data_end_row:
            raise ValueError("Invalid source table boundaries")
        if set(self.concepts) != set(FIELD_DEFINITIONS):
            raise ValueError("Manifest must describe every supported concept")
        for field, mapping in self.concepts.items():
            _, expected_type, required = FIELD_DEFINITIONS[field]
            if mapping.normalized_type != expected_type:
                raise ValueError(f"{field}: expected normalized type {expected_type}")
            if any(c not in indices for c in mapping.candidates):
                raise ValueError(f"{field}: mapping references a nonexistent source column")
            if mapping.status == "ambiguous":
                raise ValueError(f"{field}: ambiguous mapping to columns {mapping.candidates}")
            if required and mapping.status != "mapped":
                raise ValueError(f"{field}: required concept is absent")
        selected = [m.column for m in self.concepts.values() if m.status == "mapped"]
        if len(selected) != len(set(selected)):
            raise ValueError("Different supported concepts cannot silently share a source column")
        return self


def interpret_schema(
    filename: str,
    sha256: str,
    worksheet: str,
    header_row: int,
    data_start: int,
    data_end: int,
    columns: tuple[SourceColumn, ...],
) -> SchemaManifest:
    concepts, diagnostics = exact_mappings(columns, worksheet)
    try:
        return SchemaManifest(
            workbook_filename=filename,
            workbook_sha256=sha256,
            worksheet=worksheet,
            header_row=header_row,
            data_start_row=data_start,
            data_end_row=data_end,
            columns=columns,
            concepts=concepts,
            diagnostics=tuple(diagnostics),
        )
    except ValidationError as exc:
        # Do not include source cell contents in a mapping error.
        details = "; ".join(e["msg"] for e in exc.errors(include_input=False))
        raise IngestionError(FailureCategory.MAPPING, details) from exc


def exact_mappings(
    columns: tuple[SourceColumn, ...], worksheet: str
) -> tuple[dict[SemanticField, ConceptMapping], tuple[Diagnostic, ...]]:
    concepts: dict[SemanticField, ConceptMapping] = {}
    diagnostics: list[Diagnostic] = []
    for field, (label, dtype, _) in FIELD_DEFINITIONS.items():
        matches = tuple(c.index for c in columns if c.label == label)
        status = "absent" if not matches else "mapped" if len(matches) == 1 else "ambiguous"
        concepts[field] = ConceptMapping(
            status=status,
            column=matches[0] if len(matches) == 1 else None,
            candidates=matches,
            normalized_type=dtype,
        )
        diagnostics.append(
            Diagnostic(
                code=f"concept_{status}",
                message=f"{field}: {status} using exact label {label!r}",
                worksheet=worksheet,
                column=matches[0] if len(matches) == 1 else None,
                field=field,
            )
        )
    return concepts, tuple(diagnostics)
