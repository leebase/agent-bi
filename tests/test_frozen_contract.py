"""Sprint 4P: replay every case of the frozen resolver contract (architecture §5.15, D414).

``tests/fixtures/resolve/frozen-cases.yaml`` is read at test time and never edited. Inline models
are materialized by the convention in its header: an inline model keyed K becomes model ``k`` with
one table ``t_k`` and the listed objects, with no descriptions. Every field a case lists is
compared; a mismatch is a finding, never a reason to change the case file.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from agent_bi.main import main
from tests.conftest import ROOT

CASES = ROOT / "tests" / "fixtures" / "resolve" / "frozen-cases.yaml"
FIXDIR = ROOT / "tests" / "fixtures" / "semantic"
NAMED = {  # model key used by the cases -> (file to copy, model argument)
    "iww": (ROOT / "semantic" / "idle_warehouse_waste.yaml", "idle_warehouse_waste"),
    "two": (FIXDIR / "two_dates.yaml", "two_dates"),
    "tie": (FIXDIR / "tie.yaml", "tie"),
    "rv1": (FIXDIR / "rv1.yaml", "rv1"),
    "rv4": (FIXDIR / "rv4.yaml", "rv4"),
    "rv5": (FIXDIR / "rv5.yaml", "rv5"),
    "rv6": (FIXDIR / "rv6.yaml", "rv6"),
}
EXIT_STDERR = {5: "E_RESOLVE_AMBIGUOUS: ", 6: "E_RESOLVE_UNRESOLVED: "}


def materialize(models: dict, project: Path) -> None:
    (project / "semantic").mkdir(parents=True, exist_ok=True)
    (project / "agent-bi.yaml").write_text("")
    for src, arg in NAMED.values():
        shutil.copy(src, project / "semantic" / f"{arg}.yaml")
    for spec in models.values():
        table: dict = {"name": spec["table"]}
        for section in ("metrics", "dimensions", "time_dimensions"):
            objs = []
            for o in spec.get(section) or []:
                o = {"name": o} if isinstance(o, str) else o
                objs.append({"name": o["name"], "synonyms": list(o.get("synonyms") or []),
                             "expr": f'"{o["name"]}"'})
            if objs:
                table[section] = objs
        (project / "semantic" / f"{spec['name']}.yaml").write_text(
            yaml.safe_dump({"name": spec["name"], "tables": [table]}, sort_keys=False))


def model_arg(key: str, models: dict) -> str:
    if key in models:
        return models[key]["name"]
    return NAMED[key][1] if key in NAMED else key  # e.g. AMD2's per-step ``nosuch``


def run_call(project: Path, capsys, model: str, question: str, choose: list[str]):
    capsys.readouterr()
    args = ["--config", str(project / "agent-bi.yaml"), "resolve", "--model", model,
            "--question", question]
    for c in choose or []:
        args += ["--choose", c]
    rc = main(args)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def compare(project: Path, rc: int, out: str, err: str, exp: dict) -> list[str]:
    """Every field the case lists, compared; returns the mismatches."""
    bad: list[str] = []
    res = project / "out" / "resolution.json"
    if rc != exp["exit"]:
        bad.append(f"exit {rc} != {exp['exit']}")
    if "stdout" in exp and out != exp["stdout"]:
        bad.append(f"stdout {out[:60]!r} != {exp['stdout']!r}")
    last = (err.strip().splitlines() or [""])[-1]
    if "stderr_last_line_prefix" in exp and not last.startswith(exp["stderr_last_line_prefix"]):
        bad.append(f"stderr last line {last!r} lacks prefix {exp['stderr_last_line_prefix']!r}")
    if "stderr" in exp and err.strip() != exp["stderr"]:
        bad.append(f"stderr {err.strip()!r} != {exp['stderr']!r}")
    if rc in EXIT_STDERR and not last.startswith(EXIT_STDERR[rc]):
        bad.append(f"stderr last line {last!r}")
    if rc == 0 and err:
        bad.append(f"unexpected stderr {err!r}")
    fx = exp.get("out_resolution_json", "")
    if fx.startswith("removed") and res.exists():
        bad.append("out/resolution.json still exists")
    if fx.startswith("written"):
        if not res.exists():
            bad.append("out/resolution.json missing")
        elif res.read_bytes() != out.encode():
            bad.append("out/resolution.json differs from stdout")
    if rc == 2:
        return bad
    doc = json.loads(out)
    for key in ("status", "ambiguities", "unresolved", "bindings", "grain"):
        if key in exp and doc[key] != exp[key]:
            bad.append(f"{key}: {doc[key]} != {exp[key]}")
    if "semantic_query" in exp and doc["semantic_query"] != exp["semantic_query"]:
        bad.append(f"semantic_query: {doc['semantic_query']} != {exp['semantic_query']}")
    if "query_hash" in exp and doc["query_hash"] != exp["query_hash"]:
        bad.append(f"query_hash: {doc['query_hash']} != {exp['query_hash']}")
    if "status" in exp and exp["status"] != "RESOLVED" and (
        doc["semantic_query"] is not None or doc["query_hash"]
    ):
        bad.append("non-RESOLVED output carries a query")
    sq = doc["semantic_query"]
    if sq is not None:
        for k in ("metrics", "dimensions", "time"):
            if k in exp and sq.get(k) != exp[k]:
                bad.append(f"{k}: {sq.get(k)} != {exp[k]}")
        if exp.get("equality_holds") and (
            [b["object"] for b in doc["bindings"] if b["kind"] == "metric"] != sq["metrics"]
            or [b["object"] for b in doc["bindings"] if b["kind"] == "dimension"] != sq["dimensions"]
        ):
            bad.append("bindings do not equal the SemanticQuery")
    return bad


def test_frozen_contract_cases(tmp_path, capsys):
    data = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) == 179
    results, failures = [], []
    for case in cases:
        project = tmp_path / case["id"]
        materialize(data["models"], project)
        model = model_arg(case["model"], data["models"])
        (project / "out").mkdir()
        if "sequence" in case:  # Q16: exit-2 file removal across calls
            problems = []
            exps = case.get("expected_sequence") or [st["expected"] for st in case["sequence"]]
            for step, exp in zip(case["sequence"], exps, strict=True):
                (project / "out").mkdir(exist_ok=True)
                step_model = model_arg(step["model"], data["models"]) if "model" in step else model
                rc, out, err = run_call(project, capsys, step_model, step["question"], step["choose"])
                problems += compare(project, rc, out, err, {**exp, "out_resolution_json": exp[
                    "out_resolution_json"].replace("written (RESOLVED)", "written (same bytes as stdout)")})
        else:
            (project / "out" / "resolution.json").write_text("STALE")  # must be replaced or removed
            rc, out, err = run_call(project, capsys, model, case["question"], case["choose"])
            problems = compare(project, rc, out, err, case["expected"])
        line = f"{case['id']:<8} {'OK' if not problems else 'FAIL ' + '; '.join(problems)}"
        results.append(line)
        if problems:
            failures.append(line)
    print("\n".join(results))
    print(f"frozen cases passed: {len(cases) - len(failures)}/{len(cases)}")
    assert not failures, "\n".join(failures)
