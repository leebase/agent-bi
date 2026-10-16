"""Sprint 3 task 4: prove the CSV-quoted ``--properties`` value with a temporary filtered
SemanticQuery (an ``=`` filter, so the SQL holds more than one ``=`` once ``sql=`` is added).

Compiles the query for duckdb and for rill, runs the first on DuckDB and the second on a
running ``rill start rill``, compares the two results with the §5.6 rules, and prints
everything it did. Nothing is saved: the query lives only in this script's memory.
Usage (from the repo root, with Rill running): python scripts/rill_filtered_probe.py [model]
Exit 0 if Rill = DuckDB and both returned rows; 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bi.config import load_config
from agent_bi.engines import DuckDBEngine
from agent_bi.query.compile import compile as compile_query
from agent_bi.query.contract import parse_query
from agent_bi.semantic.model import load_model_dict
from agent_bi.targets.rill import RILL_SCHEMA_PIN
from agent_bi.verify import rill_route
from agent_bi.verify.known_answers import compare

MODEL = sys.argv[1] if len(sys.argv) > 1 else "idle_warehouse_waste"
QUERY = {
    "schema_version": "semantic-query/1.0",
    "model": MODEL,
    "table": "warehouse_metering_history",
    "metrics": ["total_credits_used", "row_count"],
    "dimensions": ["warehouse_name"],
    "filters": [{"dimension": "warehouse_name", "op": "=", "values": ["CORE_ELT_WH"]}],
    "named_filters": [],
    "order_by": [],
}


def main() -> int:
    config = load_config(ROOT / "agent-bi.yaml")
    model = load_model_dict(str(ROOT / "semantic" / f"{MODEL}.yaml"))
    q = parse_query(QUERY, "temporary filtered probe")
    duck_sql = compile_query(q, model, "duckdb", relation_map=config.engines.duckdb.relation_map).compiled_sql
    rill_sql = compile_query(q, model, "rill").compiled_sql
    print("temporary SemanticQuery:", QUERY)
    print("\nrill SQL :", rill_sql)
    print("properties arg:", rill_route.properties_arg("sql", rill_sql))
    print("duckdb SQL:\n" + duck_sql)

    engine = DuckDBEngine(config)
    try:
        alias = engine.resolve_relation("FIXTURE", "MAIN")[0]
        d_cols, d_rows, _ = engine.execute_with_as_of(duck_sql, alias)
    finally:
        engine.close()
    print("\nDuckDB result:", d_cols, [list(r) for r in d_rows])

    project = ROOT / config.rill.project_dir
    rill_route.check_binding(project, RILL_SCHEMA_PIN)  # D396: the Rill on :9009 must serve this project
    rill_route.wait_until_ready(project, f"SELECT row_count FROM {MODEL} LIMIT 1")
    res = rill_route.run_metrics_sql(project, rill_sql, q.limit)
    print(f"Rill  result: rc={res.returncode} columns={res.columns} rows={res.rows} stderr={res.stderr!r}")
    if res.returncode != 0 or not d_rows or not res.rows:
        print("FAIL: an empty or failed result is never a pass")
        return 1
    expected = {"columns": d_cols, "rows": [list(r) for r in d_rows]}
    diffs = compare(expected, res.columns, rill_route.typed_rows(expected, res.rows))
    if diffs:
        print("FAIL: Rill differs from DuckDB:", *diffs, sep="\n  ")
        return 1
    print("PASS: Rill = DuckDB for the '=' filter (CSV-quoted --properties value)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
