"""Sprint 2 task 1: the SemanticQuery contract (architecture §4.3)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest
import yaml

from agent_bi.errors import AgentBiError
from agent_bi.query.contract import (
    canonical_json,
    parse_query,
    query_hash,
    validate_against_model,
)
from agent_bi.query.contract_doc import contract_doc
from tests.conftest import ROOT

EXAMPLE = {
    "schema_version": "semantic-query/1.0",
    "model": "idle_warehouse_waste",
    "table": "warehouse_metering_history",
    "metrics": ["total_credits_used"],
    "dimensions": ["warehouse_name"],
    "time": {"dimension": "usage_day", "grain": "day", "start": "2026-01-01", "end": "2026-01-31"},
    "filters": [{"dimension": "warehouse_name", "op": "in", "values": ["ANALYTICS_WH"]}],
    "named_filters": [],
    "order_by": [{"field": "total_credits_used", "direction": "desc"}],
    "limit": 1000,
}


@pytest.fixture
def model():
    return yaml.safe_load((ROOT / "semantic" / "idle_warehouse_waste.yaml").read_text())


def mutated(**changes):
    q = copy.deepcopy(EXAMPLE)
    q.update(changes)
    return q


def test_the_section_4_3_example_validates(model):
    q = parse_query(EXAMPLE)
    assert q.limit == 1000 and q.time.grain == "day"
    assert validate_against_model(EXAMPLE, model) is not None


def test_limit_over_the_cap_is_an_error():
    with pytest.raises(AgentBiError) as exc:
        parse_query(mutated(limit=200000))
    assert exc.value.full_code == "E_QUERY_INVALID" and exc.value.exit_code == 2
    assert "limit" in exc.value.message
    assert parse_query(mutated(limit=100000)).limit == 100000
    with pytest.raises(AgentBiError):
        parse_query(mutated(limit=0))


def test_regex_filter_op_is_an_error():
    bad = mutated(filters=[{"dimension": "warehouse_name", "op": "regex", "values": ["x.*"]}])
    with pytest.raises(AgentBiError) as exc:
        parse_query(bad)
    assert exc.value.full_code == "E_QUERY_INVALID"
    assert "filters.0.op" in exc.value.message


def test_hour_grain_is_a_contract_error():
    bad = mutated(time={"dimension": "usage_day", "grain": "hour"})
    with pytest.raises(AgentBiError) as exc:
        parse_query(bad)
    assert exc.value.full_code == "E_QUERY_INVALID"
    assert "time.grain" in exc.value.message


@pytest.mark.parametrize(
    "flt",
    [
        {"dimension": "w", "op": "between", "values": [1]},
        {"dimension": "w", "op": "between", "values": [1, 2, 3]},
        {"dimension": "w", "op": "=", "values": ["a", "b"]},
        {"dimension": "w", "op": "in", "values": []},
        {"dimension": "w", "op": "like", "values": []},
        {"dimension": "w", "op": "=", "values": [float("nan")]},
        {"dimension": "w", "op": "=", "values": [{"a": 1}]},
    ],
)
def test_filter_arity_and_value_types(flt):
    with pytest.raises(AgentBiError) as exc:
        parse_query(mutated(filters=[flt]))
    assert exc.value.full_code == "E_QUERY_INVALID"


def test_shape_rules():
    for bad in (
        mutated(metrics=[], dimensions=[]),
        mutated(unknown_key=1),
        mutated(time={"dimension": "usage_day", "grain": "day", "start": "2026-13-01"}),
        mutated(time={"dimension": "usage_day", "grain": "day", "end": "20260101"}),
        mutated(order_by=[{"field": "x", "direction": "sideways"}]),
        mutated(schema_version="semantic-query/2.0"),
    ):
        with pytest.raises(AgentBiError) as exc:
            parse_query(bad)
        assert exc.value.full_code == "E_QUERY_INVALID"
    # a pure dimension listing is allowed
    assert parse_query(mutated(metrics=[])).metrics == []


def test_not_json_and_size_cap():
    with pytest.raises(AgentBiError) as exc:
        parse_query("{nope")
    assert exc.value.full_code == "E_QUERY_INVALID"
    with pytest.raises(AgentBiError) as exc:
        parse_query(json.dumps(mutated(metrics=["m" * 1_100_000])))
    assert "cap" in exc.value.message
    with pytest.raises(AgentBiError):
        parse_query([1, 2])


def test_key_order_and_list_order_do_not_change_the_hash():
    a = mutated(
        metrics=["total_credits_used", "avg_credits_used"],
        dimensions=["warehouse_name"],
        named_filters=["x", "y"],
    )
    b = {k: a[k] for k in reversed(list(a))}
    b["metrics"] = list(reversed(a["metrics"]))
    b["named_filters"] = ["y", "x"]
    assert canonical_json(a) == canonical_json(b)
    assert query_hash(a) == query_hash(b)
    assert query_hash(a).startswith("sha256:") and len(query_hash(a)) == len("sha256:") + 64


def test_a_real_difference_changes_the_hash():
    base = query_hash(EXAMPLE)
    assert query_hash(mutated(limit=10)) != base
    assert query_hash(mutated(metrics=["avg_credits_used"])) != base
    flipped = mutated(order_by=[{"field": "total_credits_used", "direction": "asc"}])
    assert query_hash(flipped) != base


def test_order_by_order_is_kept_and_default_limit_is_materialized():
    two = mutated(
        metrics=["total_credits_used", "avg_credits_used"],
        order_by=[{"field": "total_credits_used", "direction": "desc"},
                  {"field": "avg_credits_used", "direction": "asc"}],
    )
    swapped = copy.deepcopy(two)
    swapped["order_by"].reverse()
    assert query_hash(two) != query_hash(swapped)
    without_limit = {k: v for k, v in EXAMPLE.items() if k != "limit"}
    assert query_hash(without_limit) == query_hash(EXAMPLE)  # default limit is 1000


def test_filters_are_sorted_by_dimension_op_values():
    f1 = {"dimension": "warehouse_name", "op": "in", "values": ["B"]}
    f2 = {"dimension": "warehouse_name", "op": "=", "values": ["A"]}
    assert query_hash(mutated(filters=[f1, f2])) == query_hash(mutated(filters=[f2, f1]))


def test_unknown_names_list_the_valid_names(model):
    with pytest.raises(AgentBiError) as exc:
        validate_against_model(mutated(dimensions=["warehouse"]), model)
    assert exc.value.full_code == "E_UNKNOWN_FIELD"
    assert "'warehouse'" in exc.value.message and "valid dimensions: warehouse_name" in exc.value.message
    with pytest.raises(AgentBiError) as exc:
        validate_against_model(mutated(metrics=["nope"]), model)
    assert "avg_credits_used" in exc.value.message and "total_credits_used" in exc.value.message
    with pytest.raises(AgentBiError) as exc:
        validate_against_model(mutated(time={"dimension": "warehouse_name", "grain": "day"}), model)
    assert exc.value.full_code == "E_UNKNOWN_FIELD" and "usage_day" in exc.value.message
    with pytest.raises(AgentBiError) as exc:
        validate_against_model(mutated(named_filters=["big"]), model)
    assert exc.value.full_code == "E_UNKNOWN_FIELD"
    with pytest.raises(AgentBiError) as exc:
        validate_against_model(mutated(order_by=[{"field": "row_count", "direction": "asc"}]), model)
    assert exc.value.full_code == "E_UNKNOWN_FIELD"
    with pytest.raises(AgentBiError):
        validate_against_model(mutated(table="other"), model)
    with pytest.raises(AgentBiError) as exc:
        validate_against_model(mutated(model="other"), model)
    assert exc.value.full_code == "E_QUERY_INVALID"


def test_time_bucket_is_orderable_only_with_time(model):
    ok = mutated(order_by=[{"field": "time_bucket", "direction": "asc"}])
    validate_against_model(ok, model)
    no_time = {k: v for k, v in ok.items() if k != "time"}
    with pytest.raises(AgentBiError):
        validate_against_model(no_time, model)


def test_contract_doc_is_generated_and_committed():
    committed = (ROOT / "docs" / "semantic-query-contract.md").read_text()
    assert committed == contract_doc()
    out = subprocess.run(
        [sys.executable, "-m", "agent_bi.main", "contract", "--print"],
        check=True, capture_output=True, text=True, cwd=ROOT,
    ).stdout
    assert out == committed
    assert '"total_credits_used"' in committed and "$defs" in committed
