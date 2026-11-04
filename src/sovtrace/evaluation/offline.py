"""Independent comparisons for the fixed synthetic corpus; real ingestion and public QA."""

import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from time import perf_counter

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from sovtrace.ingestion import IngestionError, ingest_workbook
from sovtrace.ingestion.inference import InferenceRequest, InferenceResult, SchemaProposal
from sovtrace.qa import Failure, Generation, Proposal, Success, ask
from sovtrace.qa.contracts import ListValue
from sovtrace.qa.provider import SQLGenerator

from .corpus import (
    FIELDS,
    TIV,
    Fixture,
    Question,
    expected_failure,
    workbook_identity,
    write_fixture,
)
from .live import BudgetExceeded

# Separate fixed fake proposals, never inferred from observed application metadata.
PROPOSALS = {
    "E1": (1, 2, 3, 4, 5, 6),
    "E2": (14, 13, 12, 11, 10, 9),
    "E5": (1, 2, 3, 4, 5, None),
    "E6": (1, 2, 3, None, 4, 5),
    "E11": (3, 2, 4, 6, 11, 13),
}


class FixedSchema:
    def __init__(self, fixture: Fixture):
        self.fixture = fixture

    def infer(self, request: InferenceRequest) -> InferenceResult:
        proposal = {
            f: {
                "status": "absent" if c is None else "mapped",
                "column": c,
                "candidates": [] if c is None else [c],
            }
            for f, c in zip(FIELDS, PROPOSALS[self.fixture.id], strict=True)
        }
        return InferenceResult(
            proposal=SchemaProposal.model_validate(proposal), model="fake-schema"
        )


class CapturedSQL:
    """Retain only the structured proposal needed for independent SQL checks."""

    def __init__(self, provider: SQLGenerator):
        self.provider = provider
        self.generation: Generation | None = None

    def generate(self, question: str, schema: str) -> Generation:
        self.generation = self.provider.generate(question, schema)
        return self.generation


class FixedSQL:
    def __init__(self, question: Question):
        self.question = question

    def generate(self, question: str, schema: str) -> Generation:
        return Generation(
            Proposal(
                status="query",
                operation=self.question.operation,
                sql=self.question.sql,
                reason=None,
            ),
            model="fake-sql",
        )


IMPORT_CHECKS = {
    "ingestion": "physical_discovery",
    "identity": "normalization",
    "mapping": "semantic_mapping",
    "inference_path": "semantic_mapping",
    "layout": "physical_discovery",
    "population": "normalization",
    "physical_membership": "normalization",
    "source_cells": "evidence_reconciliation",
    "data_quality": "normalization",
}
QUESTION_CHECKS = {
    "outcome": "sql_validation_execution",
    "sql_semantics": "sql_generation",
    "answer": "sql_validation_execution",
    "identity": "evidence_reconciliation",
    "membership": "evidence_reconciliation",
    "source_cells": "evidence_reconciliation",
    "reconciliation": "evidence_reconciliation",
}


def checks(stages: dict[str, str], dependency: str) -> dict:
    return {
        name: {"status": "not_run", "stage": stage, "dependency": dependency}
        for name, stage in stages.items()
    }


def compare(record: dict, name: str, expected, observed, *, stage: str | None = None) -> None:
    check = record["checks"][name]
    check.update(
        status="passed" if observed == expected else "failed", expected=expected, observed=observed
    )
    check.pop("dependency", None)
    if stage:
        check["stage"] = stage


def outcome(checks_: dict, *, allowed_not_run: frozenset[str] = frozenset()) -> str:
    """Strict completion by default; only explicit negative-question skips can pass."""
    statuses = [c["status"] for c in checks_.values()]
    for status in ("failed", "inconclusive"):
        if status in statuses:
            return status
    if not statuses or all(s == "not_run" for s in statuses):
        return "not_run"
    if any(
        c["status"] == "not_run"
        and (name not in allowed_not_run or c.get("dependency") != "verified_answer_unavailable")
        for name, c in checks_.items()
    ):
        return "failed"
    return "passed" if all(s in ("passed", "not_run") for s in statuses) else "failed"


def question_outcome(record: dict) -> str:
    if not set(QUESTION_CHECKS) <= set(record["checks"]):
        return "failed"
    permitted = frozenset()
    if (
        not record["supported"]
        and record.get("observed_answer_status") == "failure"
        and record["checks"]["outcome"]["status"] == "passed"
        and record["checks"]["sql_semantics"]["status"] == "passed"
    ):
        permitted = frozenset(
            {"answer", "identity", "membership", "source_cells", "reconciliation"}
        )
    status = outcome(record["checks"], allowed_not_run=permitted)
    return "failed" if status == "not_run" and "elapsed_seconds" in record else status


def case_outcome(record: dict) -> str:
    required = set(IMPORT_CHECKS) if record["expected_ingestion"] == "accepted" else {"ingestion"}
    if not required <= set(record["checks"]):
        return "failed"
    own = outcome(record["checks"])
    combined = {own, *(q["status"] for q in record["questions"])}
    for status in ("failed", "inconclusive"):
        if status in combined:
            return status
    if own == "not_run":
        if record["attempted"]:
            return "failed"
        return "not_run" if all(q["status"] == "not_run" for q in record["questions"]) else "failed"
    if record["expected_ingestion"] == "rejected" and record["questions"]:
        return "failed"
    return outcome(
        {
            "ingestion": {"status": own},
            **{"question." + q["id"]: {"status": q["status"]} for q in record["questions"]},
        }
    )


def report_outcome(cases: list[dict]) -> str:
    return outcome({c["id"]: {"status": c["status"]} for c in cases})


def finish(record: dict) -> dict:
    record["status"] = case_outcome(record) if "questions" in record else question_outcome(record)
    stages = {
        c["stage"] for c in record["checks"].values() if c["status"] in ("failed", "inconclusive")
    }
    stages.update(
        stage for q in record.get("questions", []) for stage in q.get("failure_stages", [])
    )
    if record["status"] == "failed" and not stages:
        stages.add("evaluation_infrastructure")  # Unexpectedly incomplete evaluation.
    record["failure_stages"] = sorted(stages)
    return record


def declared_case(
    case_id: str, fixture: Fixture, questions: tuple[Question, ...], *, identity: dict | None = None
) -> dict:
    return {
        "id": case_id,
        "attempted": False,
        "fixture": fixture.id,
        "status": "not_run",
        "checks": checks(
            IMPORT_CHECKS
            if fixture.failure_category is None
            else {"ingestion": "semantic_mapping" if fixture.infer else "physical_discovery"},
            "ingestion",
        ),
        "expected_dataset_identity": identity,
        "expected_mappings": fixture.mappings,
        "expected_ambiguous_candidates": {TIV: [5, 15]} if fixture.id == "E7" else {},
        "observed_mappings": None,
        "expected_record_counts": None
        if fixture.failure_category
        else {"physical": 5, "logical": 4},
        "expected_ingestion": "accepted" if fixture.failure_category is None else "rejected",
        "observed_ingestion": "not_run",
        "application_error": None,
        "expected_failure": {
            "category": fixture.failure_category,
            "code": fixture.failure_code,
            "message_contains": fixture.failure_contains,
            "message_prefix": fixture.failure_prefix,
        },
        "questions": [
            {
                "id": q.id,
                "expected_dataset_identity": identity,
                "question": q.text,
                "expected": {
                    "status": "failure" if expected_failure(fixture, q) else "verified",
                    "failure_categories": expected_failure(fixture, q),
                    "value": None if expected_failure(fixture, q) else q.value,
                    "logical_members": q.members,
                    "required_fields": q.required,
                    "physical_sources": {a: fixture.membership[a] for a in q.members},
                },
                "status": "not_run",
                "supported": not bool(expected_failure(fixture, q)),
                "checks": checks(QUESTION_CHECKS, f"{case_id}.ingestion"),
                "application_error": None,
            }
            for q in questions
        ],
    }


def error_stage(category: str) -> str:
    return {
        "structure": "physical_discovery",
        "mapping": "semantic_mapping",
        "duplicate-conflict": "normalization",
        "invalid-data": "normalization",
        "unsupported_question": "sql_generation",
        "invalid_model_response": "sql_generation",
    }.get(
        category,
        "evidence_reconciliation"
        if category.startswith(("evidence", "source_"))
        else "sql_validation_execution",
    )


def ingestion_error(record: dict, fixture: Fixture, exc: IngestionError, published: bool) -> None:
    message = str(exc)
    category = exc.category.value
    # Ingestion has no separate structured code: preserve its message and existing
    # prefix where one exists. Do not invent an application code for structural prose.
    code = (
        message.split(":", 1)[0] if message.startswith(("mapping_", "schema_inference_")) else None
    )
    record["application_error"] = {"category": category, "code": code, "message": message}
    record["observed_ingestion"] = "rejected"
    substantive = (
        category == fixture.failure_category
        and not published
        and (fixture.failure_code is None or code == fixture.failure_code)
        and (fixture.failure_contains is None or fixture.failure_contains in message)
        and (fixture.failure_prefix is None or message.startswith(fixture.failure_prefix))
    )
    compare(record, "ingestion", True, substantive, stage=error_stage(category))
    if category == "schema-inference":
        record["checks"]["ingestion"].update(
            status="inconclusive", stage="evaluation_infrastructure"
        )


def observe_import(database: Path, report: dict) -> dict:
    """Observations only. None of these database values become expectations."""
    with closing(sqlite3.connect(database)) as connection:
        filename, digest, worksheet, manifest_json = connection.execute(
            "SELECT workbook_filename, workbook_sha256, worksheet, manifest_json FROM dataset"
        ).fetchone()
        manifest = json.loads(manifest_json)
        persisted_identity = {
            "workbook_filename": filename,
            "workbook_sha256": digest,
            "worksheet": worksheet,
            "dataset_id": f"{digest}:{worksheet}",
        }
        population = {
            row[0]: dict(zip(FIELDS, row, strict=True))
            for row in connection.execute(
                "SELECT " + ", ".join(FIELDS) + " FROM assets ORDER BY asset_id"
            )
        }
        membership: dict[str, list[int]] = {}
        for asset, row in connection.execute(
            "SELECT asset_id, row_number FROM asset_sources ORDER BY asset_id, row_number"
        ):
            membership.setdefault(asset, []).append(row)
        cells = {
            (row, column): (coordinate, json.loads(value), label)
            for row, column, coordinate, value, label in connection.execute(
                "SELECT row_number, column_index, coordinate, original_value_json, original_label FROM source_cells JOIN source_columns USING (workbook_sha256, worksheet, column_index)"
            )
        }
        exclusions = list(
            connection.execute(
                "SELECT row_number, code FROM diagnostics WHERE code LIKE 'excluded_%' ORDER BY row_number"
            )
        )
        actual_counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("source_rows", "assets", "asset_sources", "source_cells")
        ]
        invalid = connection.execute(
            "SELECT row_number, column_index, semantic_field FROM diagnostics WHERE code = 'invalid_value'"
        ).fetchall()
    return {
        "manifest": manifest,
        "persisted_identity": persisted_identity,
        "population": population,
        "membership": membership,
        "cells": cells,
        "exclusions": exclusions,
        "invalid": invalid,
        "actual_counts": actual_counts,
        "report": report,
    }


def compare_import(record: dict, fixture: Fixture, observation: dict) -> None:
    manifest, report = observation["manifest"], observation["report"]
    record["observed_mappings"] = {f: m["column"] for f, m in manifest["concepts"].items()}
    record["observed_record_counts"] = {
        "physical": report["physical_records"],
        "logical": report["logical_assets"],
    }
    record["dataset_identity"] = {
        key: manifest[key] for key in ("workbook_filename", "workbook_sha256", "worksheet")
    }
    compare(record, "ingestion", fixture.failure_category is None, True)
    if fixture.failure_category:
        return
    manifest_identity = {
        **record["dataset_identity"],
        "dataset_id": f"{manifest['workbook_sha256']}:{manifest['worksheet']}",
    }
    import_identity = {
        "workbook_filename": report["workbook"],
        "workbook_sha256": report["sha256"],
        "worksheet": report["worksheet"],
        "dataset_id": f"{report['sha256']}:{report['worksheet']}",
    }
    compare(
        record,
        "identity",
        [record["expected_dataset_identity"]] * 3,
        [observation["persisted_identity"], manifest_identity, import_identity],
    )
    compare(
        record,
        "mapping",
        fixture.mappings,
        {f: m["column"] for f, m in manifest["concepts"].items()},
    )
    record["schema_inference"] = report["schema_inference"]
    compare(
        record,
        "inference_path",
        "applied" if fixture.infer else "disabled",
        report["schema_inference"]["status"],
    )
    compare(
        record,
        "layout",
        [
            fixture.header,
            fixture.header + 1,
            fixture.header + 5,
            sorted(fixture.exclusions.items()),
        ],
        [
            manifest["header_row"],
            manifest["data_start_row"],
            manifest["data_end_row"],
            observation["exclusions"],
        ],
    )
    compare(record, "population", fixture.population, observation["population"])
    compare(
        record,
        "physical_membership",
        [fixture.membership, 5, 4, 1, [5, 4, 5, 65 if fixture.id == "E5" else 70]],
        [
            observation["membership"],
            report["physical_records"],
            report["logical_assets"],
            report["verified_duplicate_groups"],
            observation["actual_counts"],
        ],
    )
    expected_cells, observed_cells = [], []
    for asset, rows in fixture.membership.items():
        for row in rows:
            for field, column in fixture.mappings.items():
                if column is None:
                    continue
                expected = fixture.cell(asset, row, field)
                expected_cells.append(
                    [
                        expected["cell_coordinate"],
                        expected["original_value"],
                        expected["source_header"],
                    ]
                )
                observed_cells.append(
                    list(observation["cells"].get((row, column), (None, None, None)))
                )
    compare(record, "source_cells", expected_cells, observed_cells)
    absent = [f for f, c in fixture.mappings.items() if c is None]
    actual_absent = [
        f for f, c in report["field_completeness"].items() if c["availability"] == "absent"
    ]
    compare(
        record,
        "data_quality",
        [absent, fixture.id != "E9", [(4, 5, TIV)] if fixture.id == "E9" else []],
        [actual_absent, report["valuation_complete"], observation["invalid"]],
    )


def compare_answer(
    record: dict,
    fixture: Fixture,
    question: Question,
    result: Success | Failure,
    semantic_comparator,
) -> None:
    categories = expected_failure(fixture, question)
    record["observed_answer_status"] = result.status
    if isinstance(result, Failure):
        record["application_error"] = result.model_dump()
        for name, check in record["checks"].items():
            if check["status"] == "not_run":
                check["dependency"] = (
                    "sql_generation" if name == "sql_semantics" else "verified_answer_unavailable"
                )
        expected_codes = {
            "Q4": "predicate_incomplete",
            "Q5": "predicate_incomplete",
            "Q8": "valuation_incomplete",
        }
        valid = result.category in categories and (
            fixture.id != "E9" or result.code == expected_codes[question.id]
        )
        compare(record, "outcome", True, valid, stage=error_stage(result.category))
        if result.category in ("provider_error", "configuration_error"):
            record["checks"]["outcome"].update(
                status="inconclusive", stage="evaluation_infrastructure"
            )
        # A valid material-completeness rejection still needs correct SQL intent.
        if result.category == "data_incomplete":
            record["checks"]["sql_semantics"]["dependency"] = "validated generation observation"
        return
    compare(
        record,
        "outcome",
        "failure" if categories else "verified",
        result.status,
        stage="sql_generation" if categories else None,
    )
    if categories:
        # No verified reference value exists for unavailable/incomplete information.
        # Fail substantively without trying to invent source cells for an absent field.
        compare(
            record, "answer", "explicit failure", result.result.model_dump(), stage="sql_generation"
        )
        for check in record["checks"].values():
            if check["status"] == "not_run":
                check["dependency"] = "expected_material_failure"
        return
    expected_identity = record["expected_dataset_identity"]
    compare(
        record,
        "identity",
        [expected_identity, expected_identity.get("dataset_id") if expected_identity else None],
        [result.dataset.model_dump(), result.plan.dataset_id],
    )
    sql_reference = question.sql
    if question.operation == "list":
        sql_reference = (
            "SELECT COUNT(*) FROM assets WHERE construction_type = 'Wood Frame - Podium'"
        )
    compare(
        record,
        "sql_semantics",
        True,
        result.operation == question.operation
        and semantic_comparator(
            result.executed_sql, sql_reference, question.operation, result.plan.result_fields
        ),
    )
    value = (
        tuple(r.asset_id for r in result.result.records)
        if isinstance(result.result, ListValue)
        else result.result.value
    )
    compare(
        record,
        "answer",
        [question.operation, question.value],
        [result.result.kind, value],
        stage="sql_validation_execution"
        if record["checks"]["sql_semantics"]["status"] == "passed"
        else "sql_generation",
    )
    expected_rows = {a: fixture.membership[a] for a in question.members}
    actual_rows = {
        c.asset_id: [r.row for r in c.source_records] for c in result.evidence.contributors
    }
    compare(
        record,
        "membership",
        [
            list(question.members),
            len(question.members),
            len(question.members),
            sum(map(len, expected_rows.values())),
            expected_rows,
        ],
        [
            [c.asset_id for c in result.evidence.contributors],
            result.matching_count,
            result.evidence.contributing_asset_count,
            result.evidence.source_record_count,
            actual_rows,
        ],
    )
    # Listings may select additional supported fields. Check every returned field
    # against independent population/cells, without demanding one SQL spelling.
    required = set(question.required) | (
        set(result.plan.result_fields) if question.operation == "list" else set()
    )
    expected, observed = [], []
    for asset in question.members:
        for row in fixture.membership[asset]:
            for field in sorted(required):
                expected.append(fixture.cell(asset, row, field))
    for contributor in result.evidence.contributors:
        for row in contributor.source_records:
            for cell in sorted(row.cells, key=lambda c: c.semantic_field):
                observed.append(
                    {
                        key: getattr(cell, key)
                        for key in (
                            "semantic_field",
                            "column_index",
                            "column_letter",
                            "cell_coordinate",
                            "source_header",
                            "original_value",
                            "normalized_value",
                        )
                    }
                )
    compare(
        record,
        "source_cells",
        [sorted(required), expected],
        [sorted(result.evidence.required_fields), observed],
    )
    values_match = all(
        getattr(c.values, f) == fixture.population.get(c.asset_id, {}).get(f)
        for c in result.evidence.contributors
        for f in required
    )
    listing_match = not isinstance(result.result, ListValue) or all(
        getattr(r, f) == fixture.population.get(r.asset_id, {}).get(f)
        for r in result.result.records
        for f in result.plan.result_fields
    )
    compare(
        record,
        "reconciliation",
        [
            True,
            True,
            True,
            4,
            5,
            True,
            True,
            len(question.members),
            len(question.members),
            question.value if question.operation == "sum" else None,
            question.value if question.operation == "sum" else None,
        ],
        [
            result.verification.reconciled,
            result.verification.complete,
            result.verification.same_snapshot,
            result.verification.dataset_logical_assets,
            result.verification.dataset_physical_records,
            values_match,
            listing_match,
            result.verification.executed_count,
            result.evidence.matched_asset_count,
            result.verification.result_micro_usd,
            result.verification.evidence_sum_micro_usd,
        ],
    )


def proposal_matches(
    fixture: Fixture, question: Question, generation: Generation, comparator
) -> bool:
    proposal = generation.proposal
    if proposal.status == "unsupported":
        return "unsupported_question" in expected_failure(fixture, question)
    reference = question.sql
    projection: tuple[str, ...] = ()
    if question.operation == "list":
        reference = "SELECT COUNT(*) FROM assets WHERE construction_type = 'Wood Frame - Podium'"
        try:
            tree = parse_one(proposal.sql or "", read="sqlite")
            projection = tuple(
                node.name for node in tree.expressions if isinstance(node, exp.Column)
            )
        except (SqlglotError, ValueError, RecursionError):
            return False
    return proposal.operation == question.operation and comparator(
        proposal.sql or "", reference, question.operation, projection
    )


def run_case(
    directory: Path,
    case_id: str,
    fixture: Fixture,
    questions: tuple[Question, ...],
    schema_provider,
    sql_factory,
    semantic_comparator,
) -> dict:
    record = declared_case(case_id, fixture, questions)
    record["attempted"] = True
    source, database = directory / f"{case_id}.xlsx", directory / f"{case_id}.sqlite"
    started = perf_counter()
    try:
        write_fixture(source, fixture)
        identity = workbook_identity(source)
        record["expected_dataset_identity"] = identity
        for qr in record["questions"]:
            qr["expected_dataset_identity"] = identity
        record["observed_ingestion"] = "not_completed"
        ingest_started = perf_counter()
        try:
            report = ingest_workbook(
                source,
                database,
                infer_schema=fixture.infer,
                inference_provider=schema_provider if fixture.infer else None,
            )
        finally:
            record["ingestion_seconds"] = perf_counter() - ingest_started
        record["observed_ingestion"] = "accepted"
        compare_import(record, fixture, observe_import(database, report))
        inference = report["schema_inference"]
        assert isinstance(inference, dict)
        if inference["status"] == "fallback":
            record["checks"]["inference_path"].update(
                status="inconclusive", stage="evaluation_infrastructure"
            )
            record["application_error"] = getattr(schema_provider, "failure", None)
        # Even an independently incorrect accepted mapping is observed through QA;
        # its failure remains visible and no repair is attempted.
        for question, qr in zip(questions, record["questions"], strict=True):
            generator = CapturedSQL(sql_factory(question))
            qa_started = perf_counter()
            try:
                result = ask(database, question.text, generator)
                compare_answer(qr, fixture, question, result, semantic_comparator)
                if isinstance(result, Failure) and generator.generation:
                    compare(
                        qr,
                        "sql_semantics",
                        True,
                        proposal_matches(
                            fixture, question, generator.generation, semantic_comparator
                        ),
                    )
            except BudgetExceeded as exc:
                infrastructure_failure(qr, exc)
                for check in qr["checks"].values():
                    if check["status"] == "not_run":
                        check["dependency"] = "provider_operation_budget"
                finish(qr)
                for downstream in record["questions"][record["questions"].index(qr) + 1 :]:
                    for check in downstream["checks"].values():
                        check["dependency"] = "provider_operation_budget"
                break
            except Exception as exc:
                infrastructure_failure(qr, exc)
            finally:
                qr["elapsed_seconds"] = perf_counter() - qa_started
            finish(qr)
    except IngestionError as exc:
        ingestion_error(record, fixture, exc, database.exists())
    except Exception as exc:
        infrastructure_failure(record, exc)
    record["elapsed_seconds"] = perf_counter() - started
    return finish(record)


def infrastructure_failure(record: dict, exc: Exception) -> None:
    # Unexpected runner exceptions carry type/message, never hidden model content.
    record["infrastructure_error"] = {
        "type": type(exc).__name__,
        "message": str(exc)
        if isinstance(exc, (BudgetExceeded, AssertionError, KeyError))
        else "Runner failed; no provider payload retained",
    }
    record["checks"]["infrastructure"] = {"status": "failed", "stage": "evaluation_infrastructure"}


def summarize(cases: list[dict]) -> dict:
    questions = [q for case in cases for q in case["questions"]]
    all_checks = [check for record in [*cases, *questions] for check in record["checks"].values()]

    def counts(records):
        return {
            s: sum(r["status"] == s for r in records)
            for s in ("passed", "failed", "inconclusive", "not_run")
        }

    supported = [q for q in questions if q["supported"]]
    return {
        "declared_cases": len(cases),
        "attempted_cases": sum(c["attempted"] for c in cases),
        "cases": counts(cases),
        "declared_questions": len(questions),
        "attempted_questions": sum("elapsed_seconds" in q for q in questions),
        "questions": counts(questions),
        "declared_checks": len(all_checks),
        "checks": counts(all_checks),
        "mapping_outcomes": dict(Counter(c["observed_ingestion"] for c in cases)),
        "supported_questions": {
            "denominator": len(supported),
            "passed": sum(q["status"] == "passed" for q in supported),
        },
        "unexpected_verified_mismatches": sum(
            q.get("observed_answer_status") == "verified" and q["status"] == "failed"
            for q in questions
        ),
        "application_error_counts": dict(
            Counter(
                r["application_error"]["category"]
                for r in [*cases, *questions]
                if r.get("application_error")
            )
        ),
        "failure_stages": dict(
            Counter(
                check["stage"]
                for check in all_checks
                if check["status"] in ("failed", "inconclusive")
            )
        ),
    }
