"""Complete allowlist grammar. No original model SQL crosses the execution boundary."""

import re
from typing import NoReturn

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .contracts import TIV, Operation, QAError, QueryContext, ValidatedQuery

MAX_SQL_LENGTH = 16_000
MAX_NODES = 256
MAX_DEPTH = 32
MAX_IN_VALUES = 100
MAX_TEXT_LENGTH = 1_000


def reject(message: str) -> NoReturn:
    raise QAError("sql_validation_error", "unsupported_sql", message)


def shape(node: exp.Expr, cls: type[exp.Expr], keys: set[str]) -> None:
    if type(node) is not cls or any(
        k not in keys and v not in (None, False, []) for k, v in node.args.items()
    ):
        reject(f"Unsupported SQL structure: {type(node).__name__}")


def identifier(node: exp.Expr, expected: str | None = None) -> str:
    shape(node, exp.Identifier, {"this", "quoted"})
    name = node.name
    if expected is not None and name != expected:
        reject(f"Expected {expected}")
    return name


def column(node: exp.Expr, context: QueryContext) -> str:
    shape(node, exp.Column, {"this"})
    name = identifier(node.this)
    if name not in context.available_fields:
        from .contracts import FIELDS

        if name in FIELDS:
            raise QAError(
                "unavailable_field", "absent_field", f"Semantic field {name} is unavailable"
            )
        reject("Unauthorized semantic column")
    return name


def literal(node: exp.Expr, field: str) -> None:
    negative = type(node) is exp.Neg
    if negative:
        shape(node, exp.Neg, {"this"})
        node = node.this
    shape(node, exp.Literal, {"this", "is_string"})
    if field == TIV:
        if node.is_string or len(node.this) > 19 or not re.fullmatch(r"[0-9]+", node.this):
            reject("Monetary comparisons require integer micro-USD literals")
        value = int(node.this) * (-1 if negative else 1)
        if not -(2**63) <= value < 2**63:
            reject("Monetary literal exceeds signed int64")
    elif negative or not node.is_string or len(node.this) > MAX_TEXT_LENGTH or "\x00" in node.this:
        reject("Text comparisons require bounded text literals")


def predicate(
    node: exp.Expr,
    context: QueryContext,
    fields: set[str],
    ordinary: set[str],
    allows_missing: bool,
) -> None:
    if type(node) in (exp.And, exp.Or):
        shape(node, type(node), {"this", "expression"})
        predicate(node.this, context, fields, ordinary, allows_missing)
        predicate(node.expression, context, fields, ordinary, allows_missing)
    elif type(node) is exp.Paren:
        shape(node, exp.Paren, {"this"})
        predicate(node.this, context, fields, ordinary, allows_missing)
    elif type(node) is exp.Not:
        shape(node, exp.Not, {"this"})
        if type(node.this) is not exp.Is:
            reject("NOT is only permitted in IS NOT NULL")
        predicate(node.this, context, fields, ordinary, allows_missing)
    elif type(node) is exp.Is:
        shape(node, exp.Is, {"this", "expression"})
        field = column(node.this, context)
        shape(node.expression, exp.Null, set())
        if not allows_missing:
            reject("Null predicates require an explicit question about missing recorded data")
        fields.add(field)
    elif type(node) in (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Like, exp.In):
        keys = {"this", "expressions"} if type(node) is exp.In else {"this", "expression"}
        shape(node, type(node), keys)
        field = column(node.this, context)
        fields.add(field)
        ordinary.add(field)
        if type(node) is exp.In:
            values = node.expressions
            if not 1 <= len(values) <= MAX_IN_VALUES:
                reject("IN requires 1–100 scalar literals")
        else:
            values = [node.expression]
        if type(node) is exp.Like and field == TIV:
            reject("LIKE is only supported for text")
        for value in values:
            literal(value, field)
    else:
        reject(f"Unsupported predicate: {type(node).__name__}")


def _parse(sql: str) -> exp.Select:
    if not sql.strip() or len(sql) > MAX_SQL_LENGTH or "\x00" in sql:
        reject("SQL is empty or exceeds input limits")
    try:
        tokens = sqlglot.tokenize(sql, read="sqlite")
        if any(t.comments for t in tokens):
            reject("SQL comments are not supported")
        statements = sqlglot.parse(sql, read="sqlite")
    except (SqlglotError, RecursionError, ValueError):
        reject("SQL could not be parsed as SQLite")
    if len(statements) != 1 or type(statements[0]) is not exp.Select:
        reject("Exactly one SELECT is required")
    tree = statements[0]
    assert isinstance(tree, exp.Select)
    nodes = list(tree.walk())
    if len(nodes) > MAX_NODES or any(n.depth > MAX_DEPTH for n in nodes):
        reject("SQL exceeds structural limits")
    return tree


def _validate(
    tree: exp.Select, operation: Operation, context: QueryContext, allows_missing: bool
) -> tuple[tuple[str, ...], set[str], set[str]]:
    shape(tree, exp.Select, {"expressions", "from_", "where", "order"})
    source = tree.args.get("from_")
    if source is None:
        reject("FROM assets is required")
    shape(source, exp.From, {"this"})
    shape(source.this, exp.Table, {"this"})
    identifier(source.this.this, "assets")
    projections = tree.expressions
    result_fields: tuple[str, ...]
    if operation in ("exists", "count", "sum"):
        if len(projections) != 1:
            reject("Scalar operation requires one projection")
        aggregate = projections[0]
        if operation == "sum":
            shape(aggregate, exp.Sum, {"this"})
            if column(aggregate.this, context) != TIV:
                reject("Only total insurable value may be summed")
            result_fields = (TIV,)
        else:
            shape(aggregate, exp.Count, {"this", "big_int"})
            shape(aggregate.this, exp.Star, set())
            result_fields = ()
        if tree.args.get("order") is not None:
            reject("Aggregate ordering is unsupported")
    elif operation == "list":
        result_fields = tuple(column(p, context) for p in projections)
        if "asset_id" not in result_fields or len(set(result_fields)) != len(result_fields):
            reject("Lists require distinct semantic columns including asset_id")
        order = tree.args.get("order")
        if order is not None:
            shape(order, exp.Order, {"expressions"})
            if len(order.expressions) != 1:
                reject("Only ORDER BY asset_id ASC is supported")
            ordered = order.expressions[0]
            shape(ordered, exp.Ordered, {"this", "desc", "nulls_first"})
            if column(ordered.this, context) != "asset_id" or ordered.args.get("desc"):
                reject("Only ORDER BY asset_id ASC is supported")
    else:
        reject("Unsupported operation")
    fields: set[str] = set()
    ordinary: set[str] = set()
    where = tree.args.get("where")
    if where is not None:
        shape(where, exp.Where, {"this"})
        predicate(where.this, context, fields, ordinary, allows_missing)
    return result_fields, fields, ordinary


def validate_sql(
    sql: str, operation: Operation, context: QueryContext, *, allows_missing: bool = False
) -> ValidatedQuery:
    tree = _parse(sql)
    _validate(tree, operation, context, allows_missing)
    if operation == "list":
        tree.set(
            "order",
            exp.Order(
                expressions=[exp.Ordered(this=exp.column("asset_id"), desc=False, nulls_first=True)]
            ),
        )
    executed = tree.sql(dialect="sqlite")
    # Validate the exact serialized statement after canonicalization, not just its input.
    tree = _parse(executed)
    result_fields, fields, ordinary = _validate(tree, operation, context, allows_missing)
    where = tree.args.get("where")
    return ValidatedQuery(
        operation=operation,
        proposed_sql=sql,
        executed_sql=executed,
        filter=where.this.sql(dialect="sqlite") if where else None,
        referenced_fields=frozenset(fields | set(result_fields) | {"asset_id"}),
        predicate_fields=frozenset(fields),
        ordinary_predicate_fields=frozenset(ordinary),
        result_fields=result_fields,
        query_shape="asset_listing" if operation == "list" else "scalar_aggregate",
        dataset_id=context.identity.dataset_id,
        allows_missing=allows_missing,
    )
