"""Pydantic model of the Snowflake semantic-view YAML (architecture §4.1).

Key names come from ``docs/pinned/snowflake-semantic-view-yaml-spec.md``; fields are
declared in the spec's order so ``safe_dump(sort_keys=False)`` reproduces it. Every
object is ``extra="allow"`` so unknown keys round-trip verbatim. ``data_type`` is a plain
``str`` (reconciliation R2-8). ``unique`` and ``sample_values`` are not modelled: they are
not spec keys (R2-1/R2-2); a ``sample_values`` list in a file is preserved as an extra.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    model_serializer,
    model_validator,
)

from agent_bi.errors import validation_error


class _Obj(BaseModel):
    """Remembers the key order of its input so a dump reproduces the file's order."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)
    _key_order: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="wrap")
    @classmethod
    def _remember_order(cls, data: Any, handler: Any) -> Any:
        inst = handler(data)
        if isinstance(data, dict):
            inst._key_order = [str(k) for k in data]
        return inst

    @model_serializer(mode="wrap")
    def _keep_order(self, handler: Any) -> Any:
        dumped = handler(self)
        if not self._key_order or not isinstance(dumped, dict):
            return dumped
        out = {k: dumped[k] for k in self._key_order if k in dumped}
        out.update((k, v) for k, v in dumped.items() if k not in out)
        return out


class BaseTable(_Obj):
    database: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    table: str | None = None
    definition: str | None = None


class KeyColumns(_Obj):
    columns: list[str] = Field(default_factory=list)


class Dimension(_Obj):
    name: str
    synonyms: list[str] | None = None
    description: str | None = None
    expr: str
    data_type: str | None = None
    cortex_search_service: dict[str, Any] | None = None
    is_enum: bool | None = None
    labels: list[str] | None = None
    tags: list[dict[str, Any]] | None = None


class TimeDimension(_Obj):
    name: str
    synonyms: list[str] | None = None
    description: str | None = None
    expr: str
    data_type: str | None = None


class Fact(_Obj):
    name: str
    synonyms: list[str] | None = None
    description: str | None = None
    access_modifier: str | None = None
    expr: str
    data_type: str | None = None
    labels: list[str] | None = None
    tags: list[dict[str, Any]] | None = None


class Metric(_Obj):
    name: str
    synonyms: list[str] | None = None
    description: str | None = None
    access_modifier: str | None = None
    expr: str
    non_additive_dimensions: list[dict[str, Any]] | None = None
    using_relationships: list[str] | None = None
    tags: list[dict[str, Any]] | None = None


class Filter(_Obj):
    name: str
    synonyms: list[str] | None = None
    description: str | None = None
    expr: str


class Table(_Obj):
    name: str
    description: str | None = None
    base_table: BaseTable | None = None
    primary_key: KeyColumns | None = None
    unique_keys: list[KeyColumns] | None = None
    dimensions: list[Dimension] | None = None
    time_dimensions: list[TimeDimension] | None = None
    facts: list[Fact] | None = None
    metrics: list[Metric] | None = None
    filters: list[Filter] | None = None
    tags: list[dict[str, Any]] | None = None


class VerifiedQuery(_Obj):
    name: str
    question: str | None = None
    verified_at: int | None = None
    verified_by: str | None = None
    use_as_onboarding_question: bool | None = None
    sql: str | None = None


class SemanticModel(_Obj):
    name: str
    description: str | None = None
    tables: list[Table] = Field(default_factory=list)
    relationships: list[dict[str, Any]] | None = None
    variables: list[dict[str, Any]] | None = None
    metrics: list[dict[str, Any]] | None = None
    verified_queries: list[VerifiedQuery] | None = None
    custom_instructions: str | None = None
    module_custom_instructions: dict[str, Any] | None = None
    max_staleness: int | None = None
    tags: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Dump exactly what was set (declared or extra), spec key names, field order."""
        return self.model_dump(by_alias=True, exclude_unset=True)

    def dump_yaml(self) -> str:
        return dump_yaml(self.to_dict())


class _Dumper(yaml.SafeDumper):
    """safe_dump with multi-line strings (compiled SQL) as readable ``|`` blocks."""


def _str_representer(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str_representer)


def dump_yaml(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, default_flow_style=False)


def parse_model(data: Any, where: str = "model") -> SemanticModel:
    try:
        return SemanticModel.model_validate(data)
    except ValidationError as exc:
        raise validation_error("MODEL_INVALID", f"{where}: {exc}") from exc


def load_model_dict(path: str) -> dict[str, Any]:
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        raise validation_error("MODEL_NOT_FOUND", f"semantic model not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise validation_error("MODEL_INVALID", f"{p}: {exc}") from exc
    if not isinstance(data, dict):
        raise validation_error("MODEL_INVALID", f"{p}: top level must be a mapping")
    return data
