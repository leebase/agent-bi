"""SemanticQuery -> SQL (architecture §5.3), plus the closed grammars (§5.3.2, §5.3.5) and
literal helpers (§5.3.3) that ``scan`` and ``validate`` share.

Three dialects: ``duckdb`` (agent-bi's own engine), ``snowflake`` and ``rill`` (Metrics
SQL, a *name projection*: it emits names only, never an expression, and so cannot violate
the Rill invariant of D389). The compiler consumes the canonical form of the query (§4.3);
input order never reaches the SQL text.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from agent_bi.errors import validation_error

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_COL = rf'"{_IDENT}"'

# -- dimension / time-dimension expr (§5.3.5): column | CAST(column AS DATE) -----------
DIM_RE = re.compile(rf"^(?:{_COL}|(?i:CAST)\({_COL} (?i:AS) (?i:DATE)\))$")

# -- fact expr (§5.3.5): a bare quoted column --------------------------------------------
COLUMN_RE = re.compile(rf"^{_COL}$")

# -- metric expr (§5.3.2) -------------------------------------------------------------------
_AGG = (
    rf"(?:(?i:SUM|AVG|MIN|MAX)\({_COL}\)"
    r"|(?i:COUNT)\(\*\)"
    rf"|(?i:COUNT)\((?i:DISTINCT) {_COL}\))"
)
METRIC_RE = re.compile(rf"^{_AGG}(?:\s*/\s*(?i:NULLIF)\({_AGG},\s*0\))?$")

# -- filter expr (§5.3.5) ---------------------------------------------------------------------
_STRING = r"'(?:[^']|'')*'"
_NUMBER = r"-?\d+(?:\.\d+)?"
_DATE_LIT = r"(?i:DATE) '\d{4}-\d{2}-\d{2}'"
_LITERAL = rf"(?:{_STRING}|{_DATE_LIT}|{_NUMBER}|(?i:TRUE)|(?i:FALSE))"
_TERM = (
    rf"(?:{_COL} (?:=|!=|<=|>=|<|>) {_LITERAL}"
    rf"|{_COL} (?i:IN) \({_LITERAL}(?:,\s*{_LITERAL})*\)"
    rf"|{_COL} (?i:IS) (?i:NULL)"
    rf"|{_COL} (?i:IS) (?i:NOT) (?i:NULL))"
)


def _filter_pattern(depth: int) -> str:
    """term | term AND/OR filter | "(" filter ")" -- parenthesis nesting up to ``depth``
    levels (a regular expression cannot nest without bound; 4 levels is the v1 limit)."""
    inner = _TERM
    for _ in range(depth):
        unit = rf"(?:{_TERM}|\({inner}\))"
        inner = rf"{unit}(?: (?i:AND|OR) {unit})*"
    return inner


FILTER_RE = re.compile(rf"^{_filter_pattern(4)}$")

_QUOTED_COLUMN_RE = re.compile(rf'"({_IDENT})"')


def expr_columns(expr: str) -> list[str]:
    """Quoted column identifiers referenced by a (grammar-conforming) expr, in order."""
    return _QUOTED_COLUMN_RE.findall(expr)


# -- literal helpers (§5.3.3): nothing is string-interpolated without passing through these -


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_date(value: str) -> str:
    return f"DATE '{date.fromisoformat(value).isoformat()}'"


def sql_number(value: float | str) -> str:
    if isinstance(value, bool):
        raise TypeError("a bool is not a number")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return str(int(text))
        except ValueError:
            value = float(text)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("number must be finite")
    return repr(number)


def sql_bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


# ======================================================================================
# The compiler (§5.3.1, §5.3.4)
# ======================================================================================

DIALECTS = ("duckdb", "snowflake", "rill")
_CAST_DATE_RE = re.compile(rf"^(?i:CAST)\(({_COL}) (?i:AS) (?i:DATE)\)$")
_FILTER_CMP = {"=": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}


@dataclass(frozen=True)
class CompiledQuery:
    """``compiled_sql`` never carries an as-of clause (§4.4); ``relation_end`` is the offset
    in it just after the relation reference, where ``executed_sql`` splices the clause."""

    compiled_sql: str
    dialect: str
    relation_end: int


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: Any, dialect: str) -> str:
    if isinstance(value, bool):
        return sql_bool(value)
    if isinstance(value, (int, float)):
        return sql_number(value)
    text = str(value)
    if dialect in ("snowflake", "rill"):
        # Snowflake and Rill treat `\` as an escape inside '...': without doubling it, a
        # value ending in a backslash would escape the quote that sql_string doubles
        # (§5.3.3; Sprint 2 review A1). DuckDB does not, so it is passed through.
        text = text.replace("\\", "\\\\")
    return sql_string(text)


def _table_of(model: Any) -> dict[str, Any]:
    d = model.to_dict() if hasattr(model, "to_dict") else model
    tables = d.get("tables") or []
    if len(tables) != 1:
        raise validation_error(
            "UNSUPPORTED_MULTI_TABLE", f"tables: expected exactly one table, found {len(tables)}"
        )
    return tables[0]


def _obj(table: dict[str, Any], kind: str, name: str) -> dict[str, Any]:
    return next(o for o in table.get(kind) or [] if o["name"] == name)


def _snowflake_expr(expr: str) -> str:
    """The model's ``CAST("c" AS DATE)`` becomes ``TRY_TO_DATE("c")`` on Snowflake."""
    m = _CAST_DATE_RE.match(expr)
    return f"TRY_TO_DATE({m.group(1)})" if m else expr


def _relation(table: dict[str, Any], dialect: str, model_name: str,
              relation_map: dict[str, str] | None) -> str:
    if dialect == "rill":
        return model_name
    bt = table.get("base_table") or {}
    database, schema, name = bt.get("database"), bt.get("schema"), bt.get("table")
    if dialect == "snowflake":
        return ".".join(quote_ident(p) for p in (database, schema, name))
    key = f"{database}.{schema}".upper()
    for k, v in (relation_map or {}).items():
        if k.upper() == key:
            catalog, _, sch = v.partition(".")
            if catalog and sch:
                return ".".join(quote_ident(p) for p in (catalog, sch, name))
    raise validation_error("RELATION_NOT_MAPPED", f"no relation_map entry for {key}")


def _check_metric(table: dict[str, Any], metric: dict[str, Any], columns: set[str]) -> None:
    path = f"tables[0].metrics[{metric['name']}]"
    if not METRIC_RE.fullmatch(metric["expr"]):
        raise validation_error(
            "UNSUPPORTED_METRIC_EXPR",
            f"{path}: metric expr {metric['expr']!r} is outside the closed grammar",
        )
    unknown = [c for c in expr_columns(metric["expr"]) if c not in columns]
    if unknown:
        raise validation_error(
            "UNSUPPORTED_METRIC_EXPR",
            f"{path}: column {unknown[0]!r} is not a fact or dimension column of the table",
        )


def _check_dimension(kind: str, obj: dict[str, Any]) -> None:
    if not DIM_RE.fullmatch(obj["expr"]):
        raise validation_error(
            "UNSUPPORTED_DIMENSION_EXPR",
            f"tables[0].{kind}[{obj['name']}]: expr {obj['expr']!r} is outside the closed grammar",
        )


def _model_columns(table: dict[str, Any]) -> set[str]:
    cols: set[str] = set()
    for kind in ("dimensions", "time_dimensions", "facts"):
        for o in table.get(kind) or []:
            cols.update(expr_columns(o["expr"]))
    return cols


def compile(
    query: Any,
    model: Any,
    dialect: str,
    *,
    relation_map: dict[str, str] | None = None,
) -> CompiledQuery:
    """Compile a SemanticQuery against a model (a ``SemanticModel`` or its dict) for one of
    the three dialects. Validates the query against the model first (``E_UNKNOWN_FIELD``)."""
    from agent_bi.query.contract import validate_against_model

    if dialect not in DIALECTS:
        raise validation_error("UNKNOWN_ENGINE", f"unknown dialect {dialect!r}")
    q = validate_against_model(query, model).canonical()  # compile from the canonical form
    m = model.to_dict() if hasattr(model, "to_dict") else model
    table = _table_of(m)
    if dialect == "rill":
        return _compile_rill(q, m["name"], table)
    return _compile_sql(q, m["name"], table, dialect, relation_map)


def _order_by(q: Any, time_bucket: bool) -> list[tuple[str, str]]:
    """The total order (§5.3.1): the user's ``order_by`` as given, then the canonical
    tie-break -- ``time_bucket ASC``, every metric ``DESC``, every dimension ``ASC`` --
    with fields already named by the user removed."""
    out = [(o.field, o.direction.upper()) for o in q.order_by]
    named = {f for f, _ in out}
    tie = ([("time_bucket", "ASC")] if time_bucket else [])
    tie += [(mname, "DESC") for mname in q.metrics] + [(d, "ASC") for d in q.dimensions]
    return out + [(f, d) for f, d in tie if f not in named]


def _filter_sql(op: str, lhs: str, values: list[Any], dialect: str) -> str:
    lits = [_literal(v, dialect) for v in values]
    if op in _FILTER_CMP:
        return f"{lhs} {_FILTER_CMP[op]} {lits[0]}"
    if op == "in":
        return f"{lhs} IN ({','.join(lits)})"
    if op == "not_in":
        return f"{lhs} NOT IN ({','.join(lits)})"
    if op == "between":
        return f"{lhs} BETWEEN {lits[0]} AND {lits[1]}"
    if op == "like":
        return f"{lhs} LIKE {lits[0]}"
    raise validation_error("QUERY_INVALID", f"unsupported filter op {op!r}")  # unreachable


def _compile_sql(q: Any, model_name: str, table: dict[str, Any], dialect: str,
                 relation_map: dict[str, str] | None) -> CompiledQuery:
    columns = _model_columns(table)
    emit = _snowflake_expr if dialect == "snowflake" else (lambda e: e)

    select: list[str] = []
    group: list[str] = []
    for d in q.dimensions:
        obj = _obj(table, "dimensions", d)
        _check_dimension("dimensions", obj)
        select.append(f"{emit(obj['expr'])} AS {quote_ident(d)}")
        group.append(quote_ident(d))
    time_expr = None
    if q.time is not None:
        tobj = _obj(table, "time_dimensions", q.time.dimension)
        _check_dimension("time_dimensions", tobj)
        time_expr = emit(tobj["expr"])
        select.append(f"DATE_TRUNC('{q.time.grain}', {time_expr}) AS {quote_ident('time_bucket')}")
        group.append(quote_ident("time_bucket"))
    for mname in q.metrics:
        metric = _obj(table, "metrics", mname)
        _check_metric(table, metric, columns)
        select.append(f"{metric['expr']} AS {quote_ident(mname)}")

    where: list[str] = []
    if q.time is not None:
        if q.time.start:
            where.append(f"{time_expr} >= {sql_date(q.time.start)}")
        if q.time.end:
            where.append(f"{time_expr} < {sql_date(q.time.end)}")
    for f in q.filters:
        dobj = _obj(table, "dimensions", f.dimension)
        _check_dimension("dimensions", dobj)
        where.append(_filter_sql(f.op, emit(dobj["expr"]), f.values, dialect))
    for n in q.named_filters:
        fobj = _obj(table, "filters", n)
        if not FILTER_RE.fullmatch(fobj["expr"]):
            raise validation_error(
                "UNSUPPORTED_FILTER_EXPR",
                f"tables[0].filters[{n}]: filter expr {fobj['expr']!r} is outside the closed grammar",
            )
        where.append(f"({fobj['expr']})")

    head = "SELECT DISTINCT\n  " if not q.metrics else "SELECT\n  "
    prefix = head + ",\n  ".join(select) + "\nFROM "
    relation = _relation(table, dialect, model_name, relation_map)
    relation_end = len(prefix) + len(relation)
    lines = [prefix + relation, "WHERE 1=1"] + [f"  AND {w}" for w in where]
    if q.metrics and group:
        lines.append("GROUP BY " + ", ".join(group))
    order = ", ".join(f"{quote_ident(f)} {d}" for f, d in _order_by(q, q.time is not None))
    lines.append("ORDER BY " + order)
    lines.append(f"LIMIT {q.limit}")
    return CompiledQuery("\n".join(lines), dialect, relation_end)


def _rill_refuse(what: str) -> Exception:
    return validation_error(
        "UNSUPPORTED_BY_RILL_ROUTE",
        f"{what} cannot be promoted to a Rill component; the query is still answerable on "
        "the duckdb and snowflake dialects",
    )


def _compile_rill(q: Any, model_name: str, table: dict[str, Any]) -> CompiledQuery:
    """Rill Metrics SQL: names only (no aggregates, no GROUP BY); one line."""
    if any(f.op == "like" for f in q.filters):
        raise _rill_refuse("a `like` filter (Metrics SQL LIKE is case-insensitive)")
    if q.named_filters:
        raise _rill_refuse("a named filter (written over physical columns, not dimension names)")
    if not q.metrics:
        raise _rill_refuse("a pure dimension listing (no metric)")
    select = list(q.dimensions)
    if q.time is not None:
        select.append(f"date_trunc('{q.time.grain.upper()}', {q.time.dimension}) AS time_bucket")
    select += q.metrics
    where: list[str] = []
    if q.time is not None:
        if q.time.start:
            where.append(f"{q.time.dimension} >= {sql_string(q.time.start)}")
        if q.time.end:
            where.append(f"{q.time.dimension} < {sql_string(q.time.end)}")
    for f in q.filters:
        where.append(_filter_sql(f.op, f.dimension, f.values, "rill"))
    parts = [f"SELECT {', '.join(select)}", f"FROM {model_name}"]
    if where:
        parts.append("WHERE " + " AND ".join(where))
    parts.append("ORDER BY " + ", ".join(f"{f} {d}" for f, d in _order_by(q, q.time is not None)))
    parts.append(f"LIMIT {q.limit}")
    sql = " ".join(parts)
    return CompiledQuery(sql, "rill", len(parts[0]) + len(" FROM ") + len(model_name))


def executed_sql(compiled: CompiledQuery, engine: str, as_of: str | None = None) -> str:
    """Apply the engine's as-of clause to ``compiled.compiled_sql`` (§5.3.4). Only the
    Snowflake dialect has one: ``AT(TIMESTAMP => '<ts>'::TIMESTAMP_LTZ)`` right after the
    relation reference. DuckDB and Rill have none (the file digest is their as-of)."""
    if engine != compiled.dialect:
        raise validation_error(
            "UNKNOWN_ENGINE",
            f"executed_sql: query was compiled for {compiled.dialect!r}, not {engine!r}",
        )
    if engine != "snowflake":
        return compiled.compiled_sql
    if not as_of:
        raise validation_error("QUERY_INVALID", "executed_sql: snowflake needs an as-of timestamp")
    ts = sql_string(as_of)
    sql = compiled.compiled_sql
    return f"{sql[: compiled.relation_end]} AT(TIMESTAMP => {ts}::TIMESTAMP_LTZ){sql[compiled.relation_end:]}"
