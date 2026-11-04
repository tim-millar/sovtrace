"""Typed proposals, immutable plans, verified answers and expected QA failures."""

from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    field_serializer,
    model_serializer,
    model_validator,
)

from ..ingestion.manifest import SchemaManifest, SemanticField

Operation = Literal["exists", "count", "sum", "list"]
Category = Literal[
    "configuration_error",
    "provider_error",
    "invalid_model_response",
    "unsupported_question",
    "unavailable_field",
    "sql_validation_error",
    "data_incomplete",
    "query_execution_error",
    "resource_limit_exceeded",
    "evidence_incomplete",
    "evidence_mismatch",
    "evidence_limit_exceeded",
    "evidence_query_error",
    "source_reference_invalid",
]
TIV = SemanticField.TIV.value
FIELDS = tuple(f.value for f in SemanticField)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Proposal(Contract):
    status: Literal["query", "unsupported"]
    operation: Operation | None
    sql: str | None
    reason: str | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status == "query":
            if (
                self.operation is None
                or not self.sql
                or not self.sql.strip()
                or self.reason is not None
            ):
                raise ValueError("Query requires operation and SQL, and no reason")
        elif (
            self.operation is not None
            or self.sql is not None
            or not (self.reason and self.reason.strip())
        ):
            raise ValueError("Unsupported requires reason and no operation or SQL")
        return self


class Failure(Contract):
    status: Literal["failure"] = "failure"
    category: Category
    code: str
    message: str
    evidence_limit: int | None = None
    required_contributor_count: int | None = None


class QAError(Exception):
    def __init__(
        self,
        category: Category,
        code: str,
        message: str,
        *,
        evidence_limit: int | None = None,
        required_contributor_count: int | None = None,
    ):
        super().__init__(message)
        self.failure = Failure(
            category=category,
            code=code,
            message=message,
            evidence_limit=evidence_limit,
            required_contributor_count=required_contributor_count,
        )


class DatasetIdentity(Contract):
    dataset_id: str
    workbook_sha256: str
    workbook_filename: str
    worksheet: str


@dataclass(frozen=True)
class QueryContext:
    identity: DatasetIdentity
    manifest: SchemaManifest
    available_fields: frozenset[str]
    incomplete_fields: frozenset[str]
    qualifications: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedQuery:
    operation: Operation
    proposed_sql: str
    executed_sql: str
    filter: str | None
    referenced_fields: frozenset[str]
    predicate_fields: frozenset[str]
    ordinary_predicate_fields: frozenset[str]
    result_fields: tuple[str, ...]
    query_shape: Literal["scalar_aggregate", "asset_listing"]
    dataset_id: str
    allows_missing: bool


class AssetRecord(Contract):
    asset_id: str
    property_name: str | None = None
    city: str | None = None
    state: str | None = None
    total_insurable_value_micro_usd: int | None = None
    construction_type: str | None = None

    @model_serializer
    def selected_values(self, info: SerializationInfo) -> dict[str, object]:
        # Omitted projections must not appear as recorded NULL values.
        return {
            field: str(getattr(self, field))
            if info.mode == "json" and field == TIV and getattr(self, field) is not None
            else getattr(self, field)
            for field in FIELDS
            if field in self.model_fields_set
        }


class ExistsValue(Contract):
    kind: Literal["exists"] = "exists"
    value: bool
    unit: Literal["boolean"] = "boolean"


class CountValue(Contract):
    kind: Literal["count"] = "count"
    value: int = Field(ge=0)
    unit: Literal["logical_assets"] = "logical_assets"


class SumValue(Contract):
    kind: Literal["sum"] = "sum"
    value: int | None
    unit: Literal["micro_usd"] = "micro_usd"
    currency: Literal["USD"] = "USD"
    outcome: Literal["value", "no_matching_records"]

    @field_serializer("value", when_used="json")
    def exact_json(self, value: int | None) -> str | None:
        return str(value) if value is not None else None


class ListValue(Contract):
    kind: Literal["list"] = "list"
    records: tuple[AssetRecord, ...]
    selected_fields: tuple[str, ...]
    unit: Literal["logical_assets"] = "logical_assets"


class SourceCellReference(Contract):
    semantic_field: str
    column_index: int
    column_letter: str
    cell_coordinate: str
    source_header: str | None
    original_type: str
    original_value: Any
    formula: str | None
    cached_type: str | None
    cached_value: Any
    normalized_value: str | int | None
    calculation_value: Literal["original_value", "cached_value"]
    normalization: tuple[str, ...] = ()

    @field_serializer("normalized_value", when_used="json")
    def exact_json(self, value: str | int | None) -> str | int | None:
        return str(value) if self.semantic_field == TIV and value is not None else value


class SourceRecordReference(Contract):
    row: int
    cells: tuple[SourceCellReference, ...]


class Contributor(Contract):
    asset_id: str
    values: AssetRecord
    source_records: tuple[SourceRecordReference, ...]


class Evidence(Contract):
    contributor_sql: str
    required_fields: tuple[str, ...]
    matched_asset_count: int
    contributing_asset_count: int
    source_record_count: int
    contributors: tuple[Contributor, ...]


class Verification(Contract):
    reconciled: Literal[True] = True
    complete: Literal[True] = True
    same_snapshot: Literal[True] = True
    method: Literal[
        "boolean_and_count", "distinct_asset_count", "exact_integer_sum", "complete_listing"
    ]
    evidence_limit: int = 500
    dataset_logical_assets: int
    dataset_physical_records: int
    predicate_fields: tuple[str, ...]
    completeness_checked: tuple[str, ...]
    executed_count: int
    result_micro_usd: int | None = None
    evidence_sum_micro_usd: int | None = None

    @field_serializer("result_micro_usd", "evidence_sum_micro_usd", when_used="json")
    def exact_json(self, value: int | None) -> str | None:
        return str(value) if value is not None else None


class Success(Contract):
    status: Literal["verified"] = "verified"
    question: str
    dataset: DatasetIdentity
    model: str | None
    operation: Operation
    proposed_sql: str
    executed_sql: str
    result: ExistsValue | CountValue | SumValue | ListValue
    matching_count: int = Field(ge=0)
    referenced_fields: tuple[str, ...]
    qualifications: tuple[str, ...]
    plan: ValidatedQuery = Field(exclude=True)
    summary: str
    display_value: str | None = None
    evidence: Evidence
    verification: Verification
