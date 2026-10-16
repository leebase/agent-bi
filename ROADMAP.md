# Roadmap

## Delivered — M1 / 0.1.0

One local SQLite table per model → Snowflake-format semantic YAML → DuckDB execution
and a Rill v0.90.5 dashboard. Question lookup, deterministic resolution with human
ambiguity picks, constrained agent composition, answer provenance, trust labels,
known-answer checks, definition acceptance and saved charts are implemented.

M1 was accepted on 2026-10-15. A separate marketing decision-demo project subsequently
used the accepted version for descriptive analysis. Experiment statistics, causal
inference and decision scenarios remained that project's responsibility.

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

## Planned milestones — not started

| Work | Intended acceptance |
|---|---|
| Enrichment and drift (original Sprint 5) | Apply a constrained patch transactionally, protect accepted definitions, detect source drift, demonstrate stale → reverified answers. |
| M2: given a view (original Sprint 6) | Reproduce the table workflow over a view combining fixture tables; independently verify its breakdowns and render it in Rill. |
| M3: Snowflake parity (original Sprint 7) | Load unchanged semantic definitions into Snowflake and demonstrate independently checked parity across engines. Requires account access and explicit NULL-ordering conventions. |

There are no delivery dates. M1 acceptance did not start these milestones; sequencing
will follow evidence from actual consumers. The architecture describes some planned
interfaces; it is not a claim that those commands work in 0.1.0.
