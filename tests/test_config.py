from __future__ import annotations

import pytest
import yaml

from agent_bi.config import load_config, substitute_env
from agent_bi.engines import SnowflakeEngine
from agent_bi.errors import AgentBiError
from tests.conftest import ROOT


def test_whole_string_reference_unset_becomes_none():
    assert substitute_env({"a": "${X}"}, env={}) == {"a": None}
    assert substitute_env("${X}", env={"X": "v"}) == "v"
    assert substitute_env("pre-${X}-post", env={"X": "v"}) == "pre-v-post"
    assert substitute_env("pre-${X}-post", env={}) == "pre--post"
    assert substitute_env(["${A}", {"b": "${A}"}], env={"A": "1"}) == ["1", {"b": "1"}]


def test_committed_config_matches_section_4_6():
    cfg = load_config(ROOT / "agent-bi.yaml", env={})
    duck = cfg.engines.duckdb
    assert [(a.alias, a.path) for a in duck.attach] == [
        ("fixture", "tests/fixtures/idle-warehouse-waste.sqlite"),
        ("fixture2", "tests/fixtures/holiday-cookout-rush.sqlite"),
    ]
    assert duck.relation_map == {"FIXTURE.MAIN": "fixture.main", "FIXTURE2.MAIN": "fixture2.main"}
    assert duck.session.model_dump() == {
        "TimeZone": "UTC", "threads": 1, "preserve_insertion_order": True
    }
    assert cfg.engines.snowflake.account is None  # env unset -> engine unavailable
    raw = yaml.safe_load((ROOT / "agent-bi.yaml").read_text())
    assert raw["engines"]["snowflake"]["account"] == "${AGENT_BI_SNOWFLAKE_ACCOUNT}"


def test_snowflake_engine_unavailable_when_env_unset():
    cfg = load_config(ROOT / "agent-bi.yaml", env={})
    with pytest.raises(AgentBiError) as exc:
        SnowflakeEngine(cfg)
    assert exc.value.full_code == "E_ENGINE_UNAVAILABLE"
    assert exc.value.exit_code == 3


def test_missing_config_is_a_validation_error(tmp_path):
    with pytest.raises(AgentBiError) as exc:
        load_config(tmp_path / "nope.yaml")
    assert exc.value.exit_code == 2
