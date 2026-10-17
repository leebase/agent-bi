"""Engines (architecture §2, §4.4, §4.6, §9).

``Engine.execute()`` runs read statements only (``SELECT``/``WITH``, one statement).
Attach and session settings are internal methods and never go through ``execute()``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import duckdb

from agent_bi import rill_models
from agent_bi.config import Config
from agent_bi.errors import AgentBiError, engine_error, validation_error
from agent_bi.hashing import sha256_file

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LEADING_NOISE_RE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)", re.DOTALL)
_FIRST_WORD_RE = re.compile(r"[A-Za-z_]+")


def _json_nodes(value: object) -> Iterator[dict]:
    """Every mapping node in DuckDB's ``json_serialize_sql`` output, depth first."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_nodes(child)


@dataclass(frozen=True)
class AsOf:
    kind: str  # "file-digest" | "snowflake-time-travel"
    value: str
    retention_note: str | None = None


class Engine(Protocol):
    name: str

    def execute(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]: ...

    def version(self) -> str: ...

    def as_of(self, alias: str | None = None) -> AsOf: ...

    def engine_settings(self) -> dict[str, Any]: ...


def first_keyword(sql: str) -> str:
    """First SQL keyword of ``sql``, upper-case, skipping whitespace and comments."""
    rest = sql
    while True:
        m = _LEADING_NOISE_RE.match(rest)
        if not m:
            break
        rest = rest[m.end():]
    m = _FIRST_WORD_RE.match(rest)
    return m.group(0).upper() if m else ""


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _topological_order(nodes: list[str], direct: dict[str, tuple[str, ...]]) -> list[str]:
    """Deterministic topological order of ``nodes``: dependencies first, ties by name."""
    remaining = sorted(nodes)
    ordered: list[str] = []
    done: set[str] = set()
    while remaining:
        ready = next(
            (n for n in remaining
             if all(d in done or d not in remaining for d in direct.get(n, ()))),
            None,
        )
        if ready is None:  # a cycle would have been refused already; keep both engines safe
            ordered.extend(remaining)
            break
        ordered.append(ready)
        remaining.remove(ready)
        done.add(ready)
    return ordered


class DuckDBEngine:
    name = "duckdb"

    def __init__(self, config: Config):
        self._config = config
        duck = config.engines.duckdb
        self._session = duck.session
        self._attached: dict[str, tuple[Path, str]] = {}
        self._attach_failures: dict[str, tuple[Path, AgentBiError]] = {}
        self._configured_paths: dict[str, str] = {}
        self._rill_dir = rill_models.rill_dir_for_config(
            config.base_dir, config.rill.project_dir
        )
        self._rill_models: dict[str, rill_models.RillModelFacts] = {}
        self._unavailable_rill_models: dict[str, AgentBiError] = {}
        self._registered_names: list[str] = []
        self._rill_closures: dict[str, tuple[str, ...]] = {}
        self._rill_sqlite: dict[str, Path] = {}
        self._rill_cwd_active = False
        try:
            if duck.database == ":memory:":
                self._conn = duckdb.connect(":memory:")
            else:
                db_path = Path(duck.database)
                if not db_path.is_absolute():
                    db_path = config.base_dir / db_path
                self._conn = duckdb.connect(str(db_path), read_only=True)
        except duckdb.Error as exc:
            raise engine_error("ENGINE", f"cannot open duckdb database: {exc}") from exc
        self._apply_session()
        for att in duck.attach:
            try:
                self._attach(att.alias, att.path, att.type)
            except AgentBiError as exc:
                if exc.full_code != "E_ENGINE":
                    raise
                # D432/A2: a missing/unusable source is recorded per alias and never
                # blocks the engine from opening or from serving other relations.
                failed_path = Path(att.path)
                if not failed_path.is_absolute():
                    failed_path = config.base_dir / failed_path
                self._attach_failures[att.alias] = (failed_path, exc)
        self._register_rill_models(duck.rill_models)

    # -- internal (never reachable through execute()) -------------------------------

    @property
    def rill_dir(self) -> Path:
        """The absolute Rill project directory (§5.13 rule 5)."""
        return self._rill_dir

    @property
    def rill_rel(self) -> str:
        """The Rill project directory exactly as written in the config, posix."""
        return PurePosixPath(self._config.rill.project_dir).as_posix()

    @property
    def registered_rill_models(self) -> list[str]:
        """The registered Rill-model names exactly as listed in the config (§5.13 rule 1)."""
        return list(self._registered_names)

    def rill_closure(self, name: str) -> tuple[str, ...]:
        """The checked dependency closure of a registered model (§5.13 rule 4)."""
        return self._rill_closures.get(name, (name,))

    @contextlib.contextmanager
    def _rill_cwd(self) -> Iterator[None]:
        """DuckDB resolves ``sqlite_scan`` paths against the process working directory, so
        while a Rill model view is available every executed statement runs with the working
        directory set to the absolute Rill project directory, restored afterwards. The
        context is active only when at least one model view was actually created and the
        Rill directory exists (D432/A2); a missing Rill directory never touches table
        work."""
        if not self._rill_cwd_active:
            yield
            return
        old = os.getcwd()
        os.chdir(self._rill_dir)
        try:
            yield
        finally:
            os.chdir(old)

    def _internal(self, sql: str) -> list[tuple[Any, ...]]:
        with self._rill_cwd():
            try:
                return self._conn.execute(sql).fetchall()
            except duckdb.Error as exc:
                raise engine_error("ENGINE", str(exc)) from exc

    def _register_rill_models(self, names: list[str]) -> None:
        """§5.13 rules 2/3/5 with failure isolation (D431): check every registered model
        over its dependency closure (rule 4), then create the views verbatim in topological
        order (ties by name). A model that fails a rule or view creation — itself or through
        a model in its closure — is recorded as unavailable together with the error naming
        the failing file; it never prevents the engine from opening or from serving any
        relation outside the broken closure."""
        registered = list(dict.fromkeys(names))
        self._registered_names = registered
        self._rill_cwd_active = False
        attached_relations = self._attached_relation_names()
        facts_by_name: dict[str, rill_models.RillModelFacts] = {}
        direct_all: dict[str, tuple[str, ...]] = {}
        for name in registered:
            try:
                checked = rill_models.check_closure(
                    self._rill_dir, name, registered, attached=attached_relations
                )
            except AgentBiError as exc:
                self._unavailable_rill_models[name] = exc
                continue
            self._rill_closures[name] = checked.members
            self._rill_sqlite[name] = checked.sqlite_abs
            for member, facts in checked.facts.items():
                facts_by_name[member] = facts
            for member, deps in checked.direct.items():
                direct_all[member] = deps
        # A model whose closure contains an unavailable model is unavailable too; the
        # recorded error is the failing model's own error, which names its file.
        changed = True
        while changed:
            changed = False
            for name, members in self._rill_closures.items():
                if name in self._unavailable_rill_models:
                    continue
                failing = next((m for m in members if m in self._unavailable_rill_models), None)
                if failing is not None:
                    self._unavailable_rill_models[name] = self._unavailable_rill_models[failing]
                    changed = True
        available = [n for n in registered if n not in self._unavailable_rill_models]
        # The relative ``sqlite_scan`` paths bind while a view is created, so activate the
        # working-directory context only when there is at least one model to create and the
        # Rill directory exists; afterwards it stays active only if a view was created.
        self._rill_cwd_active = bool(available) and self._rill_dir.is_dir()
        for name in _topological_order(available, direct_all):
            if name in self._unavailable_rill_models:
                continue
            failing = next(
                (d for d in direct_all.get(name, ()) if d in self._unavailable_rill_models),
                None,
            )
            if failing is not None:
                self._unavailable_rill_models[name] = self._unavailable_rill_models[failing]
                continue
            facts = facts_by_name[name]
            try:
                self._internal(f'CREATE VIEW memory.main."{name}" AS {facts.text}')
            except AgentBiError as exc:
                self._unavailable_rill_models[name] = validation_error(
                    "UNSUPPORTED_VIEW_FORM",
                    f"{facts.path}: DuckDB cannot create the view: {exc.message} (§5.13 rule 5)",
                )
                continue
            self._rill_models[name] = facts
        self._rill_cwd_active = bool(self._rill_models) and self._rill_dir.is_dir()

    def _attached_relation_names(self) -> set[str]:
        """The case-folded names of tables and views in every successfully attached catalog,
        for the rule 2 CTE-shadowing refusal (D432/A1)."""
        names: set[str] = set()
        if not self._attached:
            return names
        rows = self._internal(
            "SELECT database_name, table_name FROM duckdb_tables() "
            "UNION ALL SELECT database_name, view_name FROM duckdb_views()"
        )
        for database, relation in rows:
            if database in self._attached:
                names.add(str(relation).casefold())
        return names

    def require_rill_model(self, name: str) -> None:
        """Raise the recorded error when registered model ``name`` was unavailable at
        engine start (D431); a no-op for an available or unregistered model."""
        exc = self._unavailable_rill_models.get(name)
        if exc is not None:
            raise exc

    def _apply_session(self) -> None:
        s = self._session
        self._internal(f"SET TimeZone = {_quote_literal(str(s.TimeZone))}")
        self._internal(f"SET threads = {int(s.threads)}")
        self._internal(
            f"SET preserve_insertion_order = {'true' if s.preserve_insertion_order else 'false'}"
        )

    def _attach(self, alias: str, path: str, type_: str) -> None:
        if not _IDENT_RE.match(alias):
            raise validation_error("CONFIG_INVALID", f"attach alias {alias!r} is not an identifier")
        p = Path(path)
        if not p.is_absolute():
            p = self._config.base_dir / p
        if not p.is_file():
            raise engine_error("ENGINE", f"attach path does not exist: {p}")
        options = "READ_ONLY" if type_.lower() != "sqlite" else "TYPE SQLITE, READ_ONLY"
        self._internal(f"ATTACH {_quote_literal(str(p))} AS {_quote_ident(alias)} ({options})")
        self._attached[alias] = (p, type_.lower())
        self._configured_paths[alias] = path

    # -- public ----------------------------------------------------------------------

    def _raise_failed_alias_reference(self, sql: str) -> None:
        """Re-raise a recorded attach failure when ``sql`` reads a relation qualified by
        that alias's catalog (D432/A2). Only runs while some attach has failed, so the
        normal path pays nothing."""
        if not self._attach_failures:
            return
        try:
            raw = self._conn.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
            parsed = json.loads(raw)
        except (duckdb.Error, json.JSONDecodeError):
            return  # let the engine report its own error for unparseable input
        if not isinstance(parsed, dict) or parsed.get("error"):
            return
        for node in _json_nodes(parsed):
            if node.get("type") == "BASE_TABLE":
                failure = self._attach_failures.get(str(node.get("catalog_name") or ""))
                if failure is not None:
                    raise failure[1]

    def execute(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run one ``SELECT``/``WITH`` statement; anything else is refused."""
        if first_keyword(sql) not in ("SELECT", "WITH"):
            raise validation_error(
                "STATEMENT_NOT_ALLOWED",
                "only SELECT or WITH statements may be executed; "
                f"got {first_keyword(sql) or 'an empty statement'}",
            )
        with self._rill_cwd():
            try:
                statements = self._conn.extract_statements(sql)
            except duckdb.Error as exc:
                raise engine_error("ENGINE", str(exc)) from exc
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise validation_error(
                    "STATEMENT_NOT_ALLOWED", "exactly one read-only SELECT statement is allowed"
                )
            self._raise_failed_alias_reference(sql)
            try:
                cur = self._conn.execute(sql)
                columns = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall()
            except duckdb.Error as exc:
                raise engine_error("ENGINE", str(exc)) from exc
        return columns, rows

    def version(self) -> str:
        return duckdb.__version__

    @property
    def aliases(self) -> list[str]:
        return list(self._attached)

    def _raise_attach_failure(self, alias: str) -> None:
        """Re-raise the recorded ``E_ENGINE`` error for an alias whose attach failed
        (D432/A2); a no-op for aliases that attached or were never configured."""
        failure = self._attach_failures.get(alias)
        if failure is not None:
            raise failure[1]

    def attach_path(self, alias: str) -> Path:
        self._raise_attach_failure(alias)
        try:
            return self._attached[alias][0]
        except KeyError:
            raise validation_error("UNKNOWN_ALIAS", f"no attached database {alias!r}") from None

    def configured_path(self, alias: str) -> str:
        """The attach path exactly as written in ``agent-bi.yaml``."""
        self.attach_path(alias)
        return self._configured_paths[alias]

    def attach_type(self, alias: str) -> str:
        self.attach_path(alias)
        return self._attached[alias][1]

    def resolve_relation(self, database: str, schema: str) -> tuple[str, str]:
        """Map ``DATABASE.SCHEMA`` to a DuckDB ``(catalog, schema)`` via ``relation_map``."""
        key = f"{database}.{schema}".upper()
        for k, v in self._config.engines.duckdb.relation_map.items():
            if k.upper() == key:
                catalog, _, sch = v.partition(".")
                if catalog and sch:
                    return catalog, sch
        raise validation_error("RELATION_NOT_MAPPED", f"no relation_map entry for {key}")

    def data_alias(self, database: str, schema: str, table: str) -> str:
        """The attached alias whose file supplies the relation's data (§5.13 rule 5). For
        ``VIEWS.MAIN.<registered name>`` it is the alias attached to the model's resolved
        SQLite file; for every other relation it is the ``relation_map`` catalog alias."""
        if database.upper() == "VIEWS" and schema.upper() == "MAIN":
            self.require_rill_model(table)
            sqlite_abs = self._rill_sqlite.get(table)
            if sqlite_abs is None:
                raise validation_error(
                    "CONFIG_INVALID",
                    f"VIEWS.MAIN.{table} is not listed in engines.duckdb.rill_models "
                    "(§5.13 rule 1)",
                )
            for alias, (path, _type) in self._attached.items():
                if path.resolve() == sqlite_abs:
                    return alias
            for alias, (path, exc) in self._attach_failures.items():
                if path.resolve() == sqlite_abs:
                    raise exc
            raise validation_error(
                "CONFIG_INVALID",
                f"{rill_models.model_path(self._rill_dir, table)} reads {sqlite_abs}; "
                "agent-bi.yaml has no attach entry for that file (§5.13 rule 5)",
            )
        alias = self.resolve_relation(database, schema)[0]
        self._raise_attach_failure(alias)
        return alias

    def _alias_for(self, alias: str | None) -> str:
        if alias is not None:
            self.attach_path(alias)
            return alias
        configured = list(self._attached) + [
            name for name in self._attach_failures if name not in self._attached
        ]
        if len(configured) == 1:
            return configured[0]
        raise validation_error(
            "AMBIGUOUS_AS_OF",
            f"as_of needs an alias when {len(configured)} databases are attached",
        )

    def _file_digest(self, alias: str) -> str:
        return sha256_file(self.attach_path(alias))

    def as_of(self, alias: str | None = None) -> AsOf:
        """File digest of the attached database (§4.4)."""
        return AsOf(kind="file-digest", value=self._file_digest(self._alias_for(alias)))

    def execute_with_as_of(
        self, sql: str, alias: str | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]], AsOf]:
        """Execute and return the as-of digest, hashing the file before and after (§4.4)."""
        a = self._alias_for(alias)
        before = self._file_digest(a)
        columns, rows = self.execute(sql)
        after = self._file_digest(a)
        if before != after:
            raise engine_error(
                "SNAPSHOT_CHANGED", f"{self._attached[a][0]} changed while the query ran"
            )
        return columns, rows, AsOf(kind="file-digest", value=before)

    def engine_settings(self) -> dict[str, Any]:
        """The §4.6 session block, read back from the live connection."""
        rows = self._internal(
            "SELECT current_setting('TimeZone'), current_setting('threads'), "
            "current_setting('preserve_insertion_order')"
        )
        tz, threads, pio = rows[0]
        return {"TimeZone": tz, "threads": int(threads), "preserve_insertion_order": bool(pio)}

    def close(self) -> None:
        self._conn.close()


class SnowflakeEngine:
    """Stub until Sprint 7: reports itself unavailable when its environment is unset."""

    name = "snowflake"
    _REQUIRED = ("account", "user", "password", "role", "warehouse")

    def __init__(self, config: Config):
        sf = config.engines.snowflake
        missing = [k for k in self._REQUIRED if not getattr(sf, k)]
        if missing:
            raise engine_error(
                "ENGINE_UNAVAILABLE",
                "snowflake engine unavailable: unset " + ", ".join(
                    f"AGENT_BI_SNOWFLAKE_{k.upper()}" for k in missing
                ),
            )
        raise engine_error(
            "ENGINE_UNAVAILABLE", "snowflake engine is not implemented before Sprint 7"
        )


def open_engine(name: str, config: Config) -> DuckDBEngine:
    if name == "duckdb":
        return DuckDBEngine(config)
    if name == "snowflake":
        SnowflakeEngine(config)  # always raises E_ENGINE_UNAVAILABLE in Sprint 1
    raise validation_error("UNKNOWN_ENGINE", f"unknown engine {name!r}")
