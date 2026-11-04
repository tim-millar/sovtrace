"""Self-contained SQLite dataset, committed and validated before atomic publication."""

import json
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .errors import FailureCategory, IngestionError
from .manifest import Diagnostic, SchemaManifest
from .normalize import MICRO_USD, NormalizedData, source_json

DDL = """
CREATE TABLE dataset (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    workbook_filename TEXT NOT NULL,
    workbook_sha256 TEXT NOT NULL UNIQUE,
    worksheet TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    monetary_scale INTEGER NOT NULL CHECK (monetary_scale = 1000000),
    assumptions_json TEXT NOT NULL,
    report_json TEXT NOT NULL
) STRICT;
CREATE TABLE source_columns (
    workbook_sha256 TEXT NOT NULL REFERENCES dataset(workbook_sha256),
    worksheet TEXT NOT NULL,
    column_index INTEGER NOT NULL,
    excel_letter TEXT NOT NULL,
    original_label TEXT,
    PRIMARY KEY (workbook_sha256, worksheet, column_index)
) STRICT;
CREATE TABLE source_rows (
    workbook_sha256 TEXT NOT NULL REFERENCES dataset(workbook_sha256),
    worksheet TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    PRIMARY KEY (workbook_sha256, worksheet, row_number)
) STRICT;
CREATE TABLE source_cells (
    workbook_sha256 TEXT NOT NULL,
    worksheet TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    column_index INTEGER NOT NULL,
    coordinate TEXT NOT NULL,
    original_type TEXT NOT NULL,
    original_value_json TEXT NOT NULL,
    formula TEXT,
    cached_type TEXT,
    cached_value_json TEXT NOT NULL,
    PRIMARY KEY (workbook_sha256, worksheet, row_number, column_index),
    FOREIGN KEY (workbook_sha256, worksheet, row_number)
        REFERENCES source_rows(workbook_sha256, worksheet, row_number),
    FOREIGN KEY (workbook_sha256, worksheet, column_index)
        REFERENCES source_columns(workbook_sha256, worksheet, column_index)
) STRICT;
CREATE TABLE assets (
    asset_id TEXT PRIMARY KEY CHECK (length(trim(asset_id)) > 0),
    property_name TEXT,
    city TEXT,
    state TEXT,
    total_insurable_value_micro_usd INTEGER,
    construction_type TEXT
) STRICT;
CREATE TABLE asset_sources (
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    workbook_sha256 TEXT NOT NULL,
    worksheet TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    PRIMARY KEY (asset_id, workbook_sha256, worksheet, row_number),
    UNIQUE (workbook_sha256, worksheet, row_number),
    FOREIGN KEY (workbook_sha256, worksheet, row_number)
        REFERENCES source_rows(workbook_sha256, worksheet, row_number)
) STRICT;
CREATE TABLE diagnostics (
    diagnostic_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    workbook_sha256 TEXT NOT NULL REFERENCES dataset(workbook_sha256),
    worksheet TEXT NOT NULL,
    row_number INTEGER,
    column_index INTEGER,
    semantic_field TEXT
) STRICT;
CREATE TABLE field_completeness (
    semantic_field TEXT PRIMARY KEY,
    availability TEXT NOT NULL,
    physical_non_null INTEGER NOT NULL,
    physical_total INTEGER NOT NULL,
    logical_non_null INTEGER NOT NULL,
    logical_total INTEGER NOT NULL
) STRICT;
"""


def make_report(
    manifest: SchemaManifest,
    data: NormalizedData,
    excluded: dict[str, int],
    diagnostics: tuple[Diagnostic, ...],
) -> dict[str, object]:
    completeness = {
        field.value: {
            "availability": mapping.status,
            "physical_non_null": sum(
                getattr(r.asset, field.value) is not None for r in data.records
            ),
            "physical_total": len(data.records),
            "logical_non_null": sum(
                getattr(a, field.value) is not None for a in data.assets.values()
            ),
            "logical_total": len(data.assets),
        }
        for field, mapping in manifest.concepts.items()
    }
    return {
        "workbook": manifest.workbook_filename,
        "sha256": manifest.workbook_sha256,
        "worksheet": manifest.worksheet,
        "header_row": manifest.header_row,
        "physical_records": len(data.records),
        "logical_assets": len(data.assets),
        "verified_duplicate_groups": sum(len(rows) > 1 for rows in data.source_rows.values()),
        "excluded_rows": excluded,
        "mapped_fields": [f.value for f, m in manifest.concepts.items() if m.status == "mapped"],
        "field_completeness": completeness,
        "valuation_complete": all(
            a.total_insurable_value_micro_usd is not None for a in data.assets.values()
        ),
        "diagnostic_summary": dict(Counter(d.code for d in diagnostics)),
    }


def _write_database(
    path: Path,
    manifest: SchemaManifest,
    data: NormalizedData,
    diagnostics: tuple[Diagnostic, ...],
    report: dict[str, object],
) -> None:
    identity = (manifest.workbook_sha256, manifest.worksheet)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(DDL)
        with connection:
            connection.execute(
                "INSERT INTO dataset VALUES (1, ?, ?, ?, ?, 'USD', ?, ?, ?)",
                (
                    manifest.workbook_filename,
                    *identity,
                    manifest.model_dump_json(),
                    MICRO_USD,
                    json.dumps(
                        {
                            "currency": "USD inferred from source dollar formatting and exercise context",
                            "formula_cache": "Cached values used without proving recalculation freshness",
                            "duplicates": "Same normalized EPIC; all business values verified identical",
                            "rounding": "Decimal ROUND_HALF_UP to six USD decimal places before integer scaling",
                            "aggregate_range": "Positive and negative logical subtotals each fit signed int64",
                        }
                    ),
                    json.dumps(report),
                ),
            )
            connection.executemany(
                "INSERT INTO source_columns VALUES (?, ?, ?, ?, ?)",
                ((*identity, c.index, c.letter, c.label) for c in manifest.columns),
            )
            connection.executemany(
                "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?)",
                (tuple(asdict(a).values()) for a in data.assets.values()),
            )
            for record in data.records:
                connection.execute(
                    "INSERT INTO source_rows VALUES (?, ?, ?)", (*identity, record.row)
                )
                connection.executemany(
                    "INSERT INTO source_cells VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            *identity,
                            record.row,
                            c.column,
                            c.coordinate,
                            c.data_type,
                            source_json(c.value),
                            c.value if c.data_type == "f" else None,
                            c.cached_type,
                            source_json(c.cached_value),
                        )
                        for c in record.cells
                    ),
                )
                connection.execute(
                    "INSERT INTO asset_sources VALUES (?, ?, ?, ?)",
                    (record.asset.asset_id, *identity, record.row),
                )
            connection.executemany(
                "INSERT INTO diagnostics (code, severity, message, workbook_sha256, worksheet, "
                "row_number, column_index, semantic_field) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        d.code,
                        d.severity,
                        d.message,
                        manifest.workbook_sha256,
                        d.worksheet,
                        d.row,
                        d.column,
                        d.field,
                    )
                    for d in diagnostics
                ),
            )
            for field, mapping in manifest.concepts.items():
                connection.execute(
                    "INSERT INTO field_completeness VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        field,
                        mapping.status,
                        sum(getattr(r.asset, field.value) is not None for r in data.records),
                        len(data.records),
                        sum(getattr(a, field.value) is not None for a in data.assets.values()),
                        len(data.assets),
                    ),
                )
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise sqlite3.DatabaseError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.DatabaseError("SQLite source reference validation failed")
        for relation, count in (
            ("assets", len(data.assets)),
            ("source_rows", len(data.records)),
            ("asset_sources", len(data.records)),
            ("source_cells", len(data.records) * len(manifest.columns)),
        ):
            if connection.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0] != count:
                raise sqlite3.DatabaseError(f"SQLite {relation} count validation failed")
    finally:
        connection.close()


def publish(
    output: Path,
    replace: bool,
    manifest: SchemaManifest,
    data: NormalizedData,
    diagnostics: tuple[Diagnostic, ...],
    report: dict[str, object],
) -> None:
    temporary: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".sov-import-", suffix=".sqlite", dir=output.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        _write_database(temporary, manifest, data, diagnostics, report)
        # The closed file contains its committed rollback-journal transaction.
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, output)
        else:
            # A hard link atomically claims the destination without a check/replace race.
            os.link(temporary, output)
    except (OSError, sqlite3.Error) as exc:
        raise IngestionError(
            FailureCategory.PERSISTENCE, f"Cannot publish SQLite database at {output}: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise IngestionError(
                    FailureCategory.PERSISTENCE,
                    f"Cannot remove temporary database {temporary}: {exc}",
                ) from exc
