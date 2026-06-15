# ailienant-core/tools/agent_tools.py
#
# VFS-sandboxed agent tool factories.
#
# Factory functions inject the VFS instance (and, where relevant, the shared
# mutable graph state) via closure so LangChain's @tool schema only exposes the
# model-chosen arguments. No internal service objects (VFSMiddleware, os,
# open(), the state mapping) reach the tool schema.
#
# ``make_read_file_tool`` supports offset/limit pagination plus an optional
# ``record_read`` audit hook the graph node wires to populate
# state["read_files_state"] for the RBWE guard.

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, MutableMapping, Optional, Tuple

from langchain_core.tools import BaseTool, tool

if TYPE_CHECKING:
    from brain.state import VFSFile

logger = logging.getLogger("AGENT_TOOLS")


def make_read_file_tool(
    vfs_read: Callable[[str], Optional[str]],
    *,
    vfs_stat: Optional[Callable[[str], Optional[Tuple[str, str]]]] = None,
    record_read: Optional[Callable[[str, "VFSFile"], None]] = None,
) -> BaseTool:
    """Factory: returns a @tool that reads a VFS file by path.

    Args:
        vfs_read:    Callable e.g. vfs_middleware.read(path) → str | None.
        vfs_stat:    Optional. (path) → (blob_hash, document_version_id) | None.
                     Defaults to computing a blake2b hash + ISO8601 UTC timestamp.
        record_read: Optional audit hook. When provided AND the read succeeds,
                     the factory invokes record_read(path, VFSFile(...)).
                     The graph node wires this to update state["read_files_state"]
                     so the RBWE guard in core/permissions.py sees the entry.
    """
    @tool
    def read_file(
        path: str, offset: int = 0, limit: Optional[int] = None
    ) -> str:
        """Read a file from the VFS. Optional line-based offset/limit pagination."""
        content = vfs_read(path)
        if content is None:
            logger.warning("read_file: '%s' not found in VFS.", path)
            return f"[read_file] ERROR: '{path}' not found in VFS or on disk."

        # Line-based slicing. offset clamped to non-negative; limit None means "all".
        if offset or limit is not None:
            lines = content.splitlines(keepends=True)
            start = max(0, int(offset))
            stop = (start + int(limit)) if limit is not None else None
            content = "".join(lines[start:stop])

        # Audit hook — populates state["read_files_state"] when wired up by the
        # graph node. Failures here are non-fatal: read still returns the bytes.
        if record_read is not None:
            try:
                from brain.state import VFSFile  # local import: avoid module-load cycle

                if vfs_stat is not None:
                    stat_result = vfs_stat(path)
                else:
                    stat_result = None
                if stat_result is None:
                    blob_hash = hashlib.blake2b(
                        content.encode("utf-8", errors="replace"), digest_size=16
                    ).hexdigest()
                    document_version_id = datetime.now(timezone.utc).isoformat()
                else:
                    blob_hash, document_version_id = stat_result

                vfs_file = VFSFile(
                    blob_hash=blob_hash,
                    document_version_id=document_version_id,
                    is_dirty=False,
                )
                record_read(path, vfs_file)
            except Exception as exc:  # noqa: BLE001 — audit hook is best-effort
                logger.warning("read_file: record_read hook failed for %r: %s", path, exc)

        logger.debug("read_file: '%s' read (%d chars).", path, len(content))
        return content

    return read_file


def make_write_file_tool(vfs_write: Callable[[str, str], None]) -> BaseTool:
    """Factory: returns a @tool that writes content to a VFS file.

    Args:
        vfs_write: Callable e.g. vfs_middleware.write(path, content) → None
    """
    @tool
    def write_file(path: str, content: str) -> str:
        """Write content to a file in the Virtual File System."""
        vfs_write(path, content)
        logger.info("write_file: '%s' written (%d chars).", path, len(content))
        return f"[write_file] OK: '{path}' written to VFS."

    return write_file


def make_run_command_tool() -> BaseTool:
    """Factory: returns a @tool stub for shell command execution.

    Raises NotImplementedError to surface the stub clearly; sandboxed subprocess
    execution is provided through the dedicated execution/coder tool paths.
    """
    @tool
    def run_command(command: str) -> str:
        """Execute a shell command in the sandboxed workspace environment."""
        raise NotImplementedError(
            "run_command() stub — use the sandboxed execution/coder tool paths. "
            f"Command attempted: {command!r}"
        )

    return run_command


# ---------------------------------------------------------------------------
# Graph wiring: state-aware FileReadTool that populates the RBWE audit channel
# ---------------------------------------------------------------------------


def make_state_aware_read_file_tool(
    state: MutableMapping[str, Any],
    vfs_read: Callable[[str], Optional[str]],
    *,
    vfs_stat: Optional[Callable[[str], Optional[Tuple[str, str]]]] = None,
) -> BaseTool:
    """Wire a record_read callback that populates state['read_files_state'].

    The rbwe_guard rejects WRITE/EXECUTE/DANGEROUS tools whose target_path is not
    in state['read_files_state']. make_read_file_tool exposes the record_read
    audit hook; this helper builds the closure that actually mutates state,
    completing the wiring.

    Agent / graph-node callers should swap make_read_file_tool(vfs_read) for
    make_state_aware_read_file_tool(state, vfs_read) — the returned BaseTool is
    drop-in compatible (same args_schema, same return shape), but every
    successful read now writes a VFSFile entry into state.
    """
    def _recorder(path: str, vfs_file: "VFSFile") -> None:
        bucket = state.setdefault("read_files_state", {})
        bucket[path] = vfs_file

    return make_read_file_tool(vfs_read, vfs_stat=vfs_stat, record_read=_recorder)


# ---------------------------------------------------------------------------
# State-injecting factories for execution + control tools
# ---------------------------------------------------------------------------


def make_task_create_tool(state: MutableMapping[str, Any]) -> BaseTool:
    """Build a TaskCreateTool bound to state['background_tasks'].

    The factory captures the state dict by reference, so the manager's
    mutations are immediately visible to the orchestrator. LangChain/LangGraph
    hold a strong ref to the tool for the lifetime of the node, which keeps
    the per-manager watcher-task set alive until proc.communicate() returns.
    """
    from tools.execution_tools import BackgroundTaskManager, TaskCreateTool

    registry = state.setdefault("background_tasks", {})
    manager = BackgroundTaskManager(registry)
    return TaskCreateTool(manager=manager)


def make_task_get_tool(state: MutableMapping[str, Any]) -> BaseTool:
    """Build a TaskGetTool bound to state['background_tasks']."""
    from tools.execution_tools import BackgroundTaskManager, TaskGetTool

    registry = state.setdefault("background_tasks", {})
    manager = BackgroundTaskManager(registry)
    return TaskGetTool(manager=manager)


def make_ask_user_question_tool(state: MutableMapping[str, Any]) -> BaseTool:
    """Build an AskUserQuestionTool bound to the shared state mapping.

    The tool mutates state['pending_hitl_request'] on every invocation.
    """
    from tools.control_tools import AskUserQuestionTool

    return AskUserQuestionTool(state=state)


def make_toggle_plan_mode_tool(state: MutableMapping[str, Any]) -> BaseTool:
    """Build a TogglePlanModeTool bound to the shared state mapping.

    The tool mutates state['session_permission_mode'] on every invocation.
    """
    from tools.control_tools import TogglePlanModeTool

    return TogglePlanModeTool(state=state)


# ---------------------------------------------------------------------------
# State-injecting factories for the orchestrator introspection/control tools
# ---------------------------------------------------------------------------


def make_get_wbs_status_tool(state: MutableMapping[str, Any]) -> BaseTool:
    """Build a GetWBSStatusTool bound to the live graph state.

    The returned tool reads state['mission_spec'].tasks on each invocation and
    never mutates state — a read-only WBS progress view.
    """
    from tools.orchestrator_tools import GetWBSStatusTool

    return GetWBSStatusTool(state=state)


def make_emit_hitl_request_tool(state: MutableMapping[str, Any]) -> BaseTool:
    """Build an EmitHITLRequestTool bound to the live graph state.

    The tool appends an idempotent audit entry to state['hitl_approval_requests']
    and returns the canonical HITL gate flag on every invocation.
    """
    from tools.orchestrator_tools import EmitHITLRequestTool

    return EmitHITLRequestTool(state=state)


def build_orchestrator_tools(state: MutableMapping[str, Any]) -> list[BaseTool]:
    """Canonical construction path for the orchestrator-scoped tool set.

    Returns the orchestrator's audited introspection/control tools, each bound to
    the live graph ``state`` handle. This is the single seam a tool-dispatch
    surface (or the external MCP gateway) reuses so the tools are always
    constructed with a live state handle rather than a detached one.
    """
    return [
        make_get_wbs_status_tool(state),
        make_emit_hitl_request_tool(state),
    ]
