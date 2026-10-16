"""``docs/semantic-query-contract.md``: a fixed prose header plus the JSON schema of the
pydantic model (architecture §6 ``contract --print``). Generated, committed, and tested for
equality with the generator output."""

from __future__ import annotations

import json
from importlib import resources

from agent_bi.query.contract import SemanticQuery


def contract_doc() -> str:
    header = (
        resources.files("agent_bi.query").joinpath("contract_header.md").read_text(encoding="utf-8")
    )
    schema = json.dumps(SemanticQuery.model_json_schema(), indent=2, sort_keys=True)
    return f"{header}\n```json\n{schema}\n```\n"
