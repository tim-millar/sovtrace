"""Credential-free presentation checks over real reports and typed QA results."""

import builtins
import json
import shlex
import sqlite3
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_qa import FakeGenerator
from test_qa import db as db
from workbook_factory import HEADERS, asset, write_workbook

from sovtrace import cli, qa
from sovtrace.ingestion import IngestionError, ingest_workbook
from sovtrace.ingestion.errors import FailureCategory
from sovtrace.presentation import (
    render_answer_text,
    render_failure_text,
    render_ingest_failure_text,
    render_ingest_text,
)
from sovtrace.qa.contracts import (
    TIV,
    AssetRecord,
    CountValue,
    ExistsValue,
    Failure,
    ListValue,
    Success,
    SumValue,
)


@pytest.fixture
def report(tmp_path: Path) -> dict:
    source = write_workbook(tmp_path / "report.xlsx", rows=[asset(), asset()])
    return ingest_workbook(source, tmp_path / "report.sqlite", infer_schema=True)


@pytest.fixture
def success(db: Path) -> Success:
    result = qa.ask(
        db,
        "What is the total TIV in Arizona?",
        FakeGenerator(
            "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'AZ'", "sum"
        ),
    )
    assert isinstance(result, Success)
    return result


@pytest.mark.parametrize(
    "status,expected",
    [
        ("disabled", "Deterministic mapping; model-assisted inference disabled"),
        ("not_needed", "Deterministic mapping (no model needed)"),
        ("applied", "Model-assisted mapping accepted and validated"),
        ("fallback", "Deterministic fallback after technical inference failure"),
    ],
)
@pytest.mark.parametrize("model", [None, "synthetic-model-id"])
def test_ingest_inference(report: dict, status: str, expected: str, model: str | None) -> None:
    report["schema_inference"] = {"status": status, "model": model, "inferred_fields": []}
    text = render_ingest_text(report, "destination.sqlite")
    assert f"Schema: {expected}" in text
    assert ("synthetic-model-id" in text) == (
        model is not None and status in {"applied", "fallback"}
    )
    assert "Imported: report.xlsx" in text
    assert "Worksheet: Statement of Values" in text
    assert "Database: destination.sqlite" in text
    assert "Assets: 1 logical asset\n" in text
    assert "Source records: 2 original spreadsheet rows" in text
    assert "Verified duplicate groups: 1" in text
    assert "No missing or invalid mapped values" in text
    assert "OPENAI_API_KEY" in text
    assert "error" not in text.lower()
    assert "warning" not in text.lower()
    for code, count in report["diagnostic_summary"].items():
        assert f"{code}={count}" in text


@pytest.mark.parametrize(
    "path",
    [
        "data/generated/sov.sqlite",
        "space directory/a db.sqlite",
        "data/it's here.sqlite",
        "../relative/./db;$(touch nope)&`id`|*.sqlite",
    ],
)
def test_ingest_followup_shell_quoting(report: dict, path: str) -> None:
    text = render_ingest_text(report, Path(path))
    command = next(
        line for line in text.splitlines() if line.startswith("uv run --locked sovtrace ask --db")
    )
    assert command == (
        f"uv run --locked sovtrace ask --db {shlex.quote(str(Path(path)))} 'How many assets are in Arizona?'"
    )
    assert shlex.split(command) == [
        "uv",
        "run",
        "--locked",
        "sovtrace",
        "ask",
        "--db",
        str(Path(path)),
        "How many assets are in Arizona?",
    ]


@pytest.mark.parametrize(
    "logical,physical,expected_assets,expected_rows",
    [
        (0, 0, "0 logical assets", "0 original spreadsheet rows"),
        (1, 1, "1 logical asset", "1 original spreadsheet row"),
        (2, 3, "2 logical assets", "3 original spreadsheet rows"),
        (48, 96, "48 logical assets", "96 original spreadsheet rows"),
    ],
)
def test_ingest_count_labels(
    report: dict, logical: int, physical: int, expected_assets: str, expected_rows: str
) -> None:
    report.update(logical_assets=logical, physical_records=physical)
    text = render_ingest_text(report, "db.sqlite")
    assert f"Assets: {expected_assets}\n" in text
    assert f"Source records: {expected_rows}\n" in text


def test_ingest_singular_completeness(report: dict) -> None:
    report["field_completeness"]["city"].update(
        logical_non_null=0, logical_total=1, physical_non_null=0, physical_total=1
    )
    text = render_ingest_text(report, "db.sqlite")
    assert "city: 0/1 logical asset; 0/1 source record non-NULL\n" in text


def test_ingest_quality_from_real_report(tmp_path: Path) -> None:
    rows = [asset("missing", None), asset("invalid", "not money"), asset("complete")]
    rows[0][2] = None
    headers = HEADERS.copy()
    headers[5] = "Unavailable optional concept"
    source = write_workbook(tmp_path / "quality.xlsx", rows=rows, headers=headers)
    report: dict[str, Any] = ingest_workbook(source, tmp_path / "quality.sqlite")
    text = render_ingest_text(report, "quality.sqlite")
    assert "Missing or invalid mapped values" in text
    assert "city: 2/3 logical assets; 2/3 source records non-NULL" in text
    assert f"{TIV}: 1/3 logical assets; 1/3 source records non-NULL" in text
    assert "Unavailable concepts: construction_type" in text
    assert "construction_type:" not in text
    assert "Valuations complete: No" in text
    assert "--format json" in text
    for code, count in report["diagnostic_summary"].items():
        assert f"{code}={count}" in text
    # Logical and physical completeness must be reported separately, even with duplicates.
    report["field_completeness"]["city"].update(physical_total=4, physical_non_null=2)
    assert "city: 2/3 logical assets; 2/4 source records non-NULL" in render_ingest_text(
        report, "db"
    )


@pytest.mark.parametrize("value,answer", [(True, "Yes"), (False, "No")])
def test_exists(success: Success, value: bool, answer: str) -> None:
    result = success.model_copy(update={"result": ExistsValue(value=value)})
    assert render_answer_text(result).startswith(
        f"Question: {success.question}\nAnswer: {answer}\n"
    )


@pytest.mark.parametrize(
    "count,expected",
    [
        (0, "0 logical assets"),
        (1, "1 logical asset"),
        (2, "2 logical assets"),
        (234, "234 logical assets"),
    ],
)
def test_count(success: Success, count: int, expected: str) -> None:
    result = success.model_copy(update={"result": CountValue(value=count)})
    assert f"Answer: {expected}\n" in render_answer_text(result)


@pytest.mark.parametrize(
    "amount,expected",
    [
        (1234567890000, "$1,234,567.89"),
        (0, "$0.00"),
        (-1234567890000, "$-1,234,567.89"),
        (9223372036854775807, "$9,223,372,036,854.78"),
        (1004999, "$1.00"),
        (1005000, "$1.01"),
        (-1005000, "$-1.01"),
        (-1, "$-0.00"),
    ],
)
def test_exact_money(success: Success, amount: int, expected: str) -> None:
    result = success.model_copy(
        update={"result": SumValue(value=amount, outcome="value"), "display_value": None}
    )
    assert f"Answer: {expected} (USD)" in render_answer_text(result)
    assert isinstance(result.result, SumValue)
    assert result.result.value == amount


def test_authoritative_display_and_no_match(success: Success) -> None:
    result = success.model_copy(update={"display_value": "$30,000,000.00"})
    assert "Answer: $30,000,000.00 (USD)" in render_answer_text(result)
    no_match = success.model_copy(
        update={
            "result": SumValue(value=None, outcome="no_matching_records"),
            "display_value": "$0.00",
        }
    )
    text = render_answer_text(no_match)
    assert "Answer: No matching records; no monetary total is measured." in text
    assert "$0.00" not in text


@pytest.mark.parametrize(
    "fields",
    [
        ("asset_id",),
        ("asset_id", "city"),
        ("asset_id", TIV),
        ("asset_id", "property_name", "state"),
    ],
)
def test_listing_projection_and_order(success: Success, fields: tuple[str, ...]) -> None:
    records = (
        AssetRecord(asset_id="z", city=None, total_insurable_value_micro_usd=1005000),
        AssetRecord(asset_id="a", city="NULL", total_insurable_value_micro_usd=-1005000),
    )
    result = success.model_copy(
        update={"result": ListValue(records=records, selected_fields=fields)}
    )
    text = render_answer_text(result)
    table = text.split("\n\n")[1].splitlines()
    assert [cell.strip() for cell in table[0].split("|")] == list(fields)
    assert len(table) == 4
    assert table[2].startswith('"z"') and table[3].startswith('"a"')
    if "city" in fields:
        assert table[2].endswith("NULL")
        assert table[3].endswith('"NULL"')
    if TIV in fields:
        assert "$1.01" in table[2] and "$-1.01" in table[3]
    assert "construction_type" not in table[0]


@pytest.mark.parametrize(
    "count,expected", [(0, "0 logical assets"), (1, "1 logical asset"), (2, "2 logical assets")]
)
def test_list_count_labels(success: Success, count: int, expected: str) -> None:
    records = tuple(AssetRecord(asset_id=f"asset-{index}") for index in range(count))
    value = ListValue(records=records, selected_fields=("asset_id",))
    text = render_answer_text(success.model_copy(update={"result": value}))
    assert f"Answer: {expected}\n" in text


@pytest.mark.parametrize(
    "logical,physical,expected_assets,expected_rows",
    [
        (0, 0, "0 logical assets", "0 original spreadsheet rows"),
        (1, 1, "1 logical asset", "1 original spreadsheet row"),
        (1, 2, "1 logical asset", "2 original spreadsheet rows"),
        (2, 3, "2 logical assets", "3 original spreadsheet rows"),
    ],
)
def test_contributor_count_labels(
    success: Success, logical: int, physical: int, expected_assets: str, expected_rows: str
) -> None:
    evidence = success.evidence.model_copy(
        update={"contributing_asset_count": logical, "source_record_count": physical}
    )
    text = render_answer_text(success.model_copy(update={"evidence": evidence}))
    assert f"Contributors: {expected_assets}\n" in text
    assert f"Source records: {expected_rows}\n" in text


def test_listing_empty_and_complete(success: Success) -> None:
    empty = ListValue(records=(), selected_fields=("asset_id",))
    assert "No matching records (0 logical assets)." in render_answer_text(
        success.model_copy(update={"result": empty})
    )
    records = tuple(AssetRecord(asset_id=f"item-{i:03}") for i in range(500))
    result = success.model_copy(
        update={"result": ListValue(records=records, selected_fields=("asset_id",))}
    )
    table = render_answer_text(result).split("\n\n")[1].splitlines()
    assert len(table) == 502
    assert [line.strip() for line in table[2:]] == [f'"{record.asset_id}"' for record in records]


def test_listing_terminal_safety(success: Success) -> None:
    payload = 'x|fake\\n"\x1b[31m\t\n\rfake row\x00\x07\x7f\x85\x9b\u202e\u200b\u2028\u2029'
    records = (AssetRecord(asset_id="safe", property_name=payload),)
    result = success.model_copy(
        update={"result": ListValue(records=records, selected_fields=("asset_id", "property_name"))}
    )
    text = render_answer_text(result)
    table = text.split("\n\n")[1].splitlines()
    assert len(table) == 3
    assert table[2].count("|") == 1
    assert "\\x7c" in table[2] and "\\\\n" in table[2]
    assert "\\x1b[31m" in table[2] and "\\t\\n\\r" in table[2]
    assert "\\u202e" in table[2]
    assert all(unicodedata.category(c) not in {"Cc", "Cf", "Zl", "Zp"} for c in table[2])
    assert isinstance(result.result, ListValue)
    assert result.result.records[0].property_name == payload


@pytest.mark.parametrize(
    "payload",
    [
        "".join(chr(i) for i in range(32)) + "".join(chr(i) for i in range(127, 160)),
        "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff",
        "pipe|column\\literal\\x1b[0m",
    ],
)
def test_listing_all_controls_and_delimiters(success: Success, payload: str) -> None:
    value = ListValue(records=(AssetRecord(asset_id=payload),), selected_fields=("asset_id",))
    text = render_answer_text(success.model_copy(update={"result": value}))
    table = text.split("\n\n")[1].splitlines()
    assert len(table) == 3
    assert "|" not in table[2]
    assert all(unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp"} for char in table[2])
    assert value.records[0].asset_id == payload


def test_identity_sql_counts_qualifications(success: Success) -> None:
    qualifications = ("First supplied qualification.", "Second\tqualification\x1b[0m", "Third.")
    sql = "SELECT COUNT(*)\nFROM assets WHERE state = 'AZ'"
    result = success.model_copy(update={"qualifications": qualifications, "executed_sql": sql})
    text = render_answer_text(result)
    assert "Workbook: synthetic.xlsx" in text
    assert "Worksheet: Statement of Values" in text
    assert success.dataset.dataset_id in text
    assert "Verification: verified; reconciled=True, complete=True, same snapshot=True" in text
    assert "Contributors: 3 logical assets" in text
    assert "Source records: 4 original spreadsheet rows" in text
    assert f"Executed SQL:\n{sql}\n" in text
    assert (
        "Qualifications:\n- First supplied qualification.\n- Second\\tqualification\\x1b[0m\n- Third."
        in text
    )
    assert "--format json" in text
    assert "Qualifications:" not in render_answer_text(
        success.model_copy(update={"qualifications": ()})
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"evidence_limit": 0},
        {"required_contributor_count": 501},
        {"evidence_limit": 500, "required_contributor_count": 501},
    ],
)
def test_failure(metadata: dict) -> None:
    failure = Failure(
        category="evidence_limit_exceeded",
        code="contributor_limit",
        message="Too many\ncontributors",
        **metadata,
    )
    text = render_failure_text(failure)
    assert "Reason: evidence_limit_exceeded / contributor_limit" in text
    assert "Too many" not in text
    assert failure.message == "Too many\ncontributors"
    assert "No verified answer was returned." in text
    assert ("Evidence limit:" in text) == ("evidence_limit" in metadata)
    assert ("Required contributors:" in text) == ("required_contributor_count" in metadata)
    assert "None" not in text


def test_renderers_are_pure(
    report: dict, success: Success, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_report = deepcopy(report)
    before_success = success.model_copy(deep=True)
    failure = Failure(category="provider_error", code="offline", message="Synthetic failure")
    forbidden = Mock(side_effect=AssertionError("Renderer must not perform I/O or invoke services"))
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr("sovtrace.qa.provider.OpenAI", forbidden)
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAI", forbidden)
    monkeypatch.setattr(qa, "ask", forbidden)
    monkeypatch.setattr(cli, "ingest_workbook", forbidden)
    for render, value in [(render_answer_text, success), (render_failure_text, failure)]:
        assert render(value) == render(value)
    assert render_ingest_text(report, "db") == render_ingest_text(report, "db")
    assert report == before_report
    assert success == before_success
    forbidden.assert_not_called()


@pytest.mark.parametrize("flags", [[], ["--format", "text"], ["--format", "json"]])
def test_ingest_cli_formats(
    report: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    importer = Mock(return_value=report)
    monkeypatch.setattr(cli, "ingest_workbook", importer)
    assert cli.main(["ingest", "source.xlsx", "--db", "out.sqlite", *flags]) is None
    captured = capsys.readouterr()
    assert captured.err == ""
    if flags == ["--format", "json"]:
        assert captured.out == json.dumps(report, indent=2) + "\n"
    else:
        assert captured.out == render_ingest_text(report, Path("out.sqlite")) + "\n"
    importer.assert_called_once_with(
        Path("source.xlsx"), Path("out.sqlite"), replace=False, infer_schema=True
    )


@pytest.mark.parametrize("flags", [[], ["--format", "text"], ["--format", "json"]])
def test_ingest_cli_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "ingest_workbook",
        Mock(side_effect=IngestionError(FailureCategory.WORKBOOK, "Synthetic error")),
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["ingest", "source.xlsx", "--db", "out.sqlite", *flags])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == render_ingest_failure_text(
            IngestionError(FailureCategory.WORKBOOK, "Synthetic error"), database="out.sqlite"
        )
        + "\n"
    )


@pytest.mark.parametrize("flags", [[], ["--format", "text"], ["--format", "json"]])
def test_ask_cli_formats(
    success: Success,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    service = Mock(return_value=success)
    monkeypatch.setattr(qa, "ask", service)
    assert cli.main(["ask", "--db", "db.sqlite", success.question, *flags]) is None
    captured = capsys.readouterr()
    assert captured.err == ""
    if flags == ["--format", "json"]:
        assert captured.out == success.model_dump_json(indent=2) + "\n"
        data = json.loads(captured.out)
        assert "plan" not in data
        assert data["result"]["value"] == "30000000000000"
        assert data["evidence"] == json.loads(success.model_dump_json())["evidence"]
        assert data["qualifications"] == list(success.qualifications)
        cells = [
            cell
            for c in data["evidence"]["contributors"]
            for r in c["source_records"]
            for cell in r["cells"]
        ]
        assert any(
            c["cell_coordinate"] == "E2"
            and c["original_value"] == "9999999.999999"
            and c["normalized_value"] == "9999999999999"
            for c in cells
        )
    else:
        assert captured.out == render_answer_text(success) + "\n"
    service.assert_called_once_with(Path("db.sqlite"), success.question)


@pytest.mark.parametrize("flags", [[], ["--format", "text"], ["--format", "json"]])
@pytest.mark.parametrize(
    "metadata", [{}, {"evidence_limit": 500, "required_contributor_count": 501}]
)
def test_ask_cli_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    metadata: dict,
) -> None:
    failure = Failure(
        category="evidence_limit_exceeded",
        code="contributor_limit",
        message="Synthetic failure",
        **metadata,
    )
    monkeypatch.setattr(qa, "ask", Mock(return_value=failure))
    with pytest.raises(SystemExit) as exc:
        cli.main(["ask", "--db", "db.sqlite", "Question", *flags])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    if flags == ["--format", "json"]:
        assert captured.err == ""
        assert captured.out == failure.model_dump_json(indent=2) + "\n"
    else:
        assert captured.out == ""
        assert captured.err == render_failure_text(failure) + "\n"


@pytest.mark.parametrize("command,arguments", [("ingest", ["source.xlsx"]), ("ask", ["Question"])])
def test_invalid_format_before_services(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    arguments: list[str],
) -> None:
    forbidden = Mock(side_effect=AssertionError("Parser must reject before service invocation"))
    monkeypatch.setattr(cli, "ingest_workbook", forbidden)
    monkeypatch.setattr(qa, "ask", forbidden)
    with pytest.raises(SystemExit) as exc:
        cli.main([command, "--db", "db.sqlite", *arguments, "--format", "yaml"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid choice: 'yaml'" in captured.err
    forbidden.assert_not_called()


def test_real_verified_negative_cli(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    generator = FakeGenerator("SELECT COUNT(*) FROM assets WHERE state = 'FL'", "exists")
    monkeypatch.setattr("sovtrace.qa.service.OpenAIGenerator", lambda: generator)
    assert cli.main(["ask", "--db", str(db), "Are there Florida assets?"]) is None
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Answer: No\n" in captured.out
    assert "Verification: verified" in captured.out
    assert "Contributors: 0 logical assets" in captured.out
    assert "Source records: 0 original spreadsheet rows" in captured.out
