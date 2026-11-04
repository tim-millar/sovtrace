"""evidence reconciliation service, source corruption, independent reconciliation and snapshot regressions."""

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from math import ceil
from pathlib import Path
from typing import Any

import pytest
from sqlglot import parse_one
from test_qa import FakeGenerator, database
from workbook_factory import HEADERS, asset, write_workbook

from sovtrace.ingestion import ingest_workbook
from sovtrace.qa import (
    Failure,
    Generation,
    Success,
    ask,
    ask_in_transaction,
    evidence,
    load_context,
    read_snapshot,
    service,
    validate_sql,
)
from sovtrace.qa.contracts import (
    AssetRecord,
    CountValue,
    ExistsValue,
    ListValue,
    Operation,
    QAError,
    SumValue,
)
from sovtrace.qa.execution import _statement


@pytest.fixture
def db(tmp_path: Path) -> Path:
    first = asset(" a ", "=G6+H6", " Arizona ")
    source = write_workbook(
        tmp_path / "original.xlsx",
        header=5,
        rows=[first, first, first, asset("b", "0", "CA"), asset("c", "0.0000005", "AZ")],
        caches={"E6": "1.0050004", "E7": "1.0050004", "E8": "1.0050004"},
    )
    path = tmp_path / "evidence.sqlite"
    ingest_workbook(source, path)
    source.unlink()
    return path


def run(db: Path, operation: Operation = "sum", where: str = "") -> Success | Failure:
    projection = {
        "sum": "SUM(total_insurable_value_micro_usd)",
        "exists": "COUNT(*)",
        "count": "COUNT(*)",
        "list": "asset_id, state, total_insurable_value_micro_usd",
    }[operation]
    generator = FakeGenerator(f"SELECT {projection} FROM assets" + where, operation)
    result = ask(db, "Assets with missing recorded data?", generator)
    assert len(generator.calls) == 1
    return result


@pytest.mark.parametrize("operation", ["exists", "count", "sum", "list"])
def test_complete_source_fidelity(db: Path, operation: Operation) -> None:
    result = run(db, operation, " WHERE state = 'AZ'")
    assert isinstance(result, Success)
    assert result.status == "verified" and result.verification.reconciled
    assert result.evidence.contributing_asset_count == 2
    assert result.evidence.source_record_count == 4
    assert [c.asset_id for c in result.evidence.contributors] == ["a", "c"]
    first = result.evidence.contributors[0]
    assert [r.row for r in first.source_records] == [6, 7, 8]
    assert result.dataset.workbook_filename == "original.xlsx"
    assert result.dataset.worksheet == "Statement of Values"
    for row in first.source_records:
        cells = {c.semantic_field: c for c in row.cells}
        assert cells["asset_id"].cell_coordinate == f"A{row.row}"
        assert cells["asset_id"].original_value == " a "
        assert cells["asset_id"].normalized_value == "a"
        assert cells["state"].cell_coordinate == f"D{row.row}"
        assert cells["state"].original_value == " Arizona "
        assert cells["state"].normalized_value == "AZ"
        if operation in ("sum", "list"):
            tiv = cells["total_insurable_value_micro_usd"]
            assert tiv.formula == tiv.original_value == "=G6+H6"
            assert tiv.cached_value == "1.0050004"
            assert tiv.normalized_value == 1005000
            assert tiv.calculation_value == "cached_value"
            assert tiv.normalization == ("precision_reduced",)
    if operation == "sum":
        assert result.result == SumValue(value=1005001, outcome="value")
        assert result.display_value == "$1.01"
        assert result.verification.evidence_sum_micro_usd == 1005001
        assert any("precision reduction" in q for q in result.qualifications)
        assert any("not recalculated" in q for q in result.qualifications)
    assert any("duplicate" in q for q in result.qualifications)
    assert any("excluded" in q for q in result.qualifications)
    assert "Reconciled" in result.summary


@pytest.mark.parametrize("operation", ["exists", "count", "sum", "list"])
def test_negative_and_empty_outcomes(db: Path, operation: Operation) -> None:
    result = run(db, operation, " WHERE state = 'AK'")
    assert isinstance(result, Success)
    assert result.evidence.contributors == ()
    assert result.evidence.source_record_count == 0
    assert result.verification.executed_count == 0
    assert result.verification.completeness_checked == (
        ("state", "total_insurable_value_micro_usd") if operation == "sum" else ("state",)
    )
    assert result.verification.dataset_logical_assets == 3
    assert result.verification.dataset_physical_records == 5
    if operation == "sum":
        assert result.result == SumValue(value=None, outcome="no_matching_records")
        assert result.display_value is None
    elif operation == "exists":
        assert result.result == ExistsValue(value=False)


def test_valid_zero_and_exact_json(db: Path) -> None:
    result = run(db, "sum", " WHERE state = 'CA'")
    assert isinstance(result, Success)
    assert result.result == SumValue(value=0, outcome="value")
    assert result.display_value == "$0.00"
    data = json.loads(result.model_dump_json())
    assert data["result"]["value"] == "0"
    assert data["verification"]["result_micro_usd"] == "0"
    assert data["verification"]["evidence_sum_micro_usd"] == "0"
    assert data["evidence"]["contributors"][0]["values"]["total_insurable_value_micro_usd"] == "0"
    assert (
        data["evidence"]["contributors"][0]["source_records"][0]["cells"][-1]["normalized_value"]
        == "0"
    )


@pytest.mark.parametrize(
    ("amount", "expected", "display"),
    [
        ("10000000000.000001", 10000000000000001, "$10,000,000,000.00"),
        ("-1.005", -1005000, "$-1.01"),
    ],
)
def test_exact_large_money_and_signed_display(
    tmp_path: Path, amount: str, expected: int, display: str
) -> None:
    path = database(tmp_path, [asset("a", amount), asset("zero", "0")])
    result = run(path)
    assert isinstance(result, Success)
    assert result.result == SumValue(value=expected, outcome="value")
    assert result.display_value == display
    payload = json.loads(result.model_dump_json())
    assert payload["result"]["value"] == str(expected)
    assert payload["evidence"]["contributors"][0]["values"][
        "total_insurable_value_micro_usd"
    ] == str(expected)


def test_unusable_formula_cache_is_qualified_null_in_listing(tmp_path: Path) -> None:
    row = asset("a", "=G2+H2")
    source = write_workbook(tmp_path / "missing-cache.xlsx", rows=[row], caches={"E2": None})
    path = tmp_path / "missing-cache.sqlite"
    ingest_workbook(source, path)
    result = run(path, "list")
    assert isinstance(result, Success)
    cell = result.evidence.contributors[0].source_records[0].cells[-1]
    assert cell.normalized_value is None and cell.cached_value is None
    assert cell.formula == "=G2+H2" and cell.calculation_value == "cached_value"
    assert any("Selected fields" in q for q in result.qualifications)
    assert any("not recalculated" in q for q in result.qualifications)
    assert isinstance(run(path), Failure)


@pytest.mark.parametrize("size", [500, 501])
@pytest.mark.parametrize("operation", ["exists", "count", "sum", "list"])
def test_all_operation_evidence_bounds(tmp_path: Path, size: int, operation: Operation) -> None:
    path = database(tmp_path, [asset(str(i), "1") for i in range(size)])
    result = run(path, operation)
    if size == 500:
        assert isinstance(result, Success) and len(result.evidence.contributors) == 500
    else:
        assert isinstance(result, Failure) and result.category == "evidence_limit_exceeded"
        assert result.evidence_limit == 500 and result.required_contributor_count == 501


@pytest.mark.parametrize(
    "predicate",
    [
        "(state = 'AZ' OR state IN ('CA', 'TX')) AND city LIKE 'Test%'",
        "state = 'CA' OR (state = 'AZ' AND total_insurable_value_micro_usd >= 1)",
        "city IS NOT NULL AND (state != 'CA' OR construction_type IS NULL)",
        "state <> 'TX' AND total_insurable_value_micro_usd IN (0, 1)",
        "property_name = 'can''t; DROP TABLE assets' OR state = 'AZ'",
    ],
)
def test_predicate_semantics_and_order(db: Path, predicate: str) -> None:
    result = run(db, "list", " WHERE " + predicate)
    assert isinstance(result, Success)
    plan = result.plan
    query = result.evidence.contributor_sql
    assert parse_one(query, read="sqlite").args["where"].this.sql(dialect="sqlite") == plan.filter
    with sqlite3.connect(db) as connection:
        independent = connection.execute(
            "SELECT asset_id FROM assets WHERE " + predicate + " ORDER BY asset_id ASC"
        ).fetchall()
    assert [c.asset_id for c in result.evidence.contributors] == [r[0] for r in independent]


def test_forged_plan_never_authorizes_evidence(db: Path) -> None:
    with read_snapshot(db) as connection:
        context = load_context(connection)
        plan = validate_sql("SELECT COUNT(*) FROM assets", "count", context)
        for corrupt in (
            replace(plan, filter="1=1; DROP TABLE assets"),
            replace(plan, executed_sql="SELECT * FROM source_cells"),
            replace(plan, dataset_id="other"),
            replace(plan, referenced_fields=frozenset({"secret"})),
            replace(plan, query_shape="asset_listing"),
        ):
            with pytest.raises(QAError, match="Invalid evidence query plan"):
                evidence.contributor_query(corrupt, context)
        with pytest.raises(QAError):
            _statement(connection, "SELECT original_value_json FROM source_cells")
        assert connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM assets").fetchone() == (3,)


CORRUPTIONS = [
    ("DELETE FROM asset_sources WHERE row_number = 7", "evidence_incomplete"),
    ("DELETE FROM source_rows WHERE row_number = 7", "source_reference_invalid"),
    ("DELETE FROM source_cells WHERE row_number = 7 AND column_index = 5", "evidence_incomplete"),
    ("DELETE FROM source_cells WHERE row_number = 7 AND column_index = 1", "evidence_incomplete"),
    (
        "UPDATE source_cells SET coordinate = 'E999' WHERE row_number = 7 AND column_index = 5",
        "source_reference_invalid",
    ),
    (
        "UPDATE source_cells SET cached_value_json = '\"2\"' WHERE row_number = 7 AND column_index = 5",
        "evidence_mismatch",
    ),
    (
        "UPDATE assets SET total_insurable_value_micro_usd = 1005001 WHERE asset_id = 'a'",
        "evidence_mismatch",
    ),
    ("UPDATE asset_sources SET asset_id = 'b' WHERE row_number = 7", "evidence_incomplete"),
    (
        "UPDATE source_columns SET excel_letter = 'Z' WHERE column_index = 5",
        "source_reference_invalid",
    ),
    (
        "UPDATE source_columns SET original_label = 'State' WHERE column_index = 5",
        "source_reference_invalid",
    ),
    ("UPDATE asset_sources SET worksheet = 'Other' WHERE row_number = 7", "evidence_incomplete"),
    (
        "UPDATE source_cells SET workbook_sha256 = 'other' WHERE row_number = 7 AND column_index = 5",
        "evidence_incomplete",
    ),
    (
        "UPDATE source_cells SET original_value_json = 'not JSON' WHERE row_number = 7 AND column_index = 5",
        "source_reference_invalid",
    ),
    (
        "UPDATE source_cells SET cached_value_json = 'NaN' WHERE row_number = 7 AND column_index = 5",
        "source_reference_invalid",
    ),
    (
        "UPDATE source_cells SET formula = '=invented' WHERE row_number = 7 AND column_index = 5",
        "source_reference_invalid",
    ),
    (
        "UPDATE source_cells SET cached_type = 'e' WHERE row_number = 7 AND column_index = 5",
        "evidence_mismatch",
    ),
    ("DROP TABLE source_cells", "evidence_incomplete"),
    (
        "UPDATE field_completeness SET physical_non_null = 4 WHERE semantic_field = 'state'",
        "evidence_incomplete",
    ),
    ("UPDATE dataset SET assumptions_json = '{}'", "source_reference_invalid"),
    ("UPDATE dataset SET assumptions_json = '[]'", "source_reference_invalid"),
]


@pytest.mark.parametrize(("sql", "category"), CORRUPTIONS)
def test_corrupt_evidence_fails_closed(db: Path, sql: str, category: str) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(sql)
    result = run(db)
    assert isinstance(result, Failure) and result.category == category
    assert "verification" not in result.model_dump()


def test_reassigned_rows_with_same_duplicate_group_count_are_rejected(db: Path) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE asset_sources SET asset_id = 'b' WHERE row_number IN (7,8)")
    result = run(db, "count")
    assert isinstance(result, Failure) and result.category == "source_reference_invalid"


def test_excessively_nested_source_json_is_a_structured_failure(db: Path) -> None:
    nested = "[" * 2000 + "0" + "]" * 2000
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE source_cells SET cached_value_json = ? WHERE row_number = 7 AND column_index = 5",
            (nested,),
        )
    result = run(db)
    assert isinstance(result, Failure) and result.category == "source_reference_invalid"


@pytest.mark.parametrize("where", [" WHERE state = 'AK'", " WHERE state = 'CA'"])
def test_missing_excluded_predicate_cells_cannot_support_negative_claim(
    db: Path, where: str
) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM source_cells WHERE row_number = 7 AND column_index = 4")
    result = run(db, "exists", where)
    assert isinstance(result, Failure) and result.category == "evidence_incomplete"


@pytest.mark.parametrize("operation", ["exists", "count", "sum", "list"])
def test_independent_result_mismatches_are_rejected(
    db: Path, operation: Operation, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = service.execute_query

    def tampered(*args: Any, **kwargs: Any) -> Any:
        value, count, context = execute(*args, **kwargs)
        wrong = {
            "exists": ExistsValue(value=False),
            "count": CountValue(value=9),
            "sum": SumValue(value=1005002, outcome="value"),
            "list": ListValue(
                records=(AssetRecord(asset_id="invented"),), selected_fields=("asset_id",)
            ),
        }[operation]
        return wrong, count, context

    monkeypatch.setattr(service, "execute_query", tampered)
    result = run(db, operation)
    assert isinstance(result, Failure) and result.category == "evidence_mismatch"


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "value"])
def test_inconsistent_contributors_are_rejected(
    db: Path, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = evidence._statement

    def corrupt(connection: sqlite3.Connection, sql: str, *args: Any, **kwargs: Any) -> Any:
        rows = statement(connection, sql, *args, **kwargs)
        if (
            sql.startswith("SELECT asset_id, state, total_insurable_value_micro_usd FROM assets")
            and "ORDER BY" in sql
        ):
            if change == "missing":
                return rows[:-1]
            if change == "extra":
                return rows + [("extra", "AZ", 1)]
            if change == "duplicate":
                return [rows[0], rows[0], rows[2]]
            return [(rows[0][0], rows[0][1], rows[0][2] + 1), *rows[1:]]
        return rows

    monkeypatch.setattr(evidence, "_statement", corrupt)
    result = run(db, "list", " WHERE state IS NOT NULL")
    assert isinstance(result, Failure) and result.category == "evidence_mismatch"


def test_snapshot_pins_result_contributors_and_original_cells(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(sqlite3.connect(db, timeout=0.1)) as writer:
        writer.execute("PRAGMA journal_mode=WAL").close()
        execute = service.execute_query

        def write_after_result(*args: Any, **kwargs: Any) -> Any:
            result = execute(*args, **kwargs)
            with writer:
                writer.execute(
                    "UPDATE assets SET total_insurable_value_micro_usd = 2005000 WHERE asset_id = 'a'"
                )
                writer.execute(
                    "UPDATE source_cells SET cached_value_json = '\"2.0050004\"' WHERE column_index = 5 AND row_number IN (6,7,8)"
                )
            return result

        monkeypatch.setattr(service, "execute_query", write_after_result)
        result = run(db)
        assert isinstance(result, Success)
        assert result.result == SumValue(value=1005001, outcome="value")
        assert (
            result.evidence.contributors[0].source_records[0].cells[-1].cached_value == "1.0050004"
        )
        assert result.verification.evidence_sum_micro_usd == 1005001
        monkeypatch.setattr(service, "execute_query", execute)
        fresh = run(db)
        assert isinstance(fresh, Success) and fresh.result == SumValue(
            value=2005001, outcome="value"
        )


def test_expected_failure_preserves_caller_and_closes_owned_resources(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with read_snapshot(db) as discovery:
        context = load_context(discovery)
    with sqlite3.connect(db) as writer:
        writer.execute("DELETE FROM source_cells WHERE row_number = 7 AND column_index = 5")
    generation = Generation(
        FakeGenerator("SELECT SUM(total_insurable_value_micro_usd) FROM assets", "sum").proposal
    )
    with read_snapshot(db) as reader:
        result = ask_in_transaction(reader, "Total", generation, generation_context=context)
        assert isinstance(result, Failure) and result.category == "evidence_incomplete"
        assert reader.in_transaction
        assert reader.execute("SELECT COUNT(*) FROM assets").fetchone() == (3,)
        assert _statement(reader, "SELECT COUNT(*) FROM assets") == [(3,)]
    owned: list[sqlite3.Connection] = []
    original = service.read_snapshot

    @contextmanager
    def track(path: Path):
        with original(path) as connection:
            owned.append(connection)
            yield connection

    monkeypatch.setattr(service, "read_snapshot", track)
    assert isinstance(run(db), Failure)
    assert len(owned) == 2
    for connection in owned:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_evidence_resource_failure_never_returns_partial_success(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "MAX_SOURCE_CELLS", 2)
    result = run(db)
    assert isinstance(result, Failure) and result.category == "evidence_query_error"


@pytest.mark.parametrize("operation", ["exists", "count", "sum"])
def test_large_complete_scope_uses_bounded_chunks(
    tmp_path: Path,
    operation: Operation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    size = 1025
    rows = [asset(f"asset-{i:04}", "1", "AZ" if i == 0 else "CA") for i in range(size)]
    # A contributor with three physical sources and an excluded asset with two.
    path = database(tmp_path, rows + [rows[0].copy(), rows[0].copy(), rows[-1].copy()])
    statement = evidence._statement
    queries: list[tuple[str, tuple, int]] = []

    def track(connection: sqlite3.Connection, sql: str, parameters=(), **kwargs: Any):
        result = statement(connection, sql, parameters, **kwargs)
        if "FROM source_cells" in sql:
            queries.append((sql, tuple(parameters), len(result)))
        return result

    monkeypatch.setattr(evidence, "_statement", track)
    where = " WHERE state = 'AK'" if operation == "exists" else " WHERE state = 'AZ'"
    result = run(path, operation, where)
    assert isinstance(result, Success)
    matches = 0 if operation == "exists" else 1
    assert result.matching_count == matches
    assert result.evidence.contributing_asset_count == matches
    assert result.evidence.source_record_count == matches * 3
    if operation == "exists":
        assert result.result == ExistsValue(value=False)
    else:
        assert result.evidence.contributors[0].asset_id == "asset-0000"
        assert [r.row for r in result.evidence.contributors[0].source_records] == [
            2,
            size + 2,
            size + 3,
        ]
    if operation == "sum":
        assert result.result == SumValue(value=1_000_000, outcome="value")
        assert result.verification.evidence_sum_micro_usd == 1_000_000
    chunk_count = ceil(matches / evidence.SOURCE_ASSET_CHUNK_SIZE) + ceil(
        (size - matches) / evidence.SOURCE_ASSET_CHUNK_SIZE
    )
    assert len(queries) == 1 + chunk_count  # One whole-scope EPIC check, then chunks.
    assert (
        max(len(parameters) for _, parameters, _ in queries) <= evidence.SOURCE_ASSET_CHUNK_SIZE + 3
    )
    assert result.verification.dataset_logical_assets == size
    assert result.verification.dataset_physical_records == size + 3


@pytest.mark.parametrize("operation", ["exists", "count", "sum", "list"])
def test_chunk_size_preserves_complete_answer(
    db: Path, operation: Operation, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = run(db, operation, " WHERE state = 'AZ'")
    assert isinstance(baseline, Success)
    monkeypatch.setattr(evidence, "SOURCE_ASSET_CHUNK_SIZE", 1)
    chunked = run(db, operation, " WHERE state = 'AZ'")
    assert isinstance(chunked, Success)
    assert chunked.model_dump(mode="json") == baseline.model_dump(mode="json")


def test_matching_contributors_with_three_sources_span_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    size = evidence.SOURCE_ASSET_CHUNK_SIZE + 1
    rows = [asset(f"asset-{i:04}", "1") for i in range(size)]
    path = database(tmp_path, [row.copy() for row in rows for _ in range(3)])
    statement = evidence._statement
    chunk_sizes = []

    def track(connection: sqlite3.Connection, sql: str, *args: Any, **kwargs: Any):
        cells = statement(connection, sql, *args, **kwargs)
        if sql.startswith("SELECT s.asset_id,"):
            chunk_sizes.append(len(cells))
        return cells

    monkeypatch.setattr(evidence, "_statement", track)
    result = run(path)
    assert isinstance(result, Success)
    assert result.result == SumValue(value=size * 1_000_000, outcome="value")
    assert result.evidence.contributing_asset_count == size
    assert result.evidence.source_record_count == size * 3
    assert all(len(c.source_records) == 3 for c in result.evidence.contributors)
    assert chunk_sizes == [evidence.SOURCE_ASSET_CHUNK_SIZE * 3 * 2, 3 * 2]


@pytest.mark.parametrize("corrupt", ["missing", "wrong_value", "duplicate"])
def test_excluded_cells_in_later_chunks_fail_closed(
    db: Path, corrupt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "SOURCE_ASSET_CHUNK_SIZE", 1)
    statement = evidence._statement
    chunks = 0

    def alter(connection: sqlite3.Connection, sql: str, *args: Any, **kwargs: Any):
        nonlocal chunks
        cells = statement(connection, sql, *args, **kwargs)
        if sql.startswith("SELECT s.asset_id,"):
            chunks += 1
            if chunks == 3:  # 'c' is excluded from a CA query; earlier chunks succeeded.
                if corrupt == "duplicate":
                    return cells + [cells[-1]]
                if corrupt == "missing":
                    return cells[:-1]
                changed = list(cells[-1])
                changed[7] = '"TX"'  # original_value_json for the state cell
                return [*cells[:-1], tuple(changed)]
        return cells

    monkeypatch.setattr(evidence, "_statement", alter)
    result = run(db, "count", " WHERE state = 'CA'")
    assert isinstance(result, Failure)
    assert result.category == (
        "evidence_mismatch" if corrupt == "wrong_value" else "evidence_incomplete"
    )
    assert chunks == 3 and "verification" not in result.model_dump()


def test_source_cell_limit_is_cumulative_across_scope_and_chunks(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "SOURCE_ASSET_CHUNK_SIZE", 1)
    # Five EPIC cells + two contributor cells + six excluded 'a' cells fit.
    # The final excluded 'c' chunk needs two more cells and must fail, not truncate.
    monkeypatch.setattr(evidence, "MAX_SOURCE_CELLS", 14)
    statement = evidence._statement
    allowed: list[int] = []

    def track(connection: sqlite3.Connection, sql: str, *args: Any, **kwargs: Any):
        if sql.startswith("SELECT s.asset_id,"):
            allowed.append(kwargs["max_rows"])
        return statement(connection, sql, *args, **kwargs)

    monkeypatch.setattr(evidence, "_statement", track)
    result = run(db, "count", " WHERE state = 'CA'")
    assert isinstance(result, Failure) and result.category == "evidence_query_error"
    assert allowed == [9, 7, 1]
    assert "verification" not in result.model_dump()


def test_cumulative_deadline_expires_across_successful_statements(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(evidence, "monotonic", lambda: clock[0])
    statement = evidence._statement
    deadlines = []

    def advance(connection: sqlite3.Connection, sql: str, *args: Any, **kwargs: Any):
        rows = statement(connection, sql, *args, **kwargs)
        deadlines.append(kwargs["deadline"])
        clock[0] += 1.5  # Every operation succeeds within its individual 2s budget.
        return rows

    monkeypatch.setattr(evidence, "_statement", advance)
    result = run(db, "count")
    assert isinstance(result, Failure) and result.category == "evidence_query_error"
    assert result.code == "evidence_deadline"
    assert len(deadlines) == 7 and deadlines == [2.0] * 6 + [1.0]
    assert "verification" not in result.model_dump()


@pytest.mark.parametrize("phase", ["normalization", "reconciliation", "summary", "answer"])
def test_python_work_is_inside_evidence_deadline(
    db: Path, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(evidence, "monotonic", lambda: clock[0])
    target, name = {
        "normalization": (evidence, "normalize_record"),
        "reconciliation": (evidence, "reconcile"),
        "summary": (service, "answer_summary"),
        "answer": (service, "Success"),
    }[phase]
    original = getattr(target, name)

    def expire(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        clock[0] = 10.0
        return result

    monkeypatch.setattr(target, name, expire)
    result = run(db, "count")
    assert isinstance(result, Failure) and result.category == "evidence_query_error"
    assert result.code == "evidence_deadline" and "verification" not in result.model_dump()


def test_source_chunks_respect_connection_variable_limit(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with read_snapshot(db) as connection:
        context = load_context(connection)
        generation = Generation(
            FakeGenerator("SELECT COUNT(*) FROM assets WHERE state = 'AK'").proposal
        )
        # Two columns and one asset ID fit; a two-asset chunk does not.
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 3)
        statement = evidence._statement
        sizes = []

        def track(reader: sqlite3.Connection, sql: str, parameters=(), **kwargs: Any):
            if sql.startswith("SELECT s.asset_id,"):
                sizes.append(len(parameters))
            return statement(reader, sql, parameters, **kwargs)

        monkeypatch.setattr(evidence, "_statement", track)
        result = ask_in_transaction(
            connection, "Any Alaska assets?", generation, generation_context=context
        )
        assert isinstance(result, Success)
        assert sizes == [3, 3, 3]
        assert connection.in_transaction


def test_qualifications_are_relevant_and_optional_nulls_remain_null(tmp_path: Path) -> None:
    row = asset("a", None)
    row[1] = None
    path = database(tmp_path, [row])
    result = ask(
        path, "List properties", FakeGenerator("SELECT asset_id, property_name FROM assets", "list")
    )
    assert isinstance(result, Success)
    assert result.evidence.contributors[0].values.property_name is None
    assert result.evidence.contributors[0].source_records[0].cells[-1].original_value is None
    assert any("Selected fields" in q for q in result.qualifications)
    assert not any(
        "Currency" in q or "formula" in q or "precision reduction" in q
        for q in result.qualifications
    )
    missing = run(path)
    assert isinstance(missing, Failure) and missing.category == "data_incomplete"


def test_missing_optional_schema_does_not_break_unrelated_count(tmp_path: Path) -> None:
    headers = HEADERS.copy()
    headers[5] = "Unmapped construction"
    path = database(tmp_path, [asset()], headers=headers)
    result = run(path, "count")
    assert isinstance(result, Success)
    assert result.evidence.required_fields == ("asset_id",)


def test_cli_structured_provenance_failure(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sovtrace.cli import main

    with sqlite3.connect(db) as writer:
        writer.execute("DELETE FROM asset_sources WHERE row_number = 7")
    monkeypatch.setattr("sovtrace.qa.ask", lambda path, question: run(path, "count"))
    with pytest.raises(SystemExit) as error:
        main(["ask", "--format", "json", "--db", str(db), "Count assets"])
    assert error.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failure" and output["category"] == "evidence_incomplete"
    assert "verification" not in output
