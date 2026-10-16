from __future__ import annotations

import yaml

from agent_bi.semantic.model import dump_yaml, parse_model
from tests.conftest import ROOT

MINIMAL = """\
name: minimal_model
description: A hand-written minimal model
tables:
- name: orders
  description: Orders
  base_table:
    database: SALES
    schema: PUBLIC
    table: orders
  primary_key:
    columns:
    - order_id
  dimensions:
  - name: region
    synonyms:
    - area
    description: Sales region
    expr: '"region"'
    data_type: TEXT
    is_enum: true
    x_unknown_key:
      kept: true
  facts:
  - name: amount
    description: Order amount
    expr: '"amount"'
    data_type: NUMBER
  metrics:
  - name: total_amount
    synonyms: []
    description: Sum of amount
    expr: SUM("amount")
  filters:
  - name: big
    expr: '"amount" > 100'
custom_instructions: Be careful.
x_top_level_unknown: 3
"""


def _pinned_example() -> str:
    text = (ROOT / "docs/pinned/snowflake-semantic-view-yaml-spec.md").read_text()
    start = text.index("    name: revenue_analysis")
    end = text.index("## Creating a semantic view from YAML")
    return "\n".join(line[4:] for line in text[start:end].splitlines()) + "\n"


def test_minimal_yaml_round_trips_byte_identically():
    assert dump_yaml(yaml.safe_load(MINIMAL)) == MINIMAL  # the fixture is canonical
    model = parse_model(yaml.safe_load(MINIMAL))
    assert model.dump_yaml() == MINIMAL


def test_pinned_example_round_trips():
    loaded = yaml.safe_load(_pinned_example())
    model = parse_model(loaded)
    assert yaml.safe_dump(model.to_dict(), sort_keys=False) == yaml.safe_dump(
        loaded, sort_keys=False
    )


def test_key_order_of_the_input_is_preserved():
    text = "tables:\n- name: t\n  description: d\nname: n\n"
    model = parse_model(yaml.safe_load(text))
    assert model.dump_yaml() == "tables:\n- name: t\n  description: d\nname: n\n"


def test_unknown_keys_survive_including_sample_values():
    data = {
        "name": "m",
        "tables": [
            {
                "name": "t",
                "dimensions": [
                    {"name": "d", "expr": '"d"', "sample_values": ["a", "b"], "unique": True}
                ],
            }
        ],
    }
    out = parse_model(data).to_dict()
    assert out["tables"][0]["dimensions"][0]["sample_values"] == ["a", "b"]
    assert out == data


def test_data_type_is_a_plain_string():
    data = {
        "name": "m",
        "tables": [{"name": "t", "facts": [{"name": "f", "expr": '"f"', "data_type": "VARCHAR(16777216)"}]}],
    }
    assert parse_model(data).tables[0].facts[0].data_type == "VARCHAR(16777216)"
