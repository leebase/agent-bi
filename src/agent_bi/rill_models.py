"""Rill models — the operator's model graph (architecture §5.13, M2/D430, D432).

This module is the **only** place the §5.13 rules 2–4 live: the closed supported form
(rule 2, per file and per closure), the determinism denylist (rule 3), dependency discovery
and the closure digest (rule 4), and the current digest of a registered model. agent-bi
never writes, rewrites, translates or regenerates a Rill model; the graph is treated as part
of the data, constrained, hashed into provenance and executed verbatim.

Rule 4 is not a SQL lineage parser. ``deps(M)`` is every *registered* model name that
appears as a whole word (case-insensitive, comments included) in ``M``'s file text. The
error direction is deliberate (frozen asymmetry): a false positive only causes an
unnecessary STALE; a missed dependency cannot happen because a model reads another model by
spelling its name, and would make an answer falsely governed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import duckdb

from agent_bi.config import CONFIG_FILENAME, load_config
from agent_bi.errors import validation_error
from agent_bi.hashing import sha256_canonical, sha256_text

MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

GENERATED_HEADER_PREFIX = "-- agent-bi: generated"

# Rule 2: Rill annotations (``-- @materialize`` and friends).
ANNOTATION_RE = re.compile(r"^\s*--\s*@", re.MULTILINE)

# Rule 2: templating is out of scope (documented limit, §5.13 rule 8).
TEMPLATING_RE = re.compile(r"\{\{|\{%")

# Rule 2: external readers are refused as a whole word, case-insensitive.
EXTERNAL_READERS = (
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "parquet_scan",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_text",
    "read_blob",
    "glob",
    "postgres_scan",
    "mysql_scan",
    "sqlite_attach",
    "iceberg_scan",
    "delta_scan",
    "read_xlsx",
)
EXTERNAL_READER_RE = re.compile(
    r"\b(?:" + "|".join(EXTERNAL_READERS) + r")\b", re.IGNORECASE
)

# Rule 3: nondeterministic constructs. Both lists grow only with a test.
VIEW_DENY_RE = re.compile(
    r"\b(?:CURRENT_DATE|CURRENT_TIME(?:STAMP)?|NOW|TODAY|RANDOM|RAND|UUID|GEN_RANDOM_UUID|"
    r"GETDATE|SYSDATE|LOCALTIME(?:STAMP)?|USING\s+SAMPLE|SAMPLE)\b",
    re.IGNORECASE,
)

_SCAN_NAME_RE = re.compile(r"\bsqlite_scan\b", re.IGNORECASE)
_SCAN_CALL_RE = re.compile(
    r"sqlite_scan\s*\(\s*'((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'\s*\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RillModelFacts:
    """Everything the per-file checks parsed from one operator model. ``sqlite_path`` and
    ``sqlite_abs`` are ``None`` for a model with no ``sqlite_scan`` call (a join model)."""

    name: str
    path: Path
    text: str
    sqlite_path: str | None
    sqlite_abs: Path | None
    has_sqlite_scan: bool
    tables: tuple[str, ...]


@dataclass(frozen=True)
class ClosureFacts:
    """The checked dependency closure of one registered model (rule 4 + closure rules)."""

    name: str
    members: tuple[str, ...]  # sorted closure, including ``name``
    facts: dict[str, RillModelFacts]  # per member
    direct: dict[str, tuple[str, ...]]  # per member: its direct dependencies
    sqlite_path: str
    sqlite_abs: Path


def model_path(rill_dir: str | Path, name: str) -> Path:
    return Path(rill_dir) / "models" / f"{name}.sql"


def rill_dir_for_config(project_dir: str | Path, project_dir_rel: str) -> Path:
    """The absolute Rill project directory: ``config.base_dir / config.rill.project_dir``."""
    return (Path(project_dir) / PurePosixPath(project_dir_rel)).resolve()


def rill_dir_from_source(project_dir: str | Path, source: str | None) -> Path:
    """The Rill project directory recovered from a sidecar ``relation.source`` such as
    ``rill/models/<name>.sql``; falls back to the default ``rill`` when there is none."""
    if source:
        rel = PurePosixPath(source)
        if len(rel.parts) >= 3 and rel.parts[-2] == "models":
            return (Path(project_dir) / Path(*rel.parts[:-2])).resolve()
    return (Path(project_dir) / "rill").resolve()


def source_rel(rill_project_dir_rel: str, name: str) -> str:
    """The sidecar ``relation.source`` value for a registered model (posix, project-relative)."""
    return f"{PurePosixPath(rill_project_dir_rel).as_posix()}/models/{name}.sql"


def registered_names_from_project(project_dir: str | Path) -> list[str]:
    """The registered Rill-model names from the project's ``agent-bi.yaml`` (§4.6)."""
    cfg = load_config(Path(project_dir) / CONFIG_FILENAME)
    return list(cfg.engines.duckdb.rill_models)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise validation_error(
            "UNSUPPORTED_VIEW_FORM", f"{path}: Rill model file does not exist (rule 2)"
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise validation_error(
            "UNSUPPORTED_VIEW_FORM", f"{path}: cannot read Rill model: {exc} (rule 2)"
        ) from exc


def _refuse(path: Path, reason: str) -> None:
    raise validation_error(
        "UNSUPPORTED_VIEW_FORM",
        f"{path}: outside the closed supported Rill-model form: {reason} (§5.13 rule 2)",
    )


def _parse_single_select(path: Path, text: str) -> None:
    try:
        con = duckdb.connect(":memory:")
        try:
            statements = con.extract_statements(text)
        finally:
            con.close()
    except duckdb.Error as exc:
        _refuse(path, f"DuckDB cannot parse it: {exc}")
        return  # unreachable; keeps type checkers happy
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        _refuse(path, "DuckDB must parse it as exactly one SELECT statement (WITH qualifies)")


def _json_walk(value: object) -> Iterator[dict]:
    """Every mapping node in DuckDB's ``json_serialize_sql`` output, depth first."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_walk(child)


def _serialized_sql(path: Path, text: str) -> dict:
    """Rule 2: parse the model with DuckDB's own parser (``json_serialize_sql``). A parse
    error, or output that is not a successful statement map, refuses."""
    con = duckdb.connect(":memory:")
    try:
        try:
            raw = con.execute("SELECT json_serialize_sql(?)", [text]).fetchone()[0]
        except duckdb.Error as exc:
            _refuse(path, f"DuckDB cannot parse it: {exc}")
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _refuse(path, f"DuckDB cannot serialize it as JSON: {exc}")
    finally:
        con.close()
    if not isinstance(parsed, dict) or parsed.get("error"):
        reason = parsed.get("error_message") if isinstance(parsed, dict) else None
        _refuse(path, f"DuckDB cannot parse it: {reason or 'unknown parse error'}")
    return parsed


def _check_relation_form(
    rill_dir: Path,
    path: Path,
    parsed: dict,
    candidates: tuple[str, ...] | list[str],
    attached: set[str] | None,
) -> None:
    """Rule 2 (A1): every table function is ``sqlite_scan``; every other relation is a
    registered/operator-owned model or a CTE, referenced by bare name. The CTE-vs-attached
    check runs where the attached catalogs are known (engine registration)."""
    ctes: set[str] = set()
    for node in _json_walk(parsed):
        cte_map = node.get("cte_map")
        if not isinstance(cte_map, dict):
            continue
        for entry in cte_map.get("map") or []:
            if isinstance(entry, dict) and entry.get("key"):
                ctes.add(str(entry["key"]).casefold())
    if attached:
        for cte in sorted(ctes):
            if cte in attached:
                _refuse(
                    path,
                    f"it defines the CTE {cte!r}, which is also a table or view in an "
                    "attached database; a bare reference could resolve to that raw relation",
                )
    allowed = {str(candidate).casefold() for candidate in candidates}
    for node in _json_walk(parsed):
        kind = node.get("type")
        if kind == "TABLE_FUNCTION":
            function = node.get("function")
            name = ""
            if isinstance(function, dict):
                name = str(function.get("function_name") or "")
            if name.casefold() != "sqlite_scan":
                _refuse(
                    path,
                    f"it calls the table function {name!r}; only sqlite_scan is allowed",
                )
        elif kind == "BASE_TABLE":
            catalog = str(node.get("catalog_name") or "")
            schema = str(node.get("schema_name") or "")
            relation = str(node.get("table_name") or "")
            if catalog or schema:
                qualified = ".".join(part for part in (catalog, schema, relation) if part)
                _refuse(
                    path,
                    f"it reads the qualified relation {qualified!r}; every relation must be "
                    "a registered model referenced by bare name",
                )
            if relation.casefold() in allowed or relation.casefold() in ctes:
                continue
            hint = ""
            if MODEL_NAME_RE.match(relation):
                existing = model_path(rill_dir, relation)
                if existing.is_file():
                    hint = (
                        f"; the file {existing} exists but is not in the candidate "
                        "model set"
                    )
            _refuse(
                path,
                f"it reads the relation {relation!r}, which is neither a registered model "
                f"referenced by bare name nor a CTE{hint}",
            )


def _scan_calls(path: Path, text: str) -> list[tuple[str, str]]:
    """Rule 2 per file: **at most one** ``sqlite_scan`` call, and it must be the literal
    two-single-quoted-argument form. Returns ``[(path_literal, table_literal), ...]``."""
    found = list(_SCAN_NAME_RE.finditer(text))
    if len(found) > 1:
        _refuse(
            path,
            f"it contains {len(found)} sqlite_scan calls; at most one is allowed "
            "(Rill v0.90.5 cannot build a model with two)",
        )
    if not found:
        return []
    m = _SCAN_CALL_RE.match(text, found[0].start())
    if m is None:
        _refuse(
            path,
            "every sqlite_scan call must pass a single-quoted path and table literal "
            f"(at offset {found[0].start()})",
        )
    return [(m.group(1).replace("''", "'"), m.group(2).replace("''", "'"))]


def check_model(
    rill_dir: str | Path,
    name: str,
    candidates: tuple[str, ...] | list[str] | None = None,
    attached: set[str] | None = None,
) -> RillModelFacts:
    """Rule 2 (per file) and rule 3 for one model file. Returns the parsed facts; raises
    ``E_UNSUPPORTED_VIEW_FORM`` (rule 2) or ``E_NONDETERMINISTIC_VIEW`` (rule 3) naming the
    file. ``candidates`` is the model-name set for the relation restriction (the same set
    the dependency rule uses); ``attached`` is the set of attached table/view names, known
    only at engine registration. Nothing is executed and nothing is written. Closure-level
    rule 2 checks live in :func:`check_closure`."""
    if not MODEL_NAME_RE.match(name or ""):
        raise validation_error(
            "UNSUPPORTED_VIEW_FORM",
            f"Rill model name {name!r} must match ^[a-z][a-z0-9_]*$ (§5.13 rule 2)",
        )
    rill_dir = Path(rill_dir)
    path = model_path(rill_dir, name)
    text = _read_text(path)
    if text.startswith(GENERATED_HEADER_PREFIX):
        _refuse(path, "it begins with the agent-bi generated header (not an operator model)")
    if ANNOTATION_RE.search(text):
        _refuse(path, "it contains a Rill annotation line (-- @…)")
    if TEMPLATING_RE.search(text):
        _refuse(path, "it contains templating ({{ or {%)")
    _parse_single_select(path, text)
    parsed = _serialized_sql(path, text)
    reader = EXTERNAL_READER_RE.search(text)
    if reader is not None:
        _refuse(path, f"it reads an external source with {reader.group(0)!r}")
    calls = _scan_calls(path, text)
    sqlite_path = sqlite_abs = None
    if calls:
        literal, _table = calls[0]
        resolved = Path(literal) if Path(literal).is_absolute() else rill_dir / literal
        sqlite_abs = resolved.resolve()
        if not sqlite_abs.is_file():
            _refuse(path, f"the SQLite path {literal!r} resolves to {sqlite_abs}, which is not a file")
        sqlite_path = literal
    _check_relation_form(
        rill_dir,
        path,
        parsed,
        candidates if candidates is not None else operator_model_names(rill_dir),
        attached,
    )
    denied = VIEW_DENY_RE.search(text)
    if denied is not None:
        raise validation_error(
            "NONDETERMINISTIC_VIEW",
            f"{path}: contains the nondeterministic construct {denied.group(0)!r} "
            "(§5.13 rule 3)",
        )
    return RillModelFacts(
        name=name,
        path=path,
        text=text,
        sqlite_path=sqlite_path,
        sqlite_abs=sqlite_abs,
        has_sqlite_scan=bool(calls),
        tables=tuple(table for _p, table in calls),
    )


def operator_model_names(rill_dir: str | Path) -> list[str]:
    """The candidate model names for rule 4 at ``init`` time: the operator-owned files in
    ``<rill dir>/models/`` (files without the agent-bi generated header)."""
    models = Path(rill_dir) / "models"
    names: list[str] = []
    if not models.is_dir():
        return names
    for path in sorted(models.glob("*.sql")):
        if not MODEL_NAME_RE.match(path.stem):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if text.startswith(GENERATED_HEADER_PREFIX):
            continue
        names.append(path.stem)
    return names


def _word_re(name: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)


def dependencies_of(
    rill_dir: str | Path, name: str, candidates: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """Rule 4: every candidate name ``N ≠ name`` that appears as a whole word
    (case-insensitive, comments included) in ``name``'s file text."""
    text = _read_text(model_path(rill_dir, name))
    return tuple(n for n in candidates if n != name and _word_re(n).search(text))


def _graph(
    rill_dir: str | Path, name: str, registered: tuple[str, ...] | list[str]
) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    """Dependency graph of ``name`` over ``registered``: ``(direct deps per node, finish
    order)``. Raises ``E_UNSUPPORTED_VIEW_FORM`` naming the cycle on a dependency cycle."""
    universe = tuple(dict.fromkeys(registered))
    direct: dict[str, tuple[str, ...]] = {}
    state: dict[str, int] = {}  # 0 = on the stack, 1 = finished
    stack: list[str] = []
    order: list[str] = []

    def visit(node: str) -> None:
        seen = state.get(node)
        if seen == 1:
            return
        if seen == 0:
            at = stack.index(node)
            cycle = stack[at:] + [node]
            raise validation_error(
                "UNSUPPORTED_VIEW_FORM",
                f"{model_path(rill_dir, node)}: dependency cycle in the Rill model graph: "
                + " -> ".join(cycle)
                + " (§5.13 rule 2)",
            )
        state[node] = 0
        stack.append(node)
        deps = dependencies_of(rill_dir, node, universe)
        for dep in deps:
            visit(dep)
        stack.pop()
        state[node] = 1
        direct[node] = deps
        order.append(node)

    visit(name)
    return direct, order


def closure_of(
    rill_dir: str | Path, name: str, registered: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """Rule 4: ``name`` plus the transitive closure of its dependencies over the registered
    names, sorted."""
    direct, _order = _graph(rill_dir, name, registered)
    return tuple(sorted(direct))


def check_closure(
    rill_dir: str | Path,
    name: str,
    registered: tuple[str, ...] | list[str],
    attached: set[str] | None = None,
) -> ClosureFacts:
    """Rules 2 (per file and per closure) and 3 over ``closure(name)``. Raises
    ``E_UNSUPPORTED_VIEW_FORM`` (naming the file or the cycle) or ``E_NONDETERMINISTIC_VIEW``
    (naming the file); nothing is executed and nothing is written."""
    direct, _order = _graph(rill_dir, name, registered)
    members = tuple(sorted(direct))
    facts = {
        member: check_model(rill_dir, member, candidates=registered, attached=attached)
        for member in members
    }
    scanned = {m: f for m, f in facts.items() if f.sqlite_abs is not None}
    root = model_path(rill_dir, name)
    if not scanned:
        _refuse(root, f"no model in the closure of {name!r} has a sqlite_scan call")
    distinct = {str(f.sqlite_abs) for f in scanned.values()}
    if len(distinct) > 1:
        named = ", ".join(f"{m} -> {f.sqlite_abs}" for m, f in sorted(scanned.items()))
        _refuse(
            root,
            f"the closure of {name!r} reads more than one SQLite file "
            f"(v1 reads exactly one): {named}",
        )
    first = next(iter(scanned.values()))
    assert first.sqlite_abs is not None
    return ClosureFacts(
        name=name,
        members=members,
        facts=facts,
        direct=direct,
        sqlite_path=first.sqlite_path or "",
        sqlite_abs=first.sqlite_abs,
    )


def _normalized_text(text: str) -> str:
    """Rule 4: CRLF/CR → LF, then strip trailing whitespace from every line."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n"))


def file_digest(path: str | Path) -> str:
    """Rule 4 per file: ``sha256`` of the file text after CRLF/CR → LF and stripping
    trailing whitespace from every line. Every other edit, comments included, changes it
    (fail closed)."""
    return sha256_text(_normalized_text(_read_text(Path(path))))


def models_digest(
    rill_dir: str | Path, name: str, registered: tuple[str, ...] | list[str]
) -> str:
    """Rule 4: ``sha256`` of the canonical JSON ``{<name>: <file digest>}`` over
    ``closure(name)``, with the registered name set supplying the candidate dependencies."""
    members = closure_of(rill_dir, name, registered)
    return sha256_canonical(
        {member: file_digest(model_path(rill_dir, member)) for member in members}
    )


def current_models_digest(
    rill_dir: str | Path, model_dict: object, registered: tuple[str, ...] | list[str]
) -> str | None:
    """The closure digest of the Rill model the model's ``base_table`` names, read from the
    files on disk now; ``None`` when ``base_table`` is not ``VIEWS.MAIN`` (a table model)."""
    model = model_dict.to_dict() if hasattr(model_dict, "to_dict") else model_dict
    assert isinstance(model, dict)
    table = (model.get("tables") or [{}])[0]
    base = table.get("base_table") or {}
    if str(base.get("database", "")).upper() != "VIEWS" or str(base.get("schema", "")).upper() != "MAIN":
        return None
    name = base.get("table")
    if not name:
        return None
    return models_digest(rill_dir, name, registered)
