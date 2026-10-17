"""``enrich --accept`` (architecture §4.5). ``--apply`` is Sprint 5.

Accepting flips one sidecar entry to ``confirmed``: a human has accepted that definition.
A metric is accepted only when a *current* verified query references it (``E_ACCEPT_UNCOVERED``):
a definition is accepted only when a known answer exists for it as it stands now.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from agent_bi import fs_txn
from agent_bi.errors import validation_error
from agent_bi.query.contract import parse_query
from agent_bi.query.verified import current_verified_queries
from agent_bi.rill_models import (
    current_models_digest,
    registered_names_from_project,
    rill_dir_from_source,
)
from agent_bi.semantic.model import dump_yaml, load_model_dict
from agent_bi.semantic.scan import paths_for
from agent_bi.semantic.validate import validate

_METRIC_PREFIX = "tables[0].metrics["


def accept(model_name: str, path: str, by: str, project_dir: Path) -> str:
    """Flip ``path`` (a sidecar entry key) to ``confirmed`` by ``by``; returns a one-line
    report. Idempotent: an entry already ``confirmed`` is left exactly as it is."""
    if not by or not by.strip():
        raise validation_error("QUERY_INVALID", "enrich --accept needs --by <who> (a person)")
    semantic_dir = Path(project_dir) / "semantic"
    validate(model_name, semantic_dir)
    model_path, sidecar_path = paths_for(semantic_dir, model_name)
    model = load_model_dict(str(model_path))
    # raw YAML in, raw YAML out: the rest of the sidecar is preserved byte for byte
    sidecar: dict[str, Any] = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
    entries: dict[str, Any] = sidecar.get("entries") or {}
    entry = entries.get(path)
    if entry is None:
        raise validation_error(
            "UNKNOWN_FIELD",
            f"unknown sidecar path {path!r}; valid paths: {', '.join(sorted(entries))}",
        )
    if entry.get("status") == "confirmed":
        return f"{path} is already confirmed (by {entry.get('by')}); nothing changed"
    if path.startswith(_METRIC_PREFIX):
        metric = path.removeprefix(_METRIC_PREFIX).removesuffix("]")
        models_digest = current_models_digest(
            rill_dir_from_source(project_dir, (sidecar.get("relation") or {}).get("source")),
            model,
            registered_names_from_project(project_dir),
        )
        covering = [
            n for n in current_verified_queries(
                sidecar.get("verified_queries") or {}, model, models_digest
            )
            if metric in parse_query(sidecar["verified_queries"][n]["semantic_query"]).metrics
        ]
        if not covering:
            raise validation_error(
                "ACCEPT_UNCOVERED",
                f"{path}: no current verified query references metric {metric!r}; "
                "a definition is accepted only when a known answer exists for it "
                "(save --verify --expected-sql ... first)",
            )
    entry["status"] = "confirmed"
    entry["by"] = by.strip()
    entry["at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    fs_txn.publish(
        Path(project_dir), {f"semantic/{model_name}.provenance.yaml": dump_yaml(sidecar)}
    )
    return f"accepted {path} by {by.strip()}"
