"""Sprint 4 task 1 + task 5: ``ask`` (architecture §5.8, §4.4, §4.7)."""

from __future__ import annotations

import json
import re

from agent_bi.query.contract import query_hash
from tests.s3_helpers import MODEL, tree_hash
from tests.s4_helpers import (
    CBW,
    accept_both,
    change_total_credits_used,
    cli,
    envelope_of,
    make_s4_project,
    write_query,
)

ENVELOPE_KEYS = ["schema_version", "semantic_query", "provenance", "chart", "columns", "rows"]
ARTIFACT_KEYS = [
    "query_hash", "semantic_digest", "model_file_sha256", "sidecar_sha256", "models_digest",
    "definitions", "compiled_sql", "compiled_sql_sha256", "dialect",
]
RECEIPT_KEYS = [
    "trust", "trust_reason", "evidence", "published", "verified_against", "engine",
    "engine_version", "engine_settings", "as_of", "executed_sql", "executed_sql_sha256",
    "executed_at", "row_count",
]
TOMBSTONE = "REMOVED in agent-bi-provenance-record/2.0: read receipt.evidence (definition, serving)"
TOMBSTONE_REASON = "REMOVED in agent-bi-provenance-record/2.0: read receipt.evidence"


def test_question_is_a_deterministic_exact_match_in_any_case_and_punctuation(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    for text in ("credits by warehouse", "Credits By   Warehouse?", "  CREDITS BY WAREHOUSE!! "):
        rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--question", text)
        assert rc == 0, err
        assert (
            "Definition: An independent computation agreed with this query's definitions when "
            "verified as credits_by_warehouse at " in out
        )
        assert "Serving:    No serving check recorded." in out
        env = envelope_of(p, out)
        rec = env["provenance"]["receipt"]
        assert rec["trust"] == TOMBSTONE and rec["trust_reason"] == TOMBSTONE_REASON
        assert rec["evidence"]["definition"]["state"] == "verified"
        assert rec["verified_against"] == "credits_by_warehouse"
    assert not (p / "out" / "question.json").exists()  # the question path composes nothing


def test_unmatched_question_exits_4_and_writes_nothing(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    before = tree_hash(p)
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--question", "something nobody asked")
    assert rc == 4 and out == ""
    assert err.strip().splitlines()[-1].startswith("E_NO_VERIFIED_MATCH: ")
    assert tree_hash(p) == before and not (p / "out").exists()


def test_a_saved_query_file_is_also_matched_by_question(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    q = write_query(p / "queries" / "x.json", metrics=["avg_credits_used"])
    doc = {"name": "avg_by_wh", "status": "proposed", "question_normalized": "average by warehouse",
           "semantic_query": json.loads(q.read_text())}
    q.write_text(json.dumps(doc))
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--question", "Average by warehouse?")
    assert rc == 0, err
    assert "Definition: No verified query matches this question." in out  # proposed != verified


def test_an_ambiguous_question_exits_2_listing_the_names(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    a = write_query(p / "queries" / "a.json", metrics=["avg_credits_used"])
    doc = {"name": "dup", "question_normalized": "credits by warehouse",
           "semantic_query": json.loads(a.read_text())}
    a.write_text(json.dumps(doc))
    rc, _, err = cli(p, capsys, "ask", "--model", MODEL, "--question", "credits by warehouse")
    assert rc == 2
    last = err.strip().splitlines()[-1]
    assert last.startswith("E_AMBIGUOUS_QUESTION: ") and "credits_by_warehouse" in last and "dup" in last


def test_the_verified_query_file_is_verified_with_the_unconfirmed_definitions(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(CBW))
    assert rc == 0, err
    env = envelope_of(p, out)
    assert list(env) == ENVELOPE_KEYS and env["schema_version"] == "agent-bi-answer/2.0"
    prov = env["provenance"]
    assert prov["schema_version"] == "agent-bi-provenance-record/2.0"
    assert list(prov["artifact"]) == ARTIFACT_KEYS and list(prov["receipt"]) == RECEIPT_KEYS
    rec, art = prov["receipt"], prov["artifact"]
    assert rec["trust"] == TOMBSTONE and rec["trust_reason"] == TOMBSTONE_REASON
    assert rec["evidence"]["definition"] == {
        "state": "verified", "claim": rec["evidence"]["definition"]["claim"],
        "verified_as": "credits_by_warehouse",
        "verified_at": rec["evidence"]["definition"]["verified_at"],
        "changed": [],
    }
    assert "total_credits_used, warehouse_name have not been accepted by a person" in (
        rec["evidence"]["definition"]["claim"]
    )
    assert rec["evidence"]["serving"] == {
        "state": None, "claim": "No serving check recorded.", "receipt_id": None,
        "interval": None, "applicability": None, "query_outcome": None,
    }
    assert rec["published"] is False
    assert rec["verified_against"] == "credits_by_warehouse"
    assert rec["engine"] == "duckdb" and rec["engine_settings"]["TimeZone"] == "UTC"
    assert rec["as_of"]["kind"] == "file-digest" and rec["as_of"]["value"].startswith("sha256:")
    assert rec["row_count"] == 3 and rec["executed_sql"] == art["compiled_sql"]
    assert set(art["definitions"]) == {"total_credits_used", "warehouse_name"}
    assert art["definitions"]["total_credits_used"]["status"] == "catalog"
    assert art["query_hash"] == query_hash(env["semantic_query"])
    known = json.loads(CBW.read_text())["expected"]
    assert env["columns"] == known["columns"] and env["rows"] == known["rows"]
    # stdout: interpretation, Definition, Serving, exclusions, published, definitions, SQL, table, paths
    assert "Definition: An independent computation agreed" in out
    assert "Serving:    No serving check recorded." in out
    assert "Not covered: other read paths and caches, other users, rendering" in out
    assert "published: false" in out
    assert "definitions: total_credits_used=catalog, warehouse_name=catalog" in out
    assert re.search(r"```sql\nSELECT.*\n```", out, re.DOTALL)
    assert out.count("wrote out/") == 2
    assert env["chart"]["mark"] == "bar"


def test_a_novel_query_is_exploratory(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    q = write_query(p / "out" / "q.json", metrics=["avg_credits_used"])
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(q))
    assert rc == 0, err
    assert "Definition: No verified query matches this question." in out
    env = envelope_of(p, out)
    assert env["provenance"]["receipt"]["verified_against"] is None
    assert env["provenance"]["receipt"]["evidence"]["definition"]["state"] == "none"


def test_an_unknown_field_is_e_unknown_field_with_the_valid_names(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    q = write_query(p / "out" / "q.json", metrics=["total_credit_used"])
    before = tree_hash(p)
    rc, _, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(q))
    assert rc == 2
    last = err.strip().splitlines()[-1]
    assert last.startswith("E_UNKNOWN_FIELD: ") and "total_credits_used" in last
    assert tree_hash(p) == before  # nothing written


def test_stale_label(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    change_total_credits_used(p)
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(CBW))
    assert rc == 0, err
    rec = envelope_of(p, out)["provenance"]["receipt"]
    assert rec["trust"] == TOMBSTONE
    definition = rec["evidence"]["definition"]
    assert definition["state"] == "stale"
    assert definition["claim"].startswith("This query was verified as credits_by_warehouse at")
    assert "now differ from the verified version (total_credits_used)" in definition["claim"]
    assert definition["changed"] == ["total_credits_used"]
    assert rec["verified_against"] is None  # unchanged: only governed/verified name a verification


def test_governed_label(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    accept_both(p, capsys)
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(CBW))
    assert rc == 0, err
    assert "Definition: A person accepted this query's definitions" in out
    assert "when verified as credits_by_warehouse at" in out
    rec = envelope_of(p, out)["provenance"]["receipt"]
    assert rec["evidence"]["definition"]["state"] == "governed"
    assert rec["evidence"]["definition"]["verified_as"] == "credits_by_warehouse"
    assert "definitions: total_credits_used=confirmed, warehouse_name=confirmed" in out
    # and the same through the question path, no LLM involved
    rc, out2, err = cli(p, capsys, "ask", "--model", MODEL, "--question", "credits by warehouse")
    assert rc == 0, err
    assert "Definition: A person accepted this query's definitions" in out2
    assert not (p / "out" / "question.json").exists()


def test_governed_does_not_depend_on_published(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    accept_both(p, capsys)
    cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(CBW))
    rc, out, err = cli(p, capsys, "canvas", "fork", "--model", MODEL, "--name", "main")
    assert rc == 0, err
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(CBW))
    before = envelope_of(p, out)["provenance"]["receipt"]
    assert before["evidence"]["definition"]["state"] == "governed"
    assert before["published"] is False
    path = p / next(ln.split()[1] for ln in out.splitlines() if ln.endswith(".answer.json"))
    rc, _, err = cli(p, capsys, "save", "--from", str(path), "--name", "credits_by_warehouse",
                     "--question", "credits by warehouse", "--force", "--add-to-canvas", "main")
    assert rc == 0, err
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(CBW))
    after = envelope_of(p, out)["provenance"]["receipt"]
    assert after["published"] is True and after["evidence"]["definition"]["state"] == "governed"


def test_artifact_part_and_chart_are_byte_identical_across_runs(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    runs = []
    for _ in range(2):
        rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(CBW))
        assert rc == 0, err
        env = envelope_of(p, out)
        vl = next(ln.split()[1] for ln in out.splitlines() if ln.endswith(".vl.json"))
        runs.append((json.dumps(env["provenance"]["artifact"]), (p / vl).read_bytes(),
                     json.dumps(env["semantic_query"]), json.dumps(env["chart"])))
    assert runs[0] == runs[1]


def test_json_mode_prints_the_envelope(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(CBW), "--json")
    assert rc == 0, err
    doc = json.loads(out)
    assert list(doc) == ENVELOPE_KEYS
    assert doc["provenance"]["receipt"]["trust"] == TOMBSTONE
    assert doc["provenance"]["receipt"]["evidence"]["definition"]["state"] == "verified"
    stem = doc["provenance"]["artifact"]["query_hash"].removeprefix("sha256:")
    assert json.loads((p / "out" / f"{stem}.answer.json").read_text()) == doc


def test_snowflake_engine_is_unavailable_exit_3(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(CBW),
                     "--engine", "snowflake")
    assert rc == 3 and err.strip().splitlines()[-1].startswith("E_ENGINE_UNAVAILABLE: ")
