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

For answers and saved charts in a standalone workspace, also copy
[themes/default.json](../themes/default.json) from the checkout into
`themes/default.json` in your workspace. The chart builder reads its default theme
from the project directory; the included checkout already has this file.

From this new directory, run:

```bash
agent-bi init --sqlite /absolute/path/to/your.sqlite --table orders --model orders
agent-bi validate --model orders
rill start rill --no-open
```

Replace the source path and table with yours. The model name must match
`^[a-z][a-z0-9_]*$`. Use a stable source snapshot; Agent BI reads the data and records
its digest. A model represents one relation. Joins and view-based workflows are not
part of the accepted release.

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
| `rill/` | Generated dashboard project and build provenance. |
| `out/<hash>.answer.json` | Query, rows, trust, SQL and execution receipt. |
| `out/<hash>.vl.json` | Vega-Lite chart specification for the answer. |
| `queries/<name>.json` | A saved query. |

The committed warehouse example includes known answers. Other data needs its own
independent verification. Keep your configuration, data paths, outputs and model
values private when appropriate; a generated answer contains actual result rows.

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

This is a template: replace every placeholder. The expected SQL must compute the
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
