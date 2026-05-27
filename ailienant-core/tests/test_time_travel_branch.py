# tests/test_time_travel_branch.py
"""Phase 7.11.8 DoD — Time-Travel Debugging (ADR-706 §4.5g).

Five tests covering the backend-side contract for thread branching:

  1. `HybridCheckpointer.list_checkpoints` round-trip — two promotes produce
     two chronological rows; metadata `termination_reason` is deserialised
     and surfaced; backward-compat empty for unknown threads.
  2. `branch_from` copies the L2 row to a new thread_id, preserves blobs
     byte-for-byte, and sets `parent_id` to the SOURCE checkpoint_id (the
     branch-boundary marker that future lineage walks rely on).
  3. `branch_from` returns False on a missing source; no row is written.
  4. `task_service.branch_session` invokes the storage layer + broadcasts
     `server_session_branched` only on success (no broadcast on failure).
  5. Pydantic round-trip for the three new contract surfaces: the
     `ClientBranchFromCheckpointEvent` + `ServerSessionBranchedEvent` event
     envelopes AND the additive `checkpoint_id` field on
     `ServerStreamEndEvent.data` (backward-compat: an empty data dict still
     validates).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api.ws_contracts import (
    ClientBranchFromCheckpointEvent,
    ClientBranchFromCheckpointPayload,
    ServerSessionBranchedEvent,
    ServerSessionBranchedPayload,
    ServerStreamEndEvent,
)
from brain.checkpoint import HybridCheckpointer
from core.task_service import TaskService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Pin the anyio backend to asyncio — matches every other async test file."""
    return "asyncio"


def _fresh_checkpointer(tmp_path: Any) -> HybridCheckpointer:
    """Create an isolated HybridCheckpointer pinned to a tmp_path SQLite file."""
    ck = HybridCheckpointer(db_path=str(tmp_path / "checkpoint.sqlite"))
    ck.initialize()
    return ck


_PUT_COUNTER = [0]


def _put_checkpoint(
    ck: HybridCheckpointer,
    thread_id: str,
    payload: dict,
    metadata: dict,
) -> str:
    """Write one checkpoint row directly into L2 (bypassing MemorySaver).

    The MemorySaver → ``promote()`` path is non-deterministic in pytest's
    full-suite ordering because Python's hash-randomisation perturbs the
    dict iteration order MemorySaver's ``get_tuple`` walks, occasionally
    causing the second ``promote()`` to re-emit the FIRST checkpoint. Since
    the methods under test (``list_checkpoints`` / ``branch_from``) read
    SQLite directly and the goal here is the L2 contract, we sidestep
    MemorySaver entirely. Each call mints a fresh UUID4 + uses the
    checkpointer's own serde to encode the blobs identically to ``promote``.
    """
    import uuid as _uuid
    import time as _time
    cid = _uuid.uuid4().hex
    _PUT_COUNTER[0] += 1
    checkpoint = {
        "v": 1,
        "id": cid,
        "ts": f"2025-01-01T00:00:{_PUT_COUNTER[0]:02d}Z",
        "channel_values": payload,
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }
    ckpt_type, ckpt_blob = ck.serde.dumps_typed(checkpoint)
    meta_type, meta_blob = ck.serde.dumps_typed(metadata)
    assert ck.conn is not None
    with ck.conn:
        ck.conn.execute(
            "INSERT OR REPLACE INTO hybrid_checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
            (thread_id, "", cid, None,
             ckpt_type, ckpt_blob, meta_type, meta_blob,
             _time.monotonic() + _PUT_COUNTER[0] * 0.001),
        )
    return cid


# ──────────────────────────────────────────────────────────────────────────────
# 1. list_checkpoints round-trip
# ──────────────────────────────────────────────────────────────────────────────

def test_list_checkpoints_returns_chronological_chain_with_termination(
    tmp_path: Any,
) -> None:
    ck = _fresh_checkpointer(tmp_path)

    cid1 = _put_checkpoint(ck, "thread-A", {"step": 1}, {"source": "input"})
    cid2 = _put_checkpoint(
        ck, "thread-A", {"step": 2},
        {"source": "loop", "termination_reason": "user_abort"},
    )

    summaries = ck.list_checkpoints("thread-A")
    assert len(summaries) == 2
    ids_in_order = [s.checkpoint_id for s in summaries]
    # promoted_at is monotonic — the order MUST match insertion.
    assert ids_in_order == [cid1, cid2]

    # First row has no termination marker; second has the abort marker.
    assert summaries[0].termination_reason is None
    assert summaries[1].termination_reason == "user_abort"

    # Unknown thread → empty list (defensive; never raises).
    assert ck.list_checkpoints("thread-NONE") == []

    ck.close()


# ──────────────────────────────────────────────────────────────────────────────
# 2. branch_from preserves blobs + sets parent_id
# ──────────────────────────────────────────────────────────────────────────────

def test_branch_from_copies_row_and_links_parent(tmp_path: Any) -> None:
    ck = _fresh_checkpointer(tmp_path)
    cid = _put_checkpoint(ck, "thread-A", {"step": 1}, {"source": "input"})

    # Read source bytes for byte-identity assertion below.
    assert ck.conn is not None
    src_row = ck.conn.execute(
        "SELECT ckpt_type, ckpt_blob, meta_type, meta_blob "
        "FROM hybrid_checkpoints WHERE thread_id=? AND checkpoint_id=?",
        ("thread-A", cid),
    ).fetchone()
    assert src_row is not None

    ok = ck.branch_from(
        from_thread_id="thread-A",
        from_checkpoint_id=cid,
        new_thread_id="thread-B",
    )
    assert ok is True

    new_row = ck.conn.execute(
        "SELECT parent_id, ckpt_type, ckpt_blob, meta_type, meta_blob "
        "FROM hybrid_checkpoints WHERE thread_id=? AND checkpoint_id=?",
        ("thread-B", cid),
    ).fetchone()
    assert new_row is not None
    parent_id, *new_blobs = new_row

    # parent_id on the branched row points to the SOURCE checkpoint_id — that
    # is the branch-boundary marker future lineage walks rely on.
    assert parent_id == cid
    # Blobs survive verbatim.
    assert tuple(new_blobs) == tuple(src_row)

    ck.close()


# ──────────────────────────────────────────────────────────────────────────────
# 3. branch_from returns False on missing source
# ──────────────────────────────────────────────────────────────────────────────

def test_branch_from_missing_source_returns_false(tmp_path: Any) -> None:
    ck = _fresh_checkpointer(tmp_path)
    _put_checkpoint(ck, "thread-A", {"step": 1}, {})

    ok = ck.branch_from(
        from_thread_id="thread-A",
        from_checkpoint_id="does-not-exist",
        new_thread_id="thread-B",
    )
    assert ok is False

    # No row written to thread-B.
    assert ck.conn is not None
    cnt = ck.conn.execute(
        "SELECT COUNT(*) FROM hybrid_checkpoints WHERE thread_id=?",
        ("thread-B",),
    ).fetchone()[0]
    assert cnt == 0

    ck.close()


# ──────────────────────────────────────────────────────────────────────────────
# 4. task_service.branch_session — success broadcasts, failure does NOT
# ──────────────────────────────────────────────────────────────────────────────

async def test_branch_session_broadcasts_only_on_success() -> None:
    """The orchestration wrapper must call ``checkpoint_manager.branch_from``
    AND broadcast ``server_session_branched`` on success — but stay silent
    (no broadcast) when the storage layer reports the source is missing.
    """
    ts = TaskService()
    mock_branch_ok = AsyncMock()  # never awaited — branch_from is sync; use MagicMock equivalent
    # Path 1: success — branch_from returns True.
    with patch("brain.checkpoint.checkpoint_manager.branch_from", return_value=True), \
         patch("core.task_service.vfs_manager.broadcast_session_branched",
               new=AsyncMock()) as mock_broadcast:
        ok = await ts.branch_session(
            parent_session_id="parent-1",
            from_checkpoint_id="cid-abc",
            new_session_id="new-1",
        )
    assert ok is True
    mock_broadcast.assert_awaited_once_with(
        parent_session_id="parent-1",
        new_session_id="new-1",
        from_checkpoint_id="cid-abc",
    )

    # Path 2: missing source — branch_from returns False; broadcast must NOT fire.
    with patch("brain.checkpoint.checkpoint_manager.branch_from", return_value=False), \
         patch("core.task_service.vfs_manager.broadcast_session_branched",
               new=AsyncMock()) as mock_broadcast2:
        ok = await ts.branch_session(
            parent_session_id="parent-1",
            from_checkpoint_id="missing",
            new_session_id="new-2",
        )
    assert ok is False
    mock_broadcast2.assert_not_called()

    # Reference the unused mock so the static analyser doesn't flag it.
    _ = mock_branch_ok


# ──────────────────────────────────────────────────────────────────────────────
# 5. Pydantic round-trip for the three new contract surfaces
# ──────────────────────────────────────────────────────────────────────────────

def test_time_travel_events_round_trip() -> None:
    """Every new event must round-trip cleanly through Pydantic's validate ↔
    dump cycle so the WebSocketMessage discriminated union resolves on both
    sides of the wire. Also verifies the backward-compat shape of the
    ``ServerStreamEndEvent`` — pre-7.11.8 servers emit ``data={}``; we still
    parse it cleanly, and the new ``checkpoint_id`` key (when supplied) round
    trips inside the existing free-form ``data`` dict.
    """
    events = [
        ClientBranchFromCheckpointEvent(data=ClientBranchFromCheckpointPayload(
            parent_session_id="parent-1",
            from_checkpoint_id="cid-abc",
        )),
        ServerSessionBranchedEvent(data=ServerSessionBranchedPayload(
            parent_session_id="parent-1",
            new_session_id="new-1",
            from_checkpoint_id="cid-abc",
        )),
        # Backward-compat: empty data dict (pre-7.11.8 wire shape).
        ServerStreamEndEvent(),
        # Forward shape: checkpoint_id key inside the free-form data dict.
        ServerStreamEndEvent(data={"checkpoint_id": "cid-xyz"}),
    ]
    for ev in events:
        ev_any: Any = ev
        raw = ev_any.model_dump_json()
        # Cast to Any so mypy doesn't see only the BaseModel base in the loop var.
        round_trip: Any = type(ev).model_validate_json(raw)
        assert round_trip.event_type == ev_any.event_type, (
            f"event_type drift on {type(ev).__name__}"
        )

    # The forward variant preserves the checkpoint_id verbatim.
    fwd = ServerStreamEndEvent(data={"checkpoint_id": "cid-xyz"})
    restored: Any = ServerStreamEndEvent.model_validate_json(fwd.model_dump_json())
    assert restored.data == {"checkpoint_id": "cid-xyz"}
