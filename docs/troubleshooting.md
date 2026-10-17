# Troubleshooting and limitations

## Command not found

Activate the venv in each terminal with `source .venv/bin/activate`. A harness may use
a separate shell; point it at `<checkout>/.venv/bin/agent-bi` if PATH is not inherited.
Installing the Python package alone does not install the repository's harness skills.

## Rill version or project mismatch

Use **v0.90.5** from the [pinned release](https://github.com/rilldata/rill/releases/tag/v0.90.5).
Check `rill --version` (or `~/.rill/rill --version`). Start it from your analysis project:
`rill start rill --no-open`. Agent BI's Rill verification checks the served project path
and version at port 9009. A different project correctly causes `E_ENGINE_UNAVAILABLE`.
Stop that server in its own terminal before starting the intended project.

Allow initial reconciliation to finish. If the page remains loading, inspect the Rill
terminal for source or component errors; a persistent loading screen is a failed check.
The Rill UI's AI button is a separate feature and is not the Agent BI skill.

## Missing config

`init` requires `agent-bi.yaml`; it does not create an analysis directory from nothing.
The checkout includes a working config. For your own workspace use the minimal config
in the [user guide](user-guide.md#bring-your-own-table).

## SQLite attachment fails on a view

DuckDB's SQLite attachment can inspect views unrelated to the requested table. SQLite
SQL that DuckDB cannot parse can prevent the entire attachment. A consumer project
encountered this with an alias named `method` without `AS` in an unrelated view.

For M1, extract the selected physical table into a separate SQLite database using your
data preparation tools, preserve its columns/rows, and point `init` at that file. Verify
the extract against the source and keep it read-only. Do not change production data or
drop unrelated views to make the demo run. Native view handling is future work.

## Why is a correct-looking answer exploratory?

A resolved question is not necessarily verified. A known answer must match the exact
semantic query, including filters. A filtered finance answer can remain exploratory
while the unfiltered warehouse breakdown is governed. Accepting definitions does not
create a verified query. `verified` and `governed` can also fall back to exploratory when
definition changes make their evidence stale.

The label describes the query that executed, not whether an agent correctly interpreted
every word. Review the interpretation and filters on composed answers. Receipts do not
yet record a dedicated verified/resolved/composed source field.

## Repeated ambiguity prompts or smart punctuation

The current resolver uses a closed grammar. Natural wording can require multiple picks;
smart quotes, em dashes, accents and unsupported characters can lead to composition.
Use explicit metric and dimension names for the deterministic path. Unsupported text
must not be silently discarded as if a qualifier were absent.

## A patch is refused

`enrich --apply` refuses the whole patch, writes nothing and names the failing operation.
Common `E_PATCH_REJECTED` causes: a key named `sql` or `expected_sql` anywhere in the
JSON; an `add_metric` expression outside the metric grammar or over a column the table
does not have; a path that is not `tables[0].<kind>[<name>]`; a missing or wrong model; a
non-UUID4 `patch_id`; a file over 1 MB; or an unknown key (every op value forbids extra
keys). A name already owned by a non-metric object gives `E_NAME_COLLISION`, and a
duplicate synonym gives `E_SYNONYM_NOT_UNIQUE`. Check the operation index in the message;
the patch is atomic, so nothing from earlier operations was kept.

## A confirmed entry blocks a patch

An operation that rewrites an entry a person confirmed is refused with
`E_PATCH_TOUCHES_CONFIRMED` unless `--allow-confirmed` names that exact leaf path. A
parent path, wildcard or near match does not work. With the exact leaf the entry is
rewritten, reverts to `inferred` and is attributed to the patch's `proposed_by`; any
verified query whose referenced expression changed becomes stale. Re-accept it after
independent re-verification.

## A query went STALE after enrichment

A changed metric expression invalidates the semantic digest of every query that
references it. `verify` prints `STALE <name>: <changed definitions>` and exits 1, and
`ask` labels the query `exploratory` with the reason. Re-running the old expected SQL is
correctly refused (`E_VERIFY_MISMATCH`): write a corrected independent SQL against the
new definition, then run `save --verify --force --by <person> --expected-sql "<SQL>"` on
the same name. Description, synonym and status changes alone do not stale verification.

A registered Rill model graph works the same way. Its closure digest — the sha256 of the
canonical `{name: file digest}` map over the model and every model upstream of it
(CRLF and trailing whitespace ignored) — is part of the semantic digest, so editing the
join model or any upstream source model makes every verification of the top model `STALE`
and names `models_digest`; changing an unrelated registered model does not. Clear it by
re-running the independent route for the same query names; restoring the original bytes
also restores `PASS` while the sidecar still records the old digest. Note that dependency
discovery matches registered model names as whole words in the file text, comments
included, so mentioning another model's name in a comment makes it a dependency and its
changes `STALE` your model. That false-positive-only direction is deliberate: it is
cheaper to re-verify than to miss a dependency.

## A Rill model is refused

A relation backed by an operator Rill SQL model must stay inside the closed form
(§5.13 rule 2, checked per file and per dependency closure). `E_UNSUPPORTED_VIEW_FORM`
names the file and the failed rule. The usual causes:

- a Rill annotation line (`-- @materialize: …`), templating (`{{`/`{%`), or a file that
  begins with Agent BI's generated header (a generated table model is not an operator
  model);
- DuckDB cannot parse it as exactly one `SELECT` statement (a `CREATE`, a second
  statement, a syntax error);
- a `sqlite_scan` call that is not `sqlite_scan('<path>', '<table>')` with single-quoted
  literals, more than one `sqlite_scan` call in one file (Rill v0.90.5 cannot build
  that), no `sqlite_scan` anywhere in the closure, more than one distinct SQLite file
  across the closure, or a path that does not resolve to an existing file against the
  Rill project directory;
- a dependency cycle (the message names the cycle);
- another reader such as `read_csv`, `read_parquet`, `postgres_scan` or `glob`;
- a reference to an unregistered model or to a generated table model, or to a relation
  that does not exist (a misspelled table); the view cannot be created.

Nondeterministic constructs give `E_NONDETERMINISTIC_VIEW`: `CURRENT_DATE`,
`CURRENT_TIME(STAMP)`, `NOW`, `TODAY`, `RANDOM`, `RAND`, `UUID`, `GEN_RANDOM_UUID`,
`GETDATE`, `SYSDATE`, `LOCALTIME(STAMP)`, `SAMPLE` and `USING SAMPLE`. Both rules run at
every engine start for every registered model and in `validate`; nothing is written on a
refusal.

A model that fails is unavailable only for its own closure: commands that use it raise
the recorded error naming the failing file, while every relation outside that closure
still opens, validates, verifies and answers (D431). `validate --model <view model>`
opens the DuckDB engine and fails when any model in the closure does not build —
including a view-creation failure such as a column an upstream model selects being
dropped — so a view model never validates OK while its graph is broken.

Rill annotations and templating, YAML, incremental or partitioned models, more than one
`sqlite_scan` per file, more than one SQLite file per closure, dependency cycles,
non-SQLite sources, references to unregistered or generated models and Snowflake
materialization are documented limits, not bugs. Split the work into a supported graph
or prepare the data ahead of the model; Agent BI never approximates or rewrites it.

## compile-rill refuses a table model (`E_OPERATOR_FILE`)

`compile-rill` owns `rill/models/<model>.sql` for table models and refuses to overwrite a
file there that does not begin with the generated header. That file is operator-owned,
so renaming it (and updating any references) or moving it outside `rill/models/` is the
operator's decision. If you meant to consume the file as a registered view model, run
`agent-bi init --rill-model <name>` instead; Agent BI then never writes it.

## diff exits 1 but wrote nothing

That is the contract: `diff` is a read-only preview of `scan --refresh`. Exit 1 means a
structural change (columns or semantic objects) or accepted content that a refresh would
break; exit 0 with a row-count line means the data moved but the structure did not.
Resolve structural drift with `scan --refresh` or by editing the view/model, then
re-check. `diff` never modifies the fixture, model, sidecar or outputs.

## Display and provenance details

- The default canvas uses simple inferred KPIs and row counts; it is not a designed
  business narrative. Saved charts currently lack a title.
- The dashboard formats values (for example 4200.96 appears as 4,201). Inspect answer
  JSON/SQL or known-answer output for exact stored results.
- Saved daily charts can display dates in the browser's local time; UTC midnight may
  appear on the previous local date.
- The build manifest reflects the last `compile-rill`. Re-run it after accepting a
  definition or promoting a verified query.
- Agent BI verifies Rill's all-time, unfiltered results. A dashboard filter or time-range
  change is outside that verified view.
- `--by` is attribution, not authentication. Human acceptance relies on the operator's
  workflow and environment permissions.
- Exceptional filesystem failures can leave stale output or a traceback. Check the exit
  code and the question/query in an artifact before reusing it.

For a bug report, include the Agent BI/Rill/Python versions, command, exit code and
sanitized error output. Do not upload credentials, private datasets or answer files
containing confidential rows to a public issue.
