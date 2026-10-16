"""``canvas fork`` (architecture §6, ADR-009): copy the generated default canvas to an
operator-owned canvas and strip the generated header. The operator's canvas is never
overwritten and ``compile-rill`` never touches it."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from agent_bi import fs_txn
from agent_bi.config import Config
from agent_bi.errors import validation_error
from agent_bi.targets.rill import HEADER_TEXT

CANVAS_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _generated_canvas_names(semantic_dir: Path, model: str) -> set[str]:
    """Dashboard names agent-bi generates and overwrites: ``<m>_explore``, ``<m>_default_canvas``
    for ``model`` and every other model under ``semantic/``."""
    models = {model}
    if semantic_dir.is_dir():
        models |= {
            p.name.removesuffix(".yaml")
            for p in semantic_dir.glob("*.yaml")
            if not p.name.endswith(".provenance.yaml")
        }
    return {f"{m}{suffix}" for m in models for suffix in ("_explore", "_default_canvas")}


def fork_canvas(model: str, name: str, config: Config, project_dir: Path) -> str:
    """Create ``rill/dashboards/<name>.yaml``; return its repo-relative path."""
    if not CANVAS_NAME_RE.match(name):
        raise validation_error(
            "INVALID_CANVAS_NAME", f"canvas name {name!r} must match ^[a-z][a-z0-9_]*$"
        )
    if name in _generated_canvas_names(project_dir / "semantic", model):
        raise validation_error(
            "GENERATED_FILE",
            f"{name!r} is a dashboard compile-rill generates and overwrites; choose another name",
        )
    rill_rel = PurePosixPath(config.rill.project_dir).as_posix()
    src = project_dir / rill_rel / "dashboards" / f"{model}_default_canvas.yaml"
    if not src.is_file():
        raise validation_error(
            "CANVAS_NOT_FOUND", f"{src} does not exist; run `agent-bi compile-rill --model {model}`"
        )
    rel = f"{rill_rel}/dashboards/{name}.yaml"
    if (project_dir / rel).exists():
        raise validation_error(
            "CANVAS_EXISTS", f"{rel} already exists; operator-owned canvases are never overwritten"
        )
    text = src.read_text(encoding="utf-8")
    first, sep, rest = text.partition("\n")
    if sep and first == "# " + HEADER_TEXT.format(model=model):
        text = rest
    fs_txn.publish(project_dir, {rel: text})
    return rel
