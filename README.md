# Agent BI

**A table becomes a dashboard and a queryable semantic model. Engines compute the
numbers; an agent can help you ask for them.**

Agent BI scans a local SQLite table, generates a Snowflake-format semantic YAML model,
compiles a Rill dashboard, and answers questions with SQL, provenance and an explicit
trust label. The Python CLI calls no language model. An optional Claude Code skill
tries known questions and a deterministic resolver first, then proposes a constrained
query when the resolver cannot interpret the question.

**Status: 0.1.0 / M1 accepted.** Supported today: one local SQLite table per model,
DuckDB execution, Rill **v0.90.5**, saved charts, known-answer verification, SQL-free
enrichment patches applied transactionally, and a read-only drift preview. The accepted
version has also been consumed by a separate marketing decision-demo project. Views and
Snowflake execution are future work.

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
