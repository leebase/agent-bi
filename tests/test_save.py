"""Sprint 4 task 3 (+ task 6 canvas-scope tests): ``save`` (architecture §5.10, §4.7)."""

from __future__ import annotations

import json
import re

import pytest
import yaml

from agent_bi import fs_txn
from agent_bi.errors import AgentBiError
from tests.s3_helpers import MODEL, tree_hash
from tests.s4_helpers import (
    CBW,
    cli,
    edit_yaml,
    make_s4_project,
    write_query,
)

# The committed model already holds the ninth verified query (warehouses_by_day); these tests
# promote a tenth on a scratch copy.
TENTH_SQL = (
    "SELECT warehouse_name, AVG(credits_used) AS avg_credits_used "
    "FROM warehouse_metering_history GROUP BY warehouse_name"
)
TENTH = {"name": "avg_by_warehouse", "question": "average credits per warehouse"}
AGGREGATES = re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT)\s*\(", re.IGNORECASE)


def ask(p, capsys, query_file=CBW):
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(query_file))
    assert rc == 0, err
    return p / next(ln.split()[1] for ln in out.splitlines() if ln.endswith(".answer.json"))


def save(p, capsys, answer, *extra, name="credits_by_warehouse", question="credits by warehouse"):
    args = ["save", "--from", str(answer), "--name", name]
    if question:
        args += ["--question", question]
    return cli(p, capsys, *args, *extra)


def tenth_query(p):
    return write_query(p / "out" / "tenth.json", metrics=["avg_credits_used"], order_by=[])


# ----------------------------------------------------------------------------- the files


def test_save_writes_the_query_and_the_annotated_component(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)
    rc, out, err = save(p, capsys, answer)
    assert rc == 0, err
    saved = json.loads((p / "queries/credits_by_warehouse.json").read_text())
    assert saved["status"] == "saved" and saved["schema_version"] == "agent-bi-saved-query/1.0"
    assert saved["question"] == "credits by warehouse"
    assert saved["question_normalized"] == "credits by warehouse"
    env = json.loads(answer.read_text())
    art = env["provenance"]["artifact"]
    assert saved["query_hash"] == art["query_hash"] and saved["semantic_digest"] == art["semantic_digest"]
    assert saved["semantic_query"] == env["semantic_query"]
    text = (p / "rill/components/credits_by_warehouse.yaml").read_text()
    lines = text.splitlines()
    assert [ln.split(":")[0] for ln in lines[:3]] == [
        "# agent_bi_query_hash", "# agent_bi_semantic_digest", "# agent_bi_sql_sha256",
    ]
    assert lines[0].endswith(art["query_hash"]) and lines[1].endswith(art["semantic_digest"])
    doc = yaml.safe_load(text)
    chart = doc["custom_chart"]
    assert doc["type"] == "component" and chart["metrics_view"] == MODEL
    (sql,) = chart["metrics_sql"]
    assert re.search(rf"\bFROM {MODEL}\b", sql) and not AGGREGATES.search(sql)
    spec = json.loads(chart["vega_spec"])
    assert spec["data"] == {"name": "query1"} and spec["mark"] == "bar"
    import hashlib
    assert lines[2].endswith("sha256:" + hashlib.sha256(sql.encode()).hexdigest())
    assert out.splitlines() == ["wrote queries/credits_by_warehouse.json",
                                "wrote rill/components/credits_by_warehouse.yaml"]


def test_save_is_deterministic(tmp_path, capsys):
    outs = []
    for i in range(2):
        (tmp_path / str(i)).mkdir()
        p = make_s4_project(tmp_path / str(i))
        save(p, capsys, ask(p, capsys))
        outs.append(((p / "queries/credits_by_warehouse.json").read_bytes(),
                     (p / "rill/components/credits_by_warehouse.yaml").read_bytes()))
    assert outs[0] == outs[1]


def test_a_query_file_without_a_question_saves_with_null_question(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = save(p, capsys, ask(p, capsys), question=None)
    assert rc == 0, err
    saved = json.loads((p / "queries/credits_by_warehouse.json").read_text())
    assert saved["question"] is None and saved["question_normalized"] is None


def test_a_saved_name_is_not_overwritten_without_force(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)
    assert save(p, capsys, answer)[0] == 0
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer)
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_NAME_EXISTS: ")
    assert tree_hash(p) == snap
    assert save(p, capsys, answer, "--force")[0] == 0


def test_a_proposed_query_is_upgraded_in_place(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)
    (p / "queries").mkdir()
    (p / "queries/credits_by_warehouse.json").write_text(json.dumps(
        {"name": "credits_by_warehouse", "status": "proposed", "question": "credits by warehouse"}))
    rc, _, err = save(p, capsys, answer, question=None)
    assert rc == 0, err
    saved = json.loads((p / "queries/credits_by_warehouse.json").read_text())
    assert saved["status"] == "saved" and saved["question"] == "credits by warehouse"


# ----------------------------------------------------------------------------- refusals leave the tree


def test_like_filter_is_unsupported_by_rill_and_nothing_is_written(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    q = write_query(p / "out" / "like.json", filters=[
        {"dimension": "warehouse_name", "op": "like", "values": ["FIN%"]}])
    answer = ask(p, capsys, q)
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer, name="finance_only")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_UNSUPPORTED_BY_RILL_ROUTE: ")
    assert tree_hash(p) == snap


def test_verify_without_expected_sql_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer, "--verify", "--by", "me", name="credits_by_wh2")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_NO_INDEPENDENT_SQL: ")
    assert tree_hash(p) == snap


def test_verify_with_disagreeing_sql_is_a_mismatch_and_nothing_is_written(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys, tenth_query(p))
    snap = tree_hash(p)
    wrong = TENTH_SQL.replace("AVG(credits_used)", "AVG(credits_used) + 1")
    rc, _, err = save(p, capsys, answer, "--verify", "--by", "sprint-4", "--expected-sql", wrong,
                      name=TENTH["name"], question=TENTH["question"])
    assert rc == 2 and "E_VERIFY_MISMATCH: " in err.strip().splitlines()[0] + err
    assert tree_hash(p) == snap


def test_a_non_select_expected_sql_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer, "--verify", "--by", "me", "--expected-sql",
                      "DELETE FROM warehouse_metering_history", name="x", question="q")
    assert rc == 2 and "E_STATEMENT_NOT_ALLOWED" in err
    assert tree_hash(p) == snap


def test_save_is_a_transaction_a_failure_after_the_first_output_changes_nothing(
    tmp_path, capsys, monkeypatch
):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)
    snap = tree_hash(p)
    real = fs_txn._stage
    calls = []

    def boom(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(fs_txn, "_stage", boom)
    with pytest.raises(OSError):
        save(p, capsys, answer)
    assert tree_hash(p) == snap and not (p / ".agent-bi-tmp").exists()


def test_the_chartless_answer_cannot_be_saved(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    q = write_query(p / "out" / "two.json", metrics=["row_count", "total_credits_used"], dimensions=[])
    answer = ask(p, capsys, q)
    assert json.loads(answer.read_text())["chart"] is None
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer, name="two")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_CHART_INCOMPATIBLE: ")
    assert tree_hash(p) == snap


def test_a_stale_answer_cannot_be_saved(tmp_path, capsys):
    from tests.s4_helpers import change_total_credits_used

    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)
    change_total_credits_used(p)
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer)
    assert rc == 2 and "semantic_digest differs" in err
    assert tree_hash(p) == snap


# ----------------------------------------------------------------------------- the verified path


def test_verify_promotes_a_verified_query(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    n_before = len(yaml.safe_load((p / "semantic" / f"{MODEL}.yaml").read_text())["verified_queries"])
    answer = ask(p, capsys, tenth_query(p))
    rc, out, err = save(p, capsys, answer, "--verify", "--by", "sprint-4", "--expected-sql", TENTH_SQL,
                        name=TENTH["name"], question=TENTH["question"])
    assert rc == 0, err
    assert out.splitlines() == [
        "wrote semantic/idle_warehouse_waste.yaml",
        "wrote semantic/idle_warehouse_waste.provenance.yaml",
        "wrote queries/avg_by_warehouse.json",
        "wrote tests/fixtures/known-answers/idle_warehouse_waste/avg_by_warehouse.json",
        "wrote rill/components/avg_by_warehouse.yaml",
    ]
    ka_path = p / "tests/fixtures/known-answers/idle_warehouse_waste/avg_by_warehouse.json"
    ka = json.loads(ka_path.read_text())
    assert ka["expected_sql_by"] == "sprint-4" and ka["expected_sql"] == TENTH_SQL
    assert ka["expected_computed_by"] == "direct-sql"
    assert list(ka) == ["name", "query_hash", "semantic_digest", "definition_digests", "semantic_query",
                        "expected", "expected_computed_by", "expected_sql", "expected_sql_by",
                        "expected_at", "fixture_sha256"]
    assert ka["expected"]["columns"] == ["warehouse_name", "avg_credits_used"]
    model = yaml.safe_load((p / "semantic" / f"{MODEL}.yaml").read_text())
    vqs = model["verified_queries"]
    assert len(vqs) == n_before + 1 and vqs[-1]["name"] == TENTH["name"]
    assert vqs[-1]["verified_by"] == "sprint-4" and isinstance(vqs[-1]["verified_at"], int)
    assert vqs[-1]["question"] == TENTH["question"]
    assert "AT(TIMESTAMP" not in vqs[-1]["sql"] and '"FIXTURE"."MAIN"' in vqs[-1]["sql"]
    side = yaml.safe_load((p / "semantic" / f"{MODEL}.provenance.yaml").read_text())
    rec = side["verified_queries"][TENTH["name"]]
    assert list(rec) == ["query_hash", "semantic_digest", "definition_digests", "semantic_query",
                         "question_normalized", "known_answer"]
    assert rec["question_normalized"] == TENTH["question"]
    assert set(rec["definition_digests"]) == {"base_table", "avg_credits_used", "warehouse_name"}
    assert json.loads((p / "queries/avg_by_warehouse.json").read_text())["status"] == "verified"
    assert cli(p, capsys, "validate", "--model", MODEL)[0] == 0
    rc, out, _ = cli(p, capsys, "verify", "--model", MODEL)
    assert rc == 0 and out.count("PASS ") == n_before + 1 and f"PASS {TENTH['name']}" in out
    assert cli(p, capsys, "verify", "--model", MODEL, "--coverage")[0] == 0
    # and the question now resolves with no LLM
    rc, out, _ = cli(p, capsys, "ask", "--model", MODEL, "--question", "Average credits per warehouse?")
    assert rc == 0 and f"trust: verified (verified as {TENTH['name']};" in out


def test_verify_only_adds_to_the_model_and_sidecar(tmp_path, capsys):
    import difflib

    p = make_s4_project(tmp_path)
    before_model = (p / "semantic" / f"{MODEL}.yaml").read_text()
    before_side = (p / "semantic" / f"{MODEL}.provenance.yaml").read_text()
    save(p, capsys, ask(p, capsys, tenth_query(p)), "--verify", "--by", "sprint-4",
         "--expected-sql", TENTH_SQL, name=TENTH["name"], question=TENTH["question"])
    after_model = (p / "semantic" / f"{MODEL}.yaml").read_text()
    after_side = (p / "semantic" / f"{MODEL}.provenance.yaml").read_text()
    for before, after in ((before_model, after_model), (before_side, after_side)):
        removed = [ln for ln in difflib.ndiff(before.splitlines(), after.splitlines()) if ln[0] == "-"]
        assert removed == []  # nothing existing changed or moved: the new records were only added
    assert after_model.startswith(before_model)  # appended after the existing entries


def test_the_committed_ninth_verified_query():
    """Task 3's fixed ninth query, promoted on the committed model through the real CLI."""
    from tests.conftest import ROOT

    model = yaml.safe_load((ROOT / "semantic" / f"{MODEL}.yaml").read_text())
    vq = next(v for v in model["verified_queries"] if v["name"] == "warehouses_by_day")
    assert vq["question"] == "how many warehouses per day" and vq["verified_by"] == "sprint-4"
    assert 'TRY_TO_DATE("usage_day")' in vq["sql"] and "AT(TIMESTAMP" not in vq["sql"]
    side = yaml.safe_load((ROOT / "semantic" / f"{MODEL}.provenance.yaml").read_text())
    sq = side["verified_queries"]["warehouses_by_day"]["semantic_query"]
    assert sq["metrics"] == ["distinct_warehouse_name"] and sq["dimensions"] == []
    assert sq["time"] == {"dimension": "usage_day", "grain": "day"} and sq["order_by"] == []
    ka = json.loads((ROOT / "tests/fixtures/known-answers" / MODEL / "warehouses_by_day.json").read_text())
    assert ka["expected_sql_by"] == "sprint-4"
    assert ka["expected_sql"] == (
        "SELECT date(usage_day) AS time_bucket, COUNT(DISTINCT warehouse_name) AS "
        "distinct_warehouse_name FROM warehouse_metering_history GROUP BY date(usage_day) "
        "ORDER BY time_bucket ASC, distinct_warehouse_name DESC"
    )
    saved = json.loads((ROOT / "queries/warehouses_by_day.json").read_text())
    assert saved["status"] == "verified"


def test_reverifying_the_same_name_needs_force(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)  # credits_by_warehouse, already verified
    sql = ("SELECT warehouse_name, SUM(credits_used) AS total_credits_used FROM "
           "warehouse_metering_history GROUP BY warehouse_name")
    rc, _, err = save(p, capsys, answer, "--verify", "--by", "me", "--expected-sql", sql,
                      name="credits_by_warehouse")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_NAME_EXISTS: ")
    rc, _, err = save(p, capsys, answer, "--verify", "--by", "me", "--expected-sql", sql, "--force",
                      name="credits_by_warehouse")
    assert rc == 0, err
    ka = json.loads((p / "tests/fixtures/known-answers/idle_warehouse_waste/credits_by_warehouse.json").read_text())
    assert ka["expected_sql_by"] == "me"
    model = yaml.safe_load((p / "semantic" / f"{MODEL}.yaml").read_text())
    assert len(model["verified_queries"]) == 9  # replaced in place, not duplicated
    assert cli(p, capsys, "verify", "--model", MODEL)[0] == 0


# ----------------------------------------------------------------------------- canvas placement


def fork(p, capsys, name="main"):
    rc, _, err = cli(p, capsys, "canvas", "fork", "--model", MODEL, "--name", name)
    assert rc == 0, err


def test_add_to_canvas_appends_a_last_row_and_ask_reports_published(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    fork(p, capsys)
    before = (p / "rill/dashboards/main.yaml").read_text()
    answer = ask(p, capsys)
    assert json.loads(answer.read_text())["provenance"]["receipt"]["published"] is False
    rc, out, err = save(p, capsys, answer, "--add-to-canvas", "main")
    assert rc == 0, err
    after = (p / "rill/dashboards/main.yaml").read_text()
    assert after == before + (
        "  - items:\n      - component: credits_by_warehouse\n        width: 12\n    height: 400px\n"
    )
    doc = yaml.safe_load(after)
    assert doc["rows"][-1] == {"items": [{"component": "credits_by_warehouse", "width": 12}],
                               "height": "400px"}
    assert "wrote rill/dashboards/main.yaml" in out
    # the generated default canvas is untouched
    assert "credits_by_warehouse" not in (p / f"rill/dashboards/{MODEL}_default_canvas.yaml").read_text()
    again = ask(p, capsys)
    assert json.loads(again.read_text())["provenance"]["receipt"]["published"] is True


def test_published_flag(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    fork(p, capsys)
    assert save(p, capsys, ask(p, capsys), "--add-to-canvas", "main")[0] == 0

    def published():
        env = json.loads(ask(p, capsys).read_text())
        return env["provenance"]["receipt"]["published"]

    assert published() is True
    # a hand edit of the component's metrics_sql: no longer published, and validate warns
    comp = p / "rill/components/credits_by_warehouse.yaml"
    comp.write_text(comp.read_text().replace("LIMIT 1000", "LIMIT 2"))
    assert published() is False
    rc, out, _ = cli(p, capsys, "validate", "--model", MODEL)
    assert rc == 0
    assert out.splitlines()[0].startswith("W_COMPONENT_DIVERGED rill/components/credits_by_warehouse.yaml")
    assert out.splitlines()[-1] == f"OK: {MODEL}"


def test_a_pristine_component_makes_validate_quiet(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    fork(p, capsys)
    save(p, capsys, ask(p, capsys), "--add-to-canvas", "main")
    rc, out, _ = cli(p, capsys, "validate", "--model", MODEL)
    assert rc == 0 and out == f"OK: {MODEL}\n"


def test_a_component_on_no_canvas_is_not_published(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    save(p, capsys, ask(p, capsys))
    assert json.loads(ask(p, capsys).read_text())["provenance"]["receipt"]["published"] is False


def test_a_changed_definition_unpublishes_the_component(tmp_path, capsys):
    from tests.s4_helpers import change_total_credits_used

    p = make_s4_project(tmp_path)
    fork(p, capsys)
    assert save(p, capsys, ask(p, capsys), "--add-to-canvas", "main")[0] == 0
    change_total_credits_used(p)  # the component's agent_bi_semantic_digest is now stale
    assert json.loads(ask(p, capsys).read_text())["provenance"]["receipt"]["published"] is False


def test_add_to_canvas_with_a_non_inf_default_range_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    fork(p, capsys)
    canvas = p / "rill/dashboards/main.yaml"
    canvas.write_text(canvas.read_text().replace("time_range: inf", "time_range: P7D"))
    answer = ask(p, capsys)
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer, "--add-to-canvas", "main")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_CANVAS_NOT_VERIFIED_SCOPE: ")
    assert tree_hash(p) == snap


@pytest.mark.parametrize("edit", [
    lambda d: d.setdefault("defaults", {}).update({"filters": {MODEL: "warehouse_name = 'X'"}}),
    lambda d: d.update({"filters": {"enable": True, "required": ["warehouse_name"]}}),
])
def test_default_or_required_filters_also_refuse_the_canvas(tmp_path, capsys, edit):
    p = make_s4_project(tmp_path)
    fork(p, capsys)
    edit_yaml(p / "rill/dashboards/main.yaml", edit)
    answer = ask(p, capsys)
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer, "--add-to-canvas", "main")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_CANVAS_NOT_VERIFIED_SCOPE: ")
    assert tree_hash(p) == snap


def test_editing_the_canvas_to_p7d_after_save_unpublishes(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    fork(p, capsys)
    assert save(p, capsys, ask(p, capsys), "--add-to-canvas", "main")[0] == 0
    assert json.loads(ask(p, capsys).read_text())["provenance"]["receipt"]["published"] is True
    canvas = p / "rill/dashboards/main.yaml"
    canvas.write_text(canvas.read_text().replace("time_range: inf", "time_range: P7D"))
    assert json.loads(ask(p, capsys).read_text())["provenance"]["receipt"]["published"] is False
    canvas.write_text(canvas.read_text().replace("time_range: P7D", "time_range: inf"))
    edit_yaml(canvas, lambda d: d.update({"filters": {"required": ["warehouse_name"]}}))
    assert json.loads(ask(p, capsys).read_text())["provenance"]["receipt"]["published"] is False


def test_a_generated_canvas_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer, "--add-to-canvas", f"{MODEL}_default_canvas")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_GENERATED_FILE: ")
    assert tree_hash(p) == snap


def test_a_missing_canvas_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    answer = ask(p, capsys)
    snap = tree_hash(p)
    rc, _, err = save(p, capsys, answer, "--add-to-canvas", "nope")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_CANVAS_NOT_FOUND: ")
    assert tree_hash(p) == snap


def test_placing_the_same_component_twice_is_idempotent(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    fork(p, capsys)
    answer = ask(p, capsys)
    assert save(p, capsys, answer, "--add-to-canvas", "main")[0] == 0
    first = (p / "rill/dashboards/main.yaml").read_text()
    assert save(p, capsys, answer, "--add-to-canvas", "main", "--force")[0] == 0
    assert (p / "rill/dashboards/main.yaml").read_text() == first


def test_append_canvas_row_keeps_operator_comments():
    from agent_bi.explore.save import append_canvas_row

    text = "# my canvas\ntype: canvas\ndefaults:\n  time_range: inf\nrows:\n  # first row\n  - items:\n      - component: a\n        width: 12\n    height: 400px\n"
    out = append_canvas_row(text, "b")
    assert out.startswith("# my canvas\n") and "# first row" in out
    assert yaml.safe_load(out)["rows"][-1]["items"][0]["component"] == "b"
    flow = "type: canvas\nrows: []\n"
    assert yaml.safe_load(append_canvas_row(flow, "b"))["rows"][0]["items"][0]["component"] == "b"
    with pytest.raises(AgentBiError):
        append_canvas_row("type: canvas\n", "b")
