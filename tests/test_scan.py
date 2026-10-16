from __future__ import annotations

import sqlite3

import pytest
import yaml

from agent_bi.config import load_config
from agent_bi.engines import DuckDBEngine
from agent_bi.errors import AgentBiError
from agent_bi.hashing import expr_sha256
from agent_bi.main import main
from agent_bi.semantic.model import dump_yaml, parse_model
from agent_bi.semantic.provenance import ProvenanceSidecar
from agent_bi.semantic.scan import scan, snake_name
from tests.conftest import HOLIDAY, load_config_for, write_config

REL = "FIXTURE.MAIN.warehouse_metering_history"
MODEL = "idle_warehouse_waste"
T1 = "2026-10-14T10:00:00Z"
T2 = "2026-10-14T11:00:00Z"
T3 = "2026-10-14T12:00:00Z"


def run_scan(project, refresh=False, now=T1, relation=REL, model=MODEL):
    eng = DuckDBEngine(load_config_for(project))
    try:
        return scan(eng, relation, model, project["semantic"], refresh=refresh, now=now)
    finally:
        eng.close()


def read(project, suffix=".yaml", model=MODEL):
    return (project["semantic"] / f"{model}{suffix}").read_text()


def load(project, suffix=".yaml", model=MODEL):
    return yaml.safe_load(read(project, suffix, model))


def write(project, data, suffix=".yaml", model=MODEL):
    (project["semantic"] / f"{model}{suffix}").write_text(dump_yaml(data))


def mask_scanned_at(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.startswith("scanned_at:"))


def mask_times(text: str) -> str:
    """Mask every timestamp (scanned_at and per-entry `at`); for comparing a merged sidecar
    with a fresh scan, whose `at` values legitimately differ."""
    return "\n".join(
        l.split(":")[0] + ": <ts>" if l.strip().startswith(("scanned_at:", "at:")) else l
        for l in text.splitlines()
    )


# ------------------------------------------------------------------------------ content


def test_scan_content(project):
    run_scan(project)
    model = load(project)
    table = model["tables"][0]
    assert model["name"] == MODEL
    assert table["name"] == "warehouse_metering_history"
    assert table["base_table"] == {
        "database": "FIXTURE", "schema": "MAIN", "table": "warehouse_metering_history"
    }
    assert "primary_key" not in table
    assert [t["name"] for t in table["time_dimensions"]] == ["usage_day"]
    td = table["time_dimensions"][0]
    assert td["expr"] == 'CAST("usage_day" AS DATE)'
    assert td["data_type"] == "DATE"
    assert [d["name"] for d in table["dimensions"]] == ["warehouse_name"]
    assert table["dimensions"][0]["expr"] == '"warehouse_name"'
    assert table["dimensions"][0]["data_type"] == "TEXT"
    assert [f["name"] for f in table["facts"]] == [
        "credits_used_compute", "credits_used_cloud_services", "credits_used"
    ]
    assert len(table["facts"]) == 3
    assert [m["name"] for m in table["metrics"]] == [
        "row_count",
        "total_credits_used_compute",
        "avg_credits_used_compute",
        "total_credits_used_cloud_services",
        "avg_credits_used_cloud_services",
        "total_credits_used",
        "avg_credits_used",
        "distinct_warehouse_name",
    ]
    assert {m["name"]: m["expr"] for m in table["metrics"]}["distinct_warehouse_name"] == (
        'COUNT(DISTINCT "warehouse_name")'
    )
    for forbidden in ("unique", "sample_values"):
        assert forbidden not in yaml.safe_dump(model)
    assert "relationships" not in model


def test_scan_output_round_trips_through_the_pydantic_models(project):
    run_scan(project)
    text = read(project)
    assert parse_model(yaml.safe_load(text)).dump_yaml() == text
    stext = read(project, ".provenance.yaml")
    assert ProvenanceSidecar.model_validate(yaml.safe_load(stext)).dump_yaml() == stext


def test_sidecar_has_one_entry_per_object(project):
    run_scan(project)
    table = load(project)["tables"][0]
    side = load(project, ".provenance.yaml")
    expected = {
        f"tables[0].{kind}[{o['name']}]": o
        for kind in ("dimensions", "time_dimensions", "facts", "metrics")
        for o in table.get(kind, [])
    }
    assert set(side["entries"]) == set(expected)
    assert len(expected) == 1 + 1 + 3 + 8
    for key, obj in expected.items():
        e = side["entries"][key]
        assert e["expr_sha256"] == expr_sha256(obj["expr"])
        assert (e["status"], e["origin"], e["by"]) == ("catalog", "scan", "scan")
        assert e["at"] == T1
    assert side["entries"]["tables[0].dimensions[warehouse_name]"]["unique"] is False
    assert side["entries"]["tables[0].time_dimensions[usage_day]"]["unique"] is False
    assert "unique" not in side["entries"]["tables[0].facts[credits_used]"]
    assert side["relation"]["physical"] == REL
    assert side["relation"]["row_count"] == 21
    assert side["relation"]["kind"] == "table"
    assert side["source"]["kind"] == "sqlite"
    assert side["source"]["as_of_digest"].startswith("sha256:")
    assert side["scanned_at"] == T1
    assert side["ignored_columns"] == []


def test_unique_dimension_gets_no_distinct_metric(tmp_path):
    db = tmp_path / "u.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (code TEXT, kind TEXT, n INTEGER)")
    con.executemany("INSERT INTO t VALUES (?,?,?)", [("a", "x", 1), ("b", "x", 2), ("c", "y", 3)])
    con.commit()
    con.close()
    cfg = load_config(write_config(tmp_path, {"fixture": db}))
    eng = DuckDBEngine(cfg)
    scan(eng, "FIXTURE.MAIN.t", "u", tmp_path / "semantic", now=T1)
    eng.close()
    side = yaml.safe_load((tmp_path / "semantic/u.provenance.yaml").read_text())
    assert side["entries"]["tables[0].dimensions[code]"]["unique"] is True
    assert side["entries"]["tables[0].dimensions[kind]"]["unique"] is False
    names = [m["name"] for m in yaml.safe_load((tmp_path / "semantic/u.yaml").read_text())["tables"][0]["metrics"]]
    assert "distinct_kind" in names and "distinct_code" not in names


def test_product_catalog_scan_has_no_time_dimensions(tmp_path):
    cfg = load_config(write_config(tmp_path, {"fixture2": HOLIDAY}))
    eng = DuckDBEngine(cfg)
    scan(eng, "FIXTURE2.MAIN.product_catalog", "product_catalog", tmp_path / "semantic", now=T1)
    eng.close()
    table = yaml.safe_load((tmp_path / "semantic/product_catalog.yaml").read_text())["tables"][0]
    assert "time_dimensions" not in table
    assert table["primary_key"] == {"columns": ["skuId"]}
    assert len(table["facts"]) == 11
    assert table["dimensions"][0]["name"] == "sku_id"
    assert table["dimensions"][0]["expr"] == '"skuId"'


def test_snake_name():
    assert snake_name("skuId") == "sku_id"
    assert snake_name("primarySubstituteSkuId") == "primary_substitute_sku_id"
    assert snake_name("warehouse_name") == "warehouse_name"
    assert snake_name("HTTPCode") == "http_code"
    assert snake_name("1st col") == "c_1st_col"


# ------------------------------------------------------------------------------ create-only


def test_create_only_refuses_an_existing_model(project):
    run_scan(project)
    before = (read(project), read(project, ".provenance.yaml"))
    with pytest.raises(AgentBiError) as exc:
        run_scan(project, now=T2)
    assert exc.value.full_code == "E_MODEL_EXISTS"
    assert exc.value.exit_code == 2
    assert (read(project), read(project, ".provenance.yaml")) == before


def test_cli_scan_twice(project, capsys):
    argv = ["--config", str(project["config_path"]), "scan", "--engine", "duckdb",
            "--relation", REL, "--model", MODEL]
    assert main(argv) == 0
    capsys.readouterr()
    assert main(argv) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert err[-1].startswith("E_MODEL_EXISTS: ")
    assert (project["semantic"] / f"{MODEL}.yaml").exists()


def test_cli_scan_snowflake_is_unavailable(project, capsys):
    rc = main(["--config", str(project["config_path"]), "scan", "--engine", "snowflake",
               "--relation", "DB.SCH.T", "--model", "m"])
    assert rc == 3
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith("E_ENGINE_UNAVAILABLE: ")


def test_refresh_without_a_model_is_refused(project):
    with pytest.raises(AgentBiError) as exc:
        run_scan(project, refresh=True)
    assert exc.value.full_code == "E_MODEL_NOT_FOUND"


def test_invalid_model_name_and_relation(project):
    with pytest.raises(AgentBiError) as exc:
        run_scan(project, model="Bad-Name")
    assert exc.value.full_code == "E_INVALID_MODEL_NAME"
    with pytest.raises(AgentBiError) as exc:
        run_scan(project, relation="warehouse_metering_history")
    assert exc.value.full_code == "E_INVALID_RELATION"


# ------------------------------------------------------------------------------ refresh


def test_idempotent(project):
    """Run, then --refresh on unchanged data: YAML byte-equal (no masking), sidecar
    byte-equal except scanned_at."""
    run_scan(project, now=T1)
    yaml_1, side_1 = read(project), read(project, ".provenance.yaml")
    run_scan(project, refresh=True, now=T2)
    yaml_2, side_2 = read(project), read(project, ".provenance.yaml")
    assert yaml_2 == yaml_1
    assert "scanned_at: '2026-10-14T11:00:00Z'" in side_2
    assert mask_scanned_at(side_2) == mask_scanned_at(side_1)
    run_scan(project, refresh=True, now=T3)
    assert read(project) == yaml_1
    assert mask_scanned_at(read(project, ".provenance.yaml")) == mask_scanned_at(side_1)


def test_refresh_preserves_accepted(project):
    run_scan(project, now=T1)
    model = load(project)
    side = load(project, ".provenance.yaml")
    table = model["tables"][0]
    metric = next(m for m in table["metrics"] if m["name"] == "total_credits_used")
    metric["description"] = "Total credits, as accepted by the finance team"
    key = "tables[0].metrics[total_credits_used]"
    side["entries"][key].update(
        {"status": "confirmed", "origin": "human", "by": "lee", "at": "2026-10-14T10:30:00Z"}
    )
    flt = {"name": "big_spend", "description": "Large rows", "expr": '"credits_used" > 100'}
    table["filters"] = [flt]
    side["entries"]["tables[0].filters[big_spend]"] = {
        "status": "confirmed", "origin": "human", "at": "2026-10-14T10:31:00Z", "by": "lee",
        "expr_sha256": expr_sha256(flt["expr"]),
    }
    write(project, model)
    write(project, side, ".provenance.yaml")
    yaml_before, side_before = read(project), read(project, ".provenance.yaml")

    run_scan(project, refresh=True, now=T3)

    assert read(project) == yaml_before
    after_model, after_side = load(project), load(project, ".provenance.yaml")
    assert next(m for m in after_model["tables"][0]["metrics"] if m["name"] == "total_credits_used") == metric
    assert after_model["tables"][0]["filters"] == [flt]
    assert after_side["entries"][key] == side["entries"][key]
    assert after_side["entries"][key]["at"] == "2026-10-14T10:30:00Z"
    assert after_side["entries"]["tables[0].filters[big_spend]"] == side["entries"]["tables[0].filters[big_spend]"]
    assert mask_scanned_at(read(project, ".provenance.yaml")) == mask_scanned_at(side_before)


def test_refresh_keeps_operator_added_objects_in_place(project):
    run_scan(project, now=T1)
    model, side = load(project), load(project, ".provenance.yaml")
    metrics = model["tables"][0]["metrics"]
    extra = {"name": "compute_share", "synonyms": ["share"], "description": "Compute share",
             "expr": 'SUM("credits_used_compute") / NULLIF(SUM("credits_used"), 0)'}
    metrics.insert(3, extra)
    entries = side["entries"]
    new_entries = {}
    for k, v in entries.items():
        new_entries[k] = v
        if k.endswith("metrics[avg_credits_used_compute]"):
            new_entries["tables[0].metrics[compute_share]"] = {
                "status": "inferred", "origin": "enrich:patch-1", "at": "2026-10-14T10:40:00Z",
                "by": "model-x", "expr_sha256": expr_sha256(extra["expr"]),
            }
    side["entries"] = new_entries
    write(project, model)
    write(project, side, ".provenance.yaml")
    before = (read(project), read(project, ".provenance.yaml"))
    run_scan(project, refresh=True, now=T2)
    assert read(project) == before[0]
    assert mask_scanned_at(read(project, ".provenance.yaml")) == mask_scanned_at(before[1])


def _alter(project, *statements):
    con = sqlite3.connect(project["db"])
    for s in statements:
        con.execute(s)
    con.commit()
    con.close()


def test_refresh_follows_the_catalog_for_catalog_objects(project):
    run_scan(project, now=T1)
    before_side = load(project, ".provenance.yaml")
    _alter(project, "ALTER TABLE warehouse_metering_history ADD COLUMN credits_adjustment REAL",
           "UPDATE warehouse_metering_history SET credits_adjustment = 1.5")
    run_scan(project, refresh=True, now=T2)
    table = load(project)["tables"][0]
    assert [f["name"] for f in table["facts"]][-1] == "credits_adjustment"
    metric_names = [m["name"] for m in table["metrics"]]
    assert metric_names[-1] == "distinct_warehouse_name"
    assert metric_names[7:9] == ["total_credits_adjustment", "avg_credits_adjustment"]
    side = load(project, ".provenance.yaml")
    new_key = "tables[0].facts[credits_adjustment]"
    assert side["entries"][new_key]["at"] == T2
    # untouched entries keep their original `at`
    for key in ("tables[0].facts[credits_used]", "tables[0].dimensions[warehouse_name]",
                "tables[0].metrics[row_count]"):
        assert side["entries"][key] == before_side["entries"][key]
    assert side["relation"]["columns_digest"] != before_side["relation"]["columns_digest"]
    # the refreshed file equals a fresh scan of the same data
    fresh = {**project, "semantic": project["dir"] / "semantic_fresh"}
    run_scan(fresh, now=T2)
    assert read(project) == read(fresh)
    assert mask_times(read(project, ".provenance.yaml")) == mask_times(
        read(fresh, ".provenance.yaml")
    )


def test_refresh_breaks_accepted(project):
    run_scan(project, now=T1)
    side = load(project, ".provenance.yaml")
    key = "tables[0].metrics[total_credits_used]"
    side["entries"][key]["status"] = "confirmed"
    write(project, side, ".provenance.yaml")
    _alter(project, "ALTER TABLE warehouse_metering_history DROP COLUMN credits_used")
    before = (read(project), read(project, ".provenance.yaml"))
    with pytest.raises(AgentBiError) as exc:
        run_scan(project, refresh=True, now=T2)
    assert exc.value.full_code == "E_REFRESH_BREAKS_ACCEPTED"
    assert exc.value.exit_code == 2
    assert key in exc.value.message
    assert (read(project), read(project, ".provenance.yaml")) == before


def test_refresh_breaks_a_verified_query_that_depends_on_a_dropped_column(project):
    run_scan(project, now=T1)
    side = load(project, ".provenance.yaml")
    side["verified_queries"] = {
        "spend": {
            "query_hash": "sha256:" + "a" * 64,
            "semantic_digest": "sha256:" + "b" * 64,
            "semantic_query": {"metrics": ["total_credits_used"], "dimensions": ["warehouse_name"]},
        }
    }
    write(project, side, ".provenance.yaml")
    _alter(project, "ALTER TABLE warehouse_metering_history DROP COLUMN credits_used")
    before = (read(project), read(project, ".provenance.yaml"))
    with pytest.raises(AgentBiError) as exc:
        run_scan(project, refresh=True, now=T2)
    assert exc.value.full_code == "E_REFRESH_BREAKS_ACCEPTED"
    assert "verified_queries[spend]" in exc.value.message
    assert (read(project), read(project, ".provenance.yaml")) == before


def test_refresh_drops_catalog_objects_whose_column_is_gone(project):
    run_scan(project, now=T1)
    _alter(project, "ALTER TABLE warehouse_metering_history DROP COLUMN credits_used")
    run_scan(project, refresh=True, now=T2)
    table = load(project)["tables"][0]
    assert "credits_used" not in [f["name"] for f in table["facts"]]
    assert "total_credits_used" not in [m["name"] for m in table["metrics"]]
    assert "tables[0].metrics[total_credits_used]" not in load(project, ".provenance.yaml")["entries"]


def test_the_fixture_file_is_never_modified(project):
    before = project["db"].read_bytes()
    run_scan(project)
    run_scan(project, refresh=True, now=T2)
    assert project["db"].read_bytes() == before


def test_cli_refresh_is_idempotent(project, capsys):
    base = ["--config", str(project["config_path"])]
    assert main([*base, "scan", "--engine", "duckdb", "--relation", REL, "--model", MODEL]) == 0
    first = (read(project), read(project, ".provenance.yaml"))
    assert main([*base, "scan", "--engine", "duckdb", "--relation", REL, "--model", MODEL,
                 "--refresh"]) == 0
    assert read(project) == first[0]
    assert mask_scanned_at(read(project, ".provenance.yaml")) == mask_scanned_at(first[1])
    capsys.readouterr()
    assert main([*base, "validate", "--model", MODEL]) == 0


def test_refresh_preserves_operator_primary_key(project):
    """A1: an operator-written primary_key is carried over verbatim by --refresh."""
    run_scan(project, now=T1)
    model = load(project)
    table = model["tables"][0]
    table_with_pk = {}
    for key, value in table.items():
        table_with_pk[key] = value
        if key == "base_table":
            table_with_pk["primary_key"] = {"columns": ["usage_day", "warehouse_name"]}
    model["tables"][0] = table_with_pk
    write(project, model)
    side_before = read(project, ".provenance.yaml")
    yaml_before = read(project)
    assert list(load(project)["tables"][0])[:4] == [
        "name", "description", "base_table", "primary_key"
    ]

    run_scan(project, refresh=True, now=T2)

    assert read(project) == yaml_before  # key, value and position byte-identical
    assert load(project)["tables"][0]["primary_key"] == {
        "columns": ["usage_day", "warehouse_name"]
    }
    assert mask_scanned_at(read(project, ".provenance.yaml")) == mask_scanned_at(side_before)
    base = ["--config", str(project["config_path"])]
    assert main([*base, "validate", "--model", MODEL]) == 0
