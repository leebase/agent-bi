"""Relation discovery (architecture §5.1)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from agent_bi.engines import DuckDBEngine, _quote_ident
from agent_bi.errors import AgentBiError, validation_error
from agent_bi.hashing import sha256_canonical

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TEXT_TYPES = ("VARCHAR", "TEXT", "STRING", "CHAR")
ISO_SAMPLE_ROWS = 1000


@dataclass(frozen=True)
class Column:
    name: str
    catalog_type: str
    nullable: bool
    is_pk: bool
    ordinal: int
    iso_date_text: bool = False
    distinct_count: int = 0
    null_count: int = 0


@dataclass(frozen=True)
class Relation:
    database: str
    schema: str
    name: str
    kind: str  # "table" | "view"
    columns: list[Column] = field(default_factory=list)
    row_count: int = 0

    @property
    def columns_digest(self) -> str:
        """sha256 of canonical JSON of [(name, type)] in catalog order (§4.2)."""
        return sha256_canonical([[c.name, c.catalog_type] for c in self.columns])


class Catalog(Protocol):
    def describe(self, database: str, schema: str, name: str) -> Relation: ...


def is_text_type(catalog_type: str) -> bool:
    return catalog_type.upper().split("(")[0].strip() in _TEXT_TYPES


class DuckDBCatalog:
    """Discovery over a ``DuckDBEngine``. ``database``/``schema`` are the semantic
    (Snowflake-form) names, mapped to a DuckDB catalog via ``relation_map``."""

    def __init__(self, engine: DuckDBEngine):
        self._engine = engine

    def describe(self, database: str, schema: str, name: str) -> Relation:
        catalog, duck_schema = self._engine.resolve_relation(database, schema)
        table_name, kind = self._find_table(catalog, duck_schema, name)
        rows = self._engine.execute(
            "SELECT column_name, data_type, is_nullable, ordinal_position "
            "FROM information_schema.columns "
            f"WHERE table_catalog = {_lit(catalog)} AND table_schema = {_lit(duck_schema)} "
            f"AND table_name = {_lit(table_name)} ORDER BY ordinal_position"
        )[1]
        if not rows:
            raise validation_error(
                "RELATION_NOT_FOUND", f"{database}.{schema}.{name} has no columns in the catalog"
            )
        pks = self._primary_keys(catalog, duck_schema, table_name)
        fqn = f"{_quote_ident(catalog)}.{_quote_ident(duck_schema)}.{_quote_ident(table_name)}"
        row_count = self._engine.execute(f"SELECT COUNT(*) FROM {fqn}")[1][0][0]
        columns: list[Column] = []
        for col_name, col_type, is_nullable, ordinal in rows:
            q = _quote_ident(col_name)
            distinct, nulls = self._engine.execute(
                f"SELECT COUNT(DISTINCT {q}), COUNT(*) FILTER (WHERE {q} IS NULL) FROM {fqn}"
            )[1][0]
            iso = is_text_type(col_type) and self._iso_date_text(fqn, q)
            columns.append(
                Column(
                    name=col_name,
                    catalog_type=col_type,
                    nullable=is_nullable == "YES",
                    is_pk=col_name in pks,
                    ordinal=int(ordinal),
                    iso_date_text=iso,
                    distinct_count=int(distinct),
                    null_count=int(nulls),
                )
            )
        return Relation(
            database=database,
            schema=schema,
            name=table_name,
            kind=kind,
            columns=columns,
            row_count=int(row_count),
        )

    def _find_table(self, catalog: str, schema: str, name: str) -> tuple[str, str]:
        rows = self._engine.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            f"WHERE table_catalog = {_lit(catalog)} AND table_schema = {_lit(schema)}"
        )[1]
        exact = [r for r in rows if r[0] == name]
        folded = [r for r in rows if r[0].lower() == name.lower()]
        hit = exact or folded
        if len(hit) != 1:
            raise validation_error(
                "RELATION_NOT_FOUND", f"relation {catalog}.{schema}.{name} not found"
            )
        return hit[0][0], ("view" if hit[0][1] == "VIEW" else "table")

    def _primary_keys(self, catalog: str, schema: str, table: str) -> set[str]:
        """PK columns. DuckDB's constraint view first; for attached SQLite the stdlib
        ``sqlite3`` module on the file (DuckDB does not surface SQLite PKs reliably)."""
        pks: set[str] = set()
        try:
            rows = self._engine.execute(
                "SELECT constraint_column_names FROM duckdb_constraints() "
                f"WHERE database_name = {_lit(catalog)} AND schema_name = {_lit(schema)} "
                f"AND table_name = {_lit(table)} AND constraint_type = 'PRIMARY KEY'"
            )[1]
            for (names,) in rows:
                pks.update(names)
        except AgentBiError:  # engine without duckdb_constraints support: fall through
            pass
        if not pks and self._engine.attach_type(catalog) == "sqlite":
            path = self._engine.attach_path(catalog)
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                for row in con.execute("SELECT name, pk FROM pragma_table_info(?)", (table,)):
                    if row[1]:
                        pks.add(row[0])
            finally:
                con.close()
        return pks

    def _iso_date_text(self, fqn: str, q: str) -> bool:
        sample = self._engine.execute(
            f"SELECT {q} FROM {fqn} WHERE {q} IS NOT NULL LIMIT {ISO_SAMPLE_ROWS}"
        )[1]
        if not sample or not all(ISO_DATE_RE.match(r[0]) for r in sample):
            return False
        bad = self._engine.execute(
            f"SELECT COUNT(*) FILTER (WHERE TRY_CAST({q} AS DATE) IS NULL AND {q} IS NOT NULL) "
            f"FROM {fqn}"
        )[1][0][0]
        return bad == 0


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"
