"""Standalone-workspace acceptance (D436): documented commands ship their runtime functionality.

A workspace installed from a wheel has no checkout: no ``scripts/`` and no ``themes/``. These
tests prove ``ask`` and ``save --verify`` work there through the in-process installed
independent route and the packaged default theme, that a planted workspace script is never
executed, and that the checkout shim and the packaged program stay identical.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_bi.errors import AgentBiError
from agent_bi.explore.chart import load_theme
from agent_bi.explore.save import run_independent_sql
from agent_bi.verify.compute_known_answer import (
    NotReadonlyError,
    SqlError,
    compute_known_answer,
)
from tests.conftest import IDLE, ROOT
from tests.s3_helpers import MODEL, tree_hash
from tests.s4_helpers import CBW, cli, make_s4_project, write_query

PACKAGED = ROOT / "src" / "agent_bi" / "verify" / "compute_known_answer.py"
SHIM = ROOT / "scripts" / "compute_known_answer.py"
CREDITS_SQL = (
    "SELECT warehouse_name, SUM(credits_used) AS total_credits_used "
    "FROM warehouse_metering_history GROUP BY warehouse_name"
)
TENTH_SQL = (
    "SELECT warehouse_name, AVG(credits_used) AS avg_credits_used "
    "FROM warehouse_metering_history GROUP BY warehouse_name"
)
POISON = """\
import pathlib
import sys

(pathlib.Path(__file__).resolve().with_name("EXECUTED")).write_text("executed\\n")
print('{"expected": {"columns": [], "rows": []}}')
sys.exit(0)
"""


def _ask(p: Path, capsys, query_file: Path = CBW) -> Path:
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(query_file))
    assert rc == 0, err
    return p / next(ln.split()[1] for ln in out.splitlines() if ln.endswith(".answer.json"))


def _save_verify(p: Path, capsys, answer: Path, expected_sql: str, name: str = "standalone_credits"):
    return cli(
        p, capsys, "save", "--from", str(answer), "--name", name,
        "--question", "credits by warehouse", "--verify", "--by", "standalone",
        "--expected-sql", expected_sql,
    )


def _run_program(program: Path, out: Path, sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(program), "--sqlite", str(IDLE), "--sql", sql,
         "--name", "ka", "--by", "standalone", "--out", str(out)],
        check=False, capture_output=True, text=True,
    )


def _without_expected_at(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("expected_at")
    return doc


# --------------------------------------------------------------------------- standalone save


def test_save_verify_succeeds_without_checkout_copies(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    assert not (p / "scripts").exists() and not (p / "themes").exists()
    answer = _ask(p, capsys)
    rc, out, err = _save_verify(p, capsys, answer, CREDITS_SQL)
    assert rc == 0, err
    ka_path = p / "tests/fixtures/known-answers" / MODEL / "standalone_credits.json"
    assert ka_path.is_file()
    assert f"wrote tests/fixtures/known-answers/{MODEL}/standalone_credits.json" in out
    ka = json.loads(ka_path.read_text(encoding="utf-8"))
    assert ka["expected_computed_by"] == "direct-sql"
    assert ka["expected"]["columns"] == ["warehouse_name", "total_credits_used"]
    assert ka["expected_sql_by"] == "standalone"
    assert not (p / "scripts").exists() and not (p / "themes").exists()


def test_view_model_save_verify_and_verify_pass_without_checkout_copies(tmp_path, capsys):
    from tests import test_rill_models as view

    project = view.make_view_project(tmp_path)
    assert not (project / "scripts").exists() and not (project / "themes").exists()
    assert view.init_view(project, capsys)[0] == 0
    view.create_verified(
        project, capsys, "credits_by_cost_center", "credits by cost center",
        view.SQL_COST_CENTER, view.QUERY_COST_CENTER,
    )
    rc, out, err = cli(project, capsys, "verify", "--model", view.MODEL)
    assert rc == 0, err
    assert out.splitlines() == ["PASS credits_by_cost_center"]
    assert not (project / "scripts").exists() and not (project / "themes").exists()


def test_a_poisoned_workspace_script_is_never_executed(tmp_path, capsys):
    (tmp_path / "baseline").mkdir()
    (tmp_path / "poisoned").mkdir()
    baseline = make_s4_project(tmp_path / "baseline")
    poisoned = make_s4_project(tmp_path / "poisoned")
    (poisoned / "scripts").mkdir()
    (poisoned / "scripts" / "compute_known_answer.py").write_text(POISON, encoding="utf-8")
    rc1, out1, err1 = _save_verify(baseline, capsys, _ask(baseline, capsys), CREDITS_SQL)
    rc2, out2, err2 = _save_verify(poisoned, capsys, _ask(poisoned, capsys), CREDITS_SQL)
    assert rc1 == 0, err1
    assert rc2 == 0, err2
    assert out1 == out2
    assert not (poisoned / "scripts" / "EXECUTED").exists()
    ka_rel = f"tests/fixtures/known-answers/{MODEL}/standalone_credits.json"
    assert _without_expected_at(json.loads((baseline / ka_rel).read_text())) == _without_expected_at(
        json.loads((poisoned / ka_rel).read_text())
    )


# --------------------------------------------------------------------------- the two programs


def test_the_packaged_program_imports_only_the_standard_library():
    tree = ast.parse(PACKAGED.read_text(encoding="utf-8"))
    top: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "the independent route must not use relative imports"
            assert node.module
            top.add(node.module.split(".")[0])
    assert top <= sys.stdlib_module_names
    assert "agent_bi" not in top


def test_the_shim_and_the_packaged_program_agree(tmp_path):
    packaged = _run_program(PACKAGED, tmp_path / "packaged.json", CREDITS_SQL)
    shim = _run_program(SHIM, tmp_path / "shim.json", CREDITS_SQL)
    assert packaged.returncode == shim.returncode == 0
    assert _without_expected_at(json.loads((tmp_path / "packaged.json").read_text())) == (
        _without_expected_at(json.loads((tmp_path / "shim.json").read_text()))
    )
    for sql, code in (
        ("DELETE FROM warehouse_metering_history", 2),
        ("SELECT * FROM no_such_table", 3),
    ):
        assert _run_program(PACKAGED, tmp_path / "p.json", sql).returncode == code
        assert _run_program(SHIM, tmp_path / "s.json", sql).returncode == code


# --------------------------------------------------------------------------- refusals


def test_save_verify_refusals_are_unchanged_without_checkout_copies(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    query = write_query(p / "out" / "tenth.json", metrics=["avg_credits_used"], order_by=[])
    answer = _ask(p, capsys, query)
    snap = tree_hash(p)
    rc, _, err = _save_verify(p, capsys, answer, "DELETE FROM warehouse_metering_history",
                              name="avg_by_warehouse")
    assert rc == 2 and "E_STATEMENT_NOT_ALLOWED" in err
    assert tree_hash(p) == snap
    wrong = TENTH_SQL.replace("AVG(credits_used)", "AVG(credits_used) + 1")
    rc, _, err = _save_verify(p, capsys, answer, wrong, name="avg_by_warehouse")
    assert rc == 2 and "E_VERIFY_MISMATCH" in err
    assert tree_hash(p) == snap


# --------------------------------------------------------------------------- in-process route


def test_run_independent_sql_starts_no_subprocess(monkeypatch):
    import subprocess as subprocess_module

    def forbidden(*args, **kwargs):
        raise AssertionError("run_independent_sql must call the module in-process")

    monkeypatch.setattr(subprocess_module, "run", forbidden)
    monkeypatch.setattr(subprocess_module, "Popen", forbidden)
    doc = run_independent_sql(IDLE, CREDITS_SQL, "standalone_credits", "standalone")
    assert doc["expected"]["columns"] == ["warehouse_name", "total_credits_used"]
    assert doc["expected_computed_by"] == "direct-sql"
    assert doc["expected_sql_by"] == "standalone"
    assert doc["query_hash"] is None and doc["semantic_digest"] is None
    assert doc["semantic_query"] is None
    assert doc["expected_at"].endswith("Z")
    assert doc["fixture_sha256"].startswith("sha256:")


def test_run_independent_sql_maps_the_typed_refusals_to_contract_errors():
    with pytest.raises(AgentBiError) as exc:
        run_independent_sql(IDLE, "DELETE FROM warehouse_metering_history", "ka", "standalone")
    assert exc.value.code == "STATEMENT_NOT_ALLOWED" and exc.value.exit_code == 2
    assert str(exc.value).startswith("E_STATEMENT_NOT_ALLOWED: E_NOT_READONLY: first keyword")
    with pytest.raises(AgentBiError) as exc:
        run_independent_sql(IDLE, "SELECT * FROM no_such_table", "ka", "standalone")
    assert exc.value.code == "VERIFY_MISMATCH" and exc.value.exit_code == 2
    assert str(exc.value).startswith(
        "E_VERIFY_MISMATCH: the independent route failed: E_SQL: OperationalError: no such table"
    )


def test_the_module_refuses_with_typed_exceptions():
    with pytest.raises(NotReadonlyError) as exc:
        compute_known_answer(IDLE, "DELETE FROM warehouse_metering_history", "ka", "standalone")
    assert str(exc.value) == (
        "E_NOT_READONLY: first keyword must be SELECT or WITH, got 'DELETE'"
    )
    with pytest.raises(SqlError) as exc:
        compute_known_answer(IDLE, "SELECT * FROM no_such_table", "ka", "standalone")
    assert str(exc.value).startswith("E_SQL: OperationalError: no such table")


# --------------------------------------------------------------------------- themes


def test_packaged_default_theme_matches_an_empty_workspace_default(tmp_path, capsys):
    (tmp_path / "packaged").mkdir()
    (tmp_path / "workspace").mkdir()
    packaged = make_s4_project(tmp_path / "packaged")
    workspace = make_s4_project(tmp_path / "workspace")
    (workspace / "themes").mkdir()
    (workspace / "themes" / "default.json").write_text("{}\n", encoding="utf-8")
    assert load_theme(packaged, None) == {}
    answer = _ask(packaged, capsys)
    vl_rel = Path(str(answer.relative_to(packaged)).replace(".answer.json", ".vl.json"))
    packaged_vl = (packaged / vl_rel).read_bytes()
    _ask(workspace, capsys)
    assert (workspace / vl_rel).read_bytes() == packaged_vl


def test_workspace_default_theme_overrides_the_packaged_one(tmp_path):
    (tmp_path / "themes").mkdir()
    (tmp_path / "themes" / "default.json").write_text('{"background": "#111"}\n', encoding="utf-8")
    assert load_theme(tmp_path, None) == {"background": "#111"}
    assert load_theme(tmp_path, "default") == {"background": "#111"}


def test_a_missing_named_theme_is_still_e_theme_not_found(tmp_path):
    with pytest.raises(AgentBiError) as exc:
        load_theme(tmp_path, "nope")
    assert exc.value.code == "THEME_NOT_FOUND" and exc.value.exit_code == 2


def test_no_source_file_names_the_checkout_script():
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src" / "agent_bi").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and "scripts/compute_known_answer" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == []
