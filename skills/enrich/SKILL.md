---
name: agent-bi-enrich
description: Propose richer descriptions, synonyms, metrics and queries for an agent-bi semantic model as an EnrichmentPatch JSON that carries no SQL (applied by `agent-bi enrich --apply`).
---

# Enrich a semantic model

You are the operator's harness agent. Read `semantic/<m>.yaml` and its sidecar
`semantic/<m>.provenance.yaml`, then write an **EnrichmentPatch** JSON
(`agent-bi-enrichment-patch/1.0`, architecture section 4.5) and apply it with:

```text
agent-bi enrich --model <m> --apply <patch.json>
```

A patch is one transaction: the whole patch is validated before anything is written, so a
later bad operation leaves every earlier one unwritten. The patch is at most 1 MB, its
`patch_id` is a UUID4, and its `model` is exactly the model you are enriching. Every
operation value forbids unknown keys: **a patch carries no SQL and no expected answer
anywhere, at any depth**. Never put a query's SQL or its expected result in a patch;
a query becomes verified only when the human runs
`agent-bi save --verify --expected-sql "<independent SQL>"` themselves.

Only four operations exist:

- `set_description` - `{path, value}` where `path` is a sidecar path such as
  `tables[0].dimensions[warehouse_name]`.
- `add_synonyms` - `{path, value: [..]}`; a synonym must be unique across the whole model
  and may not equal any object's name. A collision is refused with
  `E_SYNONYM_NOT_UNIQUE`.
- `add_metric` - `{path: "tables[0]", value: {name, expr, description}}`; `expr` must be one of
  the closed metric forms (`SUM|AVG|MIN|MAX("col")`, `COUNT(*)`, `COUNT(DISTINCT "col")`, or one
  such aggregate divided by `NULLIF(<aggregate>, 0)`), and the column must exist. If the
  name is already a metric, its `expr` and `description` are replaced and its synonyms and
  other fields are preserved; if the name belongs to any other object, the patch is refused
  with `E_NAME_COLLISION`. A bad expression is `E_PATCH_REJECTED` naming the operation
  index.
- `propose_query` - `{name, question, semantic_query}`: a SemanticQuery, **never SQL**. It
  writes `queries/<name>.json` with status `proposed` and touches nothing else; it creates
  no chart and no verification record. An existing name is refused. The human promotes it
  later with `save --verify --expected-sql`.

Every entry a patch changes becomes `inferred` with `origin: enrich:<patch_id>` and
`by: proposed_by`. Entries a human already confirmed are protected: without
`--allow-confirmed <exact leaf>` the patch is refused with `E_PATCH_TOUCHES_CONFIRMED`;
with that exact leaf the entry is rewritten and reverts to `inferred`. Never run
`enrich --accept` yourself: only the human owner accepts a definition (`--by` is
attribution, not authentication).

After a patch changes an expression, dependent verified queries become stale. Run
`agent-bi verify --model <m>`; it prints `STALE <name>` and exits 1. Do not re-verify the
query with the old oracle: ask the human for a corrected independent SQL, then run
`agent-bi save --verify --force --by <person> --expected-sql "<SQL>"` on the same name.
