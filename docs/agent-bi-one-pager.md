# Agent BI

**The trust layer for agent-built analytics**

Agents can now create models, metrics and dashboards faster than any human can review them.
Agent BI separates two questions that AI analytics platforms increasingly blur:

1. **Do we trust what this metric means?**
2. **When we last checked, was the analytics runtime actually serving that meaning correctly?**

Rill gives agents the power to build analytics. Agent BI gives organizations a reason to trust
what those agents built.

## What it looks like

Nine verified questions over a warehouse-cost model, on a real Rill runtime:

| Step | Definition | Serving |
|---|---|---|
| Calculations independently verified | 9 verified, 0 governed | No serving check recorded. |
| A person accepts the business definitions | **9 governed** | No serving check recorded. |
| Agent BI asks Rill the nine questions | 9 governed | **9 of 9 returned their known answers** |
| Someone edits Rill's metric behind Agent BI's back | 9 governed | Last check: 9 of 9, 1 minute ago (no claim after it) |
| Agent BI asks Rill again | **9 governed** | **3 of 9 differ:** FINANCE_REPORTING_WH 2739 → 3012.9 |

A single AI trust score has to lie in that last row. The definition is still governed. The
system serving it is wrong. Agent BI says both, separately, because they are different facts.

## The two lines

**Definition.** Has this business meaning earned governance?
- *Verified*: an independent computation agrees with the definition. That computation is
  human-written SQL over the raw data, so the check never relies on the compiler it tests.
- *Governed*: the definition is verified, and a person explicitly accepted it. An agent can
  propose a definition; it cannot accept one on anyone's behalf.
- *Stale*: change the expression, or any upstream Rill model it depends on, and the old
  verification no longer counts. Accepted definitions are protected from silent rewrites.

**Serving.** What did Rill actually return?
- Agent BI re-runs the verified questions through Rill's Metrics SQL route and compares the
  results with the known answers.
- It records a dated receipt: the route, the user context, the comparison rule, and each
  question's outcome.
- "9/9" means nine comparisons happened and matched. Skipped and failed questions are never
  counted as matches.
- The newest check is always the one shown, even when it failed.

Each line states what it does not cover: other read paths and caches, other users, chart
rendering, and any time outside the check. If you want to know whether Rill is still right,
check again. Agent BI makes that one command.

## Deterministic where it counts

- The Agent BI CLI calls no language model.
- Known questions, and questions that resolve unambiguously against the semantic model, run
  deterministically.
- When interpretation is needed, an agent proposes a structured query. Agent BI validates it
  before anything executes.
- The model reasons. Software enforces.

## Built on Rill, not instead of it

**Rill owns:** models, joins, metrics views, Metrics SQL, dashboards and the runtime.

**Agent BI owns:** human acceptance, independent verification, staleness when meaning changes,
and serving receipts.

When an operator's Rill model joins several tables, Agent BI doesn't build another join
compiler. It binds its evidence to that model and notices when the model changes.

## Status

- Open source under Apache-2.0: [github.com/leebase/agent-bi](https://github.com/leebase/agent-bi).
- Runs locally on Rill v0.90.5 with SQLite data.
- Semantic definitions are stored in Snowflake semantic-view YAML. Carrying governance into
  Snowflake itself is the natural next step, built when a client needs it.

The question to ask any AI analytics product: **when your agent changes the definition of
revenue, or the runtime stops serving it correctly, who notices?**
