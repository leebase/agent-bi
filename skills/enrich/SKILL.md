---
name: agent-bi-enrich
description: Propose richer descriptions, synonyms and metrics for an agent-bi semantic model as an EnrichmentPatch JSON that carries no SQL (applied by `agent-bi enrich --apply`, Sprint 5).
---

# Enrich a semantic model

> `enrich --apply` ships in Sprint 5; this skill is installed now so the packaging is final.
> Until then only `agent-bi enrich --accept` exists, and that is the human's decision.

You are the operator's harness agent. Read `semantic/<m>.yaml` and its sidecar
`semantic/<m>.provenance.yaml`, then write an **EnrichmentPatch** JSON
(`agent-bi-enrichment-patch/1.0`, architecture section 4.5) and apply it with
`agent-bi enrich --model <m> --apply <patch.json>`.

Only four operations exist:

- `set_description` - `{path, value}` where `path` is a sidecar path such as
  `tables[0].dimensions[warehouse_name]`.
- `add_synonyms` - `{path, value: [..]}`; a synonym must be unique across the whole model.
- `add_metric` - `{path: "tables[0]", value: {name, expr, description}}`; `expr` must be one of
  the closed metric forms (`SUM|AVG|MIN|MAX("col")`, `COUNT(*)`, `COUNT(DISTINCT "col")`, or one
  such aggregate divided by `NULLIF(<aggregate>, 0)`).
- `propose_query` - `{name, question, semantic_query}`: a SemanticQuery, **never SQL**.

**A patch carries no SQL and no expected answer.** Never put a query's SQL or its expected
result in a patch; a query becomes verified only when the human runs
`agent-bi save --verify --expected-sql "<independent SQL>"` themselves. Entries that a human
already confirmed are protected; do not try to overwrite them. Never run `enrich --accept`.
