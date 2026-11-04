"""Reusable, offline one-workbook-per-database ingestion entry point."""

import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook import Workbook
from pydantic import ValidationError

from .discovery import discover_header, finalize_rows
from .errors import FailureCategory, IngestionError
from .inference import (
    SchemaInferenceProvider,
    check_competitors,
    make_request,
    mapping_error,
    resolve,
)
from .manifest import FIELD_DEFINITIONS, Diagnostic, SchemaManifest, SemanticField, exact_mappings
from .normalize import normalize
from .numeric_source import numeric_values
from .persistence import make_report, publish
from .resources import inspect_workbook, read_snapshot

__all__ = ["IngestionError", "ingest_workbook"]


def ingest_workbook(
    source: str | Path,
    db: str | Path,
    *,
    replace: bool = False,
    infer_schema: bool = False,
    inference_provider: SchemaInferenceProvider | None = None,
) -> dict[str, object]:
    """Import a supported .xlsx snapshot, or fail without publishing a partial database."""
    if inference_provider is not None and not infer_schema:
        raise IngestionError(
            FailureCategory.INFERENCE, "schema_inference_provider_error: inference_disabled"
        )
    source, output = Path(source), Path(db)
    if source.resolve() == output.resolve():
        raise IngestionError(
            FailureCategory.PERSISTENCE, "Source workbook cannot be the output database"
        )
    if output.exists() and not replace:
        raise IngestionError(FailureCategory.PERSISTENCE, f"Output exists: {output}; use --replace")
    formulas: Workbook | None = None
    cached: Workbook | None = None
    try:
        # Both modes and the content hash refer to exactly the same immutable byte snapshot.
        content = read_snapshot(source)
        inspect_workbook(content)
        sha256 = hashlib.sha256(content).hexdigest()
        formulas = load_workbook(BytesIO(content), data_only=False)
        cached = load_workbook(BytesIO(content), data_only=True)
        structure = discover_header(formulas, infer_schema=infer_schema)
        concepts, mapping_diagnostics = exact_mappings(structure.columns, structure.worksheet)
        inference_report: dict[str, object] = {
            "status": "disabled",
            "model": None,
            "inferred_fields": [],
        }
        if infer_schema:
            for field, mapping in concepts.items():
                if mapping.status == "ambiguous":
                    raise mapping_error("mapping_ambiguous", field)
            check_competitors(structure)
            if all(m.status == "mapped" for m in concepts.values()):
                inference_report["status"] = "not_needed"
            else:
                # Metadata bounds and semantic rejections never qualify for technical fallback.
                request = make_request(formulas[structure.worksheet], structure)
                try:
                    if inference_provider is None:
                        from .provider import OpenAISchemaInferenceProvider

                        inference_provider = OpenAISchemaInferenceProvider()
                    result = inference_provider.infer(request)
                except IngestionError as exc:
                    if exc.category != FailureCategory.INFERENCE:
                        raise
                    if not all(
                        concepts[f].status == "mapped"
                        for f, (_, _, required) in FIELD_DEFINITIONS.items()
                        if required
                    ):
                        raise
                    inference_report["status"] = "fallback"
                    mapping_diagnostics += (
                        Diagnostic(
                            code="schema_inference_fallback",
                            message="Technical inference failure; exact mappings used",
                            worksheet=structure.worksheet,
                        ),
                    )
                else:
                    concepts, mapping_diagnostics, inferred_fields = resolve(
                        structure, request, result.proposal
                    )
                    inference_report = {
                        "status": "applied",
                        "model": result.model,
                        "inferred_fields": inferred_fields,
                    }
        # Keep source-label's row validation and error contract; mappings finalize the same rows.
        structure = finalize_rows(
            formulas,
            structure,
            concepts[SemanticField.ASSET_ID].column,
            concepts[SemanticField.TIV].column,
            inferred=inference_report["status"] == "applied",
        )
        try:
            manifest = SchemaManifest(
                workbook_filename=source.name,
                workbook_sha256=sha256,
                worksheet=structure.worksheet,
                header_row=structure.header_row,
                data_start_row=structure.asset_rows[0],
                data_end_row=structure.asset_rows[-1],
                columns=structure.columns,
                concepts=concepts,
                diagnostics=mapping_diagnostics,
            )
        except ValidationError as exc:
            details = "; ".join(e["msg"] for e in exc.errors(include_input=False))
            raise IngestionError(FailureCategory.MAPPING, details) from exc
        data = normalize(
            formulas[manifest.worksheet],
            cached[manifest.worksheet],
            structure.asset_rows,
            manifest,
            numeric_values(content, manifest.worksheet),
        )
        diagnostics = manifest.diagnostics + structure.diagnostics + data.diagnostics
        report = make_report(manifest, data, structure.excluded, diagnostics)
        report["schema_inference"] = inference_report
    except IngestionError:
        raise
    except (
        OSError,
        BadZipFile,
        InvalidFileException,
        KeyError,
        ValueError,
        TypeError,
        SyntaxError,
        EOFError,
        RuntimeError,
    ) as exc:
        raise IngestionError(
            FailureCategory.WORKBOOK, f"Cannot load supported workbook {source.name}: {exc}"
        ) from exc
    finally:
        if formulas is not None:
            formulas.close()
        if cached is not None:
            cached.close()
    publish(output, replace, manifest, data, diagnostics, report)
    return {**report, "output": str(output)}
