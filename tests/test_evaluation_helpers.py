"""Independent restricted SQL and artificial-rate accounting regressions."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from functools import partial

import pytest

from sovtrace.evaluation.base_sql import matches_base_sql
from sovtrace.evaluation.cost import PricingSchedule, estimated_cost
from sovtrace.evaluation.live import Ledger, measurements
from sovtrace.evaluation.sql_comparison import matches_evaluation_sql

compare_sql = partial(matches_evaluation_sql, base_comparator=matches_base_sql)
COUNT = "SELECT COUNT(*) FROM assets"


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "SELECT",
        "SELECT COUNT(*)",
        "SELECT COUNT(*) FROM other",
        COUNT + "; DELETE FROM assets",
        COUNT + "; " + COUNT,
        COUNT + " -- comment",
        COUNT + " LIMIT 1",
        COUNT + " GROUP BY state",
        "SELECT COUNT(DISTINCT asset_id) FROM assets",
        "SELECT COUNT(1) FROM assets",
        "SELECT * FROM assets",
        "WITH t AS (SELECT * FROM assets) SELECT COUNT(*) FROM t",
        COUNT + " WHERE ABS(total_insurable_value_micro_usd) > 1",
        COUNT + " WHERE state = 'AZ' UNION " + COUNT,
        COUNT + " WHERE state = (SELECT state FROM assets)",
        COUNT + " WHERE state = 'AZ' OR 1 = 1",
    ],
)
def test_unsupported_or_malformed_sql_is_rejected_even_against_itself(sql):
    assert compare_sql(sql, sql, "count") is False


def test_and_normalization_does_not_erase_or_structure():
    a, b, c = "state = 'AZ'", "city = 'Test City'", "asset_id = 'SYN-001'"
    reference = COUNT + f" WHERE ({a} OR {b}) AND {c}"
    assert compare_sql(COUNT + f" WHERE (({c})) AND ((({a}) OR ({b})))", reference, "count")
    assert not compare_sql(COUNT + f" WHERE {a} OR ({b} AND {c})", reference, "count")
    assert not compare_sql(COUNT + f" WHERE ({b} OR {a}) AND {c}", reference, "count")
    assert compare_sql(
        COUNT + f" WHERE {a} AND ({b} AND {c})", COUNT + f" WHERE ({c} AND {a}) AND {b}", "count"
    )


@pytest.mark.parametrize(
    "sql,projection,accepted",
    [
        ("SELECT asset_id, construction_type FROM assets", ("asset_id", "construction_type"), True),
        (
            "SELECT asset_id, construction_type FROM assets ORDER BY asset_id ASC",
            ("asset_id", "construction_type"),
            True,
        ),
        ("SELECT construction_type FROM assets", ("construction_type",), False),
        ("SELECT asset_id, asset_id FROM assets", ("asset_id", "asset_id"), False),
        ("SELECT asset_id, state FROM assets", ("asset_id", "construction_type"), False),
        ("SELECT asset_id FROM assets", (), False),
        ("SELECT asset_id FROM assets ORDER BY asset_id DESC", ("asset_id",), False),
        ("SELECT asset_id, unknown FROM assets", ("asset_id", "unknown"), False),
    ],
)
def test_validated_list_projection_cannot_be_bypassed(sql, projection, accepted):
    assert compare_sql(sql, COUNT, "list", projection) is accepted


def test_scalar_operations_and_predicates_remain_significant():
    total = "SELECT SUM(total_insurable_value_micro_usd) FROM assets"
    assert compare_sql(total, total, "sum")
    assert compare_sql(COUNT, COUNT, "exists")
    assert not compare_sql(total, COUNT, "count")
    assert not compare_sql(COUNT + " WHERE state = 'AZ'", COUNT + " WHERE state = 'NY'", "count")
    assert not compare_sql(COUNT, None, "count")
    assert not compare_sql(COUNT, COUNT, None)


@pytest.fixture
def rates():
    return PricingSchedule(
        model="artificial-test-model",
        service_tier="artificial-tier",
        source="Artificial test-only schedule; no real provider pricing",
        verified_on=date(2026, 1, 1),
        ordinary_input=Decimal("1.7"),
        cached_input=Decimal("0.2"),
        cache_write=Decimal("3.3"),
        output=Decimal("8.1"),
        observed_models=("artificial-test-model-observed",),
    )


@pytest.fixture
def operation():
    return {
        "case": "artificial",
        "question": "Q1",
        "operation": "sql_generation",
        "actual_call": True,
        "requested_model": "artificial-test-model",
        "actual_model": "artificial-test-model",
        "service_tier": "artificial-tier",
        "outcome": "response",
        "elapsed_seconds": 0.1,
        "usage": {
            "input_tokens": 1000,
            "cached_input_tokens": 200,
            "cache_write_tokens": 100,
            "output_tokens": 300,
            "reasoning_tokens": 80,
        },
    }


def test_independent_category_arithmetic_and_explicit_model_alias(rates, operation):
    # 700 ordinary + 200 read + 100 write; reasoning is already part of 300 output.
    expected = (
        Decimal(700) * Decimal("1.7")
        + Decimal(200) * Decimal("0.2")
        + Decimal(100) * Decimal("3.3")
        + Decimal(300) * Decimal("8.1")
    ) / Decimal(1_000_000)
    before = deepcopy(operation)
    assert estimated_cost(operation, pricing=rates) == expected
    assert operation == before
    operation["actual_model"] = "artificial-test-model-observed"
    assert estimated_cost(operation, pricing=rates) == expected
    operation["usage"]["reasoning_tokens"] = 300
    assert estimated_cost(operation, pricing=rates) == expected
    operation["usage"]["cache_write_tokens"] = 0
    expected_without_writes = expected + Decimal(100) * (Decimal("1.7") - Decimal("3.3")) / Decimal(
        1_000_000
    )
    assert estimated_cost(operation, pricing=rates) == expected_without_writes


@pytest.mark.parametrize(
    "key",
    [
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_tokens",
    ],
)
@pytest.mark.parametrize("value", [None, -1, True, "100"])
def test_incomplete_or_invalid_token_usage_is_unavailable(rates, operation, key, value):
    operation["usage"][key] = value
    assert estimated_cost(operation, pricing=rates) is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("requested_model", "unconfigured-model"),
        ("actual_model", "artificial-test-model-unknown"),
        ("service_tier", "unknown"),
        ("service_tier", None),
        ("actual_call", False),
        ("outcome", "provider_failure"),
        ("usage", None),
    ],
)
def test_unknown_compatibility_or_missing_telemetry_is_unavailable(rates, operation, key, value):
    operation[key] = value
    assert estimated_cost(operation, pricing=rates) is None


def test_unconfigured_rates_impossible_accounting_and_total_propagation(rates, operation):
    assert estimated_cost(operation) is None
    ledger = Ledger()
    ledger.operations = [deepcopy(operation), deepcopy(operation)]
    estimator = partial(estimated_cost, pricing=rates)
    expected = estimated_cost(operation, pricing=rates)
    assert expected is not None
    assert Decimal(measurements([], ledger, estimator)["cost"]["overall_usd"]) == 2 * expected
    ledger.operations[1]["usage"]["cache_write_tokens"] = None
    report = measurements([], ledger, estimator)
    assert report["operations"][0]["cost_status"] == "available"
    assert report["operations"][1]["cost_status"] == "unavailable"
    assert report["cost"]["status"] == "unavailable"
    assert report["cost"]["overall_usd"] is None
    assert report["cost"]["sql_generation_usd"] is None
    operation["usage"]["cache_write_tokens"] = 900
    assert estimated_cost(operation, pricing=rates) is None

    operation["usage"]["cache_write_tokens"] = 100
    operation["usage"]["reasoning_tokens"] = 301
    assert estimated_cost(operation, pricing=rates) is None


@pytest.mark.parametrize(
    "change,expected",
    [
        ({}, True),
        ({"actual_model": "independent-artificial-alias"}, True),
        ({"actual_model": "unconfigured-alias"}, False),
        ({"actual_model": "artificial-test-model-misleading-prefix"}, False),
        ({"requested_model": "other-request"}, False),
        ({"service_tier": "unknown-tier"}, False),
    ],
)
def test_measurements_defers_alias_compatibility_to_explicit_schedule(
    rates, operation, change, expected
):
    from dataclasses import replace

    rates = replace(rates, observed_models=("independent-artificial-alias",))
    operation.update(change)
    ledger = Ledger()
    ledger.operations = [operation]
    result = measurements([], ledger, partial(estimated_cost, pricing=rates))
    assert (result["cost"]["status"] == "available") is expected
    if expected:
        # Explicitly artificial rates: ordinary/cache-read/cache-write/output once.
        cost = (
            Decimal(700) * Decimal("1.7")
            + Decimal(200) * Decimal("0.2")
            + Decimal(100) * Decimal("3.3")
            + Decimal(300) * Decimal("8.1")
        ) / Decimal(1_000_000)
        assert Decimal(result["cost"]["overall_usd"]) == cost
    else:
        assert result["cost"]["overall_usd"] is None
    assert measurements([], ledger, estimated_cost)["cost"]["overall_usd"] is None
