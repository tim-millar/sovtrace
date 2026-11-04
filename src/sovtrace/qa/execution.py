"""Bounded read-only SQLite operations; transaction ownership stays with the caller."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from time import monotonic
from typing import Any

from pydantic import ValidationError
from sqlglot import exp

from ..ingestion.manifest import SchemaManifest, SemanticField
from .contracts import (
    FIELDS,
    TIV,
    AssetRecord,
    CountValue,
    DatasetIdentity,
    ExistsValue,
    ListValue,
    QAError,
    QueryContext,
    SumValue,
    ValidatedQuery,
)
from .validation import validate_sql

DEFAULT_DEADLINE = 2.0
MAX_LIST_ROWS = 500
SOURCE_READS = {
    "source_columns": frozenset(
        {"workbook_sha256", "worksheet", "column_index", "excel_letter", "original_label"}
    ),
    "source_rows": frozenset({"workbook_sha256", "worksheet", "row_number"}),
    "asset_sources": frozenset({"asset_id", "workbook_sha256", "worksheet", "row_number"}),
    "source_cells": frozenset(
        {
            "workbook_sha256",
            "worksheet",
            "row_number",
            "column_index",
            "coordinate",
            "original_type",
            "original_value_json",
            "formula",
            "cached_type",
            "cached_value_json",
        }
    ),
}


def open_readonly(path: Path) -> sqlite3.Connection:
    """Caller owns the returned connection; no implicit transaction is started."""
    try:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            isolation_level=None,
            timeout=0,
            cached_statements=0,
        )
        try:
            with closing(connection.execute("PRAGMA query_only = ON")):
                pass
            with closing(connection.execute("PRAGMA trusted_schema = OFF")):
                pass
            connection.enable_load_extension(False)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 32_000)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2_000_000)
            connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
        except BaseException:
            connection.close()
            raise
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise QAError(
            "configuration_error",
            "database_open",
            "Cannot open existing read-only ingestion database",
        ) from exc


@contextmanager
def read_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    """Standalone owner. evidence reconciliation may instead BEGIN/ROLLBACK on open_readonly itself."""
    with closing(open_readonly(path)) as connection:
        try:
            try:
                with closing(connection.execute("BEGIN")):
                    pass
            except sqlite3.Error as exc:
                raise QAError(
                    "query_execution_error", "snapshot_error", "SQLite read snapshot failed"
                ) from exc
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()


def _statement(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[object] = (),
    *,
    deadline: float = DEFAULT_DEADLINE,
    max_rows: int = 1,
    metadata: bool = False,
    provenance: bool = False,
) -> list[tuple[Any, ...]]:
    """Private primitive. Authorizer is independent of the SQL validator.

    Callback slots are reserved for this component while running; callbacks and
    cursors are cleared on every exit. Never commits, rolls back or closes caller state.
    """
    started = monotonic()
    expired = False

    def over_budget() -> bool:
        nonlocal expired
        expired = expired or monotonic() - started >= deadline
        return expired

    def authorize(
        action: int, first: str | None, second: str | None, database: str | None, source: str | None
    ) -> int:
        if source is not None:  # ingestion assets is a table; views/triggers are not trusted.
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and (
            database == "main" or (database is None and first == "assets" and second == "")
        ):
            if first == "assets" and (second in FIELDS or second == ""):
                return sqlite3.SQLITE_OK
            if metadata and first in ("dataset", "field_completeness", "sqlite_master"):
                return sqlite3.SQLITE_OK
            if provenance and (
                (first in SOURCE_READS and (second in SOURCE_READS[first] or second == ""))
                or (
                    first == "dataset"
                    and second in ("singleton", "report_json", "assumptions_json")
                )
                or (first == "sqlite_master" and second in ("name", "type"))
            ):
                return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and second in ("count", "sum", "like"):
            return sqlite3.SQLITE_OK
        if (
            metadata
            and action == sqlite3.SQLITE_PRAGMA
            and first in ("table_info", "table_list")
            and second == "assets"
        ):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    cursor: sqlite3.Cursor | None = None
    try:
        # SQLite's COUNT(*) table-only READ callback can omit the database name.
        # Forbid attachments/temp shadowing independently of AST validation.
        with closing(connection.execute("PRAGMA database_list")) as databases:
            if [row[1] for row in databases] != ["main"]:
                raise QAError(
                    "query_execution_error",
                    "attached_database",
                    "Execution requires only the main database; attached/temp databases are prohibited",
                )
        connection.set_authorizer(authorize)
        connection.set_progress_handler(lambda: int(over_budget()), 100)
        if over_budget():
            raise QAError(
                "resource_limit_exceeded", "deadline", "SQLite execution deadline exceeded"
            )
        cursor = connection.execute(sql, parameters)
        rows: list[tuple[Any, ...]] = []
        while True:
            if over_budget():
                raise QAError(
                    "resource_limit_exceeded", "deadline", "SQLite execution deadline exceeded"
                )
            row = cursor.fetchone()
            if over_budget():
                raise QAError(
                    "resource_limit_exceeded", "deadline", "SQLite execution deadline exceeded"
                )
            if row is None:
                return rows
            rows.append(tuple(row))
            if len(rows) > max_rows:
                raise QAError(
                    "resource_limit_exceeded",
                    "result_size",
                    f"Result exceeds {max_rows} rows; no partial result returned",
                )
    except sqlite3.Error as exc:
        if (
            expired
            or over_budget()
            or getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT
        ):
            raise QAError(
                "resource_limit_exceeded", "deadline", "SQLite execution deadline exceeded"
            ) from exc
        if getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_TOOBIG, sqlite3.SQLITE_NOMEM):
            raise QAError(
                "resource_limit_exceeded", "sqlite_resource", "SQLite resource limit exceeded"
            ) from exc
        raise QAError(
            "query_execution_error",
            "sqlite_error",
            "SQLite rejected or could not execute the query",
        ) from exc
    finally:
        if cursor is not None:
            cursor.close()
        connection.set_progress_handler(None, 0)
        connection.set_authorizer(None)


def _require_snapshot(connection: sqlite3.Connection) -> None:
    try:
        if not connection.in_transaction:
            raise QAError(
                "configuration_error",
                "transaction_required",
                "Execution requires a caller-controlled read transaction",
            )
        with closing(connection.execute("PRAGMA query_only")) as cursor:
            if cursor.fetchone() != (1,):
                raise QAError(
                    "configuration_error",
                    "read_only_required",
                    "Use open_readonly for a read-only connection",
                )
    except sqlite3.Error as exc:
        raise QAError(
            "query_execution_error", "connection_error", "SQLite connection is not usable"
        ) from exc


def load_context(connection: sqlite3.Connection) -> QueryContext:
    """Load and cross-check ingestion metadata without opening the original workbook."""
    _require_snapshot(connection)
    try:
        relations = _statement(
            connection,
            "SELECT name, type FROM sqlite_master WHERE name IN ('assets', 'dataset', 'field_completeness')",
            metadata=True,
            max_rows=3,
        )
        if set(relations) != {(n, "table") for n in ("assets", "dataset", "field_completeness")}:
            raise ValueError("Required ingestion tables missing")
        columns = _statement(connection, "PRAGMA table_info(assets)", metadata=True, max_rows=6)
        if [(r[1], r[2], r[5]) for r in columns] != [
            (f, "INTEGER" if f == TIV else "TEXT", 1 if f == "asset_id" else 0) for f in FIELDS
        ]:
            raise ValueError("Unexpected semantic schema")
        table = _statement(connection, "PRAGMA table_list(assets)", metadata=True)[0]
        if table[0] != "main" or table[2] != "table" or table[5] != 1:
            raise ValueError("Semantic assets must be an ingestion STRICT table")
        rows = _statement(
            connection,
            "SELECT workbook_filename, workbook_sha256, worksheet, manifest_json, currency, monetary_scale, report_json FROM dataset WHERE singleton = 1",
            metadata=True,
        )
        if len(rows) != 1:
            raise ValueError("Missing dataset")
        filename, sha256, worksheet, manifest_json, currency, scale, report_json = rows[0]
        manifest = SchemaManifest.model_validate_json(manifest_json)
        if (filename, sha256, worksheet, currency, scale) != (
            manifest.workbook_filename,
            manifest.workbook_sha256,
            manifest.worksheet,
            "USD",
            1_000_000,
        ):
            raise ValueError("Inconsistent identity or monetary convention")
        completeness = _statement(
            connection,
            "SELECT semantic_field, availability, physical_non_null, physical_total, logical_non_null, logical_total FROM field_completeness",
            metadata=True,
            max_rows=6,
        )
        if len(completeness) != len(FIELDS) or {r[0] for r in completeness} != set(FIELDS):
            raise ValueError("Invalid completeness metadata")
        total = _statement(connection, "SELECT COUNT(*) FROM assets")[0][0]
        available: set[str] = set()
        incomplete: set[str] = set()
        for field, availability, pnn, pt, lnn, lt in completeness:
            if (
                availability != manifest.concepts[SemanticField(field)].status
                or lt != total
                or not 0 <= lnn <= lt
                or not 0 <= pnn <= pt
            ):
                raise ValueError("Inconsistent completeness metadata")
            actual = _statement(
                connection, f'SELECT COUNT(*) FROM assets WHERE "{field}" IS NOT NULL'
            )[0][0]
            if actual != lnn:
                raise ValueError("Stale completeness metadata")
            if availability == "mapped":
                available.add(field)
            if lnn != lt:
                incomplete.add(field)
        report = json.loads(report_json)
        if report["logical_assets"] != total or report["sha256"] != sha256:
            raise ValueError("Inconsistent ingestion report")
        diagnostics = report["diagnostic_summary"]
        if not isinstance(diagnostics, dict):
            raise ValueError("Invalid diagnostics summary")
        return QueryContext(
            identity=DatasetIdentity(
                dataset_id=f"{sha256}:{worksheet}",
                workbook_sha256=sha256,
                workbook_filename=filename,
                worksheet=worksheet,
            ),
            manifest=manifest,
            available_fields=frozenset(available),
            incomplete_fields=frozenset(incomplete),
            qualifications=(),
        )
    except QAError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, ValidationError) as exc:
        raise QAError(
            "configuration_error",
            "invalid_dataset",
            "Database metadata does not satisfy the ingestion contract",
        ) from exc


def _selection(plan: ValidatedQuery, projections: Sequence[exp.Expr]) -> str:
    """Only app-authored preflight projections; filter comes from the validated plan."""
    tree = exp.select(*projections).from_("assets")
    if plan.filter is not None:
        from sqlglot import parse_one

        tree = tree.where(parse_one(plan.filter, read="sqlite"))
    return tree.sql(dialect="sqlite")


def execute_query(
    connection: sqlite3.Connection,
    plan: ValidatedQuery,
    *,
    deadline: float = DEFAULT_DEADLINE,
    complete_evidence: bool = False,
) -> tuple[ExistsValue | CountValue | SumValue | ListValue, int, QueryContext]:
    """Execute in an existing read snapshot. Caller retains connection/transaction.

    Revalidate at this public boundary so a forged/mutated plan is never authority.
    Metadata, completeness preflights and result use the caller's same snapshot.
    """
    context = load_context(connection)
    checked = validate_sql(
        plan.proposed_sql, plan.operation, context, allows_missing=plan.allows_missing
    )
    if plan != checked:
        raise QAError(
            "sql_validation_error",
            "plan_mismatch",
            "Validated plan does not match this dataset and SQL",
        )
    missing = plan.ordinary_predicate_fields & context.incomplete_fields
    if missing:
        raise QAError(
            "data_incomplete",
            "predicate_incomplete",
            "Missing or invalid values affect filter membership: " + ", ".join(sorted(missing)),
        )
    counts = [exp.Count(this=exp.Star())]
    if plan.operation == "sum":
        counts.append(exp.Count(this=exp.column(TIV)))
    population = _statement(connection, _selection(plan, counts), deadline=deadline)[0]
    count = population[0]
    if plan.operation == "sum" and population[1] != count:
        raise QAError(
            "data_incomplete",
            "valuation_incomplete",
            "Matching assets have missing or invalid valuations",
        )
    if complete_evidence and count > MAX_LIST_ROWS:
        raise QAError(
            "evidence_limit_exceeded",
            "contributor_limit",
            f"Complete evidence requires {count} logical assets; limit is {MAX_LIST_ROWS}",
            evidence_limit=MAX_LIST_ROWS,
            required_contributor_count=count,
        )
    if plan.operation == "list" and count > MAX_LIST_ROWS:
        raise QAError(
            "resource_limit_exceeded",
            "result_size",
            "Asset listing exceeds 500 records; no partial result returned",
        )
    rows = _statement(
        connection,
        plan.executed_sql,
        deadline=deadline,
        max_rows=MAX_LIST_ROWS if plan.operation == "list" else 1,
    )
    if plan.operation == "list":
        records = tuple(
            AssetRecord.model_validate(dict(zip(plan.result_fields, row, strict=True)))
            for row in rows
        )
        if len(records) != count or len({r.asset_id for r in records}) != count:
            raise QAError(
                "query_execution_error",
                "population_mismatch",
                "Listing does not match the logical population",
            )
        value = ListValue(records=records, selected_fields=plan.result_fields)
    else:
        scalar = rows[0][0]
        if plan.operation in ("count", "exists"):
            if type(scalar) is not int or scalar != count:
                raise QAError(
                    "query_execution_error", "invalid_count", "Invalid logical asset count"
                )
            value = (
                ExistsValue(value=bool(scalar))
                if plan.operation == "exists"
                else CountValue(value=scalar)
            )
        else:
            if (count and type(scalar) is not int) or (not count and scalar is not None):
                raise QAError(
                    "query_execution_error",
                    "invalid_sum",
                    "Expected exact integer micro-USD or an empty sum",
                )
            value = SumValue(value=scalar, outcome="value" if count else "no_matching_records")
    return value, count, context
