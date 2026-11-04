"""Restricted table/projection comparison for synthetic SQL evaluation.

The application grammar supplies syntax limits. The comparison context enables
all supported concepts; it makes no claim about any particular dataset's values.
"""

from sovtrace.ingestion.manifest import FIELD_DEFINITIONS, SourceColumn, interpret_schema
from sovtrace.qa.contracts import FIELDS, DatasetIdentity, Operation, QAError, QueryContext
from sovtrace.qa.validation import _parse, _validate

_CONTEXT = QueryContext(
    identity=DatasetIdentity(
        dataset_id="comparison",
        workbook_sha256="0" * 64,
        workbook_filename="comparison.xlsx",
        worksheet="comparison",
    ),
    manifest=interpret_schema(
        "comparison.xlsx",
        "0" * 64,
        "comparison",
        1,
        2,
        2,
        tuple(
            SourceColumn(index=i, letter=chr(64 + i), label=definition[0])
            for i, definition in enumerate(FIELD_DEFINITIONS.values(), 1)
        ),
    ),
    available_fields=frozenset(FIELDS),
    incomplete_fields=frozenset(),
    qualifications=(),
)


def checked_query(sql: str, operation: Operation):
    """Validate the full statement before any comparison-only simplification."""
    tree = _parse(sql)
    projection, _, _ = _validate(tree, operation, _CONTEXT, allows_missing=True)
    return tree, projection


def matches_base_sql(
    actual_sql: str,
    reference_sql: str | None,
    operation: Operation | None,
    validated_projection: tuple[str, ...] = (),
) -> bool:
    """Compare restricted query shape and predicates, honoring validated lists.

    A list oracle may use COUNT(*) to describe membership. In that case the
    actual list must still match its distinct validated projection exactly.
    """
    if reference_sql is None or operation is None:
        return False
    try:
        actual, projection = checked_query(actual_sql, operation)
        if operation == "list":
            if not validated_projection or projection != validated_projection:
                return False
            # The synthetic membership oracle deliberately uses a count reference.
            reference = _parse(reference_sql)
            from sqlglot import exp

            reference_operation = (
                "count" if isinstance(reference.expressions[0], exp.Count) else "list"
            )
            reference, reference_projection = checked_query(reference_sql, reference_operation)
            if reference_operation == "list" and reference_projection != projection:
                return False
            actual.set("expressions", reference.expressions)
            actual.set("order", None)
            reference.set("order", None)
        else:
            reference, _ = checked_query(reference_sql, operation)
            if validated_projection and validated_projection != projection:
                return False
        return actual.sql(dialect="sqlite", normalize=True) == reference.sql(
            dialect="sqlite", normalize=True
        )
    except (QAError, ValueError, RecursionError, IndexError):
        return False
