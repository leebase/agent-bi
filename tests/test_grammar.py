"""The closed grammars of §5.3.2 / §5.3.5 and the §5.3.3 literal helpers."""

from __future__ import annotations

import pytest

from agent_bi.query.compile import (
    COLUMN_RE,
    DIM_RE,
    FILTER_RE,
    METRIC_RE,
    expr_columns,
    sql_bool,
    sql_date,
    sql_number,
    sql_string,
)


@pytest.mark.parametrize("expr", ['"a"', 'CAST("a" AS DATE)', 'cast("a" as date)', '"a_b2"'])
def test_dim_re_accepts(expr):
    assert DIM_RE.fullmatch(expr)


@pytest.mark.parametrize(
    "expr",
    ['a', 'LOWER("a")', 'CAST("a" AS TIMESTAMP)', '"a" || "b"', 'CAST(a AS DATE)', '"a"\n',
     '"a b"', '"1a"', "'a'", '"a"; DROP TABLE t'],
)
def test_dim_re_refuses(expr):
    assert not DIM_RE.fullmatch(expr)


def test_column_re():
    assert COLUMN_RE.fullmatch('"a"') and not COLUMN_RE.fullmatch('CAST("a" AS DATE)')


@pytest.mark.parametrize(
    "expr",
    ['SUM("a")', 'avg("a")', 'MIN("a")', 'MAX("a")', "COUNT(*)", 'COUNT(DISTINCT "a")',
     'SUM("a") / NULLIF(SUM("b"), 0)', 'SUM("a")/NULLIF(SUM("b"),0)', 'COUNT(*) / NULLIF(COUNT(DISTINCT "b"), 0)'],
)
def test_metric_re_accepts(expr):
    assert METRIC_RE.fullmatch(expr)


@pytest.mark.parametrize(
    "expr",
    ["SUM(a)+1", 'SUM("a") + 1', 'SUM("a") / SUM("b")', 'SUM("a" * 2)', 'SUM(DISTINCT "a")',
     'COUNT("a")', 'MEDIAN("a")', 'SUM("a") / NULLIF(SUM("b"), 1)', 'SUM("a") / NULLIF("b", 0)',
     'SUM("a")\n', "SUM(\"a\"); SELECT 1"],
)
def test_metric_re_refuses(expr):
    assert not METRIC_RE.fullmatch(expr)


@pytest.mark.parametrize(
    "expr",
    [
        '"a" = 1',
        '"a" != \'x\'',
        '"a" <= -1.5',
        '"a" > DATE \'2026-01-31\'',
        '"a" = TRUE',
        "\"warehouse_name\" IN ('A','B') AND \"credits_used\" > 0",
        '"a" IN (1, 2, 3)',
        '"a" IS NULL',
        '"a" IS NOT NULL',
        '"a" = 1 OR "b" = 2',
        '("a" = 1 OR "b" = 2) AND "c" IS NULL',
        '(("a" = 1))',
        "\"a\" = 'it''s' AND \"b\" = ' AND '",
    ],
)
def test_filter_re_accepts(expr):
    assert FILTER_RE.fullmatch(expr)


@pytest.mark.parametrize(
    "expr",
    [
        '"usage_day" >= CURRENT_DATE()',
        '"a" = NOW()',
        '"a" = RANDOM()',
        'LOWER("a") = \'x\'',
        '"a" = "b"',
        '"a" = (SELECT 1)',
        '"a" = 1 AND',
        '"a" = 1 AND (',
        '"a" IN ()',
        '"a" = 1; DROP TABLE t',
        '"a" = \'x\' --',
        '"a" BETWEEN 1 AND 2',
        '"a" LIKE \'x%\'',
        'a = 1',
        '"a" = 1\n',
        '"a" = DATE \'2026-1-1\'',
    ],
)
def test_filter_re_refuses(expr):
    assert not FILTER_RE.fullmatch(expr)


def test_expr_columns():
    assert expr_columns('SUM("a") / NULLIF(SUM("b"), 0)') == ["a", "b"]
    assert expr_columns("COUNT(*)") == []


def test_literal_helpers():
    assert sql_string("it's") == "'it''s'"
    assert sql_string("a'; DROP TABLE t; --") == "'a''; DROP TABLE t; --'"
    assert sql_date("2026-01-31") == "DATE '2026-01-31'"
    with pytest.raises(ValueError):
        sql_date("2026-01-31'; --")
    assert sql_number(3) == "3"
    assert sql_number("4") == "4"
    assert sql_number("1.5") == "1.5"
    assert sql_number(2.5) == "2.5"
    for bad in ("1; DROP", "nan", float("inf")):
        with pytest.raises(ValueError):
            sql_number(bad)
    with pytest.raises(TypeError):
        sql_number(True)
    assert sql_bool(True) == "TRUE" and sql_bool(False) == "FALSE"
