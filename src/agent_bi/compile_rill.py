"""``compile-rill``: semantic model + sidecar -> the generated Rill files and manifest
(architecture §5.4, §4.4, §5.14, ADR-009).

``plan_compile_rill`` computes every output in memory (and can therefore be combined with
other outputs in one transaction, as ``init`` does); ``compile_rill`` publishes them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from agent_bi import fs_txn
from agent_bi.config import Config
from agent_bi.engines import DuckDBEngine, open_engine
from agent_bi.errors import validation_error
from agent_bi.provenance_manifest import (
    build_manifest,
    manifest_path,
    manifest_text,
    new_build_id,
)
from agent_bi.rill_models import current_models_digest
from agent_bi.semantic.model import load_model_dict
from agent_bi.semantic.provenance import load_sidecar_dict
from agent_bi.semantic.scan import paths_for
from agent_bi.semantic.validate import validate
from agent_bi.targets import rill as rill_target


def _quote_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def catalog_columns(engine: DuckDBEngine, catalog: str, schema: str, table: str) -> list[str]:
    """Column names of the relation in catalog ordinal order."""
    rows = engine.execute(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_catalog = {_quote_literal(catalog)} AND table_schema = {_quote_literal(schema)} "
        f"AND table_name = {_quote_literal(table)} ORDER BY ordinal_position"
    )[1]
    if not rows:
        raise validation_error(
            "RELATION_NOT_FOUND", f"{catalog}.{schema}.{table} has no columns in the catalog"
        )
    return [r[0] for r in rows]


_PER_BUILD_RE = re.compile(r"^\d{8}T\d{6}Z\.json$")


def superseded_manifests(project_dir: Path, rill_rel: str, model: str) -> list[str]:
    """Per-build manifests (``<build-id>.json``, written by the first Sprint 3 code) of
    ``model``: the one manifest per model (D395) supersedes them. Nothing else in
    ``rill/provenance/`` is touched."""
    out = []
    for p in sorted((project_dir / rill_rel / "provenance").glob("*.json")):
        if not _PER_BUILD_RE.match(p.name):
            continue
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("model") == model:
                out.append(f"{rill_rel}/provenance/{p.name}")
        except (OSError, ValueError):
            continue
    return out


def plan_compile_rill(
    model_name: str,
    model_dict: dict,
    sidecar_dict: dict,
    model_bytes: bytes,
    sidecar_bytes: bytes,
    config: Config,
    engine: DuckDBEngine,
    project_dir: Path,
    build_id: str | None = None,
) -> tuple[dict[str, str], str, list[str]]:
    """Return ``({repo-relative path: text}, manifest path, superseded files to delete)``
    without writing anything."""
    relation_kind = (sidecar_dict.get("relation") or {}).get("kind", "table")
    table = model_dict["tables"][0]
    rill_rel = PurePosixPath(config.rill.project_dir).as_posix()
    rill_dir_abs = (project_dir / config.rill.project_dir).resolve()
    if relation_kind == "view":
        # §5.13 rule 7, ADR-009: the operator's rill/models/<name>.sql is the model and is
        # never generated, rewritten or translated; the metrics view references it by name.
        # A model the engine could not make available is refused here (D431) before any
        # output is computed or published.
        engine.require_rill_model(table["base_table"]["table"])
        files = rill_target.render_view(
            model_dict, sidecar_dict, project_name=config.project,
            rill_model=table["base_table"]["table"],
        )
        generated = rill_target.generated_paths_view(model_name)
    elif relation_kind == "table":
        model_sql_rel = f"{rill_rel}/models/{model_name}.sql"
        existing = project_dir / model_sql_rel
        if existing.is_file() and not existing.read_text(encoding="utf-8").startswith(
            rill_target.sql_header(model_name)
        ):
            raise validation_error(
                "OPERATOR_FILE",
                f"{model_sql_rel} does not begin with the agent-bi generated header, so it is "
                "an operator-owned Rill model; compile-rill never overwrites it (ADR-009, "
                "§5.13 rule 7)",
            )
        base = table["base_table"]
        alias, schema = engine.resolve_relation(base["database"], base["schema"])
        if engine.attach_type(alias) != "sqlite":
            raise validation_error(
                "NOT_IMPLEMENTED",
                f"compile-rill supports SQLite sources only in v1; {alias!r} is {engine.attach_type(alias)}",
            )
        columns = catalog_columns(engine, alias, schema, base["table"])
        source_path = rill_target.relative_source_path(engine.attach_path(alias), rill_dir_abs)
        files = rill_target.render(
            model_dict, sidecar_dict, project_name=config.project, columns=columns,
            source_path=source_path,
        )
        generated = rill_target.generated_paths(model_name)
    else:
        raise validation_error(
            "UNSUPPORTED_VIEW_FORM", f"unsupported relation kind {relation_kind!r} in the sidecar"
        )
    outputs = {f"{rill_rel}/{p}": text for p, text in files.items()}
    bid = build_id or new_build_id()
    manifest = build_manifest(
        bid, model_name,
        {f"{rill_rel}/{p}": files[p] for p in generated},
        model_bytes, sidecar_bytes,
        current_models_digest(
            rill_dir_abs, model_dict, config.engines.duckdb.rill_models
        ),
        rill_target.RILL_SCHEMA_PIN,
    )
    path = manifest_path(rill_rel, model_name)
    outputs[path] = manifest_text(manifest)
    return outputs, path, superseded_manifests(project_dir, rill_rel, model_name)


def compile_rill(
    model_name: str, config: Config, project_dir: Path, build_id: str | None = None
) -> tuple[list[str], str]:
    """The ``compile-rill`` command: validate, compute, publish as one transaction. Returns
    ``(published paths, manifest path)``."""
    semantic_dir = project_dir / "semantic"
    validate(model_name, semantic_dir)
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    model_dict = load_model_dict(str(model_path))
    sidecar_dict = load_sidecar_dict(sidecar_path)
    engine = open_engine("duckdb", config)
    try:
        outputs, manifest, deletes = plan_compile_rill(
            model_name, model_dict, sidecar_dict, model_path.read_bytes(),
            sidecar_path.read_bytes(), config, engine, project_dir, build_id,
        )
    finally:
        engine.close()
    published = fs_txn.publish(project_dir, outputs, deletes=deletes)
    return published, manifest
