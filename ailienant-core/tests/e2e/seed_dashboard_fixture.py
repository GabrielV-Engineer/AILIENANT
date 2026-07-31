"""Hermetic fixture seeder for the Phase 11.9 Playwright dashboard suite.

Not a pytest module — invoked directly (as a subprocess, before uvicorn starts)
by `ailienant-extension/e2e/run-backend.mjs`. Writes two projects straight into
the catalog SQLite + LanceDB stores via the same helpers the real endpoints use
(``core.db``, ``core.audit``, ``core.memory.semantic_memory``), bypassing the
real indexer so the Playwright suite gets deterministic, instantly-available
data instead of racing a real indexing pass.

Refuses to run unless AILIENANT_CATALOG_DB / AILIENANT_GRAPHRAG_LANCEDB are
already set in the environment — these are the only two overrides that keep
this script off the developer's real ~/.ailienant home (see
core/storage_paths.py::graphrag_lancedb_path_for and shared/config.py's
DB_CATALOG_PATH). The caller (run-backend.mjs) points both at a fresh temp
directory before invoking this script.

Prints one line of JSON to stdout on success — the two seeded project ids/
names/roots — which the caller persists to `.fixture-data/ids.json` for the
Playwright spec to read.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# Runs as a standalone script (not via pytest), so nothing has put ailienant-core
# on sys.path yet — the interpreter only seeds sys.path[0] with this file's own
# directory (tests/e2e/). Insert the package root explicitly before `core.*` and
# `shared.*` become importable, mirroring how `python -m uvicorn main:app` (also
# run with cwd=ailienant-core) resolves the same packages.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"{name} must be set before running this fixture seed — "
            "refusing to write into the real AILIENANT home."
        )
    return value


async def _seed() -> Dict[str, str]:
    _require_env("AILIENANT_CATALOG_DB")
    _require_env("AILIENANT_GRAPHRAG_LANCEDB")

    # Imported after the env-var guard: core.db reads AILIENANT_CATALOG_DB into a
    # module-level constant at import time, so the guard must run first.
    from core import audit
    from core import db as catalog_db
    from core.memory.semantic_memory import SemanticMemoryManager

    await catalog_db.init_db()
    await audit.init_audit_table()

    fixture_root = Path(tempfile.mkdtemp(prefix="ailienant_e2e_fixture_"))
    proj_a_root = fixture_root / "proj-a"
    proj_b_root = fixture_root / "proj-b"
    proj_a_root.mkdir(parents=True, exist_ok=True)
    proj_b_root.mkdir(parents=True, exist_ok=True)

    proj_a_id = "e2e_dashboard_proj_a"
    proj_b_id = "e2e_dashboard_proj_b"

    await catalog_db.upsert_project(proj_a_id, str(proj_a_root))
    await catalog_db.upsert_project(proj_b_id, str(proj_b_root))

    # ── proj-a: 6 indexed files, one god-node dependency edge set, PPR scores ──
    base = _norm(str(proj_a_root / "pkg"))
    hub = f"{base}/hub.py"
    mods = [f"{base}/mod_{i}.py" for i in range(5)]

    for f in [hub, *mods]:
        await catalog_db.upsert_indexed_file(f, project_id=proj_a_id)

    # Every module imports the hub — hub wins the top-3 god-node ranking by
    # degree centrality (in_degree=5) deterministically, by construction.
    for m in mods:
        await catalog_db.upsert_dependencies(m, [hub], project_id=proj_a_id)
    # `_rank_god_nodes` only ranks nodes that are themselves an edge SOURCE
    # (api/memory_dashboard.py's `internal_kept` filter) — a node that is only
    # ever an import TARGET (like hub, above) is excluded from candidacy
    # entirely regardless of its in-degree. Giving hub one outgoing edge makes
    # it a source too, so its in_degree=5 + out_degree=1 total unambiguously
    # beats every mod_i's degree=1.
    await catalog_db.upsert_dependencies(hub, ["os"], project_id=proj_a_id)

    ppr_scores = {hub: 0.5, **{m: 0.1 for m in mods}}
    await catalog_db.upsert_ppr_scores(ppr_scores, project_id=proj_a_id)

    # ── proj-a: ~15 embedding rows, two separable clusters (non-degenerate PCA) ──
    sem = SemanticMemoryManager(lancedb_path=os.environ["AILIENANT_GRAPHRAG_LANCEDB"])
    now_iso = datetime.now(timezone.utc).isoformat()
    all_files = [hub, *mods]
    for i in range(15):
        cluster = i % 2
        base_val = 0.1 if cluster == 0 else 0.9
        vector = [base_val + (0.01 * j) for j in range(8)]
        file_path = all_files[i % len(all_files)] if i < len(all_files) else f"{base}/extra_{i}.py"
        record: Dict[str, Any] = {
            "file_path": file_path,
            "workspace_hash": proj_a_id,
            "content_snippet": f"def f_{i}(): return {i}",
            "token_count": 42 + i,
            "vector": vector,
            "indexed_at": now_iso,
        }
        sem._write_record(record, workspace_hash=proj_a_id, file_path=file_path, hash_valid=True)

    # ── proj-a: 3 audit rows (2 approved, 1 pending) — makes /audit/stats differ
    # from proj-b, which is the observable signal the "re-scope on switch" test
    # asserts against.
    for i, resolution in enumerate(["approved", "approved", "pending"]):
        await audit.log_audit_event(
            session_id=f"e2e-seed-session-{i}",
            action_description="Seeded fixture edit for the 11.9 checkpoint gate.",
            proposed_content="print('fixture')",
            resolution=resolution,
            project_id=proj_a_id,
        )

    # ── proj-b: deliberately sparse — a couple of files, no vectors, no audit rows.
    for i in range(2):
        await catalog_db.upsert_indexed_file(
            _norm(str(proj_b_root / "src" / f"file_{i}.py")), project_id=proj_b_id,
        )

    return {
        "proj_a": proj_a_id,
        "proj_a_name": proj_a_root.name,
        "proj_a_root": _norm(str(proj_a_root)),
        "proj_b": proj_b_id,
        "proj_b_name": proj_b_root.name,
        "proj_b_root": _norm(str(proj_b_root)),
    }


def main() -> None:
    ids = asyncio.run(_seed())
    print(json.dumps(ids))


if __name__ == "__main__":
    main()
