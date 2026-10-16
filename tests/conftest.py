from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from agent_bi.config import Config, load_config
from agent_bi.engines import DuckDBEngine

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
IDLE = FIXTURES / "idle-warehouse-waste.sqlite"
HOLIDAY = FIXTURES / "holiday-cookout-rush.sqlite"


def sha256sums() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (FIXTURES / "SHA256SUMS").read_text().splitlines():
        digest, _, path = line.partition("  ")
        out[Path(path).name] = digest
    return out


def write_config(
    directory: Path, attaches: dict[str, Path], relation_map: dict[str, str] | None = None
) -> Path:
    """Write an agent-bi.yaml in ``directory`` attaching the given sqlite files."""
    relation_map = relation_map or {
        f"{alias.upper()}.MAIN": f"{alias}.main" for alias in attaches
    }
    cfg = {
        "schema_version": "agent-bi-config/1.0",
        "project": "agent-bi-test",
        "engines": {
            "duckdb": {
                "database": ":memory:",
                "attach": [
                    {"path": str(p), "alias": a, "type": "sqlite"} for a, p in attaches.items()
                ],
                "relation_map": relation_map,
                "session": {"TimeZone": "UTC", "threads": 1, "preserve_insertion_order": True},
            },
            "snowflake": {"account": "${AGENT_BI_SNOWFLAKE_ACCOUNT}"},
        },
    }
    path = directory / "agent-bi.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def repo_config() -> Config:
    """The committed agent-bi.yaml (both fixtures attached)."""
    return load_config(ROOT / "agent-bi.yaml")


@pytest.fixture
def engine(repo_config):
    eng = DuckDBEngine(repo_config)
    yield eng
    eng.close()


@pytest.fixture
def idle_only_config(tmp_path) -> Config:
    return load_config(write_config(tmp_path, {"fixture": IDLE}))


@pytest.fixture
def project(tmp_path):
    """A throwaway project: private copy of the idle fixture + config, empty semantic/."""
    data = tmp_path / "data"
    data.mkdir()
    db = data / "idle.sqlite"
    shutil.copy(IDLE, db)
    cfg_path = write_config(tmp_path, {"fixture": db})
    return {
        "dir": tmp_path,
        "db": db,
        "config_path": cfg_path,
        "config": load_config(cfg_path),
        "semantic": tmp_path / "semantic",
    }


def load_config_for(project) -> Config:
    return load_config(project["config_path"])
