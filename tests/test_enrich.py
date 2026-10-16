"""Sprint 4 task 4: ``enrich --accept`` (architecture §4.5). Scratch copies only: no test and no
command in this sprint accepts anything in the committed ``semantic/`` (acceptance honesty, D398)."""

from __future__ import annotations

import yaml

from tests.s3_helpers import MODEL, tree_hash
from tests.s4_helpers import cli, edit_yaml, make_s4_project

METRIC = "tables[0].metrics[total_credits_used]"
DIM = "tables[0].dimensions[warehouse_name]"


def sidecar(p):
    return yaml.safe_load((p / "semantic" / f"{MODEL}.provenance.yaml").read_text())


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


def test_apply_is_sprint_5(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--apply", "patch.json")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_NOT_IMPLEMENTED: ")
