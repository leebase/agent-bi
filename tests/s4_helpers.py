"""Shared helpers for the Sprint 4 tests: a throwaway project laid out like the repo, with the
committed semantic model, sidecar, known answers and generated ``rill/`` tree."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from agent_bi.hashing import expr_sha256
from agent_bi.main import main
from tests.conftest import ROOT
from tests.s3_helpers import MODEL, make_project

KA_DIR = ROOT / "tests" / "fixtures" / "known-answers" / MODEL
CBW = KA_DIR / "credits_by_warehouse.json"  # the known-answer file (also a query file)


def make_s4_project(tmp_path: Path) -> Path:
    p = make_project(tmp_path)
    shutil.copytree(ROOT / "tests" / "fixtures" / "known-answers", p / "tests/fixtures/known-answers")
    shutil.copytree(ROOT / "rill", p / "rill", ignore=shutil.ignore_patterns("tmp"))
    shutil.copytree(ROOT / "themes", p / "themes")
    (p / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "compute_known_answer.py", p / "scripts")
    return p


def cli(project: Path, capsys, *args: str) -> tuple[int, str, str]:
    capsys.readouterr()
    rc = main(["--config", str(project / "agent-bi.yaml"), *args])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def edit_yaml(path: Path, fn) -> None:
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def accept_both(project: Path, capsys, by: str = "test") -> None:
    """Accept the two definitions behind ``credits_by_warehouse`` (scratch copies only)."""
    for path in ("tables[0].metrics[total_credits_used]", "tables[0].dimensions[warehouse_name]"):
        rc, _, err = cli(project, capsys, "enrich", "--model", MODEL, "--accept", path, "--by", by)
        assert rc == 0, err


def change_total_credits_used(project: Path) -> None:
    """Change total_credits_used's expr and keep the sidecar consistent (so validate passes)."""
    sem = project / "semantic"
    new = 'SUM("credits_used_compute")'

    def model(m):
        for metric in m["tables"][0]["metrics"]:
            if metric["name"] == "total_credits_used":
                metric["expr"] = new

    def sidecar(s):
        s["entries"]["tables[0].metrics[total_credits_used]"]["expr_sha256"] = expr_sha256(new)

    edit_yaml(sem / f"{MODEL}.yaml", model)
    edit_yaml(sem / f"{MODEL}.provenance.yaml", sidecar)


def write_query(path: Path, **overrides) -> Path:
    q = {
        "schema_version": "semantic-query/1.0", "model": MODEL, "table": "warehouse_metering_history",
        "metrics": ["total_credits_used"], "dimensions": ["warehouse_name"],
    }
    q.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(q))
    return path


def envelope_of(project: Path, out: str) -> dict:
    """The envelope ``ask`` just wrote, found from its stdout."""
    rel = next(ln.split()[1] for ln in out.splitlines() if ln.endswith(".answer.json"))
    return json.loads((project / rel).read_text())
