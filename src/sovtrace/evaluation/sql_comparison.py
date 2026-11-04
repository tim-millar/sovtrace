"""Compare predicates without losing parenthesis or boolean precedence.

Only associative/commutative AND terms are normalized; OR order and structure
remain intact. Both complete statements must satisfy the application grammar.
"""

from sqlglot import exp
from sqlglot.errors import SqlglotError

from sovtrace.qa.contracts import Operation, QAError

from .base_sql import checked_query


def _predicate(node: exp.Expr) -> object:
    if isinstance(node, exp.Paren):
        return _predicate(node.this)
    if isinstance(node, exp.And):

        def terms(item: exp.Expr) -> list[object]:
            if isinstance(item, exp.Paren):
                return terms(item.this)
            if isinstance(item, exp.And):
                return terms(item.this) + terms(item.expression)
            return [_predicate(item)]

        return ("and", tuple(sorted(terms(node), key=repr)))
    if isinstance(node, exp.Or):
        return ("or", _predicate(node.this), _predicate(node.expression))
    return node.sql(dialect="sqlite", normalize=True)


def matches_evaluation_sql(
    actual_sql: str,
    reference_sql: str | None,
    operation: Operation | None,
    validated_projection: tuple[str, ...] = (),
    *,
    base_comparator,
) -> bool:
    if reference_sql is None:
        return False
    try:
        if operation is None:
            return False
        actual, _ = checked_query(actual_sql, operation)
        # List oracles may describe membership with a count rather than a listing.
        reference_operation = operation
        if operation == "list":
            from sovtrace.qa.validation import _parse

            candidate = _parse(reference_sql)
            if isinstance(candidate.expressions[0], exp.Count):
                reference_operation = "count"
        reference, _ = checked_query(reference_sql, reference_operation)
        predicates = []
        for query in (actual, reference):
            where = query.args.get("where")
            predicates.append(_predicate(where.this) if where is not None else None)
            query.set("where", None)
        return predicates[0] == predicates[1] and base_comparator(
            actual.sql(dialect="sqlite"),
            reference.sql(dialect="sqlite"),
            operation,
            validated_projection,
        )
    except (QAError, SqlglotError, RecursionError, ValueError, IndexError):
        return False
