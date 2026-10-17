"""The 15 frozen behavioural cases of architecture §4.8.5 (M2.x1 trust contract).

Every premise is built in a scratch project with real receipts. Rill itself is faked with the
same ``rill_route`` patching technique as ``tests/test_verify_rill.py``; where a premise is an
effect inside Rill (A′ counterexamples), the fake changes what it returns when - and only
when - the premise's cause is present in the scratch project.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
from pathlib import Path

from agent_bi.errors import validation_error
from agent_bi.hashing import expr_sha256
from agent_bi.query.compile import compile as compile_query
from agent_bi.query.contract import parse_query
from agent_bi.semantic.model import load_model_dict
from agent_bi.semantic.provenance import load_sidecar_dict
from agent_bi.verify import rill_route
from agent_bi.verify import serving as sv
from tests.conftest import ROOT
from tests.s3_helpers import MODEL
from tests.s4_helpers import cli, edit_yaml, make_s4_project
from tests.s6_helpers import make_project as make_s6_project
from tests.test_known_answers import NAMES
from tests.test_verify_rill import as_csv, serve

ENRICHED = "warehouse_metering_enriched"
AGE = r"\d+ (?:s|min)"
INTERVAL = rf"\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d–\d\d:\d\d:\d\d UTC \({AGE} ago\)"
EXCL = (
    "Not covered: other read paths and caches, other users, rendering, any time outside the check."
)
TOMBSTONE = "REMOVED in agent-bi-provenance-record/2.0: read receipt.evidence (definition, serving)"
MATCHED_9 = (
    rf"During {INTERVAL}, through Rill Metrics SQL \(CLI\), as the local default user, with no "
    r"added canvas filters or time range, 9 of 9 verified queries returned their known answers "
    r"under agent-bi-known-answer/1\."
)


# ------------------------------------------------------------------------------ builders


def confirm_all(project: Path, model: str = MODEL) -> None:
    def fn(side):
        for entry in side["entries"].values():
            entry["status"] = "confirmed"

    edit_yaml(project / "semantic" / f"{model}.provenance.yaml", fn)


def governed_project(tmp_path: Path) -> Path:
    """The baseline: 9 verified queries, all definitions confirmed, ``rill/`` present."""
    p = make_s4_project(tmp_path)
    confirm_all(p)
    return p


def _ka_expected(project: Path, model: str, name: str) -> dict:
    return json.loads(
        (project / "tests/fixtures/known-answers" / model / f"{name}.json").read_text()
    )["expected"]


def rill_csv_for(project: Path, model: str, name: str) -> str:
    ka = _ka_expected(project, model, name)
    rows = []
    for r in ka["rows"]:
        rows.append(
            [c + "T00:00:00Z" if isinstance(c, str) and len(c) == 10 and c[4] == "-" else c
             for c in r]
        )
    return as_csv(ka["columns"], rows)


def fake_rill(monkeypatch, project: Path, model: str = MODEL, outputs=None, hook=None):
    """Patch the Rill runner. A verified query returns its known answer, unless ``outputs`` has
    a result for it or ``hook(name)`` returns one. Anything else is the readiness probe."""
    mdl = load_model_dict(str(project / f"semantic/{model}.yaml"))
    side = load_sidecar_dict(project / f"semantic/{model}.provenance.yaml")
    by_sql = {
        compile_query(parse_query(v["semantic_query"]), mdl, "rill").compiled_sql: n
        for n, v in side["verified_queries"].items()
    }

    def fake(project_path, sql, limit, binary=None, runner=None):
        if sql not in by_sql:
            return rill_route.RillResult(0, ["row_count"], [["21"]], "", "row_count\n21\n")
        name = by_sql[sql]
        for source in (outputs or {}, {name: hook(name)} if hook else {}):
            if source.get(name) is not None:
                return source[name]
        out = rill_csv_for(project, model, name)
        parsed = list(csv.reader(io.StringIO(out)))
        return rill_route.RillResult(0, parsed[0], parsed[1:], "", out)

    monkeypatch.setattr(rill_route, "run_metrics_sql", fake)
    serve(monkeypatch, project)
    monkeypatch.setattr(rill_route, "rill_binary", lambda: "rill")


def wrong(columns, rows):
    out = as_csv(columns, rows)
    parsed = list(csv.reader(io.StringIO(out)))
    return rill_route.RillResult(0, parsed[0], parsed[1:], "", out)


def run_check(project, capsys, model=MODEL):
    return cli(project, capsys, "verify", "--model", model, "--engine", "rill")


def status_lines(project, capsys, model=MODEL) -> list[str]:
    rc, out, err = cli(project, capsys, "status", "--model", model)
    assert rc == 0, err
    return out.splitlines()


def ask_receipt(project, capsys, name, model=MODEL) -> dict:
    side = load_sidecar_dict(project / f"semantic/{model}.provenance.yaml")
    question = side["verified_queries"][name]["question_normalized"]
    rc, out, err = cli(project, capsys, "ask", "--model", model, "--question", question, "--json")
    assert rc == 0, err
    return json.loads(out)["provenance"]["receipt"]


def receipts(project, model=MODEL) -> list[Path]:
    return sorted((project / "out" / "serving" / model).glob("*.json"))


def edit_row_count_expr(project: Path, new: str = 'COUNT(DISTINCT "usage_day")') -> None:
    def model(m):
        next(x for x in m["tables"][0]["metrics"] if x["name"] == "row_count")["expr"] = new

    def sidecar(s):
        s["entries"]["tables[0].metrics[row_count]"]["expr_sha256"] = expr_sha256(new)

    edit_yaml(project / f"semantic/{MODEL}.yaml", model)
    edit_yaml(project / f"semantic/{MODEL}.provenance.yaml", sidecar)


def context_re(state: str) -> str:
    return (
        rf"Latest serving check recorded: {INTERVAL} \({state}\); "
        r"comparison rule agent-bi-known-answer/1\."
    )


def reverify(project: Path, capsys, name: str, expected_sql: str) -> dict:
    """The real ``save --verify --force`` path for an existing verified query, with
    independent SQL; returns the known-answer document it wrote."""
    ka_path = project / "tests/fixtures/known-answers" / MODEL / f"{name}.json"
    qfile = project / "out" / f"{name}.reverify.json"
    qfile.parent.mkdir(exist_ok=True)
    qfile.write_text(json.dumps(json.loads(ka_path.read_text())["semantic_query"]))
    rc, out, err = cli(project, capsys, "ask", "--model", MODEL, "--query-file", str(qfile))
    assert rc == 0, err
    answer = project / next(ln.split()[1] for ln in out.splitlines() if ln.endswith(".answer.json"))
    rc, _, err = cli(
        project, capsys, "save", "--from", str(answer), "--name", name, "--question",
        load_sidecar_dict(project / f"semantic/{MODEL}.provenance.yaml")["verified_queries"][name][
            "question_normalized"],
        "--verify", "--by", "m2x1-remediation", "--expected-sql", expected_sql, "--force",
    )
    assert rc == 0, err
    return json.loads(ka_path.read_text())  # must be valid JSON


def assert_baseline_definition(lines: list[str]) -> None:
    assert lines[0] == "Definition: 9 verified queries: 9 governed, 0 verified, 0 stale"


def serving_text(lines: list[str]) -> list[str]:
    assert lines[1].startswith("Serving:    ")
    return [lines[1].removeprefix("Serving:    ")] + [ln.strip() for ln in lines[2:]]


# ------------------------------------------------------------------------------ cases


def test_case_01_a_matched_check_reports_matched_9_of_9(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p)
    rc, out, err = run_check(p, capsys)
    assert rc == 0, err
    assert [ln for ln in out.splitlines() if ln.startswith("PASS ")] == [f"PASS {n}" for n in NAMES]
    lines = status_lines(p, capsys)
    assert_baseline_definition(lines)
    text = serving_text(lines)
    assert re.fullmatch(MATCHED_9, text[0]) and text[1:] == [EXCL]
    assert json.loads(receipts(p)[0].read_text())["state"] == "matched"


def test_case_02_fresh_clone_has_no_serving_check(tmp_path, capsys):
    p = governed_project(tmp_path)
    assert not (p / "out").exists()  # constructed: nothing has ever run here
    lines = status_lines(p, capsys)
    assert_baseline_definition(lines)
    assert lines[1:] == ["Serving:    No serving check recorded.", f"            {EXCL}"]
    assert not (p / "out").exists()  # status wrote nothing
    ev = ask_receipt(p, capsys, "row_count_all")["evidence"]
    assert ev["serving"] == {
        "state": None, "claim": "No serving check recorded.", "receipt_id": None,
        "interval": None, "applicability": None, "query_outcome": None,
    }
    assert ev["definition"]["state"] == "governed"


def test_case_03_env_init_sql_mismatch_stays_failed_after_the_line_is_removed(
    tmp_path, capsys, monkeypatch
):
    p = governed_project(tmp_path)
    env_file = p / ".env"
    env_file.write_text("connector.duckdb.init_sql=CREATE OR REPLACE VIEW t AS SELECT 22 AS n\n")

    def hook(name):  # the rewritten relation takes effect only while the .env line exists
        if name == "row_count_all" and "init_sql" in (
            env_file.read_text() if env_file.exists() else ""
        ):
            return wrong(["row_count"], [["22"]])
        return None

    fake_rill(monkeypatch, p, hook=hook)
    rc, out, _ = run_check(p, capsys)
    assert rc == 1 and "FAIL row_count_all" in out  # constructed: at least one MISMATCH
    before = status_lines(p, capsys)
    env_file.unlink()  # the line is removed; no new check runs
    after = status_lines(p, capsys)
    assert before == after
    assert_baseline_definition(after)
    text = serving_text(after)
    assert re.fullmatch(
        rf"During {INTERVAL}, through Rill Metrics SQL \(CLI\), as the local default user, "
        r"1 of 9 verified queries returned results that differ from their known answers under "
        r"agent-bi-known-answer/1\.",
        text[0],
    )
    assert "row_count_all: row 0: expected [21.0], got [22.0]" in "\n".join(text)
    assert len(receipts(p)) == 1  # that receipt is the latest and only one


def test_case_04_migration_residue_mismatch_is_failed(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    migration = p / "rill" / "migrations" / "0001_rewrite.sql"
    migration.parent.mkdir()
    migration.write_text("CREATE OR REPLACE VIEW t AS SELECT 22 AS n;\n")
    migration.unlink()  # the migration is deleted ...
    restarted = {"v": False}  # ... but Rill has not restarted, so its residue still applies

    def hook(name):
        if name == "row_count_all" and not restarted["v"]:
            return wrong(["row_count"], [["22"]])
        return None

    fake_rill(monkeypatch, p, hook=hook)
    rc, out, _ = run_check(p, capsys)
    assert rc == 1 and "FAIL row_count_all" in out and not migration.exists()
    lines = status_lines(p, capsys)
    assert_baseline_definition(lines)
    assert re.search(r"1 of 9 verified queries returned results that differ", lines[1])


def test_case_05_edited_expr_makes_one_query_stale_and_the_receipt_does_not_apply(
    tmp_path, capsys, monkeypatch
):
    p = governed_project(tmp_path)
    side = load_sidecar_dict(p / f"semantic/{MODEL}.provenance.yaml")
    users = [n for n, v in side["verified_queries"].items()
             if "row_count" in v["semantic_query"]["metrics"]]
    assert users == ["row_count_all"]  # constructed: exactly one query references it
    fake_rill(monkeypatch, p)
    assert run_check(p, capsys)[0] == 0
    edit_row_count_expr(p)  # no new check
    lines = status_lines(p, capsys)
    assert lines[0] == "Definition: 9 verified queries: 8 governed, 0 verified, 1 stale (row_count_all: row_count)"
    text = serving_text(lines)
    assert re.fullmatch(MATCHED_9, text[0])
    assert text[1:] == ["Not covered by that check: row_count_all (stale)", EXCL]
    ev = ask_receipt(p, capsys, "row_count_all")["evidence"]
    assert ev["definition"]["state"] == "stale"
    assert ev["serving"]["applicability"] == "definition-stale" and ev["serving"]["query_outcome"] is None
    assert ev["serving"]["state"] == "matched"
    assert ev["serving"]["claim"] == (
        "This question's definition differs from its verified version; no serving check "
        "applies to the current definition."
    )


NINE_ENRICHED_JOIN = (
    "FROM warehouse_metering_history m LEFT JOIN show_warehouses w ON w.name = m.warehouse_name"
)
SEVEN_MORE = [  # (name, metric, independent SQL): with the repo's two, nine registered-model queries
    ("row_count_all", "row_count", f"SELECT COUNT(*) AS row_count {NINE_ENRICHED_JOIN}"),
    ("total_credits_all", "total_credits_used",
     f"SELECT SUM(m.credits_used) AS total_credits_used {NINE_ENRICHED_JOIN}"),
    ("avg_credits_all", "avg_credits_used",
     f"SELECT AVG(m.credits_used) AS avg_credits_used {NINE_ENRICHED_JOIN}"),
    ("compute_credits_all", "total_credits_used_compute",
     f"SELECT SUM(m.credits_used_compute) AS total_credits_used_compute {NINE_ENRICHED_JOIN}"),
    ("cloud_credits_all", "total_credits_used_cloud_services",
     ("SELECT SUM(m.credits_used_cloud_services) AS total_credits_used_cloud_services "
      f"{NINE_ENRICHED_JOIN}")),
    ("distinct_wh_all", "distinct_warehouse_name",
     f"SELECT COUNT(DISTINCT m.warehouse_name) AS distinct_warehouse_name {NINE_ENRICHED_JOIN}"),
    ("distinct_cc_all", "distinct_cost_center",
     f"SELECT COUNT(DISTINCT w.cost_center) AS distinct_cost_center {NINE_ENRICHED_JOIN}"),
]


def test_case_06_upstream_rill_model_edit_makes_every_query_stale(tmp_path, capsys, monkeypatch):
    from tests.test_rill_models import create_verified

    p = make_s6_project(tmp_path)
    shutil.copytree(ROOT / "tests/fixtures/known-answers", p / "tests/fixtures/known-answers")
    shutil.copytree(ROOT / "rill", p / "rill", dirs_exist_ok=True, ignore=shutil.ignore_patterns("tmp"))
    for name, metric, sql in SEVEN_MORE:  # the real save --verify path, independent SQL
        create_verified(
            p, capsys, name, name.replace("_", " "), sql,
            {"schema_version": "semantic-query/1.0", "model": ENRICHED, "table": ENRICHED,
             "metrics": [metric], "dimensions": []},
            model=ENRICHED,
        )
    confirm_all(p, ENRICHED)
    names = ["credits_by_cost_center", "credits_by_size_by_month"] + [n for n, _, _ in SEVEN_MORE]
    assert len(names) == 9
    before = status_lines(p, capsys, ENRICHED)
    assert before[0] == "Definition: 9 verified queries: 9 governed, 0 verified, 0 stale"
    fake_rill(monkeypatch, p, ENRICHED)
    rc, out, err = run_check(p, capsys, ENRICHED)
    assert rc == 0, (out, err)
    sql = p / "rill/models/warehouse_metering_enriched.sql"
    text = sql.read_text()
    assert '  m."credits_used"\n' in text
    sql.write_text(text.replace('  m."credits_used"\n', '  m."credits_used" + 0 AS "credits_used"\n'))
    lines = status_lines(p, capsys, ENRICHED)
    assert lines[0] == (
        "Definition: 9 verified queries: 0 governed, 0 verified, 9 stale "
        + " ".join(f"({n}: upstream Rill model changed)" for n in names)
    )
    t = serving_text(lines)
    assert re.fullmatch(MATCHED_9, t[0])
    assert t[1:] == [f"Not covered by that check: {n} (stale)" for n in names] + [EXCL]
    for n in names:
        ev = ask_receipt(p, capsys, n, ENRICHED)["evidence"]
        assert ev["definition"]["state"] == "stale"
        assert "upstream Rill model changed" in ev["definition"]["claim"]
        assert ev["serving"]["applicability"] == "definition-stale"


def test_case_07_reverified_with_a_new_known_answer_is_a_different_definition_or_answer(
    tmp_path, capsys, monkeypatch
):
    p = governed_project(tmp_path)
    ka_path = p / "tests/fixtures/known-answers" / MODEL / "row_count_all.json"
    old = json.loads(ka_path.read_text())
    fake_rill(monkeypatch, p)
    assert run_check(p, capsys)[0] == 0
    edit_row_count_expr(p)
    assert "1 stale" in status_lines(p, capsys)[0]  # case 5's state
    new = reverify(
        p, capsys, "row_count_all",
        'SELECT COUNT(DISTINCT "usage_day") AS row_count FROM warehouse_metering_history',
    )
    assert new["expected"]["rows"] != old["expected"]["rows"]  # a new known answer
    assert new["semantic_digest"] != old["semantic_digest"] and new["query_hash"] == old["query_hash"]
    assert sv.known_answer_content_sha256(new["expected"]) != sv.known_answer_content_sha256(
        old["expected"]
    )
    side = load_sidecar_dict(p / f"semantic/{MODEL}.provenance.yaml")
    assert side["verified_queries"]["row_count_all"]["semantic_digest"] == new["semantic_digest"]
    assert sv.load_known_answer(p, MODEL, "row_count_all", side["verified_queries"]["row_count_all"])
    lines = status_lines(p, capsys)
    assert_baseline_definition(lines)
    assert serving_text(lines)[1:3] == [
        "Not covered by that check: row_count_all (different definition or known answer)", EXCL,
    ]
    s = ask_receipt(p, capsys, "row_count_all")["evidence"]["serving"]
    assert s["applicability"] == "different-definition-or-answer" and s["query_outcome"] is None
    assert re.fullmatch(
        rf"The serving check during {INTERVAL} compared this question under a different "
        r"definition or known answer than the current one; it is not serving evidence for the "
        r"current definition\.",
        s["claim"],
    )


def test_case_08_reverified_unchanged_still_applies(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    ka_path = p / "tests/fixtures/known-answers" / MODEL / "row_count_all.json"
    old = json.loads(ka_path.read_text())
    fake_rill(monkeypatch, p)
    assert run_check(p, capsys)[0] == 0
    edit_yaml(p / f"semantic/{MODEL}.yaml", lambda m: next(
        v for v in m["verified_queries"] if v["name"] == "row_count_all"
    ).update(verified_at=1))  # so that a real re-verification visibly moves the time
    new = reverify(p, capsys, "row_count_all", old["expected_sql"])  # unchanged definition, same SQL
    model = load_model_dict(str(p / f"semantic/{MODEL}.yaml"))
    verified_at = next(v for v in model["verified_queries"] if v["name"] == "row_count_all")["verified_at"]
    assert verified_at > 1  # the verification time moved ...
    for key in ("query_hash", "semantic_digest", "definition_digests", "expected"):
        assert new[key] == old[key]  # ... neither digest, nor the known answer, did
    lines = status_lines(p, capsys)
    assert_baseline_definition(lines)
    assert serving_text(lines)[1:] == [EXCL]  # no "Not covered" line
    s = ask_receipt(p, capsys, "row_count_all")["evidence"]["serving"]
    assert s["applicability"] == "applies" and s["query_outcome"] == "MATCH"
    assert re.fullmatch(
        rf"In the serving check during {INTERVAL}, this question returned its known answer "
        r"under agent-bi-known-answer/1\.",
        s["claim"],
    )


def test_case_09_editing_the_metrics_view_yaml_changes_nothing_shown(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p)
    assert run_check(p, capsys)[0] == 0
    strip_age = lambda ls: [re.sub(rf"\({AGE} ago\)", "(AGE)", ln) for ln in ls]
    before = strip_age(status_lines(p, capsys))
    views = sorted((p / "rill" / "metrics").glob("*.yaml"))
    assert views
    views[0].write_text(views[0].read_text() + "# edited directly after the check\n")
    after = strip_age(status_lines(p, capsys))
    assert before == after
    assert_baseline_definition(after)
    assert "9 of 9 verified queries returned their known answers" in after[1]
    assert after[2:] == [f"            {EXCL}"]


def test_case_10_zero_verified_queries_is_no_evidence(tmp_path, capsys, monkeypatch):
    p = make_s4_project(tmp_path)
    edit_yaml(p / f"semantic/{MODEL}.yaml", lambda m: m.pop("verified_queries"))
    edit_yaml(p / f"semantic/{MODEL}.provenance.yaml", lambda s: s.update(verified_queries={}))
    fake_rill(monkeypatch, p)
    rc, out, err = run_check(p, capsys)
    assert rc == 0, err
    assert re.search(r"serving receipt: out/serving/.*\(no-evidence\)", out)
    lines = status_lines(p, capsys)
    assert lines[0] == "Definition: No verified queries."
    t = serving_text(lines)
    assert re.fullmatch(
        rf"During {INTERVAL}, no verified query was compared through Rill Metrics SQL \(CLI\); "
        r"this check is not serving evidence\.",
        t[0],
    )
    assert len(t) == 3 and re.fullmatch(context_re("no-evidence"), t[1]) and t[2] == EXCL


def test_case_11_all_queries_refused_by_the_route_is_no_evidence(tmp_path, capsys, monkeypatch):
    from agent_bi.verify import known_answers

    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p)
    real = known_answers.compile_query

    def refuse(q, model, dialect, **kw):
        if dialect == "rill":
            raise validation_error("UNSUPPORTED_BY_RILL_ROUTE", "refused for the test")
        return real(q, model, dialect, **kw)

    monkeypatch.setattr(known_answers, "compile_query", refuse)
    rc, out, err = run_check(p, capsys)
    assert rc == 0, err
    assert sum(ln.startswith("UNSUPPORTED_BY_ROUTE ") for ln in out.splitlines()) == 9
    lines = status_lines(p, capsys)
    assert_baseline_definition(lines)
    t = serving_text(lines)
    assert "no verified query was compared" in t[0]
    assert len(t) == 3 and re.fullmatch(context_re("no-evidence"), t[1]) and t[2] == EXCL
    for name in NAMES:  # every one of the nine answers
        s = ask_receipt(p, capsys, name)["evidence"]["serving"]
        assert s["state"] == "no-evidence" and s["applicability"] == "applies"
        assert s["query_outcome"] == "NOT_CHECKED_UNSUPPORTED"
        assert re.fullmatch(
            rf"In the serving check during {INTERVAL}, this question was skipped \(not supported "
            r"by the route\); it was not compared\.",
            s["claim"],
        )


def test_case_12_one_error_among_matches_is_incomplete(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p, outputs={
        "row_count_all": rill_route.RillResult(1, [], [], "boom-private-stderr", "")
    })
    rc, out, err = run_check(p, capsys)
    assert rc == 3 and err.strip().splitlines()[-1].startswith("E_ENGINE: ")
    assert sum(ln.startswith("PASS ") for ln in out.splitlines()) == 8
    lines = status_lines(p, capsys)
    assert_baseline_definition(lines)
    t = serving_text(lines)
    assert re.fullmatch(
        rf"During {INTERVAL}, the check through Rill Metrics SQL \(CLI\) did not complete: "
        r"1 queries could not be asked \(E_ENGINE\)\. 8 queries matched\.",
        t[0],
    )
    assert "boom-private-stderr" not in receipts(p)[0].read_text()


def test_case_13_latest_by_finished_at_wins_over_a_copied_older_receipt(
    tmp_path, capsys, monkeypatch
):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p)
    assert run_check(p, capsys)[0] == 0
    older = receipts(p)[0]
    fake_rill(monkeypatch, p, outputs={"row_count_all": wrong(["row_count"], [["22"]])})
    assert run_check(p, capsys)[0] == 1
    assert len(receipts(p)) == 2
    # the older matched receipt is copied into out/ under a name that sorts last, with a newer mtime
    copy = older.parent / ("99990101T000000000000Z-" + older.name.split("-", 1)[1])
    shutil.copy(older, copy)
    assert json.loads(copy.read_text())["state"] == "matched"
    lines = status_lines(p, capsys)
    assert_baseline_definition(lines)
    assert "1 of 9 verified queries returned results that differ" in lines[1]
    assert ask_receipt(p, capsys, "row_count_all")["evidence"]["serving"]["state"] == "failed"


def test_case_14_a_matched_check_names_what_it_does_not_cover(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    original = _ka_expected(p, MODEL, "total_credits_all")["rows"][0][0]
    # A controlled data source and a dashboard response cache that is not on the CLI route.
    data = {"credits": original}
    cache: dict[str, float] = {}

    def dashboard_request() -> float:
        return cache.setdefault("credits", data["credits"])

    def hook(name):  # the route reads the data as it is when the check runs
        if name == "total_credits_all" and data["credits"] != original:
            return wrong(["total_credits_used"], [[str(data["credits"])]])
        return None

    data["credits"] = original + 1000  # 1. the data is edited
    assert dashboard_request() == original + 1000  # 2. a dashboard request warms the cache
    data["credits"] = original  # 3. the data is restored
    fake_rill(monkeypatch, p, hook=hook)
    rc, _, err = run_check(p, capsys)  # 4. the check runs against the restored data
    assert rc == 0, err
    assert json.loads(receipts(p)[0].read_text())["state"] == "matched"
    assert dashboard_request() == original + 1000  # 5. the dashboard still serves the warmed value
    assert dashboard_request() != data["credits"]
    lines = status_lines(p, capsys)
    assert_baseline_definition(lines)
    t = serving_text(lines)
    assert re.fullmatch(MATCHED_9, t[0]) and t[1:] == [EXCL]  # the exclusions name what is uncovered
    assert "other read paths and caches" in EXCL
    assert not any("dashboard" in ln.lower() for ln in lines)


def test_case_15_a_1x_consumer_gets_the_tombstone(tmp_path, capsys):
    p = governed_project(tmp_path)
    rec = ask_receipt(p, capsys, "row_count_all")
    assert rec["evidence"]["definition"]["state"] == "governed"
    assert rec["trust"] == TOMBSTONE
    assert rec.get("trust", "exploratory") == TOMBSTONE  # a defaulting reader gets it too
    assert rec["trust"] != "governed"  # a 1.x reader testing for governed gets false
    assert rec["trust"] not in ("governed", "verified", "exploratory", "stale", "none")
    assert rec["trust_reason"] == "REMOVED in agent-bi-provenance-record/2.0: read receipt.evidence"
    assert sv.TOMBSTONE_TRUST == TOMBSTONE


# ------------------------------------------------------------------------------ remediation round


def serving_block(out: str) -> list[str]:
    """The text lines from ``Serving:`` up to (excluding) ``published:``."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Serving:"))
    end = next(i for i, ln in enumerate(lines) if ln.startswith("published:"))
    return lines[start:end]


def ask_text(project, capsys, name=None, query_file=None, model=MODEL) -> list[str]:
    if name is not None:
        side = load_sidecar_dict(project / f"semantic/{model}.provenance.yaml")
        args = ["--question", side["verified_queries"][name]["question_normalized"]]
    else:
        args = ["--query-file", str(query_file)]
    rc, out, err = cli(project, capsys, "ask", "--model", model, *args)
    assert rc == 0, err
    return serving_block(out)


def assert_context_line(block: list[str], state: str) -> None:
    ctx = [ln.strip() for ln in block if ln.strip().startswith("Latest serving check recorded")]
    assert len(ctx) == 1 and re.fullmatch(context_re(state), ctx[0])
    low = ctx[0].lower()
    assert not any(w.lower() in low for w in sv.BANNED_WORDS)
    assert "applies" not in low and "this question" not in low


def test_bi001_successful_empty_response_does_not_persist_stderr(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    marker = "REVIEW-PRIVATE-STDERR connector secret=env-value"
    fake_rill(monkeypatch, p, outputs={
        "row_count_all": rill_route.RillResult(0, [], [], marker, ""),
    })
    rc, out, _err = run_check(p, capsys)
    assert rc == 1
    assert marker in out  # the terminal text is as before
    doc = receipts(p)[0].read_text()
    assert marker not in doc
    entry = next(e for e in json.loads(doc)["entries"] if e["name"] == "row_count_all")
    assert entry["outcome"] == "MISMATCH" and entry["diff"] == ["rill returned no rows; expected 1"]
    assert marker not in "\n".join(status_lines(p, capsys))


def test_bi002_known_answers_are_snapshotted_before_the_first_query(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    target = p / f"tests/fixtures/known-answers/{MODEL}/row_count_all.json"
    start_digest = sv.known_answer_content_sha256(json.loads(target.read_text())["expected"])
    edits = []

    queried = []

    def hook(name):  # after the first query was sent, a later query's file changes on disk
        queried.append(name)
        if not edits and name != "row_count_all":
            doc = json.loads(target.read_text())
            doc["expected"]["rows"] = [[22]]
            target.write_text(json.dumps(doc))
            edits.append(name)

    fake_rill(monkeypatch, p, hook=hook)
    rc, _out, _err = run_check(p, capsys)
    assert edits and sv.known_answer_content_sha256(
        json.loads(target.read_text())["expected"]) != start_digest
    receipt = json.loads(receipts(p)[0].read_text())
    entry = next(e for e in receipt["entries"] if e["name"] == "row_count_all")
    assert entry["known_answer_content_sha256"] == start_digest
    assert queried.index("row_count_all") > queried.index(edits[0])  # edited before its turn
    # the route returned the edited 22; the snapshot still expects 21 -> not a MATCH
    assert entry["outcome"] == "MISMATCH" and entry["diff"]
    assert receipt["state"] == "failed" and rc == 1


def test_bi003_query_hash_disagreement_refuses_before_binding_queries_and_receipt(
    tmp_path, capsys, monkeypatch
):
    p = governed_project(tmp_path)
    target = p / f"tests/fixtures/known-answers/{MODEL}/row_count_all.json"
    doc = json.loads(target.read_text())
    doc["query_hash"] = "sha256:" + "0" * 64
    target.write_text(json.dumps(doc))
    fake_rill(monkeypatch, p)
    bound = []
    monkeypatch.setattr(rill_route, "check_binding", lambda *a, **k: bound.append(1))
    queried = []
    monkeypatch.setattr(rill_route, "run_metrics_sql", lambda *a, **k: queried.append(1))
    rc, _out, err = run_check(p, capsys)
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_PROVENANCE_MISMATCH: ")
    assert "row_count_all" in err
    assert not bound and not queried and not (p / "out").exists()


def test_bi005_bi006_incomplete_model_report_names_interval_state_and_comparator(
    tmp_path, capsys, monkeypatch
):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p, outputs={
        "row_count_all": rill_route.RillResult(1, [], [], "boom", "")
    })
    assert run_check(p, capsys)[0] == 3
    block = status_lines(p, capsys)[1:]
    assert re.fullmatch(
        rf"Serving:    During {INTERVAL}, the check through Rill Metrics SQL \(CLI\) did not "
        r"complete: 1 queries could not be asked \(E_ENGINE\)\. 8 queries matched\.",
        block[0],
    )
    assert_context_line(block, "incomplete")
    assert block[-1].strip() == EXCL


def test_bi005_bi006_matched_check_prints_no_extra_context_line(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p)
    assert run_check(p, capsys)[0] == 0
    assert not any("Latest serving check" in ln for ln in status_lines(p, capsys))
    block = ask_text(p, capsys, "credits_by_warehouse")
    assert not any("Latest serving check" in ln for ln in block)  # claim has both already


def test_bi005_bi006_no_receipt_prints_no_context_line(tmp_path, capsys):
    p = governed_project(tmp_path)
    assert not any("Latest serving check" in ln for ln in status_lines(p, capsys))
    block = ask_text(p, capsys, "credits_by_warehouse")
    assert block == ["Serving:    No serving check recorded.", f"            {EXCL}"]


def test_bi006_stale_answer_shows_the_selected_receipts_interval(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p)
    assert run_check(p, capsys)[0] == 0
    edit_row_count_expr(p)
    block = ask_text(p, capsys, "row_count_all")
    assert block[0] == (
        "Serving:    This question's definition differs from its verified version; no serving "
        "check applies to the current definition."
    )
    assert_context_line(block, "matched")


def test_bi006_unverified_answer_with_a_receipt_shows_the_receipts_interval(
    tmp_path, capsys, monkeypatch
):
    from tests.s4_helpers import write_query

    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p)
    assert run_check(p, capsys)[0] == 0
    qfile = write_query(p / "out" / "unverified.json", metrics=["avg_credits_used"], order_by=[])
    block = ask_text(p, capsys, query_file=qfile)
    assert block[0] == (
        "Serving:    This question is not a verified query; serving checks do not cover it."
    )
    assert_context_line(block, "matched")


def test_bi005_mismatch_skipped_and_error_answers_name_state_and_comparator(
    tmp_path, capsys, monkeypatch
):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p, outputs={
        "row_count_all": wrong(["row_count"], [["22"]]),
        "total_credits_all": rill_route.RillResult(1, [], [], "boom", ""),
    })
    assert run_check(p, capsys)[0] == 3  # mismatch and error: failed, exit 3
    mismatch = ask_text(p, capsys, "row_count_all")
    assert "returned a result different from its known answer" in mismatch[0]
    assert_context_line(mismatch, "failed")
    error = ask_text(p, capsys, "total_credits_all")
    assert "could not be asked (E_ENGINE)" in error[0]
    assert_context_line(error, "failed")

    from agent_bi.verify import known_answers

    real = known_answers.compile_query

    def refuse(q, model, dialect, **kw):
        if dialect == "rill":
            raise validation_error("UNSUPPORTED_BY_RILL_ROUTE", "refused for the test")
        return real(q, model, dialect, **kw)

    monkeypatch.setattr(known_answers, "compile_query", refuse)
    assert run_check(p, capsys)[0] == 0
    skipped = ask_text(p, capsys, "credits_by_warehouse")
    assert "was skipped (not supported by the route)" in skipped[0]
    assert_context_line(skipped, "no-evidence")
