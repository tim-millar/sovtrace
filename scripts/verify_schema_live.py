"""Bounded live schema inference smoke test on synthetic data; no transcripts or private inputs."""

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from openpyxl import Workbook

from sovtrace.ingestion import IngestionError, ingest_workbook
from sovtrace.ingestion.inference import (
    InferenceRequest,
    InferenceResult,
    SchemaInferenceProvider,
)
from sovtrace.ingestion.manifest import SchemaManifest, SemanticField
from sovtrace.ingestion.provider import OpenAISchemaInferenceProvider

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data/generated/schema-smoke/live.json"
LABELS = (
    "Location Reference",
    "Premises",
    "Municipality",
    "US State",
    "Total Insured Value",
    "Building Construction",
)
EXACT = (
    "EPIC Code",
    "Property Name",
    "City",
    "State",
    "Total Insurable Values",
    "Construction Type",
)
EXPECTED = dict(zip((f.value for f in SemanticField), range(1, 7), strict=True))
CASES = ("fully_renamed", "partially_renamed", "absent_optional", "ambiguous_tiv")


class ObservedProvider:
    """Keep numeric metadata and selected columns only; never retain a transcript."""

    def __init__(self, provider: SchemaInferenceProvider):
        self.provider = provider
        self.calls = 0
        self.result: InferenceResult | None = None
        self.failure: str | None = None

    def infer(self, request: InferenceRequest) -> InferenceResult:
        self.calls += 1
        try:
            self.result = self.provider.infer(request)
        except IngestionError as exc:
            # Stable error codes only, never provider response bodies.
            self.failure = f"{exc.category}/{str(exc)}"
            raise
        return self.result


def write_case(path: Path, case: str) -> dict[str, int | None]:
    """Independent synthetic population: five physical records, four logical assets."""
    labels: list[str] = list(LABELS if case != "partially_renamed" else (*EXACT[:5], LABELS[5]))
    expected: dict[str, int | None] = dict(EXPECTED)
    rows = [
        ["SYN-1", "Example One", "Example City", "AZ", 12000000, "Wood Frame - Podium"],
        ["SYN-1", "Example One", "Example City", "AZ", 12000000, "Wood Frame - Podium"],
        ["SYN-2", "Example Two", "Example City", "AZ", 8000000, "Wood Frame - Low Rise"],
        ["SYN-3", "Example Three", "Example City", "CA", 5000000, "Wood Frame - Low Rise"],
        ["SYN-4", "Example Four", "Example City", "NY", 3000000, "Wood Frame - Low Rise"],
    ]
    if case == "absent_optional":
        labels.pop(2)
        for row in rows:
            row.pop(2)
        expected = {
            field: None if field == "city" else index - (index > 3)
            for field, index in EXPECTED.items()
        }
    if case == "ambiguous_tiv":
        labels.append("TIV")
        for row in rows:
            row.append(1)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Synthetic SOV"
    sheet.append(labels)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()
    return expected


def run_verification(provider: SchemaInferenceProvider) -> dict[str, object]:
    results = []
    with tempfile.TemporaryDirectory(prefix="sov-schema-live-") as directory:
        for case in CASES:
            source, db = Path(directory) / f"{case}.xlsx", Path(directory) / f"{case}.sqlite"
            expected = write_case(source, case)
            observed = ObservedProvider(provider)
            started = monotonic()
            passed = False
            classification = "success"
            mapping = None
            try:
                report = ingest_workbook(source, db, infer_schema=True, inference_provider=observed)
                if case == "ambiguous_tiv":
                    classification = "unexpected_acceptance"
                else:
                    with sqlite3.connect(db) as connection:
                        manifest = SchemaManifest.model_validate_json(
                            connection.execute("SELECT manifest_json FROM dataset").fetchone()[0]
                        )
                        mapping = {f.value: m.column for f, m in manifest.concepts.items()}
                        # Expectations are independent of model output and normalization.
                        assert mapping == expected
                        assert isinstance(report["schema_inference"], dict)
                        assert report["schema_inference"]["status"] == "applied"
                        assert report["physical_records"] == 5
                        assert report["logical_assets"] == 4
                        assert report["verified_duplicate_groups"] == 1
                        assert connection.execute(
                            "SELECT SUM(total_insurable_value_micro_usd) FROM assets"
                        ).fetchone() == (28000000000000,)
                        assert connection.execute(
                            "SELECT COUNT(*), SUM(total_insurable_value_micro_usd) FROM assets WHERE state='AZ'"
                        ).fetchone() == (2, 20000000000000)
                        passed = True
            except IngestionError as exc:
                classification = f"{exc.category}/{str(exc).split(':', 1)[0]}"
                passed = (
                    case == "ambiguous_tiv"
                    and exc.category.value == "mapping"
                    and str(exc).startswith("mapping_ambiguous")
                    and not db.exists()
                )
            except AssertionError:
                classification = "technical_fallback" if observed.failure else "reference_mismatch"
            result = observed.result
            results.append(
                {
                    "case": case,
                    "passed": passed,
                    "classification": classification,
                    "model_calls": observed.calls,
                    "provider_failure": observed.failure,
                    "model": result.model if result else None,
                    "usage": result.usage.model_dump() if result and result.usage else None,
                    "elapsed_seconds": round(monotonic() - started, 3),
                    "accepted_mapping": mapping,
                }
            )
    return {
        "attempted": len(results),
        "passed": sum(r["passed"] for r in results),
        "failed": sum(not r["passed"] for r in results),
        "cases": results,
    }


def main() -> None:
    report = run_verification(OpenAISchemaInferenceProvider())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    # Preserve every run, including failures; no selective retry/repair.
    archive = REPORT.with_name(f"schema-live-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json")
    archive.write_text(json.dumps(report, indent=2) + "\n")
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
