"""Sprint 3 task 4: ``verify --engine rill`` (architecture §5.6), with Rill itself faked.

The live comparison against a running Rill is the DoD evidence (``verify-rill.txt``); these
tests pin the behaviours around it: CSV-quoting of ``sql=``, the readiness wait, the
normalization, and that nothing silent ever passes.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
from pathlib import Path

import pytest

from agent_bi.config import load_config
from agent_bi.errors import AgentBiError
from agent_bi.targets.rill import RILL_SCHEMA_PIN
from agent_bi.verify import rill_route
from agent_bi.verify.known_answers import verify
from tests.s3_helpers import MODEL, make_project
from tests.test_known_answers import NAMES

KA = Path(__file__).resolve().parents[1] / "tests/fixtures/known-answers" / MODEL


def completed(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def as_csv(columns, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------------- --properties quoting


def test_properties_arg_is_csv_quoted_when_the_value_holds_another_equals():
    sql = "SELECT a, b FROM m WHERE a = 'x' AND b >= 1 ORDER BY a LIMIT 10"
    arg = rill_route.properties_arg("sql", sql)
    assert arg == '"sql=' + sql + '"'
    assert next(iter(csv.reader(io.StringIO(arg)))) == ["sql=" + sql]  # pflag's own parse


def test_properties_arg_doubles_embedded_double_quotes():
    sql = 'SELECT "a" FROM m WHERE x = 1'
    arg = rill_route.properties_arg("sql", sql)
    assert arg == '"sql=SELECT ""a"" FROM m WHERE x = 1"'
    assert next(iter(csv.reader(io.StringIO(arg)))) == ["sql=" + sql]


def test_properties_arg_is_bare_when_there_is_no_other_equals():
    sql = "SELECT a, b FROM m ORDER BY a LIMIT 10"
    assert rill_route.properties_arg("sql", sql) == "sql=" + sql  # pflag trims quotes otherwise


def test_run_metrics_sql_builds_the_documented_command(tmp_path):
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        return completed(as_csv(["a"], [["1"]]))

    res = rill_route.run_metrics_sql(tmp_path, "SELECT a FROM m WHERE a = 1 LIMIT 5", 5, "rill", fake)
    cmd = seen["cmd"]
    assert cmd[:2] == ["rill", "query"] and "--local" in cmd
    assert cmd[cmd.index("--path") + 1] == str(tmp_path)
    assert cmd[cmd.index("--format") + 1] == "csv"
    assert cmd[cmd.index("--resolver") + 1] == "metrics_sql"
    assert cmd[cmd.index("--limit") + 1] == "5"
    assert cmd[cmd.index("--properties") + 1] == '"sql=SELECT a FROM m WHERE a = 1 LIMIT 5"'
    assert res.columns == ["a"] and res.rows == [["1"]]


# ---------------------------------------------------------------------- readiness


def test_readiness_polls_until_one_row_comes_back(tmp_path):
    answers = [completed(rc=1), completed(rc=1), completed(as_csv(["row_count"], [["21"]]))]
    sleeps = []
    rill_route.wait_until_ready(
        tmp_path, "SELECT row_count FROM m LIMIT 1", timeout=30, interval=2, binary="rill",
        runner=lambda *a, **k: answers.pop(0), sleep=sleeps.append, clock=lambda: 0.0,
    )
    assert not answers and sleeps == [2, 2]


def test_readiness_timeout_is_e_engine_unavailable(tmp_path):
    t = iter(range(0, 1000, 10))
    with pytest.raises(AgentBiError) as exc:
        rill_route.wait_until_ready(
            tmp_path, "SELECT row_count FROM m LIMIT 1", timeout=25, interval=1, binary="rill",
            runner=lambda *a, **k: completed(rc=1), sleep=lambda s: None, clock=lambda: next(t),
        )
    assert exc.value.code == "ENGINE_UNAVAILABLE" and exc.value.exit_code == 3


def test_readiness_needs_exactly_one_row(tmp_path):
    t = iter(range(0, 1000, 10))
    with pytest.raises(AgentBiError):  # rc 0 with nothing printed is not "ready"
        rill_route.wait_until_ready(
            tmp_path, "SELECT row_count FROM m LIMIT 1", timeout=15, interval=1, binary="rill",
            runner=lambda *a, **k: completed(""), sleep=lambda s: None, clock=lambda: next(t),
        )


# ---------------------------------------------------------------------- normalization


def test_typed_rows_parse_numbers_by_the_expected_column_and_normalise_midnight():
    expected = {"columns": ["day", "n", "name"],
                "rows": [["2026-06-01", 7, "A"], ["2026-06-02", 7.5, "B"]]}
    rows = rill_route.typed_rows(
        expected, [["2026-06-01T00:00:00Z", "7", "A"], ["2026-06-02T00:00:00Z", "7.5", ""]]
    )
    assert rows == [("2026-06-01", 7.0, "A"), ("2026-06-02", 7.5, None)]
    # a timestamp that is not exactly midnight is left alone (it must not equal a date)
    assert rill_route.typed_rows(expected, [["2026-06-01T01:00:00Z", "1", "A"]])[0][0] == (
        "2026-06-01T01:00:00Z"
    )


def test_a_numeric_looking_string_column_stays_a_string():
    expected = {"columns": ["code"], "rows": [["007"]]}
    assert rill_route.typed_rows(expected, [["007"]]) == [("007",)]


# ---------------------------------------------------------------------- verify() with a fake Rill


@pytest.fixture
def project(tmp_path):
    p = make_project(tmp_path)
    (p / "tests/fixtures/known-answers").mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copytree(KA.parent, p / "tests/fixtures/known-answers", dirs_exist_ok=True)
    (p / "rill").mkdir()
    return p


def rill_csv_for(name):
    """What Rill would print for known answer ``name`` (timestamps as Rill renders them)."""
    ka = json.loads((KA / f"{name}.json").read_text())["expected"]
    rows = []
    for r in ka["rows"]:
        cells = []
        for c in r:
            if isinstance(c, str) and len(c) == 10 and c[4] == "-" and c[7] == "-":
                c = c + "T00:00:00Z"
            cells.append(c)
        rows.append(cells)
    return as_csv(ka["columns"], rows)


def serve(monkeypatch, project, path=None, version=RILL_SCHEMA_PIN):
    """Patch the ``/local/config`` fetch: Rill claims to serve ``path`` (default: this project)."""
    served = {"instance_id": "default", "project_path": str(path or project / "rill"),
              "version": version}
    monkeypatch.setattr(rill_route, "fetch_local_config", lambda *a, **k: served)


def fake_rill(monkeypatch, project, outputs=None):
    """Patch the Rill runner: readiness probe answers, every other query returns the known
    answer of the verified query it compiles from (matched by SQL), or ``outputs[name]``."""
    from agent_bi.query.compile import compile as compile_query
    from agent_bi.query.contract import parse_query
    from agent_bi.semantic.model import load_model_dict
    from agent_bi.semantic.provenance import load_sidecar_dict

    model = load_model_dict(str(project / f"semantic/{MODEL}.yaml"))
    side = load_sidecar_dict(project / f"semantic/{MODEL}.provenance.yaml")
    by_sql = {
        compile_query(parse_query(v["semantic_query"]), model, "rill").compiled_sql: n
        for n, v in side["verified_queries"].items()
    }
    calls = []

    def fake(project_path, sql, limit, binary=None, runner=None):
        calls.append(sql)
        if sql.startswith("SELECT row_count FROM") and sql.endswith("LIMIT 1"):
            return rill_route.RillResult(0, ["row_count"], [["21"]], "", "row_count\n21\n")
        name = by_sql[sql]
        if outputs and name in outputs:
            return outputs[name]
        out = rill_csv_for(name)
        parsed = list(csv.reader(io.StringIO(out)))
        return rill_route.RillResult(0, parsed[0], parsed[1:], "", out)

    monkeypatch.setattr(rill_route, "run_metrics_sql", fake)
    serve(monkeypatch, project)
    monkeypatch.setattr(rill_route, "rill_binary", lambda: "rill")
    return calls


def run_verify(project):
    return verify(MODEL, "rill", load_config(project / "agent-bi.yaml"), project)


def test_verify_rill_passes_all_eight(project, monkeypatch):
    calls = fake_rill(monkeypatch, project)
    res = run_verify(project)
    assert res.exit_code == 0
    assert res.lines == [f"PASS {n}" for n in NAMES]
    assert calls[0] == f"SELECT row_count FROM {MODEL} LIMIT 1"  # readiness first
    assert len(calls) == 10  # one readiness probe, then the nine verified queries


def test_an_empty_answer_when_rows_are_expected_is_a_fail(project, monkeypatch):
    fake_rill(monkeypatch, project, {"credits_by_warehouse": rill_route.RillResult(0, [], [], "", "")})
    res = run_verify(project)
    assert res.exit_code == 1 and res.results["credits_by_warehouse"] == "FAIL"
    assert "FAIL credits_by_warehouse" in res.lines
    assert any("rill returned no rows; expected 3" in ln for ln in res.lines)


def test_a_failed_rill_query_is_a_fail_not_a_pass(project, monkeypatch):
    fake_rill(monkeypatch, project,
              {"row_count_all": rill_route.RillResult(1, [], [], "boom", "")})
    res = run_verify(project)
    assert res.results["row_count_all"] == "FAIL" and res.exit_code == 1
    assert any("rc=1" in ln and "boom" in ln for ln in res.lines)


def test_a_header_only_answer_is_a_fail(project, monkeypatch):
    fake_rill(monkeypatch, project, {"credits_by_warehouse": rill_route.RillResult(
        0, ["warehouse_name", "total_credits_used"], [], "", "warehouse_name,total_credits_used\n")})
    res = run_verify(project)
    assert res.results["credits_by_warehouse"] == "FAIL"


def test_a_wrong_number_from_rill_is_a_fail_with_a_diff(project, monkeypatch):
    bad = rill_route.RillResult(0, ["row_count"], [["22"]], "", "row_count\n22\n")
    fake_rill(monkeypatch, project, {"row_count_all": bad})
    res = run_verify(project)
    assert res.results["row_count_all"] == "FAIL" and res.exit_code == 1
    assert any("expected [21.0], got [22.0]" in ln for ln in res.lines)


def test_unsupported_by_route_is_printed_and_does_not_fail(project, monkeypatch):
    from agent_bi.errors import validation_error
    from agent_bi.verify import known_answers

    fake_rill(monkeypatch, project)
    real = known_answers.compile_query

    def refuse_one(q, model, dialect, **kw):
        if dialect == "rill" and q.metrics == ["row_count"]:
            raise validation_error("UNSUPPORTED_BY_RILL_ROUTE", "refused for the test")
        return real(q, model, dialect, **kw)

    monkeypatch.setattr(known_answers, "compile_query", refuse_one)
    res = run_verify(project)
    assert "UNSUPPORTED_BY_ROUTE row_count_all" in res.lines and res.exit_code == 0
    assert sum(1 for ln in res.lines if ln.startswith("PASS ")) == 8


def test_stale_is_reported_without_touching_rill(project, monkeypatch):
    import yaml

    p = project / f"semantic/{MODEL}.yaml"
    doc = yaml.safe_load(p.read_text())
    m = next(m for m in doc["tables"][0]["metrics"] if m["name"] == "row_count")
    m["expr"] = 'COUNT(DISTINCT "usage_day")'
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    sc = project / f"semantic/{MODEL}.provenance.yaml"
    side = yaml.safe_load(sc.read_text())
    from agent_bi.hashing import expr_sha256

    side["entries"]["tables[0].metrics[row_count]"]["expr_sha256"] = expr_sha256(m["expr"])
    sc.write_text(yaml.safe_dump(side, sort_keys=False))
    fake_rill(monkeypatch, project)
    res = run_verify(project)
    assert res.results["row_count_all"] == "STALE" and res.exit_code == 1


# ---------------------------------------------------------------------- project binding (A1, D396)


def test_binding_proceeds_when_rill_serves_this_project_at_the_pinned_version(project, monkeypatch):
    fake_rill(monkeypatch, project)
    rill_route.check_binding(project / "rill", RILL_SCHEMA_PIN)  # no error
    assert run_verify(project).exit_code == 0


def test_binding_refuses_a_rill_serving_another_directory(project, monkeypatch, tmp_path):
    calls = fake_rill(monkeypatch, project)
    other = tmp_path / "elsewhere" / "rill"
    serve(monkeypatch, project, path=other)
    with pytest.raises(AgentBiError) as exc:
        run_verify(project)
    assert exc.value.code == "ENGINE_UNAVAILABLE" and exc.value.exit_code == 3
    assert str(other) in exc.value.message and RILL_SCHEMA_PIN in exc.value.message
    assert calls == []  # not even the readiness probe ran


def test_binding_refuses_the_wrong_version(project, monkeypatch):
    calls = fake_rill(monkeypatch, project)
    serve(monkeypatch, project, version="v0.91.0")
    with pytest.raises(AgentBiError) as exc:
        run_verify(project)
    assert exc.value.code == "ENGINE_UNAVAILABLE" and "v0.91.0" in exc.value.message
    assert calls == []


@pytest.mark.parametrize("failure", [
    OSError("connection refused"), ValueError("bad json"), TimeoutError("timed out"),
])
def test_binding_refuses_an_unreachable_or_unreadable_endpoint(project, monkeypatch, failure):
    calls = fake_rill(monkeypatch, project)

    def broken(*a, **k):
        raise failure

    monkeypatch.setattr(rill_route, "fetch_local_config", broken)
    with pytest.raises(AgentBiError) as exc:
        run_verify(project)
    assert exc.value.code == "ENGINE_UNAVAILABLE" and exc.value.exit_code == 3
    assert "/local/config" in exc.value.message
    assert calls == []


@pytest.mark.parametrize("body", [{}, {"project_path": "/x"}, {"version": "v0.90.5"},
                                  {"project_path": 1, "version": "v0.90.5"}, [], "text"])
def test_binding_refuses_a_response_missing_a_key(project, monkeypatch, body):
    fake_rill(monkeypatch, project)
    monkeypatch.setattr(rill_route, "fetch_local_config", lambda *a, **k: body)
    with pytest.raises(AgentBiError) as exc:
        rill_route.check_binding(project / "rill", RILL_SCHEMA_PIN)
    assert exc.value.code == "ENGINE_UNAVAILABLE"


def test_binding_refuses_a_project_without_a_rill_directory(project, monkeypatch):
    import shutil

    fake_rill(monkeypatch, project)
    shutil.rmtree(project / "rill")
    with pytest.raises(AgentBiError) as exc:
        run_verify(project)
    assert exc.value.code == "ENGINE_UNAVAILABLE" and "does not exist" in exc.value.message


def test_fetch_local_config_reads_the_pinned_endpoint(monkeypatch):
    import io as _io

    seen = {}

    class Resp(_io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=None):
        seen.update(url=url, timeout=timeout)
        return Resp(b'{"project_path": "/p", "version": "v0.90.5"}')

    monkeypatch.setattr(rill_route.urllib.request, "urlopen", fake_urlopen)
    assert rill_route.fetch_local_config()["version"] == "v0.90.5"
    assert seen == {"url": "http://localhost:9009/local/config", "timeout": rill_route.CONFIG_TIMEOUT_S}
