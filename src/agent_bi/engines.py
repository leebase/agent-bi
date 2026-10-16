"""Engines (architecture §2, §4.4, §4.6, §9).

``Engine.execute()`` runs read statements only (``SELECT``/``WITH``, one statement).
Attach and session settings are internal methods and never go through ``execute()``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import duckdb

from agent_bi.config import Config
from agent_bi.errors import engine_error, validation_error
from agent_bi.hashing import sha256_file

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LEADING_NOISE_RE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)", re.DOTALL)
_FIRST_WORD_RE = re.compile(r"[A-Za-z_]+")


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


class DuckDBEngine:
    name = "duckdb"

    def __init__(self, config: Config):
        self._config = config
        duck = config.engines.duckdb
        self._session = duck.session
        self._attached: dict[str, tuple[Path, str]] = {}
        self._configured_paths: dict[str, str] = {}
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
            self._attach(att.alias, att.path, att.type)

    # -- internal (never reachable through execute()) -------------------------------

    def _internal(self, sql: str) -> list[tuple[Any, ...]]:
        try:
            return self._conn.execute(sql).fetchall()
        except duckdb.Error as exc:
            raise engine_error("ENGINE", str(exc)) from exc

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

    def execute(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run one ``SELECT``/``WITH`` statement; anything else is refused."""
        if first_keyword(sql) not in ("SELECT", "WITH"):
            raise validation_error(
                "STATEMENT_NOT_ALLOWED",
                "only SELECT or WITH statements may be executed; "
                f"got {first_keyword(sql) or 'an empty statement'}",
            )
        try:
            statements = self._conn.extract_statements(sql)
        except duckdb.Error as exc:
            raise engine_error("ENGINE", str(exc)) from exc
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise validation_error(
                "STATEMENT_NOT_ALLOWED", "exactly one read-only SELECT statement is allowed"
            )
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

    def attach_path(self, alias: str) -> Path:
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

    def _alias_for(self, alias: str | None) -> str:
        if alias is not None:
            self.attach_path(alias)
            return alias
        if len(self._attached) == 1:
            return next(iter(self._attached))
        raise validation_error(
            "AMBIGUOUS_AS_OF",
            f"as_of needs an alias when {len(self._attached)} databases are attached",
        )

    def _file_digest(self, alias: str) -> str:
        return sha256_file(self._attached[alias][0])

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
