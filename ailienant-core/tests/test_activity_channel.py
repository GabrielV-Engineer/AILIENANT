"""Glass-Box Timeline activity channel — the un-throttled, ordered event stream.

Covers the two new backend surfaces: the pure label→kind classifier that feeds the
channel from the single `_narrate` choke point, and the `broadcast_activity_event`
emitter (a typed `server_activity_event`, distinct from the throttled pipeline step).
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from core.task_service import _classify_activity

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --------------------------------------------------------------------------- #
# _classify_activity — raw node label → (kind, target, metric)
# --------------------------------------------------------------------------- #


def test_classify_free_text_action_verbs() -> None:
    assert _classify_activity("reading fibonacci.py") == ("read", "fibonacci.py", None)
    assert _classify_activity("editing app.py") == ("edit", "app.py", None)
    assert _classify_activity("writing gui.py") == ("edit", "gui.py", None)
    assert _classify_activity("verified mypy .") == ("command", "mypy .", None)
    assert _classify_activity("self-healing coder_agent") == ("heal", "coder_agent", None)
    assert _classify_activity("recovered coder_agent") == ("heal", "coder_agent", None)


def test_classify_blocked_command_carries_denied_metric() -> None:
    # A command intercepted before record_execution ever runs (a permission-gate
    # denial or a dangerous-pattern match) still surfaces on the timeline — as a
    # distinct "denied" outcome rather than the ordinary "command" a completed
    # execution produces. "running " is deliberately absent from the verb table:
    # record_execution itself is now the sole emitter of that marker, correlated
    # by `ref` to the I/O detail channel — see core/exec_log.py.
    assert _classify_activity("blocked rm -rf /") == ("command", "rm -rf /", "denied")


def test_classify_phase_tokens() -> None:
    assert _classify_activity("context_gather") == ("understanding", None, None)
    assert _classify_activity("synthesizing_intent") == ("understanding", None, None)
    assert _classify_activity("drafting_spec") == ("planning", None, None)
    assert _classify_activity("handoff_to_planner") == ("planning", None, None)
    assert _classify_activity("critic_review") == ("reviewing", None, None)
    assert _classify_activity("plan_validated") == ("reviewing", None, None)
    assert _classify_activity("critic_rejected → replanning (1/3)") == (
        "reviewing", None, "replanning",
    )


def test_classify_grill_phase_tokens() -> None:
    """The Socratic grill (agents/analyst.py) narrates its own two phases —
    without these the whole interview was silent on the timeline."""
    assert _classify_activity("grill_grounding") == ("understanding", None, None)
    assert _classify_activity("grill_composing_questions") == ("planning", None, None)


def test_classify_unknown_returns_none() -> None:
    # A label with no timeline equivalent flows only to the legacy pipeline-step channel.
    assert _classify_activity("some_new_internal_token") == (None, None, None)
    assert _classify_activity("") == (None, None, None)


def test_classify_kinds_are_all_in_the_contract_enum() -> None:
    # Every kind the classifier can produce must be a member of ActivityKind, or the
    # payload would fail contract validation at the edge (no raw token ever escapes).
    from api.ws_contracts import ActivityEventPayload

    import typing
    allowed = set(typing.get_args(ActivityEventPayload.model_fields["kind"].annotation))
    labels = [
        "reading x", "editing x", "writing x", "blocked x", "verified x",
        "giving up on x after 3 attempts", "self-healing x", "recovered x",
        "could not auto-fix x", "retrieving context",
        "context_gather", "synthesizing_intent", "handoff_to_planner",
        "drafting_spec", "critic_review", "unwrapping_schema", "plan_validated",
        "plan_budget_overage_advisory", "critic_rejected → replanning (1/3)",
    ]
    for lbl in labels:
        kind, _, _ = _classify_activity(lbl)
        assert kind is None or kind in allowed, f"{lbl!r} → {kind!r} not in enum"


# --------------------------------------------------------------------------- #
# broadcast_activity_event — a typed server_activity_event, un-gated
# --------------------------------------------------------------------------- #


async def test_broadcast_activity_event_shape() -> None:
    from api.websocket_manager import vfs_manager
    from api.ws_contracts import ServerActivityEvent

    sent: List[Any] = []

    async def _capture(client_id: str, event: Any) -> None:
        sent.append(event)

    with patch.object(vfs_manager, "send_personal_message", new=AsyncMock(side_effect=_capture)):
        await vfs_manager.broadcast_activity_event(
            "sess-1", seq=0, ts=1.0, kind="read", target="fibonacci.py",
        )

    assert len(sent) == 1
    ev = sent[0]
    assert isinstance(ev, ServerActivityEvent)
    assert ev.event_type == "server_activity_event"
    assert ev.data.seq == 0
    assert ev.data.kind == "read"
    assert ev.data.target == "fibonacci.py"
    assert ev.data.session_id == "sess-1"


# --------------------------------------------------------------------------- #
# _ThinkingStreamer — reasoning span correlation (on_span_start + ref)
# --------------------------------------------------------------------------- #


async def test_thinking_streamer_fires_span_start_once_with_ref() -> None:
    from core.task_service import _ThinkingStreamer

    spans: List[str] = []
    broadcasts: List[dict] = []

    async def _capture_broadcast(sid: str, chunk: str, n: int = 0, source: str = "native", ref: Optional[str] = None) -> None:
        broadcasts.append({"chunk": chunk, "ref": ref})

    async def _on_span_start(ref: str) -> None:
        spans.append(ref)

    streamer = _ThinkingStreamer(
        "sess-X", broadcast=_capture_broadcast, on_span_start=_on_span_start,
    )
    await streamer.feed("hello ")
    await streamer.flush()
    await streamer.feed("world")
    await streamer.flush()

    # Fired exactly once, regardless of how many flushes happen in the span.
    assert len(spans) == 1
    # Every flush in the span carries the SAME ref, so the frontend correlates
    # both deltas to one timeline node.
    assert len(broadcasts) == 2
    assert broadcasts[0]["ref"] == spans[0]
    assert broadcasts[1]["ref"] == spans[0]


async def test_thinking_streamer_no_span_start_hook_is_a_noop() -> None:
    from core.task_service import _ThinkingStreamer

    calls: List[Any] = []
    streamer = _ThinkingStreamer(
        "sess-X", broadcast=AsyncMock(side_effect=lambda *a, **k: calls.append(a)),
    )
    await streamer.feed("hi")
    await streamer.flush()  # must not raise with on_span_start=None (the default)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# LiveCellDispatcher — cell activity marker (ref = cell:{iteration})
# --------------------------------------------------------------------------- #


async def test_live_cell_dispatcher_pushes_cell_activity_marker_once() -> None:
    from api.websocket_manager import LiveCellDispatcher

    calls: List[Tuple[Any, ...]] = []

    async def _push(kind: str, target: Any = None, metric: Any = None, ref: Any = None) -> None:
        calls.append((kind, target, metric, ref))

    with patch("api.websocket_manager.vfs_manager.broadcast_cell_tool_start", new=AsyncMock()):
        dispatcher = LiveCellDispatcher("sess-cell", push_activity=_push)
        await dispatcher.emit_tool_call_start(
            iteration=2, tool_name="run_terminal", args_scrubbed={"command": "pytest"},
        )

    assert calls == [("cell", "run_terminal", "iteration 3", "cell:2")]


async def test_live_cell_dispatcher_without_push_activity_is_a_noop() -> None:
    from api.websocket_manager import LiveCellDispatcher

    with patch("api.websocket_manager.vfs_manager.broadcast_cell_tool_start", new=AsyncMock()):
        dispatcher = LiveCellDispatcher("sess-cell")  # no push_activity — default None
        await dispatcher.emit_tool_call_start(
            iteration=0, tool_name="run_terminal", args_scrubbed={},
        )  # must not raise


# --------------------------------------------------------------------------- #
# Plan + diff activity markers — end-to-end through _run_coding_task
# --------------------------------------------------------------------------- #


async def test_plan_and_diff_activity_markers_fire_in_order() -> None:
    from typing import AsyncIterator, Dict

    from core.task_service import TaskService, TaskPayload
    from brain.state import MissionSpecification, WBSStep

    mission = MissionSpecification(
        outcome="Bump the increment.", scope=["calc.py"], constraints=["none"],
        decisions=["go"],
        tasks=[WBSStep(
            step_number=1, target_role="core_dev", action="edit_file",  # type: ignore[arg-type]
            target_file="calc.py", description="bump", status="pending",  # type: ignore[arg-type]
        )],
        checks=["ok"],
    )
    final_state: Dict[str, Any] = {
        "mission_spec": mission,
        "pending_patches": {"calc.py": "--- a/calc.py\n+++ b/calc.py\n@@\n-def f():\n-    return 1\n+def f():\n+    return 2\n"},
        "pending_contents": {"calc.py": "def f():\n    return 2\n"},
        "pending_base_hash": {"calc.py": "deadbeef"},
        "errors": [],
        "hitl_pending": False,
        "session_permission_mode": "AUTO",
    }

    async def _fake_astream(*_a: Any, **_k: Any) -> AsyncIterator[Dict[str, Any]]:
        yield final_state  # single snapshot: both the early-seed latch and the final pass see it

    captured: List[dict] = []

    async def _capture_activity(sid: str, *, seq: int, ts: float, kind: str, target=None, metric=None, ref=None) -> None:
        captured.append({"seq": seq, "kind": kind, "target": target, "metric": metric, "ref": ref})

    apply_mock = AsyncMock(return_value={"ok": True, "applied_files": ["calc.py"], "stale_files": []})
    payload = TaskPayload(
        task_prompt="bump the increment", dirty_buffers=[], project_id=None, workspace_root="/ws",
    )

    ctxs = [
        patch("brain.engine.alienant_app.astream", side_effect=_fake_astream),
        patch("core.write_pipeline.apply_patch_set", new=apply_mock),
        patch("core.task_service.vfs_manager.broadcast_activity_event", new=AsyncMock(side_effect=_capture_activity)),
        patch("core.task_service.vfs_manager.broadcast_pipeline_step", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_token", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_stream_end", new=AsyncMock()),
        patch("core.task_service.vfs_manager.broadcast_plan_document", new=AsyncMock()),
    ]
    for c in ctxs:
        c.start()
    try:
        await TaskService()._run_coding_task("s1", payload, "SEQUENTIAL")
    finally:
        for c in ctxs:
            c.stop()

    kinds = [c["kind"] for c in captured]
    # "understanding" (context_gather) fires first, then "plan" (seeded as soon as
    # mission_spec appears), then "diff" once the file actually lands on disk.
    assert kinds[0] == "understanding"
    assert "plan" in kinds
    assert "diff" in kinds
    assert kinds.index("plan") < kinds.index("diff")

    plan_evt = next(c for c in captured if c["kind"] == "plan")
    assert plan_evt["metric"] == "1 steps"

    diff_evt = next(c for c in captured if c["kind"] == "diff")
    assert diff_evt["target"] == "calc.py"
    assert diff_evt["ref"] == "calc.py"
    assert diff_evt["metric"] == "+2 -2"

    # seq is strictly increasing across the whole captured sequence.
    seqs = [c["seq"] for c in captured]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
