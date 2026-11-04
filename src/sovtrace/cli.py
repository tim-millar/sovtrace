"""Local command-line ingestion and application information."""

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path

from .ingestion import IngestionError, ingest_workbook
from .presentation import (
    render_answer_text,
    render_failure_text,
    render_ingest_failure_text,
    render_ingest_text,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="sovtrace",
        description=(
            "Import a local Excel SOV into SQLite with ingest, then ask a supported "
            "natural-language question and receive a verified answer with ask."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run --locked sovtrace ingest examples/synthetic-sov.xlsx --db data/generated/sov.sqlite\n"
            "  uv run --locked sovtrace ask --db data/generated/sov.sqlite 'How many assets are in Arizona?'"
        ),
    )
    parser.add_argument("--version", action="version", version=f"sovtrace {version('sovtrace')}")
    subcommands = parser.add_subparsers(dest="command")
    ingest = subcommands.add_parser(
        "ingest",
        help="Import one supported local .xlsx into SQLite",
        description=(
            "Import one supported local .xlsx workbook into SQLite. Exact deterministic schema "
            "mappings are attempted first. If supported concepts remain unresolved, "
            "default ingestion may send bounded column metadata to OpenAI: original column "
            "headings and local type-count summaries. Headers may contain sensitive information; "
            "appropriate authorization is required. Headings are sent verbatim, including sensitive "
            "names or monetary values embedded in them. Cell records and values are not "
            "separately serialized as source records. CLI inference defaults on for unresolved "
            "required or optional concepts; the Python ingestion API defaults off. "
            "OPENAI_API_KEY is required when live inference "
            "is needed. --no-infer-schema retains deterministic-only mapping, which may fail "
            "on unfamiliar headings. store=False does not establish zero provider retention."
        ),
    )
    ingest.add_argument("source", type=Path, help="Path to a local supported .xlsx workbook")
    ingest.add_argument("--db", required=True, type=Path, help="Required SQLite output destination")
    ingest.add_argument(
        "--replace", action="store_true", help="Explicitly replace an existing database"
    )
    inference = ingest.add_mutually_exclusive_group()
    inference.add_argument(
        "--infer-schema",
        action="store_true",
        help="Explicit compatibility equivalent of the default: bounded inference when needed; does not force a model request",
    )
    inference.add_argument(
        "--no-infer-schema",
        dest="infer_schema",
        action="store_false",
        help="Disable model-assisted inference and schema metadata transmission to OpenAI (deterministic-only/offline)",
    )
    ingest.set_defaults(infer_schema=True)
    ask_command = subcommands.add_parser(
        "ask",
        help="Ask a supported question of an imported SQLite database",
        description=(
            "Ask one natural-language question of an existing imported SQLite database. "
            "Live SQL generation requires OPENAI_API_KEY and sends the verbatim question and semantic schema to OpenAI. "
            "Sensitive information in the question is transmitted; store=False does not prove zero retention. "
            "Text is the default; --format json "
            "provides the complete structured result and original-source evidence. "
            "Unsupported requests or unverifiable results fail instead of returning speculative answers."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "POSIX example (single quotes prevent shell expansion of $10):\n"
            "  uv run --locked sovtrace ask --db data/generated/sov.sqlite \\\n"
            "    'How many assets exceed $10 million?'"
        ),
    )
    ask_command.add_argument(
        "--db", required=True, type=Path, help="Existing imported SQLite database"
    )
    ask_command.add_argument("question")
    for command in (ingest, ask_command):
        command.add_argument(
            "--format",
            choices=("text", "json"),
            default="text",
            help="Output format: text (default) or json (full report/evidence)",
        )
    args = parser.parse_args(argv)
    if args.command == "ingest":
        try:
            report = ingest_workbook(
                args.source, args.db, replace=args.replace, infer_schema=args.infer_schema
            )
        except IngestionError as exc:
            parser.exit(1, render_ingest_failure_text(exc, database=args.db) + "\n")
        print(
            json.dumps(report, indent=2)
            if args.format == "json"
            else render_ingest_text(report, args.db)
        )
    elif args.command == "ask":
        from .qa import Failure, ask

        result = ask(args.db, args.question)
        if args.format == "json":
            print(result.model_dump_json(indent=2))
        elif isinstance(result, Failure):
            print(render_failure_text(result), file=sys.stderr)
        else:
            print(render_answer_text(result))
        if isinstance(result, Failure):
            raise SystemExit(1)
