"""Reading a SemanticQuery from a file (shared by ``compile-sql`` and ``ask``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_bi.errors import validation_error


def read_text_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise validation_error("QUERY_INVALID", f"cannot read query file {path}: {exc}") from exc


def load_query_file(path: str) -> Any:
    """A SemanticQuery file, or a document that wraps one under ``semantic_query`` (a
    known-answer file, or a saved ``queries/<name>.json``). Returns the query dict, or the raw
    text when it is not JSON (so ``parse_query`` reports the JSON error with the path)."""
    text = read_text_file(path)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(doc, dict) and isinstance(doc.get("semantic_query"), dict):
        return doc["semantic_query"]
    if isinstance(doc, dict) and doc.get("schema_version") == "agent-bi-resolution/1.0":
        # a resolution (§5.15) that did not resolve carries no query: refuse it by name
        raise validation_error(
            "QUERY_INVALID",
            f"{path}: resolution status is {doc.get('status')!r} and semantic_query is null; "
            "there is nothing to run",
        )
    return text
