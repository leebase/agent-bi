"""Sprint 6′ (M2, D430/D432): the operator's Rill model graph (architecture §5.13).

Covers the closed form (per file and per closure) and the determinism denylist, dependency
discovery and the closure digest, engine view creation in topological order with the
working-directory rule, ``init --rill-model`` registering the closure, scan/refresh sidecar
fields (``dependencies``, ``models_digest``), compile-rill for a view model, the
``E_OPERATOR_FILE`` guard, staleness bound to the closure digest (any closure file), the
D431 failure isolation including the build-validity invariant, and the operator files' byte
identity across every command.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from agent_bi.config import DuckDBConfig, load_config
from agent_bi.engines import DuckDBEngine
from agent_bi.errors import AgentBiError
from agent_bi.hashing import (
    canonical_dumps,
    definition_digests,
    semantic_digest,
    sha256_canonical,
)
from agent_bi.main import main
from agent_bi.rill_models import (
    check_closure,
    check_model,
    current_models_digest,
    file_digest,
    models_digest,
)
from agent_bi.semantic.model import load_model_dict
from tests.conftest import HOLIDAY, IDLE, ROOT, write_config
from tests.s3_helpers import tree_hash

MODEL = "warehouse_metering_enriched"
SOURCE = "warehouse_metering_source"
WAREHOUSES = "warehouses_source"
CLOSURE = [MODEL, SOURCE, WAREHOUSES]
OPERATOR_DIR = ROOT / "rill" / "models"
ORIGINALS = {name: (OPERATOR_DIR / f"{name}.sql").read_text() for name in CLOSURE}
SHAS = {
    name: "sha256:" + hashlib.sha256((OPERATOR_DIR / f"{name}.sql").read_bytes()).hexdigest()
    for name in CLOSURE
}
SOURCES = [SOURCE, WAREHOUSES]
SQLITE_REL = "../tests/fixtures/idle-warehouse-waste.sqlite"
HOLIDAY_REL = "../tests/fixtures/holiday-cookout-rush.sqlite"

# Task 4 of SPRINT6.md, verbatim.
SQL_COST_CENTER = (
    "SELECT w.cost_center AS cost_center, SUM(m.credits_used) AS total_credits_used "
    "FROM warehouse_metering_history m LEFT JOIN show_warehouses w "
    "ON w.name = m.warehouse_name GROUP BY w.cost_center "
    "ORDER BY total_credits_used DESC, cost_center ASC"
)
SQL_SIZE_BY_MONTH = (
    "SELECT w.size AS warehouse_size, date(m.usage_day, 'start of month') AS time_bucket, "
    "SUM(m.credits_used) AS total_credits_used "
    "FROM warehouse_metering_history m LEFT JOIN show_warehouses w "
    "ON w.name = m.warehouse_name "
    "GROUP BY w.size, date(m.usage_day, 'start of month') "
    "ORDER BY total_credits_used DESC, warehouse_size ASC"
)
QUERY_COST_CENTER = {
    "schema_version": "semantic-query/1.0", "model": MODEL, "table": MODEL,
    "metrics": ["total_credits_used"], "dimensions": ["cost_center"],
}
QUERY_SIZE_BY_MONTH = {
    "schema_version": "semantic-query/1.0", "model": MODEL, "table": MODEL,
    "metrics": ["total_credits_used"], "dimensions": ["warehouse_size"],
    "time": {"dimension": "usage_day", "grain": "month"},
}

CONFIG_WITH_COMMENTS = """\
schema_version: agent-bi-config/1.0
project: s6-test
engines:                                  # the engine block
  duckdb:
    database: ":memory:"                  # or a .duckdb path
    attach:
      - {path: tests/fixtures/idle-warehouse-waste.sqlite, alias: fixture, type: sqlite}
    relation_map:                         # base_table map
      "FIXTURE.MAIN": "fixture.main"
    session:
      TimeZone: UTC
      threads: 1
      preserve_insertion_order: true
rill:
  project_dir: rill
defaults:
  limit: 1000
"""


# --------------------------------------------------------------------------- fixtures/helpers


def make_view_project(tmp_path: Path, *, attach_idle: bool = True, commented: bool = True) -> Path:
    """A scratch project with the three committed operator model files and no view-model
    artifacts."""
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    for name in ("idle-warehouse-waste.sqlite", "holiday-cookout-rush.sqlite"):
        shutil.copy(ROOT / "tests" / "fixtures" / name, tmp_path / "tests/fixtures" / name)
    (tmp_path / "semantic").mkdir()
    (tmp_path / "rill" / "models").mkdir(parents=True)
    for name in CLOSURE:
        shutil.copy(OPERATOR_DIR / f"{name}.sql", tmp_path / "rill" / "models" / f"{name}.sql")
    if commented:
        (tmp_path / "agent-bi.yaml").write_text(CONFIG_WITH_COMMENTS, encoding="utf-8")
    else:
        attaches = {"fixture": IDLE} if attach_idle else {"holiday": HOLIDAY}
        write_config(tmp_path, attaches)
    return tmp_path


def cli(project: Path, capsys, *args: str) -> tuple[int, str, str]:
    capsys.readouterr()
    rc = main(["--config", str(project / "agent-bi.yaml"), *args])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def init_view(project: Path, capsys, name: str = MODEL) -> tuple[int, str, str]:
    return cli(project, capsys, "init", "--rill-model", name)


def model_path(project: Path, name: str = MODEL) -> Path:
    return project / "rill" / "models" / f"{name}.sql"


def write_model(project: Path, name: str, text: str) -> None:
    model_path(project, name).write_text(text, encoding="utf-8")


def closure_digest(project: Path, registered: list[str] | None = None) -> str:
    return models_digest(project / "rill", MODEL, registered or CLOSURE)


def register_extra(project: Path, name: str) -> None:
    """Append one name to the config's ``rill_models`` (scratch copies only)."""
    path = project / "agent-bi.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["engines"]["duckdb"].setdefault("rill_models", []).append(name)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def create_verified(
    project: Path, capsys, name: str, question: str, sql: str, query: dict,
    model: str = MODEL,
) -> dict:
    qpath = project / "out" / f"{name}.query.json"
    qpath.parent.mkdir(exist_ok=True)
    qpath.write_text(json.dumps(query), encoding="utf-8")
    rc, out, err = cli(
        project, capsys, "ask", "--model", model, "--query-file", str(qpath), "--json"
    )
    assert rc == 0, err
    env = json.loads(out)
    answer = project / "out" / f"{env['provenance']['artifact']['query_hash'].removeprefix('sha256:')}.answer.json"
    rc, _, err = cli(
        project, capsys, "save", "--from", str(answer), "--name", name, "--question", question,
        "--verify", "--by", "sprint-6", "--expected-sql", sql,
    )
    assert rc == 0, err
    return env


def create_both_verified(project: Path, capsys) -> None:
    create_verified(
        project, capsys, "credits_by_cost_center", "credits by cost center",
        SQL_COST_CENTER, QUERY_COST_CENTER,
    )
    create_verified(
        project, capsys, "credits_by_size_by_month", "credits by warehouse size by month",
        SQL_SIZE_BY_MONTH, QUERY_SIZE_BY_MONTH,
    )


# ------------------------------------------------------------------------------- digest


def test_file_digest_normalizes_crlf_and_trailing_whitespace(tmp_path):
    (tmp_path / "a.sql").write_bytes(b'SELECT "x"\nFROM sqlite_scan(\'a\', \'t\')\n')
    (tmp_path / "b.sql").write_bytes(b'SELECT "x"  \r\nFROM sqlite_scan(\'a\', \'t\')\r\n\t')
    (tmp_path / "c.sql").write_bytes(b'SELECT "y"\nFROM sqlite_scan(\'a\', \'t\')\n')
    assert file_digest(tmp_path / "a.sql") == file_digest(tmp_path / "b.sql")
    assert file_digest(tmp_path / "a.sql") != file_digest(tmp_path / "c.sql")


def test_file_digest_is_a_comment_sensitive_sha256(tmp_path):
    path = tmp_path / "m.sql"
    path.write_text("SELECT 1\n")
    first = file_digest(path)
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 64
    path.write_text("SELECT 1\n-- a new comment\n")
    assert file_digest(path) != first  # comments are part of the bound text (fail closed)


def test_models_digest_is_the_canonical_closure_map(tmp_path):
    project = make_view_project(tmp_path)
    expected = sha256_canonical(
        {name: file_digest(model_path(project, name)) for name in CLOSURE}
    )
    assert closure_digest(project) == expected
    # The canonical JSON has each closure member as a key; an upstream edit changes it.
    assert canonical_dumps({name: file_digest(model_path(project, name)) for name in CLOSURE})
    write_model(project, SOURCE, ORIGINALS[SOURCE] + "-- upstream edit\n")
    assert closure_digest(project) != expected


def test_current_models_digest_is_none_for_a_table_model_and_reads_now_for_a_view(tmp_path):
    idle = load_model_dict(str(ROOT / "semantic" / "idle_warehouse_waste.yaml"))
    assert current_models_digest(tmp_path / "rill", idle, []) is None
    view = {"name": MODEL, "tables": [{"base_table": {"database": "VIEWS", "schema": "MAIN", "table": MODEL}}]}
    (tmp_path / "rill" / "models").mkdir(parents=True)
    (tmp_path / "rill" / "models" / f"{MODEL}.sql").write_text("SELECT 1\n")
    first = current_models_digest(tmp_path / "rill", view, [MODEL])
    assert first == sha256_canonical({MODEL: file_digest(tmp_path / "rill" / "models" / f"{MODEL}.sql")})
    (tmp_path / "rill" / "models" / f"{MODEL}.sql").write_text("SELECT 2\n")
    assert current_models_digest(tmp_path / "rill", view, [MODEL]) != first


def test_config_rill_models_defaults_to_empty():
    assert DuckDBConfig().rill_models == []


# ------------------------------------------------------------------- rules 2 and 3


def test_check_model_parses_a_source_model(tmp_path):
    project = make_view_project(tmp_path)
    facts = check_model(project / "rill", SOURCE)
    assert facts.name == SOURCE
    assert facts.path == model_path(project, SOURCE)
    assert facts.sqlite_path == SQLITE_REL
    assert facts.sqlite_abs == (project / "tests/fixtures/idle-warehouse-waste.sqlite").resolve()
    assert facts.has_sqlite_scan
    assert facts.tables == ("warehouse_metering_history",)


def test_check_model_allows_a_join_model_without_a_sqlite_scan(tmp_path):
    project = make_view_project(tmp_path)
    facts = check_model(project / "rill", MODEL)
    assert facts.sqlite_path is None and facts.sqlite_abs is None
    assert not facts.has_sqlite_scan and facts.tables == ()
    assert "LEFT JOIN" in facts.text  # the operator text is returned verbatim, never studied


def test_check_closure_resolves_the_graph_file_and_direct_deps(tmp_path):
    project = make_view_project(tmp_path)
    checked = check_closure(project / "rill", MODEL, CLOSURE)
    assert checked.members == tuple(CLOSURE)
    assert checked.sqlite_abs == (project / "tests/fixtures/idle-warehouse-waste.sqlite").resolve()
    assert checked.direct[MODEL] == (SOURCE, WAREHOUSES)


def _annotation(t: str) -> str:
    return t.replace("SELECT\n", "-- @materialize: true\nSELECT\n", 1)


def _templating(t: str) -> str:
    return t.replace("SELECT\n", "SELECT '{{ ref }}' AS \"x\",\n", 1)


def _create_not_select(t: str) -> str:
    return t.replace("SELECT\n", "CREATE TABLE t AS SELECT\n", 1)


def _two_statements(t: str) -> str:
    return t + ";\nSELECT 1\n"


def _external_reader(t: str) -> str:
    return t.replace("SELECT\n", "SELECT read_csv('x') AS \"extra\",\n", 1)


def _generated_header(t: str) -> str:
    return "-- agent-bi: generated from semantic/x.yaml — do not edit\n" + t


def _non_literal_scan(t: str) -> str:
    return t.replace(f"sqlite_scan('{SQLITE_REL}'", "sqlite_scan(path_column", 1)


def _second_file(t: str) -> str:
    return t.replace(SQLITE_REL, HOLIDAY_REL, 1)


def _missing_file(t: str) -> str:
    return t.replace(SQLITE_REL, "../tests/fixtures/no-such.sqlite", 1)


def _two_scans(t: str) -> str:
    return t.replace(
        "SELECT * FROM",
        f"SELECT * FROM sqlite_scan('{SQLITE_REL}', 'show_warehouses') AS extra,",
        1,
    )


JOIN_RULE_2_CASES = [
    ("annotation", _annotation),
    ("templating", _templating),
    ("create-non-select", _create_not_select),
    ("two-statements", _two_statements),
    ("external-reader", _external_reader),
    ("generated-header", _generated_header),
]
SOURCE_RULE_2_CASES = [
    ("non-literal-scan", _non_literal_scan),
    ("missing-file", _missing_file),
    ("two-scans", _two_scans),
]


@pytest.mark.parametrize(("label", "mutate"), JOIN_RULE_2_CASES, ids=[c[0] for c in JOIN_RULE_2_CASES])
def test_rule_2_refusals_in_the_join_model(tmp_path, label, mutate):
    project = make_view_project(tmp_path)
    write_model(project, MODEL, mutate(ORIGINALS[MODEL]))
    with pytest.raises(AgentBiError) as exc:
        check_model(project / "rill", MODEL)
    assert exc.value.full_code == "E_UNSUPPORTED_VIEW_FORM" and exc.value.exit_code == 2
    assert str(model_path(project, MODEL)) in str(exc.value)


@pytest.mark.parametrize(("label", "mutate"), SOURCE_RULE_2_CASES, ids=[c[0] for c in SOURCE_RULE_2_CASES])
def test_rule_2_refusals_in_a_source_model(tmp_path, label, mutate):
    project = make_view_project(tmp_path)
    write_model(project, SOURCE, mutate(ORIGINALS[SOURCE]))
    with pytest.raises(AgentBiError) as exc:
        check_closure(project / "rill", MODEL, CLOSURE)
    assert exc.value.full_code == "E_UNSUPPORTED_VIEW_FORM" and exc.value.exit_code == 2
    assert str(model_path(project, SOURCE)) in str(exc.value)


def test_the_closure_must_contain_a_sqlite_scan(tmp_path):
    project = make_view_project(tmp_path)
    write_model(project, MODEL, 'SELECT 1 AS "usage_day"\n')
    with pytest.raises(AgentBiError) as exc:
        check_closure(project / "rill", MODEL, CLOSURE)
    assert exc.value.full_code == "E_UNSUPPORTED_VIEW_FORM"
    assert "sqlite_scan" in str(exc.value) and str(model_path(project)) in str(exc.value)


def test_the_closure_must_read_one_sqlite_file(tmp_path):
    project = make_view_project(tmp_path)
    write_model(project, MODEL, f"SELECT * FROM {SOURCE} CROSS JOIN other_source\n")
    write_model(project, "other_source", ORIGINALS[WAREHOUSES].replace(SQLITE_REL, HOLIDAY_REL))
    with pytest.raises(AgentBiError) as exc:
        check_closure(project / "rill", MODEL, CLOSURE + ["other_source"])
    assert exc.value.full_code == "E_UNSUPPORTED_VIEW_FORM"
    assert "more than one SQLite file" in str(exc.value)


def test_the_closure_must_be_acyclic_and_the_error_names_the_cycle(tmp_path):
    project = make_view_project(tmp_path)
    write_model(project, "cycle_a", 'SELECT "x" FROM cycle_b\n')
    write_model(project, "cycle_b", 'SELECT "x" FROM cycle_a\n')
    with pytest.raises(AgentBiError) as exc:
        check_closure(project / "rill", "cycle_a", ["cycle_a", "cycle_b"])
    assert exc.value.full_code == "E_UNSUPPORTED_VIEW_FORM"
    message = str(exc.value)
    assert "cycle" in message and "cycle_a" in message and "cycle_b" in message


def test_an_invalid_registered_name_is_a_rule_2_refusal(tmp_path):
    project = make_view_project(tmp_path)
    with pytest.raises(AgentBiError) as exc:
        check_model(project / "rill", "Bad-Name")
    assert exc.value.full_code == "E_UNSUPPORTED_VIEW_FORM"


DENY_WORDS = [
    "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "NOW", "TODAY", "RANDOM",
    "RAND", "UUID", "GEN_RANDOM_UUID", "GETDATE", "SYSDATE", "LOCALTIME",
    "LOCALTIMESTAMP", "SAMPLE",
]


@pytest.mark.parametrize("word", DENY_WORDS)
def test_rule_3_refuses_each_nondeterminism_word(tmp_path, word):
    project = make_view_project(tmp_path)
    write_model(
        project, SOURCE,
        f"SELECT {word} AS \"usage_day\" "
        f"FROM sqlite_scan('{SQLITE_REL}', 'warehouse_metering_history')\n",
    )
    with pytest.raises(AgentBiError) as exc:
        check_closure(project / "rill", MODEL, CLOSURE)
    assert exc.value.full_code == "E_NONDETERMINISTIC_VIEW" and exc.value.exit_code == 2
    assert word.lower() in str(exc.value).lower()


def test_rule_3_refuses_using_sample(tmp_path):
    project = make_view_project(tmp_path)
    write_model(
        project, SOURCE,
        f'SELECT "usage_day" FROM sqlite_scan(\'{SQLITE_REL}\', \'warehouse_metering_history\') '
        "USING SAMPLE 10\n",
    )
    with pytest.raises(AgentBiError) as exc:
        check_closure(project / "rill", MODEL, CLOSURE)
    assert exc.value.full_code == "E_NONDETERMINISTIC_VIEW"


INIT_REFUSALS = [
    ("join-annotation", MODEL, _annotation),
    ("join-external-reader", MODEL, _external_reader),
    ("join-no-scan-closure", MODEL, lambda _t: 'SELECT 1 AS "usage_day"\n'),
    ("source-two-scans", SOURCE, _two_scans),
    ("source-missing-file", SOURCE, _missing_file),
]


@pytest.mark.parametrize(("label", "target", "mutate"), INIT_REFUSALS, ids=[c[0] for c in INIT_REFUSALS])
def test_a_rule_refusal_during_init_writes_nothing(tmp_path, capsys, label, target, mutate):
    project = make_view_project(tmp_path)
    write_model(project, target, mutate(ORIGINALS[target]))
    before = tree_hash(project)
    rc, _, err = init_view(project, capsys)
    assert rc == 2, (label, err)
    assert err.strip().splitlines()[-1].startswith("E_") and "E_UNSUPPORTED_VIEW_FORM" in err
    assert tree_hash(project) == before


def test_a_cycle_refusal_during_init_writes_nothing(tmp_path, capsys):
    project = make_view_project(tmp_path)
    write_model(project, "cycle_a", 'SELECT "x" FROM cycle_b\n')
    write_model(project, "cycle_b", 'SELECT "x" FROM cycle_a\n')
    before = tree_hash(project)
    rc, _, err = init_view(project, capsys, "cycle_a")
    assert rc == 2
    assert "E_UNSUPPORTED_VIEW_FORM" in err and "cycle_a" in err and "cycle_b" in err
    assert tree_hash(project) == before


def test_two_sqlite_files_in_a_closure_refuse_init_and_write_nothing(tmp_path, capsys):
    project = make_view_project(tmp_path)
    write_model(project, "two_files_c", ORIGINALS[SOURCE])
    write_model(
        project, "two_files_other",
        ORIGINALS[WAREHOUSES].replace(SQLITE_REL, HOLIDAY_REL),
    )
    write_model(project, "two_files_a", "SELECT * FROM two_files_c CROSS JOIN two_files_other\n")
    before = tree_hash(project)
    rc, _, err = init_view(project, capsys, "two_files_a")
    assert rc == 2
    assert "E_UNSUPPORTED_VIEW_FORM" in err and "more than one SQLite file" in err
    assert tree_hash(project) == before


def test_a_reference_to_an_unregistered_model_refuses_init_and_writes_nothing(tmp_path, capsys):
    project = make_view_project(tmp_path)
    write_model(
        project, "bad_ref",
        f"SELECT t.* FROM sqlite_scan('{SQLITE_REL}', 'warehouse_metering_history') AS t, "
        "not_registered_model AS u\n",
    )
    before = tree_hash(project)
    rc, _, err = init_view(project, capsys, "bad_ref")
    assert rc == 2
    assert "E_UNSUPPORTED_VIEW_FORM" in err and "not_registered_model" in err
    assert str(model_path(project, "bad_ref")) in err
    assert tree_hash(project) == before


def test_a_reference_to_a_generated_model_refuses_init_and_writes_nothing(tmp_path, capsys):
    project = make_view_project(tmp_path)
    write_model(
        project, "generated_source",
        "-- agent-bi: generated from semantic/idle.yaml — do not edit\n"
        f"SELECT * FROM sqlite_scan('{SQLITE_REL}', 'show_warehouses')\n",
    )
    register_extra(project, "generated_source")
    write_model(
        project, "refs_generated",
        f"SELECT t.* FROM sqlite_scan('{SQLITE_REL}', 'warehouse_metering_history') AS t "
        "JOIN generated_source AS g ON TRUE\n",
    )
    before = tree_hash(project)
    rc, _, err = init_view(project, capsys, "refs_generated")
    assert rc == 2
    assert "E_UNSUPPORTED_VIEW_FORM" in err
    assert str(model_path(project, "generated_source")) in err  # the failing file, not the reader
    assert tree_hash(project) == before


# ------------------------------------------------- rule 2 relation restriction (A1)

A1_ESCAPE_CASES = [
    (
        "dynamic-table-function",
        ("SELECT m.* FROM warehouse_metering_source m "
         "LEFT JOIN query_table('warehouses_' || 'source') w ON w.name=m.warehouse_name\n"),
    ),
    (
        "bare-attached-table",
        ("SELECT m.* FROM warehouse_metering_source m "
         "JOIN show_warehouses w ON w.name=m.warehouse_name\n"),
    ),
    (
        "qualified-attached-table",
        ("SELECT m.* FROM warehouse_metering_source m "
         "JOIN fixture.main.show_warehouses w ON w.name=m.warehouse_name\n"),
    ),
    (
        "second-file-scalar-subquery",
        ("SELECT m.*, (SELECT COUNT(*) FROM fixture2.main.product_catalog) AS outside_count "
         "FROM warehouse_metering_source m\n"),
    ),
    (
        "memory-catalog",
        ("SELECT m.* FROM warehouse_metering_source m "
         "JOIN memory.main.warehouses_source w ON w.name=m.warehouse_name\n"),
    ),
    (
        "cte-shadows-attached-table",
        ("WITH show_warehouses AS (SELECT * FROM warehouse_metering_source) "
         "SELECT * FROM show_warehouses\n"),
    ),
]


@pytest.mark.parametrize(
    ("label", "text"), A1_ESCAPE_CASES, ids=[c[0] for c in A1_ESCAPE_CASES]
)
def test_a1_relation_escapes_refuse_init_and_write_nothing(tmp_path, capsys, label, text):
    """A1: a table function other than sqlite_scan, an attached raw relation (bare or
    qualified), a second file, the memory catalog, and a CTE shadowing an attached relation
    are all E_UNSUPPORTED_VIEW_FORM refusals with no writes. The committed project registers
    the operator sources, so acceptance would otherwise succeed (as it did before A1)."""
    project = _make_registered_project(tmp_path)
    write_model(project, "escape_model", text)
    before = tree_hash(project)
    rc, _, err = init_view(project, capsys, "escape_model")
    assert rc == 2, (label, err)
    assert "E_UNSUPPORTED_VIEW_FORM" in err
    assert str(model_path(project, "escape_model")) in err
    assert tree_hash(project) == before


def test_a1_the_stale_trust_graph_cannot_be_constructed(tmp_path, capsys):
    """A1: the review's stale-trust reproducer needs ``init`` to accept a dynamic
    ``query_table`` relation; the committed graph already registers the sources, so only
    the new relation restriction can refuse it."""
    project = _make_registered_project(tmp_path)
    write_model(
        project, "zz_review_dynamic",
        'SELECT m."usage_day", m."warehouse_name", w."size" AS warehouse_size,\n'
        '       w."cost_center", m."credits_used"\n'
        "FROM warehouse_metering_source m\n"
        "LEFT JOIN query_table('warehouses_' || 'source') w ON w.name=m.warehouse_name\n",
    )
    before = tree_hash(project)
    rc, _, err = init_view(project, capsys, "zz_review_dynamic")
    assert rc == 2
    assert "E_UNSUPPORTED_VIEW_FORM" in err and "query_table" in err
    assert tree_hash(project) == before


A1_POSITIVE_CASES = [
    (
        "cte-over-registered-model",
        "WITH src AS (SELECT * FROM warehouses_source) SELECT * FROM src\n",
    ),
    ("quoted-registered-model", 'SELECT * FROM "warehouses_source"\n'),
    ("mixed-case-registered-model", "SELECT * FROM Warehouses_Source\n"),
    (
        "in-list-registered-model",
        ("SELECT m.* FROM warehouse_metering_source m "
         "WHERE m.warehouse_name IN (SELECT name FROM warehouses_source)\n"),
    ),
    (
        "subquery-registered-model",
        "SELECT * FROM (SELECT * FROM warehouses_source) AS s\n",
    ),
]


@pytest.mark.parametrize(
    ("label", "text"), A1_POSITIVE_CASES, ids=[c[0] for c in A1_POSITIVE_CASES]
)
def test_a1_supported_relation_forms_still_init(tmp_path, capsys, label, text):
    """A1 positive cases: registered models remain readable through CTEs, quoting, case
    changes, subqueries and IN lists."""
    project = make_view_project(tmp_path)
    write_model(project, "positive_ref", text)
    rc, _out, err = init_view(project, capsys, "positive_ref")
    assert rc == 0, (label, err)
    rc, _, err = cli(project, capsys, "validate", "--model", "positive_ref")
    assert rc == 0, (label, err)


def test_the_engine_checks_registered_models_at_start(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    write_model(project, MODEL, ORIGINALS[MODEL].replace("SELECT\n", "SELECT CURRENT_DATE AS \"x\",\n", 1))
    engine = DuckDBEngine(load_config(project / "agent-bi.yaml"))  # D431: opens anyway
    try:
        with pytest.raises(AgentBiError) as exc:
            engine.require_rill_model(MODEL)
    finally:
        engine.close()
    assert exc.value.full_code == "E_NONDETERMINISTIC_VIEW"


def test_the_engine_creates_dependency_views_before_dependent_views(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    engine = DuckDBEngine(load_config(project / "agent-bi.yaml"))
    try:
        rows = engine.execute(
            f'SELECT "cost_center", SUM("credits_used") AS "total_credits_used" '
            f'FROM "memory"."main"."{MODEL}" GROUP BY "cost_center" ORDER BY 1'
        )[1]
    finally:
        engine.close()
    assert rows == [("DATA", 544.0), ("FIN", 2739.0), ("MKT", 1093.0)]


# ---------------------------------------------------------------------- init / config


def test_init_rill_model_registers_the_closure_and_writes_the_sidecar(tmp_path, capsys):
    project = make_view_project(tmp_path)
    rc, out, err = init_view(project, capsys)
    assert rc == 0, err
    assert "attach: reused alias 'fixture'" in out and "created" in out
    text = (project / "agent-bi.yaml").read_text()
    assert "# the engine block" in text and "# base_table map" in text  # comments survive
    cfg = yaml.safe_load(text)
    assert cfg["engines"]["duckdb"]["rill_models"] == CLOSURE
    assert cfg["engines"]["duckdb"]["relation_map"]["VIEWS.MAIN"] == "memory.main"
    model = load_model_dict(str(project / "semantic" / f"{MODEL}.yaml"))
    assert model["tables"][0]["base_table"] == {
        "database": "VIEWS", "schema": "MAIN", "table": MODEL}
    sidecar = yaml.safe_load((project / "semantic" / f"{MODEL}.provenance.yaml").read_text())
    assert sidecar["relation"]["kind"] == "view"
    assert sidecar["relation"]["source"] == f"rill/models/{MODEL}.sql"
    assert sidecar["relation"]["dependencies"] == [
        f"rill/models/{SOURCE}.sql", f"rill/models/{WAREHOUSES}.sql",
    ]
    assert sidecar["relation"]["models_digest"] == closure_digest(project)
    for name in CLOSURE:
        assert model_path(project, name).read_bytes() == (OPERATOR_DIR / f"{name}.sql").read_bytes()


def test_init_rill_model_is_idempotent(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    config = (project / "agent-bi.yaml").read_bytes()
    rc, out, err = init_view(project, capsys)
    assert rc == 0, err
    assert "wrote agent-bi.yaml" not in out
    assert (project / "agent-bi.yaml").read_bytes() == config
    sidecar = yaml.safe_load((project / "semantic" / f"{MODEL}.provenance.yaml").read_text())
    assert sidecar["relation"]["models_digest"] == closure_digest(project)


def test_init_rill_model_appends_the_attach_and_relation_map_when_needed(tmp_path, capsys):
    project = make_view_project(tmp_path, attach_idle=False, commented=False)
    rc, _out, err = init_view(project, capsys)
    assert rc == 0, err
    text = (project / "agent-bi.yaml").read_text()
    cfg = yaml.safe_load(text)
    attach = cfg["engines"]["duckdb"]["attach"][-1]
    assert attach["alias"] == MODEL and attach["type"] == "sqlite"
    assert attach["path"] == "tests/fixtures/idle-warehouse-waste.sqlite"
    assert cfg["engines"]["duckdb"]["relation_map"][f"{MODEL.upper()}.MAIN"] == f"{MODEL}.main"
    assert cfg["engines"]["duckdb"]["relation_map"]["VIEWS.MAIN"] == "memory.main"
    assert cfg["engines"]["duckdb"]["rill_models"] == CLOSURE
    # a second run reuses what it added
    assert init_view(project, capsys)[0] == 0
    assert (project / "agent-bi.yaml").read_text() == text


def test_init_view_form_requires_one_form_and_a_model_name(tmp_path, capsys):
    project = make_view_project(tmp_path)
    rc, _, err = cli(project, capsys, "init", "--rill-model", MODEL, "--table", "t")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_CONFIG_INVALID: ")
    rc, _, err = cli(project, capsys, "init", "--rill-model", "Bad")
    assert rc == 2 and "E_INVALID_MODEL_NAME" in err


def test_init_view_form_defaults_the_model_name_to_the_rill_model(tmp_path, capsys):
    project = make_view_project(tmp_path)
    rc, out, err = init_view(project, capsys)
    assert rc == 0 and "semantic/warehouse_metering_enriched.yaml" in out, err


def test_init_rill_model_with_a_different_semantic_model_name(tmp_path, capsys):
    project = make_view_project(tmp_path)
    rc, _, err = cli(
        project, capsys, "init", "--rill-model", MODEL, "--model", "enriched_metrics"
    )
    assert rc == 0, err
    model = load_model_dict(str(project / "semantic" / "enriched_metrics.yaml"))
    assert model["tables"][0]["base_table"]["table"] == MODEL
    assert cli(project, capsys, "compile-rill", "--model", "enriched_metrics")[0] == 0
    view = yaml.safe_load((project / "rill/metrics/enriched_metrics.yaml").read_text())
    assert view["model"] == MODEL and view["display_name"] == "enriched_metrics"
    canvas = yaml.safe_load(
        (project / "rill/dashboards/enriched_metrics_default_canvas.yaml").read_text()
    )
    assert canvas["rows"][0]["items"][0]["kpi_grid"]["metrics_view"] == "enriched_metrics"
    sidecar = yaml.safe_load((project / "semantic/enriched_metrics.provenance.yaml").read_text())
    assert sidecar["relation"]["source"] == f"rill/models/{MODEL}.sql"
    assert sidecar["relation"]["models_digest"] == closure_digest(project)


# --------------------------------------------------------------------- engine / scan


def test_engine_creates_the_view_and_queries_it_verbatim(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    engine = DuckDBEngine(load_config(project / "agent-bi.yaml"))
    try:
        columns, rows = engine.execute(
            f'SELECT "cost_center", SUM("credits_used") AS "total_credits_used" '
            f'FROM "memory"."main"."{MODEL}" GROUP BY "cost_center" ORDER BY 1'
        )
    finally:
        engine.close()
    assert columns == ["cost_center", "total_credits_used"]
    assert rows == [("DATA", 544.0), ("FIN", 2739.0), ("MKT", 1093.0)]


def test_data_alias_resolves_the_closures_sqlite_file(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    engine = DuckDBEngine(load_config(project / "agent-bi.yaml"))
    try:
        assert engine.data_alias("VIEWS", "MAIN", MODEL) == "fixture"
        assert engine.data_alias("FIXTURE", "MAIN", "warehouse_metering_history") == "fixture"
    finally:
        engine.close()


def test_data_alias_without_the_attach_entry_is_config_invalid(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    cfg_path = project / "agent-bi.yaml"
    doc = yaml.safe_load(cfg_path.read_text())
    doc["engines"]["duckdb"]["attach"] = []
    cfg_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    engine = DuckDBEngine(load_config(cfg_path))
    try:
        with pytest.raises(AgentBiError) as exc:
            engine.data_alias("VIEWS", "MAIN", MODEL)
    finally:
        engine.close()
    assert exc.value.full_code == "E_CONFIG_INVALID"
    assert str(model_path(project)) in str(exc.value)


def test_scan_refresh_recomputes_dependencies_and_closure_digest(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    write_model(project, SOURCE, ORIGINALS[SOURCE].replace("SELECT *", "SELECT *, 0 AS \"x\"", 1))
    rc, _, err = cli(
        project, capsys, "scan", "--engine", "duckdb", "--relation", f"VIEWS.MAIN.{MODEL}",
        "--model", MODEL, "--refresh",
    )
    assert rc == 0, err
    sidecar = yaml.safe_load((project / "semantic" / f"{MODEL}.provenance.yaml").read_text())
    assert sidecar["relation"]["source"] == f"rill/models/{MODEL}.sql"
    assert sidecar["relation"]["dependencies"] == [
        f"rill/models/{SOURCE}.sql", f"rill/models/{WAREHOUSES}.sql",
    ]
    assert sidecar["relation"]["models_digest"] == closure_digest(project)


# ----------------------------------------------------------------- compile-rill / validate


def test_compile_rill_for_a_view_writes_no_model_file(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    rc, _out, err = cli(project, capsys, "compile-rill", "--model", MODEL)
    assert rc == 0, err
    for name in CLOSURE:
        assert model_path(project, name).read_bytes() == (OPERATOR_DIR / f"{name}.sql").read_bytes()
    metrics = yaml.safe_load((project / "rill" / "metrics" / f"{MODEL}.yaml").read_text())
    assert metrics["model"] == MODEL
    explore = yaml.safe_load((project / "rill" / "dashboards" / f"{MODEL}_explore.yaml").read_text())
    assert explore["metrics_view"] == MODEL
    manifest = json.loads((project / "rill" / "provenance" / f"{MODEL}.json").read_text())
    assert manifest["models_digest"] == closure_digest(project)
    assert f"rill/models/{MODEL}.sql" not in [f["path"] for f in manifest["files"]]


def test_compile_rill_refuses_to_overwrite_an_operator_table_model(tmp_path):
    from agent_bi.compile_rill import compile_rill

    project = make_view_project(tmp_path, commented=False)
    (project / "semantic").mkdir(exist_ok=True)
    shutil.copy(ROOT / "semantic" / "idle_warehouse_waste.yaml", project / "semantic")
    shutil.copy(ROOT / "semantic" / "idle_warehouse_waste.provenance.yaml", project / "semantic")
    operator = project / "rill" / "models" / "idle_warehouse_waste.sql"
    operator.write_text("-- an operator model\nSELECT 1 AS \"a\"\n", encoding="utf-8")
    before = tree_hash(project)
    with pytest.raises(AgentBiError) as exc:
        compile_rill("idle_warehouse_waste", load_config(project / "agent-bi.yaml"), project)
    assert exc.value.full_code == "E_OPERATOR_FILE" and exc.value.exit_code == 2
    assert "idle_warehouse_waste.sql" in str(exc.value)
    assert tree_hash(project) == before


def test_compile_rill_for_a_view_is_deterministic(tmp_path, capsys):
    from agent_bi.compile_rill import compile_rill

    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    cfg = load_config(project / "agent-bi.yaml")
    compile_rill(MODEL, cfg, project, build_id="20261016T000001Z")
    first = {
        p.relative_to(project).as_posix(): p.read_bytes()
        for p in (project / "rill").rglob("*") if p.is_file()
    }
    first_manifest = json.loads((project / "rill/provenance" / f"{MODEL}.json").read_text())
    compile_rill(MODEL, cfg, project, build_id="20261016T000002Z")
    second = {
        p.relative_to(project).as_posix(): p.read_bytes()
        for p in (project / "rill").rglob("*") if p.is_file()
    }
    for rel, text in first.items():
        if not rel.endswith(f"provenance/{MODEL}.json"):
            assert second[rel] == text, rel
    second_manifest = json.loads((project / "rill/provenance" / f"{MODEL}.json").read_text())
    assert first_manifest.pop("build_id") != second_manifest.pop("build_id")
    assert second_manifest == first_manifest
    assert first_manifest["models_digest"] == closure_digest(project)


def test_diff_reports_no_drift_for_the_view_model(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    before = tree_hash(project)
    rc, out, err = cli(project, capsys, "diff", "--model", MODEL, "--engine", "duckdb")
    assert rc == 0, err
    assert "no drift" in out
    assert tree_hash(project) == before  # diff writes nothing


def test_validate_runs_rules_2_and_3_on_the_registered_graph(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    rc, _, err = cli(project, capsys, "validate", "--model", MODEL)
    assert rc == 0, err and "OK"
    write_model(project, MODEL, ORIGINALS[MODEL].replace('  m."credits_used"\n', '  m."credits_used" + CURRENT_DATE\n'))
    rc, _, err = cli(project, capsys, "validate", "--model", MODEL)
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_NONDETERMINISTIC_VIEW: ")
    # a closure-level refusal (no scan anywhere) is caught by validate too
    write_model(project, MODEL, ORIGINALS[MODEL])
    write_model(project, SOURCE, 'SELECT 1 AS "usage_day"\n')
    rc, _, err = cli(project, capsys, "validate", "--model", MODEL)
    assert rc == 2 and "E_UNSUPPORTED_VIEW_FORM" in err


def test_no_join_is_emitted_for_the_view_model(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    assert cli(project, capsys, "compile-rill", "--model", MODEL)[0] == 0
    manifest = json.loads((project / "rill/provenance" / f"{MODEL}.json").read_text())
    generated = [project / f["path"] for f in manifest["files"]]
    generated += [
        project / "semantic" / f"{MODEL}.yaml",
        project / "semantic" / f"{MODEL}.provenance.yaml",
    ]
    joined = [
        str(path.relative_to(project)) for path in generated
        if path.is_file() and "join" in path.read_text(encoding="utf-8").lower()
    ]
    assert joined == [], joined
    qpath = project / "out" / "q1.json"
    qpath.parent.mkdir(exist_ok=True)
    qpath.write_text(json.dumps(QUERY_COST_CENTER), encoding="utf-8")
    rc, out, err = cli(project, capsys, "compile-sql", "--model", MODEL, "--query-file",
                       str(qpath), "--engine", "duckdb")
    assert rc == 0, err
    assert " join " not in out.lower()


# ------------------------------------------------------------------------ the example


def test_the_two_task_4_queries_verify_in_scratch(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    create_both_verified(project, capsys)
    rc, out, err = cli(project, capsys, "verify", "--model", MODEL)
    assert rc == 0, err
    assert out.splitlines() == [
        "PASS credits_by_cost_center", "PASS credits_by_size_by_month",
    ]


def test_receipts_and_manifests_carry_the_current_closure_digest(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    env = create_verified(
        project, capsys, "credits_by_cost_center", "credits by cost center",
        SQL_COST_CENTER, QUERY_COST_CENTER,
    )
    current = closure_digest(project)
    assert env["provenance"]["artifact"]["models_digest"] == current
    ka = json.loads(
        (project / "tests/fixtures/known-answers" / MODEL / "credits_by_cost_center.json").read_text()
    )
    assert ka["definition_digests"]["models_digest"] == current
    sidecar = yaml.safe_load((project / "semantic" / f"{MODEL}.provenance.yaml").read_text())
    assert sidecar["verified_queries"]["credits_by_cost_center"]["definition_digests"][
        "models_digest"] == current
    assert cli(project, capsys, "compile-rill", "--model", MODEL)[0] == 0
    manifest = json.loads((project / "rill/provenance" / f"{MODEL}.json").read_text())
    assert manifest["models_digest"] == current
    model = load_model_dict(str(project / "semantic" / f"{MODEL}.yaml"))
    assert current_models_digest(project / "rill", model, CLOSURE) == current


def test_the_ask_receipt_uses_the_current_digest_not_the_sidecar_copy(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    create_both_verified(project, capsys)
    sidecar_before = yaml.safe_load(
        (project / "semantic" / f"{MODEL}.provenance.yaml").read_text()
    )["relation"]["models_digest"]
    write_model(project, MODEL, ORIGINALS[MODEL].replace('  m."credits_used"\n', '  m."credits_used" + 0 AS "credits_used"\n'))
    current = closure_digest(project)
    assert current != sidecar_before
    qpath = project / "out" / "q.json"
    qpath.write_text(json.dumps(QUERY_COST_CENTER), encoding="utf-8")
    rc, out, err = cli(project, capsys, "ask", "--model", MODEL, "--query-file", str(qpath), "--json")
    assert rc == 0, err
    env = json.loads(out)
    assert env["provenance"]["artifact"]["models_digest"] == current
    definition = env["provenance"]["receipt"]["evidence"]["definition"]
    assert definition["state"] == "stale"
    assert "upstream Rill model changed" in definition["claim"]


# --------------------------------------------------------------------------- staleness


def test_a_meaningful_edit_to_the_join_model_makes_both_queries_stale(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    create_both_verified(project, capsys)
    write_model(project, MODEL, ORIGINALS[MODEL].replace('  m."credits_used"\n', '  m."credits_used" + 0 AS "credits_used"\n'))
    rc, out, err = cli(project, capsys, "verify", "--model", MODEL)
    assert rc == 1, err
    stale = [ln for ln in out.splitlines() if ln.startswith("STALE ")]
    assert stale == [
        "STALE credits_by_cost_center: models_digest",
        "STALE credits_by_size_by_month: models_digest",
    ]
    rc, out, err = cli(project, capsys, "ask", "--model", MODEL,
                       "--question", "credits by cost center", "--json")
    assert rc == 0, err
    env = json.loads(out)
    definition = env["provenance"]["receipt"]["evidence"]["definition"]
    assert definition["state"] == "stale"
    assert "upstream Rill model changed" in definition["claim"]


def test_a_meaningful_edit_to_an_upstream_source_makes_both_queries_stale(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    create_both_verified(project, capsys)
    write_model(
        project, WAREHOUSES,
        ORIGINALS[WAREHOUSES].replace(
            "SELECT * FROM", 'SELECT "name", "size", "cost_center" FROM', 1
        ),
    )
    rc, out, err = cli(project, capsys, "verify", "--model", MODEL)
    assert rc == 1, err
    assert [ln for ln in out.splitlines() if ln.startswith("STALE ")] == [
        "STALE credits_by_cost_center: models_digest",
        "STALE credits_by_size_by_month: models_digest",
    ]


def test_changing_an_unrelated_registered_model_is_not_stale(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    unrelated = f"SELECT * FROM sqlite_scan('{SQLITE_REL}', 'show_warehouses')\n"
    write_model(project, "unrelated_source", unrelated)
    register_extra(project, "unrelated_source")
    create_both_verified(project, capsys)
    before = models_digest(project / "rill", MODEL, CLOSURE + ["unrelated_source"])
    write_model(project, "unrelated_source", unrelated + "-- a meaningful edit\n")
    after = models_digest(project / "rill", MODEL, CLOSURE + ["unrelated_source"])
    assert after == before
    rc, out, err = cli(project, capsys, "verify", "--model", MODEL)
    assert rc == 0, (out, err)
    assert out.splitlines() == [
        "PASS credits_by_cost_center", "PASS credits_by_size_by_month",
    ]


CHAIN_C = f"SELECT * FROM sqlite_scan('{SQLITE_REL}', 'warehouse_metering_history')\n"
CHAIN_B = "SELECT * FROM chain_c\n"
CHAIN_A = "SELECT * FROM chain_b\n"
CHAIN_QUERY = {
    "schema_version": "semantic-query/1.0", "model": "chain_a", "table": "chain_a",
    "metrics": ["total_credits_used"],
}
CHAIN_SQL = "SELECT SUM(credits_used) AS total_credits_used FROM warehouse_metering_history"


def test_a_transitive_dependency_change_makes_the_top_model_stale(tmp_path, capsys):
    project = make_view_project(tmp_path)
    for name, text in (("chain_c", CHAIN_C), ("chain_b", CHAIN_B), ("chain_a", CHAIN_A)):
        write_model(project, name, text)
    rc, _, err = init_view(project, capsys, "chain_a")
    assert rc == 0, err
    create_verified(
        project, capsys, "total_chain", "total chain credits", CHAIN_SQL, CHAIN_QUERY,
        model="chain_a",
    )
    before = models_digest(project / "rill", "chain_a", ["chain_a", "chain_b", "chain_c"])
    write_model(project, "chain_c", CHAIN_C.replace("SELECT *", 'SELECT *, 0 AS "zero"', 1))
    after = models_digest(project / "rill", "chain_a", ["chain_a", "chain_b", "chain_c"])
    assert after != before
    rc, out, err = cli(project, capsys, "verify", "--model", "chain_a")
    assert rc == 1, err
    assert [ln for ln in out.splitlines() if ln.startswith("STALE ")] == [
        "STALE total_chain: models_digest"
    ]


def test_a_whitespace_or_crlf_only_edit_to_any_closure_file_keeps_pass(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    create_both_verified(project, capsys)
    for name in CLOSURE:
        write_model(project, name, ORIGINALS[name].replace("\n", "  \r\n"))
        assert file_digest(model_path(project, name)) == file_digest(OPERATOR_DIR / f"{name}.sql")
    assert closure_digest(project) == closure_digest_from_repo()
    rc, out, err = cli(project, capsys, "verify", "--model", MODEL)
    assert rc == 0, (out, err)
    assert "STALE" not in out
    for name in CLOSURE:
        write_model(project, name, ORIGINALS[name])
    assert cli(project, capsys, "verify", "--model", MODEL)[0] == 0


def closure_digest_from_repo() -> str:
    return models_digest(ROOT / "rill", MODEL, CLOSURE)


def test_save_verify_force_with_the_same_sql_clears_the_stale_state(tmp_path, capsys):
    project = make_view_project(tmp_path)
    assert init_view(project, capsys)[0] == 0
    create_both_verified(project, capsys)
    write_model(project, SOURCE, ORIGINALS[SOURCE].replace("SELECT *", "SELECT *, 0 AS \"x\"", 1))
    assert cli(project, capsys, "verify", "--model", MODEL)[0] == 1
    for name, question, sql, query in (
        ("credits_by_cost_center", "credits by cost center", SQL_COST_CENTER, QUERY_COST_CENTER),
        ("credits_by_size_by_month", "credits by warehouse size by month", SQL_SIZE_BY_MONTH,
         QUERY_SIZE_BY_MONTH),
    ):
        qpath = project / "out" / f"{name}.q.json"
        qpath.write_text(json.dumps(query), encoding="utf-8")
        rc, out, err = cli(project, capsys, "ask", "--model", MODEL, "--query-file", str(qpath), "--json")
        assert rc == 0, err
        env = json.loads(out)
        answer = project / "out" / (
            env["provenance"]["artifact"]["query_hash"].removeprefix("sha256:") + ".answer.json"
        )
        rc, _, err = cli(
            project, capsys, "save", "--from", str(answer), "--name", name,
            "--question", question, "--verify", "--by", "sprint-6",
            "--expected-sql", sql, "--force",
        )
        assert rc == 0, err
    rc, out, err = cli(project, capsys, "verify", "--model", MODEL)
    assert rc == 0, (out, err)
    assert "STALE" not in out


def test_the_operator_files_are_byte_identical_after_every_command(tmp_path, capsys):
    project = make_view_project(tmp_path)
    before = {name: model_path(project, name).read_bytes() for name in CLOSURE}
    assert init_view(project, capsys)[0] == 0
    assert cli(project, capsys, "compile-rill", "--model", MODEL)[0] == 0
    create_both_verified(project, capsys)
    assert cli(project, capsys, "verify", "--model", MODEL)[0] == 0
    for name in CLOSURE:
        assert model_path(project, name).read_bytes() == before[name]
        assert model_path(project, name).read_bytes() == (OPERATOR_DIR / f"{name}.sql").read_bytes()
        assert SHAS[name] == "sha256:" + hashlib.sha256(before[name]).hexdigest()


# ------------------------------------------------------------------ table regression


def test_table_model_digests_are_unchanged_by_the_models_digest_parameter():
    model = load_model_dict(str(ROOT / "semantic" / "idle_warehouse_waste.yaml"))
    query = {"model": "idle_warehouse_waste", "table": "warehouse_metering_history",
             "metrics": ["total_credits_used"], "dimensions": ["warehouse_name"]}
    assert semantic_digest(query, model) == semantic_digest(query, model, None)
    assert "models_digest" not in definition_digests(query, model)


def test_the_committed_example_verifies_on_the_repo():
    """The task 4 artifacts exist and verify as committed (two PASS, exit 0)."""
    from agent_bi.verify.known_answers import verify

    result = verify(MODEL, "duckdb", load_config(ROOT / "agent-bi.yaml"), ROOT)
    assert result.exit_code == 0
    assert result.results == {"credits_by_cost_center": "PASS", "credits_by_size_by_month": "PASS"}
    assert result.lines == ["PASS credits_by_cost_center", "PASS credits_by_size_by_month"]


# ------------------------------------------------- failure isolation (D431, §5.13 rule 5)


def _make_registered_project(tmp_path: Path) -> Path:
    """An s4 scratch project whose committed config registers the view model graph."""
    from tests.s4_helpers import make_s4_project

    return make_s4_project(tmp_path)


def _drop_fixture_column(project: Path, column: str) -> None:
    import sqlite3

    con = sqlite3.connect(project / "tests" / "fixtures" / "idle-warehouse-waste.sqlite")
    try:
        con.execute(f"ALTER TABLE warehouse_metering_history DROP COLUMN {column}")
        con.commit()
    finally:
        con.close()


def test_a_broken_registered_view_does_not_block_the_table_model_diff(tmp_path, capsys):
    """D431: a view whose source lost a column is recorded, not raised, so diff reports the
    table model's own drift (exit 1) naming the removed column and its dependents."""
    project = _make_registered_project(tmp_path)
    _drop_fixture_column(project, "credits_used_compute")
    before = tree_hash(project)
    rc, out, err = cli(
        project, capsys, "diff", "--engine", "duckdb", "--model", "idle_warehouse_waste"
    )
    assert rc == 1, err
    assert "credits_used_compute" in out
    assert "total_credits_used_compute" in out
    assert "verified_queries[avg_compute_by_warehouse]" in out
    assert "verified_queries[compute_and_cloud_by_warehouse]" in out
    assert tree_hash(project) == before


def test_a_broken_registered_view_refuses_its_own_commands(tmp_path, capsys):
    """D431: every command whose relation is the broken view raises the recorded
    E_UNSUPPORTED_VIEW_FORM naming the model file, and writes nothing."""
    project = _make_registered_project(tmp_path)
    _drop_fixture_column(project, "credits_used_compute")
    before = tree_hash(project)
    commands = (
        ("verify", "--model", MODEL),
        ("diff", "--engine", "duckdb", "--model", MODEL),
        ("compile-rill", "--model", MODEL),
        ("validate", "--model", MODEL),
    )
    for command in commands:
        rc, _, err = cli(project, capsys, *command)
        assert rc == 2, (command, err)
        assert "E_UNSUPPORTED_VIEW_FORM" in err
        assert str(model_path(project)) in err
        assert tree_hash(project) == before


def test_a_source_broken_by_a_dropped_column_refuses_the_join_commands(tmp_path, capsys):
    """D431 + build-validity invariant: a broken upstream source makes the join model's
    verify, ask and validate fail with the recorded error naming the source file."""
    project = _make_registered_project(tmp_path)
    write_model(project, SOURCE, ORIGINALS[SOURCE].replace(
        "SELECT * FROM", 'SELECT "credits_used_compute" FROM', 1))
    _drop_fixture_column(project, "credits_used_compute")
    before = tree_hash(project)
    commands = (
        ("verify", "--model", MODEL),
        ("validate", "--model", MODEL),
        ("ask", "--model", MODEL, "--question", "credits by cost center", "--json"),
    )
    for command in commands:
        rc, _, err = cli(project, capsys, *command)
        assert rc == 2, (command, err)
        assert "E_UNSUPPORTED_VIEW_FORM" in err
        assert str(model_path(project, SOURCE)) in err
        assert tree_hash(project) == before


def test_a_broken_registered_model_outside_the_closure_blocks_nothing(tmp_path, capsys):
    """D431: a registered model that fails to build never blocks another graph's validate
    or verify."""
    project = _make_registered_project(tmp_path)
    write_model(project, "outside_broken", 'SELECT "no_such_column" FROM sqlite_scan(\'../tests/fixtures/idle-warehouse-waste.sqlite\', \'warehouse_metering_history\')\n')
    register_extra(project, "outside_broken")
    before = tree_hash(project)
    rc, _, err = cli(project, capsys, "validate", "--model", MODEL)
    assert rc == 0, err
    rc, out, err = cli(project, capsys, "verify", "--model", MODEL)
    assert rc == 0, err
    assert out.splitlines() == [
        "PASS credits_by_cost_center", "PASS credits_by_size_by_month",
    ]
    assert tree_hash(project) == before


def test_a_nondeterministic_registered_view_does_not_block_the_table_model(tmp_path, capsys):
    """D431: NOW() in the operator file is recorded at engine start; the table model still
    verifies all nine known answers."""
    project = _make_registered_project(tmp_path)
    operator = project / "rill" / "models" / f"{MODEL}.sql"
    operator.write_text(operator.read_text().replace("SELECT\n", 'SELECT NOW() AS "x",\n', 1))
    before = tree_hash(project)
    rc, out, err = cli(project, capsys, "verify", "--model", "idle_warehouse_waste")
    assert rc == 0, err
    assert sum(1 for line in out.splitlines() if line.startswith("PASS ")) == 9
    assert tree_hash(project) == before


def test_a_nondeterministic_registered_view_refuses_verify_and_validate(tmp_path, capsys):
    """D431: verify and validate of the view model raise E_NONDETERMINISTIC_VIEW naming the
    model file, and write nothing."""
    project = _make_registered_project(tmp_path)
    operator = project / "rill" / "models" / f"{MODEL}.sql"
    operator.write_text(operator.read_text().replace("SELECT\n", 'SELECT NOW() AS "x",\n', 1))
    before = tree_hash(project)
    for command in (("verify", "--model", MODEL), ("validate", "--model", MODEL)):
        rc, _, err = cli(project, capsys, *command)
        assert rc == 2, (command, err)
        assert "E_NONDETERMINISTIC_VIEW" in err
        assert str(operator) in err
        assert tree_hash(project) == before


# --------------------------------------------- missing-file isolation (A2, rule 5)


REVIEW_EXTRA_MODEL = (
    "SELECT * FROM sqlite_scan('../tests/fixtures/review-extra.sqlite', 'product_catalog')\n"
)


def test_a_missing_unrelated_attach_isolates_its_graph(tmp_path, capsys):
    """A2: one missing attachment (an unrelated registered graph) is recorded per alias
    and per model; the selected view graph and the table model are unaffected, and only
    uses of the broken alias/model fail with their recorded errors."""
    project = _make_registered_project(tmp_path)
    (project / "tests" / "fixtures" / "review-extra.sqlite").write_bytes(
        (project / "tests" / "fixtures" / "holiday-cookout-rush.sqlite").read_bytes()
    )
    write_model(project, "review_extra", REVIEW_EXTRA_MODEL)
    assert init_view(project, capsys, "review_extra")[0] == 0
    rc, out, err = cli(project, capsys, "verify", "--model", MODEL)
    assert rc == 0, err
    assert out.splitlines() == [
        "PASS credits_by_cost_center", "PASS credits_by_size_by_month",
    ]
    (project / "tests" / "fixtures" / "review-extra.sqlite").rename(
        project / "tests" / "fixtures" / "review-extra.moved"
    )
    for command in (
        ("validate", "--model", MODEL),
        ("verify", "--model", MODEL),
        ("ask", "--model", MODEL, "--question", "credits by cost center", "--json"),
        ("compile-rill", "--model", MODEL),
    ):
        rc, out, err = cli(project, capsys, *command)
        assert rc == 0, (command, err)
        assert "Traceback" not in err
    # the failed alias re-raises its recorded E_ENGINE error unchanged (exit 3)
    engine = DuckDBEngine(load_config(project / "agent-bi.yaml"))
    try:
        for use in (
            lambda: engine.attach_path("review_extra"),
            lambda: engine.attach_type("review_extra"),
            lambda: engine.as_of("review_extra"),
            lambda: engine.data_alias("REVIEW_EXTRA", "MAIN", "product_catalog"),
            lambda: engine.execute('SELECT * FROM "review_extra"."main"."product_catalog"'),
            lambda: engine.execute_with_as_of('SELECT 1 AS "x"', "review_extra"),
        ):
            with pytest.raises(AgentBiError) as exc:
                use()
            assert exc.value.full_code == "E_ENGINE" and exc.value.exit_code == 3
            assert "attach path does not exist" in exc.value.message
    finally:
        engine.close()
    # commands for the broken graph fail with its recorded rule 2 error, writing nothing
    before = tree_hash(project)
    for command in (
        ("validate", "--model", "review_extra"),
        ("verify", "--model", "review_extra"),
        ("ask", "--model", "review_extra", "--question", "x", "--json"),
        ("scan", "--engine", "duckdb", "--relation", "VIEWS.MAIN.review_extra",
         "--model", "review_extra", "--refresh"),
    ):
        rc, _, err = cli(project, capsys, *command)
        assert rc == 2, (command, err)
        assert "E_UNSUPPORTED_VIEW_FORM" in err
        assert str(model_path(project, "review_extra")) in err
    assert tree_hash(project) == before
    # the table model's commands are unaffected
    rc, out, err = cli(project, capsys, "verify", "--model", "idle_warehouse_waste")
    assert rc == 0, err
    assert sum(1 for line in out.splitlines() if line.startswith("PASS ")) == 9
    rc, _, err = cli(
        project, capsys, "diff", "--engine", "duckdb", "--model", "idle_warehouse_waste"
    )
    assert rc == 0, err and "Traceback" not in err
    rc, _, err = cli(
        project, capsys, "ask", "--model", "idle_warehouse_waste", "--question",
        "credits by warehouse", "--json",
    )
    assert rc == 0, err and "Traceback" not in err


def test_a_missing_rill_directory_does_not_block_table_models(tmp_path, capsys):
    """A2: when the Rill directory is gone every registered model is unavailable through
    rule 2, the working-directory context stays off, and table models are served normally
    with no traceback."""
    project = _make_registered_project(tmp_path)
    shutil.rmtree(project / "rill")
    before = tree_hash(project)
    rc, _, err = cli(project, capsys, "validate", "--model", MODEL)
    assert rc == 2, err
    assert "E_UNSUPPORTED_VIEW_FORM" in err
    assert tree_hash(project) == before  # the refusal writes nothing
    rc, out, err = cli(project, capsys, "verify", "--model", "idle_warehouse_waste")
    assert rc == 0, err and "Traceback" not in err
    assert sum(1 for line in out.splitlines() if line.startswith("PASS ")) == 9
    rc, _, err = cli(
        project, capsys, "diff", "--engine", "duckdb", "--model", "idle_warehouse_waste"
    )
    assert rc == 0, err and "Traceback" not in err
    rc, _, err = cli(
        project, capsys, "ask", "--model", "idle_warehouse_waste", "--question",
        "credits by warehouse", "--json",
    )
    assert rc == 0, err and "Traceback" not in err


def test_a_table_models_own_missing_file_still_fails_with_exit_3(tmp_path, capsys):
    """A2: per-alias recording does not change the table model's own missing-file failure
    (E_ENGINE, exit 3), exactly as today."""
    project = _make_registered_project(tmp_path)
    (project / "tests" / "fixtures" / "idle-warehouse-waste.sqlite").rename(
        project / "tests" / "fixtures" / "idle-warehouse-waste.moved"
    )
    rc, _, err = cli(project, capsys, "verify", "--model", "idle_warehouse_waste")
    assert rc == 3
    assert "E_ENGINE" in err and "attach path does not exist" in err
    rc, _, err = cli(
        project, capsys, "ask", "--model", "idle_warehouse_waste", "--question",
        "credits by warehouse", "--json",
    )
    assert rc == 3
    assert "E_ENGINE" in err and "attach path does not exist" in err
