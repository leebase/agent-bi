"""Sprint 2 task 0: re-scan both fixtures into a temp directory and diff the YAML against the
committed ``semantic/*.yaml`` (architecture §4.1.1 changes no fixture output). Prints a
unified diff and exits 1 on any difference; prints nothing and exits 0 when identical.

``verified_queries`` (added in Sprint 2 task 3) is not produced by a scan, so it is removed
from the committed text before comparing: a scan and the committed file must agree on
everything a scan can produce. Run from the repo root.
"""

from __future__ import annotations

import difflib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bi.config import load_config
from agent_bi.engines import DuckDBEngine
from agent_bi.semantic.scan import scan

MODELS = {
    "idle_warehouse_waste": "FIXTURE.MAIN.warehouse_metering_history",
    "product_catalog": "FIXTURE2.MAIN.product_catalog",
}


def strip_verified_queries(text: str) -> str:
    """Cut the top-level ``verified_queries:`` block (and nothing else) out of the text."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if line.startswith("verified_queries:"):
            skipping = True
            continue
        if skipping and line[:1].isalpha():
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def main() -> int:
    config = load_config(ROOT / "agent-bi.yaml")
    differs = False
    with tempfile.TemporaryDirectory() as tmp:
        for model, relation in MODELS.items():
            engine = DuckDBEngine(config)
            try:
                scan(engine, relation, model, Path(tmp))
            finally:
                engine.close()
            fresh = (Path(tmp) / f"{model}.yaml").read_text(encoding="utf-8")
            committed_path = ROOT / "semantic" / f"{model}.yaml"
            committed_text = strip_verified_queries(committed_path.read_text(encoding="utf-8"))
            if committed_text != fresh:
                differs = True
                sys.stdout.writelines(
                    difflib.unified_diff(
                        committed_text.splitlines(keepends=True), fresh.splitlines(keepends=True),
                        f"committed/{model}.yaml", f"rescan/{model}.yaml",
                    )
                )
    return 1 if differs else 0


if __name__ == "__main__":
    sys.exit(main())
