"""SemanticQuery contract (architecture §4.3): the IR a producer writes.

A SemanticQuery names model objects and carries no executable text. Everything here is
pydantic-validated; ``canonical_json`` / ``query_hash`` define identity, and the compiler
consumes the canonical form only (§4.3 "The compiler consumes the canonical form").
"""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from agent_bi.errors import validation_error
from agent_bi.hashing import canonical_dumps, sha256_text

SCHEMA_VERSION = "semantic-query/1.0"
MAX_LIMIT = 100000
DEFAULT_LIMIT = 1000
MAX_QUERY_BYTES = 1_000_000  # §11: untrusted input is size-capped (1 MB)

GRAINS = ("day", "week", "month", "quarter", "year")
FILTER_OPS = ("=", "!=", "in", "not_in", ">", ">=", "<", "<=", "between", "like")

Scalar = StrictStr | StrictBool | StrictInt | StrictFloat


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeSpec(_Strict):
    dimension: StrictStr
    grain: Literal["day", "week", "month", "quarter", "year"]
    start: StrictStr | None = None  # ISO date, inclusive
    end: StrictStr | None = None  # ISO date, exclusive

    @field_validator("start", "end")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if v is not None:
            date.fromisoformat(v)
            if v != date.fromisoformat(v).isoformat():
                raise ValueError("must be an ISO date YYYY-MM-DD")
        return v


class FilterSpec(_Strict):
    dimension: StrictStr
    op: Literal["=", "!=", "in", "not_in", ">", ">=", "<", "<=", "between", "like"]
    values: list[Scalar]

    @field_validator("values")
    @classmethod
    def _finite(cls, v: list[Scalar]) -> list[Scalar]:
        if any(isinstance(x, float) and not math.isfinite(x) for x in v):
            raise ValueError("numeric values must be finite")
        return v

    @model_validator(mode="after")
    def _arity(self) -> FilterSpec:
        n = len(self.values)
        if self.op == "between":
            if n != 2:
                raise ValueError("op 'between' takes exactly 2 values")
        elif self.op in ("in", "not_in"):
            if n < 1:
                raise ValueError(f"op {self.op!r} takes at least 1 value")
        elif n != 1:
            raise ValueError(f"op {self.op!r} takes exactly 1 value")
        return self


class OrderSpec(_Strict):
    field: StrictStr
    direction: Literal["asc", "desc"] = "asc"


class SemanticQuery(_Strict):
    schema_version: Literal["semantic-query/1.0"] = SCHEMA_VERSION
    model: StrictStr
    table: StrictStr
    metrics: list[StrictStr] = Field(default_factory=list)
    dimensions: list[StrictStr] = Field(default_factory=list)
    time: TimeSpec | None = None
    filters: list[FilterSpec] = Field(default_factory=list)
    named_filters: list[StrictStr] = Field(default_factory=list)
    order_by: list[OrderSpec] = Field(default_factory=list)
    limit: StrictInt = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)

    @model_validator(mode="after")
    def _shape(self) -> SemanticQuery:
        if not self.metrics and not self.dimensions:
            raise ValueError("a query needs at least one metric, or at least one dimension")
        for label, names in (
            ("metrics", self.metrics), ("dimensions", self.dimensions),
            ("named_filters", self.named_filters),
        ):
            repeated = sorted({n for n in names if names.count(n) > 1})
            if repeated:  # refused, not collapsed (§4.3, ADR-006, pre-Sprint-4, D398)
                raise ValueError(f"{label}: {repeated[0]!r} is listed more than once")
        return self

    # -- canonical form (§4.3) -------------------------------------------------------

    def canonical(self) -> SemanticQuery:
        """The canonical form: ``metrics``/``dimensions``/``named_filters`` sorted (and
        de-duplicated), ``filters`` sorted by ``(dimension, op, values)``, ``order_by`` kept
        in order."""
        return self.model_copy(
            update={
                "metrics": sorted(set(self.metrics)),
                "dimensions": sorted(set(self.dimensions)),
                "named_filters": sorted(set(self.named_filters)),
                "filters": sorted(
                    self.filters,
                    key=lambda f: (f.dimension, f.op, json.dumps(f.values, sort_keys=True)),
                ),
            }
        )

    def canonical_dict(self) -> dict[str, Any]:
        d = self.canonical().model_dump(exclude_none=True)
        return d


def parse_query(data: Any, where: str = "query") -> SemanticQuery:
    """Validate a SemanticQuery from a dict, a JSON string, or an existing instance.
    Any violation of the contract is ``E_QUERY_INVALID`` (exit 2)."""
    if isinstance(data, SemanticQuery):
        return data
    if isinstance(data, (str, bytes)):
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if len(raw) > MAX_QUERY_BYTES:
            raise validation_error(
                "QUERY_INVALID", f"{where}: larger than the {MAX_QUERY_BYTES}-byte cap"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise validation_error("QUERY_INVALID", f"{where}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise validation_error("QUERY_INVALID", f"{where}: top level must be a JSON object")
    try:
        return SemanticQuery.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<query>'}: {e['msg']}" for e in exc.errors()
        )
        raise validation_error("QUERY_INVALID", f"{where}: {problems}") from exc


def canonical_json(query: SemanticQuery | dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, whitespace-free, canonical list order (§4.3)."""
    return canonical_dumps(parse_query(query).canonical_dict())


def query_hash(query: SemanticQuery | dict[str, Any]) -> str:
    """``sha256:<64 hex>`` of the canonical JSON; identifies the IR (§0 rule 4)."""
    return sha256_text(canonical_json(query))


def _valid(names: list[str]) -> str:
    return ", ".join(sorted(names)) if names else "(none)"


def _model_table(model: Any) -> dict[str, Any]:
    d = model.to_dict() if hasattr(model, "to_dict") else model
    return (d.get("tables") or [{}])[0] | {"__model__": d.get("name")}


def validate_against_model(query: SemanticQuery | dict[str, Any], model: Any) -> SemanticQuery:
    """§4.3 model-side rules. Unknown names are ``E_UNKNOWN_FIELD`` naming the offender and
    the sorted list of valid names. Returns the validated query."""
    q = parse_query(query)
    table = _model_table(model)
    if q.model != table["__model__"]:
        raise validation_error(
            "QUERY_INVALID", f"model: query is for {q.model!r}, this model is {table['__model__']!r}"
        )
    if q.table != table.get("name"):
        raise validation_error(
            "UNKNOWN_FIELD",
            f"table: unknown table {q.table!r}; valid names: {table.get('name')}",
        )

    def names(kind: str) -> list[str]:
        return [o["name"] for o in table.get(kind) or []]

    def need(label: str, name: str, kind: str) -> None:
        valid = names(kind)
        if name not in valid:
            raise validation_error(
                "UNKNOWN_FIELD", f"unknown {label} {name!r}; valid {label}s: {_valid(valid)}"
            )

    for m in q.metrics:
        need("metric", m, "metrics")
    for d in q.dimensions:
        need("dimension", d, "dimensions")
    if q.time is not None:
        need("time dimension", q.time.dimension, "time_dimensions")
    for f in q.filters:
        need("dimension", f.dimension, "dimensions")
    for n in q.named_filters:
        need("named filter", n, "filters")
    selectable = {*q.metrics, *q.dimensions}
    if q.time is not None:
        selectable.add("time_bucket")
    for o in q.order_by:
        if o.field not in selectable:
            raise validation_error(
                "UNKNOWN_FIELD",
                f"order_by field {o.field!r} is not a selected metric, dimension or "
                f"time_bucket; valid names: {_valid(list(selectable))}",
            )
    return q
