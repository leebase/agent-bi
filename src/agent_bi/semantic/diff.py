"""``diff`` (architecture §5.12): a read-only preview of ``scan --refresh``.

Re-runs catalog discovery and compares the catalog with the committed YAML + sidecar:
added, removed and type-changed columns, the row count, and — through exactly the merge
logic ``scan --refresh`` uses (``generate`` + ``refresh_merge_detailed``) — the semantic
objects a refresh would add, update or remove and every accepted object or verified query
the refresh would break. Output is a Markdown report on stdout; ``diff`` writes nothing.
Exit 1 when a refresh would change the structure; a row-count-only change is reported and
exits 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_bi.catalog import Column, DuckDBCatalog
from agent_bi.config import Config
from agent_bi.engines import open_engine
from agent_bi.errors import AgentBiError, engine_error
from agent_bi.query.compile import expr_columns
from agent_bi.semantic.model import load_model_dict
from agent_bi.semantic.provenance import load_sidecar_dict
from agent_bi.semantic.scan import (
    _sidecar,
    classify,
    generate,
    paths_for,
    refresh_merge_detailed,
    utc_now,
)
from agent_bi.semantic.validate import validate

_KINDS = ("dimensions", "time_dimensions", "facts", "metrics", "filters")
_KIND_OF_COLUMN = {"dimensions": "dimension", "time_dimensions": "time_dimension", "facts": "fact"}


@dataclass(frozen=True)
class DiffResult:
    lines: list[str]
    exit_code: int


def _model_columns(model: dict[str, Any], sidecar: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The physical columns the committed model knows, with a comparable classification key.

    Names come from the exprs of dimensions, time dimensions and facts plus the sidecar's
    ignored columns; the YAML records each object's classified ``data_type``, the same value
    ``classify`` produces for the current catalog column."""
    table = (model.get("tables") or [{}])[0]
    out: dict[str, dict[str, str]] = {}
    for kind in ("dimensions", "time_dimensions", "facts"):
        for obj in table.get(kind) or []:
            for column in expr_columns(obj.get("expr", "")):
                data_type = obj.get("data_type") or "?"
                out[column] = {
                    "display": f"{_KIND_OF_COLUMN[kind]}:{data_type}",
                    "key": f"{kind}:{data_type}",
                }
    for ignored in sidecar.get("ignored_columns") or []:
        catalog_type = ignored.get("type") or "?"
        out[ignored["name"]] = {
            "display": f"ignored:{catalog_type}",
            "key": f"ignored:{catalog_type}",
        }
    return out


_CLASSIFY_KIND = {"time": "time_dimensions", "dimension": "dimensions", "fact": "facts"}


def _current_columns(columns: list[Column]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for col in columns:
        kind, data_type = classify(col)
        if kind == "ignored":
            display = f"ignored:{col.catalog_type}"
        else:
            display = f"{_CLASSIFY_KIND[kind]}:{data_type}"
        out[col.name] = {"display": display, "key": display}
    return out


def _column_rows(
    old: dict[str, dict[str, str]], new: dict[str, dict[str, str]]
) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for name in sorted(new.keys() - old.keys()):
        rows.append(("added", name, "--", new[name]["display"]))
    for name in sorted(old.keys() - new.keys()):
        rows.append(("removed", name, old[name]["display"], "--"))
    for name in sorted(old.keys() & new.keys()):
        if old[name]["key"] != new[name]["key"]:
            rows.append(
                ("type-changed", name, old[name]["display"], new[name]["display"])
            )
    return rows


def run_diff(
    model_name: str, engine_name: str, config: Config, project_dir: Path
) -> DiffResult:
    project_dir = Path(project_dir)
    semantic_dir = project_dir / "semantic"
    validate(model_name, semantic_dir)
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    model = load_model_dict(str(model_path))
    sidecar = load_sidecar_dict(sidecar_path)
    table = (model.get("tables") or [{}])[0]
    base = table["base_table"]

    engine = open_engine(engine_name, config)
    try:
        alias = engine.data_alias(base["database"], base["schema"], base["table"])
        before = engine.as_of(alias).value
        relation = DuckDBCatalog(engine).describe(base["database"], base["schema"], base["table"])
        after = engine.as_of(alias).value
        if before != after:
            raise engine_error(
                "SNAPSHOT_CHANGED", f"{engine.attach_path(alias)} changed during diff"
            )

        old_columns = _model_columns(model, sidecar)
        new_columns = _current_columns(relation.columns)
        column_rows = _column_rows(old_columns, new_columns)
        digest_changed = sidecar.get("relation", {}).get("columns_digest") != relation.columns_digest
        if digest_changed and not column_rows:
            column_rows.append(
                (
                    "type-changed",
                    "(columns_digest)",
                    str(sidecar.get("relation", {}).get("columns_digest")),
                    relation.columns_digest,
                )
            )

        now = utc_now()
        objs, entries, ignored, primary_key = generate(relation, model_name, now)
        new_sidecar = _sidecar(
            engine, relation, model_name, alias, entries, ignored, now, before, model
        )
        try:
            merged = refresh_merge_detailed(
                model, sidecar, objs, entries, primary_key, new_sidecar,
                {c.name for c in relation.columns}, model_name,
            )
            broken = list(merged.broken)
            merged_table = (merged.model.get("tables") or [{}])[0]
        except AgentBiError as exc:
            if exc.code != "NAME_COLLISION":  # e.g. a new column collides with an operator object
                raise
            broken = [f"refresh would refuse: {exc}"]
            merged_table = table

        object_rows: list[tuple[str, str]] = []
        for kind in _KINDS:
            old_by = {o["name"]: o for o in table.get(kind) or []}
            new_by = {o["name"]: o for o in merged_table.get(kind) or []}
            for name in sorted(new_by.keys() - old_by.keys()):
                object_rows.append(("added", f"tables[0].{kind}[{name}]"))
            for name in sorted(old_by.keys() - new_by.keys()):
                object_rows.append(("removed", f"tables[0].{kind}[{name}]"))
            for name in sorted(old_by.keys() & new_by.keys()):
                if old_by[name] != new_by[name]:
                    object_rows.append(("updated", f"tables[0].{kind}[{name}]"))

        old_rows = sidecar.get("relation", {}).get("row_count")
        new_rows = relation.row_count
        row_changed = old_rows != new_rows
        structural = bool(column_rows or object_rows or broken)

        lines = [f"# diff {model_name}", ""]
        if structural:
            lines.append("structural drift detected")
        elif row_changed:
            lines.append(f"no structural drift; row count changed {old_rows} -> {new_rows}")
        else:
            lines.append("no drift")

        lines += ["", "## Columns", "", "| Change | Column | Model | Catalog |", "|---|---|---|---|"]
        if column_rows:
            lines += [f"| {change} | {name} | {old} | {new} |" for change, name, old, new in column_rows]
        else:
            lines.append("| -- | -- | -- | -- |")

        lines += [
            "", "## Relation", "", "| Field | Model | Catalog |", "|---|---|---|",
            f"| row_count | {old_rows} | {new_rows} |",
            (
                f"| columns_digest | {sidecar.get('relation', {}).get('columns_digest')} "
                f"| {relation.columns_digest} |"
            ),
        ]

        lines += [
            "", "## Refresh preview", "", "| Object | Change |", "|---|---|",
        ]
        if object_rows or broken:
            lines += [f"| {name} | {change} |" for change, name in object_rows]
            lines += [f"| {entry} | refresh would break |" for entry in broken]
        else:
            lines.append("| -- | -- |")
        lines.append("")
        return DiffResult(lines, 1 if structural else 0)
    finally:
        engine.close()
