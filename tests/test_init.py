"""Sprint 3 task 3: ``init`` and the attach-alias rule (D394)."""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from agent_bi.initialize import plan_config
from agent_bi.main import main
from tests.conftest import IDLE
from tests.s3_helpers import MODEL, manifests, tree_hash
from tests.s6_helpers import make_project, strip_rill_models

ARGS = ["--sqlite", "tests/fixtures/idle-warehouse-waste.sqlite", "--table",
        "warehouse_metering_history", "--model", MODEL]


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = make_project(tmp_path)
    monkeypatch.chdir(p)
    return p


def _without_scanned_at(text):
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("scanned_at:"))


def run(capsys, project, *args):
    capsys.readouterr()
    rc = main(["--config", str(project / "agent-bi.yaml"), *args])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_init_on_an_existing_model_is_a_noop_except_scanned_at(project, capsys):
    before = {p.name: p.read_text() for p in (project / "semantic").iterdir()}
    config_before = (project / "agent-bi.yaml").read_text()
    rc, out, err = run(capsys, project, "init", *ARGS)
    assert rc == 0, err
    assert "reused alias 'fixture'" in out and "refreshed" in out
    assert "Next: rill start rill --no-open" in out
    assert (project / "agent-bi.yaml").read_text() == config_before  # nothing added
    for name, text in before.items():
        now = (project / "semantic" / name).read_text()
        if name == f"{MODEL}.provenance.yaml":
            assert _without_scanned_at(now) == _without_scanned_at(text)
        else:
            assert now == text, name
    assert len(manifests(project)) == 1
    assert (project / f"rill/dashboards/{MODEL}_default_canvas.yaml").is_file()


def test_init_keeps_the_eight_verified_queries(project, capsys):
    assert run(capsys, project, "init", *ARGS)[0] == 0
    model = yaml.safe_load((project / f"semantic/{MODEL}.yaml").read_text())
    assert len(model["verified_queries"]) == 9


def test_init_twice_is_stable(project, capsys):
    assert run(capsys, project, "init", *ARGS)[0] == 0
    first = {p: p.read_bytes() for p in (project / "rill").rglob("*") if p.is_file()
             and "provenance" not in p.parts}
    assert run(capsys, project, "init", *ARGS)[0] == 0
    assert {p: p.read_bytes() for p in first} == first


def test_a_new_file_appends_the_attach_and_relation_map_entries(project, capsys):
    data = project / "data"
    data.mkdir()
    shutil.copy(IDLE, data / "copy.sqlite")
    rc, out, err = run(capsys, project, "init", "--sqlite", "data/copy.sqlite", "--table",
                       "warehouse_metering_history", "--model", "waste_copy")
    assert rc == 0, err
    assert "added alias 'waste_copy'" in out and "created" in out
    text = (project / "agent-bi.yaml").read_text()
    assert "- {path: data/copy.sqlite, alias: waste_copy, type: sqlite}" in text
    assert '"WASTE_COPY.MAIN": "waste_copy.main"' in text
    assert "# or a .duckdb path" in text  # comments survive the edit
    cfg = yaml.safe_load(text)
    assert cfg["engines"]["duckdb"]["attach"][-1] == {
        "path": "data/copy.sqlite", "alias": "waste_copy", "type": "sqlite"}
    assert cfg["engines"]["duckdb"]["relation_map"]["WASTE_COPY.MAIN"] == "waste_copy.main"
    model = yaml.safe_load((project / "semantic/waste_copy.yaml").read_text())
    assert model["tables"][0]["base_table"] == {
        "database": "WASTE_COPY", "schema": "MAIN", "table": "warehouse_metering_history"}
    sql = (project / "rill/models/waste_copy.sql").read_text()
    assert "sqlite_scan('../data/copy.sqlite'" in sql
    # a second init reuses the entry it just added
    assert run(capsys, project, "init", "--sqlite", "data/copy.sqlite", "--table",
               "warehouse_metering_history", "--model", "waste_copy")[0] == 0
    assert (project / "agent-bi.yaml").read_text() == text


def test_the_same_file_is_matched_by_resolved_absolute_path(project, tmp_path):
    cwd = project / "sub"
    cwd.mkdir()
    plan = plan_config(project / "agent-bi.yaml", "../tests/fixtures/idle-warehouse-waste.sqlite",
                       "anything", cwd)
    assert plan.reused and plan.alias == "fixture" and plan.database == "FIXTURE"
    assert plan.new_text is None
    plan = plan_config(project / "agent-bi.yaml", str(project / "tests/fixtures/holiday-cookout-rush.sqlite"),
                       "anything", cwd)
    assert plan.reused and plan.alias == "fixture2" and plan.database == "FIXTURE2"


def test_init_refuses_a_model_that_models_another_relation(project, capsys):
    rc, _, err = run(capsys, project, "init", "--sqlite", "tests/fixtures/idle-warehouse-waste.sqlite",
                     "--table", "query_history", "--model", MODEL)
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_MODEL_EXISTS: ")


def test_init_failure_changes_nothing(project, capsys):
    before = tree_hash(project)
    rc, _, err = run(capsys, project, "init", "--sqlite", "tests/fixtures/idle-warehouse-waste.sqlite",
                     "--table", "no_such_table", "--model", "fresh")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_RELATION_NOT_FOUND: ")
    assert tree_hash(project) == before


def test_init_rejects_an_invalid_model_name(project, capsys):
    rc, _, err = run(capsys, project, "init", "--sqlite", "tests/fixtures/idle-warehouse-waste.sqlite",
                     "--table", "warehouse_metering_history", "--model", "Bad Name")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_INVALID_MODEL_NAME: ")


def test_the_attach_is_dumped_when_the_config_layout_cannot_be_edited_in_place(tmp_path):
    cfg = tmp_path / "agent-bi.yaml"
    cfg.write_text("project: p\nengines: {duckdb: {database: ':memory:', attach: [], relation_map: {}}}\n")
    plan = plan_config(cfg, "x.sqlite", "m", tmp_path)
    assert not plan.reused and plan.new_text is not None
    doc = yaml.safe_load(plan.new_text)
    assert doc["engines"]["duckdb"]["attach"] == [{"path": "x.sqlite", "alias": "m", "type": "sqlite"}]
    assert doc["engines"]["duckdb"]["relation_map"] == {"M.MAIN": "m.main"}


def test_an_alias_clash_with_a_different_file_is_refused(tmp_path):
    cfg = tmp_path / "agent-bi.yaml"
    cfg.write_text(
        "engines:\n  duckdb:\n    attach:\n      - {path: other.sqlite, alias: m, type: sqlite}\n"
        "    relation_map:\n      \"M.MAIN\": \"m.main\"\n"
    )
    from agent_bi.errors import AgentBiError

    with pytest.raises(AgentBiError) as exc:
        plan_config(cfg, "x.sqlite", "m", tmp_path)
    assert exc.value.code == "CONFIG_INVALID"


def test_fresh_clone_reproducibility_mechanism(tmp_path):
    """The Sprint 3 task 3 procedure in miniature: clone a committed state, drop rill/, run
    init, and see no change under semantic/ except scanned_at and only build-id under rill/."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    (tmp_path / "src").mkdir()
    src = make_project(tmp_path / "src")
    # This test drops all of rill/ and regenerates it, which an operator-owned Rill model
    # forbids; it is only about the idle table model, so the view registration is removed.
    strip_rill_models(src)
    for operator in (src / "rill" / "models").glob("*.sql"):
        operator.unlink()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], cwd=src, check=True)
    from agent_bi.compile_rill import compile_rill
    from agent_bi.config import load_config

    compile_rill(MODEL, load_config(src / "agent-bi.yaml"), src, build_id="20200101T000000Z")
    subprocess.run([*git, "add", "-A"], cwd=src, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=src, check=True)
    clone = tmp_path / "clone"
    subprocess.run([*git, "clone", "-q", str(src), str(clone)], cwd=tmp_path, check=True)
    shutil.rmtree(clone / "rill")
    old = main(["--config", str(clone / "agent-bi.yaml"), "init", *[
        a if a != ARGS[1] else str(clone / ARGS[1]) for a in ARGS]])
    assert old == 0
    status = subprocess.run([*git, "status", "--porcelain"], cwd=clone, text=True,
                            capture_output=True, check=True).stdout.splitlines()
    # semantic/: only the sidecar (scanned_at); rill/: only the manifest, only its build_id
    assert sorted(ln[3:] for ln in status) == [
        f"rill/provenance/{MODEL}.json", f"semantic/{MODEL}.provenance.yaml"], status
    diff = subprocess.run([*git, "diff", "-U0"], cwd=clone, text=True, capture_output=True,
                          check=True).stdout
    changed_lines = [ln for ln in diff.splitlines()
                     if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    assert len(changed_lines) == 4, changed_lines  # two lines each: scanned_at, build_id
    assert sorted(ln[1:].strip().split(":")[0] for ln in changed_lines) == [
        '"build_id"', '"build_id"', "scanned_at", "scanned_at"]
