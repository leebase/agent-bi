"""Write transactions (architecture §5.14).

Every command that writes more than one file computes all outputs in memory first, stages
them under ``<repo>/.agent-bi-tmp/<txn-id>/<relative path>``, and only then publishes them with
``os.replace`` per file in one fixed, documented order. A failure while computing or staging
leaves the repository untouched. A failure while publishing (disk full, permission) exits 3 with
``E_PARTIAL_PUBLISH`` listing what was already replaced; the temp directory is kept for
recovery and ``validate`` detects the inconsistency (there is no rollback journal in v1).
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from agent_bi.errors import EXIT_ENGINE, AgentBiError

TMP_DIRNAME = ".agent-bi-tmp"

# Publish order (§5.14 step 3, extended to the files ``init`` and ``compile-rill`` write):
# config, semantic YAML, sidecar, queries/, known answers, the Rill inputs a dashboard
# reads (project file, models, metrics views), components, dashboards, manifest last.
_ORDER: tuple[tuple[str, str], ...] = (
    ("exact", "agent-bi.yaml"),
    ("semantic-yaml", "semantic/"),
    ("sidecar", "semantic/"),
    ("prefix", "queries/"),
    ("prefix", "tests/fixtures/known-answers/"),
    ("rill", "rill.yaml"),
    ("rill", "models/"),
    ("rill", "metrics/"),
    ("rill", "components/"),
    ("rill", "dashboards/"),
    ("rill", "provenance/"),
)


def publish_rank(rel: str) -> int:
    """Position of ``rel`` in the fixed publish order; unlisted paths sort last."""
    p = PurePosixPath(rel)
    for i, (kind, key) in enumerate(_ORDER):
        if kind == "exact" and rel == key:
            return i
        if kind == "semantic-yaml" and rel.startswith(key) and rel.endswith(".yaml") \
                and not rel.endswith(".provenance.yaml"):
            return i
        if kind == "sidecar" and rel.startswith(key) and rel.endswith(".provenance.yaml"):
            return i
        if kind == "prefix" and rel.startswith(key):
            return i
        if kind == "rill":
            parts = p.parts
            rest = "/".join(parts[1:]) if len(parts) > 1 else ""
            if len(parts) > 1 and (rest == key or (key.endswith("/") and rest.startswith(key))):
                return i
    return len(_ORDER)


def _stage(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _replace(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _check_rel(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"transaction path must be relative and inside the repo: {rel!r}")
    return p


def publish(
    root: Path, outputs: Mapping[str, str | bytes], txn_id: str | None = None,
    deletes: Sequence[str] = (),
) -> list[str]:
    """Publish ``outputs`` (repo-relative path -> text or bytes) under ``root``; return the
    published paths in publish order. ``deletes`` (repo-relative files this command owns and
    supersedes) are removed after every replace has succeeded."""
    root = Path(root)
    for rel in [*outputs, *deletes]:
        _check_rel(rel)
    ordered = sorted(outputs, key=lambda r: (publish_rank(r), r))
    tmp_root = root / TMP_DIRNAME
    tmp = tmp_root / (txn_id or uuid.uuid4().hex)
    try:
        for rel in ordered:  # step 2: stage; any failure here changes nothing under the repo
            data = outputs[rel]
            _stage(tmp / rel, data.encode("utf-8") if isinstance(data, str) else data)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        _remove_if_empty(tmp_root)
        raise
    done: list[str] = []
    try:
        for rel in ordered:  # step 3: publish by os.replace, fixed order
            _replace(tmp / rel, root / rel)
            done.append(rel)
        for rel in deletes:
            _unlink(root / rel)
    except OSError as exc:
        raise AgentBiError(
            "PARTIAL_PUBLISH",
            f"publish failed after replacing {len(done)} of {len(ordered)} files "
            f"({', '.join(done) or 'none'}): {exc}; staged files kept in {tmp}; "
            "run `agent-bi validate` to see the inconsistency",
            EXIT_ENGINE,
        ) from exc
    shutil.rmtree(tmp, ignore_errors=True)
    _remove_if_empty(tmp_root)
    return done


def _remove_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
