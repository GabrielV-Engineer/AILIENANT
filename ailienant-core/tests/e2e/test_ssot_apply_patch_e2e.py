"""End-to-end: a coding task returns an applied patch over real HTTP/WebSocket.

Traverses the live transport stack — FastAPI app, the WebSocket endpoint, session
multiplexing, the write pipeline (`apply_patch_set`), and the patch-apply ack
handshake — exactly as production does. The heavy cognitive engine is sealed at
the graph-stream boundary (the charter's Gateway pattern): `alienant_app.astream`
is replaced with a deterministic stand-in that yields a final state carrying a
patch, so the test asserts the SSoT *integration* (prompt → graph → WS → applied
patch) without spinning real LLMs.

Synchronous by design: Starlette's TestClient runs the app on its own portal
thread, so the background `process_task` coroutine (scheduled via the portal)
makes progress while this thread drives the socket — no event-loop deadlock.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from brain.state import MissionSpecification, WBSStep
from core.task_service import TaskPayload

_SESSION = "e2e-ssot"
_TARGET = "hello.py"
_CONTENT = "def hello():\n    return 'world'\n"


def _mission() -> MissionSpecification:
    return MissionSpecification(
        outcome="Add a hello function.",
        scope=[_TARGET],
        constraints=[],
        decisions=[],
        tasks=[WBSStep(step_number=1, target_role="architect_refactor",
                       action="write_file", target_file=_TARGET,
                       description="create hello", status="pending")],
        checks=["module imports"],
    )


class _SealedGraph:
    """Deterministic stand-in for the compiled graph's value stream."""

    async def astream(self, state: Any, config: Any = None, stream_mode: Any = None):
        yield {
            "mission_spec": _mission(),
            "session_permission_mode": "auto",   # → ALLOW: applies without HITL
            "pending_patches": {_TARGET: "@@ new file @@\n+def hello(): ..."},
            "pending_contents": {_TARGET: _CONTENT},
            "pending_base_hash": {},
            "errors": [],
            "hitl_pending": False,
        }

    async def aget_state(self, config: Any) -> Any:
        """Satisfy the post-stream interrupt check: no pending interrupts."""
        class _Snap:
            interrupts: list = []
            tasks: list = []
            next: list = []
        return _Snap()


def test_ssot_apply_patch_over_real_http_ws(e2e_client, tmp_path, monkeypatch) -> None:
    import main

    # Real HTTP surface is live.
    assert e2e_client.get("/").status_code == 200

    # Seal the cognitive engine at the graph-stream boundary; everything downstream
    # (write pipeline, WS transport, ack loop) stays real.
    monkeypatch.setattr("brain.engine.alienant_app", _SealedGraph())

    with e2e_client.websocket_connect(f"/api/v1/ws/{_SESSION}") as ws:
        # Announce the session on this physical socket.
        ws.send_json({
            "event_type": "client_register_session",
            "data": {"session_id": _SESSION},
        })

        payload = TaskPayload(
            task_prompt="add a hello function",
            dirty_buffers=[],
            project_id="e2e",
            planner_mode_active=True,          # forces the coding path (no intent LLM)
            execution_mode="auto",            # ALLOW — apply without an approval card
            workspace_root=str(tmp_path),
        )
        # Run the real task pipeline on the app's event loop.
        fut = e2e_client.portal.start_task_soon(
            main.task_service.process_task, _SESSION, payload, "SEQUENTIAL"
        )

        apply_event: Optional[Dict[str, Any]] = None
        for _ in range(80):
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

        # Surface any background failure deterministically rather than hanging.
        fut.result(timeout=30)

    assert apply_event is not None, "never received server_apply_workspace_edit over the WS"
    edits = apply_event["data"]["edits"]
    assert any(e["file_path"].endswith(_TARGET) and "hello" in e["new_content"]
               for e in edits), edits
