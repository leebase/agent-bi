"""Exact-match lookup against a model's verified queries (architecture §4.2, §5.8).

No fuzzy matching, no synonyms, no ranking: a query is found by its ``query_hash`` or by
its ``question_normalized``, character for character.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_WS_RE = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """NFKC, lower-case, collapse whitespace, strip surrounding whitespace and trailing
    ``?.!`` (architecture §5.8)."""
    t = unicodedata.normalize("NFKC", text).lower()
    t = _WS_RE.sub(" ", t).strip()
    return t.rstrip("?.! ")


def find_by_hash(sidecar_verified: dict[str, Any], query_hash: str) -> list[str]:
    """Names of the verified queries whose ``query_hash`` equals ``query_hash`` (sorted)."""
    return sorted(
        n for n, rec in sidecar_verified.items() if (rec or {}).get("query_hash") == query_hash
    )


def find_by_question(sidecar_verified: dict[str, Any], question: str) -> list[str]:
    """Names of the verified queries whose ``question_normalized`` equals the normalized
    ``question`` (sorted). More than one name is an ambiguity the caller must report."""
    wanted = normalize_question(question)
    return sorted(
        n
        for n, rec in sidecar_verified.items()
        if (rec or {}).get("question_normalized") == wanted
    )


def current_verified_queries(sidecar_verified: dict[str, Any], model: Any) -> list[str]:
    """Names of the verified queries whose stored ``semantic_digest`` equals the digest
    computed now from ``model`` (§4.7): the *current*, non-stale ones. A query that references
    a definition the model no longer has is stale."""
    from agent_bi.hashing import definition_digests, semantic_digest
    from agent_bi.query.contract import parse_query

    out = []
    for name, rec in sorted(sidecar_verified.items()):
        q = parse_query((rec or {}).get("semantic_query"), f"verified_queries[{name}]")
        missing: list[str] = []
        definition_digests(q, model, missing)
        if not missing and (rec or {}).get("semantic_digest") == semantic_digest(q, model):
            out.append(name)
    return out
