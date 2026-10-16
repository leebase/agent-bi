"""Provenance sidecar (architecture §4.2), ``semantic/<model>.provenance.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_bi.errors import validation_error
from agent_bi.semantic.model import dump_yaml

SCHEMA_VERSION = "agent-bi-provenance/1.0"


class _Obj(BaseModel):
    model_config = ConfigDict(extra="allow")


class Entry(_Obj):
    status: Literal["catalog", "inferred", "confirmed"]  # anything else -> E_PROVENANCE_MISMATCH (D398)
    origin: str  # scan | enrich:<patch-id> | human
    at: str
    by: str
    expr_sha256: str | None = None
    unique: bool | None = None  # dimensions and time dimensions only


class Source(_Obj):
    kind: str
    path_or_account: str
    as_of_digest: str


class RelationInfo(_Obj):
    physical: str
    kind: str
    row_count: int
    columns_digest: str
    models_digest: str | None = None


class IgnoredColumn(_Obj):
    name: str
    type: str
    reason: str


class ProvenanceSidecar(_Obj):
    schema_version: str = SCHEMA_VERSION
    model: str
    scanned_at: str
    engine: str
    engine_version: str
    source: Source
    relation: RelationInfo
    entries: dict[str, Entry] = Field(default_factory=dict)
    verified_queries: dict[str, dict[str, Any]] = Field(default_factory=dict)
    ignored_columns: list[IgnoredColumn] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)

    def dump_yaml(self) -> str:
        return dump_yaml(self.to_dict())


def entry_key(kind: str, name: str) -> str:
    """Sidecar key: ``tables[0].<kind>[<name>]`` (names, not indexes, inside brackets)."""
    return f"tables[0].{kind}[{name}]"


def parse_sidecar(data: Any, where: str = "sidecar") -> ProvenanceSidecar:
    try:
        return ProvenanceSidecar.model_validate(data)
    except ValidationError as exc:
        raise validation_error("PROVENANCE_MISMATCH", f"{where}: {exc}") from exc


def load_sidecar_dict(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise validation_error("PROVENANCE_MISMATCH", f"sidecar not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise validation_error("PROVENANCE_MISMATCH", f"{p}: {exc}") from exc
    if not isinstance(data, dict):
        raise validation_error("PROVENANCE_MISMATCH", f"{p}: top level must be a mapping")
    return data
