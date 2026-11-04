"""Fast admission failures before eager parsing, inference or publication."""

import re
import struct
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import load_workbook
from test_schema_inference import RENAMED
from workbook_factory import HEADERS, asset, write_workbook

from sovtrace import ingestion
from sovtrace.ingestion import IngestionError, ingest_workbook, numeric_source, resources


def rewrite(source, transform, extras=()):
    with ZipFile(source) as archive:
        parts = [(name, archive.read(name)) for name in archive.namelist()]
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        for name, data in parts:
            archive.writestr(name, transform(name, data))
        for name, data in extras:
            archive.writestr(name, data)


def worksheet(source, transform):
    rewrite(
        source, lambda name, data: transform(data) if name == "xl/worksheets/sheet1.xml" else data
    )


def rejected(source, tmp_path, monkeypatch, message, *, existing=True):
    output = tmp_path / "result.sqlite"
    sentinel = b"existing database must survive rejected replacement"
    if existing:
        output.write_bytes(sentinel)
    forbidden = Mock(side_effect=AssertionError("Rejected input reached a downstream stage"))
    for name in ("load_workbook", "discover_header", "make_request", "numeric_values", "publish"):
        monkeypatch.setattr(ingestion, name, forbidden)
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", forbidden)
    with pytest.raises(IngestionError, match=message) as error:
        ingest_workbook(source, output, infer_schema=True, replace=True)
    assert error.value.category in {"workbook", "structure"}
    forbidden.assert_not_called()
    assert output.read_bytes() == sentinel if existing else not output.exists()


@pytest.mark.parametrize("existing", [False, True])
def test_oversized_input_is_rejected_before_open_or_materialization(
    tmp_path, monkeypatch, existing
):
    source = write_workbook(tmp_path / "large.xlsx")
    monkeypatch.setattr(resources, "MAX_INPUT_BYTES", source.stat().st_size - 1)
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path != source, "Oversized input must not be opened"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    rejected(source, tmp_path, monkeypatch, "compressed input bytes", existing=existing)


def test_input_growth_is_bounded_during_the_actual_snapshot_read(tmp_path, monkeypatch):
    source = write_workbook(tmp_path / "growing.xlsx")
    maximum = source.stat().st_size + 8
    monkeypatch.setattr(resources, "MAX_INPUT_BYTES", maximum)
    original = Path.open
    streams = []

    class Growing:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            assert size == maximum + 1
            with original(source, "ab") as writer:
                writer.write(b"x" * 32)
            return self.stream.read(size)

    def growing(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        if path == source and args == ("rb",):
            streams.append(stream)
            return Growing(stream)
        return stream

    monkeypatch.setattr(Path, "open", growing)
    rejected(source, tmp_path, monkeypatch, "compressed input bytes")
    assert len(streams) == 1 and streams[0].closed


@pytest.mark.parametrize("resource", ["MAX_ZIP_MEMBERS", "MAX_MEMBER_BYTES", "MAX_EXPANDED_BYTES"])
def test_declared_zip_budgets_precede_decompression(tmp_path, monkeypatch, resource):
    source = write_workbook(tmp_path / "declared.xlsx")
    with ZipFile(source) as archive:
        members = archive.infolist()
        ceiling = {
            "MAX_ZIP_MEMBERS": len(members),
            "MAX_MEMBER_BYTES": max(m.file_size for m in members),
            "MAX_EXPANDED_BYTES": sum(m.file_size for m in members),
        }[resource]
    monkeypatch.setattr(resources, resource, ceiling - 1)
    decode = Mock(side_effect=AssertionError("Declared limit must precede decompression"))
    monkeypatch.setattr(resources, "_member_chunks", decode)
    rejected(source, tmp_path, monkeypatch, "Workbook resource limit exceeded")
    decode.assert_not_called()


def underdeclared_zip(payloads, *, honest_first=False):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for index, payload in enumerate(payloads):
            archive.writestr(f"artificial-{index}.bin", payload)
    data = bytearray(output.getvalue())
    with ZipFile(BytesIO(data)) as archive:
        for index, member in enumerate(archive.infolist()):
            struct.pack_into(
                "<L",
                data,
                member.header_offset + 22,
                len(payloads[0]) if honest_first and index == 0 else 1,
            )
    for index, match in enumerate(re.finditer(b"PK\x01\x02", data)):
        struct.pack_into(
            "<L", data, match.start() + 24, len(payloads[0]) if honest_first and index == 0 else 1
        )
    return bytes(data)


@pytest.mark.parametrize("kind", ["member", "aggregate"])
def test_actual_expansion_is_counted_despite_false_size_metadata(tmp_path, monkeypatch, kind):
    data = underdeclared_zip(
        [b"A" * 5000] if kind == "member" else [b"A" * 300, b"B" * 300],
        honest_first=kind == "aggregate",
    )
    source = tmp_path / "underdeclared.xlsx"
    source.write_bytes(data)
    monkeypatch.setattr(resources, "MAX_MEMBER_BYTES", 100 if kind == "member" else 1000)
    monkeypatch.setattr(resources, "MAX_EXPANDED_BYTES", 10_000 if kind == "member" else 500)
    rejected(
        source,
        tmp_path,
        monkeypatch,
        f"actual {'expanded member' if kind == 'member' else 'aggregate expanded'} bytes",
    )


def test_small_compressed_archive_with_large_expansion(tmp_path, monkeypatch):
    source = write_workbook(tmp_path / "expansion.xlsx")
    rewrite(source, lambda n, b: b, [("artificial-padding.bin", b"A" * 50_000)])
    assert source.stat().st_size < 10_000
    monkeypatch.setattr(resources, "MAX_MEMBER_BYTES", 40_000)
    rejected(source, tmp_path, monkeypatch, "declared expanded member bytes")


@pytest.mark.parametrize("reference", ["A1:A10001", "A1:IW3", "A1:XFD1048576"])
def test_declared_dimensions_are_checked_before_openpyxl(tmp_path, monkeypatch, reference):
    source = write_workbook(tmp_path / "dimension.xlsx")
    worksheet(
        source,
        lambda b: re.sub(rb"<dimension[^>]*/>", f'<dimension ref="{reference}"/>'.encode(), b),
    )
    rejected(source, tmp_path, monkeypatch, "worksheet (row|column) index")


@pytest.mark.parametrize("remove_dimension", [False, True])
@pytest.mark.parametrize("row,cell", [("10001", "A10001"), ("2", "ZZZ2"), ("1000000", "A1000000")])
def test_actual_sparse_coordinates_cannot_hide_behind_dimensions(
    tmp_path, monkeypatch, row, cell, remove_dimension
):
    source = write_workbook(tmp_path / "sparse.xlsx")

    def sparse(data):
        data = re.sub(
            rb"<sheetData>.*?</sheetData>",
            f'<sheetData><row r="{row}"><c r="{cell}" t="n"><v>1</v></c></row></sheetData>'.encode(),
            data,
        )
        return re.sub(rb"<dimension[^>]*/>", b"", data) if remove_dimension else data

    worksheet(source, sparse)
    rejected(source, tmp_path, monkeypatch, "worksheet (row|column) index")


def two_sheets(source):
    workbook = load_workbook(source)
    workbook.create_sheet("Artificial distractor").cell(3, 14, "unselected")
    workbook.save(source)
    workbook.close()


@pytest.mark.parametrize("kind", ["grid", "cells"])
def test_aggregate_budgets_include_unselected_worksheets(tmp_path, monkeypatch, kind):
    source = write_workbook(tmp_path / "aggregate.xlsx")
    two_sheets(source)
    with ZipFile(source) as archive:
        counts = [
            len(ET.fromstring(archive.read(n)).findall(".//" + resources.SHEET_NS + "c"))
            for n in archive.namelist()
            if n.startswith("xl/worksheets/")
        ]
    if kind == "grid":
        monkeypatch.setattr(resources, "MAX_GRID_POSITIONS", 2 * 3 * 14 - 1)
    else:
        assert max(counts) <= sum(counts) - 1
        monkeypatch.setattr(resources, "MAX_REPRESENTED_CELLS", sum(counts) - 1)
    rejected(
        source,
        tmp_path,
        monkeypatch,
        "aggregate worksheet grid positions" if kind == "grid" else "aggregate represented cells",
    )


def test_excessive_worksheet_count_is_rejected(tmp_path, monkeypatch):
    source = write_workbook(tmp_path / "sheets.xlsx")
    workbook = load_workbook(source)
    for i in range(8):
        workbook.create_sheet(f"Artificial {i}")
    workbook.save(source)
    workbook.close()
    rejected(source, tmp_path, monkeypatch, "worksheet count")


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "conflicting",
        "unsafe",
        "encrypted",
        "method",
        "zip",
        "xml",
        "row",
        "cell",
        "merge",
        "columns",
        "entity",
        "crc",
    ],
)
def test_malformed_or_unsupported_metadata_fails_before_parsing(tmp_path, monkeypatch, kind):
    source = write_workbook(tmp_path / "malformed.xlsx")
    if kind == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            rewrite(source, lambda n, b: b, [("xl/workbook.xml", b"conflicting")])
    elif kind in {"conflicting", "unsafe"}:
        rewrite(
            source,
            lambda n, b: b,
            [("XL/WORKBOOK.XML" if kind == "conflicting" else "xl/../unsafe.xml", b"x")],
        )
    elif kind in {"encrypted", "method"}:
        data = bytearray(source.read_bytes())
        central = data.index(b"PK\x01\x02")
        struct.pack_into(
            "<H", data, 6 if kind == "encrypted" else 8, 1 if kind == "encrypted" else 99
        )
        struct.pack_into(
            "<H",
            data,
            central + (8 if kind == "encrypted" else 10),
            1 if kind == "encrypted" else 99,
        )
        source.write_bytes(data)
    elif kind == "zip":
        source.write_bytes(b"not an archive")
    elif kind == "crc":
        data = bytearray(source.read_bytes())
        data[40] ^= 0x40
        source.write_bytes(data)
    else:
        replacements = {
            "xml": b"<worksheet>",
            "row": b'<row r="not-a-row"><c r="A2"><v>1</v></c></row>',
            "cell": b'<row r="2"><c r="A3"><v>1</v></c></row>',
            "merge": b'<row r="2"><c r="A2"><v>1</v></c><c r="A2"><v>2</v></c></row>',
            "columns": b'<row r="2"><c r="!2"><v>1</v></c></row>',
            "entity": b'<!DOCTYPE worksheet [<!ENTITY test "x">]><worksheet/>',
        }
        if kind in {"xml", "entity"}:
            worksheet(source, lambda b: replacements[kind])
        else:
            worksheet(
                source,
                lambda b: re.sub(
                    rb"<sheetData>.*?</sheetData>",
                    b"<sheetData>" + replacements[kind] + b"</sheetData>",
                    b,
                ),
            )
    rejected(source, tmp_path, monkeypatch, "Malformed or unsupported")


def test_merge_extent_cannot_force_oversized_eager_materialization(tmp_path, monkeypatch):
    source = write_workbook(tmp_path / "merge.xlsx")
    worksheet(
        source,
        lambda b: b.replace(
            b"</worksheet>",
            b'<mergeCells count="1"><mergeCell ref="A1:XFD1048576"/></mergeCells></worksheet>',
        ),
    )
    rejected(source, tmp_path, monkeypatch, "worksheet (row|column) index")


@pytest.mark.parametrize("margin", [0, 1])
def test_success_at_and_immediately_below_representative_limits(tmp_path, monkeypatch, margin):
    source = write_workbook(tmp_path / "near-limit.xlsx", trailing=0)
    original = source.read_bytes()
    with ZipFile(source) as archive:
        members = archive.infolist()
        count = sum(
            len(ET.fromstring(archive.read(n)).findall(".//" + resources.SHEET_NS + "c"))
            for n in archive.namelist()
            if n.startswith("xl/worksheets/")
        )
    limits = {
        "MAX_INPUT_BYTES": len(original),
        "MAX_ZIP_MEMBERS": len(members),
        "MAX_MEMBER_BYTES": max(m.file_size for m in members),
        "MAX_EXPANDED_BYTES": sum(m.file_size for m in members),
        "MAX_WORKSHEETS": 1,
        "MAX_ROW_INDEX": 3,
        "MAX_COLUMN_INDEX": 14,
        "MAX_GRID_POSITIONS": 42,
        "MAX_REPRESENTED_CELLS": count,
    }
    for key, value in limits.items():
        monkeypatch.setattr(resources, key, value + margin)
    report = ingest_workbook(source, tmp_path / "accepted.sqlite")
    assert report["physical_records"] == report["logical_assets"] == 1
    assert source.read_bytes() == original


@pytest.mark.parametrize("failure", [False, True])
def test_archive_streams_and_workbooks_close_on_both_paths(tmp_path, monkeypatch, failure):
    source = write_workbook(tmp_path / "closure.xlsx", rows=[asset(123)] if failure else None)
    archives, streams, workbooks = [], [], []

    class TrackedZip(ZipFile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            archives.append(self)

        def open(self, *args, **kwargs):
            stream = super().open(*args, **kwargs)
            streams.append(stream)
            return stream

    original = ingestion.load_workbook

    def load(*args, **kwargs):
        workbook = original(*args, **kwargs)
        workbook.close = Mock(wraps=workbook.close)
        workbooks.append(workbook)
        return workbook

    monkeypatch.setattr(resources, "ZipFile", TrackedZip)
    monkeypatch.setattr(numeric_source, "ZipFile", TrackedZip)
    monkeypatch.setattr(ingestion, "load_workbook", load)
    if failure:
        with pytest.raises(IngestionError):
            ingest_workbook(source, tmp_path / "failure.sqlite")
    else:
        ingest_workbook(source, tmp_path / "success.sqlite")
    assert archives and streams and len(workbooks) == 2
    assert all(a.fp is None for a in archives)
    assert all(s.closed for s in streams)
    for workbook in workbooks:
        workbook.close.assert_called_once_with()


def test_numeric_extraction_direct_call_also_enforces_admission(tmp_path, monkeypatch):
    source = write_workbook(tmp_path / "numeric.xlsx")
    worksheet(
        source, lambda b: re.sub(rb"<dimension[^>]*/>", b'<dimension ref="A1:XFD1048576"/>', b)
    )
    with pytest.raises(IngestionError, match="worksheet (row|column) index"):
        numeric_source.numeric_values(source.read_bytes(), "Statement of Values")


def test_existing_committed_synthetic_workbook_is_admitted(tmp_path):
    source = Path(__file__).resolve().parents[1] / "examples/synthetic-sov.xlsx"
    report = ingest_workbook(source, tmp_path / "demo.sqlite")
    assert (report["logical_assets"], report["physical_records"]) == (4, 5)


def test_false_central_count_cannot_bypass_member_limit(tmp_path, monkeypatch):
    source = write_workbook(tmp_path / "count.xlsx")
    with ZipFile(source) as archive:
        maximum = len(archive.infolist()) - 1
    data = bytearray(source.read_bytes())
    end = data.rindex(b"PK\x05\x06")
    struct.pack_into("<2H", data, end + 8, 1, 1)
    source.write_bytes(data)
    monkeypatch.setattr(resources, "MAX_ZIP_MEMBERS", maximum)
    zip_parser = Mock(
        side_effect=AssertionError("Central count must precede member-list allocation")
    )
    monkeypatch.setattr(resources, "ZipFile", zip_parser)
    rejected(source, tmp_path, monkeypatch, "ZIP member count")
    zip_parser.assert_not_called()


def test_sparse_unselected_sheet_is_rejected_before_any_workbook_load(tmp_path, monkeypatch):
    source = write_workbook(tmp_path / "distractor.xlsx")
    two_sheets(source)
    rewrite(
        source,
        lambda n, b: (
            b.replace(b'r="3"', b'r="1000000"').replace(b'r="N3"', b'r="A1000000"')
            if n == "xl/worksheets/sheet2.xml"
            else b
        ),
    )
    rejected(source, tmp_path, monkeypatch, "worksheet row index")


def test_nonregular_source_is_rejected_before_open(tmp_path, monkeypatch):
    source = tmp_path / "directory.xlsx"
    source.mkdir()
    rejected(source, tmp_path, monkeypatch, "Source path refers to a directory")


@pytest.mark.parametrize("infer_schema", [False, True])
def test_257_column_workbook_is_rejected_before_eager_parsing(tmp_path, monkeypatch, infer_schema):
    source = write_workbook(
        tmp_path / "too-wide.xlsx",
        headers=[*HEADERS, *[f"Artificial extra {i}" for i in range(257 - len(HEADERS))]],
        rows=[[*asset(), *["artificial" for _ in range(257 - len(HEADERS))]]],
        summary=False,
    )
    output = tmp_path / "existing.sqlite"
    output.write_bytes(b"existing database sentinel")
    forbidden = Mock(side_effect=AssertionError("Over-budget input reached a downstream stage"))
    for name in ("load_workbook", "make_request", "publish"):
        monkeypatch.setattr(ingestion, name, forbidden)
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", forbidden)
    with pytest.raises(IngestionError, match="worksheet column index") as error:
        ingest_workbook(source, output, replace=True, infer_schema=infer_schema)
    assert error.value.category == "structure"
    forbidden.assert_not_called()
    assert output.read_bytes() == b"existing database sentinel"


@pytest.mark.parametrize("width", [129, 144, 256])
@pytest.mark.parametrize("unresolved", ["all", "optional"])
def test_actual_inference_width_limit_rejects_without_provider_or_truncation(
    tmp_path, monkeypatch, width, unresolved
):
    labels = RENAMED.copy() if unresolved == "all" else HEADERS.copy()
    labels[5] = RENAMED[5]
    labels += [f"Artificial extra {i}" for i in range(width - len(labels))]
    source = write_workbook(tmp_path / "needs-inference.xlsx", headers=labels, summary=False)
    output = tmp_path / "existing.sqlite"
    output.write_bytes(b"existing database sentinel")
    forbidden = Mock(
        side_effect=AssertionError("Oversized inference must not call provider/publish")
    )
    monkeypatch.setattr("sovtrace.ingestion.provider.OpenAISchemaInferenceProvider", forbidden)
    monkeypatch.setattr(ingestion, "publish", forbidden)
    original = ingestion.make_request
    captured = []

    def request(sheet, structure):
        captured.append([c.label for c in structure.columns])
        return original(sheet, structure)

    monkeypatch.setattr(ingestion, "make_request", request)
    with pytest.raises(IngestionError) as error:
        ingest_workbook(source, output, replace=True, infer_schema=True)
    if unresolved == "all":
        assert error.value.category == "structure"
        assert str(error.value) == "Unsupported inference column span"
    else:
        assert error.value.category == "schema-inference"
        assert str(error.value) == "schema_inference_metadata_limit"
        assert captured == [labels]  # Complete physical span; no truncation or omission.
    forbidden.assert_not_called()
    assert output.read_bytes() == b"existing database sentinel"


@pytest.mark.parametrize("ceiling", ["MAX_GRID_POSITIONS", "MAX_REPRESENTED_CELLS"])
def test_wide_admitted_columns_still_obey_independent_aggregate_budgets(
    tmp_path, monkeypatch, ceiling
):
    labels = [*HEADERS, *[f"Artificial extra {i}" for i in range(144 - len(HEADERS))]]
    source = write_workbook(tmp_path / "wide-aggregate.xlsx", headers=labels, summary=False)
    with ZipFile(source) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cells = len(root.findall(".//" + resources.SHEET_NS + "c"))
    # The general 256-column budget passes; only the selected independent ceiling fails.
    monkeypatch.setattr(
        resources, ceiling, 144 - 1 if ceiling == "MAX_GRID_POSITIONS" else cells - 1
    )
    assert resources.MAX_COLUMN_INDEX == 256
    rejected(
        source,
        tmp_path,
        monkeypatch,
        "aggregate worksheet grid positions"
        if ceiling == "MAX_GRID_POSITIONS"
        else "aggregate represented cells",
    )


@pytest.mark.parametrize("directory64", [False, True])
def test_in_budget_zip64_metadata_preserves_supported_workbook(tmp_path, directory64):
    source = write_workbook(tmp_path / "small-zip64.xlsx")
    with ZipFile(source) as archive:
        members = [(n, archive.read(n)) for n in archive.namelist()]
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        for name, data in members:
            with archive.open(name, "w", force_zip64=True) as member:
                member.write(data)
    if directory64:
        content = bytearray(source.read_bytes())
        end = content.rindex(b"PK\x05\x06")
        original = content[end:]
        _, _, _, count, _, size, offset, _ = struct.unpack_from("<4s4H2LH", original)
        record = struct.pack(
            "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, size, offset
        )
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, end, 1)
        struct.pack_into("<2H2L", original, 8, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
        source.write_bytes(content[:end] + record + locator + original)
    report = ingest_workbook(source, tmp_path / "accepted.sqlite")
    assert report["logical_assets"] == report["physical_records"] == 1
