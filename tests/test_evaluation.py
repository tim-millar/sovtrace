"""synthetic oracle/evaluator self-tests. All providers are deterministic; no credentials."""

import json
import sys
import unicodedata
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from runpy import run_path
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook

from sovtrace.evaluation.corpus import (
    BY_ID,
    FIXTURES,
    QUESTIONS,
    TIV,
    workbook_identity,
    write_fixture,
)
from sovtrace.evaluation.offline import (
    FixedSchema,
    FixedSQL,
    compare_answer,
    compare_import,
    declared_case,
    finish,
    ingestion_error,
    observe_import,
    report_outcome,
    run_case,
    summarize,
)
from sovtrace.ingestion import ingest_workbook
from sovtrace.ingestion.errors import FailureCategory, IngestionError
from sovtrace.qa import Success, ask


@pytest.fixture(scope="module")
def runner():
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
        yield run_path(str(Path(__file__).parents[1] / "scripts/verify_synthetic.py"))


@pytest.fixture
def imported(tmp_path):
    def make(identifier):
        fixture = BY_ID[identifier]
        source, db = tmp_path / f"{identifier}.xlsx", tmp_path / f"{identifier}.sqlite"
        write_fixture(source, fixture)
        report = ingest_workbook(
            source,
            db,
            infer_schema=fixture.infer,
            inference_provider=FixedSchema(fixture) if fixture.infer else None,
        )
        return fixture, db, observe_import(db, report)

    return make


def test_complete_offline_suite_and_accounting(runner, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    report = runner["run_offline"]()
    assert report["status"] == "passed"
    assert report["summary"]["declared_cases"] == 14
    assert report["summary"]["declared_questions"] == 80
    assert report["summary"]["questions"]["passed"] == 80
    assert report["actual_live_provider_calls"] == 0
    assert "gate_3" not in report
    assert report["evaluator_version"] == "synthetic-report-v3"
    assert report["fixture_oracle_version"] == "synthetic-corpus-oracle-v1"
    runner["validate_report"](report)
    for tamper in ("omission", "miscount"):
        wrong = deepcopy(report)
        if tamper == "omission":
            wrong["cases"].pop(6)
        else:
            wrong["summary"]["attempted_questions"] -= 1
        with pytest.raises(AssertionError):
            runner["validate_report"](wrong)


def test_corpus_is_byte_reproducible_and_e11_layout_is_exact(tmp_path):
    first, second = tmp_path / "first.xlsx", tmp_path / "second.xlsx"
    for fixture in FIXTURES:
        write_fixture(first, fixture)
        write_fixture(second, fixture)
        assert first.read_bytes() == second.read_bytes()
    wb = load_workbook(first)
    sheet = wb.active
    assert sheet is not None
    assert [sheet.cell(8, c).value for c in range(1, 15)] == [
        "Building",
        "Premises",
        "Location Reference",
        "Municipality",
        "Contents",
        "US State",
        "Rental Income",
        "Street Level Address",
        "Construction Quality",
        "Cladding Type",
        "Total Insured Value",
        "Cladding Type",
        "Building Construction",
        "Construction Quality",
    ]
    assert sheet["K9"].value == sheet["K13"].value == "$12,000,000.00"
    assert sheet["A14"].value == "=SUM(A9:A13)"
    assert sheet["K14"].value == "=SUM(K9:K13)"
    wb.close()


@pytest.mark.parametrize(
    "identifier,mutation,check",
    [
        ("E11", "wrong_column", "mapping"),
        ("E9", "zero", "population"),
        ("E5", "fabrication", "population"),
    ],
)
def test_independent_import_oracle_rejects_bad_observations(imported, identifier, mutation, check):
    fixture, db, observation = imported(identifier)
    bad = deepcopy(observation)
    if mutation == "wrong_column":
        bad["manifest"]["concepts"]["state"]["column"] = 7
    elif mutation == "zero":
        bad["population"]["SYN-003"][TIV] = 0
    else:
        bad["population"]["SYN-001"]["construction_type"] = "Wood Frame - Podium"
    record = declared_case(
        identifier, fixture, (), identity=workbook_identity(db.with_suffix(".xlsx"))
    )
    compare_import(record, fixture, bad)
    assert finish(record)["status"] == "failed"
    assert record["checks"][check]["status"] == "failed"


def test_unsupported_acceptance_is_not_success(imported):
    _, _, observation = imported("E0a")
    fixture = BY_ID["E10b"]
    record = declared_case("E10b", fixture, ())
    compare_import(record, fixture, observation)
    assert finish(record)["status"] == "failed"


def test_timeout_is_not_a_passing_missing_mapping():
    fixture = BY_ID["E6"]
    record = declared_case("E6", fixture, ())
    ingestion_error(
        record,
        fixture,
        IngestionError(FailureCategory.INFERENCE, "schema_inference_provider_error: timeout"),
        False,
    )
    assert finish(record)["status"] == "inconclusive"
    assert record["application_error"]["code"] == "schema_inference_provider_error"


@pytest.mark.parametrize(
    "mutation,expected_check",
    [
        ("wrong_members", "membership"),
        ("wrong_operator", "sql_semantics"),
        ("old_coordinate", "source_cells"),
        ("missing_duplicate", "membership"),
        ("verified_wrong_value", "answer"),
        ("wrong_sql_and_value", "sql_semantics"),
    ],
)
def test_verified_status_cannot_hide_bad_interpretation_or_evidence(
    imported, runner, mutation, expected_check
):
    fixture, db, _ = imported("E11")
    question = QUESTIONS[4]
    result = ask(db, question.text, FixedSQL(question))
    assert isinstance(result, Success)
    if mutation == "wrong_members":
        contributor = result.evidence.contributors[0].model_copy(update={"asset_id": "SYN-004"})
        result = result.model_copy(
            update={"evidence": result.evidence.model_copy(update={"contributors": (contributor,)})}
        )
    elif mutation in ("wrong_operator", "wrong_sql_and_value"):
        wrong = result.executed_sql.replace(" > ", " >= ")
        result = result.model_copy(
            update={"executed_sql": wrong, "plan": replace(result.plan, executed_sql=wrong)}
        )
        if mutation == "wrong_sql_and_value":
            result = result.model_copy(
                update={"result": result.result.model_copy(update={"value": 1})}
            )
    elif mutation in ("old_coordinate", "missing_duplicate"):
        contributor = result.evidence.contributors[0]
        rows = contributor.source_records
        if mutation == "old_coordinate":
            row = rows[0]
            cells = tuple(
                c.model_copy(update={"cell_coordinate": "E2"}) if c.semantic_field == TIV else c
                for c in row.cells
            )
            rows = (row.model_copy(update={"cells": cells}), *rows[1:])
        else:
            rows = rows[:1]
        contributor = contributor.model_copy(update={"source_records": rows})
        result = result.model_copy(
            update={"evidence": result.evidence.model_copy(update={"contributors": (contributor,)})}
        )
    else:
        result = result.model_copy(update={"result": result.result.model_copy(update={"value": 1})})
    qr = declared_case(
        "E11", fixture, (question,), identity=workbook_identity(db.with_suffix(".xlsx"))
    )["questions"][0]
    compare_answer(qr, fixture, question, result, runner["matches_reference_sql"])
    assert result.status == "verified"
    assert finish(qr)["status"] == "failed"
    assert qr["checks"][expected_check]["status"] == "failed"
    stage = {
        "verified_wrong_value": "sql_validation_execution",
        "wrong_operator": "sql_generation",
        "wrong_sql_and_value": "sql_generation",
    }.get(mutation, "evidence_reconciliation")
    assert qr["checks"][expected_check]["stage"] == stage
    assert qr["failure_stages"] == [stage]


def test_wrong_sql_with_same_answer_is_generation_failure(imported, runner):
    fixture, db, _ = imported("E0a")
    # Same amount and membership, wrong question meaning: evidence reconciliation legitimately verifies it.
    question = QUESTIONS[4]
    wrong_question = replace(
        question,
        sql="SELECT SUM(total_insurable_value_micro_usd) FROM assets WHERE asset_id = 'SYN-001'",
    )
    result = ask(db, "Sum the selected asset", FixedSQL(wrong_question))
    assert isinstance(result, Success)
    qr = declared_case(
        "E0a", fixture, (question,), identity=workbook_identity(db.with_suffix(".xlsx"))
    )["questions"][0]
    compare_answer(qr, fixture, question, result, runner["matches_reference_sql"])
    assert qr["checks"]["answer"]["status"] == "passed"
    assert qr["checks"]["sql_semantics"]["status"] == "failed"
    assert "sql_generation" in finish(qr)["failure_stages"]


def test_original_failed_reports_are_archived(runner, tmp_path):
    report = runner["run_offline"]()
    report["status"] = "failed"
    original = runner["save_report"](report, tmp_path)
    original_content = original.read_bytes()
    report["utc_timestamp"] += "-followup"
    report["status"] = "passed"
    runner["save_report"](report, tmp_path)
    assert original.read_bytes() == original_content


class SuiteSchema:
    model = "injected-schema"

    def infer(self, request):
        from sovtrace.evaluation.corpus import FIELDS
        from sovtrace.ingestion.inference import InferenceResult, SchemaProposal

        # Test double only: predeclared labels, with no values or production helpers.
        labels = (
            "Location Reference",
            "Premises",
            "Municipality",
            "US State",
            "Total Insured Value",
            "Building Construction",
        )
        exact = (
            "EPIC Code",
            "Property Name",
            "City",
            "State",
            "Total Insurable Values",
            "Construction Type",
        )
        columns = {c.label: c.index for c in request.columns}
        proposal = {}
        for field, renamed, known in zip(FIELDS, labels, exact, strict=True):
            column = columns.get(renamed, columns.get(known))
            proposal[field] = {
                "status": "absent" if column is None else "mapped",
                "column": column,
                "candidates": [] if column is None else [column],
            }
        return InferenceResult(proposal=SchemaProposal.model_validate(proposal), model=self.model)


class SuiteSQL:
    model = "injected-sql"

    def generate(self, question, schema):
        return FixedSQL(next(q for q in QUESTIONS if q.text == question)).generate(question, schema)


def test_live_fixed_matrix_with_injected_providers_only(runner):
    report = runner["run_live"](SuiteSchema(), SuiteSQL())
    assert report["status"] == "passed"
    assert report["provider_mode"] == "injected"
    assert report["actual_live_provider_calls"] == 0
    assert report["measurements"]["actual_provider_calls"] == 23
    assert report["summary"]["declared_questions"] == 19
    assert report["summary"]["questions"]["passed"] == 19
    assert report["cases"][4]["schema_inference_attribution"] == "deterministic_no_provider_call"
    assert report["measurements"]["cost"]["status"] == "unavailable"


def test_live_ingestion_failure_keeps_skipped_question_denominator(runner):
    class TimeoutSchema(SuiteSchema):
        def infer(self, request):
            raise IngestionError(
                FailureCategory.INFERENCE, "schema_inference_provider_error: timeout"
            )

    report = runner["run_live"](TimeoutSchema(), SuiteSQL())
    assert report["status"] == "inconclusive"
    assert report["summary"]["declared_questions"] == 19
    assert report["summary"]["questions"]["not_run"] == 16
    assert report["summary"]["questions"]["passed"] == 3
    # E5's independently valid required exact mappings permit technical fallback;
    # this is not successful live inference and is not counted as passed.
    assert report["cases"][2]["schema_inference_attribution"] == "technical_fallback"
    assert report["cases"][2]["status"] == "inconclusive"
    assert report["cases"][3]["status"] == "inconclusive"
    assert report["cases"][3]["application_error"]["message"].endswith("timeout")
    assert all(
        q["checks"]["outcome"]["dependency"] == "L1.ingestion"
        for q in report["cases"][0]["questions"]
    )
    assert report["measurements"]["actual_provider_calls"] == 7


def test_live_budget_exhaustion_happens_before_provider_call(runner):
    report = runner["run_live"](SuiteSchema(), SuiteSQL(), limit=2)
    assert report["status"] == "failed"
    assert report["measurements"]["actual_provider_calls"] == 2
    assert report["measurements"]["blocked_operations"] == 1
    assert report["summary"]["declared_questions"] == 19
    assert report["summary"]["questions"]["not_run"] == 17
    assert report["cases"][0]["questions"][1]["status"] == "failed"
    assert report["cases"][1]["status"] == "not_run"
    with pytest.raises(ValueError, match="30-operation"):
        runner["run_live"](SuiteSchema(), SuiteSQL(), limit=31)


def test_absolute_budget_and_failed_calls_are_counted():
    from sovtrace.evaluation.live import BudgetExceeded, Ledger

    ledger = Ledger()
    calls = []

    def failure():
        calls.append(1)
        raise RuntimeError("injected failure")

    for _ in range(30):
        with pytest.raises(RuntimeError):
            ledger.invoke("test", None, "schema_inference", "fake", failure)
    with pytest.raises(BudgetExceeded):
        ledger.invoke("test", None, "schema_inference", "fake", failure)
    assert len(calls) == ledger.calls == 30
    assert len(ledger.operations) == 31


def test_live_sql_failure_is_retained_and_not_retried(runner):
    from sovtrace.qa.contracts import QAError

    class TimeoutSQL(SuiteSQL):
        def generate(self, question, schema):
            raise QAError("provider_error", "timeout", "Injected timeout")

    report = runner["run_live"](SuiteSchema(), TimeoutSQL())
    assert report["summary"]["declared_questions"] == 19
    assert report["summary"]["questions"]["inconclusive"] == 19
    assert report["measurements"]["actual_provider_calls"] == 23
    assert report["status"] == "inconclusive"
    assert report["measurements"]["tokens"]["input_tokens"] is None


def test_missing_live_credentials_preserves_entire_matrix_without_calls(runner, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    report = runner["run_live"]()
    assert report["summary"]["declared_cases"] == 5
    assert report["summary"]["declared_questions"] == 19
    assert report["summary"]["questions"]["not_run"] == 19
    assert report["status"] == "inconclusive"
    assert report["measurements"]["actual_provider_calls"] == 0


def test_cost_accounting_with_artificial_rates_without_double_reasoning(runner):
    from sovtrace.evaluation.live import Ledger, measurements

    ledger = Ledger()
    ledger.operations = [
        {
            "case": "L1",
            "question": "Q1",
            "operation": "sql_generation",
            "actual_call": True,
            "requested_model": "artificial-cost-model",
            "actual_model": "artificial-cost-model",
            "elapsed_seconds": 0.1,
            "service_tier": "default",
            "outcome": "response",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "cache_write_tokens": 10,
                "output_tokens": 50,
                "reasoning_tokens": 30,
            },
        }
    ]
    from datetime import date
    from decimal import Decimal
    from functools import partial

    from sovtrace.evaluation.cost import PricingSchedule

    rates = PricingSchedule(
        model="artificial-cost-model",
        service_tier="default",
        source="Artificial test-only rates; not provider prices",
        verified_on=date(2026, 1, 1),
        ordinary_input=Decimal("2"),
        cached_input=Decimal("0.5"),
        cache_write=Decimal("3"),
        output=Decimal("7"),
    )
    estimator = partial(runner["estimated_cost"], pricing=rates)
    expected = (
        Decimal(70) * 2 + Decimal(20) * Decimal("0.5") + Decimal(10) * 3 + Decimal(50) * 7
    ) / Decimal(1_000_000)
    result = measurements([], ledger, estimator)
    assert Decimal(result["cost"]["overall_usd"]) == expected
    assert result["cost"]["status"] == "available"
    ledger.operations[0]["usage"]["reasoning_tokens"] = 0
    assert Decimal(measurements([], ledger, estimator)["cost"]["overall_usd"]) == expected
    assert measurements([], ledger, runner["estimated_cost"])["cost"]["overall_usd"] is None
    ledger.operations[0]["usage"]["cache_write_tokens"] = None
    result = measurements([], ledger, estimator)
    assert result["cost"]["overall_usd"] is None


def test_fabricated_verified_construction_is_substantive_failure(imported, runner):
    _, db, _ = imported("E0a")
    question = QUESTIONS[5]
    result = ask(db, question.text, FixedSQL(question))
    assert isinstance(result, Success)
    fixture = BY_ID["E5"]
    qr = declared_case("E5", fixture, (question,))["questions"][0]
    compare_answer(qr, fixture, question, result, runner["matches_reference_sql"])
    assert finish(qr)["status"] == "failed"
    assert qr["failure_stages"] == ["sql_generation"]


# Semantic regressions: fixed corpus and real public pipeline, injected SQL only.
@pytest.mark.parametrize(
    "question_index,sql",
    [
        (1, "SELECT COUNT(*) FROM assets WHERE (state = 'AZ')"),
        (1, "SELECT COUNT(*) FROM assets WHERE (((state = 'AZ')))"),
        (
            3,
            "SELECT COUNT(*) FROM assets WHERE ((state = 'AZ') AND (total_insurable_value_micro_usd > 10000000000000))",
        ),
        (
            3,
            "SELECT COUNT(*) FROM assets WHERE (((total_insurable_value_micro_usd > 10000000000000)) AND ((state = 'AZ')))",
        ),
    ],
)
def test_evaluation_accepts_redundant_parentheses_through_injected_pipeline(
    tmp_path, runner, question_index, sql
):
    fixture, question = BY_ID["E0a"], QUESTIONS[question_index]
    case = run_case(
        tmp_path,
        "E0a",
        fixture,
        (question,),
        FixedSchema(fixture),
        lambda _: FixedSQL(replace(question, sql=sql)),
        runner["matches_reference_sql"],
    )
    assert case["status"] == "passed"
    assert all(c["status"] == "passed" for c in case["questions"][0]["checks"].values())


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM assets WHERE (state = 'AZ') AND (total_insurable_value_micro_usd >= 10000000000000)",
        "SELECT COUNT(*) FROM assets WHERE (state = 'NY') AND (total_insurable_value_micro_usd > 10000000000000)",
        "SELECT COUNT(*) FROM assets WHERE (state = 'AZ') AND (total_insurable_value_micro_usd > 11000000000000)",
        "SELECT COUNT(*) FROM assets WHERE (state = 'AZ') OR (total_insurable_value_micro_usd > 10000000000000)",
    ],
)
def test_evaluation_parentheses_never_hide_wrong_predicate(tmp_path, runner, sql):
    fixture, question = BY_ID["E0a"], QUESTIONS[3]
    case = run_case(
        tmp_path,
        "E0a",
        fixture,
        (question,),
        FixedSchema(fixture),
        lambda _: FixedSQL(replace(question, sql=sql)),
        runner["matches_reference_sql"],
    )
    assert case["status"] == "failed"
    check = case["questions"][0]["checks"]["sql_semantics"]
    assert check["status"] == "failed" and check["stage"] == "sql_generation"


def test_evaluation_parentheses_preserve_boolean_precedence(runner):
    reference = "SELECT COUNT(*) FROM assets WHERE (state = 'AZ' OR state = 'NY') AND total_insurable_value_micro_usd > 10000000000000"
    equivalent = "SELECT COUNT(*) FROM assets WHERE ((total_insurable_value_micro_usd > 10000000000000)) AND (((state = 'AZ') OR (state = 'NY')))"
    wrong = "SELECT COUNT(*) FROM assets WHERE state = 'AZ' OR (state = 'NY' AND total_insurable_value_micro_usd > 10000000000000)"
    assert runner["matches_reference_sql"](equivalent, reference, "count")
    assert not runner["matches_reference_sql"](wrong, reference, "count")
    # Tables and projections retain the shared comparator's existing restrictions.
    assert not runner["matches_reference_sql"](
        equivalent.replace("FROM assets", "FROM other"), reference, "count"
    )
    assert not runner["matches_reference_sql"](
        equivalent.replace("COUNT(*)", "SUM(total_insurable_value_micro_usd)"), reference, "count"
    )


@pytest.mark.parametrize("identifier,question_index", [("E5", 5), ("E9", 3), ("E9", 7)])
def test_rejected_proposals_use_same_parentheses_comparison(
    tmp_path, runner, identifier, question_index
):
    fixture, question = BY_ID[identifier], QUESTIONS[question_index]
    sql = question.sql.replace(" WHERE ", " WHERE (((")
    sql += ")))" if " WHERE " in question.sql else ""
    case = run_case(
        tmp_path,
        identifier,
        fixture,
        (question,),
        FixedSchema(fixture),
        lambda _: FixedSQL(replace(question, sql=sql)),
        runner["matches_reference_sql"],
    )
    assert case["status"] == "passed"
    qr = case["questions"][0]
    assert qr["observed_answer_status"] == "failure"
    assert qr["checks"]["sql_semantics"]["status"] == "passed"


@pytest.mark.parametrize("identifier", ["E0a", "E11"])
def test_independent_identity_passes_row_one_and_row_eight(imported, runner, identifier):
    fixture, db, observation = imported(identifier)
    identity = workbook_identity(db.with_suffix(".xlsx"))
    record = declared_case(identifier, fixture, (), identity=identity)
    compare_import(record, fixture, observation)
    assert finish(record)["status"] == "passed"
    assert identity["workbook_filename"] == identifier + ".xlsx"
    assert identity["dataset_id"] == identity["workbook_sha256"] + ":Statement of Values"
    for question in (QUESTIONS[1], QUESTIONS[6]):
        result = ask(db, question.text, FixedSQL(question))
        qr = declared_case(identifier, fixture, (question,), identity=identity)["questions"][0]
        compare_answer(qr, fixture, question, result, runner["matches_reference_sql"])
        assert finish(qr)["status"] == "passed"


@pytest.mark.parametrize("question_index", [1, 6])
@pytest.mark.parametrize(
    "component",
    ["workbook_sha256", "workbook_filename", "worksheet", "dataset_id", "plan_dataset_id"],
)
def test_answer_identity_corruption_including_empty_contributors(
    imported, runner, question_index, component
):
    fixture, db, _ = imported("E11")
    question = QUESTIONS[question_index]
    result = ask(db, question.text, FixedSQL(question))
    assert isinstance(result, Success)
    if question_index == 6:
        assert not result.evidence.contributors
    if component == "plan_dataset_id":
        result = result.model_copy(update={"plan": replace(result.plan, dataset_id="unrelated")})
    else:
        result = result.model_copy(
            update={"dataset": result.dataset.model_copy(update={component: "unrelated"})}
        )
    qr = declared_case(
        "E11", fixture, (question,), identity=workbook_identity(db.with_suffix(".xlsx"))
    )["questions"][0]
    compare_answer(qr, fixture, question, result, runner["matches_reference_sql"])
    assert finish(qr)["status"] == "failed"
    assert qr["failure_stages"] == ["evidence_reconciliation"]
    assert qr["checks"]["identity"]["status"] == "failed"
    assert qr["checks"]["answer"]["status"] == "passed"


@pytest.mark.parametrize("target", ["dataset", "manifest", "import_report"])
@pytest.mark.parametrize("component", ["workbook_sha256", "workbook_filename", "worksheet"])
def test_persisted_identity_never_supplies_oracle_expectations(imported, target, component):
    import json
    import sqlite3

    fixture, db, observation = imported("E0a")
    identity = workbook_identity(db.with_suffix(".xlsx"))
    report = deepcopy(observation["report"])
    if target == "import_report":
        report[
            {
                "workbook_sha256": "sha256",
                "workbook_filename": "workbook",
                "worksheet": "worksheet",
            }[component]
        ] = "unrelated"
    else:
        with sqlite3.connect(db) as connection:
            if target == "dataset":
                connection.execute(f"UPDATE dataset SET {component} = ?", ("unrelated",))
            else:
                manifest = json.loads(
                    connection.execute("SELECT manifest_json FROM dataset").fetchone()[0]
                )
                manifest[component] = "unrelated"
                connection.execute("UPDATE dataset SET manifest_json = ?", (json.dumps(manifest),))
    observed = observe_import(db, report)
    record = declared_case("E0a", fixture, (), identity=identity)
    compare_import(record, fixture, observed)
    assert finish(record)["status"] == "failed"
    assert record["checks"]["identity"]["stage"] == "normalization"
    assert record["checks"]["identity"]["status"] == "failed"
    assert workbook_identity(db.with_suffix(".xlsx")) == identity


@pytest.mark.parametrize(
    "message,category,expected",
    [
        (
            "Expected one SOV table; found 2 candidate headers: Statement of Values!1, Second table!1",
            FailureCategory.STRUCTURE,
            "passed",
        ),
        ("Expected one SOV table; found 0 candidate headers", FailureCategory.STRUCTURE, "failed"),
        ("Expected one SOV table; found 1 candidate headers", FailureCategory.STRUCTURE, "failed"),
        ("Unexpected table boundary", FailureCategory.STRUCTURE, "failed"),
        ("schema_inference_provider_error: timeout", FailureCategory.INFERENCE, "inconclusive"),
        (
            "Unrelated: Expected one SOV table; found 2 candidate headers",
            FailureCategory.STRUCTURE,
            "failed",
        ),
    ],
)
def test_e10b_requires_substantive_two_candidate_rejection(message, category, expected):
    fixture = BY_ID["E10b"]
    record = declared_case("E10b", fixture, ())
    ingestion_error(record, fixture, IngestionError(category, message), False)
    assert finish(record)["status"] == expected
    if expected == "passed":
        published = declared_case("E10b", fixture, ())
        ingestion_error(published, fixture, IngestionError(category, message), True)
        assert finish(published)["status"] == "failed"


@pytest.mark.parametrize(
    "identifier,prefix",
    [
        ("E10a", "Statement of Values!1: unsupported document metadata"),
        ("E10b", "Expected one SOV table; found 2 candidate headers"),
    ],
)
def test_real_structural_rejections_remain_substantive(tmp_path, runner, identifier, prefix):
    fixture = BY_ID[identifier]
    case = run_case(
        tmp_path,
        identifier,
        fixture,
        (),
        FixedSchema(fixture),
        FixedSQL,
        runner["matches_reference_sql"],
    )
    assert case["status"] == "passed"
    assert case["application_error"]["message"].startswith(prefix)
    assert not (tmp_path / (identifier + ".sqlite")).exists()


@pytest.fixture(scope="module")
def offline_report(runner):
    return runner["run_offline"]()


@pytest.mark.parametrize(
    "tamper",
    [
        "skipped_question",
        "skipped_check",
        "failed_case",
        "inconclusive_case",
        "omitted_question",
        "attempts",
        "checks",
        "missing_check",
        "skip_without_dependency",
        "attempted_not_run",
        "technical_failure_as_rejection",
    ],
)
def test_report_validator_rejects_incomplete_or_inconsistent_evaluation(
    runner, offline_report, tamper
):
    report = deepcopy(offline_report)
    case, qr = report["cases"][0], report["cases"][0]["questions"][0]
    if tamper == "attempted_not_run":
        for check in qr["checks"].values():
            check.update(status="not_run", dependency="verified_answer_unavailable")
        finish(qr)
        finish(case)
    elif tamper == "technical_failure_as_rejection":
        report["cases"][6]["questions"][5]["application_error"]["category"] = "provider_error"
    elif tamper == "skipped_question":
        qr.pop("elapsed_seconds")
        qr.pop("observed_answer_status")
        for check in qr["checks"].values():
            check.update(status="not_run", dependency="E0a.ingestion")
        finish(qr)
        finish(case)
    elif tamper == "skipped_check":
        qr["checks"]["membership"].update(
            status="not_run", dependency="verified_answer_unavailable"
        )
        finish(qr)
        finish(case)
    elif tamper in ("failed_case", "inconclusive_case"):
        qr["checks"]["answer"]["status"] = "failed" if tamper == "failed_case" else "inconclusive"
        finish(qr)
        finish(case)
        # Deliberately keep the top-level report claiming success.
    elif tamper == "omitted_question":
        case["questions"].pop()
    elif tamper == "missing_check":
        qr["checks"].pop("identity")
        finish(qr)
        finish(case)
    elif tamper == "skip_without_dependency":
        negative = report["cases"][6]["questions"][5]
        negative["checks"]["membership"].pop("dependency")
        finish(negative)
        finish(report["cases"][6])
    if tamper not in ("failed_case", "inconclusive_case"):
        report["status"] = report_outcome(report["cases"])
    report["summary"] = summarize(report["cases"])
    if tamper == "attempts":
        report["summary"]["attempted_questions"] -= 1
    elif tamper == "checks":
        report["summary"]["declared_checks"] -= 1
    with pytest.raises(AssertionError):
        runner["validate_report"](report)


def test_report_completion_accepts_only_expected_negative_skips(runner, offline_report):
    runner["validate_report"](offline_report)
    assert offline_report["status"] == "passed"
    for case in offline_report["cases"]:
        for qr in case["questions"]:
            if not qr["supported"]:
                assert qr["status"] == "passed" and qr["observed_answer_status"] == "failure"
                assert all(
                    c.get("dependency") == "verified_answer_unavailable"
                    for c in qr["checks"].values()
                    if c["status"] == "not_run"
                )
    assert all(
        case["status"] == "passed"
        for case in offline_report["cases"]
        if case["id"] in ("E6", "E7", "E10a", "E10b")
    )


def test_value_and_evidence_disagreements_remain_separately_attributed(imported, runner):
    fixture, db, _ = imported("E11")
    question = QUESTIONS[4]
    result = ask(db, question.text, FixedSQL(question))
    assert isinstance(result, Success)
    contributor = result.evidence.contributors[0]
    contributor = contributor.model_copy(update={"source_records": contributor.source_records[:1]})
    result = result.model_copy(
        update={
            "result": result.result.model_copy(update={"value": 1}),
            "evidence": result.evidence.model_copy(update={"contributors": (contributor,)}),
        }
    )
    qr = declared_case(
        "E11", fixture, (question,), identity=workbook_identity(db.with_suffix(".xlsx"))
    )["questions"][0]
    compare_answer(qr, fixture, question, result, runner["matches_reference_sql"])
    assert finish(qr)["failure_stages"] == ["evidence_reconciliation", "sql_validation_execution"]
    assert qr["checks"]["answer"]["status"] == qr["checks"]["membership"]["status"] == "failed"


def test_question_failure_takes_precedence_over_inconclusive_ingestion(runner):
    from sovtrace.qa.contracts import QAError

    class TimeoutSchema(SuiteSchema):
        def infer(self, request):
            raise IngestionError(
                FailureCategory.INFERENCE, "schema_inference_provider_error: timeout"
            )

    class MixedSQL(SuiteSQL):
        def generate(self, question, schema):
            if question == QUESTIONS[1].text:
                bad = replace(QUESTIONS[1], sql="SELECT COUNT(*) FROM assets WHERE state = 'NY'")
                return FixedSQL(bad).generate(question, schema)
            raise QAError("provider_error", "timeout", "Injected SQL timeout")

    report = runner["run_live"](TimeoutSchema(), MixedSQL())
    # L3 has a permissible technical inference fallback; its wrong Q2 is still failed.
    case = report["cases"][2]
    assert case["checks"]["inference_path"]["status"] == "inconclusive"
    assert case["questions"][0]["status"] == case["status"] == "failed"
    assert report["status"] == "failed"
    assert "sql_generation" in case["failure_stages"]
    assert "evaluation_infrastructure" in case["failure_stages"]
    runner["validate_report"](report)


def test_reporting_offline_authoritative_totals_and_categories(runner, offline_report):
    before = deepcopy(offline_report)
    text = runner["render_summary"](offline_report)
    for fragment in (
        "Synthetic cross-workbook evaluation",
        "OFFLINE — fixed/injected providers; no real model calls",
        "Report status: PASSED",
        "Fixtures: declared 14; attempted 14",
        "passed 14; failed 0; inconclusive 0; not run 0",
        "Questions: declared 80; attempted 80",
        "passed 80; failed 0; inconclusive 0; not run 0",
        "Successfully verified supported questions: 76",
        "Expected unsupported/unavailable refusals passed: 4",
        "Expected ingestion rejections passed: 4",
        "Unexpected failures: 0 fixtures; 0 questions",
        "Observed model identifiers (fixed/injected): fake-schema, fake-sql",
        "Actual live provider calls: 0",
    ):
        assert fragment in text
    assert "Timestamped archive:" not in text and "Latest report:" not in text
    assert offline_report == before


@pytest.mark.parametrize("status,exit_code", [("passed", 0), ("failed", 1), ("inconclusive", 2)])
def test_reporting_main_stdout_saved_payload_and_exits(
    runner, offline_report, tmp_path, monkeypatch, capsys, status, exit_code
):
    report = deepcopy(offline_report)
    if status != "passed":
        case = report["cases"][0]
        case["questions"][0]["checks"]["answer"]["status"] = status
        finish(case["questions"][0])
        finish(case)
        report["summary"] = summarize(report["cases"])
        report["status"] = report_outcome(report["cases"])
    runner["validate_report"](report)
    before = deepcopy(report)
    evaluator = Mock(return_value=report)
    monkeypatch.setitem(runner["main"].__globals__, "run_offline", evaluator)
    monkeypatch.setitem(runner["main"].__globals__, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify_synthetic.py", "--offline"])
    assert runner["main"]() == exit_code
    captured = capsys.readouterr()
    stdout = json.loads(captured.out)
    archive = Path(stdout["archive"])
    latest = archive.parent / "offline-report.json"
    assert (
        captured.out
        == json.dumps(
            {"status": status, "summary": report["summary"], "archive": str(archive)},
            sort_keys=True,
        )
        + "\n"
    )
    assert (
        archive.read_text()
        == latest.read_text()
        == json.dumps(before, indent=2, sort_keys=True) + "\n"
    )
    assert report == before
    assert f"Timestamped archive: {archive}" in captured.err
    assert f"Latest report: {latest}" in captured.err
    assert f"Report status: {status.upper()}" in captured.err
    if status != "passed":
        assert f"E0a/Q1: {status}; stages: sql_validation_execution" in captured.err
    evaluator.assert_called_once_with()


def test_reporting_live_injected_and_actual_identifiers(runner):
    injected = runner["run_live"](SuiteSchema(), SuiteSQL())
    before = deepcopy(injected)
    text = runner["render_summary"](injected)
    assert "LIVE orchestration — injected test providers; no real model calls" in text
    assert "Successfully verified supported questions: 18" in text
    assert "Expected unsupported/unavailable refusals passed: 1" in text
    assert "Expected ingestion rejections passed: 2" in text
    assert "Actual live provider calls: 0" in text
    assert injected == before
    # Synthetic report evidence tests presentation of an actual-provider run;
    # no real providers are invoked by this test.
    actual = deepcopy(injected)
    actual.update(
        provider_mode="actual", actual_live_provider_calls=23, actual_models=["observed-model"]
    )
    actual["configured_models"] = {"schema": "configured-schema", "sql": "configured-sql"}
    text = runner["render_summary"](actual)
    assert "LIVE — actual OpenAI providers" in text
    assert "Actual live provider calls: 23" in text
    assert "Observed model identifiers: observed-model" in text
    assert "Configured models: schema=configured-schema, sql=configured-sql" in text
    actual.update(actual_live_provider_calls=0, actual_models=[])
    text = runner["render_summary"](actual)
    assert "actual model execution unconfirmed" in text
    assert "Observed model identifiers: not recorded" in text


def test_reporting_missing_credentials_no_requests(runner, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    forbidden = Mock(side_effect=AssertionError("No provider request is allowed"))
    monkeypatch.setattr("sovtrace.qa.provider.OpenAIGenerator.generate", forbidden)
    monkeypatch.setattr(
        "sovtrace.ingestion.provider.OpenAISchemaInferenceProvider.infer", forbidden
    )
    save = runner["save_report"]
    monkeypatch.setitem(
        runner["main"].__globals__,
        "save_report",
        lambda report, directory: save(report, tmp_path / "reports"),
    )
    monkeypatch.setattr(sys, "argv", ["verify_synthetic.py", "--live"])
    assert runner["main"]() == 2
    captured = capsys.readouterr()
    stdout = json.loads(captured.out)
    assert stdout["status"] == "inconclusive"
    report = json.loads(Path(stdout["archive"]).read_text())
    runner["validate_report"](report)
    assert report["actual_live_provider_calls"] == 0
    for fragment in (
        "Report status: INCONCLUSIVE",
        "BLOCKED — missing credentials",
        "underlying report status remains inconclusive",
        "Fixtures: declared 5; attempted 0",
        "Questions: declared 19; attempted 0",
        "passed 0; failed 0; inconclusive 0; not run 19",
        "Observed model identifiers: not recorded",
        "Actual live provider calls: 0",
    ):
        assert fragment in captured.err
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "scenario", ["ingestion_infrastructure", "question_infrastructure", "budget"]
)
def test_reporting_infrastructure_not_run_and_budget(runner, scenario):
    from sovtrace.qa.contracts import QAError

    class TimeoutSchema(SuiteSchema):
        def infer(self, request):
            raise IngestionError(
                FailureCategory.INFERENCE, "schema_inference_provider_error: SECRET payload"
            )

    class TimeoutSQL(SuiteSQL):
        def generate(self, question, schema):
            raise QAError("provider_error", "timeout", "SECRET provider payload")

    report = runner["run_live"](
        TimeoutSchema() if scenario == "ingestion_infrastructure" else SuiteSchema(),
        TimeoutSQL() if scenario == "question_infrastructure" else SuiteSQL(),
        limit=2 if scenario == "budget" else 30,
    )
    runner["validate_report"](report)
    before = deepcopy(report)
    text = runner["render_summary"](report)
    assert "Infrastructure/prerequisite problem recorded" in text
    assert "evaluation_infrastructure" in text
    assert "SECRET" not in text and "provider_error:" not in text
    assert "Successfully verified supported questions:" in text
    if scenario == "budget":
        assert "Report status: FAILED" in text
        assert "Provider-operation budget exhausted: 1 operations blocked" in text
        assert "not run 17" in text and "L1/Q2: failed" in text
    elif scenario == "question_infrastructure":
        assert "Report status: INCONCLUSIVE" in text
        assert "inconclusive 19; not run 0" in text
        assert "more affected records in report" in text
        assert sum(line.startswith("  L") for line in text.splitlines()) == 10
    else:
        assert "Report status: INCONCLUSIVE" in text
        assert "not run 16" in text
    assert report == before


def test_reporting_terminal_safety_and_private_evidence(runner, offline_report):
    report = deepcopy(offline_report)
    controls = "\x1b[31m\n\r\t\x00\x9b\u2028\u2029\u202e\u2066"
    case = report["cases"][0]
    question = case["questions"][0]
    question["checks"]["answer"]["status"] = "failed"
    finish(question)
    finish(case)
    report["summary"] = summarize(report["cases"])
    report["status"] = report_outcome(report["cases"])
    runner["validate_report"](report)
    case["id"] += controls
    question.update(id="Q1" + controls, question="SECRET source cell")
    case["application_error"] = {"message": "SECRET response"}
    question["failure_stages"].append("stage" + controls)
    report["actual_models"] = ["model" + controls]
    report["configured_models"] = {"role" + controls: "model" + controls}
    report["summary"]["failure_stages"]["stage" + controls] = 1
    before = deepcopy(report)
    text = runner["render_summary"](
        report, archive=Path("archive" + controls), latest=Path("latest" + controls)
    )
    assert "SECRET" not in text and "\\x1b[31m" in text
    assert all(
        unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for c in text.replace("\n", "")
    )
    assert report == before


def test_reporting_unexpected_fixture_failure_is_separate_from_expected_rejection(
    runner, offline_report
):
    report = deepcopy(offline_report)
    case = report["cases"][0]
    case["checks"]["mapping"]["status"] = "failed"
    finish(case)
    report["summary"] = summarize(report["cases"])
    report["status"] = report_outcome(report["cases"])
    runner["validate_report"](report)
    text = runner["render_summary"](report)
    assert "Unexpected failures: 1 fixtures; 0 questions" in text
    assert "E0a: failed; stages: semantic_mapping" in text
    assert "Expected ingestion rejections passed: 4" in text


@pytest.mark.parametrize("mode", ["offline", "injected"])
def test_neutral_report_schema_and_version_rejections(runner, offline_report, mode):
    report = offline_report if mode == "offline" else runner["run_live"](SuiteSchema(), SuiteSQL())
    assert report["evaluator_version"] == "synthetic-report-v3"
    assert report["fixture_oracle_version"] == "synthetic-corpus-oracle-v1"
    assert "gate_3" not in report
    for key, value in [
        ("evaluator_version", "unknown"),
        ("fixture_oracle_version", "unknown"),
        ("gate_3", "removed"),
        ("unexpected_field", "unrecognized"),
        ("mode", "unknown"),
    ]:
        changed = deepcopy(report)
        changed[key] = value
        with pytest.raises(AssertionError):
            runner["validate_report"](changed)
