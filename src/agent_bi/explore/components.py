"""Saved-chart components and canvases under ``rill/`` (architecture §4.7 ``published``, §5.4, §5.10).

A saved component is ``rill/components/<name>.yaml`` whose first three lines are the comment
annotations ``# agent_bi_query_hash``, ``# agent_bi_semantic_digest``, ``# agent_bi_sql_sha256``
(Rill rejects unknown top-level keys, Sprint 0 Q6). ``published`` and ``W_COMPONENT_DIVERGED``
are computed here, from the files as they are *now*, at every ``ask`` / ``validate``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from agent_bi.hashing import sha256_text
from agent_bi.targets.rill import _y

ANNOTATIONS = ("agent_bi_query_hash", "agent_bi_semantic_digest", "agent_bi_sql_sha256")
_ANNOT_RE = re.compile(r"^# (agent_bi_[a-z0-9_]+): (sha256:[0-9a-f]{64})\s*$")
GENERATED_PREFIX = "# agent-bi: generated"


def rill_rel(config: Any) -> str:
    return PurePosixPath(config.rill.project_dir).as_posix()


def render_component(
    name: str, model: str, query_hash: str, semantic_digest: str, metrics_sql: str,
    vega_spec: dict[str, Any],
) -> str:
    """The component file text (§5.4 saved-answer row, §5.10 step 2). Deterministic."""
    spec = json.dumps(vega_spec, separators=(",", ":"), ensure_ascii=False)
    return (
        f"# agent_bi_query_hash: {query_hash}\n"
        f"# agent_bi_semantic_digest: {semantic_digest}\n"
        f"# agent_bi_sql_sha256: {sha256_text(metrics_sql)}\n"
        "type: component\n"
        f"display_name: {_y(name)}\n"
        "custom_chart:\n"
        f"  metrics_view: {_y(model)}\n"
        "  metrics_sql:\n"
        f"    - {_y(metrics_sql)}\n"
        f"  vega_spec: {_y(spec)}\n"
    )


@dataclass(frozen=True)
class Component:
    name: str
    path: str  # project-relative, POSIX
    annotations: dict[str, str]
    metrics_view: str | None
    metrics_sql: str | None  # the current text of metrics_sql[0]

    @property
    def query_hash(self) -> str | None:
        return self.annotations.get("agent_bi_query_hash")

    @property
    def diverged(self) -> bool:
        """The component's current ``metrics_sql`` no longer matches its ``agent_bi_sql_sha256``
        annotation: an operator-edited chart (§4.7)."""
        want = self.annotations.get("agent_bi_sql_sha256")
        return want is None or self.metrics_sql is None or sha256_text(self.metrics_sql) != want


def parse_annotations(text: str) -> dict[str, str]:
    """The ``agent_bi_*`` annotations among the file's leading comment lines."""
    out: dict[str, str] = {}
    for line in text.split("\n"):
        if not line.startswith("#"):
            break
        m = _ANNOT_RE.match(line)
        if m and m.group(1) in ANNOTATIONS:
            out[m.group(1)] = m.group(2)
    return out


def read_component(path: Path, rel: str) -> Component | None:
    """``None`` when the file is not an agent-bi saved component (no annotations)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    ann = parse_annotations(text)
    if not ann:
        return None
    sql = view = None
    try:
        doc = yaml.safe_load(text)
        chart = (doc or {}).get("custom_chart") or {}
        view = chart.get("metrics_view")
        sqls = chart.get("metrics_sql")
        if isinstance(sqls, list) and sqls and isinstance(sqls[0], str):
            sql = sqls[0]
        elif isinstance(sqls, str):
            sql = sqls
    except (yaml.YAMLError, AttributeError):
        pass
    return Component(path.stem, rel, ann, view, sql)


def saved_components(project_dir: Path, config: Any) -> list[Component]:
    rel = rill_rel(config)
    out: list[Component] = []
    for p in sorted((Path(project_dir) / rel / "components").glob("*.yaml")):
        c = read_component(p, f"{rel}/components/{p.name}")
        if c is not None:
            out.append(c)
    return out


# -- canvases --------------------------------------------------------------------------------


def operator_canvases(project_dir: Path, config: Any) -> list[tuple[str, dict[str, Any]]]:
    """``(name, parsed yaml)`` of every operator-owned canvas: a ``type: canvas`` file under
    ``rill/dashboards/`` that does not begin with the generated header (ADR-009)."""
    out: list[tuple[str, dict[str, Any]]] = []
    for p in sorted((Path(project_dir) / rill_rel(config) / "dashboards").glob("*.yaml")):
        try:
            text = p.read_text(encoding="utf-8")
            doc = yaml.safe_load(text)
        except (OSError, yaml.YAMLError):
            continue
        if text.startswith(GENERATED_PREFIX) or not isinstance(doc, dict) or doc.get("type") != "canvas":
            continue
        out.append((p.stem, doc))
    return out


def canvas_scope_problem(doc: dict[str, Any]) -> str | None:
    """Why a canvas does not show the verified view (ADR-008): its default time range is not
    ``inf``, or it declares default or required filters. ``None`` when it qualifies. Re-checked
    at every ``ask`` and ``save`` (B-029)."""
    defaults = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
    time_range = defaults.get("time_range")
    if time_range != "inf":
        return f"defaults.time_range is {time_range!r}, not 'inf'"
    if defaults.get("filters"):
        return "it declares default filters (defaults.filters)"
    filters = doc.get("filters") if isinstance(doc.get("filters"), dict) else {}
    if filters.get("required"):
        return "it declares required filters (filters.required)"
    return None


def canvas_components(doc: dict[str, Any]) -> list[str]:
    """Names of the saved components a canvas references (plain rows and tab-group rows)."""
    names: list[str] = []

    def walk(rows: Any) -> None:
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            for item in row.get("items") or []:
                if isinstance(item, dict) and isinstance(item.get("component"), str):
                    names.append(item["component"])
            for tab in row.get("tabs") or []:
                if isinstance(tab, dict):
                    walk(tab.get("rows"))

    walk(doc.get("rows"))
    return names


def is_published(
    project_dir: Path, config: Any, query_hash: str, semantic_digest: str
) -> bool:
    """§4.7: some component carries this ``query_hash``, its ``agent_bi_semantic_digest`` equals
    the current digest, its ``agent_bi_sql_sha256`` equals the sha256 of its current
    ``metrics_sql``, and an operator-owned canvas that qualifies (default range ``inf``, no
    default or required filters) references it."""
    live = {
        c.name for c in saved_components(project_dir, config)
        if c.query_hash == query_hash
        and c.annotations.get("agent_bi_semantic_digest") == semantic_digest
        and not c.diverged
    }
    if not live:
        return False
    for _name, doc in operator_canvases(project_dir, config):
        if canvas_scope_problem(doc) is None and live & set(canvas_components(doc)):
            return True
    return False


def component_warnings(project_dir: Path, rill_project_dir: str, model_name: str) -> list[str]:
    """``W_COMPONENT_DIVERGED <path>`` for every saved component of ``model_name`` whose current
    ``metrics_sql`` no longer matches its ``agent_bi_sql_sha256`` annotation (§4.7, §6)."""

    class _Cfg:
        class rill:
            project_dir = rill_project_dir

    return [
        f"W_COMPONENT_DIVERGED {c.path}: metrics_sql no longer matches its agent_bi_sql_sha256 "
        "annotation (an operator-edited chart); it does not count as published"
        for c in saved_components(project_dir, _Cfg)
        if c.metrics_view == model_name and c.diverged
    ]
