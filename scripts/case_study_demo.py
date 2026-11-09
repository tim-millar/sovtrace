"""Offline case study: simulated proposals through real application boundaries."""

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sovtrace.ingestion import ingest_workbook
from sovtrace.presentation import _money as money
from sovtrace.presentation import _safe as safe_text
from sovtrace.qa import Failure, Generation, Proposal, Success, ask
from sovtrace.qa.contracts import SumValue

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = ROOT / "examples" / "synthetic-sov.xlsx"
QUESTION = "What is the aggregate TIV of Arizona assets?"
SQL = "SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE state = 'AZ'"
UNSUPPORTED_QUESTION = "What was the net profit of the Arizona assets?"
INVALID_QUESTION = "How many assets are in Arizona?"
INVALID_SQL = "SELECT COUNT(*) FROM sqlite_master"
REASON = "Profit is outside the supported SOV semantic model."
MODEL = "simulated-case-study-sql-generator"


class DemoError(Exception):
    """A fixed, path-free diagnostic for a failed demonstration expectation."""


def require(condition: bool, diagnostic: str) -> None:
    if not condition:
        raise DemoError(diagnostic)


class FixedSQLGenerator:
    """Small deterministic test double; never constructs an external provider."""

    def __init__(self) -> None:
        self.questions: list[str] = []

    def generate(self, question: str, schema: str) -> Generation:
        self.questions.append(question)
        if question == QUESTION:
            proposal = Proposal(status="query", operation="sum", sql=SQL, reason=None)
        elif question == UNSUPPORTED_QUESTION:
            proposal = Proposal(status="unsupported", operation=None, sql=None, reason=REASON)
        elif question == INVALID_QUESTION:
            proposal = Proposal(status="query", operation="count", sql=INVALID_SQL, reason=None)
        else:
            raise DemoError("Unexpected question sent to simulated generator")
        return Generation(proposal, model=MODEL)


@dataclass(frozen=True)
class Walkthrough:
    import_report: dict[str, object]
    answer: Success
    unsupported: Failure
    rejected: Failure


def require_failure(result: Success | Failure, category: str, code: str) -> Failure:
    if not isinstance(result, Failure):
        raise DemoError(f"Expected {category}/{code}; received a successful answer")
    require(
        (result.category, result.code) == (category, code),
        f"Expected {category}/{code}; received {result.category}/{safe_text(result.code)}",
    )
    return result


def run_demo() -> Walkthrough:
    """Collect and check all five steps before exposing completed output."""
    with tempfile.TemporaryDirectory(prefix="sovtrace-case-study-") as directory:
        database = Path(directory) / "case-study.sqlite"
        report = ingest_workbook(WORKBOOK, database, infer_schema=False)
        for key, expected in (
            ("logical_assets", 4),
            ("physical_records", 5),
            ("verified_duplicate_groups", 1),
        ):
            require(report[key] == expected, f"Import: unexpected {key}")
        require(
            report["schema_inference"]
            == {"status": "disabled", "model": None, "inferred_fields": []},
            "Import: schema inference was not disabled",
        )
        generator = FixedSQLGenerator()
        answer = ask(database, QUESTION, generator)
        if isinstance(answer, Failure):
            raise DemoError(f"Arizona query failed: {answer.category}/{safe_text(answer.code)}")
        require(answer.status == "verified" and answer.operation == "sum", "Query not verified sum")
        require(answer.model == MODEL, "Query used unexpected generator")
        require(answer.executed_sql == SQL, "Unexpected executed SQL")
        require(isinstance(answer.result, SumValue), "Unexpected answer type")
        require(
            isinstance(answer.result, SumValue) and answer.result.value == 20_000_000_000_000,
            "Incorrect exact Arizona TIV",
        )
        require(answer.display_value == "$20,000,000.00", "Incorrect monetary display")
        require(
            answer.matching_count == answer.evidence.contributing_asset_count == 2,
            "Incorrect logical contributor count",
        )
        require(answer.evidence.source_record_count == 3, "Incorrect physical contributor count")
        verification = answer.verification
        require(
            verification.reconciled and verification.complete and verification.same_snapshot,
            "Reconciliation, completeness or same-snapshot verification failed",
        )
        require(
            (verification.dataset_logical_assets, verification.dataset_physical_records) == (4, 5),
            "Verification population differs from import",
        )
        require(answer.dataset.workbook_filename == WORKBOOK.name, "Unexpected source workbook")
        require(answer.dataset.worksheet == "Statement of Values", "Unexpected worksheet")
        cells = {
            cell.cell_coordinate
            for contributor in answer.evidence.contributors
            for source in contributor.source_records
            for cell in source.cells
        }
        require({"D2", "E2", "D4", "E4"} <= cells, "Required original coordinates missing")
        unsupported = require_failure(
            ask(database, UNSUPPORTED_QUESTION, generator),
            "unsupported_question",
            "model_unsupported",
        )
        require(unsupported.message == REASON, "Unexpected unsupported reason")
        rejected = require_failure(
            ask(database, INVALID_QUESTION, generator), "sql_validation_error", "unsupported_sql"
        )
        require(
            generator.questions == [QUESTION, UNSUPPORTED_QUESTION, INVALID_QUESTION],
            "Unexpected simulated generator calls",
        )
        return Walkthrough(report, answer, unsupported, rejected)


def render_demo(run: Walkthrough) -> str:
    """Pure rendering of completed outcomes; no workbook, database or provider I/O."""
    answer = run.answer
    if not isinstance(answer.result, SumValue) or answer.result.value is None:
        raise DemoError("Missing monetary result")
    lines = [
        "SOVTrace — Engineering Case Study",
        f"OFFLINE — SQL generation is simulated ({safe_text(answer.model)}).",
        "Real: workbook ingestion, SQL validation, SQLite execution and evidence reconciliation.",
        "No credentials or external provider requests; schema inference explicitly disabled.",
        "",
        "1. Import",
        f"   {safe_text(answer.dataset.workbook_filename)}: "
        f"{run.import_report['logical_assets']} logical assets; "
        f"{run.import_report['physical_records']} physical source records; "
        f"{run.import_report['verified_duplicate_groups']} verified duplicate group.",
        "",
        "2. Successful question",
        f"   Question: {safe_text(answer.question)}",
        f"   Executed SQL: {safe_text(answer.executed_sql)}",
        f"   Answer: {money(answer.result.value)}",
        f"   Exact value: {answer.result.value} micro-USD",
        f"   Contributors: {answer.evidence.contributing_asset_count} logical assets; "
        f"{answer.evidence.source_record_count} physical source records.",
        f"   Reconciled: {answer.verification.reconciled}; "
        f"complete: {answer.verification.complete}; "
        f"same snapshot: {answer.verification.same_snapshot}.",
        "",
        "3. Original-source evidence",
        f"   Workbook: {safe_text(answer.dataset.workbook_filename)}; "
        f"worksheet: {safe_text(answer.dataset.worksheet)}",
        "   Complete returned contributing rows and source-cell coordinates:",
    ]
    for contributor in answer.evidence.contributors:
        for source in contributor.source_records:
            coordinates = ", ".join(safe_text(cell.cell_coordinate) for cell in source.cells)
            lines.append(
                f"   Asset {safe_text(contributor.asset_id)}, row {source.row}: {coordinates}"
            )
    lines.extend(
        [
            "   Verified duplicate consolidation explains two logical assets and three physical records.",
            "   Additional route to full source-cell evidence:",
            "   uv run --locked python scripts/demo.py --format json",
            "",
            "4. Simulated model-declared refusal",
            f"   Question: {UNSUPPORTED_QUESTION}",
            f"   Withheld: {run.unsupported.category}/{safe_text(run.unsupported.code)}",
            f"   Reason: {safe_text(run.unsupported.message)}",
            "   Simulated provider declared unsupported; no SQL executed.",
            "   This does not prove a live model would refuse the question.",
            "",
            "5. Local SQL-validation rejection",
            f"   Question: {INVALID_QUESTION}",
            f"   Simulated proposal: {INVALID_SQL}",
            f"   Rejected: {run.rejected.category}/{safe_text(run.rejected.code)}",
            "   Deterministic local validation rejected the proposal before result execution.",
            "   Both failures withhold answers; neither is an empty or false successful result.",
            "",
            "Result: PASS — all five demonstration steps checked.",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    try:
        completed = run_demo()
        output = render_demo(completed)
    except DemoError as exc:
        print(f"Case study failed: {safe_text(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Application exception strings and tracebacks can contain temporary/private paths.
        print(
            f"Case study failed: unexpected {type(exc).__name__}; no completed walkthrough.",
            file=sys.stderr,
        )
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
