from __future__ import annotations

import pytest

from agent_bi.catalog import DuckDBCatalog
from agent_bi.errors import AgentBiError


def test_warehouse_metering_history(engine):
    rel = DuckDBCatalog(engine).describe("FIXTURE", "MAIN", "warehouse_metering_history")
    assert [c.name for c in rel.columns] == [
        "usage_day",
        "warehouse_name",
        "credits_used_compute",
        "credits_used_cloud_services",
        "credits_used",
    ]
    assert [c.ordinal for c in rel.columns] == [1, 2, 3, 4, 5]
    assert len(rel.columns) == 5
    assert rel.row_count == 21
    assert rel.kind == "table"
    by_name = {c.name: c for c in rel.columns}
    assert by_name["usage_day"].iso_date_text is True
    assert by_name["warehouse_name"].iso_date_text is False
    assert by_name["credits_used"].iso_date_text is False
    assert [c.catalog_type for c in rel.columns] == ["VARCHAR", "VARCHAR", "DOUBLE", "DOUBLE", "DOUBLE"]
    assert not any(c.is_pk for c in rel.columns)
    assert by_name["warehouse_name"].distinct_count < rel.row_count


def test_product_catalog_has_24_columns_and_pk_from_sqlite(engine):
    rel = DuckDBCatalog(engine).describe("FIXTURE2", "MAIN", "product_catalog")
    assert len(rel.columns) == 24
    assert rel.row_count == 120
    assert [c.name for c in rel.columns if c.is_pk] == ["skuId"]  # stdlib sqlite3 fallback
    assert not any(c.iso_date_text for c in rel.columns)


def test_columns_digest_is_stable_and_type_sensitive(engine):
    cat = DuckDBCatalog(engine)
    a = cat.describe("FIXTURE", "MAIN", "warehouse_metering_history")
    b = cat.describe("FIXTURE", "MAIN", "warehouse_metering_history")
    assert a.columns_digest == b.columns_digest
    assert a.columns_digest.startswith("sha256:") and len(a.columns_digest) == 71


def test_lookup_is_case_tolerant_but_reports_catalog_spelling(engine):
    rel = DuckDBCatalog(engine).describe("FIXTURE", "MAIN", "WAREHOUSE_METERING_HISTORY")
    assert rel.name == "warehouse_metering_history"


def test_unknown_relation(engine):
    with pytest.raises(AgentBiError) as exc:
        DuckDBCatalog(engine).describe("FIXTURE", "MAIN", "nope")
    assert exc.value.full_code == "E_RELATION_NOT_FOUND"
    with pytest.raises(AgentBiError) as exc:
        DuckDBCatalog(engine).describe("NOPE", "MAIN", "x")
    assert exc.value.full_code == "E_RELATION_NOT_MAPPED"
