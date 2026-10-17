"""Domain errors (architecture §9). Every error prints ``E_<CODE>: <message>``.

``CATALOGUE`` is the single authoritative list of codes: code -> (exit code, meaning).
``docs/errors.md`` is generated from it by ``python -m agent_bi.errors --doc``.
"""

from __future__ import annotations

import sys

EXIT_VALIDATION = 2
EXIT_ENGINE = 3
EXIT_NO_MATCH = 4  # ``ask --question`` found no verified or saved query (a normal outcome)
EXIT_AMBIGUOUS = 5  # ``resolve`` found more than one valid binding (a normal outcome, §5.15)
EXIT_UNRESOLVED = 6  # ``resolve`` left the closed grammar (a normal outcome, §5.15)

CATALOGUE: dict[str, tuple[int, str]] = {
    "E_ACCEPT_UNCOVERED": (2, "enrich --accept refused to confirm a metric that no current verified query references (§4.5)."),
    "E_AMBIGUOUS_AS_OF": (2, "More than one database is attached and no alias says which one's file digest is the as-of."),
    "E_AMBIGUOUS_QUESTION": (2, "More than one verified or saved query has this normalized question; the message lists the names (§5.8)."),
    "E_BAD_CHOICE": (2, "resolve --choose was refused (§5.15 rule 10): malformed, conflicting values, unknown id, or not a shown candidate; nothing is written to stdout and any existing out/resolution.json is removed."),
    "E_CANVAS_EXISTS": (2, "canvas fork found an operator-owned canvas with that name; it is never overwritten (ADR-009)."),
    "E_CANVAS_NOT_FOUND": (2, "The generated default canvas does not exist; run compile-rill first."),
    "E_CANVAS_NOT_VERIFIED_SCOPE": (2, "The target canvas's default time range is not inf or it declares default or required filters, so a saved chart there would not show the verified view (ADR-008)."),
    "E_CHART_INCOMPATIBLE": (2, "The requested mark does not fit the answer's shape, or the answer has no chart (§5.9)."),
    "E_CONFIG_INVALID": (2, "agent-bi.yaml (or a value in it) is malformed."),
    "E_CONFIG_NOT_FOUND": (2, "The project config file does not exist."),
    "E_ENGINE": (3, "The engine reported an error; its message is appended verbatim."),
    "E_ENGINE_UNAVAILABLE": (3, "The requested engine is not configured or not available in this build."),
    "E_GENERATED_FILE": (2, "The target is a file agent-bi generates and overwrites (ADR-009); operator files are created by canvas fork."),
    "E_INVALID_CANVAS_NAME": (2, "A canvas name does not match ^[a-z][a-z0-9_]*$."),
    "E_INVALID_MODEL_NAME": (2, "A model name does not match ^[a-z][a-z0-9_]*$."),
    "E_INVALID_RELATION": (2, "A relation reference is not DATABASE.SCHEMA.NAME."),
    "E_MODEL_EXISTS": (2, "scan without --refresh found an existing model; nothing was written."),
    "E_MODEL_INVALID": (2, "The semantic model YAML does not parse or violates the supported subset."),
    "E_MODEL_NOT_FOUND": (2, "The semantic model file does not exist."),
    "E_NAME_COLLISION": (2, "Two objects share one name in the model's single global namespace (§4.1.1 rule 2); the message names both."),
    "E_NAME_EXISTS": (2, "save found an existing query or component with that name; it is never overwritten without --force (ADR-009)."),
    "E_NOT_IMPLEMENTED": (2, "The command is registered but not implemented yet."),
    "E_NONDETERMINISTIC_VIEW": (2, "A registered Rill model contains a nondeterministic construct (§5.13 rule 3); nothing is executed."),
    "E_NOT_READONLY": (2, "The independent-route program (§5.6) accepts exactly one SELECT or WITH statement; save --verify maps this to E_STATEMENT_NOT_ALLOWED."),
    "E_NO_INDEPENDENT_SQL": (2, "save --verify needs --expected-sql: a verified query is never created from a single route (§5.10)."),
    "E_OPERATOR_FILE": (2, "compile-rill refuses to overwrite an operator-owned rill/models/<model>.sql that lacks the generated header (ADR-009, §5.13 rule 7)."),
    "E_NO_VERIFIED_MATCH": (4, "ask --question found no verified or saved query with that normalized question (§5.8); a normal outcome the skill branches on."),
    "E_PARTIAL_PUBLISH": (3, "A multi-file publish failed midway (§5.14); the files already replaced are listed and the staged files are kept. validate reports the inconsistency."),
    "E_PATCH_REJECTED": (2, "The whole enrichment patch was refused before anything was written: shape, size, UUID, model, an unknown op, a key named sql/expected_sql, or an add_metric expr outside the metric grammar (the message lists the failing operation index)."),
    "E_PATCH_TOUCHES_CONFIRMED": (2, "An enrichment patch would rewrite a confirmed entry without --allow-confirmed matching that exact leaf path; nothing was written."),
    "E_PROVENANCE_MISMATCH": (2, "The sidecar and the model disagree (missing entry, expr_sha256, or verified_queries)."),
    "E_QUERY_INVALID": (2, "A SemanticQuery violates the contract (§4.3): shape, enum, arity, size or limit."),
    "E_REFRESH_BREAKS_ACCEPTED": (2, "scan --refresh would leave accepted or operator-owned metadata without its physical dependency; nothing was written."),
    "E_RESOLVE_AMBIGUOUS": (5, "resolve found more than one valid binding and refused to choose; the candidates are in out/resolution.json (§5.15); a normal outcome the skill branches on."),
    "E_RESOLVE_UNRESOLVED": (6, "resolve met a word or construct outside its closed grammar; the reasons are in out/resolution.json (§5.15); a normal outcome the skill branches on."),
    "E_RELATION_NOT_FOUND": (2, "The relation does not exist in the catalog."),
    "E_RELATION_NOT_MAPPED": (2, "No relation_map entry resolves the model's DATABASE.SCHEMA for this engine."),
    "E_SNAPSHOT_CHANGED": (3, "The data file's digest changed between the before and after reads of a scan or query."),
    "E_SQL": (3, "The independent-route program (§5.6) could not execute the supplied SQL on the read-only SQLite fixture; save --verify maps this to E_VERIFY_MISMATCH."),
    "E_STATEMENT_NOT_ALLOWED": (2, "Engine.execute accepts exactly one SELECT or WITH statement."),
    "E_SYNONYM_NOT_UNIQUE": (2, "An add_synonyms value is already used as a synonym or object name in this semantic model; the message names both paths."),
    "E_THEME_NOT_FOUND": (2, "The named chart theme file themes/<name>.json does not exist (§5.9)."),
    "E_UNKNOWN_ALIAS": (2, "No attached database has that alias."),
    "E_UNKNOWN_ENGINE": (2, "The engine or dialect name is not one of duckdb, snowflake, rill."),
    "E_UNKNOWN_FIELD": (2, "A SemanticQuery names a field the model does not define; the message lists the valid names."),
    "E_UNSUPPORTED_BASE_TABLE_DEFINITION": (2, "base_table.definition has no physical identity in v1 (ADR-011)."),
    "E_UNSUPPORTED_BY_RILL_ROUTE": (2, "The query cannot be expressed as Rill Metrics SQL (like filter, named filter, or no metric); it is still answerable on duckdb and snowflake."),
    "E_UNSUPPORTED_DIMENSION_EXPR": (2, "A dimension or time dimension expr is outside the closed grammar (§5.3.5)."),
    "E_UNSUPPORTED_FACT_EXPR": (2, "A fact expr is not a bare quoted column."),
    "E_UNSUPPORTED_FILTER_EXPR": (2, "A named filter expr is outside the closed grammar (§5.3.5)."),
    "E_UNSUPPORTED_METRIC_EXPR": (2, "A metric expr is outside the closed grammar (§5.3.2), or names a column that is not a fact or dimension column."),
    "E_UNSUPPORTED_MULTI_TABLE": (2, "v1 compiles over exactly one table."),
    "E_UNSUPPORTED_RELATIONSHIPS": (2, "relationships[] must be absent in v1."),
    "E_UNSUPPORTED_VIEW_FORM": (2, "A Rill model is outside the closed supported form (§5.13 rule 2) or DuckDB cannot create its view; nothing is executed."),
    "E_VERIFY_MISMATCH": (2, "The compiled query and the operator-supplied independent SQL disagree; save wrote nothing (§5.10)."),
}


def catalogue_doc() -> str:
    """The text of ``docs/errors.md``: generated, never hand-edited."""
    lines = [
        "# Error codes",
        "",
        "Generated by `python -m agent_bi.errors --doc` from `agent_bi.errors.CATALOGUE`;",
        "do not edit by hand. Every error prints `E_<CODE>: <message>` as the last line of stderr.",
        "Exit codes: 2 validation or contract error (user-fixable); 3 engine or connection error;",
        "4 no verified match (`ask --question` only: a normal outcome, not a failure);",
        "5 ambiguous and 6 unresolved (`resolve` only: normal outcomes, not failures).",
        "",
        "| Code | Exit | Meaning |",
        "|---|---|---|",
    ]
    for code in sorted(CATALOGUE):
        exit_code, meaning = CATALOGUE[code]
        lines.append(f"| `{code}` | {exit_code} | {meaning} |")
    return "\n".join(lines) + "\n"


class AgentBiError(Exception):
    """Base class: ``code`` is the bare code without the ``E_`` prefix."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_VALIDATION):
        super().__init__(message)
        code = code.removeprefix("E_")
        self.code = code
        self.message = message
        self.exit_code = exit_code

    @property
    def full_code(self) -> str:
        return f"E_{self.code}"

    def __str__(self) -> str:
        return f"{self.full_code}: {self.message}"


def validation_error(code: str, message: str) -> AgentBiError:
    return AgentBiError(code, message, EXIT_VALIDATION)


def engine_error(code: str, message: str) -> AgentBiError:
    return AgentBiError(code, message, EXIT_ENGINE)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--doc"]:
        sys.stdout.write(catalogue_doc())
        return 0
    print("usage: python -m agent_bi.errors --doc", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
