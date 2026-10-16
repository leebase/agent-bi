"""Sprint 2 task 3: author the eight known-answer SemanticQueries.

Writes, for ``semantic/idle_warehouse_waste``: ``verified_queries[]`` in the YAML (``sql`` =
compiled Snowflake-dialect SQL without as-of), the sidecar's ``verified_queries`` records,
and the ``query_hash`` / ``semantic_digest`` / ``semantic_query`` fields of each known-answer
file. The ``expected`` blocks are never touched. Run from the repo root.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bi.hashing import definition_digests, semantic_digest
from agent_bi.query.compile import compile as compile_query
from agent_bi.query.contract import parse_query, query_hash
from agent_bi.query.verified import normalize_question
from agent_bi.semantic.model import dump_yaml, load_model_dict
from agent_bi.semantic.provenance import load_sidecar_dict

MODEL = "idle_warehouse_waste"
TABLE = "warehouse_metering_history"
BY_DAY = {"dimension": "usage_day", "grain": "day"}

# name -> (question, metrics, dimensions, time). Question texts are fixed by the sprint plan.
SPECS = {
    "total_credits_all": ("total credits used", ["total_credits_used"], [], None),
    "credits_by_warehouse": ("credits by warehouse", ["total_credits_used"], ["warehouse_name"], None),
    "credits_by_day": ("credits by day", ["total_credits_used"], [], BY_DAY),
    "row_count_all": ("how many rows", ["row_count"], [], None),
    "avg_compute_by_warehouse": (
        "average compute credits by warehouse", ["avg_credits_used_compute"], ["warehouse_name"], None,
    ),
    "compute_and_cloud_by_warehouse": (
        "compute and cloud services credits by warehouse",
        ["total_credits_used_compute", "total_credits_used_cloud_services"], ["warehouse_name"], None,
    ),
    "averages_by_day": (
        "average credits and cloud services credits by day",
        ["avg_credits_used", "avg_credits_used_cloud_services"], [], BY_DAY,
    ),
    "distinct_warehouses": ("how many warehouses", ["distinct_warehouse_name"], [], None),
}


def semantic_query(metrics, dimensions, time_spec):
    q = {
        "schema_version": "semantic-query/1.0",
        "model": MODEL,
        "table": TABLE,
        "metrics": metrics,
        "dimensions": dimensions,
    }
    if time_spec:
        q["time"] = time_spec
    q.update({"filters": [], "named_filters": [], "order_by": []})
    return q


def _with_digests(ka, digests):
    """Insert ``definition_digests`` right after ``semantic_digest`` (§5.6 key order)."""
    out = {}
    for k, v in ka.items():
        if k == "definition_digests":
            continue
        out[k] = v
        if k == "semantic_digest":
            out["definition_digests"] = digests
    return out


def main() -> int:
    model_path = ROOT / "semantic" / f"{MODEL}.yaml"
    sidecar_path = ROOT / "semantic" / f"{MODEL}.provenance.yaml"
    model = load_model_dict(str(model_path))
    sidecar = load_sidecar_dict(sidecar_path)
    verified_at = int(time.time())
    vq_yaml, vq_sidecar = [], {}
    for name, (question, metrics, dims, time_spec) in SPECS.items():
        sq = semantic_query(metrics, dims, time_spec)
        q = parse_query(sq)
        qhash, digest = query_hash(q), semantic_digest(q, model)
        sql = compile_query(q, model, "snowflake").compiled_sql  # no as-of clause
        vq_yaml.append(
            {"name": name, "question": question, "verified_at": verified_at,
             "verified_by": "sprint-2", "sql": sql}
        )
        ka_rel = f"tests/fixtures/known-answers/{MODEL}/{name}.json"
        vq_sidecar[name] = {
            "query_hash": qhash,
            "semantic_digest": digest,
            "definition_digests": definition_digests(q, model),
            "semantic_query": sq,
            "question_normalized": normalize_question(question),
            "known_answer": ka_rel,
        }
        ka_path = ROOT / ka_rel
        ka = json.loads(ka_path.read_text(encoding="utf-8"))
        assert ka["name"] == name
        ka["query_hash"], ka["semantic_digest"], ka["semantic_query"] = qhash, digest, sq
        ka = _with_digests(ka, definition_digests(q, model))
        ka_path.write_text(json.dumps(ka, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    model["verified_queries"] = vq_yaml
    sidecar["verified_queries"] = vq_sidecar
    model_path.write_text(dump_yaml(model), encoding="utf-8")
    sidecar_path.write_text(dump_yaml(sidecar), encoding="utf-8")
    print(f"authored {len(SPECS)} verified queries at verified_at={verified_at}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
