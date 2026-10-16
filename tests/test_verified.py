"""Sprint 2 tasks 3-4: question normalization, exact-match lookup, authored verified queries."""

from __future__ import annotations

import json

import yaml

from agent_bi.hashing import base_table_digest, definition_digests, semantic_digest
from agent_bi.query.compile import compile
from agent_bi.query.contract import parse_query, query_hash
from agent_bi.query.verified import find_by_hash, find_by_question, normalize_question
from agent_bi.semantic.validate import validate
from tests.conftest import ROOT

SEMANTIC = ROOT / "semantic"
QUESTIONS = {
    "total_credits_all": "total credits used",
    "credits_by_warehouse": "credits by warehouse",
    "credits_by_day": "credits by day",
    "row_count_all": "how many rows",
    "avg_compute_by_warehouse": "average compute credits by warehouse",
    "compute_and_cloud_by_warehouse": "compute and cloud services credits by warehouse",
    "averages_by_day": "average credits and cloud services credits by day",
    "distinct_warehouses": "how many warehouses",
}


def load(name):
    return yaml.safe_load((SEMANTIC / name).read_text())


def test_normalize_question():
    assert normalize_question("  Total   Credits\tUsed?  ") == "total credits used"
    assert normalize_question("HOW MANY ROWS?!.") == "how many rows"
    assert normalize_question("ＨＯＷ  many rows") == "how many rows"  # NFKC
    assert normalize_question("what's the total?") == "what's the total"
    assert normalize_question("a.b") == "a.b"  # only trailing punctuation is stripped
    assert normalize_question("") == ""


def test_lookup_is_exact_by_hash_and_by_question():
    side = load("idle_warehouse_waste.provenance.yaml")["verified_queries"]
    for name, text in QUESTIONS.items():
        assert find_by_question(side, text) == [name]
        assert find_by_question(side, text.upper() + "?") == [name]
        assert find_by_hash(side, side[name]["query_hash"]) == [name]
    assert find_by_question(side, "credits by warehouse please") == []  # no fuzzy matching
    assert find_by_question(side, "credit by warehouse") == []
    assert find_by_hash(side, "sha256:" + "0" * 64) == []
    dup = {"a": {"question_normalized": "x"}, "b": {"question_normalized": "x"}}
    assert find_by_question(dup, "X?") == ["a", "b"]


def test_the_eight_authored_verified_queries():
    model, side = load("idle_warehouse_waste.yaml"), load("idle_warehouse_waste.provenance.yaml")
    vqs = {v["name"]: v for v in model["verified_queries"]}
    # the eight Sprint-2 queries, then the ninth promoted in Sprint 4 (checked in test_save.py)
    assert list(vqs) == [*QUESTIONS, "warehouses_by_day"]
    assert set(side["verified_queries"]) == {*QUESTIONS, "warehouses_by_day"}
    for name, question in QUESTIONS.items():
        v, rec = vqs[name], side["verified_queries"][name]
        assert v["question"] == question
        assert v["verified_by"] == "sprint-2"
        assert type(v["verified_at"]) is int and v["verified_at"] > 1_700_000_000
        assert set(v) == {"name", "question", "verified_at", "verified_by", "sql"}
        sq = rec["semantic_query"]
        assert sq["order_by"] == [] and "limit" not in sq and sq["filters"] == []
        assert sq["named_filters"] == []
        if name.endswith("_by_day"):
            assert sq["time"] == {"dimension": "usage_day", "grain": "day"}
        else:
            assert "time" not in sq
        q = parse_query(sq)
        assert rec["query_hash"] == query_hash(q)
        assert rec["semantic_digest"] == semantic_digest(q, model)
        assert rec["definition_digests"] == definition_digests(q, model)
        assert next(iter(rec["definition_digests"])) == "base_table"
        assert rec["definition_digests"]["base_table"] == base_table_digest(model)
        assert rec["question_normalized"] == normalize_question(question)
        assert rec["known_answer"] == f"tests/fixtures/known-answers/idle_warehouse_waste/{name}.json"
        assert v["sql"] == compile(q, model, "snowflake").compiled_sql  # Snowflake dialect, no as-of
        ka = json.loads((ROOT / rec["known_answer"]).read_text())
        assert (ka["query_hash"], ka["semantic_digest"], ka["semantic_query"]) == (
            rec["query_hash"], rec["semantic_digest"], sq
        )
        assert ka["definition_digests"] == rec["definition_digests"]
    assert "AT(TIMESTAMP" not in (SEMANTIC / "idle_warehouse_waste.yaml").read_text()


def test_the_model_with_verified_queries_still_validates():
    validate("idle_warehouse_waste", SEMANTIC)


def test_product_catalog_is_untouched():
    model = load("product_catalog.yaml")
    assert "verified_queries" not in model
    assert load("product_catalog.provenance.yaml")["verified_queries"] == {}


def test_refresh_over_the_committed_model_with_verified_queries_is_a_no_op(tmp_path):
    """verified_queries[] and the sidecar records are carried over verbatim (§5.2)."""
    import shutil

    from agent_bi.config import load_config
    from agent_bi.engines import DuckDBEngine
    from agent_bi.semantic.scan import scan

    sem = tmp_path / "semantic"
    shutil.copytree(SEMANTIC, sem)
    before = {p.name: p.read_text() for p in sem.glob("idle_*")}
    engine = DuckDBEngine(load_config(ROOT / "agent-bi.yaml"))
    try:
        scan(engine, "FIXTURE.MAIN.warehouse_metering_history", "idle_warehouse_waste", sem,
             refresh=True, now="2030-01-01T00:00:00Z")
    finally:
        engine.close()
    after = {p.name: p.read_text() for p in sem.glob("idle_*")}
    assert after["idle_warehouse_waste.yaml"] == before["idle_warehouse_waste.yaml"]
    mask = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("scanned_at:"))
    assert mask(after["idle_warehouse_waste.provenance.yaml"]) == mask(
        before["idle_warehouse_waste.provenance.yaml"]
    )
    validate("idle_warehouse_waste", sem)
