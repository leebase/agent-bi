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
| `canvas fork --model MODEL --name NAME` | Create an operator-owned canvas. |
| `save --from ANSWER --name NAME [--question TEXT] [--add-to-canvas NAME]` | Keep a query and chart. |
| `save ... --verify --by PERSON --expected-sql SQL [--force]` | Independently verify the saved query; `--force` replaces an existing name. |

`ask` accepts exactly one of `--question` and `--query-file`. For the full flags use
`agent-bi <command> --help`.

## Exit codes used by the question workflow

| Code | Meaning / action |
|---|---|
| 0 | Successful execution or a resolved query. |
| 1 | Verification failure/staleness or another reported failure; inspect the message. |
| 2 | Validation/usage refusal; includes bad picks and ambiguous saved-question matches. |
| 3 | Engine unavailable or execution error. |
| 4 | `ask --question`: no verified/saved match; proceed to `resolve`. |
| 5 | `resolve`: ambiguous; show candidates and obtain a human pick. |
| 6 | `resolve`: unresolved; only now may an agent compose a SemanticQuery. |

Do not interpret a nonzero exit as an empty successful answer. Read the `E_...` message.
After a resolver refusal, do not run `ask` against a leftover file from an older question.
The complete [error catalog](errors.md) lists individual codes.

## Not implemented

`enrich --apply`, `diff`, `parity`, `snowflake-load`, and Snowflake query execution are
not part of the supported release. Some names appear in CLI help as reserved commands
and return `E_NOT_IMPLEMENTED` or an engine-unavailable refusal. The packaged
`agent-bi-enrich` skill describes planned work; use only human `enrich --accept` today.
