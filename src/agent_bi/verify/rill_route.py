"""Executing Metrics SQL on a running Rill (architecture §5.6, ``verify --engine rill``).

``rill query --local --path <project> --format csv --resolver metrics_sql --properties sql=<text>``

Two observed behaviours of the pinned CLI (``docs/pinned/rill-cli-query.md``; Sprint 2
review B5, BACKLOG B-056) are handled here:

1. ``--properties`` is a pflag ``stringToString``: a value containing more than one ``=``
   is parsed as CSV, so the pair is passed CSV-quoted with each ``"`` doubled; and
2. a query issued before Rill finishes reconciling fails silently (rc 1, no message), so a
   readiness probe is polled with a bounded wait first. A timeout is ``E_ENGINE_UNAVAILABLE``.

Project binding (Sprint 3 review A1, D396): ``rill query --local`` ignores ``--path`` and queries
whatever runtime listens on :9009, so before any query ``check_binding`` reads
``GET /local/config`` and requires ``project_path`` and ``version`` to be this project's.

An empty result is never read as success: ``rill query`` prints nothing at all (not even a
header) for zero rows, so a silent zero-row answer is ``FAIL`` whenever rows were expected.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_bi.errors import engine_error

RILL_URL = "http://localhost:9009"
CONFIG_TIMEOUT_S = 5.0
READY_TIMEOUT_S = 60.0
READY_INTERVAL_S = 1.0
QUERY_TIMEOUT_S = 120.0
_TS_MIDNIGHT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T00:00:00Z$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class QuotedStr(str):
    """A CSV cell that was written in double quotes: ``"null"`` is then the string, not NULL."""


def parse_csv(text: str) -> list[list[str]]:
    """RFC-4180 CSV that remembers which cells were quoted (``QuotedStr``). Rill prints SQL NULL
    as the *unquoted* text ``null`` (§5.6, pre-Sprint-4, D398), which ``csv.reader`` cannot tell
    from a quoted ``"null"``."""
    rows: list[list[str]] = []
    row: list[str] = []
    cell: list[str] = []
    quoted = False
    in_quotes = False
    i, n = 0, len(text)

    def end_cell() -> None:
        nonlocal cell, quoted
        value = "".join(cell)
        row.append(QuotedStr(value) if quoted else value)
        cell, quoted = [], False

    while i < n:
        ch = text[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    cell.append('"')
                    i += 1
                else:
                    in_quotes = False
            else:
                cell.append(ch)
        elif ch == '"' and not cell and not quoted:
            in_quotes = quoted = True
        elif ch == ",":
            end_cell()
        elif ch in "\r\n":
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            end_cell()
            rows.append(row)
            row = []
        else:
            cell.append(ch)
        i += 1
    if cell or quoted or row:
        end_cell()
        rows.append(row)
    return rows


@dataclass
class RillResult:
    returncode: int
    columns: list[str]
    rows: list[list[str]]  # raw CSV cells
    stderr: str
    stdout: str = ""


def rill_binary() -> str:
    """``rill`` from PATH, else ``~/.rill/rill`` (the install location)."""
    found = shutil.which("rill")
    if found:
        return found
    home = Path(os.path.expanduser("~/.rill/rill"))
    if home.is_file():
        return str(home)
    raise engine_error(
        "ENGINE_UNAVAILABLE", "rill is not installed (not on PATH and no ~/.rill/rill)"
    )


def properties_arg(key: str, value: str) -> str:
    """The ``--properties`` argument for ``key=value``. pflag ``stringToString`` splits on the
    first ``=`` after CSV-parsing the whole argument when it holds more than one ``=``; a
    CSV field is quoted with each ``"`` doubled, so a value with commas, ``=`` or quotes
    survives. With a single ``=`` pflag strips surrounding quotes instead, so the pair is
    passed bare."""
    pair = f"{key}={value}"
    if pair.count("=") > 1:
        return '"' + pair.replace('"', '""') + '"'
    return pair


def run_metrics_sql(
    project_path: Path, sql: str, limit: int, binary: str | None = None,
    runner: Runner = subprocess.run,
) -> RillResult:
    cmd = [
        binary or rill_binary(), "query", "--local", "--path", str(project_path),
        "--format", "csv", "--limit", str(limit), "--resolver", "metrics_sql",
        "--properties", properties_arg("sql", sql),
    ]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=QUERY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return RillResult(124, [], [], f"rill query timed out after {QUERY_TIMEOUT_S:.0f}s")
    parsed = parse_csv(proc.stdout)
    columns = parsed[0] if parsed else []
    return RillResult(proc.returncode, columns, parsed[1:], proc.stderr.strip(), proc.stdout)


def fetch_local_config(url: str = RILL_URL, timeout: float | None = None) -> Any:
    """``GET <url>/local/config`` as parsed JSON (stdlib ``urllib``). Patched in tests."""
    timeout = CONFIG_TIMEOUT_S if timeout is None else timeout
    with urllib.request.urlopen(f"{url}/local/config", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_binding(
    project_path: Path, expected_version: str, fetch: Callable[[], Any] | None = None
) -> Any:
    """Require the Rill on :9009 to serve *this* project at the pinned version: ``project_path``
    equals the resolved absolute ``project_path`` argument and ``version`` equals
    ``expected_version``. Anything else is ``E_ENGINE_UNAVAILABLE`` naming what was served.
    Returns the parsed ``/local/config`` body so callers can record the bound version."""
    wanted = Path(project_path).resolve()
    if not wanted.is_dir():
        raise engine_error(
            "ENGINE_UNAVAILABLE", f"the Rill project directory {wanted} does not exist"
        )
    try:
        cfg = (fetch or fetch_local_config)()
        served_path, served_version = cfg["project_path"], cfg["version"]
        if not isinstance(served_path, str) or not isinstance(served_version, str):
            raise TypeError("project_path and version must be strings")
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
        raise engine_error(
            "ENGINE_UNAVAILABLE",
            f"cannot read {RILL_URL}/local/config to confirm which project Rill serves "
            f"(is `rill start {wanted.name} --no-open` running?): {type(exc).__name__}: {exc}",
        ) from exc
    if Path(served_path).resolve() != wanted or served_version != expected_version:
        raise engine_error(
            "ENGINE_UNAVAILABLE",
            f"the Rill on {RILL_URL} serves project_path {served_path!r} version {served_version!r}; "
            f"this project needs project_path {str(wanted)!r} version {expected_version!r}",
        )
    return cfg


def wait_until_ready(
    project_path: Path, probe_sql: str, *, timeout: float | None = None,
    interval: float | None = None, binary: str | None = None, runner: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
    before_query: Callable[[], None] | None = None,
) -> None:
    """Poll ``probe_sql`` (``SELECT <metric> FROM <model> LIMIT 1``) until it returns one row;
    ``E_ENGINE_UNAVAILABLE`` after ``timeout`` seconds. ``before_query`` (the project-binding
    check) runs immediately before every poll."""
    timeout = READY_TIMEOUT_S if timeout is None else timeout
    interval = READY_INTERVAL_S if interval is None else interval
    binary = binary or rill_binary()
    deadline = clock() + timeout
    last = ""
    while True:
        if before_query is not None:
            before_query()
        res = run_metrics_sql(project_path, probe_sql, 1, binary, runner)
        if res.returncode == 0 and len(res.rows) == 1:
            return
        last = res.stderr or f"rc={res.returncode}, {len(res.rows)} rows"
        if clock() >= deadline:
            raise engine_error(
                "ENGINE_UNAVAILABLE",
                f"rill did not answer the readiness probe within {timeout:g}s "
                f"(is `rill start {project_path.name} --no-open` running and reconciled?): {last}",
            )
        sleep(interval)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def typed_rows(expected: dict[str, Any], rows: list[list[str]]) -> list[tuple[Any, ...]]:
    """Convert Rill's CSV cells for comparison with a known answer: a column whose expected
    cells are all numbers is parsed as numbers; an empty cell is NULL; a timestamp at exactly
    ``00:00:00Z`` becomes its ``YYYY-MM-DD`` date (the one normalization §5.6 allows). An empty
    cell and an *unquoted* ``null`` are NULL; a quoted ``"null"`` is the string (D398)."""
    ncols = len(expected["columns"])
    numeric = []
    for i in range(ncols):
        cells = [r[i] for r in expected["rows"] if r[i] is not None]
        numeric.append(bool(cells) and all(_is_number(c) for c in cells))
    out: list[tuple[Any, ...]] = []
    for row in rows:
        cells: list[Any] = []
        for i, cell in enumerate(row):
            if cell == "" or (cell == "null" and not isinstance(cell, QuotedStr)):
                cells.append(None)
            elif i < ncols and numeric[i]:
                try:
                    cells.append(float(cell))
                except ValueError:
                    cells.append(cell)
            else:
                m = _TS_MIDNIGHT_RE.match(cell)
                cells.append(m.group(1) if m else cell)
        out.append(tuple(cells))
    return out
