"""Sprint 2 task 2: the SemanticQuery compiler (architecture §5.3)."""

from __future__ import annotations

import copy
import json
import re
import sqlite3

import pytest
import yaml

from agent_bi.config import load_config
from agent_bi.engines import DuckDBEngine
from agent_bi.errors import AgentBiError
from agent_bi.query.compile import (
    DIALECTS,
    CompiledQuery,
    compile,
    executed_sql,
)
from agent_bi.query.contract import query_hash
from tests.conftest import ROOT
from tests.test_scan import MODEL, load_config_for, run_scan

KA_DIR = ROOT / "tests" / "fixtures" / "known-answers" / "idle_warehouse_waste"
GOLDEN = ROOT / "tests" / "golden" / "sql"
NAMES = sorted(p.stem for p in KA_DIR.glob("*.json"))
CONFIG = load_config(ROOT / "agent-bi.yaml")
RELATION_MAP = CONFIG.engines.duckdb.relation_map


@pytest.fixture(scope="module")
def model():
    return yaml.safe_load((ROOT / "semantic" / "idle_warehouse_waste.yaml").read_text())


def known_query(name: str) -> dict:
    return json.loads((KA_DIR / f"{name}.json").read_text())["semantic_query"]


def q(**changes) -> dict:
    base = {
        "schema_version": "semantic-query/1.0",
        "model": "idle_warehouse_waste",
        "table": "warehouse_metering_history",
        "metrics": ["total_credits_used"],
        "dimensions": ["warehouse_name"],
    }
    base.update(changes)
    return base


def sql_of(query, model, dialect="duckdb") -> str:
    return compile(query, model, dialect, relation_map=RELATION_MAP).compiled_sql


# ----------------------------------------------------------------------------- (a) goldens


def test_there_are_eight_known_answers_and_twenty_four_goldens():
    assert len(NAMES) == 9
    assert len(list(GOLDEN.glob("*.sql"))) == 27


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name", NAMES)
def test_golden_sql(name, dialect, model):
    expected = (GOLDEN / f"{name}.{dialect}.sql").read_text()
    assert sql_of(known_query(name), model, dialect) == expected


def test_no_golden_has_an_as_of_clause():
    for path in GOLDEN.glob("*.sql"):
        assert "AT(TIMESTAMP" not in path.read_text(), path.name


def test_rill_goldens_carry_no_aggregate_and_no_group_by():
    """The Rill invariant (D389): the rill dialect projects names; it never emits an
    aggregate or a GROUP BY (and so never a translated metric formula)."""
    agg = re.compile(r"\b(?:SUM|AVG|MIN|MAX|COUNT)\s*\(", re.IGNORECASE)
    files = list(GOLDEN.glob("*.rill.sql"))
    assert len(files) == 9
    for path in files:
        text = path.read_text()
        assert not agg.search(text), path.name
        assert "GROUP BY" not in text.upper(), path.name
        assert "\n" not in text and text.startswith("SELECT ") and " FROM idle_warehouse_waste " in text


def test_snowflake_goldens_use_the_physical_identity_and_try_to_date():
    for path in GOLDEN.glob("*.snowflake.sql"):
        text = path.read_text()
        assert '"FIXTURE"."MAIN"."warehouse_metering_history"' in text
        assert "CAST(" not in text
        if "usage_day" in text:
            assert 'TRY_TO_DATE("usage_day")' in text


def test_duckdb_goldens_use_the_relation_map():
    for path in GOLDEN.glob("*.duckdb.sql"):
        text = path.read_text()
        assert '"fixture"."main"."warehouse_metering_history"' in text
        assert "TRY_TO_DATE" not in text


# ----------------------------------------------------------------------------- (b) refusals


def test_refusals(model):
    bad = copy.deepcopy(model)
    next(m for m in bad["tables"][0]["metrics"] if m["name"] == "total_credits_used")["expr"] = (
        'SUM("a")*2'
    )
    for dialect in ("duckdb", "snowflake"):
        with pytest.raises(AgentBiError) as exc:
            sql_of(q(), bad, dialect)
        assert exc.value.full_code == "E_UNSUPPORTED_METRIC_EXPR"
        assert "total_credits_used" in exc.value.message

    with pytest.raises(AgentBiError) as exc:
        sql_of(q(dimensions=["warehouse"]), model)
    assert exc.value.full_code == "E_UNKNOWN_FIELD"
    assert "valid dimensions: warehouse_name" in exc.value.message

    with pytest.raises(AgentBiError) as exc:
        sql_of(q(time={"dimension": "usage_day", "grain": "hour"}), model)
    assert exc.value.full_code == "E_QUERY_INVALID" and exc.value.exit_code == 2

    for refused in (
        q(filters=[{"dimension": "warehouse_name", "op": "like", "values": ["A%"]}]),
        q(named_filters=["big"]),
        q(metrics=[]),
    ):
        scoped = copy.deepcopy(model)
        scoped["tables"][0]["filters"] = [{"name": "big", "expr": '"credits_used" > 100'}]
        with pytest.raises(AgentBiError) as exc:
            sql_of(refused, scoped, "rill")
        assert exc.value.full_code == "E_UNSUPPORTED_BY_RILL_ROUTE" and exc.value.exit_code == 2
        # the same query is fine on the other two dialects
        for dialect in ("duckdb", "snowflake"):
            assert sql_of(refused, scoped, dialect)


def test_unsupported_dimension_and_filter_exprs_are_refused(model):
    bad = copy.deepcopy(model)
    bad["tables"][0]["dimensions"][0]["expr"] = 'UPPER("warehouse_name")'
    with pytest.raises(AgentBiError) as exc:
        sql_of(q(), bad)
    assert exc.value.full_code == "E_UNSUPPORTED_DIMENSION_EXPR"
    bad = copy.deepcopy(model)
    bad["tables"][0]["filters"] = [{"name": "f", "expr": "CURRENT_DATE() > DATE '2020-01-01'"}]
    with pytest.raises(AgentBiError) as exc:
        sql_of(q(named_filters=["f"]), bad)
    assert exc.value.full_code == "E_UNSUPPORTED_FILTER_EXPR"
    bad = copy.deepcopy(model)
    next(m for m in bad["tables"][0]["metrics"] if m["name"] == "row_count")["expr"] = 'SUM("ghost")'
    with pytest.raises(AgentBiError) as exc:
        sql_of(q(metrics=["row_count"]), bad)
    assert exc.value.full_code == "E_UNSUPPORTED_METRIC_EXPR" and "ghost" in exc.value.message


def test_unmapped_relation_and_unknown_dialect(model):
    with pytest.raises(AgentBiError) as exc:
        compile(q(), model, "duckdb", relation_map={})
    assert exc.value.full_code == "E_RELATION_NOT_MAPPED"
    with pytest.raises(AgentBiError) as exc:
        compile(q(), model, "oracle")
    assert exc.value.full_code == "E_UNKNOWN_ENGINE"


def test_ratio_metrics_are_emitted_from_the_model_on_sql_dialects(model):
    ratio = copy.deepcopy(model)
    ratio["tables"][0]["metrics"].append(
        {"name": "compute_share", "expr": 'SUM("credits_used_compute") / NULLIF(SUM("credits_used"), 0)'}
    )
    text = sql_of(q(metrics=["compute_share"], dimensions=[]), ratio)
    assert 'SUM("credits_used_compute") / NULLIF(SUM("credits_used"), 0) AS "compute_share"' in text
    # rill: the name only, never the formula
    rill = sql_of(q(metrics=["compute_share"], dimensions=[]), ratio, "rill")
    assert rill == (
        "SELECT compute_share FROM idle_warehouse_waste ORDER BY compute_share DESC LIMIT 1000"
    )


# ----------------------------------------------------------------------------- clauses


def test_time_range_filters_order_and_limit(model):
    query = q(
        time={"dimension": "usage_day", "grain": "month", "start": "2026-06-01", "end": "2026-06-05"},
        filters=[
            {"dimension": "warehouse_name", "op": "in", "values": ["B", "A"]},
            {"dimension": "warehouse_name", "op": "!=", "values": ["C"]},
        ],
        order_by=[{"field": "total_credits_used", "direction": "asc"}],
        limit=5,
    )
    expected = (
        [
            "SELECT",
            '  "warehouse_name" AS "warehouse_name",',
            "  DATE_TRUNC('month', CAST(\"usage_day\" AS DATE)) AS \"time_bucket\",",
            '  SUM("credits_used") AS "total_credits_used"',
            'FROM "fixture"."main"."warehouse_metering_history"',
            "WHERE 1=1",
            "  AND CAST(\"usage_day\" AS DATE) >= DATE '2026-06-01'",
            "  AND CAST(\"usage_day\" AS DATE) < DATE '2026-06-05'",
            "  AND \"warehouse_name\" != 'C'",
            "  AND \"warehouse_name\" IN ('B','A')",
            'GROUP BY "warehouse_name", "time_bucket"',
            'ORDER BY "total_credits_used" ASC, "time_bucket" ASC, "warehouse_name" ASC',
            "LIMIT 5",
        ]
    )
    assert sql_of(query, model) == "\n".join(expected)
    assert sql_of(query, model, "rill") == (
        "SELECT warehouse_name, date_trunc('MONTH', usage_day) AS time_bucket, total_credits_used "
        "FROM idle_warehouse_waste "
        "WHERE usage_day >= '2026-06-01' AND usage_day < '2026-06-05' "
        "AND warehouse_name != 'C' AND warehouse_name IN ('B','A') "
        "ORDER BY total_credits_used ASC, time_bucket ASC, warehouse_name ASC LIMIT 5"
    )


def test_filter_operators_and_literals(model):
    cases = {
        ("=", ("A",)): "= 'A'",
        (">=", (3,)): ">= 3",
        ("<", (2.5,)): "< 2.5",
        ("=", (True,)): "= TRUE",
        ("not_in", ("a", "b")): "NOT IN ('a','b')",
        ("between", (1, 2)): "BETWEEN 1 AND 2",
        ("like", ("A%",)): "LIKE 'A%'",
    }
    for (op, values), tail in cases.items():
        text = sql_of(
            q(filters=[{"dimension": "warehouse_name", "op": op, "values": list(values)}]), model
        )
        assert f'AND "warehouse_name" {tail}' in text


def test_named_filters_are_appended_in_canonical_order(model):
    with_filters = copy.deepcopy(model)
    with_filters["tables"][0]["filters"] = [
        {"name": "zeta", "expr": '"credits_used" > 100'},
        {"name": "alpha", "expr": '"credits_used" < 500 AND "credits_used_compute" > 1'},
    ]
    text = sql_of(q(named_filters=["zeta", "alpha"]), with_filters)
    assert text.index('("credits_used" < 500 AND') < text.index('("credits_used" > 100)')
    assert "  AND (\"credits_used\" > 100)" in text


def test_pure_dimension_listing(model):
    text = sql_of(q(metrics=[], dimensions=["warehouse_name"]), model)
    expected = (
        [
            "SELECT DISTINCT",
            '  "warehouse_name" AS "warehouse_name"',
            'FROM "fixture"."main"."warehouse_metering_history"',
            "WHERE 1=1",
            'ORDER BY "warehouse_name" ASC',
            "LIMIT 1000",
        ]
    )
    assert text == "\n".join(expected)


# ----------------------------------------------------------------------------- (c) injection


def test_injection_quoting(model):
    evil = "x' OR 1=1 --"
    query = q(filters=[{"dimension": "warehouse_name", "op": "=", "values": [evil]}])
    for dialect in DIALECTS:
        text = sql_of(query, model, dialect)
        assert "'x'' OR 1=1 --'" in text
        # the only place the payload text occurs is inside that one literal
        assert text.count("OR 1=1") == 1
        assert text.replace("'x'' OR 1=1 --'", "").count("1=1") == (0 if dialect == "rill" else 1)  # WHERE 1=1
    # a Snowflake string literal treats `\` as an escape: it is doubled so the quote that
    # follows cannot be swallowed
    sneaky = "x\\' OR 1=1 --"
    text = sql_of(q(filters=[{"dimension": "warehouse_name", "op": "=", "values": [sneaky]}]), model, "snowflake")
    assert "'x\\\\'' OR 1=1 --'" in text
    # Rill's Metrics SQL treats `\` as an escape too: same doubling (A1)
    text = sql_of(q(filters=[{"dimension": "warehouse_name", "op": "=", "values": [sneaky]}]), model, "rill")
    assert "warehouse_name = 'x\\\\'' OR 1=1 --'" in text
    # a DuckDB literal does not treat backslash specially: it is passed through
    text = sql_of(q(filters=[{"dimension": "warehouse_name", "op": "=", "values": [sneaky]}]), model)
    assert "'x\\'' OR 1=1 --'" in text


def test_injection_payload_executes_as_data(project):
    run_scan(project)
    model = yaml.safe_load((project["semantic"] / f"{MODEL}.yaml").read_text())
    eng = DuckDBEngine(load_config_for(project))
    try:
        sql = compile(
            q(filters=[{"dimension": "warehouse_name", "op": "=", "values": ["x' OR 1=1 --"]}]),
            model, "duckdb", relation_map=eng._config.engines.duckdb.relation_map,
        ).compiled_sql
        columns, rows = eng.execute(sql)
    finally:
        eng.close()
    assert columns == ["warehouse_name", "total_credits_used"] and rows == []


# ----------------------------------------------------------------------------- (d)(e) determinism


def test_deterministic(model):
    for name in NAMES:
        for dialect in DIALECTS:
            first = sql_of(known_query(name), model, dialect)
            again = sql_of(copy.deepcopy(known_query(name)), copy.deepcopy(model), dialect)
            assert first == again


def test_canonical_order(model):
    a = q(
        metrics=["total_credits_used", "avg_credits_used", "row_count"],
        dimensions=["warehouse_name"],
        filters=[
            {"dimension": "warehouse_name", "op": "=", "values": ["A"]},
            {"dimension": "warehouse_name", "op": "in", "values": ["B"]},
        ],
    )
    b = copy.deepcopy(a)
    b["metrics"] = list(reversed(a["metrics"]))
    b["filters"] = list(reversed(a["filters"]))
    assert query_hash(a) == query_hash(b)
    for dialect in DIALECTS:
        assert sql_of(a, model, dialect) == sql_of(b, model, dialect)
    text = sql_of(a, model)
    assert text.index("avg_credits_used") < text.index("row_count") < text.index("total_credits_used")


# ----------------------------------------------------------------------------- (f) total order


def test_total_order(model):
    pattern = re.compile(r"ORDER BY (.*)\nLIMIT")
    for name in NAMES:
        sq = known_query(name)
        text = sql_of(sq, model)
        order = pattern.search(text).group(1)
        tie = []
        if sq.get("time"):
            tie.append('"time_bucket" ASC')
        tie += [f'"{m}" DESC' for m in sorted(sq["metrics"])]
        tie += [f'"{d}" ASC' for d in sorted(sq["dimensions"])]
        assert order == ", ".join(tie), name
    user = q(order_by=[{"field": "warehouse_name", "direction": "desc"}],
             metrics=["row_count", "total_credits_used"])
    order = pattern.search(sql_of(user, model)).group(1)
    assert order == '"warehouse_name" DESC, "row_count" DESC, "total_credits_used" DESC'


def test_total_order_makes_limit_deterministic_on_ties(project):
    con = sqlite3.connect(project["db"])
    con.execute(
        "UPDATE warehouse_metering_history SET credits_used = 5000 "
        "WHERE warehouse_name IN ('FINANCE_REPORTING_WH', 'CORE_ELT_WH')"
    )
    con.commit()
    con.close()
    run_scan(project)
    model = yaml.safe_load((project["semantic"] / f"{MODEL}.yaml").read_text())
    query = q(order_by=[{"field": "total_credits_used", "direction": "desc"}], limit=1)
    results = []
    for _ in range(2):
        eng = DuckDBEngine(load_config_for(project))
        try:
            sql = compile(query, model, "duckdb",
                          relation_map=eng._config.engines.duckdb.relation_map).compiled_sql
            results.append(eng.execute(sql)[1])
        finally:
            eng.close()
    assert results[0] == results[1]
    assert results[0][0][0] == "CORE_ELT_WH"  # alphabetically first of the two tied at 35000.0
    assert results[0][0][1] == 35000.0


# ----------------------------------------------------------------------------- (g) as-of split


def test_executed_sql_snowflake(model):
    compiled = compile(known_query("credits_by_warehouse"), model, "snowflake")
    assert isinstance(compiled, CompiledQuery)
    assert "AT(TIMESTAMP" not in compiled.compiled_sql
    executed = executed_sql(compiled, "snowflake", "2026-10-14T20:00:00Z")
    assert "AT(TIMESTAMP =>" in executed
    assert (
        'FROM "FIXTURE"."MAIN"."warehouse_metering_history" '
        "AT(TIMESTAMP => '2026-10-14T20:00:00Z'::TIMESTAMP_LTZ)\nWHERE 1=1" in executed
    )
    assert executed.replace(" AT(TIMESTAMP => '2026-10-14T20:00:00Z'::TIMESTAMP_LTZ)", "") == compiled.compiled_sql


def test_as_of_is_not_spliced_into_a_literal_that_looks_like_a_relation(model):
    tricky = q(filters=[{"dimension": "warehouse_name", "op": "=",
                         "values": ['"FIXTURE"."MAIN"."warehouse_metering_history"\nFROM']}])
    compiled = compile(tricky, model, "snowflake")
    executed = executed_sql(compiled, "snowflake", "2026-10-14T20:00:00Z")
    assert executed.count("AT(TIMESTAMP =>") == 1
    assert executed.index("AT(TIMESTAMP =>") < executed.index("WHERE 1=1")


def test_duckdb_and_rill_have_no_as_of_clause(model):
    for dialect in ("duckdb", "rill"):
        compiled = compile(known_query("credits_by_warehouse"), model, dialect, relation_map=RELATION_MAP)
        assert executed_sql(compiled, dialect, "2026-10-14T20:00:00Z") == compiled.compiled_sql


def test_executed_sql_needs_the_matching_engine_and_a_timestamp(model):
    compiled = compile(known_query("credits_by_warehouse"), model, "snowflake")
    with pytest.raises(AgentBiError):
        executed_sql(compiled, "duckdb", "2026-10-14T20:00:00Z")
    with pytest.raises(AgentBiError):
        executed_sql(compiled, "snowflake", None)
