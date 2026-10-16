"""sha256 helpers. Format everywhere: ``sha256:<64 hex>`` (architecture §2)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_bi.errors import validation_error

PREFIX = "sha256:"


def sha256_bytes(data: bytes) -> str:
    return PREFIX + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return PREFIX + h.hexdigest()


def canonical_dumps(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_canonical(obj: Any) -> str:
    return sha256_text(canonical_dumps(obj))


def expr_sha256(expr: str) -> str:
    """Digest of an object's ``expr`` exactly as written (§4.2)."""
    return sha256_text(expr)


def referenced_definitions(
    query: Any, model: Any, missing: list[str] | None = None
) -> dict[str, dict[str, str]]:
    """``{name: {kind, expr}}`` (sorted by name) of every metric, dimension, time dimension
    and named filter ``query`` references, ``expr`` as written in the model (§4.7).
    ``query`` is a SemanticQuery (or its dict); ``model`` a SemanticModel (or its dict).
    A referenced name the model no longer defines raises ``E_UNKNOWN_FIELD``, or, when a
    ``missing`` list is passed, is appended to it and skipped."""
    q = query.model_dump(exclude_none=True) if hasattr(query, "model_dump") else dict(query)
    m = model.to_dict() if hasattr(model, "to_dict") else model
    table = (m.get("tables") or [{}])[0]
    wanted = [("metrics", "metric", n) for n in q.get("metrics") or []]
    wanted += [("dimensions", "dimension", n) for n in q.get("dimensions") or []]
    if q.get("time"):
        wanted.append(("time_dimensions", "time_dimension", q["time"]["dimension"]))
    wanted += [("dimensions", "dimension", f["dimension"]) for f in q.get("filters") or []]
    wanted += [("filters", "filter", n) for n in q.get("named_filters") or []]
    out: dict[str, dict[str, str]] = {}
    for kind, label, name in wanted:
        obj = next((o for o in table.get(kind) or [] if o.get("name") == name), None)
        if obj is None:
            if missing is not None:
                missing.append(name)
                continue
            raise validation_error("UNKNOWN_FIELD", f"{label} {name!r} is not defined in the model")
        out[name] = {"kind": label, "expr": obj["expr"]}
    return dict(sorted(out.items()))


def semantic_digest(query: Any, model: Any) -> str:
    """The semantic digest of ``query`` against ``model`` (architecture §4.7): sha256 of the
    canonical JSON of ``{base_table, definitions}``. Descriptions, synonyms, sample values
    and sidecar statuses are not in it, so changing them does not change what a query
    computes; changing any referenced ``expr`` or the ``base_table`` does. This is the only
    implementation."""
    m = model.to_dict() if hasattr(model, "to_dict") else model
    table = (m.get("tables") or [{}])[0]
    return sha256_canonical(
        {"base_table": table.get("base_table"), "definitions": referenced_definitions(query, m)}
    )


def base_table_digest(model: Any) -> str:
    """sha256 of the canonical JSON of ``tables[0].base_table`` as written (§4.2)."""
    m = model.to_dict() if hasattr(model, "to_dict") else model
    return sha256_canonical((m.get("tables") or [{}])[0].get("base_table"))


def definition_digests(
    query: Any, model: Any, missing: list[str] | None = None
) -> dict[str, str]:
    """``{base_table: <digest>, <name>: expr_sha256, ...}`` with ``base_table`` first and the
    referenced definitions in name order (§4.2). Diagnosis only: it names what changed when a
    query goes STALE; identity and staleness are decided by ``semantic_digest`` alone."""
    out = {"base_table": base_table_digest(model)}
    for name, d in referenced_definitions(query, model, missing).items():
        out[name] = expr_sha256(d["expr"])
    return out
