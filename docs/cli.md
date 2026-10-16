# CLI reference — 0.1.0

Activate the installation venv. If a harness does not inherit PATH, use the full path
to its `bin/agent-bi`. Run from the analysis project's root, or put
`--config /path/to/agent-bi.yaml` **before** the subcommand.

## Supported commands

| Command | Use |
|---|---|
| `init --sqlite FILE --table TABLE --model MODEL` | Attach a SQLite table, scan/refresh its model, generate Rill files. Requires config. |
| `scan --engine duckdb --relation DB.SCHEMA.TABLE --model MODEL [--refresh]` | Scan an existing configured attachment. |
| `validate --model MODEL` | Validate semantic definitions and provenance consistency. |
| `compile-rill --model MODEL` | Generate Rill files and refresh the build manifest. |
| `compile-sql --model MODEL --query-file FILE --engine duckdb\|rill\|snowflake` | Print compiled SQL; a Snowflake dialect output is not Snowflake execution support. |
| `contract --print` | Print the SemanticQuery contract. |
| `ask --model MODEL --question TEXT` | Answer a matching saved/verified question. |
| `ask --model MODEL --query-file FILE [--json]` | Execute a SemanticQuery, saved query, known-answer file or resolved result. |
| `resolve --model MODEL --question TEXT [--choose ID=VALUE ...]` | Resolve deterministically, or return ambiguities/unresolved words. |
| `verify --model MODEL [--engine duckdb\|rill] [--coverage]` | Check existing known answers and report stale verification. |
| `enrich --model MODEL --accept PATH --by PERSON` | Attribute a human acceptance of a covered definition. |
| `enrich --model MODEL --apply PATCH [--allow-confirmed PATH]...` | Apply a whole-patch validated EnrichmentPatch in one transaction; SQL-free by construction. |
| `diff --model MODEL --engine duckdb\|snowflake` | Read-only refresh preview: columns, row count, semantic objects and anything a refresh would break. |
| `canvas fork --model MODEL --name NAME` | Create an operator-owned canvas. |
| `save --from ANSWER --name NAME [--question TEXT] [--add-to-canvas NAME]` | Keep a query and chart. |
| `save ... --verify --by PERSON --expected-sql SQL [--force]` | Independently verify the saved query; `--force` replaces an existing name. |

`ask` accepts exactly one of `--question` and `--query-file`. For the full flags use
`agent-bi <command> --help`.

## Exit codes used by the question workflow

| Code | Meaning / action |
|---|---|
| 0 | Successful execution or a resolved query. |
| 1 | Verification failure/staleness or reported drift; inspect the message. |
| 2 | Validation/usage refusal; includes bad picks and ambiguous saved-question matches. |
| 3 | Engine unavailable or execution error. |
| 4 | `ask --question`: no verified/saved match; proceed to `resolve`. |
| 5 | `resolve`: ambiguous; show candidates and obtain a human pick. |
| 6 | `resolve`: unresolved; only now may an agent compose a SemanticQuery. |

Do not interpret a nonzero exit as an empty successful answer. Read the `E_...` message.
After a resolver refusal, do not run `ask` against a leftover file from an older question.
The complete [error catalog](errors.md) lists individual codes.

## Enrichment patches

`enrich --apply` accepts only `agent-bi-enrichment-patch/1.0` JSON. Four operations exist
(`set_description`, `add_synonyms`, `add_metric`, `propose_query`); the whole patch is
validated before one write transaction, no patch may contain a `sql`/`expected_sql` key at
any depth, and no operation deletes anything. `add_metric` creates a metric or rewrites
the `expr`/`description` of the metric named in its value, preserving other fields.
Entries a person confirmed are protected; `--allow-confirmed` must name the exact leaf
(such as `tables[0].metrics[total_credits_used]`) or the patch is refused with
`E_PATCH_TOUCHES_CONFIRMED`. `propose_query` writes only `queries/<name>.json` with status
`proposed`; promotion to `verified` still requires a human `save --verify --expected-sql`.

`diff` re-runs discovery and previews exactly what `scan --refresh` would change. It is
read-only: it writes no file. Structural drift (added/removed/type-changed columns, or a
refresh that would add, update, remove or break a semantic object) exits 1; a row-count
change alone is reported and exits 0.

## Not implemented

`parity`, `snowflake-load`, and Snowflake query execution are not part of the supported
release. Some names appear in CLI help as reserved commands and return
`E_NOT_IMPLEMENTED` or an engine-unavailable refusal.
