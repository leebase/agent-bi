"""Sprint 3 task 2: the build manifest (architecture §4.4; one file per model, D395)."""

from __future__ import annotations

import hashlib
import json
import re

from agent_bi.compile_rill import compile_rill
from agent_bi.config import load_config
from agent_bi.provenance_manifest import SCHEMA_VERSION, new_build_id, sidecar_digest
from agent_bi.targets.rill import RILL_SCHEMA_PIN, generated_paths
from tests.s3_helpers import MODEL, manifests
from tests.s6_helpers import make_project


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_manifest_lists_every_generated_file_with_a_matching_sha256(tmp_path):
    project = make_project(tmp_path)
    compile_rill(MODEL, load_config(project / "agent-bi.yaml"), project)
    (path,) = manifests(project)
    assert path.name == f"{MODEL}.json"  # one file per model (D395)
    doc = json.loads(path.read_text())
    assert doc["schema_version"] == SCHEMA_VERSION and doc["model"] == MODEL
    assert re.fullmatch(r"\d{8}T\d{6}Z", doc["build_id"])
    assert [f["path"] for f in doc["files"]] == [f"rill/{p}" for p in generated_paths(MODEL)]
    for f in doc["files"]:  # what `sha256sum <path>` prints, with the project's sha256: prefix
        assert f["sha256"] == sha((project / f["path"]).read_bytes()), f["path"]


def test_manifest_records_the_inputs_and_the_pin(tmp_path):
    project = make_project(tmp_path)
    compile_rill(MODEL, load_config(project / "agent-bi.yaml"), project)
    doc = json.loads(manifests(project)[0].read_text())
    sem = project / "semantic"
    assert doc["model_file_sha256"] == sha((sem / f"{MODEL}.yaml").read_bytes())
    sidecar = (sem / f"{MODEL}.provenance.yaml").read_bytes()
    assert doc["sidecar_sha256"] == sidecar_digest(sidecar)
    assert doc["models_digest"] is None  # a table model
    assert doc["rill_schema_pin"] == RILL_SCHEMA_PIN


def test_sidecar_sha256_excludes_only_the_top_level_scanned_at_line():
    base = b"schema_version: x\nscanned_at: '2026-01-01T00:00:00Z'\nentries:\n  a:\n    at: t\n"
    other = base.replace(b"2026-01-01", b"2030-12-31")
    assert sidecar_digest(base) == sidecar_digest(other)
    assert sidecar_digest(base) == sha(b"schema_version: x\nentries:\n  a:\n    at: t\n")
    assert sidecar_digest(base) != sidecar_digest(base.replace(b"at: t", b"at: u"))
    assert sidecar_digest(base) != sha(base)  # not the raw file hash


def test_the_manifest_does_not_change_when_only_scanned_at_does(tmp_path):
    project = make_project(tmp_path)
    cfg = load_config(project / "agent-bi.yaml")
    compile_rill(MODEL, cfg, project, build_id="20261014T000001Z")
    first = json.loads(manifests(project)[0].read_text())
    sc = project / "semantic" / f"{MODEL}.provenance.yaml"
    sc.write_text(re.sub(r"(?m)^scanned_at: .*$", "scanned_at: '2031-01-01T00:00:00Z'", sc.read_text()))
    compile_rill(MODEL, cfg, project, build_id="20261014T000002Z")
    second = json.loads(manifests(project)[0].read_text())
    assert first.pop("build_id") != second.pop("build_id")
    assert first == second


def test_one_manifest_per_model_overwritten_by_every_compile(tmp_path):
    project = make_project(tmp_path)
    cfg = load_config(project / "agent-bi.yaml")
    compile_rill(MODEL, cfg, project, build_id="20261014T000001Z")
    compile_rill(MODEL, cfg, project, build_id="20261014T000002Z")
    assert [m.name for m in manifests(project)] == [f"{MODEL}.json"]
    assert json.loads(manifests(project)[0].read_text())["build_id"] == "20261014T000002Z"
    compile_rill("product_catalog", cfg, project)
    assert [m.name for m in manifests(project)] == [f"{MODEL}.json", "product_catalog.json"]


def test_only_the_models_own_per_build_manifests_are_removed(tmp_path):
    project = make_project(tmp_path)
    prov = project / "rill" / "provenance"
    prov.mkdir(parents=True)
    old = prov / "20261014T232957Z.json"
    old.write_text(json.dumps({"model": MODEL}))
    other_model = prov / "20261014T232958Z.json"
    other_model.write_text(json.dumps({"model": "product_catalog"}))
    operator = prov / "notes.json"
    operator.write_text("{}")
    compile_rill(MODEL, load_config(project / "agent-bi.yaml"), project)
    assert not old.exists()
    assert other_model.exists() and operator.exists()
    assert (prov / f"{MODEL}.json").exists()


def test_build_id_format():
    assert re.fullmatch(r"\d{8}T\d{6}Z", new_build_id())
