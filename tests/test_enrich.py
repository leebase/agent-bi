"""Sprint 4 task 4 and Sprint 5: ``enrich --accept`` and ``enrich --apply`` (architecture §4.5).
Scratch copies only: no test and no command in this sprint accepts anything in the committed
``semantic/`` (acceptance honesty, D398)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from agent_bi.hashing import expr_sha256, semantic_digest
from agent_bi.query.contract import parse_query, query_hash
from agent_bi.semantic.model import load_model_dict
from tests.s3_helpers import MODEL, tree_hash
from tests.s4_helpers import cli, edit_yaml, make_s4_project

METRIC = "tables[0].metrics[total_credits_used]"
DIM = "tables[0].dimensions[warehouse_name]"
CLOUD_TOTAL = "tables[0].metrics[total_credits_used_cloud_services]"
PATCH_ID = "9f8c7b6a-5d4e-4f3a-8b2c-1d0e9f8a7b6c"
PROPOSED_BY = "synthetic-test-operator"
RATIO_EXPR = 'SUM("credits_used_compute") / NULLIF(SUM("credits_used"), 0)'


def sidecar(p):
    return yaml.safe_load((p / "semantic" / f"{MODEL}.provenance.yaml").read_text())


def model(p):
    return load_model_dict(str(p / "semantic" / f"{MODEL}.yaml"))


def patch_doc(operations, **overrides):
    doc = {
        "schema_version": "agent-bi-enrichment-patch/1.0",
        "patch_id": PATCH_ID,
        "model": MODEL,
        "proposed_by": PROPOSED_BY,
        "proposed_at": "2026-10-15T12:00:00Z",
        "operations": operations,
    }
    doc.update(overrides)
    return doc


def scratch_path(name):
    """A patch file outside the project tree (writing it inside would change ``tree_hash``)."""
    return Path(tempfile.mkdtemp(prefix="agent-bi-s5-patch-")) / name


def write_patch(project, operations, name="patch.json", **overrides):
    path = scratch_path(name)
    path.write_text(json.dumps(patch_doc(operations, **overrides)))
    return path


def apply(project, capsys, operations, *extra, name="patch.json", **overrides):
    path = write_patch(project, operations, name=name, **overrides)
    return cli(project, capsys, "enrich", "--model", MODEL, "--apply", str(path), *extra)


def accept_both(project, capsys, by: str = "test") -> None:
    for path in (METRIC, DIM):
        rc, _, err = cli(project, capsys, "enrich", "--model", MODEL, "--accept", path, "--by", by)
        assert rc == 0, err


def test_accept(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    before = sidecar(p)
    for path in (METRIC, DIM):
        rc, out, err = cli(p, capsys, "enrich", "--model", MODEL, "--accept", path, "--by", "test")
        assert rc == 0, err
        assert out.strip() == f"accepted {path} by test"
    after = sidecar(p)
    changed = {k for k in after["entries"] if after["entries"][k] != before["entries"][k]}
    assert changed == {METRIC, DIM}  # exactly those entries
    for k in changed:
        e = after["entries"][k]
        assert (e["status"], e["by"]) == ("confirmed", "test")
        assert e["expr_sha256"] == before["entries"][k]["expr_sha256"]  # the definition is untouched
    # nothing else in the sidecar moved
    for key in ("verified_queries", "relation", "source", "scanned_at", "ignored_columns"):
        assert after[key] == before[key]
    cli(p, capsys, "validate", "--model", MODEL)  # still valid


def test_accept_leaves_the_rest_of_the_sidecar_byte_identical(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    path = p / "semantic" / f"{MODEL}.provenance.yaml"
    before = path.read_text().splitlines()
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--accept", METRIC, "--by", "test")
    assert rc == 0, err
    after = path.read_text().splitlines()
    assert len(before) == len(after)
    diff = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert {a.split(":")[0].strip() for a, _ in diff} == {"status", "at", "by"}  # three lines only


def test_accepting_an_already_confirmed_entry_changes_nothing(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    cli(p, capsys, "enrich", "--model", MODEL, "--accept", DIM, "--by", "test")
    snap = tree_hash(p)
    rc, out, _ = cli(p, capsys, "enrich", "--model", MODEL, "--accept", DIM, "--by", "someone-else")
    assert rc == 0 and "already confirmed" in out and tree_hash(p) == snap


def test_accept_uncovered_metric_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)

    def add_metric(m):
        m["tables"][0]["metrics"].append(
            {"name": "x", "synonyms": [], "description": "scratch", "expr": 'MAX("credits_used")'}
        )

    from agent_bi.hashing import expr_sha256

    edit_yaml(p / "semantic" / f"{MODEL}.yaml", add_metric)
    edit_yaml(p / "semantic" / f"{MODEL}.provenance.yaml", lambda s: s["entries"].update({
        "tables[0].metrics[x]": {
            "status": "inferred", "origin": "human", "at": "2026-10-15T00:00:00Z", "by": "test",
            "expr_sha256": expr_sha256('MAX("credits_used")'),
        }}))
    snap = tree_hash(p)
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--accept", "tables[0].metrics[x]",
                     "--by", "test")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_ACCEPT_UNCOVERED: ")
    assert tree_hash(p) == snap


def test_a_stale_verified_query_does_not_cover_a_metric(tmp_path, capsys):
    from tests.s4_helpers import change_total_credits_used

    p = make_s4_project(tmp_path)
    change_total_credits_used(p)  # every query on total_credits_used is stale
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--accept", METRIC, "--by", "test")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_ACCEPT_UNCOVERED: ")


def test_accept_needs_a_person_and_a_known_path(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--accept", METRIC)
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_QUERY_INVALID: ")
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--accept", "tables[0].metrics[nope]",
                     "--by", "test")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_UNKNOWN_FIELD: ")


def test_apply_missing_patch_is_rejected(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--apply", str(tmp_path / "nope.json"))
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_PATCH_REJECTED: ")
    assert tree_hash(p) == snap


# ---------------------------------------------------------------------------------
# Sprint 5 task 1: enrich --apply
# ---------------------------------------------------------------------------------


def test_apply_three_operations_marks_them_inferred(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    before = sidecar(p)
    operations = [
        {"op": "set_description", "path": DIM, "value": "Virtual warehouse name"},
        {"op": "add_synonyms", "path": METRIC, "value": ["credits", "spend"]},
        {
            "op": "add_metric", "path": "tables[0]",
            "value": {"name": "compute_share", "expr": RATIO_EXPR, "description": "Compute share"},
        },
    ]
    rc, out, err = apply(p, capsys, operations)
    assert rc == 0, err
    assert out.splitlines() == [
        f"wrote semantic/{MODEL}.yaml",
        f"wrote semantic/{MODEL}.provenance.yaml",
    ]
    after = sidecar(p)
    leaf = "tables[0].metrics[compute_share]"
    for key in (DIM, METRIC, leaf):
        entry = after["entries"][key]
        assert (entry["status"], entry["origin"], entry["by"]) == (
            "inferred", f"enrich:{PATCH_ID}", PROPOSED_BY
        )
    table = model(p)["tables"][0]
    metric = next(m for m in table["metrics"] if m["name"] == "compute_share")
    assert metric["expr"] == RATIO_EXPR
    assert metric["synonyms"] == []
    assert table["dimensions"][0]["description"] == "Virtual warehouse name"
    assert after["entries"][METRIC]["expr_sha256"] == before["entries"][METRIC]["expr_sha256"]
    assert after["entries"][DIM]["expr_sha256"] == before["entries"][DIM]["expr_sha256"]
    rc, _, err = cli(p, capsys, "validate", "--model", MODEL)
    assert rc == 0, err


def test_apply_rejects_a_bad_metric_grammar_and_writes_nothing(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [
        {"op": "add_metric", "path": "tables[0]",
         "value": {"name": "bad", "expr": "SUM(a)+1"}},
    ])
    assert rc == 2
    assert err.strip().splitlines()[-1].startswith("E_PATCH_REJECTED: operation 0: ")
    assert tree_hash(p) == snap


def test_apply_rejects_a_metric_column_outside_the_table(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [
        {"op": "add_metric", "path": "tables[0]",
         "value": {"name": "bad", "expr": 'SUM("not_a_column")'}},
    ])
    assert rc == 2
    assert err.strip().splitlines()[-1].startswith("E_PATCH_REJECTED: operation 0: ")
    assert tree_hash(p) == snap


def test_apply_rejects_nonexistent_and_unbounded_paths(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    for operations, code in (
        ([{"op": "set_description", "path": "tables[0].metrics[nope]", "value": "x"}],
         "E_UNKNOWN_FIELD"),
        ([{"op": "set_description", "path": "tables[1].metrics[row_count]", "value": "x"}],
         "E_PATCH_REJECTED"),
        ([{"op": "set_description", "path": "../../etc/passwd", "value": "x"}],
         "E_PATCH_REJECTED"),
        ([{"op": "add_metric", "path": "tables[1]",
           "value": {"name": "x", "expr": 'SUM("credits_used")'}}],
         "E_PATCH_REJECTED"),
    ):
        rc, _, err = apply(p, capsys, operations)
        assert rc == 2, (operations, err)
        assert err.strip().splitlines()[-1].startswith(f"{code}: "), (operations, err)
        assert tree_hash(p) == snap


def test_confirmed_entry_is_protected_and_exact_override_applies(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    accept_both(p, capsys)
    snap = tree_hash(p)
    operations = [{"op": "set_description", "path": METRIC, "value": "changed"}]
    rc, _, err = apply(p, capsys, operations)
    assert rc == 2 and METRIC in err and err.strip().splitlines()[-1].startswith(
        "E_PATCH_TOUCHES_CONFIRMED: "
    )
    for near_miss in (
        "tables[0].metrics",
        f"{METRIC}*",
        "tables[0].metrics[total_credits_usd]",
        "tables[0].dimensions[warehouse_name]",
    ):
        rc, _, err = apply(p, capsys, operations, "--allow-confirmed", near_miss)
        assert rc == 2 and "E_PATCH_TOUCHES_CONFIRMED" in err, near_miss
        assert tree_hash(p) == snap, near_miss
    rc, _, err = apply(p, capsys, operations, "--allow-confirmed", METRIC)
    assert rc == 0, err
    entry = sidecar(p)["entries"][METRIC]
    assert entry["status"] == "inferred" and entry["by"] == PROPOSED_BY
    assert tree_hash(p) != snap


def test_rewriting_a_confirmed_metric_uses_the_metric_leaf(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    accept_both(p, capsys)
    snap = tree_hash(p)
    operations = [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "total_credits_used", "expr": 'SUM("credits_used_compute")',
                  "description": "rewritten"},
    }]
    rc, _, err = apply(p, capsys, operations, "--allow-confirmed", "tables[0]")
    assert rc == 2 and METRIC in err
    assert err.strip().splitlines()[-1].startswith("E_PATCH_TOUCHES_CONFIRMED: ")
    assert tree_hash(p) == snap
    rc, _, err = apply(p, capsys, operations, "--allow-confirmed", METRIC)
    assert rc == 0, err
    entry = sidecar(p)["entries"][METRIC]
    assert entry["status"] == "inferred" and entry["origin"] == f"enrich:{PATCH_ID}"
    metric = next(m for m in model(p)["tables"][0]["metrics"] if m["name"] == "total_credits_used")
    assert metric["expr"] == 'SUM("credits_used_compute")'


def test_patch_no_sql_keys_anywhere(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    good_query = {
        "schema_version": "semantic-query/1.0", "model": MODEL,
        "table": "warehouse_metering_history", "metrics": ["total_credits_used"],
        "dimensions": ["warehouse_name"],
    }
    variants = [
        patch_doc([{"op": "set_description", "path": DIM, "value": "x"}], sql="SELECT 1"),
        patch_doc([{"op": "set_description", "path": DIM, "value": "x",
                    "expected_sql": "SELECT 1"}]),
        patch_doc([{"op": "add_metric", "path": "tables[0]",
                    "value": {"name": "x", "expr": 'SUM("credits_used")',
                              "expected_sql": "SELECT 1"}}]),
        patch_doc([{"op": "propose_query", "path": "",
                    "value": {"name": "q", "question": "q?",
                              "semantic_query": {**good_query,
                                                 "filters": [{"dimension": "warehouse_name",
                                                              "op": "in", "values": ["A"],
                                                              "sql": "x"}]}}}]),
        patch_doc([{"op": "set_description", "path": DIM, "value": "x",
                    "notes": {"nested": {"SQL": "SELECT 1"}}}]),
    ]
    for i, doc in enumerate(variants):
        path = scratch_path(f"sql-{i}.json")
        path.write_text(json.dumps(doc))
        rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--apply", str(path))
        assert rc == 2, (i, err)
        assert err.strip().splitlines()[-1].startswith("E_PATCH_REJECTED: "), (i, err)
        assert tree_hash(p) == snap, i


def test_non_metric_name_collision_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    for name in ("warehouse_name", "credits_used", "usage_day", "time_bucket"):
        rc, _, err = apply(p, capsys, [{
            "op": "add_metric", "path": "tables[0]",
            "value": {"name": name, "expr": 'SUM("credits_used")'},
        }])
        assert rc == 2, (name, err)
        assert err.strip().splitlines()[-1].startswith("E_NAME_COLLISION: "), (name, err)
        assert name in err
        assert tree_hash(p) == snap


def test_synonym_uniqueness_is_model_wide(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [
        {"op": "add_synonyms", "path": METRIC, "value": ["spend", "spend"]},
    ])
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_SYNONYM_NOT_UNIQUE: ")
    rc, _, err = apply(p, capsys, [
        {"op": "add_synonyms", "path": METRIC, "value": ["warehouse_name"]},
    ])
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_SYNONYM_NOT_UNIQUE: ")
    assert tree_hash(p) == snap
    rc, _, err = apply(p, capsys, [
        {"op": "add_synonyms", "path": METRIC, "value": ["spend"]},
    ])
    assert rc == 0, err
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [
        {"op": "add_synonyms", "path": DIM, "value": ["spend"]},
    ])
    assert rc == 2 and METRIC in err and DIM in err
    assert err.strip().splitlines()[-1].startswith("E_SYNONYM_NOT_UNIQUE: ")
    assert tree_hash(p) == snap
    rc, _, err = apply(p, capsys, [
        {"op": "add_synonyms", "path": METRIC, "value": ["spend"]},
    ])
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_SYNONYM_NOT_UNIQUE: ")
    rc, _, err = apply(p, capsys, [
        {"op": "add_synonyms", "path": METRIC, "value": []},
    ])
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_PATCH_REJECTED: ")


def test_same_name_rewrite_preserves_unrelated_fields(tmp_path, capsys):
    p = make_s4_project(tmp_path)

    def add_custom(m):
        for metric in m["tables"][0]["metrics"]:
            if metric["name"] == "total_credits_used":
                metric["synonyms"] = ["spend"]
                metric["custom_note"] = "keep me"

    edit_yaml(p / "semantic" / f"{MODEL}.yaml", add_custom)
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "total_credits_used", "expr": 'SUM("credits_used_compute")',
                  "description": "rewritten"},
    }])
    assert rc == 0, err
    metric = next(m for m in model(p)["tables"][0]["metrics"] if m["name"] == "total_credits_used")
    assert metric["synonyms"] == ["spend"]
    assert metric["custom_note"] == "keep me"
    assert metric["description"] == "rewritten"
    assert metric["expr"] == 'SUM("credits_used_compute")'
    entry = sidecar(p)["entries"][METRIC]
    assert entry["status"] == "inferred"
    assert entry["expr_sha256"] == expr_sha256('SUM("credits_used_compute")')
    # an omitted description is preserved; only the expression changes
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "total_credits_used", "expr": 'SUM("credits_used")'},
    }])
    assert rc == 0, err
    metric = next(m for m in model(p)["tables"][0]["metrics"] if m["name"] == "total_credits_used")
    assert metric["description"] == "rewritten"


def test_valid_earlier_ops_plus_a_later_refusal_change_nothing(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    before = {path.name: path.read_bytes() for path in (p / "semantic").glob("*")}
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [
        {"op": "set_description", "path": DIM, "value": "changed"},
        {"op": "add_metric", "path": "tables[0]",
         "value": {"name": "bad", "expr": "SUM(a)+1"}},
    ])
    assert rc == 2 and "operation 1" in err
    assert tree_hash(p) == snap
    assert {path.name: path.read_bytes() for path in (p / "semantic").glob("*")} == before
    rc, _, err = apply(p, capsys, [
        {"op": "add_metric", "path": "tables[0]",
         "value": {"name": "ok_metric", "expr": 'SUM("credits_used")'}},
        {"op": "set_description", "path": "tables[0].metrics[nope]", "value": "x"},
    ])
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_UNKNOWN_FIELD: ")
    assert tree_hash(p) == snap
    rc, _, err = apply(p, capsys, [
        {"op": "propose_query", "path": "", "value": {
            "name": "proposed_q", "question": "q?",
            "semantic_query": {"schema_version": "semantic-query/1.0", "model": MODEL,
                               "table": "warehouse_metering_history",
                               "metrics": ["total_credits_used"]}}},
        {"op": "add_metric", "path": "tables[0]",
         "value": {"name": "bad", "expr": "SUM(a)+1"}},
    ])
    assert rc == 2 and not (p / "queries/proposed_q.json").exists()
    assert tree_hash(p) == snap


def test_apply_stage_failure_changes_nothing(tmp_path, capsys, monkeypatch):
    from agent_bi import fs_txn

    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    real_stage = fs_txn._stage
    calls = []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real_stage(path, data)

    monkeypatch.setattr(fs_txn, "_stage", flaky)
    path = write_patch(p, [
        {"op": "set_description", "path": DIM, "value": "changed"},
        {"op": "add_metric", "path": "tables[0]",
         "value": {"name": "ok_metric", "expr": 'SUM("credits_used")'}},
    ])
    with pytest.raises(OSError):
        cli(p, capsys, "enrich", "--model", MODEL, "--apply", str(path))
    assert tree_hash(p) == snap
    assert not (p / fs_txn.TMP_DIRNAME).exists()


# ---------------------------------------------------------------------------------
# Sprint 5 task 2: propose_query
# ---------------------------------------------------------------------------------

SEMANTIC_QUERY = {
    "schema_version": "semantic-query/1.0",
    "model": MODEL,
    "table": "warehouse_metering_history",
    "metrics": ["total_credits_used"],
    "dimensions": ["warehouse_name"],
}


def test_propose_query_writes_the_saved_shape_and_nothing_else(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    before_sidecar = (p / "semantic" / f"{MODEL}.provenance.yaml").read_bytes()
    before_model = (p / "semantic" / f"{MODEL}.yaml").read_bytes()
    before_ka = tree_hash(p / "tests/fixtures/known-answers")
    operations = [{"op": "propose_query", "path": "", "value": {
        "name": "proposed_credits", "question": "Credits by warehouse (proposed)?",
        "semantic_query": SEMANTIC_QUERY}}]
    rc, out, err = apply(p, capsys, operations)
    assert rc == 0, err
    assert out.splitlines() == ["wrote queries/proposed_credits.json"]
    doc = json.loads((p / "queries/proposed_credits.json").read_text())
    q = parse_query(doc["semantic_query"])
    assert doc["schema_version"] == "agent-bi-saved-query/1.0"
    assert doc["name"] == "proposed_credits" and doc["status"] == "proposed"
    assert doc["question"] == "Credits by warehouse (proposed)?"
    assert doc["question_normalized"] == "credits by warehouse (proposed)"
    assert doc["query_hash"] == query_hash(q)
    assert doc["semantic_digest"] == semantic_digest(q, model(p))
    assert (p / "semantic" / f"{MODEL}.provenance.yaml").read_bytes() == before_sidecar
    assert (p / "semantic" / f"{MODEL}.yaml").read_bytes() == before_model
    assert tree_hash(p / "tests/fixtures/known-answers") == before_ka
    assert not (p / "rill/components/proposed_credits.yaml").exists()


def test_propose_query_refuses_existing_names(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    operations = [{"op": "propose_query", "path": "", "value": {
        "name": "credits_by_warehouse", "question": "credits by warehouse",
        "semantic_query": SEMANTIC_QUERY}}]  # a verified query name
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, operations)
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_NAME_EXISTS: ")
    assert tree_hash(p) == snap
    rc, _, err = apply(p, capsys, [{"op": "propose_query", "path": "", "value": {
        "name": "dup", "question": "q?", "semantic_query": SEMANTIC_QUERY}},
        {"op": "propose_query", "path": "", "value": {
            "name": "dup", "question": "q?", "semantic_query": SEMANTIC_QUERY}}])
    assert rc == 2 and "appears twice" in err
    assert tree_hash(p) == snap
    # a second apply against the just-written proposed file is refused
    rc, _, err = apply(p, capsys, [{"op": "propose_query", "path": "", "value": {
        "name": "dup", "question": "q?", "semantic_query": SEMANTIC_QUERY}}])
    assert rc == 0, err
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [{"op": "propose_query", "path": "", "value": {
        "name": "dup", "question": "q?", "semantic_query": SEMANTIC_QUERY}}])
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_NAME_EXISTS: ")
    assert tree_hash(p) == snap


def test_propose_query_sees_the_post_patch_model(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    operations = [
        {"op": "add_metric", "path": "tables[0]",
         "value": {"name": "compute_share", "expr": RATIO_EXPR, "description": "share"}},
        {"op": "propose_query", "path": "", "value": {
            "name": "share_by_warehouse", "question": "compute share by warehouse",
            "semantic_query": {**SEMANTIC_QUERY, "metrics": ["compute_share"]}}},
    ]
    rc, _, err = apply(p, capsys, operations)
    assert rc == 0, err
    doc = json.loads((p / "queries/share_by_warehouse.json").read_text())
    q = parse_query(doc["semantic_query"])
    assert doc["semantic_digest"] == semantic_digest(q, model(p))
    table = model(p)["tables"][0]
    assert next(m for m in table["metrics"] if m["name"] == "compute_share")["expr"] == RATIO_EXPR


def test_propose_query_upgrades_to_verified_through_human_save(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    operations = [{"op": "propose_query", "path": "", "value": {
        "name": "proposed_credits", "question": "credits by warehouse proposed",
        "semantic_query": SEMANTIC_QUERY}}]
    rc, _, err = apply(p, capsys, operations)
    assert rc == 0, err
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file",
                       str(p / "queries/proposed_credits.json"))
    assert rc == 0, err
    env_rel = next(ln.split()[1] for ln in out.splitlines() if ln.endswith(".answer.json"))
    expected_sql = (
        "SELECT warehouse_name, SUM(credits_used) AS total_credits_used "
        "FROM warehouse_metering_history GROUP BY warehouse_name"
    )
    rc, _, err = cli(p, capsys, "save", "--from", str(p / env_rel), "--name", "proposed_credits",
                     "--verify", "--by", PROPOSED_BY, "--expected-sql", expected_sql)
    assert rc == 0, err
    doc = json.loads((p / "queries/proposed_credits.json").read_text())
    assert doc["status"] == "verified"
    assert "proposed_credits" in sidecar(p)["verified_queries"]
    rc, out, err = cli(p, capsys, "verify", "--model", MODEL)
    assert rc == 0, err
    assert "PASS proposed_credits" in out


# ---------------------------------------------------------------------------------
# Sprint 5 task 3 (pins) and the R1 metadata/staleness boundary
# ---------------------------------------------------------------------------------


def test_metadata_only_changes_do_not_stale_verification(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = cli(p, capsys, "verify", "--model", MODEL)
    assert rc == 0, err
    first = {name: rec["semantic_digest"] for name, rec in sidecar(p)["verified_queries"].items()}
    rc, _, err = apply(p, capsys, [
        {"op": "set_description", "path": METRIC, "value": "Sum of measured credits"},
        {"op": "add_synonyms", "path": METRIC, "value": ["spend"]},
    ])
    assert rc == 0, err
    rc, out, err = cli(p, capsys, "verify", "--model", MODEL)
    assert rc == 0, err
    assert "STALE" not in out
    second = {name: rec["semantic_digest"] for name, rec in sidecar(p)["verified_queries"].items()}
    assert first == second  # the stored digest was not rewritten and is still current
    # the same-name replacement path with an unchanged expression is metadata-only too
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "total_credits_used", "expr": 'SUM("credits_used")',
                  "description": "Sum of measured credits, reworded"},
    }])
    assert rc == 0, err
    rc, out, err = cli(p, capsys, "verify", "--model", MODEL)
    assert rc == 0, err
    assert "STALE" not in out
    metric = next(m for m in model(p)["tables"][0]["metrics"] if m["name"] == "total_credits_used")
    assert metric["description"] == "Sum of measured credits, reworded"
    assert metric["synonyms"] == ["spend"]  # the replacement preserved them


def test_expr_rewrite_makes_the_query_stale_and_ask_exploratory(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "total_credits_used", "expr": 'SUM("credits_used_compute")'},
    }])
    assert rc == 0, err
    rc, out, err = cli(p, capsys, "verify", "--model", MODEL)
    assert rc == 1
    assert "STALE credits_by_warehouse: total_credits_used" in out
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file",
                       str(p / "tests/fixtures/known-answers" / MODEL / "credits_by_warehouse.json"))
    assert rc == 0, err
    assert "trust: exploratory (verification of credits_by_warehouse is stale" in out


def test_stale_cycle_closes_through_independent_reverification(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--accept", CLOUD_TOTAL,
                     "--by", PROPOSED_BY)
    assert rc == 0, err
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "total_credits_used_cloud_services",
                  "expr": 'SUM("credits_used_compute")', "description": "rewritten"},
    }])
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_PATCH_TOUCHES_CONFIRMED: ")
    assert tree_hash(p) == snap
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "total_credits_used_cloud_services",
                  "expr": 'SUM("credits_used_compute")', "description": "rewritten"},
    }], "--allow-confirmed", CLOUD_TOTAL)
    assert rc == 0, err
    rc, out, err = cli(p, capsys, "verify", "--model", MODEL)
    assert rc == 1, out
    assert "STALE compute_and_cloud_by_warehouse" in out
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file",
                       str(p / "tests/fixtures/known-answers" / MODEL
                           / "compute_and_cloud_by_warehouse.json"))
    assert rc == 0, err
    assert "trust: exploratory (verification of compute_and_cloud_by_warehouse is stale" in out
    env_rel = next(ln.split()[1] for ln in out.splitlines() if ln.endswith(".answer.json"))
    # the old oracle no longer matches: refusing to verify with it writes nothing
    wrong_sql = (
        "SELECT warehouse_name, SUM(credits_used_cloud_services) "
        "FROM warehouse_metering_history GROUP BY warehouse_name"
    )
    snap = tree_hash(p)
    rc, _, err = cli(p, capsys, "save", "--from", str(p / env_rel), "--name",
                     "compute_and_cloud_by_warehouse", "--question",
                     "compute and cloud services credits by warehouse", "--verify", "--force",
                     "--by", PROPOSED_BY, "--expected-sql", wrong_sql)
    assert rc == 2 and "E_VERIFY_MISMATCH" in err
    assert tree_hash(p) == snap
    correct_sql = (
        "SELECT warehouse_name, SUM(credits_used_compute) AS total_credits_used_cloud_services, "
        "SUM(credits_used_compute) AS total_credits_used_compute "
        "FROM warehouse_metering_history GROUP BY warehouse_name"
    )
    rc, _, err = cli(p, capsys, "save", "--from", str(p / env_rel), "--name",
                     "compute_and_cloud_by_warehouse", "--question",
                     "compute and cloud services credits by warehouse", "--verify", "--force",
                     "--by", PROPOSED_BY, "--expected-sql", correct_sql)
    assert rc == 0, err
    rc, out, err = cli(p, capsys, "verify", "--model", MODEL)
    assert rc == 0, err
    assert "PASS compute_and_cloud_by_warehouse" in out


# ---------------------------------------------------------------------------------
# Sprint 5 task 5: patch-envelope refusals
# ---------------------------------------------------------------------------------


def test_patch_envelope_refusals(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    good = [{"op": "set_description", "path": DIM, "value": "x"}]
    variants = {
        "wrong model": patch_doc(good, model="other_model"),
        "bad uuid": patch_doc(good, patch_id="not-a-uuid"),
        "uuid3": patch_doc(good, patch_id="9f8c7b6a-5d4e-3f3a-8b2c-1d0e9f8a7b6c"),
        "bad proposed_at": patch_doc(good, proposed_at="yesterday"),
        "empty operations": patch_doc([]),
        "unknown op": patch_doc([{"op": "delete_everything", "path": DIM, "value": "x"}]),
        "extra top-level key": patch_doc(good, extra=1),
        "empty proposed_by": patch_doc(good, proposed_by=""),
        "non-string value": patch_doc([{"op": "set_description", "path": DIM, "value": 7}]),
        "extra op key": patch_doc([{"op": "set_description", "path": DIM, "value": "x",
                                     "sneaky": 1}]),
        "add_metric extra key": patch_doc([{"op": "add_metric", "path": "tables[0]",
                                             "value": {"name": "x", "expr": 'SUM("credits_used")',
                                                       "synonyms": []}}]),
        "propose_query bad path": patch_doc([{"op": "propose_query", "path": "tables[0]",
                                               "value": {"name": "q", "question": "q?",
                                                         "semantic_query": SEMANTIC_QUERY}}]),
        "bad metric name": patch_doc([{"op": "add_metric", "path": "tables[0]",
                                        "value": {"name": "Bad Name", "expr": 'SUM("credits_used")'}}]),
        "bad query name": patch_doc([{"op": "propose_query", "path": "",
                                       "value": {"name": "Bad Name", "question": "q?",
                                                 "semantic_query": SEMANTIC_QUERY}}]),
        "unknown query field": patch_doc([{"op": "propose_query", "path": "",
                                            "value": {"name": "q", "question": "q?",
                                                      "semantic_query": {**SEMANTIC_QUERY,
                                                                         "metrics": ["nope"]}}}]),
    }
    for i, (label, doc) in enumerate(variants.items()):
        path = scratch_path(f"variant-{i}.json")
        path.write_text(json.dumps(doc))
        rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--apply", str(path))
        assert rc == 2, (label, err)
        last = err.strip().splitlines()[-1]
        assert last.startswith(("E_PATCH_REJECTED: ", "E_QUERY_INVALID: ",
                                "E_UNKNOWN_FIELD: ")), (label, err)
        assert tree_hash(p) == snap, label


def test_oversized_patch_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    doc = patch_doc([{"op": "set_description", "path": DIM, "value": "x" * 1_100_000}])
    path = scratch_path("huge.json")
    path.write_text(json.dumps(doc))
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--apply", str(path))
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_PATCH_REJECTED: ")
    assert "cap" in err
    assert tree_hash(p) == snap


# ---------------------------------------------------------------------------------
# Sprint 5 defect repair: a new metric name must not shadow a synonym (§4.5)
# ---------------------------------------------------------------------------------

SYNONYM = "review_credit_alias"


def dir_bytes(project: Path, *rels: str) -> dict[str, bytes]:
    """The bytes of every file under ``rels``, keyed by project-relative path (a missing
    root contributes nothing)."""
    snapshot: dict[str, bytes] = {}
    for rel in rels:
        root = project / rel
        if root.exists():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    snapshot[path.relative_to(project).as_posix()] = path.read_bytes()
    return snapshot


def test_add_metric_canonical_name_collision_still_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "warehouse_name", "expr": 'SUM("credits_used")'},
    }])
    assert rc == 2
    assert err.strip().splitlines()[-1].startswith("E_NAME_COLLISION: ")
    assert tree_hash(p) == snap


def test_add_metric_name_equal_to_existing_dimension_synonym_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = apply(p, capsys, [
        {"op": "add_synonyms", "path": DIM, "value": [SYNONYM]},
    ])
    assert rc == 0, err
    snap = tree_hash(p)
    before = dir_bytes(p, "semantic", "queries")
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": SYNONYM, "expr": "COUNT(*)"},
    }])
    assert rc == 2
    assert err.strip().splitlines()[-1].startswith("E_SYNONYM_NOT_UNIQUE: ")
    assert DIM in err
    assert f"tables[0].metrics[{SYNONYM}]" in err
    assert tree_hash(p) == snap
    assert dir_bytes(p, "semantic", "queries") == before


def test_add_metric_name_equal_to_existing_metric_synonym_is_refused(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = apply(p, capsys, [
        {"op": "add_synonyms", "path": METRIC, "value": ["spend"]},
    ])
    assert rc == 0, err
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "spend", "expr": "COUNT(*)"},
    }])
    assert rc == 2
    assert err.strip().splitlines()[-1].startswith("E_SYNONYM_NOT_UNIQUE: ")
    assert METRIC in err
    assert "tables[0].metrics[spend]" in err
    assert tree_hash(p) == snap


def test_same_patch_synonym_name_conflict_is_order_independent(tmp_path, capsys):
    codes: list[str] = []
    for label, operations in (
        ("synonyms_first", [
            {"op": "add_synonyms", "path": DIM, "value": [SYNONYM]},
            {"op": "add_metric", "path": "tables[0]",
             "value": {"name": SYNONYM, "expr": "COUNT(*)"}},
        ]),
        ("metric_first", [
            {"op": "add_metric", "path": "tables[0]",
             "value": {"name": SYNONYM, "expr": "COUNT(*)"}},
            {"op": "add_synonyms", "path": DIM, "value": [SYNONYM]},
        ]),
    ):
        root = tmp_path / label
        root.mkdir()
        p = make_s4_project(root)
        snap = tree_hash(p)
        rc, _, err = apply(p, capsys, operations)
        assert rc == 2, (label, err)
        last = err.strip().splitlines()[-1]
        assert last.startswith("E_SYNONYM_NOT_UNIQUE: "), (label, err)
        assert "operation 1" in err, (label, err)
        assert tree_hash(p) == snap, label
        codes.append(last.split(":", 1)[0])
    assert codes[0] == codes[1]


def test_valid_earlier_ops_plus_synonym_collision_write_nothing(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    rc, _, err = apply(p, capsys, [
        {"op": "set_description", "path": DIM, "value": "changed"},
        {"op": "add_synonyms", "path": METRIC, "value": ["spend"]},
        {"op": "propose_query", "path": "", "value": {
            "name": "proposed_q", "question": "q?",
            "semantic_query": SEMANTIC_QUERY}},
        {"op": "add_metric", "path": "tables[0]",
         "value": {"name": "spend", "expr": "COUNT(*)"}},
    ])
    assert rc == 2, err
    assert err.strip().splitlines()[-1].startswith("E_SYNONYM_NOT_UNIQUE: ")
    assert "operation 3" in err
    assert not (p / "queries" / "proposed_q.json").exists()
    assert tree_hash(p) == snap


def test_non_colliding_add_metric_succeeds_while_synonyms_exist(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = apply(p, capsys, [
        {"op": "add_synonyms", "path": DIM, "value": [SYNONYM]},
    ])
    assert rc == 0, err
    rc, _, err = apply(p, capsys, [{
        "op": "add_metric", "path": "tables[0]",
        "value": {"name": "review_credit_total", "expr": 'SUM("credits_used")'},
    }])
    assert rc == 0, err
    metric = next(
        m for m in model(p)["tables"][0]["metrics"] if m["name"] == "review_credit_total"
    )
    assert metric["expr"] == 'SUM("credits_used")'
    entry = sidecar(p)["entries"]["tables[0].metrics[review_credit_total]"]
    assert entry["status"] == "inferred"
