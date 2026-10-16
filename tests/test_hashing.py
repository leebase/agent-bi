"""Sprint 2 task 1: semantic digest (architecture §4.7)."""

from __future__ import annotations

import copy

import pytest
import yaml

from agent_bi.errors import AgentBiError
from agent_bi.hashing import (
    canonical_dumps,
    referenced_definitions,
    semantic_digest,
    sha256_text,
)
from agent_bi.query.contract import parse_query
from tests.conftest import ROOT

QUERY = {
    "model": "idle_warehouse_waste",
    "table": "warehouse_metering_history",
    "metrics": ["total_credits_used"],
    "dimensions": ["warehouse_name"],
    "time": {"dimension": "usage_day", "grain": "day"},
}


@pytest.fixture
def model():
    return yaml.safe_load((ROOT / "semantic" / "idle_warehouse_waste.yaml").read_text())


def metric(model, name):
    return next(m for m in model["tables"][0]["metrics"] if m["name"] == name)


def test_digest_changes_when_a_referenced_metric_expr_changes(model):
    before = semantic_digest(parse_query(QUERY), model)
    changed = copy.deepcopy(model)
    metric(changed, "total_credits_used")["expr"] = 'SUM("credits_used_compute")'
    assert semantic_digest(parse_query(QUERY), changed) != before


def test_digest_is_unchanged_by_description_or_synonyms(model):
    before = semantic_digest(parse_query(QUERY), model)
    changed = copy.deepcopy(model)
    m = metric(changed, "total_credits_used")
    m["description"] = "something entirely different"
    m["synonyms"] = ["spend", "burn"]
    changed["tables"][0]["dimensions"][0]["description"] = "renamed prose"
    changed["tables"][0]["dimensions"][0]["synonyms"] = ["wh"]
    assert semantic_digest(parse_query(QUERY), changed) == before


def test_digest_ignores_definitions_the_query_does_not_reference(model):
    before = semantic_digest(parse_query(QUERY), model)
    changed = copy.deepcopy(model)
    metric(changed, "avg_credits_used")["expr"] = 'AVG("credits_used_compute")'
    assert semantic_digest(parse_query(QUERY), changed) == before


def test_digest_changes_with_the_base_table_or_a_dimension_expr(model):
    before = semantic_digest(parse_query(QUERY), model)
    moved = copy.deepcopy(model)
    moved["tables"][0]["base_table"]["schema"] = "OTHER"
    assert semantic_digest(parse_query(QUERY), moved) != before
    dim = copy.deepcopy(model)
    dim["tables"][0]["time_dimensions"][0]["expr"] = '"usage_day"'
    assert semantic_digest(parse_query(QUERY), dim) != before


def test_digest_is_the_hash_of_base_table_and_definitions(model):
    q = parse_query(QUERY)
    defs = referenced_definitions(q, model)
    assert defs == {
        "total_credits_used": {"kind": "metric", "expr": 'SUM("credits_used")'},
        "usage_day": {"kind": "time_dimension", "expr": 'CAST("usage_day" AS DATE)'},
        "warehouse_name": {"kind": "dimension", "expr": '"warehouse_name"'},
    }
    expected = sha256_text(
        canonical_dumps({"base_table": model["tables"][0]["base_table"], "definitions": defs})
    )
    assert semantic_digest(q, model) == expected


def test_filter_dimensions_and_named_filters_are_referenced(model):
    q = parse_query(
        {**QUERY, "metrics": ["row_count"], "dimensions": [], "time": None,
         "filters": [{"dimension": "warehouse_name", "op": "=", "values": ["A"]}]}
    )
    assert set(referenced_definitions(q, model)) == {"row_count", "warehouse_name"}
    with_filter = copy.deepcopy(model)
    with_filter["tables"][0]["filters"] = [{"name": "big", "expr": '"credits_used" > 100'}]
    q2 = parse_query({**QUERY, "named_filters": ["big"]})
    assert referenced_definitions(q2, with_filter)["big"] == {
        "kind": "filter", "expr": '"credits_used" > 100'
    }


def test_unknown_reference_is_an_error(model):
    q = parse_query({**QUERY, "metrics": ["nope"]})
    with pytest.raises(AgentBiError) as exc:
        semantic_digest(q, model)
    assert exc.value.full_code == "E_UNKNOWN_FIELD"
