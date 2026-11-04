"""Presentation contracts using the approved synthetic demo and artificial scalars."""

import builtins
import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
import unicodedata
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import httpx
import openpyxl
import pytest

from sovtrace.ingestion.provider import OpenAISchemaInferenceProvider
from sovtrace.qa.provider import OpenAIGenerator

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("demo_output_subject", ROOT / "scripts/demo.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def evaluated(demo):
    return demo.run_demo()


@pytest.mark.parametrize("operation", ["count", "list"])
@pytest.mark.parametrize("value,expected", [(0, "0 assets"), (1, "1 asset"), (3, "3 assets")])
def test_count_and_list_pluralization(demo, operation, value, expected):
    assert demo.format_answer({"operation": operation, "answer_value": value}) == expected


@pytest.mark.parametrize("value,expected", [(True, "Yes"), (False, "No")])
def test_existence_display(demo, value, expected):
    assert demo.format_answer({"operation": "exists", "answer_value": value}) == expected


@pytest.mark.parametrize(
    "micro,expected",
    [
        (0, "$0.00"),
        (1_234_567, "$1.23"),
        (-1_234_567, "$-1.23"),
        (4_999, "$0.00"),
        (5_000, "$0.01"),
        (-5_000, "$-0.01"),
        (999_999, "$1.00"),
        (1_005_000, "$1.01"),
        (-1_005_000, "$-1.01"),
        # Adjacent cent boundaries beyond binary floating-point integer precision.
        (9_007_199_254_744_999, "$9,007,199,254.74"),
        (9_007_199_254_745_000, "$9,007,199,254.75"),
        (-9_007_199_254_745_000, "$-9,007,199,254.75"),
    ],
)
def test_exact_signed_micro_usd_display(demo, micro, expected):
    assert demo.format_answer({"operation": "sum", "answer_value": micro}) == expected


def test_text_layout_evaluated_values_and_disclosure(demo, evaluated):
    text = demo.render_demo(evaluated)
    assert text.startswith("SOVTrace — Offline Demonstration\nOFFLINE — predefined SQL;")
    expected = [
        "Yes",
        "2 assets",
        "$20,000,000.00",
        "1 asset",
        "$12,000,000.00",
        "1 asset",
        "No",
        "$28,000,000.00",
    ]
    previous = -1
    for index, (record, value) in enumerate(zip(evaluated, expected, strict=True), 1):
        block = f"{index}. {record['question']}\n   Answer: {value} | PASS"
        position = text.index(block)
        assert position > previous
        previous = position
        assert record["executed_sql"] not in text
    assert text.count(" | PASS") == 8
    assert "Result: PASS — 8/8 cases passed" in text
    assert "4 logical assets; 5 physical source records" in text
    assert "executed_sql" not in text and "source_cells" not in text
    for coordinate in {c for record in evaluated for c in record["source_cells"]}:
        assert coordinate not in text
    changed = deepcopy(evaluated)
    changed[1]["answer_value"] = 3
    assert "2. How many assets are in Arizona?\n   Answer: 3 assets | PASS" in demo.render_demo(
        changed
    )


def test_safe_immutable_rendering_without_io(demo, evaluated, monkeypatch, capsys):
    records = deepcopy(evaluated)
    controls = "\n\r\t\x1b[31m\x07\u202e\u200b\u2028\u2029"
    records[0]["question"] += controls
    records[0]["source_workbook"] += controls
    records[0]["executed_sql"] = "ARTIFICIAL_SQL_DISCLOSURE_SENTINEL"
    records[0]["source_cells"] = ["ARTIFICIAL_CELL_DISCLOSURE_SENTINEL"]
    before = deepcopy(records)
    forbidden = Mock(side_effect=AssertionError("Rendering attempted a side effect"))
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        for method in ("open", "read_bytes", "write_bytes", "read_text", "write_text"):
            patch.setattr(Path, method, forbidden)
        patch.setattr(sqlite3, "connect", forbidden)
        patch.setattr(openpyxl, "load_workbook", forbidden)
        patch.setattr(httpx.Client, "send", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        patch.setattr(socket, "getaddrinfo", forbidden)
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(OpenAISchemaInferenceProvider, "infer", forbidden)
        patch.setattr(OpenAIGenerator, "generate", forbidden)
        for name in ("run_demo", "ingest_workbook", "ask"):
            patch.setattr(demo, name, forbidden)
        patch.setattr(sys.stdout, "write", forbidden)
        patch.setattr(sys.stderr, "write", forbidden)
        text = demo.render_demo(records)
    forbidden.assert_not_called()
    assert capsys.readouterr() == ("", "")
    assert records == before
    assert all(
        c == "\n" or unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in text
    )
    assert "\\x1b[31m" in text and "\\u202e" in text
    assert "ARTIFICIAL_SQL_DISCLOSURE_SENTINEL" not in text
    assert "ARTIFICIAL_CELL_DISCLOSURE_SENTINEL" not in text


@pytest.mark.parametrize("arguments", [[], ["--format", "text"], ["--format", "json"]])
def test_real_cli_streams_and_records(demo, evaluated, arguments):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/demo.py"), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    if arguments[-1:] != ["json"]:
        assert result.stdout == demo.render_demo(evaluated) + "\n"
        return
    lines = result.stdout.splitlines()
    assert len(lines) == 8
    assert [json.loads(line) for line in lines] == evaluated
    assert lines == [json.dumps(record, sort_keys=True) for record in evaluated]
    for index, record in enumerate(map(json.loads, lines)):
        assert isinstance(record["question"], str)
        assert record["operation"] in {"exists", "count", "sum", "list"}
        assert type(record["answer_value"]) is (bool if record["operation"] == "exists" else int)
        assert type(record["logical_contributing_assets"]) is int
        assert type(record["physical_source_records"]) is int
        assert record["evidence_reconciled"] is True
        assert record["source_workbook"] == "synthetic-sov.xlsx"
        assert record["worksheet"] == "Statement of Values"
        assert record["executed_sql"] == evaluated[index]["executed_sql"]
        assert record["source_cells"] == evaluated[index]["source_cells"]
        assert all(isinstance(c, str) for c in record["source_cells"])


@pytest.mark.parametrize("mode", ["text", "json"])
def test_failed_evaluation_has_nonzero_exit_without_partial_success(mode, tmp_path):
    # Fail inside the real evaluator after an earlier case has completed. The CLI
    # must wait for the complete run before emitting any successful records.
    bootstrap = tmp_path / "fail_demo.py"
    bootstrap.write_text(
        "import runpy, sys\n"
        f"subject = runpy.run_path({str(ROOT / 'scripts/demo.py')!r})\n"
        "main = subject['main']\n"
        "original = subject['ask']\n"
        "calls = 0\n"
        "def failing(*args, **kwargs):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    if calls == 2: raise RuntimeError('artificial evaluation failure')\n"
        "    return original(*args, **kwargs)\n"
        "subject['run_demo'].__globals__['ask'] = failing\n"
        f"sys.argv = ['demo.py', '--format', {mode!r}]\n"
        "main()\n"
    )
    result = subprocess.run(
        [sys.executable, str(bootstrap)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "artificial evaluation failure" in result.stderr
    assert "PASS" not in result.stdout + result.stderr
