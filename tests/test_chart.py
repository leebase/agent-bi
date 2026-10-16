"""Sprint 4 task 2: the Vega-Lite builder (architecture §5.9)."""

from __future__ import annotations

import json

import pytest

from agent_bi.errors import AgentBiError
from agent_bi.explore.chart import build_chart, load_theme, shape_of
from agent_bi.query.contract import parse_query
from agent_bi.semantic.model import load_model_dict
from tests.conftest import ROOT
from tests.s3_helpers import MODEL

MODEL_DICT = load_model_dict(str(ROOT / "semantic" / f"{MODEL}.yaml"))
BY_DAY = {"dimension": "usage_day", "grain": "day"}


def q(metrics, dimensions=(), time=None):
    d = {"schema_version": "semantic-query/1.0", "model": MODEL, "table": "warehouse_metering_history",
         "metrics": list(metrics), "dimensions": list(dimensions)}
    if time:
        d["time"] = time
    return parse_query(d).canonical()


def chart(query, **kw):
    return build_chart(query, MODEL_DICT, theme=load_theme(ROOT, kw.pop("theme", None)), **kw)


def test_shape_table_rows_yield_the_stated_marks():
    # time_bucket + 1 metric, 0 dims -> line, x temporal, y quantitative
    spec, note = chart(q(["total_credits_used"], time=BY_DAY))
    assert note is None and spec["mark"] == "line"
    assert spec["encoding"]["x"] == {"field": "time_bucket", "type": "temporal"}
    assert spec["encoding"]["y"]["field"] == "total_credits_used"
    assert spec["encoding"]["y"]["type"] == "quantitative"
    assert "color" not in spec["encoding"]
    # time_bucket + 1 metric, 1 dim -> line with color: dim
    spec, _ = chart(q(["total_credits_used"], ["warehouse_name"], BY_DAY))
    assert spec["mark"] == "line"
    assert spec["encoding"]["color"] == {"field": "warehouse_name", "type": "nominal"}
    # 0 time, 1 dim, 1 metric -> bar, x nominal sorted by -y
    spec, _ = chart(q(["total_credits_used"], ["warehouse_name"]))
    assert spec["mark"] == "bar"
    assert spec["encoding"]["x"] == {"field": "warehouse_name", "type": "nominal", "sort": "-y"}
    assert spec["encoding"]["y"]["field"] == "total_credits_used"
    # 0 time, 1 dim, >=2 metrics -> bar, grouped via a column facet per metric
    spec, _ = chart(q(["total_credits_used_compute", "total_credits_used_cloud_services"],
                      ["warehouse_name"]))
    assert spec["mark"] == "bar"
    assert spec["transform"] == [{"fold": [
        "total_credits_used_cloud_services", "total_credits_used_compute"], "as": ["metric", "value"]}]
    assert spec["encoding"]["column"] == {"field": "metric", "type": "nominal"}
    # 0 time, 0 dims, 1 metric -> text KPI
    spec, _ = chart(q(["row_count"]))
    assert spec["mark"] == "text"
    assert spec["encoding"]["text"]["field"] == "row_count"


def test_anything_else_is_table_only():
    for query in (
        q(["total_credits_used", "row_count"], time=BY_DAY),  # time + 2 metrics
        q(["row_count", "total_credits_used"]),               # no dims, 2 metrics
    ):
        spec, note = chart(query)
        assert spec is None and note == "table only"


def test_a_pure_dimension_listing_is_table_only():
    d = parse_query({"schema_version": "semantic-query/1.0", "model": MODEL,
                     "table": "warehouse_metering_history", "dimensions": ["warehouse_name"]})
    assert shape_of(d.canonical()) == "none"
    assert chart(d.canonical()) == (None, "table only")


def test_two_dimensions_is_table_only():
    # only one dimension exists in the fixture; the shape rule does not depend on names
    from types import SimpleNamespace

    two = SimpleNamespace(time=None, metrics=["total_credits_used"], dimensions=["a", "b"])
    assert shape_of(two) == "none"


def test_mark_override_when_compatible():
    spec, _ = chart(q(["total_credits_used"], time=BY_DAY), mark="area")
    assert spec["mark"] == "area"
    spec, _ = chart(q(["total_credits_used"], time=BY_DAY), mark="point")
    assert spec["mark"] == "point"
    spec, _ = chart(q(["total_credits_used"], ["warehouse_name"]), mark="point")
    assert spec["mark"] == "point"
    assert chart(q(["total_credits_used"], ["warehouse_name"]), mark="table") == (None, "table only")


def test_incompatible_mark_is_e_chart_incompatible():
    with pytest.raises(AgentBiError) as exc:
        chart(q(["total_credits_used"], ["warehouse_name"]), mark="line")
    assert exc.value.code == "CHART_INCOMPATIBLE" and exc.value.exit_code == 2
    with pytest.raises(AgentBiError) as exc:
        chart(q(["row_count"]), mark="bar")
    assert exc.value.code == "CHART_INCOMPATIBLE"
    with pytest.raises(AgentBiError) as exc:
        chart(q(["total_credits_used"], time=BY_DAY), mark="sparkles")
    assert exc.value.code == "CHART_INCOMPATIBLE"


def test_number_formats():
    spec, _ = chart(q(["total_credits_used"], ["warehouse_name"]))
    assert spec["encoding"]["y"]["axis"]["format"] == ",.0f"
    spec, _ = chart(q(["avg_credits_used"], ["warehouse_name"]))
    assert spec["encoding"]["y"]["axis"]["format"] == ".2f"
    ratio = json.loads(json.dumps(MODEL_DICT))
    for m in ratio["tables"][0]["metrics"]:
        if m["name"] == "total_credits_used":
            m["expr"] = 'SUM("credits_used") / NULLIF(SUM("credits_used_compute"), 0)'
    spec, _ = build_chart(q(["total_credits_used"], ["warehouse_name"]), ratio)
    assert spec["encoding"]["y"]["axis"]["format"] == ".2f"


def test_data_is_bound_by_name_for_rill_and_never_carries_rows():
    spec, _ = chart(q(["total_credits_used"], ["warehouse_name"]))
    assert spec["data"] == {"name": "query1"}
    assert "values" not in json.dumps(spec)
    assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")


def test_default_theme_is_the_empty_vega_config(tmp_path):
    assert (ROOT / "themes" / "default.json").read_text().strip() == "{}"
    spec, _ = chart(q(["total_credits_used"], ["warehouse_name"]))
    assert spec["config"] == {}


def test_a_named_theme_becomes_the_config(tmp_path):
    (tmp_path / "themes").mkdir()
    (tmp_path / "themes" / "dark.json").write_text('{"background": "#111"}')
    spec, _ = build_chart(q(["row_count"]), MODEL_DICT, theme=load_theme(tmp_path, "dark"))
    assert spec["config"] == {"background": "#111"}
    for bad in ("nope", "../x"):
        with pytest.raises(AgentBiError) as exc:
            load_theme(tmp_path, bad)
        assert exc.value.code == "THEME_NOT_FOUND"


def test_deterministic():
    for query in (q(["total_credits_used"], time=BY_DAY), q(["total_credits_used"], ["warehouse_name"]),
                  q(["row_count"])):
        a = json.dumps(chart(query)[0], sort_keys=False)
        b = json.dumps(chart(query)[0], sort_keys=False)
        assert a == b
