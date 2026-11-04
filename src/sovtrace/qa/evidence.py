"""Deterministic, complete provenance within the caller's authoritative snapshot."""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Sequence
from decimal import ROUND_HALF_UP, Decimal
from time import monotonic
from typing import Any, NoReturn

from pydantic import ValidationError
from sqlglot import exp

from ..ingestion.errors import IngestionError
from ..ingestion.manifest import SemanticField
from ..ingestion.normalize import SourceCell, normalize_record
from .contracts import (
    FIELDS,
    TIV,
    AssetRecord,
    Contributor,
    CountValue,
    Evidence,
    ExistsValue,
    ListValue,
    QAError,
    QueryContext,
    SourceCellReference,
    SourceRecordReference,
    SumValue,
    ValidatedQuery,
    Verification,
)
from .execution import DEFAULT_DEADLINE, MAX_LIST_ROWS, _require_snapshot, _selection, _statement
from .validation import validate_sql

# Physical evidence is bounded independently of the 500-logical-asset contract.
# Every bound fails closed; none is a truncation/pagination boundary.
MAX_SOURCE_ROWS = 100_000
MAX_SOURCE_CELLS = 100_000
SOURCE_ASSET_CHUNK_SIZE = 128
DEFAULT_EVIDENCE_TIMEOUT_SECONDS = 10.0


class EvidenceBudget:
    """One cooperative deadline and cumulative cell allowance for an evidence reconciliation answer."""

    def __init__(self) -> None:
        self.expires_at = monotonic() + DEFAULT_EVIDENCE_TIMEOUT_SECONDS
        self.remaining_cells = MAX_SOURCE_CELLS

    def check(self) -> float:
        remaining = self.expires_at - monotonic()
        if remaining <= 0:
            raise QAError(
                "evidence_query_error",
                "evidence_deadline",
                "Cumulative evidence verification deadline exceeded; no partial answer returned",
            )
        return remaining

    def consume_cells(self, count: int) -> None:
        self.remaining_cells -= count
        if self.remaining_cells < 0:
            raise QAError(
                "evidence_query_error",
                "source_cell_limit",
                "Cumulative source-cell limit exceeded; no partial evidence returned",
            )
        self.check()


def contributor_query(plan: ValidatedQuery, context: QueryContext) -> str:
    """Revalidate the complete plan before reusing its AST-serialized predicate."""
    if plan != validate_sql(
        plan.proposed_sql, plan.operation, context, allows_missing=plan.allows_missing
    ):
        raise QAError("sql_validation_error", "plan_mismatch", "Invalid evidence query plan")
    fields = tuple(f for f in FIELDS if f in plan.referenced_fields)
    from sqlglot import parse_one

    tree = parse_one(_selection(plan, [exp.column(f) for f in fields]), read="sqlite")
    assert isinstance(tree, exp.Select)
    return tree.order_by(exp.column("asset_id")).sql(dialect="sqlite")


def _evidence_read(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
    *,
    budget: EvidenceBudget,
    max_rows: int = MAX_SOURCE_ROWS,
    provenance: bool = False,
    metadata: bool = False,
) -> list[tuple[Any, ...]]:
    """Keep SQL execution's authorization/cleanup and stricter per-statement deadline intact."""
    try:
        rows = _statement(
            connection,
            sql,
            parameters,
            provenance=provenance,
            metadata=metadata,
            max_rows=max_rows,
            deadline=min(DEFAULT_DEADLINE, budget.check()),
        )
        budget.check()
        return rows
    except QAError as exc:
        budget.check()  # Translate an exhausted cumulative budget consistently.
        raise QAError(
            "evidence_query_error",
            exc.failure.code,
            "Trusted evidence read failed or exceeded its resource bound; no partial evidence returned",
        ) from exc


def _invalid(message: str) -> NoReturn:
    raise QAError("source_reference_invalid", "source_identity", message)


def _incomplete(message: str) -> NoReturn:
    raise QAError("evidence_incomplete", "missing_source", message)


def _mismatch(message: str) -> NoReturn:
    raise QAError("evidence_mismatch", "reconciliation", message)


def _json(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError("Non-finite source JSON")

    return json.loads(text, parse_float=Decimal, parse_constant=reject_constant)


def _typed(value: Any, dtype: str | None) -> Any:
    # ingestion persists exact numeric XML Decimals as JSON decimal strings. Restore their
    # type before reusing ingestion normalization; never interpret numeric cells as text.
    if dtype == "n" and value is not None:
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            _invalid("Numeric source value has an inconsistent retained type")
        numeric = Decimal(value)
        if not numeric.is_finite():
            _invalid("Numeric source value must be finite")
        return numeric
    if dtype in ("s", "str", "inlineStr") and value is not None and not isinstance(value, str):
        _invalid("Text source value has an inconsistent retained type")
    if dtype == "b" and value is not None and not isinstance(value, bool):
        _invalid("Boolean source value has an inconsistent retained type")
    return value


def _source_scope(
    connection: sqlite3.Connection,
    context: QueryContext,
    budget: EvidenceBudget,
) -> tuple[dict[str, list[int]], dict, dict]:
    """Check the entire physical-to-logical mapping, including negative query scope.

    Report totals alone cannot reveal a removed duplicate link or a reassigned row.
    Cross-check the complete mapping and every retained EPIC cell in this snapshot.
    """
    manifest = context.manifest
    identity = (manifest.workbook_sha256, manifest.worksheet)
    tables = _evidence_read(
        connection,
        "SELECT name, type FROM sqlite_master WHERE name IN ('source_columns', 'source_rows', 'source_cells', 'asset_sources')",
        max_rows=4,
        budget=budget,
        provenance=True,
    )
    if set(tables) != {
        (name, "table")
        for name in ("source_columns", "source_rows", "source_cells", "asset_sources")
    }:
        _incomplete("Required ingestion source tables are unavailable")
    metadata = _evidence_read(
        connection,
        "SELECT report_json, assumptions_json FROM dataset WHERE singleton = 1",
        max_rows=1,
        budget=budget,
        provenance=True,
    )
    report, assumptions = (_json(v) for v in metadata[0])
    if not isinstance(report, dict) or not isinstance(assumptions, dict):
        _invalid("ingestion report and assumptions must be structured objects")
    excluded = report.get("excluded_rows")
    if not isinstance(excluded, dict) or any(
        type(v) is not int or v < 0 for v in excluded.values()
    ):
        _invalid("ingestion source exclusion metadata is inconsistent")
    columns = _evidence_read(
        connection,
        "SELECT workbook_sha256, worksheet, column_index, excel_letter, original_label FROM source_columns",
        budget=budget,
        provenance=True,
    )
    if set(columns) != {(*identity, c.index, c.letter, c.label) for c in manifest.columns}:
        _invalid("Source columns do not match the validated ingestion manifest")
    source_rows = _evidence_read(
        connection,
        "SELECT workbook_sha256, worksheet, row_number FROM source_rows",
        budget=budget,
        provenance=True,
    )
    sources = _evidence_read(
        connection,
        "SELECT asset_id, workbook_sha256, worksheet, row_number FROM asset_sources ORDER BY asset_id, row_number",
        budget=budget,
        provenance=True,
    )
    logical = _evidence_read(
        connection,
        "SELECT " + ", ".join(FIELDS) + " FROM assets",
        max_rows=MAX_SOURCE_ROWS,
        budget=budget,
    )
    identifiers = {row[0] for row in logical}
    physical = {row for row in source_rows}
    if (
        report["workbook"] != manifest.workbook_filename
        or report["worksheet"] != manifest.worksheet
        or report["header_row"] != manifest.header_row
        or report["physical_records"] != len(source_rows)
        or report["logical_assets"] != len(logical)
        or len(physical) != len(source_rows)
    ):
        _invalid("Source population or dataset identity disagrees with the ingestion report")
    if len(sources) != len(source_rows) or {tuple(row[1:]) for row in sources} != physical:
        _incomplete("Every retained physical row must have exactly one logical source mapping")
    mapping: dict[str, list[int]] = defaultdict(list)
    for asset_id, sha, sheet, row in sources:
        budget.check()
        if (
            (sha, sheet) != identity
            or asset_id not in identifiers
            or not manifest.data_start_row <= row <= manifest.data_end_row
        ):
            _invalid("Source row identity or worksheet boundary is inconsistent")
        mapping[asset_id].append(row)
    if set(mapping) != identifiers or any(len(rows) != len(set(rows)) for rows in mapping.values()):
        _incomplete("Logical assets must retain distinct complete physical mappings")
    if report["verified_duplicate_groups"] != sum(len(rows) > 1 for rows in mapping.values()):
        _incomplete("Duplicate source mappings disagree with ingestion consolidation metadata")
    id_column = manifest.concepts[SemanticField.ASSET_ID].column
    assert id_column is not None
    originals = _evidence_read(
        connection,
        "SELECT workbook_sha256, worksheet, row_number, coordinate, original_type, original_value_json, formula, cached_type, cached_value_json FROM source_cells WHERE column_index = ?",
        (id_column,),
        budget=budget,
        provenance=True,
        max_rows=budget.remaining_cells,
    )
    budget.consume_cells(len(originals))
    if len(originals) != len(source_rows) or {tuple(r[:3]) for r in originals} != physical:
        _incomplete("Every physical row requires its original asset identity cell")
    owners = {row: asset_id for asset_id, _, _, row in sources}
    letter = manifest.columns[id_column - 1].letter  # required ingestion concept
    for sha, sheet, row, coordinate, dtype, original, formula, cached_type, cached in originals:
        budget.check()
        value = _json(cached if dtype == "f" else original)
        if (
            coordinate != f"{letter}{row}"
            or not isinstance(value, str)
            or value.strip() != owners[row]
            or (dtype == "f" and (formula != _json(original) or cached_type not in ("s", "str")))
            or (dtype != "f" and dtype not in ("s", "str", "inlineStr"))
            or (
                dtype != "f"
                and (formula is not None or cached_type is not None or _json(cached) is not None)
            )
        ):
            _invalid("Physical EPIC identity does not match its logical source mapping")
    completeness = _evidence_read(
        connection,
        "SELECT semantic_field, availability, physical_non_null, physical_total, logical_non_null, logical_total FROM field_completeness",
        metadata=True,
        max_rows=6,
        budget=budget,
    )
    if any(
        r[3] != len(source_rows)
        or r[2] != sum(len(mapping[a[0]]) for a in logical if a[FIELDS.index(r[0])] is not None)
        or report["field_completeness"][r[0]]
        != dict(
            zip(
                (
                    "availability",
                    "physical_non_null",
                    "physical_total",
                    "logical_non_null",
                    "logical_total",
                ),
                r[1:],
                strict=True,
            )
        )
        for r in completeness
    ):
        _incomplete("Physical completeness totals disagree with retained source rows")
    if any(
        not isinstance(assumptions.get(k), str) or not assumptions[k].strip()
        for k in ("currency", "formula_cache", "duplicates", "rounding")
    ):
        _invalid("Required ingestion interpretation assumptions are unavailable")
    budget.check()
    return dict(mapping), report, assumptions


def _resolve_chunks(
    connection: sqlite3.Connection,
    context: QueryContext,
    records: Sequence[AssetRecord],
    fields: tuple[str, ...],
    mapping: dict[str, list[int]],
    budget: EvidenceBudget,
) -> Iterator[Contributor]:
    """Bound parameters and materialized cells; never issue a query per asset."""
    manifest = context.manifest
    columns = tuple(manifest.concepts[SemanticField(f)].column for f in fields)
    if any(c is None for c in columns):
        _incomplete("Required semantic source mapping is unavailable")
    chunk_size = min(
        SOURCE_ASSET_CHUNK_SIZE,
        connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - len(columns),
    )
    if chunk_size < 1:
        raise QAError(
            "evidence_query_error",
            "variable_limit",
            "SQLite variable limit cannot accommodate source verification",
        )
    for start in range(0, len(records), chunk_size):
        budget.check()
        chunk = records[start : start + chunk_size]
        identifiers = {record.asset_id for record in chunk}
        if len(identifiers) != len(chunk):
            _mismatch("Source verification requires distinct logical assets")
        # Only app-authored placeholders/identifiers are interpolated. Values bind.
        cells = _evidence_read(
            connection,
            "SELECT s.asset_id, c.workbook_sha256, c.worksheet, c.row_number, c.column_index, c.coordinate, c.original_type, c.original_value_json, c.formula, c.cached_type, c.cached_value_json FROM source_cells AS c JOIN asset_sources AS s USING (workbook_sha256, worksheet, row_number) WHERE s.asset_id IN ("
            + ",".join("?" for _ in chunk)
            + ") AND c.column_index IN ("
            + ",".join("?" for _ in columns)
            + ") ORDER BY s.asset_id ASC, c.row_number ASC, c.column_index ASC",
            (*[r.asset_id for r in chunk], *columns),
            budget=budget,
            provenance=True,
            max_rows=budget.remaining_cells,
        )
        budget.consume_cells(len(cells))
        grouped: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        for asset_id, *cell in cells:
            budget.check()
            if asset_id not in identifiers:
                _invalid("Unexpected logical asset in source-cell retrieval")
            grouped[asset_id].append(tuple(cell))
        for record in chunk:
            budget.check()
            yield _resolve(
                context, record, fields, mapping[record.asset_id], grouped[record.asset_id], budget
            )


def _resolve(
    context: QueryContext,
    record: AssetRecord,
    fields: tuple[str, ...],
    rows: list[int],
    cells: list[tuple[Any, ...]],
    budget: EvidenceBudget,
) -> Contributor:
    """Validate/normalize one logical asset using only already-retrieved cells."""
    manifest = context.manifest
    columns = tuple(manifest.concepts[SemanticField(f)].column for f in fields)
    if len(cells) != len(rows) * len(fields):
        _incomplete("Required original source cells are missing")
    grouped: dict[int, dict[int, tuple[SourceCell, Any, Any]]] = defaultdict(dict)
    for (
        sha,
        sheet,
        row,
        col,
        coordinate,
        dtype,
        original_json,
        formula,
        cached_type,
        cached_json,
    ) in cells:
        budget.check()
        column = manifest.columns[col - 1]
        original, cached = _json(original_json), _json(cached_json)
        if (
            (sha, sheet) != (manifest.workbook_sha256, manifest.worksheet)
            or row not in rows
            or coordinate != f"{column.letter}{row}"
            or col in grouped[row]
        ):
            _invalid("Original source cell coordinate or identity is inconsistent")
        if (
            dtype not in ("n", "s", "str", "inlineStr", "b", "e", "d", "f")
            or (
                dtype == "f"
                and (
                    formula != original
                    or not isinstance(formula, str)
                    or not formula.startswith("=")
                    or cached_type not in ("n", "s", "str", "b", "e", "d")
                )
            )
            or (
                dtype != "f"
                and (formula is not None or cached_type is not None or cached is not None)
            )
        ):
            _invalid("Original formula, cell type or cache metadata is inconsistent")
        grouped[row][col] = (
            SourceCell(
                col,
                coordinate,
                dtype,
                _typed(original, dtype),
                cached_type,
                _typed(cached, cached_type),
            ),
            original,
            cached,
        )
    references: list[SourceRecordReference] = []
    for row in rows:
        budget.check()
        stored = grouped[row]
        if set(stored) != set(columns):
            _incomplete("Required source column cannot be resolved for a physical row")
        # Reuse ingestion's authority, rather than a second normalization implementation.
        # Unrequested columns are placeholders and never compared or emitted.
        inputs = tuple(
            stored[c.index][0]
            if c.index in stored
            else SourceCell(c.index, f"{c.letter}{row}", "n", None, None, None)
            for c in manifest.columns
        )
        diagnostics = []
        normalized = normalize_record(row, inputs, manifest, diagnostics)
        budget.check()
        if any(getattr(normalized, f) != getattr(record, f) for f in fields):
            _mismatch("Retained source values do not reproduce the logical contributor")
        refs = []
        for field in fields:
            col = manifest.concepts[SemanticField(field)].column
            assert col is not None
            cell, original, cached = stored[col]
            column = manifest.columns[col - 1]
            refs.append(
                SourceCellReference(
                    semantic_field=field,
                    column_index=col,
                    column_letter=column.letter,
                    cell_coordinate=cell.coordinate,
                    source_header=column.label,
                    original_type=cell.data_type,
                    original_value=original,
                    formula=original if cell.data_type == "f" else None,
                    cached_type=cell.cached_type,
                    cached_value=cached,
                    normalized_value=getattr(normalized, field),
                    calculation_value="cached_value" if cell.data_type == "f" else "original_value",
                    normalization=tuple(d.code for d in diagnostics if d.column == col),
                )
            )
        references.append(SourceRecordReference(row=row, cells=tuple(refs)))
    contributor = Contributor(
        asset_id=record.asset_id, values=record, source_records=tuple(references)
    )
    budget.check()
    return contributor


def reconcile(
    plan: ValidatedQuery,
    value: ExistsValue | CountValue | SumValue | ListValue,
    count: int,
    contributors: tuple[Contributor, ...],
    report: dict,
) -> Verification:
    ids = tuple(c.asset_id for c in contributors)
    if len(ids) != len(set(ids)) or ids != tuple(sorted(ids)) or len(ids) != count:
        _mismatch("Evidence population differs from the executed distinct logical population")
    amount = None
    if plan.operation == "exists":
        if not isinstance(value, ExistsValue) or value.value != bool(count):
            _mismatch("Existence result disagrees with count and complete evidence")
        method = "boolean_and_count"
    elif plan.operation == "count":
        if not isinstance(value, CountValue) or value.value != len(ids):
            _mismatch("SQL count differs from distinct evidence assets")
        method = "distinct_asset_count"
    elif plan.operation == "sum":
        amounts = [c.values.total_insurable_value_micro_usd for c in contributors]
        if any(type(v) is not int for v in amounts):
            _incomplete("Contributing valuations must be exact integer micro-USD")
        amount = sum(v for v in amounts if v is not None) if ids else None
        if (
            not isinstance(value, SumValue)
            or value.value != amount
            or value.outcome != ("value" if ids else "no_matching_records")
        ):
            _mismatch("SQL SUM differs from the independent exact Python integer sum")
        method = "exact_integer_sum"
    else:
        if (
            not isinstance(value, ListValue)
            or tuple(r.asset_id for r in value.records) != ids
            or len(set(r.asset_id for r in value.records)) != len(ids)
        ):
            _mismatch("Listing identities differ from complete ordered evidence")
        for result, contributor in zip(value.records, contributors, strict=True):
            if any(
                getattr(result, f) != getattr(contributor.values, f) for f in plan.result_fields
            ):
                _mismatch("Selected listing values disagree with evidence")
        method = "complete_listing"
    return Verification(
        method=method,
        dataset_logical_assets=report["logical_assets"],
        dataset_physical_records=report["physical_records"],
        predicate_fields=tuple(sorted(plan.predicate_fields)),
        completeness_checked=tuple(
            sorted(plan.ordinary_predicate_fields | ({TIV} if plan.operation == "sum" else set()))
        ),
        executed_count=count,
        result_micro_usd=value.value if isinstance(value, SumValue) else None,
        evidence_sum_micro_usd=amount,
    )


def build_evidence(
    connection: sqlite3.Connection,
    plan: ValidatedQuery,
    context: QueryContext,
    value: ExistsValue | CountValue | SumValue | ListValue,
    count: int,
    *,
    budget: EvidenceBudget | None = None,
) -> tuple[Evidence, Verification, tuple[str, ...]]:
    budget = budget or EvidenceBudget()
    budget.check()
    _require_snapshot(connection)
    sql = contributor_query(plan, context)
    try:
        fields = tuple(f for f in FIELDS if f in plan.referenced_fields)
        population = _evidence_read(connection, sql, max_rows=MAX_LIST_ROWS + 1, budget=budget)
        if count > MAX_LIST_ROWS or len(population) > MAX_LIST_ROWS:
            raise QAError(
                "evidence_limit_exceeded",
                "contributor_limit",
                f"Complete evidence requires {count} logical assets; limit is {MAX_LIST_ROWS}",
                evidence_limit=MAX_LIST_ROWS,
                required_contributor_count=count,
            )
        records = tuple(
            AssetRecord.model_validate(dict(zip(fields, row, strict=True))) for row in population
        )
        if len(records) != count or len({r.asset_id for r in records}) != count:
            _mismatch("Contributor retrieval differs from executed population")
        mapping, report, assumptions = _source_scope(connection, context, budget)
        scope_formula = False
        scope_precision = False
        complete = tuple(_resolve_chunks(connection, context, records, fields, mapping, budget))
        # An absent match still depends on the retained predicate values of the
        # excluded population. Check those cells as well, without inventing negative
        # citations or adding noncontributors to the returned evidence.
        if plan.predicate_fields:
            scope_fields = tuple(f for f in FIELDS if f in plan.predicate_fields or f == "asset_id")
            scope = _evidence_read(
                connection,
                "SELECT " + ", ".join(scope_fields) + " FROM assets ORDER BY asset_id ASC",
                max_rows=MAX_SOURCE_ROWS,
                budget=budget,
            )
            matched = {r.asset_id for r in records}
            excluded_records = []
            for row in scope:
                budget.check()
                record = AssetRecord.model_validate(dict(zip(scope_fields, row, strict=True)))
                if record.asset_id not in matched:
                    excluded_records.append(record)
            for checked in _resolve_chunks(
                connection, context, excluded_records, scope_fields, mapping, budget
            ):
                budget.check()
                scope_formula |= any(
                    c.formula is not None for r in checked.source_records for c in r.cells
                )
                scope_precision |= any(
                    "precision_reduced" in c.normalization
                    for r in checked.source_records
                    for c in r.cells
                )
        budget.check()
        verification = reconcile(plan, value, count, complete, report)
        budget.check()
        evidence = Evidence(
            contributor_sql=sql,
            required_fields=fields,
            matched_asset_count=count,
            contributing_asset_count=len(complete),
            source_record_count=sum(len(c.source_records) for c in complete),
            contributors=complete,
        )
        qualifications = [
            "Distinct logical assets; ingestion duplicate policy: "
            + assumptions["duplicates"]
            + "."
        ]
        excluded = {k: v for k, v in report["excluded_rows"].items() if v}
        if excluded:
            qualifications.append(
                "ingestion excluded non-asset rows: "
                + ", ".join(f"{k}={v}" for k, v in sorted(excluded.items()))
                + "."
            )
        if TIV in fields:
            qualifications.extend(
                (
                    "Currency assumption: " + assumptions["currency"] + ".",
                    "Integer micro-USD (six USD decimal places); ingestion normalization: "
                    + assumptions["rounding"]
                    + ". Display rounds HALF_UP to cents only after aggregation.",
                )
            )
        source_cells = [cell for c in complete for r in c.source_records for cell in r.cells]
        if scope_formula or any(c.formula is not None for c in source_cells):
            qualifications.append(
                "Excel formulas are not recalculated: " + assumptions["formula_cache"] + "."
            )
        if scope_precision or any("precision_reduced" in c.normalization for c in source_cells):
            qualifications.append(
                "Relevant TIV cells required ingestion precision reduction to six USD decimal places; original values are retained."
            )
        budget.check()
        return evidence, verification, tuple(qualifications)
    except QAError as exc:
        budget.check()
        if exc.failure.category in ("query_execution_error", "resource_limit_exceeded"):
            raise QAError(
                "evidence_query_error", exc.failure.code, "Complete evidence query failed"
            ) from exc
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        ArithmeticError,
        RecursionError,
        ValidationError,
        IngestionError,
    ) as exc:
        budget.check()
        raise QAError(
            "source_reference_invalid",
            "corrupt_source",
            "Retained source metadata or values are inconsistent with ingestion",
        ) from exc


def answer_summary(
    value: ExistsValue | CountValue | SumValue | ListValue, evidence: Evidence
) -> tuple[str, str | None]:
    display = None
    if isinstance(value, ExistsValue):
        answer = (
            "Matching assets exist."
            if value.value
            else "No matching assets exist in the complete relevant dataset."
        )
    elif isinstance(value, CountValue):
        answer = f"Count: {value.value} distinct logical assets."
    elif isinstance(value, SumValue):
        if value.value is None:
            answer = "No matching records; no monetary total is measured."
        else:
            dollars = (Decimal(value.value) / 1_000_000).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            display = f"${dollars:,.2f}"
            answer = f"Total insurable value: {display} (USD)."
    else:
        answer = f"Listing: {len(value.records)} distinct logical assets."
    return (
        f"{answer} Reconciled against {evidence.contributing_asset_count} logical assets and {evidence.source_record_count} original spreadsheet rows.",
        display,
    )
