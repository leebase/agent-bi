"""``ask`` (architecture §5.8): a verified question or a SemanticQuery in, an answer envelope out.

No LLM anywhere: the ``--question`` path is a deterministic normalized exact match; the
``--query-file`` path validates, canonicalizes, compiles, executes read-only, labels trust from
the sidecar (§4.7) and writes ``out/<query_hash>.answer.json`` (+ ``.vl.json``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent_bi.config import Config
from agent_bi.engines import open_engine
from agent_bi.errors import EXIT_NO_MATCH, AgentBiError, validation_error
from agent_bi.explore import components
from agent_bi.explore.chart import build_chart, load_theme
from agent_bi.hashing import (
    definition_digests,
    semantic_digest,
    sha256_bytes,
    sha256_text,
)
from agent_bi.provenance_manifest import sidecar_digest
from agent_bi.query.compile import compile as compile_query
from agent_bi.query.compile import executed_sql
from agent_bi.query.contract import (
    SemanticQuery,
    parse_query,
    query_hash,
    validate_against_model,
)
from agent_bi.query.files import load_query_file
from agent_bi.query.verified import normalize_question
from agent_bi.rill_models import current_models_digest
from agent_bi.semantic.model import load_model_dict
from agent_bi.semantic.provenance import entry_key, load_sidecar_dict
from agent_bi.semantic.scan import paths_for
from agent_bi.semantic.validate import validate
from agent_bi.verify import serving

ENVELOPE_SCHEMA = "agent-bi-answer/2.0"
RECORD_SCHEMA = "agent-bi-provenance-record/2.0"
MAX_TEXT_ROWS = 50
_KIND_PLURAL = {
    "metric": "metrics", "dimension": "dimensions", "time_dimension": "time_dimensions",
    "filter": "filters",
}


@dataclass
class Answer:
    envelope: dict[str, Any]
    text: str
    paths: list[str]  # project-relative


# -- question lookup (§5.8, no fuzzy matching) -----------------------------------------------


def find_question(
    question: str, sidecar: dict[str, Any], project_dir: Path
) -> tuple[str, dict[str, Any]]:
    """``(name, semantic_query dict)`` of the one record whose ``question_normalized`` equals
    the normalized ``question``; ``E_NO_VERIFIED_MATCH`` (exit 4) for none, ``E_AMBIGUOUS_QUESTION``
    (exit 2) for more than one distinct record. The sidecar's verified queries and every
    ``queries/*.json`` are searched; one name found in both is one record."""
    wanted = normalize_question(question)
    hits: dict[tuple[str, str], dict[str, Any]] = {}  # (name, query_hash) -> semantic_query
    for name, rec in sorted((sidecar.get("verified_queries") or {}).items()):
        if (rec or {}).get("question_normalized") == wanted:
            sq = rec["semantic_query"]
            hits[(name, query_hash(sq))] = sq
    for p in sorted((project_dir / "queries").glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and doc.get("question_normalized") == wanted \
                and isinstance(doc.get("semantic_query"), dict):
            name = doc.get("name") or p.stem
            sq = doc["semantic_query"]
            try:
                hits.setdefault((name, query_hash(sq)), sq)
            except AgentBiError:
                continue
    if not hits:
        raise AgentBiError(
            "NO_VERIFIED_MATCH",
            f"no verified or saved query has the question {wanted!r}", EXIT_NO_MATCH,
        )
    if len(hits) > 1:
        names = sorted({n for n, _ in hits})
        raise validation_error(
            "AMBIGUOUS_QUESTION",
            f"{len(hits)} queries have the question {wanted!r}: {', '.join(names)}; "
            "run `ask --query-file queries/<name>.json` with the one you mean",
        )
    (name, _h), sq = next(iter(hits.items()))
    return name, sq


# -- definition state (§4.7 table; §4.8.2 claim sentences) ----------------------------------


def referenced_with_status(
    query: SemanticQuery, model: dict[str, Any], sidecar: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """The provenance record's ``definitions`` block: every referenced object, sorted by name."""
    from agent_bi.hashing import expr_sha256, referenced_definitions

    entries = sidecar.get("entries") or {}
    out: dict[str, dict[str, Any]] = {}
    for name, d in referenced_definitions(query, model).items():
        entry = entries.get(entry_key(_KIND_PLURAL[d["kind"]], name)) or {}
        out[name] = {
            "kind": d["kind"], "expr": d["expr"], "expr_sha256": expr_sha256(d["expr"]),
            "status": entry.get("status"),
        }
    return out


# -- text rendering --------------------------------------------------------------------------


def interpretation(q: SemanticQuery) -> str:
    """One paragraph rendered from the canonical query by a fixed template (§5.8 step 8)."""
    text = "Metrics: " + (", ".join(q.metrics) if q.metrics else "(none)")
    if q.dimensions:
        text += " by " + ", ".join(q.dimensions)
    if q.time is not None:
        text += f" over {q.time.dimension} ({q.time.grain})"
        if q.time.start:
            text += f" from {q.time.start}"
        if q.time.end:
            text += f" to {q.time.end} (exclusive)"
    filters = [f"{f.dimension} {f.op} {json.dumps(f.values)}" for f in q.filters]
    filters += [f"named filter {n}" for n in q.named_filters]
    if filters:
        text += ", filtered " + "; ".join(filters)
    return text + "."


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return format(value, ".15g")
    return str(value)


def text_table(columns: list[str], rows: list[list[Any]]) -> str:
    shown = [[_cell(c) for c in r] for r in rows[:MAX_TEXT_ROWS]]
    widths = [max([len(columns[i])] + [len(r[i]) for r in shown]) for i in range(len(columns))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)).rstrip()]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in shown]
    if len(rows) > MAX_TEXT_ROWS:
        lines.append(f"... {len(rows) - MAX_TEXT_ROWS} more rows (first {MAX_TEXT_ROWS} shown)")
    return "\n".join(lines)


def render_text(
    env: dict[str, Any], q: SemanticQuery, note: str | None, paths: list[str],
    serving_info: serving.ServingInfo | None = None,
) -> str:
    prov = env["provenance"]
    rec, art = prov["receipt"], prov["artifact"]
    defs = ", ".join(f"{n}={d['status']}" for n, d in art["definitions"].items())
    evidence = rec["evidence"]
    parts = [
        interpretation(q),
        f"Definition: {evidence['definition']['claim']}",
        f"Serving:    {evidence['serving']['claim']}",
    ]
    if serving_info is not None and serving_info.context:
        parts.append(f"            {serving_info.context}")
    if serving_info is not None and serving_info.diff:
        parts += [f"            {d}" for d in serving_info.diff]
    parts += [
        f"            {serving.EXCLUSIONS}",
        f"published: {'true' if rec['published'] else 'false'}",
        f"definitions: {defs}",
        "```sql\n" + art["compiled_sql"] + "\n```",
        text_table(env["columns"], env["rows"]),
    ]
    if note:
        parts.append(f"chart: none ({note})")
    parts += [f"wrote {p}" for p in paths]
    return "\n".join(parts)


# -- JSON-safe rows --------------------------------------------------------------------------


def json_value(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time().isoformat() == "00:00:00" else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


# -- the pipeline ----------------------------------------------------------------------------


def run_ask(
    model_name: str, config: Config, project_dir: Path, *, query_file: str | None = None,
    question: str | None = None, engine_name: str | None = None, mark: str | None = None,
    theme: str | None = None,
) -> Answer:
    project_dir = Path(project_dir)
    semantic_dir = project_dir / "semantic"
    validate(model_name, semantic_dir)  # step 1
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    model = load_model_dict(str(model_path))
    sidecar = load_sidecar_dict(sidecar_path)

    if question is not None:  # the deterministic, LLM-free path
        _name, raw = find_question(question, sidecar, project_dir)
        source = f"verified or saved query {_name!r}"
    else:
        assert query_file is not None
        raw, source = load_query_file(query_file), query_file
    q = validate_against_model(parse_query(raw, source), model).canonical()  # step 2
    models_digest = current_models_digest(
        config.base_dir / config.rill.project_dir, model, config.engines.duckdb.rill_models
    )
    q_hash, digest = query_hash(q), semantic_digest(q, model, models_digest)  # step 3
    definitions = referenced_with_status(q, model, sidecar)
    removed: list[str] = []
    current_defs = definition_digests(q, model, removed, models_digest)
    statuses = {name: d["status"] for name, d in definitions.items()}
    definition = serving.classify_definition(
        q_hash, digest, sidecar, model, current_defs, removed, statuses
    )
    against = definition.name if definition.state in ("governed", "verified") else None
    ka_digest = None
    if definition.name is not None:
        side_rec = (sidecar.get("verified_queries") or {}).get(definition.name) or {}
        ka = serving.load_known_answer(project_dir, model_name, definition.name, side_rec)
        if ka is not None:
            ka_digest = serving.known_answer_content_sha256(ka.get("expected"))
    serving_info = serving.answer_serving(
        project_dir, model_name, q_hash, definition.state, digest, ka_digest
    )
    published = components.is_published(project_dir, config, q_hash, digest)

    engine_name = engine_name or "duckdb"
    chart, note = build_chart(q, model, mark=mark, theme=load_theme(project_dir, theme))  # step 7 input
    engine = open_engine(engine_name, config)
    try:
        base = model["tables"][0]["base_table"]
        alias = engine.data_alias(base["database"], base["schema"], base["table"])
        compiled = compile_query(  # step 4
            q, model, engine_name, relation_map=config.engines.duckdb.relation_map
        )
        run_sql = executed_sql(compiled, engine_name)
        executed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        columns, rows, as_of = engine.execute_with_as_of(run_sql, alias)  # step 5
        version, settings = engine.version(), engine.engine_settings()
    finally:
        engine.close()
    out_rows = [[json_value(c) for c in r] for r in rows]

    sidecar_bytes = sidecar_path.read_bytes()  # step 6
    record = {
        "schema_version": RECORD_SCHEMA,
        "artifact": {
            "query_hash": q_hash,
            "semantic_digest": digest,
            "model_file_sha256": sha256_bytes(model_path.read_bytes()),
            "sidecar_sha256": sidecar_digest(sidecar_bytes),
            "models_digest": models_digest,
            "definitions": definitions,
            "compiled_sql": compiled.compiled_sql,
            "compiled_sql_sha256": sha256_text(compiled.compiled_sql),
            "dialect": compiled.dialect,
        },
        "receipt": {
            "trust": serving.TOMBSTONE_TRUST,
            "trust_reason": serving.TOMBSTONE_TRUST_REASON,
            "evidence": {
                "definition": definition.evidence(),
                "serving": serving_info.evidence(),
            },
            "published": published,
            "verified_against": against,
            "engine": engine_name,
            "engine_version": version,
            "engine_settings": settings,
            "as_of": {"kind": as_of.kind, "value": as_of.value, "retention_note": as_of.retention_note},
            "executed_sql": run_sql,
            "executed_sql_sha256": sha256_text(run_sql),
            "executed_at": executed_at,
            "row_count": len(out_rows),
        },
    }
    envelope = {
        "schema_version": ENVELOPE_SCHEMA,
        "semantic_query": q.canonical_dict(),
        "provenance": record,
        "chart": chart,
        "columns": columns,
        "rows": out_rows,
    }
    out_dir = project_dir / "out"  # step 8
    out_dir.mkdir(exist_ok=True)
    stem = q_hash.removeprefix("sha256:")
    written = [out_dir / f"{stem}.answer.json"]
    written[0].write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if chart is not None:
        vl = out_dir / f"{stem}.vl.json"
        vl.write_text(json.dumps(chart, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(vl)
    rels = [p.relative_to(project_dir).as_posix() for p in written]
    return Answer(envelope, render_text(envelope, q, note, rels, serving_info), rels)
