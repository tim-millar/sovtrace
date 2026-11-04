"""Explicit currency tokens must survive intact before SQL consistency checks."""

import pytest
from test_qa import FakeGenerator
from workbook_factory import write_workbook

from sovtrace.ingestion import ingest_workbook
from sovtrace.qa import Failure, Success, ask, load_context, read_snapshot, validate_sql
from sovtrace.qa.contracts import TIV, QAError
from sovtrace.qa.service import check_thresholds


@pytest.fixture(scope="module")
def threshold_data(tmp_path_factory):
    directory = tmp_path_factory.mktemp("thresholds")
    source = write_workbook(directory / "artificial.xlsx")
    database = directory / "artificial.sqlite"
    ingest_workbook(source, database)
    with read_snapshot(database) as connection:
        context = load_context(connection)
    return database, context


def plan(context, value, operator=">"):
    return validate_sql(
        f"SELECT COUNT(*) FROM assets WHERE {TIV} {operator} {value}", "count", context
    )


@pytest.mark.parametrize(
    "currency,value",
    [
        ("$1000", 1_000_000_000),
        ("$1,000", 1_000_000_000),
        ("$1,000.50", 1_000_500_000),
        ("USD 1,000,000", 1_000_000_000_000),
        ("USD1000", 1_000_000_000),
        ("$10m", 10_000_000_000_000),
        ("$10 million", 10_000_000_000_000),
        ("$1.5 billion", 1_500_000_000_000_000),
        ("$2k", 2_000_000_000),
        ("$2 thousand", 2_000_000_000),
        ("$1b", 1_000_000_000_000_000),
        ("$1.25M", 1_250_000_000_000),
        ("-$0.000001", -1),
        ("$-0.000001", -1),
        ("-USD 0.000001", -1),
        ("USD -0.000001", -1),
        ("$0", 0),
        ("$1.0000000", 1_000_000),
        ("$0.0000001 million", 100_000),
        ("$9223372036854.775807", 2**63 - 1),
        ("-$9223372036854.775808", -(2**63)),
        ("$-9,223,372,036,854.775808", -(2**63)),
    ],
)
def test_valid_currency_and_exact_micro_usd_boundaries(threshold_data, currency, value):
    database, context = threshold_data
    query = plan(context, value)
    check_thresholds(f"How many assets over {currency}?", query)
    result = ask(database, f"How many assets over {currency}?", FakeGenerator(query.executed_sql))
    assert isinstance(result, Success)


@pytest.mark.parametrize(
    "phrase,operator",
    [
        ("over", ">"),
        ("more than", ">"),
        ("greater than", ">"),
        ("exceed", ">"),
        ("exceeds", ">"),
        ("exceeding", ">"),
        ("at least", ">="),
        ("under", "<"),
        ("less than", "<"),
        ("below", "<"),
        ("at most", "<="),
        ("MORE  THAN", ">"),
    ],
)
def test_supported_inequality_phrases_are_preserved(threshold_data, phrase, operator):
    _, context = threshold_data
    question = f"How many assets {phrase} $1,000.50?"
    check_thresholds(question, plan(context, 1_000_500_000, operator))
    with pytest.raises(QAError) as error:
        check_thresholds(question, plan(context, 1_000_500_000, "="))
    assert error.value.failure.code == "threshold_operator"


@pytest.mark.parametrize(
    "currency",
    [
        "$1,2",
        "$1 000",
        "$1 .50",
        "$1 ,000",
        "$1 +2",
        "$10m 2",
        "$12,34",
        "$1234,567",
        "$1,,000",
        "$1,000,",
        "$1,000,00",
        "$1.2.3",
        "$1..2",
        "$1.",
        "$1,000.",
        "$.5",
        "$-",
        "$",
        "USD",
        "$10mm",
        "$10mx",
        "$10m2",
        "$10m_",
        "$10millionx",
        "$10 millionx",
        "$10 million billion",
        "$10 trillion",
        "$10mn",
        "$10bn",
        "$10e3",
        "$10/2",
        "$10:2",
        "$10%",
        "$10+2",
        "$10-2",
        "$10'000",
        "$10_000",
        "$1，000",
        "$1．5",
        "$10m's",
        "$10€",
        "$10£",
        "$10$20",
        "$--1",
        "+$10",
        "−$10",
        "- $10",
        "USDx10",
        "$1.2.3 and $1000",
    ],
)
def test_malformed_currency_cannot_disappear_or_partially_match(threshold_data, currency):
    database, context = threshold_data
    question = f"How many assets over {currency}?"
    with pytest.raises(QAError) as error:
        check_thresholds(question, plan(context, 1_000_000_000))
    assert error.value.failure.category == "unsupported_question"
    assert error.value.failure.code in {"threshold_syntax", "threshold_sign"}
    result = ask(database, question, FakeGenerator(plan(context, 1_000_000_000).executed_sql))
    assert isinstance(result, Failure) and result.category == "unsupported_question"


@pytest.mark.parametrize(
    "currency,value",
    [
        ("$1,2", 12_000_000),
        ("$1.2.3", 1_200_000),
        ("$1,000,", 1_000_000_000),
    ],
)
def test_sql_using_the_old_incorrect_interpretation_still_fails(threshold_data, currency, value):
    database, context = threshold_data
    query = plan(context, value)
    result = ask(database, f"How many assets over {currency}?", FakeGenerator(query.executed_sql))
    assert isinstance(result, Failure) and result.code == "threshold_syntax"


@pytest.mark.parametrize(
    "currency",
    [
        "$9223372036854.775808",
        "-$9223372036854.775809",
        "$9223372036855",
        "$0.0000001",
        "-$0.0000001",
        "$0.00000100000000000000000000000000001",
    ],
)
def test_out_of_range_and_fractional_micro_thresholds_fail_without_rounding(
    threshold_data, currency
):
    database, context = threshold_data
    result = ask(
        database, f"How many assets over {currency}?", FakeGenerator(plan(context, 0).executed_sql)
    )
    assert isinstance(result, Failure)
    assert result.category == "unsupported_question" and result.code == "threshold_precision"


def test_valid_amount_mismatch_and_malformed_tokens_alongside_valid_ones(threshold_data):
    _, context = threshold_data
    with pytest.raises(QAError) as error:
        check_thresholds("How many assets over $1000?", plan(context, 999_000_000))
    assert error.value.failure.code == "threshold_scale"
    sql = f"SELECT COUNT(*) FROM assets WHERE {TIV} > 1000000000 AND {TIV} < 2000000000"
    query = validate_sql(sql, "count", context)
    check_thresholds("How many assets over $1000 but under $2000?", query)
    for question in (
        "How many assets over $1000 but under $1,2?",
        "How many assets over $1,2 but under $2000?",
    ):
        with pytest.raises(QAError) as malformed:
            check_thresholds(question, query)
        assert malformed.value.failure.code == "threshold_syntax"
