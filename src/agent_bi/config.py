"""Project config (``agent-bi.yaml``, architecture §4.6) with ``${ENV}`` substitution."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_bi.errors import validation_error

CONFIG_FILENAME = "agent-bi.yaml"
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


class Attach(_Base):
    path: str
    alias: str
    type: str = "sqlite"


class DuckDBSession(_Base):
    TimeZone: str = "UTC"
    threads: int = 1
    preserve_insertion_order: bool = True


class DuckDBConfig(_Base):
    database: str = ":memory:"
    attach: list[Attach] = Field(default_factory=list)
    relation_map: dict[str, str] = Field(default_factory=dict)
    rill_models: list[str] = Field(default_factory=list)
    session: DuckDBSession = Field(default_factory=DuckDBSession)


class SnowflakeSession(_Base):
    WEEK_START: int = 1
    TIMEZONE: str = "UTC"


class SnowflakeConfig(_Base):
    account: str | None = None
    user: str | None = None
    password: str | None = None
    role: str | None = None
    warehouse: str | None = None
    semantic_view_schema: str | None = None
    session: SnowflakeSession = Field(default_factory=SnowflakeSession)


class EnginesConfig(_Base):
    duckdb: DuckDBConfig = Field(default_factory=DuckDBConfig)
    snowflake: SnowflakeConfig = Field(default_factory=SnowflakeConfig)


class RillConfig(_Base):
    project_dir: str = "rill"
    olap: str = "duckdb"


class Defaults(_Base):
    limit: int = 1000


class Config(_Base):
    schema_version: str = "agent-bi-config/1.0"
    project: str = "agent-bi"
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    rill: RillConfig = Field(default_factory=RillConfig)
    defaults: Defaults = Field(default_factory=Defaults)
    # Directory that relative paths in the config (attach paths) resolve against.
    base_dir: Path = Field(default=Path("."), exclude=True)


def substitute_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """Replace ``${VAR}`` in every string. A string that is exactly one reference to an
    unset (or empty) variable becomes ``None`` (the engine then reports itself
    unavailable); an unset reference embedded in a longer string becomes empty."""
    env = os.environ if env is None else env
    if isinstance(value, str):
        whole = _ENV_RE.fullmatch(value)
        if whole:
            return env.get(whole.group(1)) or None
        return _ENV_RE.sub(lambda m: env.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [substitute_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: substitute_env(v, env) for k, v in value.items()}
    return value


def parse_config_text(
    text: str, path: str | Path, env: dict[str, str] | None = None
) -> Config:
    """Parse config text as if it had been read from ``path`` (relative paths in the config
    resolve against that file's directory)."""
    cfg_path = Path(path)
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise validation_error("CONFIG_INVALID", f"{cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise validation_error("CONFIG_INVALID", f"{cfg_path}: top level must be a mapping")
    try:
        cfg = Config.model_validate(substitute_env(raw, env))
    except ValidationError as exc:
        raise validation_error("CONFIG_INVALID", f"{cfg_path}: {exc}") from exc
    cfg.base_dir = cfg_path.resolve().parent
    return cfg


def load_config(path: str | Path | None = None, env: dict[str, str] | None = None) -> Config:
    cfg_path = Path(path) if path is not None else Path(CONFIG_FILENAME)
    if not cfg_path.is_file():
        raise validation_error("CONFIG_NOT_FOUND", f"config file not found: {cfg_path}")
    return parse_config_text(cfg_path.read_text(encoding="utf-8"), cfg_path, env)
