"""``enrich --apply`` (architecture §4.5): one whole-patch validated write transaction.

Only four operations exist. The whole patch is parsed with ``extra="forbid"`` models,
checked for SQL-shaped keys anywhere, and every precondition (paths, grammars, synonym
uniqueness, confirmed-entry protection) is satisfied in memory before a single file is
written; a later refusal leaves every byte of the earlier operations unchanged. No
operation deletes anything. ``add_metric`` creates a metric or rewrites the ``expr`` and
``description`` of the metric named by ``value.name`` (Sprint 5 supervisor reconciliation,
2026-10-15), preserving its synonyms and other fields; non-metric name collisions are
``E_NAME_COLLISION``. ``propose_query`` writes only ``queries/<name>.json``. a new metric
name equal to any synonym is ``E_SYNONYM_NOT_UNIQUE``.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_bi import fs_txn
from agent_bi.errors import validation_error
from agent_bi.explore.save import _saved_query_doc
from agent_bi.hashing import expr_sha256, semantic_digest
from agent_bi.query.compile import METRIC_RE, expr_columns
from agent_bi.query.contract import parse_query, query_hash, validate_against_model
from agent_bi.semantic.model import dump_yaml, load_model_dict
from agent_bi.semantic.names import RESERVED_NAMES
from agent_bi.semantic.provenance import load_sidecar_dict
from agent_bi.semantic.scan import paths_for, utc_now
from agent_bi.semantic.validate import validate, validate_model

SCHEMA_VERSION = "agent-bi-enrichment-patch/1.0"
MAX_PATCH_BYTES = 1_000_000  # §11: untrusted input is size-capped (1 MB)
OBJ_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ENTRY_PATH_RE = re.compile(
    r"^tables\[0\]\.(dimensions|time_dimensions|facts|metrics|filters)"
    r"\[([A-Za-z_][A-Za-z0-9_]*)\]$"
)
_KINDS = ("dimensions", "time_dimensions", "facts", "metrics", "filters")
_SQL_KEYS = ("sql", "expected_sql")
_OBJECT_KEYS: dict[str, tuple[str, ...]] = {
    "dimensions": ("name", "synonyms", "description", "expr", "data_type"),
    "time_dimensions": ("name", "synonyms", "description", "expr", "data_type"),
    "facts": ("name", "synonyms", "description", "access_modifier", "expr", "data_type"),
    "metrics": ("name", "synonyms", "description", "access_modifier", "expr"),
    "filters": ("name", "synonyms", "description", "expr"),
}


# --------------------------------------------------------------------------- patch schema


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetDescriptionOp(_Strict):
    op: Literal["set_description"]
    path: str
    value: str


class AddSynonymsOp(_Strict):
    op: Literal["add_synonyms"]
    path: str
    value: list[str]


class MetricValue(_Strict):
    name: str
    expr: str
    description: str | None = None


class AddMetricOp(_Strict):
    op: Literal["add_metric"]
    path: str
    value: MetricValue


class ProposedQuery(_Strict):
    name: str
    question: str = Field(min_length=1)
    semantic_query: dict[str, Any]


class ProposeQueryOp(_Strict):
    op: Literal["propose_query"]
    path: str = ""
    value: ProposedQuery


Operation = Annotated[
    SetDescriptionOp | AddSynonymsOp | AddMetricOp | ProposeQueryOp,
    Field(discriminator="op"),
]


class EnrichmentPatch(_Strict):
    schema_version: Literal["agent-bi-enrichment-patch/1.0"]
    patch_id: str
    model: str
    proposed_by: str = Field(min_length=1)
    proposed_at: str
    operations: list[Operation] = Field(min_length=1)


# ------------------------------------------------------------------------------ loading


def _format_validation_error(exc: ValidationError) -> str:
    problems: list[str] = []
    for error in exc.errors():
        loc = error["loc"]
        index = loc[1] if len(loc) > 1 and isinstance(loc[1], int) else None
        where = f"operation {index}" if index is not None else "patch"
        rest = ".".join(str(part) for part in loc[2:])
        problems.append(f"{where}{': ' + rest if rest else ''}: {error['msg']}")
    return "; ".join(problems)


def _reject_sql_keys(node: Any, where: str) -> None:
    """No key named ``sql``/``expected_sql`` may appear anywhere in a patch, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in _SQL_KEYS:
                raise validation_error(
                    "PATCH_REJECTED",
                    f"{where}: key {key!r} is not permitted in an enrichment patch; "
                    "a patch carries no SQL and no expected answer",
                )
            _reject_sql_keys(value, where)
    elif isinstance(node, list):
        for item in node:
            _reject_sql_keys(item, where)


def _canonical_uuid4(patch_id: str) -> str:
    try:
        parsed = uuid.UUID(patch_id)
    except ValueError as exc:
        raise validation_error(
            "PATCH_REJECTED", f"patch_id {patch_id!r} is not a UUID4"
        ) from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise validation_error("PATCH_REJECTED", f"patch_id {patch_id!r} is not a UUID4")
    return str(parsed)


def load_patch(path: str | Path) -> tuple[EnrichmentPatch, str]:
    """Parse and validate the patch envelope; return ``(patch, canonical patch id)``."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise validation_error(
            "PATCH_REJECTED", f"--apply {path}: cannot read the patch: {exc}"
        ) from exc
    if len(raw) > MAX_PATCH_BYTES:
        raise validation_error(
            "PATCH_REJECTED",
            f"--apply {path}: patch is {len(raw)} bytes; the cap is {MAX_PATCH_BYTES}",
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise validation_error(
            "PATCH_REJECTED", f"--apply {path}: not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise validation_error(
            "PATCH_REJECTED", f"--apply {path}: top level must be a JSON object"
        )
    _reject_sql_keys(data, "patch")
    try:
        patch = EnrichmentPatch.model_validate(data)
    except ValidationError as exc:
        raise validation_error(
            "PATCH_REJECTED", f"invalid enrichment patch: {_format_validation_error(exc)}"
        ) from exc
    patch_id = _canonical_uuid4(patch.patch_id)
    try:
        datetime.fromisoformat(patch.proposed_at)
    except ValueError as exc:
        raise validation_error(
            "PATCH_REJECTED", f"proposed_at {patch.proposed_at!r} is not an ISO-8601 timestamp"
        ) from exc
    return patch, patch_id


# ------------------------------------------------------------------------- model helpers


def _object(table: dict[str, Any], kind: str, name: str) -> dict[str, Any] | None:
    return next((o for o in table.get(kind) or [] if o.get("name") == name), None)


def _all_columns(table: dict[str, Any]) -> set[str]:
    columns: set[str] = set()
    for kind in ("dimensions", "time_dimensions", "facts"):
        for obj in table.get(kind) or []:
            columns.update(expr_columns(obj.get("expr", "")))
    return columns


def _set_ordered(obj: dict[str, Any], kind: str, key: str, value: Any) -> None:
    """Set ``key``, inserting a missing key at its spec position (never reordering the rest)."""
    if key in obj:
        obj[key] = value
        return
    order = _OBJECT_KEYS.get(kind, ())
    rank = order.index(key) if key in order else len(order)
    rebuilt: dict[str, Any] = {}
    placed = False
    for k, v in obj.items():
        if not placed and k in order and order.index(k) > rank:
            rebuilt[key] = value
            placed = True
        rebuilt[k] = v
    if not placed:
        rebuilt[key] = value
    obj.clear()
    obj.update(rebuilt)


def _parse_entry_path(index: int, path: str) -> tuple[str, str]:
    match = ENTRY_PATH_RE.fullmatch(path)
    if match is None:
        raise validation_error(
            "PATCH_REJECTED",
            f"operation {index}: path {path!r} is not a bounded "
            "tables[0].<kind>[<name>] entry path",
        )
    return match.group(1), match.group(2)


def _require_exact_override(
    index: int, leaf: str, entries: dict[str, Any], allowed: set[str]
) -> None:
    entry = entries.get(leaf)
    if isinstance(entry, dict) and entry.get("status") == "confirmed" and leaf not in allowed:
        raise validation_error(
            "PATCH_TOUCHES_CONFIRMED",
            f"operation {index}: {leaf} is confirmed; pass "
            f"--allow-confirmed {leaf} (the exact leaf) to rewrite it",
        )


def _check_metric_expr(index: int, expr: str, table: dict[str, Any]) -> None:
    if not METRIC_RE.fullmatch(expr):
        raise validation_error(
            "PATCH_REJECTED",
            f"operation {index}: metric expr {expr!r} is outside the metric grammar "
            "(§5.3.2); the whole patch is refused",
        )
    valid = _all_columns(table)
    unknown = [column for column in expr_columns(expr) if column not in valid]
    if unknown:
        raise validation_error(
            "PATCH_REJECTED",
            f"operation {index}: metric expr {expr!r} names {unknown[0]!r}, which is not a "
            "fact or dimension column of the table",
        )


def _check_synonyms(index: int, target_leaf: str, values: list[str], table: dict[str, Any]) -> None:
    names: dict[str, str] = {}
    synonyms: dict[str, str] = {}
    for kind in _KINDS:
        for obj in table.get(kind) or []:
            leaf = f"tables[0].{kind}[{obj['name']}]"
            names.setdefault(obj["name"], leaf)
            for synonym in obj.get("synonyms") or []:
                synonyms.setdefault(synonym, leaf)
    added_here: set[str] = set()
    for value in values:
        if not value.strip():
            raise validation_error(
                "PATCH_REJECTED", f"operation {index}: synonym {value!r} is empty"
            )
        if value in added_here:
            raise validation_error(
                "SYNONYM_NOT_UNIQUE",
                f"operation {index}: synonym {value!r} is listed twice; add_synonyms at "
                f"{target_leaf} would collide",
            )
        if value in RESERVED_NAMES:
            raise validation_error(
                "SYNONYM_NOT_UNIQUE",
                f"operation {index}: synonym {value!r} is reserved ({RESERVED_NAMES[value]}); "
                f"add_synonyms at {target_leaf} would collide",
            )
        if value in synonyms:
            raise validation_error(
                "SYNONYM_NOT_UNIQUE",
                f"operation {index}: synonym {value!r} is already used by {synonyms[value]}; "
                f"add_synonyms at {target_leaf} would collide",
            )
        if value in names:
            raise validation_error(
                "SYNONYM_NOT_UNIQUE",
                f"operation {index}: synonym {value!r} is already the name of {names[value]}; "
                f"add_synonyms at {target_leaf} would collide",
            )
        added_here.add(value)
        synonyms[value] = target_leaf


def _name_owner(table: dict[str, Any], name: str) -> str | None:
    for kind in _KINDS:
        for obj in table.get(kind) or []:
            if obj.get("name") == name:
                return f"tables[0].{kind}[{name}]"
    return None


def _synonym_owner(table: dict[str, Any], name: str) -> str | None:
    for kind in _KINDS:
        for obj in table.get(kind) or []:
            if name in (obj.get("synonyms") or []):
                return f"tables[0].{kind}[{obj['name']}]"
    return None


def _mark_inferred(
    entry: dict[str, Any], origin: str, by: str, at: str, expr: str | None = None
) -> None:
    entry["status"] = "inferred"
    entry["origin"] = origin
    entry["at"] = at
    entry["by"] = by
    if expr is not None:
        entry["expr_sha256"] = expr_sha256(expr)


# -------------------------------------------------------------------------------- apply


def apply_patch(
    model_name: str,
    patch_path: str | Path,
    allow_confirmed: list[str] | tuple[str, ...],
    project_dir: Path,
    now: str | None = None,
) -> list[str]:
    """Validate and apply ``patch_path`` to ``model_name`` as one write transaction; return
    the published repo-relative paths."""
    project_dir = Path(project_dir)
    patch, patch_id = load_patch(patch_path)
    if patch.model != model_name:
        raise validation_error(
            "PATCH_REJECTED",
            f"patch model {patch.model!r} does not match --model {model_name!r}",
        )
    semantic_dir = project_dir / "semantic"
    validate(model_name, semantic_dir)
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    model = load_model_dict(str(model_path))
    sidecar = load_sidecar_dict(sidecar_path)
    tables = model.get("tables") or []
    if len(tables) != 1:
        raise validation_error(
            "UNSUPPORTED_MULTI_TABLE", f"tables: expected exactly one table, found {len(tables)}"
        )
    table = tables[0]
    entries = sidecar.setdefault("entries", {})
    origin = f"enrich:{patch_id}"
    at = now or utc_now()
    allowed = set(allow_confirmed)
    changed_model = False
    changed_sidecar = False
    proposals: list[tuple[int, ProposedQuery]] = []
    outputs: dict[str, str] = {}

    for index, op in enumerate(patch.operations):
        if isinstance(op, SetDescriptionOp):
            kind, name = _parse_entry_path(index, op.path)
            obj = _object(table, kind, name)
            if obj is None:
                raise validation_error(
                    "UNKNOWN_FIELD", f"operation {index}: {op.path} does not exist in {model_name}"
                )
            _require_exact_override(index, op.path, entries, allowed)
            _set_ordered(obj, kind, "description", op.value)
            _mark_inferred(entries[op.path], origin, patch.proposed_by, at)
            changed_model = changed_sidecar = True
        elif isinstance(op, AddSynonymsOp):
            kind, name = _parse_entry_path(index, op.path)
            obj = _object(table, kind, name)
            if obj is None:
                raise validation_error(
                    "UNKNOWN_FIELD", f"operation {index}: {op.path} does not exist in {model_name}"
                )
            _require_exact_override(index, op.path, entries, allowed)
            if not op.value:
                raise validation_error(
                    "PATCH_REJECTED",
                    f"operation {index}: add_synonyms needs at least one synonym",
                )
            _check_synonyms(index, op.path, op.value, table)
            current = list(obj.get("synonyms") or [])
            current.extend(op.value)
            _set_ordered(obj, kind, "synonyms", current)
            _mark_inferred(entries[op.path], origin, patch.proposed_by, at)
            changed_model = changed_sidecar = True
        elif isinstance(op, AddMetricOp):
            if op.path != "tables[0]":
                raise validation_error(
                    "PATCH_REJECTED",
                    f"operation {index}: add_metric path must be 'tables[0]', got {op.path!r}",
                )
            value = op.value
            if not OBJ_NAME_RE.fullmatch(value.name):
                raise validation_error(
                    "PATCH_REJECTED",
                    f"operation {index}: metric name {value.name!r} must match ^[a-z][a-z0-9_]*$",
                )
            leaf = f"tables[0].metrics[{value.name}]"
            existing = _object(table, "metrics", value.name)
            if existing is None:
                owner = _name_owner(table, value.name)
                if owner is not None:
                    raise validation_error(
                        "NAME_COLLISION",
                        f"operation {index}: name {value.name!r} is already used by {owner}; "
                        "add_metric may only create a metric or rewrite an existing metric",
                    )
                if value.name in RESERVED_NAMES:
                    raise validation_error(
                        "NAME_COLLISION",
                        f"operation {index}: name {value.name!r} is reserved "
                        f"({RESERVED_NAMES[value.name]})",
                    )
                synonym_owner = _synonym_owner(table, value.name)
                if synonym_owner is not None:
                    raise validation_error(
                        "SYNONYM_NOT_UNIQUE",
                        f"operation {index}: name {value.name!r} is already a synonym of "
                        f"{synonym_owner}; add_metric at {leaf} would collide",
                    )
                _check_metric_expr(index, value.expr, table)
                metric: dict[str, Any] = {"name": value.name, "synonyms": []}
                if "description" in value.model_fields_set:
                    metric["description"] = value.description
                metric["expr"] = value.expr
                table.setdefault("metrics", []).append(metric)
                entry: dict[str, Any] = {}
                entries[leaf] = entry
                _mark_inferred(entry, origin, patch.proposed_by, at, expr=value.expr)
            else:
                _require_exact_override(index, leaf, entries, allowed)
                _check_metric_expr(index, value.expr, table)
                if "description" in value.model_fields_set:
                    _set_ordered(existing, "metrics", "description", value.description)
                existing["expr"] = value.expr
                _mark_inferred(entries[leaf], origin, patch.proposed_by, at, expr=value.expr)
            changed_model = changed_sidecar = True
        elif isinstance(op, ProposeQueryOp):
            if op.path:
                raise validation_error(
                    "PATCH_REJECTED",
                    f"operation {index}: propose_query path must be \"\" (it writes "
                    "queries/<name>.json)",
                )
            proposals.append((index, op.value))

    if changed_model or changed_sidecar:
        validate_model(model, sidecar, model_name)  # the patched pair must be consistent
        outputs[f"semantic/{model_name}.yaml"] = dump_yaml(model)
        outputs[f"semantic/{model_name}.provenance.yaml"] = dump_yaml(sidecar)

    proposed_names: set[str] = set()
    for index, value in proposals:
        if not OBJ_NAME_RE.fullmatch(value.name):
            raise validation_error(
                "PATCH_REJECTED",
                f"operation {index}: query name {value.name!r} must match ^[a-z][a-z0-9_]*$",
            )
        if value.name in proposed_names:
            raise validation_error(
                "PATCH_REJECTED",
                f"operation {index}: propose_query name {value.name!r} appears twice in this patch",
            )
        proposed_names.add(value.name)
        query_rel = f"queries/{value.name}.json"
        if (project_dir / query_rel).exists() or value.name in (sidecar.get("verified_queries") or {}):
            raise validation_error(
                "NAME_EXISTS",
                f"operation {index}: {query_rel} already exists; a proposed query never "
                "overwrites an existing name (ADR-009)",
            )
        q = validate_against_model(
            parse_query(value.semantic_query, f"operation {index}: propose_query semantic_query"),
            model,
        )
        canonical = q.canonical()
        outputs[query_rel] = _saved_query_doc(
            value.name, "proposed", value.question, query_hash(canonical),
            semantic_digest(canonical, model), canonical.canonical_dict(),
        )

    return fs_txn.publish(project_dir, outputs)
