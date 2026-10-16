"""Known-answer verification (architecture §5.6).

For every ``verified_queries[]`` entry: compile the sidecar's ``semantic_query`` on the
requested engine, execute it read-only, and compare with the human-written expected
result in ``tests/fixtures/known-answers/<model>/<name>.json``. Before comparing, the
stored semantic digest is checked against the current one (§4.7): a mismatch is ``STALE``
and is never compared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent_bi.config import Config
from agent_bi.engines import open_engine
from agent_bi.errors import AgentBiError, validation_error
from agent_bi.hashing import definition_digests, semantic_digest
from agent_bi.query.compile import compile as compile_query
from agent_bi.query.contract import parse_query, query_hash
from agent_bi.semantic.model import load_model_dict
from agent_bi.semantic.provenance import entry_key, load_sidecar_dict
from agent_bi.semantic.scan import paths_for
from agent_bi.semantic.validate import validate
from agent_bi.targets import rill as rill_target
from agent_bi.verify import rill_route

TOLERANCE = 0.005  # "to the cent" (§5.6)
MAX_DIFF_ROWS = 20


@dataclass
class VerifyResult:
    lines: list[str] = field(default_factory=list)
    exit_code: int = 0
    results: dict[str, str] = field(default_factory=dict)  # name -> PASS | FAIL | STALE | UNSUPPORTED


# -- comparison -----------------------------------------------------------------------


def _norm(v: Any) -> Any:
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time(0) else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _cell_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= TOLERANCE
    return a == b


def _sort_key(row: list[Any]) -> list[tuple[int, Any]]:
    key: list[tuple[int, Any]] = []
    for v in row:
        if v is None:
            key.append((0, 0))
        elif isinstance(v, bool):
            key.append((3, int(v)))
        elif isinstance(v, float):
            key.append((1, v))
        else:
            key.append((2, str(v)))
    return key


def compare(
    expected: dict[str, Any], columns: list[str], rows: list[tuple[Any, ...]]
) -> list[str]:
    """Return the differences (empty = equal): same column names in order, same row count,
    rows compared after sorting by all columns, numbers to the cent, strings exact, NULL
    only equals NULL (§5.6). At most ``MAX_DIFF_ROWS`` row differences are listed."""
    exp_cols = list(expected["columns"])
    if columns != exp_cols:
        return [f"columns differ: expected {exp_cols}, got {columns}"]
    exp_rows = sorted(([_norm(c) for c in r] for r in expected["rows"]), key=_sort_key)
    act_rows = sorted(([_norm(c) for c in r] for r in rows), key=_sort_key)
    diffs: list[str] = []
    if len(exp_rows) != len(act_rows):
        diffs.append(f"row count differs: expected {len(exp_rows)}, got {len(act_rows)}")
    for i, (e, a) in enumerate(zip(exp_rows, act_rows, strict=False)):
        if len(e) != len(a) or not all(_cell_equal(x, y) for x, y in zip(e, a, strict=True)):
            diffs.append(f"row {i}: expected {e}, got {a}")
    if len(exp_rows) != len(act_rows):
        longer, label = (exp_rows, "expected") if len(exp_rows) > len(act_rows) else (act_rows, "got")
        for i in range(min(len(exp_rows), len(act_rows)), len(longer)):
            diffs.append(f"row {i}: only in {label}: {longer[i]}")
    if len(diffs) > MAX_DIFF_ROWS:
        extra = len(diffs) - MAX_DIFF_ROWS
        diffs = diffs[:MAX_DIFF_ROWS] + [f"... {extra} more differing rows"]
    return diffs


# -- verify ----------------------------------------------------------------------------


def _load_known_answer(project_dir: Path, rec: dict[str, Any], model: str, name: str) -> dict[str, Any]:
    rel = rec.get("known_answer") or f"tests/fixtures/known-answers/{model}/{name}.json"
    path = project_dir / rel
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise validation_error("PROVENANCE_MISMATCH", f"{name}: known-answer file {rel} not found") from None
    except json.JSONDecodeError as exc:
        raise validation_error("PROVENANCE_MISMATCH", f"{name}: {rel} is not valid JSON: {exc}") from exc


def _changed_definitions(
    stored: dict[str, str], current: dict[str, str], removed: list[str]
) -> str:
    """The definitions whose current digest differs from the stored one, ``base_table``
    first then name order; ``<name> (removed)`` for one the model no longer defines (§4.7).
    Diagnosis only: staleness was already decided by the semantic digest."""
    names = ["base_table"] + sorted(k for k in stored if k != "base_table")
    parts = []
    for n in names:
        if n not in stored:
            continue
        if n in removed:
            parts.append(f"{n} (removed)")
        elif current.get(n) != stored[n]:
            parts.append(n)
    return ", ".join(parts) or "definitions changed (no per-definition record to name them)"


def _rill_outcome(
    expected: dict[str, Any], res: rill_route.RillResult
) -> tuple[list[str], list[tuple[Any, ...]], list[str]]:
    """Turn one ``rill query`` result into ``(columns, rows, diffs)``. A failed query, or an
    empty answer when rows are expected, is a difference, never a pass."""
    if res.returncode != 0:
        return [], [], [f"rill query failed (rc={res.returncode}): {res.stderr or 'no message'}"]
    if not res.stdout.strip():  # rill prints nothing at all for zero rows
        if expected["rows"]:
            return [], [], [
                f"rill returned no rows; expected {len(expected['rows'])}"
                + (f" ({res.stderr})" if res.stderr else "")
            ]
        return list(expected["columns"]), [], []
    rows = rill_route.typed_rows(expected, res.rows)
    return res.columns, rows, compare(expected, res.columns, rows)


def verify(
    model_name: str,
    engine_name: str,
    config: Config,
    project_dir: Path,
    coverage: bool = False,
) -> VerifyResult:
    """Run every known answer of ``model_name``. Exit 0 only if all PASS (and, with
    ``coverage``, no ``confirmed`` metric is uncovered); 1 on FAIL, STALE or uncovered."""
    semantic_dir = project_dir / "semantic"
    validate(model_name, semantic_dir)
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    model = load_model_dict(str(model_path))
    sidecar = load_sidecar_dict(sidecar_path)
    table = model["tables"][0]
    base = table["base_table"]
    side_vqs: dict[str, Any] = sidecar.get("verified_queries") or {}
    out = VerifyResult()

    relation_map = config.engines.duckdb.relation_map
    engine = None if engine_name == "rill" else open_engine(engine_name, config)
    rill_state = {"ready": False}
    try:
        def recheck_binding() -> None:
            rill_route.check_binding(
                project_dir / config.rill.project_dir, rill_target.RILL_SCHEMA_PIN
            )

        if engine is None:  # bind to this project's Rill before anything else (A1, D396)
            recheck_binding()
        alias = (
            engine.resolve_relation(base["database"], base["schema"])[0] if engine else None
        )
        queries = [v["name"] for v in model.get("verified_queries") or []]
        stale: set[str] = set()
        for name in queries:
            rec = side_vqs[name]
            ka = _load_known_answer(project_dir, rec, model_name, name)
            q = parse_query(rec["semantic_query"], f"verified_queries[{name}].semantic_query")
            removed: list[str] = []
            current_defs = definition_digests(q, model, removed)
            current_digest = None if removed else semantic_digest(q, model)
            stored = {rec.get("semantic_digest"), ka.get("semantic_digest")}
            if stored != {current_digest}:
                stale.add(name)
                changed = _changed_definitions(
                    rec.get("definition_digests") or ka.get("definition_digests") or {},
                    current_defs, removed,
                )
                out.lines.append(f"STALE {name}: {changed}")
                out.results[name] = "STALE"
                continue
            current_hash = query_hash(q)
            if {rec.get("query_hash"), ka.get("query_hash")} != {current_hash}:
                out.lines.append(
                    f"FAIL {name}: query_hash of the stored semantic_query is {current_hash}, "
                    f"sidecar has {rec.get('query_hash')}, known-answer file has {ka.get('query_hash')}"
                )
                out.results[name] = "FAIL"
                continue
            if engine is None:  # the rill route (§5.6)
                try:
                    sql = compile_query(q, model, "rill").compiled_sql
                except AgentBiError as exc:
                    if exc.code != "UNSUPPORTED_BY_RILL_ROUTE":
                        raise
                    out.lines.append(f"UNSUPPORTED_BY_ROUTE {name}")
                    out.results[name] = "UNSUPPORTED"
                    continue
                if not rill_state["ready"]:
                    metric_names = [m["name"] for m in table.get("metrics") or []]
                    probe_metric = "row_count" if "row_count" in metric_names else metric_names[0]
                    rill_route.wait_until_ready(
                        project_dir / config.rill.project_dir,
                        f"SELECT {probe_metric} FROM {model_name} LIMIT 1",
                        before_query=recheck_binding,
                    )
                    rill_state["ready"] = True
                recheck_binding()  # immediately before every query (D398, B-075)
                res = rill_route.run_metrics_sql(
                    project_dir / config.rill.project_dir, sql, q.limit
                )
                columns, rows, diffs = _rill_outcome(ka["expected"], res)
            else:
                sql = compile_query(q, model, "duckdb", relation_map=relation_map).compiled_sql
                columns, rows, _ = engine.execute_with_as_of(sql, alias)
                diffs = compare(ka["expected"], columns, rows)
            if diffs:
                out.lines.append(f"FAIL {name}")
                out.lines.extend(f"  {d}" for d in diffs)
                out.results[name] = "FAIL"
            else:
                out.lines.append(f"PASS {name}")
                out.results[name] = "PASS"
        if any(r not in ("PASS", "UNSUPPORTED") for r in out.results.values()):
            out.exit_code = 1

        if coverage:
            covered: dict[str, list[str]] = {}
            for name in queries:
                if name in stale:
                    continue
                for m in parse_query(side_vqs[name]["semantic_query"]).metrics:
                    covered.setdefault(m, []).append(name)
            out.lines.append("coverage (current verified queries per metric):")
            for m in table.get("metrics") or []:
                names = covered.get(m["name"], [])
                status = (sidecar.get("entries") or {}).get(
                    entry_key("metrics", m["name"]), {}
                ).get("status")
                out.lines.append(f"  {m['name']}: {', '.join(names) if names else '-- none --'}")
                if not names and status == "confirmed":
                    out.lines.append(f"UNCOVERED {m['name']}: confirmed metric has no verified query")
                    out.exit_code = 1
    finally:
        if engine is not None:
            engine.close()
    return out
