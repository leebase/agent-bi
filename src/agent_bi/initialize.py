"""``init``: attach, scan (create or refresh), validate, compile-rill (architecture §5.2
rule 4, §6, §5.14, sprint-plan Sprint 3 task 3).

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


def _insert_text(text: str, key: str, make_line) -> str | None:
    """Insert one line at the end of the block ``key:`` found under ``duckdb:``; ``None`` when
    the file's layout is not the plain block form (the caller then re-dumps)."""
    lines = text.split("\n")
    duck = next(
        (i for i, ln in enumerate(lines) if re.match(r"^\s*duckdb:\s*(#.*)?$", ln)), None
    )
    if duck is None:
        return None
    pat = re.compile(rf"^(\s*){key}:\s*(#.*)?$")
    idx = next((i for i in range(duck + 1, len(lines)) if pat.match(lines[i])), None)
    if idx is None:
        return None
    last, first_indent = _block_end(lines, idx)
    if first_indent is None:
        return None
    lines.insert(last + 1, make_line(" " * first_indent))
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


@dataclass
class InitResult:
    published: list[str]
    manifest: str
    alias: str
    reused_attach: bool
    created: bool
    rill_dir: str


def run_init(
    config_path: Path, sqlite_arg: str, table: str, model_name: str, cwd: Path | None = None
) -> InitResult:
    cwd = cwd or Path.cwd()
    if not MODEL_NAME_RE.match(model_name):
        raise validation_error(
            "INVALID_MODEL_NAME", f"model name {model_name!r} must match ^[a-z][a-z0-9_]*$"
        )
    project_dir = config_path.resolve().parent
    plan = plan_config(config_path, sqlite_arg, model_name, cwd)
    config = parse_config_text(
        plan.new_text if plan.new_text is not None else config_path.read_text(encoding="utf-8"),
        config_path,
    )
    relation = f"{plan.database}.{SCHEMA}.{table}"
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
        PurePosixPath(config.rill.project_dir).as_posix(),
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
