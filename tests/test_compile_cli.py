"""Sprint 2 task 5: the compile-sql command (architecture §6)."""

from __future__ import annotations

import json
from pathlib import Path

from agent_bi.main import main
from tests.conftest import ROOT

QUERY_FILE = str(
    ROOT / "tests/fixtures/known-answers/idle_warehouse_waste/credits_by_warehouse.json"
)
BASE = ["--config", str(ROOT / "agent-bi.yaml"), "compile-sql", "--model", "idle_warehouse_waste"]


def run(capsys, *extra):
    capsys.readouterr()
    rc = main([*BASE, "--query-file", *extra])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_snowflake_prints_try_to_date_form_without_as_of(capsys):
    day = str(ROOT / "tests/fixtures/known-answers/idle_warehouse_waste/credits_by_day.json")
    rc, out, _ = run(capsys, day, "--engine", "snowflake")
    assert rc == 0 and "TRY_TO_DATE" in out and "AT(TIMESTAMP" not in out
    rc, out, _ = run(capsys, QUERY_FILE, "--engine", "snowflake")
    assert rc == 0 and "AT(TIMESTAMP" not in out and '"FIXTURE"."MAIN"."warehouse_metering_history"' in out


def test_executed_adds_the_snowflake_as_of_clause(capsys):
    rc, out, _ = run(capsys, QUERY_FILE, "--engine", "snowflake", "--executed")
    assert rc == 0 and "AT(TIMESTAMP =>" in out and "::TIMESTAMP_LTZ)" in out


def test_rill_prints_names_only_from_the_bare_model_name(capsys):
    rc, out, _ = run(capsys, QUERY_FILE, "--engine", "rill")
    assert rc == 0
    assert " FROM idle_warehouse_waste " in out
    assert out.strip() == (
        "SELECT warehouse_name, total_credits_used FROM idle_warehouse_waste "
        "ORDER BY total_credits_used DESC, warehouse_name ASC LIMIT 1000"
    )
    rc, out, _ = run(capsys, QUERY_FILE, "--engine", "rill", "--executed")
    assert rc == 0 and "AT(" not in out


def test_duckdb_uses_the_relation_map(capsys):
    rc, out, _ = run(capsys, QUERY_FILE, "--engine", "duckdb")
    assert rc == 0 and '"fixture"."main"."warehouse_metering_history"' in out


def test_a_bare_semantic_query_file_works_too(capsys, tmp_path):
    ka = json.loads(Path(QUERY_FILE).read_text())
    bare = tmp_path / "q.json"
    bare.write_text(json.dumps(ka["semantic_query"]))
    rc, out, _ = run(capsys, str(bare), "--engine", "snowflake")
    assert rc == 0
    assert out == (ROOT / "tests/golden/sql/credits_by_warehouse.snowflake.sql").read_text() + "\n"


def test_errors_exit_2_with_a_code_on_the_last_stderr_line(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": "idle_warehouse_waste"}))
    rc, _, err = run(capsys, str(bad), "--engine", "snowflake")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_QUERY_INVALID: ")
    rc, _, err = run(capsys, str(tmp_path / "missing.json"), "--engine", "snowflake")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_QUERY_INVALID: ")
    refused = json.loads(Path(QUERY_FILE).read_text())["semantic_query"]
    refused["filters"] = [{"dimension": "warehouse_name", "op": "like", "values": ["A%"]}]
    path = tmp_path / "like.json"
    path.write_text(json.dumps(refused))
    rc, _, err = run(capsys, str(path), "--engine", "rill")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_UNSUPPORTED_BY_RILL_ROUTE: ")
