"""Vega-Lite chart builder (architecture §5.9). Deterministic: the spec is a pure function of
the canonical SemanticQuery, the model (for number formats), the requested mark and the theme --
never of the rows, so ``out/<hash>.vl.json`` is a byte-identical artifact (ADR-007 a).

The spec binds its data by name (``"data": {"name": "query1"}``): Rill's ``custom_chart`` exposes
its ``metrics_sql[0]`` result as the dataset ``query1`` (``docs/pinned/rill-custom-chart-v0.90.5.md``),
and ``save`` writes this spec into the component unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent_bi.errors import validation_error

SCHEMA_URL = "https://vega.github.io/schema/vega-lite/v5.json"
DATA_NAME = "query1"
MARKS = ("line", "bar", "area", "point", "table")
DEFAULT_THEME = "default"
_THEME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Shapes (§5.9 table) and the marks each accepts under ``--mark``. A line or area across an
# unordered nominal axis would mislead, a bar across time is not in the plan: both refused.
_COMPATIBLE: dict[str, tuple[str, ...]] = {
    "timeseries": ("line", "area", "point", "table"),
    "timeseries_by_dim": ("line", "area", "point", "table"),
    "bars": ("bar", "point", "table"),
    "bars_grouped": ("bar", "point", "table"),
    "kpi": ("table",),
    "none": ("table",),
}
_DEFAULT_MARK = {
    "timeseries": "line", "timeseries_by_dim": "line", "bars": "bar", "bars_grouped": "bar",
    "kpi": "text",
}


def shape_of(query: Any) -> str:
    """The §5.9 result shape of a (canonical) SemanticQuery."""
    has_time = query.time is not None
    n_dims, n_metrics = len(query.dimensions), len(query.metrics)
    if has_time and n_metrics == 1 and n_dims == 0:
        return "timeseries"
    if has_time and n_metrics == 1 and n_dims == 1:
        return "timeseries_by_dim"
    if not has_time and n_dims == 1 and n_metrics == 1:
        return "bars"
    if not has_time and n_dims == 1 and n_metrics >= 2:
        return "bars_grouped"
    if not has_time and n_dims == 0 and n_metrics == 1:
        return "kpi"
    return "none"


def load_theme(project_dir: Path, name: str | None) -> dict[str, Any]:
    name = name or DEFAULT_THEME
    if not _THEME_RE.match(name):
        raise validation_error("THEME_NOT_FOUND", f"theme name {name!r} must match ^[a-z][a-z0-9_-]*$")
    path = Path(project_dir) / "themes" / f"{name}.json"
    if not path.is_file():
        raise validation_error("THEME_NOT_FOUND", f"theme {name!r}: {path} does not exist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise validation_error("THEME_NOT_FOUND", f"theme {name!r}: {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise validation_error("THEME_NOT_FOUND", f"theme {name!r}: {path} must hold a JSON object")
    return data


def _number_format(metrics: list[str], model: dict[str, Any]) -> str:
    """``.2f`` for a metric named ``avg_*`` or whose expr is a ratio (contains ``/``), else
    ``,.0f`` (§5.9). Several metrics on one axis: ``.2f`` if any of them needs it."""
    table = (model.get("tables") or [{}])[0]
    exprs = {m["name"]: m["expr"] for m in table.get("metrics") or []}
    for name in metrics:
        if name.startswith("avg_") or "/" in exprs.get(name, ""):
            return ".2f"
    return ",.0f"


def _tip(field: str, type_: str) -> dict[str, str]:
    return {"field": field, "type": type_}


def build_chart(
    query: Any, model: dict[str, Any], *, mark: str | None = None,
    theme: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """``(vega_lite_spec | None, note | None)`` for a canonical SemanticQuery. ``note`` is
    ``"table only"`` whenever there is no chart."""
    if mark is not None and mark not in MARKS:
        raise validation_error(
            "CHART_INCOMPATIBLE", f"--mark {mark!r} is not one of {', '.join(MARKS)}"
        )
    shape = shape_of(query)
    if mark is not None and mark not in _COMPATIBLE[shape]:
        raise validation_error(
            "CHART_INCOMPATIBLE",
            f"--mark {mark} does not fit this answer's shape ({shape}); "
            f"compatible: {', '.join(_COMPATIBLE[shape])}",
        )
    if shape == "none" or mark == "table":
        return None, "table only"
    chosen = mark or _DEFAULT_MARK[shape]
    fmt = _number_format(list(query.metrics), model)
    metric = query.metrics[0]
    dim = query.dimensions[0] if query.dimensions else None
    transform: list[dict[str, Any]] | None = None
    if shape in ("timeseries", "timeseries_by_dim"):
        x = {"field": "time_bucket", "type": "temporal"}
        y = {"field": metric, "type": "quantitative", "axis": {"format": fmt}}
        enc: dict[str, Any] = {"x": x, "y": y}
        tips = [_tip("time_bucket", "temporal")]
        if dim:
            enc["color"] = {"field": dim, "type": "nominal"}
            tips.append(_tip(dim, "nominal"))
        tips.append(_tip(metric, "quantitative"))
        enc["tooltip"] = tips
    elif shape == "bars":
        enc = {
            "x": {"field": dim, "type": "nominal", "sort": "-y"},
            "y": {"field": metric, "type": "quantitative", "axis": {"format": fmt}},
            "tooltip": [_tip(dim, "nominal"), _tip(metric, "quantitative")],
        }
    elif shape == "bars_grouped":
        transform = [{"fold": list(query.metrics), "as": ["metric", "value"]}]
        enc = {
            "x": {"field": dim, "type": "nominal"},
            "y": {"field": "value", "type": "quantitative", "axis": {"format": fmt}},
            "column": {"field": "metric", "type": "nominal"},
            "tooltip": [_tip(dim, "nominal"), _tip("metric", "nominal"), _tip("value", "quantitative")],
        }
    else:  # kpi: a one-row table rendered as a text mark
        enc = {"text": {"field": metric, "type": "quantitative", "format": fmt}}
    spec: dict[str, Any] = {"$schema": SCHEMA_URL, "data": {"name": DATA_NAME}}
    if transform:
        spec["transform"] = transform
    spec["mark"] = chosen
    spec["encoding"] = enc
    spec.update({"width": "container", "height": "container", "autosize": {"type": "fit"}})
    spec["config"] = dict(theme or {})
    return spec, None
