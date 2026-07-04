# ailienant-core/brain/agentic_cell.py
"""Autonomous ReAct execution cell.

A bounded sub-loop the LLM drives over a live, persistent terminal:
``run a command -> read the structured verdict -> reason -> edit -> re-run`` until the
verdict is green or the iteration budget is spent — all within a single turn, with no
re-submission. Each visit to ``run_agentic_cell_node`` is exactly one ReAct iteration;
the conditional loop-back edge (``route_after_cell``) makes every iteration a graph
super-step boundary, so each one leaves a Rewind-able checkpoint and a trajectory record.

The cell exposes three strict-schema tools to the model — ``run_terminal`` (dispatches over
the persistent session and returns *structured* diagnostics, never raw stdout),
``read_file_ast`` (AST skeleton, not the full file), and ``apply_granular_edit`` (the
SEARCH/REPLACE format applied transactionally with an optimistic-concurrency guard). It
coexists with the one-shot coder path: only steps the planner flags as needing iteration
are routed here; everything else keeps the simpler, cheaper graph path.

When a single reasoning turn proposes two or more competing edits for the same file, a
contained Monte-Carlo tree governs the choice: each candidate is evaluated over the shared
work surface transactionally (push -> verify -> roll the surface back to the clean base
before the next candidate), scored by its own structured verdict, and the UCB1 winner is
committed. The linear single-edit path never pays this cost.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from brain.cell_dispatcher import CellEventDispatcher, NullCellDispatcher
from brain.iteration_governor import AxisExhausted, check_governor, estimate_iteration_cost
from brain.retry_policy import (
    AGENTIC_CELL_MAX_COST_USD,
    AGENTIC_CELL_MAX_ELAPSED_S,
    AGENTIC_CELL_MAX_ITERATIONS,
)
from brain.state import VFSFile
from brain.subagent_tournament import _content_to_vfs
from brain.subagent_tournament import run_tournament as select_candidate_via_mcts

logger = logging.getLogger("AGENTIC_CELL")

# Same text ceiling the VFS firewall applies; surface files larger than this are
# never decoded into the cell's working set.
_MAX_EDIT_FILE_BYTES: int = 500 * 1024
# Per-command wall-clock ceiling inside the session; a hung command is killed and
# surfaces as a non-zero verdict rather than blocking the loop.
_RUN_TERMINAL_TIMEOUT_S: float = 120.0


# =====================================================================
# Tool schemas (strict JSON contracts bound to the model)
# =====================================================================
# The model receives these as native tool definitions. Execution is dispatched by the
# cell against its own session/surface/VFS closure (the args carry no privileged handle),
# mirroring how the coder parses SEARCH/REPLACE rather than trusting a free-form payload.

class RunTerminalArgs(BaseModel):
    """Execute a shell command on the session's work surface."""

    command: str = Field(
        description="The exact shell command to run (e.g. 'python -m pytest -q'). "
        "Output is returned as a structured diagnostic summary, not raw stdout."
    )


class ReadFileAstArgs(BaseModel):
    """Read a file as an AST skeleton (signatures + docstrings, bodies elided)."""

    path: str = Field(description="Workspace-relative path of the file to inspect.")


class ApplyGranularEditArgs(BaseModel):
    """Apply a single SEARCH/REPLACE edit to a file under an OCC guard."""

    path: str = Field(description="Workspace-relative path of the file to edit.")
    search: str = Field(
        description="Verbatim anchor copied from the current file content. Leave empty "
        "to create a new file (full content goes in 'replace')."
    )
    replace: str = Field(description="Replacement text written verbatim between the markers.")


# The contract surface bound to a tool-calling model. ``bind_cell_tools`` is the single
# seam; it accepts any LangChain chat model exposing ``bind_tools`` and returns it bound.
CELL_TOOLS: List[type[BaseModel]] = [RunTerminalArgs, ReadFileAstArgs, ApplyGranularEditArgs]


def bind_cell_tools(llm: Any) -> Any:
    """Bind the three cell tools to a tool-calling chat model. Returns it unchanged
    when the model does not expose ``bind_tools`` (degraded/gateway path)."""
    binder = getattr(llm, "bind_tools", None)
    if binder is None:
        return llm
    return binder(CELL_TOOLS)


# =====================================================================
# Reasoner abstraction
# =====================================================================
# One reasoning turn maps a message history to an ordered list of tool calls. The default
# is gateway-backed; tests (and any non-tool-calling backend) inject a deterministic
# reasoner via config["configurable"]["cell_reasoner"]. Keeping the model interaction
# behind this seam makes the loop's control flow fully testable without a live LLM.

@dataclass(frozen=True)
class ToolCall:
    """A single model-proposed tool invocation."""

    name: str
    args: Dict[str, Any]


CellReasoner = Callable[[Sequence[Dict[str, str]]], Awaitable[List[ToolCall]]]


async def _default_reasoner(messages: Sequence[Dict[str, str]]) -> List[ToolCall]:
    """Gateway-backed reasoning: ask the model for a strict-JSON tool-call envelope.

    The project's gateway returns text (litellm ``ModelResponse``), so — like the coder's
    SEARCH/REPLACE parsing — tool intent is carried in a small JSON object the model emits
    and we parse here. Best-effort: any parse/transport failure yields an empty turn, which
    the caller treats as a graceful concede rather than a crash.
    """
    import json

    from shared.config import MODEL_BIG
    from tools.llm_gateway import LLMGateway

    schema_hint = (
        "Respond with ONLY a JSON object of the form "
        '{"tool_calls":[{"name":"run_terminal","args":{"command":"..."}}]}. '
        "Available tools: run_terminal(command), read_file_ast(path), "
        "apply_granular_edit(path, search, replace)."
    )
    convo: List[Dict[str, Any]] = [{"role": "system", "content": schema_hint}, *messages]
    try:
        response = await LLMGateway.ainvoke(messages=convo, model=MODEL_BIG, temperature=0.0)
        text = response.choices[0].message.content or ""
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return []
        envelope = json.loads(text[start : end + 1])
        calls: List[ToolCall] = []
        for raw in envelope.get("tool_calls", []):
            name = raw.get("name")
            args = raw.get("args")
            if isinstance(name, str) and isinstance(args, dict):
                calls.append(ToolCall(name=name, args=args))
        return calls
    except Exception as exc:  # noqa: BLE001 — a reasoning failure is a soft concede
        logger.warning("AgenticCell default reasoner failed: %s", exc)
        return []


# =====================================================================
# Tool-arg auditing (prompt-injection defense)
# =====================================================================

@dataclass
class _AuditOutcome:
    allowed: bool
    reason: str
    entry: Dict[str, Any]


def audit_tool_args(tool_name: str, args: Dict[str, Any]) -> _AuditOutcome:
    """Audit a model-proposed tool call before it executes.

    - ``run_terminal`` commands pass through the DANGEROUS interceptor; a hit is rejected.
    - Every argument is secret-scrubbed before it is recorded, so the audit ledger never
      persists credentials a prompt-injection attempt may have smuggled into an argument.

    Returns the decision plus a scrubbed, append-ready audit entry.
    """
    from shared.logging_filters import SecretsScrubber

    scrubbed: Dict[str, str] = {
        key: SecretsScrubber.scrub(str(value)) for key, value in args.items()
    }
    allowed = True
    reason = "allowed"
    if tool_name == "run_terminal":
        from tools.execution_tools import _match_dangerous

        hit = _match_dangerous(str(args.get("command", "")))
        if hit is not None:
            allowed = False
            reason = f"dangerous_command:{hit}"
    entry: Dict[str, Any] = {
        "tool": tool_name,
        "args": scrubbed,
        "decision": "allow" if allowed else "reject",
        "reason": reason,
    }
    return _AuditOutcome(allowed=allowed, reason=reason, entry=entry)


# =====================================================================
# Session registry (one persistent shell per task, leak-safe)
# =====================================================================

@dataclass
class _CellSession:
    """A task's persistent session plus a background collector draining its stream."""

    session: Any
    surface: Any
    buffer: bytearray = field(default_factory=bytearray)
    collector: Optional["asyncio.Task[None]"] = None
    last_snapshot: Any = None  # WorkspaceSnapshot of the most recent push
    start_time: float = field(default_factory=time.monotonic)  # monotonic clock at session open
    _chunk_hook: Optional[Callable[[bytes], Awaitable[None]]] = field(default=None)


_session_registry: Dict[str, _CellSession] = {}


async def _collect_into(cell: _CellSession) -> None:
    """Drain the session stream into the cell buffer for the session's lifetime."""
    try:
        async for chunk in cell.session.stream():
            cell.buffer.extend(chunk)
            hook = cell._chunk_hook
            if hook is not None:
                try:
                    await hook(chunk)
                except Exception:  # noqa: BLE001 — hook errors must never crash the collector
                    pass
    except Exception as exc:  # noqa: BLE001 — collector death must not crash the loop
        logger.debug("AgenticCell stream collector ended: %s", exc)


async def _close_cell(task_id: str) -> None:
    """Tear down a task's session and drop its registry entry. Idempotent."""
    cell = _session_registry.pop(task_id, None)
    if cell is None:
        return
    if cell.collector is not None:
        cell.collector.cancel()
    try:
        await cell.session.close()
    except Exception as exc:  # noqa: BLE001 — teardown is best-effort
        logger.debug("AgenticCell session close failed for %s: %s", task_id, exc)


async def sweep_orphaned_sessions(live_task_ids: Sequence[str]) -> int:
    """Close any registered session whose task is no longer live; return the count.

    The cleanup of last resort for runs aborted mid-loop (the Stop button never reaches the
    node's ``finally``). The turn-level ``try/finally`` already covers normal terminal exits;
    this sweep is the safety net for cancellation.
    """
    # TODO: tie this sweep to the LangGraph Run lifecycle / WS disconnect collector
    # so an aborted run is reaped immediately rather than on the next sweep tick.
    live = set(live_task_ids)
    orphans = [tid for tid in list(_session_registry) if tid not in live]
    for tid in orphans:
        await _close_cell(tid)
    return len(orphans)


async def write_session_stdin(session_id: str, data: bytes) -> bool:
    """Feed a line of stdin to a session's live terminal. Returns False if no session.

    Lets the user answer a blocking prompt on a persistent session. Best-effort: a
    write failure (session torn down mid-keystroke) is logged, never raised.
    """
    cell = _session_registry.get(session_id)
    if cell is None:
        return False
    try:
        await cell.session.write_stdin(data)
        return True
    except Exception as exc:  # noqa: BLE001 — a dead session must not crash the WS loop
        logger.debug("AgenticCell stdin write failed for %s: %s", session_id, exc)
        return False


async def interrupt_session(session_id: str) -> bool:
    """Send an interrupt (Ctrl-C) to a session's foreground process. False if none.

    Used by the Stop control to signal the live command before the run is cancelled.
    Best-effort: an interrupt failure is logged, never raised.
    """
    cell = _session_registry.get(session_id)
    if cell is None:
        return False
    try:
        await cell.session.interrupt()
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort signal
        logger.debug("AgenticCell interrupt failed for %s: %s", session_id, exc)
        return False


# =====================================================================
# Surface helpers
# =====================================================================

async def _run_on_surface(cell: _CellSession, command: str, timeout_s: float) -> Tuple[int, str]:
    """Run a command on the persistent session and return (exit_code, combined_output).

    Output is captured by slicing the background collector's buffer around the run, so a
    persistent multi-command session needs no per-command teardown.
    """
    start = len(cell.buffer)
    try:
        exit_code = await cell.session.run(command, timeout_s=timeout_s)
    except asyncio.TimeoutError:
        await cell.session.kill()
        return -1, "[command timed out]"
    # Let the background collector drain any trailing demux deltas into the buffer.
    # Yield until the buffer stops growing (bounded), so per-command output is captured
    # without a fixed sleep racing the collector.
    prev = -1
    for _ in range(64):
        if len(cell.buffer) == prev:
            break
        prev = len(cell.buffer)
        await asyncio.sleep(0)
    output = bytes(cell.buffer[start:]).decode("utf-8", errors="replace")
    return exit_code, output


# =====================================================================
# The node
# =====================================================================

def _concede(state: Dict[str, Any], iteration: int, reason: str) -> Dict[str, Any]:
    """Terminal delta that exits the loop with an honest record (never an exception)."""
    return {
        "agentic_iteration": iteration + 1,
        "agentic_trajectory": [
            {"iteration": iteration, "status": "concede", "reason": reason, "exit_code": None}
        ],
        "errors": [f"AgenticCell conceded: {reason}"],
    }


async def run_agentic_cell_node(
    state: Dict[str, Any], config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """One ReAct iteration. See module docstring for the loop contract."""
    from core.blob_storage import blob_storage
    from core.workspace_sync import push_vfs_to_surface, pull_surface_to_vfs

    task_id: str = str(state.get("task_id", ""))
    iteration: int = int(state.get("agentic_iteration", 0))
    cwd: str = str(state.get("workspace_root") or os.getcwd())
    configurable: Dict[str, Any] = (config or {}).get("configurable", {}) if config else {}
    reasoner: CellReasoner = configurable.get("cell_reasoner") or _default_reasoner
    dispatcher: CellEventDispatcher = configurable.get("cell_dispatcher") or NullCellDispatcher()

    terminal = False
    cell: Optional[_CellSession] = None
    try:
        # ── Resolve / reuse the persistent session for this task ─────────────────
        cell = _session_registry.get(task_id)
        if cell is None:
            adapter = configurable.get("cell_adapter")
            if adapter is None:
                from core.sandbox import get_active_adapter

                adapter = get_active_adapter()
            if adapter is None or not getattr(adapter, "supports_sessions", False):
                terminal = True
                return _concede(state, iteration, "no session-capable sandbox adapter")

            from tools.execution_tools import _sandbox_env

            session = await adapter.open_session(cwd=cwd, env_whitelist=_sandbox_env())
            surface = adapter.get_sync_surface(cwd)
            cell = _CellSession(session=session, surface=surface)
            cell.collector = asyncio.ensure_future(_collect_into(cell))
            _session_registry[task_id] = cell

        # ── Exec-approval phase ──────────────────────────────────────────────────
        # A prior iteration deferred a HITL-gated command. interrupt() is the FIRST
        # action here (only the idempotent session-reuse precedes it), so a replay
        # before resume is side-effect-free and the command runs exactly once after
        # the operator approves.
        pending_cmd: Optional[str] = state.get("pending_exec_command")
        if pending_cmd:
            approved = await _approve_exec(state, pending_cmd, configurable)
            rec: Dict[str, Any] = {
                "iteration": iteration, "edits": [], "occ_conflicts": [],
                "exit_code": None, "diagnostics": "",
            }
            if not approved:
                rec["status"] = "continue"
                return {
                    "agentic_iteration": iteration + 1,
                    "agentic_trajectory": [
                        rec,
                        {"role": "system", "content": (
                            "Tool denied: the run_terminal command was not approved by the "
                            "operator. Choose a different approach.")},
                    ],
                    "pending_exec_command": None,
                    "security_flags": ["EXECUTE_TIER_HITL_DENIED"],
                }
            # Approved → push the committed edits (idempotent full-content), run once, pull.
            ef_vfs: Dict[str, VFSFile] = dict(state.get("vfs_buffer") or {})
            ef_vids: Dict[str, str] = {p: vf.document_version_id for p, vf in ef_vfs.items()}
            cell.last_snapshot = await push_vfs_to_surface(
                cell.surface, ef_vfs, blob_storage, ef_vids
            )
            _ei = iteration
            cell._chunk_hook = lambda chunk, _i=_ei: dispatcher.emit_pty_chunk(
                iteration=_i, text=chunk.decode("utf-8", errors="replace")
            )
            try:
                exit_code, output = await _run_on_surface(
                    cell, pending_cmd, _RUN_TERMINAL_TIMEOUT_S
                )
            finally:
                cell._chunk_hook = None
            rec["exit_code"] = exit_code
            rec["diagnostics"] = _structured_verdict(pending_cmd, output)
            new_files, conflicts, _deleted = await pull_surface_to_vfs(
                cell.surface, cell.last_snapshot, ef_vids, blob_storage
            )
            occ_msgs: List[Dict[str, str]] = [_occ_diagnostic(p) for p in conflicts]
            for p in conflicts:
                rec["occ_conflicts"].append(p)
            success = exit_code == 0
            rec["status"] = "green" if success else "continue"
            terminal = success
            ef_delta: Dict[str, Any] = {
                "agentic_iteration": iteration + 1,
                "agentic_trajectory": [rec, *occ_msgs],
                "pending_exec_command": None,
            }
            if new_files:
                ef_delta["vfs_buffer"] = {p: vf for p, vf in new_files.items()}
            return ef_delta

        # ── Working VFS for this turn (clean base for transactional MCTS) ────────
        vfs_files: Dict[str, VFSFile] = dict(state.get("vfs_buffer") or {})
        version_ids: Dict[str, str] = {
            p: vf.document_version_id for p, vf in vfs_files.items()
        }
        working: Dict[str, str] = {}
        for path, vf in vfs_files.items():
            content = blob_storage.get(vf.blob_hash)
            if content is not None and len(content.encode("utf-8")) <= _MAX_EDIT_FILE_BYTES:
                working[path] = content
        clean_base_content: Dict[str, str] = dict(working)

        cell.last_snapshot = await push_vfs_to_surface(
            cell.surface, vfs_files, blob_storage, version_ids
        )

        # ── Reason (cache bypassed — never probe; each iteration is fresh) ───────
        messages = _build_messages(state)
        tool_calls = await reasoner(messages)

        record: Dict[str, Any] = {
            "iteration": iteration,
            "edits": [],
            "occ_conflicts": [],
            "exit_code": None,
            "diagnostics": "",
        }
        audit_entries: List[Dict[str, Any]] = []
        security_flags: List[str] = []
        occ_messages: List[Dict[str, str]] = []
        pending_contents: Dict[str, str] = {}
        candidate_edits: Dict[str, List[str]] = {}  # path -> competing replacement contents
        edited_paths: List[str] = []
        last_exit: Optional[int] = None
        mission_state = state.get("mission_spec")

        async def _verify(cmd: str) -> Tuple[int, str]:
            code, out = await _run_on_surface(cell, cmd, _RUN_TERMINAL_TIMEOUT_S)
            return code, _structured_verdict(cmd, out)

        # ── Dispatch each proposed tool call, in the order the model emitted them ─
        # Edits land in the working set and are flushed to the surface before any
        # run_terminal, so a same-turn "edit then test" sees the edited files.
        for call in tool_calls:
            audit = audit_tool_args(call.name, call.args)
            audit_entries.append(audit.entry)
            await dispatcher.emit_tool_call_start(
                iteration=iteration,
                tool_name=call.name,
                args_scrubbed={k: str(v) for k, v in audit.entry.get("args", {}).items()},
            )
            if not audit.allowed:
                security_flags.append("SECURITY_TOOL_ARG_REJECTED")
                continue

            if call.name == "run_terminal":
                command = str(call.args.get("command", ""))
                verdict = _classify_execute(state)
                if verdict == "allow":
                    # YOLO Guard: upgrade to HITL for high-risk commands even in
                    # permissive session modes (FULL_AUTO / STANDARD).
                    from core.permissions import (
                        PermissionDecision,
                        risk_intercept_guard,
                        session_mode_from_channel,
                    )
                    _cell_mode = session_mode_from_channel(state.get("session_permission_mode"))
                    _eff, _ = risk_intercept_guard(command, PermissionDecision.ALLOW, _cell_mode)
                    if _eff is PermissionDecision.HITL:
                        verdict = "hitl"
                if verdict == "deny":
                    security_flags.append("EXECUTE_TIER_DENIED")
                    continue
                if verdict == "hitl":
                    # Defer to the interrupt-first exec-approval phase (next super-step):
                    # do NOT run the command, and stop processing further calls so no side
                    # effect precedes the approval. Edits computed so far are committed in
                    # this delta and re-pushed (idempotent full-content) on re-entry; the
                    # 'continue' status routes the cell back to itself for the gate.
                    defer_rec: Dict[str, Any] = {
                        "iteration": iteration, "edits": list(record["edits"]),
                        "occ_conflicts": [], "exit_code": None, "diagnostics": "",
                        "status": "continue",
                    }
                    defer_delta: Dict[str, Any] = {
                        "agentic_iteration": iteration + 1,
                        "agentic_trajectory": [defer_rec],
                        "pending_exec_command": command,
                    }
                    if edited_paths:
                        defer_delta["vfs_buffer"] = {p: vfs_files[p] for p in edited_paths}
                    if pending_contents:
                        defer_delta["pending_contents"] = pending_contents
                    if audit_entries:
                        defer_delta["permission_audit_log"] = audit_entries
                    if security_flags:
                        defer_delta["security_flags"] = security_flags
                    return defer_delta
                cell.last_snapshot = await push_vfs_to_surface(
                    cell.surface, vfs_files, blob_storage, version_ids
                )
                _iter = iteration  # capture for the lambda closure
                cell._chunk_hook = lambda chunk, _i=_iter: dispatcher.emit_pty_chunk(
                    iteration=_i,
                    text=chunk.decode("utf-8", errors="replace"),
                )
                try:
                    exit_code, output = await _run_on_surface(
                        cell, command, _RUN_TERMINAL_TIMEOUT_S
                    )
                finally:
                    cell._chunk_hook = None
                last_exit = exit_code
                record["exit_code"] = exit_code
                record["diagnostics"] = _structured_verdict(command, output)
                # Pull surface deltas back; an OCC conflict becomes a system diagnostic the
                # model sees next iteration (so it re-reads instead of looping blindly).
                new_files, conflicts, _deleted = await pull_surface_to_vfs(
                    cell.surface, cell.last_snapshot, version_ids, blob_storage
                )
                for path, vf in new_files.items():
                    vfs_files[path] = vf
                    version_ids[path] = vf.document_version_id
                    working[path] = blob_storage.get(vf.blob_hash) or working.get(path, "")
                for path in conflicts:
                    record["occ_conflicts"].append(path)
                    occ_messages.append(_occ_diagnostic(path))

            elif call.name == "read_file_ast":
                path = str(call.args.get("path", ""))
                _ = _read_file_ast(working.get(path, ""), path)

            elif call.name == "apply_granular_edit":
                path = str(call.args.get("path", ""))
                search = str(call.args.get("search", ""))
                replace = str(call.args.get("replace", ""))
                # A second edit to the same path in one turn is a competing candidate:
                # compute it from the clean base, not from the first edit's result.
                base = clean_base_content.get(path, "") if path in candidate_edits else working.get(path, clean_base_content.get(path, ""))
                try:
                    new_content = _compute_edit(base, path, search, replace)
                except _StaleEdit:
                    record["occ_conflicts"].append(path)
                    occ_messages.append(_occ_diagnostic(path))
                    continue
                await dispatcher.emit_ast_diff(
                    iteration=iteration, path=path, search=search, replace=replace
                )
                candidate_edits.setdefault(path, []).append(new_content)
                if path not in edited_paths:
                    edited_paths.append(path)
                record["edits"].append(path)
                # Single-candidate: commit immediately into the working set + VFS.
                working[path] = new_content
                vfs_files[path] = _content_to_vfs({path: new_content}, blob_storage)[path]
                version_ids[path] = vfs_files[path].document_version_id
                pending_contents[path] = new_content

        # ── Branch governance: >=2 competing candidates for one file → MCTS ──────
        for path, replacements in candidate_edits.items():
            if len(replacements) < 2:
                continue
            candidates = [{**clean_base_content, path: rep} for rep in replacements]
            _winner_idx, winner = await select_candidate_via_mcts(
                surface=cell.surface,
                clean_base_content=clean_base_content,
                candidates=candidates,
                verify_command=configurable.get("cell_verify_command", "python -m pytest -q"),
                run_verify=_verify,
                blob_store=blob_storage,
                mission_state=mission_state,
            )
            working[path] = winner[path]
            vfs_files[path] = _content_to_vfs({path: winner[path]}, blob_storage)[path]
            version_ids[path] = vfs_files[path].document_version_id
            pending_contents[path] = winner[path]

        # ── Decide terminal condition (three-axis governor) ──────────────────────
        success = last_exit == 0
        cost_delta = estimate_iteration_cost(messages, tool_calls)
        axis: Optional[AxisExhausted] = None
        if not success:
            axis = check_governor(
                step=iteration + 1,
                cost_usd=float(state.get("current_cost_usd", 0.0)) + cost_delta,
                elapsed_s=time.monotonic() - cell.start_time,
                max_steps=int(configurable.get("cell_max_steps", AGENTIC_CELL_MAX_ITERATIONS)),
                max_cost_usd=float(
                    configurable.get(
                        "cell_max_cost_usd",
                        state.get("max_budget_usd", AGENTIC_CELL_MAX_COST_USD),
                    )
                ),
                max_elapsed_s=float(
                    configurable.get("cell_max_elapsed_s", AGENTIC_CELL_MAX_ELAPSED_S)
                ),
            )
        terminal = success or (axis is not None)
        await dispatcher.emit_governor_tick(
            step=iteration + 1,
            cost_usd=float(state.get("current_cost_usd", 0.0)) + cost_delta,
            elapsed_s=time.monotonic() - cell.start_time,
            axis=axis.value if axis is not None else None,
        )
        if success:
            record["status"] = "green"
        elif axis is not None:
            record["status"] = "budget"
            record["axis"] = axis.value
        else:
            record["status"] = "continue"

        delta: Dict[str, Any] = {
            "agentic_iteration": iteration + 1,
            "agentic_trajectory": [record, *occ_messages],
            "current_cost_usd": cost_delta,
        }
        # Carry edits forward: vfs_buffer keeps the loop-back iteration consistent;
        # pending_contents feeds the write pipeline once the loop exits to apply_patch.
        if edited_paths:
            delta["vfs_buffer"] = {p: vfs_files[p] for p in edited_paths}
        if pending_contents:
            delta["pending_contents"] = pending_contents
        if audit_entries:
            delta["permission_audit_log"] = audit_entries
        if security_flags:
            delta["security_flags"] = security_flags
        return delta

    except Exception as exc:  # noqa: BLE001 — a node fault must still tear down the session
        terminal = True
        logger.exception("AgenticCell iteration failed: %s", exc)
        return _concede(state, iteration, f"exception: {exc}")
    finally:
        # Keep the session open while looping; close it on any terminal exit so an aborted
        # or finished run never strands a live PTY.
        if terminal and task_id:
            await _close_cell(task_id)


def route_after_cell(state: Dict[str, Any]) -> str:
    """Loop-back router: re-enter the cell only while the latest verdict says 'continue'."""
    trajectory: List[Dict[str, Any]] = state.get("agentic_trajectory") or []
    if not trajectory:
        return "contract_guard"
    if trajectory[-1].get("status") == "continue":
        return "agentic_cell"
    return "contract_guard"


# =====================================================================
# Internal dispatch helpers
# =====================================================================

class _StaleEdit(Exception):
    """Raised when an edit hits an OCC conflict (the file drifted under the cell)."""


def _build_messages(state: Dict[str, Any]) -> List[Dict[str, str]]:
    """Compose the reasoning context: the task, plus the running trajectory (verdicts and
    any OCC-collision diagnostics) so the model reasons over real feedback each iteration."""
    messages: List[Dict[str, str]] = [
        {
            "role": "system",
            "content": "You are an autonomous coding agent driving a live terminal. Run "
            "commands, read the structured verdict, edit, and re-run until the task passes.",
        },
        {"role": "user", "content": str(state.get("user_input", ""))},
    ]
    for record in state.get("agentic_trajectory") or []:
        if record.get("role") == "system":
            messages.append({"role": "system", "content": str(record.get("content", ""))})
        elif record.get("diagnostics"):
            messages.append(
                {"role": "user", "content": f"Verdict: {record.get('diagnostics')}"}
            )
    return messages


def _structured_verdict(command: str, output: str) -> str:
    """Reduce raw terminal output to a structured diagnostic summary (never raw stdout)."""
    from tools.validation.diagnostics import format_diagnostics, select_parser

    parser = select_parser(command)
    return format_diagnostics(parser(output, ""))


def _occ_diagnostic(path: str) -> Dict[str, str]:
    """A system-role record telling the model *why* an edit was dropped, so it re-reads
    rather than re-issuing the identical patch until the budget is spent (livelock)."""
    return {
        "role": "system",
        "content": (
            f"Tool failed: OCC conflict on {path}. The file was modified concurrently — "
            f"read_file_ast it again before patching."
        ),
    }


def _classify_execute(state: Dict[str, Any]) -> str:
    """Classify an EXECUTE-tier terminal command under the session policy.

    Returns ``"allow"`` / ``"deny"`` / ``"hitl"`` from the same three-axis matrix every
    other call site consults (PLAN denies, AUTO admits, DEFAULT → HITL). Pure and
    state-sourced, so the cell's run/defer decision is replay-stable: it never performs
    the approval itself — a HITL verdict triggers the deferral, and the approval happens
    interrupt-first in the next super-step's exec-approval phase.
    """
    from core.permissions import (  # type: ignore[attr-defined]  # noqa: attr-defined — PermissionMode re-exported without __all__
        PermissionDecision,
        PermissionMode,
        ToolPrivilegeTier,
        evaluate_action,
        session_mode_from_channel,
    )

    verdict = evaluate_action(
        session_mode_from_channel(state.get("session_permission_mode")),
        ToolPrivilegeTier.EXECUTE,
        PermissionMode.EDIT_EXECUTE_RBW,
    )
    if verdict is PermissionDecision.DENY:
        return "deny"
    if verdict is PermissionDecision.ALLOW:
        return "allow"
    return "hitl"


async def _approve_exec(
    state: Dict[str, Any],
    command: str,
    configurable: Dict[str, Any],
) -> bool:
    """Approve a deferred command — the FIRST action of the exec-approval phase.

    Uses the injected ``cell_approval_fn`` seam in tests, else native
    ``request_graph_approval`` (LangGraph ``interrupt()``) so the graph suspends and
    the runtime is freed until the operator replies. Trust-once is intentionally not
    applied here — each gated command resumes through its own interrupt.
    """
    approval_fn = configurable.get("cell_approval_fn")
    if approval_fn is not None:
        return bool(await approval_fn(command))
    from core.hitl import request_graph_approval
    from core.permissions import (
        PermissionDecision,
        risk_intercept_guard,
        session_mode_from_channel,
    )

    _mode = session_mode_from_channel(state.get("session_permission_mode"))
    _, _risk_labels = risk_intercept_guard(command[:2000], PermissionDecision.ALLOW, _mode)
    resp = request_graph_approval(
        session_id=str(state.get("task_id") or ""),
        action_description=f"COMMAND_EXEC: {command[:200]}",
        proposed_content=command[:2000],
        request_kind="RISK_INTERCEPT" if _risk_labels else "COMMAND_EXEC",
        risk_patterns_matched=_risk_labels or None,
    )
    return bool(resp.get("approved"))


def _read_file_ast(content: str, path: str) -> str:
    """AST skeleton of a file (signatures + docstrings, bodies elided)."""
    from core.ast_engine import extract_skeleton

    language = "python" if path.endswith(".py") else "text"
    try:
        return extract_skeleton(content, language)
    except Exception:  # noqa: BLE001 — skeleton extraction is best-effort
        return content[:1500]


def _compute_edit(base: str, path: str, search: str, replace: str) -> str:
    """Compute one SEARCH/REPLACE edit against ``base`` and return the new content.

    Reuses ``apply_patch_to_vfs`` over a throwaway one-file buffer with ``expected_hash``
    set to ``base``'s OCC token, so a file that drifted under the cell raises
    ``StaleFileException`` — surfaced as ``_StaleEdit`` for the livelock-breaking diagnostic.
    The shared working set is never mutated here; the caller decides what to commit.
    """
    from agents.coder import content_hash
    from core.exceptions import StaleFileException
    from tools.patch_tool import apply_patch_to_vfs

    if base == "" and search.strip() == "":
        # New-file creation: empty search anchor + full content in replace.
        return replace

    buffer: Dict[str, str] = {path: base}
    expected = content_hash(base)

    def _read(p: str) -> Optional[str]:
        return buffer.get(p)

    def _write(p: str, value: str) -> None:
        buffer[p] = value

    try:
        apply_patch_to_vfs(_read, _write, path, search, replace, expected_hash=expected)
    except StaleFileException as exc:
        raise _StaleEdit(str(exc)) from exc
    return buffer[path]
