"""Catalog -> semantic model + provenance sidecar (architecture §4.1, §4.2, §5.2).

``scan`` never reads or writes an LLM. Create-only by default; ``refresh`` merges: objects
with status ``catalog`` follow the catalog, everything else is carried over verbatim.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_bi.catalog import Column, DuckDBCatalog, Relation
from agent_bi.engines import DuckDBEngine
from agent_bi.errors import engine_error, validation_error
from agent_bi.hashing import expr_sha256
from agent_bi.query.compile import expr_columns
from agent_bi.rill_models import current_models_digest, source_rel
from agent_bi.semantic.model import (
    BaseTable,
    Dimension,
    Fact,
    KeyColumns,
    Metric,
    SemanticModel,
    Table,
    TimeDimension,
    dump_yaml,
    load_model_dict,
    parse_model,
)
from agent_bi.semantic.names import check_global_namespace, table_objects
from agent_bi.semantic.provenance import (
    SCHEMA_VERSION,
    ProvenanceSidecar,
    entry_key,
    load_sidecar_dict,
    parse_sidecar,
)

MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INT_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "INT", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
    "UINTEGER", "UBIGINT", "UHUGEINT",
}
_NUM_TYPES = _INT_TYPES | {"DECIMAL", "NUMERIC", "NUMBER", "FIXED", "DOUBLE", "FLOAT", "REAL"}
_ID_SUFFIXES = ("_id", "id", "_code", "_flag", "_key")
SMALL_DISTINCT = 20

KINDS = ("dimensions", "time_dimensions", "facts", "metrics")
_TABLE_KEY_ORDER = (
    "name", "description", "base_table", "primary_key", "unique_keys", "synonyms",
    "dimensions", "time_dimensions", "facts", "metrics", "filters", "tags",
)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def snake_name(column: str) -> str:
    """Semantic object name for a column: ``skuId`` -> ``sku_id`` (``^[a-z][a-z0-9_]*$``)."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", column)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if not s or not s[0].isalpha():
        s = "c_" + s
    return s


# ------------------------------------------------------------------------- classification


def _base_type(catalog_type: str) -> str:
    return catalog_type.upper().split("(")[0].strip()


def classify(col: Column) -> tuple[str, str | None]:
    """Return ``(kind, extra)``; kind in time|dimension|fact|ignored (§4.1.1 rule 3: the
    first rule that matches wins, and a column is exactly one of these). ``extra`` is the
    ``data_type`` for time/dimension/fact and the reason for ``ignored``."""
    t = _base_type(col.catalog_type)
    # 1. not a plain identifier, or a type outside §4.1 -> ignored
    if not _IDENT_RE.match(col.name):
        return "ignored", "column name is not a plain identifier"
    is_text = t in ("VARCHAR", "TEXT", "STRING", "CHAR")
    is_bool = t in ("BOOLEAN", "BOOL")
    is_num = t in _NUM_TYPES
    is_date = t == "DATE"
    is_ts = t.startswith("TIMESTAMP") or t == "DATETIME"
    if not (is_text or is_bool or is_num or is_date or is_ts):
        return "ignored", f"unsupported catalog type {col.catalog_type}"
    # 2. DATE / TIMESTAMP* / DATETIME, or TEXT whose every non-null value is an ISO date
    if col.iso_date_text or is_date:
        return "time", "DATE"
    if is_ts:
        return "time", "TIMESTAMP"
    data_type = "TEXT" if is_text else "BOOLEAN" if is_bool else "NUMBER"
    # 3. declared key -> dimension, always, whatever the type or cardinality
    if col.is_pk:
        return "dimension", data_type
    # 4. numeric with small distinct count and an ID-style name -> dimension (only)
    if is_num and _id_style_dimension(col):
        return "dimension", data_type
    # 5. other numeric -> fact
    if is_num:
        return "fact", data_type
    # 6. VARCHAR/TEXT/STRING/BOOLEAN -> dimension
    return "dimension", data_type


def _is_unique(col: Column, row_count: int) -> bool:
    return col.is_pk or (row_count > 0 and col.distinct_count == row_count)


def _id_style_dimension(col: Column) -> bool:
    """§4.1.1 rule 3.4: distinct count <= 20 and the physical or semantic name ends in an
    ID-style suffix."""
    if col.distinct_count > SMALL_DISTINCT:
        return False
    return col.name.lower().endswith(_ID_SUFFIXES) or snake_name(col.name).endswith(_ID_SUFFIXES)


# ------------------------------------------------------------------------- generation


def _entry(
    now: str, expr: str | None, unique: bool | None = None
) -> dict[str, Any]:
    e: dict[str, Any] = {"status": "catalog", "origin": "scan", "at": now, "by": "scan"}
    if expr is not None:
        e["expr_sha256"] = expr_sha256(expr)
    if unique is not None:
        e["unique"] = unique
    return e


def generate(
    relation: Relation, model_name: str, now: str
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], list[dict[str, str]], dict[str, Any] | None]:
    """Return ``(objects_by_kind, entries, ignored_columns, primary_key)`` (§4.1, §5.2)."""
    objs: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    entries: dict[str, dict[str, Any]] = {}
    ignored: list[dict[str, str]] = []
    dim_cols: list[tuple[Column, str]] = []  # (column, name) in order for distinct metrics
    fact_cols: list[tuple[Column, str]] = []

    for col in relation.columns:
        kind, extra = classify(col)
        name = snake_name(col.name)
        q = f'"{col.name}"'
        desc = f'Column "{col.name}" ({col.catalog_type})'
        unique = _is_unique(col, relation.row_count)
        if kind == "ignored":
            ignored.append({"name": col.name, "type": col.catalog_type, "reason": extra or ""})
        elif kind == "time":
            expr = f'CAST({q} AS DATE)' if col.iso_date_text else q
            objs["time_dimensions"].append(
                TimeDimension(
                    name=name, synonyms=[], description=desc, expr=expr, data_type=extra
                ).model_dump(exclude_unset=True)
            )
            entries[entry_key("time_dimensions", name)] = _entry(now, expr, unique)
        elif kind == "dimension":
            objs["dimensions"].append(
                Dimension(
                    name=name, synonyms=[], description=desc, expr=q, data_type=extra
                ).model_dump(exclude_unset=True)
            )
            entries[entry_key("dimensions", name)] = _entry(now, q, unique)
            dim_cols.append((col, name))
        else:  # fact
            objs["facts"].append(
                Fact(name=name, description=desc, expr=q, data_type=extra).model_dump(
                    exclude_unset=True
                )
            )
            entries[entry_key("facts", name)] = _entry(now, q)
            fact_cols.append((col, name))

    metrics: list[tuple[str, str, str]] = [("row_count", "COUNT(*)", "Number of rows")]
    for col, name in fact_cols:
        metrics.append((f"total_{name}", f'SUM("{col.name}")', f'Sum of "{col.name}"'))
        metrics.append((f"avg_{name}", f'AVG("{col.name}")', f'Average of "{col.name}"'))
    for col, name in dim_cols:
        if not entries[entry_key("dimensions", name)]["unique"]:
            metrics.append(
                (f"distinct_{name}", f'COUNT(DISTINCT "{col.name}")', f'Distinct "{col.name}" values')
            )
    for mname, expr, desc in metrics:
        objs["metrics"].append(
            Metric(name=mname, synonyms=[], description=desc, expr=expr).model_dump(
                exclude_unset=True
            )
        )
        entries[entry_key("metrics", mname)] = _entry(now, expr)

    creation: list[tuple[str, str, str]] = []
    for col in relation.columns:  # column-derived objects first, in catalog order
        kind, _ = classify(col)
        if kind != "ignored":
            k = {"time": "time_dimensions", "dimension": "dimensions", "fact": "facts"}[kind]
            creation.append((k, snake_name(col.name), f'column "{col.name}"'))
    creation += [("metrics", m[0], "generated") for m in metrics]
    check_global_namespace(creation)

    entries = {
        entry_key(kind, o["name"]): entries[entry_key(kind, o["name"])]
        for kind in KINDS
        for o in objs[kind]
    }
    pk = [c.name for c in relation.columns if c.is_pk]
    primary_key = KeyColumns(columns=pk).model_dump(exclude_unset=True) if pk else None
    return objs, entries, ignored, primary_key


def build_model(
    relation: Relation, model_name: str, objs: dict[str, list[dict[str, Any]]],
    primary_key: dict[str, Any] | None,
) -> dict[str, Any]:
    ref = f"{relation.database}.{relation.schema}.{relation.name}"
    table_kwargs: dict[str, Any] = {
        "name": relation.name.lower(),
        "description": f"Scanned from {ref}",
        "base_table": BaseTable.model_validate(
            {"database": relation.database, "schema": relation.schema, "table": relation.name}
        ),
    }
    if primary_key:
        table_kwargs["primary_key"] = KeyColumns(**primary_key)
    for kind in KINDS:
        if objs[kind]:
            table_kwargs[kind] = objs[kind]
    table = Table(**table_kwargs)
    model = SemanticModel(
        name=model_name, description=f"Semantic model for {ref}", tables=[table]
    )
    return model.to_dict()


def _sidecar(
    engine: DuckDBEngine, relation: Relation, model_name: str, alias: str,
    entries: dict[str, Any], ignored: list[dict[str, str]], now: str, as_of_digest: str,
    model: dict[str, Any],
) -> dict[str, Any]:
    relation_info: dict[str, Any] = {
        "physical": f"{relation.database}.{relation.schema}.{relation.name}",
        "kind": relation.kind,
        "row_count": relation.row_count,
        "columns_digest": relation.columns_digest,
    }
    if relation.kind == "view":  # §5.13 rule 6: the registered operator model backs it
        models_digest = current_models_digest(
            engine.rill_dir, model, engine.registered_rill_models
        )
        if models_digest is not None:
            relation_info["source"] = source_rel(engine.rill_rel, relation.name)
            relation_info["dependencies"] = sorted(
                source_rel(engine.rill_rel, member)
                for member in engine.rill_closure(relation.name)
                if member != relation.name
            )
            relation_info["models_digest"] = models_digest
    sc = ProvenanceSidecar(
        schema_version=SCHEMA_VERSION,
        model=model_name,
        scanned_at=now,
        engine=engine.name,
        engine_version=engine.version(),
        source={
            "kind": "sqlite" if engine.attach_type(alias) == "sqlite" else "duckdb",
            "path_or_account": engine.configured_path(alias),
            "as_of_digest": as_of_digest,
        },
        relation=relation_info,
        entries=entries,
        verified_queries={},
        ignored_columns=ignored,
    )
    return sc.to_dict()


# ------------------------------------------------------------------------- refresh merge


def _status(entries: dict[str, Any], kind: str, name: str) -> str:
    e = entries.get(entry_key(kind, name))
    return e.get("status", "catalog") if isinstance(e, dict) else "human"


def _merge_list(
    old: list[dict[str, Any]], gen: list[dict[str, Any]], kind: str, old_entries: dict[str, Any]
) -> list[dict[str, Any]]:
    gen_by_name = {o["name"]: o for o in gen}
    result: list[dict[str, Any]] = []
    for o in old:
        if _status(old_entries, kind, o["name"]) != "catalog":
            result.append(o)  # accepted / unregistered: verbatim
        elif o["name"] in gen_by_name:
            result.append(gen_by_name[o["name"]])
        # else: catalog object no longer in the catalog -> removed
    anchor: str | None = None
    for g in gen:
        name = g["name"]
        present = [i for i, r in enumerate(result) if r["name"] == name]
        if not present:
            at = 0
            if anchor is not None:
                at = next(i for i, r in enumerate(result) if r["name"] == anchor) + 1
            result.insert(at, g)
        anchor = name
    return result


def _insert_ordered(table: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    if key in table:
        table[key] = value
        return table
    rank = _TABLE_KEY_ORDER.index(key)
    out: dict[str, Any] = {}
    placed = False
    for k, v in table.items():
        if not placed and k in _TABLE_KEY_ORDER and _TABLE_KEY_ORDER.index(k) > rank:
            out[key] = value
            placed = True
        out[k] = v
    if not placed:
        out[key] = value
    return out


def _query_names(sq: Any) -> list[str]:
    """Names of model objects referenced by a stored SemanticQuery (§4.3)."""
    if not isinstance(sq, dict):
        return []
    names = list(sq.get("metrics") or []) + list(sq.get("dimensions") or [])
    time = sq.get("time")
    if isinstance(time, dict) and time.get("dimension"):
        names.append(time["dimension"])
    names += [f.get("dimension") for f in sq.get("filters") or [] if isinstance(f, dict)]
    names += list(sq.get("named_filters") or [])
    return [n for n in names if isinstance(n, str)]


def refresh_merge(
    existing_model: dict[str, Any], existing_sidecar: dict[str, Any],
    gen_objs: dict[str, list[dict[str, Any]]], gen_entries: dict[str, Any],
    primary_key: dict[str, Any] | None, new_sidecar: dict[str, Any], column_names: set[str],
    model_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = refresh_merge_detailed(
        existing_model, existing_sidecar, gen_objs, gen_entries, primary_key, new_sidecar,
        column_names, model_name, merge_when_broken=False,
    )
    if result.broken:
        raise validation_error(
            "REFRESH_BREAKS_ACCEPTED",
            "refresh would break accepted content: " + "; ".join(result.broken)
            + "; nothing written",
        )
    return result.model, result.sidecar


@dataclass(frozen=True)
class RefreshMerge:
    """The merge ``scan --refresh`` would perform, plus what it would break (§5.12)."""

    model: dict[str, Any]
    sidecar: dict[str, Any]
    broken: list[str]


def refresh_merge_detailed(
    existing_model: dict[str, Any], existing_sidecar: dict[str, Any],
    gen_objs: dict[str, list[dict[str, Any]]], gen_entries: dict[str, Any],
    primary_key: dict[str, Any] | None, new_sidecar: dict[str, Any], column_names: set[str],
    model_name: str, merge_when_broken: bool = True,
) -> RefreshMerge:
    """Compute the merged model/sidecar and the list of accepted objects or verified queries
    a refresh would break, without writing or raising (``scan`` turns a non-empty list into
    ``E_REFRESH_BREAKS_ACCEPTED``; ``diff`` reports it). ``merge_when_broken=False`` returns
    as soon as breakage is known, so ``scan`` refuses before any further merge step runs."""
    tables = existing_model.get("tables") or []
    if len(tables) != 1:
        raise validation_error(
            "UNSUPPORTED_MULTI_TABLE", f"tables: expected exactly one table, found {len(tables)}"
        )
    table = copy.deepcopy(tables[0])
    old_entries: dict[str, Any] = existing_sidecar.get("entries") or {}

    # Accepted content that references a column the catalog no longer has blocks the refresh.
    broken: list[str] = []
    unavailable: set[str] = set()  # kept objects whose required physical column is gone
    for kind in KINDS + ("filters",):
        for o in table.get(kind) or []:
            if _status(old_entries, kind, o["name"]) == "catalog" and kind != "filters":
                continue
            missing = [c for c in expr_columns(o.get("expr", "")) if c not in column_names]
            if missing:
                broken.append(f"{entry_key(kind, o['name'])} (missing column {', '.join(missing)})")
                unavailable.add(o["name"])

    merged: dict[str, list[dict[str, Any]]] = {}
    for kind in KINDS:
        merged[kind] = _merge_list(table.get(kind) or [], gen_objs[kind], kind, old_entries)

    # Names the merge keeps. An intentionally empty merged kind is empty and must not fall
    # back to the pre-merge table, or a removed object's name would falsely survive.
    surviving = {n["name"] for kind in KINDS for n in merged[kind]}
    surviving.update(n["name"] for n in table.get("filters") or [])
    for vq_name, vq in (existing_sidecar.get("verified_queries") or {}).items():
        refs = _query_names((vq or {}).get("semantic_query"))
        gone = sorted({n for n in refs if n not in surviving})
        blocked = sorted({n for n in refs if n in unavailable})
        notes: list[str] = []
        if gone:
            notes.append("missing " + ", ".join(gone))
        if blocked:
            notes.append("broken dependency " + ", ".join(blocked))
        if notes:
            broken.append(f"verified_queries[{vq_name}] ({'; '.join(notes)})")
    # A primary key (scan-generated or operator-written) is preserved metadata once carried
    # over; a key column that left the catalog breaks it (§5.2 invariant).
    pk_missing = [
        c for c in (table.get("primary_key") or {}).get("columns") or [] if c not in column_names
    ]
    if pk_missing:
        broken.append(f"tables[0].primary_key (missing column {', '.join(pk_missing)})")
    if broken and not merge_when_broken:
        return RefreshMerge(existing_model, existing_sidecar, broken)

    # Table: replace managed lists in place. An existing primary_key is carried over
    # verbatim (ADR-009); it is derived from the catalog only when the model has none.
    for kind in KINDS:
        if merged[kind]:
            table = _insert_ordered(table, kind, merged[kind])
        else:
            table.pop(kind, None)
    if "primary_key" not in table and primary_key:
        table = _insert_ordered(table, "primary_key", primary_key)
    model = dict(existing_model)
    model["tables"] = [table]
    gen_names = {(k, o["name"]) for k in KINDS for o in gen_objs[k]}

    def origin_of(kind: str, name: str) -> str | None:
        old_e = old_entries.get(entry_key(kind, name))
        if isinstance(old_e, dict):
            return old_e.get("origin")
        return "scan" if (kind, name) in gen_names else None

    check_global_namespace(table_objects(table, origin_of))

    # Sidecar entries, in object order; `at` changes only when the entry's content does.
    entries: dict[str, Any] = {}
    old_objects = {
        (kind, o["name"]): o for kind in KINDS for o in (tables[0].get(kind) or [])
    }
    generated_ids = {id(g) for kind in KINDS for g in gen_objs[kind]}
    for kind in KINDS + ("filters",):
        for o in table.get(kind) or []:
            key = entry_key(kind, o["name"])
            old_e = old_entries.get(key)
            if id(o) not in generated_ids:  # accepted / operator-owned: entry verbatim
                if old_e is not None:
                    entries[key] = old_e
                continue
            new_e = gen_entries[key]
            unchanged = (
                isinstance(old_e, dict)
                and old_objects.get((kind, o["name"])) == o
                and {k: v for k, v in old_e.items() if k != "at"}
                == {k: v for k, v in new_e.items() if k != "at"}
            )
            entries[key] = old_e if unchanged else new_e
    for key, e in old_entries.items():  # orphans that are not stale catalog entries
        if key not in entries and isinstance(e, dict) and e.get("status") != "catalog":
            entries[key] = e

    sidecar = dict(existing_sidecar)
    for k in ("schema_version", "model", "scanned_at", "engine", "engine_version", "source",
              "relation", "ignored_columns"):
        sidecar[k] = new_sidecar[k]
    sidecar["model"] = model_name
    sidecar["entries"] = entries
    sidecar.setdefault("verified_queries", {})
    return RefreshMerge(model, sidecar, broken)


# ------------------------------------------------------------------------- entry point


def parse_relation_ref(ref: str) -> tuple[str, str, str]:
    parts = ref.split(".")
    if len(parts) != 3 or not all(parts):
        raise validation_error(
            "INVALID_RELATION", f"--relation must be DATABASE.SCHEMA.NAME, got {ref!r}"
        )
    return parts[0].upper(), parts[1].upper(), parts[2]


def paths_for(semantic_dir: Path, model_name: str) -> tuple[Path, Path]:
    return semantic_dir / f"{model_name}.yaml", semantic_dir / f"{model_name}.provenance.yaml"


@dataclass(frozen=True)
class ScanPlan:
    """Everything ``scan`` would write, computed in memory (§5.14 step 1)."""

    model_path: Path
    sidecar_path: Path
    model: dict[str, Any]
    sidecar: dict[str, Any]
    model_text: str
    sidecar_text: str


def plan_scan(
    engine: DuckDBEngine, relation_ref: str, model_name: str, semantic_dir: Path,
    refresh: bool = False, now: str | None = None,
) -> ScanPlan:
    if not MODEL_NAME_RE.match(model_name):
        raise validation_error(
            "INVALID_MODEL_NAME", f"model name {model_name!r} must match ^[a-z][a-z0-9_]*$"
        )
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    exists = model_path.exists() or sidecar_path.exists()
    if exists and not refresh:
        raise validation_error(
            "MODEL_EXISTS", f"{model_path} already exists; use --refresh to merge"
        )
    if refresh and not exists:
        raise validation_error("MODEL_NOT_FOUND", f"{model_path} does not exist; nothing to refresh")

    now = now or utc_now()
    database, schema, table = parse_relation_ref(relation_ref)
    alias = engine.data_alias(database, schema, table)
    before = engine.as_of(alias).value
    relation = DuckDBCatalog(engine).describe(database, schema, table)
    after = engine.as_of(alias).value
    if before != after:
        raise engine_error("SNAPSHOT_CHANGED", f"{engine.attach_path(alias)} changed during scan")

    objs, entries, ignored, primary_key = generate(relation, model_name, now)
    model = build_model(relation, model_name, objs, primary_key)
    sidecar = _sidecar(engine, relation, model_name, alias, entries, ignored, now, before, model)

    if refresh:
        existing_model = load_model_dict(str(model_path))
        existing_sidecar = load_sidecar_dict(sidecar_path)
        model, sidecar = refresh_merge(
            existing_model, existing_sidecar, objs, entries, primary_key, sidecar,
            {c.name for c in relation.columns}, model_name,
        )

    parse_model(model, str(model_path))  # fail before writing anything
    parse_sidecar(sidecar, str(sidecar_path))
    return ScanPlan(model_path, sidecar_path, model, sidecar, dump_yaml(model), dump_yaml(sidecar))


def scan(
    engine: DuckDBEngine, relation_ref: str, model_name: str, semantic_dir: Path,
    refresh: bool = False, now: str | None = None,
) -> tuple[Path, Path]:
    plan = plan_scan(engine, relation_ref, model_name, semantic_dir, refresh, now)
    _write_pair(plan.model_path, plan.model_text, plan.sidecar_path, plan.sidecar_text)
    return plan.model_path, plan.sidecar_path


def _write_pair(p1: Path, text1: str, p2: Path, text2: str) -> None:
    p1.parent.mkdir(parents=True, exist_ok=True)
    tmps: list[str] = []
    try:
        for p, text in ((p1, text1), (p2, text2)):
            fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
            tmps.append(tmp)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        os.replace(tmps[0], p1)
        os.replace(tmps[1], p2)
    finally:
        for t in tmps:
            if os.path.exists(t):
                os.unlink(t)

