"""Subset checks and sidecar consistency (architecture §4.1, §4.2, §5.3.2, §5.3.5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_bi.config import CONFIG_FILENAME, load_config
from agent_bi.engines import DuckDBEngine
from agent_bi.errors import AgentBiError, validation_error
from agent_bi.hashing import expr_sha256
from agent_bi.query.compile import (
    COLUMN_RE,
    DIM_RE,
    FILTER_RE,
    METRIC_RE,
    expr_columns,
)
from agent_bi.rill_models import check_closure, rill_dir_for_config
from agent_bi.semantic.model import SemanticModel, load_model_dict, parse_model
from agent_bi.semantic.names import check_global_namespace, table_objects
from agent_bi.semantic.provenance import (
    entry_key,
    load_sidecar_dict,
    parse_sidecar,
)
from agent_bi.semantic.scan import MODEL_NAME_RE, _query_names, paths_for

_CHECKED_KINDS = ("dimensions", "time_dimensions", "facts", "metrics", "filters")
_EXPR_ERRORS = {
    "dimensions": ("E_UNSUPPORTED_DIMENSION_EXPR", DIM_RE, "dimension"),
    "time_dimensions": ("E_UNSUPPORTED_DIMENSION_EXPR", DIM_RE, "time dimension"),
    "facts": ("E_UNSUPPORTED_FACT_EXPR", COLUMN_RE, "fact"),
    "metrics": ("E_UNSUPPORTED_METRIC_EXPR", METRIC_RE, "metric"),
    "filters": ("E_UNSUPPORTED_FILTER_EXPR", FILTER_RE, "filter"),
}


def validate_model(model_dict: dict[str, Any], sidecar_dict: dict[str, Any], model_name: str) -> None:
    """Raise the first violated rule as an ``AgentBiError`` (exit 2)."""
    model = parse_model(model_dict, model_name)
    sidecar = parse_sidecar(sidecar_dict, f"{model_name}.provenance")
    _check_subset(model, model_name)
    table = model.tables[0]
    entries = sidecar.entries
    check_global_namespace(
        table_objects(
            table.model_dump(by_alias=True, exclude_unset=True),
            lambda kind, name: getattr(entries.get(entry_key(kind, name)), "origin", None),
        )
    )
    _check_exprs(table.model_dump(by_alias=True, exclude_unset=True))
    _check_sidecar(model, sidecar.model_dump(exclude_none=True), model_name)


def _check_subset(model: SemanticModel, model_name: str) -> None:
    if not MODEL_NAME_RE.match(model.name):
        raise validation_error("MODEL_INVALID", f"name: {model.name!r} must match ^[a-z][a-z0-9_]*$")
    if model.name != model_name:
        raise validation_error(
            "MODEL_INVALID", f"name: model file declares {model.name!r}, expected {model_name!r}"
        )
    if len(model.tables) != 1:
        raise validation_error(
            "UNSUPPORTED_MULTI_TABLE",
            f"tables: expected exactly one table, found {len(model.tables)}",
        )
    if model.relationships:
        raise validation_error(
            "UNSUPPORTED_RELATIONSHIPS",
            f"relationships: {len(model.relationships)} entries present; v1 refuses relationships",
        )
    bt = model.tables[0].base_table
    if bt is None:
        raise validation_error("MODEL_INVALID", "tables[0].base_table: missing")
    if bt.definition is not None:
        raise validation_error(
            "UNSUPPORTED_BASE_TABLE_DEFINITION",
            "tables[0].base_table.definition: v1 has no physical identity for a SQL definition",
        )
    for field in ("database", "schema_", "table"):
        if not getattr(bt, field):
            raise validation_error(
                "MODEL_INVALID", f"tables[0].base_table.{field.rstrip('_')}: missing"
            )


def _check_exprs(table: dict[str, Any]) -> None:
    columns: set[str] = set()
    for kind in ("dimensions", "time_dimensions", "facts"):
        for obj in table.get(kind) or []:
            code, rx, label = _EXPR_ERRORS[kind]
            if not rx.fullmatch(obj["expr"]):
                raise validation_error(
                    code, f"{entry_key(kind, obj['name'])}: {label} expr {obj['expr']!r} "
                    "is outside the closed grammar"
                )
            columns.update(expr_columns(obj["expr"]))
    for kind in ("metrics", "filters"):
        for obj in table.get(kind) or []:
            code, rx, label = _EXPR_ERRORS[kind]
            path = entry_key(kind, obj["name"])
            if not rx.fullmatch(obj["expr"]):
                raise validation_error(
                    code, f"{path}: {label} expr {obj['expr']!r} is outside the closed grammar"
                )
            if kind == "metrics":
                unknown = [c for c in expr_columns(obj["expr"]) if c not in columns]
                if unknown:
                    raise validation_error(
                        code,
                        f"{path}: column {unknown[0]!r} is not a fact or dimension column of the table",
                    )


def _check_sidecar(model: SemanticModel, sidecar: dict[str, Any], model_name: str) -> None:
    if sidecar.get("model") != model_name:
        raise validation_error(
            "PROVENANCE_MISMATCH",
            f"sidecar model is {sidecar.get('model')!r}, expected {model_name!r}",
        )
    entries: dict[str, Any] = sidecar.get("entries") or {}
    table = model.tables[0].model_dump(by_alias=True, exclude_unset=True)
    seen: set[str] = set()
    for kind in _CHECKED_KINDS:
        for obj in table.get(kind) or []:
            key = entry_key(kind, obj["name"])
            seen.add(key)
            entry = entries.get(key)
            if entry is None:
                raise validation_error("PROVENANCE_MISMATCH", f"{key}: no sidecar entry")
            digest = entry.get("expr_sha256")
            if digest is None:
                raise validation_error("PROVENANCE_MISMATCH", f"{key}: sidecar entry has no expr_sha256")
            if digest != expr_sha256(obj["expr"]):
                raise validation_error(
                    "PROVENANCE_MISMATCH",
                    f"{key}: expr_sha256 does not match the YAML expr (edited by hand?)",
                )
    extra = sorted(set(entries) - seen)
    if extra:
        raise validation_error(
            "PROVENANCE_MISMATCH", f"{extra[0]}: sidecar entry has no object in the YAML"
        )
    for vq_name, rec in (sidecar.get("verified_queries") or {}).items():
        digests = (rec or {}).get("definition_digests")
        if digests is None:  # required in every record (§4.2, pre-Sprint-4, D398)
            raise validation_error(
                "PROVENANCE_MISMATCH",
                f"verified_queries[{vq_name}]: definition_digests is required "
                "(it names what changed when the query goes STALE)",
            )
        allowed = {"base_table", "models_digest", *_query_names((rec or {}).get("semantic_query"))}
        stray = sorted(set(digests) - allowed)
        if stray:
            raise validation_error(
                "PROVENANCE_MISMATCH",
                f"verified_queries[{vq_name}].definition_digests[{stray[0]}]: not base_table, "
                "models_digest or a definition the query references",
            )
    yaml_vqs = {v.name for v in (model.verified_queries or [])}
    side_vqs = set((sidecar.get("verified_queries") or {}).keys())
    if yaml_vqs != side_vqs:
        diff = sorted(yaml_vqs ^ side_vqs)
        raise validation_error(
            "PROVENANCE_MISMATCH", f"verified_queries[{diff[0]}]: present in only one of YAML and sidecar"
        )


def _check_registered_view(
    model_dict: dict[str, Any], sidecar_dict: dict[str, Any], semantic_dir: Path
) -> None:
    """§5.13 rules 2/3 over the registered model's closure and the build-validity invariant
    (rule 5): a view model validates only when every model in its closure builds. Table
    models are untouched."""
    table = (model_dict.get("tables") or [{}])[0]
    base = table.get("base_table") or {}
    if str(base.get("database", "")).upper() != "VIEWS" or str(base.get("schema", "")).upper() != "MAIN":
        return
    project_dir = Path(semantic_dir).parent
    config = load_config(project_dir / CONFIG_FILENAME)
    rill_dir = rill_dir_for_config(config.base_dir, config.rill.project_dir)
    check_closure(rill_dir, base.get("table"), config.engines.duckdb.rill_models)
    engine = DuckDBEngine(config)
    try:
        # The engine records a model that rule 2/3 or view creation failed as unavailable
        # (D431); ``data_alias`` raises that recorded error, so validate never reports OK
        # for a view model whose graph does not build.
        engine.data_alias(base.get("database"), base.get("schema"), base.get("table"))
    finally:
        engine.close()


def validate(model_name: str, semantic_dir: Path) -> None:
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    model_dict = load_model_dict(str(model_path))
    sidecar_dict = load_sidecar_dict(sidecar_path)
    try:
        validate_model(model_dict, sidecar_dict, model_name)
        _check_registered_view(model_dict, sidecar_dict, semantic_dir)
    except AgentBiError as exc:
        raise AgentBiError(exc.code, f"{model_path}: {exc.message}", exc.exit_code) from exc
