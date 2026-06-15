"""Per-project storage-path resolution for the GraphRAG semantic store.

Global runtime stores (catalog SQLite, MCTS audit DB, gateway ledger, the global
LanceDB tables) live at fixed locations under the application home and are
resolved directly in ``shared.config``. They are global on purpose: their
per-project rows are isolated by a ``project_id`` / ``workspace_hash`` column,
and tables such as skills, MCP servers and hooks are shared across projects.

The one store that benefits from physical per-project partitioning is the
GraphRAG semantic index (``workspace_embeddings``): it is the bulkiest, most
project-specific surface, and giving each project its own directory keeps an
index small and makes deleting a project's memory a single ``rmtree``.

This module owns that single late-bound path. The active project is unknown at
import time — it arrives with the editor's ``client_workspace_init`` event — so
the consumer binds it once per session via :func:`bind_project` and reads the
resolved directory through :func:`graphrag_lancedb_path`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

from shared.config import AILIENANT_HOME

logger = logging.getLogger("STORAGE_PATHS")

# Process-level binding of the active project. A backend process serves one
# editor window (one workspace) for its lifetime, so a single slot suffices.
# Guarded by a lock because the bind happens on the WS receive loop while
# semantic-memory consumers read it from fan-out worker threads.
_lock = threading.Lock()
_bound_project_id: str | None = None

# Sentinel directory used before any project is bound (unit tests, early probes).
# Keeps writes inside the application home — never silently in the CWD.
_UNBOUND_ID = "_unbound"


def project_id_for(workspace_root: str) -> str:
    """Derive the per-workspace project id the on-disk stores are keyed by.

    The SHA-256 hex digest of the raw workspace root path. This mirrors the
    editor's identity exactly (``PathResolver.resolveProjectId``); the caller
    must pass the same absolute path the editor uses or the digest will not
    match the indexed data.
    """
    return hashlib.sha256(workspace_root.encode("utf-8")).hexdigest()


def _projects_root() -> Path:
    return AILIENANT_HOME / "projects"


def bind_project(workspace_root: str) -> str:
    """Bind the active project for this process and ensure its store directory.

    Idempotent: re-binding the same workspace is a no-op beyond the directory
    check. Returns the resolved project id.
    """
    global _bound_project_id
    project_id = project_id_for(workspace_root)
    target = _projects_root() / project_id / "lancedb"
    target.mkdir(parents=True, exist_ok=True)
    with _lock:
        _bound_project_id = project_id
    logger.info("Project storage bound: id=%s root=%s", project_id, workspace_root)
    return project_id


def graphrag_lancedb_path_for(project_id: str) -> str:
    """Resolve the GraphRAG LanceDB directory for an explicit project id.

    Used by consumers that operate on a project other than (or independent of)
    the process binding — the out-of-process gateway and the memory dashboard,
    which both receive a ``project_id`` directly. The ``AILIENANT_GRAPHRAG_LANCEDB``
    override still wins so a test can pin every consumer to one directory.
    """
    override = os.getenv("AILIENANT_GRAPHRAG_LANCEDB")
    if override:
        Path(override).mkdir(parents=True, exist_ok=True)
        return override
    path = _projects_root() / (project_id or _UNBOUND_ID) / "lancedb"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def graphrag_lancedb_path() -> str:
    """Resolve the per-project GraphRAG LanceDB directory for the bound project.

    Precedence: explicit ``AILIENANT_GRAPHRAG_LANCEDB`` override (tests) → the
    bound project's directory → an ``_unbound`` fallback under the home. The
    fallback never raises and never escapes the application home.
    """
    with _lock:
        project_id = _bound_project_id or _UNBOUND_ID
    return graphrag_lancedb_path_for(project_id)


# ── One-time migration of legacy CWD stores ──────────────────────────────────

# Filenames a previous version wrote relative to the working directory. They are
# relocated into the application home so a launch from any directory keeps using
# the same data. SQLite companions (-wal / -shm) travel with the main file.
_LEGACY_SQLITE: tuple[str, ...] = ("ailienant_catalog.sqlite", "ailienant_mcts.sqlite")
_LEGACY_HOME_NAMES: dict[str, str] = {
    "ailienant_catalog.sqlite": "catalog.sqlite",
    "ailienant_mcts.sqlite": "mcts.sqlite",
}
_LEGACY_LANCEDB_DIR = "ailienant_lancedb"


def _migrate_legacy_cwd_stores() -> None:
    """Best-effort relocation of CWD-era stores into the application home.

    Only moves a file when its home target does not already exist, so a fresh
    install that already wrote to the home is never clobbered. Fully non-fatal:
    any failure is logged and startup continues (the stores are either rebuilt
    or remain readable at their default home path).
    """
    cwd = Path.cwd()
    for legacy_name in _LEGACY_SQLITE:
        home_name = _LEGACY_HOME_NAMES[legacy_name]
        for suffix in ("", "-wal", "-shm"):
            src = cwd / f"{legacy_name}{suffix}"
            dst = AILIENANT_HOME / f"{home_name}{suffix}"
            if src.exists() and not dst.exists():
                try:
                    os.replace(src, dst)
                    logger.info("Migrated legacy store %s -> %s", src, dst)
                except OSError as exc:  # noqa: BLE001 - migration must never crash boot
                    logger.warning("Legacy store migration skipped for %s: %s", src, exc)

    legacy_lancedb = cwd / _LEGACY_LANCEDB_DIR
    home_lancedb = AILIENANT_HOME / "lancedb"
    if legacy_lancedb.is_dir() and not home_lancedb.exists():
        try:
            os.replace(legacy_lancedb, home_lancedb)
            logger.info("Migrated legacy vector store %s -> %s", legacy_lancedb, home_lancedb)
        except OSError as exc:  # noqa: BLE001 - migration must never crash boot
            logger.warning("Legacy vector-store migration skipped: %s", exc)


def ensure_home() -> None:
    """Ensure the application home exists and run the one-time CWD migration.

    Safe to call repeatedly; the home directory is created by ``shared.config``
    at import and the migration only acts when a legacy file is present and its
    home target is absent.
    """
    AILIENANT_HOME.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_cwd_stores()


# Run the migration at import: this module is imported early (the gateway and
# janitor both depend on it), well before any store connection is opened.
ensure_home()


# ── AILIENANT-internal runtime artifacts ─────────────────────────────────────

# The workspace-root home directory and the tail-able telemetry sink. Kept in
# sync with core.telemetry_log._LOG_FILENAME (hardcoded here to avoid importing
# the logging machinery into this early-loaded leaf module).
_INTERNAL_HOME_DIR = ".ailienant"
_TELEMETRY_LOG_BASENAME = ".ailienant_telemetry.log"


def is_ailienant_internal_path(path: str) -> bool:
    """True when ``path`` is one of AILIENANT's own runtime artifacts.

    These — the ``.ailienant/`` workspace home and the continuously-rewritten
    ``.ailienant_telemetry.log`` (plus its rotated ``.1``/``.2`` siblings) — must
    never be surfaced to the agent as user content: the log self-mutates mid-task,
    so any proposed move fails the optimistic-concurrency hash check.

    Format-agnostic by design: it is called both on bare tree filenames and on
    absolute patch paths (Windows or POSIX separators), so it matches on the
    basename and on a ``.ailienant`` directory segment rather than a path prefix.
    """
    if not path:
        return False
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    if base == _TELEMETRY_LOG_BASENAME or base.startswith(_TELEMETRY_LOG_BASENAME + "."):
        return True
    # The home dir as a path segment (`.ailienant/...`). `AILIENANT.md` inside it is
    # user-authored, shareable project guidance — intentionally readable/editable, so
    # it is never treated as internal.
    if _INTERNAL_HOME_DIR in norm.split("/"):
        return base != "AILIENANT.md"
    return False
