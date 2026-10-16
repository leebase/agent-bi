"""Sprint 2 task 4: verify (architecture §5.6, §4.7)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest
import yaml

from agent_bi.config import load_config
from agent_bi.engines import DuckDBEngine
from agent_bi.errors import AgentBiError
from agent_bi.hashing import expr_sha256
from agent_bi.main import main
from agent_bi.verify.known_answers import MAX_DIFF_ROWS, compare, verify
from tests.conftest import IDLE, ROOT, write_config

MODEL = "idle_warehouse_waste"
NAMES = [
    "total_credits_all", "credits_by_warehouse", "credits_by_day", "row_count_all",
    "avg_compute_by_warehouse", "compute_and_cloud_by_warehouse", "averages_by_day",
    "distinct_warehouses", "warehouses_by_day",
]


@pytest.fixture
def vproject(tmp_path):
    """A throwaway project with the committed model, sidecar and known answers and a private
    copy of the fixture."""
    shutil.copytree(ROOT / "semantic", tmp_path / "semantic")
    shutil.copytree(
        ROOT / "tests" / "fixtures" / "known-answers", tmp_path / "tests/fixtures/known-answers"
    )
    data = tmp_path / "data"
    data.mkdir()
    db = data / "idle.sqlite"
    shutil.copy(IDLE, db)
    cfg = write_config(tmp_path, {"fixture": db})
    return {"dir": tmp_path, "db": db, "cfg": cfg, "argv": ["--config", str(cfg)]}


def run_cli(vp, capsys, *args):
    capsys.readouterr()
    rc = main([*vp["argv"], *args])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def edit_yaml(path, fn):
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


# ----------------------------------------------------------------------------- the repo model


def test_all_eight_known_answers_pass_on_duckdb(vproject, capsys):
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL, "--engine", "duckdb")
    assert rc == 0
    assert out.splitlines() == [f"PASS {n}" for n in NAMES]


def test_engine_defaults_to_duckdb(vproject, capsys):
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL)
    assert rc == 0 and out.count("PASS ") == 9


def test_verify_with_the_committed_repo_config(capsys):
    capsys.readouterr()
    assert main(["--config", str(ROOT / "agent-bi.yaml"), "verify", "--model", MODEL]) == 0
    assert capsys.readouterr().out.count("PASS ") == 9


# ----------------------------------------------------------------------------- negative fixture


def test_a_deliberately_wrong_expected_file_fails_with_a_diff(vproject, capsys):
    negative = ROOT / "tests/fixtures/known-answers-negative" / MODEL / "credits_by_warehouse.json"
    shutil.copy(
        negative, vproject["dir"] / "tests/fixtures/known-answers" / MODEL / "credits_by_warehouse.json"
    )
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL, "--engine", "duckdb")
    assert rc == 1
    lines = out.splitlines()
    assert "FAIL credits_by_warehouse" in lines
    diff = [l for l in lines if l.startswith("  row ")]
    assert diff and "2740.0" in diff[0] and "2739.0" in diff[0]
    assert sum(1 for l in lines if l.startswith("PASS ")) == 8


def test_a_wrong_query_hash_is_a_failure_not_a_pass(vproject, capsys):
    ka = vproject["dir"] / "tests/fixtures/known-answers" / MODEL / "row_count_all.json"
    doc = json.loads(ka.read_text())
    doc["query_hash"] = "sha256:" + "0" * 64
    ka.write_text(json.dumps(doc))
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL)
    assert rc == 1 and "FAIL row_count_all: query_hash" in out


def test_a_missing_known_answer_file_is_an_error(vproject, capsys):
    (vproject["dir"] / "tests/fixtures/known-answers" / MODEL / "row_count_all.json").unlink()
    rc, _, err = run_cli(vproject, capsys, "verify", "--model", MODEL)
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_PROVENANCE_MISMATCH: ")


# ----------------------------------------------------------------------------- STALE


def test_stale(vproject, capsys):
    sem = vproject["dir"] / "semantic"

    def change_model(m):
        for metric in m["tables"][0]["metrics"]:
            if metric["name"] == "total_credits_used":
                metric["expr"] = 'SUM("credits_used_compute")'

    def change_sidecar(s):
        s["entries"]["tables[0].metrics[total_credits_used]"]["expr_sha256"] = expr_sha256(
            'SUM("credits_used_compute")'
        )

    edit_yaml(sem / f"{MODEL}.yaml", change_model)
    edit_yaml(sem / f"{MODEL}.provenance.yaml", change_sidecar)
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL, "--engine", "duckdb")
    assert rc == 1
    lines = out.splitlines()
    # exactly the three queries that reference total_credits_used go stale, each naming
    # exactly that definition (A2); the other five still PASS
    assert [l for l in lines if l.startswith("STALE ")] == [
        "STALE total_credits_all: total_credits_used",
        "STALE credits_by_warehouse: total_credits_used",
        "STALE credits_by_day: total_credits_used",
    ]
    assert [l for l in lines if l.startswith("PASS ")] == [
        f"PASS {n}" for n in NAMES
        if n not in ("total_credits_all", "credits_by_warehouse", "credits_by_day")
    ]
    # a stale query is never compared, so it cannot also FAIL
    assert not any(l.startswith("FAIL ") for l in lines)


def test_stale_names_base_table_first_and_removed_definitions(vproject, capsys):
    sem = vproject["dir"] / "semantic"

    def change_model(m):
        m["tables"][0]["base_table"]["table"] = "renamed_history"
        m["tables"][0]["metrics"] = [
            x for x in m["tables"][0]["metrics"] if x["name"] != "avg_credits_used_cloud_services"
        ]
        for metric in m["tables"][0]["metrics"]:
            if metric["name"] == "avg_credits_used":
                metric["expr"] = 'AVG("credits_used_compute")'

    def change_sidecar(s):
        del s["entries"]["tables[0].metrics[avg_credits_used_cloud_services]"]
        s["entries"]["tables[0].metrics[avg_credits_used]"]["expr_sha256"] = expr_sha256(
            'AVG("credits_used_compute")'
        )

    edit_yaml(sem / f"{MODEL}.yaml", change_model)
    edit_yaml(sem / f"{MODEL}.provenance.yaml", change_sidecar)
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL)
    assert rc == 1
    lines = out.splitlines()
    assert "STALE averages_by_day: base_table, avg_credits_used, avg_credits_used_cloud_services (removed)" in lines
    assert "STALE credits_by_warehouse: base_table" in lines  # base_table changed only
    assert len([l for l in lines if l.startswith("STALE ")]) == 9  # base_table is in every digest
    assert not any(l.startswith(("PASS ", "FAIL ")) for l in lines)


def test_stale_is_decided_by_semantic_digest_alone(vproject, capsys):
    """definition_digests only explains: altering it without a definition change is no STALE."""
    sem = vproject["dir"] / "semantic"

    def tamper(s):
        s["verified_queries"]["credits_by_warehouse"]["definition_digests"]["total_credits_used"] = (
            "sha256:" + "0" * 64
        )

    edit_yaml(sem / f"{MODEL}.provenance.yaml", tamper)
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL)
    assert rc == 0 and out.count("PASS ") == 9


def test_validate_rejects_a_definition_digest_for_an_unreferenced_name(vproject, capsys):
    sem = vproject["dir"] / "semantic"
    edit_yaml(
        sem / f"{MODEL}.provenance.yaml",
        lambda s: s["verified_queries"]["credits_by_warehouse"]["definition_digests"].__setitem__(
            "avg_credits_used", "sha256:" + "0" * 64
        ),
    )
    rc, _, err = run_cli(vproject, capsys, "validate", "--model", MODEL)
    assert rc == 2
    last = err.strip().splitlines()[-1]
    assert last.startswith("E_PROVENANCE_MISMATCH: ") and "definition_digests[avg_credits_used]" in last


def test_description_edits_do_not_make_a_query_stale(vproject, capsys):
    def change(m):
        for metric in m["tables"][0]["metrics"]:
            metric["description"] = "reworded"
            metric["synonyms"] = ["x"]

    edit_yaml(vproject["dir"] / "semantic" / f"{MODEL}.yaml", change)
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL)
    assert rc == 0 and out.count("PASS ") == 9


# ----------------------------------------------------------------------------- snapshot


def test_snapshot_changed(vproject, monkeypatch):
    real = DuckDBEngine.execute
    state = {"done": False}

    def tampering_execute(self, sql):
        if not state["done"] and "FROM" in sql and "warehouse_metering_history" in sql:
            state["done"] = True
            con = sqlite3.connect(vproject["db"])
            con.execute(
                "INSERT INTO warehouse_metering_history VALUES ('2026-06-30','X_WH',1.0,1.0,2.0)"
            )
            con.commit()
            con.close()
        return real(self, sql)

    monkeypatch.setattr(DuckDBEngine, "execute", tampering_execute)
    cfg = load_config(vproject["cfg"])
    with pytest.raises(AgentBiError) as exc:
        verify(MODEL, "duckdb", cfg, vproject["dir"])
    assert exc.value.full_code == "E_SNAPSHOT_CHANGED" and exc.value.exit_code == 3


# ----------------------------------------------------------------------------- coverage


def test_coverage_lists_every_metric_with_at_least_one_query(vproject, capsys):
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL, "--coverage")
    assert rc == 0
    cov = out.split("coverage (current verified queries per metric):\n")[1].splitlines()
    metrics = [m["name"] for m in yaml.safe_load((ROOT / "semantic" / f"{MODEL}.yaml").read_text())["tables"][0]["metrics"]]
    assert len(cov) == len(metrics) == 8
    for line, metric in zip(cov, metrics, strict=True):
        name, _, queries = line.strip().partition(": ")
        assert name == metric and queries and "none" not in queries
    assert "total_credits_used: total_credits_all, credits_by_warehouse, credits_by_day" in out


def test_an_uncovered_confirmed_metric_fails_coverage(vproject, capsys):
    sem = vproject["dir"] / "semantic"
    edit_yaml(
        sem / f"{MODEL}.yaml",
        lambda m: m.__setitem__(
            "verified_queries", [v for v in m["verified_queries"] if v["name"] != "averages_by_day"]
        ),
    )

    def sidecar(s):
        del s["verified_queries"]["averages_by_day"]
        s["entries"]["tables[0].metrics[avg_credits_used]"]["status"] = "confirmed"

    edit_yaml(sem / f"{MODEL}.provenance.yaml", sidecar)
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL, "--coverage")
    assert rc == 1
    assert "avg_credits_used: -- none --" in out
    assert "UNCOVERED avg_credits_used" in out
    assert "UNCOVERED avg_credits_used_cloud_services" not in out  # status catalog: only listed
    # the same gap without a confirmed metric is listed but is not an error
    edit_yaml(sem / f"{MODEL}.provenance.yaml",
              lambda s: s["entries"]["tables[0].metrics[avg_credits_used]"].__setitem__("status", "catalog"))
    rc, out, _ = run_cli(vproject, capsys, "verify", "--model", MODEL, "--coverage")
    assert rc == 0 and "avg_credits_used: -- none --" in out


def test_stale_queries_do_not_count_as_coverage(vproject, capsys):
    sem = vproject["dir"] / "semantic"

    def change_model(m):
        for metric in m["tables"][0]["metrics"]:
            if metric["name"] == "row_count":
                metric["expr"] = 'COUNT(DISTINCT "usage_day")'

    edit_yaml(sem / f"{MODEL}.yaml", change_model)
    edit_yaml(sem / f"{MODEL}.provenance.yaml", lambda s: s["entries"][
        "tables[0].metrics[row_count]"].__setitem__("expr_sha256", expr_sha256('COUNT(DISTINCT "usage_day")')))
    rc, out, err = run_cli(vproject, capsys, "verify", "--model", MODEL, "--coverage")
    assert rc == 1, err
    assert "STALE row_count_all" in out and "row_count: -- none --" in out


# ----------------------------------------------------------------------------- engines


def test_snowflake_is_unavailable_and_rill_is_unavailable_when_not_running(
    vproject, capsys, monkeypatch
):
    from agent_bi.verify import rill_route

    # Hermetic (Sprint 3 review A1): no Rill is reachable whatever is running on :9009.
    def unreachable(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(rill_route, "fetch_local_config", unreachable)
    rc, _, err = run_cli(vproject, capsys, "verify", "--model", MODEL, "--engine", "snowflake")
    assert rc == 3 and err.strip().splitlines()[-1].startswith("E_ENGINE_UNAVAILABLE: ")
    rc, _, err = run_cli(vproject, capsys, "verify", "--model", MODEL, "--engine", "rill")
    assert rc == 3 and err.strip().splitlines()[-1].startswith("E_ENGINE_UNAVAILABLE: ")


def test_verify_validates_the_model_first(vproject, capsys):
    edit_yaml(vproject["dir"] / "semantic" / f"{MODEL}.yaml",
              lambda m: m["tables"][0]["metrics"].pop())
    rc, _, err = run_cli(vproject, capsys, "verify", "--model", MODEL)
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_PROVENANCE_MISMATCH: ")


# ----------------------------------------------------------------------------- compare()


EXPECTED = {"columns": ["w", "n"], "rows": [["A", 1.0], ["B", 2.0]]}


def test_compare_equal_after_sorting_and_to_the_cent():
    assert compare(EXPECTED, ["w", "n"], [("B", 2.004), ("A", 1)]) == []
    assert compare(EXPECTED, ["w", "n"], [("B", Decimal("2.0")), ("A", 1.0)]) == []


def test_compare_rejects_more_than_a_cent_and_string_differences():
    assert compare(EXPECTED, ["w", "n"], [("A", 1.0), ("B", 2.006)])
    assert compare(EXPECTED, ["w", "n"], [("a", 1.0), ("B", 2.0)])


def test_compare_columns_row_count_and_null():
    assert "columns differ" in compare(EXPECTED, ["n", "w"], [])[0]
    assert "row count differs" in compare(EXPECTED, ["w", "n"], [("A", 1.0)])[0]
    assert compare({"columns": ["x"], "rows": [[None]]}, ["x"], [(None,)]) == []
    assert compare({"columns": ["x"], "rows": [[None]]}, ["x"], [(0,)])
    assert compare({"columns": ["x"], "rows": [[0.0]]}, ["x"], [(None,)])


def test_compare_normalizes_dates():
    exp = {"columns": ["d", "n"], "rows": [["2026-06-01", 1.0]]}
    assert compare(exp, ["d", "n"], [(date(2026, 6, 1), 1.0)]) == []
    assert compare(exp, ["d", "n"], [(datetime(2026, 6, 1), 1.0)]) == []
    assert compare(exp, ["d", "n"], [(datetime(2026, 6, 1, 3), 1.0)])


def test_compare_lists_at_most_twenty_differences():
    exp = {"columns": ["n"], "rows": [[float(i)] for i in range(50)]}
    diffs = compare(exp, ["n"], [(float(i) + 1.0,) for i in range(50)])
    assert len(diffs) == MAX_DIFF_ROWS + 1 and diffs[-1].startswith("... ")
