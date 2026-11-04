"""Fixed synthetic cross-workbook evaluation. Live mode is separately opt-in."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from sovtrace.evaluation.base_sql import matches_base_sql
from sovtrace.evaluation.corpus import FIXTURES, QUESTIONS, VERSION, expected_failure
from sovtrace.evaluation.cost import estimated_cost
from sovtrace.evaluation.live import (
    LIVE_PLAN,
    Ledger,
    MeasuredSchema,
    MeasuredSQL,
    measurements,
)
from sovtrace.evaluation.offline import (
    IMPORT_CHECKS,
    QUESTION_CHECKS,
    FixedSchema,
    FixedSQL,
    case_outcome,
    declared_case,
    finish,
    question_outcome,
    report_outcome,
    run_case,
    summarize,
)
from sovtrace.evaluation.sql_comparison import matches_evaluation_sql
from sovtrace.ingestion.provider import OpenAISchemaInferenceProvider
from sovtrace.presentation import _safe as safe_text
from sovtrace.qa.provider import OpenAIGenerator

ROOT = Path(__file__).resolve().parents[1]
matches_reference_sql = partial(matches_evaluation_sql, base_comparator=matches_base_sql)


def run_offline() -> dict:
    with tempfile.TemporaryDirectory(prefix="sov-synthetic-offline-") as directory:
        cases = [
            run_case(
                Path(directory),
                f.id,
                f,
                () if f.failure_category else QUESTIONS,
                FixedSchema(f),
                FixedSQL,
                matches_reference_sql,
            )
            for f in FIXTURES
        ]
    report = {
        "evaluator_version": "synthetic-report-v3",
        "fixture_oracle_version": VERSION,
        "tracked_tree_dirty": bool(
            subprocess.check_output(
                ["git", "diff", "HEAD", "--name-only"], cwd=ROOT, text=True
            ).strip()
        ),
        "mode": "offline",
        "utc_timestamp": datetime.now(UTC).isoformat(),
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "credentials_available": "not_inspected",
        "providers_available": True,
        "configured_models": {"schema": "fake-schema", "sql": "fake-sql"},
        "actual_models": ["fake-schema", "fake-sql"],
        "executed_fixtures": [f.id for f in FIXTURES],
        "cases": cases,
        "summary": summarize(cases),
        "status": report_outcome(cases),
        "latency": {"status": "offline_only_not_model_latency"},
        "cost": {"status": "unavailable", "reason": "offline_fake_providers"},
        "actual_live_provider_calls": 0,
    }

    validate_report(report)
    return report


def validate_report(report: dict) -> None:
    assert report["evaluator_version"] == "synthetic-report-v3"
    assert report["fixture_oracle_version"] == "synthetic-corpus-oracle-v1"
    assert "gate_3" not in report
    assert report["mode"] in {"offline", "live"}
    common = {
        "evaluator_version",
        "fixture_oracle_version",
        "tracked_tree_dirty",
        "mode",
        "utc_timestamp",
        "git_sha",
        "credentials_available",
        "providers_available",
        "configured_models",
        "actual_models",
        "executed_fixtures",
        "cases",
        "summary",
        "status",
        "actual_live_provider_calls",
    }
    specific = (
        {"latency", "cost"}
        if report["mode"] == "offline"
        else {
            "provider_mode",
            "measurements",
            "settings",
        }
    )
    assert set(report) == common | specific
    plan = (
        [(f.id, f, () if f.failure_category else QUESTIONS) for f in FIXTURES]
        if report["mode"] == "offline"
        else LIVE_PLAN
    )
    assert [
        (c["id"], c["fixture"], [q["id"] for q in c["questions"]]) for c in report["cases"]
    ] == [(cid, f.id, [q.id for q in qs]) for cid, f, qs in plan]
    assert report["summary"] == summarize(report["cases"])
    assert report["status"] == report_outcome(report["cases"])
    budget_blocked = False
    for case, (_, fixture, questions) in zip(report["cases"], plan, strict=True):
        required = set(IMPORT_CHECKS) if fixture.failure_category is None else {"ingestion"}
        assert required <= set(case["checks"]) <= required | {"infrastructure"}
        assert case["expected_ingestion"] == (
            "rejected" if fixture.failure_category else "accepted"
        )
        assert case["attempted"] == ("elapsed_seconds" in case)
        if not case["attempted"]:
            assert case["observed_ingestion"] == "not_run"
            assert budget_blocked or (
                report["mode"] == "live" and not report["credentials_available"]
            )
        for qr, question in zip(case["questions"], questions, strict=True):
            assert (
                set(QUESTION_CHECKS)
                <= set(qr["checks"])
                <= set(QUESTION_CHECKS) | {"infrastructure"}
            )
            assert qr["supported"] == (not bool(expected_failure(fixture, question)))
            assert qr["expected"]["status"] == ("verified" if qr["supported"] else "failure")
            assert qr["status"] == question_outcome(qr)
            attempted = "elapsed_seconds" in qr
            if not attempted:
                assert qr["status"] == "not_run"
                assert case["observed_ingestion"] != "accepted" or budget_blocked
            else:
                assert qr["status"] != "not_run"
                assert case["attempted"] and case["observed_ingestion"] == "accepted"
                assert "observed_answer_status" in qr or "infrastructure_error" in qr
                if any(c["status"] == "not_run" for c in qr["checks"].values()):
                    assert (
                        qr.get("observed_answer_status") == "failure"
                        or "infrastructure_error" in qr
                        or (not qr["supported"] and qr["checks"]["outcome"]["status"] == "failed")
                    )
            if qr["status"] == "passed" and not qr["supported"]:
                assert qr["application_error"]["category"] in expected_failure(fixture, question)
                if fixture.id == "E9":
                    assert qr["application_error"]["code"] == (
                        "valuation_incomplete" if question.id == "Q8" else "predicate_incomplete"
                    )
            if qr.get("infrastructure_error", {}).get("type") == "BudgetExceeded":
                budget_blocked = True
            for check in qr["checks"].values():
                if check["status"] == "not_run":
                    assert check.get("dependency") in {
                        case["id"] + ".ingestion",
                        "verified_answer_unavailable",
                        "sql_generation",
                        "expected_material_failure",
                        "provider_operation_budget",
                    }
        if case.get("infrastructure_error", {}).get("type") == "BudgetExceeded":
            budget_blocked = True
        assert case["status"] == case_outcome(case)
        if case["status"] == "passed" and fixture.failure_category:
            error = case["application_error"]
            assert error["category"] == fixture.failure_category
            assert fixture.failure_code is None or error["code"] == fixture.failure_code
            assert fixture.failure_contains is None or fixture.failure_contains in error["message"]
            assert fixture.failure_prefix is None or error["message"].startswith(
                fixture.failure_prefix
            )
        for record in [case, *case["questions"]]:
            assert all(
                c["status"] in ("passed", "failed", "inconclusive", "not_run")
                for c in record["checks"].values()
            )
            assert all(
                c.get("dependency") for c in record["checks"].values() if c["status"] == "not_run"
            )


def run_live(schema_provider=None, sql_provider=None, *, limit: int = 30) -> dict:
    injected = schema_provider is not None or sql_provider is not None
    if injected and (schema_provider is None or sql_provider is None):
        raise ValueError("Inject both providers to prevent accidental real requests")
    credentials = (
        bool(os.environ.get("OPENAI_API_KEY", "").strip())
        if not injected
        else "not_required_for_injected_providers"
    )
    schema_provider = schema_provider or OpenAISchemaInferenceProvider()
    sql_provider = sql_provider or OpenAIGenerator()
    schema_model = getattr(schema_provider, "model", "injected-schema")
    sql_model = getattr(sql_provider, "model", "injected-sql")
    ledger = Ledger(limit)
    cases = []
    with tempfile.TemporaryDirectory(prefix="sov-synthetic-live-") as directory:
        for case_id, fixture, questions in LIVE_PLAN:
            if any(o["outcome"] == "budget_exhausted" for o in ledger.operations):
                case = declared_case(case_id, fixture, questions)
                for check in case["checks"].values():
                    check["dependency"] = "provider_operation_budget"
            elif credentials:
                case = run_case(
                    Path(directory),
                    case_id,
                    fixture,
                    questions,
                    MeasuredSchema(ledger, case_id, schema_provider, schema_model),
                    lambda q, cid=case_id: MeasuredSQL(ledger, cid, q.id, sql_provider, sql_model),
                    matches_reference_sql,
                )
            else:
                case = declared_case(case_id, fixture, questions)
                case["checks"]["ingestion"].update(
                    status="inconclusive",
                    stage="evaluation_infrastructure",
                    reason="missing_credentials",
                )
                finish(case)
            cases.append(case)
    report = {
        "evaluator_version": "synthetic-report-v3",
        "fixture_oracle_version": VERSION,
        "tracked_tree_dirty": bool(
            subprocess.check_output(
                ["git", "diff", "HEAD", "--name-only"], cwd=ROOT, text=True
            ).strip()
        ),
        "mode": "live",
        "provider_mode": "injected" if injected else "actual",
        "utc_timestamp": datetime.now(UTC).isoformat(),
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "credentials_available": credentials,
        "providers_available": "injected" if injected else "not_probed" if credentials else False,
        "configured_models": {"schema": schema_model, "sql": sql_model},
        "actual_models": sorted(
            {o["actual_model"] for o in ledger.operations if o["actual_model"]}
        ),
        "executed_fixtures": [c["fixture"] for c in cases if c["observed_ingestion"] != "not_run"],
        "cases": cases,
        "summary": summarize(cases),
        "actual_live_provider_calls": 0 if injected else ledger.calls,
        "measurements": measurements(cases, ledger, estimated_cost),
        "settings": {
            "application_attempts_per_operation": 1,
            "sdk_transport_retries": 1,
            "reasoning_effort": "low",
            "max_output_tokens": 3000,
            "timeout_seconds": 30,
        },
    }
    report["status"] = report_outcome(cases)
    validate_report(report)
    return report


def save_report(report: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    # Exclusive archive retains every original attempt. Latest is a convenience copy.
    stamp = report["utc_timestamp"].replace(":", "").replace("+", "_")
    archive = directory / f"{report['mode']}-{stamp}.json"
    content = json.dumps(report, indent=2, sort_keys=True) + "\n"
    with archive.open("x") as handle:
        handle.write(content)
    latest = directory / f"{report['mode']}-report.json"
    latest.write_text(content)
    return archive


def render_summary(report: dict, *, archive: Path | None = None, latest: Path | None = None) -> str:
    """Present a validated report; outcomes remain entirely evaluator-owned."""
    if report["mode"] == "offline":
        mode = "OFFLINE — fixed/injected providers; no real model calls"
    elif report.get("provider_mode") == "injected":
        mode = "LIVE orchestration — injected test providers; no real model calls"
    elif report.get("provider_mode") == "actual" and report["actual_live_provider_calls"]:
        mode = "LIVE — actual OpenAI providers"
    else:
        mode = "LIVE selected — actual model execution unconfirmed"
    summary = report["summary"]
    lines = [
        "Synthetic cross-workbook evaluation",
        mode,
        f"Report status: {safe_text(report['status']).upper()}",
    ]
    if report.get("credentials_available") is False:
        lines.append(
            "BLOCKED — missing credentials; underlying report status remains inconclusive."
        )
    elif summary["failure_stages"].get("evaluation_infrastructure"):
        lines.append("Infrastructure/prerequisite problem recorded; attempted work is shown below.")
    for label, key in (("Fixtures", "cases"), ("Questions", "questions")):
        counts = summary[key]
        lines.extend(
            [
                f"{label}: declared {summary['declared_' + key]}; attempted {summary['attempted_' + key]}",
                f"  passed {counts['passed']}; failed {counts['failed']}; "
                f"inconclusive {counts['inconclusive']}; not run {counts['not_run']}",
            ]
        )
    questions = [q for c in report["cases"] for q in c["questions"]]
    refusals = sum(q["status"] == "passed" and not q["supported"] for q in questions)
    rejections = sum(
        c["status"] == "passed" and c["expected_ingestion"] == "rejected" for c in report["cases"]
    )
    lines.extend(
        [
            f"Successfully verified supported questions: {summary['supported_questions']['passed']}",
            f"Expected unsupported/unavailable refusals passed: {refusals}",
            f"Expected ingestion rejections passed: {rejections}",
            f"Unexpected failures: {summary['cases']['failed']} fixtures; {summary['questions']['failed']} questions",
            "Failure stages: "
            + (
                ", ".join(
                    f"{safe_text(stage)}={count}"
                    for stage, count in sorted(summary["failure_stages"].items())
                )
                or "none"
            ),
        ]
    )
    affected = []
    for case in report["cases"]:
        for identifier, record in [
            (case["id"], case),
            *[(f"{case['id']}/{q['id']}", q) for q in case["questions"]],
        ]:
            if record["status"] in ("failed", "inconclusive"):
                stages = ", ".join(safe_text(s) for s in record["failure_stages"]) or "not recorded"
                affected.append(
                    f"  {safe_text(identifier)}: {safe_text(record['status'])}; stages: {stages}"
                )
    lines.extend(affected[:10])
    if len(affected) > 10:
        lines.append(f"  ... {len(affected) - 10} more affected records in report")
    blocked_operations = report.get("measurements", {}).get("blocked_operations")
    if blocked_operations:
        lines.append(
            f"Provider-operation budget exhausted: {blocked_operations} operations blocked"
        )
    lines.extend(
        [
            "Configured models: "
            + ", ".join(
                f"{safe_text(role)}={safe_text(model)}"
                for role, model in sorted(report["configured_models"].items())
            ),
            "Observed model identifiers"
            + (
                " (fixed/injected)"
                if report["mode"] == "offline" or report.get("provider_mode") == "injected"
                else ""
            )
            + ": "
            + (", ".join(safe_text(m) for m in report["actual_models"]) or "not recorded"),
            f"Actual live provider calls: {report['actual_live_provider_calls']}",
        ]
    )
    if archive is not None:
        lines.append(f"Timestamped archive: {safe_text(archive)}")
    if latest is not None:
        lines.append(f"Latest report: {safe_text(latest)}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args()
    report = run_offline() if args.offline else run_live()
    archive = save_report(report, ROOT / "data/generated/synthetic-evaluation")
    print(
        json.dumps(
            {"status": report["status"], "summary": report["summary"], "archive": str(archive)},
            sort_keys=True,
        )
    )
    print(
        render_summary(
            report, archive=archive, latest=archive.parent / f"{report['mode']}-report.json"
        ),
        file=sys.stderr,
    )
    return 0 if report["status"] == "passed" else 2 if report["status"] == "inconclusive" else 1


if __name__ == "__main__":
    raise SystemExit(main())
