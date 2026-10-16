"""``save`` (architecture §5.10): promote an answer envelope into a saved query, a Rill
component and, optionally, a canvas row and a verified query -- as one write transaction.

Everything is computed and every precondition (including the independent route of
``--verify``) is satisfied in memory first; only then are files published, in the fixed order
of §5.14. Any failure before publication leaves the repository byte-identical.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from agent_bi import fs_txn
from agent_bi.config import Config
from agent_bi.engines import open_engine
from agent_bi.errors import engine_error, validation_error
from agent_bi.explore import components
from agent_bi.explore.chart import DATA_NAME
from agent_bi.hashing import canonical_dumps, definition_digests, semantic_digest
from agent_bi.query.compile import compile as compile_query
from agent_bi.query.contract import parse_query, query_hash, validate_against_model
from agent_bi.query.verified import normalize_question
from agent_bi.semantic.model import dump_yaml, load_model_dict
from agent_bi.semantic.scan import paths_for
from agent_bi.semantic.validate import validate
from agent_bi.verify.known_answers import compare

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SAVED_QUERY_SCHEMA = "agent-bi-saved-query/1.0"
ANSWER_SCHEMA = "agent-bi-answer/1.0"
COMPUTE_SCRIPT = "scripts/compute_known_answer.py"
CANVAS_ROW_HEIGHT = "400px"
CANVAS_ROW_WIDTH = 12


def _load_envelope(path: str) -> dict[str, Any]:
    try:
        env = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise validation_error("QUERY_INVALID", f"--from {path}: cannot read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise validation_error("QUERY_INVALID", f"--from {path}: not valid JSON: {exc}") from exc
    if not isinstance(env, dict) or env.get("schema_version") != ANSWER_SCHEMA:
        raise validation_error(
            "QUERY_INVALID", f"--from {path}: not an answer envelope ({ANSWER_SCHEMA})"
        )
    for key in ("semantic_query", "provenance", "chart", "columns", "rows"):
        if key not in env:
            raise validation_error("QUERY_INVALID", f"--from {path}: envelope has no {key!r}")
    return env


def _saved_query_doc(
    name: str, status: str, question: str | None, q_hash: str, digest: str, sq: dict[str, Any]
) -> str:
    doc = {
        "schema_version": SAVED_QUERY_SCHEMA,
        "name": name,
        "status": status,
        "question": question,
        "question_normalized": normalize_question(question) if question else None,
        "query_hash": q_hash,
        "semantic_digest": digest,
        "semantic_query": json.loads(canonical_dumps(sq)),  # canonical: keys sorted
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# -- canvas row ------------------------------------------------------------------------------


def append_canvas_row(text: str, component: str) -> str:
    """Append ``{items: [{component: <name>, width: 12}], height: 400px}`` as the new last row
    (§5.10 step 3). Block-style ``rows:`` is edited textually so the operator's comments and
    layout survive; any other layout is re-dumped."""
    doc = yaml.safe_load(text)
    rows = doc.get("rows") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise validation_error("QUERY_INVALID", "canvas has no `rows:` list to append to")
    if component in components.canvas_components(doc):
        return text  # already placed: save is idempotent
    lines = text.rstrip("\n").split("\n")
    idx = next((i for i, ln in enumerate(lines) if re.match(r"^rows:\s*(#.*)?$", ln)), None)
    if idx is not None:
        end = idx
        dash_indent = 2
        for i in range(idx + 1, len(lines)):
            ln = lines[i]
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            indent = len(ln) - len(ln.lstrip())
            if indent == 0:
                break
            if ln.lstrip().startswith("- ") and end == idx:
                dash_indent = indent
            end = i
        pad = " " * dash_indent
        new = [
            f"{pad}- items:",
            f"{pad}    - component: {component}",
            f"{pad}      width: {CANVAS_ROW_WIDTH}",
            f"{pad}  height: {CANVAS_ROW_HEIGHT}",
        ]
        lines[end + 1:end + 1] = new
        out = "\n".join(lines) + "\n"
        check = yaml.safe_load(out)
        if component in components.canvas_components(check) and len(check["rows"]) == len(rows) + 1:
            return out
    rows.append({"items": [{"component": component, "width": CANVAS_ROW_WIDTH}],
                 "height": CANVAS_ROW_HEIGHT})
    return yaml.safe_dump(doc, sort_keys=False)


# -- the independent route (--verify) ---------------------------------------------------------


def run_independent_sql(
    project_dir: Path, sqlite_path: Path, sql: str, name: str, by: str
) -> dict[str, Any]:
    """Run the operator's SQL with ``scripts/compute_known_answer.py`` (read-only sqlite3, one
    SELECT/WITH statement) and return the known-answer document it writes."""
    script = project_dir / COMPUTE_SCRIPT
    if not script.is_file():
        raise engine_error("ENGINE_UNAVAILABLE", f"{COMPUTE_SCRIPT} not found under {project_dir}")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ka.json"
        proc = subprocess.run(
            [sys.executable, str(script), "--sqlite", str(sqlite_path), "--sql", sql,
             "--name", name, "--by", by, "--out", str(out)],
            check=False, capture_output=True, text=True, cwd=project_dir,
        )
        if proc.returncode == 2:
            raise validation_error(
                "STATEMENT_NOT_ALLOWED", proc.stderr.strip() or "--expected-sql must be a SELECT or WITH"
            )
        if proc.returncode != 0:
            raise validation_error(
                "VERIFY_MISMATCH",
                f"the independent route failed: {proc.stderr.strip() or proc.stdout.strip()}",
            )
        return json.loads(out.read_text(encoding="utf-8"))


# -- save ------------------------------------------------------------------------------------


def run_save(
    from_path: str, name: str, config: Config, project_dir: Path, *, question: str | None = None,
    force: bool = False, verify: bool = False, by: str | None = None,
    expected_sql: str | None = None, add_to_canvas: str | None = None,
) -> list[str]:
    project_dir = Path(project_dir)
    if not NAME_RE.match(name):
        raise validation_error("QUERY_INVALID", f"--name {name!r} must match ^[a-z][a-z0-9_]*$")
    env = _load_envelope(from_path)
    q = parse_query(env["semantic_query"], f"{from_path}: semantic_query").canonical()
    q_hash = query_hash(q)
    art = (env.get("provenance") or {}).get("artifact") or {}
    if art.get("query_hash") != q_hash:
        raise validation_error(
            "QUERY_INVALID", f"{from_path}: provenance.artifact.query_hash does not match its semantic_query"
        )
    model_name = q.model
    semantic_dir = project_dir / "semantic"
    validate(model_name, semantic_dir)
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    model = load_model_dict(str(model_path))
    validate_against_model(q, model)
    digest = semantic_digest(q, model)
    if art.get("semantic_digest") != digest:
        raise validation_error(
            "QUERY_INVALID",
            f"{from_path}: the answer was computed against different definitions than the model "
            "has now (semantic_digest differs); run `ask` again and save the new answer",
        )
    if verify:
        if not expected_sql:
            raise validation_error(
                "NO_INDEPENDENT_SQL",
                "--verify needs --expected-sql \"<SQL>\": a verified query is never created from a "
                "single route, and the independent SQL is supplied by hand on the command line",
            )
        if not by or not by.strip():
            raise validation_error("QUERY_INVALID", "--verify needs --by <who> (the person who wrote the SQL)")
    elif expected_sql or by:
        raise validation_error("QUERY_INVALID", "--expected-sql and --by are only used with --verify")

    rill_rel = components.rill_rel(config)
    outputs: dict[str, str] = {}

    # 1. queries/<name>.json
    query_rel = f"queries/{name}.json"
    existing = project_dir / query_rel
    prior_question: str | None = None
    upgrade = False
    if existing.exists():
        try:
            prior = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}
        upgrade = isinstance(prior, dict) and prior.get("status") == "proposed"
        prior_question = prior.get("question") if isinstance(prior, dict) else None
        if not upgrade and not force:
            raise validation_error(
                "NAME_EXISTS", f"{query_rel} already exists; use --force to overwrite (ADR-009)"
            )
    question = question if question is not None else prior_question
    if verify and not question:
        raise validation_error("QUERY_INVALID", "--verify needs --question \"<text>\" (the verified question)")
    status = "verified" if verify else "saved"
    outputs[query_rel] = _saved_query_doc(name, status, question, q_hash, digest, q.canonical_dict())

    # 2. rill/components/<name>.yaml (the rill dialect refuses what it cannot express)
    metrics_sql = compile_query(q, model, "rill").compiled_sql
    chart = env.get("chart")
    if not isinstance(chart, dict):
        raise validation_error(
            "CHART_INCOMPATIBLE",
            "the answer has no chart (table only), so there is nothing to save as a component",
        )
    vega = {**chart, "data": {"name": DATA_NAME}}
    comp_rel = f"{rill_rel}/components/{name}.yaml"
    if (project_dir / comp_rel).exists() and not force:
        raise validation_error(
            "NAME_EXISTS", f"{comp_rel} already exists; use --force to overwrite (ADR-009)"
        )
    outputs[comp_rel] = components.render_component(name, model_name, q_hash, digest, metrics_sql, vega)

    # 3. --add-to-canvas
    if add_to_canvas:
        canvas_rel = f"{rill_rel}/dashboards/{add_to_canvas}.yaml"
        canvas_path = project_dir / canvas_rel
        generated = {f"{model_name}_default_canvas", f"{model_name}_explore"}
        if add_to_canvas in generated or (
            canvas_path.is_file()
            and canvas_path.read_text(encoding="utf-8").startswith(components.GENERATED_PREFIX)
        ):
            raise validation_error(
                "GENERATED_FILE",
                f"{canvas_rel} is generated and overwritten by compile-rill; "
                f"`canvas fork --model {model_name} --name <name>` creates an operator-owned canvas",
            )
        if not canvas_path.is_file():
            raise validation_error(
                "CANVAS_NOT_FOUND",
                f"{canvas_rel} does not exist; `canvas fork --model {model_name} --name {add_to_canvas}`",
            )
        canvas_text = canvas_path.read_text(encoding="utf-8")
        try:
            canvas_doc = yaml.safe_load(canvas_text)
        except yaml.YAMLError as exc:
            raise validation_error("QUERY_INVALID", f"{canvas_rel}: not valid YAML: {exc}") from exc
        problem = components.canvas_scope_problem(canvas_doc if isinstance(canvas_doc, dict) else {})
        if problem:
            raise validation_error(
                "CANVAS_NOT_VERIFIED_SCOPE",
                f"{canvas_rel}: {problem}; Rill applies the canvas time range and filters on top of "
                "every component, so a chart placed here would not show the verified view (ADR-008)",
            )
        outputs[canvas_rel] = append_canvas_row(canvas_text, name)

    # 4. --verify: the independent route, before anything is published
    if verify:
        _prepare_verification(
            outputs, name, question or "", by or "", expected_sql or "", force, q, q_hash, digest,
            model, model_path, sidecar_path, config, project_dir, model_name,
        )
    return fs_txn.publish(project_dir, outputs)


def _prepare_verification(
    outputs: dict[str, str], name: str, question: str, by: str, expected_sql: str, force: bool,
    q: Any, q_hash: str, digest: str, model: dict[str, Any], model_path: Path, sidecar_path: Path,
    config: Config, project_dir: Path, model_name: str,
) -> None:
    sidecar: dict[str, Any] = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
    side_vqs: dict[str, Any] = sidecar.setdefault("verified_queries", {})
    if name in side_vqs and not force:
        raise validation_error(
            "NAME_EXISTS",
            f"verified query {name!r} already exists; `--force` re-verifies it (the way to clear STALE)",
        )
    base = model["tables"][0]["base_table"]
    engine = open_engine("duckdb", config)
    try:
        alias = engine.resolve_relation(base["database"], base["schema"])[0]
        if engine.attach_type(alias) != "sqlite":
            raise engine_error(
                "ENGINE_UNAVAILABLE", "save --verify's independent route reads a SQLite source in v1"
            )
        sqlite_path = engine.attach_path(alias)
        compiled = compile_query(q, model, "duckdb", relation_map=config.engines.duckdb.relation_map)
        columns, rows, _ = engine.execute_with_as_of(compiled.compiled_sql, alias)
    finally:
        engine.close()
    ka = run_independent_sql(project_dir, sqlite_path, expected_sql, name, by)
    diffs = compare(ka["expected"], columns, rows)
    if diffs:
        raise validation_error(
            "VERIFY_MISMATCH",
            "the compiled query and the independent SQL disagree; nothing was written:\n  "
            + "\n  ".join(diffs),
        )
    digests = definition_digests(q, model)
    # known-answer file (§5.6 key order)
    known = {
        "name": name, "query_hash": q_hash, "semantic_digest": digest,
        "definition_digests": digests, "semantic_query": q.canonical_dict(),
        "expected": ka["expected"], "expected_computed_by": ka["expected_computed_by"],
        "expected_sql": ka["expected_sql"], "expected_sql_by": by,
        "expected_at": ka["expected_at"], "fixture_sha256": ka["fixture_sha256"],
    }
    ka_rel = f"tests/fixtures/known-answers/{model_name}/{name}.json"
    outputs[ka_rel] = json.dumps(known, indent=2) + "\n"
    # verified_queries[] in the YAML: Snowflake-dialect SQL, no as-of clause (§4.1)
    snow = compile_query(q, model, "snowflake").compiled_sql
    entry = {
        "name": name, "question": question,
        "verified_at": int(datetime.now(UTC).timestamp()), "verified_by": by, "sql": snow,
    }
    model_doc: dict[str, Any] = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    vqs = model_doc.setdefault("verified_queries", [])
    for i, existing in enumerate(vqs):
        if existing.get("name") == name:
            vqs[i] = entry
            break
    else:
        vqs.append(entry)
    outputs[f"semantic/{model_name}.yaml"] = dump_yaml(model_doc)
    # sidecar record (§4.2 key order)
    side_vqs[name] = {
        "query_hash": q_hash, "semantic_digest": digest, "definition_digests": digests,
        "semantic_query": q.canonical_dict(), "question_normalized": normalize_question(question),
        "known_answer": ka_rel,
    }
    outputs[f"semantic/{model_name}.provenance.yaml"] = dump_yaml(sidecar)
