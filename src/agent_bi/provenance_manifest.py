"""Build manifest (architecture §4.4): one file per model, ``rill/provenance/<model>.json``,
overwritten by every ``compile-rill`` of that model (D395); ``build_id`` is a field inside.

Dashboard-level provenance: per generated Rill file its path and sha256, plus the digests of
the semantic model file and sidecar it was generated from, ``models_digest`` (``null`` for a
table model) and ``rill_schema_pin``. ``sidecar_sha256`` is the digest of the sidecar bytes with the top-level ``scanned_at:`` line
removed (D395), so the manifest is an artifact except for ``build_id``.
Rill's own UI shows no per-number provenance; this manifest is the dashboard-level provenance
and the explorer (Sprint 4) gives per-number provenance.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from agent_bi.fs_txn import publish_rank
from agent_bi.hashing import sha256_bytes

SCHEMA_VERSION = "agent-bi-build-manifest/1.0"


def sidecar_digest(sidecar: str | bytes) -> str:
    """sha256 of the sidecar bytes with its top-level ``scanned_at:`` line removed (§4.4)."""
    lines = _bytes(sidecar).splitlines(keepends=True)
    return sha256_bytes(b"".join(ln for ln in lines if not ln.startswith(b"scanned_at:")))


def manifest_path(rill_rel: str, model: str) -> str:
    return f"{rill_rel}/provenance/{model}.json"


def new_build_id(now: datetime | None = None) -> str:
    """UTC ``YYYYMMDDTHHMMSSZ``."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _bytes(data: str | bytes) -> bytes:
    return data.encode("utf-8") if isinstance(data, str) else data


def build_manifest(
    build_id: str,
    model: str,
    files: Mapping[str, str | bytes],
    model_file: str | bytes,
    sidecar_file: str | bytes,
    models_digest: str | None,
    rill_schema_pin: str,
) -> dict[str, Any]:
    """``files`` maps repo-relative path -> content. Keys are in exactly the §4.4 order and
    ``files`` is listed in the §5.14 publish order (``fs_txn.publish_rank``, then path), whatever
    order ``files`` arrives in (pre-Sprint-4, D398)."""
    listed = sorted(files, key=lambda rel: (publish_rank(rel), rel))
    return {
        "schema_version": SCHEMA_VERSION,
        "build_id": build_id,
        "model": model,
        "model_file_sha256": sha256_bytes(_bytes(model_file)),
        "sidecar_sha256": sidecar_digest(sidecar_file),
        "models_digest": models_digest,
        "rill_schema_pin": rill_schema_pin,
        "files": [{"path": p, "sha256": sha256_bytes(_bytes(files[p]))} for p in listed],
    }


def manifest_text(manifest: Mapping[str, Any]) -> str:
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
