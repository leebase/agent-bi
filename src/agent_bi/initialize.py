"""``init``: attach, scan (create or refresh), validate, compile-rill (architecture §5.2
rule 4, §6, §5.14, sprint-plan Sprint 3 task 3).

The table form scans ``<MODEL>.MAIN.<table>``. The view form (``--rill-model <name>``,
§5.13 rule 1) registers the operator's ``rill/models/<name>.sql``, maps
``VIEWS.MAIN`` to ``memory.main``, attaches the model's SQLite file and scans
``VIEWS.MAIN.<name>``; the operator file is never written or regenerated.

The attach-alias rule (D394): if ``agent-bi.yaml`` already attaches the same file (compared
after resolving both paths to absolute), the entry's alias and its ``relation_map`` entry are
reused and nothing is added; otherwise ``{path: <file as given>, alias: <model>, type: sqlite}``
and ``"<MODEL>.MAIN": "<model>.main"`` are appended. The relation passed to scan is always
``<DATABASE>.MAIN.<table>`` from that entry.

Every output (config, semantic model, sidecar, Rill files, manifest) is computed in memory
and published in one write transaction.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from agent_bi import fs_txn
from agent_bi.compile_rill import plan_compile_rill
from agent_bi.config import parse_config_text, substitute_env
from agent_bi.engines import DuckDBEngine
from agent_bi.errors import validation_error
from agent_bi.rill_models import check_closure, operator_model_names
from agent_bi.semantic.scan import MODEL_NAME_RE, paths_for, plan_scan
from agent_bi.semantic.validate import validate_model
from agent_bi.targets.rill import _y

SCHEMA = "MAIN"


@dataclass
class ConfigPlan:
    alias: str
    database: str
    reused: bool
    new_text: str | None  # None: agent-bi.yaml is unchanged
    raw: dict[str, Any] = field(default_factory=dict)


def _abs(path: str, base: Path) -> Path:
    p = Path(path)
    return (p if p.is_absolute() else base / p).resolve()


def _block_end(lines: list[str], key_idx: int) -> tuple[int, int | None]:
    """(index of the last line of the block under ``lines[key_idx]``, indent of its first
    content line). The block is every following line indented deeper than the key."""
    key_indent = len(lines[key_idx]) - len(lines[key_idx].lstrip())
    last, first_indent = key_idx, None
    for i in range(key_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= key_indent:
            break
        if first_indent is None:
            first_indent = indent
        last = i
    return last, first_indent


def _duckdb_key_index(lines: list[str], key: str) -> int | None:
    duck = next(
        (i for i, ln in enumerate(lines) if re.match(r"^\s*duckdb:\s*(#.*)?$", ln)), None
    )
    if duck is None:
        return None
    pat = re.compile(rf"^(\s*){key}:\s*(#.*)?$")
    return next((i for i in range(duck + 1, len(lines)) if pat.match(lines[i])), None)


def _insert_text(text: str, key: str, make_line) -> str | None:
    """Insert one line at the end of the block ``key:`` found under ``duckdb:``; ``None`` when
    the file's layout is not the plain block form (the caller then re-dumps)."""
    lines = text.split("\n")
    idx = _duckdb_key_index(lines, key)
    if idx is None:
        return None
    last, first_indent = _block_end(lines, idx)
    if first_indent is None:
        return None
    lines.insert(last + 1, make_line(" " * first_indent))
    return "\n".join(lines)


def _insert_block_after(text: str, anchor_key: str, key: str, make_lines) -> str | None:
    """Insert a new ``key:`` block after the block of ``anchor_key`` under ``duckdb:``; the
    key takes the anchor's indent and its content lines the anchor's content indent.
    ``make_lines(indent)`` returns the content lines to add."""
    lines = text.split("\n")
    anchor = _duckdb_key_index(lines, anchor_key)
    if anchor is None:
        return None
    last, first_indent = _block_end(lines, anchor)
    if first_indent is None or first_indent < 2:
        return None
    lines[last + 1:last + 1] = [f"{' ' * (first_indent - 2)}{key}:"] + list(
        make_lines(" " * first_indent)
    )
    return "\n".join(lines)


def plan_config(config_path: Path, sqlite_arg: str, model_name: str, cwd: Path) -> ConfigPlan:
    """Decide the attach alias for ``sqlite_arg`` and the (possibly unchanged) config text."""
    text = config_path.read_text(encoding="utf-8")
    base = config_path.resolve().parent
    raw = yaml.safe_load(text) or {}
    duck = ((raw.get("engines") or {}).get("duckdb")) or {}
    attaches = duck.get("attach") or []
    relation_map = duck.get("relation_map") or {}
    wanted = _abs(sqlite_arg, cwd)
    for entry in attaches:
        if _abs(substitute_env(entry["path"]), base) == wanted:
            alias = entry["alias"]
            for key, value in relation_map.items():
                if str(value).lower() == f"{alias}.main".lower():
                    return ConfigPlan(alias, key.partition(".")[0].upper(), True, None, raw)
            raise validation_error(
                "RELATION_NOT_MAPPED",
                f"{config_path.name} attaches {sqlite_arg} as {alias!r} but relation_map has no "
                f"entry for {alias}.main",
            )
    alias, database = model_name, model_name.upper()
    if any(e.get("alias") == alias for e in attaches):
        raise validation_error(
            "CONFIG_INVALID",
            f"{config_path.name}: alias {alias!r} is already attached to a different file",
        )
    map_key = f"{database}.{SCHEMA}"
    if map_key in relation_map:
        raise validation_error(
            "CONFIG_INVALID", f"{config_path.name}: relation_map already has {map_key!r}"
        )
    expected = copy.deepcopy(raw)
    eng = expected.setdefault("engines", {}).setdefault("duckdb", {})
    eng.setdefault("attach", []).append({"path": sqlite_arg, "alias": alias, "type": "sqlite"})
    eng.setdefault("relation_map", {})[map_key] = f"{alias}.main"
    new_text = _insert_text(
        text, "attach",
        lambda ind: f"{ind}- {{path: {_y(sqlite_arg)}, alias: {alias}, type: sqlite}}",
    )
    if new_text is not None:
        new_text = _insert_text(
            new_text, "relation_map",
            lambda ind: f"{ind}{json.dumps(map_key)}: {json.dumps(f'{alias}.main')}",
        )
    if new_text is None or yaml.safe_load(new_text) != expected:
        new_text = yaml.safe_dump(expected, sort_keys=False)  # layout not editable in place
    return ConfigPlan(alias, database, False, new_text, expected)


def plan_config_view(config_path: Path, rill_model: str) -> ConfigPlan:
    """The view form of ``init``: register ``rill_model``, map ``VIEWS.MAIN`` to
    ``memory.main`` and apply the attach-alias rule to the model's SQLite file (§5.13
    rules 1 and 5). Idempotent: a second run adds nothing and returns ``new_text=None``."""
    text = config_path.read_text(encoding="utf-8")
    base = config_path.resolve().parent
    raw = yaml.safe_load(text) or {}
    duck = ((raw.get("engines") or {}).get("duckdb")) or {}
    attaches = duck.get("attach") or []
    relation_map = duck.get("relation_map") or {}
    rill_cfg = raw.get("rill") or {}
    project_dir_rel = str(rill_cfg.get("project_dir") or "rill")
    rill_dir = _abs(project_dir_rel, base)
    # §5.13 rules 2–4 before any plan: the named model plus its closure over the
    # operator-owned files present in <rill dir>/models/ (generated files excluded).
    closure = check_closure(rill_dir, rill_model, operator_model_names(rill_dir))
    wanted = closure.sqlite_abs

    views_key = "VIEWS.MAIN"
    views_present = False
    for key, value in relation_map.items():
        if key.upper() == views_key:
            views_present = True
            if str(value).lower() != "memory.main":
                raise validation_error(
                    "CONFIG_INVALID",
                    f"{config_path.name}: relation_map {key!r} is {value!r}, expected 'memory.main'",
                )

    alias: str | None = None
    for entry in attaches:
        if _abs(substitute_env(entry["path"]), base) == wanted:
            alias = entry["alias"]
            break
    reused = alias is not None
    map_key = ""
    attach_rel = ""
    if not reused:
        alias = rill_model
        if any(e.get("alias") == alias for e in attaches):
            raise validation_error(
                "CONFIG_INVALID",
                f"{config_path.name}: alias {alias!r} is already attached to a different file",
            )
        map_key = f"{alias.upper()}.{SCHEMA}"
        if any(k.upper() == map_key for k in relation_map):
            raise validation_error(
                "CONFIG_INVALID", f"{config_path.name}: relation_map already has {map_key!r}"
            )
        attach_rel = Path(os.path.relpath(wanted, base)).as_posix()

    names = list(duck.get("rill_models") or [])
    add_names = [n for n in closure.members if n not in names]
    expected = copy.deepcopy(raw)
    eng = expected.setdefault("engines", {}).setdefault("duckdb", {})
    if not views_present:
        eng.setdefault("relation_map", {})[views_key] = "memory.main"
    if not reused:
        eng.setdefault("attach", []).append(
            {"path": attach_rel, "alias": alias, "type": "sqlite"}
        )
        eng.setdefault("relation_map", {})[map_key] = f"{alias}.main"
    if add_names:
        eng.setdefault("rill_models", []).extend(add_names)
    if views_present and reused and not add_names:
        return ConfigPlan(alias or rill_model, "VIEWS", reused, None, raw)

    def edit_attach(t: str) -> str | None:
        return _insert_text(
            t, "attach",
            lambda ind: f"{ind}- {{path: {_y(attach_rel)}, alias: {alias}, type: sqlite}}",
        )

    def edit_views(t: str) -> str | None:
        return _insert_text(
            t, "relation_map",
            lambda ind: f"{ind}{json.dumps(views_key)}: {json.dumps('memory.main')}",
        )

    def edit_alias_map(t: str) -> str | None:
        return _insert_text(
            t, "relation_map",
            lambda ind: f"{ind}{json.dumps(map_key)}: {json.dumps(f'{alias}.main')}",
        )

    def edit_rill_model(t: str) -> str | None:
        out: str | None = t
        for name in add_names:
            if out is None:
                return None
            out = _insert_text(out, "rill_models", lambda ind, n=name: f"{ind}- {n}")
        return out

    def edit_rill_models_block(t: str) -> str | None:
        return _insert_block_after(
            t, "relation_map", "rill_models",
            lambda ind: [f"{ind}- {n}" for n in add_names],
        )

    new_text: str | None = text
    edits = []
    if not reused:
        edits.append(edit_attach)
    if not views_present:
        edits.append(edit_views)
    if not reused:
        edits.append(edit_alias_map)
    if add_names:
        known_key = any(
            re.match(r"^\s*rill_models:\s*(#.*)?$", ln) for ln in text.split("\n")
        )
        edits.append(edit_rill_model if known_key else edit_rill_models_block)
    for edit in edits:
        if new_text is None:
            break
        new_text = edit(new_text)
    if new_text is None or yaml.safe_load(new_text) != expected:
        new_text = yaml.safe_dump(expected, sort_keys=False)  # layout not editable in place
    return ConfigPlan(alias or rill_model, "VIEWS", reused, new_text, expected)


@dataclass
class InitResult:
    published: list[str]
    manifest: str
    alias: str
    reused_attach: bool
    created: bool
    rill_dir: str
    source: str


def run_init(
    config_path: Path, sqlite_arg: str | None = None, table: str | None = None,
    model_name: str | None = None, cwd: Path | None = None, rill_model: str | None = None,
) -> InitResult:
    cwd = cwd or Path.cwd()
    if (sqlite_arg is None) == (rill_model is None):
        raise validation_error(
            "CONFIG_INVALID",
            "init needs exactly one form: --sqlite <file> --table <name> --model <name>, or "
            "--rill-model <name> [--model <name>]",
        )
    if sqlite_arg is not None:
        if not table:
            raise validation_error("CONFIG_INVALID", "init --sqlite needs --table <name>")
        if not model_name:
            raise validation_error("CONFIG_INVALID", "init --sqlite needs --model <name>")
    else:
        if table:
            raise validation_error("CONFIG_INVALID", "init --rill-model cannot be combined with --table")
        model_name = model_name or rill_model
    assert model_name is not None
    if not MODEL_NAME_RE.match(model_name):
        raise validation_error(
            "INVALID_MODEL_NAME", f"model name {model_name!r} must match ^[a-z][a-z0-9_]*$"
        )
    project_dir = config_path.resolve().parent
    if rill_model is not None:
        plan = plan_config_view(config_path, rill_model)
        relation = f"VIEWS.{SCHEMA}.{rill_model}"
        source = rill_model
    else:
        plan = plan_config(config_path, sqlite_arg or "", model_name, cwd)
        relation = f"{plan.database}.{SCHEMA}.{table}"
        source = sqlite_arg or ""
    config = parse_config_text(
        plan.new_text if plan.new_text is not None else config_path.read_text(encoding="utf-8"),
        config_path,
    )
    semantic_dir = project_dir / "semantic"
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    exists = model_path.exists() or sidecar_path.exists()
    if exists:
        _require_same_relation(model_path, relation)
    engine = DuckDBEngine(config)
    try:
        scan_plan = plan_scan(engine, relation, model_name, semantic_dir, refresh=exists)
        validate_model(scan_plan.model, scan_plan.sidecar, model_name)
        rill_outputs, manifest, deletes = plan_compile_rill(
            model_name, scan_plan.model, scan_plan.sidecar,
            scan_plan.model_text.encode("utf-8"), scan_plan.sidecar_text.encode("utf-8"),
            config, engine, project_dir,
        )
    finally:
        engine.close()
    outputs: dict[str, str] = {}
    if plan.new_text is not None:
        outputs[config_path.name] = plan.new_text
    outputs[f"semantic/{model_name}.yaml"] = scan_plan.model_text
    outputs[f"semantic/{model_name}.provenance.yaml"] = scan_plan.sidecar_text
    outputs.update(rill_outputs)
    published = fs_txn.publish(project_dir, outputs, deletes=deletes)
    return InitResult(
        published, manifest, plan.alias, plan.reused, not exists,
        PurePosixPath(config.rill.project_dir).as_posix(), source,
    )


def _require_same_relation(model_path: Path, relation: str) -> None:
    from agent_bi.semantic.model import load_model_dict

    base = (load_model_dict(str(model_path)).get("tables") or [{}])[0].get("base_table") or {}
    existing = f"{base.get('database')}.{base.get('schema')}.{base.get('table')}"
    if existing != relation:
        raise validation_error(
            "MODEL_EXISTS",
            f"{model_path} already models {existing}, not {relation}; choose another --model",
        )
