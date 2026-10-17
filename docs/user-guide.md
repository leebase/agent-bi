# Using Agent BI

Start with the [README example](../README.md). It includes synthetic SQLite data,
two semantic models and nine known answers for the warehouse model. This guide covers
the supported M1 workflow; commands shown here are implemented in 0.1.0.

## Bring your own table

Install Agent BI from the Git checkout in a Python 3.12+ virtual environment. Keep
that environment active. Create a separate project directory for your data and
create an `agent-bi.yaml` there:

```yaml
schema_version: agent-bi-config/1.0
project: my-analysis
engines:
  duckdb:
    database: ":memory:"
    attach: []
    relation_map: {}
rill:
  project_dir: rill
  olap: duckdb
```

`init` requires an existing configuration file.

The chart builder uses the default theme shipped with Agent BI. A
`themes/default.json` in your workspace overrides it; `--theme <name>` loads
`themes/<name>.json` from the workspace instead.

From this new directory, run:

```bash
agent-bi init --sqlite /absolute/path/to/your.sqlite --table orders --model orders
agent-bi validate --model orders
rill start rill --no-open
```

Replace the source path and table with yours. The model name must match
`^[a-z][a-z0-9_]*$`. Use a stable source snapshot; Agent BI reads the data and records
its digest. A model represents one relation; to combine tables, use an operator-authored
Rill SQL model (see [Combine tables with a Rill model](#combine-tables-with-a-rill-model)).

`init` adds the SQLite attachment, scans the catalog, validates the semantic model
and generates Rill files. It preserves existing semantic content when refreshing
the same model. Reusing a model name for a different relation is refused.
Newly scanned data has no precomputed known answers: do not assume a `verify` command
alone creates evidence for your metrics.

Use `rill/dashboards/<model>_default_canvas.yaml` to identify the generated canvas;
the browser URL is `http://localhost:9009/canvas/<model>_default_canvas`.
Rill verification expects the default local server on port 9009.

If a SQLite file contains views DuckDB cannot parse, attachment can fail even when
your chosen table is valid. Use a table-only SQLite extract for M1; see
[troubleshooting](troubleshooting.md#sqlite-attachment-fails-on-a-view).

## Understand the generated files

| Path | Purpose |
|---|---|
| `agent-bi.yaml` | Attachments, relation mapping, Rill location and engine settings. |
| `semantic/<model>.yaml` | Semantic definitions in Snowflake semantic-view YAML form. |
| `semantic/<model>.provenance.yaml` | Origin, definition status and verification metadata. |
| `rill/models/<model>.sql` | Generated for a table model; **operator-owned** for a view model (never written by Agent BI). |
| `rill/` | Generated dashboard project and build provenance. |
| `out/<hash>.answer.json` | Query, rows, trust, SQL and execution receipt. |
| `out/<hash>.vl.json` | Vega-Lite chart specification for the answer. |
| `queries/<name>.json` | A saved query. |

The committed warehouse example includes known answers. Other data needs its own
independent verification. Keep your configuration, data paths, outputs and model
values private when appropriate; a generated answer contains actual result rows.

## Combine tables with a Rill model

Agent BI compiles one relation; combining tables is Rill's job. An operator-authored
**model graph** is a set of ordinary Rill SQL files under `<rill.project_dir>/models/`:
one single-table **source model** per table, each with its own `sqlite_scan(...)`, and an
ordinary **join model** that reads the source models by bare name. Rill executes the
graph; Agent BI treats every file as data — constrained, hashed and executed verbatim,
never generated, rewritten, translated or parsed as a join specification.

```sql
-- rill/models/warehouse_metering_source.sql
SELECT * FROM sqlite_scan('../tests/fixtures/idle-warehouse-waste.sqlite', 'warehouse_metering_history')
```

```sql
-- rill/models/warehouse_metering_enriched.sql
SELECT m."usage_day", m."warehouse_name", w."size" AS "warehouse_size",
       w."cost_center", m."credits_used"
FROM warehouse_metering_source AS m
LEFT JOIN warehouses_source AS w ON w."name" = m."warehouse_name"
```

The supported form is closed. **Per file:** one statement, a `SELECT` (a `WITH … SELECT`
qualifies); at most one `sqlite_scan('<path>', '<table>')` call, with single-quoted
literals; no Rill annotation lines (`-- @…`), no templating (`{{`/`{%`), no external
readers (`read_csv`, `read_parquet`, `postgres_scan`, `glob`, …), and none of the
nondeterministic constructs (`CURRENT_DATE`, `NOW`, `RANDOM`, `SAMPLE`, …). Every other
relation a model reads must be a registered model referenced by bare name. **Per
closure:** at least one model has a `sqlite_scan`; every `sqlite_scan` in the closure
names the same SQLite file (resolved against the Rill project directory; the file must
exist); and the dependency graph has no cycle (a cycle is refused and named).

Register the top model (the semantic model name defaults to the Rill model name):

```bash
agent-bi init --rill-model warehouse_metering_enriched
agent-bi verify --model warehouse_metering_enriched
```

`init` registers the named model **plus its transitive dependency closure** in
`agent-bi.yaml` (`rill_models`), adds the `"VIEWS.MAIN": "memory.main"` mapping, applies
the attach-alias rule to the graph's single SQLite file and scans
`VIEWS.MAIN.warehouse_metering_enriched` into `semantic/<model>.yaml`. Only the model
`init` names is scanned; its upstream models are registered but get no semantic model
unless you initialize them yourself. `scan --refresh` recomputes the sidecar's
`relation.source` (`rill/models/<name>.sql`), `relation.dependencies` (the sorted
`rill/models/<n>.sql` paths of the closure minus the model) and `relation.models_digest`
(the closure digest). `compile-rill` writes the metrics view with `model: <name>`, the
explore, the default canvas and the manifest, and no `rill/models` file. For a table
model, `compile-rill` refuses with `E_OPERATOR_FILE` to overwrite an existing
`rill/models/<model>.sql` that lacks the generated header.

**Dependency staleness.** `models_digest` is the sha256 of the canonical
`{name: file digest}` map over the closure, where a file digest is the file text with
CRLF/CR → LF and trailing whitespace stripped (every other edit, comments included,
changes it — fail closed). Changing the join model **or any model upstream of it at any
depth** makes every verification of the top model `STALE` and names `models_digest`; a
whitespace-only change does not, and changing an unrelated registered model does not.
Clear it by re-running the independent route
(`save --verify --force --expected-sql "<human SQL>"`). The independent SQL for a view
model is human-written SQL over the raw tables, never a query against the Rill model.

**How dependencies are found (frozen asymmetry).** Dependency discovery is not a SQL
lineage parser: `deps(M)` is every candidate model name that appears in `M`'s file text
as a whole word, case-insensitive, **comments and string literals included** (the
candidates are the operator-owned files at `init` and the registered names at engine
start). The error direction is deliberate: a name that merely appears in a comment or
literal causes an unnecessary `STALE`; a missed dependency cannot happen because a model
can only read another registered model by spelling its name. Do not put another
registered model's name in a comment unless you want that dependency.

**Failure isolation (D431).** Every registered model is checked and created
independently. A model that fails the form, the denylist or view creation — itself, or
because a model in its closure failed — is unavailable and refuses only commands that
use it, with the error naming the failing file; every other relation is unaffected.
Every model in the closure of a semantic model's relation must build for that model to
validate, verify or answer, so `validate --model <view model>` opens the DuckDB engine
and fails when any model in the closure does not build. A broken registered model
outside that closure blocks neither `validate` nor `verify`.

Rill annotations and templating, YAML, incremental or partitioned models, more than one
`sqlite_scan` per file, more than one SQLite file per closure, dependency cycles,
non-SQLite sources, references to unregistered or generated models, and Snowflake
materialization are **not supported**: each is refused (`E_UNSUPPORTED_VIEW_FORM` or
`E_NONDETERMINISTIC_VIEW`), never approximated. (Rill v0.90.5 cannot build a model with
two relative `sqlite_scan` calls, so neither does Agent BI.)

## Ask questions without a model

The CLI first supports exact normalized matches to saved/verified question text:

```bash
agent-bi ask --model idle_warehouse_waste --question "Credits by warehouse?"
```

For a new question, use the deterministic resolver:

```bash
agent-bi resolve --model idle_warehouse_waste \
  --question "avg credits used by warehouse name"
agent-bi ask --model idle_warehouse_waste --query-file out/resolution.json
```

Run the second command only after `resolve` exits 0 (`RESOLVED`). This example returns
averages about 391.29, 156.14 and 77.71, labelled `exploratory`. Deterministic parsing
does not make an answer verified.

An ambiguous phrase stops for a human choice:

```bash
agent-bi resolve --model idle_warehouse_waste \
  --question "how much compute by warehouse"
# Exits 5. Read the candidates, then explicitly choose:
agent-bi resolve --model idle_warehouse_waste \
  --question "how much compute by warehouse" \
  --choose compute=total_credits_used_compute
agent-bi ask --model idle_warehouse_waste --query-file out/resolution.json
```

Keep the original question unchanged when submitting picks. Unsupported questions
exit 6 (`UNRESOLVED`); inspect the unresolved words rather than executing an old result.

For direct machine integration, supply a SemanticQuery JSON to `ask --query-file`.
Print its schema and rules with `agent-bi contract --print`; a query names semantic
objects and constrained filters, not SQL. The full [contract](semantic-query-contract.md)
and [error catalog](errors.md) are included.

## Ask through an agent

In Claude Code, use `/agent-bi-ask` from the checkout root. For another harness, give
it [skills/ask/SKILL.md](../skills/ask/SKILL.md). For a separate analysis directory,
install or link that skill into the harness's project skill directory first; the
repository's `.claude/skills` links apply only inside the checkout.

The skill follows this sequence:

1. Try the verified-question lookup using your question verbatim.
2. If no match exists, call the resolver. Show ambiguous choices to you.
3. Only after resolver exit 6 may the agent compose `out/question.json` and execute it.

For example, `total credits used by warehouse name for finance` leaves `finance`
unresolved. The agent can propose a filter for `FINANCE_REPORTING_WH`, disclose that
interpretation and submit the JSON. Agent BI validates it and computes the result.
The agent must never supply SQL or invent a number. Inspect its interpretation and
trust line before relying on an answer.

## Establish a known answer

Saving and verifying are separate operations. To verify a new query, a person supplies
an independently authored SQLite `SELECT`/`WITH` query against the same source file.
After inspecting the answer envelope, use:

```text
agent-bi save --from out/<hash>.answer.json --name <query_name> \
  --question "<saved question>" --verify --by <your-name> \
  --expected-sql '<independent SQLite SELECT>'
```

This is a template: replace every placeholder. `save --verify` runs the independent
route installed with Agent BI read-only against the source SQLite file; it never reads
code from the workspace or from a source checkout. The expected SQL must compute the
same columns and deterministic row ordering as the semantic query, by an independent
route. Do not derive the expected result by copying Agent BI's compiled SQL or its
printed number. Existing saved queries require `--force` to replace.

Then run `agent-bi verify --model <model>` and, with the correct Rill project running,
`agent-bi verify --model <model> --engine rill`. `--coverage` shows per-metric coverage.
Changes to definitions invalidate verification and require independent re-verification.

## Accept business definitions

For the included example, review that `total_credits_used` means `SUM("credits_used")`
and that `warehouse_name` is the intended grouping. Then, as the human owner:

```bash
agent-bi enrich --model idle_warehouse_waste \
  --accept "tables[0].metrics[total_credits_used]" --by your-name
agent-bi enrich --model idle_warehouse_waste \
  --accept "tables[0].dimensions[warehouse_name]" --by your-name
agent-bi ask --model idle_warehouse_waste --question "Credits by warehouse?"
```

That question becomes `governed`. An unverified question remains `exploratory`, even
if its definitions are accepted. A definition without current known-answer coverage
cannot be accepted (`E_ACCEPT_UNCOVERED`).

`--by` records attribution; it is not identity authentication or access control.
The human-only rule is a CLI/agent workflow contract. Use your environment's access
controls if multiple users or agents can write the project.

After accepting definitions or promoting a verified query, refresh the build manifest:

```bash
agent-bi compile-rill --model idle_warehouse_waste
```

## Enrich with a patch

A harness agent (or you) can propose descriptions, synonyms, metrics and queries as an
EnrichmentPatch JSON and apply it with one command:

```bash
agent-bi enrich --model idle_warehouse_waste --apply out/patch.json
```

The patch is a single transaction. Its four operations are `set_description`,
`add_synonyms`, `add_metric` and `propose_query`; every touched entry becomes `inferred`
with `origin: enrich:<patch_id>`. A patch may not contain a `sql` or `expected_sql` key
anywhere; `add_metric` expressions must pass the metric grammar; a name that belongs to a
non-metric object is refused (`E_NAME_COLLISION`); synonyms must be unique across the
model; and the file is capped at 1 MB with a UUID4 `patch_id`. A bad later operation
leaves earlier operations unwritten.

Confirmed entries are protected. To rewrite one, pass its exact leaf:

```bash
agent-bi enrich --model idle_warehouse_waste --apply out/patch.json \
  --allow-confirmed "tables[0].metrics[total_credits_used]"
```

A parent path, wildcard or near match is not sufficient; without the exact leaf the patch
is refused with `E_PATCH_TOUCHES_CONFIRMED` and nothing is written. Rewriting a confirmed
expression reverts that entry to `inferred` and can make verified queries stale.

`propose_query` writes `queries/<name>.json` with status `proposed` and touches nothing
else. Promote it through the human route you already use: run `ask` to produce an answer
envelope, then `save --verify --by <person> --expected-sql "<independent SQL>"` with the
same name. No SQL ever travels inside a patch.

## Preview drift before a refresh

`diff` re-runs catalog discovery and shows exactly what `scan --refresh` would change,
without writing anything:

```bash
agent-bi diff --model idle_warehouse_waste --engine duckdb
```

The Markdown report lists added, removed and type-changed columns, the row count, the
semantic objects a refresh would add, update or remove, and every accepted object or
verified query the refresh would break. Structural drift exits 1; a row-count change
alone is reported and exits 0. Resolve structural drift before accepting definitions or
re-verifying queries against the new shape.

When an expression change makes verification stale, `verify` prints `STALE <name>` and
`ask` labels that query `exploratory` with the stale reason. The only way to clear it is
the operator re-running the independent route (`save --verify --force --expected-sql` on
the same name); nothing re-verifies automatically.

## Keep an answer as a chart

After running the warehouse question above:

```bash
agent-bi canvas fork --model idle_warehouse_waste --name main
agent-bi save \
  --from out/28a29b6593efff6a85c2de86a667f7d6d83f01413c23fc9cdc40436e253c4267.answer.json \
  --name credits_by_warehouse_chart --add-to-canvas main
```

Open `http://localhost:9009/canvas/main`. The copied dashboard has a new bar chart
showing 2739, 1093 and 544. For other answers, use the path printed by `ask`.
Saving a chart does not itself verify, govern or publish it. The generated default
canvas may be regenerated; the forked canvas is operator-owned.

Verification covers Rill's **all-time, unfiltered** view. Changes to its time range or
dashboard filters are not independently verified by Agent BI. `published: false` is
separate from trust and chart placement; `save` does not deploy a dashboard.
