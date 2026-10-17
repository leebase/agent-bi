# Roadmap

## Delivered — M1 / 0.1.0

One local SQLite table per model → Snowflake-format semantic YAML → DuckDB execution
and a Rill v0.90.5 dashboard. Question lookup, deterministic resolution with human
ambiguity picks, constrained agent composition, answer provenance, trust labels,
known-answer checks, definition acceptance and saved charts are implemented.

M1 was accepted on 2026-10-15. A separate marketing decision-demo project subsequently
used the accepted version for descriptive analysis. Experiment statistics, causal
inference and decision scenarios remained that project's responsibility.

Sprint 5 (enrichment and drift) is implemented on this branch: transactional SQL-free
enrichment patches with confirmed-entry protection and proposed-query promotion
(`enrich --apply`), the stale → independently re-verified cycle, and a read-only refresh
preview (`diff`).

## Candidate improvements after public release

These are proposals, not scheduled or authorized implementation work:

- Consumer reliability: table attachment in SQLite databases containing incompatible
  views, clearer setup, and usable default charts.
- Trust visibility: record whether an answer came from lookup, resolution or agent
  composition, preserving the original question and unresolved terms.
- Presentation and provenance: UTC daily chart display, chart titles, and manifest
  freshness after acceptance/verification changes.
- Resolver usability: fewer repeated choices and an explicit policy for common smart
  punctuation; any grammar change must preserve refusal semantics and frozen regressions.

## Direction (D430, 2026-10-16)

Agent BI is the deterministic trust and control plane for agent-built analytics on Rill.
Rill owns transformation execution, the metrics runtime and the dashboards; Agent BI owns
trust in them. The original "replace Power BI with agent-first development" goal is the
project's origin, not its product definition. The SemanticQuery compiler stays as an
independent verification route and future Snowflake compilation, in maintenance mode.

## Planned milestones — not started

| Work | Intended acceptance |
|---|---|
| M2: trust Rill models | An operator writes one ordinary Rill SQL model joining two tables. Agent BI does not generate or rewrite that join; `init` consumes its output, builds the semantic and dashboard layer, independently verifies two known answers, and changing the Rill model makes those verifications STALE. |
| M2.x: govern agent changes | When Rill's MCP server or developer agent changes analytics definitions, Agent BI detects the semantic change, demotes trust and requires acceptance and evidence again. Designed from pinned evidence of Rill's actual local write path. |
| Rill skill | Deterministic-first resolution offered inside Rill's own agent experience as a project skill, instead of a separate chat interface. |
| M3: Snowflake | Carry governed definitions into Snowflake's warehouse-native semantic representation with independently checked parity. Requires account access and explicit NULL-ordering conventions. |

Not planned, because Rill already provides them: a join or relationship compiler, a chat
UI, a built-in LLM client, sharing, alerts and Rill administration.

There are no delivery dates. The architecture describes some planned interfaces; it is not
a claim that those commands work in 0.1.0.
