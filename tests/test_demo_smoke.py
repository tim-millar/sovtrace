"""Protect the credential-free, committed synthetic end-to-end demonstration."""

from pathlib import Path
from runpy import run_path

demo = run_path(str(Path(__file__).parents[1] / "scripts" / "demo.py"))


def test_offline_demo_imports_executes_and_reconciles_every_reference_case() -> None:
    answers = demo["run_demo"]()

    assert len(answers) == len(demo["CASES"]) == 8
    assert [answer["answer_value"] for answer in answers] == [
        case.expected for case in demo["CASES"]
    ]
    assert all(answer["evidence_reconciled"] for answer in answers)
    assert all(answer["source_workbook"] == "synthetic-sov.xlsx" for answer in answers)
    assert all(answer["worksheet"] == "Statement of Values" for answer in answers)

    aggregate = answers[2]
    assert aggregate["operation"] == "sum"
    assert aggregate["answer_value"] == demo["CASES"][2].expected
    assert aggregate["logical_contributing_assets"] == 2
    assert aggregate["physical_source_records"] == 3
    assert "D2" in aggregate["source_cells"]
    assert "E2" in aggregate["source_cells"]
    assert "D4" in aggregate["source_cells"]
    assert "E4" in aggregate["source_cells"]

    negative = answers[6]
    assert negative["answer_value"] is False
    assert negative["logical_contributing_assets"] == 0
    assert negative["physical_source_records"] == 0
