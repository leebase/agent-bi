"""Sprint 3 task 1: the Rill target (architecture §5.4, §5.4.1, ADR-009)."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
import yaml

from agent_bi.compile_rill import compile_rill
from agent_bi.config import load_config
from agent_bi.main import main
from agent_bi.semantic.model import load_model_dict
from agent_bi.targets import rill as rill_target
from agent_bi.targets.rill import HEADER_TEXT, RILL_SCHEMA_PIN, strip_header
from tests.conftest import ROOT
from tests.s3_helpers import GOLDEN, MODEL, manifests
from tests.s6_helpers import make_project

BUILD_ID_RE = re.compile(r"\d{8}T\d{6}Z")


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def compile_into(project, model=MODEL, build_id=None):
    return compile_rill(model, load_config(project / "agent-bi.yaml"), project, build_id)


def test_schema_pin_equals_the_pinned_version_file():
    text = (ROOT / "docs/pinned/rill-version.txt").read_text().strip()
    m = re.match(r"^rill version (v\S+) \(build commit: [0-9a-f]{40} date: \S+\)$", text)
    assert m, text
    assert RILL_SCHEMA_PIN == m.group(1)


def test_matches_golden(project):
    """Byte for byte after masking the generated header line (the first line) and build-id."""
    compile_into(project)
    golden_files = sorted(p for p in GOLDEN.rglob("*") if p.is_file())
    assert len(golden_files) == 5
    for g in golden_files:
        generated = (project / "rill" / g.relative_to(GOLDEN)).read_text()
        assert strip_header(generated) == g.read_text(), g.name


def test_every_generated_file_starts_with_the_adr_009_header(project):
    compile_into(project)
    header = HEADER_TEXT.format(model=MODEL)
    assert header == (
        f"agent-bi: generated from semantic/{MODEL}.yaml — do not edit; "
        "edit the semantic model and re-run compile-rill"
    )
    for rel in rill_target.generated_paths(MODEL):
        first = (project / "rill" / rel).read_text().split("\n", 1)[0]
        if rel == "rill.yaml":  # project-level: model-neutral header (D395)
            assert first == (
                "# agent-bi: generated project file \u2014 do not edit; re-run compile-rill"
            )
            continue
        assert first == (f"-- {header}" if rel.endswith(".sql") else f"# {header}"), rel


def test_rill_yaml_is_identical_whichever_model_is_compiled(project):
    compile_into(project)
    first = (project / "rill/rill.yaml").read_bytes()
    compile_into(project, "product_catalog")
    assert (project / "rill/rill.yaml").read_bytes() == first


def test_deterministic(project):
    """Two compiles (the second after a scanned_at-only change of the sidecar) differ only in
    the manifest's build_id: no other masking."""
    compile_into(project, build_id="20261014T000001Z")
    first = {p: (project / p).read_bytes() for p in _generated(project)}
    (m,) = manifests(project)
    first_manifest = json.loads(m.read_text())
    compile_into(project, build_id="20261014T000002Z")
    assert {p: (project / p).read_bytes() for p in _generated(project)} == first
    (m,) = manifests(project)  # one manifest per model, overwritten (D395)
    second_manifest = json.loads(m.read_text())
    assert first_manifest.pop("build_id") != second_manifest.pop("build_id")
    assert first_manifest == second_manifest
    for f in second_manifest["files"]:
        assert f["sha256"] == "sha256:" + hashlib.sha256((project / f["path"]).read_bytes()).hexdigest()


def _generated(project):
    return [
        f"rill/{p}" for p in rill_target.generated_paths(MODEL)
    ]


def test_measures_are_the_accepted_metric_exprs_verbatim(project):
    """Rill invariant (D389): the authoritative metric expression is emitted unchanged."""
    compile_into(project)
    metrics = load_model_dict(str(project / "semantic" / f"{MODEL}.yaml"))["tables"][0]["metrics"]
    view = yaml.safe_load((project / f"rill/metrics/{MODEL}.yaml").read_text())
    assert [(m["name"], m["expr"]) for m in metrics] == [
        (m["name"], m["expression"]) for m in view["measures"]
    ]


def test_product_catalog_canvas_has_no_line_component(project):
    compile_into(project, "product_catalog")
    canvas = yaml.safe_load(
        (project / "rill/dashboards/product_catalog_default_canvas.yaml").read_text()
    )
    kinds = [k for row in canvas["rows"] for item in row["items"] for k in item if k != "width"]
    assert "line_chart" not in kinds
    assert canvas["defaults"] == {"time_range": "inf"}
    explore = yaml.safe_load(
        (project / "rill/dashboards/product_catalog_explore.yaml").read_text()
    )
    assert explore["defaults"] == {"time_range": "inf"}
    view = yaml.safe_load((project / "rill/metrics/product_catalog.yaml").read_text())
    assert "timeseries" not in view


def test_product_catalog_table_lists_non_time_dimensions_then_metrics(project):
    compile_into(project, "product_catalog")
    model = load_model_dict(str(project / "semantic/product_catalog.yaml"))["tables"][0]
    canvas = yaml.safe_load(
        (project / "rill/dashboards/product_catalog_default_canvas.yaml").read_text()
    )
    tables = [i["table"] for row in canvas["rows"] for i in row["items"] if "table" in i]
    assert len(tables) == 1
    assert tables[0]["columns"] == (
        [d["name"] for d in model["dimensions"]] + [m["name"] for m in model["metrics"]]
    )
    # KPIs: the first 4 metrics, one measure each; breakdowns: at most 2, non-unique only
    kpis = [i["kpi_grid"]["measures"] for row in canvas["rows"] for i in row["items"] if "kpi_grid" in i]
    assert kpis == [[m["name"]] for m in model["metrics"][:4]]
    bars = [i["bar_chart"]["x"]["field"] for row in canvas["rows"] for i in row["items"] if "bar_chart" in i]
    assert len(bars) <= 2 and "sku_id" not in bars  # sku_id is the unique key


def test_idle_canvas_row_order_and_defaults(project):
    compile_into(project)
    canvas = yaml.safe_load(
        (project / f"rill/dashboards/{MODEL}_default_canvas.yaml").read_text()
    )
    assert canvas["defaults"] == {"time_range": "inf"} and "filters" not in canvas
    heights = [r["height"] for r in canvas["rows"]]
    assert heights == ["128px", "320px", "320px", "400px"]
    assert sum(1 for r in canvas["rows"] for _ in r["items"]) == 7


def test_the_catalog_column_order_drives_the_model_sql(project):
    compile_into(project)
    sql = (project / f"rill/models/{MODEL}.sql").read_text()
    cols = re.findall(r'^\s+(?:CAST\()?"(\w+)"', sql, re.MULTILINE)
    assert cols == [
        "usage_day", "warehouse_name", "credits_used_compute", "credits_used_cloud_services",
        "credits_used",
    ]
    assert "sqlite_scan('../tests/fixtures/idle-warehouse-waste.sqlite'" in sql


def test_time_dimension_named_differently_from_its_column_is_aliased():
    table = {
        "time_dimensions": [{"name": "event_time", "expr": '"eventTime"'}],
        "dimensions": [{"name": "day", "expr": 'CAST("dayText" AS DATE)'}],
    }
    sql = rill_target.render_model_sql(
        "m", table, ["eventTime", "dayText", "other"], "../x.sqlite", "t"
    )
    assert '  "eventTime" AS "event_time",\n' in sql
    assert '  CAST("dayText" AS DATE) AS "dayText",\n' in sql
    assert sql.rstrip().endswith("FROM sqlite_scan('../x.sqlite', 't')")


def test_operator_files_untouched(project, capsys):
    """Fork a canvas, modify it, re-run compile-rill: the operator's bytes are unchanged."""
    compile_into(project)
    cfg = ["--config", str(project / "agent-bi.yaml")]
    assert main([*cfg, "canvas", "fork", "--model", MODEL, "--name", "mine"]) == 0
    mine = project / "rill/dashboards/mine.yaml"
    mine.write_text(mine.read_text() + "# operator edit\n")
    component = project / "rill/components/saved.yaml"
    component.parent.mkdir()
    component.write_text("type: component\n")
    before = {p: p.read_bytes() for p in (mine, component)}
    assert main([*cfg, "compile-rill", "--model", MODEL]) == 0
    assert {p: p.read_bytes() for p in before} == before


def test_canvas_fork_copies_without_the_header(project):
    compile_into(project)
    cfg = ["--config", str(project / "agent-bi.yaml")]
    assert main([*cfg, "canvas", "fork", "--model", MODEL, "--name", "mine"]) == 0
    forked = (project / "rill/dashboards/mine.yaml").read_text()
    assert forked == (GOLDEN / f"dashboards/{MODEL}_default_canvas.yaml").read_text()
    assert yaml.safe_load(forked)["defaults"] == {"time_range": "inf"}


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("Bad-Name", "E_INVALID_CANVAS_NAME"),
        (f"{MODEL}_default_canvas", "E_GENERATED_FILE"),
        (f"{MODEL}_explore", "E_GENERATED_FILE"),
        ("product_catalog_default_canvas", "E_GENERATED_FILE"),
    ],
)
def test_canvas_fork_refusals(project, capsys, name, code):
    compile_into(project)
    capsys.readouterr()
    rc = main(["--config", str(project / "agent-bi.yaml"), "canvas", "fork", "--model", MODEL,
               "--name", name])
    assert rc == 2
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith(f"{code}: ")


def test_canvas_fork_never_overwrites_and_needs_a_compiled_canvas(project, capsys):
    cfg = ["--config", str(project / "agent-bi.yaml")]
    capsys.readouterr()
    assert main([*cfg, "canvas", "fork", "--model", MODEL, "--name", "mine"]) == 2
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith("E_CANVAS_NOT_FOUND: ")
    compile_into(project)
    assert main([*cfg, "canvas", "fork", "--model", MODEL, "--name", "mine"]) == 0
    (project / "rill/dashboards/mine.yaml").write_text("operator\n")
    capsys.readouterr()
    assert main([*cfg, "canvas", "fork", "--model", MODEL, "--name", "mine"]) == 2
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith("E_CANVAS_EXISTS: ")
    assert (project / "rill/dashboards/mine.yaml").read_text() == "operator\n"
