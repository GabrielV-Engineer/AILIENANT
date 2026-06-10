"""Protocol and null implementation for agentic cell event dispatch.

The concrete live dispatcher (LiveCellDispatcher) lives in api.websocket_manager
to keep brain/ free of api/ imports. Test code injects CapturingCellDispatcher
stubs via config["configurable"]["cell_dispatcher"].
"""
from __future__ import annotations

from typing import Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class CellEventDispatcher(Protocol):
    """Async protocol for streaming granular cell events to any observer."""

    async def emit_tool_call_start(
        self, *, iteration: int, tool_name: str, args_scrubbed: Dict[str, str]
    ) -> None: ...

    async def emit_pty_chunk(
        self, *, iteration: int, text: str, is_stderr: bool = False
    ) -> None: ...

    async def emit_ast_diff(
        self, *, iteration: int, path: str, search: str, replace: str
    ) -> None: ...

    async def emit_governor_tick(
        self,
        *,
        step: int,
        cost_usd: float,
        elapsed_s: float,
        axis: Optional[str],
    ) -> None: ...


class NullCellDispatcher:
    """Silent no-op dispatcher — default when no WS session is attached."""

    async def emit_tool_call_start(self, **_: object) -> None:
        pass

    async def emit_pty_chunk(self, **_: object) -> None:
        pass

    async def emit_ast_diff(self, **_: object) -> None:
        pass

    async def emit_governor_tick(self, **_: object) -> None:
        pass
