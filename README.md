# Agent BI

**The deterministic trust and control plane for agent-built analytics on Rill.**

Rill runs the transformations, the metrics layer and the dashboards. Agent BI answers the
questions Rill does not: who defined this metric, has a human accepted its meaning, what
independent evidence verifies it, has anything it depends on changed, can an agent change
it without silently changing what "governed" means, and can this question be answered
deterministically without asking an LLM. Agent BI does not replace Rill capabilities: when
Rill can deterministically perform an analytics operation, Agent BI adopts it and adds
provenance, verification, human acceptance, staleness detection and agent-change
governance where required.

Agent BI scans a local SQLite table, generates a Snowflake-format semantic YAML model,
compiles a Rill dashboard, and answers questions with SQL, provenance and an explicit
trust label. The Python CLI calls no language model. An optional Claude Code skill
tries known questions and a deterministic resolver first, then proposes a constrained
query when the resolver cannot interpret the question.

**Status: 0.1.0 / M1 accepted; M2 "Trust Rill models" implemented.** Supported today:
one local relation per model, operator-authored Rill SQL models (views) executed
verbatim on both DuckDB and Rill, DuckDB execution, Rill **v0.90.5**, saved charts,
known-answer verification, SQL-free enrichment patches applied transactionally, and a
read-only drift preview. Snowflake execution is future work.

## Try the included example

Requirements: Python **3.12+**, Git, and Rill **v0.90.5** for the dashboard. Install the
matching Rill binary from its [release page](https://github.com/rilldata/rill/releases/tag/v0.90.5)
using the asset for your operating system and architecture. Place `rill` on PATH or at
`~/.rill/rill`. The latest Rill release is not this project's compatibility pin.

```bash
git clone https://github.com/leebase/agent-bi.git
cd agent-bi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

agent-bi init --sqlite tests/fixtures/idle-warehouse-waste.sqlite \
  --table warehouse_metering_history --model idle_warehouse_waste
agent-bi verify --model idle_warehouse_waste
agent-bi ask --model idle_warehouse_waste --question "Credits by warehouse?"
```

The verification command prints **nine PASS lines**. The answer is
FINANCE_REPORTING_WH **2739**, MARKETING_ADHOC_WH **1093**, CORE_ELT_WH **544**.
Its label is `verified`: the fixture has known answers, but its definitions have not
been accepted by you.

Start the dashboard and leave this terminal running:

```bash
rill start rill --no-open
# If installed at its default location: ~/.rill/rill start rill --no-open
```

Open [the local dashboard](http://localhost:9009/canvas/idle_warehouse_waste_default_canvas).
Expected KPIs: **21** rows, **4,201** compute credits, **200.05** average compute credits,
**175.04** cloud-services credits. In a second terminal, activate the same venv and run:

```bash
agent-bi verify --model idle_warehouse_waste --engine rill
```

This checks the nine known answers through Rill and confirms it is serving this project.

## Combine tables with a Rill model

Rill performs the join; Agent BI never writes, rewrites or translates it. Build the graph
the way Rill supports it: one ordinary single-table **source model** per table, each with
its own `sqlite_scan(...)`, and an ordinary **join model** that references the sources by
name — for example `rill/models/warehouse_metering_source.sql`, 
`rill/models/warehouses_source.sql` and `rill/models/warehouse_metering_enriched.sql`
(the join). Register the top model:

```bash
agent-bi init --rill-model warehouse_metering_enriched
agent-bi verify --model warehouse_metering_enriched
```

`init` registers `warehouse_metering_enriched` **and every model in its dependency
closure** (`rill_models`), adds the `"VIEWS.MAIN": "memory.main"` mapping and the
attachment for the graph's SQLite file to `agent-bi.yaml`, scans
`VIEWS.MAIN.warehouse_metering_enriched`, and generates the metrics view, explore and
default canvas. Only the model you name is scanned; its upstream models are registered
but get no semantic model unless you initialize them yourself.

Every operator file is executed verbatim by DuckDB and by Rill. The sidecar records the
closure's `relation.dependencies` and `models_digest`: the sha256 of the canonical
`{name: normalized file digest}` map over the whole closure (CRLF and trailing whitespace
ignored). Changing the join model **or any model upstream of it at any depth** changes
that digest and makes every verification of the model `STALE` until you re-run the
independent route (`save --verify --force --expected-sql "<human SQL>"`); changing an
unrelated registered model does not. The closed form, the dependency rule's
false-positive-only asymmetry and the documented limits are in the
[user guide](docs/user-guide.md#combine-tables-with-a-rill-model).

## Ask through Claude Code

From the clone's root, with the venv active, start `claude` and enter:

```text
/agent-bi-ask Credits by warehouse?
```

If asked to choose a model, select `idle_warehouse_waste`. The repository includes the
skill under `.claude/skills/`. Claude itself uses a model and its normal authentication;
the underlying Agent BI verified lookup and resolver do not call a model. The CLI-only
example above requires no Claude subscription or model API key.

## Enrich and preview drift

A harness agent (or you) can propose descriptions, synonyms, metrics and queries as an
EnrichmentPatch and apply it in one transaction:

```bash
agent-bi enrich --model idle_warehouse_waste --apply out/patch.json
agent-bi diff --model idle_warehouse_waste --engine duckdb
```

A patch carries no SQL, never deletes, and is refused whole if any operation fails;
confirmed definitions require `--allow-confirmed <exact leaf>`. `propose_query` writes a
`proposed` query only — a human still promotes it with `save --verify --expected-sql`.
`diff` writes nothing and exits 1 on structural drift. The [user guide](docs/user-guide.md#enrich-with-a-patch)
shows the stale → independently re-verified cycle.

## What the trust label means

| Label | Meaning |
|---|---|
| `exploratory` | No current known answer matches this query, or its verification is stale. |
| `verified` | This exact query has a current known answer; some definitions remain unaccepted. |
| `governed` | This exact query has a current known answer and every definition it uses is human-accepted. |

These labels describe the executed query. When an agent interprets your words, also
read its interpretation: a correct computation can still answer a different question.

## Documentation

- [User guide](docs/user-guide.md): your own table, questions, human acceptance, charts.
- [CLI reference](docs/cli.md): supported commands, exit codes and output files.
- [Troubleshooting](docs/troubleshooting.md): runtime setup and current limitations.
- [Roadmap](ROADMAP.md): accepted scope and proposed follow-on work.
- [Architecture](architecture.md): detailed contracts, including the frozen resolver grammar.
- [Contributing](CONTRIBUTING.md): local checks and contribution boundaries.

## License

Copyright 2026 Lee Harrington. Licensed under the [Apache License 2.0](LICENSE).
See [third-party notices](THIRD_PARTY_NOTICES.md) for external references.
