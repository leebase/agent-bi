"""One global namespace per model (architecture §4.1.1 rule 2).

Names are unique across ``dimensions``, ``time_dimensions``, ``facts``, ``metrics`` and
``filters`` together. A collision is ``E_NAME_COLLISION`` whose message names both
objects as ``<kind> <name> (column "<physical>")`` or ``metric <name> (generated)``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from agent_bi.errors import validation_error
from agent_bi.query.compile import expr_columns

NAMESPACE_KINDS = ("dimensions", "time_dimensions", "facts", "metrics", "filters")
_KIND_LABEL = {
    "dimensions": "dimension",
    "time_dimensions": "time_dimension",
    "facts": "fact",
    "metrics": "metric",
    "filters": "filter",
}


# Used by the compiler (``time_bucket``) and by ``definition_digests`` (``base_table``); a semantic
# object may not take either name (§4.1.1 rule 2, pre-Sprint-4, D398).
RESERVED_NAMES = {
    "time_bucket": "the compiler's time column",
    "base_table": "the key of the base-table digest in definition_digests",
}


def describe(kind: str, name: str, detail: str) -> str:
    """``dimension warehouse_name (column "warehouse_name")`` / ``metric x (generated)``."""
    return f"{_KIND_LABEL[kind]} {name} ({detail})"


def check_global_namespace(objects: Iterable[tuple[str, str, str]]) -> None:
    """``objects`` is ``(kind, name, detail)`` in creation order; the first name seen twice
    raises ``E_NAME_COLLISION`` naming the earlier and the later object; a reserved name
    (``time_bucket``, ``base_table``) raises it naming the reservation."""
    seen: dict[str, str] = {}
    for kind, name, detail in objects:
        here = describe(kind, name, detail)
        if name in RESERVED_NAMES:
            raise validation_error(
                "NAME_COLLISION",
                f"name {name!r} is reserved ({RESERVED_NAMES[name]}) and cannot name {here}; "
                "nothing is renamed or suffixed automatically",
            )
        if name in seen:
            raise validation_error(
                "NAME_COLLISION",
                f"name {name!r} is used twice in one model: {seen[name]} and {here}; "
                "nothing is renamed or suffixed automatically",
            )
        seen[name] = here


def table_objects(
    table: dict[str, Any], origin_of: Callable[[str, str], str | None] | None = None
) -> list[tuple[str, str, str]]:
    """Namespace objects of a table dict (YAML shape), in YAML order. ``origin_of(kind,
    name)`` returns the sidecar entry origin (``scan`` marks a generated metric)."""
    out: list[tuple[str, str, str]] = []
    for kind in NAMESPACE_KINDS:
        for obj in table.get(kind) or []:
            name = obj["name"]
            if kind in ("dimensions", "time_dimensions", "facts"):
                cols = expr_columns(obj.get("expr", ""))
                detail = f'column "{cols[0]}"' if cols else "defined"
            elif kind == "metrics":
                generated = origin_of is not None and origin_of(kind, name) == "scan"
                detail = "generated" if generated else "defined"
            else:
                detail = "defined"
            out.append((kind, name, detail))
    return out
