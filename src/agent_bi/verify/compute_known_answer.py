#!/usr/bin/env python3
"""Compute a known answer by human-written SQL on a SQLite fixture (architecture.md section 5.6).

Independent route installed with Agent BI (D436): stdlib sqlite3 only, fixture opened
read-only, exactly one SELECT/WITH statement. ``save --verify`` calls
:func:`compute_known_answer` **in-process** through this module; the module itself never
imports ``agent_bi``, so the answer it computes is independent of the compiler. ``main`` is
the CLI wrapper kept for source-checkout scripts; no runtime command uses it. The output
JSON leaves semantic_query, query_hash and semantic_digest as null; later sprints fill
them.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from typing import Any

_COMMENT_RE = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)


class NotReadonlyError(Exception):
    """First keyword is not SELECT or WITH (``main`` prints ``E_NOT_READONLY``, exit 2)."""


class SqlError(Exception):
    """The fixture could not be opened or the SQL could not run (``main`` prints ``E_SQL``, exit 3)."""


def first_keyword(sql: str) -> str:
    rest = sql[_COMMENT_RE.match(sql).end():]
    m = re.match(r"[A-Za-z_]+", rest)
    return m.group(0).upper() if m else ""


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def compute_known_answer(
    sqlite_path: str | os.PathLike[str], sql: str, name: str, by: str
) -> dict[str, Any]:
    """The known-answer document (§5.6) computed read-only with stdlib ``sqlite3``.

    Refusals are typed: :class:`NotReadonlyError` for a first keyword other than SELECT/WITH
    and :class:`SqlError` for any sqlite3 error (including the multiple-statement and
    read-only write refusals).
    """
    kw = first_keyword(sql)
    if kw not in ("SELECT", "WITH"):
        raise NotReadonlyError(
            f"E_NOT_READONLY: first keyword must be SELECT or WITH, got {kw or '(none)'!r}"
        )

    digest = sha256_file(sqlite_path)
    uri = "file:" + os.path.abspath(sqlite_path) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            cur = con.execute(sql)
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        finally:
            con.close()
    except sqlite3.Error as e:  # includes the multiple-statement and read-only write refusals
        raise SqlError(f"E_SQL: {type(e).__name__}: {e}") from e

    return {
        "name": name,
        "query_hash": None,
        "semantic_digest": None,
        "semantic_query": None,
        "expected": {"columns": columns, "rows": rows},
        "expected_computed_by": "direct-sql",
        "expected_sql": sql,
        "expected_sql_by": by,
        "expected_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fixture_sha256": digest,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", required=True, help="path to the SQLite fixture (opened read-only)")
    ap.add_argument("--sql", required=True, help="exactly one SELECT/WITH statement")
    ap.add_argument("--name", required=True, help="known-answer name")
    ap.add_argument("--by", required=True, help="person who wrote the SQL (expected_sql_by)")
    ap.add_argument("--out", required=True, help="output JSON file, or an existing directory (writes <name>.json)")
    args = ap.parse_args(argv)

    try:
        doc = compute_known_answer(args.sqlite, args.sql, args.name, args.by)
    except NotReadonlyError as e:
        print(e, file=sys.stderr)
        return 2
    except SqlError as e:
        print(e, file=sys.stderr)
        return 3

    out = args.out
    if os.path.isdir(out):
        out = os.path.join(out, args.name + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    columns = doc["expected"]["columns"]
    rows = doc["expected"]["rows"]
    print(f"wrote {out} columns={columns} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
