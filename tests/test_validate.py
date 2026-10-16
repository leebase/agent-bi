from __future__ import annotations

import copy

import pytest
import yaml

from agent_bi.engines import DuckDBEngine
from agent_bi.errors import AgentBiError
from agent_bi.hashing import expr_sha256
from agent_bi.main import main
from agent_bi.semantic.model import dump_yaml
from agent_bi.semantic.scan import scan
from agent_bi.semantic.validate import validate, validate_model
from tests.conftest import load_config_for

MODEL = "idle_warehouse_waste"
REL = "FIXTURE.MAIN.warehouse_metering_history"


@pytest.fixture
def scanned(project):
    eng = DuckDBEngine(load_config_for(project))
    try:
        scan(eng, REL, MODEL, project["semantic"], now="2026-10-14T10:00:00Z")
    finally:
        eng.close()
    sem = project["semantic"]
    return (
        yaml.safe_load((sem / f"{MODEL}.yaml").read_text()),
        yaml.safe_load((sem / f"{MODEL}.provenance.yaml").read_text()),
    )


def check(model, side):
    validate_model(copy.deepcopy(model), copy.deepcopy(side), MODEL)


def expect(model, side, code):
    with pytest.raises(AgentBiError) as exc:
        check(model, side)
    assert exc.value.full_code == code
    assert exc.value.exit_code == 2
    return exc.value


def add_entry(side, kind, obj, status="confirmed"):
    side["entries"][f"tables[0].{kind}[{obj['name']}]"] = {
        "status": status, "origin": "human", "at": "2026-10-14T10:00:00Z", "by": "lee",
        "expr_sha256": expr_sha256(obj["expr"]),
    }


def test_valid_model_passes(scanned, project):
    check(*scanned)
    validate(MODEL, project["semantic"])


def test_cli_validate_exit_codes(scanned, project, capsys):
    argv = ["--config", str(project["config_path"]), "validate", "--model", MODEL]
    assert main(argv) == 0
    model, _side = scanned
    model["relationships"] = [{"name": "r", "left_table": "a", "right_table": "b"}]
    (project["semantic"] / f"{MODEL}.yaml").write_text(dump_yaml(model))
    capsys.readouterr()
    assert main(argv) == 2
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith("E_UNSUPPORTED_RELATIONSHIPS: ")


def test_missing_model_file(project):
    with pytest.raises(AgentBiError) as exc:
        validate("nope", project["semantic"])
    assert exc.value.full_code == "E_MODEL_NOT_FOUND"


def test_relationships_refused(scanned):
    model, side = scanned
    model["relationships"] = [
        {"name": "r", "left_table": "a", "right_table": "b",
         "relationship_columns": [{"left_column": "x", "right_column": "y"}]}
    ]
    expect(model, side, "E_UNSUPPORTED_RELATIONSHIPS")


def test_empty_relationships_is_accepted(scanned):
    model, side = scanned
    model["relationships"] = []
    check(model, side)


def test_multi_table_refused(scanned):
    model, side = scanned
    model["tables"].append(copy.deepcopy(model["tables"][0]) | {"name": "second"})
    expect(model, side, "E_UNSUPPORTED_MULTI_TABLE")


def test_base_table_definition_refused(scanned):
    model, side = scanned
    model["tables"][0]["base_table"] = {"definition": "SELECT 1 AS x"}
    expect(model, side, "E_UNSUPPORTED_BASE_TABLE_DEFINITION")


def test_metric_expr_outside_the_grammar(scanned):
    model, side = scanned
    metric = next(m for m in model["tables"][0]["metrics"] if m["name"] == "total_credits_used")
    metric["expr"] = "SUM(a)+1"
    err = expect(model, side, "E_UNSUPPORTED_METRIC_EXPR")
    assert "tables[0].metrics[total_credits_used]" in err.message


def test_metric_column_must_be_a_fact_or_dimension_column(scanned):
    model, side = scanned
    metric = next(m for m in model["tables"][0]["metrics"] if m["name"] == "total_credits_used")
    metric["expr"] = 'SUM("not_a_column")'
    err = expect(model, side, "E_UNSUPPORTED_METRIC_EXPR")
    assert "not_a_column" in err.message


def test_dimension_expr_outside_the_grammar(scanned):
    model, side = scanned
    model["tables"][0]["dimensions"][0]["expr"] = 'LOWER("warehouse_name")'
    err = expect(model, side, "E_UNSUPPORTED_DIMENSION_EXPR")
    assert "tables[0].dimensions[warehouse_name]" in err.message


def test_time_dimension_expr_outside_the_grammar(scanned):
    model, side = scanned
    model["tables"][0]["time_dimensions"][0]["expr"] = 'DATE_TRUNC(\'day\', "usage_day")'
    expect(model, side, "E_UNSUPPORTED_DIMENSION_EXPR")


def test_fact_expr_must_be_a_bare_column(scanned):
    model, side = scanned
    model["tables"][0]["facts"][0]["expr"] = '"credits_used" * 2'
    expect(model, side, "E_UNSUPPORTED_FACT_EXPR")


def test_filter_with_a_moving_value_is_refused(scanned):
    model, side = scanned
    flt = {"name": "recent", "expr": '"usage_day" >= CURRENT_DATE()'}
    model["tables"][0]["filters"] = [flt]
    add_entry(side, "filters", flt)
    err = expect(model, side, "E_UNSUPPORTED_FILTER_EXPR")
    assert "tables[0].filters[recent]" in err.message


def test_closed_grammar_filter_is_accepted(scanned):
    model, side = scanned
    flt = {"name": "two_wh", "expr": "\"warehouse_name\" IN ('A','B') AND \"credits_used\" > 0"}
    model["tables"][0]["filters"] = [flt]
    add_entry(side, "filters", flt)
    check(model, side)


def test_sidecar_missing_one_entry(scanned):
    model, side = scanned
    del side["entries"]["tables[0].metrics[avg_credits_used]"]
    err = expect(model, side, "E_PROVENANCE_MISMATCH")
    assert "tables[0].metrics[avg_credits_used]" in err.message


def test_sidecar_entry_without_an_object(scanned):
    model, side = scanned
    side["entries"]["tables[0].metrics[ghost]"] = copy.deepcopy(
        side["entries"]["tables[0].metrics[row_count]"]
    )
    err = expect(model, side, "E_PROVENANCE_MISMATCH")
    assert "ghost" in err.message


def test_hand_edited_metric_expr_without_updating_the_digest(scanned):
    model, side = scanned
    metric = next(m for m in model["tables"][0]["metrics"] if m["name"] == "total_credits_used")
    metric["expr"] = 'AVG("credits_used")'  # valid grammar, but the digest is stale
    err = expect(model, side, "E_PROVENANCE_MISMATCH")
    assert "tables[0].metrics[total_credits_used]" in err.message


def test_updating_the_digest_makes_the_edit_legitimate(scanned):
    model, side = scanned
    metric = next(m for m in model["tables"][0]["metrics"] if m["name"] == "total_credits_used")
    metric["expr"] = 'AVG("credits_used")'
    side["entries"]["tables[0].metrics[total_credits_used]"]["expr_sha256"] = expr_sha256(metric["expr"])
    check(model, side)


def test_missing_sidecar_file(scanned, project):
    (project["semantic"] / f"{MODEL}.provenance.yaml").unlink()
    with pytest.raises(AgentBiError) as exc:
        validate(MODEL, project["semantic"])
    assert exc.value.full_code == "E_PROVENANCE_MISMATCH"


def test_verified_queries_must_be_in_both_files(scanned):
    model, side = scanned
    model["verified_queries"] = [{"name": "q1", "question": "?", "sql": "SELECT 1", "verified_at": 1}]
    err = expect(model, side, "E_PROVENANCE_MISMATCH")
    assert "q1" in err.message


def test_model_name_must_match(scanned):
    model, side = scanned
    model["name"] = "other"
    expect(model, side, "E_MODEL_INVALID")
