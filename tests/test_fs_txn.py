"""Sprint 3: write transactions (architecture §5.14)."""

from __future__ import annotations

import pytest

from agent_bi import compile_rill as compile_mod
from agent_bi import fs_txn
from agent_bi.config import load_config
from agent_bi.errors import AgentBiError
from agent_bi.targets import rill as rill_target
from tests.s3_helpers import MODEL, make_project, tree_hash


def test_publish_writes_every_output_and_removes_the_temp_dir(tmp_path):
    done = fs_txn.publish(tmp_path, {"rill/dashboards/a.yaml": "a", "semantic/m.yaml": b"m"})
    assert done == ["semantic/m.yaml", "rill/dashboards/a.yaml"]  # fixed order, not input order
    assert (tmp_path / "semantic/m.yaml").read_bytes() == b"m"
    assert not (tmp_path / fs_txn.TMP_DIRNAME).exists()


def test_deletes_run_after_every_replace_and_only_on_success(tmp_path, monkeypatch):
    (tmp_path / "rill/provenance").mkdir(parents=True)
    old = tmp_path / "rill/provenance/old.json"
    old.write_text("x")
    fs_txn.publish(tmp_path, {"rill/provenance/new.json": "n"}, deletes=["rill/provenance/old.json"])
    assert not old.exists() and (tmp_path / "rill/provenance/new.json").exists()
    old.write_text("x")
    monkeypatch.setattr(fs_txn, "_stage", lambda *a: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        fs_txn.publish(tmp_path, {"rill/provenance/n2.json": "n"}, deletes=["rill/provenance/old.json"])
    assert old.exists()  # a failed transaction deletes nothing


def test_publish_order_is_the_documented_one():
    paths = [
        "rill/provenance/b.json", "rill/dashboards/d.yaml", "rill/components/c.yaml",
        "rill/metrics/m.yaml", "rill/models/m.sql", "rill/rill.yaml",
        "tests/fixtures/known-answers/m/q.json", "queries/q.json",
        "semantic/m.provenance.yaml", "semantic/m.yaml", "agent-bi.yaml", "zzz.txt",
    ]
    ranked = sorted(paths, key=fs_txn.publish_rank)
    assert ranked == [
        "agent-bi.yaml", "semantic/m.yaml", "semantic/m.provenance.yaml", "queries/q.json",
        "tests/fixtures/known-answers/m/q.json", "rill/rill.yaml", "rill/models/m.sql",
        "rill/metrics/m.yaml", "rill/components/c.yaml", "rill/dashboards/d.yaml",
        "rill/provenance/b.json", "zzz.txt",
    ]


def test_a_failure_while_staging_changes_nothing(tmp_path, monkeypatch):
    (tmp_path / "keep.txt").write_text("x")
    before = tree_hash(tmp_path)
    real = fs_txn._stage
    calls = []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 2:  # fail after the first output was staged
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(fs_txn, "_stage", flaky)
    with pytest.raises(OSError):
        fs_txn.publish(tmp_path, {"semantic/a.yaml": "a", "rill/rill.yaml": "b"})
    assert tree_hash(tmp_path) == before
    assert not (tmp_path / fs_txn.TMP_DIRNAME).exists()


def test_a_failure_while_publishing_is_e_partial_publish(tmp_path, monkeypatch):
    real = fs_txn._replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("permission denied")
        real(src, dst)

    monkeypatch.setattr(fs_txn, "_replace", flaky)
    with pytest.raises(AgentBiError) as exc:
        fs_txn.publish(tmp_path, {"semantic/a.yaml": "a", "rill/rill.yaml": "b"}, txn_id="t1")
    err = exc.value
    assert err.code == "PARTIAL_PUBLISH" and err.exit_code == 3
    assert "semantic/a.yaml" in err.message and "permission denied" in err.message
    assert (tmp_path / "semantic/a.yaml").exists() and not (tmp_path / "rill/rill.yaml").exists()
    assert (tmp_path / fs_txn.TMP_DIRNAME / "t1" / "rill/rill.yaml").exists()  # kept for recovery


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../x", ""])
def test_paths_must_stay_inside_the_repo(tmp_path, bad):
    with pytest.raises(ValueError):
        fs_txn.publish(tmp_path, {bad: "x"})


def test_compile_rill_is_a_write_transaction(tmp_path, monkeypatch):
    """Inject a failure after the first file is computed: the tree hash is unchanged."""
    project = make_project(tmp_path)
    before = tree_hash(project)

    def boom(*a, **k):
        raise RuntimeError("injected after the first file was computed")

    monkeypatch.setattr(rill_target, "render_metrics_view", boom)
    with pytest.raises(RuntimeError, match="injected"):
        compile_mod.compile_rill(MODEL, load_config(project / "agent-bi.yaml"), project)
    assert tree_hash(project) == before
    assert not (project / "rill").exists()


def test_compile_rill_failure_while_staging_changes_nothing(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    before = tree_hash(project)
    real = fs_txn._stage
    calls = []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(fs_txn, "_stage", flaky)
    with pytest.raises(OSError):
        compile_mod.compile_rill(MODEL, load_config(project / "agent-bi.yaml"), project)
    assert tree_hash(project) == before
