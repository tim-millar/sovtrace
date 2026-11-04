"""Source-preserving cells, typed values and verified duplicate consolidation."""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from openpyxl.worksheet.worksheet import Worksheet

from .errors import FailureCategory, IngestionError
from .manifest import Diagnostic, SchemaManifest, SemanticField

MICRO_USD = 1_000_000
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
# Deterministic US state names; DC is supported explicitly.
STATE_NAMES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
    "Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|"
    "Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|"
    "Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|"
    "North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
    "South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|"
    "Wisconsin|Wyoming|District of Columbia"
).split("|")
STATE_CODES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
STATES = dict(zip((name.upper() for name in STATE_NAMES), STATE_CODES, strict=True))
STATES.update({code: code for code in STATE_CODES})


def source_json(value: object) -> str:
    """Preserve openpyxl values and distinguish date/time subtypes losslessly."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise IngestionError(FailureCategory.DATA, "Non-finite source number")
        return str(value)
    if isinstance(value, (datetime, date, time)):
        value = {"python_type": type(value).__name__, "iso": value.isoformat()}
    elif isinstance(value, timedelta):
        value = {
            "python_type": "timedelta",
            "days": value.days,
            "seconds": value.seconds,
            "microseconds": value.microseconds,
        }
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise IngestionError(FailureCategory.DATA, "Unsupported source-cell value type") from exc


@dataclass(frozen=True)
class SourceCell:
    column: int
    coordinate: str
    data_type: str
    value: object
    cached_type: str | None
    cached_value: object

    @property
    def effective_value(self) -> object:
        return self.cached_value if self.data_type == "f" else self.value

    @property
    def effective_type(self) -> str:
        return (self.cached_type or "n") if self.data_type == "f" else self.data_type


def _is_numeric(value: object) -> bool:
    """Only decoded numbers may receive exact XML numeric values."""
    return isinstance(value, (int, float, Decimal)) and not isinstance(
        value, (bool, date, datetime, time, timedelta)
    )


def read_cells(
    formulas: Worksheet,
    cached: Worksheet,
    row: int,
    manifest: SchemaManifest,
    numeric: dict[str, Decimal],
) -> tuple[SourceCell, ...]:
    cells: list[SourceCell] = []
    for column in manifest.columns:
        original = formulas.cell(row, column.index)
        calculated = cached.cell(row, column.index)
        if original.data_type == "f" and not isinstance(original.value, str):
            raise IngestionError(
                FailureCategory.STRUCTURE,
                f"{formulas.title}!{original.coordinate}: unsupported formula kind",
            )
        cells.append(
            SourceCell(
                column.index,
                original.coordinate,
                original.data_type,
                numeric.get(original.coordinate, original.value)
                if original.data_type == "n" and _is_numeric(original.value)
                else original.value,
                calculated.data_type if original.data_type == "f" else None,
                numeric.get(original.coordinate, calculated.value)
                if original.data_type == "f"
                and calculated.data_type == "n"
                and _is_numeric(calculated.value)
                else calculated.value
                if original.data_type == "f"
                else None,
            )
        )
    return tuple(cells)


@dataclass(frozen=True)
class Asset:
    asset_id: str
    property_name: str | None
    city: str | None
    state: str | None
    total_insurable_value_micro_usd: int | None
    construction_type: str | None


@dataclass(frozen=True)
class SourceRecord:
    row: int
    cells: tuple[SourceCell, ...]
    asset: Asset


@dataclass(frozen=True)
class NormalizedData:
    records: tuple[SourceRecord, ...]
    assets: dict[str, Asset]
    source_rows: dict[str, list[int]]
    diagnostics: tuple[Diagnostic, ...]


def money(value: object) -> tuple[int | None, str | None]:
    """Return micro-USD and quality code. Range/unsupported precision are fatal."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "missing_value"
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None, "invalid_value"
    text = str(value).strip()
    if isinstance(value, str) and ("," in text or "$" in text or "usd" in text.casefold()):
        # Only the new grouped/currency forms use this grammar. Preserve ordinary
        # Decimal strings (including exponents) on the established path. The guard
        # casefolds only for detection; validation checks the original text as ASCII.
        if not re.fullmatch(
            r"-?(?:\$|USD +)?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?",
            text,
            re.ASCII | re.IGNORECASE,
        ):
            return None, "invalid_value"
        text = re.sub(r"\$|USD +|,", "", text, flags=re.ASCII | re.IGNORECASE)
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None, "invalid_value"
    if not amount.is_finite():
        return None, "invalid_value"
    parts = amount.as_tuple()
    if len(parts.digits) > 1000 or abs(int(parts.exponent)) > 1000:
        raise IngestionError(FailureCategory.DATA, "TIV has unsupported decimal precision")
    if amount.adjusted() > 13:
        raise IngestionError(FailureCategory.DATA, "TIV exceeds signed 64-bit micro-USD range")
    with localcontext() as context:
        context.prec = 1100
        rounded = amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        scaled = int(rounded * MICRO_USD)
    if not INT64_MIN <= scaled <= INT64_MAX:
        raise IngestionError(FailureCategory.DATA, "Rounded TIV exceeds signed 64-bit range")
    return scaled, "precision_reduced" if rounded != amount else None


def normalize_record(
    row: int, cells: tuple[SourceCell, ...], manifest: SchemaManifest, diagnostics: list[Diagnostic]
) -> Asset:
    values: dict[SemanticField, str | int | None] = {}
    for cell in cells:
        if cell.data_type == "f" and (cell.cached_value is None or cell.cached_type == "e"):
            diagnostics.append(
                Diagnostic(
                    code="formula_cache_unusable",
                    message="Formula has no usable cached result; not recalculated",
                    worksheet=manifest.worksheet,
                    row=row,
                    column=cell.column,
                    severity="warning",
                )
            )
    for field, mapping in manifest.concepts.items():
        if mapping.column is None:
            values[field] = None
            continue
        cell = cells[mapping.column - 1]
        value = cell.effective_value
        missing = value is None or (isinstance(value, str) and not value.strip())
        code: str | None = "missing_value" if missing else None
        normalized: str | int | None = None
        if cell.effective_type == "e":
            code = "invalid_value"
        elif not missing:
            if field == SemanticField.TIV:
                try:
                    normalized, code = money(value)
                except IngestionError as exc:
                    raise IngestionError(
                        exc.category, f"{manifest.worksheet}!{cell.coordinate}: {exc}"
                    ) from exc
            elif field == SemanticField.STATE:
                normalized = STATES.get(value.strip().upper()) if isinstance(value, str) else None
                code = None if normalized is not None else "invalid_value"
            elif isinstance(value, str):
                normalized = value.strip() if field == SemanticField.ASSET_ID else value
            else:
                code = "invalid_value"
        if field == SemanticField.ASSET_ID and normalized is None:
            raise IngestionError(
                FailureCategory.DATA,
                f"{manifest.worksheet}!{cell.coordinate}: invalid asset identifier",
            )
        values[field] = normalized
        if code:
            diagnostics.append(
                Diagnostic(
                    code=code,
                    message=f"{field}: {code}",
                    worksheet=manifest.worksheet,
                    row=row,
                    column=cell.column,
                    field=field,
                    severity="info" if code == "precision_reduced" else "warning",
                )
            )
    # Every field's runtime type is established above; keep the public projection explicitly typed.
    return Asset(
        asset_id=str(values[SemanticField.ASSET_ID]),
        property_name=_text(values[SemanticField.PROPERTY_NAME]),
        city=_text(values[SemanticField.CITY]),
        state=_text(values[SemanticField.STATE]),
        total_insurable_value_micro_usd=_integer(values[SemanticField.TIV]),
        construction_type=_text(values[SemanticField.CONSTRUCTION]),
    )


def _text(value: str | int | None) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: str | int | None) -> int | None:
    return value if isinstance(value, int) else None


def equivalent(left: SourceCell, right: SourceCell) -> bool:
    # Unknown formula results cannot establish verified equality, even if both are missing.
    for cell in (left, right):
        if cell.data_type == "f" and (cell.cached_value is None or cell.cached_type == "e"):
            return False
    a, b = left.effective_value, right.effective_value
    if left.effective_type != right.effective_type:
        return False
    if (
        left.effective_type == "n"
        and isinstance(a, (int, float, Decimal))
        and isinstance(b, (int, float, Decimal))
    ):
        return Decimal(str(a)) == Decimal(str(b))
    return source_json(a) == source_json(b)


def normalize(
    formulas: Worksheet,
    cached: Worksheet,
    rows: tuple[int, ...],
    manifest: SchemaManifest,
    numeric: dict[str, Decimal],
) -> NormalizedData:
    # Revalidate mutable nested containers at the deterministic ingestion boundary.
    manifest = SchemaManifest.model_validate(manifest.model_dump())
    records: list[SourceRecord] = []
    assets: dict[str, Asset] = {}
    sources: dict[str, list[int]] = {}
    first_cells: dict[str, tuple[SourceCell, ...]] = {}
    diagnostics: list[Diagnostic] = []
    for row in rows:
        cells = read_cells(formulas, cached, row, manifest, numeric)
        asset = normalize_record(row, cells, manifest, diagnostics)
        if asset.asset_id in assets:
            previous = first_cells[asset.asset_id]
            conflicts = [
                a.coordinate.rstrip("0123456789")
                for a, b in zip(previous, cells, strict=True)
                if not equivalent(a, b)
            ]
            if conflicts:
                raise IngestionError(
                    FailureCategory.DUPLICATE,
                    f"{manifest.worksheet}: duplicate identity conflict between rows "
                    f"{sources[asset.asset_id][0]} and {row}; columns {', '.join(conflicts)} "
                    "(differing values or unverifiable formula caches)",
                )
        else:
            assets[asset.asset_id] = asset
            first_cells[asset.asset_id] = cells
            sources[asset.asset_id] = []
        sources[asset.asset_id].append(row)
        records.append(SourceRecord(row, cells, asset))
    for rows_for_asset in sources.values():
        if len(rows_for_asset) > 1:
            diagnostics.append(
                Diagnostic(
                    code="duplicate_consolidated",
                    message=f"Verified identical source rows {rows_for_asset}",
                    worksheet=manifest.worksheet,
                    row=rows_for_asset[0],
                )
            )
    # Bounding positive and negative subtotals also bounds any filtered subset and SUM order.
    amounts = [
        a.total_insurable_value_micro_usd
        for a in assets.values()
        if a.total_insurable_value_micro_usd is not None
    ]
    if sum(x for x in amounts if x > 0) > INT64_MAX or sum(x for x in amounts if x < 0) < INT64_MIN:
        raise IngestionError(
            FailureCategory.DATA, "Logical-asset SUM may overflow signed 64-bit micro-USD"
        )
    return NormalizedData(tuple(records), assets, sources, tuple(diagnostics))
