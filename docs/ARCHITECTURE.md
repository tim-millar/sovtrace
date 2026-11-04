# SOVTrace architecture

The supported domain is a bounded insurance Schedule of Values table, with six
semantic concepts: asset identifier, property name, city, state, total insurable
value and construction type. The application retains its physical source records
and consolidates only verified duplicate logical assets. It rejects unsupported
structure, conflicting identities and unavailable interpretations explicitly.

## Ingestion

`ingestion` reads an immutable workbook byte snapshot in formula and cached-value
views. Physical discovery determines one supported table and validates boundaries,
metadata, merged cells and summary rows. Exact heading mappings take precedence;
optional inference proposes semantic mappings from bounded column metadata. The
proposal is validated locally against positions, type compatibility and required
concepts before normalization or persistence.

Numeric XML source values are retained and interpreted using Decimal arithmetic.
Normalized TIV is signed int64 micro-USD, with explicit precision-reduction
qualifications. SQLite stores logical assets, physical source rows, cell values,
column mappings, identity, import assumptions and completeness metadata. Database
publication is atomic; replacement requires explicit authorization through the
application flag. No source workbook is needed after a successful import to
retrieve its retained evidence.

## Workbook admission budgets

`ingestion.resources` defines fixed application limits, rather than user settings:

| Resource | Maximum |
| --- | ---: |
| Compressed input bytes | 16 MiB (16,777,216 bytes) |
| ZIP members | 128 |
| Aggregate expanded ZIP bytes | 64 MiB (67,108,864 bytes) |
| Expanded bytes per ZIP member | 16 MiB (16,777,216 bytes) |
| Worksheets | 8 |
| Worksheet row index | 10,000 |
| Worksheet column index | 256 (IV) |
| Aggregate coordinate-grid positions | 250,000 |
| Aggregate explicitly represented worksheet cells | 100,000 |

Admission checks file type and size before reading, rechecks the opened file and
caps the actual read at the byte ceiling plus one rejection byte. One immutable
snapshot supplies the hash, formula view, cached-value view and exact numeric
source extraction. Neither openpyxl load begins before the complete preflight.

The general workbook ceiling differs from the **128-column model-inference
metadata ceiling** (`MAX_INFERENCE_COLUMNS`). Complete deterministic mappings can
import wider tables, including 144-column tables, in either inference mode without
constructing a provider. If an actual inference request is needed, a wider request
fails before provider construction or transmission; columns are never truncated
or omitted to fit the request limit. The existing 256-character-per-heading and
32,768-total-heading-character request bounds also remain in force.

ZIP central records are counted before the member list is materialized. Declared
sizes are checked, then actual decompression is counted in bounded output chunks
independently of ZIP's declared output lengths. Malformed, conflicting/duplicate
member names, encrypted/unsupported members, corrupt payloads and XML entity/DTD
declarations are rejected. Supported archives use single-disk ZIP with stored or deflated members. ZIP64
metadata does not exempt an otherwise valid archive from any size or count ceiling.

Worksheet XML inspection streams and clears elements. It checks declared dimensions,
actual row/cell coordinates (including implicit coordinates), merge extents and
column spans before eager parsing or rectangular iteration. Every worksheet part,
including hidden, unselected and distractor sheets, contributes to the budgets.
Each grid area uses maximum row index times maximum column index, measured from
position 1, and the areas are summed across sheets. The explicit-cell ceiling
counts XML cell records; it does not count placeholders openpyxl later creates
inside an admitted grid. Misleading dimensions cannot hide sparse far-away cells.

`numeric_values` independently enforces the same admission policy for direct callers
and streams numeric XML values without building a complete worksheet tree.
Discovery and inference also check loaded worksheet extents before iteration.
Admission failures use workbook/structure errors before inference or database
publication; `--replace` cannot destroy an existing database on admission failure.
These ceilings bound supported inputs and parsing work. They do not provide
comprehensive protection against every resource-exhaustion attack.

## Question interpretation and execution

`qa.service` discovers the schema briefly, closes that connection, and asks the
provider for one structured query proposal. `qa.validation` enforces a bounded
SELECT grammar over `assets`, supported aggregate/list projections, literal
predicates and permitted ordering. Unsupported statements and malformed output fail
closed. `qa.execution` opens SQLite read-only and validates dataset metadata.

`qa.evidence` selects contributors, validates original-cell references, reconstructs
normalization and checks answer arithmetic independently in the same read snapshot.
Complete evidence, resource limits and consistent metadata are required for a
verified answer. Missing predicate or valuation information cannot be substituted
with fabricated values. Provider interpretation quality remains a separate concern
from deterministic execution and evidence reconciliation.

## Presentation

`sovtrace.cli` exposes `ingest` and `ask`. Text rendering summarizes validated
objects and makes control characters visible; JSON retains structured reports and
source evidence. Monetary display uses Decimal half-up cent rounding, while stored
and structured monetary values retain exact integer micro-USD semantics.

`scripts/demo.py` runs the committed artificial workbook through ingestion and eight
predefined SQL questions. Its renderer consumes completed evaluated records without
performing I/O or rerunning evaluation. Human text omits raw SQL and source-cell
arrays; JSON lines preserve those intended fields.

## Synthetic evaluation

`evaluation.corpus` declares independent artificial populations, layout variants,
questions and expected evidence. `evaluation.offline` compares real application
results with those oracles, preserving substantive failures, expected refusals,
ingestion rejections and infrastructure failures separately. Reports use
`synthetic-report-v3` and `synthetic-corpus-oracle-v1`.

SQL comparison validates restricted shapes and projections before normalizing
parentheses and associative/commutative AND terms. OR structure and boolean
precedence remain significant. A count reference may describe list membership,
but actual list projections must match the validated projection contract.

Optional injected-provider orchestration retains a hard 30-operation budget and
requested/observed model telemetry. Cost estimation has no default provider rates.
An explicitly injected schedule must identify its independently verified source,
date, model compatibility and service tier. Missing rates, usage or cache-write
accounting make estimates unavailable; reasoning is already included in output
tokens. Ordinary input, cached input, cache writes and output are charged separately.

## Data exposure

Schema inference transmits original headings verbatim, physical column metadata
and local type-count summaries. CLI ingestion enables it by default for unresolved
required or optional concepts; the Python ingestion API defaults off.
`--no-infer-schema` prevents inference requests. SQL generation separately sends
the user's verbatim question and relevant semantic schema.

These paths do not separately serialize source workbook rows, full datasets,
individual source-cell records or returned evidence merely as those structures.
Headings and questions can nevertheless contain sensitive names, financial amounts,
paths, records or formulas. They are not generically redacted, so the structured
payload boundary is not a promise that sensitive data stays local. `store=False`
is set by both adapters, but does not establish zero retention or absence of
provider logging. Account policy, authentication/SDK metadata, proxies and
instrumentation require their own handling assessment.

## Reproducibility

[The README](../README.md) lists local commands. Python 3.12 and the locked dependency
artifacts define the baseline. Offline CI checks lint, formatting, types, tests, CLI,
both demonstration formats and the synthetic evaluator without provider credentials
or application network requests. Generated reports stay outside tracked source.
