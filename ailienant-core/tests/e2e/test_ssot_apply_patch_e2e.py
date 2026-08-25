"""End-to-end: a coding task returns an applied patch over real HTTP/WebSocket.

Traverses the live transport stack — FastAPI app, the WebSocket endpoint, session
multiplexing, the write pipeline (`apply_patch_set`), and the patch-apply ack
handshake — AND, since 13.0.9 moved the whole apply gate (permission verdict,
HITL approval, actuation) into the real compiled graph
(`brain/apply_gate.py`), the real graph itself: planner_agent, apply_patch
(prepare), apply_commit (gate) all run for real. The only sealed boundary is
the two LLM call sites (planner's structured-output call, the coder's
SEARCH/REPLACE generation) — the charter's Gateway pattern, moved DOWN from
the old `alienant_app.astream` seam (which used to fake the whole graph and,
as a direct consequence, never actually exercised anything this file claims
to test) to the actual LLM boundary, so control flow — routing, the
permission gate, and for the HITL test, a REAL native `interrupt()` /
`Command(resume=…)` round trip — all run unfaked.

Synchronous by design: Starlette's TestClient runs the app on its own portal
thread, so the background `process_task` coroutine (scheduled via the portal)
makes progress while this thread drives the socket — no event-loop deadlock.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

_TARGET = "hello.py"
_CONTENT = "def hello():\n    return 'world'\n"

# The coder's own SEARCH/REPLACE wire format (agents/coder.py's
# _parse_search_replace_blocks) — an empty/short SEARCH anchor (<10 chars)
# is the documented "new file / full-content write" convention.
_CODER_STUB_RESPONSE = (
    f"### EDIT {_TARGET}\n"
    "<<<<<<< SEARCH\n"
    "=======\n"
    f"{_CONTENT}"
    ">>>>>>> REPLACE\n"
)

# The planner's real (non-debug) structured-output call — a raw JSON object
# matching MissionSpecification (agents/planner.py's own schema; unrecognized
# extra fields are absent here, only what the model requires). The built-in
# AILIENANT_PLANNER_DEBUG stub is NOT used for the planner in this file: it
# hardcodes a fixed "read_file main.py" WBS (agents/planner.py's DEBUG_MODE
# branch — meant for validating graph routing, not for choosing what the plan
# actually does), which can never exercise the FILE_WRITE apply-gate path
# this test exists to certify.
_PLANNER_STUB_JSON = (
    '{"outcome": "Add a hello function.", "scope": ["hello.py"], '
    '"constraints": [], "decisions": [], '
    '"tasks": [{"step_number": 1, "target_role": "core_dev", "action": "write_file", '
    '"target_file": "hello.py", "description": "Create the hello function."}], '
    '"checks": ["module imports"]}'
)


def _mock_litellm_response(content: str = "{}") -> MagicMock:
    """Minimal ModelResponse-shaped stand-in — mirrors the same helper shape
    tests/test_llm_gateway_timeout.py already uses to mock litellm.acompletion."""
    resp = MagicMock()
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=1)
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_litellm_embedding_response() -> MagicMock:
    """Minimal shape for litellm.aembedding — core/memory/trajectory_memory.py's
    _get_embedding reads resp.data[0].embedding (or the dict-shaped equivalent).
    1536 dims matches the real embedding table's fixed-size column so the
    (non-fatal either way) TrajectoryMemory write doesn't also log a Lance
    cast-error warning on top of being a stub."""
    resp = MagicMock()
    resp.data = [MagicMock(embedding=[0.0] * 1536)]
    return resp


async def _acomplete_with_thinking_stub(*_args: Any, **kwargs: Any) -> str:
    """Dispatches by response_format — the ONE signal that reliably tells the
    planner's structured-output call (response_format={"type":"json_object"})
    apart from the coder's free-text SEARCH/REPLACE call (no response_format
    at all); both real call sites go through this exact same LLMGateway
    method (agents/planner.py and agents/coder.py), so a caller-blind stub
    would have to guess."""
    if kwargs.get("response_format"):
        return _PLANNER_STUB_JSON
    return _CODER_STUB_RESPONSE


def _seals(monkeypatch: Any) -> None:
    """Seal every LLM/retrieval call site the coding path can reach; only
    graph control flow (routing, permission gate, apply gate, actuation) runs
    for real.

    Planner: the built-in AILIENANT_PLANNER_DEBUG stub is deliberately NOT
    used — it hardcodes a fixed "read_file main.py" WBS (agents/planner.py's
    DEBUG_MODE branch, meant for validating graph routing, not for choosing
    what the plan actually does), which can never exercise the FILE_WRITE
    apply-gate path this test exists to certify. Instead, the planner takes
    its REAL code path and its structured-output call
    (LLMGateway.acomplete_with_thinking, response_format=json_object) is
    intercepted directly — see _acomplete_with_thinking_stub above.

    Researcher: the project's own existing debug stub
    (AILIENANT_RESEARCHER_DEBUG), the established pattern for a hermetic
    real-graph run (see tests/test_engine_respine.py) — DEBUG_MODE is read
    into a module constant at import time, so the env var alone would not
    take effect here; patch the resolved attribute directly, same as that
    file does.

    Coder + planner generation: LLMGateway.acomplete_with_thinking is patched
    directly (see _acomplete_with_thinking_stub) — this intercepts ABOVE
    litellm entirely, so neither call ever reaches the network-level mock
    below at all.

    litellm.acompletion (blanket safety net): the coder's own grounding
    pre-pass (agents/coder.py::_run_grounding_loop, which fires unconditionally
    for a brand-new file — exactly this test's scenario) makes its OWN
    separate LLM call via core.tool_dispatch.make_gateway_reasoner, entirely
    independent of acomplete_with_thinking. Patching litellm.acompletion
    directly closes every such path at once, including the fire-and-forget
    Coder Companion explanation and any other chat-completion caller this
    graph run touches — a single choke point every LLMGateway chat method
    funnels through (tools/llm_gateway.py). A bare "{}" is what the grounding
    reasoner's own prompt asks for to end its tool loop with no further calls.

    litellm.aembedding: a SEPARATE top-level litellm function
    (core/memory/trajectory_memory.py's _get_embedding, invoked once per
    coder step to record the step's trajectory) — NOT covered by the
    acompletion seal above. Missing this was a real, live-reproduced hang:
    with no BYOM preset configured in this test environment, litellm
    couldn't resolve a provider from the raw embedding-model string and
    retried internally for well over a minute before ever reaching
    TrajectoryMemory's own non-fatal exception handler.
    """
    import agents.researcher as researcher_mod

    monkeypatch.setattr(researcher_mod, "DEBUG_MODE", True)
    monkeypatch.setattr(
        "tools.llm_gateway.LLMGateway.acomplete_with_thinking",
        _acomplete_with_thinking_stub,
    )
    monkeypatch.setattr(
        "litellm.acompletion", AsyncMock(return_value=_mock_litellm_response()),
    )
    monkeypatch.setattr(
        "litellm.aembedding", AsyncMock(return_value=_mock_litellm_embedding_response()),
    )


def test_ssot_apply_patch_over_real_http_ws(e2e_client, tmp_path, monkeypatch) -> None:
    """Auto mode: ALLOW, no interrupt — the graph proposes and applies the
    step in one continuous run, exactly as production does for Auto."""
    import main

    _seals(monkeypatch)
    session_id = "e2e-ssot-auto"

    assert e2e_client.get("/").status_code == 200

    with e2e_client.websocket_connect(f"/api/v1/ws/{session_id}") as ws:
        ws.send_json({
            "event_type": "client_register_session",
            "data": {"session_id": session_id},
        })

        from core.task_service import TaskPayload
        payload = TaskPayload(
            # Distinct project_id per test — project-scoped stores (TrajectoryMemory,
            # semantic_memory) are file-backed and keyed by it; sharing one across
            # tests produced a cross-test WebSocketDisconnect on the second test's
            # connection, live-reproduced when both tests ran in the same session.
            task_prompt="add a hello function", dirty_buffers=[], project_id="e2e-auto",
            planner_mode_active=False, execution_mode="automatic",
            workspace_root=str(tmp_path),
        )
        fut = e2e_client.portal.start_task_soon(
            main.task_service.process_task, session_id, payload, "SEQUENTIAL"
        )

        apply_event: Optional[Dict[str, Any]] = None
        for _ in range(200):
            msg = ws.receive_json()
            if msg.get("event_type") == "server_apply_workspace_edit":
                apply_event = msg
                ws.send_json({
                    "event_type": "client_patch_applied",
                    "data": {
                        "patch_id": msg["data"]["patch_id"],
                        "ok": True,
                        "applied_files": [_TARGET],
                    },
                })
                break

        fut.result(timeout=30)

    assert apply_event is not None, "never received server_apply_workspace_edit over the WS"
    edits = apply_event["data"]["edits"]
    assert any(e["file_path"].endswith(_TARGET) for e in edits), edits


def test_ssot_apply_patch_via_real_interrupt_and_resume(e2e_client, tmp_path, monkeypatch) -> None:
    """Ask mode: the exact round trip 13.0.9 exists to protect — a genuine
    native interrupt() suspends the graph (server_hitl_approval_request
    arrives with a real diff, closing the DEBT-190/191-adjacent bug where
    _emit_interrupt_card used to hardcode proposed_files=None), the client
    replies over client_hitl_response, and ONLY THEN does
    server_apply_workspace_edit fire — proving the interrupt genuinely
    suspended the run rather than the old post-graph replay silently
    re-running the whole WBS regardless of the card."""
    import main

    _seals(monkeypatch)
    session_id = "e2e-ssot-ask"

    with e2e_client.websocket_connect(f"/api/v1/ws/{session_id}") as ws:
        ws.send_json({
            "event_type": "client_register_session",
            "data": {"session_id": session_id},
        })

        from core.task_service import TaskPayload
        payload = TaskPayload(
            task_prompt="add a hello function", dirty_buffers=[], project_id="e2e-ask",
            planner_mode_active=False, execution_mode="ask_before_edits",
            workspace_root=str(tmp_path),
        )
        fut = e2e_client.portal.start_task_soon(
            main.task_service.process_task, session_id, payload, "SEQUENTIAL"
        )

        approval_event: Optional[Dict[str, Any]] = None
        apply_event: Optional[Dict[str, Any]] = None
        for _ in range(200):
            msg = ws.receive_json()
            kind = msg.get("event_type")
            if kind == "server_hitl_approval_request":
                approval_event = msg
                break
            if kind == "server_apply_workspace_edit":
                # Must never happen before the card — a native interrupt that
                # didn't actually suspend the run is exactly the DEBT-185-class
                # bug this split (prepare/commit) exists to prevent.
                raise AssertionError(
                    "server_apply_workspace_edit arrived before any HITL card "
                    "— the interrupt did not suspend the graph"
                )

        assert approval_event is not None, "never received server_hitl_approval_request over the WS"
        data = approval_event["data"]
        assert data["request_kind"] == "FILE_WRITE"
        proposed = data.get("proposed_files")
        assert proposed and len(proposed) == 1, (
            "the approval card carried no diff — _emit_interrupt_card regressed "
            "back to hardcoding proposed_files=None"
        )
        assert proposed[0]["file_path"].endswith(_TARGET)
        assert proposed[0]["unified_diff"]

        # `fut` (process_task) is only the PRE-PAUSE half of this turn — a
        # native interrupt() makes _run_coding_task return once the graph is
        # paused, well before any resume. Confirm THAT half completed cleanly
        # (a real error here — not a pause — would otherwise surface as a
        # confusing timeout later) before moving on to the resume, which runs
        # as a SEPARATE, untracked asyncio.create_task inside main.py's
        # client_hitl_response handler — awaiting `fut` again after resuming
        # would be waiting on a future that already finished, which is
        # exactly what produced an unrelated anyio cross-task CancelScope
        # error the first time this test was written naively.
        fut.result(timeout=30)

        # Resume over the real WS transport — the production path, not a
        # direct task_service call.
        ws.send_json({
            "event_type": "client_hitl_response",
            "data": {
                "approval_id": data["approval_id"],
                "approved": True,
                "session_id": session_id,
            },
        })

        for _ in range(200):
            msg = ws.receive_json()
            if msg.get("event_type") == "server_apply_workspace_edit":
                apply_event = msg
                ws.send_json({
                    "event_type": "client_patch_applied",
                    "data": {
                        "patch_id": msg["data"]["patch_id"],
                        "ok": True,
                        "applied_files": [_TARGET],
                    },
                })
                break

        # apply_event, captured over the real WS stream above, is the
        # authoritative completion signal for the resumed half of the turn —
        # there is no separate future to await for it.

    assert apply_event is not None, "never received server_apply_workspace_edit after resuming"
    edits = apply_event["data"]["edits"]
    assert any(e["file_path"].endswith(_TARGET) for e in edits), edits
