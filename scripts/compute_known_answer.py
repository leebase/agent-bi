#!/usr/bin/env python3
"""Compute a known answer by human-written SQL on a SQLite fixture (architecture.md section 5.6).

Independent route: stdlib sqlite3 only, fixture opened read-only, exactly one SELECT/WITH statement.
The output JSON leaves semantic_query, query_hash and semantic_digest as null; later sprints fill them.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys

_COMMENT_RE = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)


def first_keyword(sql: str) -> str:
    rest = sql[_COMMENT_RE.match(sql).end():]
    m = re.match(r"[A-Za-z_]+", rest)
    return m.group(0).upper() if m else ""


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", required=True, help="path to the SQLite fixture (opened read-only)")
    ap.add_argument("--sql", required=True, help="exactly one SELECT/WITH statement")
    ap.add_argument("--name", required=True, help="known-answer name")
    ap.add_argument("--by", required=True, help="person who wrote the SQL (expected_sql_by)")
    ap.add_argument("--out", required=True, help="output JSON file, or an existing directory (writes <name>.json)")
    args = ap.parse_args(argv)

    kw = first_keyword(args.sql)
    if kw not in ("SELECT", "WITH"):
        print(f"E_NOT_READONLY: first keyword must be SELECT or WITH, got {kw or '(none)'!r}", file=sys.stderr)
        return 2

    digest = sha256_file(args.sqlite)
    uri = "file:" + os.path.abspath(args.sqlite) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        try:
            cur = con.execute(args.sql)
        except sqlite3.Error as e:  # includes the multiple-statement refusal and read-only write refusals
            print(f"E_SQL: {type(e).__name__}: {e}", file=sys.stderr)
            return 3
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
    finally:
        con.close()

    doc = {
        "name": args.name,
        "query_hash": None,
        "semantic_digest": None,
        "semantic_query": None,
        "expected": {"columns": columns, "rows": rows},
        "expected_computed_by": "direct-sql",
        "expected_sql": args.sql,
        "expected_sql_by": args.by,
        "expected_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fixture_sha256": digest,
    }
    out = args.out
    if os.path.isdir(out):
        out = os.path.join(out, args.name + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print(f"wrote {out} columns={columns} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
