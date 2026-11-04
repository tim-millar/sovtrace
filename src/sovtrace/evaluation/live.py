"""Fixed L1–L5 orchestration and numeric telemetry. No retries or repair loops."""

from dataclasses import asdict
from decimal import Decimal
from math import ceil
from statistics import median
from time import perf_counter

from sovtrace.ingestion.errors import IngestionError
from sovtrace.qa.contracts import QAError

from .corpus import BY_ID, QUESTIONS

LIVE_PLAN = (
    ("L1", BY_ID["E1"], QUESTIONS),
    ("L2", BY_ID["E11"], QUESTIONS),
    ("L3", BY_ID["E5"], (QUESTIONS[1], QUESTIONS[7], QUESTIONS[5])),
    ("L4", BY_ID["E6"], ()),
    ("L5", BY_ID["E7"], ()),
)
HARD_LIMIT = 30


class BudgetExceeded(Exception):
    """Raised before calling a provider. Never eligible for ingestion fallback."""


class Ledger:
    def __init__(self, limit: int = HARD_LIMIT):
        if not 0 <= limit <= HARD_LIMIT:
            raise ValueError("Evaluation budget must be within the absolute 30-operation bound")
        self.limit = limit
        self.operations: list[dict] = []

    @property
    def calls(self) -> int:
        return sum(o["actual_call"] for o in self.operations)

    def invoke(
        self, case: str, question: str | None, operation: str, requested_model: str, function, *args
    ):
        record = {
            "case": case,
            "question": question,
            "operation": operation,
            "requested_model": requested_model,
            "actual_model": None,
            "outcome": "budget_exhausted",
            "actual_call": False,
            "elapsed_seconds": None,
            "usage": None,
            "service_tier": None,
            "application_error": None,
        }
        self.operations.append(record)
        if self.calls >= self.limit:
            raise BudgetExceeded("Evaluation application-level model-operation budget exhausted")
        record["actual_call"] = True
        started = perf_counter()
        try:
            result = function(*args)
            record.update(outcome="response", actual_model=result.model)
            if result.usage is not None:
                usage = result.usage
                record["usage"] = (
                    usage.model_dump() if hasattr(usage, "model_dump") else asdict(usage)
                )
                record["usage"].setdefault("cache_write_tokens", None)
            record["service_tier"] = getattr(result, "service_tier", None)
            return result
        except IngestionError as exc:
            record["outcome"] = (
                "semantic_rejection" if exc.category.value == "mapping" else "provider_failure"
            )
            record["application_error"] = {
                "category": exc.category.value,
                "code": str(exc).split(":", 1)[0],
                "message": str(exc),
            }
            raise
        except QAError as exc:
            record.update(outcome="provider_failure", application_error=exc.failure.model_dump())
            raise
        except Exception as exc:
            record.update(outcome="infrastructure_failure", exception_type=type(exc).__name__)
            raise
        finally:
            record["elapsed_seconds"] = perf_counter() - started


class MeasuredSchema:
    def __init__(self, ledger: Ledger, case: str, provider, requested_model: str):
        self.ledger, self.case, self.provider, self.model = ledger, case, provider, requested_model
        self.failure: dict | None = None

    def infer(self, request):
        try:
            return self.ledger.invoke(
                self.case, None, "schema_inference", self.model, self.provider.infer, request
            )
        except IngestionError:
            self.failure = self.ledger.operations[-1]["application_error"]
            raise


class MeasuredSQL:
    def __init__(self, ledger: Ledger, case: str, question: str, provider, requested_model: str):
        self.ledger, self.case, self.question = ledger, case, question
        self.provider, self.model = provider, requested_model

    def generate(self, question, schema):
        return self.ledger.invoke(
            self.case,
            self.question,
            "sql_generation",
            self.model,
            self.provider.generate,
            question,
            schema,
        )


def latency(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "median_seconds": median(ordered) if ordered else None,
        "p95_seconds": ordered[ceil(0.95 * len(ordered)) - 1] if len(ordered) >= 2 else None,
        "interpretation": "descriptive only; small sequential fixed corpus",
    }


def measurements(cases: list[dict], ledger: Ledger, cost_estimator) -> dict:
    operations = ledger.operations
    calls = [o for o in operations if o["actual_call"]]
    for operation in operations:
        # The estimator owns explicit model/alias/tier compatibility checks.
        try:
            cost = cost_estimator(operation)
        except (ValueError, KeyError, TypeError):
            cost = None
        operation["estimated_usd"] = str(cost) if cost is not None else None
        operation["cost_status"] = "available" if cost is not None else "unavailable"
    for case in cases:
        own = [o for o in calls if o["case"] == case["id"]]
        case["provider_calls"] = len(own)
        schema = [o for o in own if o["operation"] == "schema_inference"]
        case["schema_inference_seconds"] = (
            sum(o["elapsed_seconds"] for o in schema) if schema else None
        )
        case["schema_inference_attribution"] = (
            "technical_fallback"
            if case["checks"].get("inference_path", {}).get("observed") == "fallback"
            else "provider_invoked"
            if schema
            else "deterministic_no_provider_call"
        )
        sql = [o for o in own if o["operation"] == "sql_generation"]
        case["sql_generation_seconds"] = sum(o["elapsed_seconds"] for o in sql) if sql else None
        case["qa_seconds"] = sum(q.get("elapsed_seconds", 0) for q in case["questions"])
        for question in case["questions"]:
            own_q = [o for o in own if o["question"] == question["id"]]
            question["provider_calls"] = len(own_q)
            question["sql_generation_seconds"] = (
                sum(o["elapsed_seconds"] for o in own_q) if own_q else None
            )

    def cost_total(selected):
        if not selected or any(o["estimated_usd"] is None for o in selected):
            return None
        return str(sum((Decimal(o["estimated_usd"]) for o in selected), Decimal(0)))

    overall = cost_total(calls)
    successes = sum(
        q["status"] == "passed" and q["supported"] for c in cases for q in c["questions"]
    )
    return {
        "budget_limit": ledger.limit,
        "absolute_budget_limit": HARD_LIMIT,
        "actual_provider_calls": ledger.calls,
        "blocked_operations": sum(not o["actual_call"] for o in operations),
        "operations": operations,
        "latency": latency([o["elapsed_seconds"] for o in calls]),
        "case_latency": latency([c["elapsed_seconds"] for c in cases if "elapsed_seconds" in c]),
        "tokens": {
            key: sum(o["usage"][key] for o in calls)
            if calls
            and all(o["usage"] is not None and o["usage"].get(key) is not None for o in calls)
            else None
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "output_tokens",
                "reasoning_tokens",
            )
        },
        "usage_records": sum(o["usage"] is not None for o in calls),
        "cost": {
            "schema_inference_usd": cost_total(
                [o for o in calls if o["operation"] == "schema_inference"]
            ),
            "sql_generation_usd": cost_total(
                [o for o in calls if o["operation"] == "sql_generation"]
            ),
            "overall_usd": overall,
            "per_successful_supported_question_usd": str(Decimal(overall) / successes)
            if overall is not None and successes
            else None,
            "status": "available" if overall is not None else "unavailable",
            "basis": "Explicitly configured verified pricing only; estimates are not invoices; reasoning included in output",
        },
    }
