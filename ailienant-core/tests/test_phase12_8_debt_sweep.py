# ailienant-core/tests/test_phase12_8_debt_sweep.py
#
# Phase 12.8 — Fresh Debt Triage Sweep checkpoint coverage.
#
# One file, sectioned by debt item, mirroring the sub-phase's own lettered
# structure (A/B/C1-C4/D/E). Hermetic throughout: no live LLM, no real
# WebSocket — a recording ActivitySink test double stands in for the
# transport, exactly as tests/test_execution_provenance.py already does for
# core/exec_log.py::record_execution.

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# =====================================================================
# Shared test doubles
# =====================================================================


class _RecordingSink:
    """Captures every marker/blocked/detail(+chunk) call, in order."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def emit_marker(self, *, ref: str, target: Optional[str]) -> None:
        self.calls.append({"op": "marker", "ref": ref, "target": target})

    async def emit_blocked(self, *, target: str) -> None:
        self.calls.append({"op": "blocked", "target": target})

    async def emit_detail(
        self, *, ref: str, source: str, cwd: Optional[str],
        initiator: Optional[str], stdout: Optional[str], stderr: Optional[str],
        exit_code: Optional[int], duration_ms: Optional[float], truncated: bool,
        error: Optional[str],
    ) -> None:
        self.calls.append({
            "op": "detail", "ref": ref, "source": source, "cwd": cwd,
            "initiator": initiator, "stdout": stdout, "stderr": stderr,
            "exit_code": exit_code, "duration_ms": duration_ms,
            "truncated": truncated, "error": error,
        })

    async def emit_detail_chunk(self, *, ref: str, stream: str, chunk: str) -> None:
        self.calls.append({"op": "chunk", "ref": ref, "stream": stream, "chunk": chunk})


class _NarrowSink:
    """Conforms only to the PRE-12.8 ActivitySink surface — no emit_detail_chunk.
    Stands in for a hermetic test double built before DEBT-134 landed."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def emit_marker(self, *, ref: str, target: Optional[str]) -> None:
        self.calls.append({"op": "marker", "ref": ref})

    async def emit_blocked(self, *, target: str) -> None:
        self.calls.append({"op": "blocked", "target": target})

    async def emit_detail(self, **kwargs: Any) -> None:
        self.calls.append({"op": "detail", **kwargs})


# =====================================================================
# A1 — DEBT-121: real non-blocking VRAM probe
# =====================================================================


class TestGpuSlotProbe:
    def setup_method(self) -> None:
        from core.resource_manager import GPUResourceManager
        GPUResourceManager.reset_for_tests()

    def teardown_method(self) -> None:
        from core.resource_manager import GPUResourceManager
        GPUResourceManager.reset_for_tests()

    async def test_gpu1_local_tier_lock_held_by_other_session_skips(self) -> None:
        from brain.coder_companion import _companion_gpu_slot_available
        from core.config.byom_config import ModelTarget
        from core.resource_manager import GPUResourceManager

        mgr = await GPUResourceManager.get()
        assert await mgr.try_acquire_now("other-session", "local/model") is True

        with patch("brain.coder_companion._resolve_judge_tier", return_value="small"), \
             patch(
                 "core.config.model_resolver.get_chat_target",
                 return_value=ModelTarget(model="ollama/x", provider="ollama", is_local=True),
             ):
            admitted = await _companion_gpu_slot_available("my-session")
        assert admitted is False

    async def test_gpu2_same_session_or_cloud_tier_admits(self) -> None:
        from brain.coder_companion import _companion_gpu_slot_available
        from core.config.byom_config import ModelTarget
        from core.resource_manager import GPUResourceManager

        mgr = await GPUResourceManager.get()
        assert await mgr.try_acquire_now("my-session", "local/model") is True

        # Same session holds the lock — never contends with itself.
        with patch("brain.coder_companion._resolve_judge_tier", return_value="small"), \
             patch(
                 "core.config.model_resolver.get_chat_target",
                 return_value=ModelTarget(model="ollama/x", provider="ollama", is_local=True),
             ):
            assert await _companion_gpu_slot_available("my-session") is True

        # A cloud-tier judge never contends for local VRAM regardless of the lock.
        with patch("brain.coder_companion._resolve_judge_tier", return_value="big"), \
             patch(
                 "core.config.model_resolver.get_chat_target",
                 return_value=ModelTarget(model="gpt-4o", provider="openai", is_local=False),
             ):
            assert await _companion_gpu_slot_available("some-other-session") is True

    async def test_gpu3_probe_fault_fails_open_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging
        from brain.coder_companion import _companion_gpu_slot_available

        caplog.set_level(logging.WARNING, logger="CODER_COMPANION")
        with patch(
            "brain.coder_companion._resolve_judge_tier",
            side_effect=RuntimeError("resolver exploded"),
        ):
            admitted = await _companion_gpu_slot_available("s1")
        assert admitted is True  # fail-open — never suppress the card
        assert any(
            r.levelno == logging.WARNING and "GPU-slot probe failed" in r.message
            for r in caplog.records
        ), "a probe fault must WARN, not silently degrade at debug level"


# =====================================================================
# A2 — DEBT-123: vestigial NarrationGate instance swept
# =====================================================================


class TestNarrationGateSweep:
    def test_gate1_no_narration_gate_instance_in_task_service(self) -> None:
        import inspect
        import core.task_service as ts

        src = inspect.getsource(ts)
        assert "NarrationGate(" not in src
        assert "gate.record_answer" not in src
        assert "NarrationGate" not in [n for n in dir(ts) if n == "NarrationGate"]

    def test_gate1_token_batcher_narration_gate_class_untouched(self) -> None:
        # The class itself (and broadcast_pipeline_step) stay — only the
        # task_service.py *instance* was swept. A wire-contract removal would
        # break test_phase7_10_checkpoint_gate.py / test_token_batcher.py.
        from transport.token_batcher import NarrationGate

        gate = NarrationGate()
        assert gate.allow(10_000) is True
        gate.record_answer(100)
        assert gate.allow(5) is True
        assert gate.allow(10_000) is False


# =====================================================================
# A3 — DEBT-128: analyst_name wired into persona + chat prompts
# =====================================================================


class TestAnalystNameWiring:
    def test_name1_configured_name_follows_identity_clause(self, tmp_path: Any) -> None:
        from brain.personality import SoulManager
        from shared.persona import AILIENANT_IDENTITY

        soul_path = tmp_path / "SOUL.md"
        soul_path.write_text("A helpful assistant.", encoding="utf-8")
        manager = SoulManager(path=soul_path)

        with patch("api.system_settings.resolve_analyst_name", return_value="Zephyr"):
            prompt = manager.get_prompt()

        assert prompt.startswith(AILIENANT_IDENTITY)
        idx_identity = prompt.index(AILIENANT_IDENTITY)
        idx_name = prompt.index("Zephyr")
        assert idx_name > idx_identity + len(AILIENANT_IDENTITY)
        assert "does not change your identity" in prompt

    def test_name2_default_name_is_byte_identical(self, tmp_path: Any) -> None:
        from brain.personality import SoulManager
        from shared.persona import compose

        soul_path = tmp_path / "SOUL.md"
        soul_path.write_text("A helpful assistant.", encoding="utf-8")
        manager = SoulManager(path=soul_path)

        with patch("api.system_settings.resolve_analyst_name", return_value="Natt"):
            prompt = manager.get_prompt()

        assert prompt == compose("A helpful assistant.")

    def test_name2_chat_prompt_default_name_matches_prefix_stability(self) -> None:
        from core.task_service import _resolve_chat_system_prompt, _CHAT_SYSTEM_PROMPT

        with patch(
            "api.system_settings._read_settings",
            return_value={"output_style": "default", "permission_mode": "default"},
        ):
            prompt = _resolve_chat_system_prompt("Is the database connected right now?")
        assert prompt == _CHAT_SYSTEM_PROMPT

    def test_name1_chat_prompt_carries_configured_name_directive(self) -> None:
        from core.task_service import _resolve_chat_system_prompt

        with patch(
            "api.system_settings._read_settings",
            return_value={"output_style": "default", "permission_mode": "default", "analyst_name": "Zephyr"},
        ):
            prompt = _resolve_chat_system_prompt("Is the database connected right now?")
        assert "Zephyr" in prompt


# =====================================================================
# B — DEBT-125: approval-card risk-label wiring
# =====================================================================


class TestApprovalCardRiskLabels:
    async def test_risk1_file_write_approval_carries_risk_patterns_matched(self) -> None:
        import api.websocket_manager as wm

        mgr = wm.ConnectionManager()
        captured: Dict[str, Any] = {}

        async def _fake_send(session_id: str, message: Any) -> None:
            captured["payload"] = message

        with patch.object(mgr, "send_personal_message", _fake_send):
            await mgr.request_human_approval(
                session_id="s1",
                action_description="Apply change to app.py (1 of 1)",
                proposed_content="rm -rf /",
                request_kind="FILE_WRITE",
                risk_patterns_matched=["mass_deletion"],
                timeout_s=0.01,
            )
        payload = captured["payload"]
        assert payload.data.risk_patterns_matched == ["mass_deletion"]
        assert payload.data.request_kind == "FILE_WRITE"


# =====================================================================
# C1 — DEBT-133: tool-dispatch Glass-Box Timeline detail
# =====================================================================


def _echo_tool() -> Any:
    from langchain_core.tools import BaseTool

    class _EchoTool(BaseTool):
        name: str = "echo"
        description: str = "Echo the value back."

        def _run(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def _arun(self, value: str) -> str:
            return f"echo:{value}"

    return _EchoTool()


def _boom_tool() -> Any:
    from langchain_core.tools import BaseTool

    class _BoomTool(BaseTool):
        name: str = "boom"
        description: str = "Always raises."

        def _run(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def _arun(self, value: str) -> str:
            raise RuntimeError("kaboom")

    return _BoomTool()


class TestToolDispatchTimeline:
    async def test_tool1_dispatched_tool_emits_marker_and_masked_detail(self) -> None:
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from core.permissions import SessionPermissionMode, ToolPrivilegeTier
        from core.tool_dispatch import RegisteredTool, ToolCall, ToolDispatcher
        from shared.rbac import PermissionMode

        sink = _RecordingSink()
        token = bind_activity_sink(sink)
        try:
            tools = {"echo": RegisteredTool(tool=_echo_tool(), tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({"analyst"}))}
            dispatcher = ToolDispatcher(
                tools, active_role="analyst", session_mode=SessionPermissionMode.DEFAULT,
                state={}, agent_permission=PermissionMode.READ_ONLY,
            )
            result = await dispatcher.dispatch(ToolCall("echo", {"value": "api_key=HUNTER2SECRET"}))
        finally:
            reset_activity_sink(token)

        assert result.executed is True
        assert [c["op"] for c in sink.calls] == ["marker", "detail"]
        marker, detail = sink.calls
        assert marker["target"] == "echo"
        assert detail["ref"] == marker["ref"]
        assert detail["initiator"] == "analyst"
        assert detail["exit_code"] == 0
        assert "HUNTER2SECRET" not in (detail["stdout"] or "")
        assert "***REDACTED***" in (detail["stdout"] or "")

    async def test_tool2_denied_and_unknown_tool_emit_blocked_no_detail(self) -> None:
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from core.permissions import SessionPermissionMode, ToolPrivilegeTier
        from core.tool_dispatch import RegisteredTool, ToolCall, ToolDispatcher
        from shared.rbac import PermissionMode

        sink = _RecordingSink()
        token = bind_activity_sink(sink)
        try:
            tools = {"echo": RegisteredTool(tool=_echo_tool(), tier=ToolPrivilegeTier.WRITE, allowed_roles=frozenset({"analyst"}))}
            dispatcher = ToolDispatcher(
                tools, active_role="analyst", session_mode=SessionPermissionMode.DEFAULT,
                state={}, agent_permission=PermissionMode.READ_ONLY,
            )
            # WRITE tier under READ_ONLY agent_permission → DENY.
            deny_result = await dispatcher.dispatch(ToolCall("echo", {"value": "x"}))
            unknown_result = await dispatcher.dispatch(ToolCall("nonexistent", {}))
        finally:
            reset_activity_sink(token)

        assert deny_result.executed is False
        assert unknown_result.executed is False
        assert [c["op"] for c in sink.calls] == ["blocked", "blocked"]
        assert sink.calls[0]["target"] == "echo"
        assert sink.calls[1]["target"] == "nonexistent"

    async def test_tool3_sink_fault_never_changes_dispatch_result(self) -> None:
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from core.permissions import SessionPermissionMode, ToolPrivilegeTier
        from core.tool_dispatch import RegisteredTool, ToolCall, ToolDispatcher
        from shared.rbac import PermissionMode

        class _RaisingSink:
            async def emit_marker(self, *, ref: str, target: Optional[str]) -> None:
                raise RuntimeError("boom")

            async def emit_blocked(self, *, target: str) -> None:
                raise RuntimeError("boom")

            async def emit_detail(self, **_kwargs: Any) -> None:
                raise RuntimeError("boom")

        token = bind_activity_sink(_RaisingSink())
        try:
            tools = {"echo": RegisteredTool(tool=_echo_tool(), tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({"analyst"}))}
            dispatcher = ToolDispatcher(
                tools, active_role="analyst", session_mode=SessionPermissionMode.DEFAULT,
                state={}, agent_permission=PermissionMode.READ_ONLY,
            )
            result = await dispatcher.dispatch(ToolCall("echo", {"value": "hi"}))
        finally:
            reset_activity_sink(token)

        assert result.executed is True
        assert result.observation == "echo:hi"

    async def test_tool_fault_path_emits_error_detail_with_none_exit_code(self) -> None:
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from core.permissions import SessionPermissionMode, ToolPrivilegeTier
        from core.tool_dispatch import RegisteredTool, ToolCall, ToolDispatcher
        from shared.rbac import PermissionMode

        sink = _RecordingSink()
        token = bind_activity_sink(sink)
        try:
            tools = {"boom": RegisteredTool(tool=_boom_tool(), tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({"analyst"}))}
            dispatcher = ToolDispatcher(
                tools, active_role="analyst", session_mode=SessionPermissionMode.DEFAULT,
                state={}, agent_permission=PermissionMode.READ_ONLY,
            )
            result = await dispatcher.dispatch(ToolCall("boom", {"value": "x"}))
        finally:
            reset_activity_sink(token)

        assert result.executed is False
        detail = next(c for c in sink.calls if c["op"] == "detail")
        assert detail["exit_code"] is None
        assert detail["error"] is not None and "kaboom" in detail["error"]


class TestToolDispatchReplaySafety:
    """DEFER1-3: the agentic-cell HITL-defer/resume path (DEBT-129 x DEBT-133)."""

    def setup_method(self) -> None:
        import brain.agentic_cell as ac
        ac._session_registry.clear()

    async def _register_todo_write(self) -> Any:
        from core.permissions import ToolPrivilegeTier
        from core.tool_rag import ToolRAGStore, ToolSchema

        async def _fake_embed(text: str) -> List[float]:
            return [0.0] * 1536

        store = ToolRAGStore(embed_fn=_fake_embed)
        await store.register_schema(
            ToolSchema(
                name="todo_write", description="Write TODOs.", json_schema="{}",
                privilege_tier=ToolPrivilegeTier.WRITE, allowed_roles=frozenset({"core_dev"}),
            )
        )
        return store

    async def test_defer1_replay_emits_same_ref_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from brain.agentic_cell import run_agentic_cell_node
        from tests.test_phase7_19_2_agentic_cell import StubAdapter, StubSession, StubSyncSurface, _base_state, _config

        store = await self._register_todo_write()
        monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

        session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
        adapter = StubAdapter(session, StubSyncSurface())
        approve = AsyncMock(return_value=True)
        state = _base_state(
            session_permission_mode="DEFAULT",
            pending_tool_call={"name": "todo_write", "args": {"todos": []}, "activity_ref": "fixed-ref-123"},
        )

        async def _never_called(_messages: Any) -> Any:
            raise AssertionError("reasoner must not run during the approval phase")

        config = _config(adapter, _never_called, cell_tool_approval_fn=approve)

        sink = _RecordingSink()
        token = bind_activity_sink(sink)
        try:
            delta1 = await run_agentic_cell_node(state, config)
            # Simulate a LangGraph replay: the same paused state is re-driven.
            delta2 = await run_agentic_cell_node(state, config)
        finally:
            reset_activity_sink(token)

        assert delta1.get("pending_tool_call") is None
        assert delta2.get("pending_tool_call") is None
        detail_refs = {c["ref"] for c in sink.calls if c["op"] == "detail"}
        assert detail_refs == {"fixed-ref-123"}

    async def test_defer2_post_approval_detail_correlates_to_deferred_ref(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from brain.agentic_cell import run_agentic_cell_node
        from tests.test_phase7_19_2_agentic_cell import StubAdapter, StubSession, StubSyncSurface, _base_state, _config

        store = await self._register_todo_write()
        monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

        session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
        adapter = StubAdapter(session, StubSyncSurface())
        approve = AsyncMock(return_value=True)
        state = _base_state(
            session_permission_mode="DEFAULT",
            pending_tool_call={
                "name": "todo_write",
                "args": {"todos": [{"content": "x", "status": "pending", "active_form": "X"}]},
                "activity_ref": "ref-abc",
            },
        )
        async def _empty_reasoner(_messages: Any) -> List[Any]:
            return []

        config = _config(adapter, _empty_reasoner, cell_tool_approval_fn=approve)

        sink = _RecordingSink()
        token = bind_activity_sink(sink)
        try:
            await run_agentic_cell_node(state, config)
        finally:
            reset_activity_sink(token)

        # Only ONE detail — resolved directly against the deferred ref, no
        # separate fresh marker/detail pair minted by dispatch()'s own path.
        assert [c["op"] for c in sink.calls] == ["detail"]
        assert sink.calls[0]["ref"] == "ref-abc"
        assert sink.calls[0]["exit_code"] == 0

    async def test_defer3_denial_emits_terminal_detail_not_a_hang(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from brain.agentic_cell import run_agentic_cell_node
        from tests.test_phase7_19_2_agentic_cell import StubAdapter, StubSession, StubSyncSurface, _base_state, _config, _reasoner_from

        store = await self._register_todo_write()
        monkeypatch.setattr("core.tool_rag.tool_rag_store", store)

        session = StubSession(exit_codes=[0], outputs=[b"ok\n"])
        adapter = StubAdapter(session, StubSyncSurface())
        deny = AsyncMock(return_value=False)
        state = _base_state(
            session_permission_mode="DEFAULT",
            pending_tool_call={"name": "todo_write", "args": {"todos": []}, "activity_ref": "ref-denied"},
        )
        config = _config(adapter, _reasoner_from([[]]), cell_tool_approval_fn=deny)

        sink = _RecordingSink()
        token = bind_activity_sink(sink)
        try:
            delta = await run_agentic_cell_node(state, config)
        finally:
            reset_activity_sink(token)

        assert "TOOL_CALL_HITL_DENIED" in (delta.get("security_flags") or [])
        assert [c["op"] for c in sink.calls] == ["detail"]
        detail = sink.calls[0]
        assert detail["ref"] == "ref-denied"
        assert detail["exit_code"] == 1  # resolved, not left hanging on "running…"
        assert detail["error"] is None


# =====================================================================
# C2 — DEBT-132: background-task executions on the timeline
# =====================================================================


class TestBackgroundTaskTimeline:
    async def test_bg1_create_and_watch_emit_correlated_marker_and_detail(self) -> None:
        import sys
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from tools.execution_tools import BackgroundTaskManager

        sink = _RecordingSink()
        token = bind_activity_sink(sink)
        try:
            registry: Dict[str, Dict[str, Any]] = {}
            manager = BackgroundTaskManager(registry)
            task_id = await manager.create(f'{sys.executable} -c "print(\'hi\')"')

            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                entry = manager.get(task_id)
                if entry is not None and entry.get("status") == "completed":
                    break
                await asyncio.sleep(0.05)
        finally:
            reset_activity_sink(token)

        assert [c["op"] for c in sink.calls] == ["marker", "detail"]
        marker, detail = sink.calls
        assert marker["ref"] == task_id
        assert detail["ref"] == task_id
        assert detail["source"] == "native_host"
        assert detail["initiator"] == "background_task"
        assert detail["exit_code"] == 0
        assert "hi" in (detail["stdout"] or "")

    async def test_bg2_cancelled_branch_still_emits_terminal_detail(self) -> None:
        import sys
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from tools.execution_tools import BackgroundTaskManager

        sink = _RecordingSink()
        token = bind_activity_sink(sink)
        try:
            registry: Dict[str, Dict[str, Any]] = {}
            manager = BackgroundTaskManager(registry)
            task_id = await manager.create(
                f'{sys.executable} -c "import time; time.sleep(5)"'
            )
            await asyncio.sleep(0.1)  # let the process actually start
            await manager.stop(task_id)

            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if any(c["op"] == "detail" for c in sink.calls):
                    break
                await asyncio.sleep(0.05)
        finally:
            reset_activity_sink(token)

        assert registry[task_id]["status"] == "cancelled"
        detail = next(c for c in sink.calls if c["op"] == "detail")
        assert detail["ref"] == task_id
        assert detail["error"] == "cancelled by operator"


# =====================================================================
# C3 — DEBT-134: incremental devcontainer exec chunks
# =====================================================================


class TestDevcontainerLiveChunks:
    def _mgr(self) -> Any:
        import api.websocket_manager as wm
        return wm.ConnectionManager()

    async def test_chunk1_marker_registered_then_chunk_forwards_before_detail(self) -> None:
        mgr = self._mgr()
        sink = _RecordingSink()
        mgr.register_devcontainer_exec_activity("req1", "exec-ref-1", sink)
        mgr._devc_exec_buffers["req1"] = {"stdout": [], "stderr": []}

        await mgr.append_devcontainer_stream("req1", "stdout", "building…")

        assert [c["op"] for c in sink.calls] == ["chunk"]
        assert sink.calls[0]["ref"] == "exec-ref-1"
        assert sink.calls[0]["stream"] == "stdout"
        assert sink.calls[0]["chunk"] == "building…"
        # The buffer (final-result authority) is unaffected by live-forwarding.
        assert mgr._devc_exec_buffers["req1"]["stdout"] == ["building…"]

    async def test_chunk2_unregistered_request_forwards_nothing(self) -> None:
        mgr = self._mgr()
        mgr._devc_exec_buffers["req2"] = {"stdout": [], "stderr": []}
        # No register_devcontainer_exec_activity call — no turn context.
        await mgr.append_devcontainer_stream("req2", "stdout", "output")
        assert mgr._devc_exec_buffers["req2"]["stdout"] == ["output"]  # buffered unchanged

    async def test_chunk2_narrow_sink_predating_debt134_is_a_noop(self) -> None:
        mgr = self._mgr()
        sink = _NarrowSink()
        mgr.register_devcontainer_exec_activity("req3", "exec-ref-3", sink)
        mgr._devc_exec_buffers["req3"] = {"stdout": [], "stderr": []}

        await mgr.append_devcontainer_stream("req3", "stdout", "x")  # must not raise
        assert sink.calls == []  # no emit_detail_chunk on this sink — silently skipped

    # CHUNK3 (terminal detail replaces, never appends, accumulated chunk text)
    # and CHUNK5 (client-side retention clamp) are frontend behaviors —
    # timelineBuilder.ts's upsertExecutionChunk/upsertExecutionBody — covered
    # by src/test/timelineBuilder.test.ts, not here.

    async def test_chunk4_runaway_backend_budget_suppresses_after_cap(self) -> None:
        import api.websocket_manager as wm

        mgr = self._mgr()
        sink = _RecordingSink()
        mgr.register_devcontainer_exec_activity("req4", "exec-ref-4", sink)
        mgr._devc_exec_buffers["req4"] = {"stdout": [], "stderr": []}

        big_chunk = "x" * (wm._LIVE_STREAM_CAP + 1)
        await mgr.append_devcontainer_stream("req4", "stdout", big_chunk)
        # A second chunk after the cap trips must produce NO further chunk frame.
        await mgr.append_devcontainer_stream("req4", "stdout", "more output")

        chunk_ops = [c for c in sink.calls if c["op"] == "chunk"]
        # Exactly one real chunk (mirroring the buffer), then one suppression
        # notice, then silence — never a third frame for the second call.
        assert len(chunk_ops) == 2
        assert "suppressed" in chunk_ops[-1]["chunk"]
        # The buffer (terminal-detail authority) keeps growing regardless.
        assert mgr._devc_exec_buffers["req4"]["stdout"] == [big_chunk, "more output"]

    async def test_bridge_registers_only_when_both_exec_ref_and_sink_resolve(self) -> None:
        from api.devcontainer_bridge import WebSocketHostBridge
        from core.activity_context import (
            bind_activity_sink, bind_exec_ref, reset_activity_sink, reset_exec_ref,
        )

        class _StubMgr:
            registered: List[Any] = []

            async def emit_devcontainer_exec_request(self, **kwargs: Any) -> None:
                pass

            async def wait_devcontainer_exec(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
                return {"stdout": "", "stderr": "", "exit_code": 0}

            def register_devcontainer_exec_activity(self, request_id: str, exec_ref: str, sink: Any) -> None:
                self.registered.append((request_id, exec_ref, sink))

        stub = _StubMgr()
        bridge = WebSocketHostBridge(manager=stub)  # type: ignore[arg-type]

        # Neither bound — no registration, no exception.
        result = await bridge.exec_command(
            session_id="s1", command="echo hi", cwd="/w", env_whitelist={}, timeout_s=5.0,
        )
        assert result.exit_code == 0
        assert stub.registered == []

        # Both bound — registers exactly once with the correlated (exec_ref, sink).
        sink = _RecordingSink()
        sink_token = bind_activity_sink(sink)
        ref_token = bind_exec_ref("exec-9")
        try:
            await bridge.exec_command(
                session_id="s1", command="echo hi", cwd="/w", env_whitelist={}, timeout_s=5.0,
            )
        finally:
            reset_activity_sink(sink_token)
            reset_exec_ref(ref_token)

        assert len(stub.registered) == 1
        _req_id, exec_ref, registered_sink = stub.registered[0]
        assert exec_ref == "exec-9"
        assert registered_sink is sink


# =====================================================================
# C1 (ROLE1) — attribution: initiator carries the calling role, not
# hardcoded — proven by driving the same dispatcher under two roles.
# =====================================================================


class TestToolDispatchAttribution:
    async def test_role1_initiator_reflects_the_calling_role(self) -> None:
        from core.activity_context import bind_activity_sink, reset_activity_sink
        from core.permissions import SessionPermissionMode, ToolPrivilegeTier
        from core.tool_dispatch import RegisteredTool, ToolCall, ToolDispatcher
        from shared.rbac import PermissionMode

        for role in ("analyst", "core_dev"):
            sink = _RecordingSink()
            token = bind_activity_sink(sink)
            try:
                tools = {"echo": RegisteredTool(tool=_echo_tool(), tier=ToolPrivilegeTier.READ_ONLY, allowed_roles=frozenset({role}))}
                dispatcher = ToolDispatcher(
                    tools, active_role=role, session_mode=SessionPermissionMode.DEFAULT,
                    state={}, agent_permission=PermissionMode.READ_ONLY,
                )
                await dispatcher.dispatch(ToolCall("echo", {"value": "x"}))
            finally:
                reset_activity_sink(token)
            detail = next(c for c in sink.calls if c["op"] == "detail")
            assert detail["initiator"] == role


# =====================================================================
# E — DEBT-126b: server_indexing_started is stale (verification, not code)
# =====================================================================


class TestStaleIndexingHandler:
    def test_stale1_server_indexing_started_has_zero_occurrences(self) -> None:
        from pathlib import Path

        this_file = Path(__file__).resolve()
        target = "server_indexing" + "_started"  # split so this assertion's own
        # source text is never a false-positive match against itself.
        repo_root = this_file.parents[2]
        hits: List[str] = []
        for base in (repo_root / "ailienant-core", repo_root / "ailienant-extension" / "src"):
            for path in base.rglob("*"):
                if path.is_dir() or path.suffix not in {".py", ".ts", ".tsx"}:
                    continue
                if "venv" in path.parts or "node_modules" in path.parts:
                    continue
                if path.resolve() == this_file:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if target in text:
                    hits.append(str(path))
        assert hits == [], f"{target} resurfaced in: {hits}"
