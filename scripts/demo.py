"""Run the credential-free synthetic ingestion and verified queries demonstration."""

import argparse
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sovtrace.ingestion import ingest_workbook
from sovtrace.presentation import _money as money
from sovtrace.presentation import _safe as safe_text
from sovtrace.qa import Failure, Generation, Proposal, Success, ask
from sovtrace.qa.contracts import ListValue, Operation

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = ROOT / "examples" / "synthetic-sov.xlsx"


@dataclass(frozen=True)
class DemoCase:
    question: str
    operation: Operation
    sql: str
    expected: bool | int
    matching_count: int
    physical_count: int


# These references are fixed independently of model output and application results.
CASES = (
    DemoCase(
        "Are there assets in Arizona?",
        "exists",
        "SELECT COUNT(*) FROM assets WHERE state = 'AZ'",
        True,
        2,
        3,
    ),
    DemoCase(
        "How many assets are in Arizona?",
        "count",
        "SELECT COUNT(*) FROM assets WHERE state = 'AZ'",
        2,
        2,
        3,
    ),
    DemoCase(
        "What is the aggregate TIV of Arizona assets?",
        "sum",
        "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'AZ'",
        20_000_000_000_000,
        2,
        3,
    ),
    DemoCase(
        "How many Arizona assets are worth more than $10 million?",
        "count",
        "SELECT COUNT(*) FROM assets WHERE state = 'AZ' AND total_insurable_value_micro_usd > 10000000000000",
        1,
        1,
        2,
    ),
    DemoCase(
        "What is the TIV of Arizona assets worth over $10 million?",
        "sum",
        "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'AZ' AND total_insurable_value_micro_usd > 10000000000000",
        12_000_000_000_000,
        1,
        2,
    ),
    DemoCase(
        "Which assets have Wood Frame - Podium construction?",
        "list",
        "SELECT asset_id, construction_type FROM assets WHERE construction_type = 'Wood Frame - Podium'",
        1,
        1,
        2,
    ),
    DemoCase(
        "Are there assets in Florida?",
        "exists",
        "SELECT COUNT(*) FROM assets WHERE state = 'FL'",
        False,
        0,
        0,
    ),
    DemoCase(
        "What is the aggregate TIV of all assets?",
        "sum",
        "SELECT SUM(total_insurable_value_micro_usd) FROM assets",
        28_000_000_000_000,
        4,
        5,
    ),
)


class FixedSQLGenerator:
    """Demo-only provider: SQL generation is simulated; execution remains real."""

    def __init__(self, case: DemoCase) -> None:
        self.case = case
        self.calls = 0

    def generate(self, question: str, schema: str) -> Generation:
        self.calls += 1
        return Generation(
            Proposal(status="query", operation=self.case.operation, sql=self.case.sql, reason=None),
            model="simulated-sql-generator",
        )


def run_demo() -> list[dict[str, object]]:
    """Import the tracked workbook and execute every example through the application."""
    with tempfile.TemporaryDirectory(prefix="sovtrace-demo-") as directory:
        database = Path(directory) / "demo.sqlite"
        report = ingest_workbook(WORKBOOK, database)
        assert report["physical_records"] == 5
        assert report["logical_assets"] == 4
        assert report["verified_duplicate_groups"] == 1
        answers: list[dict[str, object]] = []
        for case in CASES:
            generator = FixedSQLGenerator(case)
            result = ask(database, case.question, generator)
            assert generator.calls == 1
            if isinstance(result, Failure):
                raise AssertionError(f"{case.question}: {result.category}/{result.code}")
            assert isinstance(result, Success)
            assert result.status == "verified"
            assert result.operation == case.operation
            expected_filter = case.sql.partition(" WHERE ")[2] or None
            assert result.plan.filter == expected_filter
            if isinstance(result.result, ListValue):
                observed: bool | int = len(result.result.records)
            else:
                value = result.result.value
                assert value is not None
                observed = value
            if case.operation == "sum":
                observed = int(observed)
            assert observed == case.expected, (case.question, observed, case.expected)
            assert result.matching_count == case.matching_count
            assert result.evidence.contributing_asset_count == case.matching_count
            assert result.evidence.source_record_count == case.physical_count
            assert result.verification.reconciled and result.verification.complete
            assert result.verification.same_snapshot
            assert result.verification.dataset_logical_assets == 4
            assert result.verification.dataset_physical_records == 5
            answers.append(
                {
                    "question": case.question,
                    "source_workbook": result.dataset.workbook_filename,
                    "worksheet": result.dataset.worksheet,
                    "operation": result.operation,
                    "executed_sql": result.executed_sql,
                    "answer": result.summary,
                    "answer_value": observed,
                    "logical_contributing_assets": result.evidence.contributing_asset_count,
                    "physical_source_records": result.evidence.source_record_count,
                    "evidence_reconciled": result.verification.reconciled,
                    "source_cells": [
                        cell.cell_coordinate
                        for contributor in result.evidence.contributors
                        for source_record in contributor.source_records
                        for cell in source_record.cells
                    ],
                }
            )
        return answers


def format_answer(record: dict[str, object]) -> str:
    """Display the evaluated value; never consult the expected CASES oracle."""
    value = record["answer_value"]
    assert isinstance(value, (bool, int))
    match record["operation"]:
        case "exists":
            return "Yes" if value else "No"
        case "count" | "list":
            return f"{value} asset{'' if value == 1 else 's'}"
        case "sum":
            return money(value)
        case _:
            raise ValueError("Unknown demonstration operation")


def render_demo(records: list[dict[str, object]]) -> str:
    """Render a completed run_demo return value, whose assertions all passed.

    The demonstration has no partial-success or status-bearing record contract.
    """
    lines = [
        "SOVTrace — Offline Demonstration",
        "OFFLINE — predefined SQL; no live OpenAI model requests",
        "Real: Excel ingestion, SQLite, SQL validation/execution and",
        "complete source-evidence reconciliation.",
        f"Fixture: {safe_text(records[0]['source_workbook'])}",
        "Validated population: 4 logical assets; 5 physical source records;",
        "1 verified duplicate group.",
        "Counts and listings below count matching logical assets.",
        "",
    ]
    for index, record in enumerate(records, 1):
        lines.extend(
            [
                f"{index}. {safe_text(record['question'])}",
                f"   Answer: {format_answer(record)} | PASS",
            ]
        )
    lines.extend(
        [
            "",
            f"Result: PASS — {len(records)}/{len(records)} cases passed",
            "Full evaluated records and evidence:",
            "  uv run --locked python scripts/demo.py --format json",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    records = run_demo()
    if args.format == "json":
        for answer in records:
            print(json.dumps(answer, sort_keys=True))
    else:
        print(render_demo(records))


if __name__ == "__main__":
    main()
