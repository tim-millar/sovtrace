# SOVTrace

SOVTrace imports a bounded insurance Schedule of Values (SOV) workbook into SQLite,
answers supported questions through validated SQL, and reconciles each answer with
its original spreadsheet cells. Version 0.1.0 is unreleased.

This is a local Python 3.12 application for supported insurance SOV tables. It is
not an arbitrary-spreadsheet interpreter. Supported operations are existence,
logical-asset counts, total insurable value sums, and bounded asset listings.
Supported concepts are asset identifier, property name, city, US state, total
insurable value and construction type. The identifier, state and valuation concepts
are required; other concepts may be unavailable. Missing or invalid recorded values
can cause a question to fail rather than produce a speculative answer.

## Install and try the offline demonstration

Use Python 3.12 and uv:

```sh
uv sync --locked
uv run --locked sovtrace --help
uv run --locked sovtrace --version
uv run --locked python scripts/demo.py
uv run --locked python scripts/demo.py --format json
uv run --locked python scripts/verify_synthetic.py --offline
```

The demonstration imports [the synthetic workbook](examples/synthetic-sov.xlsx)
and runs eight fixed questions with predefined SQL, without credentials or model
requests. SQL execution, numerical arithmetic and source-evidence reconciliation
are real. Text summarizes evaluated answers; JSON lines retain the executed SQL
and complete demonstrated source-cell coordinates. Passing the offline suite does
not measure live model accuracy. The evaluator uses 14 artificial fixtures and 80
question outcomes, including deliberate refusals and ingestion rejections.

## Import and ask

```sh
uv run --locked sovtrace ingest examples/synthetic-sov.xlsx --db data/generated/sov.sqlite --no-infer-schema
uv run --locked sovtrace ingest examples/synthetic-sov.xlsx --db data/generated/sov.sqlite --replace --format json --no-infer-schema
```

Workbook admission has fixed byte, ZIP-expansion, sheet, coordinate and cell budgets;
see the [exact resource ceilings and enforcement](docs/ARCHITECTURE.md#workbook-admission-budgets).
Over-limit inputs fail before inference or publication, including when replacing
an existing database. These limits are not a comprehensive resource-exhaustion guarantee.

Import refuses to overwrite an existing database without `--replace`. It reads an
immutable workbook snapshot, validates the supported table structure and records
original worksheet/row/column coordinates. Exact duplicates are consolidated into
logical assets while retaining every physical source record; conflicting duplicates
fail. Counts and listings refer to logical assets, not spreadsheet rows.

Model-backed questions require an authorized OpenAI account and credentials exported
in the shell. The application does not automatically load `.env` files:

```sh
export OPENAI_API_KEY='<your-api-key>'
uv run --locked sovtrace ask --db data/generated/sov.sqlite 'How many assets are in Arizona?'
uv run --locked sovtrace ask --db data/generated/sov.sqlite 'What is the aggregate TIV of all assets?' --format json
```

`OPENAI_MODEL` and `OPENAI_SCHEMA_MODEL` configure SQL generation and schema
inference independently. Model availability and account handling must be checked
for the configured account. The optional credentialed schema smoke command is
`uv run --locked python scripts/verify_schema_live.py`; it is not part of offline
verification. Do not run live commands with sensitive inputs without authorization.

## What leaves the machine

Schema inference sends original headings verbatim plus column indices, letters and
bounded local metadata/type-count summaries. Headings are not generically redacted:
they may contain names, records, identifiers, paths, amounts or formulas, and those
characters are transmitted. CLI inference defaults on when required **or optional**
concepts remain unresolved after deterministic mapping. The Python `ingest_workbook`
API defaults to inference off. `--no-infer-schema` prevents inference requests.

Model-backed querying separately sends the user's question verbatim and relevant
semantic schema information to generate SQL. Questions can contain the same kinds
of sensitive information. The inference off switch does not disable this separate
query-generation path.

Neither workflow separately serializes workbook rows, source-cell values, full
datasets or returned evidence merely as those structured records. That boundary
does not establish that all sensitive financial information stays local: headings
and questions may themselves carry it. Both adapters set `store=False`; this does
not prove zero retention, absence of provider logging or account-specific handling.
Authentication, normal SDK metadata, provider account controls, proxies and
instrumentation also affect handling. Review those settings before use.

## Numerical and execution boundaries

Valuations use signed integer micro-USD (six decimal places), with Decimal-based
normalization and exact integer aggregation. Displayed cents use half-up rounding;
structured answers preserve exact micro-USD as decimal strings. An empty sum is
not silently presented as a known zero. Thresholds are strict when the SQL predicate
uses `>`; no floating-point conversion supplies monetary comparisons.

Generated SQL passes a restricted SQLite grammar and executes read-only. There
are no joins, arbitrary expressions, data mutations or automatic repair loops.
Answers and complete contributing evidence are checked within one read snapshot.
A verified answer establishes execution and reconciliation for the validated
interpretation; it does not prove a model understood every natural-language intent.

See [architecture](docs/ARCHITECTURE.md) for component boundaries.

## Offline checks

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest -q
```

CI runs these checks, CLI smoke checks, both demonstration formats and the offline
evaluator with credentials removed and a network guard. No provider-price schedule
is shipped. Cost estimates are unavailable unless a caller explicitly injects a
separately verified, sourced and dated model/tier-compatible schedule. Test rates
are artificial and are not provider prices. Dependencies are installed from the
lockfile; their implementation and environments are not bundled in this repository.
