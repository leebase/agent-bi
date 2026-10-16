"""Shared helpers for the Sprint 3 tests (a throwaway project laid out like the repo)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from tests.conftest import ROOT

GOLDEN = ROOT / "tests" / "golden" / "rill" / "idle_warehouse_waste"
MODEL = "idle_warehouse_waste"


def make_project(tmp_path: Path) -> Path:
    """A copy of the repo's config, semantic models and both fixtures at the same relative
    paths (so ``../tests/fixtures/...`` from ``rill/`` resolves exactly as in the repo)."""
    shutil.copy(ROOT / "agent-bi.yaml", tmp_path / "agent-bi.yaml")
    shutil.copytree(ROOT / "semantic", tmp_path / "semantic")
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    for name in ("idle-warehouse-waste.sqlite", "holiday-cookout-rush.sqlite"):
        shutil.copy(ROOT / "tests" / "fixtures" / name, tmp_path / "tests" / "fixtures" / name)
    return tmp_path


def tree_hash(root: Path) -> str:
    """sha256 over every file's relative path and bytes under ``root``."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def manifests(project: Path) -> list[Path]:
    return sorted((project / "rill" / "provenance").glob("*.json"))
