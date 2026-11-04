"""Public question-answering service and deterministic monetary checks."""

import re
import sqlite3
from decimal import Decimal, localcontext
from pathlib import Path

from pydantic import ValidationError
from sqlglot import exp, parse_one

from .contracts import TIV, Failure, Proposal, QAError, QueryContext, Success, ValidatedQuery
from .evidence import EvidenceBudget, answer_summary, build_evidence
from .execution import execute_query, load_context, read_snapshot
from .provider import Generation, OpenAIGenerator, SQLGenerator, schema_context
from .validation import validate_sql

# Explicit currency amounts only. This checks scale/strictness, not general NL intent.
MONEY = re.compile(
    r"(?P<operator>more than|greater than|over|exceed(?:s|ing)?|at least|less than|under|below|at most)?\s*"
    r"(?P<sign>-)?(?:\$|USD\s*)(?P<amount>-?\d[\d,]*(?:\.\d+)?)\s*(?P<scale>million|billion|thousand|[mbk])?\b",
    re.IGNORECASE,
)
MISSING = re.compile(
    r"\b(missing|unknown|unrecorded|null|not recorded|not provided|unavailable|blank)\b",
    re.IGNORECASE,
)


def check_thresholds(question: str, plan: ValidatedQuery) -> None:
    amounts: set[int] = set()
    required_operators: dict[int, type[exp.Expr]] = {}
    operators = {
        "more than": exp.GT,
        "greater than": exp.GT,
        "over": exp.GT,
        "exceed": exp.GT,
        "exceeds": exp.GT,
        "exceeding": exp.GT,
        "at least": exp.GTE,
        "less than": exp.LT,
        "under": exp.LT,
        "below": exp.LT,
        "at most": exp.LTE,
    }
    for match in MONEY.finditer(question):
        scale = (match["scale"] or "").lower()
        multiplier = {
            "": 1,
            "k": 1000,
            "thousand": 1000,
            "m": 1_000_000,
            "million": 1_000_000,
            "b": 1_000_000_000,
            "billion": 1_000_000_000,
        }[scale]
        if match["sign"] and match["amount"].startswith("-"):
            raise QAError(
                "unsupported_question", "threshold_sign", "Ambiguous currency threshold sign"
            )
        with localcontext() as arithmetic:
            arithmetic.prec = len(match["amount"]) + 30
            amount = Decimal(match["amount"].replace(",", "")) * multiplier * 1_000_000
            if match["sign"]:
                amount = -amount
        if amount != amount.to_integral_value() or not -(2**63) <= amount < 2**63:
            raise QAError(
                "unsupported_question",
                "threshold_precision",
                "Currency threshold must be representable as signed int64 micro-USD",
            )
        integer = int(amount)
        amounts.add(integer)
        if match["operator"]:
            required_operators[integer] = operators[match["operator"].lower()]
    if not amounts:
        return
    observed: set[int] = set()
    if plan.filter:
        tree = parse_one(plan.filter, read="sqlite")
        for node in tree.walk():
            if (
                type(node) in (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.In)
                and isinstance(node.this, exp.Column)
                and node.this.name == TIV
            ):
                literals = node.expressions if type(node) is exp.In else [node.expression]
                for value in literals:
                    number = int(value.sql(dialect="sqlite"))
                    observed.add(number)
                    if (
                        number in required_operators
                        and type(node) is not required_operators[number]
                    ):
                        raise QAError(
                            "sql_validation_error",
                            "threshold_operator",
                            "SQL does not preserve the explicit financial threshold inequality",
                        )
    if observed != amounts:
        raise QAError(
            "sql_validation_error",
            "threshold_scale",
            "SQL monetary literals do not match the question's exact micro-USD thresholds",
        )


def _validate_question(question: str) -> None:
    if not question.strip() or len(question) > 8_000:
        raise QAError(
            "unsupported_question", "question_size", "Question must contain 1–8000 characters"
        )


def ask_in_transaction(
    connection: sqlite3.Connection,
    question: str,
    generation: Generation,
    *,
    generation_context: QueryContext,
) -> Success | Failure:
    """Provider-free evidence reconciliation entry point; caller retains connection/transaction ownership.

    The proposal must have been generated before opening the execution snapshot.
    Compare the discovery context with this authoritative snapshot before using SQL.
    Completeness may change: it is checked afresh along with the result/evidence reads.
    """
    try:
        _validate_question(question)
        context = load_context(connection)
        if (
            context.identity != generation_context.identity
            or context.available_fields != generation_context.available_fields
            or context.manifest.concepts != generation_context.manifest.concepts
        ):
            raise QAError(
                "configuration_error",
                "dataset_changed",
                "Dataset identity or semantic contract changed after context discovery",
            )
        proposal = Proposal.model_validate(generation.proposal.model_dump())
        if proposal.status == "unsupported":
            raise QAError(
                "unsupported_question",
                "model_unsupported",
                proposal.reason or "Unsupported question",
            )
        assert proposal.sql is not None and proposal.operation is not None
        plan = validate_sql(
            proposal.sql, proposal.operation, context, allows_missing=bool(MISSING.search(question))
        )
        check_thresholds(question, plan)
        value, count, context = execute_query(connection, plan, complete_evidence=True)
        budget = EvidenceBudget()
        evidence, verification, provenance_qualifications = build_evidence(
            connection, plan, context, value, count, budget=budget
        )
        summary, display = answer_summary(value, evidence)
        qualifications = list(provenance_qualifications)
        if plan.allows_missing and plan.predicate_fields - plan.ordinary_predicate_fields:
            qualifications.append(
                "Null predicates describe missing recorded data, not known negative business facts."
            )
        if set(plan.result_fields) & context.incomplete_fields:
            qualifications.append(
                "Selected fields contain missing recorded values, represented as null."
            )
        answer = Success(
            question=question,
            dataset=context.identity,
            model=generation.model,
            operation=plan.operation,
            proposed_sql=plan.proposed_sql,
            executed_sql=plan.executed_sql,
            result=value,
            matching_count=count,
            referenced_fields=tuple(sorted(plan.referenced_fields)),
            qualifications=tuple(qualifications),
            plan=plan,
            summary=summary,
            display_value=display,
            evidence=evidence,
            verification=verification,
        )
        budget.check()
        return answer
    except QAError as exc:
        return exc.failure
    except ValidationError:
        return Failure(
            category="invalid_model_response",
            code="malformed_output",
            message="Invalid structured model proposal",
        )


def ask(database: Path, question: str, generator: SQLGenerator | None = None) -> Success | Failure:
    """Discover briefly, generate without a connection, then execute in a fresh snapshot."""
    try:
        _validate_question(question)
        with read_snapshot(database) as connection:
            generation_context = load_context(connection)
        generation = (generator or OpenAIGenerator()).generate(
            question, schema_context(generation_context)
        )
        with read_snapshot(database) as connection:
            return ask_in_transaction(
                connection, question, generation, generation_context=generation_context
            )
    except QAError as exc:
        return exc.failure
    except ValidationError:
        return Failure(
            category="invalid_model_response",
            code="malformed_output",
            message="Invalid structured model proposal",
        )
