"""Constrained question answering with complete, reconciled source provenance."""

from .contracts import Failure, Proposal, QueryContext, Success, ValidatedQuery
from .execution import execute_query, load_context, open_readonly, read_snapshot
from .provider import Generation, OpenAIGenerator, SQLGenerator, schema_context
from .service import ask, ask_in_transaction
from .validation import validate_sql

__all__ = [
    "Failure",
    "Generation",
    "OpenAIGenerator",
    "Proposal",
    "SQLGenerator",
    "QueryContext",
    "Success",
    "ValidatedQuery",
    "ask",
    "ask_in_transaction",
    "execute_query",
    "load_context",
    "open_readonly",
    "read_snapshot",
    "schema_context",
    "validate_sql",
]
