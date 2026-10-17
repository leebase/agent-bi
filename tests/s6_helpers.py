"""Sprint 6′ scratch-copy glue (M2, D430).

The committed ``agent-bi.yaml`` registers the operator Rill model (``rill_models``,
architecture §5.13), so a scratch copy laid out by ``tests.s3_helpers.make_project`` must
carry the operator model file too. The frozen fixture helpers in ``s3_helpers``/``s4_helpers``
cannot learn that; this module wraps them for the tests that copy the repo.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from tests.conftest import ROOT
from tests.s3_helpers import make_project as _make_project
from tests.s4_helpers import make_s4_project as _make_s4_project


def make_project(tmp_path: Path) -> Path:
    """``s3_helpers.make_project`` plus the committed operator Rill models."""
    project = _make_project(tmp_path)
    (project / "rill" / "models").mkdir(parents=True, exist_ok=True)
    for model in (ROOT / "rill" / "models").glob("*.sql"):
        shutil.copy(model, project / "rill" / "models" / model.name)
    return project


def strip_rill_models(project: Path) -> Path:
    """Remove the operator view-model registration from a scratch ``agent-bi.yaml``."""
    path = project / "agent-bi.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    duck = doc["engines"]["duckdb"]
    duck.pop("rill_models", None)
    (duck.get("relation_map") or {}).pop("VIEWS.MAIN", None)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return project


def make_s4_project_without_rill_models(tmp_path: Path) -> Path:
    """An ``s4_helpers`` project whose config does not register the operator view model: for
    tests that intentionally break the idle fixture, which the operator model also reads, or
    that are only about the idle table model."""
    return strip_rill_models(_make_s4_project(tmp_path))
