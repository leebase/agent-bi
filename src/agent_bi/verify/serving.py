"""Serving receipts and applicability (architecture §4.8.3–§4.8.4).

Evidence object 2 is a dated receipt of what Rill returned for the ``verified_queries`` of
one model through route ``rill-cli-metrics-sql`` as principal ``rill-local-default``. The
receipt is written by ``verify --engine rill``; whether it says anything about a later
answer is computed here, at display time, from the latest receipt by ``finished_at``. The
module also renders the Definition claim sentences (§4.8.2.2) so ``ask`` and ``status``
compose the two dimensions the same way.

This module is pure over loaded data: it reads receipt JSON files and canonicalises the
current definition state. It never runs Rill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_bi.hashing import (
    definition_digests,
    semantic_digest,
    sha256_canonical,
)
from agent_bi.query.contract import parse_query
from agent_bi.query.verified import find_by_hash
from agent_bi.semantic.provenance import entry_key

SCHEMA_VERSION = "agent-bi-serving-receipt/1.0"
ROUTE_ID = "rill-cli-metrics-sql"
PRINCIPAL_ID = "rill-local-default"
COMPARATOR_ID = "agent-bi-known-answer/1"

MATCH = "MATCH"
MISMATCH = "MISMATCH"
NOT_CHECKED_STALE = "NOT_CHECKED_STALE"
NOT_CHECKED_UNSUPPORTED = "NOT_CHECKED_UNSUPPORTED"
ERROR = "ERROR"
NOT_CHECKED = (NOT_CHECKED_STALE, NOT_CHECKED_UNSUPPORTED)

# §4.8.4 rule 3: always printed under Serving, verbatim.
EXCLUSIONS = (
    "Not covered: other read paths and caches, other users, rendering, "
    "any time outside the check."
)

# §4.8.4 rule 4: the Serving line never contains these words.
BANNED_WORDS = (
    "is serving", "serves", "currently", "still", "verified by Rill", "Rill verified",
    "live", "in sync", "guaranteed", "trusted",
)

TOMBSTONE_TRUST = (
    "REMOVED in agent-bi-provenance-record/2.0: read receipt.evidence (definition, serving)"
)
TOMBSTONE_TRUST_REASON = "REMOVED in agent-bi-provenance-record/2.0: read receipt.evidence"

VALID_OUTCOMES = (MATCH, MISMATCH, NOT_CHECKED_STALE, NOT_CHECKED_UNSUPPORTED, ERROR)
VALID_RUN_STATES = ("failed", "incomplete", "matched", "no-evidence")
VALID_APPLICABILITY = (
    "applies", "different-definition-or-answer", "not-in-check", "definition-stale",
    "not-verified",
)

_KIND_PLURAL = {
    "metric": "metrics", "dimension": "dimensions", "time_dimension": "time_dimensions",
    "filter": "filters",
}


def _iso(epoch: Any) -> str:
    """Today's ``_iso``: an int epoch renders ``YYYY-MM-DDTHH:MM:SSZ``; anything else is
    "an earlier verification" (§4.8.2.2)."""
    if isinstance(epoch, int):
        return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "an earlier verification"


def _iso_or_none(epoch: Any) -> str | None:
    if isinstance(epoch, int):
        return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


# -- receipt construction ----------------------------------------------------------------


def make_entry(
    *,
    name: str,
    query_hash: str | None,
    semantic_digest: str | None,
    known_answer_content_sha256: str | None,
    intrinsic_scope: dict[str, Any],
    outcome: str,
    executed_sql_sha256: str | None = None,
    error_code: str | None = None,
    diff: list[str] | None = None,
) -> dict[str, Any]:
    """One receipt entry, in the §4.8.3.6 key order."""
    return {
        "name": name,
        "query_hash": query_hash,
        "semantic_digest": semantic_digest,
        "known_answer_content_sha256": known_answer_content_sha256,
        "intrinsic_scope": intrinsic_scope,
        "outcome": outcome,
        "executed_sql_sha256": executed_sql_sha256,
        "error_code": error_code,
        "diff": diff,
    }


def intrinsic_scope(query: Any) -> dict[str, Any]:
    """The query's own filters, time-range bounds and limit, as plain JSON values."""
    q = parse_query(query)
    return {
        "filters": [f.model_dump() for f in q.filters],
        "time_start": q.time.start if q.time is not None else None,
        "time_end": q.time.end if q.time is not None else None,
        "limit": q.limit,
    }


def known_answer_content_sha256(expected: Any) -> str | None:
    """sha256 of the canonical ``{"columns", "rows"}`` of the known answer's expected result
    (§4.8.3.6); ``None`` when there is no usable expected block."""
    if not isinstance(expected, dict) or "columns" not in expected or "rows" not in expected:
        return None
    return sha256_canonical({"columns": expected["columns"], "rows": expected["rows"]})


def receipt_state(entries: list[dict[str, Any]]) -> str:
    """First match wins (§4.8.3.5): any MISMATCH -> failed; else any ERROR -> incomplete;
    else M >= 1 -> matched; else no-evidence."""
    outcomes = [e.get("outcome") for e in entries]
    if MISMATCH in outcomes:
        return "failed"
    if ERROR in outcomes:
        return "incomplete"
    if outcomes.count(MATCH) >= 1:
        return "matched"
    return "no-evidence"


def receipt_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    outcomes = [e.get("outcome") for e in entries]
    return {
        "verified_queries": len(entries),
        "matched": outcomes.count(MATCH),
        "mismatched": outcomes.count(MISMATCH),
        "not_checked": sum(1 for o in outcomes if o in NOT_CHECKED),
        "errors": outcomes.count(ERROR),
    }


def build_receipt(
    *,
    model: str,
    rill_binary: str | None,
    rill_version: str | None,
    started_at: str,
    finished_at: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """The receipt object: its fields in §4.8 order, with ``receipt_id`` = sha256 of the
    canonical JSON of every other field."""
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "route": {"id": ROUTE_ID, "rill_binary": rill_binary, "rill_version": rill_version},
        "principal": {"id": PRINCIPAL_ID},
        "comparator": {"id": COMPARATOR_ID},
        "scope": {"canvas_filters": "none-added", "canvas_time_range": "none-added"},
        "started_at": started_at,
        "finished_at": finished_at,
        "entries": entries,
        "counts": receipt_counts(entries),
        "state": receipt_state(entries),
    }
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "receipt_id": sha256_canonical(body),
    }
    receipt.update({k: v for k, v in body.items() if k != "schema_version"})
    return receipt


def receipt_filename(receipt: dict[str, Any]) -> str:
    """``<finished_at_compact>-<receipt_id_hex>.json``."""
    finished = datetime.fromisoformat(str(receipt["finished_at"]))
    compact = finished.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    hexid = str(receipt["receipt_id"]).removeprefix("sha256:")
    return f"{compact}-{hexid}.json"


def write_receipt(project_dir: Path, model: str, receipt: dict[str, Any]) -> Path:
    """Append-only write under ``out/serving/<model>/``. Never overwrites or deletes."""
    directory = Path(project_dir) / "out" / "serving" / model
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / receipt_filename(receipt)
    if not path.exists():
        path.write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return path


# -- receipt selection -------------------------------------------------------------------


def _receipt_is_usable(doc: Any, model: str) -> bool:
    if not isinstance(doc, dict):
        return False
    if doc.get("model") != model:
        return False
    if not isinstance(doc.get("finished_at"), str):
        return False
    if not isinstance(doc.get("entries"), list):
        return False
    route = doc.get("route")
    principal = doc.get("principal")
    return (
        isinstance(route, dict) and route.get("id") == ROUTE_ID
        and isinstance(principal, dict) and principal.get("id") == PRINCIPAL_ID
    )


def read_receipts(project_dir: Path, model: str) -> list[dict[str, Any]]:
    """Every readable receipt for ``model`` under ``out/serving/<model>/``. An unreadable or
    unrecognisable file is ignored, never fatal."""
    directory = Path(project_dir) / "out" / "serving" / model
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _receipt_is_usable(doc, model):
            out.append(doc)
    return out


def latest_receipt(project_dir: Path, model: str) -> dict[str, Any] | None:
    """The shown receipt: greatest ``finished_at`` field value for this model, route and
    principal — never by filename or mtime (§4.8.3.6)."""
    candidates = read_receipts(project_dir, model)
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r["finished_at"], str(r.get("receipt_id") or "")))


# -- interval and age --------------------------------------------------------------------


def _as_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def age_text(seconds: float) -> str:
    """``<n> s``, ``<n> min``, ``<n> h`` or ``<n> d``: floor, largest unit at least 1."""
    seconds = max(0.0, seconds)
    if seconds >= 86400:
        return f"{int(seconds // 86400)} d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)} h"
    if seconds >= 60:
        return f"{int(seconds // 60)} min"
    return f"{int(seconds)} s"


def interval_text(started_at: str, finished_at: str, now: datetime | None = None) -> str:
    """``YYYY-MM-DD HH:MM:SS–HH:MM:SS UTC (<age> ago)``; the full date appears on both
    sides when the dates differ. Age is measured from ``finished_at``."""
    now = now or datetime.now(UTC)
    start, end = _as_utc(started_at), _as_utc(finished_at)
    start_txt = start.strftime("%Y-%m-%d %H:%M:%S")
    end_txt = (
        end.strftime("%H:%M:%S") if start.date() == end.date()
        else end.strftime("%Y-%m-%d %H:%M:%S")
    )
    age = (now - end).total_seconds()
    return f"{start_txt}–{end_txt} UTC ({age_text(age)} ago)"


# -- state claim sentences ---------------------------------------------------------------


def _skip_reason(entry: dict[str, Any]) -> str:
    return {
        NOT_CHECKED_STALE: "stale definition",
        NOT_CHECKED_UNSUPPORTED: "not supported by the route",
    }.get(entry.get("outcome"), "skipped")


def state_claim(receipt: dict[str, Any], interval: str) -> str:
    """The §4.8.3.5 sentence for the receipt's run state."""
    state = receipt.get("state")
    entries = [e for e in (receipt.get("entries") or []) if isinstance(e, dict)]
    counts = receipt_counts(entries)
    k, m = counts["verified_queries"], counts["matched"]
    if state == "failed":
        return (
            f"During {interval}, through Rill Metrics SQL (CLI), as the local default user, "
            f"{counts['mismatched']} of {k} verified queries returned results that differ from "
            "their known answers under agent-bi-known-answer/1."
        )
    if state == "incomplete":
        codes = ", ".join(
            dict.fromkeys(e.get("error_code") for e in entries if e.get("outcome") == ERROR)
        )
        return (
            f"During {interval}, the check through Rill Metrics SQL (CLI) did not complete: "
            f"{counts['errors']} queries could not be asked ({codes}). "
            f"{m} queries matched."
        )
    if state == "matched":
        sentence = (
            f"During {interval}, through Rill Metrics SQL (CLI), as the local default user, "
            f"with no added canvas filters or time range, {m} of {k} verified queries returned "
            "their known answers under agent-bi-known-answer/1."
        )
        skipped = [e for e in entries if e.get("outcome") in NOT_CHECKED]
        if skipped:
            items = ", ".join(f"{e.get('name')}: {_skip_reason(e)}" for e in skipped)
            sentence += f" {k - m} were skipped, not compared ({items})."
        return sentence
    return (
        f"During {interval}, no verified query was compared through Rill Metrics SQL (CLI); "
        "this check is not serving evidence."
    )


# -- Definition state and claim sentences ------------------------------------------------


@dataclass
class DefinitionInfo:
    state: str  # governed | verified | stale | none
    name: str | None
    verified_at: str | None  # ISO or None (raw rendering for the claim goes via _iso)
    verified_at_raw: Any = None
    changed: list[str] = field(default_factory=list)
    claim: str = ""

    def evidence(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "claim": self.claim,
            "verified_as": self.name if self.state in ("governed", "verified", "stale") else None,
            "verified_at": self.verified_at,
            "changed": list(self.changed) if self.state == "stale" else [],
        }


def _changed_tokens(
    stored: dict[str, str] | None, current: dict[str, str], removed: list[str]
) -> list[str]:
    """The §4.8.2.2 change explanation, one token per changed entry: ``base_table`` first,
    names in canonical order, a changed ``models_digest`` as "upstream Rill model changed",
    a removed object as "<name> (removed)"."""
    stored = stored or {}
    names = ["base_table"] + sorted(k for k in stored if k != "base_table")
    tokens: list[str] = []
    for n in names:
        if n not in stored:
            continue
        if n in removed:
            tokens.append(f"{n} (removed)")
        elif current.get(n) != stored[n]:
            tokens.append("upstream Rill model changed" if n == "models_digest" else n)
    if not tokens:
        tokens.append("definitions changed (no per-definition record to name them)")
    return tokens


def _verified_at_source(model: dict[str, Any], name: str) -> Any:
    for vq in model.get("verified_queries") or []:
        if vq.get("name") == name:
            return vq.get("verified_at")
    return None


def classify_definition(
    q_hash: str,
    digest: str | None,
    sidecar: dict[str, Any],
    model: dict[str, Any],
    current_defs: dict[str, str],
    removed: list[str],
    statuses: dict[str, str | None],
) -> DefinitionInfo:
    """The §4.7 state table, first match wins, with the §4.8.2.2 claim sentence."""
    records = sidecar.get("verified_queries") or {}
    names = find_by_hash(records, q_hash)
    current = [n for n in names if (records[n] or {}).get("semantic_digest") == digest]
    if current:
        name = current[0]
        unconfirmed = sorted(n for n, st in statuses.items() if st != "confirmed")
        epoch = _verified_at_source(model, name)
        at = _iso(epoch)
        if not unconfirmed:
            claim = (
                "A person accepted this query's definitions, and an independent computation "
                f"agreed with them when verified as {name} at {at}; the definitions covered by "
                "the semantic digest match those recorded with that verification."
            )
            return DefinitionInfo("governed", name, _iso_or_none(epoch), epoch, [], claim)
        claim = (
            "An independent computation agreed with this query's definitions when verified as "
            f"{name} at {at}; the definitions covered by the semantic digest match those "
            f"recorded with that verification; {', '.join(unconfirmed)} have not been accepted "
            "by a person."
        )
        return DefinitionInfo("verified", name, _iso_or_none(epoch), epoch, [], claim)
    if names:
        name = names[0]
        rec = records[name] or {}
        tokens = _changed_tokens(rec.get("definition_digests"), current_defs, removed)
        epoch = _verified_at_source(model, name)
        at = _iso(epoch)
        claim = (
            f"This query was verified as {name} at {at}, but its definitions now differ from "
            f"the verified version ({', '.join(tokens)}); no verification of the current "
            "definitions exists."
        )
        return DefinitionInfo("stale", name, _iso_or_none(epoch), epoch, tokens, claim)
    return DefinitionInfo("none", None, None, None, [], "No verified query matches this question.")


def definition_statuses(
    query: Any, model: dict[str, Any], sidecar: dict[str, Any]
) -> dict[str, str | None]:
    """The sidecar status of every definition a query references."""
    from agent_bi.hashing import referenced_definitions

    entries = sidecar.get("entries") or {}
    out: dict[str, str | None] = {}
    for name, d in referenced_definitions(query, model).items():
        entry = entries.get(entry_key(_KIND_PLURAL[d["kind"]], name)) or {}
        out[name] = entry.get("status")
    return out


def model_definition_line(
    model: dict[str, Any], sidecar: dict[str, Any], models_digest: str | None
) -> str:
    """The model-level Definition line (§4.8.2.2): counts, never a whole-model status."""
    verified = model.get("verified_queries") or []
    if not verified:
        return "No verified queries."
    records = sidecar.get("verified_queries") or {}
    counts = {"governed": 0, "verified": 0, "stale": 0}
    stale: list[str] = []
    for vq in verified:
        name = vq.get("name")
        rec = records.get(name) or {}
        q = parse_query(rec.get("semantic_query"), f"verified_queries[{name}].semantic_query")
        removed: list[str] = []
        current_defs = definition_digests(q, model, removed, models_digest)
        digest = None if removed else semantic_digest(q, model, models_digest)
        statuses = definition_statuses(q, model, sidecar)
        info = classify_definition(
            rec.get("query_hash"), digest, sidecar, model, current_defs, removed, statuses
        )
        state = info.state if info.state in counts else "stale"
        counts[state] += 1
        if state == "stale":
            stale.append(f"({name}: {', '.join(info.changed)})")
    line = (
        f"{len(verified)} verified queries: {counts['governed']} governed, "
        f"{counts['verified']} verified, {counts['stale']} stale"
    )
    if stale:
        line += " " + " ".join(stale)
    return line


# -- applicability (§4.8.3.7) ------------------------------------------------------------


@dataclass
class ServingInfo:
    state: str | None
    claim: str
    receipt_id: str | None
    interval: list[str] | None
    applicability: str | None
    query_outcome: str | None
    diff: list[str] | None = None
    context: str | None = None  # display-only line; not part of the evidence object

    def evidence(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "claim": self.claim,
            "receipt_id": self.receipt_id,
            "interval": list(self.interval) if self.interval else None,
            "applicability": self.applicability,
            "query_outcome": self.query_outcome,
        }


def _entry_diff(entry: dict[str, Any]) -> list[str] | None:
    if entry.get("outcome") != MISMATCH:
        return None
    diff = entry.get("diff")
    return list(diff) if isinstance(diff, list) else None


def _verb(entry: dict[str, Any]) -> str:
    outcome = entry.get("outcome")
    if outcome in (MATCH, MISMATCH):
        return "compared"
    if outcome in NOT_CHECKED:
        return "skipped"
    return "attempted"


def _outcome_text(interval: str, entry: dict[str, Any]) -> str:
    prefix = f"In the serving check during {interval}, this question"
    outcome = entry.get("outcome")
    if outcome == MATCH:
        return f"{prefix} returned its known answer under agent-bi-known-answer/1."
    if outcome == MISMATCH:
        return f"{prefix} returned a result different from its known answer."
    if outcome == NOT_CHECKED_UNSUPPORTED:
        return f"{prefix} was skipped (not supported by the route); it was not compared."
    if outcome == NOT_CHECKED_STALE:
        return f"{prefix} was skipped (stale definition); it was not compared."
    code = entry.get("error_code")
    return f"{prefix} was attempted but could not be asked ({code}); it was not compared."


def context_line(claim: str, interval: str, state: str | None) -> str | None:
    """One display line naming the receipt's interval, run state and comparator when the
    shown claim sentence lacks either the interval or the comparator (§4.8.3.3, §4.8.4
    rule 3). It describes the recorded check only, not the answer."""
    if COMPARATOR_ID in claim and interval in claim:
        return None
    return f"Latest serving check recorded: {interval} ({state}); comparison rule {COMPARATOR_ID}."


def answer_serving(
    project_dir: Path,
    model: str,
    q_hash: str,
    definition_state: str,
    current_digest: str | None,
    current_known_answer_digest: str | None,
    now: datetime | None = None,
) -> ServingInfo:
    """``_answer_serving`` plus the display-only context line when a receipt exists."""
    now = now or datetime.now(UTC)
    info = _answer_serving(
        project_dir, model, q_hash, definition_state, current_digest,
        current_known_answer_digest, now,
    )
    if info.interval:
        info.context = context_line(
            info.claim, interval_text(info.interval[0], info.interval[1], now), info.state
        )
    return info


def _answer_serving(
    project_dir: Path,
    model: str,
    q_hash: str,
    definition_state: str,
    current_digest: str | None,
    current_known_answer_digest: str | None,
    now: datetime | None = None,
) -> ServingInfo:
    """The six ordered applicability steps (§4.8.3.7) for one answer."""
    receipt = latest_receipt(project_dir, model)
    if receipt is None:
        return ServingInfo(None, "No serving check recorded.", None, None, None, None)
    interval = interval_text(receipt["started_at"], receipt["finished_at"], now)
    state = receipt.get("state") if receipt.get("state") in VALID_RUN_STATES else receipt_state(
        [e for e in receipt.get("entries") or [] if isinstance(e, dict)]
    )
    evidence_base = {
        "state": state,
        "receipt_id": receipt.get("receipt_id"),
        "interval": [receipt["started_at"], receipt["finished_at"]],
    }
    if definition_state == "none":
        return ServingInfo(
            **evidence_base,
            claim="This question is not a verified query; serving checks do not cover it.",
            applicability="not-verified",
            query_outcome=None,
        )
    if definition_state == "stale":
        return ServingInfo(
            **evidence_base,
            claim=(
                "This question's definition differs from its verified version; no serving "
                "check applies to the current definition."
            ),
            applicability="definition-stale",
            query_outcome=None,
        )
    entries = [e for e in receipt.get("entries") or [] if isinstance(e, dict)]
    entry = next((e for e in entries if e.get("query_hash") == q_hash), None)
    if entry is None:
        return ServingInfo(
            **evidence_base,
            claim=f"This question was not part of the serving check during {interval}.",
            applicability="not-in-check",
            query_outcome=None,
        )
    if (
        entry.get("semantic_digest") != current_digest
        or entry.get("known_answer_content_sha256") != current_known_answer_digest
    ):
        claim = (
            f"The serving check during {interval} {_verb(entry)} this question under a "
            "different definition or known answer than the current one; it is not serving "
            "evidence for the current definition."
        )
        return ServingInfo(
            **evidence_base, claim=claim,
            applicability="different-definition-or-answer", query_outcome=None,
        )
    return ServingInfo(
        **evidence_base,
        claim=_outcome_text(interval, entry),
        applicability="applies",
        query_outcome=str(entry.get("outcome")),
        diff=_entry_diff(entry),
    )


# -- model-level serving -----------------------------------------------------------------


@dataclass
class _CurrentQuery:
    name: str
    rec: dict[str, Any]
    q: Any
    q_hash: str
    digest: str | None
    definition: DefinitionInfo
    known_answer_digest: str | None


def load_known_answer(project_dir: Path, model: str, name: str, rec: dict[str, Any]) -> dict[str, Any] | None:
    rel = rec.get("known_answer") or f"tests/fixtures/known-answers/{model}/{name}.json"
    try:
        doc = json.loads((Path(project_dir) / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _current_verified(
    project_dir: Path, model: dict[str, Any], sidecar: dict[str, Any], models_digest: str | None
) -> list[_CurrentQuery]:
    out: list[_CurrentQuery] = []
    records = sidecar.get("verified_queries") or {}
    for vq in model.get("verified_queries") or []:
        name = vq.get("name")
        rec = records.get(name) or {}
        q = parse_query(rec.get("semantic_query"), f"verified_queries[{name}].semantic_query")
        removed: list[str] = []
        current_defs = definition_digests(q, model, removed, models_digest)
        digest = None if removed else semantic_digest(q, model, models_digest)
        statuses = definition_statuses(q, model, sidecar)
        info = classify_definition(
            rec.get("query_hash"), digest, sidecar, model, current_defs, removed, statuses
        )
        ka = load_known_answer(project_dir, str(model.get("name")), name, rec)
        ka_digest = known_answer_content_sha256(ka.get("expected")) if ka else None
        out.append(
            _CurrentQuery(
                name=name, rec=rec, q=q, q_hash=str(rec.get("query_hash")),
                digest=digest, definition=info, known_answer_digest=ka_digest,
            )
        )
    return out


def model_serving_lines(
    project_dir: Path,
    model_name: str,
    model: dict[str, Any],
    sidecar: dict[str, Any],
    models_digest: str | None,
    now: datetime | None = None,
) -> list[str]:
    """The model-level Serving line(s) (§4.8.3.7 "Model level") including the always-present
    exclusions line. With no receipt: only "No serving check recorded." and the exclusions."""
    receipt = latest_receipt(project_dir, model_name)
    if receipt is None:
        return ["No serving check recorded.", EXCLUSIONS]
    now = now or datetime.now(UTC)
    interval = interval_text(receipt["started_at"], receipt["finished_at"], now)
    claim = state_claim(receipt, interval)
    lines = [claim]
    context = context_line(claim, interval, receipt.get("state"))
    if context is not None:
        lines.append(context)
    for entry in receipt.get("entries") or []:
        if isinstance(entry, dict) and entry.get("outcome") == MISMATCH:
            for d in entry.get("diff") or []:
                lines.append(f"{entry.get('name')}: {d}")
    labels = {
        "definition-stale": "stale",
        "not-in-check": "not in the check",
        "different-definition-or-answer": "different definition or known answer",
    }
    for current in _current_verified(project_dir, model, sidecar, models_digest):
        info = answer_serving(
            project_dir, model_name, current.q_hash, current.definition.state, current.digest,
            current.known_answer_digest, now,
        )
        label = labels.get(info.applicability or "")
        if label is not None:
            lines.append(f"Not covered by that check: {current.name} ({label})")
    lines.append(EXCLUSIONS)
    return lines


def model_report(
    project_dir: Path,
    model_name: str,
    model: dict[str, Any],
    sidecar: dict[str, Any],
    models_digest: str | None,
    now: datetime | None = None,
) -> tuple[str, list[str]]:
    """``(definition line, serving lines)`` for ``status --model`` and other model reports."""
    return (
        model_definition_line(model, sidecar, models_digest),
        model_serving_lines(project_dir, model_name, model, sidecar, models_digest, now),
    )
