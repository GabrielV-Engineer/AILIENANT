# ailienant-core/tests/test_engine_respine.py
"""Engine Re-Spine — the live coding path drives the compiled LangGraph engine.

These tests certify the foundational backend correction: routing
``_run_coding_task`` through ``alienant_app`` re-arms the mode router, the
Socratic ideation loop, and the checkpointer in one move, and the WS planner
toggle reaches the graph via the submit endpoint.

The planner stub (``AILIENANT_PLANNER_DEBUG=1``) and the analyst Socratic stub
make the graph hermetic — no BYOM engine is contacted.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict
from unittest.mock import AsyncMock, patch

import pytest

from core.task_service import TaskService, TaskPayload
from brain.state import MissionSpecification, WBSStep


@pytest.fixture(autouse=True)
def _planner_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the deterministic planner stub so the graph never needs a model."""
    monkeypatch.setenv("AILIENANT_PLANNER_DEBUG", "1")


@pytest.fixture
def _analyst_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the analyst's synthetic question batch so the grill needs no model.

    ``DEBUG_MODE`` is read into a module constant at import time, so the env var
    alone would not take effect here — patch the resolved attribute.
    """
    import agents.analyst as analyst_mod

    monkeypatch.setattr(analyst_mod, "DEBUG_MODE", True)


@pytest.fixture
def _l2_checkpoint(tmp_path: Any) -> Any:
    """Open an isolated L2 sqlite so promote() can persist; close on teardown."""
    from brain.checkpoint import checkpoint_manager

    prev_conn = checkpoint_manager._conn
    prev_path = checkpoint_manager.db_path
    checkpoint_manager.db_path = str(tmp_path / "respine_state.sqlite")
    checkpoint_manager.initialize()
    try:
        yield checkpoint_manager
    finally:
        checkpoint_manager.close()
        checkpoint_manager._conn = prev_conn
        checkpoint_manager.db_path = prev_path


def _payload(*, planner_mode: bool) -> TaskPayload:
    return TaskPayload(
        task_prompt="build a CSV exporter",
        dirty_buffers=[],
        explicit_mentions=[],
        attachments=[],
        planner_mode_active=planner_mode,
        workspace_root="",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. Planner mode → Socratic ideation suspend (no plan yet, question asked)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_planner_mode_enters_ideation_and_suspends(_analyst_debug: None) -> None:
    """planner_mode_active=True must route to the ideation loop: the analyst asks
    a batch of questions and the turn suspends on a native interrupt WITHOUT a
    MissionSpecification and without ever reaching the write-tier approval path."""
    ts = TaskService()
    broadcast_stream_end = AsyncMock()
    request_human_approval = AsyncMock()
    send_personal_message = AsyncMock()

    with patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", broadcast_stream_end), \
         patch("core.task_service.vfs_manager.request_human_approval", request_human_approval), \
         patch("core.task_service.vfs_manager.send_personal_message", send_personal_message):
        await ts._run_coding_task("sess-ideation", _payload(planner_mode=True), "SEQUENTIAL")
        await asyncio.sleep(0)

    # No write-tier approval on a Socratic suspend — Ask/Plan never got that far.
    request_human_approval.assert_not_awaited()
    # The stream was finalized so the UI's isStreaming flips back.
    broadcast_stream_end.assert_awaited()

    # The grill's question batch went out as a clarification card (the analyst no
    # longer streams prose questions — it suspends on interrupt() and the card
    # carries the structured questions).
    assert send_personal_message.await_count >= 1
    events = [c.args[1] for c in send_personal_message.await_args_list]
    cards = [
        e for e in events
        if getattr(e, "event_type", "") == "server_hitl_approval_request"
    ]
    assert cards, f"expected a HITL clarification card, got: {events}"
    data = cards[0].data
    assert data.request_kind == "CLARIFICATION_NEEDED"
    assert data.questions, "the card must carry the structured question batch"
    # Every question offers real options with exactly one recommended.
    for q in data.questions:
        assert len(q.options) >= 2
        assert sum(1 for o in q.options if o.recommended) == 1


# ──────────────────────────────────────────────────────────────────────────────
# 2. Non-planner: graph's final state -> the honest turn-end summary
#    (13.0.9: approve/reject/hooks/apply-gate control flow lives entirely in
#     brain/apply_gate.py now, exercised at the node level in
#     test_task_service_apply.py. What's left of _run_coding_task's own logic
#     at this point is reporting the graph's final applied_files_log as the
#     turn-end plan-document summary — that's what this test now certifies.)
# ──────────────────────────────────────────────────────────────────────────────


_MISSION = MissionSpecification(
    outcome="Add a CSV exporter.",
    scope=["export.py"],
    constraints=["none"],
    decisions=["go"],
    tasks=[
        WBSStep(
            step_number=1,
            target_role="core_dev",
            action="edit_file",
            target_file="export.py",
            description="write exporter",
        )
    ],
    checks=["ok"],
)

_FINAL_WITH_APPLIED_STEP: Dict[str, Any] = {
    "mission_spec": _MISSION,
    "applied_files_log": [
        {"file_path": "export.py", "command": None, "status": "completed", "step_number": 1}
    ],
    "errors": [],
    "hitl_pending": False,
}


def _astream_final(state: Dict[str, Any]) -> Any:
    def _maker(*_a: Any, **_k: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            yield state

        return _gen()

    return _maker


@pytest.mark.anyio
async def test_non_planner_final_state_produces_the_applied_summary() -> None:
    """An initial (non-resumed) run legitimately broadcasts the plan document
    twice: the early empty-summary seed (as soon as mission_spec first
    appears — lets the chat show the checklist before any step resolves) and
    the final one carrying the real, applied-outcome summary. Only the LAST
    call matters here; the seed's own behavior is exercised in the resume
    test below."""
    ts = TaskService()
    plan_doc = AsyncMock()

    with patch("brain.engine.alienant_app.astream", side_effect=_astream_final(_FINAL_WITH_APPLIED_STEP)), \
         patch("core.task_service.vfs_manager.broadcast_plan_document", plan_doc), \
         patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()):
        await ts._run_coding_task("sess-code", _payload(planner_mode=False), "SEQUENTIAL")

    assert plan_doc.await_count == 2
    assert plan_doc.await_args is not None
    _sid, final_payload = plan_doc.await_args.args[:2]
    assert "Applied 1 file change to disk" in final_payload.summary
    assert final_payload.tasks[0]["target_file"] == "export.py"


@pytest.mark.anyio
async def test_resume_does_not_re_seed_the_plan_document() -> None:
    """13.0.9 regression guard: every per-step approval interrupt re-enters
    _run_coding_task via resume_graph (resume_value is not None). Without
    gating the early-seed latch on that, EVERY step's resume would
    re-broadcast the (now-redundant) empty-summary plan seed on top of the
    turn's real final summary — noisy at best, and on a multi-step WBS,
    proportional to step count. A resumed run must broadcast the plan
    document exactly once: the real final summary, never the seed."""
    ts = TaskService()
    plan_doc = AsyncMock()

    with patch("brain.engine.alienant_app.astream", side_effect=_astream_final(_FINAL_WITH_APPLIED_STEP)), \
         patch("core.task_service.vfs_manager.broadcast_plan_document", plan_doc), \
         patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()):
        await ts._run_coding_task(
            "sess-code", _payload(planner_mode=False), "SEQUENTIAL",
            resume_value={"approved": True, "comment": None, "modified_content": None},
        )

    plan_doc.assert_awaited_once()
    assert plan_doc.await_args is not None
    _sid, payload = plan_doc.await_args.args[:2]
    assert "Applied 1 file change to disk" in payload.summary


# ──────────────────────────────────────────────────────────────────────────────
# 3. A completed graph run emits a checkpoint_id (⟲ Rewind affordance)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_completed_run_emits_checkpoint_id(_l2_checkpoint: Any) -> None:
    """Because the graph now runs on thread_id=session_id, _finalize_stream finds
    the L1 tuple, promotes it, and broadcasts a non-None checkpoint_id."""
    ts = TaskService()
    broadcast_stream_end = AsyncMock()

    with patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", broadcast_stream_end), \
         patch("api.websocket_manager.vfs_manager.broadcast_token", new=AsyncMock()):
        # Planner mode suspends at the analyst (hermetic, no model) but still runs
        # ≥1 node on the thread → a checkpoint exists to promote.
        await ts._run_coding_task("sess-ckpt", _payload(planner_mode=True), "SEQUENTIAL")
        await asyncio.sleep(0)

    broadcast_stream_end.assert_awaited()
    cids = [c.kwargs.get("checkpoint_id") for c in broadcast_stream_end.await_args_list]
    assert any(cid is not None for cid in cids), (
        f"expected a non-None checkpoint_id in {broadcast_stream_end.await_args_list}"
    )
    # The promoted snapshot is discoverable in L2.
    assert len(_l2_checkpoint.list_checkpoints("sess-ckpt")) >= 1


# ──────────────────────────────────────────────────────────────────────────────
# 4. Self-healing is owned by the graph, not the task service
# ──────────────────────────────────────────────────────────────────────────────


def test_task_service_no_longer_drives_self_heal() -> None:
    """The external attempt_correction loop was removed — self-healing now lives
    inside the graph (reflexion_guard → error_correction), and the planner/coder
    nodes are reached via the compiled graph, not imported directly. Guard against
    regressions that re-introduce the orphaned imports (checked at the import
    statements only — docstrings may still name the nodes for context)."""
    import re

    from pathlib import Path

    import core.task_service as ts_mod

    src = Path(ts_mod.__file__).read_text(encoding="utf-8")
    import_lines = [ln for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
    joined = "\n".join(import_lines)
    assert "run_planner_node" not in joined
    assert "run_coder_node" not in joined
    assert "attempt_correction" not in joined
    # The compiled graph IS imported (the new spine).
    assert any("alienant_app" in ln for ln in import_lines)


# ──────────────────────────────────────────────────────────────────────────────
# 5. The submit endpoint folds the WS planner-mode toggle into the payload
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_submit_reads_planner_mode_registry() -> None:
    """The toggle is stored per-session in planner_mode_registry; submit_task must
    fold it into the payload so the coding path can route to the ideation loop.
    Without this read the flag was always the default False."""
    import main

    captured: Dict[str, Any] = {}

    async def _capture(*, session_id: str, payload: TaskPayload, effort_level: str) -> Dict[str, Any]:
        captured["planner_mode_active"] = payload.planner_mode_active
        return {"status": "success"}

    main.planner_mode_registry["sess-toggle"] = True
    body = TaskPayload(task_prompt="x", dirty_buffers=[], workspace_root="/ws")
    try:
        with patch.object(main.task_service, "process_task", side_effect=_capture), \
             patch("main.get_effort_level", return_value="balanced"):
            await main.submit_task(body, x_task_id="sess-toggle")
            # submit_task schedules the runner as a background task; drain it.
            for _ in range(50):
                if "planner_mode_active" in captured:
                    break
                await asyncio.sleep(0.01)
    finally:
        main.planner_mode_registry.pop("sess-toggle", None)

    assert captured.get("planner_mode_active") is True
