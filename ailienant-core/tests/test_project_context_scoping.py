"""Project-context scoping: registry, additive project_id columns, filters.

Verifies the dashboard's active-project substrate:
  * the persistent ``projects`` registry + its ghost-path filter,
  * additive ``project_id`` columns tag-on-write + index-backed read filters on
    the telemetry, audit, and DLQ ledgers,
  * the additive migration path (old schema -> column added, old rows read NULL),
  * the audit blake2b chain still verifies after the column add,
  * ``project_id_for_thread`` resolves from the persisted checkpoint (the
    reconnection-safe audit source) and falls back cleanly when absent.

Async functions are driven via ``asyncio.run`` so the suite needs no
pytest-asyncio. Each test isolates its DB in a ``tmp_path`` file — through the
``db_path=`` seam where one exists, else by monkeypatching the module's
``DB_CATALOG_PATH``.
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest


# ── Project registry + ghost filter ──────────────────────────────────────────

def test_projects_registry_roundtrip_and_ghost_filter(tmp_path, monkeypatch) -> None:
    import core.db as catalog_db
    import api.projects as projects_api

    db = str(tmp_path / "catalog.sqlite")
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", db)

    real_root = str(tmp_path / "live_ws")
    (tmp_path / "live_ws").mkdir()
    ghost_root = str(tmp_path / "deleted_ws")  # never created on disk

    async def _run():
        # Create just the projects table (avoid init_db's global sync conn).
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE projects (project_id TEXT PRIMARY KEY, "
            "workspace_root TEXT NOT NULL, last_seen_utc INTEGER NOT NULL)"
        )
        conn.commit()
        conn.close()
        await catalog_db.upsert_project("pid_live", real_root)
        await catalog_db.upsert_project("pid_ghost", ghost_root)
        all_rows = await catalog_db.get_all_projects()
        visible = await projects_api.list_projects()
        return all_rows, visible

    all_rows, visible = asyncio.run(_run())

    # Registry persists both rows; the endpoint hides the ghost.
    assert {r[0] for r in all_rows} == {"pid_live", "pid_ghost"}
    ids = {p["id"] for p in visible}
    assert ids == {"pid_live"}
    live = next(p for p in visible if p["id"] == "pid_live")
    assert live["name"] == "live_ws"
    assert live["path"] == real_root


def test_upsert_project_is_idempotent(tmp_path, monkeypatch) -> None:
    import core.db as catalog_db

    db = str(tmp_path / "catalog.sqlite")
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", db)

    async def _run():
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE projects (project_id TEXT PRIMARY KEY, "
            "workspace_root TEXT NOT NULL, last_seen_utc INTEGER NOT NULL)"
        )
        conn.commit()
        conn.close()
        await catalog_db.upsert_project("p", "/root/a")
        await catalog_db.upsert_project("p", "/root/b")  # re-connect, new path
        await catalog_db.upsert_project("", "/blank")     # ignored — blank id
        return await catalog_db.get_all_projects()

    rows = asyncio.run(_run())
    assert len(rows) == 1
    assert rows[0][0] == "p"
    assert rows[0][1] == "/root/b"  # overwritten, not duplicated


# ── Audit ledger scoping + chain integrity + migration ───────────────────────

def test_audit_project_scoping_and_chain(tmp_path) -> None:
    from core.audit import init_audit_table, log_audit_event, verify_chain

    db = str(tmp_path / "audit.sqlite")

    async def _run():
        await init_audit_table(db_path=db)
        await log_audit_event(
            session_id="s1", action_description="edit a", proposed_content="x",
            resolution="approved", project_id="proj_A", db_path=db,
        )
        await log_audit_event(
            session_id="s2", action_description="edit b", proposed_content="y",
            resolution="approved", project_id="proj_B", db_path=db,
        )
        # Chain integrity is independent of the new column.
        return await verify_chain("s1", db_path=db), await verify_chain("s2", db_path=db)

    ok1, ok2 = asyncio.run(_run())
    assert ok1 and ok2

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT session_id FROM hitl_audit_log WHERE project_id = ? ORDER BY rowid",
        ("proj_A",),
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["s1"]  # index-backed filter returns only proj_A


def test_audit_migration_adds_column_and_index(tmp_path) -> None:
    from core.audit import init_audit_table

    db = str(tmp_path / "audit_old.sqlite")
    # Seed the PRE-migration schema (no project_id) + one legacy row.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE hitl_audit_log ("
        "audit_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_kind TEXT NOT NULL, "
        "action_description TEXT NOT NULL, proposed_content_scrubbed TEXT, "
        "proposed_content_hash TEXT NOT NULL, resolution TEXT NOT NULL, "
        "resolution_comment TEXT, operator_user_email TEXT, prev_chain_hash TEXT, "
        "chain_hash TEXT NOT NULL, resolved_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO hitl_audit_log (audit_id, session_id, request_kind, "
        "action_description, proposed_content_hash, resolution, chain_hash, resolved_at) "
        "VALUES ('a1','s','OTHER','x','h','approved','c',1)"
    )
    conn.commit()
    conn.close()

    asyncio.run(init_audit_table(db_path=db))

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hitl_audit_log)")}
    idx = {r[1] for r in conn.execute("PRAGMA index_list(hitl_audit_log)")}
    legacy = conn.execute(
        "SELECT project_id FROM hitl_audit_log WHERE audit_id='a1'"
    ).fetchone()
    conn.close()
    assert "project_id" in cols
    assert "idx_audit_project" in idx
    assert legacy[0] is None  # pre-migration row reads NULL, not a crash


# ── Telemetry (routing) scoping + migration ──────────────────────────────────

def test_telemetry_routing_project_filter(tmp_path) -> None:
    import core.telemetry as tele

    db = str(tmp_path / "telemetry.sqlite")
    tele.init_telemetry_db(db)
    try:
        tele.log_routing_decision("s1", "planner", "coder", "r", project_id="proj_A")
        tele.log_routing_decision("s2", "planner", "coder", "r", project_id="proj_B")
        tele.log_routing_decision("s3", "planner", "coder", "r")  # unscoped -> NULL

        scoped = tele.recent_routing_decisions(project_id="proj_A")
        every = tele.recent_routing_decisions()
    finally:
        tele.shutdown_telemetry_db()

    assert len(scoped) == 1
    assert scoped[0]["session_id"] == "s1"
    assert scoped[0]["project_id"] == "proj_A"
    assert len(every) == 3  # unfiltered returns all, including the NULL row


def test_telemetry_migration_adds_column(tmp_path) -> None:
    import core.telemetry as tele

    db = str(tmp_path / "telemetry_old.sqlite")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, session_id TEXT, "
        "source_node TEXT, target_node TEXT, reason TEXT, css_score REAL, "
        "tci_score REAL, hardware_constraint TEXT)"
    )
    conn.execute("INSERT INTO routing_decisions (session_id) VALUES ('legacy')")
    conn.commit()
    conn.close()

    tele.init_telemetry_db(db)
    try:
        rows = tele.recent_routing_decisions()
    finally:
        tele.shutdown_telemetry_db()

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(routing_decisions)")}
    conn.close()
    assert "project_id" in cols
    assert rows[0]["project_id"] is None  # legacy row surfaces NULL


# ── DLQ scoping + migration ──────────────────────────────────────────────────

def test_dlq_project_scoping_and_filter(tmp_path, monkeypatch) -> None:
    import core.dead_letter as dlq

    db = str(tmp_path / "catalog.sqlite")
    monkeypatch.setattr(dlq, "DB_CATALOG_PATH", db)

    async def _run():
        await dlq.init_dlq_table()
        await dlq.save_dead_letter(
            task_id="t1", thread_id="t1", failed_node="coder",
            exc=RuntimeError("boom"), state={"task_id": "t1", "project_id": "proj_A"},
        )
        await dlq.save_dead_letter(
            task_id="t2", thread_id="t2", failed_node="coder",
            exc=RuntimeError("boom"), state={"task_id": "t2", "project_id": "proj_B"},
        )
        scoped = await dlq.get_pending_dlqs(project_id="proj_A")
        every = await dlq.get_pending_dlqs()
        return scoped, every

    scoped, every = asyncio.run(_run())
    assert [r.task_id for r in scoped] == ["t1"]
    assert scoped[0].project_id == "proj_A"
    assert len(every) == 2


def test_dlq_migration_adds_column(tmp_path, monkeypatch) -> None:
    import core.dead_letter as dlq

    db = str(tmp_path / "catalog.sqlite")
    monkeypatch.setattr(dlq, "DB_CATALOG_PATH", db)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE dead_letter_tasks (episode_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
        "thread_id TEXT NOT NULL, failed_node TEXT NOT NULL, exception_class TEXT NOT NULL, "
        "exception_message TEXT NOT NULL, state_snapshot_blob_hash TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, resolved_at INTEGER)"
    )
    conn.commit()
    conn.close()

    asyncio.run(dlq.init_dlq_table())

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dead_letter_tasks)")}
    idx = {r[1] for r in conn.execute("PRAGMA index_list(dead_letter_tasks)")}
    conn.close()
    assert "project_id" in cols
    assert "idx_dlq_project" in idx


# ── Reconnection-safe audit source ───────────────────────────────────────────

def test_project_id_for_thread_resolves_from_checkpoint(monkeypatch) -> None:
    import brain.checkpoint as ckpt

    fake = SimpleNamespace(checkpoint={"channel_values": {"project_id": "proj_X"}})
    monkeypatch.setattr(ckpt.checkpoint_manager, "get_tuple", lambda cfg: fake)
    assert ckpt.project_id_for_thread("thread-1") == "proj_X"


def test_project_id_for_thread_falls_back_when_absent(monkeypatch) -> None:
    import brain.checkpoint as ckpt

    # No checkpoint at all -> None (audit still writes a NULL-project row).
    monkeypatch.setattr(ckpt.checkpoint_manager, "get_tuple", lambda cfg: None)
    assert ckpt.project_id_for_thread("thread-x") is None
    assert ckpt.project_id_for_thread("") is None

    # Checkpoint present but project channel unset -> None.
    empty = SimpleNamespace(checkpoint={"channel_values": {}})
    monkeypatch.setattr(ckpt.checkpoint_manager, "get_tuple", lambda cfg: empty)
    assert ckpt.project_id_for_thread("thread-y") is None
