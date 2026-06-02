# tests/test_abort_mesh.py
"""Phase 7.11.3 DoD — Execution Interruption (ADR-706 §4.5b).

Five tests covering the backend half of the Stop button:

  1. `TaskService.register_active_task` + `abort_session` round-trip — registry
     correctly stores the asyncio.Task and `abort_session` cancels it (returns
     True); unknown session_id returns False.
  2. `_run_coding_task` honors cancellation — the compiled graph's `astream` is
     patched to a slow async-generator; the task is cancelled mid-stream;
     afterwards `broadcast_stream_end` was emitted and the cancellation
     propagated cleanly (no unhandled exception escapes the orchestrator).
  3. `stream_analyst_reply` honors cancellation — analyst stream is patched to
     a slow async-generator; cancel mid-stream; `broadcast_natt_stream_end` was
     still emitted in the finally path.
  4. `LLMGateway.astream_byom` records token usage from the final chunk into
     `token_ledger` (Phase 7.11.3 FinOps fix — the streaming-no-record bug).
  5. `ClientAbortMeshEvent` payload contract round-trips via Pydantic.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

from api.ws_contracts import ClientAbortMeshEvent, ClientAbortMeshPayload
from core.task_service import TaskService, TaskPayload
from core.token_ledger import token_ledger
from tools.llm_gateway import LLMGateway

pytestmark = pytest.mark.anyio


# ──────────────────────────────────────────────────────────────────────────────
# 1. Registry round-trip
# ──────────────────────────────────────────────────────────────────────────────


async def test_register_and_abort_session() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]
    started = asyncio.Event()

    async def _slow_runner() -> None:
        started.set()
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(_slow_runner())
    await started.wait()
    ts.register_active_task("sess-A", task)

    assert ts.abort_session("sess-A") is True
    assert ts.abort_session("sess-unknown") is False

    # Wait for the cancelled task to settle (it catches CancelledError → returns).
    await asyncio.wait_for(task, timeout=1.0)
    # The done-callback should have auto-popped the entry.
    assert "sess-A" not in ts._active_tasks


# ──────────────────────────────────────────────────────────────────────────────
# 2. _run_coding_task — CancelledError handled, stream_end emitted, marker set
# ──────────────────────────────────────────────────────────────────────────────


async def test_run_coding_task_aborts_cleanly_on_cancel() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]
    started = asyncio.Event()

    def _slow_astream(*_a: Any, **_k: Any) -> AsyncIterator[dict[str, Any]]:
        async def _gen() -> AsyncIterator[dict[str, Any]]:
            started.set()
            await asyncio.sleep(5.0)     # the test cancels before this yields
            yield {"mission_spec": None}

        return _gen()

    broadcast_stream_end = AsyncMock()
    broadcast_token = AsyncMock()
    broadcast_pipeline_step = AsyncMock()

    payload = TaskPayload(
        task_prompt="add a comment",
        dirty_buffers=[],
        explicit_mentions=[],
        attachments=[],
        planner_mode_active=False,
        workspace_root="",
    )

    with patch("brain.engine.alienant_app.astream", side_effect=_slow_astream), \
         patch("core.task_service.vfs_manager.broadcast_stream_end", broadcast_stream_end), \
         patch("core.task_service.vfs_manager.broadcast_token", broadcast_token), \
         patch("core.task_service.vfs_manager.broadcast_pipeline_step", broadcast_pipeline_step):
        task = asyncio.create_task(ts._run_coding_task("sess-B", payload, "SEQUENTIAL"))
        ts.register_active_task("sess-B", task)
        await started.wait()
        assert ts.abort_session("sess-B") is True
        # The orchestrator swallows CancelledError and returns normally.
        await asyncio.wait_for(task, timeout=2.0)

    # Stream-end was emitted (UI's isStreaming flips back to false). Phase
    # 7.11.8 added an optional `checkpoint_id` kwarg to `broadcast_stream_end`
    # so we accept either the bare positional call or the new kwarg-bearing
    # form (the abort path passes `checkpoint_id=None` since the L1 state
    # may carry the user_abort marker but no graph node ran to completion).
    se_calls = broadcast_stream_end.call_args_list
    assert any(
        c.args == ("sess-B",) or
        (c.args == ("sess-B",) and "checkpoint_id" in c.kwargs) or
        c == (("sess-B",), {"checkpoint_id": None})
        for c in se_calls
    ), f"broadcast_stream_end('sess-B', ...) not found in {se_calls}"
    # The "Stopped by user" marker was streamed.
    marker_calls = [
        c for c in broadcast_token.call_args_list
        if "Stopped by user" in str(c)
    ]
    assert marker_calls, "expected the '⏹ Stopped by user.' marker to be broadcast"


# ──────────────────────────────────────────────────────────────────────────────
# 3. stream_analyst_reply — CancelledError handled, natt_stream_end emitted
# ──────────────────────────────────────────────────────────────────────────────


async def test_stream_analyst_reply_aborts_cleanly_on_cancel() -> None:
    ts = TaskService()  # type: ignore[no-untyped-call]
    started = asyncio.Event()

    async def _slow_analyst(*_a: Any, **_k: Any) -> AsyncIterator[str]:
        started.set()
        await asyncio.sleep(5.0)
        yield "never reaches here"

    broadcast_natt_stream_end = AsyncMock()
    broadcast_natt_token = AsyncMock()

    with patch(
        "agents.analyst.generate_analyst_reply_stream",
        side_effect=lambda *a, **kw: _slow_analyst(*a, **kw),
    ), patch(
        "agents.analyst_context.assemble_analyst_context",
        new=AsyncMock(return_value=""),
    ), patch(
        "core.task_service.vfs_manager.broadcast_natt_stream_end", broadcast_natt_stream_end,
    ), patch(
        "core.task_service.vfs_manager.broadcast_natt_token", broadcast_natt_token,
    ):
        task = asyncio.create_task(ts.stream_analyst_reply("sess-C", "hi", []))
        ts.register_active_task("sess-C", task)
        await started.wait()
        assert ts.abort_session("sess-C") is True
        await asyncio.wait_for(task, timeout=2.0)

    # stream_end fired in the finally — the natt bubble closes cleanly.
    broadcast_natt_stream_end.assert_awaited()


# ──────────────────────────────────────────────────────────────────────────────
# 4. astream_byom — token usage IS recorded into the ledger (the FinOps fix)
# ──────────────────────────────────────────────────────────────────────────────


class _StubDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _StubChoice:
    def __init__(self, content: str | None) -> None:
        self.delta = _StubDelta(content)


class _StubUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _StubChunk:
    def __init__(self, content: str | None = None, usage: _StubUsage | None = None) -> None:
        self.choices = [_StubChoice(content)] if content is not None else []
        self.usage = usage


async def _fake_litellm_acompletion(**_kwargs: Any) -> AsyncIterator[_StubChunk]:
    async def _gen() -> AsyncIterator[_StubChunk]:
        yield _StubChunk(content="hello ")
        yield _StubChunk(content="world")
        yield _StubChunk(usage=_StubUsage(prompt=10, completion=20))
    return _gen()


async def test_astream_byom_records_usage_on_completion() -> None:
    from core.config.byom_config import ModelTarget

    token_ledger.reset()
    snap_before = token_ledger.snapshot()
    assert snap_before["local_tokens"] + snap_before["cloud_tokens"] == 0.0

    stub_target = ModelTarget(
        model="ollama/qwen2.5-coder:7b",
        provider="ollama",
        api_base="http://127.0.0.1:11434",
        api_key="not-needed",
        is_local=True,
    )

    with patch(
        "core.config.model_resolver.get_chat_target", return_value=stub_target,
    ), patch(
        "litellm.acompletion", new=AsyncMock(side_effect=_fake_litellm_acompletion),
    ):
        collected: list[str] = []
        async for delta in LLMGateway.astream_byom(
            [{"role": "user", "content": "x"}], tier="medium", session_id="s1",
        ):
            collected.append(delta)

    assert "".join(collected) == "hello world"
    snap_after = token_ledger.snapshot()
    # ollama → local tier; record_local credits prompt+completion to local_tokens.
    assert snap_after["local_tokens"] - snap_before["local_tokens"] == 30
    token_ledger.reset()


# ──────────────────────────────────────────────────────────────────────────────
# 5. Payload contract round-trip
# ──────────────────────────────────────────────────────────────────────────────


def test_client_abort_mesh_payload_contract_round_trip() -> None:
    evt = ClientAbortMeshEvent(data=ClientAbortMeshPayload(session_id="sess-Z"))
    dumped = evt.model_dump()
    restored = ClientAbortMeshEvent.model_validate(dumped)
    assert restored == evt
    assert restored.event_type == "client_abort_mesh"
    assert restored.data.session_id == "sess-Z"


# ──────────────────────────────────────────────────────────────────────────────
# 6. Stream-resilience (ADR-715) — delivery ACKs + idempotent submit
# ──────────────────────────────────────────────────────────────────────────────


def test_abort_and_hitl_ack_contracts_round_trip() -> None:
    from api.ws_contracts import (
        AbortAckPayload,
        HitlAckPayload,
        ServerAbortAckEvent,
        ServerHitlAckEvent,
    )

    abort = ServerAbortAckEvent(data=AbortAckPayload(session_id="s1", signalled=False))
    assert ServerAbortAckEvent.model_validate(abort.model_dump()) == abort
    assert abort.event_type == "server_abort_ack"

    hitl = ServerHitlAckEvent(data=HitlAckPayload(approval_id="a1", ok=True))
    assert ServerHitlAckEvent.model_validate(hitl.model_dump()) == hitl
    assert hitl.event_type == "server_hitl_ack"


async def test_broadcast_abort_ack_sends_signalled_flag() -> None:
    from unittest.mock import AsyncMock

    from api.websocket_manager import ConnectionManager
    from api.ws_contracts import ServerAbortAckEvent

    mgr = ConnectionManager()  # type: ignore[no-untyped-call]
    sent = AsyncMock()
    with patch.object(mgr, "send_personal_message", sent):
        await mgr.broadcast_abort_ack("sess-X", signalled=False)

    sent.assert_awaited_once()
    assert sent.await_args is not None
    envelope = sent.await_args.args[1]
    assert isinstance(envelope, ServerAbortAckEvent)
    assert envelope.data.session_id == "sess-X"
    assert envelope.data.signalled is False


def test_is_duplicate_request_dedups_within_ttl() -> None:
    import main

    main._recent_request_ids.clear()
    rid = "req-" + "a" * 8
    # First sighting records and is NOT a duplicate; the immediate repeat IS.
    assert main._is_duplicate_request(rid) is False
    assert main._is_duplicate_request(rid) is True
    # A distinct id is independent.
    assert main._is_duplicate_request("req-other") is False
    main._recent_request_ids.clear()


def test_is_duplicate_request_is_size_bounded() -> None:
    import main

    main._recent_request_ids.clear()
    for i in range(main._RECENT_REQUEST_CAP + 50):
        main._is_duplicate_request(f"req-{i}")
    assert len(main._recent_request_ids) <= main._RECENT_REQUEST_CAP
    main._recent_request_ids.clear()


def test_stream_watchdog_ms_is_local_vs_cloud_aware() -> None:
    from unittest.mock import patch as _patch

    from core.config.byom_config import (
        BYOMConfig,
        ModelTarget,
        _WATCHDOG_CLOUD_MS,
        _WATCHDOG_LOCAL_MS,
        stream_watchdog_ms,
    )

    local_cfg = BYOMConfig(
        chat_models={
            "big": ModelTarget(model="ollama/llama3.1", provider="ollama", is_local=True)
        }
    )
    cloud_cfg = BYOMConfig(
        chat_models={
            "big": ModelTarget(model="gpt-4o", provider="openai", is_local=False)
        }
    )
    with _patch("core.config.byom_config.load_byom_config", return_value=local_cfg):
        assert stream_watchdog_ms() == _WATCHDOG_LOCAL_MS
    with _patch("core.config.byom_config.load_byom_config", return_value=cloud_cfg):
        assert stream_watchdog_ms() == _WATCHDOG_CLOUD_MS
