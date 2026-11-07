# Contributing to SOVTrace

## Project scope

SOVTrace is deliberately limited to supported insurance Schedule of Values
workbooks and a bounded set of existence, count, TIV sum and asset-list operations.
Contributions should preserve that scope, its safety restrictions, exact monetary
handling and source-evidence guarantees. General spreadsheet interpretation,
general-purpose text-to-SQL, hosted services and additional providers are not
committed project features.

## Development setup and verification

Use Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
From a local checkout, install the locked dependencies and run the existing checks:

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest -q
uv run --locked python scripts/demo.py
uv run --locked python scripts/demo.py --format json
uv run --locked python scripts/verify_synthetic.py --offline
```

The demonstrations and synthetic evaluator use artificial data and do not require
provider credentials.

## Issues, pull requests and support

Once the repository is public, use GitHub issues for ordinary bug reports,
questions and feature proposals. Propose code changes through GitHub pull requests.
Include concise reproduction steps and applicable synthetic test coverage. Do not
submit real customer workbooks, secrets, or restricted personal or business
information in issues, pull requests, fixtures or other contributions.

Report security vulnerabilities according to [SECURITY.md](SECURITY.md), rather
than through ordinary public issues. Contributions are reviewed on a best-effort
basis; no response-time commitment is offered.
