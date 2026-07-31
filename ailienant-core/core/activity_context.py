# ailienant-core/core/activity_context.py
#
# Turn-scoped sink for the Glass-Box Timeline's execution-detail channel.
#
# `record_execution` (core/exec_log.py) runs several async call stack layers
# below the coding turn that owns the WebSocket session — inside a LangChain
# tool, inside a LangGraph node. Threading an emitter parameter down to it would
# touch LLM-facing tool signatures for infrastructure the model never needs to
# see. A contextvars.ContextVar propagates through that stack (including tasks
# spawned downstream and the asyncio.to_thread hops the Docker/Wasm sandbox
# tiers use, since each copies the ambient context at creation) without
# touching a single tool signature.
#
# Leaf module: no transport or graph imports, so any layer can import this
# without a cycle.

from __future__ import annotations

import contextvars
import logging
from typing import Optional, Protocol

logger = logging.getLogger("ACTIVITY_CONTEXT")


class ActivitySink(Protocol):
    """Structural type for anything that can receive one execution's timeline
    frames. `core/task_service.py` binds a concrete implementation over the
    turn's `session_id` and `_activity_seq` counter; `core/exec_log.py` only
    needs this narrow surface.
    """

    async def emit_marker(self, *, ref: str, target: Optional[str]) -> None:
        """Push the pre-execution "command" marker (kind="command", ref=ref)."""
        ...

    async def emit_blocked(self, *, target: str) -> None:
        """Push a ref-less "command" marker for an attempt that never reached
        an adapter — a permission-gate denial or a dangerous-pattern intercept.
        No `emit_detail` ever follows (nothing executed), so the frontend
        resolves this node immediately rather than waiting on a body that will
        never arrive. Exists so a caller with no `_narrate` closure in scope
        (a leaf ``BaseTool`` several call-stack layers below the coding turn,
        e.g. ``tools.execution_tools.SandboxBashTool``) can still surface a
        blocked command on the timeline.
        """
        ...

    async def emit_detail(
        self,
        *,
        ref: str,
        source: str,
        cwd: Optional[str],
        initiator: str,
        stdout: Optional[str],
        stderr: Optional[str],
        exit_code: Optional[int],
        duration_ms: Optional[float],
        truncated: bool,
        error: Optional[str],
    ) -> None:
        """Push the post-execution I/O body correlated to the same ref."""
        ...


_current_sink: "contextvars.ContextVar[Optional[ActivitySink]]" = contextvars.ContextVar(
    "ailienant_activity_sink", default=None
)


def bind_activity_sink(sink: ActivitySink) -> contextvars.Token:
    """Bind ``sink`` for the current async context. Returns the reset token —
    the caller MUST reset it in a ``finally`` (charter §5.1) so a turn's sink
    can never leak into the next.
    """
    return _current_sink.set(sink)


def reset_activity_sink(token: contextvars.Token) -> None:
    """Undo `bind_activity_sink`. Idempotent-safe only when called with the
    exact token returned by the matching bind — never call this speculatively.
    """
    _current_sink.reset(token)


def current_activity_sink() -> Optional[ActivitySink]:
    """The bound sink for this async context, or ``None`` outside any turn
    (e.g. the dev-palette `execute_tracked_tool` smoke path, which has no
    turn-scoped `_push_activity` closure to report to — DEBT-122). Callers
    MUST treat ``None`` as "emit nothing", never as an error.
    """
    return _current_sink.get()
