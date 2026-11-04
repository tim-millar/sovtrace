"""Fixed admission budgets for every part of an untrusted XLSX snapshot.

ZIP payloads are counted during raw, output-limited decompression, independently
of their declared sizes. Worksheet XML is inspected incrementally before either
eager openpyxl load. This is a bounded admission policy, not a universal DoS guard.
"""

import posixpath
import re
import stat
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from openpyxl.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from .errors import FailureCategory, IngestionError

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_ZIP_MEMBERS = 128
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_WORKSHEETS = 8
MAX_ROW_INDEX = 10_000
MAX_COLUMN_INDEX = 256
MAX_INFERENCE_COLUMNS = 128
MAX_GRID_POSITIONS = 250_000
MAX_REPRESENTED_CELLS = 100_000

_CHUNK = 64 * 1024
_LOCAL = struct.Struct("<4s5H3L2H")
SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def limit(resource: str, *, structure: bool = False) -> None:
    raise IngestionError(
        FailureCategory.STRUCTURE if structure else FailureCategory.WORKBOOK,
        f"Workbook resource limit exceeded: {resource}",
    )


def invalid(*, structure: bool = False) -> None:
    if structure:
        raise IngestionError(
            FailureCategory.STRUCTURE, "Malformed or unsupported workbook archive metadata"
        )
    # Preserve the typed archive cause used by the existing safe CLI diagnostic.
    raise BadZipFile("Malformed or unsupported workbook archive metadata")


def read_snapshot(source: Path) -> bytes:
    """Reject size/type before reading, then cap the actual immutable snapshot."""
    if source.suffix.lower() != ".xlsx":
        raise IngestionError(FailureCategory.WORKBOOK, "Only .xlsx workbooks are supported")
    metadata = source.stat()
    if stat.S_ISDIR(metadata.st_mode):
        raise IsADirectoryError("Source path refers to a directory")
    if not stat.S_ISREG(metadata.st_mode):
        raise IngestionError(FailureCategory.WORKBOOK, "Source must be a regular file")
    if metadata.st_size > MAX_INPUT_BYTES:
        limit("compressed input bytes")
    with source.open("rb") as stream:
        # Recheck the opened file, not just a potentially replaced path.
        import os

        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise IngestionError(FailureCategory.WORKBOOK, "Source must be a regular file")
        if opened.st_size > MAX_INPUT_BYTES:
            limit("compressed input bytes")
        content = stream.read(MAX_INPUT_BYTES + 1)
        if len(content) > MAX_INPUT_BYTES:
            limit("compressed input bytes")
    return content


def _member_range(content: bytes, info: ZipInfo, directory: int) -> tuple[int, int]:
    offset = info.header_offset
    if offset < 0 or offset + _LOCAL.size > directory:
        invalid()
    signature, _, flags, method, _, _, crc, compressed, expanded, names, extra = _LOCAL.unpack_from(
        content, offset
    )
    start = offset + _LOCAL.size
    end = start + names
    if signature != b"PK\x03\x04" or end + extra + info.compress_size > directory:
        invalid()
    filename = content[start:end].decode("utf-8" if flags & 0x800 else "cp437")
    if filename != info.filename or flags != info.flag_bits or method != info.compress_type:
        invalid()
    if expanded == 0xFFFFFFFF or compressed == 0xFFFFFFFF:
        # A small, valid ZIP64 entry can still fit every admission ceiling.
        cursor = end
        while cursor + 4 <= end + extra:
            tag, length = struct.unpack_from("<2H", content, cursor)
            cursor += 4
            if cursor + length > end + extra:
                invalid()
            if tag == 1:
                required = 8 * ((expanded == 0xFFFFFFFF) + (compressed == 0xFFFFFFFF))
                if length < required:
                    invalid()
                if expanded == 0xFFFFFFFF:
                    expanded = struct.unpack_from("<Q", content, cursor)[0]
                    cursor += 8
                    length -= 8
                if compressed == 0xFFFFFFFF:
                    compressed = struct.unpack_from("<Q", content, cursor)[0]
                break
            cursor += length
        else:
            invalid()
    if not flags & 8 and (crc, compressed, expanded) != (
        info.CRC,
        info.compress_size,
        info.file_size,
    ):
        invalid()
    start = end + extra
    end = start + info.compress_size
    return start, end


def _member_chunks(content: bytes, info: ZipInfo, directory: int) -> Iterator[bytes]:
    """Decode actual compressed bytes without trusting ZIP's output-length field."""
    start, end = _member_range(content, info, directory)
    method = info.compress_type
    decoder = zlib.decompressobj(-zlib.MAX_WBITS) if method == ZIP_DEFLATED else None
    for index in range(start, end, _CHUNK):
        data = content[index : min(index + _CHUNK, end)]
        if decoder is None:
            yield data
        else:
            while data:
                output = decoder.decompress(data, _CHUNK)
                data = decoder.unconsumed_tail
                if output:
                    yield output
                if decoder.unused_data:
                    invalid()
    if decoder is not None and not decoder.eof:
        invalid()


def _central_directory(content: bytes) -> None:
    """Bound central record counting before ZipFile materializes its member list."""
    end = content.rfind(b"PK\x05\x06", max(0, len(content) - 65557))
    if end < 0 or end + 22 > len(content):
        invalid()
    _, disk, start_disk, local_count, count, size, offset, comment = struct.unpack_from(
        "<4s4H2LH", content, end
    )
    if disk or start_disk or local_count != count or end + 22 + comment != len(content):
        invalid()
    directory_end = end
    if end >= 20 and content[end - 20 : end - 16] == b"PK\x06\x07":
        _, locator_disk, location, disks = struct.unpack_from("<4sLQL", content, end - 20)
        if locator_disk or disks != 1 or location + 56 > end - 20:
            invalid()
        signature, record_size, _, _, disk64, start64, local64, count64, size64, offset64 = (
            struct.unpack_from("<4sQ2H2L4Q", content, location)
        )
        if (
            signature != b"PK\x06\x06"
            or record_size < 44
            or location + 12 + record_size != end - 20
            or disk64
            or start64
            or local64 != count64
        ):
            invalid()
        if any(
            value not in (sentinel, actual)
            for value, sentinel, actual in (
                (count, 0xFFFF, count64),
                (size, 0xFFFFFFFF, size64),
                (offset, 0xFFFFFFFF, offset64),
            )
        ):
            invalid()
        count, size, offset, directory_end = count64, size64, offset64, location
    if count > MAX_ZIP_MEMBERS:
        limit("ZIP member count")
    if offset + size != directory_end:
        invalid()
    actual = 0
    cursor = offset
    while cursor < directory_end:
        if cursor + 46 > directory_end or content[cursor : cursor + 4] != b"PK\x01\x02":
            invalid()
        name, extra, annotation = struct.unpack_from("<3H", content, cursor + 28)
        cursor += 46 + name + extra + annotation
        actual += 1
        if actual > MAX_ZIP_MEMBERS:
            limit("ZIP member count")
    if cursor != directory_end or actual != count:
        invalid()


def _inspect_zip(content: bytes, archive: ZipFile) -> None:
    members = archive.infolist()
    if len(members) > MAX_ZIP_MEMBERS:
        limit("ZIP member count")
    names: set[str] = set()
    files: set[str] = set()
    declared = 0
    for member in members:
        name = member.filename
        canonical = name.rstrip("/").casefold()
        if (
            not canonical
            or member.orig_filename != name
            or "\\" in name
            or name.endswith("//")
            or ":" in name
            or any(ord(c) < 32 for c in name)
            or name.startswith("/")
            or any(p in {"", ".", ".."} for p in name.rstrip("/").split("/"))
            or canonical in names
            or member.flag_bits & 0x41
            or member.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
        ):
            invalid()
        names.add(canonical)
        if not member.is_dir():
            files.add(canonical)
        elif member.file_size:
            invalid()
        if member.file_size > MAX_MEMBER_BYTES:
            limit("declared expanded member bytes")
        declared += member.file_size
        if declared > MAX_EXPANDED_BYTES:
            limit("declared aggregate expanded bytes")
    for name in names:
        if any("/".join(name.split("/")[:i]) in files for i in range(1, len(name.split("/")))):
            invalid()
    # Reject overlapping local records as well as conflicting central names.
    ordered = sorted(members, key=lambda m: m.header_offset)
    for first, second in zip(ordered, ordered[1:]):
        if _member_range(content, first, archive.start_dir)[1] > second.header_offset:
            invalid()
    actual = 0
    for member in members:
        size = 0
        checksum = 0
        carry = b""
        for chunk in _member_chunks(content, member, archive.start_dir):
            size += len(chunk)
            actual += len(chunk)
            if size > MAX_MEMBER_BYTES:
                limit("actual expanded member bytes")
            if actual > MAX_EXPANDED_BYTES:
                limit("actual aggregate expanded bytes")
            checksum = zlib.crc32(chunk, checksum)
            if member.filename.lower().endswith((".xml", ".rels")):
                probe = (carry + chunk).replace(b"\x00", b"").lower()
                if b"<!doctype" in probe or b"<!entity" in probe:
                    invalid()
                carry = chunk[-64:]
        if size != member.file_size or checksum != member.CRC:
            invalid()


@contextmanager
def xml_events(archive: ZipFile, member: str, expected_root: str | None = None):
    """Bounded, clearing XML events; children are consumed before being cleared."""
    with archive.open(member) as stream:

        def events():
            stack = []
            first = True
            for event, element in ET.iterparse(stream, events=("start", "end")):
                if event == "start":
                    if first and expected_root is not None and element.tag != expected_root:
                        invalid()
                    first = False
                    stack.append(element)
                yield event, element
                if event == "end":
                    stack.pop()
                    element.clear()
                    if stack:
                        stack[-1].remove(element)

        yield events()


def coordinate(reference: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Za-z]{1,3})([1-9][0-9]{0,6})", reference)
    if match is None:
        invalid(structure=True)
    assert match is not None
    column = 0
    for letter in match[1].upper():
        column = column * 26 + ord(letter) - 64
    row = int(match[2])
    if row > MAX_ROW_INDEX:
        limit("worksheet row index", structure=True)
    if column > MAX_COLUMN_INDEX:
        limit("worksheet column index", structure=True)
    return row, column


def _index(value: str, maximum: int, resource: str) -> int:
    if not re.fullmatch(r"[0-9]{1,7}", value) or int(value) < 1:
        invalid(structure=True)
    result = int(value)
    if result > maximum:
        limit(resource, structure=True)
    return result


def _extent(reference: str) -> tuple[int, int]:
    parts = reference.split(":")
    if len(parts) not in {1, 2}:
        invalid(structure=True)
    low = coordinate(parts[0])
    high = coordinate(parts[-1])
    if low[0] > high[0] or low[1] > high[1]:
        invalid(structure=True)
    return high


def _worksheet(
    archive: ZipFile, member: str, previous_area: int, previous_cells: int
) -> tuple[int, int]:
    max_row = max_column = row = column = cells = 0
    last_row = 0
    in_row = False
    dimension = False
    seen: set[tuple[int, int]] = set()
    with xml_events(archive, member) as events:
        root = True
        for event, element in events:
            if event == "end":
                if element.tag == SHEET_NS + "row":
                    in_row = False
                continue
            if root:
                if element.tag != SHEET_NS + "worksheet":
                    invalid()
                root = False
            if element.tag == SHEET_NS + "dimension":
                if dimension:
                    invalid(structure=True)
                dimension = True
                r, c = _extent(element.attrib.get("ref", ""))
                max_row, max_column = max(max_row, r), max(max_column, c)
            elif element.tag == SHEET_NS + "row":
                if in_row:
                    invalid(structure=True)
                row = _index(
                    element.attrib.get("r", str(last_row + 1)), MAX_ROW_INDEX, "worksheet row index"
                )
                if row <= last_row:
                    invalid(structure=True)
                last_row, column, in_row = row, 0, True
                max_row = max(max_row, row)
            elif element.tag == SHEET_NS + "c":
                if not in_row:
                    invalid(structure=True)
                r, c = (
                    coordinate(element.attrib["r"]) if "r" in element.attrib else (row, column + 1)
                )
                if c > MAX_COLUMN_INDEX:
                    limit("worksheet column index", structure=True)
                if r != row or (r, c) in seen:
                    invalid(structure=True)
                seen.add((r, c))
                column = c
                max_row, max_column = max(max_row, r), max(max_column, c)
                cells += 1
                if previous_cells + cells > MAX_REPRESENTED_CELLS:
                    limit("aggregate represented cells", structure=True)
            elif element.tag == SHEET_NS + "mergeCell":
                r, c = _extent(element.attrib.get("ref", ""))
                max_row, max_column = max(max_row, r), max(max_column, c)
            elif element.tag == SHEET_NS + "col":
                low = _index(
                    element.attrib.get("min", ""), MAX_COLUMN_INDEX, "worksheet column index"
                )
                high = _index(
                    element.attrib.get("max", ""), MAX_COLUMN_INDEX, "worksheet column index"
                )
                if low > high:
                    invalid(structure=True)
                max_column = max(max_column, high)
            if previous_area + max_row * max_column > MAX_GRID_POSITIONS:
                limit("aggregate worksheet grid positions", structure=True)
    return max_row * max_column, cells


def inspect_workbook(content: bytes) -> dict[str, str]:
    """Admit every worksheet, returning names/parts for exact numeric extraction."""
    if len(content) > MAX_INPUT_BYTES:
        limit("compressed input bytes")
    try:
        _central_directory(content)
        with ZipFile(BytesIO(content)) as archive:
            _inspect_zip(content, archive)
            parts = set()
            with xml_events(
                archive,
                "[Content_Types].xml",
                "{http://schemas.openxmlformats.org/package/2006/content-types}Types",
            ) as events:
                for event, el in events:
                    if event == "start" and el.attrib.get("ContentType", "").endswith(
                        ".worksheet+xml"
                    ):
                        parts.add(el.attrib["PartName"].lstrip("/"))
                        if len(parts) > MAX_WORKSHEETS:
                            limit("worksheet count", structure=True)
            relationships = {}
            with xml_events(
                archive, "xl/_rels/workbook.xml.rels", _REL_NS + "Relationships"
            ) as events:
                for event, el in events:
                    if event == "start" and el.tag == _REL_NS + "Relationship":
                        identity = el.attrib["Id"]
                        if identity in relationships:
                            invalid()
                        target = el.attrib["Target"]
                        member = (
                            target.lstrip("/")
                            if target.startswith("/")
                            else posixpath.normpath("xl/" + target)
                        )
                        relationships[identity] = (
                            member,
                            el.attrib.get("Type", ""),
                            el.attrib.get("TargetMode"),
                        )
                        if el.attrib.get("Type", "").endswith("/worksheet"):
                            if el.attrib.get("TargetMode") == "External":
                                invalid()
                            parts.add(member)
                            if len(parts) > MAX_WORKSHEETS:
                                limit("worksheet count", structure=True)
            sheets = {}
            with xml_events(archive, "xl/workbook.xml", SHEET_NS + "workbook") as events:
                for event, el in events:
                    if event == "start" and el.tag == SHEET_NS + "sheet":
                        name = el.attrib["name"]
                        if name.casefold() in {n.casefold() for n in sheets}:
                            invalid()
                        member, kind, external = relationships[el.attrib[_REL_ID]]
                        if (
                            not kind.endswith("/worksheet")
                            or external == "External"
                            or member in sheets.values()
                        ):
                            invalid()
                        sheets[name] = member
                        parts.add(member)
                        if len(sheets) > MAX_WORKSHEETS:
                            limit("worksheet count", structure=True)
            parts.update(
                n
                for n in archive.namelist()
                if n.startswith("xl/worksheets/") and n.endswith(".xml")
            )
            if not sheets:
                invalid()
            if len(parts) > MAX_WORKSHEETS:
                limit("worksheet count", structure=True)
            area = cells = 0
            for member in sorted(parts):
                positions, represented = _worksheet(archive, member, area, cells)
                area += positions
                cells += represented
            return sheets
    except IngestionError:
        raise
    except (
        BadZipFile,
        ET.ParseError,
        KeyError,
        ValueError,
        OSError,
        EOFError,
        RuntimeError,
        struct.error,
        zlib.error,
        NotImplementedError,
    ) as exc:
        raise IngestionError(
            FailureCategory.WORKBOOK, "Malformed or unsupported workbook archive metadata"
        ) from exc


def check_sheet_bounds(sheet: Worksheet) -> None:
    """Defend direct helper calls before openpyxl rectangular iteration."""
    if sheet.max_row > MAX_ROW_INDEX:
        limit("worksheet row index", structure=True)
    if sheet.max_column > MAX_COLUMN_INDEX:
        limit("worksheet column index", structure=True)
    if sheet.max_row * sheet.max_column > MAX_GRID_POSITIONS:
        limit("aggregate worksheet grid positions", structure=True)


def check_loaded_workbook(workbook: Workbook) -> None:
    if len(workbook.worksheets) > MAX_WORKSHEETS:
        limit("worksheet count", structure=True)
    area = 0
    for sheet in workbook.worksheets:
        check_sheet_bounds(sheet)
        area += sheet.max_row * sheet.max_column
    if area > MAX_GRID_POSITIONS:
        limit("aggregate worksheet grid positions", structure=True)
