from __future__ import annotations

import duckdb
import pytest

from agent_bi.engines import DuckDBEngine, first_keyword
from agent_bi.errors import AgentBiError
from tests.conftest import IDLE, sha256sums

REL = '"fixture"."main"."warehouse_metering_history"'


def test_attach_fixture_and_count(engine):
    columns, rows = engine.execute(f"SELECT COUNT(*) AS n FROM {REL}")
    assert columns == ["n"]
    assert rows == [(21,)]


def test_as_of_equals_sha256sums(idle_only_config):
    eng = DuckDBEngine(idle_only_config)
    try:
        expected = "sha256:" + sha256sums()["idle-warehouse-waste.sqlite"]
        assert eng.as_of().value == expected
        assert eng.as_of().kind == "file-digest"
        assert eng.as_of("fixture").value == expected
    finally:
        eng.close()


def test_as_of_with_two_attachments_needs_an_alias(engine):
    sums = sha256sums()
    assert engine.as_of("fixture").value == "sha256:" + sums["idle-warehouse-waste.sqlite"]
    assert engine.as_of("fixture2").value == "sha256:" + sums["holiday-cookout-rush.sqlite"]
    with pytest.raises(AgentBiError) as exc:
        engine.as_of()
    assert exc.value.full_code == "E_AMBIGUOUS_AS_OF"


def test_execute_with_as_of_returns_digest(engine):
    _columns, rows, as_of = engine.execute_with_as_of(f"SELECT COUNT(*) FROM {REL}", "fixture")
    assert rows == [(21,)]
    assert as_of.value == "sha256:" + sha256sums()["idle-warehouse-waste.sqlite"]


def test_snapshot_change_during_execution_is_refused(engine, monkeypatch):
    digests = iter(["sha256:" + "0" * 64, "sha256:" + "1" * 64])
    monkeypatch.setattr(engine, "_file_digest", lambda alias: next(digests))
    with pytest.raises(AgentBiError) as exc:
        engine.execute_with_as_of(f"SELECT COUNT(*) FROM {REL}", "fixture")
    assert exc.value.full_code == "E_SNAPSHOT_CHANGED"
    assert exc.value.exit_code == 3


@pytest.mark.parametrize(
    "sql",
    [
        "SET threads=4",
        f"ATTACH '{IDLE}' AS other (TYPE SQLITE)",
        f"INSERT INTO {REL} VALUES ('2026-01-01', 'X', 1, 1, 1)",
        f"DELETE FROM {REL}",
        f"UPDATE {REL} SET credits_used = 0",
        "DROP TABLE x",
        "PRAGMA database_list",
        "COPY (SELECT 1) TO 'x.csv'",
        "/* leading comment */ DROP TABLE x",
        "-- comment\nSET threads=4",
        "SELECT 1; INSERT INTO t VALUES (1)",
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
        "",
        "   ",
    ],
)
def test_execute_refuses_everything_but_select_and_with(engine, sql):
    with pytest.raises(AgentBiError) as exc:
        engine.execute(sql)
    assert exc.value.full_code == "E_STATEMENT_NOT_ALLOWED"
    assert exc.value.exit_code == 2


def test_execute_accepts_select_and_with(engine):
    assert engine.execute("select 1 as one")[1] == [(1,)]
    assert engine.execute("-- note\n  WITH x AS (SELECT 2 AS v) SELECT v FROM x")[1] == [(2,)]
    assert engine.execute("/* c */ SELECT 3;")[1] == [(3,)]


def test_first_keyword():
    assert first_keyword("  /* a */ -- b\n select 1") == "SELECT"
    assert first_keyword("") == ""


def test_attach_is_read_only(engine):
    with pytest.raises(duckdb.Error, match="read-only"):
        engine._conn.execute(f"INSERT INTO {REL} VALUES ('2026-01-01', 'X', 1, 1, 1)")
    assert engine.execute(f"SELECT COUNT(*) FROM {REL}")[1] == [(21,)]


def test_engine_settings_is_exactly_the_session_block(engine):
    assert engine.engine_settings() == {
        "TimeZone": "UTC",
        "threads": 1,
        "preserve_insertion_order": True,
    }


def test_version(engine):
    assert engine.version() == duckdb.__version__


def test_engine_error_wraps_the_engine_message(engine):
    with pytest.raises(AgentBiError) as exc:
        engine.execute('SELECT * FROM "fixture"."main"."no_such_table"')
    assert exc.value.full_code == "E_ENGINE"
    assert exc.value.exit_code == 3
    assert "no_such_table" in exc.value.message
