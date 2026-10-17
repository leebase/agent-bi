"""Sprint 4R: the deterministic resolver ``agent-bi resolve`` (architecture §5.15).

``test_normative_cases`` reads the case table out of ``architecture.md`` §5.15 at test time, so
the contract and the code cannot drift. The two-date fixture ``tests/fixtures/semantic/
two_dates.yaml`` is the committed ``idle_warehouse_waste.yaml`` plus a ``billing_date`` time
dimension and is used only here.
"""

from __future__ import annotations

import ast
import json
import os
import random
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

from agent_bi.explore import resolve as resolve_module
from agent_bi.explore.resolve import FUNCTION_WORDS, resolve, words_of
from agent_bi.main import main
from agent_bi.query.contract import parse_query, query_hash
from tests.conftest import ROOT
from tests.s3_helpers import MODEL, tree_hash
from tests.s4_helpers import cli, envelope_of, make_s4_project

TWO_DATES = ROOT / "tests" / "fixtures" / "semantic" / "two_dates.yaml"
TIE = ROOT / "tests" / "fixtures" / "semantic" / "tie.yaml"
FIXDIR = ROOT / "tests" / "fixtures" / "semantic"
ARCH = ROOT / "architecture.md"
MODELS = {"iww": MODEL, "two": "two_dates", "tie": "tie", "rv1": "rv1", "rv4": "rv4", "rv5": "rv5", "rv6": "rv6"}
STATUSES = ("RESOLVED", "AMBIGUOUS", "UNRESOLVED")
EXIT = {"RESOLVED": 0, "AMBIGUOUS": 5, "UNRESOLVED": 6}
DOC_KEYS = [
    "ambiguities", "bindings", "grain", "model", "query_hash", "question", "schema_version",
    "semantic_query", "status", "unresolved", "words",
]


def resolver_project(tmp_path: Path) -> Path:
    p = make_s4_project(tmp_path)
    shutil.copy(TWO_DATES, p / "semantic" / "two_dates.yaml")
    for fixture in ("tie", "rv1", "rv4", "rv5", "rv6"):
        shutil.copy(FIXDIR / f"{fixture}.yaml", p / "semantic" / f"{fixture}.yaml")
    return p


def load_model(name: str) -> dict:
    path = {"two_dates": TWO_DATES, "tie": TIE}.get(name) or (
        FIXDIR / f"{name}.yaml" if name in ("rv1", "rv4", "rv5", "rv6") else ROOT / "semantic" / f"{name}.yaml")
    return yaml.safe_load(path.read_text())


# -- the normative table, parsed from architecture.md at test time ---------------------------


def normative_rows() -> list[dict]:
    text = ARCH.read_text(encoding="utf-8")
    start = text.index("### 5.15 ")
    end = text.index("\n## 7.", start)
    rows = []
    for line in text[start:end].splitlines():
        m = re.match(r"^\|\s*(\d+)\s*\|(.*)\|\s*$", line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        assert len(cells) == 4, line
        model, question, status, result = cells
        question = question.removesuffix(" (full-width)")  # row 56's annotation, not part of the question
        choose: list[tuple[str, str]] = []
        after_success = False
        am = re.match(r"^(.*?) — after a successful call that wrote `out/resolution.json`$", question)
        if am:  # row 52 (D417): the failing call comes after a successful one
            question, after_success = am.group(1), True
        cm = re.match(r"^(.*?) — with `(--choose .*)`$", question)
        if cm:  # ids may contain spaces and colons; values never do
            question = cm.group(1)
            for part in re.split(r"(?:^|\s)--choose\s", cm.group(2)):
                if part.strip():
                    cid, _, value = part.strip().partition("=")
                    choose.append((cid, value))
        rows.append({
            "n": int(m.group(1)), "model": MODELS.get(model, model), "question": question,
            "choose": choose, "status": status, "result": result, "after_success": after_success,
        })
    return rows


def check_result(row: dict, doc: dict) -> str | None:
    """None when ``doc`` matches the table's result cell, else a description of the mismatch."""
    result = row["result"]
    status = row["status"]
    if doc["status"] != status:
        return f"status {doc['status']} != {status}"
    if status == "RESOLVED":
        parts = result.split(" / ")
        assert len(parts) == 3, result
        sq = doc["semantic_query"]
        want_m = [] if parts[0] == "—" else parts[0].split(", ")
        want_d = [] if parts[1] == "—" else parts[1].split(", ")
        want_t = None
        if parts[2] != "—":
            dim, grain = parts[2].split()
            want_t = {"dimension": dim, "grain": grain}
        got = (sq["metrics"], sq["dimensions"], sq.get("time"))
        return None if got == (want_m, want_d, want_t) else f"{got} != {(want_m, want_d, want_t)}"
    if status == "AMBIGUOUS":
        entries = []
        depth, cur = 0, ""
        for ch in result + ",":  # split at top-level commas: "a (x, y), b" -> a (x, y) | b
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                m2 = re.fullmatch(r"\s*([^()]+?)(?: \((.*)\))?\s*", cur)
                entries.append((m2.group(1), m2.group(2) or ""))
                cur = ""
            else:
                cur += ch
        amb = doc["ambiguities"]
        if [e[0] for e in entries] != [a["id"] for a in amb]:
            return f"ambiguity ids {[a['id'] for a in amb]} != {[e[0] for e in entries]}"
        for (_id, detail), a in zip(entries, amb, strict=True):
            vals = [c["value"] for c in a["candidates"]]
            if not detail or detail == a["reason"]:
                continue
            cm = re.fullmatch(r"(\d+) (metric|dimension|time_dimension)s?", detail)
            if cm:
                kinds = {c["kind"] for c in a["candidates"]}
                if (len(vals), kinds) != (int(cm.group(1)), {cm.group(2)}):
                    return f"{a['id']}: {len(vals)} {kinds} != {detail}"
            elif sorted(detail.split(", ")) != sorted(vals):
                return f"{a['id']}: candidates {vals} != {detail}"
        return None
    reasons = {u["reason"] for u in doc["unresolved"]}
    times = re.fullmatch(r"(.*) \(×(\d+)\)", result)  # row 57: "reason (×2)" = two entries
    if times:
        n = sum(1 for u in doc["unresolved"] if u["reason"] == times.group(1))
        return None if n == int(times.group(2)) else f"{n} entries with {times.group(1)!r} != {times.group(2)}"
    # rows with a full-output pin (40, 41, 42, 49) list more entries; the pin is checked separately
    return None if result in reasons else f"unresolved reasons {reasons} lack {result}"


def full_output_pins() -> dict[int, list[str]]:
    """The 'Full-output expectations for rows 36–50' paragraph of §5.15, split by row."""
    text = ARCH.read_text(encoding="utf-8")
    start = text.index("**Full-output expectations for rows 36")
    end = text.index("\n## 7.", start)
    body = text[start:end]
    body = " ".join(body.split())  # the paragraph is hard-wrapped
    body = body[body.index("fields): row ") + len("fields): "):]
    body, _, d421 = body.partition("Owner amendment D421 pins")
    pins: dict[int, list[str]] = {}
    if d421:  # rows 55-58: shared clause "(exit 6 each; `bindings` = [...]; `grain` null; no ambiguities)"
        common = re.search(r"`bindings` = \[([^\]]*)\]", d421).group(0) + ", `grain` null"
        for m in re.finditer(r"rows? (\d+)(?: and (\d+))? `unresolved` = \[(.+?)\]", d421):
            for n in filter(None, m.groups()[:2]):
                pins.setdefault(int(n), []).append(f"`unresolved` = [{m.group(3)}], {common}")
    for part in re.split(r"(?:^|;\s+|\.\s+Owner amendment D417 pins:\s+)row (?=\d+ )", body.strip().rstrip(".")):
        m = re.match(r"(\d+) (.*)", part, re.DOTALL)
        if m:
            pins.setdefault(int(m.group(1)), []).append(m.group(2).strip())
    return pins


def check_pins(row: int, pins: list[str], doc: dict | None, err: str) -> list[str]:
    bad: list[str] = []
    for pin in pins:
        m = re.search(r"stderr `([^`]+)`", pin)
        if m and err.strip() != m.group(1):
            bad.append(f"row {row}: stderr {err.strip()!r} != {m.group(1)!r}")
        if doc is None:
            continue
        m = re.search(r"`bindings` = \[([^\]]*)\]", pin)
        if m and [b["object"] for b in doc["bindings"]] != [x.strip() for x in m.group(1).split(",")]:
            bad.append(f"row {row}: bindings {doc['bindings']} != [{m.group(1)}]")
        m = re.search(r"`bindings` exclude `([^`]+)`", pin)
        if m and any(b["object"] == m.group(1) for b in doc["bindings"]):
            bad.append(f"row {row}: bindings contain {m.group(1)}")
        if "`grain` null" in pin and doc["grain"] is not None:
            bad.append(f"row {row}: grain {doc['grain']} != null")
        m = re.search(r"`grain` (day|week|month|quarter|year)\b", pin)
        if m and doc["grain"] != m.group(1):
            bad.append(f"row {row}: grain {doc['grain']} != {m.group(1)}")
        m = re.search(r"`unresolved` = \[(.+?)\]", pin)
        if m:
            want = [tuple(x.split(": ", 1)) for x in m.group(1).split(", ")]
            got = [(u["word"], u["reason"]) for u in doc["unresolved"]]
            if got != want:
                bad.append(f"row {row}: unresolved {got} != {want}")
        m = re.search(r"`unresolved` lists `([^`]+)`", pin)
        if m and tuple(m.group(1).split(": ", 1)) not in [(u["word"], u["reason"]) for u in doc["unresolved"]]:
            bad.append(f"row {row}: unresolved lacks {m.group(1)}")
        m = re.search(r"`unresolved` word `([^`]+)`", pin)
        if m and m.group(1) not in [u["word"] for u in doc["unresolved"]]:
            bad.append(f"row {row}: unresolved has no word {m.group(1)!r}")
    return bad


def test_full_output_paragraph_is_parsed():
    pins = full_output_pins()
    assert sorted(pins) == [38, 40, 41, 42, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58]


def test_normative_cases(tmp_path, capsys):
    rows = normative_rows()
    assert [r["n"] for r in rows] == list(range(1, 59)), "the §5.15 table must have rows 1..58"
    p = resolver_project(tmp_path)
    lines, failures = [], []
    for row in rows:
        choose = [f"{k}={v}" for k, v in row["choose"]]
        args = ["resolve", "--model", row["model"], "--question", row["question"]]
        for c in choose:
            args += ["--choose", c]
        (p / "out").mkdir(exist_ok=True)
        (p / "out" / "resolution.json").write_text("STALE")  # a refused pick must remove it
        if row["after_success"]:  # D417: a valid result written by a preceding successful call
            rc0, _o, _e = cli(p, capsys, "resolve", "--model", MODEL, "--question", row["question"])
            assert rc0 == 0 and (p / "out" / "resolution.json").read_bytes() != b"STALE"
        rc, out, err = cli(p, capsys, *args)
        pins = full_output_pins().get(row["n"], [])
        if row["status"] == "E_MODEL_NOT_FOUND":  # row 52: exit 2, stdout empty, file removed
            last = err.strip().splitlines()[-1:] or [""]
            ok = (rc == 2 and out == "" and last[0].startswith("E_MODEL_NOT_FOUND")
                  and not (p / "out" / "resolution.json").exists())
            lines.append(f"{row['n']:>2} {row['model']:<24} {row['status']:<10} rc={rc} "
                         f"{'OK' if ok else 'FAIL ' + str(last)}  | {row['question']} (after a successful call)")
            if not ok:
                failures.append(lines[-1])
            continue
        if row["status"] == "E_BAD_CHOICE":
            pin_bad = check_pins(row["n"], pins, None, err)
            failures.extend(pin_bad)
            last = err.strip().splitlines()[-1:] or [""]
            ok = rc == 2 and out == "" and last[0].startswith("E_BAD_CHOICE: ") and row["result"] in last[0]
            after = (p / "out" / "resolution.json").read_bytes() if (p / "out" / "resolution.json").exists() else None
            ok = ok and after is None  # exit 2: stdout empty and any existing file removed
            lines.append(f"{row['n']:>2} {row['model']:<24} {row['status']:<10} rc={rc} "
                         f"{'OK' if ok else 'FAIL ' + str(last)}  | {row['question']} {choose or ''}")
            if not ok:
                failures.append(lines[-1])
            continue
        doc = json.loads(out)
        failures.extend(check_pins(row["n"], pins, doc, err))
        file_bytes = (p / "out" / "resolution.json").read_bytes()
        problem = check_result(row, doc)
        if rc != EXIT[row["status"]]:
            problem = problem or f"exit {rc}"
        if out.encode() != file_bytes:
            problem = problem or "stdout bytes differ from out/resolution.json"
        if row["status"] != "RESOLVED":
            code = "E_RESOLVE_AMBIGUOUS" if row["status"] == "AMBIGUOUS" else "E_RESOLVE_UNRESOLVED"
            last = err.strip().splitlines()[-1:] or [""]
            if not last[0].startswith(code + ": "):
                problem = problem or f"stderr last line {last}"
        elif err:
            problem = problem or f"unexpected stderr {err!r}"
        lines.append(f"{row['n']:>2} {row['model']:<24} {row['status']:<10} rc={rc} "
                     f"{'OK' if problem is None else 'FAIL ' + problem}  | {row['question']} {choose or ''}")
        if problem:
            failures.append(lines[-1])
    print("\n".join(lines))
    assert not failures, "\n".join(failures)


# -- output contract -------------------------------------------------------------------------


def test_output_shape_and_bytes(tmp_path, capsys):
    p = resolver_project(tmp_path)
    rc, out, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question", "Total credits used by month?")
    assert rc == 0
    doc = json.loads(out)
    assert sorted(doc) == DOC_KEYS
    assert doc["schema_version"] == "agent-bi-resolution/1.0"
    assert out == json.dumps(doc, sort_keys=True, indent=2) + "\n"
    assert doc["words"] == ["total", "credit", "used", "by", "month"]
    assert doc["semantic_query"] == {
        "schema_version": "semantic-query/1.0", "model": MODEL, "table": "warehouse_metering_history",
        "metrics": ["total_credits_used"], "dimensions": [],
        "time": {"dimension": "usage_day", "grain": "month"},
    }
    assert doc["query_hash"] == query_hash(doc["semantic_query"])
    assert (p / "out" / "resolution.json").read_bytes() == out.encode()


def test_non_resolved_has_null_query_and_hash(tmp_path, capsys):
    p = resolver_project(tmp_path)
    for q in ("credits by warehouse", "total credits used by warehouse for finance"):
        rc, out, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question", q)
        doc = json.loads(out)
        assert rc in (5, 6) and doc["semantic_query"] is None and doc["query_hash"] is None


def test_candidates_carry_value_kind_description(tmp_path, capsys):
    p = resolver_project(tmp_path)
    _, out, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question", "how much compute by warehouse")
    amb = json.loads(out)["ambiguities"]
    assert len(amb) == 1 and amb[0]["id"] == "compute"
    assert [sorted(c) for c in amb[0]["candidates"]] == [["description", "kind", "value"]] * 2
    assert {c["kind"] for c in amb[0]["candidates"]} == {"metric"}


# -- --choose (rule 10) ----------------------------------------------------------------------


@pytest.mark.parametrize("choose", [
    ["compute=row_count"],                      # not one of that ambiguity's candidates
    ["compute=warehouse_name"],                 # an object, but not a candidate
    ["nothing=row_count"],                      # no ambiguity with that id
    ["compute"],                                # malformed
    ["compute=total_credits_used_compute", "compute=avg_credits_used_compute"],  # two values
])
def test_bad_choice_exits_2_and_writes_nothing(tmp_path, capsys, choose):
    p = resolver_project(tmp_path)
    args = ["resolve", "--model", MODEL, "--question", "how much compute by warehouse"]
    for c in choose:
        args += ["--choose", c]
    rc, out, err = cli(p, capsys, *args)
    assert rc == 2 and out == ""
    assert err.strip().splitlines()[-1].startswith("E_BAD_CHOICE: ")
    assert not (p / "out").exists()


def test_choose_on_a_question_with_no_ambiguity_is_a_bad_choice(tmp_path, capsys):
    p = resolver_project(tmp_path)
    rc, _, err = cli(p, capsys, "resolve", "--model", MODEL, "--question", "row count by warehouse name",
                     "--choose", "row=row_count")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_BAD_CHOICE: ")


def test_a_choice_is_applied_only_when_passed(tmp_path, capsys):
    p = resolver_project(tmp_path)
    rc, out, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question", "how much compute by warehouse")
    assert rc == 5 and json.loads(out)["semantic_query"] is None
    rc, out, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question", "how much compute by warehouse",
                     "--choose", "compute=avg_credits_used_compute")
    assert rc == 0 and json.loads(out)["semantic_query"]["metrics"] == ["avg_credits_used_compute"]


def test_grain_and_date_choices(tmp_path, capsys):
    p = resolver_project(tmp_path)
    rc, out, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question",
                     "total credits used by day and month", "--choose", "@grain=month")
    assert rc == 0 and json.loads(out)["semantic_query"]["time"] == {"dimension": "usage_day", "grain": "month"}
    rc, out, _ = cli(p, capsys, "resolve", "--model", "two_dates", "--question",
                     "total credits used by billing date and usage day", "--choose", "@date=usage_day")
    assert rc == 0 and json.loads(out)["semantic_query"]["time"] == {"dimension": "usage_day", "grain": "day"}
    rc, out, err = cli(p, capsys, "resolve", "--model", "two_dates", "--question",
                       "total credits used by billing date and usage day", "--choose", "@date=nothing")
    assert rc == 2 and err.strip().splitlines()[-1].startswith("E_BAD_CHOICE: ")


# -- closed-grammar spot checks (each rule, one line) ----------------------------------------


def run_doc(question: str, model: str = MODEL) -> dict:
    return resolve(load_model(model), question)


def test_rule1_words():
    assert words_of("Credits, WAREHOUSES; class-as_is?") == ["credit", "warehouse", "class", "as", "is"]
    assert words_of("") == [] and words_of("?!") == []
    assert words_of("abc abcs ab") == ["abc", "abc", "ab"]  # 'abcs' is longer than 3, 'abc' is not


def test_descriptions_are_not_matched():
    # "Sum" appears in descriptions only
    doc = run_doc("sum by warehouse name")
    assert doc["status"] == "UNRESOLVED"
    assert [u["reason"] for u in doc["unresolved"]] == ["no matching name or synonym"]


def test_facts_are_never_candidates():
    doc = run_doc("credits used compute by warehouse name")
    assert all(c["kind"] != "fact" for a in doc["ambiguities"] for c in a["candidates"])
    assert {b["object"] for b in doc["bindings"]} <= {
        m["name"] for m in load_model(MODEL)["tables"][0]["metrics"]
    } | {"warehouse_name", "usage_day", "day", "month"}


def test_only_a_time_dimension_is_not_enough():
    doc = run_doc("usage day")
    assert doc["status"] == "UNRESOLVED"
    assert [u["reason"] for u in doc["unresolved"]] == ["no metric or dimension"]
    assert run_doc("")["status"] == "UNRESOLVED"


def test_synonyms_bind_by_phrase_and_by_word():
    model = load_model(MODEL)
    table = model["tables"][0]
    for m in table["metrics"]:
        if m["name"] == "total_credits_used":
            m["synonyms"] = ["spend", "total spend"]
    for d in table["dimensions"]:
        d["synonyms"] = ["wh", "virtual warehouse"]
    doc = resolve(model, "total spend by virtual warehouse")
    assert doc["status"] == "RESOLVED"
    assert (doc["semantic_query"]["metrics"], doc["semantic_query"]["dimensions"]) == (
        ["total_credits_used"], ["warehouse_name"])
    assert doc["bindings"] == [{"object": "total_credits_used", "kind": "metric"},
                               {"object": "warehouse_name", "kind": "dimension"}]
    doc = resolve(model, "total credits used by virtual")  # one word of a two-word synonym
    assert doc["status"] == "RESOLVED"
    assert [b["object"] for b in doc["bindings"]] == ["total_credits_used", "warehouse_name"]


def test_a_shared_phrase_never_binds_by_file_order():
    model = load_model("tie")
    doc = resolve(model, "spend by warehouse name")
    assert doc["status"] == "AMBIGUOUS" and doc["semantic_query"] is None
    assert [(a["id"], a["reason"]) for a in doc["ambiguities"]] == [("spend", "phrase shared by several objects")]
    assert [c["value"] for c in doc["ambiguities"][0]["candidates"]] == [
        "total_credits_used", "total_credits_used_compute"]
    assert [b["object"] for b in doc["bindings"]] == ["warehouse_name"]
    # reversing the file order of the metrics changes nothing in the output
    model["tables"][0]["metrics"].reverse()
    assert resolve(model, "spend by warehouse name") == doc


def test_a_shared_phrase_choice_is_validated_and_ordered_first():
    model = load_model("tie")
    doc = resolve(model, "credits spend by warehouse name")
    assert [a["id"] for a in doc["ambiguities"]] == ["spend", "credit"]  # phrase ties, then words
    for bad in ("avg_credits_used", "nothing"):
        with pytest.raises(Exception, match="BAD_CHOICE|candidates|ambiguity"):
            resolve(model, "spend by warehouse name", {"spend": bad})
    ok = resolve(model, "spend by warehouse name", {"spend": "total_credits_used_compute"})
    assert ok["semantic_query"]["metrics"] == ["total_credits_used_compute"]
    assert [b["object"] for b in ok["bindings"]] == ["total_credits_used_compute", "warehouse_name"]


def test_phrases_bind_over_function_words_but_a_lone_function_word_is_never_bound():
    model = load_model("tie")
    assert resolve(model, "cost of compute by warehouse name")["semantic_query"]["metrics"] == [
        "avg_credits_used_compute"]
    # the 'of' inside the phrase does not end the grouping phrase: the metric is in grouping position
    doc = resolve(model, "total credits used by cost of compute month")
    assert doc["status"] == "UNRESOLVED"
    assert {u["reason"] for u in doc["unresolved"]} == {"metric in grouping position"}  # month is a grain
    # lone function words are skipped, never bound
    doc = resolve(model, "show me the of warehouse names")
    assert doc["status"] == "RESOLVED" and [b["object"] for b in doc["bindings"]] == ["warehouse_name"]
    # 'cost of' alone is not the phrase: 'cost' matches by word, 'of' is skipped
    doc = resolve(model, "cost of warehouse names")
    assert [b["object"] for b in doc["bindings"]] == ["avg_credits_used_compute", "warehouse_name"]


def test_resolver_ids_cannot_collide_with_question_words():
    model = load_model("two_dates")
    doc = resolve(model, "total credits used by date and month")  # 'date' is also a word of billing_date
    assert "@date" not in {b["object"] for b in doc["bindings"]}
    for a in resolve(model, "total credits used by day and month")["ambiguities"]:
        assert a["id"].startswith("@")


def reverse_model(model: dict) -> dict:
    """The same model with every object list and synonym list in the opposite file order."""
    import copy

    m = copy.deepcopy(model)
    t = m["tables"][0]
    for section in ("metrics", "dimensions", "time_dimensions"):
        if t.get(section):
            t[section].reverse()
            for o in t[section]:
                o["synonyms"] = list(reversed(o.get("synonyms") or []))
    return m


def test_equal_length_overlap_never_depends_on_file_order():
    """D403 A-2 / falsifier F9: reversing the YAML order must not change any output."""
    rv1 = load_model("rv1")
    q = "total sales by customer region manager"
    doc = resolve(rv1, q)
    assert doc["status"] == "AMBIGUOUS" and doc["semantic_query"] is None
    assert [(a["id"], a["reason"]) for a in doc["ambiguities"]] == [
        ("customer region manager", "overlapping phrases")]
    assert [c["value"] for c in doc["ambiguities"][0]["candidates"]] == ["customer_region", "region_manager"]
    assert resolve(reverse_model(rv1), q) == doc
    # swapping only the two dimensions (the reviewer's probe) is the same
    swapped = load_model("rv1")
    dims = swapped["tables"][0]["dimensions"]
    i, j = (next(k for k, d in enumerate(dims) if d["name"] == n) for n in ("customer_region", "region_manager"))
    dims[i], dims[j] = dims[j], dims[i]
    assert resolve(swapped, q) == doc
    # every normative row of every fixture model is file-order independent
    for row in normative_rows():
        if row["model"] not in MODELS.values():  # row 52 (D417) is a missing-model row
            continue
        model = load_model(row["model"])
        choices = dict(row["choose"])
        try:
            a = resolve(model, row["question"], choices)
        except Exception as exc:  # noqa: BLE001 - an E_BAD_CHOICE row: same error both ways
            with pytest.raises(type(exc)):
                resolve(reverse_model(model), row["question"], choices)
            continue
        assert resolve(reverse_model(model), row["question"], choices) == a, row["n"]


def test_a_pick_is_checked_against_the_candidates_shown_for_its_own_id():
    """D403 A-1: the same word with different candidate sets gets one id per occurrence."""
    rv4 = load_model("rv4")
    q = "by warehouse show warehouse total credits"
    doc = resolve(rv4, q)
    assert [a["id"] for a in doc["ambiguities"]] == ["warehouse:2", "warehouse:4"]
    shown = {a["id"]: [c["value"] for c in a["candidates"]] for a in doc["ambiguities"]}
    assert shown["warehouse:2"] == ["warehouse_name", "warehouse_size"]
    assert shown["warehouse:4"] == ["distinct_warehouse_name", "warehouse_name", "warehouse_size"]
    for choices in ({"warehouse": "distinct_warehouse_name"}, {"warehouse:2": "distinct_warehouse_name"}):
        with pytest.raises(Exception, match="E_BAD_CHOICE"):
            resolve(rv4, q, choices)
    # a valid pick for one occurrence leaves the other displayed (nothing is erased)
    half = resolve(rv4, q, {"warehouse:4": "distinct_warehouse_name"})
    assert half["status"] == "AMBIGUOUS" and [a["id"] for a in half["ambiguities"]] == ["warehouse:2"]
    other = resolve(rv4, q, {"warehouse:2": "warehouse_size"})
    assert other["status"] == "AMBIGUOUS" and [a["id"] for a in other["ambiguities"]] == ["warehouse:4"]
    # the same candidate set shares one plain id and one pick binds both
    same = resolve(rv4, "show warehouse show warehouse total credits")
    assert [a["id"] for a in same["ambiguities"]] == ["warehouse"]
    both = resolve(rv4, "show warehouse show warehouse total credits", {"warehouse": "warehouse_size"})
    assert both["semantic_query"]["dimensions"] == ["warehouse_size"]
    assert [b["object"] for b in both["bindings"]] == ["total_credits", "warehouse_size"]  # distinct objects


def test_function_words_of_an_ambiguous_span_are_neutral_for_grouping_in_every_round():
    """D403 A-3: an unpicked tied span consumes its function words exactly as a bound phrase does,
    so what the user sees does not change when a pick is made."""
    model = load_model("rv1")
    for m in model["tables"][0]["metrics"]:
        if m["name"] == "order_count":
            m["synonyms"] = [*m["synonyms"], "sales by region"]  # now shared with total_sales
    q = "sales by region month"
    before = resolve(model, q)
    assert [(a["id"], a["reason"]) for a in before["ambiguities"]] == [
        ("sale by region", "phrase shared by several objects")]
    outside = [{"word": "month", "reason": "time grain outside grouping phrase"}]
    assert before["unresolved"] == outside  # 'by' inside the span did not open a grouping phrase
    after = resolve(model, q, {"sale by region": "order_count"})
    assert after["unresolved"] == outside and after["ambiguities"] == []
    assert [b["object"] for b in after["bindings"]] == ["order_count"]


def test_the_rest_of_a_picked_span_is_rejected_material():
    """Rule 4 pick (Sprint 4P): the other words of the span never re-enter matching."""
    rv1 = load_model("rv1")
    doc = resolve(rv1, "total sales by customer region manager", {"customer region manager": "customer_region"})
    assert doc["status"] == "UNRESOLVED"
    assert doc["unresolved"] == [{"word": "manager", "reason": "rejected interpretation material"}]
    assert [b["object"] for b in doc["bindings"]] == ["customer_region", "total_sales"]


def test_rv_fixtures_are_valid_models_with_the_objects_5_15_lists():
    from agent_bi.semantic.model import parse_model
    from agent_bi.semantic.names import check_global_namespace, table_objects
    from agent_bi.semantic.validate import _check_exprs, _check_subset

    want = {
        "rv1": {
            "metrics": ["total_sales", "order_count", "avg_order_value", "cost_per_unit", "region_total"],
            "dimensions": ["order_status", "region", "customer_region", "region_manager", "customer_name",
                           "order_month", "date", "status_class"],
            "time_dimensions": ["order_date", "ship_date", "due_date"],
            "synonyms": {"total_sales": ["revenue", "sales by region"], "order_count": ["orders"],
                         "region_total": ["region"]},
        },
        "rv4": {
            "metrics": ["distinct_warehouse_name", "total_credits"],
            "dimensions": ["warehouse_name", "warehouse_size"], "time_dimensions": ["usage_day"],
            "synonyms": {},
        },
    }
    for name, w in want.items():
        raw = load_model(name)
        pm = parse_model(raw, name)
        _check_subset(pm, name)
        table = pm.tables[0].model_dump(by_alias=True, exclude_unset=True)
        check_global_namespace(table_objects(table))
        _check_exprs(table)
        t = raw["tables"][0]
        for section in ("metrics", "dimensions", "time_dimensions"):
            assert [o["name"] for o in t[section]] == w[section]
        got = {o["name"]: o["synonyms"] for sec in ("metrics", "dimensions", "time_dimensions")
               for o in t[sec] if o["synonyms"]}
        assert got == w["synonyms"]


# -- determinism and purity (task 2) ---------------------------------------------------------


def _run_process(project: Path, hashseed: str, *args: str) -> tuple[int, bytes, bytes]:
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    proc = subprocess.run(
        [sys.executable, "-m", "agent_bi.main", "--config", str(project / "agent-bi.yaml"), *args],
        capture_output=True, env=env, cwd=project, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_determinism_across_runs_and_processes(tmp_path):
    p = resolver_project(tmp_path)
    questions = [
        ("how much compute by warehouse", MODEL), ("total credits used by warehouse and month", MODEL),
        ("total credits used by month", "two_dates"), ("Credits by warehouse?", MODEL),
        ("total credits used by warehouse or day", MODEL),
    ]
    lines = []
    for question, model in questions:
        args = ("resolve", "--model", model, "--question", question)
        runs = []
        for hashseed in ("0", "1", "random", "random"):
            rc, out, err = _run_process(p, hashseed, *args)
            runs.append((rc, out, err, (p / "out" / "resolution.json").read_bytes()))
        assert len({r for r in runs}) == 1, question
        rc, out, err, saved = runs[0]
        assert out == saved
        lines.append(f"{question!r} [{model}] rc={rc} sha256(stdout)={__import__('hashlib').sha256(out).hexdigest()} "
                     f"identical across 4 processes (PYTHONHASHSEED 0,1,random,random)")
    print("\n".join(lines))


def test_two_in_process_runs_are_byte_identical(tmp_path, capsys):
    p = resolver_project(tmp_path)
    a = cli(p, capsys, "resolve", "--model", MODEL, "--question", "total credits used by warehouse and month")
    first = (p / "out" / "resolution.json").read_bytes()
    b = cli(p, capsys, "resolve", "--model", MODEL, "--question", "total credits used by warehouse and month")
    assert a == b and first == (p / "out" / "resolution.json").read_bytes()


FORBIDDEN_IMPORTS = {"urllib", "http", "socket", "requests", "anthropic", "openai"}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_resolver_imports_no_network_or_model_client():
    imports = _imports(Path(resolve_module.__file__))
    assert imports, "found no imports to check"
    bad = {i for i in imports if i.split(".")[0] in FORBIDDEN_IMPORTS}
    assert not bad, bad
    print("resolve.py imports:", ", ".join(sorted(imports)))


def test_resolve_leaves_semantic_unchanged_and_writes_only_resolution_json(tmp_path, capsys):
    p = resolver_project(tmp_path)
    before_semantic = tree_hash(p / "semantic")
    before_files = {f.relative_to(p).as_posix() for f in p.rglob("*") if f.is_file()}
    for q, model in [("total credits used by month", "two_dates"), ("credits by warehouse", MODEL),
                     ("warehouse names", MODEL), ("warehouse name by total credits used", MODEL)]:
        cli(p, capsys, "resolve", "--model", model, "--question", q)
        assert tree_hash(p / "semantic") == before_semantic
    cli(p, capsys, "resolve", "--model", MODEL, "--question", "how much compute by warehouse",
        "--choose", "compute=total_credits_used_compute")
    after_files = {f.relative_to(p).as_posix() for f in p.rglob("*") if f.is_file()}
    assert after_files - before_files == {"out/resolution.json"}
    assert before_files - after_files == set()
    assert tree_hash(p / "semantic") == before_semantic
    print(f"semantic/ tree sha256 before == after: {before_semantic}")
    print(f"files added by resolve: {sorted(after_files - before_files)}")


def test_resolved_query_hash_equals_asks(tmp_path, capsys):
    p = resolver_project(tmp_path)
    for question in ("avg credits used by warehouse name", "total credits used by warehouse and month",
                     "warehouse names", "row count by warehouse name"):
        rc, out, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question", question)
        assert rc == 0
        doc = json.loads(out)
        rc, aout, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(p / "out" / "resolution.json"))
        assert rc == 0, err
        env = envelope_of(p, aout)
        assert env["provenance"]["artifact"]["query_hash"] == doc["query_hash"]
        assert env["provenance"]["receipt"]["trust"].startswith("REMOVED in agent-bi-provenance-record/2.0")  # resolved is not verified
        assert env["provenance"]["receipt"]["evidence"]["definition"]["state"] == "none"
        print(f"{question!r}: resolve query_hash == ask query_hash == {doc['query_hash']}")


def test_ask_refuses_a_resolution_without_a_query(tmp_path, capsys):
    p = resolver_project(tmp_path)
    rc, _, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question", "credits by warehouse")
    assert rc == 5
    before = tree_hash(p / "out")
    rc, out, err = cli(p, capsys, "ask", "--model", MODEL, "--query-file", str(p / "out" / "resolution.json"))
    assert rc == 2 and out == ""
    assert err.strip().splitlines()[-1].startswith("E_QUERY_INVALID: ")
    assert tree_hash(p / "out") == before  # nothing was run or written


# -- refusal properties (task 3) -------------------------------------------------------------

SEED = 20261014
PER_KIND = 25
EXTRA_RV_BASES = [  # the reviewer's adversarial families (D403): overlaps, repeated words, shared names
    ("rv1", "total sales by customer region manager"), ("rv1", "revenue by region"),
    ("rv1", "orders by region and order month"), ("rv1", "region manager customer name"),
    ("rv1", "order date by status class"), ("rv1", "region"), ("rv1", "sales by region month"),
    ("rv4", "by warehouse show warehouse total credits"), ("rv4", "warehouse size by warehouse"),
    ("rv4", "distinct warehouse name"), ("rv4", "show warehouse by warehouse name"),
]
EXTRA_TIE_BASES = [  # the tie-model family: a shared synonym, and a synonym containing a function word
    "spend", "total spend by warehouse name", "cost of compute", "avg cost of compute by month",
    "spend and cost of compute by warehouse name",
]
UNKNOWN_WORDS = ["zqxv", "blorpt", "finance", "quux", "mumble"]
VARIANT_KINDS = ("permute", "insert_function", "insert_unknown", "duplicate", "insert_and_or", "plural_swap")


def tokens_of(question: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", question)


def make_variant(kind: str, tokens: list[str], rng: random.Random) -> list[str]:
    t = list(tokens)
    if kind == "permute":
        rng.shuffle(t)
    elif kind == "insert_function":
        for _ in range(rng.randint(1, 3)):
            t.insert(rng.randint(0, len(t)), rng.choice(sorted(FUNCTION_WORDS)))
    elif kind == "insert_unknown":
        for _ in range(rng.randint(1, 2)):
            t.insert(rng.randint(0, len(t)), rng.choice(UNKNOWN_WORDS))
    elif kind == "duplicate":
        for _ in range(rng.randint(1, 2)):
            i = rng.randrange(len(t))
            t.insert(rng.randint(0, len(t)), t[i])
    elif kind == "insert_and_or":
        for _ in range(rng.randint(1, 2)):
            t.insert(rng.randint(0, len(t)), rng.choice(["and", "or"]))
    elif kind == "plural_swap":
        t = [
            (w[:-1] if w.endswith("s") and len(w) > 3 else w + "s") if rng.random() < 0.5 else w
            for w in t
        ]
    else:  # pragma: no cover
        raise AssertionError(kind)
    return t


def independent_word_sets(model: dict) -> tuple[dict[str, set[str]], list[tuple[str, tuple[str, ...]]]]:
    """name -> words of its name and synonyms, and every (object, phrase), recomputed here from
    the YAML without using the resolver's own object code."""
    sets: dict[str, set[str]] = {}
    phrases: list[tuple[str, tuple[str, ...]]] = []
    table = model["tables"][0]
    for section in ("metrics", "dimensions", "time_dimensions"):
        for o in table.get(section) or []:
            for text in [o["name"], *(o.get("synonyms") or [])]:
                ws = tuple(words_of(text))
                if ws:
                    phrases.append((o["name"], ws))
                    sets.setdefault(o["name"], set()).update(ws)
    return sets, phrases


def grouping_positions(words: list[str]) -> set[int]:
    pos, open_ = set(), False
    for i, w in enumerate(words):
        if w in ("by", "per"):
            open_ = True
        elif w in FUNCTION_WORDS and w != "and":
            open_ = False
        elif w not in FUNCTION_WORDS and open_:
            pos.add(i)
    return pos


def admits_two_bindings(model: dict, words: list[str]) -> bool:
    """Independent oracle: some word sits outside every grouping phrase, outside every phrase
    occurrence that could cover it, is not a function/grain/'or' word, and matches at least two
    eligible objects by name or synonym word."""
    sets, phrases = independent_word_sets(model)
    group = grouping_positions(words)
    covered: set[int] = set()
    for _obj, ph in phrases:
        for i in range(len(words) - len(ph) + 1):
            if tuple(words[i:i + len(ph)]) == ph:
                covered.update(range(i, i + len(ph)))
    if any(words[i] in FUNCTION_WORDS for i in covered):
        return False  # grouping boundaries depend on phrase binding here: no independent claim
    # a phrase owned by two objects whose occurrence no longer phrase can overlap must be a tie
    owners: dict[tuple[str, ...], set[str]] = {}
    for obj, ph in phrases:
        owners.setdefault(ph, set()).add(obj)
    for ph, who in owners.items():
        if len(who) < 2:
            continue
        for i in range(len(words) - len(ph) + 1):
            if tuple(words[i:i + len(ph)]) != ph:
                continue
            span = range(i, i + len(ph))
            overlapped = any(
                len(other) > len(ph) and any(
                    tuple(words[j:j + len(other)]) == other
                    and set(range(j, j + len(other))) & set(span)
                    for j in range(len(words) - len(other) + 1))
                for _o, other in phrases
            )
            if not overlapped:
                return True
    for i, w in enumerate(words):
        if i in group or i in covered or w in FUNCTION_WORDS or w == "or":
            continue
        if w in ("day", "week", "month", "quarter", "year"):
            continue
        if sum(1 for ws in sets.values() if w in ws) >= 2:
            return True
    return False


def check_properties(model: dict, question: str, doc: dict, chosen: dict | None = None) -> list[str]:
    errs: list[str] = []
    if doc["status"] not in STATUSES:
        errs.append(f"status {doc['status']}")
    if sorted(doc) != DOC_KEYS:
        errs.append("keys")
    if (doc["status"] == "RESOLVED") != (doc["semantic_query"] is not None):
        errs.append("semantic_query/status mismatch")
    if doc["status"] == "AMBIGUOUS" and not doc["ambiguities"]:
        errs.append("AMBIGUOUS without ambiguities")
    if doc["status"] != "AMBIGUOUS" and doc["ambiguities"]:
        errs.append("ambiguities hidden by status")
    if doc["status"] == "UNRESOLVED" and not doc["unresolved"]:
        errs.append("UNRESOLVED without reasons")
    if doc["status"] != "RESOLVED":
        return errs
    sq = doc["semantic_query"]
    try:
        assert parse_query(sq).model_dump(exclude_none=True) and query_hash(sq) == doc["query_hash"]
    except Exception as exc:  # noqa: BLE001 - any failure is a property violation
        errs.append(f"semantic_query invalid: {exc}")
    sets, phrases = independent_word_sets(model)
    words = doc["words"]
    table = model["tables"][0]
    sole_time = len(table.get("time_dimensions") or []) == 1
    objects = [*sq["metrics"], *sq["dimensions"]]
    if sq.get("time"):
        objects.append(sq["time"]["dimension"])
    if [b["object"] for b in doc["bindings"]] != sorted(objects):
        errs.append("bindings do not equal the SemanticQuery")  # Output: equal when RESOLVED
    if doc["grain"] != (sq.get("time") or {}).get("grain"):
        errs.append("grain does not equal the SemanticQuery time grain")
    grain_in_question = any(w in ("day", "week", "month", "quarter", "year") for w in words)
    for name in objects:
        is_time = name == (sq.get("time") or {}).get("dimension")
        by_phrase = any(
            o == name and tuple(words[i:i + len(p)]) == p
            for o, p in phrases for i in range(len(words) - len(p) + 1)
        )
        by_word = any(w in sets.get(name, set()) and w not in FUNCTION_WORDS for w in words)
        if by_phrase or by_word:
            continue
        # a time dimension may be reached through a grain word (sole date), or by a human @date pick
        if is_time and grain_in_question and (sole_time or "@date" in (chosen or {})):
            continue
        errs.append(f"{name} bound without being named")
    return errs


def pick_properties(model: dict, question: str, doc: dict) -> list[str]:
    """Replays of user picks (D403 A-1): a pick outside an id's shown candidates is E_BAD_CHOICE;
    a shown pick never erases another displayed ambiguity; picking the first candidate of
    every displayed ambiguity, round by round, ends in a status the checks accept."""
    errs: list[str] = []
    if doc["status"] != "AMBIGUOUS":
        return errs
    all_objects = [o["name"] for sec in ("metrics", "dimensions", "time_dimensions")
                   for o in model["tables"][0].get(sec) or []]
    for a in doc["ambiguities"]:
        shown = [c["value"] for c in a["candidates"]]
        for bad in [o for o in all_objects if o not in shown][:3]:
            try:
                resolve(model, question, {a["id"]: bad})
                errs.append(f"pick {a['id']}={bad} outside the shown candidates was accepted")
            except Exception as exc:  # noqa: BLE001
                if "BAD_CHOICE" not in str(exc):
                    errs.append(f"pick outside candidates raised {exc}")
        for good in shown[:2]:
            after = resolve(model, question, {a["id"]: good})
            still = {x["id"] for x in after["ambiguities"]}
            for other in doc["ambiguities"]:
                if other["id"] != a["id"] and not other["id"].startswith("@") and other["id"] not in still:
                    errs.append(f"pick {a['id']}={good} erased the ambiguity {other['id']}")
            if a["id"] in still and not a["id"].startswith("@"):
                errs.append(f"pick {a['id']}={good} left its own ambiguity")
    s1: dict[str, str] = {}  # stage-1 picks accumulate; stage-2 picks are re-derived each time
    for _ in range(6):
        d = resolve(model, question, s1)
        new = {a["id"]: a["candidates"][0]["value"] for a in d["ambiguities"]
               if not a["id"].startswith("@") and a["id"] not in s1}
        if not new:
            break
        s1.update(new)
    d = resolve(model, question, s1)
    s2 = {a["id"]: a["candidates"][0]["value"] for a in d["ambiguities"] if a["id"].startswith("@")}
    choices = {**s1, **s2}
    cur = resolve(model, question, choices)
    if cur["status"] == "RESOLVED":
        errs += [f"after picks {choices}: {e}" for e in check_properties(model, question, cur, choices)]
    return errs


def test_refusal_properties():
    rng = random.Random(SEED)
    rows = normative_rows()
    bases = [(r["model"], r["question"]) for r in rows
             if not r["choose"] and r["model"] in MODELS.values()]
    bases += [("tie", q) for q in EXTRA_TIE_BASES]
    bases += EXTRA_RV_BASES
    models = {m: load_model(m) for m in {b[0] for b in bases}}
    statuses: dict[str, Counter] = {k: Counter() for k in VARIANT_KINDS}
    oracle_hits = unknown_checked = total = 0
    failures: list[str] = []
    for model_name, question in bases:
        model = models[model_name]
        for kind in VARIANT_KINDS:
            for _ in range(PER_KIND):
                toks = make_variant(kind, tokens_of(question), rng)
                variant = " ".join(toks)
                doc = resolve(model, variant)
                total += 1
                statuses[kind][doc["status"]] += 1
                errs = check_properties(model, variant, doc)
                if resolve(model, variant) != doc:
                    errs.append("not deterministic")
                if resolve(reverse_model(model), variant) != doc:  # F9: file order never matters
                    errs.append("output depends on the YAML order")
                errs += pick_properties(model, variant, doc)
                if kind == "insert_unknown":
                    unknown_checked += 1
                    if not any(t in UNKNOWN_WORDS for t in toks):
                        errs.append("generator bug: no unknown word inserted")
                    if doc["status"] == "RESOLVED":
                        errs.append("inserted unknown word but RESOLVED")
                if admits_two_bindings(model, doc["words"]):
                    oracle_hits += 1
                    if doc["status"] != "AMBIGUOUS":
                        errs.append(f"words admit two bindings but status {doc['status']}")
                if errs:
                    failures.append(f"[{model_name}] {variant!r}: {errs}")
    print(f"tie-model family: {[b for b in bases if b[0] == 'tie']}")
    print(f"generator: random.Random({SEED}) over {len(bases)} base questions x {len(VARIANT_KINDS)} "
          f"kinds x {PER_KIND} variants = {total} variants; kinds: {', '.join(VARIANT_KINDS)}")
    for kind in VARIANT_KINDS:
        print(f"  {kind:<16} {dict(sorted(statuses[kind].items()))}")
    print(f"inserted-unknown variants checked: {unknown_checked}; two-binding oracle fired on {oracle_hits}")
    print(f"violations: {len(failures)}")
    assert not failures, "\n".join(failures[:20])
    assert oracle_hits > 0 and unknown_checked > 0  # the properties were not vacuous
    # sanity: the three statuses all occur
    assert {s for c in statuses.values() for s in c} == set(STATUSES)


def test_property_oracle_is_not_vacuous():
    model = load_model(MODEL)
    assert admits_two_bindings(model, words_of("show me the warehouses"))
    assert not admits_two_bindings(model, words_of("row count by warehouse name"))
    tie = load_model("tie")
    assert admits_two_bindings(tie, words_of("spend by warehouse name"))
    assert not admits_two_bindings(tie, words_of("cost of compute by warehouse name"))
    # a deliberately wrong resolver output must be caught by the checks
    doc = resolve(model, "row count by warehouse name")
    forged = {**doc, "semantic_query": {**doc["semantic_query"], "metrics": ["avg_credits_used"]},
              "query_hash": doc["query_hash"]}
    assert check_properties(model, "row count by warehouse name", forged)


def test_function_word_list_matches_section_5_15():
    m = re.search(r"\*\*Function words\*\* \(closed\): `([^`]+)`", ARCH.read_text(encoding="utf-8"))
    assert m and set(m.group(1).split()) == set(FUNCTION_WORDS)


# -- D417: every non-successful invocation removes out/resolution.json -----------------------


def _seed_valid_result(p: Path, capsys) -> bytes:
    """A real, valid result written by a successful call (not a placeholder)."""
    rc, out, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question", "total credits used by month")
    result = p / "out" / "resolution.json"
    assert rc == 0 and result.read_bytes() == out.encode() and json.loads(out)["status"] == "RESOLVED"
    return result.read_bytes()


def test_missing_model_removes_the_result_and_prints_nothing(tmp_path, capsys):
    p = resolver_project(tmp_path)
    _seed_valid_result(p, capsys)
    rc, out, err = cli(p, capsys, "resolve", "--model", "nosuch", "--question", "total credits used")
    assert rc == 2 and out == "" and err.strip().splitlines()[-1].startswith("E_MODEL_NOT_FOUND")
    assert not (p / "out" / "resolution.json").exists()


def test_invalid_model_yaml_removes_the_result_and_prints_nothing(tmp_path, capsys):
    p = resolver_project(tmp_path)
    _seed_valid_result(p, capsys)
    (p / "semantic" / "broken.yaml").write_text("name: [unclosed\n  - : :\n")
    rc, out, err = cli(p, capsys, "resolve", "--model", "broken", "--question", "total credits used")
    assert rc == 2 and out == "" and "E_MODEL_INVALID" in err
    assert not (p / "out" / "resolution.json").exists()
    _seed_valid_result(p, capsys)
    (p / "semantic" / "listy.yaml").write_text("- not\n- a mapping\n")
    rc, out, err = cli(p, capsys, "resolve", "--model", "listy", "--question", "x")
    assert rc == 2 and out == "" and not (p / "out" / "resolution.json").exists()


def test_malformed_choose_removes_the_result_and_prints_nothing(tmp_path, capsys):
    p = resolver_project(tmp_path)
    _seed_valid_result(p, capsys)
    rc, out, err = cli(p, capsys, "resolve", "--model", MODEL, "--question", "how much compute by warehouse",
                       "--choose", "compute")
    assert rc == 2 and out == "" and err.strip().splitlines()[-1] == "E_BAD_CHOICE: malformed: compute"
    assert not (p / "out" / "resolution.json").exists()


def test_resolve_usage_error_removes_the_result_and_prints_nothing(tmp_path, capsys):
    p = resolver_project(tmp_path)
    _seed_valid_result(p, capsys)
    capsys.readouterr()
    for bad in (["resolve", "--model", MODEL],                      # --question missing
                ["resolve", "--model", MODEL, "--question", "x", "--nosuch-flag"]):
        _seed_valid_result(p, capsys)
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            main(["--config", str(p / "agent-bi.yaml"), *bad])
        cap = capsys.readouterr()
        assert exc.value.code == 2 and cap.out == "" and "usage:" in cap.err
        assert not (p / "out" / "resolution.json").exists()
    # --help resolves nothing and fails nothing: it leaves an existing result alone
    _seed_valid_result(p, capsys)
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(p / "agent-bi.yaml"), "resolve", "--help"])
    assert exc.value.code == 0 and (p / "out" / "resolution.json").exists()


def test_any_other_failure_removes_the_result(tmp_path, capsys):
    p = resolver_project(tmp_path)
    _seed_valid_result(p, capsys)
    (p / "semantic" / "nameless.yaml").write_text("name: nameless\ntables:\n- name: t\n  metrics:\n  - synonyms: []\n")
    with pytest.raises(KeyError):  # an unexpected exception still propagates, after cleanup
        cli(p, capsys, "resolve", "--model", "nameless", "--question", "x")
    assert not (p / "out" / "resolution.json").exists()


def test_a_successful_exit_5_or_6_replaces_the_result(tmp_path, capsys):
    p = resolver_project(tmp_path)
    _seed_valid_result(p, capsys)
    for q, want in (("credits by warehouse", 5), ("total credits used by warehouse or day", 6)):
        rc, out, _ = cli(p, capsys, "resolve", "--model", MODEL, "--question", q)
        assert rc == want and (p / "out" / "resolution.json").read_bytes() == out.encode()


# -- D419 repair: the two failure paths found by the D417 re-verification --------------------


def test_dangling_config_after_resolve_removes_the_default_projects_result(tmp_path, capsys, monkeypatch):
    """A-1: ``resolve --model m --question x --config`` (no value) is a usage error; the applicable
    result location is the default config path, or the last parseable ``--config``."""
    p = resolver_project(tmp_path)
    monkeypatch.chdir(p)
    _seed_valid_result(p, capsys)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["resolve", "--model", MODEL, "--question", "x", "--config"])
    cap = capsys.readouterr()
    assert exc.value.code == 2 and cap.out == "" and "usage:" in cap.err
    assert not (p / "out" / "resolution.json").exists()
    # a parseable --config given before the subcommand is the location in force
    (tmp_path / "other").mkdir()
    other = resolver_project(tmp_path / "other")
    _seed_valid_result(other, capsys)
    _seed_valid_result(p, capsys)
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["--config", str(other / "agent-bi.yaml"), "resolve", "--model", MODEL, "--question", "x",
              "--config"])
    assert not (other / "out" / "resolution.json").exists()
    assert (p / "out" / "resolution.json").exists()  # the cwd project is not the one in force


class _FullStdout:
    """A stdout whose writes fail like a full disk."""

    def write(self, _text):
        raise OSError(28, "No space left on device")

    def flush(self):
        raise OSError(28, "No space left on device")


@pytest.mark.parametrize("question", ["total credits used by month",       # exit 0
                                      "credits by warehouse",              # exit 5
                                      "total credits used by warehouse or day"])  # exit 6
def test_a_failed_stdout_write_leaves_no_result_file(tmp_path, capsys, monkeypatch, question):
    """A-2: the file this call would write, and any valid earlier one, are gone."""
    p = resolver_project(tmp_path)
    _seed_valid_result(p, capsys)  # a valid earlier result for another question
    monkeypatch.setattr(sys, "stdout", _FullStdout())
    with pytest.raises(OSError):
        main(["--config", str(p / "agent-bi.yaml"), "resolve", "--model", MODEL, "--question", question])
    assert not (p / "out" / "resolution.json").exists()
    shutil.rmtree(p / "out")  # and with nothing pre-existing: still nothing from this call
    with pytest.raises(OSError):
        main(["--config", str(p / "agent-bi.yaml"), "resolve", "--model", MODEL, "--question", question])
    assert not (p / "out" / "resolution.json").exists()


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="needs /dev/full")
def test_stdout_to_dev_full_leaves_no_result_file_real_process(tmp_path):
    p = resolver_project(tmp_path)
    with open("/dev/full", "w") as full:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_bi.main", "--config", str(p / "agent-bi.yaml"), "resolve",
             "--model", MODEL, "--question", "total credits used by month"],
            stdout=full, stderr=subprocess.PIPE, cwd=p, check=False,
        )
    assert proc.returncode != 0
    assert not (p / "out" / "resolution.json").exists()


# -- D421: rule 1 keeps unsupported (non-ASCII) words instead of dropping them ---------------


def test_rule1_unsupported_words_are_kept_verbatim_in_place():
    assert words_of("Credits! 仅财务? ünïcode") == ["credit", "仅财务?", "ü", "n", "ï", "code"]
    assert words_of("ｆｉｎａｎｃｅ") == ["finance"]  # NFKC first
    assert words_of("для финансов") == ["для", "финансов"]  # never plural-normalized
    assert words_of("Total, credits; used?") == ["total", "credit", "used"]  # ASCII punctuation separates


def test_an_unsupported_word_inside_an_exact_phrase_breaks_the_match():
    iww = load_model(MODEL)
    exact = resolve(iww, "total credits used")
    assert [b["object"] for b in exact["bindings"]] == ["total_credits_used"]
    broken = resolve(iww, "total 仅 credits used")
    assert broken["words"] == ["total", "仅", "credit", "used"]
    assert "total_credits_used" not in [b["object"] for b in broken["bindings"]]
    assert broken["status"] == "AMBIGUOUS"  # total / credit / used each fall to rule 7
    assert [a["id"] for a in broken["ambiguities"]] == ["total", "credit", "used"]
    assert {"word": "仅", "reason": "no matching name or synonym"} in broken["unresolved"]


def test_unsupported_words_hold_their_position_and_count_in_ids():
    rv4 = load_model("rv4")
    doc = resolve(rv4, "by warehouse 仅 show warehouse total credits")
    assert [a["id"] for a in doc["ambiguities"]] == ["warehouse:2", "warehouse:5"]
    assert doc["unresolved"] == [{"word": "仅", "reason": "no matching name or synonym"}]


def test_an_object_phrase_with_an_unsupported_word_never_matches():
    model = load_model(MODEL)
    for m in model["tables"][0]["metrics"]:
        if m["name"] == "total_credits_used":
            m["synonyms"] = ["营收", "total 仅 credits"]
    doc = resolve(model, "营收 by warehouse name")
    assert doc["status"] == "UNRESOLVED" and doc["words"][0] == "营收"
    assert doc["unresolved"] == [{"word": "营收", "reason": "no matching name or synonym"}]
    assert [b["object"] for b in doc["bindings"]] == ["warehouse_name"]
    doc2 = resolve(model, "total 仅 credits by warehouse name")  # the synonym's words do not leak
    assert doc2["status"] == "AMBIGUOUS" and "total_credits_used" not in [b["object"] for b in doc2["bindings"]]


def test_unsupported_words_are_never_function_words_or_grains_or_disjunction():
    iww = load_model(MODEL)
    doc = resolve(iww, "total credits used by warehouse ｏｒ 日")  # NFKC makes the first one `or`
    reasons = [(u["word"], u["reason"]) for u in doc["unresolved"]]
    assert ("or", "disjunction") in reasons and ("日", "no matching name or synonym") in reasons


def _probe_model(dimension: str) -> dict:
    """metrics ``total_credits`` (synonym ``gross 收入``) and ``row_count``; one dimension."""
    def obj(n, syn=()):
        return {"name": n, "synonyms": list(syn), "expr": f'"{n}"'}
    return {"name": "probe", "tables": [{"name": "t", "metrics": [obj("total_credits", ["gross 收入"]),
                                                                 obj("row_count")],
                                         "dimensions": [obj(dimension)]}]}


def test_supported_words_of_a_synonym_with_an_unsupported_word_still_match_in_rule_7():
    """D421 repair: only the *phrase* is excluded; the supported words stay rule-7 words."""
    # (a) the word is shared with a dimension: an ambiguity, not a silent single binding
    doc = resolve(_probe_model("gross_region"), "row count gross")
    assert doc["status"] == "AMBIGUOUS" and doc["semantic_query"] is None
    assert [(a["id"], a["reason"], [c["value"] for c in a["candidates"]]) for a in doc["ambiguities"]] == [
        ("gross", "metric and dimension", ["gross_region", "total_credits"])]
    assert [b["object"] for b in doc["bindings"]] == ["row_count"]
    # (b) no other object has the word: it binds the metric that owns the synonym
    doc = resolve(_probe_model("warehouse_name"), "gross by warehouse name")
    assert doc["status"] == "RESOLVED"
    assert doc["semantic_query"]["metrics"] == ["total_credits"]
    assert doc["semantic_query"]["dimensions"] == ["warehouse_name"]
    # the phrase containing the unsupported word still never matches, with either word order
    for q in ("gross 收入 by warehouse name", "gross 收入"):
        d = resolve(_probe_model("warehouse_name"), q)
        assert {"word": "收入", "reason": "no matching name or synonym"} in d["unresolved"]
        assert d["status"] == "UNRESOLVED"
    d = resolve(_probe_model("warehouse_name"), "收入")
    assert d["bindings"] == [] and d["unresolved"] == [{"word": "收入", "reason": "no matching name or synonym"}]
