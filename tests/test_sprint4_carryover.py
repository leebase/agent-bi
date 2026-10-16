"""Sprint 4 task 0: pre-Sprint-4 carry-over (D398), one test per item (a)-(f).

Each test names the rule it pins; ``evidence/sprint-4/summary.md`` records the falsifier (the
one-line change that makes it fail).
"""

from __future__ import annotations

import json
import shutil

import pytest
import yaml

from agent_bi.compile_rill import compile_rill
from agent_bi.config import load_config
from agent_bi.errors import AgentBiError
from agent_bi.fs_txn import publish_rank
from agent_bi.query.contract import parse_query
from agent_bi.semantic.validate import validate
from agent_bi.verify import rill_route
from tests.conftest import ROOT
from tests.s3_helpers import MODEL, make_project, manifests
from tests.test_carryover import make_db, scan_tmp
from tests.test_verify_rill import (
    as_csv,
    completed,
    fake_rill,
    run_verify,
    serve,
)
from tests.test_verify_rill import project as rill_project  # noqa: F401  (fixture)

BASE_QUERY = {
    "schema_version": "semantic-query/1.0", "model": MODEL, "table": "warehouse_metering_history",
    "metrics": ["total_credits_used"], "dimensions": ["warehouse_name"],
}


# (a) §4.3 duplicates are refused, not collapsed ------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [("metrics", ["total_credits_used", "total_credits_used"]),
     ("dimensions", ["warehouse_name", "warehouse_name"]),
     ("named_filters", ["big", "big"])],
)
def test_a_duplicate_names_are_refused_not_collapsed(field, value):
    q = {**BASE_QUERY, field: value}
    with pytest.raises(AgentBiError) as exc:
        parse_query(q)
    assert exc.value.code == "QUERY_INVALID" and exc.value.exit_code == 2
    assert field in str(exc.value) and repr(value[0]) in str(exc.value)  # names the offender


# (b) §4.2 sidecar status is one of catalog|inferred|confirmed ---------------------------


def test_b_sidecar_status_outside_the_enum_is_a_provenance_mismatch(tmp_path):
    shutil.copytree(ROOT / "semantic", tmp_path / "semantic")
    validate(MODEL, tmp_path / "semantic")  # the committed sidecar is fine
    path = tmp_path / "semantic" / f"{MODEL}.provenance.yaml"
    side = yaml.safe_load(path.read_text())
    side["entries"]["tables[0].metrics[total_credits_used]"]["status"] = "approved"
    path.write_text(yaml.safe_dump(side, sort_keys=False))
    with pytest.raises(AgentBiError) as exc:
        validate(MODEL, tmp_path / "semantic")
    assert exc.value.code == "PROVENANCE_MISMATCH"
    assert "status" in str(exc.value)


# (c) §4.2 definition_digests is required in every verified-query record ------------------


def test_c_a_verified_query_record_without_definition_digests_is_a_provenance_mismatch(tmp_path):
    shutil.copytree(ROOT / "semantic", tmp_path / "semantic")
    path = tmp_path / "semantic" / f"{MODEL}.provenance.yaml"
    side = yaml.safe_load(path.read_text())
    del side["verified_queries"]["credits_by_warehouse"]["definition_digests"]
    path.write_text(yaml.safe_dump(side, sort_keys=False))
    with pytest.raises(AgentBiError) as exc:
        validate(MODEL, tmp_path / "semantic")
    assert exc.value.code == "PROVENANCE_MISMATCH"
    assert "credits_by_warehouse" in str(exc.value) and "definition_digests" in str(exc.value)


# (d) §4.1.1 `time_bucket` and `base_table` are reserved names ----------------------------


@pytest.mark.parametrize("column", ["time_bucket", "base_table"])
def test_d_scan_refuses_a_column_that_normalizes_to_a_reserved_name(tmp_path, column):
    db = make_db(tmp_path, f'CREATE TABLE t ("{column}" TEXT, n REAL)', [("a", 1.0), ("b", 2.0)])
    with pytest.raises(AgentBiError) as exc:
        scan_tmp(tmp_path, db)
    assert exc.value.code == "NAME_COLLISION" and "reserved" in str(exc.value)
    assert column in str(exc.value)


def test_d_validate_refuses_a_hand_edited_reserved_name(tmp_path):
    shutil.copytree(ROOT / "semantic", tmp_path / "semantic")
    path = tmp_path / "semantic" / f"{MODEL}.yaml"
    model = yaml.safe_load(path.read_text())
    model["tables"][0]["dimensions"][0]["name"] = "time_bucket"
    path.write_text(yaml.safe_dump(model, sort_keys=False))
    with pytest.raises(AgentBiError) as exc:
        validate(MODEL, tmp_path / "semantic")
    assert exc.value.code == "NAME_COLLISION" and "reserved" in str(exc.value)


# (e) §4.4 manifest key order and files in the §5.14 publish order ------------------------


def test_e_manifest_keys_and_file_order(tmp_path):
    project = make_project(tmp_path)
    compile_rill(MODEL, load_config(project / "agent-bi.yaml"), project)
    doc = json.loads(manifests(project)[0].read_text())
    assert list(doc) == [
        "schema_version", "build_id", "model", "model_file_sha256", "sidecar_sha256",
        "models_digest", "rill_schema_pin", "files",
    ]
    paths = [f["path"] for f in doc["files"]]
    assert paths == sorted(paths, key=lambda p: (publish_rank(p), p))
    assert paths.index(f"rill/dashboards/{MODEL}_default_canvas.yaml") < paths.index(
        f"rill/dashboards/{MODEL}_explore.yaml"
    )
    assert paths[0] == "rill/rill.yaml"


# (f) §5.6 verify --engine rill: binding re-checked before every query; unquoted null = NULL


def test_f1_the_binding_is_rechecked_before_every_query(rill_project, monkeypatch):  # noqa: F811
    fake_rill(monkeypatch, rill_project)
    checks = []
    real = rill_route.check_binding

    def counting(*a, **k):
        checks.append(1)
        return real(*a, **k)

    monkeypatch.setattr(rill_route, "check_binding", counting)
    res = run_verify(rill_project)
    assert res.exit_code == 0
    assert len(checks) >= 9  # one per query (the probe is one) and the first check up front


def test_f1_a_rill_restarted_on_another_project_mid_run_is_caught(rill_project, monkeypatch):  # noqa: F811
    fake_rill(monkeypatch, rill_project)
    other = rill_project.parent / "elsewhere"
    other.mkdir()
    calls = {"n": 0}
    real_fake = rill_route.run_metrics_sql

    def swap_after_probe(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:  # Rill is replaced between two queries
            serve(monkeypatch, rill_project, path=other)
        return real_fake(*a, **k)

    monkeypatch.setattr(rill_route, "run_metrics_sql", swap_after_probe)
    with pytest.raises(AgentBiError) as exc:
        run_verify(rill_project)
    assert exc.value.code == "ENGINE_UNAVAILABLE" and str(other) in str(exc.value)


def test_f2_unquoted_null_is_null_and_quoted_null_is_the_string():
    out = 'name,n\nnull,null\n"null",3\n,4\n'
    parsed = rill_route.parse_csv(out)
    assert parsed[0] == ["name", "n"]
    expected = {"columns": ["name", "n"], "rows": [[None, None], ["null", 3], [None, 4]]}
    rows = rill_route.typed_rows(expected, parsed[1:])
    assert rows == [(None, None), ("null", 3.0), (None, 4.0)]


def test_f2_run_metrics_sql_keeps_quoting_so_null_survives_the_runner(tmp_path):
    out = as_csv(["a"], [["x"]]).replace("x", '"null"')
    res = rill_route.run_metrics_sql(tmp_path, "SELECT a FROM m LIMIT 1", 1, "rill",
                                     lambda *a, **k: completed(out))
    assert isinstance(res.rows[0][0], rill_route.QuotedStr) and res.rows[0][0] == "null"


def test_f2_parse_csv_handles_quotes_commas_and_newlines():
    text = 'a,b\r\n"x, y","say ""hi""\nthere"\r\nz,\r\n'
    assert rill_route.parse_csv(text) == [["a", "b"], ["x, y", 'say "hi"\nthere'], ["z", ""]]
