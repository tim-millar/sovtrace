"""Protect the case study's real application boundary and offline presentation."""

import importlib.util
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from sovtrace.ingestion import ingest_workbook
from sovtrace.ingestion.provider import OpenAISchemaInferenceProvider
from sovtrace.qa import Failure, Generation, Proposal, Success, ask, service
from sovtrace.qa.contracts import ExistsValue, SumValue
from sovtrace.qa.provider import OpenAIGenerator

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/case_study_demo.py"
GUARD = ROOT / ".ci/offline_guard"


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("case_study_demo", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guarded_env():
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("OPENAI_")},
        "PYTHONPATH": str(GUARD),
        "UV_OFFLINE": "1",
    }


def test_real_workflow_and_rejections_before_result_execution(demo, monkeypatch):
    execute = Mock(wraps=service.execute_query)
    generate = Mock(wraps=demo.FixedSQLGenerator.generate)
    # Preserve method binding while recording every deterministic provider call.
    monkeypatch.setattr(demo.FixedSQLGenerator, "generate", lambda *args: generate(*args))
    monkeypatch.setattr(service, "execute_query", execute)
    forbidden = Mock(side_effect=AssertionError("External provider was constructed"))
    monkeypatch.setattr(OpenAIGenerator, "__init__", forbidden)
    monkeypatch.setattr(OpenAISchemaInferenceProvider, "__init__", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    run = demo.run_demo()
    assert [
        run.import_report[key]
        for key in ("logical_assets", "physical_records", "verified_duplicate_groups")
    ] == [4, 5, 1]
    assert run.import_report["schema_inference"]["status"] == "disabled"
    answer = run.answer
    assert answer.status == "verified" and answer.model == demo.MODEL
    assert isinstance(answer.result, SumValue) and answer.result.value == 20_000_000_000_000
    assert answer.executed_sql == answer.plan.executed_sql == demo.SQL
    assert answer.matching_count == answer.evidence.contributing_asset_count == 2
    assert answer.evidence.source_record_count == 3
    assert answer.dataset.workbook_filename == "synthetic-sov.xlsx"
    assert answer.dataset.worksheet == "Statement of Values"
    sources = [source for c in answer.evidence.contributors for source in c.source_records]
    assert [source.row for source in sources] == [2, 3, 4]
    assert {"D2", "E2", "D4", "E4"} <= {cell.cell_coordinate for s in sources for cell in s.cells}
    assert answer.verification.reconciled
    assert answer.verification.complete
    assert answer.verification.same_snapshot
    assert (run.unsupported.category, run.unsupported.code) == (
        "unsupported_question",
        "model_unsupported",
    )
    assert run.unsupported.message == demo.REASON
    assert (run.rejected.category, run.rejected.code) == ("sql_validation_error", "unsupported_sql")
    assert generate.call_count == 3
    assert [call.args[1] for call in generate.call_args_list] == [
        demo.QUESTION,
        demo.UNSUPPORTED_QUESTION,
        demo.INVALID_QUESTION,
    ]
    execute.assert_called_once()
    assert execute.call_args.args[1] == answer.plan
    forbidden.assert_not_called()
    # Temporary publication has been removed before the outcomes escape run_demo.
    assert not Path(run.import_report["output"]).exists()


def test_false_existence_is_verified_not_refused(tmp_path):
    database = tmp_path / "negative.sqlite"
    ingest_workbook(ROOT / "examples/synthetic-sov.xlsx", database, infer_schema=False)

    class NegativeGenerator:
        def generate(self, question, schema):
            return Generation(
                Proposal(
                    status="query",
                    operation="exists",
                    sql="SELECT COUNT(*) FROM assets WHERE state = 'FL'",
                    reason=None,
                ),
                model="simulated-negative-existence",
            )

    result = ask(database, "Are there assets in Florida?", NegativeGenerator())
    assert isinstance(result, Success)
    assert isinstance(result.result, ExistsValue) and result.result.value is False
    assert result.matching_count == result.evidence.source_record_count == 0
    assert result.verification.reconciled and result.verification.complete


def test_guarded_cli_stable_output_without_credentials(demo, guarded_env, tmp_path):
    results = [
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, runpy, sys; "
                "assert 'sitecustomize' in sys.modules; "
                "assert not any(k.startswith('OPENAI_') for k in os.environ); "
                f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')",
            ],
            cwd=tmp_path,
            env=guarded_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        for _ in range(2)
    ]
    for result in results:
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
    output = results[0].stdout
    assert output == results[1].stdout == demo.render_demo(demo.run_demo()) + "\n"
    for expected in (
        demo.SQL,
        "$20,000,000.00",
        "20000000000000 micro-USD",
        "simulated-case-study-sql-generator",
        "4 logical assets",
        "5 physical source records",
        "1 verified duplicate group",
        "D2",
        "E2",
        "D4",
        "E4",
        "unsupported_question/model_unsupported",
        "sql_validation_error/unsupported_sql",
        "same snapshot: True",
    ):
        assert expected in output
    assert "Simulated model-declared refusal" in output
    assert "Deterministic local validation" in output
    assert str(ROOT) not in output and str(tmp_path) not in output
    assert "sovtrace-case-study-" not in output
    assert list(tmp_path.iterdir()) == []


def test_rendering_has_no_query_or_provider_operations(demo, monkeypatch):
    run = demo.run_demo()
    forbidden = Mock(side_effect=AssertionError("Rendering attempted I/O"))
    for name in ("ask", "ingest_workbook", "run_demo"):
        monkeypatch.setattr(demo, name, forbidden)
    monkeypatch.setattr(demo.FixedSQLGenerator, "generate", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(OpenAIGenerator, "generate", forbidden)
    monkeypatch.setattr(OpenAISchemaInferenceProvider, "infer", forbidden)
    assert demo.render_demo(run) == demo.render_demo(run)
    forbidden.assert_not_called()


@pytest.mark.parametrize("failure_at", [1, 2, 3])
def test_intermediate_exception_exits_nonzero_without_partial_output(
    failure_at, guarded_env, tmp_path
):
    code = f"""
import runpy
subject = runpy.run_path({str(SCRIPT)!r})
original = subject['ask']
calls = 0
def failing(*args, **kwargs):
    global calls
    calls += 1
    if calls == {failure_at}:
        raise RuntimeError('/private/transient/path must not be printed')
    return original(*args, **kwargs)
subject['run_demo'].__globals__['ask'] = failing
raise SystemExit(subject['main']())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=guarded_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1 and result.stdout == ""
    assert "unexpected RuntimeError" in result.stderr
    assert "PASS" not in result.stdout + result.stderr
    assert "/private" not in result.stderr


@pytest.mark.parametrize("defect", ["import", "value", "evidence", "classification"])
def test_wrong_outcomes_cannot_produce_success(demo, monkeypatch, capsys, defect):
    if defect == "import":
        original = demo.ingest_workbook
        monkeypatch.setattr(
            demo, "ingest_workbook", lambda *a, **kw: {**original(*a, **kw), "logical_assets": 99}
        )
    else:
        original = demo.ask

        def wrong(database, question, generator):
            result = original(database, question, generator)
            if isinstance(result, Success):
                if defect == "value":
                    return result.model_copy(update={"result": SumValue(value=1, outcome="value")})
                if defect == "evidence":
                    evidence = result.evidence.model_copy(update={"contributors": ()})
                    return result.model_copy(update={"evidence": evidence})
            elif defect == "classification":
                return Failure(category="provider_error", code="transport", message="Unexpected")
            return result

        monkeypatch.setattr(demo, "ask", wrong)
    assert demo.main() == 1
    output = capsys.readouterr()
    assert output.out == "" and "Case study failed:" in output.err
    assert "PASS" not in output.out + output.err
