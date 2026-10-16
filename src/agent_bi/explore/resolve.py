"""``resolve`` (architecture §5.15, FROZEN): the deterministic, closed-grammar question resolver.

Maps the resolvable structure of a natural-language question onto existing semantic objects, or
refuses with the candidates and reasons needed to continue. It never guesses, makes no model or
network call, reads only the semantic model YAML and writes only ``out/resolution.json``.

The grammar is exactly rules 1-11 and the Output section of §5.15 as frozen (Sprint 4P);
nothing here extends or reinterprets it. Rule numbers in comments refer to that section.

Shape of the computation (the frozen text makes this possible):

* ``_layout`` is independent of any pick: rule-4 phrase clusters, grouping phrases (rule 5),
  rule-7 word occurrences and the one-meaning ids of both. A pick never changes which
  occurrences are ambiguous or what their ids are.
* ``_settle`` applies stage-1 picks (word and span ids), then rule 8 with the stage-2 picks
  (``@grain``, ``@date``), and derives status inputs, ``bindings`` and ``grain`` from the
  settled result.
* ``resolve`` validates the picks in the frozen order using three calls of ``_settle``: the
  zero-pick resolution (stage 1), the resolution with this call's stage-1 picks (stage 2), and
  the final one.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_bi.errors import validation_error
from agent_bi.query.contract import SCHEMA_VERSION, query_hash
from agent_bi.semantic.model import load_model_dict
from agent_bi.semantic.scan import paths_for

RESOLUTION_SCHEMA = "agent-bi-resolution/1.0"
RESOLUTION_FILE = "resolution.json"

FUNCTION_WORDS = frozenset((
    "a", "an", "and", "are", "by", "for", "give", "how", "in", "is", "list", "many", "me", "much",
    "of", "per", "show", "the", "to", "what", "which", "with",
))
GRAINS = ("day", "week", "month", "quarter", "year")  # rule 7 / rule 8
_KIND_OF = {"metrics": "metric", "dimensions": "dimension", "time_dimensions": "time_dimension"}
_RUNS = re.compile(r"[a-z0-9]+|\s+|[^a-z0-9\s]+")  # rule 1: maximal runs, left to right

R4, R6, R7, R8 = 4, 6, 7, 8  # rule order for ties in the order of ``unresolved``


# -- rule 1: words ---------------------------------------------------------------------------


def _normalize_word(word: str) -> str:
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def is_unsupported(word: str) -> bool:
    """An unsupported word (rule 1, D421) holds at least one non-ASCII character; every supported
    word is ``[a-z0-9]+``."""
    return not word.isascii()


def words_of(text: str) -> list[str]:
    """Rule 1 (D421): NFKC then lower-case, exactly as ``normalize_question()``; then maximal runs
    left to right: ``[a-z0-9]+`` is a word (one trailing ``s`` removed when longer than 3
    characters and not ending in ``ss``); whitespace separates; any other run is dropped when it is
    all ASCII, otherwise it is an *unsupported word* kept verbatim (never normalized further)."""
    out: list[str] = []
    for run in _RUNS.findall(unicodedata.normalize("NFKC", text).lower()):
        if run[0].isalnum() and run.isascii():
            out.append(_normalize_word(run))
        elif run.isspace() or run.isascii():
            continue
        else:
            out.append(run)
    return out


# -- the eligible objects (rule 3) -----------------------------------------------------------


@dataclass(frozen=True)
class Obj:
    name: str
    kind: str  # metric | dimension | time_dimension
    description: str
    phrases: tuple[tuple[str, ...], ...]  # the name's words first, then each synonym's words
    wordset: frozenset[str]  # every word of the name and of every synonym


def eligible_objects(model: dict[str, Any]) -> tuple[Obj, ...]:
    """Metrics, dimensions and time dimensions of the model's table, in file order. Facts and
    filters are never candidates (rule 3)."""
    table = (model.get("tables") or [{}])[0]
    out: list[Obj] = []
    for section in ("metrics", "dimensions", "time_dimensions"):
        for raw in table.get(section) or []:
            name = raw["name"]
            phrases: list[tuple[str, ...]] = []
            supported: set[str] = set()  # rule 7 matches the supported words of every name and synonym
            for text in [name, *(raw.get("synonyms") or [])]:
                ws = tuple(words_of(str(text)))
                supported.update(w for w in ws if not is_unsupported(w))
                if ws and not any(is_unsupported(w) for w in ws):  # such a phrase never matches
                    phrases.append(ws)
            out.append(Obj(
                name=name, kind=_KIND_OF[section], description=raw.get("description") or "",
                phrases=tuple(phrases), wordset=frozenset(supported),
            ))
    return tuple(out)


def _candidate(value: str, kind: str, description: str) -> dict[str, str]:
    return {"value": value, "kind": kind, "description": description}


def _candidates_of(objs: list[Obj]) -> list[dict[str, str]]:
    return sorted((_candidate(o.name, o.kind, o.description) for o in objs), key=lambda c: c["value"])


# -- the pick-independent layout (rules 4, 5, 7) ---------------------------------------------


@dataclass
class _Span:
    start: int
    end: int
    matches: list[tuple[int, tuple[str, ...]]]
    who: list[Obj]
    reason: str
    in_group: bool
    id: str = ""


@dataclass
class _Occ:  # an ambiguous rule-7 word occurrence
    pos: int
    word: str
    cands: list[Obj]
    reason: str
    in_group: bool
    id: str = ""


@dataclass
class _Layout:
    words: list[str]
    owners: dict[tuple[str, ...], list[Obj]]
    in_group: list[bool]
    hits: list[tuple[int, tuple[str, ...], Obj]]  # unambiguous phrase bindings
    spans: list[_Span]
    # rule-7 outcomes in question order: (pos, kind, payload)
    events: list[tuple[int, str, Any]] = field(default_factory=list)
    occs: list[_Occ] = field(default_factory=list)


def _layout(objs: tuple[Obj, ...], words: list[str]) -> _Layout:
    n = len(words)
    consumed = [False] * n

    # Rule 4: phrases first over the full word sequence, longest length first. At each length
    # every match over unconsumed words is found; overlapping matches (transitively) form a
    # cluster. A one-object cluster binds; any other cluster never binds by file order: its whole
    # span becomes an ambiguity and is consumed.
    owners: dict[tuple[str, ...], list[Obj]] = {}
    for obj in objs:
        for ph in obj.phrases:
            if obj not in owners.setdefault(ph, []):
                owners[ph].append(obj)
    hits: list[tuple[int, tuple[str, ...], Obj]] = []
    spans: list[_Span] = []
    for length in sorted({len(ph) for ph in owners}, reverse=True):
        matches: list[tuple[int, tuple[str, ...]]] = []
        for ph in (t for t in owners if len(t) == length):
            for i in range(n - length + 1):
                if all(not consumed[i + k] and words[i + k] == ph[k] for k in range(length)):
                    matches.append((i, ph))
        matches.sort()
        clusters: list[list[tuple[int, tuple[str, ...]]]] = []
        end = -1
        for m in matches:
            if clusters and m[0] < end:
                clusters[-1].append(m)
                end = max(end, m[0] + length)
            else:
                clusters.append([m])
                end = m[0] + length
        for cl in clusters:
            who: list[Obj] = []
            for _s, ph in cl:
                for o in owners[ph]:
                    if o not in who:
                        who.append(o)
            lo, hi = cl[0][0], max(s0 + length for s0, _ in cl)
            for k in range(lo, hi):
                consumed[k] = True
            if len(who) == 1:
                hits.extend((s0, ph, who[0]) for s0, ph in cl)
            else:
                reason = (
                    "phrase shared by several objects" if len({ph for _, ph in cl}) == 1
                    else "overlapping phrases"
                )
                spans.append(_Span(lo, hi, cl, who, reason, False))
    spans.sort(key=lambda sp: sp.start)
    hits.sort(key=lambda h: (h[0], h[1], h[2].name))

    # Rule 5: grouping phrases are determined after rule 4. A consumed function word (in a bound
    # phrase, an ambiguous span or rejected material) neither opens nor closes a grouping phrase.
    in_group = [False] * n
    open_group = False
    for i, w in enumerate(words):
        if consumed[i]:
            in_group[i] = open_group
        elif w in ("by", "per"):
            open_group = True
        elif w in FUNCTION_WORDS and w != "and":
            open_group = False
        elif w not in FUNCTION_WORDS:
            in_group[i] = open_group
    for sp in spans:
        sp.in_group = in_group[sp.start]

    lay = _Layout(words, owners, in_group, hits, spans)

    # Rule 7: the remaining words, in order.
    for i, w in enumerate(words):
        if consumed[i] or w in FUNCTION_WORDS:  # a lone function word is skipped
            continue
        if is_unsupported(w):  # D421: kept verbatim, never matched, always unresolved
            lay.events.append((i, "unresolved", "no matching name or synonym"))
            continue
        if w == "or":
            lay.events.append((i, "unresolved", "disjunction"))
            continue
        if w in GRAINS:
            if in_group[i]:
                lay.events.append((i, "grain", w))
            else:
                lay.events.append((i, "unresolved", "time grain outside grouping phrase"))
            continue
        pool = [o for o in objs if o.kind != "metric"] if in_group[i] else list(objs)
        ms = [o for o in pool if w in o.wordset]
        if not ms:
            lay.events.append((i, "unresolved", "no matching name or synonym"))
        elif len(ms) == 1:
            lay.events.append((i, "bind", ms[0]))
        else:
            kinds = {o.kind for o in ms}
            reason = (
                "metric and dimension" if "metric" in kinds and kinds != {"metric"}
                else "several objects"
            )
            occ = _Occ(i, w, ms, reason, in_group[i])
            lay.occs.append(occ)
            lay.events.append((i, "amb", occ))

    # Ids: all-or-nothing over every ambiguous occurrence, so an id has exactly one meaning.
    by_word: dict[str, list[_Occ]] = defaultdict(list)
    for occ in lay.occs:
        by_word[occ.word].append(occ)
    for w, occs in by_word.items():
        sigs = {(frozenset(o.name for o in oc.cands), oc.in_group) for oc in occs}
        for oc in occs:
            oc.id = w if len(sigs) == 1 else f"{w}:{oc.pos + 1}"
    by_text: dict[str, list[_Span]] = defaultdict(list)
    for sp in lay.spans:
        by_text[" ".join(words[sp.start:sp.end])].append(sp)
    for text, sps in by_text.items():
        sigs2 = {(frozenset(o.name for o in sp.who), sp.in_group) for sp in sps}
        for sp in sps:
            sp.id = text if len(sigs2) == 1 else f"{text}:{sp.start + 1}"
    return lay


# -- settling (picks applied) ----------------------------------------------------------------


@dataclass
class _Settled:
    ambiguities: list[dict[str, Any]]
    unresolved: list[tuple[int | None, int, str | None, str]]
    metrics: list[Obj]
    dims: list[Obj]
    date: Obj | None
    grain: str | None


def _settle(
    lay: _Layout, objs: tuple[Obj, ...], picks1: dict[str, str], picks2: dict[str, str]
) -> _Settled:
    words = lay.words
    unresolved: list[tuple[int | None, int, str | None, str]] = []  # (pos, rule, word, reason)
    ambs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    bound: dict[str, Obj] = {}  # distinct bound objects (any role) by name
    metric_ok: set[str] = set()  # metrics with at least one binding rule 6 did not refuse
    named_dates: set[str] = set()
    grain_words: list[tuple[int, str]] = []

    def add_amb(aid: str, reason: str, cands: list[dict[str, str]]) -> None:
        if aid not in seen_ids:
            seen_ids.add(aid)
            ambs.append({"id": aid, "reason": reason, "candidates": cands})

    def bind(obj: Obj) -> None:
        bound[obj.name] = obj
        if obj.kind == "time_dimension":
            named_dates.add(obj.name)

    def bind_phrase(start: int, ph: tuple[str, ...], obj: Obj) -> None:
        bind(obj)
        if obj.kind == "metric":
            if lay.in_group[start]:  # rule 6: one entry per bound match
                unresolved.append((start, R6, " ".join(ph), "metric in grouping position"))
            else:
                metric_ok.add(obj.name)

    for start, ph, obj in lay.hits:
        bind_phrase(start, ph, obj)

    # Spans: an unpicked span is an ambiguity; a picked one binds the cluster's matches of the
    # chosen object, and every other word of the span is rejected material.
    for sp in lay.spans:
        picked = picks1.get(sp.id)
        if picked is None or picked not in {o.name for o in sp.who}:
            add_amb(sp.id, sp.reason, _candidates_of(sp.who))
            continue
        obj = next(o for o in sp.who if o.name == picked)
        covered: set[int] = set()
        for s0, ph in sp.matches:
            if obj in lay.owners[ph]:
                bind_phrase(s0, ph, obj)
                covered.update(range(s0, s0 + len(ph)))
        for k in range(sp.start, sp.end):
            if k not in covered:
                unresolved.append((k, R4, words[k], "rejected interpretation material"))

    # Rule 7 outcomes.
    for pos, kind, payload in lay.events:
        if kind == "unresolved":
            unresolved.append((pos, R7, words[pos], payload))
        elif kind == "grain":
            grain_words.append((pos, payload))
        elif kind == "bind":
            bind(payload)
            if payload.kind == "metric":
                metric_ok.add(payload.name)
        else:
            occ: _Occ = payload
            picked = picks1.get(occ.id)
            if picked is not None and picked in {o.name for o in occ.cands}:
                obj = next(o for o in occ.cands if o.name == picked)
                bind(obj)
                if obj.kind == "metric":
                    metric_ok.add(obj.name)
            else:
                add_amb(occ.id, occ.reason, _candidates_of(occ.cands))

    # Rule 8: time (two-stage: stage-2 picks replace the grains / the named dates).
    time_dims = [o for o in objs if o.kind == "time_dimension"]
    by_name = {o.name: o for o in time_dims}
    grains = sorted({g for _, g in grain_words})
    dates = sorted(named_dates)
    grain_set = grains
    if len(grains) > 1:
        if picks2.get("@grain") in grains:
            grain_set = [picks2["@grain"]]
        else:
            add_amb("@grain", "several grains named", [_candidate(g, "grain", "") for g in grains])
    date_set: list[str] = dates
    if len(dates) > 1:
        if picks2.get("@date") in dates:
            date_set = [picks2["@date"]]
        else:
            add_amb("@date", "several dates named", _candidates_of([by_name[d] for d in dates]))
            date_set = []
    elif not dates and grains:
        if len(time_dims) == 1:
            date_set = [time_dims[0].name]
        elif len(time_dims) > 1:
            if picks2.get("@date") in by_name:
                date_set = [picks2["@date"]]
            else:
                add_amb("@date", "several eligible dates, none named", _candidates_of(time_dims))
        else:
            unresolved.append((grain_words[0][0], R8, grain_words[0][1], "model has no date"))
    date: Obj | None = by_name[date_set[0]] if len(date_set) == 1 else None
    if len(grain_set) == 1:
        grain: str | None = grain_set[0]
    elif not grains and date is not None:
        grain = "day"  # one settled date and no grain counted by rule 8
    else:
        grain = None

    metrics = sorted((bound[m] for m in metric_ok), key=lambda o: o.name)
    dims = sorted((o for o in bound.values() if o.kind == "dimension"), key=lambda o: o.name)
    if not ambs and not unresolved and not (metrics or dims):
        unresolved.append((None, R8, None, "no metric or dimension"))
    # order of ``unresolved``: by the position of the first question word; null last; ties by rule
    unresolved.sort(key=lambda u: (u[0] is None, u[0] or 0, u[1]))
    return _Settled(ambs, unresolved, metrics, dims, date, grain)


# -- picks (rule 10) and the document --------------------------------------------------------


def _bad(reason: str, detail: str) -> Exception:
    return validation_error("BAD_CHOICE", f"{reason}: {detail}")


def _as_arguments(choices: Any) -> list[str]:
    if not choices:
        return []
    if isinstance(choices, dict):
        return [f"{k}={v}" for k, v in choices.items()]
    return list(choices)


def resolve(model: dict[str, Any], question: str, choices: Any = None) -> dict[str, Any]:
    """The resolution document (§5.15 *Output*) for ``question`` against ``model``.

    ``choices`` is the list of ``--choose`` arguments (``"<id>=<value>"``, in argument order).
    Any invalid pick raises ``E_BAD_CHOICE`` with the frozen reason and stderr text."""
    args = _as_arguments(choices)
    objs = eligible_objects(model)
    table = (model.get("tables") or [{}])[0]
    words = words_of(question)
    lay = _layout(objs, words)

    # Validation, first failure wins: malformed over all arguments first.
    parsed: list[tuple[str, str]] = []
    for arg in args:
        cid, sep, value = arg.partition("=")
        if not sep or not cid or not value:
            raise _bad("malformed", arg)
        parsed.append((cid, value))
    values_of: dict[str, list[str]] = {}
    for cid, value in parsed:
        vs = values_of.setdefault(cid, [])
        if value not in vs:
            vs.append(value)
    for cid, _v in parsed:  # the conflicting id that appears first in argument order
        if len(values_of[cid]) > 1:
            raise _bad("conflicting values", "; ".join(f"{cid}={v}" for v in values_of[cid]))
    unique: list[tuple[str, str]] = []
    for pair in parsed:
        if pair not in unique:
            unique.append(pair)
    stage1 = [(c, v) for c, v in unique if not c.startswith("@")]
    stage2 = [(c, v) for c, v in unique if c.startswith("@")]

    zero = _settle(lay, objs, {}, {})
    shown = {a["id"]: {c["value"] for c in a["candidates"]} for a in zero.ambiguities}
    for cid, value in stage1:
        if cid not in shown:
            raise _bad("unknown id", f"{cid}={value}")
        if value not in shown[cid]:
            raise _bad("not a shown candidate", f"{cid}={value}")
    picks1 = dict(stage1)
    after1 = _settle(lay, objs, picks1, {})
    shown2 = {a["id"]: {c["value"] for c in a["candidates"]} for a in after1.ambiguities}
    for cid, value in stage2:
        if cid not in shown2:
            raise _bad("unknown id", f"{cid}={value}")
        if value not in shown2[cid]:
            raise _bad("not a shown candidate", f"{cid}={value}")
    final = _settle(lay, objs, picks1, dict(stage2))

    if final.ambiguities:
        status = "AMBIGUOUS"
    elif final.unresolved:
        status = "UNRESOLVED"
    else:
        status = "RESOLVED"

    bindings = sorted(
        [{"object": o.name, "kind": o.kind} for o in (*final.metrics, *final.dims)]
        + ([{"object": final.date.name, "kind": "time_dimension"}] if final.date else []),
        key=lambda b: b["object"],
    )
    semantic_query: dict[str, Any] | None = None
    q_hash: str | None = None
    if status == "RESOLVED":  # rule 11
        semantic_query = {
            "schema_version": SCHEMA_VERSION,
            "model": model.get("name"),
            "table": table.get("name"),
            "metrics": [o.name for o in final.metrics],
            "dimensions": [o.name for o in final.dims],
        }
        if final.date is not None and final.grain is not None:
            semantic_query["time"] = {"dimension": final.date.name, "grain": final.grain}
        q_hash = query_hash(semantic_query)

    return {
        "schema_version": RESOLUTION_SCHEMA,
        "model": model.get("name"),
        "question": question,
        "words": words,
        "status": status,
        "semantic_query": semantic_query,
        "query_hash": q_hash,
        "bindings": bindings,
        "grain": final.grain,
        "ambiguities": final.ambiguities,
        "unresolved": [{"word": w, "reason": r} for _p, _rule, w, r in final.unresolved],
    }


def render(doc: dict[str, Any]) -> str:
    """The bytes of stdout and of ``out/resolution.json``: sorted keys, two-space indent,
    trailing newline. ASCII-escaped so the bytes do not depend on the locale."""
    return json.dumps(doc, sort_keys=True, indent=2) + "\n"


EXIT_BY_STATUS = {"RESOLVED": 0, "AMBIGUOUS": 5, "UNRESOLVED": 6}


def run_resolve(
    model_name: str, question: str, choose: list[str], project_dir: Path
) -> tuple[dict[str, Any], str]:
    """Load the model, resolve, write ``out/resolution.json``; returns ``(doc, text)``.
    Reads only ``semantic/<model>.yaml``. Any failure (D417: ``E_BAD_CHOICE``, a missing or invalid
    model, any other exception) removes an existing ``out/resolution.json`` and re-raises, so no
    stale resolution survives a non-successful invocation."""
    project_dir = Path(project_dir)
    result = project_dir / "out" / RESOLUTION_FILE
    try:
        model_path, _sidecar = paths_for(project_dir / "semantic", model_name)
        model = load_model_dict(str(model_path))
        doc = resolve(model, question, choose)
        text = render(doc)
        result.parent.mkdir(exist_ok=True)
        result.write_bytes(text.encode("utf-8"))
    except BaseException:
        result.unlink(missing_ok=True)
        raise
    return doc, text
