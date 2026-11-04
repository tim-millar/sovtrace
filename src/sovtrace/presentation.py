"""Pure text views of existing ingestion reports and verified QA results."""

import re
import shlex
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException

from .ingestion.errors import FailureCategory, IngestionError
from .ingestion.manifest import SemanticField
from .qa.contracts import TIV, CountValue, ExistsValue, Failure, ListValue, Success, SumValue


def _safe(value: object, *, table: bool = False) -> str:
    """Make controls visible; table strings also escape delimiters and literal escapes."""
    rendered = []
    for char in str(value):
        if char == "\\" and table:
            rendered.append("\\\\")
        elif char == '"' and table:
            rendered.append('\\"')
        elif char == "|" and table:
            rendered.append("\\x7c")
        elif unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            rendered.append(ascii(char)[1:-1])
        else:
            rendered.append(char)
    return "".join(rendered)


def _money(micro_usd: int) -> str:
    # Display conversion only; the verified integer and aggregation stay untouched.
    with localcontext() as context:
        context.prec = max(28, len(str(abs(micro_usd))) + 2)
        dollars = (Decimal(micro_usd) / 1_000_000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${dollars:,.2f}"


def _count(count: int, singular: str) -> str:
    return f"{count} {singular}{'' if count == 1 else 's'}"


def render_ingest_text(report: Mapping[str, Any], database: str | Path) -> str:
    """Summarize recorded counts without assigning severity to diagnostic codes."""
    inference = report["schema_inference"]
    status = inference["status"]
    schema = {
        "disabled": "Deterministic mapping; model-assisted inference disabled",
        "not_needed": "Deterministic mapping (no model needed)",
        "applied": "Model-assisted mapping accepted and validated",
        "fallback": "Deterministic fallback after technical inference failure",
    }[status]
    if inference.get("model") and status in {"applied", "fallback"}:
        label = "model" if status == "applied" else "attempted model"
        schema += f" ({label}: {_safe(inference['model'])})"
    lines = [
        f"Imported: {_safe(report['workbook'])}",
        f"Worksheet: {_safe(report['worksheet'])}",
        f"Database: {_safe(database)}",
        "",
        f"Assets: {_count(report['logical_assets'], 'logical asset')}",
        f"Source records: {_count(report['physical_records'], 'original spreadsheet row')}",
        f"Verified duplicate groups: {report['verified_duplicate_groups']}",
        "",
        f"Schema: {schema}",
    ]
    incomplete = []
    absent = []
    for field, quality in report["field_completeness"].items():
        if quality["availability"] == "absent":
            absent.append(field)
        elif field in report["mapped_fields"] and (
            quality["logical_non_null"] < quality["logical_total"]
            or quality["physical_non_null"] < quality["physical_total"]
        ):
            incomplete.append(
                f"  {field}: {quality['logical_non_null']}/{_count(quality['logical_total'], 'logical asset')}; "
                f"{quality['physical_non_null']}/{_count(quality['physical_total'], 'source record')} non-NULL"
            )
    lines.append(
        "Data quality: Missing or invalid mapped values (non-NULL completeness below)"
        if incomplete
        else "Data quality: No missing or invalid mapped values"
    )
    lines.extend(incomplete)
    if absent:
        lines.append("Unavailable concepts: " + ", ".join(absent))
    lines.append("Valuations complete: " + ("Yes" if report["valuation_complete"] else "No"))
    exclusions = [
        f"{key}={value}" for key, value in sorted(report["excluded_rows"].items()) if value
    ]
    if exclusions:
        lines.append("Excluded non-asset rows: " + ", ".join(exclusions))
    diagnostics = report["diagnostic_summary"]
    if diagnostics:
        lines.append(
            "Recorded diagnostics (counts by code): "
            + ", ".join(f"{code}={count}" for code, count in sorted(diagnostics.items()))
        )
    lines.extend(
        [
            "Full report and diagnostics: --format json",
            "",
            "Next:",
            (
                f"uv run --locked sovtrace ask --db {shlex.quote(str(database))} 'How many assets are in Arizona?'"
                if _safe(database) == str(database)
                else "Ask a question with sovtrace ask using this database as --db."
            ),
            "",
            "Live questions require OPENAI_API_KEY.",
        ]
    )
    return "\n".join(lines)


def _listing(value: ListValue) -> list[str]:
    if not value.records:
        return ["No matching records (0 logical assets)."]
    headers = [_safe(field, table=True) for field in value.selected_fields]
    rows = []
    for record in value.records:
        row = []
        for field in value.selected_fields:
            cell = getattr(record, field)
            if cell is None:
                row.append("NULL")
            elif field == TIV:
                row.append(_money(cell))
            else:
                row.append('"' + _safe(cell, table=True) + '"')
        rows.append(row)
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]

    def line(row: list[str]) -> str:
        return " | ".join(
            cell.ljust(width) for cell, width in zip(row, widths, strict=True)
        ).rstrip()

    return [line(headers), "-+-".join("-" * width for width in widths), *map(line, rows)]


def render_answer_text(success: Success) -> str:
    """Present the typed answer and its established verification and evidence counts."""
    value = success.result
    table = []
    if isinstance(value, ExistsValue):
        answer = "Yes" if value.value else "No"
    elif isinstance(value, CountValue):
        answer = _count(value.value, "logical asset")
    elif isinstance(value, SumValue):
        if value.outcome == "no_matching_records":
            answer = "No matching records; no monetary total is measured."
        else:
            assert value.value is not None
            display = (
                success.display_value if success.display_value is not None else _money(value.value)
            )
            answer = f"{_safe(display)} (USD)"
    else:
        answer = _count(len(value.records), "logical asset")
        table = _listing(value)
    lines = [f"Question: {_safe(success.question)}", f"Answer: {answer}"]
    if table:
        lines.extend(["", *table])
    lines.extend(
        [
            "",
            f"Workbook: {_safe(success.dataset.workbook_filename)}",
            f"Worksheet: {_safe(success.dataset.worksheet)}",
            f"Dataset: {_safe(success.dataset.dataset_id)}",
            f"Verification: {success.status}; reconciled={success.verification.reconciled}, "
            f"complete={success.verification.complete}, same snapshot={success.verification.same_snapshot}",
            f"Contributors: {_count(success.evidence.contributing_asset_count, 'logical asset')}",
            f"Source records: {_count(success.evidence.source_record_count, 'original spreadsheet row')}",
            "",
            "Executed SQL:",
            # Preserve ordinary SQL verbatim, including its line breaks.
            "\n".join(_safe(line) for line in success.executed_sql.split("\n")),
        ]
    )
    if success.qualifications:
        lines.extend(["", "Qualifications:"])
        lines.extend(f"- {_safe(qualification)}" for qualification in success.qualifications)
    lines.extend(["", "Complete original-cell evidence: --format json"])
    return "\n".join(lines)


@dataclass(frozen=True)
class _Diagnostic:
    summary: str
    detail: str = ""
    next_steps: tuple[str, ...] = ()


# Fixed recovery prose only: paths, provider payloads and source values never become commands.
_SUPPORTED_QUESTION = (
    "Try an existence, count, TIV sum or bounded listing question using supported asset concepts."
)
_NARROW = "Narrow the question to a smaller supported set of assets."
_MAPPING = _Diagnostic(
    "A valid supported schema could not be established.",
    "Mandatory concepts: asset_id, state, total_insurable_value_micro_usd; other supported "
    "concepts are optional. Missing mandatory concepts, ambiguous or contradictory mappings "
    "and prohibited value substitutes are rejected.",
    (
        "Review source headings and their business meaning; component valuations cannot substitute for TIV.",
    ),
)
_STRUCTURE = _Diagnostic(
    "Workbook structure is outside the supported layout.",
    "Supported layout: one unambiguous SOV table; header in rows 1–10; supported pre-header "
    "metadata; contiguous asset records. Merges are limited to recognized single-row "
    "pre-header section headings; summaries must follow the documented terminal SUM grammar.",
    ("Review the workbook against the supported structure documented in README.md.",),
)
_PROVIDER = {
    "authentication": _Diagnostic(
        "OpenAI authentication or authorization failed.",
        next_steps=(
            "Check that the environment credential is valid and authorized for the configured model.",
        ),
    ),
    "timeout": _Diagnostic(
        "The OpenAI request timed out.",
        next_steps=("Retry later if appropriate; no usable proposal was received.",),
    ),
    "transport": _Diagnostic(
        "The OpenAI service could not be reached.",
        next_steps=("Check network connectivity to OpenAI before retrying.",),
    ),
    "rate_limit": _Diagnostic(
        "OpenAI rate limit exceeded.",
        next_steps=("Check the account's request quota or wait before retrying.",),
    ),
    "service_unavailable": _Diagnostic(
        "OpenAI could not complete the request.",
        next_steps=("Check service availability before retrying later.",),
    ),
    "request_rejected": _Diagnostic(
        "OpenAI rejected the request.",
        next_steps=("Check the configured model and account access before retrying.",),
    ),
}
_INVALID_RESPONSE_CODES = {"model_refusal", "incomplete_output", "malformed_output"}
_MAPPING_CODES = {
    "mapping_ambiguous",
    "mapping_missing_required",
    "mapping_invalid_column",
    "mapping_conflict",
    "mapping_incompatible",
}
_MONEY_ERRORS = {
    "TIV has unsupported decimal precision": "TIV precision exceeds the supported decimal bounds.",
    "TIV exceeds signed 64-bit micro-USD range": "TIV exceeds the signed 64-bit micro-USD range.",
    "Rounded TIV exceeds signed 64-bit range": "Rounded TIV exceeds the signed 64-bit range.",
    "Logical-asset SUM may overflow signed 64-bit micro-USD": "Logical-asset monetary subtotals exceed the signed 64-bit micro-USD range.",
}


def _ingest_diagnostic(
    error: IngestionError, database: str | Path | None
) -> tuple[str, _Diagnostic]:
    """Category first, then exact messages/maintained forms, context, typed cause, fallback.

    No free-text substring matching. Captured source labels and exception details are
    never displayed. Semantic fields must be members of the existing six-field contract.
    """
    category, message = error.category, str(error)
    reason = str(category)
    if category == FailureCategory.WORKBOOK:
        if message == "Only .xlsx workbooks are supported":
            return reason, _Diagnostic(
                "Unsupported source format.",
                "The importer accepts supported .xlsx workbooks.",
                (
                    "Export a supported Excel .xlsx workbook; changing a filename extension does not convert a file.",
                ),
            )
        cause = error.__cause__
        if isinstance(cause, FileNotFoundError):
            return reason, _Diagnostic(
                "The source path could not be opened.",
                "Relative paths are resolved against the current working directory.",
                ("Check the supplied source path and filename.",),
            )
        if isinstance(cause, IsADirectoryError):
            return reason, _Diagnostic(
                "The source path refers to a directory.",
                next_steps=("Supply the path to a readable .xlsx workbook file, not a directory.",),
            )
        if isinstance(cause, PermissionError):
            return reason, _Diagnostic(
                "The source workbook could not be read.",
                next_steps=("Check read access to the supplied source file.",),
            )
        if isinstance(cause, (BadZipFile, InvalidFileException)):
            return reason, _Diagnostic(
                "The supplied file could not be interpreted as a supported workbook.",
                next_steps=("Check that the file is a valid supported Excel .xlsx workbook.",),
            )
        return reason, _Diagnostic("Workbook loading failed.")
    if category == FailureCategory.STRUCTURE:
        if message == "Expected one SOV table; found 0 candidate headers":
            return reason, _Diagnostic(
                "No candidate SOV header was found.", _STRUCTURE.detail, _STRUCTURE.next_steps
            )
        return reason, _STRUCTURE
    if category == FailureCategory.MAPPING:
        identifier, separator, field = message.partition(": ")
        if (
            separator
            and identifier in _MAPPING_CODES
            and field in {"schema", *(f.value for f in SemanticField)}
        ):
            return f"{reason} / {identifier}: {_safe(field)}", _MAPPING
        # SchemaManifest's established deterministic-only Pydantic messages.
        for concept in SemanticField:
            if message == f"Value error, {concept}: required concept is absent":
                return reason, _Diagnostic(
                    f"Required concept is absent: {_safe(concept)}.",
                    _MAPPING.detail,
                    _MAPPING.next_steps,
                )
            if re.fullmatch(
                rf"Value error, {concept}: ambiguous mapping to columns \([1-9][0-9]*(?:, [1-9][0-9]*)+\)",
                message,
            ):
                return reason, _Diagnostic(
                    f"Ambiguous mapping: {_safe(concept)}.", _MAPPING.detail, _MAPPING.next_steps
                )
        return reason, _MAPPING
    if category == FailureCategory.INFERENCE:
        if message == "schema_inference_metadata_limit":
            return f"{reason} / {message}", _Diagnostic(
                "Schema inference metadata exceeds its established bounds.",
                "Inference accepts at most 128 columns, 256 characters per label and 32,768 label characters in total.",
            )
        identifier, separator, code = message.partition(": ")
        if separator and identifier == "schema_inference_provider_error":
            if code == "missing_credentials":
                return f"{reason} / {identifier}: {code}", _Diagnostic(
                    "Schema inference requires OpenAI credentials.",
                    "Model-assisted schema inference was required but OPENAI_API_KEY was unavailable.",
                    (
                        "Set OPENAI_API_KEY in your environment and retry (for example: export OPENAI_API_KEY='<your-api-key>').",
                        "--no-infer-schema disables model-assisted inference and schema metadata transmission to OpenAI. "
                        "It retains deterministic-only mapping and may fail on unfamiliar headings.",
                    ),
                )
            if code in _PROVIDER:
                return f"{reason} / {identifier}: {code}", _PROVIDER[code]
        if (
            separator
            and identifier == "schema_inference_invalid_response"
            and code in _INVALID_RESPONSE_CODES
        ):
            return f"{reason} / {identifier}: {code}", _Diagnostic(
                "A usable schema proposal was not received.",
                "No replacement proposal was generated.",
            )
        return reason, _Diagnostic("Schema inference could not complete.")
    if category == FailureCategory.PERSISTENCE:
        if message == "Source workbook cannot be the output database":
            return reason, _Diagnostic(
                "Source workbook and output destination must differ.",
                next_steps=("Select a separate --db destination.",),
            )
        if database is not None and message == f"Output exists: {database}; use --replace":
            return reason, _Diagnostic(
                "Destination database already exists.",
                "Existing output is protected.",
                (
                    "Choose another --db destination, or use --replace if you intend to replace the existing database.",
                ),
            )
        return reason, _Diagnostic("SQLite publication could not complete.")
    if category == FailureCategory.DATA:
        # Monetary errors are fixed messages, possibly wrapped with a typed IngestionError cause.
        money_message = message
        if (
            money_message not in _MONEY_ERRORS
            and isinstance(error.__cause__, IngestionError)
            and error.__cause__.category == category
        ):
            money_message = str(error.__cause__)
        if money_message in _MONEY_ERRORS:
            return reason, _Diagnostic(
                _MONEY_ERRORS[money_message],
                next_steps=(
                    "Review source monetary values against the documented precision and range requirements.",
                ),
            )
        if re.fullmatch(
            r"[^!\x00-\x1f]{1,31}![A-Z]{1,3}[1-9][0-9]*: "
            r"(?:invalid asset identifier|missing asset identifier in populated row)",
            message,
        ):
            return reason, _Diagnostic(
                "A populated asset row has a missing or invalid identifier.",
                "Asset identifiers require nonempty text after trimming, or a usable text formula cache.",
                ("Review source asset identifiers; do not fabricate missing identities.",),
            )
        return reason, _Diagnostic(
            "Source data could not satisfy established normalization requirements.",
            next_steps=("Review source data against the documented normalization requirements.",),
        )
    if category == FailureCategory.DUPLICATE:
        return reason, _Diagnostic(
            "Records sharing an identity could not be verified as identical.",
            "Duplicate consolidation requires equality of all source business-column values and usable formula caches.",
            ("Review conflicting source records; records cannot be silently dropped.",),
        )
    return reason, _Diagnostic("Import could not complete.")


def _failure_lines(operation: str, reason: str, diagnostic: _Diagnostic) -> list[str]:
    lines = [f"{operation} failed: {diagnostic.summary}", "", f"Reason: {reason}"]
    if diagnostic.detail:
        lines.extend(["", diagnostic.detail])
    return lines


def _with_next(lines: list[str], diagnostic: _Diagnostic) -> str:
    if diagnostic.next_steps:
        lines.extend(["", "Next:", *(f"  {step}" for step in diagnostic.next_steps)])
    return "\n".join(lines)


def render_ingest_failure_text(error: IngestionError, *, database: str | Path | None = None) -> str:
    """Present expected import failures without changing or disclosing raw exceptions.

    database is the CLI's parsed --db argument, used only for exact destination-error
    matching. This renderer performs no filesystem or service operations.
    """
    reason, diagnostic = _ingest_diagnostic(error, database)
    return _with_next(_failure_lines("Import", _safe(reason), diagnostic), diagnostic)


_QA_DIAGNOSTICS: dict[tuple[str, str], _Diagnostic] = {
    ("configuration_error", "missing_credentials"): _Diagnostic(
        "Live question answering requires OpenAI credentials.",
        "OPENAI_API_KEY is required for live SQL generation.",
        (
            "Set OPENAI_API_KEY in your environment (for example: export OPENAI_API_KEY='<your-api-key>').",
        ),
    ),
    ("configuration_error", "database_open"): _Diagnostic(
        "The imported database could not be opened.",
        "An existing, accessible imported SQLite database is required.",
        ("Check --db, or import a supported workbook with sovtrace ingest first.",),
    ),
    **dict.fromkeys(
        (("configuration_error", code) for code in ("invalid_dataset", "dataset_changed")),
        _Diagnostic(
            "The database contract could not be validated.",
            next_steps=(
                "Use a valid imported database; review or reimport the source without bypassing metadata checks.",
            ),
        ),
    ),
    **{("provider_error", code): diagnostic for code, diagnostic in _PROVIDER.items()},
    **dict.fromkeys(
        (("invalid_model_response", code) for code in _INVALID_RESPONSE_CODES),
        _Diagnostic("A usable SQL proposal was not received.", "No replacement SQL was generated."),
    ),
    ("unsupported_question", "model_unsupported"): _Diagnostic(
        "The model reported an unsupported question.",
        "The model's explanation is not independently verified.",
        (_SUPPORTED_QUESTION,),
    ),
    ("unsupported_question", "question_size"): _Diagnostic(
        "Question length is outside the supported bounds.",
        next_steps=("Supply a question containing 1–8000 characters.",),
    ),
    ("unsupported_question", "threshold_sign"): _Diagnostic(
        "The currency threshold sign is ambiguous.",
        next_steps=("State an unambiguous signed currency threshold.",),
    ),
    ("unsupported_question", "threshold_precision"): _Diagnostic(
        "The currency threshold exceeds supported precision or range.",
        "Thresholds must be representable as signed int64 micro-USD.",
    ),
    ("unavailable_field", "absent_field"): _Diagnostic(
        "A requested semantic concept is unavailable in the imported schema.",
        next_steps=(
            "Ask about available supported concepts; unrelated columns cannot substitute for missing fields.",
        ),
    ),
    ("sql_validation_error", "unsupported_sql"): _Diagnostic(
        "Proposed SQL could not be validated.",
        "The proposal failed deterministic SQL validation. This does not necessarily mean "
        "the question itself is unsupported.",
        (
            "Try rephrasing the question using supported asset concepts: existence, count, TIV sum or bounded listing.",
        ),
    ),
    ("sql_validation_error", "plan_mismatch"): _Diagnostic(
        "Validated query plan could not be accepted.",
        "The validated plan was inconsistent with the required execution or verification context.",
    ),
    ("sql_validation_error", "threshold_operator"): _Diagnostic(
        "Proposed SQL could not be validated.",
        "Deterministic SQL validation found that the proposal did not preserve the question's explicit financial threshold inequality.",
        (
            "If rephrasing, keep the intended monetary inequality explicit; generated SQL must preserve it.",
        ),
    ),
    ("sql_validation_error", "threshold_scale"): _Diagnostic(
        "Proposed SQL could not be validated.",
        "Deterministic SQL validation found that the proposal's monetary literals did not match the question's exact micro-USD thresholds.",
        (
            "If rephrasing, retain the exact currency threshold; generated SQL must preserve its micro-USD value.",
        ),
    ),
    ("data_incomplete", "predicate_incomplete"): _Diagnostic(
        "Missing or invalid data prevents reliable filter membership.",
        next_steps=(
            "Review the source data needed by the filter; unknown values cannot establish membership.",
        ),
    ),
    ("data_incomplete", "valuation_incomplete"): _Diagnostic(
        "Required monetary values are incomplete.",
        next_steps=(
            "Review missing or invalid source valuations; unknown amounts cannot be treated as zero.",
        ),
    ),
    **dict.fromkeys(
        (
            ("resource_limit_exceeded", code)
            for code in ("deadline", "result_size", "sqlite_resource")
        ),
        _Diagnostic(
            "The query exceeded an established execution or result bound.",
            "No partial answer was returned.",
            (_NARROW,),
        ),
    ),
    ("evidence_limit_exceeded", "contributor_limit"): _Diagnostic(
        "Complete evidence limit exceeded.",
        "The answer was withheld because complete source evidence is required. "
        "Evidence is never truncated or sampled.",
        (_NARROW,),
    ),
    **dict.fromkeys(
        [
            ("evidence_incomplete", "missing_source"),
            ("evidence_mismatch", "reconciliation"),
            ("source_reference_invalid", "source_identity"),
            ("source_reference_invalid", "corrupt_source"),
        ]
        + [
            ("evidence_query_error", code)
            for code in (
                "evidence_deadline",
                "source_cell_limit",
                "variable_limit",
                "deadline",
                "result_size",
                "sqlite_resource",
                "attached_database",
                "sqlite_error",
            )
        ],
        _Diagnostic(
            "A reliable verified answer could not be established.",
            "Complete source evidence and independent reconciliation could not be established; the answer was withheld.",
        ),
    ),
    **dict.fromkeys(
        (
            ("query_execution_error", code)
            for code in (
                "snapshot_error",
                "attached_database",
                "sqlite_error",
                "connection_error",
                "population_mismatch",
                "invalid_count",
                "invalid_sum",
            )
        ),
        _Diagnostic("Query execution could not produce a verified answer."),
    ),
}


def _unavailable_field(failure: Failure) -> str | None:
    """Recognize only a complete validator message naming an existing semantic field."""
    if (failure.category, failure.code) != ("unavailable_field", "absent_field"):
        return None
    for field in SemanticField:
        if failure.message == f"Semantic field {field.value} is unavailable":
            return field.value
    return None


def _incomplete_predicate_fields(failure: Failure) -> tuple[str, ...] | None:
    """Accept only execution's nonempty, sorted, unique, comma-space field list."""
    if (failure.category, failure.code) != ("data_incomplete", "predicate_incomplete"):
        return None
    prefix, separator, names = failure.message.partition(": ")
    if prefix != "Missing or invalid values affect filter membership" or not separator:
        return None
    fields = tuple(names.split(", "))
    supported = {field.value for field in SemanticField}
    if any(field not in supported for field in fields):
        return None
    if fields != tuple(sorted(set(fields))):
        return None
    return fields


def render_failure_text(failure: Failure) -> str:
    """Use category/code pairs, typed limits and exactly recognized semantic fields.

    In particular, model_unsupported.message is an unverified model claim, and other
    messages can contain provider details or private source content. Canonical JSON
    and the original frozen Failure remain untouched.
    """
    diagnostic = _QA_DIAGNOSTICS.get(
        (failure.category, failure.code), _Diagnostic("Question answering could not complete.")
    )
    if (field := _unavailable_field(failure)) is not None:
        diagnostic = _Diagnostic(
            f"Requested field is unavailable: {_safe(field)}.",
            "The imported dataset does not expose this supported semantic concept.",
            diagnostic.next_steps,
        )
    if (fields := _incomplete_predicate_fields(failure)) is not None:
        diagnostic = _Diagnostic(
            diagnostic.summary,
            f"Affected fields: {_safe(', '.join(fields))}\n\n"
            "The application cannot establish which assets satisfy the filter.",
            diagnostic.next_steps,
        )
    lines = _failure_lines(
        "Question", f"{_safe(failure.category)} / {_safe(failure.code)}", diagnostic
    )
    if failure.evidence_limit is not None:
        lines.append(f"Evidence limit: {_count(failure.evidence_limit, 'logical asset')}")
    if failure.required_contributor_count is not None:
        lines.append(
            f"Required contributors: {_count(failure.required_contributor_count, 'logical asset')}"
        )
    lines.extend(["", "No verified answer was returned."])
    return _with_next(lines, diagnostic)
