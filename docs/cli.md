# CLI reference — 0.1.0

Activate the installation venv. If a harness does not inherit PATH, use the full path
to its `bin/agent-bi`. Run from the analysis project's root, or put
`--config /path/to/agent-bi.yaml` **before** the subcommand.

## Supported commands

| Command | Use |
|---|---|
| `init --sqlite FILE --table TABLE --model MODEL` | Attach a SQLite table, scan/refresh its model, generate Rill files. Requires config. |
| `init --rill-model NAME [--model MODEL]` | Register an operator Rill SQL model and its dependency closure (single-table `sqlite_scan` source models plus a join model), attach the graph's SQLite file, scan/refresh the named model and generate Rill files. `--model` defaults to `NAME`. |
| `scan --engine duckdb --relation DB.SCHEMA.TABLE --model MODEL [--refresh]` | Scan an existing configured attachment. |
| `validate --model MODEL` | Validate semantic definitions and provenance consistency. |
| `compile-rill --model MODEL` | Generate Rill files and refresh the build manifest. |
| `compile-sql --model MODEL --query-file FILE --engine duckdb\|rill\|snowflake` | Print compiled SQL; a Snowflake dialect output is not Snowflake execution support. |
| `contract --print` | Print the SemanticQuery contract. |
| `ask --model MODEL --question TEXT` | Answer a matching saved/verified question. |
| `ask --model MODEL --query-file FILE [--json]` | Execute a SemanticQuery, saved query, known-answer file or resolved result. |
| `resolve --model MODEL --question TEXT [--choose ID=VALUE ...]` | Resolve deterministically, or return ambiguities/unresolved words. |
| `verify --model MODEL [--engine duckdb\|rill] [--coverage]` | Check existing known answers and report stale verification. With `--engine rill` it also writes a serving receipt to `out/serving/<model>/` and prints `serving receipt: <path> (<state>)` last. |
| `status --model MODEL` | Read-only. Print the `Definition:` and `Serving:` lines (latest receipt in `out/serving/<model>/`), the `Not covered by that check` lines and the exclusions line. Writes nothing, never calls Rill, exits 0. |
| `enrich --model MODEL --accept PATH --by PERSON` | Attribute a human acceptance of a covered definition. |
| `enrich --model MODEL --apply PATCH [--allow-confirmed PATH]...` | Apply a whole-patch validated EnrichmentPatch in one transaction; SQL-free by construction. |
| `diff --model MODEL --engine duckdb\|snowflake` | Read-only refresh preview: columns, row count, semantic objects and anything a refresh would break. |
| `canvas fork --model MODEL --name NAME` | Create an operator-owned canvas. |
| `save --from ANSWER --name NAME [--question TEXT] [--add-to-canvas NAME]` | Keep a query and chart. |
| `save ... --verify --by PERSON --expected-sql SQL [--force]` | Independently verify the saved query; `--force` replaces an existing name. |

`ask` prints `Definition:`, `Serving:` and the exclusions line instead of a single trust label. Its
envelope is `agent-bi-answer/2.0`: `receipt.trust` and `receipt.trust_reason` are fixed
`REMOVED in agent-bi-provenance-record/2.0 ...` tombstone strings (not trust states); read
`receipt.evidence` (`definition`, `serving`). `ask` reads receipts only and never calls Rill.

`verify --engine rill` exit codes: 0 when every query matched or was skipped as unsupported; 1 on
any `MISMATCH` or `STALE` query; 3 when any query could not be asked (`E_ENGINE`,
`E_ENGINE_UNAVAILABLE`). A receipt is written in every such case. A stored `query_hash` that
disagrees with the stored `semantic_query` is refused before the run starts (`E_PROVENANCE_MISMATCH`,
exit 2, naming the query); no query is sent and no receipt is written.

`ask` accepts exactly one of `--question` and `--query-file`. For the full flags use
`agent-bi <command> --help`.

`init --rill-model` registers the operator's `rill/models/<name>.sql` **and every model in its dependency closure** (transitive, case-insensitive whole-word name matches: over the operator-owned files at `init`, over the registered names at engine start); Agent BI never writes, rewrites or translates any of those files. `compile-rill` for a view model writes the metrics view, explore, default canvas and manifest and no `rill/models` file; for a table model it refuses (`E_OPERATOR_FILE`) to overwrite an existing `rill/models/<model>.sql` without the generated header. The closed form is checked at every engine start and by `validate`, per file and per closure: refusals are `E_UNSUPPORTED_VIEW_FORM` (annotations, templating, non-SELECT, multiple statements, external readers, non-literal or multiple `sqlite_scan` calls, missing file, generated header, more than one SQLite file in the closure, a dependency cycle, an unresolvable relation, or a view that DuckDB cannot create) and `E_NONDETERMINISTIC_VIEW` (`CURRENT_DATE`, `NOW`, `RANDOM`, `SAMPLE` and the other denied words). A model that fails only its own closure refuses only its own commands; other relations keep working (D431).

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
