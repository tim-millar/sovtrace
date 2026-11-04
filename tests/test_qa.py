"""Deterministic SQL execution contract tests using only synthetic ingestion imports and fake proposals."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlglot import exp, parse_one
from workbook_factory import HEADERS, asset, write_workbook

from sovtrace.ingestion import ingest_workbook
from sovtrace.ingestion.manifest import ConceptMapping, SchemaManifest, SemanticField
from sovtrace.qa import (
    Failure,
    Generation,
    Proposal,
    Success,
    ask,
    ask_in_transaction,
    execute_query,
    execution,
    load_context,
    open_readonly,
    read_snapshot,
    validate_sql,
)
from sovtrace.qa.contracts import Operation, QAError, QueryContext
from sovtrace.qa.execution import _statement
from sovtrace.qa.provider import schema_context
from sovtrace.qa.validation import MAX_IN_VALUES

BOUNDARY = 10_000_000_000_000


class FakeGenerator:
    def __init__(self, sql: str, operation: Operation = "count"):
        self.proposal = Proposal(status="query", operation=operation, sql=sql, reason=None)
        self.calls: list[tuple[str, str]] = []

    def generate(self, question: str, schema: str) -> Generation:
        self.calls.append((question, schema))
        return Generation(self.proposal)


def database(tmp_path: Path, rows: list[list[Any]], *, headers: list[str] | None = None) -> Path:
    source = write_workbook(tmp_path / "synthetic.xlsx", rows=rows, headers=headers)
    output = tmp_path / "synthetic.sqlite"
    ingest_workbook(source, output)
    source.unlink()  # SQL execution must work without the workbook.
    return output


@pytest.fixture
def db(tmp_path: Path) -> Path:
    rows = [
        asset("below", "9999999.999999"),
        asset("equal", "10000000"),
        asset("above", "10000000.000001"),
        asset("zero", "0", "CA"),
        asset("negative", "-0.000001", "TX"),
    ]
    rows[0][5] = "Wood Frame - Podium"
    rows[2][5] = "Wood Frame - Podium"
    return database(tmp_path, rows + [rows[2].copy()])


@pytest.fixture
def context(db: Path) -> QueryContext:
    with read_snapshot(db) as connection:
        return load_context(connection)


@pytest.mark.parametrize(
    ("operation", "sql"),
    [
        ("exists", "SELECT COUNT(*) FROM assets WHERE state = 'AZ'"),
        ("count", "SELECT COUNT(*) FROM assets WHERE state != 'CA'"),
        ("count", "SELECT COUNT(*) FROM assets WHERE state <> 'CA'"),
        (
            "count",
            "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd >= 10000000000000",
        ),
        ("count", "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd <= -1"),
        (
            "sum",
            "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'AZ' AND total_insurable_value_micro_usd > 10000000000000",
        ),
        (
            "list",
            "SELECT asset_id, state, total_insurable_value_micro_usd FROM assets WHERE (state IN ('AZ', 'CA') OR city LIKE 'Test%') AND construction_type = 'Wood Frame - Podium'",
        ),
        (
            "list",
            "SELECT asset_id FROM assets WHERE city IS NULL OR property_name IS NOT NULL ORDER BY asset_id ASC",
        ),
    ],
)
def test_accepted_sql(context: QueryContext, operation: Operation, sql: str) -> None:
    plan = validate_sql(sql, operation, context, allows_missing=True)
    assert plan.operation == operation
    assert plan.proposed_sql == sql
    assert plan.dataset_id == context.identity.dataset_id
    tree = parse_one(plan.executed_sql, read="sqlite")
    assert (
        tree.args["where"].this.sql(dialect="sqlite") if tree.args.get("where") else None
    ) == plan.filter
    if operation == "list":
        assert plan.executed_sql.endswith("ORDER BY asset_id ASC")
    assert (
        validate_sql(plan.executed_sql, operation, context, allows_missing=True).executed_sql
        == plan.executed_sql
    )


BAD_SQL = [
    "",
    "SELECT",
    "SELECT FROM assets",
    "SELECT COUNT(*) FROM assets; SELECT COUNT(*) FROM assets",
    "SELECT COUNT(*) FROM assets;;",
    "DELETE FROM assets",
    "UPDATE assets SET state = 'AZ'",
    "INSERT INTO assets(asset_id) VALUES ('bad')",
    "DROP TABLE assets",
    "CREATE TABLE bad(x)",
    "PRAGMA query_only=OFF",
    "ATTACH DATABASE ':memory:' AS other",
    "VACUUM",
    "BEGIN",
    "SELECT COUNT(*) FROM source_cells",
    "SELECT COUNT(*) FROM asset_sources",
    "SELECT COUNT(*) FROM dataset",
    "SELECT COUNT(*) FROM sqlite_master",
    "SELECT secret FROM assets",
    "SELECT * FROM assets",
    "SELECT COUNT(asset_id) FROM assets",
    "SELECT COUNT(DISTINCT asset_id) FROM assets",
    "SELECT SUM(state) FROM assets",
    "SELECT AVG(total_insurable_value_micro_usd) FROM assets",
    "SELECT MAX(state) FROM assets",
    "SELECT COUNT(*) + 1 FROM assets",
    "SELECT asset_id || state FROM assets",
    "SELECT asset_id, length(city) FROM assets",
    "SELECT DISTINCT asset_id FROM assets",
    "SELECT asset_id FROM assets GROUP BY asset_id",
    "SELECT COUNT(*) FROM assets HAVING COUNT(*) > 1",
    "SELECT COUNT(*) OVER () FROM assets",
    "SELECT COUNT(*) FROM assets a JOIN assets b ON a.asset_id = b.asset_id",
    "SELECT COUNT(*) FROM assets, assets a",
    "SELECT COUNT(*) FROM (SELECT * FROM assets)",
    "WITH a AS (SELECT * FROM assets) SELECT COUNT(*) FROM a",
    "SELECT asset_id FROM assets UNION SELECT asset_id FROM assets",
    "SELECT asset_id FROM assets INTERSECT SELECT asset_id FROM assets",
    "SELECT asset_id FROM assets EXCEPT SELECT asset_id FROM assets",
    "SELECT asset_id FROM assets LIMIT 1",
    "SELECT asset_id FROM assets OFFSET 1",
    "SELECT COUNT(*) FROM main.assets",
    "SELECT COUNT(*) FROM other.assets",
    "SELECT COUNT(*) FROM assets AS a",
    "SELECT assets.asset_id FROM assets",
    "SELECT COUNT(*) FROM assets WHERE 1 = 1",
    "SELECT COUNT(*) FROM assets WHERE state = city",
    "SELECT COUNT(*) FROM assets WHERE state = (SELECT state FROM assets)",
    "SELECT COUNT(*) FROM assets WHERE state IN (SELECT state FROM assets)",
    "SELECT COUNT(*) FROM assets WHERE state IN ()",
    "SELECT COUNT(*) FROM assets WHERE state = 1",
    "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd > 1.5",
    "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd > 1e13",
    "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd > '10000000000000'",
    "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd > 9223372036854775808",
    "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd < -9223372036854775809",
    "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd LIKE '1%'",
    "SELECT COUNT(*) FROM assets WHERE state BETWEEN 'AZ' AND 'CA'",
    "SELECT COUNT(*) FROM assets WHERE NOT state = 'AZ'",
    "SELECT COUNT(*) FROM assets WHERE state NOT IN ('AZ')",
    "SELECT COUNT(*) FROM assets WHERE state IS TRUE",
    "SELECT COUNT(*) FROM assets WHERE state IS 'AZ'",
    "SELECT COUNT(*) FROM assets WHERE state = 'AZ' COLLATE NOCASE",
    "SELECT COUNT(*) FROM assets WHERE city LIKE 'X%' ESCAPE 'x'",
    "SELECT COUNT(*) FROM assets WHERE lower(state) = 'az'",
    "SELECT COUNT(*) FROM assets WHERE state = ?",
    "SELECT COUNT(*) FROM assets WHERE state = :state",
    "SELECT COUNT(*) FROM assets WHERE state = 'AZ' -- bypass",
    "SELECT /* hidden */ COUNT(*) FROM assets",
    "SELECT COUNT(*) FROM assets; -- empty",
    "SELECT COUNT(*) AS n FROM assets",
    "SELECT asset_id AS a FROM assets",
    "SELECT asset_id FROM assets ORDER BY state",
    "SELECT asset_id FROM assets ORDER BY asset_id DESC",
    "SELECT asset_id FROM assets ORDER BY asset_id, city",
    "SELECT asset_id FROM assets WHERE state GLOB 'A*'",
    "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd + 1 > 10",
    "SELECT COUNT(*) FROM assets WHERE state IN ('AZ', 1)",
    "SELECT COUNT(*) FROM assets WHERE state = NULL",
    "SELECT COUNT(*) FROM assets WHERE EXISTS (SELECT 1)",
    "SELECT asset_id FROM assets TABLESAMPLE(1)",
    "SELECT COUNT(*) FROM assets INDEXED BY evil",
    "SELECT COUNT(*) FROM assets FOR UPDATE",
]


@pytest.mark.parametrize("sql", BAD_SQL)
def test_rejected_sql(context: QueryContext, sql: str) -> None:
    for operation in ("count", "list", "sum", "exists"):
        with pytest.raises(QAError) as failure:
            validate_sql(sql, operation, context)  # type: ignore[arg-type]
        assert failure.value.failure.category in ("sql_validation_error", "unavailable_field")


@pytest.mark.parametrize(
    ("sql", "operation"),
    [
        ("SELECT SUM(total_insurable_value_micro_usd) FROM assets", "count"),
        ("SELECT COUNT(*) FROM assets", "sum"),
        ("SELECT asset_id FROM assets", "exists"),
        ("SELECT COUNT(*) FROM assets", "list"),
        ("SELECT state FROM assets", "list"),
        ("SELECT asset_id, asset_id FROM assets", "list"),
    ],
)
def test_operation_mismatch(context: QueryContext, sql: str, operation: Operation) -> None:
    with pytest.raises(QAError):
        validate_sql(sql, operation, context)


def test_input_limits_and_comment_literals(context: QueryContext) -> None:
    for sql in [
        " " * 16001,
        "SELECT COUNT(*) FROM assets WHERE state IN ("
        + ",".join("'AZ'" for _ in range(MAX_IN_VALUES + 1))
        + ")",
        "SELECT COUNT(*) FROM assets WHERE city = '" + "x" * 1001 + "'",
        "SELECT COUNT(*) FROM assets WHERE " + "(" * 40 + "state = 'AZ'" + ")" * 40,
        "SELECT COUNT(*) FROM assets WHERE " + " OR ".join("state = 'AZ'" for _ in range(90)),
        "SELECT COUNT(*) FROM assets WHERE state = 'AZ'\x00",
    ]:
        with pytest.raises(QAError):
            validate_sql(sql, "count", context)
    assert validate_sql(
        "SELECT COUNT(*) FROM assets WHERE city = '-- not a comment /*'", "count", context
    )


@pytest.mark.parametrize(
    ("operation", "sql", "question", "expected", "count"),
    [
        (
            "exists",
            "SELECT COUNT(*) FROM assets WHERE state = 'AZ'",
            "Are there Arizona assets?",
            True,
            3,
        ),
        (
            "exists",
            "SELECT COUNT(*) FROM assets WHERE state = 'NY'",
            "Are there New York assets?",
            False,
            0,
        ),
        (
            "count",
            "SELECT COUNT(*) FROM assets WHERE state = 'AZ'",
            "How many Arizona assets?",
            3,
            3,
        ),
        (
            "count",
            "SELECT COUNT(*) FROM assets WHERE state = 'NY'",
            "How many New York assets?",
            0,
            0,
        ),
        (
            "count",
            "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd > 10000000000000",
            "How many assets are worth over $10m?",
            1,
            1,
        ),
        (
            "count",
            "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd >= 10000000000000",
            "How many assets are worth at least $10 million?",
            2,
            2,
        ),
        (
            "count",
            "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd < 10000000000000",
            "How many assets are worth under USD 10,000,000?",
            3,
            3,
        ),
        (
            "sum",
            "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'AZ'",
            "Arizona total?",
            3 * BOUNDARY,
            3,
        ),
        (
            "sum",
            "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'CA'",
            "California total?",
            0,
            1,
        ),
        (
            "sum",
            "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'NY'",
            "New York total?",
            None,
            0,
        ),
        (
            "sum",
            "SELECT SUM(total_insurable_value_micro_usd) FROM assets",
            "Overall TIV?",
            3 * BOUNDARY - 1,
            5,
        ),
    ],
)
def test_service_scalar(
    db: Path, operation: Operation, sql: str, question: str, expected: object, count: int
) -> None:
    generator = FakeGenerator(sql, operation)
    result = ask(db, question, generator)
    assert isinstance(result, Success), result
    assert result.result.model_dump()["value"] == expected
    assert type(result.result.model_dump()["value"]) is type(expected)
    assert result.matching_count == count
    assert result.operation == operation
    assert result.plan.proposed_sql == result.proposed_sql == sql
    assert result.plan.executed_sql == result.executed_sql
    assert len(generator.calls) == 1
    assert result.dataset.workbook_sha256 in result.dataset.dataset_id
    assert "plan" not in result.model_dump()
    if operation == "sum":
        assert result.result.model_dump()["outcome"] == (
            "value" if count else "no_matching_records"
        )
        assert result.result.unit == "micro_usd"


@pytest.mark.parametrize("state", ["AZ", "NY"])
def test_lists(db: Path, state: str) -> None:
    result = ask(
        db,
        "List assets",
        FakeGenerator(
            f"SELECT asset_id, total_insurable_value_micro_usd FROM assets WHERE state = '{state}'",
            "list",
        ),
    )
    assert isinstance(result, Success)
    records = result.result.model_dump()["records"]
    assert [r["asset_id"] for r in records] == (
        ["above", "below", "equal"] if state == "AZ" else []
    )
    assert result.matching_count == len(records)
    assert result.executed_sql.endswith("ORDER BY asset_id ASC")


@pytest.mark.parametrize(
    ("question", "literal", "operator", "code"),
    [
        ("How many assets over $10m?", "10000000", ">", "threshold_scale"),
        ("How many assets over $10 million?", "10000000000000", ">=", "threshold_operator"),
        ("How many assets over USD 10,000,000?", "10000000000001", ">", "threshold_scale"),
    ],
)
def test_money_misinterpretation_rejected(
    db: Path, question: str, literal: str, operator: str, code: str
) -> None:
    result = ask(
        db,
        question,
        FakeGenerator(
            f"SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd {operator} {literal}"
        ),
    )
    assert isinstance(result, Failure)
    assert result.category == "sql_validation_error" and result.code == code


@pytest.mark.parametrize(("field", "bad_value"), [(3, None), (3, "Atlantis"), (5, None)])
def test_predicate_completeness(tmp_path: Path, field: int, bad_value: object) -> None:
    row = asset("missing")
    row[field] = bad_value
    db = database(tmp_path, [asset(), row])
    column = "state" if field == 3 else "construction_type"
    result = ask(
        db,
        "How many assets match?",
        FakeGenerator(f"SELECT COUNT(*) FROM assets WHERE {column} = 'NY'"),
    )
    assert isinstance(result, Failure) and result.category == "data_incomplete"


@pytest.mark.parametrize("bad_value", [None, "invalid", "#VALUE!"])
def test_missing_valuation(tmp_path: Path, bad_value: object) -> None:
    db = database(tmp_path, [asset(), asset("bad", bad_value, "TX")])
    complete = ask(
        db,
        "Arizona total?",
        FakeGenerator(
            "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'AZ'", "sum"
        ),
    )
    assert isinstance(complete, Success)
    for sql in [
        "SELECT SUM(total_insurable_value_micro_usd) FROM assets",
        "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'TX'",
    ]:
        result = ask(db, "Total TIV?", FakeGenerator(sql, "sum"))
        assert isinstance(result, Failure) and result.category == "data_incomplete"
    result = ask(
        db,
        "How many assets exceed $10m?",
        FakeGenerator(
            "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd > 10000000000000"
        ),
    )
    assert isinstance(result, Failure) and result.category == "data_incomplete"


def test_explicit_nulls_and_mixed_predicates(tmp_path: Path) -> None:
    db = database(tmp_path, [asset(), asset("missing", state=None)])
    sql = "SELECT COUNT(*) FROM assets WHERE state IS NULL"
    result = ask(db, "How many assets have missing recorded state?", FakeGenerator(sql))
    assert isinstance(result, Success) and result.matching_count == 1
    assert any("negative business facts" in q for q in result.qualifications)
    result = ask(db, "How many assets?", FakeGenerator(sql))
    assert isinstance(result, Failure) and result.category == "sql_validation_error"
    result = ask(
        db,
        "How many have missing state or are in Arizona?",
        FakeGenerator(sql + " OR state = 'AZ'"),
    )
    assert isinstance(result, Failure) and result.category == "data_incomplete"
    result = ask(
        db,
        "How many have a non-missing state?",
        FakeGenerator("SELECT COUNT(*) FROM assets WHERE state IS NOT NULL"),
    )
    assert isinstance(result, Success) and result.matching_count == 1


def test_absent_optional_and_projected_null(tmp_path: Path) -> None:
    labels = HEADERS.copy()
    labels[5] = "Unsupported category"
    db = database(tmp_path, [asset()], headers=labels)
    for sql, operation in [
        ("SELECT COUNT(*) FROM assets WHERE construction_type = 'X'", "count"),
        ("SELECT asset_id, construction_type FROM assets", "list"),
    ]:
        result = ask(db, "Question", FakeGenerator(sql, operation))  # type: ignore[arg-type]
        assert isinstance(result, Failure) and result.category == "unavailable_field"
    with read_snapshot(db) as c:
        assert "construction_type" not in schema_context(load_context(c))


def test_null_projection_qualified(tmp_path: Path) -> None:
    row = asset()
    row[1] = None
    db = database(tmp_path, [row])
    result = ask(
        db, "List properties", FakeGenerator("SELECT asset_id, property_name FROM assets", "list")
    )
    assert isinstance(result, Success)
    assert any("Selected fields" in q for q in result.qualifications)


def test_schema_data_minimization(context: QueryContext) -> None:
    schema = schema_context(context)
    assert context.identity.workbook_sha256 not in schema
    assert context.identity.worksheet not in schema
    assert "Synthetic property" not in schema
    assert "source_cells" not in schema
    assert "INTEGER micro-USD" in schema


def test_rejected_proposal_never_executes(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Rejected SQL reached result execution")

    monkeypatch.setattr("sovtrace.qa.service.execute_query", forbidden)
    result = ask(db, "Delete everything", FakeGenerator("DELETE FROM assets"))
    assert isinstance(result, Failure) and result.category == "sql_validation_error"


def test_provider_failures_and_no_retries(db: Path) -> None:
    class Failing:
        calls = 0

        def generate(self, question: str, schema: str) -> Generation:
            self.calls += 1
            raise QAError("provider_error", "transport", "Transport failed")

    generator = Failing()
    result = ask(db, "Question", generator)
    assert isinstance(result, Failure) and result.category == "provider_error"
    assert generator.calls == 1
    fake = FakeGenerator("SELECT COUNT(*) FROM assets")
    fake.proposal = Proposal(
        status="unsupported", operation=None, sql=None, reason="Unavailable business field"
    )
    result = ask(db, "What is the roof age?", fake)
    assert isinstance(result, Failure) and result.category == "unsupported_question"


@pytest.mark.parametrize(
    "values",
    [
        {"status": "query", "operation": None, "sql": "SELECT 1", "reason": None},
        {"status": "query", "operation": "count", "sql": " ", "reason": None},
        {"status": "query", "operation": "count", "sql": "SELECT 1", "reason": "why"},
        {"status": "unsupported", "operation": None, "sql": "SELECT 1", "reason": "why"},
        {"status": "unsupported", "operation": "count", "sql": None, "reason": "why"},
        {"status": "unsupported", "operation": None, "sql": None, "reason": " "},
    ],
)
def test_proposal_consistency(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Proposal.model_validate(values)


def test_invalid_fake_output(db: Path) -> None:
    fake = FakeGenerator("SELECT COUNT(*) FROM assets")
    fake.proposal = Proposal.model_construct(status="query", operation="sum", sql=None, reason=None)
    result = ask(db, "Question", fake)
    assert isinstance(result, Failure) and result.category == "invalid_model_response"


def test_dataset_errors(db: Path, tmp_path: Path) -> None:
    assert isinstance(ask(tmp_path / "missing.sqlite", "Question"), Failure)
    assert not (tmp_path / "missing.sqlite").exists()
    with sqlite3.connect(db) as c:
        c.execute("UPDATE dataset SET monetary_scale = 1000000, report_json = '{}' ")
    result = ask(db, "Question", FakeGenerator("SELECT COUNT(*) FROM assets"))
    assert isinstance(result, Failure) and result.category == "configuration_error"


def test_stale_completeness_is_rejected(db: Path) -> None:
    with sqlite3.connect(db) as c:
        c.execute("UPDATE assets SET state = NULL WHERE asset_id = 'above'")
    result = ask(db, "Question", FakeGenerator("SELECT COUNT(*) FROM assets"))
    assert isinstance(result, Failure) and result.code == "invalid_dataset"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM assets",
        "UPDATE assets SET state = 'TX'",
        "CREATE TABLE evil(x)",
        "PRAGMA query_only=OFF",
        "ATTACH ':memory:' AS evil",
        "SELECT * FROM source_cells",
        "SELECT * FROM dataset",
        "SELECT * FROM sqlite_master",
        "SELECT load_extension('evil')",
    ],
)
def test_independent_authorizer(db: Path, sql: str) -> None:
    before = db.read_bytes()
    # A deliberately writable connection demonstrates that authorization is an independent layer.
    with sqlite3.connect(db) as c:
        with pytest.raises(QAError) as failure:
            _statement(c, sql)
        assert failure.value.failure.category == "query_execution_error"
        assert c.execute("SELECT COUNT(*) FROM assets").fetchone() == (5,)
    assert db.read_bytes() == before


def test_readonly_and_cleanup(db: Path) -> None:
    with read_snapshot(db) as c:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM assets")
        with pytest.raises(QAError):
            _statement(c, "SELECT secret FROM assets")
        # Authorizer/progress slots have been released; owner reads still work.
        assert c.execute("SELECT COUNT(*) FROM dataset").fetchone() == (1,)
        assert c.in_transaction
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


def test_resource_limits_and_transaction_cleanup(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with read_snapshot(db) as c:
        context = load_context(c)
        plan = validate_sql("SELECT COUNT(*) FROM assets", "count", context)
        with pytest.raises(QAError) as failure:
            execute_query(c, plan, deadline=0)
        assert failure.value.failure.category == "resource_limit_exceeded"
        assert c.in_transaction
        ticks = iter(range(10000))
        monkeypatch.setattr("sovtrace.qa.execution.monotonic", lambda: next(ticks) / 10)
        with pytest.raises(QAError) as failure:
            _statement(
                c,
                "SELECT COUNT(*) FROM assets a, assets b, assets c, assets d, assets e, assets f, assets g",
                deadline=0.25,
            )
        assert failure.value.failure.category == "resource_limit_exceeded"
        assert c.execute("SELECT COUNT(*) FROM assets").fetchone() == (5,)


@pytest.mark.parametrize(
    "error",
    [
        None,
        sqlite3.OperationalError("caller operational error"),
        sqlite3.IntegrityError("caller integrity error"),
        RuntimeError("caller runtime error"),
        QAError("evidence_query_error", "caller", "Caller QA failure"),
    ],
)
def test_snapshot_preserves_caller_exceptions_and_cleans_up(
    db: Path, error: Exception | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = sqlite3.connect
    connections = []

    class TrackingConnection(sqlite3.Connection):
        rolled_back = False
        closed = False

        def rollback(self) -> None:
            assert self.in_transaction
            self.rolled_back = True
            super().rollback()

        def close(self) -> None:
            assert not self.in_transaction
            self.closed = True
            super().close()

    def tracked(*args: Any, **kwargs: Any):
        connection = connect(*args, factory=TrackingConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(execution.sqlite3, "connect", tracked)

    def caller() -> None:
        with read_snapshot(db) as connection:
            assert connection.in_transaction
            assert connection.execute("SELECT COUNT(*) FROM assets").fetchone() == (5,)
            if error is not None:
                raise error

    if error is None:
        caller()
    else:
        with pytest.raises(type(error)) as raised:
            caller()
        assert raised.value is error
    assert len(connections) == 1
    assert connections[0].rolled_back and connections[0].closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize("transaction_started", [False, True])
def test_snapshot_setup_failure_is_translated_and_owned_resources_close(
    db: Path, transaction_started: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = sqlite3.connect
    setup_error = sqlite3.OperationalError("BEGIN failed")
    connections = []

    class FailingConnection(sqlite3.Connection):
        rolled_back = False
        closed = False

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql == "BEGIN":
                if transaction_started:
                    super().execute(sql, parameters).close()
                raise setup_error
            return super().execute(sql, parameters)

        def rollback(self) -> None:
            self.rolled_back = True
            super().rollback()

        def close(self) -> None:
            assert not self.in_transaction
            self.closed = True
            super().close()

    def tracked(*args: Any, **kwargs: Any):
        connection = connect(*args, factory=FailingConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(execution.sqlite3, "connect", tracked)
    with pytest.raises(QAError) as raised:
        with read_snapshot(db):
            pytest.fail("Transaction setup must fail before yielding")
    assert raised.value.failure.category == "query_execution_error"
    assert raised.value.failure.code == "snapshot_error"
    assert raised.value.__cause__ is setup_error
    assert connections[0].closed
    assert connections[0].rolled_back is transaction_started


@pytest.mark.parametrize("size", [500, 501])
def test_listing_bound(tmp_path: Path, size: int) -> None:
    db = database(tmp_path, [asset(str(i), "1") for i in range(size)])
    result = ask(db, "List assets", FakeGenerator("SELECT asset_id FROM assets", "list"))
    if size == 500:
        assert isinstance(result, Success) and result.matching_count == 500
    else:
        assert isinstance(result, Failure) and result.category == "evidence_limit_exceeded"
        assert result.evidence_limit == 500 and result.required_contributor_count == 501
    with read_snapshot(db) as c:
        with pytest.raises(QAError) as failure:
            _statement(c, "SELECT asset_id FROM assets", max_rows=499)
        assert failure.value.failure.code == "result_size"
        assert c.in_transaction


def test_forged_plan_rejected(db: Path, context: QueryContext) -> None:
    plan = validate_sql("SELECT COUNT(*) FROM assets", "count", context)
    with read_snapshot(db) as c:
        for forged in [
            replace(plan, executed_sql="DELETE FROM assets"),
            replace(plan, dataset_id="other"),
            replace(plan, filter="state = 'TX'"),
        ]:
            with pytest.raises(QAError) as failure:
                execute_query(c, forged)
            assert failure.value.failure.code == "plan_mismatch"
        assert c.in_transaction


def test_transaction_requirements(db: Path, context: QueryContext) -> None:
    plan = validate_sql("SELECT COUNT(*) FROM assets", "count", context)
    c = open_readonly(db)
    try:
        with pytest.raises(QAError) as failure:
            execute_query(c, plan)
        assert failure.value.failure.code == "transaction_required"
    finally:
        c.close()
    with sqlite3.connect(db) as c:
        c.execute("BEGIN")
        with pytest.raises(QAError) as failure:
            execute_query(c, plan)
        assert failure.value.failure.code == "read_only_required"


def test_contract_filter_population_and_same_snapshot(
    db: Path,
    context: QueryContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Caller-owned execution must not generate a model proposal")

    monkeypatch.setattr("sovtrace.qa.provider.OpenAIGenerator.generate", forbidden)
    sql = "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE (state = 'AZ' OR state = 'CA') AND total_insurable_value_micro_usd > 10000000000000"
    generation = Generation(
        Proposal(status="query", operation="sum", sql=sql, reason=None), model="synthetic-model"
    )
    writer = sqlite3.connect(db, timeout=0.1)
    writer.execute("PRAGMA journal_mode=WAL").close()
    try:
        with read_snapshot(db) as reader:
            result = ask_in_transaction(
                reader,
                "Total assets over $10m in Arizona or California?",
                generation,
                generation_context=context,
            )
            assert isinstance(result, Success)
            assert result.matching_count == 1
            assert result.model == "synthetic-model"
            # evidence reconciliation-like query construction only in the contract test; no source evidence implementation.
            selection = (
                exp.select("asset_id", "total_insurable_value_micro_usd")
                .from_("assets")
                .where(parse_one(result.plan.filter or "", read="sqlite"))
                .order_by("asset_id")
            )
            writer.execute(
                "UPDATE assets SET total_insurable_value_micro_usd = 10000000000002 WHERE asset_id = 'above'"
            )
            writer.commit()
            inputs = reader.execute(selection.sql(dialect="sqlite")).fetchall()
            assert inputs == [("above", BOUNDARY + 1)]
            assert sum(row[1] for row in inputs) == result.result.model_dump()["value"]
            assert len(inputs) == result.matching_count
            assert reader.in_transaction
            assert reader.execute(
                "SELECT COUNT(*) FROM asset_sources WHERE asset_id = 'above'"
            ).fetchone() == (2,)
        with read_snapshot(db) as reader:
            assert reader.execute(
                "SELECT total_insurable_value_micro_usd FROM assets WHERE asset_id = 'above'"
            ).fetchone() == (BOUNDARY + 2,)
    finally:
        writer.close()


def test_cli_success_and_expected_failure(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sovtrace.cli import main
    from sovtrace.qa import service

    generator = FakeGenerator("SELECT SUM(total_insurable_value_micro_usd) FROM assets", "sum")
    monkeypatch.setattr(
        "sovtrace.qa.ask", lambda database, question: service.ask(database, question, generator)
    )
    main(["ask", "--format", "json", "--db", str(db), "Total TIV?"])
    output = capsys.readouterr().out
    assert '"unit": "micro_usd"' in output and '"executed_sql"' in output
    assert '"value": "29999999999999"' in output
    generator.proposal = Proposal(
        status="query", operation="count", sql="DELETE FROM assets", reason=None
    )
    with pytest.raises(SystemExit) as failure:
        main(["ask", "--format", "json", "--db", str(db), "Question"])
    assert failure.value.code == 1
    assert '"category": "sql_validation_error"' in capsys.readouterr().out


def test_cli_missing_credentials(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sovtrace.cli import main

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit) as failure:
        main(["ask", "--format", "json", "--db", str(db), "How many assets?"])
    assert failure.value.code == 1
    assert '"code": "missing_credentials"' in capsys.readouterr().out


def test_unselected_columns_are_not_serialized_as_null(db: Path) -> None:
    result = ask(db, "List assets", FakeGenerator("SELECT asset_id FROM assets", "list"))
    assert isinstance(result, Success)
    assert all(set(row) == {"asset_id"} for row in result.result.model_dump()["records"])


def test_fractional_micro_threshold_never_rounds_to_supported_value(db: Path) -> None:
    result = ask(
        db,
        "How many assets exceed $0.00000100000000000000000000000000001?",
        FakeGenerator("SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd > 1"),
    )
    assert isinstance(result, Failure) and result.code == "threshold_precision"


def test_closed_connection_is_explicit_failure(db: Path, context: QueryContext) -> None:
    connection = open_readonly(db)
    connection.close()
    result = ask_in_transaction(
        connection,
        "Question",
        Generation(FakeGenerator("SELECT COUNT(*) FROM assets").proposal),
        generation_context=context,
    )
    assert isinstance(result, Failure) and result.category == "query_execution_error"


def test_aggregate_overflow_is_execution_failure(db: Path) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE assets SET total_insurable_value_micro_usd = 9000000000000000000"
        )
    result = ask(
        db,
        "Total TIV?",
        FakeGenerator("SELECT SUM(total_insurable_value_micro_usd) FROM assets", "sum"),
    )
    assert isinstance(result, Failure) and result.category == "query_execution_error"


@pytest.mark.parametrize("temporary", [False, True])
def test_count_authorization_cannot_read_attached_or_shadow_table(
    db: Path, temporary: bool
) -> None:
    with sqlite3.connect(db) as connection:
        if temporary:
            connection.execute("CREATE TEMP TABLE assets(asset_id TEXT)")
            connection.execute("INSERT INTO temp.assets VALUES ('shadow')")
            sql = "SELECT COUNT(*) FROM assets"
        else:
            connection.execute("ATTACH ':memory:' AS other")
            connection.execute("CREATE TABLE other.assets(asset_id TEXT)")
            connection.execute("INSERT INTO other.assets VALUES ('private')")
            sql = "SELECT COUNT(*) FROM other.assets"
        with pytest.raises(QAError) as failure:
            _statement(connection, sql)
        assert failure.value.failure.code == "attached_database"
        assert connection.execute("SELECT COUNT(*) FROM main.assets").fetchone() == (5,)


def test_sqlite_value_resource_limit(db: Path) -> None:
    with read_snapshot(db) as connection:
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 100)
        with pytest.raises(QAError) as failure:
            _statement(connection, "SELECT ?", ("x" * 101,))
        assert failure.value.failure.category == "resource_limit_exceeded"
        assert connection.in_transaction


def test_semantic_primary_key_is_required(db: Path) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute("ALTER TABLE assets RENAME TO old_assets")
        connection.execute(
            "CREATE TABLE assets (asset_id TEXT, property_name TEXT, city TEXT, state TEXT, total_insurable_value_micro_usd INTEGER, construction_type TEXT) STRICT"
        )
        connection.execute("INSERT INTO assets SELECT * FROM old_assets")
    result = ask(db, "How many assets?", FakeGenerator("SELECT COUNT(*) FROM assets"))
    assert isinstance(result, Failure) and result.code == "invalid_dataset"


@pytest.mark.parametrize("operation", ["count", "sum", "list"])
def test_combined_geographic_numeric_categorical_filters(db: Path, operation: Operation) -> None:
    projection = {
        "count": "COUNT(*)",
        "sum": "SUM(total_insurable_value_micro_usd)",
        "list": "asset_id, total_insurable_value_micro_usd",
    }[operation]
    sql = f"SELECT {projection} FROM assets WHERE state = 'AZ' AND construction_type = 'Wood Frame - Podium' AND total_insurable_value_micro_usd > 10000000000000"
    result = ask(db, "Arizona Podium assets worth over $10m", FakeGenerator(sql, operation))
    assert isinstance(result, Success) and result.matching_count == 1
    if operation == "list":
        assert result.result.model_dump()["records"] == (
            {"asset_id": "above", "total_insurable_value_micro_usd": BOUNDARY + 1},
        )
    else:
        assert result.result.model_dump()["value"] == (1 if operation == "count" else BOUNDARY + 1)


def test_statement_cursors_are_closed_on_success_and_failure(db: Path) -> None:
    cursors: list[sqlite3.Cursor] = []

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            cursors.append(cursor)
            return cursor

    with sqlite3.connect(db, factory=TrackingConnection) as connection:
        assert _statement(connection, "SELECT COUNT(*) FROM assets") == [(5,)]
        with pytest.raises(QAError):
            _statement(connection, "SELECT asset_id FROM assets", max_rows=1)
        for cursor in cursors:
            with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
                cursor.fetchone()


@pytest.mark.parametrize("amount", ["-$0.000001", "$-0.000001"])
def test_signed_currency_thresholds(db: Path, amount: str) -> None:
    result = ask(
        db,
        f"How many assets are worth over {amount}?",
        FakeGenerator("SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd > -1"),
    )
    assert isinstance(result, Success) and result.matching_count == 4


def test_generation_releases_discovery_connection_and_allows_writer_commit(
    db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sovtrace.qa import service

    with closing(sqlite3.connect(db, timeout=0.1)) as writer:
        assert writer.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    snapshots: list[sqlite3.Connection] = []
    original_snapshot = service.read_snapshot

    @contextmanager
    def tracked_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
        with original_snapshot(path) as connection:
            snapshots.append(connection)
            yield connection

    monkeypatch.setattr(service, "read_snapshot", tracked_snapshot)

    class WritingGenerator(FakeGenerator):
        def generate(self, question: str, schema: str) -> Generation:
            assert len(snapshots) == 1
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                snapshots[0].execute("SELECT COUNT(*) FROM assets")
            # Synchronous commit is the synchronization point. Any retained read
            # snapshot in DELETE mode would fail this commit within 100 ms.
            with closing(sqlite3.connect(db, timeout=0.1)) as writer, writer:
                writer.execute("UPDATE assets SET state = 'AZ' WHERE asset_id = 'zero'")
                writer.execute(
                    "UPDATE source_cells SET original_value_json = '\"AZ\"' WHERE column_index = 4 AND row_number IN (SELECT row_number FROM asset_sources WHERE asset_id = 'zero')"
                )
            return super().generate(question, schema)

    generator = WritingGenerator("SELECT COUNT(*) FROM assets WHERE state = 'AZ'")
    result = ask(db, "How many Arizona assets?", generator)
    assert isinstance(result, Success) and result.matching_count == 4
    assert result.result.model_dump()["value"] == 4
    assert len(generator.calls) == 1 and len(snapshots) == 2
    for connection in snapshots:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("question", [" ", "x" * 8001])
def test_question_validated_before_discovery_or_generation(
    db: Path,
    question: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid question reached context discovery")

    monkeypatch.setattr("sovtrace.qa.service.read_snapshot", forbidden)
    generator = FakeGenerator("SELECT COUNT(*) FROM assets")
    result = ask(db, question, generator)
    assert isinstance(result, Failure) and result.code == "question_size"
    assert not generator.calls


def test_database_replacement_during_generation_fails_closed(
    db: Path,
    context: QueryContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement_dir = tmp_path / "replacement"
    replacement_dir.mkdir()
    replacement_db = database(replacement_dir, [asset("other", "1")])

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Stale SQL reached validation or execution")

    monkeypatch.setattr("sovtrace.qa.service.validate_sql", forbidden)
    monkeypatch.setattr("sovtrace.qa.service.execute_query", forbidden)

    class ReplacingGenerator(FakeGenerator):
        def generate(self, question: str, schema: str) -> Generation:
            replacement_db.replace(db)
            return super().generate(question, schema)

    generator = ReplacingGenerator("SELECT COUNT(*) FROM assets")
    result = ask(db, "How many assets?", generator)
    assert isinstance(result, Failure) and result.code == "dataset_changed"
    assert len(generator.calls) == 1
    with read_snapshot(db) as connection:
        assert load_context(connection).identity != context.identity


@pytest.mark.parametrize("change", ["availability", "mapping"])
def test_semantic_contract_change_during_generation_fails_closed(
    db: Path,
    context: QueryContext,
    change: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Incompatible semantic context reached SQL validation or execution")

    monkeypatch.setattr("sovtrace.qa.service.validate_sql", forbidden)
    monkeypatch.setattr("sovtrace.qa.service.execute_query", forbidden)

    class ChangingGenerator(FakeGenerator):
        def generate(self, question: str, schema: str) -> Generation:
            with closing(sqlite3.connect(db, timeout=0.1)) as writer, writer:
                manifest_json, report_json = writer.execute(
                    "SELECT manifest_json, report_json FROM dataset"
                ).fetchone()
                manifest = SchemaManifest.model_validate_json(manifest_json)
                concepts = manifest.concepts.copy()
                if change == "availability":
                    field = SemanticField.CONSTRUCTION
                    concepts[field] = ConceptMapping(
                        status="absent", normalized_type=concepts[field].normalized_type
                    )
                    writer.execute("UPDATE assets SET construction_type = NULL")
                    writer.execute(
                        "UPDATE field_completeness SET availability = 'absent', physical_non_null = 0, logical_non_null = 0 WHERE semantic_field = 'construction_type'"
                    )
                    report = json.loads(report_json)
                    report["field_completeness"][field].update(
                        availability="absent", physical_non_null=0, logical_non_null=0
                    )
                    report["mapped_fields"].remove(field)
                    writer.execute("UPDATE dataset SET report_json = ?", (json.dumps(report),))
                else:
                    first, second = SemanticField.PROPERTY_NAME, SemanticField.CITY
                    concepts[first], concepts[second] = concepts[second], concepts[first]
                updated = SchemaManifest.model_validate(
                    {**manifest.model_dump(), "concepts": concepts}
                )
                writer.execute("UPDATE dataset SET manifest_json = ?", (updated.model_dump_json(),))
            return super().generate(question, schema)

    generator = ChangingGenerator("SELECT COUNT(*) FROM assets")
    result = ask(db, "How many assets?", generator)
    assert isinstance(result, Failure) and result.code == "dataset_changed"
    assert len(generator.calls) == 1
    with read_snapshot(db) as connection:
        current = load_context(connection)
        assert current.identity == context.identity
        assert current.manifest.concepts != context.manifest.concepts


@pytest.mark.parametrize(
    "outcome", ["validation", "incomplete", "unsupported", "malformed", "threshold"]
)
def test_provider_free_transaction_failures_preserve_owner(
    tmp_path: Path,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = database(tmp_path, [asset(), asset("missing", None)])
    with read_snapshot(db) as discovery:
        context = load_context(discovery)
    question = "Total TIV?"
    if outcome == "unsupported":
        proposal = Proposal(
            status="unsupported", operation=None, sql=None, reason="Unsupported concept"
        )
        expected = "unsupported_question"
    elif outcome == "malformed":
        proposal = Proposal.model_construct(status="query", operation="sum", sql=None, reason=None)
        expected = "invalid_model_response"
    elif outcome == "threshold":
        question = "How many assets exceed $10m?"
        proposal = FakeGenerator(
            "SELECT COUNT(*) FROM assets WHERE total_insurable_value_micro_usd > 10"
        ).proposal
        expected = "sql_validation_error"
    else:
        sql = (
            "DELETE FROM assets"
            if outcome == "validation"
            else "SELECT SUM(total_insurable_value_micro_usd) FROM assets"
        )
        proposal = FakeGenerator(sql, "sum").proposal
        expected = "sql_validation_error" if outcome == "validation" else "data_incomplete"
    generation = Generation(proposal, model="synthetic-model")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Transaction execution contacted a model provider")

    monkeypatch.setattr("sovtrace.qa.provider.OpenAIGenerator.generate", forbidden)
    with read_snapshot(db) as reader:
        result = ask_in_transaction(reader, question, generation, generation_context=context)
        assert isinstance(result, Failure) and result.category == expected
        assert reader.in_transaction
        assert reader.execute("SELECT COUNT(*) FROM assets").fetchone() == (2,)


def test_authoritative_preflight_result_and_evidence_share_snapshot(
    db: Path,
    context: QueryContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sovtrace.qa import service

    generation = Generation(
        FakeGenerator(
            "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'AZ'", "sum"
        ).proposal
    )
    original_validate = service.validate_sql
    with closing(sqlite3.connect(db, timeout=0.1)) as writer:
        writer.execute("PRAGMA journal_mode=WAL").close()

        def validate_then_write(*args: Any, **kwargs: Any) -> Any:
            plan = original_validate(*args, **kwargs)
            with writer:
                writer.execute(
                    "UPDATE assets SET total_insurable_value_micro_usd = NULL WHERE asset_id = 'above'"
                )
                writer.execute(
                    "UPDATE field_completeness SET logical_non_null = logical_non_null - 1, physical_non_null = physical_non_null - 2 WHERE semantic_field = 'total_insurable_value_micro_usd'"
                )
            return plan

        monkeypatch.setattr(service, "validate_sql", validate_then_write)
        with read_snapshot(db) as reader:
            result = ask_in_transaction(
                reader, "Arizona TIV?", generation, generation_context=context
            )
            assert isinstance(result, Success) and result.matching_count == 3
            assert result.result.model_dump()["value"] == 3 * BOUNDARY
            inputs = reader.execute(
                "SELECT asset_id, total_insurable_value_micro_usd FROM assets WHERE state = 'AZ' ORDER BY asset_id"
            ).fetchall()
            assert len(inputs) == 3 and sum(row[1] for row in inputs) == 3 * BOUNDARY
            assert reader.in_transaction
        monkeypatch.setattr(service, "validate_sql", original_validate)
        with read_snapshot(db) as reader:
            result = ask_in_transaction(
                reader, "Arizona TIV?", generation, generation_context=context
            )
            assert isinstance(result, Failure) and result.category == "data_incomplete"
            assert reader.in_transaction
