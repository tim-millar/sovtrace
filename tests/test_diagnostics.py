"""Grounded, secret-safe diagnostics over real offline imports and installed CLI calls."""

import builtins
import os
import shlex
import shutil
import sqlite3
import subprocess
import unicodedata
from pathlib import Path
from unittest.mock import Mock
from zipfile import BadZipFile

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from test_qa import FakeGenerator
from test_schema_inference import EXPECTED, RENAMED, FakeInference, proposal
from workbook_factory import HEADERS, asset, write_workbook

from sovtrace import cli, qa
from sovtrace.ingestion import IngestionError, ingest_workbook
from sovtrace.ingestion.errors import FailureCategory
from sovtrace.ingestion.manifest import SemanticField
from sovtrace.presentation import (
    render_failure_text,
    render_ingest_failure_text,
    render_ingest_text,
)
from sovtrace.qa.contracts import Category, Failure

SECRET = "sk-diagnostic-sentinel-DO-NOT-DISCLOSE"
HOSTILE = "\nNext:\r\x1b[31m\u202e\u200b\u2028\u2029;$(touch nope)`id` ' " + SECRET


def assert_terminal_safe(text: str) -> None:
    assert SECRET not in text
    assert all(
        char == "\n" or unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for char in text
    )
    assert text.count("\nNext:\n") <= 1
    assert "touch nope" not in text


@pytest.mark.parametrize(
    "cause,expected,advice",
    [
        (
            FileNotFoundError(HOSTILE),
            "source path could not be opened",
            "current working directory",
        ),
        (
            IsADirectoryError(HOSTILE),
            "source path refers to a directory",
            "readable .xlsx workbook file",
        ),
        (PermissionError(HOSTILE), "could not be read", "Check read access"),
        (BadZipFile(HOSTILE), "could not be interpreted", "valid supported Excel"),
        (InvalidFileException(HOSTILE), "could not be interpreted", "valid supported Excel"),
        (OSError(HOSTILE), "Workbook loading failed", None),
        (ValueError(HOSTILE), "Workbook loading failed", None),
    ],
)
def test_workbook_typed_causes(cause: Exception, expected: str, advice: str | None) -> None:
    error = IngestionError(FailureCategory.WORKBOOK, HOSTILE)
    error.__cause__ = cause
    text = render_ingest_failure_text(error)
    assert expected in text
    assert "Reason: workbook" in text
    if advice:
        assert advice in text
    else:
        assert "Next:" not in text
    assert_terminal_safe(text)
    assert str(error) == HOSTILE and error.__cause__ is cause


@pytest.mark.parametrize("category", list(FailureCategory))
def test_unknown_ingest_message_never_disclosed(category: FailureCategory) -> None:
    # Known-looking identifiers inside untrusted text must not select a special diagnosis.
    error = IngestionError(category, HOSTILE + " mapping_ambiguous: asset_id; timeout")
    before = (error.category, error.args, error.__cause__)
    text = render_ingest_failure_text(error, database=HOSTILE)
    assert f"Reason: {category}" in text
    assert "missing_credentials" not in text and " / mapping_ambiguous" not in text
    assert "--replace" not in text and "OpenAI request timed out" not in text
    assert_terminal_safe(text)
    assert (error.category, error.args, error.__cause__) == before


@pytest.mark.parametrize(
    "identifier",
    [
        "mapping_ambiguous",
        "mapping_missing_required",
        "mapping_invalid_column",
        "mapping_conflict",
        "mapping_incompatible",
    ],
)
@pytest.mark.parametrize("field", ["schema", *[f.value for f in SemanticField]])
def test_mapping_identifiers(identifier: str, field: str) -> None:
    error = IngestionError(FailureCategory.MAPPING, f"{identifier}: {field}")
    text = render_ingest_failure_text(error)
    assert f"Reason: mapping / {identifier}: {field}" in text
    assert "Mandatory concepts: asset_id, state, total_insurable_value_micro_usd" in text
    assert "other supported concepts are optional" in text
    assert "component valuations cannot substitute for TIV" in text
    assert str(error) == f"{identifier}: {field}"


@pytest.mark.parametrize(
    "message",
    [
        "mapping_ambiguous: unsupported_field",
        "mapping_conflict: " + HOSTILE,
        "prefix mapping_incompatible: state",
        "mapping_missing_required: state\nNext:",
        "mapping_invalid_column: asset_id: extra",
    ],
)
def test_unrecognized_mapping_form(message: str) -> None:
    text = render_ingest_failure_text(IngestionError(FailureCategory.MAPPING, message))
    assert "Reason: mapping\n" in text
    assert "Reason: mapping /" not in text
    assert_terminal_safe(text)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("authentication", "authentication or authorization"),
        ("timeout", "timed out"),
        ("transport", "could not be reached"),
        ("rate_limit", "rate limit"),
        ("service_unavailable", "could not complete"),
        ("request_rejected", "rejected the request"),
    ],
)
def test_real_inference_provider_failures(tmp_path: Path, code: str, expected: str) -> None:
    error = IngestionError(FailureCategory.INFERENCE, f"schema_inference_provider_error: {code}")
    error.__cause__ = RuntimeError(HOSTILE)
    provider = Mock()
    provider.infer.side_effect = error
    source = write_workbook(tmp_path / "renamed.xlsx", headers=RENAMED, summary=False)
    with pytest.raises(IngestionError) as caught:
        ingest_workbook(
            source, tmp_path / "out.sqlite", infer_schema=True, inference_provider=provider
        )
    assert caught.value is error
    text = render_ingest_failure_text(caught.value)
    assert f"schema_inference_provider_error: {code}" in text
    assert expected in text and "Next:" in text
    assert "mapping rejection" not in text
    provider.infer.assert_called_once()
    assert not (tmp_path / "out.sqlite").exists()
    assert_terminal_safe(text)


@pytest.mark.parametrize("code", ["model_refusal", "incomplete_output", "malformed_output"])
def test_invalid_inference_response(code: str) -> None:
    text = render_ingest_failure_text(
        IngestionError(FailureCategory.INFERENCE, f"schema_inference_invalid_response: {code}")
    )
    assert f"schema_inference_invalid_response: {code}" in text
    assert "usable schema proposal was not received" in text
    assert "Next:" not in text


@pytest.mark.parametrize(
    "message",
    [
        "schema_inference_provider_error: timeout: " + SECRET,
        "schema_inference_provider_error: unknown",
        "prefix schema_inference_provider_error: timeout",
        "schema_inference_invalid_response: authentication",
    ],
)
def test_inference_identifier_forms_are_narrow(message: str) -> None:
    text = render_ingest_failure_text(IngestionError(FailureCategory.INFERENCE, message))
    assert text.endswith("Reason: schema-inference") and "Next:" not in text
    assert_terminal_safe(text)


def test_metadata_limit() -> None:
    text = render_ingest_failure_text(
        IngestionError(FailureCategory.INFERENCE, "schema_inference_metadata_limit")
    )
    assert "schema_inference_metadata_limit" in text
    assert "128 columns" in text and "256 characters" in text and "32,768" in text


def test_destination_requires_exact_invocation_context() -> None:
    path = Path("space ' quote;$(touch nope)\n" + SECRET + ".sqlite")
    error = IngestionError(FailureCategory.PERSISTENCE, f"Output exists: {path}; use --replace")
    text = render_ingest_failure_text(error, database=path)
    assert "Destination database already exists" in text
    assert "Choose another --db destination" in text and "use --replace" in text
    assert_terminal_safe(text)
    for db in [None, "different.sqlite"]:
        fallback = render_ingest_failure_text(error, database=db)
        assert "SQLite publication could not complete" in fallback
        assert "--replace" not in fallback and "Next:" not in fallback
    error.__cause__ = PermissionError(HOSTILE)
    fallback = render_ingest_failure_text(error)
    assert "permission" not in fallback.lower() and "unchanged" not in fallback.lower()


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("zero", "No candidate SOV header"),
        ("boundary", "supported layout"),
        ("merge", "supported layout"),
        ("missing", "Required concept is absent: asset_id"),
        ("ambiguous", "Ambiguous mapping: state"),
        ("identifier", "missing or invalid identifier"),
        ("precision", "precision exceeds"),
        ("range", "signed 64-bit"),
        ("duplicate", "could not be verified as identical"),
    ],
)
def test_real_ingestion_errors(tmp_path: Path, variant: str, expected: str) -> None:
    headers = HEADERS.copy()
    rows = [asset()]
    if variant == "missing":
        headers[0] = "Unknown identity"
    if variant == "ambiguous":
        headers[2] = "State"
    if variant == "identifier":
        rows = [asset(123)]
    if variant == "precision":
        rows = [asset(amount="1e-1001")]
    if variant == "range":
        rows = [asset(amount="100000000000000")]
    if variant == "duplicate":
        rows = [asset(), asset(amount="11")]
    source = write_workbook(tmp_path / "source.xlsx", headers=headers, rows=rows, summary=False)
    if variant == "zero":
        workbook = Workbook()
        workbook.save(source)
        workbook.close()
    if variant in {"boundary", "merge"}:
        workbook = load_workbook(source)
        sheet = workbook.active
        assert sheet is not None
        if variant == "boundary":
            sheet["A4"] = "later record"
        else:
            sheet.merge_cells("A2:B2")
        workbook.save(source)
        workbook.close()
    with pytest.raises(IngestionError) as caught:
        ingest_workbook(source, tmp_path / "out.sqlite")
    error = caught.value
    before = (error.category, str(error))
    text = render_ingest_failure_text(error)
    assert expected in text
    assert f"Reason: {error.category}" in text
    assert (error.category, str(error)) == before
    assert not (tmp_path / "out.sqlite").exists()
    if error.category == FailureCategory.STRUCTURE:
        assert "header in rows 1–10" in text and "contiguous asset records" in text
        assert "inference" not in text.lower() and "empty" not in text.lower()


@pytest.mark.parametrize(
    "variant", ["missing", "incompatible", "conflict", "invalid_column", "ambiguous"]
)
def test_real_mapping_rejection(tmp_path: Path, variant: str) -> None:
    mapping = proposal({**EXPECTED, "state": None}) if variant == "missing" else proposal()
    labels = RENAMED.copy()
    if variant == "incompatible":
        labels[4] = "Building Value"
    elif variant == "conflict":
        mapping = proposal({**EXPECTED, "city": 4})
    elif variant == "invalid_column":
        mapping = proposal({**EXPECTED, "city": 999})
    elif variant == "ambiguous":
        data = mapping.model_dump()
        data["city"] = {"status": "ambiguous", "column": None, "candidates": [2, 3]}
        mapping = type(mapping).model_validate(data)
    fake = FakeInference(mapping)
    source = write_workbook(tmp_path / "schema.xlsx", headers=labels, summary=False)
    with pytest.raises(IngestionError) as caught:
        ingest_workbook(source, tmp_path / "out.sqlite", infer_schema=True, inference_provider=fake)
    expected = {
        "missing": "mapping_missing_required",
        "incompatible": "mapping_incompatible",
        "conflict": "mapping_conflict",
        "invalid_column": "mapping_invalid_column",
        "ambiguous": "mapping_ambiguous",
    }[variant]
    assert expected in str(caught.value)
    assert expected in render_ingest_failure_text(caught.value)
    assert len(fake.requests) == 1


def test_real_technical_fallback_still_succeeds(tmp_path: Path) -> None:
    headers = HEADERS.copy()
    headers[5] = RENAMED[5]
    fake = FakeInference(fail=True)
    source = write_workbook(tmp_path / "fallback.xlsx", headers=headers)
    report = ingest_workbook(
        source, tmp_path / "db.sqlite", infer_schema=True, inference_provider=fake
    )
    assert report["schema_inference"] == {
        "status": "fallback",
        "model": None,
        "inferred_fields": [],
    }
    assert "Deterministic fallback after technical inference failure" in render_ingest_text(
        report, "db.sqlite"
    )
    assert len(fake.requests) == 1


QA_CASES: list[tuple[Category, str, str]] = [
    ("configuration_error", "missing_credentials", "Live question answering requires"),
    ("configuration_error", "database_open", "existing, accessible imported SQLite"),
    ("configuration_error", "invalid_dataset", "database contract could not be validated"),
    ("configuration_error", "dataset_changed", "database contract could not be validated"),
    ("provider_error", "authentication", "authentication or authorization"),
    ("provider_error", "timeout", "timed out"),
    ("provider_error", "transport", "could not be reached"),
    ("provider_error", "rate_limit", "rate limit"),
    ("provider_error", "service_unavailable", "could not complete the request"),
    ("provider_error", "request_rejected", "rejected the request"),
    ("invalid_model_response", "malformed_output", "usable SQL proposal was not received"),
    ("invalid_model_response", "model_refusal", "usable SQL proposal was not received"),
    ("invalid_model_response", "incomplete_output", "usable SQL proposal was not received"),
    ("unsupported_question", "model_unsupported", "explanation is not independently verified"),
    ("unavailable_field", "absent_field", "semantic concept is unavailable"),
    ("sql_validation_error", "unsupported_sql", "deterministic SQL validation"),
    ("sql_validation_error", "threshold_operator", "explicit financial threshold inequality"),
    ("sql_validation_error", "threshold_scale", "exact micro-USD thresholds"),
    ("data_incomplete", "predicate_incomplete", "reliable filter membership"),
    ("data_incomplete", "valuation_incomplete", "monetary values are incomplete"),
    ("resource_limit_exceeded", "deadline", "established execution or result bound"),
    ("resource_limit_exceeded", "result_size", "established execution or result bound"),
    ("evidence_limit_exceeded", "contributor_limit", "Complete evidence limit exceeded"),
    ("evidence_incomplete", "missing_source", "answer was withheld"),
    ("evidence_mismatch", "reconciliation", "answer was withheld"),
    ("evidence_query_error", "evidence_deadline", "answer was withheld"),
    ("source_reference_invalid", "source_identity", "answer was withheld"),
    ("source_reference_invalid", "corrupt_source", "answer was withheld"),
    ("query_execution_error", "sqlite_error", "could not produce a verified answer"),
]


@pytest.mark.parametrize("category,code,expected", QA_CASES)
def test_qa_diagnostics(category: Category, code: str, expected: str) -> None:
    failure = Failure(
        category=category, code=code, message=HOSTILE + " SELECT private FROM secrets"
    )
    before = failure.model_dump_json(indent=2)
    text = render_failure_text(failure)
    assert expected in text
    assert f"Reason: {category} / {code}" in text
    assert "No verified answer was returned." in text
    assert "SELECT" not in text and "Answer:" not in text
    assert_terminal_safe(text)
    assert failure.model_dump_json(indent=2) == before


@pytest.mark.parametrize("category", sorted({case[0] for case in QA_CASES}))
def test_qa_unknown_codes(category: Category) -> None:
    failure = Failure(category=category, code="future_code\nNext:\x1b[31m\u202e", message=HOSTILE)
    text = render_failure_text(failure)
    assert f"Reason: {category} / future_code\\nNext:\\x1b[31m\\u202e" in text
    assert "Question answering could not complete" in text
    assert "\nNext:\n" not in text
    assert_terminal_safe(text)


def test_model_claim_is_not_diagnosis() -> None:
    failure = Failure(
        category="unsupported_question",
        code="model_unsupported",
        message="Amount missing because the shell expanded $10. " + HOSTILE,
    )
    text = render_failure_text(failure)
    assert "shell" not in text and "Amount missing" not in text
    assert "not independently verified" in text
    assert "existence, count, TIV sum or bounded listing" in text


def test_evidence_limit_metadata() -> None:
    failure = Failure(
        category="evidence_limit_exceeded",
        code="contributor_limit",
        message=HOSTILE,
        evidence_limit=500,
        required_contributor_count=501,
    )
    text = render_failure_text(failure)
    assert "Evidence limit: 500 logical assets" in text
    assert "Required contributors: 501 logical assets" in text
    assert "never truncated or sampled" in text
    assert "Narrow the question" in text


def test_ingestion_renderer_is_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError("No renderer I/O"))
    for owner, name in [
        (builtins, "open"),
        (Path, "open"),
        (Path, "exists"),
        (Path, "resolve"),
        (sqlite3, "connect"),
        (qa, "ask"),
        (cli, "ingest_workbook"),
    ]:
        monkeypatch.setattr(owner, name, forbidden)
    for category in FailureCategory:
        error = IngestionError(category, HOSTILE)
        assert render_ingest_failure_text(error) == render_ingest_failure_text(error)
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "path", ["line\nbreak.sqlite", "return\r.sqlite", "ansi\x1b[31m.sqlite", "format\u202e.sqlite"]
)
def test_success_followup_controls_use_explanation(tmp_path: Path, path: str) -> None:
    source = write_workbook(tmp_path / "known.xlsx")
    report = ingest_workbook(source, tmp_path / "known.sqlite")
    text = render_ingest_text(report, path)
    assert "uv run --locked sovtrace ask --db" not in text
    assert "Ask a question with sovtrace ask" in text
    assert all(c == "\n" or unicodedata.category(c) not in {"Cc", "Cf", "Zl", "Zp"} for c in text)


@pytest.fixture
def installed_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Actual installed entry point; any network connection makes the test fail visibly."""
    executable = shutil.which("sovtrace")
    assert executable
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import socket\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise AssertionError('Unexpected network connection')\n"
        "socket.socket.connect = forbidden\n"
        "socket.create_connection = forbidden\n"
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = {**os.environ, "PYTHONPATH": str(guard)}

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=env,
        )

    return run


@pytest.mark.parametrize("format_name", ["text", "json"])
@pytest.mark.parametrize(
    "variant", ["missing", "extension", "invalid", "exists", "layout", "credentials", "replace"]
)
def test_installed_ingest_failures(
    tmp_path: Path, installed_cli, variant: str, format_name: str
) -> None:
    source = tmp_path / "source.xlsx"
    db = tmp_path / "destination.sqlite"
    expected = "source path could not be opened"
    flags = []
    if variant == "extension":
        source = tmp_path / "source.csv"
        source.write_text("not xlsx")
        expected = "Unsupported source format"
    elif variant == "invalid":
        source.write_text("invalid container " + SECRET)
        expected = "could not be interpreted"
    elif variant == "exists":
        db.write_text("protected " + SECRET)
        expected = "Destination database already exists"
    elif variant == "layout":
        workbook = Workbook()
        workbook.save(source)
        workbook.close()
        expected = "No candidate SOV header"
    elif variant == "credentials":
        write_workbook(source, headers=RENAMED, summary=False)
        expected = "Schema inference requires OpenAI credentials"
    elif variant == "replace":
        write_workbook(source)
        ingest_workbook(source, db)
        source = write_workbook(tmp_path / "rejected.xlsx", rows=[asset(123)])
        flags = ["--replace"]
        expected = "missing or invalid identifier"
    before = db.read_bytes() if db.exists() else None
    result = installed_cli("ingest", str(source), "--db", str(db), "--format", format_name, *flags)
    assert result.returncode == 1
    assert result.stdout == "" and expected in result.stderr
    assert "Reason:" in result.stderr and "Next:" in result.stderr
    assert "Traceback" not in result.stderr and "Unexpected network" not in result.stderr
    assert_terminal_safe(result.stderr)
    if before is not None:
        assert db.read_bytes() == before
    else:
        assert not db.exists()
    if variant == "credentials":
        assert "OPENAI_API_KEY was unavailable" in result.stderr
        assert "metadata transmission" in result.stderr
        assert "may fail on unfamiliar headings" in result.stderr


@pytest.mark.parametrize("format_name", ["text", "json"])
@pytest.mark.parametrize("database", ["missing", "invalid", "credentials"])
def test_installed_ask_failures(
    tmp_path: Path, installed_cli, database: str, format_name: str
) -> None:
    db = tmp_path / "db.sqlite"
    code = "database_open"
    if database == "invalid":
        sqlite3.connect(db).close()
        code = "invalid_dataset"
    elif database == "credentials":
        source = write_workbook(tmp_path / "source.xlsx")
        ingest_workbook(source, db)
        code = "missing_credentials"
    result = installed_cli("ask", "--db", str(db), "How many assets?", "--format", format_name)
    assert result.returncode == 1
    if format_name == "json":
        assert result.stderr == ""
        failure = Failure.model_validate_json(result.stdout)
        assert failure.category == "configuration_error" and failure.code == code
        assert result.stdout == failure.model_dump_json(indent=2) + "\n"
    else:
        assert result.stdout == ""
        assert f"Reason: configuration_error / {code}" in result.stderr
        assert "No verified answer was returned" in result.stderr
    assert "Traceback" not in result.stderr and "Unexpected network" not in result.stderr
    assert_terminal_safe(result.stdout + result.stderr)


@pytest.mark.parametrize("command", [None, "ingest", "ask"])
def test_installed_help(installed_cli, command: str | None) -> None:
    result = installed_cli(*([command] if command else []), "--help")
    assert result.returncode == 0 and result.stderr == ""
    text = " ".join(result.stdout.split())
    if command is None:
        assert "Excel SOV into SQLite" in text and "verified answer" in text
        assert "sovtrace ingest" in text and "sovtrace ask" in text
    elif command == "ingest":
        for expected in [
            "local .xlsx",
            "--db",
            "--replace",
            "text (default)",
            "json",
            "Exact deterministic",
            "first",
            "bounded column metadata",
            "OpenAI",
            "OPENAI_API_KEY",
            "type-count",
            "sensitive information",
            "appropriate authorization",
            "--no-infer-schema",
            "--infer-schema",
            "does not force a model request",
            "compatibility equivalent",
            "metadata transmission",
            "Headings are sent verbatim",
            "monetary values embedded in them",
            "not separately serialized as source records",
            "required or optional concepts",
            "Python ingestion API defaults off",
            "zero provider retention",
        ]:
            assert expected in text
    else:
        for expected in [
            "existing imported SQLite",
            "verbatim question and semantic schema",
            "OPENAI_API_KEY",
            "Text is the default",
            "--format json",
            "original-source evidence",
            "Unsupported requests",
            "unverifiable results",
            "single quotes prevent shell expansion of $10",
        ]:
            assert expected in text
        lines = result.stdout.splitlines()
        command_line = next(i for i, line in enumerate(lines) if line.startswith("  uv run"))
        example = "\n".join(lines[command_line:])
        assert "\\\n" in example
        assert shlex.split(example.replace("\\\n", "")) == [
            "uv",
            "run",
            "--locked",
            "sovtrace",
            "ask",
            "--db",
            "data/generated/sov.sqlite",
            "How many assets exceed $10 million?",
        ]


@pytest.mark.parametrize("variant", ["path", "worksheet"])
def test_installed_hostile_source_context(tmp_path: Path, installed_cli, variant: str) -> None:
    source = tmp_path / ("missing" + HOSTILE + ".xlsx")
    if variant == "worksheet":
        source = write_workbook(tmp_path / "private.xlsx", rows=[asset(123)])
        workbook = load_workbook(source)
        sheet = workbook.active
        assert sheet is not None
        sheet.title = "private\nNext\r\u202e" + SECRET[:10]
        workbook.save(source)
        workbook.close()
    result = installed_cli("ingest", str(source), "--db", str(tmp_path / "db.sqlite"))
    assert result.returncode == 1 and result.stdout == ""
    assert "Import failed:" in result.stderr and "Traceback" not in result.stderr
    assert "private" not in result.stderr
    assert SECRET[:10] not in result.stderr
    assert_terminal_safe(result.stderr)


def test_category_takes_precedence_over_message_and_cause() -> None:
    error = IngestionError(
        FailureCategory.PERSISTENCE, "schema_inference_provider_error: missing_credentials"
    )
    error.__cause__ = FileNotFoundError(HOSTILE)
    text = render_ingest_failure_text(error, database="out.sqlite")
    assert text == "Import failed: SQLite publication could not complete.\n\nReason: persistence"


def test_fixed_message_takes_precedence_over_typed_cause() -> None:
    error = IngestionError(FailureCategory.WORKBOOK, "Only .xlsx workbooks are supported")
    error.__cause__ = FileNotFoundError(HOSTILE)
    text = render_ingest_failure_text(error)
    assert "Unsupported source format" in text and "current working directory" not in text


# Field context is presented only after validating the entire maintained message form.
_ABSENT = ("unavailable_field", "absent_field")
_PREDICATE = ("data_incomplete", "predicate_incomplete")
_PREDICATE_PREFIX = "Missing or invalid values affect filter membership: "


def _render_preserving_contract(failure: Failure) -> str:
    original = failure.model_copy(deep=True)
    original_json = failure.model_dump_json(indent=2).encode()
    text = render_failure_text(failure)
    assert failure == original
    assert failure.message == original.message
    assert failure.category == original.category and failure.code == original.code
    assert failure.model_dump_json(indent=2).encode() == original_json
    assert "No verified answer was returned." in text
    assert_terminal_safe(text)
    return text


@pytest.mark.parametrize("field", [field.value for field in SemanticField])
def test_unavailable_field_context(field: str) -> None:
    failure = Failure(
        category="unavailable_field",
        code="absent_field",
        message=f"Semantic field {field} is unavailable",
    )
    text = _render_preserving_contract(failure)
    assert text.startswith(f"Question failed: Requested field is unavailable: {field}.\n")
    assert "Reason: unavailable_field / absent_field" in text
    assert "The imported dataset does not expose this supported semantic concept." in text
    assert "unrelated columns cannot substitute for missing fields" in text


@pytest.mark.parametrize(
    "names",
    [
        "asset_id",
        "property_name",
        "city",
        "state",
        "total_insurable_value_micro_usd",
        "construction_type",
        "city, state",
        "construction_type, property_name, state",
        "asset_id, city, construction_type, property_name, state, total_insurable_value_micro_usd",
    ],
)
def test_incomplete_predicate_field_context(names: str) -> None:
    failure = Failure(
        category="data_incomplete", code="predicate_incomplete", message=_PREDICATE_PREFIX + names
    )
    text = _render_preserving_contract(failure)
    assert text.startswith(
        "Question failed: Missing or invalid data prevents reliable filter membership."
    )
    assert "Reason: data_incomplete / predicate_incomplete" in text
    assert f"Affected fields: {names}\n" in text
    assert "The application cannot establish which assets satisfy the filter." in text
    assert "unknown values cannot establish membership" in text
    assert text == render_failure_text(failure)


@pytest.mark.parametrize(
    "message",
    [
        "Semantic field unknown_field is unavailable",
        "Semantic field  is unavailable",
        "Semantic field city, state is unavailable",
        "Semantic field city  is unavailable",
        "Semantic field City is unavailable",
        "Semantic field city is unavailable.",
        "prefix Semantic field city is unavailable",
        "Semantic field city is unavailable suffix",
        " Semantic field city is unavailable",
        "Semantic field city is unavailable ",
        "Semantic field city is unavailable\n",
        "Semantic field city\r is unavailable",
        "Semantic field city\x1b[31m is unavailable",
        "Semantic field city\u202e is unavailable",
        "Semantic field city\u200b is unavailable",
        "Semantic field city\u2028 is unavailable",
        "Semantic field city\u2029 is unavailable",
        "Semantic field city" + SECRET + " is unavailable",
        "Semantic field " + HOSTILE + " is unavailable",
    ],
)
def test_unavailable_field_untrusted_message_falls_back(message: str) -> None:
    failure = Failure(category="unavailable_field", code="absent_field", message=message)
    text = _render_preserving_contract(failure)
    generic = render_failure_text(failure.model_copy(update={"message": "unrecognized"}))
    assert text == generic
    assert "Requested field is unavailable:" not in text
    assert "semantic concept is unavailable in the imported schema" in text


@pytest.mark.parametrize(
    "names",
    [
        "",
        "unknown_field",
        "city, unknown_field",
        "city, ",
        ", city",
        "city,, state",
        "city,state",
        "city,  state",
        "city , state",
        "city; state",
        "city, city",
        "city, state, city",
        "state, city",
        "state, property_name, city",
        "City",
        " city",
        "city ",
        "city\n",
        "city, state\r",
        "city, \x1b[31mstate",
        "city\u202e",
        "city\u200b",
        "city\u2028",
        "city\u2029",
        "city, " + SECRET,
        "city" + HOSTILE,
    ],
)
def test_predicate_fields_untrusted_list_falls_back(names: str) -> None:
    failure = Failure(
        category="data_incomplete", code="predicate_incomplete", message=_PREDICATE_PREFIX + names
    )
    text = _render_preserving_contract(failure)
    assert text == render_failure_text(failure.model_copy(update={"message": "unrecognized"}))
    assert "Affected fields:" not in text


@pytest.mark.parametrize(
    "message",
    [
        "prefix " + _PREDICATE_PREFIX + "city",
        _PREDICATE_PREFIX + "city suffix",
        "Missing or invalid values affect filter membership:city",
        "Missing or invalid values affect filter membership : city",
        "Missing or invalid values affect filter membership:\tcity",
    ],
)
def test_predicate_fields_malformed_message_falls_back(message: str) -> None:
    failure = Failure(category="data_incomplete", code="predicate_incomplete", message=message)
    text = _render_preserving_contract(failure)
    assert text == render_failure_text(failure.model_copy(update={"message": "unrecognized"}))
    assert "Affected fields:" not in text


@pytest.mark.parametrize(
    "category,code",
    [
        ("unavailable_field", "unknown_code"),
        ("data_incomplete", "absent_field"),
        ("unavailable_field", "predicate_incomplete"),
        ("data_incomplete", "unknown_code"),
        ("data_incomplete", "valuation_incomplete"),
        ("unsupported_question", "model_unsupported"),
    ],
)
@pytest.mark.parametrize(
    "message",
    [
        "Semantic field construction_type is unavailable",
        _PREDICATE_PREFIX + "city, state",
    ],
)
def test_field_context_requires_exact_category_and_code(
    category: Category, code: str, message: str
) -> None:
    failure = Failure(category=category, code=code, message=message)
    text = _render_preserving_contract(failure)
    assert text == render_failure_text(failure.model_copy(update={"message": "unrecognized"}))
    assert "Requested field is unavailable:" not in text and "Affected fields:" not in text
    if code == "valuation_incomplete":
        assert "Required monetary values are incomplete." in text


@pytest.mark.parametrize("variant", ["absent", "single_predicate", "multiple_predicates"])
def test_real_qa_field_diagnostics(tmp_path: Path, variant: str) -> None:
    headers = HEADERS.copy()
    rows = [asset()]
    if variant == "absent":
        headers[5] = "Not a supported construction heading"
        sql = "SELECT asset_id, construction_type FROM assets"
        generator = FakeGenerator(sql, "list")
        expected_message = "Semantic field construction_type is unavailable"
    else:
        rows[0][2] = None
        if variant == "multiple_predicates":
            rows[0][3] = None
        sql = "SELECT COUNT(*) FROM assets WHERE city = 'Test City' AND state = 'AZ'"
        generator = FakeGenerator(sql)
        expected_message = _PREDICATE_PREFIX + (
            "city, state" if variant == "multiple_predicates" else "city"
        )
    source = write_workbook(tmp_path / "source.xlsx", rows=rows, headers=headers)
    database = tmp_path / "imported.sqlite"
    ingest_workbook(source, database)
    failure = qa.ask(database, "Which assets match?", generator)
    assert isinstance(failure, Failure) and failure.message == expected_message
    assert (failure.category, failure.code) == (_ABSENT if variant == "absent" else _PREDICATE)
    text = _render_preserving_contract(failure)
    if variant == "absent":
        assert "Requested field is unavailable: construction_type." in text
    else:
        assert "Affected fields: " + expected_message.removeprefix(_PREDICATE_PREFIX) + "\n" in text
    assert len(generator.calls) == 1


@pytest.mark.parametrize(
    "code,detail,advice",
    [
        (
            "unsupported_sql",
            "does not necessarily mean the question itself is unsupported",
            "Try rephrasing the question using supported asset concepts",
        ),
        (
            "threshold_operator",
            "explicit financial threshold inequality",
            "keep the intended monetary inequality explicit; generated SQL must preserve it",
        ),
        (
            "threshold_scale",
            "exact micro-USD thresholds",
            "retain the exact currency threshold; generated SQL must preserve its micro-USD value",
        ),
        ("plan_mismatch", "inconsistent with the required execution or verification context", None),
    ],
)
def test_sql_validation_guidance(code: str, detail: str, advice: str | None) -> None:
    failure = Failure(
        category="sql_validation_error",
        code=code,
        message="SELECT rejected_private_sql FROM secrets " + HOSTILE,
    )
    text = _render_preserving_contract(failure)
    assert f"Reason: sql_validation_error / {code}" in text
    assert detail in text and "rejected_private_sql" not in text and "SELECT" not in text
    assert "question is unsupported" not in text and "unsupported operator" not in text
    assert "will succeed" not in text and "guarantee" not in text
    assert "Try an existence" not in text
    if advice:
        assert advice in text
    else:
        assert text.startswith("Question failed: Validated query plan could not be accepted.")
        assert "Next:" not in text and "rephrasing" not in text


@pytest.mark.parametrize(
    "category,code,message",
    [
        ("unavailable_field", "absent_field", "Semantic field construction_type is unavailable"),
        ("data_incomplete", "predicate_incomplete", _PREDICATE_PREFIX + "city, state"),
        *[
            ("sql_validation_error", code, HOSTILE)
            for code in (
                "unsupported_sql",
                "threshold_operator",
                "threshold_scale",
                "plan_mismatch",
            )
        ],
    ],
)
@pytest.mark.parametrize("format_name", ["text", "json"])
def test_amended_qa_failure_streams_and_contracts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    category: Category,
    code: str,
    message: str,
    format_name: str,
) -> None:
    failure = Failure(category=category, code=code, message=message)
    original_json = failure.model_dump_json(indent=2).encode()
    service = Mock(return_value=failure)
    monkeypatch.setattr(qa, "ask", service)
    with pytest.raises(SystemExit) as caught:
        cli.main(["ask", "--db", "db.sqlite", "How many assets?", "--format", format_name])
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert failure.model_dump_json(indent=2).encode() == original_json
    if format_name == "json":
        assert captured.out.encode() == original_json + b"\n" and captured.err == ""
    else:
        assert captured.out == "" and captured.err == _render_preserving_contract(failure) + "\n"
    service.assert_called_once()


@pytest.mark.parametrize("format_name", ["text", "json"])
def test_installed_directory_source_failure(
    tmp_path: Path, installed_cli, format_name: str
) -> None:
    source = tmp_path / ("untrusted directory" + HOSTILE + ".xlsx")
    source.mkdir()
    database = tmp_path / "unpublished.sqlite"
    result = installed_cli("ingest", str(source), "--db", str(database), "--format", format_name)
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == (
        "Import failed: The source path refers to a directory.\n\nReason: workbook\n\nNext:\n"
        "  Supply the path to a readable .xlsx workbook file, not a directory.\n"
    )
    assert "Traceback" not in result.stderr and "Unexpected network" not in result.stderr
    assert "untrusted directory" not in result.stderr and str(source) not in result.stderr
    assert not database.exists()
    assert_terminal_safe(result.stderr)


@pytest.mark.parametrize(
    "category,code,message",
    [
        ("unavailable_field", "absent_field", "Semantic field construction_type is unavailable"),
        ("data_incomplete", "predicate_incomplete", _PREDICATE_PREFIX + "city, state"),
    ],
)
def test_recognized_field_rendering_is_pure(
    monkeypatch: pytest.MonkeyPatch,
    category: Category,
    code: str,
    message: str,
) -> None:
    forbidden = Mock(side_effect=AssertionError("No diagnostic I/O or service calls"))
    for owner, name in [
        (builtins, "open"),
        (Path, "open"),
        (Path, "exists"),
        (Path, "resolve"),
        (sqlite3, "connect"),
        (qa, "ask"),
        (cli, "ingest_workbook"),
    ]:
        monkeypatch.setattr(owner, name, forbidden)
    failure = Failure(category=category, code=code, message=message)
    assert _render_preserving_contract(failure) == render_failure_text(failure)
    forbidden.assert_not_called()


@pytest.mark.parametrize("unresolved", ["required", "optional"])
def test_verbatim_sensitive_heading_boundary_and_inference_defaults(
    tmp_path, monkeypatch, capsys, unresolved
):
    import json

    import httpx
    from test_qa_provider import client_for, response_body

    from sovtrace.ingestion.provider import OpenAISchemaInferenceProvider

    labels = HEADERS.copy()
    labels[0 if unresolved == "required" else 5] = RENAMED[0 if unresolved == "required" else 5]
    heading = "Artificial Mira Test owes $4200; =SUM(A2:A3); /artificial/example.xlsx"
    labels[10] = heading
    row = asset("SOURCE_RECORD_ONLY_SENTINEL", "12345678.876543")
    row[1] = "SOURCE_CELL_ONLY_SENTINEL"
    source = write_workbook(
        tmp_path / "artificial-input.xlsx", headers=labels, rows=[row], summary=False
    )
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body(text=proposal().model_dump_json()))

    with client_for(transport) as client:
        constructor = Mock(return_value=OpenAISchemaInferenceProvider(client=client))
        monkeypatch.setattr(
            "sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", constructor
        )
        cli.main(
            ["ingest", str(source), "--db", str(tmp_path / "default.sqlite"), "--format", "json"]
        )
        assert json.loads(capsys.readouterr().out)["schema_inference"]["status"] == "applied"
        constructor.assert_called_once_with()
        assert len(requests) == 1
        body = requests[0]
        columns = json.loads(body["input"][0]["content"])["columns"]
        assert [c["label"] for c in columns] == labels
        assert columns[10]["label"] == heading
        assert set(columns[10]) == {"index", "letter", "label", "profile"}
        serialized = json.dumps(body)
        for sentinel in (
            "SOURCE_RECORD_ONLY_SENTINEL",
            "SOURCE_CELL_ONLY_SENTINEL",
            "12345678.876543",
            source.name,
        ):
            assert sentinel not in serialized
        assert body["store"] is False
        assert "exact recognized source label" in body["instructions"]
        assert "Stage A" not in body["instructions"]
        constructor.reset_mock()
        # Python ingestion defaults off; a required renamed label may reject.
        try:
            report = ingest_workbook(source, tmp_path / "api-off.sqlite")
            inference = report["schema_inference"]
            assert isinstance(inference, dict) and inference["status"] == "disabled"
            assert unresolved == "optional"
        except IngestionError:
            assert unresolved == "required"
        constructor.assert_not_called()
        assert len(requests) == 1
        arguments = [
            "ingest",
            str(source),
            "--db",
            str(tmp_path / "cli-off.sqlite"),
            "--no-infer-schema",
            "--format",
            "json",
        ]
        if unresolved == "required":
            with pytest.raises(SystemExit) as exc:
                cli.main(arguments)
            assert exc.value.code == 1
            assert capsys.readouterr().out == ""
        else:
            cli.main(arguments)
            assert json.loads(capsys.readouterr().out)["schema_inference"]["status"] == "disabled"
        constructor.assert_not_called()
        assert len(requests) == 1
