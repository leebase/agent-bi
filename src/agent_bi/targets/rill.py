"""SemanticModel -> the generated Rill project files (architecture §5.4, §5.4.1, ADR-009).

``render`` is a pure function: model dict + sidecar dict + the relation's catalog columns in
ordinal order + the source path relative to ``rill/`` -> ``{path under rill/: text}``. It
never translates or regenerates a metric formula (AGENTS.md, Rill invariant, D389): a
measure's ``expression`` is the metric ``expr`` exactly as accepted in the model.

The exact layout of the files is fixed by ``tests/golden/rill/idle_warehouse_waste/``; each
generated file additionally begins with the ADR-009 header line.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from agent_bi.semantic.provenance import entry_key

# Must equal the version token of docs/pinned/rill-version.txt (a test asserts it).
RILL_SCHEMA_PIN = "v0.90.5"

HEADER_TEXT = (
    "agent-bi: generated from semantic/{model}.yaml — do not edit; "
    "edit the semantic model and re-run compile-rill"
)

_COL = r'"([A-Za-z_][A-Za-z0-9_]*)"'
_PLAIN_COL_RE = re.compile(rf"^{_COL}$")
_CAST_DATE_RE = re.compile(rf"^(?i:CAST)\({_COL} (?i:AS) (?i:DATE)\)$")

KPI_COUNT = 4
BREAKDOWN_COUNT = 2


def generated_paths(model: str) -> list[str]:
    """Paths (under the Rill project dir) that ``compile-rill`` owns for ``model``; the
    manifest lists exactly these, in the §5.14 publish order (``fs_txn.publish_rank`` then path).
    ``rill/provenance/*`` is owned too but is not hashed."""
    return [
        "rill.yaml",
        f"models/{model}.sql",
        f"metrics/{model}.yaml",
        f"dashboards/{model}_default_canvas.yaml",
        f"dashboards/{model}_explore.yaml",
    ]


PROJECT_HEADER_LINE = (
    "# agent-bi: generated project file \u2014 do not edit; re-run compile-rill\n"
)


def yaml_header(model: str) -> str:
    return f"# {HEADER_TEXT.format(model=model)}\n"


def sql_header(model: str) -> str:
    return f"-- {HEADER_TEXT.format(model=model)}\n"


def strip_header(text: str) -> str:
    """Remove the generated header (the first line) from a generated file's text."""
    return text.split("\n", 1)[1] if "\n" in text else ""


def _y(value: str) -> str:
    """One YAML scalar, plain when YAML allows it, quoted otherwise."""
    out = yaml.safe_dump(value, width=1 << 30, allow_unicode=True)
    return out.removesuffix("\n").removesuffix("...").rstrip("\n")


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def column_of(expr: str) -> str:
    """Physical column named by a dimension or time-dimension expr (§5.3.5 grammar)."""
    m = _PLAIN_COL_RE.match(expr) or _CAST_DATE_RE.match(expr)
    if not m:
        raise ValueError(f"expr {expr!r} is outside the dimension grammar")
    return m.group(1)


def render_model_sql(
    model: str, table: dict[str, Any], columns: list[str], source_path: str, source_table: str
) -> str:
    """``rill/models/<model>.sql`` (§5.4 row 1): every catalog column in ordinal order, one per
    line; a dimension or time dimension written as ``CAST("c" AS DATE)`` keeps that cast. A
    time dimension whose name differs from its column is aliased to its name, so the name the
    Metrics SQL uses is the name of the column Rill sees (identity when they are equal)."""
    cast_cols: set[str] = set()
    time_alias: dict[str, str] = {}
    for kind in ("dimensions", "time_dimensions"):
        for d in table.get(kind) or []:
            col = column_of(d["expr"])
            if _CAST_DATE_RE.match(d["expr"]):
                cast_cols.add(col)
            if kind == "time_dimensions" and d["name"] != col:
                time_alias[col] = d["name"]
    lines = []
    for col in columns:
        expr = f'CAST("{col}" AS DATE)' if col in cast_cols else f'"{col}"'
        alias = time_alias.get(col, col if col in cast_cols else None)
        lines.append(f'  {expr} AS "{alias}"' if alias is not None else f"  {expr}")
    body = ",\n".join(lines)
    return (
        sql_header(model)
        + f"SELECT\n{body}\nFROM sqlite_scan({_sql_str(source_path)}, {_sql_str(source_table)})\n"
    )


def render_rill_yaml(model: str, project_name: str) -> str:
    """The project-level file: identical whichever model is compiled, so its header is
    model-neutral (D395)."""
    return (
        PROJECT_HEADER_LINE
        + f"compiler: rillv1\n\ndisplay_name: {_y(project_name)}\n\nolap_connector: duckdb\n"
    )


def render_metrics_view(model: str, table: dict[str, Any]) -> str:
    out = [yaml_header(model).rstrip("\n"), "type: metrics_view", f"display_name: {_y(model)}",
           f"model: {_y(model)}"]
    time_dims = table.get("time_dimensions") or []
    if time_dims:
        out.append(f"timeseries: {_y(time_dims[0]['name'])}")
    out += ["first_day_of_week: 1", "explore:", "  skip: true"]
    dims = table.get("dimensions") or []
    if dims:
        out.append("dimensions:")
        for d in dims:
            out += [f"  - name: {_y(d['name'])}", f"    column: {_y(column_of(d['expr']))}"]
    metrics = table.get("metrics") or []
    if metrics:
        out.append("measures:")
        for m in metrics:
            out += [f"  - name: {_y(m['name'])}", f"    expression: {_y(m['expr'])}"]
    return "\n".join(out) + "\n"


def render_explore(model: str) -> str:
    return (
        yaml_header(model)
        + f"type: explore\ndisplay_name: {_y(model)}\nmetrics_view: {_y(model)}\n"
        "defaults:\n  time_range: inf\n"
    )


def _kpi_items(model: str, metrics: list[str]) -> list[str]:
    out = []
    for m in metrics:
        out += ["      - kpi_grid:", f"          metrics_view: {_y(model)}", "          measures:",
                f"            - {_y(m)}", "        width: 3"]
    return out


def render_default_canvas(model: str, table: dict[str, Any], sidecar: dict[str, Any]) -> str:
    """The default canvas (§5.4.1, deterministic). No title keys, ``defaults: {time_range: inf}``,
    no default filters."""
    metrics = [m["name"] for m in table.get("metrics") or []]
    dims = [d["name"] for d in table.get("dimensions") or []]
    time_dims = [d["name"] for d in table.get("time_dimensions") or []]
    entries = sidecar.get("entries") or {}
    first = metrics[:1]
    rows: list[list[str]] = []
    if metrics:
        rows.append(["  - items:", *_kpi_items(model, metrics[:KPI_COUNT]), "    height: 128px"])
    if first and time_dims:
        rows.append([
            "  - items:", "      - line_chart:", f"          metrics_view: {_y(model)}",
            "          x:", f"            field: {_y(time_dims[0])}", "            type: temporal",
            "          y:", f"            field: {_y(first[0])}", "            type: quantitative",
            "        width: 12", "    height: 320px",
        ])
    if first:
        breakdown = [
            d for d in dims
            if (entries.get(entry_key("dimensions", d)) or {}).get("unique") is False
        ][:BREAKDOWN_COUNT]
        if breakdown:
            items = ["  - items:"]
            for d in breakdown:
                items += [
                    "      - bar_chart:", f"          metrics_view: {_y(model)}",
                    "          x:", f"            field: {_y(d)}", "            type: nominal",
                    "          y:", f"            field: {_y(first[0])}",
                    "            type: quantitative", "        width: 6",
                ]
            rows.append([*items, "    height: 320px"])
    if dims:
        cols = [f"            - {_y(c)}" for c in [*dims, *metrics]]
        rows.append([
            "  - items:", "      - table:", f"          metrics_view: {_y(model)}",
            "          columns:", *cols, "        width: 12", "    height: 400px",
        ])
    lines = [yaml_header(model).rstrip("\n"), "type: canvas", f"display_name: {_y(model)}",
             "defaults:", "  time_range: inf", "rows:"]
    for r in rows:
        lines += r
    return "\n".join(lines) + "\n"


def render(
    model_dict: dict[str, Any],
    sidecar_dict: dict[str, Any],
    *,
    project_name: str,
    columns: list[str],
    source_path: str,
) -> dict[str, str]:
    """All generated Rill files for one model, keyed by path under the Rill project dir."""
    model = model_dict["name"]
    table = model_dict["tables"][0]
    source_table = table["base_table"]["table"]
    return {
        "rill.yaml": render_rill_yaml(model, project_name),
        f"models/{model}.sql": render_model_sql(model, table, columns, source_path, source_table),
        f"metrics/{model}.yaml": render_metrics_view(model, table),
        f"dashboards/{model}_explore.yaml": render_explore(model),
        f"dashboards/{model}_default_canvas.yaml": render_default_canvas(
            model, table, sidecar_dict
        ),
    }


def relative_source_path(attach_abs: Path, rill_dir_abs: Path) -> str:
    """The attach path made relative to the Rill project directory (POSIX separators)."""
    import os

    return Path(os.path.relpath(attach_abs, rill_dir_abs)).as_posix()
