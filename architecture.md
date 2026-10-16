# agent-bi Architecture

> **Working instructions for implementers.** This document is written so that an
> implementing model does not have to make design decisions. Where a detail depends on an
> external spec (Snowflake semantic view YAML, Rill project YAML), Sprint 0 pins the exact
> external text into `docs/pinned/` and the pinned text wins over any paraphrase here.
> If you find a contradiction between this document and a pinned spec, stop and record it
> in `evidence/<sprint>/CONTRADICTION.md`; do not resolve it by guessing.

---

## 0. The Nondeterminism Boundary (read first)

agent-bi has exactly **two** probabilistic inputs. Everything else is deterministic,
replayable and hashable.

```
 probabilistic intent / reasoning   →   validated intermediate representation (IR)   →   deterministic compiler / runtime
 ─────────────────────────────────      ──────────────────────────────────────────       ───────────────────────────────
 harness agent reads a question         SemanticQuery JSON   (§4.3)                      compile → SQL → engine → rows
 harness agent proposes semantics       EnrichmentPatch JSON (§4.5)                      enrich → YAML + sidecar
                                        both: pydantic-validated, size-capped,           scan, compile-rill, chart, save,
                                        hashed, stored in git                            verify, parity, diff, manifest
```

Rules that follow from the boundary:

1. **Reasoning variance is allowed left of the IR.** Two runs may compose two different
   SemanticQueries for the same question. That is acceptable, visible (the interpretation
   line and the trust label), and decays as verified queries accumulate.
2. **Nothing right of the IR may vary.** Given identical IR, model, sidecar, config and
   data bytes, every *artifact* is byte-identical and every *receipt* differs only in the
   execution facts ADR-007 enumerates. A test asserts this for every deterministic component.
3. **The IR is the only thing the producer writes.** The producer (the operator's harness
   agent acting at ask/enrich time, §5.11) never writes SQL, never writes Rill YAML, never
   writes a number. Neither IR type carries executable text: a SemanticQuery names
   model objects; an EnrichmentPatch's only expression-bearing op is `add_metric`, whose
   `expr` must parse under the closed metric grammar (§5.3.2). If a producer-authored
   artifact other than the two IR types appears in the repo, that is a defect.
4. **Every IR instance is hashed and replayable.** `query_hash` and `patch_id` identify
   them; the provenance record ties a number to the IR that produced it **and** to the
   semantic definitions it depended on (`semantic_digest`, §4.7).
5. **Ambiguity is resolved left of the boundary, by a human.** When a question maps to
   more than one metric or dimension by name or synonym, the skill (§5.11) presents the
   choices and stops. The CLI never guesses: a SemanticQuery is explicit by construction.
6. **Executable text has exactly four sources, none of them the producer.** (a) The
   compiler, from IR. (b) The scanner, which emits dimension `expr` in exactly two fixed
   forms (§5.3.5). (c) The operator — a human — who may author named-filter expressions
   within the closed filter grammar (§5.3.5), view SQL under the §5.13 rules, and the
   independent known-answer SQL that `save --verify` requires. (d) Rill's Metrics SQL
   engine, which executes measure expressions that `compile-rill` copied verbatim from the
   semantic model, selected **by name** in Metrics SQL text that the compiler generated
   from IR (§5.3.4). Operator-authored SQL is executed read-only (§5.6).
7. **Development-time agents are not producers.** Sprint implementers and reviewers are
   models that write code and tests under the sprint protocol; that is ordinary
   supervised software development, outside this boundary. The boundary governs what the
   *shipped system* accepts from an LLM at runtime, not who typed the compiler.
8. **Publication is not governance.** Placing a chart on a dashboard says nothing about
   whether its definitions were accepted. Trust labels are computed from semantic
   acceptance and verification (§4.7); `published` is reported separately.

## 1. System Overview

```
                 ┌──────────────────────────── agent-bi (Python package) ────────────────────────────┐
                 │                                                                                    │
 DuckDB ──┐      │  catalog.py ──► semantic/scan.py ──► semantic/<rel>.yaml  + <rel>.provenance.yaml  │
 (SQLite  │      │                                          │                                         │
 attached)│      │                       semantic/validate.py (spec subset + sidecar consistency)     │
 Snowflake┘      │                                          │                                         │
                 │            ┌─────────────────────────────┼───────────────────────────┐            │
                 │            ▼                             ▼                           ▼            │
                 │   targets/rill.py              query/compile.py              targets/snowflake.py  │
                 │   rill/ project files          SemanticQuery → SQL            Semantic View YAML   │
                 │   (models, metrics view,       (duckdb | snowflake dialect)   + DDL loader         │
                 │    explore, canvas)                       │                                         │
                 │                                           ▼                                         │
                 │                         engines.py (execute) ──► verify/known_answers.py            │
                 │                                           │       verify/parity.py                  │
                 │                                           ▼                                         │
                 │                         explore/ask.py ──► answer = {interpretation, sql, trust,    │
                 │                                             provenance, rows, vega-lite chart}      │
                 │                         explore/save.py ──► queries/<name>.json + rill canvas comp. │
                 └────────────────────────────────────────────────────────────────────────────────────┘
                                   ▲
                    harness skill (Claude Code / Codex / OMP / Pi) produces
                    SemanticQuery JSON and enrichment patches — the only LLM steps
```

**Rill** (external binary, Apache-2.0) serves the `rill/` directory: `rill start rill/`.
agent-bi never calls Rill's internal APIs; it only writes files Rill reads.

## 2. Technology Stack

| Layer | Choice | Rule |
|---|---|---|
| Language | Python 3.12 | `requires-python = ">=3.12"` |
| CLI | `argparse` (stdlib) | No Typer/Click. Subcommands listed in §6. |
| Schemas | `pydantic>=2` | Every file format in §4 has a pydantic model; `model_validate` is the only parser. |
| YAML | `pyyaml` (`safe_load`, `safe_dump` with `sort_keys=False`) | Preserve key order on write. |
| Local engine | `duckdb` (Python package) | SQLite files are attached with `ATTACH '<path>' AS <alias> (TYPE SQLITE)`. |
| Cloud engine | `snowflake-connector-python` | Optional extra `[snowflake]`; import lazily; absence must not break DuckDB paths. |
| BI runtime | Rill Developer, installed via `curl https://rill.sh \| sh` | Version pinned in `docs/pinned/rill-version.txt` by Sprint 0. |
| Charts | Vega-Lite JSON (explorer) and Rill canvas components (dashboard) | One deterministic builder: `explore/chart.py`. |
| Tests | `pytest` | Fixtures in `tests/fixtures/`. No network in tests except the Snowflake parity suite, which is skipped when `AGENT_BI_SNOWFLAKE_*` env is unset. |
| Lint | `ruff` (default rules) | `ruff check src tests` must pass. |
| Hashing | `hashlib.sha256`; format `sha256:<64 hex>` | Everywhere a digest appears. |

No other runtime dependencies without a recorded decision in `BACKLOG.md`.

## 3. Directory Structure

```
agent-bi/
├── AGENTS.md  CLAUDE.md  WHERE_AM_I.md  BACKLOG.md
├── product-definition.md  architecture.md  project-plan.md  sprint-plan.md
├── pyproject.toml
├── agent-bi.yaml                 # project config (§4.6)
├── docs/
│   └── pinned/                   # Sprint 0: verbatim external specs + versions
│       ├── snowflake-semantic-view-yaml-spec.md
│       ├── snowflake-semantic-view-query.md      # SEMANTIC_VIEW(...) query syntax (§5.5 route 3)
│       ├── rill-metrics-view-yaml.md
│       ├── rill-metrics-sql.md                   # Metrics SQL (the `rill` dialect, §5.3.4; pinned in Sprint 0 task 10)
│       ├── rill-custom-chart-v0.90.5.md          # custom_chart keys from tagged source (Sprint 0 remediation A2)
│       ├── rill-time-syntax.md                   # `inf` = All Time (Sprint 0 remediation A3)
│       ├── rill-canvas-yaml.md
│       ├── rill-explore-yaml.md
│       ├── rill-connectors-duckdb-sqlite-snowflake.md
│       └── rill-version.txt
├── semantic/                     # committed semantic models
│   ├── <model>.yaml              # spec-pure Snowflake semantic view YAML
│   └── <model>.provenance.yaml   # sidecar (§4.2)
├── models/                       # SQL views for M2 ("views combine tables")
│   └── <view>.sql
├── queries/                      # saved SemanticQuery JSON (§4.3)
│   └── <name>.json
├── rill/                         # generated Rill project (checked in)
│   ├── rill.yaml
│   ├── connectors/               # if the pinned Rill docs require connector files
│   ├── models/<model>.sql
│   ├── metrics/<model>.yaml
│   ├── dashboards/<model>_explore.yaml
│   ├── dashboards/<model>_default_canvas.yaml   # generated (ADR-009)
│   ├── dashboards/<name>.yaml                   # operator-owned canvases (fork + builder edits)
│   ├── components/<name>.yaml    # saved explorer charts (Vega-Lite components)
│   └── provenance/<model>.json   # one build manifest per model (§4.4, D395)
├── src/agent_bi/
│   ├── __init__.py  version.py  main.py  errors.py
│   ├── config.py                 # loads agent-bi.yaml
│   ├── engines.py                # Engine protocol + DuckDBEngine + SnowflakeEngine; session settings; apply_models (§5.13)
│   ├── catalog.py                # Relation/Column discovery per engine
│   ├── hashing.py                # sha256 helpers, canonical JSON, semantic_digest (§4.7), models_digest (§5.13)
│   ├── fs_txn.py                 # write transactions (§5.14)
│   ├── semantic/
│   │   ├── model.py              # pydantic: SemanticModel and children (§4.1)
│   │   ├── provenance.py         # pydantic: ProvenanceSidecar (§4.2)
│   │   ├── scan.py               # catalog → SemanticModel + sidecar
│   │   ├── validate.py           # subset + consistency checks
│   │   ├── enrich.py             # apply an EnrichmentPatch (§4.5)
│   │   └── diff.py               # drift report
│   ├── query/
│   │   ├── contract.py           # pydantic: SemanticQuery (§4.3)
│   │   ├── compile.py            # SemanticQuery → SQL per dialect (§5.3)
│   │   └── verified.py           # exact-match lookup against verified_queries
│   ├── targets/
│   │   ├── rill.py               # SemanticModel → rill/ files (§5.4)
│   │   └── snowflake.py          # SemanticModel → Semantic View YAML/DDL + loader (§5.5)
│   ├── verify/
│   │   ├── known_answers.py      # run verified_queries vs expected (§5.6)
│   │   └── parity.py             # duckdb vs snowflake (§5.7)
│   ├── provenance_manifest.py    # build manifest (§4.4)
│   └── explore/
│       ├── ask.py                # pipeline (§5.8)
│       ├── chart.py              # Vega-Lite builder (§5.9)
│       └── save.py               # promotion (§5.10)
├── skills/
│   └── ask/SKILL.md              # harness skill: question → SemanticQuery JSON (§5.11)
├── tests/
│   ├── fixtures/
│   │   ├── idle-warehouse-waste.sqlite       # copied from dataForge (never regenerated here)
│   │   ├── holiday-cookout-rush.sqlite       # copied from dataForge
│   │   └── known-answers/<model>/<query>.json
│   ├── test_scan.py  test_validate.py  test_compile.py  test_verified.py  test_hashing.py
│   ├── test_rill_target.py  test_snowflake_target.py  test_known_answers.py  test_fs_txn.py
│   ├── test_parity.py  test_ask.py  test_chart.py  test_save.py  test_enrich.py  test_diff.py  test_views.py
│   └── test_cli.py
└── evidence/
    └── sprint-<n>/               # exact files named in each sprint's DoD
```

## 4. File Formats (the contracts)

### 4.1 Semantic model YAML (`semantic/<model>.yaml`)

Format: **Snowflake semantic view YAML**, spec-pure. Sprint 0 pins the spec page
verbatim to `docs/pinned/snowflake-semantic-view-yaml-spec.md` and the pydantic model
in `semantic/model.py` is written **from the pinned text**, using exactly its key names.
The subset agent-bi v1 reads and writes:

| Spec object | v1 uses | Notes |
|---|---|---|
| top-level `name`, `description` | yes | `name` = model id, `^[a-z][a-z0-9_]*$` |
| `tables[]` | exactly one entry in v1 | More than one → `E_UNSUPPORTED_MULTI_TABLE` |
| `tables[].name`, `description` | yes | logical name = physical relation name lower-cased |
| `tables[].base_table.{database,schema,table}` | yes | physical identity; mapped per engine via `agent-bi.yaml` (§4.6) |
| `tables[].primary_key.columns` | yes when detectable | from catalog PK; else omitted |
| `tables[].base_table.definition` | **refused** | the spec allows a SQL `definition` instead of `database/schema/table`; v1 has no physical identity for it (ADR-011) → `E_UNSUPPORTED_BASE_TABLE_DEFINITION` |
| `tables[].dimensions[]` | yes | `name`, `expr` (**one of the two fixed forms in §5.3.5**; anything else → `E_UNSUPPORTED_DIMENSION_EXPR`), `data_type`, `description`, `synonyms`. **Not generated** (Sprint 0 reconciliation): `unique` — not a spec key; uniqueness lives in the sidecar entry (`unique: true\|false`, §4.2); `sample_values` — prose-only in the pinned spec, absent from its syntax block (preserved if present; B-014) |
| `tables[].time_dimensions[]` | yes | `name`, `expr` (same restriction), `data_type` (`DATE` or `TIMESTAMP`), `description`, `synonyms` |
| `tables[].facts[]` | yes | numeric row-level columns: `name`, `expr` (bare quoted column only), `data_type`, `description` |
| `tables[].metrics[]` | yes | `name`, `expr` (aggregate; grammar §5.3.2), `description`, `synonyms` |
| `tables[].filters[]` | read, not generated | operator-authored; `expr` must parse under the closed filter grammar (§5.3.5) or `validate` fails with `E_UNSUPPORTED_FILTER_EXPR`; compiler appends accepted `expr` to WHERE |
| `relationships[]` | **must be empty/absent in v1** | present → `E_UNSUPPORTED_RELATIONSHIPS` |
| `verified_queries[]` | yes | `name`, `question`, `sql`, `verified_at`, `verified_by`. **`verified_at` is an integer: seconds since the UNIX epoch** (pinned spec type `<int>`), never ISO text. **`sql` is the compiled Snowflake-dialect SQL without any as-of clause** (the YAML is a Snowflake artifact; §5.5 physical-identity policy makes it valid there verbatim). Pre-decided Sprint 7 fallback: if `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` or Cortex Analyst rejects the physical form (the pinned example selects from the semantic view with logical names), `sql` becomes the route-3 `SEMANTIC_VIEW(...)` text of §5.5 and route 4 executes that; recorded in Sprint 7 `summary.md`, no other change. The DuckDB form is compiled on demand from the sidecar's `semantic_query`. |
| `custom_instructions` | read, not generated | passed through |
| anything else in the spec | read and preserved verbatim; never generated | the model must round-trip unknown keys (`model_config = ConfigDict(extra="allow")`) |

**Scan column classification (deterministic, §5.1):**

| Catalog type (DuckDB / Snowflake) | Classified as |
|---|---|
| DATE, TIMESTAMP*, DATETIME | `time_dimensions` (`data_type` DATE if DATE else TIMESTAMP) |
| any integer/decimal/float type | per the precedence of §4.1.1 rule 3: `dimensions` (only) if it is a declared key, or if distinct count ≤ 20 **and** the ID-style suffix rule matches; otherwise `facts` |
| VARCHAR/TEXT/STRING, BOOLEAN | `dimensions` (`data_type` TEXT / BOOLEAN) |
| TEXT column whose **every** non-null value parses as ISO date `YYYY-MM-DD` (checked on the first 1,000 rows and on a full `TRY_CAST` count) | `time_dimensions` with `expr: CAST("col" AS DATE)` and `data_type: DATE` — this is required for SQLite fixtures, whose dates are TEXT |
| BLOB/binary/struct/list/map | ignored; listed in the sidecar under `ignored_columns` with a reason |

**Default metrics generated by scan** (names are deterministic; no LLM):
- `row_count`: `COUNT(*)`
- for each fact `f`: `total_<f>`: `SUM("<f>")`, `avg_<f>`: `AVG("<f>")`
- for each dimension `d` whose sidecar entry has `unique: false`: `distinct_<d>`: `COUNT(DISTINCT "<d>")`
- No ratio metrics are generated by scan; enrichment may add them.
- `data_type` values: scan writes `TEXT`, `BOOLEAN`, `NUMBER`, `DATE`, `TIMESTAMP` (all appear in the pinned spec's examples or are Snowflake types); the pydantic model types `data_type` as `str` and never enumerates allowed values, because the spec does not.

#### 4.1.1 Naming, namespace and classification precedence (frozen before Sprint 2; ratified by the XO, D391)

1. **Semantic names are normalized; physical names are never rewritten.** Every object
   derived from a column is named `snake_name(<physical column>)`, implemented exactly as
   `src/agent_bi/semantic/scan.py::snake_name` at Sprint 1 (`5af314f`): insert `_` between a
   lower-case letter or digit and a following upper-case letter; insert `_` between an
   upper-case letter and a following upper-then-lower pair; replace every run of
   characters outside `[A-Za-z0-9]` by `_`; strip leading/trailing `_`; lower-case; if the
   result is empty or does not start with a letter, prefix `c_`. Example: `skuId` → `sku_id`.
   The object's `expr` always keeps the exact quoted physical column (`"skuId"`). Generated
   metric names apply the §4.1 patterns to the semantic name (`total_<name>`).
2. **One global namespace per model.** Names are unique across `dimensions`,
   `time_dimensions`, `facts`, `metrics` and `filters` together (Rill and `semantic_digest`
   resolve by name alone). Any collision — two columns normalizing to one name, or a
   generated metric name equal to a column-derived name — is a deterministic refusal
   `E_NAME_COLLISION` whose message names **both** objects as `<kind> <name> (column
   "<physical>")` or `metric <name> (generated)`. Nothing is auto-suffixed or renamed.
   Checked by `scan` (create and refresh) and by `validate` (so hand edits and later
   enrichment are caught too). `verified_queries[].name` is a separate namespace. The names `time_bucket` and `base_table` are **reserved** (used by the compiler and by `definition_digests`); a column normalizing to either → `E_NAME_COLLISION` naming the reservation (pre-Sprint-4, D398).
3. **Classification precedence for a column, first rule that matches wins:**
   1. name is not a plain identifier, or the type is BLOB/binary/struct/list/map or not
      listed in §4.1 → ignored, recorded in `ignored_columns` with the reason;
   2. DATE/TIMESTAMP*/DATETIME, or TEXT whose every non-null value is an ISO date →
      time dimension;
   3. **declared key** — the catalog's primary key (v1 reads primary keys only; catalog
      UNIQUE constraints are B-049) → **dimension, always**, regardless of type and
      cardinality; never a fact. The 20-value threshold never suppresses a declared key;
   4. integer/decimal/float with distinct count ≤ 20 **and** the physical name or the
      semantic name ending in `_id`, `id`, `_code`, `_flag` or `_key` → dimension (only);
   5. other integer/decimal/float → fact;
   6. VARCHAR/TEXT/STRING/BOOLEAN → dimension.
   A column is therefore exactly one of {ignored, time dimension, dimension, fact} — the
   earlier "fact **and** dimension" reading is removed because it would put two objects
   with one name in the global namespace.
4. Sidecar `unique` (§4.2) is a separate, *observed* property — declared key **or**
   distinct count = row count — used only for `distinct_<d>` metric generation and
   §5.4.1 breakdown choice. Observed uniqueness never makes a column a dimension.

### 4.2 Provenance sidecar (`semantic/<model>.provenance.yaml`)

```yaml
schema_version: agent-bi-provenance/1.0
model: <model name>
scanned_at: <ISO-8601 UTC>
engine: duckdb | snowflake
engine_version: <string>
source:
  kind: sqlite | duckdb | snowflake
  path_or_account: <string>
  as_of_digest: sha256:<64hex>           # file digest at scan time (§4.4 as-of rule); for Snowflake, the scan's start timestamp
relation:
  physical: <database>.<schema>.<table>
  kind: table | view
  row_count: <int>
  columns_digest: sha256:<64hex>          # sha256 of canonical JSON of [(name, type)] in catalog order
entries:                                  # one per semantic object, keyed by YAML path
  "tables[0].dimensions[warehouse_name]":
    status: catalog | inferred | confirmed   # exactly these three; anything else → E_PROVENANCE_MISMATCH (pre-Sprint-4, D398)
    origin: scan | enrich:<patch-id> | human
    at: <ISO-8601 UTC>                    # set when the entry was created or last changed; unchanged on refresh
    by: <string>                          # "scan", model id, or person
    expr_sha256: sha256:<64hex>           # digest of the object's expr (metrics, dimensions, facts, filters); absent otherwise
    unique: false                         # dimensions and time dimensions only: catalog PK/unique, or distinct count = row count (not a spec key, so it lives here)
  "tables[0].metrics[total_credits_used]": {status: catalog, origin: scan, at: ..., by: scan, expr_sha256: ...}
verified_queries:                         # one per verified_queries[] entry in the YAML, keyed by name
  credits_by_warehouse:
    query_hash: sha256:<64hex>            # §4.3 canonical hash of semantic_query
    semantic_digest: sha256:<64hex>       # §4.7 — the definitions this query was verified against
    definition_digests:                   # required (validate: E_PROVENANCE_MISMATCH if absent); diagnosis only (names what changed in STALE); identity is semantic_digest
      base_table: sha256:<64hex>          # sha256 of the canonical JSON of tables[0].base_table as written
      total_credits_used: sha256:<64hex>  # one per referenced definition: that object's expr_sha256 at verification
      warehouse_name: sha256:<64hex>
    semantic_query: { ...SemanticQuery... }
    question_normalized: <string>         # §5.8 normalization of verified_queries[].question
    known_answer: tests/fixtures/known-answers/<model>/<name>.json
ignored_columns:
  - {name: <col>, type: <catalog type>, reason: <string>}
```

Rules: every object in the YAML has exactly one sidecar entry and every
`verified_queries[]` entry has exactly one `verified_queries` record; `validate` fails
with `E_PROVENANCE_MISMATCH` otherwise. Keys use the object's `name`, not its index,
inside brackets so that reordering does not break provenance. `expr_sha256` must equal
the digest of the YAML object's current `expr`; a mismatch is `E_PROVENANCE_MISMATCH`
naming the path (this is how a hand edit to a confirmed definition is caught).

### 4.3 SemanticQuery JSON (`queries/<name>.json`, and the explorer's input)

```json
{
  "schema_version": "semantic-query/1.0",
  "model": "idle_warehouse_waste",
  "table": "warehouse_metering_history",
  "metrics": ["total_credits_used"],
  "dimensions": ["warehouse_name"],
  "time": {"dimension": "usage_day", "grain": "day", "start": "2026-01-01", "end": "2026-01-31"},
  "filters": [{"dimension": "warehouse_name", "op": "in", "values": ["ANALYTICS_WH"]}],
  "named_filters": [],
  "order_by": [{"field": "total_credits_used", "direction": "desc"}],
  "limit": 1000
}
```

Rules enforced by `query/contract.py` + `validate_against_model()`:
- `metrics` ≥ 1 unless `dimensions` ≥ 1 and `metrics` = [] (a pure dimension listing is allowed, compiled as `SELECT DISTINCT`).
- every name must exist in the model's single table; unknown → `E_UNKNOWN_FIELD` with the offending name and the list of valid names.
- `time` is optional; if present `dimension` must be a time dimension; `grain` ∈ {day, week, month, quarter, year}; `start`/`end` are ISO dates, inclusive start, **exclusive end**.
- `filters[].op` ∈ {`=`, `!=`, `in`, `not_in`, `>`, `>=`, `<`, `<=`, `between`, `like`}; `values` is a list (length 1 except `in`/`not_in` any, `between` exactly 2).
- `order_by[].field` must be a selected metric, dimension, or `"time_bucket"`.
- `limit` default 1000, max 100000.
- **Duplicates are refused, not collapsed** (ADR-006; pre-Sprint-4, D398): a name repeated within `metrics`, `dimensions` or `named_filters` → `E_QUERY_INVALID` naming it. (Sprint 2's canonical form de-duplicated silently; no golden or verified query contains a duplicate, so no `query_hash` changes.)
- **Canonical form** (`canonical_json()`): JSON with sorted keys, `metrics`/`dimensions`/`named_filters` sorted, `filters` sorted by `(dimension, op, values)`, `order_by` kept in order, whitespace-free. `query_hash = sha256(canonical)`.
- **The compiler consumes the canonical form, never the input order.** `compile()` first
  calls `canonical_json()` and compiles from that; SELECT-list order, GROUP BY order and
  default ORDER BY (§5.3.1) are therefore functions of the hash, and two inputs with the
  same `query_hash` always produce the same SQL text (`test_compile.py::test_canonical_order`).

### 4.4 Answer envelope, provenance record, and build manifest

`ask` writes **one** file per answer, `out/<query_hash>.answer.json`, the **answer
envelope**. It is the complete input to `save`; nothing about the answer has to be
recovered from anywhere else.

```json
{
  "schema_version": "agent-bi-answer/1.0",
  "semantic_query": { ...the SemanticQuery exactly as validated (canonical form)... },
  "provenance": { ...provenance record below... },
  "chart": { ...Vega-Lite spec from §5.9, or null... },
  "columns": ["warehouse_name", "total_credits_used"],
  "rows": [["ANALYTICS_WH", 123.45]]
}
```

The provenance record has two parts. The **artifact** part is a pure function of the IR,
the model, the sidecar and the config and is byte-identical across runs; the **receipt**
part records facts about one execution.

```json
{
  "schema_version": "agent-bi-provenance-record/1.0",
  "artifact": {
    "query_hash": "sha256:...",
    "semantic_digest": "sha256:...",
    "model_file_sha256": "sha256:...",
    "sidecar_sha256": "sha256:...",
    "models_digest": "sha256:... | null",
    "definitions": {
      "total_credits_used": {"kind": "metric", "expr": "SUM(\"credits_used\")", "expr_sha256": "sha256:...", "status": "catalog"},
      "warehouse_name":     {"kind": "dimension", "expr": "\"warehouse_name\"", "expr_sha256": "sha256:...", "status": "confirmed"}
    },
    "compiled_sql": "SELECT ...",
    "compiled_sql_sha256": "sha256:...",
    "dialect": "duckdb"
  },
  "receipt": {
    "trust": "governed | verified | exploratory",
    "trust_reason": "<one line from the §4.7 table>",
    "published": false,
    "verified_against": "<verified query name> | null",
    "engine": "duckdb",
    "engine_version": "1.x.y",
    "engine_settings": {"TimeZone": "UTC", "threads": 1},
    "as_of": {"kind": "file-digest | snowflake-time-travel", "value": "sha256:... | 2026-10-14T20:00:00Z", "retention_note": "<string | null>"},
    "executed_sql": "SELECT ... AT(TIMESTAMP => ...)",
    "executed_sql_sha256": "sha256:...",
    "executed_at": "2026-10-14T20:00:00Z",
    "row_count": 21
  }
}
```

- `semantic_digest`, `definitions` and the trust computation are defined in §4.7.
- `sidecar_sha256` here uses the same definition as the build manifest (sidecar bytes with the top-level `scanned_at:` line removed; one function, `provenance_manifest.sidecar_digest`), so it is an artifact field (D396).
- `compiled_sql` never contains an as-of clause; `executed_sql` is `compiled_sql` with the
  engine's as-of clause applied (§5.3.4). Only `compiled_sql_sha256` is used for identity.
- `models_digest`: sha256 of the concatenated canonical text of `models/*.sql` in sorted
  filename order (§5.13); `null` when the model's relation is not a view.
- **`as_of` rule.** For a SQLite or DuckDB **file**: the engine opens the file
  **read-only** (`ATTACH ... (TYPE SQLITE, READ_ONLY)` / `duckdb.connect(path, read_only=True)`),
  hashes the file bytes **before** execution and **after** execution, and refuses with
  `E_SNAPSHOT_CHANGED` if they differ; `as_of.value` is that digest. For Snowflake:
  `as_of.value` is the ISO timestamp of the query's start, the executed SQL carries
  `AT(TIMESTAMP => '<value>'::TIMESTAMP_LTZ)`, and `retention_note` states
  `"replayable only within the account's Time Travel retention (1 day default, up to 90 days)"`.
  A Snowflake as-of is a **replay reference with an expiry**, not a content digest; the
  record says so and never calls it a digest.

Build manifest: **one file per model, `rill/provenance/<model>.json`, overwritten by every
`compile-rill` of that model** (Sprint 3 ruling, D395 — a file per build would accumulate in
the checked-in `rill/` tree and make its file set depend on history). Shape, as implemented
in Sprint 3 and ratified: `{"schema_version": "agent-bi-build-manifest/1.0", "build_id":
"<UTC YYYYMMDDTHHMMSSZ>", "model", "model_file_sha256", "sidecar_sha256", "models_digest",
"rill_schema_pin", "files": [{"path", "sha256"}, ...]}` — keys in exactly this order, `files` in the §5.14 publish order (pre-Sprint-4, D398).
`sidecar_sha256` is the sha256 of the sidecar file's bytes **with its top-level
`scanned_at:` line removed** (D395 — otherwise every run would change the manifest and it
could never be an artifact). The manifest is an artifact except for `build_id`.
Rill's own UI does not show per-number provenance; the manifest is the dashboard-level
provenance and the explorer gives per-number provenance. State this limitation in
`WHERE_AM_I.md`; do not try to inject provenance into Rill's UI.

### 4.5 Enrichment patch (`EnrichmentPatch`, produced by the LLM via the skill, applied by `enrich`)

```json
{
  "schema_version": "agent-bi-enrichment-patch/1.0",
  "patch_id": "<uuid4>",
  "model": "idle_warehouse_waste",
  "proposed_by": "<model id string>",
  "proposed_at": "<ISO>",
  "operations": [
    {"op": "set_description", "path": "tables[0].dimensions[warehouse_name]", "value": "Virtual warehouse name"},
    {"op": "add_synonyms", "path": "tables[0].metrics[total_credits_used]", "value": ["credits", "spend"]},
    {"op": "add_metric", "path": "tables[0]", "value": {"name": "compute_share", "expr": "SUM(\"credits_used_compute\") / NULLIF(SUM(\"credits_used\"), 0)", "description": "..."}},
    {"op": "propose_query", "path": "", "value": {"name": "...", "question": "...", "semantic_query": { ...SemanticQuery... }}}
  ]
}
```

Only these four ops exist in v1. Rules:

- `enrich --apply` validates the **whole** patch first, then applies all operations in
  one write transaction (§5.14); any failure leaves every file unchanged. Each touched
  object is marked `inferred` with `origin: enrich:<patch-id>`.
- `add_metric` exprs must pass the metric grammar (§5.3.2) or the whole patch is rejected
  (`E_PATCH_REJECTED`, listing the failing op index). There is no op that adds a
  dimension, a fact, a filter or a relationship in v1.
- `add_synonyms`: the pinned spec requires synonyms to be "unique across all synonyms in
  this semantic model"; a synonym already used by any object (or equal to any object's
  `name`) rejects the whole patch with `E_SYNONYM_NOT_UNIQUE` naming both paths.
- **`propose_query` carries no SQL.** It writes `queries/<name>.json` with
  `"status": "proposed"` and the normalized question; it does **not** touch
  `verified_queries[]`, the sidecar's `verified_queries` map, or `tests/fixtures/known-answers/`.
  Promotion to verified happens only through `save --verify --expected-sql` run by the
  operator (§5.10): the independent SQL is always human-supplied and never travels inside
  a patch. (The former `add_verified_query` op, which carried `expected_sql`, is removed:
  a producer may not author both the query and its oracle.)
- **Confirmed entries are protected.** An op whose `path` resolves to a sidecar entry with
  `status: confirmed` is rejected with `E_PATCH_TOUCHES_CONFIRMED` naming the path, unless
  the operator passes `--allow-confirmed <path>` for that exact path, in which case the
  entry is rewritten and reverts to `inferred` (and any verified query depending on it
  becomes stale under §4.7, which is the intended consequence).
- `enrich --accept <path> --by <who>` flips one entry to `confirmed`. For a metric, it is
  refused with `E_ACCEPT_UNCOVERED` unless at least one current (non-stale) verified query
  references that metric: a definition is accepted only when a known answer exists for it.
- Patches never delete.

### 4.6 Project config (`agent-bi.yaml`)

```yaml
schema_version: agent-bi-config/1.0
project: agent-bi
engines:
  duckdb:
    database: ":memory:"                 # or a .duckdb path
    attach:
      - {path: tests/fixtures/idle-warehouse-waste.sqlite, alias: fixture, type: sqlite}
    relation_map:                        # semantic base_table (DATABASE.SCHEMA) → duckdb catalog.schema — DuckDB only
      "FIXTURE.MAIN": "fixture.main"
    session:                             # fixed; the engine applies these on connect and records them in every receipt
      TimeZone: UTC
      threads: 1
      preserve_insertion_order: true
  snowflake:                             # all values may be ${ENV_VAR}; absent → engine unavailable
    account: ${AGENT_BI_SNOWFLAKE_ACCOUNT}
    user: ${AGENT_BI_SNOWFLAKE_USER}
    password: ${AGENT_BI_SNOWFLAKE_PASSWORD}
    role: ${AGENT_BI_SNOWFLAKE_ROLE}
    warehouse: ${AGENT_BI_SNOWFLAKE_WAREHOUSE}
    semantic_view_schema: ${AGENT_BI_SNOWFLAKE_SEMANTIC_SCHEMA}   # DATABASE.SCHEMA where Semantic Views are created; base tables live at their YAML identity (§5.5)
    session:                             # fixed; applied with ALTER SESSION on connect, recorded in every receipt
      WEEK_START: 1
      TIMEZONE: UTC
rill:
  project_dir: rill
  olap: duckdb
defaults:
  limit: 1000
```

**Physical-identity policy (ADR-011).** `base_table` in the semantic YAML is the
relation's physical identity on Snowflake, written in Snowflake form (upper-case
`database`, `schema`; `table` in catalog spelling), e.g. `FIXTURE.MAIN.warehouse_metering_history`.
On Snowflake it is used **verbatim** — there is no Snowflake `relation_map`; the fixture
loader (§5.5) creates the relation at exactly that identity. On DuckDB, `relation_map`
resolves `DATABASE.SCHEMA` to an attached catalog and schema. Identifiers are always
double-quoted in generated SQL on every engine, using the catalog's exact spelling.
`session` blocks are not user-tunable in v1: they are the execution settings the
determinism contract (ADR-007) depends on, and every receipt records them.

### 4.7 Semantic digest, trust labels, and invalidation

**Semantic digest.** For a SemanticQuery `q` against model `m`, `semantic_digest(q, m)` is
the sha256 of the canonical JSON of:

```
{ "base_table": <tables[0].base_table as written>,
  "definitions": { <name>: {"kind": metric|dimension|time_dimension|filter, "expr": <expr as written>} 
                   for every metric, dimension, time dimension and named filter q references, sorted by name } }
```

Descriptions, synonyms, sample values and sidecar statuses are **not** in the digest:
changing them does not change what a query computes. Changing any referenced `expr` or
the `base_table` does. The function lives in `hashing.py` and is the only implementation.

**What is bound to what.**

| Record | Carries | Checked by |
|---|---|---|
| sidecar `verified_queries[name]` | `query_hash`, `semantic_digest`, `definition_digests` at verification time | `verify`, `ask` |
| known-answer file | `query_hash`, `semantic_digest`, `definition_digests` (copied at verification) | `verify` |
| `rill/components/<name>.yaml` annotation | `agent_bi_query_hash`, `agent_bi_semantic_digest`, `agent_bi_sql_sha256` (of the component's own `metrics_sql` text as written by `save`), as the first three line comments of the file (Sprint 0 Q6 = NO: Rill rejects unknown top-level keys) | `ask`, `validate` |
| provenance record `artifact` | `query_hash`, `semantic_digest`, `definitions` with statuses | reviewers, parity |

**Trust label** (computed in `ask`, step 3 of §5.8; exactly this table, first matching
row wins):

| Condition | `trust` | `trust_reason` |
|---|---|---|
| a sidecar `verified_queries` record has this `query_hash` **and** its `semantic_digest` equals the current digest **and** every referenced metric and dimension has sidecar status `confirmed` | `governed` | `verified as <name>; all definitions confirmed` |
| a sidecar record has this `query_hash` and a matching current `semantic_digest` | `verified` | `verified as <name>; unconfirmed definitions: <list>` |
| a sidecar record has this `query_hash` but a **different** `semantic_digest` | `exploratory` | `verification of <name> is stale: definitions changed since <verified_at> (<changed names>)` |
| otherwise | `exploratory` | `no verified query matches` |

`published` (receipt, boolean, independent of `trust`): true iff some
`rill/components/*.yaml` carries this `query_hash`, its `agent_bi_semantic_digest` equals
the current digest, its `agent_bi_sql_sha256` equals the sha256 of its current
`metrics_sql` text, **and** that component is referenced from an operator-owned canvas under
`rill/dashboards/` **whose current `defaults.time_range` is `inf` and which declares no default or required filters** (re-checked at every `ask`, not only at save time; pre-Sprint-4, D398). A component whose `metrics_sql` text no longer matches its annotation is an
operator-edited chart: it is reported by `validate` as `W_COMPONENT_DIVERGED <path>`
(warning, exit 0) and never counts as published for this query.

**Invalidation.** `verify` reports a verified query whose stored `semantic_digest` differs
from the current one as `STALE <name>: <changed definitions>` and exits 1, exactly like a
mismatch. `<changed definitions>` is computed by comparing the stored `definition_digests`
with the current `expr_sha256` of each referenced object (and the current `base_table`
digest), in canonical name order, `base_table` first; a referenced object that no longer
exists is listed as `<name> (removed)`. Trust and staleness are **decided** by
`semantic_digest` alone; `definition_digests` only explains them (Sprint 2 review A2, D392). The only way to clear it is the operator re-running the independent route
(`save --verify --expected-sql ... --force` on the same name), which rewrites the
known-answer file and the sidecar record with the new digest. Nothing re-verifies
automatically. `enrich --accept` refuses to confirm a metric with no current verified
query (`E_ACCEPT_UNCOVERED`, §4.5), so `governed` can only be reached through a known
answer that exists for the definitions as they stand.

## 5. Component Details

### 5.1 `catalog.py` — discovery

`Relation(database, schema, name, kind: "table"|"view", columns: list[Column], row_count)`;
`Column(name, catalog_type, nullable, is_pk, ordinal)`. Implementations:
- `DuckDBCatalog.describe(alias, schema, name)`: `information_schema.columns` filtered by `table_catalog = alias`; PK from `duckdb_constraints()` where `constraint_type = 'PRIMARY KEY'`; for attached SQLite, PK falls back to `PRAGMA table_info` via `sqlite_scan`-independent query `SELECT * FROM pragma_table_info('<name>')` executed **on the SQLite file with the stdlib `sqlite3` module** (DuckDB's SQLite attachment does not surface PKs reliably). Row count via `SELECT COUNT(*)`.
- `SnowflakeCatalog.describe(database, schema, name)`: `INFORMATION_SCHEMA.COLUMNS`; PK via `SHOW PRIMARY KEYS IN TABLE`.
- Distinct counts and ISO-date detection for classification are computed with `SELECT COUNT(DISTINCT "c")` and `SELECT COUNT(*) FILTER (WHERE TRY_CAST("c" AS DATE) IS NULL AND "c" IS NOT NULL)` (DuckDB) / `TRY_TO_DATE` (Snowflake).

### 5.2 `semantic/scan.py`

`scan(engine, relation_ref, model_name) -> (SemanticModel, ProvenanceSidecar)`. Applies
§4.1 classification and default metrics. Output ordering: columns in catalog ordinal
order; metrics in the order generated. Running scan twice on the same data must produce
byte-identical YAML and sidecar (`test_scan.py::test_idempotent`, no masking needed —
see refresh rule 3). `scan` never reads or writes the LLM.

**Create versus refresh (there is no `--force`).**

1. `scan` without `--refresh` is **create-only**: if `semantic/<model>.yaml` exists it
   exits 2 with `E_MODEL_EXISTS` and the hint `use --refresh`. Nothing is written.
2. `scan --refresh` re-runs discovery and classification and **merges**:
   - objects with status `catalog` are updated, added or removed to match the catalog;
   - objects with status `inferred` or `confirmed`, every `verified_queries[]` entry,
     `filters[]`, `custom_instructions`, and every preserved unknown key are carried over
     **verbatim**;
   - **Invariant (ratified D391): refresh may not preserve accepted or operator-owned
     metadata whose required physical dependency no longer exists.** Refresh refuses with
     `E_REFRESH_BREAKS_ACCEPTED`, listing every broken path, and writes nothing, when a
     column removed from the catalog is referenced by: the `expr` of an `inferred` or
     `confirmed` object; a `filters[]` entry; a verified query; or `tables[0].primary_key.columns`
     (whether scan-generated or operator-written — once carried over, it is preserved
     metadata). Any metadata type added later carries the same obligation: it either
     lists its physical dependencies for this check or is owned by refresh. The operator
     resolves it (edit the view, edit the key, or `enrich --allow-confirmed`) and re-runs.
3. **Timestamp invariant (ratified D391).** On create, every entry's `at` equals that
   run's `scanned_at`. On refresh, an entry's `at` changes only when that entry's content
   changes; an unchanged object keeps its `at`. `scanned_at` is updated on every run.
   Consequence, and the only claim made: re-scanning **unchanged** semantics produces a
   byte-identical YAML and a sidecar identical except `scanned_at`. Two independent first
   scans run at different times produce different `at`/`scanned_at` values; that is
   expected and outside every guarantee.
   Consequently a refresh on unchanged data is a no-op diff, and `diff` (§5.12) is the
   read-only preview of what a refresh would change.
4. `init` (§6) calls `scan` create-only when the model is absent and `scan --refresh`
   when it is present, then `validate`, then `compile-rill`.

### 5.3 `query/compile.py`

#### 5.3.1 SQL shape (both dialects)

```sql
SELECT
  "<dim1>" AS "<dim1>", ...,                                   -- dimensions in canonical (sorted) order
  DATE_TRUNC('<grain>', <time_expr>) AS "time_bucket",         -- only if time present
  <metric_expr_1> AS "<metric1>", ...                          -- metrics in canonical (sorted) order
FROM <relation>                                                -- resolved per dialect (§5.3.4); no as-of clause here
WHERE 1=1
  AND <time_expr> >= DATE '<start>' AND <time_expr> < DATE '<end>'   -- only if time.start/end present
  AND "<dim>" IN ('v1','v2') ...                               -- filters in canonical order, §5.3.3
  AND (<named_filter_expr>) ...                                -- named filters in canonical order, §5.3.5
GROUP BY "<dim1>", ..., "time_bucket"                          -- only if any metric
ORDER BY <user order_by as given>, <canonical tie-break>       -- see ordering rule
LIMIT <limit>
```

- **Compilation is from the canonical form (§4.3).** Every list in the SQL above is in
  canonical order. Input order never reaches the SQL text.
- **Ordering rule (total order, always).** The ORDER BY is the user's `order_by` as given
  (may be empty) followed by the **canonical tie-break**: `"time_bucket" ASC` if present,
  then every metric in canonical order `DESC`, then every dimension in canonical order
  `ASC`, with duplicates of fields already named by the user removed. With this rule a
  result set is a total order over its selected columns, so `LIMIT` selects the same rows
  on every engine and every run. Default (no `order_by`) is therefore
  `time_bucket ASC, metrics DESC (canonical), dimensions ASC (canonical)`.
- Dimension `expr` from the model is emitted exactly as §5.3.5 permits; bare columns are
  emitted double-quoted.
- `week` grain: `DATE_TRUNC('week', ...)` is Monday-based on DuckDB (ISO) and on Snowflake
  **only when `WEEK_START = 1`**, which the engine sets per session (§4.6). The receipt's
  `engine_settings` proves it.
- Pure dimension listing (no metrics): `SELECT DISTINCT ... ORDER BY <dims ASC canonical> LIMIT ...`, no GROUP BY.
- Numbers are never formatted in SQL. Formatting is a presentation concern (`explore/chart.py`).
- **Byte identity applies to SQL text and artifacts, not to floating-point row values.**
  Row values are compared under the §5.6 tolerance; the engine settings in §4.6 remove
  the known sources of run-to-run variance (thread count, insertion order, time zone),
  and the total order removes row-selection variance.

#### 5.3.2 Metric expression grammar (the only accepted forms)

```
metric_expr := agg | agg "/" "NULLIF(" agg ", 0)"
agg         := ("SUM" | "AVG" | "MIN" | "MAX") "(" column ")"
             | "COUNT" "(" "*" ")"
             | "COUNT" "(" "DISTINCT" column ")"
column      := '"' identifier '"'
```

Parsed with a regular expression in `compile.py` (`METRIC_RE`), case-insensitive on
keywords, exact on identifiers. Anything else → `E_UNSUPPORTED_METRIC_EXPR` naming the
metric. A metric's `column` must be a fact or dimension column of the table.

#### 5.3.3 Literals

Strings single-quoted with `'` doubled **and, in the `snowflake` and `rill` dialects, `\` doubled
as well** (both engines treat backslash as an escape inside a quoted string, so `x\' OR 1=1 --`
would otherwise close the literal; DuckDB does not, so the `duckdb` dialect doubles `'`
only — Sprint 2 review A1, D392); dates as `DATE 'YYYY-MM-DD'`; numbers verbatim
after `float()`/`int()` validation; booleans `TRUE`/`FALSE`; `like` passes the pattern
verbatim as a string literal. Nothing is ever string-interpolated from a user without
passing through these literal functions (`test_compile.py::test_injection_quoting`,
which covers all three dialects and, for `rill`, is also executed against Rill in Sprint 3's
`verify --engine rill` evidence).

#### 5.3.4 Dialect differences table (complete list; add a row only with a test)

Three dialects: `duckdb` (agent-bi's own engine), `snowflake`, and `rill`.

**Invariant (ratified 2026-10-14, D389):** Agent BI MUST NOT translate or regenerate accepted metric formulas when producing Rill chart queries. Rill Metrics SQL may reference accepted metrics and dimensions by identity; the authoritative metric expression is emitted unchanged into Rill's semantic definition.

**The `rill` dialect is Rill Metrics SQL** (`docs/pinned/rill-metrics-sql.md`; Sprint 0
reconciliation, replacing the earlier plain-`sql:` design, which Rill v0.90.5 does not
support — `evidence/sprint-0/CONTRADICTION.md` C-1). It is a **name projection**, not an
expression translation: `compile-rill` writes every metric's `expr` verbatim as the
measure `expression` and every dimension as a metrics-view dimension of the same name
(§5.4), so the `rill` compiler only ever emits names, never expressions:

```sql
SELECT <dim1>, ..., date_trunc('<GRAIN>', <time_dim>) AS time_bucket, <metric1>, ...   -- names only, canonical order; no aggregates
FROM <metrics_view>                                                                    -- the metrics view name = model name
WHERE <time_dim> >= '<start>' AND <time_dim> < '<end>' AND <dim> IN ('v1','v2') ...   -- §5.3.3 literals; dimension names only
ORDER BY <user order_by>, <canonical tie-break>                                        -- same total-order rule as §5.3.1
LIMIT <limit>
```

Grain is upper-case (`DAY`, `WEEK`, `MONTH`, `QUARTER`, `YEAR`); identifiers are bare
names (the metrics view guarantees `^[a-z][a-z0-9_]*$` names via the semantic model).
**Refused for the `rill` dialect** with `E_UNSUPPORTED_BY_RILL_ROUTE` (the explorer still
answers the query on DuckDB/Snowflake; only promotion to a Rill component is refused):
`like` filters (Metrics SQL `LIKE` is case-insensitive per the pinned page, agent-bi's is
case-sensitive), `named_filters` (they are written over physical columns, not dimension
names), and pure dimension listings with no metric (no known-answer route in v1). B-015.

| Concern | DuckDB | Snowflake | Rill (Metrics SQL) |
|---|---|---|---|
| Relation reference | `"<catalog>"."<schema>"."<table>"` from `relation_map` | `"<DATABASE>"."<SCHEMA>"."<table>"` verbatim from `base_table` (ADR-011) | `<model>` — the metrics view name |
| As-of clause (**executed SQL only**, never in `compiled_sql`) | none (file digest is the as-of) | `AT(TIMESTAMP => '<ts>'::TIMESTAMP_LTZ)` appended after the relation reference | none (Rill reads the same fixture file; its digest is the as-of) |
| TEXT→DATE in `expr` | `CAST("c" AS DATE)` | `TRY_TO_DATE("c")` — the compiler rewrites the model's `CAST(... AS DATE)` pattern for Snowflake | done once in the Rill model SQL (§5.4); Metrics SQL names the dimension |
| Time bucketing | `DATE_TRUNC('month', x)` | same; `week` requires session `WEEK_START = 1` (§4.6) | `date_trunc('MONTH', <time_dim>)`; `week` Monday-based via metrics view `first_day_of_week: 1` (§5.4) |
| Metric expressions | emitted from the model | emitted from the model | **never emitted**; selected by measure name |
| Integer division | `/` is float division in DuckDB ≥1.0 | `/` is decimal division | as DuckDB (Rill's embedded DuckDB) | all compared to the cent (§5.6/§5.7) |

#### 5.3.5 Closed grammars for dimension and filter expressions

Dimension and time-dimension `expr` (the only forms `scan` emits and `validate` accepts):

```
dim_expr := column | "CAST(" column " AS DATE)"
column   := '"' identifier '"'
```

Anything else → `E_UNSUPPORTED_DIMENSION_EXPR` naming the path. Fact `expr` is `column`
only. Named filter `expr` (operator-authored in `tables[].filters[]`):

```
filter_expr := term | term " AND " filter_expr | term " OR " filter_expr | "(" filter_expr ")"
term        := column cmp literal | column " IN (" literal ("," literal)* ")" | column " IS NULL" | column " IS NOT NULL"
cmp         := "=" | "!=" | "<" | "<=" | ">" | ">="
literal     := string | number | "DATE '" YYYY-MM-DD "'" | "TRUE" | "FALSE"
```

No function calls, no subqueries, no identifiers other than `column`, so no
`CURRENT_DATE()`, `NOW()`, `RANDOM()` or any other value that varies with the moment of
execution can enter a filter. Anything else → `E_UNSUPPORTED_FILTER_EXPR` naming the
filter. Both grammars are regular expressions in `compile.py` (`DIM_RE`, `FILTER_RE`),
imported by `validate.py`, with tests for accepted and refused forms.

### 5.4 `targets/rill.py`

Writes the files under `rill/` listed in §3, from the SemanticModel. Exact YAML keys for
metrics views, explore dashboards and canvas components come from the pinned Rill docs;
`rill.py` contains a `RILL_SCHEMA_PIN = "<rill version>"` constant that must equal
`docs/pinned/rill-version.txt`. Mapping:

| Semantic object | Rill |
|---|---|
| `base_table` (relation kind `table`) | `rill/models/<model>.sql`: `SELECT` every catalog column in ordinal order, one per line, each as `"<col>"` except a time dimension whose `expr` is `CAST("<col>" AS DATE)`, emitted as `CAST("<col>" AS DATE) AS "<col>"`; `FROM sqlite_scan('<fixture path relative to the rill/ directory>', '<table>')` for SQLite sources (Sprint 0 Q1 = YES). Exact layout: `tests/golden/rill/idle_warehouse_waste/models/idle_warehouse_waste.sql`. |
| `base_table` (relation kind `view`, §5.13) | `rill/models/<model>.sql`: the view's SELECT body extracted from `models/<view>.sql`, with every relation reference rewritten through `relation_map` to the Sprint 0 Q1 source form. Rill materializes the view as its own model; agent-bi's in-process DuckDB view is never visible to Rill and is never expected to be. |
| time dimension (first one) | metrics view `timeseries` |
| week convention | metrics view `first_day_of_week: 1` (Monday; matches DuckDB and the Snowflake session) |
| dimensions | metrics view `dimensions[]` (`name`, `column` — the model already applied any cast) |
| metrics | metrics view `measures[]` (`name`, `expression` = the metric `expr` verbatim) |
| default explore | metrics view `explore: {skip: true}` so Rill does not emit a second auto-named explore; the explore is `dashboards/<model>_explore.yaml` with `defaults: {time_range: inf}` |
| whole model | `dashboards/<model>_explore.yaml` referencing the metrics view |
| Rill project file | `rill/rill.yaml`: `compiler: rillv1`, `display_name: <agent-bi.yaml project>`, `olap_connector: duckdb` — **project-level, identical whichever model is compiled** (pre-Sprint-3 correction, D394: the Sprint 0 golden named it after one model, which would make the file depend on compile order once a second model exists) |
| default canvas (§5.4.1) | `dashboards/<model>_default_canvas.yaml` (generated; operators fork it, ADR-009) |
| saved explorer answer (§5.10) | `rill/components/<name>.yaml`: `type: component`, `display_name`, and a **`custom_chart`** renderer with `metrics_view: <model>`, `metrics_sql: [<rill-dialect compilation of the saved SemanticQuery>]` (a one-element list), and `vega_spec: '<the envelope's Vega-Lite spec as compact JSON, with "data": {"name": "query1"}>'`. Keys exactly as the v0.90.5 `CustomChart` interface defines them — `metrics_view?`, `metrics_sql: string[]`, `vega_spec: string` (`docs/pinned/rill-custom-chart-v0.90.5.md`; the pinned canvas reference lists `custom_chart` without properties). Rill executes the Metrics SQL; the numbers it shows are computed from the same measure expressions, and `verify --engine rill` (§5.6) proves they equal the known answers **at the canvas's all-time range with no canvas filters** (ADR-008 as amended: Rill applies canvas time range and filters on top). Shape proven in Sprint 0 (`evidence/sprint-0/q4-*`). |

#### 5.4.1 Default canvas composition rule (deterministic)

The canvas carries `defaults: {time_range: inf}`: Rill's default windows ("last 7 days",
"last 24 hours") are anchored to the data's latest day and silently drop rows, which
would make the generated dashboard disagree with the known answers (Sprint 0 finding:
Row Count 18 instead of 21). `inf` is Rill's "All Time" (`docs/pinned/rill-time-syntax.md`).
The canvas declares no default filters, so its default view is exactly the scope
`verify --engine rill` proves (ADR-008 as amended).

1. KPI components: the first 4 metrics in model order, each a `kpi_grid` with one measure.
2. Trend: a `line_chart` of the first metric over the first time dimension — **omitted if the model has no time dimension**.
3. Breakdowns: a `bar_chart` of the first metric by each of the first 2 dimensions whose sidecar entry has `unique: false` (fewer if fewer exist).
4. Table: a `table` component over the metrics view with `columns` = every non-time dimension in model order followed by every metric in model order — an aggregate table, one row per dimension combination (Rill canvas tables read metrics views only; a raw-row table is not available, Sprint 0 C-2). **Omitted if the model has no non-time dimension.**
"The first metric" is literal (for scanned models it is `row_count`). Components carry **no title keys**:
Rill renders each component's heading from the measure and dimension names (pre-Sprint-3
supervisor correction, D394 — the Sprint 0 golden files, which Sprint 3 reproduces byte for
byte, have none). No other components. Layout uses the pinned `rows[]`
form: row 1 = the KPIs, `width: 3` each, `height: 128px`; row 2 = the trend, `width: 12`,
`height: 320px`; row 3 = the breakdowns, `width: 6` each, `height: 320px`; row 4 = the
table, `width: 12`, `height: 400px`. Rows that would be empty are omitted. Exact layout:
`tests/golden/rill/idle_warehouse_waste/dashboards/idle_warehouse_waste_default_canvas.yaml`.

### 5.5 `targets/snowflake.py`

- `emit_semantic_view_yaml(model) -> str`: the semantic YAML itself (it is already spec-pure); this function only validates and returns the file text **unchanged** so there is exactly one writer. No identity rewriting happens here or anywhere: ADR-011 makes the file valid on Snowflake as committed.
- `load(engine, yaml_text)`: executes `CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML('<semantic_view_schema>', $$<yaml>$$)` — exact procedure signature from the pinned Snowflake doc; the target schema is `engines.snowflake.semantic_view_schema` (§4.6). Returns the created view name. Precondition checked first: the `base_table` relation exists at its YAML identity (`SELECT 1 FROM "<DATABASE>"."<SCHEMA>"."<table>" LIMIT 1`); absent → `E_BASE_TABLE_MISSING` naming the identity and the loader script that creates it.
- `scripts/load_fixture_to_snowflake.py` creates the fixture relations at **exactly the YAML identity**: `CREATE DATABASE IF NOT EXISTS "<DATABASE>"`, `CREATE SCHEMA IF NOT EXISTS "<DATABASE>"."<SCHEMA>"`, then the tables. For a view model (§5.13) it creates the view from `models/<view>.sql` with relation references rewritten to their YAML identities (the DuckDB `relation_map` inverted — a pure text substitution over the quoted three-part names the §5.13 form requires).
- `query_semantic_view_sql(semantic_query) -> str`: the `SELECT * FROM SEMANTIC_VIEW(<name> DIMENSIONS ... METRICS ...)` form from `docs/pinned/snowflake-semantic-view-query.md`, with every dimension and metric qualified by its logical table name (`<tables[0].name>.<name>`, as the pinned examples do); filters go in the inner `WHERE` the pinned page shows (no aggregates there). Named filters are always `UNSUPPORTED_BY_ROUTE` on route 3: the pinned spec says the semantic SQL compiler ignores standalone `filters` and uses entity-level `labels` instead. (pinned in Sprint 0 task 4 from Snowflake's "Querying a semantic view" page). It supports the **semantic-view-queryable subset** of SemanticQuery: metrics, dimensions, equality/IN filters on dimensions, time grain via a dimension expression only if the pinned page shows one; `time.start/end`, `between`, `like`, and named filters are compiled as an outer `WHERE` over the `SEMANTIC_VIEW(...)` result only where the pinned page shows an outer WHERE is permitted, otherwise the query is reported `UNSUPPORTED_BY_ROUTE` (parity skips it for route 3 **and says so per query**; it is not silently dropped). This route is **mandatory for M3**: if the pinned page lacks the `SEMANTIC_VIEW(` syntax with both `DIMENSIONS` and `METRICS` clauses, Sprint 7 is `BLOCKED` with a `CONTRADICTION.md`, not quietly narrowed.

### 5.6 `verify/known_answers.py`

For each `verified_queries[]` entry `q` in the model there is a file
`tests/fixtures/known-answers/<model>/<q.name>.json`:

```json
{"name": "...", "query_hash": "sha256:...", "semantic_digest": "sha256:...", "definition_digests": {"base_table": "sha256:...", "<name>": "sha256:..."}, "semantic_query": {...}, "expected": {"columns": ["warehouse_name","total_credits_used"], "rows": [["ANALYTICS_WH", 123.45], ...]}, "expected_computed_by": "direct-sql", "expected_sql": "SELECT ...", "expected_sql_by": "<person>", "expected_at": "<ISO>", "fixture_sha256": "sha256:..."}
```

`expected` is produced **once** by `scripts/compute_known_answer.py` using the stdlib
`sqlite3` module and **human-written** SQL (an independent route from the compiler),
reviewed, and committed. The script opens the fixture **read-only**
(`sqlite3.connect("file:<path>?mode=ro", uri=True)`), executes exactly one statement,
and refuses (`E_NOT_READONLY`) any SQL whose first keyword is not `SELECT` or `WITH`;
it records `fixture_sha256` and `expected_sql_by`. `expected_sql` never comes from a
producer (§0 rule 6): the only callers are the operator (Sprint 0 task 6, `save --verify`)
and Sprint tasks executed by implementers under review.

`verify` compiles `semantic_query`, executes on the requested engine, and compares: same
column names in order; same row count; rows compared after sorting by all columns;
numeric cells compared with `abs(a-b) <= 0.005` (to the cent); strings exact; NULL only
equals NULL. Before comparing, it checks the file's `semantic_digest` against the current
one (§4.7) and reports `STALE` on mismatch. Any mismatch or stale entry → exit 1 and a
diff listing up to 20 rows. `verify --coverage` additionally prints every metric in the
model with the names of the current verified queries that reference it, and exits 1 if
any metric with sidecar status `confirmed` has none.

**`verify --engine rill`** proves the numbers Rill displays **in the verified scope: all-time range, no canvas filters** (ADR-008 as amended — Rill applies the canvas's time range and filters on top of every component, so other views are Rill's computation, not agent-bi's verified answer). Precondition: `rill start
rill --no-open` is running **for this project** (otherwise `E_ENGINE_UNAVAILABLE`, exit 3).
**Project binding (Sprint 3 review A1, D396):** `rill query --local` ignores `--path` and
queries whatever runtime listens on :9009, so before any query `verify` reads
`GET http://localhost:9009/local/config` (stdlib `urllib`; observed response pinned in
`docs/pinned/rill-cli-query.md`) and requires `project_path` == the resolved absolute path
of `<config dir>/<rill.project_dir>` **and** `version` == `RILL_SCHEMA_PIN`. Any mismatch,
missing key or unreachable endpoint → `E_ENGINE_UNAVAILABLE` naming the served path and
version; never a PASS. The readiness probe runs only after the binding check succeeds, and
**the binding is re-checked immediately before every query** (pre-Sprint-4, D398 — otherwise
the time-of-check/time-of-use window merely moves). For each verified
query not refused by the `rill` dialect, it runs
`rill query --local --path rill --format csv --resolver metrics_sql --properties sql=<rill-dialect SQL>`
(csv preserves column order; json does not), and compares to the known answer with the
same rules plus one normalization: a Rill timestamp at exactly `00:00:00Z` equals the
known answer's `YYYY-MM-DD` date. NULL handling: Rill's csv prints NULL as the unquoted
text `null`, which equals a known-answer NULL; a quoted `"null"` is the string (pre-Sprint-4,
D398). Refused queries print `UNSUPPORTED_BY_ROUTE <name>`.
Proven by hand in Sprint 0 task 10 (`evidence/sprint-0/rill-route-check.txt`).

### 5.7 `verify/parity.py`

Runs every known-answer query through four routes and compares every route's result
set to the known answer with the §5.6 comparison:

| Route | What executes | Proves |
|---|---|---|
| 1 | compiled `duckdb` SQL on DuckDB | the compiler on the local engine (same as `verify`) |
| 2 | compiled `snowflake` SQL on Snowflake | the compiler's Snowflake dialect |
| 3 | `SEMANTIC_VIEW(...)` query (§5.5) on the **loaded Semantic View** | the delivered artifact computes the same numbers; per-query `UNSUPPORTED_BY_ROUTE` is printed, never hidden |
| 4 | the YAML's own `verified_queries[].sql` text, verbatim, on Snowflake | the examples shipped inside the client's file actually run and agree |

Output is one line per query per route: `PASS`, `FAIL`, `STALE`, or `UNSUPPORTED_BY_ROUTE`.
Exit 0 only when routes 1, 2 and 4 are all `PASS` for every query and route 3 is `PASS`
for every query it supports **and** supports at least one query per model. Skips (exit 0
with `SKIPPED: snowflake not configured`) when the Snowflake engine is unavailable.
Requires the fixture relations to have been created at their YAML identities by
`scripts/load_fixture_to_snowflake.py` (Sprint 7).

### 5.8 `explore/ask.py` — pipeline

Input: exactly one of `--query-file <SemanticQuery JSON>` or `--question "<text>"`.

**`--question` is the deterministic, LLM-free path and is tried first by the skill (§5.11).**
It normalizes the text (`normalize_question()`: NFKC, lower-case, collapse whitespace,
strip surrounding whitespace and trailing `?.!`) and looks for an exact match of the
normalized string among the sidecar's `verified_queries[*].question_normalized` and the
`question_normalized` of every `queries/*.json`. Exactly one match → that record's
`semantic_query` becomes the input and steps 1–8 run. No match → exit 4 with
`E_NO_VERIFIED_MATCH` and nothing else; more than one match → exit 2 with
`E_AMBIGUOUS_QUESTION` listing the names. No fuzzy matching, no synonyms, no ranking.

Steps, in order, with no branching beyond what is written:
1. Load model + sidecar; `validate`. Failure → exit 2 with the validation error.
2. Parse SemanticQuery; canonicalize; `validate_against_model`. Failure → exit 2 with `E_UNKNOWN_FIELD` etc.
3. Compute `query_hash` and `semantic_digest`; compute `trust`, `trust_reason`, `verified_against` and `published` exactly per the §4.7 tables.
4. Compile for the engine (`--engine duckdb|snowflake`, default duckdb) from the canonical form; `compiled_sql` has no as-of clause.
5. Execute: apply session settings, take the as-of (file digest before; Snowflake start timestamp), run `executed_sql`, collect rows, row_count, engine version; file digest after (§4.4, `E_SNAPSHOT_CHANGED` on change).
6. Build the provenance record (§4.4): artifact part from steps 2–4, receipt part from steps 3 and 5.
7. Build the Vega-Lite spec (§5.9).
8. Write `out/<query_hash>.answer.json` (the envelope, §4.4) and `out/<query_hash>.vl.json` (the chart alone, convenience copy). Emit to stdout, in this order: a one-paragraph **interpretation** rendered from the canonical SemanticQuery by a fixed template (`"Metrics: … by … over … from … to …, filtered …"` — no LLM), the line `trust: <label> (<trust_reason>)`, the line `published: true|false`, the line `definitions: <name>=<status>, …` for every referenced definition, the compiled SQL in a fenced block, a plain-text table (first 50 rows), and the two paths written.
`--json` emits the envelope itself on stdout instead.

### 5.9 `explore/chart.py` — Vega-Lite builder (deterministic)

Mark selection by result shape (no other rules):

| Shape | Mark |
|---|---|
| time_bucket + 1 metric, 0 dims | `line` (x: time_bucket temporal, y: metric quantitative) |
| time_bucket + 1 metric, 1 dim | `line` with `color: dim` |
| 0 time, 1 dim, 1 metric | `bar` (x: dim nominal sorted by -y, y: metric) |
| 0 time, 1 dim, ≥2 metrics | `bar`, grouped via `column` facet per metric |
| 0 time, 0 dims, 1 metric | `text` single-value "KPI" (a 1-row table rendered as text mark) |
| anything else | no chart; `chart: null` and a note "table only" |

`--mark <line|bar|area|point|table>` overrides when compatible; incompatible → `E_CHART_INCOMPATIBLE`.
Theme: `--theme <name>` loads `themes/<name>.json` (a Vega config object) and sets it as `config`; default `themes/default.json`, whose exact content is `{}` (an empty Vega config — Rill and Vega defaults apply; pre-Sprint-4, D398). Number formatting: `.2f` for a metric whose name starts with `avg_` or whose `expr` contains `/` (a ratio), `,.0f` otherwise, set via `axis.format`.

### 5.10 `explore/save.py` — promotion

`agent-bi save --from out/<query_hash>.answer.json --name <name> [--question "<text>"]`
reads the **envelope** (§4.4) — the SemanticQuery, chart and provenance are all in it —
and runs as **one write transaction** (§5.14): every output below is computed and
validated in memory, every precondition (including the independent route of step 4) is
satisfied, and only then are files published, in the order listed. Any failure before
publication leaves the repository byte-identical.

1. `queries/<name>.json`: `{"schema_version": "agent-bi-saved-query/1.0", "name", "status": "saved" | "proposed" | "verified", "question", "question_normalized", "query_hash", "semantic_digest", "semantic_query" (canonical, pretty-printed, sorted keys)}`. A `proposed` file of the same name (from `propose_query`, §4.5) is upgraded in place; any other existing name is refused without `--force` (ADR-009).
2. `rill/components/<name>.yaml`: the `custom_chart` component of §5.4 (`metrics_sql` = the `rill`-dialect compilation of the SemanticQuery; `vega_spec` = the envelope's chart with `"data": {"name": "query1"}`), whose first three lines are the annotations `# agent_bi_query_hash: sha256:...`, `# agent_bi_semantic_digest: sha256:...`, `# agent_bi_sql_sha256: sha256:...` (sha256 of the exact `metrics_sql` string written). Comments, because Sprint 0 Q6 = NO (Rill rejects unknown top-level keys). A query refused by the `rill` dialect (§5.3.4) refuses the whole `save` with `E_UNSUPPORTED_BY_RILL_ROUTE`.
3. `--add-to-canvas <canvas-name>` appends the component to `rill/dashboards/<canvas-name>.yaml` (an **operator-owned** canvas, ADR-009; refused with `E_GENERATED_FILE` if the target is a generated `_default_canvas`). Refused with `E_CANVAS_NOT_VERIFIED_SCOPE` if the target canvas's `defaults.time_range` is not `inf` or it declares default filters (ADR-008 as amended; `canvas fork` copies the generated `defaults: {time_range: inf}`, so forked canvases qualify). Placement rule, no exceptions (pinned canvas uses `rows[].items[]`, no coordinates): append a new last row `{items: [{component: <name>, width: 12}], height: 400px}`. Without the flag, the operator places the component in Rill's canvas editor.
4. `--verify --by <who> --expected-sql "<SQL>"`: the operator supplies the independent SQL by hand. **Before any file is published**, `scripts/compute_known_answer.py` runs it read-only (§5.6) and the compiled query is executed; the two must agree under the §5.6 comparison or `save` refuses with `E_VERIFY_MISMATCH` and writes nothing. On agreement the transaction additionally publishes: the known-answer file (`query_hash`, `semantic_digest`, `expected_sql_by = --by`); a `verified_queries[]` entry in the YAML (`question`, `sql` = compiled **Snowflake**-dialect SQL without as-of per §4.1, `verified_at` = now, `verified_by` = `--by`); the sidecar `verified_queries[<name>]` record (§4.2); and `status: verified` in step 1's file. Without `--expected-sql`, `--verify` is refused with `E_NO_INDEPENDENT_SQL`. A verified query is never created from a single route, and never from SQL that arrived in a patch.

### 5.11 The LLM steps live in the harness (`skills/ask/SKILL.md`)

The skill instructs the operator's harness agent, in this order (rewritten for the
deterministic resolver, D402; the skill carries **no** resolution rules of its own):
1. Run `agent-bi ask --model <m> --question "<the user's question verbatim>"` **first**.
   Exit 0 → present the answer; no model composed anything. Exit 2 `E_AMBIGUOUS_QUESTION`
   → show the listed names and ask the user to pick one, then run `--query-file` with that
   saved query. Exit 4 `E_NO_VERIFIED_MATCH` → step 2.
2. Run `agent-bi resolve --model <m> --question "<verbatim>"` (§5.15).
   - Exit 0 `RESOLVED` → run `agent-bi ask --model <m> --query-file out/resolution.json`.
     No model composed the query.
   - Exit 5 `AMBIGUOUS` → show each ambiguity's candidates with their descriptions, ask the
     user to pick, re-run `resolve` with all of the user's picks so far plus the new one
     (`--choose <id>=<their pick>` each); repeat until it exits 0 or 6. Exit 2
     `E_BAD_CHOICE` → drop every pick for each id the error line names (an earlier
     date/grain pick can be invalidated by a later word pick); for `malformed`, drop that
     argument (r5, reviewer wording); then ask again. The call wrote no resolution. **Never pick for the user.**
   - Exit 6 `UNRESOLVED` → the question is outside the deterministic grammar. Only now may
     the agent compose a SemanticQuery proposal (the one probabilistic step) to
     `out/question.json`, using `out/resolution.json`'s `bindings` and `unresolved` as
     context, then run `agent-bi ask --model <m> --query-file out/question.json`. If `ask`
     returns `E_UNKNOWN_FIELD`, retry **once** with the corrected name from the error, then
     stop. Tell the user the query was composed by the agent; the interpretation line shows
     what was asked.
The skill must not write SQL or a number, must not skip steps 1–2, and must not contain its
own copy of the resolver's rules (a test checks that `SKILL.md` invokes `agent-bi resolve`
and does not carry the function-word list or the grammar). **Packaging:** the canonical skill text is
`skills/ask/SKILL.md` and `skills/enrich/SKILL.md`, each with YAML frontmatter
`name: agent-bi-ask` / `agent-bi-enrich` and a one-line `description`; for Claude Code,
`.claude/skills/agent-bi-ask` and `.claude/skills/agent-bi-enrich` are relative symlinks to
`../../skills/ask` and `../../skills/enrich` (other harnesses read `skills/` directly). For enrichment, `skills/enrich/SKILL.md` instructs the agent to
write an EnrichmentPatch JSON (§4.5: no SQL in any op) and run `agent-bi enrich --apply`.
An in-process LLM client is **not** v1 (BACKLOG).

## 6. CLI (`agent-bi`, `src/agent_bi/main.py`)

| Command | Args | Effect | Exit |
|---|---|---|---|
| `scan` | `--engine duckdb\|snowflake --relation <db.schema.name> --model <name> [--refresh]` | create-only, or merge-refresh per §5.2; writes `semantic/<name>.yaml` + sidecar | 0 / 2 validation, `E_MODEL_EXISTS`, `E_REFRESH_BREAKS_ACCEPTED` / 3 engine |
| `validate` | `--model <name>` | §4.1/§4.2/§4.7 checks incl. `expr_sha256`, grammars, component annotations (`W_COMPONENT_DIVERGED` is a warning) | 0 / 2 |
| `compile-rill` | `--model <name>` | writes `rill/` files + manifest | 0 / 2 |
| `compile-sql` | `--model <name> --query-file <f> --engine duckdb\|snowflake\|rill [--executed]` | prints `compiled_sql`; with `--executed`, the as-of form for the engine | 0 / 2 |
| `verify` | `--model <name> --engine duckdb\|snowflake\|rill [--coverage]` | §5.6, incl. `STALE` detection; `rill` needs a running `rill start` | 0 / 1 mismatch, stale, uncovered confirmed metric / 2 / 3 |
| `parity` | `--model <name>` | §5.7, four routes | 0 / 1 / 3 |
| `ask` | `--model <name> (--query-file <f> \| --question <text>) [--engine] [--mark] [--theme] [--json]` | §5.8 | 0 / 2 / 3 / 4 `E_NO_VERIFIED_MATCH` |
| `save` | `--from <answer.json> --name <n> [--question <q>] [--force] [--verify --by <who> --expected-sql <sql>] [--add-to-canvas <canvas-name>]` | §5.10, one write transaction | 0 / 2 (nothing written on any failure) |
| `canvas fork` | `--model <name> --name <canvas-name>` | copies the generated default canvas to an operator-owned canvas (ADR-009) | 0 / 2 |
| `enrich` | `--model <name> (--apply <patch.json> [--allow-confirmed <path>]... \| --accept <path> --by <who>)` | §4.5, one write transaction | 0 / 2 |
| `diff` | `--model <name> --engine …` | drift report (§5.12) = read-only preview of `scan --refresh` | 0 same / 1 drift / 3 |
| `contract` | `--print` | prints `docs/semantic-query-contract.md` content | 0 |
| `resolve` | `--model <name> --question <text> [--choose <id>=<value>]...` | §5.15 deterministic resolver; writes `out/resolution.json` | 0 RESOLVED / 2 `E_BAD_CHOICE` / 5 AMBIGUOUS / 6 UNRESOLVED |
| `init` | `--sqlite <file> --table <name> --model <name>` | **the M1 dashboard command**: attach, scan (create or refresh, §5.2 rule 4), validate, compile-rill, print `rill start rill` instruction | 0 / 2 / 3 |
| `snowflake-load` | `--model <name>` | §5.5 load (precondition: base table at its YAML identity) | 0 / 2 `E_BASE_TABLE_MISSING` / 3 |

Exit codes: 0 ok; 1 verification mismatch/drift/stale; 2 validation/contract error (user-fixable); 3 engine/connection error; 4 no verified match (`ask --question` only — a normal outcome the skill branches on); 5 ambiguous and 6 unresolved (`resolve` only — normal outcomes the skill branches on). Every error prints `E_<CODE>: <message>` on stderr as the last line. Codes are enumerated in `errors.py` and documented in `docs/errors.md` (generated by `python -m agent_bi.errors --doc`).

### 5.12 `semantic/diff.py`

Re-runs catalog discovery and compares against the committed YAML + sidecar
`columns_digest`: reports added columns, removed columns, type changes, and row-count
change, and — using the same merge logic as `scan --refresh` (§5.2) — lists which
semantic objects a refresh would add, update or remove and which accepted objects or
verified queries would be broken. Output is a Markdown table on stdout; exit 1 if any
structural change. `diff` writes nothing.

### 5.13 Views (`models/*.sql`) — operator-authored relations

A view is operator-authored SQL and is treated as **part of the data**, not as a query:
it is hashed into provenance and constrained for determinism, never compiled from IR.

1. **Form.** `models/<view>.sql` contains exactly one statement of the form
   `CREATE OR REPLACE VIEW <view> AS <select>` (case-insensitive prefix, matched by
   `VIEW_RE`). Every relation reference inside `<select>` is a quoted three-part DuckDB
   name resolvable through `relation_map` (e.g. `"fixture"."main"."warehouse_metering_history"`).
   Anything else → `E_UNSUPPORTED_VIEW_FORM` naming the file.
2. **Determinism.** `<select>` may not contain, as a whole word (regex `VIEW_DENY_RE`,
   case-insensitive): `CURRENT_DATE`, `CURRENT_TIME`, `CURRENT_TIMESTAMP`, `NOW`, `TODAY`,
   `RANDOM`, `RAND`, `UUID`, `GEN_RANDOM_UUID`, `GETDATE`, `SYSDATE`, `LOCALTIME`,
   `LOCALTIMESTAMP`, `SAMPLE`, `USING SAMPLE`. Violation → `E_NONDETERMINISTIC_VIEW`.
   This is a denylist, so it is checked on every engine start (`apply_models`) and in
   `validate`; the list grows only with a test.
3. **Digest.** `models_digest` (§4.4) = sha256 over the concatenation of each file's text
   (LF-normalized, trailing whitespace stripped) in sorted filename order. It is part of
   every provenance record and manifest for a view model and part of what the fresh-clone
   check compares.
4. **Where the view lives per engine.** agent-bi's DuckDB: `apply_models(dir)` executes
   every `models/*.sql` after attach, in sorted filename order, into the in-process
   catalog (`relation_map` entry `"VIEWS.MAIN": "memory.main"`). Rill: `targets/rill.py`
   emits the `<select>` body as `rill/models/<view>.sql` with relation references rewritten
   to the Q1 source form (§5.4) — Rill never sees agent-bi's connection. Snowflake: the
   loader creates the view at `VIEWS.MAIN.<view>` from the same `<select>` with references
   rewritten to YAML identities (§5.5). Three materializations, one source text, one digest.
5. **Scan.** `scan --relation VIEWS.MAIN.<view>` discovers the view through the DuckDB
   catalog like a table; the sidecar records `relation.kind: view` and `models_digest`.

### 5.14 Write transactions

Every command that writes more than one file (`save`, `enrich --apply`, `scan --refresh`,
`compile-rill`, `init`) follows one rule, implemented once in `fs_txn.py`:

1. Compute every output in memory; run every validation and every precondition
   (including external executions such as `save --verify`'s two routes).
2. Write each output to `<repo>/.agent-bi-tmp/<txn-id>/<relative path>`.
3. Publish by `os.replace` per file, in a fixed documented order (semantic YAML, sidecar,
   `queries/`, `tests/fixtures/known-answers/`, `rill/components/`, `rill/dashboards/`,
   `rill/provenance/`), after which the temp directory is removed.
4. If step 1 or 2 fails, nothing under the repo changed (`test_fs_txn.py` proves it by
   injecting a failure after the first in-memory output and comparing a tree hash).
   If step 3 fails midway (disk full, permission), the command exits 3 with
   `E_PARTIAL_PUBLISH` listing the files already replaced and the temp directory kept for
   recovery; `validate` then reports the inconsistency (`E_PROVENANCE_MISMATCH`), which
   is the recovery signal. There is no rollback journal in v1; the fixed order plus
   `validate` is the recovery contract.

### 5.15 `explore/resolve.py` — the deterministic resolver (`agent-bi resolve`)

**FROZEN 2026-10-15 (D414).** This section is the normative resolver contract, frozen by the
owner after five rule-review rounds (Sprint 4P). It changes only by an owner decision recorded in
the Chief of Staff's decisions ledger. During implementation and verification nobody "clarifies"
it: a discovered defect is a finding (`FAIL-OPEN` and return), never an edit to make a test pass.
Git tag `resolve-contract-frozen-20261015` marks the frozen text. Owner amendments since: D417
(tag `resolve-contract-amended-20261015`) and D421 (tag `resolve-contract-amended2-20261015`).

**Responsibility (XO-authorized, D402).** `resolve` deterministically maps the *resolvable*
structure of a natural-language question onto existing semantic objects, or refuses with the
candidates and reasons needed to continue. **It never guesses.** It owns a deliberately small,
closed grammar — normalized metric and dimension names and synonyms, grouping phrases, the
five time grains, eligible-date binding, and the ambiguity rules discovered in the Sprint 4
reviews — and nothing else. Anything outside the grammar is `UNRESOLVED` and handed to the
probabilistic producer (§5.11) with context. **Guardrail:** this is not a natural-language
parser. Adding a construct to the grammar requires an owner decision and new normative cases
in the table below; understanding that needs inference rather than grammar is out of scope by
definition. **Refusal is success** whenever more than one valid binding remains.

`resolve` reads only the semantic model YAML; it makes no model/LLM call and no network call,
writes nothing except `out/resolution.json`, and never changes a semantic definition.
**Resolution is not verification:** a `RESOLVED` question still gets whatever trust label
§4.7 gives its SemanticQuery (often `exploratory`); "we know exactly what was asked" and "we
have independent evidence for the answer" stay separate.

**Grammar (exact).**
1. **Words.** **Owner amendment D421 (XO wording):** Normalize the question using the same
   canonical Unicode normalization used by verified-question lookup. Tokenize supported ASCII
   alphanumeric runs normally. Every remaining non-whitespace run containing unsupported
   characters MUST be preserved as unmatched input. Unsupported characters are never silently
   discarded. **Exactly:** apply Unicode NFKC, then lower-case (Python `str.lower`), as
   `normalize_question()` does (§5.8). Read the result left to right as maximal runs of
   (a) characters in `[a-z0-9]` — a **word**; (b) whitespace (Python regex `\s`, the whitespace
   `normalize_question()` collapses) — a separator; (c) any other characters — a run made only of
   ASCII characters is dropped (ASCII punctuation separates words, as before); a run containing at
   least one non-ASCII character is an **unsupported word**, kept verbatim. An unsupported word
   takes its place in the word list (positions for rule 4, `w:<n>` ids and rule 9 order) but is
   never part of a phrase match (an object phrase containing an unsupported word never matches),
   never a function word, `or` or grain, never matched by rule 7, and never normalized further:
   it is always `UNRESOLVED` with reason `no matching name or synonym`, `word` = the unsupported
   word, one entry per occurrence. Normalize each (supported) word: remove one trailing `s` when the word is longer than 3 characters and
   does not end in `ss` (`credits`→`credit`, `warehouses`→`warehouse`; `as`, `class` unchanged).
   Object names (split on `_`) and synonyms are normalized the same way.
2. **Function words** (closed): `a an and are by for give how in is list many me much of per
   show the to what which with`. A function word standing on its own is never bound (rule 7);
   inside a rule-4 phrase it is part of that phrase ("cost of compute" binds as a whole).
   `by` and `per` open a grouping phrase.
3. **Eligible objects** are metrics, dimensions and time dimensions. Facts are never
   candidates (they are not queryable in a SemanticQuery, §4.3).
4. **Phrases first** (rewritten by Sprint 4R review ruling D403). Each object's phrase list
   is its name's words and each synonym's words. Phrases are matched against the question's
   full word sequence (function words included), **longest length first**; at each length,
   every match over words not yet consumed is found, and matches that overlap form a
   cluster. A cluster whose matches all belong to one object binds them. A cluster with
   more than one object **never binds by file order**: its whole span becomes an ambiguity
   with `id` = the span's normalized words joined by one space and reason `phrase shared by
   several objects` (all matches are the same phrase) or `overlapping phrases` (they differ);
   every word of the span, function words included, is consumed. Matches overlap if they
   share a word, directly or through other matches of the same length (clusters are
   transitive). **Pick (Sprint 4P; rejected function words as unresolved material ratified by the owner, D412):** `--choose <span id>=<object>` binds **the
   cluster's matches that belong to the chosen object** — never any other phrase of that
   object found inside the span — and rule 6 judges its role by the span's position. Every
   other word of the span, **function words included**, is **rejected material**: it stays
   consumed, never re-enters deterministic matching (rules 4, 5, 7, 8), neither opens nor
   closes a grouping phrase, and is `UNRESOLVED` with reason `rejected interpretation
   material`, one entry per rejected word occurrence, `word` = that normalized word.
5. **Grouping phrase.** The words after `by`/`per`, up to the end of the question or the next
   function word other than `and`. Grouping phrases are determined after rule 4: a word
   consumed by rule 4 — in a bound phrase or in an ambiguous span — is part of that phrase
   and, if it is a function word, neither opens nor closes a grouping phrase (D403).
6. **Roles.** A metric bound by a phrase inside a grouping phrase is `UNRESOLVED` with
   reason `metric in grouping position`: **one entry per bound match**, `word` = that
   match's normalized question words joined by one space (rule 7 never offers a metric
   there). Dimensions and time dimensions may
   be bound anywhere (outside a grouping phrase they form a dimension listing or name a date).
7. **Remaining words**, in order: a function word is skipped; `or` is `UNRESOLVED`
   (`disjunction`); a time-grain word (`day week month quarter year`) inside a grouping
   phrase is a grain, outside one it is `UNRESOLVED` (`time grain outside grouping phrase`);
   any other word is matched against the normalized name words and synonym words of the
   objects eligible for its position (inside a grouping phrase: dimensions and time
   dimensions; outside: metrics, dimensions and time dimensions). 0 candidates →
   `UNRESOLVED` (`no matching name or synonym`); 1 → bound; more than 1 → an ambiguity with
   `id` = the word and reason `metric and dimension` if the candidates include at least one
   metric and at least one dimension or time dimension, else `several objects`. **Ids
   (Sprint 4P draft r2).** Collect every **ambiguous** occurrence of a word `w`. If all of
   them have the identical candidate set and the same position class (inside or outside a
   grouping phrase), all of them get the id `w`; otherwise **all** of them get their own id
   `w:<n>` (`n` = the occurrence's 1-based position in the normalized word list) — all or
   nothing, never a mix. A pick binds exactly the ambiguous occurrences that carry its id;
   occurrences of `w` that were not ambiguous are unaffected. Rule-4 span ids follow the same
   all-or-nothing rule over every **ambiguous** occurrence of the same span text (same object
   set and same position class → span text; otherwise every ambiguous occurrence is
   `<span text>:<n>`, `n` = the position of the span's first word); text bound elsewhere by a
   longer phrase is not an ambiguous occurrence. A pick id therefore has exactly one meaning.
8. **Time.** Grains = the grain words found. **Owner amendment D417 (XO wording):** Multiple
   occurrences of the same resolved time-grain token represent one grain. Distinct resolved time
   grains remain conflicting/ambiguous according to the existing contract. Named dates = the time dimensions bound. More
   than one grain → ambiguity `id: @grain` (`several grains named`). More than one named date
   → ambiguity `id: @date` (`several dates named`). Grains but no named date: the model's sole time dimension if it
   has exactly one; several → ambiguity `id: @date` (`several eligible dates, none named`) —
   never chosen heuristically; none → `UNRESOLVED` (`model has no date`). A named date with no
   grain counted by rule 8 uses grain `day` (r5, reviewer wording).
9. **Status**, in precedence: any ambiguity → `AMBIGUOUS` (a human decides first); else any
   unresolved word → `UNRESOLVED`; else, if at least one metric or non-time dimension is bound
   → `RESOLVED`; else `UNRESOLVED` (`no metric or dimension`, `word` null). Metric,
   dimension and named-date lists are of distinct objects. `model has no date` carries
   `word` = the first grain word **that rule 8 counted** (a grain word inside a grouping
   phrase), in question order, before any pick. **Order of
   `unresolved`:** by the position of the entry's first question word; entries with
   `word: null` last; equal positions in rule order 4, 6, 7, 8.
10. **`--choose <id>=<value>`** (repeatable) records a human's pick (`<word or span id>=<object>`,
    `@grain=<grain>`, `@date=<time dimension>`; the `@` prefix keeps resolver ids apart from
    question words). Ids and values are compared as exact strings. Each argument is split at
    its **first** `=`: the id is everything before it, the value everything after (a value
    may itself contain `=`). **Malformed** = no `=`, an empty id, or an empty value. An id
    beginning with `@` is a stage-2 id (`@grain`, `@date`; any other `@` id is `unknown id`
    at stage 2). **Two stages (Sprint 4P; ratified by the owner, D412):**
    - **Stage 1 — word and span picks.** Valid only if the id is an ambiguity in the
      **zero-pick** resolution of the question and the value is one of that id's candidates
      there.
    - **Stage 2 — `@grain` and `@date` picks.** Valid only if the id is an ambiguity in the
      resolution of the question **with this call's stage-1 picks applied and no stage-2
      picks**, and the value is one of that id's candidates there. (Stage-1 picks may create,
      remove or change `@grain`/`@date` ambiguities — that is why stage 2 is judged after
      them; a pick never affects another id of its own stage.)
    - **Validation order and reason**, first failure wins and then nothing is applied:
      malformed argument (`malformed`, checked over all arguments first); one id given two
      different values (`conflicting values`; the same `id=value` repeated counts once);
      then the stage-1 arguments **in argument order**, each checked for `unknown id` before
      `not a shown candidate`; then the stage-2 arguments likewise. Every failure is
      `E_BAD_CHOICE` (exit 2), and its stderr line names exactly the failing argument(s):
      `E_BAD_CHOICE: <reason>: <id>=<value>` (for `conflicting values`, the conflicting id that appears first in argument order; its
      distinct values in order of first appearance, each as `<id>=<value>`, separated by `; ` —
      r5, reviewer wording; for `malformed`, the argument verbatim).
    - **Application**, all valid picks together: stage-1 picks bind their objects (rule 4
      pick semantics for spans); then rule 8 runs, with `@grain` replacing the grains by its value and `@date`
      replacing the named dates by its value; `bindings` and `grain` then follow from the
      settled result as the Output section defines.
    - An ambiguity that remains after application is reported and needs a further call. A
      later stage-1 pick can make an earlier stage-2 pick invalid (`unknown id`); that is the
      intended, safe outcome — the user is asked again. The agent never supplies a choice
      the user did not make.
11. **SemanticQuery** (only when `RESOLVED`; reviewer wording A5-2, adopted verbatim, D414): `semantic_query` has exactly the keys `schema_version`
(`"semantic-query/1.0"`), `model`, `table` (the model's name and its table's name), `metrics`,
`dimensions`, and `time` only when one is settled; `filters`, `named_filters`, `order_by` and
`limit` are absent. `metrics` and `dimensions` are sorted; `time` is `{dimension, grain}`.

**Output** (Sprint 4P draft r4). On exit 0, 5 or 6: JSON on stdout and the same bytes in
`out/resolution.json` (sorted keys, two-space indent, trailing newline): `{"schema_version":
"agent-bi-resolution/1.0", "model", "question", "words", "status", "semantic_query": {...} |
null, "query_hash": "sha256:..." | null, "bindings": [{"object", "kind"}], "grain": <grain> |
null, "ambiguities": [{"id", "reason", "candidates": [{"value", "kind", "description"}]}],
"unresolved": [{"word", "reason"}]}`. (Reviewer wording A5-1, adopted verbatim, D414:) A candidate's `kind` is the object's kind, or `"grain"` for an `@grain`
candidate. Its `description` is the object's YAML description, or `""` when the YAML has none and for
every `@grain` candidate. **`bindings` is derived only from the settled result
after all picks are applied:** one entry per **distinct** object: every metric with at least
one binding that rule 6 did not refuse (r5, reviewer wording), every non-time dimension that is
bound, and the time dimension **only if rule 8 settled exactly one**; sorted by `object`;
`kind` is `metric`, `dimension` or `time_dimension`. Rejected material, metrics whose every
binding rule 6 refused, dates removed by `@date`, and unsettled dates never appear.
**`grain`** is the settled grain (the single grain counted by rule 8, the `@grain` pick, or
`day` when rule 8 settled exactly one date and counted no grain word — r5, reviewer wording; "no
grain word" means with no grain counted by rule 8), or `null` if none is settled. When the status is `RESOLVED`,
`bindings` and `grain` equal the SemanticQuery's metrics, dimensions and `time`. **Owner amendment D417 (XO wording):** Any non-successful resolve invocation leaves no result
artifact from that invocation or any previous invocation at the configured result path. A
successful invocation is one that exits 0, 5 or 6 (it completed a resolution and writes it as
above); every other exit — `E_BAD_CHOICE`, a missing or invalid model or config, an invalid or
malformed argument to `resolve`, or any other failure — writes nothing to stdout and removes any
existing `out/resolution.json`. Same question + same model bytes →
byte-identical output. Fixed output details (ratified from the implementation, D402):
`candidates` sorted by `value`; `ambiguities` ordered phrase ties, then word ambiguities, both
in question order, then `@grain`, then `@date`, one entry per `id`; JSON `ensure_ascii`. Exit: 0 `RESOLVED`; 5 `AMBIGUOUS` (`E_RESOLVE_AMBIGUOUS`); 6
`UNRESOLVED` (`E_RESOLVE_UNRESOLVED`); for 5 and 6 the `E_` line is the last stderr line, as
for exit 4. `ask --query-file out/resolution.json` accepts the file (it carries
`semantic_query`) and refuses it when `semantic_query` is null.

**Normative cases** (the replay set from both failed Sprint 4 reviews; a test reads this table
from `architecture.md`, so it cannot drift). Model `iww` = `semantic/idle_warehouse_waste.yaml`;
`two` = the same model plus a second time dimension `billing_date`
(`tests/fixtures/semantic/two_dates.yaml`); `tie` = the same model with synonym `spend` on both
`total_credits_used` and `total_credits_used_compute` and synonym `cost of compute` on
`avg_credits_used_compute` (`tests/fixtures/semantic/tie.yaml`); `rv1` and `rv4` are the Sprint 4R reviewer's adversarial models
(`tests/fixtures/semantic/rv1.yaml`, `rv4.yaml`; only names, synonyms and kinds matter to the resolver):
`rv1` metrics `total_sales` (synonyms `revenue`, `sales by region`), `order_count` (`orders`),
`avg_order_value`, `cost_per_unit`, `region_total` (`region`); dimensions `order_status`, `region`,
`customer_region`, `region_manager`, `customer_name`, `order_month`, `date`, `status_class`; time
dimensions `order_date`, `ship_date`, `due_date`. `rv4` metrics `distinct_warehouse_name`,
`total_credits`; dimensions `warehouse_name`, `warehouse_size`; time dimension `usage_day`. `rv5`
(`tests/fixtures/semantic/rv5.yaml`): metric `total_credits`; dimensions `billing_account`,
`ship_mode`; time dimensions `usage_day`, `billing_date`, `ship_date`. `rv6`
(`tests/fixtures/semantic/rv6.yaml`): metrics `revenue`, `spend`; dimensions `store_region`,
`region_code` (synonym `region by`), `team_lead`, `lead_source` (synonym `lead in`); time dimension
`usage_day`. Rows 36–50 were derived by the Sprint 4P rule reviewer (r4/r5). Result = the SemanticQuery's
metrics / dimensions / time for `RESOLVED`, else the ambiguity ids or unresolved reason.

| # | model | question | status | result |
|---|---|---|---|---|
| 1 | iww | Credits by warehouse? | AMBIGUOUS | credit (6 metrics) |
| 2 | iww | avg credits used by warehouse name | RESOLVED | avg_credits_used / warehouse_name / — |
| 3 | iww | total credit used by warehouse name | RESOLVED | total_credits_used / warehouse_name / — |
| 4 | iww | how much compute by warehouse | AMBIGUOUS | compute (avg_credits_used_compute, total_credits_used_compute) |
| 5 | iww | number of warehouses per day | AMBIGUOUS | warehouse (metric and dimension) |
| 6 | iww | credits used by warehouse | AMBIGUOUS | credit, used |
| 7 | iww | row count by warehouse name | RESOLVED | row_count / warehouse_name / — |
| 8 | iww | total credits used by month | RESOLVED | total_credits_used / — / usage_day month |
| 9 | iww | total credits used by warehouse and month | RESOLVED | total_credits_used / warehouse_name / usage_day month |
| 10 | iww | usage day total credits used | RESOLVED | total_credits_used / — / usage_day day |
| 11 | iww | total credits used by usage day | RESOLVED | total_credits_used / — / usage_day day |
| 12 | iww | total credits used by warehouse and day | RESOLVED | total_credits_used / warehouse_name / usage_day day |
| 13 | iww | total credits used by warehouse for the month | UNRESOLVED | time grain outside grouping phrase |
| 14 | iww | total credits used by warehouse or day | UNRESOLVED | disjunction |
| 15 | iww | warehouse name by total credits used | UNRESOLVED | metric in grouping position |
| 16 | iww | row count by distinct warehouse name | UNRESOLVED | metric in grouping position |
| 17 | iww | total credits used by day and month | AMBIGUOUS | @grain |
| 18 | iww | show me the warehouses | AMBIGUOUS | warehouse (metric and dimension) |
| 19 | iww | warehouse names | RESOLVED | — / warehouse_name / — |
| 20 | iww | total credits used by warehouse for finance | UNRESOLVED | no matching name or synonym |
| 21 | two | total credits used by month | AMBIGUOUS | @date (several eligible dates, none named) |
| 22 | two | total credits used by billing date | RESOLVED | total_credits_used / — / billing_date day |
| 23 | two | total credits used by billing date and month | RESOLVED | total_credits_used / — / billing_date month |
| 24 | two | total credits used by billing date and usage day | AMBIGUOUS | @date (several dates named) |
| 25 | iww | how much compute by warehouse — with `--choose compute=total_credits_used_compute` | RESOLVED | total_credits_used_compute / warehouse_name / — |
| 26 | two | total credits used by month — with `--choose @date=billing_date` | RESOLVED | total_credits_used / — / billing_date month |
| 27 | tie | spend by warehouse name | AMBIGUOUS | spend (phrase shared by several objects) |
| 28 | tie | spend by warehouse name — with `--choose spend=total_credits_used` | RESOLVED | total_credits_used / warehouse_name / — |
| 29 | tie | cost of compute by warehouse name | RESOLVED | avg_credits_used_compute / warehouse_name / — |
| 30 | rv4 | by warehouse show warehouse total credits | AMBIGUOUS | warehouse:2, warehouse:4 |
| 31 | rv4 | by warehouse show warehouse total credits — with `--choose warehouse=distinct_warehouse_name` | E_BAD_CHOICE | unknown id |
| 32 | rv4 | by warehouse show warehouse total credits — with `--choose warehouse:2=distinct_warehouse_name` | E_BAD_CHOICE | not a shown candidate |
| 33 | rv4 | by warehouse show warehouse total credits — with `--choose warehouse:2=warehouse_size --choose warehouse:4=distinct_warehouse_name` | RESOLVED | distinct_warehouse_name, total_credits / warehouse_size / — |
| 34 | rv1 | total sales by customer region manager | AMBIGUOUS | customer region manager (overlapping phrases) |
| 35 | rv1 | total sales by customer region manager — with `--choose customer region manager=region_manager` | UNRESOLVED | rejected interpretation material |
| 36 | rv1 | region by region | AMBIGUOUS | region:1, region:3 |
| 37 | rv1 | region by region — with `--choose region:1=region_total --choose region:3=region` | RESOLVED | region_total / region / — |
| 38 | rv1 | orders by region — with `--choose region=region_total` | UNRESOLVED | metric in grouping position |
| 39 | rv4 | warehouse and warehouse by warehouse | AMBIGUOUS | warehouse:1, warehouse:3, warehouse:5 |
| 40 | rv1 | finance total sales by customer region manager — with `--choose customer region manager=region_manager` | UNRESOLVED | no matching name or synonym |
| 41 | rv1 | total sales by customer region manager and customer — with `--choose customer region manager=region_manager` | AMBIGUOUS | customer |
| 42 | rv6 | store region by revenue — with `--choose store region by=store_region` | UNRESOLVED | rejected interpretation material |
| 43 | rv5 | total credits by usage day and billing — with `--choose billing=billing_date --choose @date=billing_date` | RESOLVED | total_credits / — / billing_date day |
| 44 | rv5 | total credits by billing and month — with `--choose billing=billing_date --choose @date=billing_date` | E_BAD_CHOICE | unknown id |
| 45 | two | total credits used by billing date and usage day — with `--choose @date=billing_date` | RESOLVED | total_credits_used / — / billing_date day |
| 46 | iww | total credits used by day and month | AMBIGUOUS | @grain |
| 47 | iww | how much compute by warehouse — with `--choose compute=row_count --choose nosuch=row_count` | E_BAD_CHOICE | not a shown candidate |
| 48 | two | total credits used by month — with `--choose @date=billing_date --choose @date=usage_day` | E_BAD_CHOICE | conflicting values |
| 49 | iww | total credits used by total credits used | UNRESOLVED | metric in grouping position |
| 50 | two | total credits used by billing date and usage day | AMBIGUOUS | @date (several dates named) |
| 51 | iww | total credits used by month and month | RESOLVED | total_credits_used / — / usage_day month |
| 52 | nosuch | total credits used by warehouse name — after a successful call that wrote `out/resolution.json` | E_MODEL_NOT_FOUND | file removed |
| 53 | iww | finance total credits used by warehouse name finance | UNRESOLVED | no matching name or synonym |
| 54 | iww | row count by total credits used and total credits used | UNRESOLVED | metric in grouping position |
| 55 | iww | total credits used by warehouse for finance | UNRESOLVED | no matching name or synonym |
| 56 | iww | total credits used by warehouse for ｆｉｎａｎｃｅ (full-width) | UNRESOLVED | no matching name or synonym |
| 57 | iww | total credits used by warehouse для финансов | UNRESOLVED | no matching name or synonym (×2) |
| 58 | iww | total credits used by warehouse 仅财务 | UNRESOLVED | no matching name or synonym |

**Full-output expectations for rows 36–50** (the result column holds status-level content; these
pin the r4/r5 output fields): row 38 `bindings` = [order_count]; row 40 `unresolved` = [finance: no
matching name or synonym, customer: rejected interpretation material] in that order; row 41
`unresolved` lists `customer: rejected interpretation material` alongside the ambiguity; row 42
`unresolved` word `by`; row 44 stderr `E_BAD_CHOICE: unknown id: @date=billing_date`; row 45
`bindings` exclude `usage_day`; row 46 `bindings` = [total_credits_used, usage_day], `grain` null;
row 47 stderr `E_BAD_CHOICE: not a shown candidate: compute=row_count`; row 48 stderr
`E_BAD_CHOICE: conflicting values: @date=billing_date; @date=usage_day`; row 49 `unresolved` word
`total credit used`, `bindings` = [total_credits_used], `grain` null; row 50 `bindings` =
[total_credits_used], `grain` null. Owner amendment D417 pins: row 51 `bindings` =
[total_credits_used, usage_day], `grain` month; row 52 exit 2, stdout empty, last stderr line starts
`E_MODEL_NOT_FOUND`, and an `out/resolution.json` written by a preceding successful call no longer
exists; row 53 `unresolved` = [finance: no matching name or synonym, finance: no matching name or
synonym] (one entry per occurrence), `bindings` = [total_credits_used, warehouse_name]; row 54
`unresolved` = [total credit used: metric in grouping position, total credit used: metric in
grouping position] (one entry per bound match), `bindings` = [row_count]. Owner amendment D421
pins (exit 6 each; `bindings` = [total_credits_used, warehouse_name]; `grain` null; no
ambiguities): rows 55 and 56 `unresolved` = [finance: no matching name or synonym] (NFKC folds the
full-width letters to ASCII); row 57 `unresolved` = [для: no matching name or synonym, финансов: no
matching name or synonym]; row 58 `unresolved` = [仅财务: no matching name or synonym].

## 7. Data Flow — the M1 path, end to end

```
agent-bi init --sqlite tests/fixtures/idle-warehouse-waste.sqlite --table warehouse_metering_history --model idle_warehouse_waste
  1. DuckDBEngine: ATTACH fixture; catalog.describe
  2. scan → semantic/idle_warehouse_waste.yaml + .provenance.yaml
       time_dimensions: usage_day (TEXT ISO → CAST AS DATE)
       dimensions: warehouse_name
       facts: credits_used_compute, credits_used_cloud_services, credits_used
       metrics: row_count, total_/avg_ for 3 facts, distinct_warehouse_name
  3. validate
  4. compile-rill → rill/models, metrics, dashboards (explore + default canvas), provenance manifest
  5. print: "Run: rill start rill   → http://localhost:9009"
Then (M1 is complete only when this part works too — product-definition M1):
agent-bi ask --model idle_warehouse_waste --question "credits by warehouse"
  → exit 0 if a verified or saved query has that normalized question (no LLM involved)
  → exit 4 E_NO_VERIFIED_MATCH otherwise; the skill then composes out/question.json
agent-bi ask --model idle_warehouse_waste --query-file out/question.json
  → interpretation, trust + reason, published, definitions, SQL, table,
    out/<hash>.answer.json (envelope), out/<hash>.vl.json
agent-bi save --from out/<hash>.answer.json --name credits_by_warehouse --question "credits by warehouse"
  → queries/credits_by_warehouse.json (status: saved), rill/components/credits_by_warehouse.yaml
agent-bi save ... --verify --by lee --expected-sql "SELECT warehouse_name, SUM(credits_used) ..."
  → + known answer, verified_queries[] entry (Snowflake SQL), sidecar record; status: verified
agent-bi enrich --model idle_warehouse_waste --accept "tables[0].metrics[total_credits_used]" --by lee
  → confirmed (allowed because a current verified query covers it); the query now answers `governed`
```

## 8. Architecture Decision Records

### ADR-001: The agent sits at build time; its output is text in git
**Status:** Accepted (Lee, 2026-10-14). **Consequence:** no LLM call inside `scan`,
`compile`, `verify`, `ask` execution, or `save`. The two LLM steps (SemanticQuery
composition, enrichment patch) are external and validated.

### ADR-002: Snowflake semantic-view YAML is the semantic format, spec-pure, with a sidecar
**Status:** Accepted (Lee: "steal Snowflake semantic layer lock stock and barrel").
**Consequence:** agent-bi never adds keys to the YAML; provenance and hashes live in the
sidecar; the file loads into Snowflake unchanged (M3 proves it).

### ADR-003: Rill is the BI runtime; agent-bi writes files Rill reads
**Status:** Accepted (Lee, 2026-10-14; free tier first). **Consequence:** no Rill API
calls, no forked Rill code, no custom renderer. If Rill cannot render something, the
feature is cut or the chart is a Vega-Lite component.

### ADR-004: v1 compiles over exactly one relation; views combine tables
**Status:** Accepted (Lee: "version 1 uses views to combine tables and we can define
table relationships later"). **Consequence:** `relationships[]` is refused; join
compilation does not exist; M2 scans a view.

### ADR-005: Numbers are verified by independent routes, never by inspection
**Status:** Accepted. Known answers come from hand-written direct SQL; parity comes from
a second engine. A reviewer reading SQL is not evidence that a number is right.

### ADR-006: Refuse, don't approximate
**Status:** Accepted. Every unsupported construct has an error code. There is no
"best effort" path anywhere in the compiler.

### ADR-007: Determinism contract — artifacts, receipts, and what each guarantees
**Status:** Accepted; revised 2026-10-14 (review R3). Three different guarantees, stated
separately so none is claimed by the others:

**(a) Deterministic compilation — byte identity of artifacts.** Given identical inputs
(model file bytes, sidecar bytes, canonical query JSON, `agent-bi.yaml`, `models/*.sql`
text, pinned Rill version), these outputs are byte-identical with **no masking**:
`semantic/<model>.yaml`; every sidecar field except `scanned_at`; `compiled_sql` for every
dialect; `queries/*.json`; `rill/components/*.yaml`; every generated Rill file; the
`artifact` part of every provenance record; the build manifest except `build_id`;
`out/<hash>.vl.json`. Tests run each producer twice and `diff` the outputs, masking only
`scanned_at` and `build_id`.

**(b) Reproducible execution — same rows for the same as-of.** Given the same `compiled_sql`,
the same engine version, the same session settings (§4.6) and the same as-of (file digest,
or Snowflake Time Travel timestamp within retention), execution returns the same rows
under the §5.6 comparison (to the cent; ordered by the total order of §5.3.1). This is
asserted by `verify` on every commit and by `parity` across engines. It is **not** byte
identity of floats and is not claimed to be.

**(c) Execution receipts vary and are expected to.** The `receipt` part of a provenance
record (`executed_at`, `executed_sql` and its hash when it carries an as-of clause,
`as_of`, `engine_version`, `row_count`) records facts about one run. Two receipts for the
same artifact may differ; nothing compares receipts for identity. The sidecar's per-entry
`at` fields are stable across refreshes of unchanged objects (§5.2 rule 3), so for a
**refresh** they are part of (a); a **first** scan stamps them with that run's time, so
two independent first scans are not claimed to be byte-identical (ratified wording, D391).

The fields outside every guarantee are exactly: `scanned_at`, `build_id`, `executed_at`,
`verified_at`, `proposed_at`, `expected_at`, `as_of.value` for Snowflake, and
`executed_sql`/`executed_sql_sha256` when an as-of clause is present. Nothing else is
"just a timestamp".

### ADR-008: agent-bi propagates no filters; Rill's canvas filters apply to every component, including saved ones
**Status:** Accepted; amended 2026-10-14 (Sprint 0 review finding A3). A SemanticQuery's
`filters` apply to that query only, and a saved component bakes them into its
`metrics_sql`. agent-bi implements no cross-component filter propagation and no global
filter state. **But Rill does:** a `custom_chart` registers with "the canvas filter
system" (`docs/pinned/rill-custom-chart-v0.90.5.md`), so the canvas's current time range
and dimension filters are applied **on top of** the component's own Metrics SQL —
observed in the Sprint 0 review: the same `credits_by_warehouse` component shows
2739/1093/544 at `inf`, 1174/469/233 at the last 3 days, and only `CORE_ELT_WH` under a
warehouse filter (`evidence/sprint-0/review/13-*`). This is Rill's native behaviour,
unmodified, and it is useful; it is also outside agent-bi's verification. Consequences:
1. **What is verified.** `verify --engine rill` (§5.6) proves the numbers a saved or
   generated component shows **at the canvas's all-time range with no canvas filters** —
   the scope of its own query. Any other canvas time range or filter is Rill's computation
   over the same measure definitions; agent-bi neither verifies nor labels it.
2. **Default view is the verified view.** Every canvas agent-bi writes or forks carries
   `defaults: {time_range: inf}` and no default filters (§5.4.1). `save --add-to-canvas`
   refuses a target canvas whose `defaults.time_range` is not `inf` or which declares
   default filters (`E_CANVAS_NOT_VERIFIED_SCOPE`, §5.10 step 3).
3. **Disclosure.** `WHERE_AM_I.md` states this limitation beside the existing "Rill's UI
   shows no per-number provenance" limitation (Sprint 3 task 6).
A request for agent-bi-side filter propagation, or for verifying filtered views, is a
BACKLOG item (B-016).

### ADR-009: Generated files are regenerated; operator files are never touched
**Status:** Accepted. `compile-rill` **owns and overwrites** exactly: `rill/rill.yaml`,
`rill/models/<model>.sql`, `rill/metrics/<model>.yaml`,
`rill/dashboards/<model>_explore.yaml`, `rill/dashboards/<model>_default_canvas.yaml`,
`rill/provenance/<model>.json`. Each generated per-model file begins with the line
`# agent-bi: generated from semantic/<model>.yaml — do not edit; edit the semantic model and re-run compile-rill`
(for SQL files, `-- agent-bi: generated ...`); the project-level `rill/rill.yaml` begins with
the model-neutral line `# agent-bi: generated project file — do not edit; re-run compile-rill`
(D395: a per-model header would make the shared file flip with the last model compiled).
A JSON manifest carries no header line. Every other file under `rill/` is
**operator-owned**: `compile-rill` never writes it, `save` creates new files only and
refuses to overwrite an existing name without `--force`, and the builder (Rill's canvas
editor) edits operator-owned canvases, which the operator creates by copying the default
canvas (`agent-bi canvas fork --model <m> --name <canvas-name>` copies
`<model>_default_canvas.yaml` to `<canvas-name>.yaml` and strips the generated header).
**Consequence:** regeneration is always safe; `test_rill_target.py::test_operator_files_untouched`
modifies an operator canvas, re-runs `compile-rill`, and asserts the bytes are unchanged.

The same ownership rule extends to the semantic side (revised 2026-10-14, review R4):
`scan` is create-only; `scan --refresh` owns exactly the `catalog`-status objects and the
sidecar's `scanned_at`/`relation`/`columns_digest` fields, and carries everything else
over verbatim (§5.2). `queries/*.json`, `tests/fixtures/known-answers/`, verified queries
and `inferred`/`confirmed` objects are **accepted additions** that no regeneration
command removes. A fresh-clone reproducibility check therefore means: regenerate the
generated set (`rill/`, and `catalog` objects via `--refresh`) and assert a zero diff;
it never deletes `semantic/` to see whether `init` can recreate accepted content, because
by design it cannot.

### ADR-010: Trust is bound to semantics; publication is reported, not trusted
**Status:** Accepted 2026-10-14 (review R2). A trust label certifies a specific set of
definitions (the `semantic_digest`, §4.7). Changing a referenced `expr` invalidates every
verification and every component annotation that depended on it, visibly (`STALE`), and
nothing re-verifies without the operator re-running the independent route. `governed`
requires both verification against the current digest and `confirmed` status on every
referenced definition; a chart's presence on a dashboard is `published`, a separate fact.
Confirmed entries cannot be overwritten by a patch without an explicit per-path operator
override, and a metric cannot be confirmed without a current known answer.

### ADR-011: The YAML's `base_table` is the Snowflake physical identity
**Status:** Accepted 2026-10-14 (review R6). ADR-002 promises the semantic YAML loads
unchanged. The only policy compatible with that promise is that `base_table` names the
real Snowflake relation and the loader creates fixture relations **at that identity**
(`FIXTURE.MAIN.<table>`, `VIEWS.MAIN.<view>`), rather than mapping identities at load
time and shipping a file whose references point somewhere else. `relation_map` exists
for DuckDB only. `verified_queries[].sql` is Snowflake-dialect for the same reason, and
parity route 4 (§5.7) executes it verbatim to prove the shipped examples run. For a
client engagement the model is scanned from Snowflake, so the identity is the client's
own and the loader step does not apply.

## 9. Error Handling Strategy

- All domain errors are subclasses of `AgentBiError(code: str, message: str, exit_code: int)` in `errors.py`.
- `main.py` catches `AgentBiError`, prints `E_<CODE>: <message>` to stderr, exits with `exit_code`. Any other exception propagates with a traceback (exit 1) — no blanket `except Exception`.
- Engine errors are wrapped as `E_ENGINE` (exit 3) with the engine's message appended verbatim.
- `Engine.execute()` accepts only `SELECT`/`WITH` statements; anything else → `E_STATEMENT_NOT_ALLOWED` (exit 2). Attach, session settings and `apply_models` are internal engine methods.
- Validation messages always include the offending path/name and, for `E_UNKNOWN_FIELD`, the sorted list of valid names.
- **Error catalogue (ratified D391).** `errors.py` holds the single authoritative catalogue
  `CATALOGUE: dict[str, tuple[int, str]]` — code → (exit code, one-line meaning). Every
  `E_<CODE>` raised anywhere in `src/` must be a key of it (a test greps `src/` for
  `E_[A-Z_]+` literals and for codes passed to the error helpers and asserts membership).
  `docs/errors.md` is **generated** by `python -m agent_bi.errors --doc` and committed; a
  test asserts the committed file equals the generator output. Codes already implemented
  keep their names; no renaming for aesthetics.

## 10. Performance Considerations

Fixtures are tiny (≤ 2,000 rows). No optimization work is in scope. The only
performance requirement is M1's ten-minute wall clock, which is dominated by Rill
installation and startup, not agent-bi.

## 11. Security

- Secrets only via environment variables referenced from `agent-bi.yaml`; never written to any file under the repo; `.gitignore` covers `out/`, `*.duckdb`, `.env`.
- All generated SQL uses quoted identifiers from the catalog and literal functions (§5.3.3); user text never reaches SQL unquoted. `test_compile.py::test_injection_quoting` is mandatory.
- Enrichment patches and SemanticQuery JSON are untrusted input: schema-validated, size-capped (1 MB), and never executed. No op in a patch and no field in a query carries SQL; the only expression a patch may carry (`add_metric.expr`) must parse under the closed grammar of §5.3.2 before anything is written.
- Operator-authored SQL (`--expected-sql`, `models/*.sql`, `filters[].expr`) is executed read-only (SQLite `mode=ro`, DuckDB `read_only=True`, Snowflake role as configured), single-statement, and — for filters and views — only after passing the closed grammar or denylist (§5.3.5, §5.13).
- Every multi-file write is a transaction (§5.14); a half-written promotion cannot exist unless the file system itself failed mid-publish, and `validate` detects that state.

---

*Architecture created 2026-10-14 by the Chief of Staff; revision 2 the same day applying
the independent review's findings R1–R6 (`docs/review-response-2026-10-14-r2.md`).
Pinned external specs (Sprint 0) override paraphrases in this document.*
