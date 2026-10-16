"""Sprint 2 task 0: Sprint 1 carry-over (D391) -- §4.1.1, §5.2, §9."""

from __future__ import annotations

import ast
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_bi import errors
from agent_bi.config import load_config
from agent_bi.engines import DuckDBEngine
from agent_bi.errors import AgentBiError
from agent_bi.semantic.model import dump_yaml
from agent_bi.semantic.scan import scan
from agent_bi.semantic.validate import validate
from tests.conftest import ROOT, write_config
from tests.test_scan import MODEL, T1, T2, load, read, run_scan, write

SRC = ROOT / "src" / "agent_bi"


def make_db(tmp_path: Path, ddl: str, rows: list[tuple], table: str = "t") -> Path:
    db = tmp_path / "x.sqlite"
    con = sqlite3.connect(db)
    con.execute(ddl)
    n = len(rows[0])
    con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * n)})", rows)
    con.commit()
    con.close()
    return db


def scan_tmp(tmp_path: Path, db: Path, table: str = "t", model: str = "m", now: str = T1):
    cfg = load_config(write_config(tmp_path, {"fixture": db}))
    eng = DuckDBEngine(cfg)
    try:
        return scan(eng, f"FIXTURE.MAIN.{table}", model, tmp_path / "semantic", now=now)
    finally:
        eng.close()


def names_of(tmp_path: Path, model: str = "m") -> dict:
    t = yaml.safe_load((tmp_path / "semantic" / f"{model}.yaml").read_text())["tables"][0]
    return {k: [o["name"] for o in t.get(k, [])] for k in ("dimensions", "time_dimensions", "facts", "metrics")}


# ------------------------------------------------------------------------ (a) classification


def test_declared_key_dimension_over_20_rows(tmp_path):
    rows = [(i, f"c{i % 3}", float(i)) for i in range(1, 51)]
    db = make_db(tmp_path, "CREATE TABLE t (order_id INTEGER PRIMARY KEY, kind TEXT, amount REAL)", rows)
    scan_tmp(tmp_path, db)
    n = names_of(tmp_path)
    assert "order_id" in n["dimensions"]
    assert "order_id" not in n["facts"]
    assert "total_order_id" not in n["metrics"] and "avg_order_id" not in n["metrics"]
    assert n["facts"] == ["amount"]
    side = yaml.safe_load((tmp_path / "semantic/m.provenance.yaml").read_text())
    assert side["entries"]["tables[0].dimensions[order_id]"]["unique"] is True
    assert "distinct_order_id" not in n["metrics"]  # unique -> no distinct metric


def test_declared_key_text_and_small_numeric_keys_are_dimensions(tmp_path):
    db = make_db(
        tmp_path,
        "CREATE TABLE t (code TEXT PRIMARY KEY, n INTEGER)",
        [("a", 1), ("b", 2), ("c", 3)],
    )
    scan_tmp(tmp_path, db)
    n = names_of(tmp_path)
    assert n["dimensions"] == ["code"] and n["facts"] == ["n"]


def test_numeric_never_both(tmp_path):
    """No column yields both a fact and a dimension: small-cardinality ID-style numerics are
    dimensions only; the global namespace would also forbid the duplicate name."""
    rows = [(i % 5, i % 2, float(i), i) for i in range(1, 41)]
    db = make_db(
        tmp_path, "CREATE TABLE t (region_id INTEGER, is_flag INTEGER, amount REAL, qty INTEGER)", rows
    )
    scan_tmp(tmp_path, db)
    n = names_of(tmp_path)
    assert n["dimensions"] == ["region_id", "is_flag"]
    assert n["facts"] == ["amount", "qty"]
    everything = n["dimensions"] + n["time_dimensions"] + n["facts"] + n["metrics"]
    assert len(everything) == len(set(everything))
    assert "total_region_id" not in n["metrics"] and "avg_is_flag" not in n["metrics"]


# ------------------------------------------------------------------------ (b) namespace


def _state(tmp_path):
    sem = tmp_path / "semantic"
    return sorted(p.name for p in sem.glob("*")) if sem.exists() else []


def test_global_namespace_scan(tmp_path):
    # two columns normalizing to one name
    db = make_db(tmp_path, 'CREATE TABLE t ("rowCount" REAL, row_count REAL)', [(1.0, 2.0), (3.0, 4.0)])
    with pytest.raises(AgentBiError) as exc:
        scan_tmp(tmp_path, db)
    assert exc.value.full_code == "E_NAME_COLLISION" and exc.value.exit_code == 2
    msg = exc.value.message
    assert 'fact row_count (column "rowCount")' in msg
    assert 'fact row_count (column "row_count")' in msg or "metric row_count (generated)" in msg
    assert _state(tmp_path) == []  # nothing written

    # a generated metric equal to a column-derived name
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    db2 = make_db(tmp2, "CREATE TABLE t (amount REAL, total_amount REAL)", [(1.0, 2.0), (3.0, 4.0)])
    with pytest.raises(AgentBiError) as exc:
        scan_tmp(tmp2, db2)
    assert exc.value.full_code == "E_NAME_COLLISION"
    assert 'fact total_amount (column "total_amount")' in exc.value.message
    assert "metric total_amount (generated)" in exc.value.message
    assert _state(tmp2) == []


def test_global_namespace_scan_refresh_leaves_files_unchanged(project):
    run_scan(project, now=T1)
    before = (read(project), read(project, ".provenance.yaml"))
    con = sqlite3.connect(project["db"])
    con.execute("ALTER TABLE warehouse_metering_history ADD COLUMN total_credits_used_compute REAL")
    con.commit()
    con.close()
    with pytest.raises(AgentBiError) as exc:
        run_scan(project, refresh=True, now=T2)
    assert exc.value.full_code == "E_NAME_COLLISION"
    assert (read(project), read(project, ".provenance.yaml")) == before


def test_global_namespace_validate(project):
    run_scan(project)
    validate(MODEL, project["semantic"])
    model = load(project)
    model["tables"][0]["facts"].append(
        {"name": "total_credits_used", "expr": '"credits_used"', "data_type": "NUMBER"}
    )
    write(project, model)
    with pytest.raises(AgentBiError) as exc:
        validate(MODEL, project["semantic"])
    assert exc.value.full_code == "E_NAME_COLLISION"
    assert "metric total_credits_used (generated)" in exc.value.message
    assert 'fact total_credits_used (column "credits_used")' in exc.value.message


# ------------------------------------------------------------------------ (c) refresh invariant


def test_refresh_breaks_on_dropped_pk_column(tmp_path):
    db = make_db(
        tmp_path,
        "CREATE TABLE t (id INTEGER PRIMARY KEY, kind TEXT, amount REAL)",
        [(1, "a", 1.0), (2, "b", 2.0)],
    )
    scan_tmp(tmp_path, db)
    table = yaml.safe_load((tmp_path / "semantic/m.yaml").read_text())["tables"][0]
    assert table["primary_key"] == {"columns": ["id"]}
    before = {p.name: p.read_bytes() for p in (tmp_path / "semantic").glob("*")}
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE t RENAME TO old")
    con.execute("CREATE TABLE t (kind TEXT, amount REAL)")
    con.execute("INSERT INTO t SELECT kind, amount FROM old")
    con.execute("DROP TABLE old")
    con.commit()
    con.close()
    cfg = load_config(write_config(tmp_path, {"fixture": db}))
    eng = DuckDBEngine(cfg)
    try:
        with pytest.raises(AgentBiError) as exc:
            scan(eng, "FIXTURE.MAIN.t", "m", tmp_path / "semantic", refresh=True, now=T2)
    finally:
        eng.close()
    assert exc.value.full_code == "E_REFRESH_BREAKS_ACCEPTED" and exc.value.exit_code == 2
    assert "tables[0].primary_key" in exc.value.message and "id" in exc.value.message
    assert {p.name: p.read_bytes() for p in (tmp_path / "semantic").glob("*")} == before


# ------------------------------------------------------------------------ (d) catalogue


def _codes_in_src() -> dict[str, set[str]]:
    """Every ``E_<CODE>`` used in src/: string literals, and the first argument of the error
    helpers/constructor (bare codes get the ``E_`` prefix)."""
    found: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in re.findall(r"\bE_[A-Z][A-Z0-9_]*\b", node.value):
                    found.setdefault(m, set()).add(path.name)
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) in ("validation_error", "engine_error", "AgentBiError")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                code = node.args[0].value
                code = code if code.startswith("E_") else "E_" + code
                found.setdefault(code, set()).add(path.name)
    return found


def test_error_catalogue():
    used = _codes_in_src()
    assert used, "found no error codes in src/"
    missing = {c: sorted(f) for c, f in used.items() if c not in errors.CATALOGUE}
    assert not missing, f"codes used in src/ but not catalogued: {missing}"
    for code, (exit_code, meaning) in errors.CATALOGUE.items():
        assert re.fullmatch(r"E_[A-Z][A-Z0-9_]*", code)
        assert exit_code in (2, 3, 4, 5, 6) and meaning  # 4: §5.8, D398; 5, 6: resolve §5.15
    committed = (ROOT / "docs" / "errors.md").read_text()
    generated = subprocess.run(
        [sys.executable, "-m", "agent_bi.errors", "--doc"], capture_output=True, text=True, check=True
    ).stdout
    assert committed == generated


def test_helper_exit_codes_match_the_catalogue():
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) in ("validation_error", "engine_error")
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                code = node.args[0].value
                code = code if code.startswith("E_") else "E_" + code
                want = 2 if node.func.id == "validation_error" else 3
                assert errors.CATALOGUE[code][0] == want, (path.name, code)


def test_error_code_prefix_is_normalized():
    assert AgentBiError("E_UNKNOWN_FIELD", "x").full_code == "E_UNKNOWN_FIELD"
    assert AgentBiError("UNKNOWN_FIELD", "x").full_code == "E_UNKNOWN_FIELD"


# ------------------------------------------------------------------------ (e) timestamps, rescans


def test_create_at_equals_scanned_at(project):
    run_scan(project, now=T1)
    side = load(project, ".provenance.yaml")
    assert side["scanned_at"] == T1
    assert side["entries"]
    assert {e["at"] for e in side["entries"].values()} == {T1}


def test_rescanning_both_fixtures_is_byte_identical_to_the_committed_yaml():
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "rescan_diff.py")],
        check=False, capture_output=True, text=True, cwd=ROOT,
    )
    assert out.stdout == "" and out.returncode == 0, out.stdout + out.stderr


def test_dump_yaml_writes_multiline_strings_as_blocks():
    text = dump_yaml({"sql": "SELECT 1\nFROM t"})
    assert "|" in text and yaml.safe_load(text) == {"sql": "SELECT 1\nFROM t"}
