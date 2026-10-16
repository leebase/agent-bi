"""Sprint 5 task 4: ``diff`` (architecture §5.12) — a read-only preview of ``scan --refresh``."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from tests.s3_helpers import MODEL, tree_hash
from tests.s4_helpers import cli, make_s4_project

DB = "tests/fixtures/idle-warehouse-waste.sqlite"


def _fixture(project):
    return project / DB


def _drop_credits_used(project):
    con = sqlite3.connect(_fixture(project))
    try:
        con.execute("ALTER TABLE warehouse_metering_history DROP COLUMN credits_used")
        con.commit()
    finally:
        con.close()


def diff(project, capsys):
    return cli(project, capsys, "diff", "--model", MODEL, "--engine", "duckdb")


def test_an_unchanged_fixture_reports_no_drift_and_exits_0(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    snap = tree_hash(p)
    rc, out, err = diff(p, capsys)
    assert rc == 0, err
    assert "no drift" in out
    assert "| row_count | 21 | 21 |" in out
    assert "structural drift" not in out
    assert tree_hash(p) == snap  # absolutely no writes


def test_a_dropped_column_exits_1_and_names_the_breaks(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    _drop_credits_used(p)
    snap = tree_hash(p)
    rc, out, err = diff(p, capsys)
    assert rc == 1, err
    assert "structural drift detected" in out
    assert "| removed | credits_used |" in out
    assert "tables[0].facts[credits_used] | removed" in out
    assert "tables[0].metrics[total_credits_used] | removed" in out
    assert "verified_queries[credits_by_warehouse] (missing total_credits_used)" in out
    assert "refresh would break" in out
    assert tree_hash(p) == snap


def test_a_verified_query_through_a_preserved_broken_metric_is_listed(tmp_path, capsys):
    """A1: an accepted metric keeps its name but loses its column; its queries are broken."""
    p = make_s4_project(tmp_path)
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--accept",
                     "tables[0].metrics[total_credits_used_compute]",
                     "--by", "synthetic-test-operator")
    assert rc == 0, err
    con = sqlite3.connect(_fixture(p))
    try:
        con.execute("ALTER TABLE warehouse_metering_history DROP COLUMN credits_used_compute")
        con.commit()
    finally:
        con.close()
    snap = tree_hash(p)
    rc, out, err = diff(p, capsys)
    assert rc == 1, err
    assert (
        "tables[0].metrics[total_credits_used_compute] (missing column credits_used_compute)"
        in out
    )
    assert (
        "verified_queries[compute_and_cloud_by_warehouse] "
        "(broken dependency total_credits_used_compute)" in out
    )
    assert "verified_queries[avg_compute_by_warehouse] (missing avg_credits_used_compute)" in out
    # queries that do not depend on the dropped column stay out of the preview
    for unaffected in (
        "total_credits_all", "credits_by_warehouse", "credits_by_day", "row_count_all",
        "averages_by_day", "distinct_warehouses", "warehouses_by_day",
    ):
        assert f"verified_queries[{unaffected}]" not in out
    assert tree_hash(p) == snap  # read-only


def test_a_dropped_dimension_lists_every_dependent_query(tmp_path, capsys):
    """A1b: an emptied merged kind must not revive the removed names of the old table."""
    p = make_s4_project(tmp_path)
    con = sqlite3.connect(_fixture(p))
    try:
        con.execute("ALTER TABLE warehouse_metering_history DROP COLUMN warehouse_name")
        con.commit()
    finally:
        con.close()
    snap = tree_hash(p)
    rc, out, err = diff(p, capsys)
    assert rc == 1, err
    assert "| removed | warehouse_name | dimension:TEXT | -- |" in out
    assert "tables[0].dimensions[warehouse_name] | removed" in out
    assert "tables[0].metrics[distinct_warehouse_name] | removed" in out
    assert "verified_queries[credits_by_warehouse] (missing warehouse_name)" in out
    assert "verified_queries[avg_compute_by_warehouse] (missing warehouse_name)" in out
    assert "verified_queries[compute_and_cloud_by_warehouse] (missing warehouse_name)" in out
    assert "verified_queries[distinct_warehouses] (missing distinct_warehouse_name)" in out
    assert "verified_queries[warehouses_by_day] (missing distinct_warehouse_name)" in out
    for unaffected in ("total_credits_all", "credits_by_day", "row_count_all", "averages_by_day"):
        assert f"verified_queries[{unaffected}]" not in out
    assert tree_hash(p) == snap  # read-only


def test_an_accepted_object_a_refresh_would_break_is_listed(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--accept",
                     "tables[0].metrics[total_credits_used]", "--by", "synthetic-test-operator")
    assert rc == 0, err
    _drop_credits_used(p)
    snap = tree_hash(p)
    rc, out, err = diff(p, capsys)
    assert rc == 1, err
    assert "tables[0].metrics[total_credits_used] (missing column credits_used) | refresh would break" in out
    assert "verified_queries[averages_by_day] (missing avg_credits_used)" in out
    assert "verified_queries[total_credits_all] (broken dependency total_credits_used)" in out
    assert "verified_queries[credits_by_warehouse] (broken dependency total_credits_used)" in out
    assert "verified_queries[credits_by_day] (broken dependency total_credits_used)" in out
    # queries that do not use the accepted metric stay out of the preview
    assert "verified_queries[compute_and_cloud_by_warehouse]" not in out
    assert "verified_queries[distinct_warehouses]" not in out
    assert tree_hash(p) == snap


def test_an_added_column_exits_1(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    con = sqlite3.connect(_fixture(p))
    try:
        con.execute("ALTER TABLE warehouse_metering_history ADD COLUMN region TEXT")
        con.commit()
    finally:
        con.close()
    snap = tree_hash(p)
    rc, out, err = diff(p, capsys)
    assert rc == 1, err
    assert "| added | region |" in out
    assert "tables[0].dimensions[region] | added" in out
    assert tree_hash(p) == snap


def test_a_row_count_change_only_is_reported_and_exits_0(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    con = sqlite3.connect(_fixture(p))
    try:
        con.execute(
            "DELETE FROM warehouse_metering_history WHERE rowid = "
            "(SELECT MIN(rowid) FROM warehouse_metering_history)"
        )
        con.commit()
    finally:
        con.close()
    snap = tree_hash(p)
    rc, out, err = diff(p, capsys)
    assert rc == 0, err
    assert "no structural drift; row count changed 21 -> 20" in out
    assert "| row_count | 21 | 20 |" in out
    assert "structural drift detected" not in out
    assert tree_hash(p) == snap


def test_a_refresh_time_name_collision_is_reported_as_drift(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    patch = {
        "schema_version": "agent-bi-enrichment-patch/1.0",
        "patch_id": "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f",
        "model": MODEL,
        "proposed_by": "synthetic-test-operator",
        "proposed_at": "2026-10-15T12:00:00Z",
        "operations": [{
            "op": "add_metric", "path": "tables[0]",
            "value": {"name": "region", "expr": 'SUM("credits_used")', "description": "region"},
        }],
    }
    patch_path = Path(tempfile.mkdtemp(prefix="agent-bi-s5-diff-")) / "patch.json"
    patch_path.write_text(json.dumps(patch))
    rc, _, err = cli(p, capsys, "enrich", "--model", MODEL, "--apply", str(patch_path))
    assert rc == 0, err
    con = sqlite3.connect(_fixture(p))
    try:
        con.execute("ALTER TABLE warehouse_metering_history ADD COLUMN region TEXT")
        con.commit()
    finally:
        con.close()
    snap = tree_hash(p)
    rc, out, err = diff(p, capsys)
    assert rc == 1, err
    assert "E_NAME_COLLISION" in out and "region" in out
    assert tree_hash(p) == snap


def test_the_committed_fixture_and_model_are_untouched(tmp_path, capsys):
    p = make_s4_project(tmp_path)
    fixture = _fixture(p).read_bytes()
    semantic = (p / "semantic" / f"{MODEL}.yaml").read_bytes()
    sidecar = (p / "semantic" / f"{MODEL}.provenance.yaml").read_bytes()
    diff(p, capsys)
    _drop_credits_used(p)
    diff(p, capsys)
    assert (p / "semantic" / f"{MODEL}.yaml").read_bytes() == semantic
    assert (p / "semantic" / f"{MODEL}.provenance.yaml").read_bytes() == sidecar
    assert isinstance(fixture, bytes)
