"""Shared memory snapshot — portable export/import of the dependency graph.

The dependency graph plus its PageRank/community analytics is expensive to
recompute but cheap to carry: this module serializes a project's graph to a
compressed ``.ailienant/memory.db.zst`` artifact that can be committed to the
repository and, on a fresh clone, imported *before* the full crawl so the graph
is queryable in seconds instead of after a long re-index.

**Portability.** The catalog keys every row by a path-derived ``project_id`` and
stores absolute file paths, so a verbatim copy would import as an invisible dead
graph on any other clone path. The snapshot is therefore path-relative and
project-agnostic: export relativizes file paths to the workspace root and drops
``project_id``; import re-absolutizes to the *local* workspace root and stamps the
*local* ``project_id``. Import paths are confined to the workspace so a poisoned,
committed artifact can never inject an out-of-tree node.

**Safety.** The export reads all tables inside one ``BEGIN DEFERRED`` transaction
for a consistent, non-tearing WAL snapshot. Both the export container build and
the import parse go through a file-backed temp SQLite (never the optional
``sqlite3_(de)serialize`` C API, which stripped builds omit). Decompression is
bounded mid-stream so a synthetic "zip bomb" can never expand into RAM or onto
disk. Every write is atomic (handle closed before ``os.replace``); every failure
path is fail-open so a bad artifact never blocks a session or a crawl.
"""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import sqlite3
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import aiosqlite
import zstandard as zstd

from core import db as catalog_db

logger = logging.getLogger("MEMORY_SNAPSHOT")

# Artifact location, relative to the workspace root, and its git merge strategy.
SNAPSHOT_RELPATH: str = ".ailienant/memory.db.zst"
_GITATTRIBUTES_LINE: str = f"{SNAPSHOT_RELPATH} merge=ours"

# Bumped only on a breaking change to the portable schema (additive tolerance, so
# a reader can refuse or adapt an unknown version rather than misread it).
SCHEMA_VERSION: int = 1

# Zip-bomb ceiling: the decompressed artifact is refused past this size. Comfortably
# above a real graph (a 5K-node / 15K-edge stress graph serializes to a few MB).
MAX_DECOMPRESSED_BYTES: int = 64 * 1024 * 1024
_DECOMPRESS_CHUNK: int = 1024 * 1024  # 1 MiB streaming window
_SQLITE_MAGIC: bytes = b"SQLite format 3\x00"

# Portable container schema — no project_id, workspace-relative paths.
_PORTABLE_DDL: Tuple[str, ...] = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE graph_edges (source_rel TEXT, target TEXT, target_is_path INTEGER, "
    "confidence TEXT, confidence_score REAL)",
    "CREATE TABLE ppr (file_rel TEXT, ppr_score REAL, leiden_community_id INTEGER)",
)


# ── Path portability ─────────────────────────────────────────────────────────

def _norm(p: str) -> str:
    return p.replace("\\", "/")


def _is_path_target(target: str) -> bool:
    """True when an edge target names a file path (vs an opaque module string).

    A resolved TS/JS target carries a path separator (or a Windows drive); a Python
    dotted module (``brain.state``) never does, so it round-trips verbatim.
    """
    nt = _norm(target)
    return "/" in nt or (len(nt) > 1 and nt[1] == ":")


def _relativize_file(abs_path: str, workspace_root: str) -> str:
    """Return ``abs_path`` relative to the workspace (posix), or unchanged.

    A path already outside the workspace (a rare resolved edge beyond the tree) is
    stored verbatim; import drops it if it does not fall inside the local root.
    """
    np = _norm(abs_path)
    root = _norm(workspace_root).rstrip("/")
    if root and (np == root or np.startswith(root + "/")):
        return np[len(root) + 1:]
    return np


def _absolutize_file(rel: str, workspace_root: str) -> Optional[str]:
    """Re-anchor a stored path to the local workspace, confined to it.

    Returns ``None`` when the result escapes the workspace root — the zero-trust
    guard against a poisoned, committed snapshot smuggling ``../../etc/x`` nodes.
    """
    nr = _norm(rel)
    root = _norm(workspace_root).rstrip("/")
    if not root:
        return None
    if posixpath.isabs(nr) or (len(nr) > 1 and nr[1] == ":"):
        candidate = posixpath.normpath(nr)
    else:
        candidate = posixpath.normpath(posixpath.join(root, nr))
    # Directory-boundary containment (not a naive prefix): `/ws_evil` must not pass `/ws`.
    if candidate == root or (candidate + "/").startswith(root + "/"):
        return candidate
    return None


# ── Export ───────────────────────────────────────────────────────────────────

async def _read_project_graph(
    project_id: str,
) -> Tuple[
    List[Tuple[str, str, Optional[str], Optional[float]]],
    List[Tuple[str, float, Optional[int]]],
]:
    """Read a project's edges + PPR rows inside one deferred read transaction.

    ``isolation_level=None`` puts the connection in autocommit so the explicit
    ``BEGIN DEFERRED`` fully owns the transaction; under WAL this pins one
    consistent snapshot across both SELECTs while concurrent writers proceed
    unblocked. Reads only, so the transaction is rolled back to release it.
    """
    async with aiosqlite.connect(catalog_db.DB_CATALOG_PATH, isolation_level=None) as db:
        await db.execute("BEGIN DEFERRED")
        try:
            async with db.execute(
                "SELECT source_file, target_dependency, confidence, confidence_score "
                "FROM dependency_graph WHERE project_id=?",
                (project_id,),
            ) as cur:
                edges = [(r[0], r[1], r[2], r[3]) for r in await cur.fetchall()]
            async with db.execute(
                "SELECT file_path, ppr_score, leiden_community_id "
                "FROM ppr_scores WHERE project_id=?",
                (project_id,),
            ) as cur:
                ppr = [(r[0], float(r[1]), r[2]) for r in await cur.fetchall()]
        finally:
            await db.execute("ROLLBACK")
    return edges, ppr


def _build_and_compress(
    edges: List[Tuple[str, str, Optional[str], Optional[float]]],
    ppr: List[Tuple[str, float, Optional[int]]],
    workspace_root: str,
) -> bytes:
    """Build the portable SQLite container in a temp file and return its zstd bytes.

    File-backed (never ``.serialize()``, an optional C API) so it works on every
    SQLite build. The temp file is always unlinked, its handle closed first (§5.6).
    """
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(tmp)
        try:
            for stmt in _PORTABLE_DDL:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            edge_rows: List[Tuple[str, str, int, Optional[str], Optional[float]]] = []
            for src, tgt, conf, score in edges:
                src_rel = _relativize_file(src, workspace_root)
                if _is_path_target(tgt):
                    edge_rows.append((src_rel, _relativize_file(tgt, workspace_root), 1, conf, score))
                else:
                    edge_rows.append((src_rel, tgt, 0, conf, score))
            conn.executemany(
                "INSERT INTO graph_edges "
                "(source_rel, target, target_is_path, confidence, confidence_score) "
                "VALUES (?,?,?,?,?)",
                edge_rows,
            )
            conn.executemany(
                "INSERT INTO ppr (file_rel, ppr_score, leiden_community_id) VALUES (?,?,?)",
                [(_relativize_file(fp, workspace_root), score, comm) for fp, score, comm in ppr],
            )
            conn.commit()
        finally:
            conn.close()
        return zstd.ZstdCompressor().compress(Path(tmp).read_bytes())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _atomic_write(directory: Path, name: str, data: bytes) -> Path:
    """Write ``data`` to ``directory/name`` atomically via a same-volume temp + os.replace."""
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / name
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final


def _ensure_gitattributes(workspace_root: str) -> None:
    """Idempotently ensure the ``merge=ours`` rule for the artifact.

    Read → in-memory presence check → atomic rewrite. Never an open-append: a raced
    or partial append can corrupt the file on Windows.
    """
    ga = Path(workspace_root) / ".gitattributes"
    existing = ""
    if ga.exists():
        existing = ga.read_text(encoding="utf-8", errors="replace")
        if _GITATTRIBUTES_LINE in (ln.strip() for ln in existing.splitlines()):
            return
    new_content = existing
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    new_content += _GITATTRIBUTES_LINE + "\n"
    _atomic_write(Path(workspace_root), ".gitattributes", new_content.encode("utf-8"))


async def export_memory_snapshot(project_id: str, workspace_root: str) -> Optional[Path]:
    """Serialize a project's graph + PPR to ``.ailienant/memory.db.zst``.

    Best-effort: any failure logs and returns ``None`` rather than propagating, so a
    manual export command can never crash the caller. Returns the artifact path on
    success.
    """
    if not workspace_root:
        return None
    try:
        edges, ppr = await _read_project_graph(project_id)
        blob = await asyncio.to_thread(_build_and_compress, edges, ppr, workspace_root)
        home = Path(workspace_root) / ".ailienant"
        target = await asyncio.to_thread(_atomic_write, home, "memory.db.zst", blob)
        await asyncio.to_thread(_ensure_gitattributes, workspace_root)
        logger.info(
            "Memory snapshot exported: %d edges, %d PPR rows -> %s",
            len(edges), len(ppr), target,
        )
        return target
    except Exception:  # noqa: BLE001 — export is best-effort; never crash the caller
        logger.warning("Memory snapshot export failed", exc_info=True)
        return None


# ── Import ───────────────────────────────────────────────────────────────────

def _decompress_and_parse(
    artifact_path: str,
) -> Optional[
    Tuple[
        List[Tuple[str, str, int, Optional[str], Optional[float]]],
        List[Tuple[str, float, Optional[int]]],
    ]
]:
    """Bounded-decompress the artifact to a temp file and read its portable rows.

    Returns ``None`` on a zip bomb, a truncated/corrupt stream, or a non-SQLite /
    wrong-schema blob — every one is fail-open. Decompression is capped mid-stream
    so an oversized expansion never fully materializes in RAM or on disk.
    """
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        total = 0
        dctx = zstd.ZstdDecompressor()
        with open(artifact_path, "rb") as cf, open(tmp, "wb") as out:
            with dctx.stream_reader(cf) as reader:
                while True:
                    chunk = reader.read(_DECOMPRESS_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DECOMPRESSED_BYTES:
                        logger.warning(
                            "Memory snapshot exceeds the %d-byte ceiling — refusing "
                            "(possible zip bomb).",
                            MAX_DECOMPRESSED_BYTES,
                        )
                        return None
                    out.write(chunk)
        with open(tmp, "rb") as f:
            if f.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
                logger.warning("Memory snapshot is not a SQLite database — ignoring.")
                return None
        conn = sqlite3.connect(tmp)
        try:
            raw_edges = conn.execute(
                "SELECT source_rel, target, target_is_path, confidence, confidence_score "
                "FROM graph_edges"
            ).fetchall()
            raw_ppr = conn.execute(
                "SELECT file_rel, ppr_score, leiden_community_id FROM ppr"
            ).fetchall()
        finally:
            conn.close()
        edges = [
            (r[0], r[1], int(r[2]) if r[2] is not None else 0, r[3], r[4])
            for r in raw_edges
        ]
        ppr = [(r[0], float(r[1]), r[2]) for r in raw_ppr]
        return edges, ppr
    except (zstd.ZstdError, sqlite3.DatabaseError, OSError) as exc:
        logger.warning("Memory snapshot decode failed: %s", exc)
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def import_memory_snapshot(project_id: str, workspace_root: str) -> bool:
    """Warm-start a project's graph from its committed snapshot, if any.

    No-op (returns ``False``) when the artifact is absent or the project already has
    a graph — a live graph is never clobbered. Paths are re-anchored to the local
    workspace and confined to it; rows that escape are dropped. Fully fail-open: any
    fault returns ``False`` so the normal full crawl still runs. Returns ``True`` on
    a successful warm-start.
    """
    if not workspace_root:
        return False
    artifact = Path(workspace_root) / ".ailienant" / "memory.db.zst"
    if not artifact.exists():
        return False
    try:
        if await catalog_db.get_all_edges(project_id):
            logger.debug("Memory snapshot import skipped: project already has a graph.")
            return False
        parsed = await asyncio.to_thread(_decompress_and_parse, str(artifact))
        if parsed is None:
            return False
        raw_edges, raw_ppr = parsed
        edges: List[Tuple[str, str, Optional[str], Optional[float]]] = []
        for src_rel, tgt, is_path, conf, score in raw_edges:
            src = _absolutize_file(src_rel, workspace_root)
            if src is None:
                continue
            if is_path:
                abs_tgt = _absolutize_file(tgt, workspace_root)
                if abs_tgt is None:
                    continue
                edges.append((src, abs_tgt, conf, score))
            else:
                edges.append((src, tgt, conf, score))
        ppr: List[Tuple[str, float, Optional[int]]] = []
        for file_rel, ppr_score, comm in raw_ppr:
            fp = _absolutize_file(file_rel, workspace_root)
            if fp is None:
                continue
            ppr.append((fp, ppr_score, comm))
        if not edges and not ppr:
            return False
        await catalog_db.bulk_import_graph(edges, ppr, project_id)
        logger.info(
            "Memory snapshot imported: %d edges, %d PPR rows (project=%s)",
            len(edges), len(ppr), project_id,
        )
        return True
    except Exception:  # noqa: BLE001 — a bad artifact must never block a session or crawl
        logger.warning("Memory snapshot import failed (fail-open)", exc_info=True)
        return False
