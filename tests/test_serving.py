"""Focused tests for the M2.x1 serving module (architecture §4.8.3–§4.8.4)."""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from agent_bi.verify import rill_route
from agent_bi.verify import serving as sv
from tests.s3_helpers import MODEL
from tests.s4_helpers import cli
from tests.test_m2x1_cases import (
    EXCL,
    TOMBSTONE,
    ask_receipt,
    fake_rill,
    governed_project,
    receipts,
    run_check,
    status_lines,
)

NOW = datetime(2026, 10, 17, 15, 0, 0, tzinfo=UTC)


def entry(name="q", outcome=sv.MATCH, **kw):
    base = {
        "name": name, "query_hash": "sha256:h", "semantic_digest": "sha256:d",
        "known_answer_content_sha256": "sha256:k",
        "intrinsic_scope": {"filters": [], "time_start": None, "time_end": None, "limit": 10},
        "outcome": outcome, **kw,
    }
    return sv.make_entry(**base)


def receipt(entries, finished="2026-10-17T14:42:06.000000Z", model=MODEL):
    return sv.build_receipt(
        model=model, rill_binary="rill", rill_version="v0.90.5",
        started_at="2026-10-17T14:41:58.000000Z", finished_at=finished, entries=entries,
    )


# ---------------------------------------------------------------- receipt-state partition


def _expected_state(counts):
    mm, er, ma, _ = counts
    if mm:
        return "failed"
    if er:
        return "incomplete"
    return "matched" if ma >= 1 else "no-evidence"


@pytest.mark.parametrize("counts", list(itertools.product(range(3), range(3), range(3), range(3))))
def test_receipt_state_partitions_every_outcome_grid(counts):
    mm, er, ma, sk = counts
    entries = (
        [entry(outcome=sv.MISMATCH)] * mm + [entry(outcome=sv.ERROR)] * er
        + [entry(outcome=sv.MATCH)] * ma + [entry(outcome=sv.NOT_CHECKED_STALE)] * sk
    )
    assert sv.receipt_state(entries) == _expected_state(counts)
    c = sv.receipt_counts(entries)
    assert c == {"verified_queries": len(entries), "matched": ma, "mismatched": mm,
                 "not_checked": sk, "errors": er}


# ---------------------------------------------------------------- receipt shape


def test_receipt_field_order_id_and_filename():
    r = receipt([entry()])
    assert list(r) == [
        "schema_version", "receipt_id", "model", "route", "principal", "comparator", "scope",
        "started_at", "finished_at", "entries", "counts", "state",
    ]
    assert list(r["entries"][0]) == [
        "name", "query_hash", "semantic_digest", "known_answer_content_sha256",
        "intrinsic_scope", "outcome", "executed_sql_sha256", "error_code", "diff",
    ]
    from agent_bi.hashing import canonical_dumps, sha256_text

    others = {k: v for k, v in r.items() if k != "receipt_id"}
    assert r["receipt_id"] == sha256_text(canonical_dumps(others))
    assert sv.receipt_filename(r) == (
        "20261017T144206000000Z-" + r["receipt_id"].removeprefix("sha256:") + ".json"
    )


# ---------------------------------------------------------------- latest by finished_at


def test_latest_is_by_finished_at_field_not_filename_or_mtime(tmp_path):
    old = receipt([entry()], finished="2026-10-17T10:00:00.000000Z")
    new = receipt([entry(outcome=sv.MISMATCH)], finished="2026-10-17T12:00:00.000000Z")
    sv.write_receipt(tmp_path, MODEL, old)
    new_path = sv.write_receipt(tmp_path, MODEL, new)
    copy = new_path.parent / ("99990101T000000000000Z-" + sv.receipt_filename(old).split("-", 1)[1])
    shutil.copy(sv.write_receipt(tmp_path, MODEL, old), copy)  # a copied older file, newest name
    os.utime(copy, (4_000_000_000, 4_000_000_000))  # and the newest mtime
    (new_path.parent / "broken.json").write_text("{not json")
    (new_path.parent / "other.json").write_text(json.dumps({"model": MODEL}))
    assert sv.latest_receipt(tmp_path, MODEL)["receipt_id"] == new["receipt_id"]
    assert sv.latest_receipt(tmp_path, "nothing_here") is None


def test_write_receipt_never_overwrites(tmp_path):
    r = receipt([entry()])
    path = sv.write_receipt(tmp_path, MODEL, r)
    path.write_text("kept")
    assert sv.write_receipt(tmp_path, MODEL, r) == path and path.read_text() == "kept"


# ---------------------------------------------------------------- the six applicability steps

R_ENTRY = entry("q", sv.MATCH)


def _serve(tmp_path, entries, **args):
    if entries is not None:
        sv.write_receipt(tmp_path, MODEL, receipt(entries))
    base = {"q_hash": "sha256:h", "definition_state": "governed", "current_digest": "sha256:d",
            "current_known_answer_digest": "sha256:k", "now": NOW, **args}
    return sv.answer_serving(tmp_path, MODEL, **base)


def test_step_1_no_receipt(tmp_path):
    info = _serve(tmp_path, None)
    assert info.applicability is None and info.state is None
    assert info.claim == "No serving check recorded."


def test_step_2_not_verified(tmp_path):
    info = _serve(tmp_path, [R_ENTRY], definition_state="none")
    assert info.applicability == "not-verified" and info.state == "matched"
    assert info.claim == "This question is not a verified query; serving checks do not cover it."


def test_step_3_definition_stale(tmp_path):
    info = _serve(tmp_path, [R_ENTRY], definition_state="stale")
    assert info.applicability == "definition-stale"
    assert info.claim == (
        "This question's definition differs from its verified version; no serving check "
        "applies to the current definition."
    )


def test_step_4_not_in_check(tmp_path):
    info = _serve(tmp_path, [R_ENTRY], q_hash="sha256:other")
    assert info.applicability == "not-in-check" and info.query_outcome is None
    assert info.claim == (
        "This question was not part of the serving check during "
        "2026-10-17 14:41:58–14:42:06 UTC (17 min ago)."
    )


@pytest.mark.parametrize(("outcome", "verb"), [
    (sv.MATCH, "compared"), (sv.MISMATCH, "compared"), (sv.NOT_CHECKED_STALE, "skipped"),
    (sv.NOT_CHECKED_UNSUPPORTED, "skipped"), (sv.ERROR, "attempted"),
])
@pytest.mark.parametrize("which", ["semantic_digest", "known_answer"])
def test_step_5_different_definition_or_answer(tmp_path, outcome, verb, which):
    e = entry("q", outcome, error_code="E_ENGINE" if outcome == sv.ERROR else None)
    kw = {"current_digest": "sha256:new"} if which == "semantic_digest" else {
        "current_known_answer_digest": "sha256:new"}
    info = _serve(tmp_path, [e], **kw)
    assert info.applicability == "different-definition-or-answer" and info.query_outcome is None
    assert info.claim == (
        f"The serving check during 2026-10-17 14:41:58–14:42:06 UTC (17 min ago) {verb} this "
        "question under a different definition or known answer than the current one; it is "
        "not serving evidence for the current definition."
    )


@pytest.mark.parametrize(("e", "tail"), [
    (entry("q", sv.MATCH), "returned its known answer under agent-bi-known-answer/1."),
    (entry("q", sv.MISMATCH, diff=["x"]), "returned a result different from its known answer."),
    (entry("q", sv.NOT_CHECKED_UNSUPPORTED),
     "was skipped (not supported by the route); it was not compared."),
    (entry("q", sv.NOT_CHECKED_STALE), "was skipped (stale definition); it was not compared."),
    (entry("q", sv.ERROR, error_code="E_ENGINE"),
     "was attempted but could not be asked (E_ENGINE); it was not compared."),
])
def test_step_6_applies(tmp_path, e, tail):
    info = _serve(tmp_path, [e])
    assert info.applicability == "applies" and info.query_outcome == e["outcome"]
    assert info.claim == (
        "In the serving check during 2026-10-17 14:41:58–14:42:06 UTC (17 min ago), this "
        f"question {tail}"
    )


def test_steps_are_ordered_first_match_wins(tmp_path):
    # stale definition beats not-in-check and a digest difference
    info = _serve(tmp_path, [R_ENTRY], definition_state="stale", q_hash="x", current_digest="y")
    assert info.applicability == "definition-stale"
    info = _serve(tmp_path, [R_ENTRY], definition_state="none", q_hash="x")
    assert info.applicability == "not-verified"


# ---------------------------------------------------------------- interval and age


@pytest.mark.parametrize(("seconds", "text"), [
    (0, "0 s"), (59.9, "59 s"), (60, "1 min"), (3599, "59 min"), (3600, "1 h"),
    (86399, "23 h"), (86400, "1 d"), (200000, "2 d"),
])
def test_age_text_floors_with_the_largest_unit(seconds, text):
    assert sv.age_text(seconds) == text


def test_interval_text_shows_both_dates_when_they_differ():
    same = sv.interval_text("2026-10-17T14:41:58.1Z", "2026-10-17T14:42:06.9Z", NOW)
    assert same == "2026-10-17 14:41:58–14:42:06 UTC (17 min ago)"
    cross = sv.interval_text("2026-10-16T23:59:58.0Z", "2026-10-17T00:00:06.0Z", NOW + timedelta(days=1))
    assert cross == "2026-10-16 23:59:58–2026-10-17 00:00:06 UTC (1 d ago)"


# ---------------------------------------------------------------- banned words, tombstones, stderr


def test_the_banned_words_never_appear_in_any_serving_text(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p, outputs={
        "row_count_all": rill_route.RillResult(1, [], [], "x", ""),
        "total_credits_all": rill_route.RillResult(0, ["a"], [["1"]], "", "a\n1\n"),
    })
    run_check(p, capsys)
    texts = status_lines(p, capsys)[1:]
    for name in ("row_count_all", "total_credits_all", "credits_by_day"):
        texts.append(ask_receipt(p, capsys, name)["evidence"]["serving"]["claim"])
    _, out, _ = cli(p, capsys, "ask", "--model", MODEL, "--question", "credits by warehouse")
    texts += [ln for ln in out.splitlines() if ln.startswith(("Serving:", "            "))]
    for text in texts:
        for banned in sv.BANNED_WORDS:
            assert not re.search(rf"\b{re.escape(banned)}\b", text, re.IGNORECASE), (banned, text)
    assert sv.BANNED_WORDS == (
        "is serving", "serves", "currently", "still", "verified by Rill", "Rill verified",
        "live", "in sync", "guaranteed", "trusted",
    )


def test_tombstones_are_exact_in_the_envelope_and_the_record(tmp_path, capsys):
    p = governed_project(tmp_path)
    rec = ask_receipt(p, capsys, "row_count_all")
    assert rec["trust"] == (
        "REMOVED in agent-bi-provenance-record/2.0: read receipt.evidence (definition, serving)"
    ) == TOMBSTONE
    assert rec["trust_reason"] == "REMOVED in agent-bi-provenance-record/2.0: read receipt.evidence"
    assert list(rec["evidence"]) == ["definition", "serving"]
    assert list(rec["evidence"]["definition"]) == [
        "state", "claim", "verified_as", "verified_at", "changed"]
    assert list(rec["evidence"]["serving"]) == [
        "state", "claim", "receipt_id", "interval", "applicability", "query_outcome"]
    from agent_bi.explore import ask as ask_mod

    assert ask_mod.ENVELOPE_SCHEMA == "agent-bi-answer/2.0"
    assert ask_mod.RECORD_SCHEMA == "agent-bi-provenance-record/2.0"


def test_a_receipt_never_contains_raw_rill_stderr(tmp_path, capsys, monkeypatch):
    marker = "ZZ-DISTINCT-RILL-STDERR-8841 /secret/path token=abc"
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p, outputs={
        "row_count_all": rill_route.RillResult(1, [], [], marker, ""),
    })
    rc, out, _ = run_check(p, capsys)
    assert rc == 3
    assert marker in out  # terminal output keeps it, as before
    (path,) = receipts(p)
    assert marker not in path.read_text()
    e = next(x for x in json.loads(path.read_text())["entries"] if x["name"] == "row_count_all")
    assert e["error_code"] == "E_ENGINE" and e["outcome"] == "ERROR" and e["diff"] is None
    assert e["executed_sql_sha256"].startswith("sha256:")
    assert not any(marker in ln for ln in status_lines(p, capsys))


def test_binding_failure_still_writes_a_receipt_and_exits_3(tmp_path, capsys, monkeypatch):
    p = governed_project(tmp_path)
    fake_rill(monkeypatch, p)
    monkeypatch.setattr(rill_route, "fetch_local_config", lambda *a, **k: {
        "project_path": "/elsewhere", "version": "v0.90.5"})
    rc, out, err = run_check(p, capsys)
    assert rc == 3 and err.strip().splitlines()[-1].startswith("E_ENGINE_UNAVAILABLE: ")
    (path,) = receipts(p)
    doc = json.loads(path.read_text())
    assert doc["state"] == "incomplete" and doc["route"]["rill_version"] is None
    assert {e["error_code"] for e in doc["entries"]} == {"E_ENGINE_UNAVAILABLE"}
    assert len(doc["entries"]) == 9
    assert out.splitlines()[-1].startswith("serving receipt: out/serving/") and out.endswith("(incomplete)\n")


def test_verify_without_the_rill_engine_writes_no_receipt(tmp_path, capsys):
    p = governed_project(tmp_path)
    cli(p, capsys, "verify", "--model", MODEL)
    assert not (p / "out" / "serving").exists()


def test_status_exclusion_line_is_verbatim():
    assert sv.EXCLUSIONS == EXCL
