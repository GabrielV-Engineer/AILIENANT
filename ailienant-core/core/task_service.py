import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Tuple
from .vfs_middleware import VFSMiddleware, DirtyBuffer
from brain.state import ManualAttachment
from api.websocket_manager import vfs_manager
from tools.llm_gateway import LLMGateway
import logging
from shared.persona import compose
from transport.token_batcher import batch_tokens, NarrationGate

logger = logging.getLogger(__name__)


# Phase 7.11.6 (ADR-706 §4.5f) — Rich Tool Chips: in-memory tool-call registry.
@dataclass
class ToolCallSpec:
    """A single tracked tool invocation.

    Lives in ``TaskService._tool_call_registry`` keyed by
    ``(session_id, tool_call_id)``. Holds enough metadata that
    ``retry_tool_call`` can reconstruct the exact invocation verbatim (same
    tool, same args). The buffered output is for diagnostics/audit only — the
    frontend chip is fed via ``broadcast_tool_stream_chunk`` as it streams.
    """

    tool_call_id: str
    tool_name: str
    args: Dict[str, Any]
    side_effect_free: bool
    invoked_at: float
    status: str = "pending"  # "pending" | "success" | "error"
    output_buffer: str = ""
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = None
    # Optional graph attachment (Phase 7.11.6 dep-graph view); populated by
    # the caller when the tool's context includes a k-hop neighborhood.
    dep_graph_nodes: List[Dict[str, str]] = field(default_factory=list)
    dep_graph_edges: List[Dict[str, str]] = field(default_factory=list)


# Approximate output cap for tool chunks (mirrors execution_tools._truncate).
# Keeps WS frames < ~3 KB and avoids DOM bloat on the frontend.
_TOOL_OUTPUT_TRUNC: int = 2000


def _truncate_tool_output(text: str, cap: int = _TOOL_OUTPUT_TRUNC) -> str:
    """Middle-truncate any tool output to ``cap`` chars with a marker."""
    if len(text) <= cap:
        return text
    half = (cap - 32) // 2
    return f"{text[:half]}\n…[TRUNCATED {len(text) - cap} CHARS]…\n{text[-half:]}"


# Phase 7.10.1 — identity sovereignty: ADR-701 clause prepended via shared.persona.compose().
_CHAT_SYSTEM_PROMPT: str = compose(
    "An expert AI coding assistant embedded in the user's IDE. "
    "Answer the user's request directly and concisely. When the task involves code, "
    "provide correct, idiomatic snippets and explain the key decisions briefly. "
    "If the request is ambiguous, state the assumption you are making and proceed."
)

# Phase 7.9.B.15 — short-term session memory + GraphRAG injection.
# In-memory and ephemeral by design (matches the "/context clear" UX wording);
# keyed by the stable session_id (== WS client_id == X-Task-ID).
_MAX_HISTORY_MESSAGES: int = 24      # ~12 turns retained per conversation
_RAG_TOP_K: int = 5
_conversations: Dict[str, List[Dict[str, str]]] = {}

# Phase 7.9.B.16 — intent routing (edit/coding task vs conversational question).
_MAX_CODER_STEPS: int = 6
_EDIT_VERBS: tuple[str, ...] = (
    "add", "create", "write", "implement", "refactor", "fix", "rename", "delete",
    "remove", "change", "modify", "update", "move", "replace", "insert", "extract",
    "generate", "build", "make", "wire", "scaffold", "patch",
)
_QUESTION_STARTERS: tuple[str, ...] = (
    "how", "what", "why", "when", "where", "who", "which", "is ", "are ", "can ",
    "does ", "do ", "should ", "explain", "describe", "summarize", "list ",
)
_INTENT_SYSTEM_PROMPT: str = (
    "Classify the user's message for a coding IDE assistant as either an 'edit' "
    "(a request to write, modify, create, refactor or delete code/files) or a "
    "'question' (asking for information, explanation or discussion). "
    'Respond with ONLY JSON: {"intent": "edit"} or {"intent": "question"}.'
)


# Replicamos el contrato del frontend (api_client.ts)
class TaskPayload(BaseModel):
    task_prompt: str
    dirty_buffers: List[DirtyBuffer]
    project_id: Optional[str] = None
    explicit_mentions: List[str] = Field(default_factory=list)
    attachments: List[ManualAttachment] = Field(default_factory=list)
    document_version_id: Optional[str] = None  # OCC: version at submission (Phase 1.5)
    planner_mode_active: bool = False  # Phase 2.19: Planner-Mode toggle forwarded from WS registry
    workspace_root: Optional[str] = None  # Passed from _workspace_registry at HTTP layer


class TaskService:
    """
    Capa de orquestación intermedia.
    Aísla la lógica de LangGraph y VFS de la capa de transporte HTTP.
    """

    def __init__(self):
        # Inyección de dependencias (Singleton)
        self.vfs = VFSMiddleware()
        # Phase 7.11.1 — in-flight inline edits, keyed by edit_id. Each entry
        # owns one cancel_event (the agent loop polls it between yields, plan
        # W2) and the asyncio.Task running the orchestrator (cancel() is the
        # hard escape if the cooperative event misses a window).
        self._inline_edits: Dict[str, Tuple[asyncio.Event, "asyncio.Task[None]"]] = {}
        # Phase 7.11.3 (ADR-706 §4.5b) — Abort Controller Mesh registry.
        # Maps session_id → the runner asyncio.Task that owns one in-flight
        # generation cycle (process_task or stream_analyst_reply). ONE active
        # task per session matches the UI's single-stream contract. The runner
        # MUST register its OWN current_task() (NEVER the WS receive loop's
        # task — that would kill the socket on cancel; see plan W1).
        self._active_tasks: Dict[str, "asyncio.Task[Any]"] = {}
        # Phase 7.11.6 (ADR-706 §4.5f) — Rich Tool Chips: tracked tool-call
        # registry keyed by (session_id, tool_call_id). Side-bag (NOT in
        # AIlienantGraphState) so agents stay isolated from this transport-tier
        # concern. ``retry_tool_call`` resolves the key and re-invokes
        # ``execute_tracked_tool`` verbatim ("exact replay" semantics). The
        # ``cleanup_session`` callback registered with
        # ``api.websocket_manager.register_session_cleanup_hook`` purges
        # entries on disconnect so a crashed session never leaks specs.
        self._tool_call_registry: Dict[Tuple[str, str], ToolCallSpec] = {}

    async def process_task(
        self, session_id: str, payload: TaskPayload, execution_mode: str = "SEQUENTIAL"
    ) -> Dict[str, Any]:
        """Route a chat turn: an edit/coding task runs the agents (plan → code →
        propose diffs); a question uses the direct chat completion (memory + RAG)."""
        logger.info(
            "[Session: %s][Project: %s] Task received. buffers=%d mentions=%d planner_mode=%s",
            session_id, payload.project_id, len(payload.dirty_buffers),
            len(payload.explicit_mentions), payload.planner_mode_active,
        )

        # 1. Asimilación de Entropía O(1)
        self.vfs.ingest_dirty_buffers(payload.dirty_buffers)

        # 2. Intent routing. Planner-mode toggle forces the coding path.
        intent = "edit" if payload.planner_mode_active else await self._classify_intent(
            payload.task_prompt
        )
        logger.info("[Session: %s] routed intent=%s", session_id, intent)

        if intent == "edit":
            await self._run_coding_task(session_id, payload, execution_mode)
        else:
            await self._stream_chat_answer(
                session_id, payload.task_prompt, payload.project_id
            )

        return {"status": "success", "message": "Task completed.", "session_id": session_id}

    def _build_initial_state(
        self, session_id: str, payload: TaskPayload, execution_mode: str
    ) -> dict:
        """Construct the AIlienantGraphState seed consumed by run_planner_node."""
        initial_state: dict = {
            "task_id": session_id,
            "user_input": payload.task_prompt,
            "project_id": payload.project_id,
            "explicit_mentions": payload.explicit_mentions,
            "attachments": payload.attachments,
            "messages": [],
            "tci": 0.0,
            "css": 100.0,
            "is_manual_override": False,
            "planner_mode_active": payload.planner_mode_active,
            "workspace_root": payload.workspace_root or "",
            "hitl_pending": False,
            "hitl_response": None,
            "shared_understanding_reached": False,
            "target_role": None,
            "current_step_id": None,
            "mission_spec": None,
            "parallel_tasks": [],
            "read_files_state": {},
            "vfs_buffer": {},
            "has_images": any(a.type == "image" for a in payload.attachments),
            "routing_warning": None,
            "hardware_profile": None,
            "execution_mode": execution_mode,
            "provider": "CLOUD",
            "generated_code": {},
            "errors": [],
            "retry_count": 0,
            "security_flags": [],
            "terminal_output": "",
            "session_delta": "",
            "is_indexing_complete": True,
            "guardrail_failed": False,
            "validation_feedback": None,
            "immutable_wbs": None,
            "pending_patches": {},
            "current_cost_usd": 0.0,
            "max_budget_usd": float(os.getenv("AILIENANT_MAX_BUDGET_USD", "inf")),
            # Phase 7.11.3 — populated by the abort handler on CancelledError.
            "termination_reason": None,
        }
        try:
            from api.system_settings import _read_settings as _read_sys_settings
            _pref_mode = str(_read_sys_settings().get("permission_mode", "default")).upper()
            if _pref_mode in ("DEFAULT", "PLAN", "AUTO"):
                initial_state["session_permission_mode"] = _pref_mode
        except Exception:  # noqa: BLE001 — preference seeding must never block a task
            pass
        return initial_state

    async def _classify_intent(self, prompt: str) -> str:
        """Return 'edit' or 'question'. Heuristic first, cheap LLM tie-break, safe default."""
        text = prompt.strip().lower()
        if not text:
            return "question"
        starts_question = text.startswith(_QUESTION_STARTERS) or text.endswith("?")
        has_edit_verb = any(re.search(rf"\b{v}\b", text) for v in _EDIT_VERBS)
        if has_edit_verb and not starts_question:
            return "edit"
        if starts_question and not has_edit_verb:
            return "question"
        # Ambiguous → cheap small-tier classifier; default to 'question' (never silently edit).
        try:
            from shared.config import MODEL_SMALL
            resp = await LLMGateway.ainvoke(
                messages=[
                    {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=MODEL_SMALL, temperature=0.0,
                response_format={"type": "json_object"}, max_tokens=20,
            )
            raw = LLMGateway._sanitize_json_response(resp.choices[0].message.content or "")
            intent = str(json.loads(raw).get("intent", "question")).lower()
            return "edit" if intent == "edit" else "question"
        except Exception as exc:  # noqa: BLE001
            logger.debug("Intent classification failed (defaulting to question): %s", exc)
            return "question"

    # Phase 7.11.3 — shared abort response. Broadcasts the "Stopped by user"
    # turn + closes the stream + persists the marker into history. Best-effort:
    # individual broadcasts are guarded so a half-dead WS connection cannot
    # block the cancel path.
    _ABORT_MARKER: str = "\n\n_⏹ Stopped by user._"

    async def _emit_abort_response(
        self,
        session_id: str,
        *,
        history_key: Optional[str] = None,
    ) -> None:
        """Finalize an aborted stream: broadcast the marker + stream_end + persist."""
        try:
            await vfs_manager.broadcast_token(session_id, self._ABORT_MARKER)
        except Exception:  # noqa: BLE001 — never block cancel on broadcast failure
            pass
        try:
            await vfs_manager.broadcast_stream_end(session_id)
        except Exception:  # noqa: BLE001
            pass
        if history_key:
            try:
                self._append_history(history_key, "assistant", self._ABORT_MARKER.strip())
            except Exception:  # noqa: BLE001 — history persistence is best-effort here
                pass

    async def _run_coding_task(
        self, session_id: str, payload: TaskPayload, execution_mode: str
    ) -> None:
        """Drive the planner + coder agents to propose patches (review-only, no disk write)."""
        from agents.planner import run_planner_node  # deferred — avoids import cycle
        from agents.coder import run_coder_node

        state = self._build_initial_state(session_id, payload, execution_mode)

        # Phase 7.10.2 (ADR-702): granular sub-step narration over server_pipeline_step.
        # A NarrationGate keeps narration <= 15% of streamed volume once the answer is
        # live; pre-answer phases (answer_bytes == 0) are never suppressed. The emitter
        # is injected into the graph state so the planner can narrate without importing
        # the transport layer (cognitive-isolation fence stays intact).
        gate = NarrationGate()

        async def _narrate(node_name: str, step_id: Optional[int] = None) -> None:
            if gate.allow(len(node_name.encode())):
                await vfs_manager.broadcast_pipeline_step(session_id, node_name, step_id)

        state["narrate"] = _narrate

        # Phase 7.11.3 (ADR-706 §4.5b) — Abort Controller Mesh. CancelledError
        # may surface from ANY await in this coroutine (planner, coder steps,
        # HITL approval, write pipeline). The single outer except catches it,
        # tags the state with the savepoint marker so a future checkpointer
        # promote() carries it through cold-serializable, and emits the
        # standard abort response (broadcast marker + stream_end + persist).
        try:
            await _narrate("context_gather")
            try:
                plan = await run_planner_node(state)
            except asyncio.CancelledError:
                raise  # propagate to the outer handler — don't swallow here
            except Exception as exc:  # noqa: BLE001 — planner failure must not crash the task
                logger.warning("Planner failed: %s", exc)
                await vfs_manager.broadcast_token(
                    session_id,
                    "I couldn't draft a plan — make sure a BYOM preset is active and its "
                    f"engine is running. ({exc})",
                )
                await vfs_manager.broadcast_stream_end(session_id)
                return

            mission = plan.get("mission_spec")
            if mission is None:
                errs = plan.get("errors") or ["the planner did not return a plan"]
                await vfs_manager.broadcast_token(
                    session_id, "I couldn't draft a plan: " + "; ".join(errs)
                )
                await vfs_manager.broadcast_stream_end(session_id)
                return

            patches: Dict[str, str] = {}
            contents: Dict[str, str] = {}
            base_hashes: Dict[str, str] = {}
            errors: List[str] = []
            coder_steps = list(mission.tasks)[:_MAX_CODER_STEPS]
            total_steps = len(coder_steps)
            for idx, step in enumerate(coder_steps, start=1):
                # "coding step N/M" rides in node_name — PipelineStepPayload schema unchanged.
                await _narrate(f"coder_agent ({idx}/{total_steps})", step_id=step.step_number)
                try:
                    cres = await run_coder_node(
                        {**state, "mission_spec": mission, "current_step_id": step.step_number}
                    )
                except asyncio.CancelledError:
                    raise  # propagate up to the outer abort handler
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"step #{step.step_number}: {exc}")
                    continue
                patches.update(cres.get("pending_patches") or {})
                contents.update(cres.get("pending_contents") or {})
                base_hashes.update(cres.get("pending_base_hash") or {})
                errors.extend(cres.get("errors") or [])

            # 1) Stream the plan + proposed diffs as a reviewable message.
            summary = self._format_coding_summary(mission, patches, errors)
            gate.record_answer(len(summary.encode()))  # flips the gate into 15% enforcement
            await vfs_manager.broadcast_token(session_id, summary)
            await vfs_manager.broadcast_stream_end(session_id)
            self._append_history(session_id, "user", payload.task_prompt)
            self._append_history(session_id, "assistant", summary)

            # 2) No concrete edits → nothing to apply.
            if not contents:
                return

            # 3) One authorization for the whole change set (HITL card).
            combined_diff = "\n".join(patches[p] for p in patches)
            decision = await vfs_manager.request_human_approval(
                session_id=session_id,
                action_description=(
                    f"Apply {len(contents)} file change(s): " + ", ".join(contents)
                ),
                proposed_content=combined_diff,
                request_kind="FILE_WRITE",
            )
            if not decision or not decision.get("approved"):
                discarded = "Changes discarded — no files were modified."
                gate.record_answer(len(discarded.encode()))
                await vfs_manager.broadcast_token(session_id, discarded)
                await vfs_manager.broadcast_stream_end(session_id)
                return

            # 4) Single-file edit-before-apply: honor the card's edited payload.
            modified = decision.get("modified_content")
            if modified and len(contents) == 1:
                only_path = next(iter(contents))
                contents[only_path] = modified

            # 5) Actuate via the VS Code applyEdit bridge (Python never writes to disk).
            from core.write_pipeline import apply_patch_set
            res = await apply_patch_set(session_id, contents, base_hashes)
            if res.get("ok"):
                applied = res.get("applied_files") or list(contents)
                result_msg = f"✓ Applied {len(applied)} file(s) to disk — use Ctrl+Z to undo."
            elif res.get("stale_files"):
                result_msg = (
                    "⚠️ Not applied — these files changed since the proposal: "
                    + ", ".join(res["stale_files"])
                    + ". Re-run the request to regenerate against the current code."
                )
            else:
                result_msg = "⚠️ Could not apply the changes: " + str(
                    res.get("error") or "unknown error"
                )
            gate.record_answer(len(result_msg.encode()))
            await vfs_manager.broadcast_token(session_id, result_msg)
            await vfs_manager.broadcast_stream_end(session_id)
            self._append_history(session_id, "assistant", result_msg)
        except asyncio.CancelledError:
            # Phase 7.11.3 — emergency savepoint. Cold-serializable marker
            # written into state for the next checkpointer promote(); see
            # plan W1/W4 and ADR-706 §4.5(b).
            state["termination_reason"] = "user_abort"
            logger.info("[Session: %s] _run_coding_task aborted by user", session_id)
            await self._emit_abort_response(session_id, history_key=session_id)
            # Swallow — the orchestrator owns the lifecycle. Re-raising would
            # mark the runner task as cancelled, but the user-visible flow is
            # already complete (broadcast_stream_end fired).
            return

    @staticmethod
    def _format_coding_summary(
        mission: Any, patches: Dict[str, str], errors: List[str]
    ) -> str:
        """Render the plan outcome + proposed diffs as a reviewable chat message."""
        lines: List[str] = []
        outcome = getattr(mission, "outcome", "") or ""
        if outcome:
            lines.append(str(outcome))
        if patches:
            lines.append(f"\nProposed changes to {len(patches)} file(s):")
            for fp, diff in patches.items():
                lines.append(f"\n**{fp}**\n```diff\n{diff.rstrip()}\n```")
            lines.append(
                "\n_Review the proposed diffs above. Applying changes to disk is not "
                "yet enabled._"
            )
        else:
            lines.append("\nI drafted a plan but produced no concrete edits for this request.")
        if errors:
            lines.append("\n_Notes:_ " + "; ".join(errors[:5]))
        return "\n".join(lines)

    def _append_history(self, session_id: str, role: str, content: str) -> None:
        """Append a turn message to the session's short-term memory (bounded)."""
        history = _conversations.setdefault(session_id, [])
        history.append({"role": role, "content": content})
        if len(history) > _MAX_HISTORY_MESSAGES:
            del history[: len(history) - _MAX_HISTORY_MESSAGES]

    def clear_conversation(self, session_id: str) -> None:
        """Drop the session's short-term memory (wired to /context clear)."""
        _conversations.pop(session_id, None)

    def restore_conversation(self, session_id: str, messages: List[Dict[str, str]]) -> None:
        """Re-seed short-term chat memory from a persisted transcript (Phase 7.9.B.20).

        Called when a saved session is reopened so the model regains continuity.
        Seed-if-absent: only populates when the session has no live memory, so an
        in-flight conversation is never clobbered. Bounded to _MAX_HISTORY_MESSAGES.
        """
        if not messages or _conversations.get(session_id):
            return
        cleaned = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if cleaned:
            _conversations[session_id] = cleaned[-_MAX_HISTORY_MESSAGES:]
            logger.info(
                "[Session: %s] Conversation memory restored (%d turns).",
                session_id, len(_conversations[session_id]),
            )

    async def _build_rag_context(self, task_prompt: str, project_id: Optional[str]) -> str:
        """Fetch top-k LanceDB snippets for the prompt and format them for the system prompt.

        Returns '' when no project, no index, or any failure — RAG is best-effort
        and must never block or break a chat turn.
        """
        if not project_id:
            return ""
        try:
            from core.memory.semantic_memory import SemanticMemoryManager
            snippets = await SemanticMemoryManager().search_snippets(
                task_prompt, workspace_hash=project_id, k=_RAG_TOP_K
            )
        except Exception as exc:  # noqa: BLE001 — RAG fetch is non-fatal
            logger.debug("RAG context fetch failed (non-fatal): %s", exc)
            return ""
        if not snippets:
            return ""
        blocks = "\n\n".join(f"### {path}\n{snip}" for path, snip in snippets if snip)
        if not blocks:
            return ""
        return (
            "\n\n# Relevant workspace context (GraphRAG)\n"
            "Code excerpts from the user's project that may be relevant. "
            "Use them when helpful; ignore them otherwise.\n\n" + blocks
        )

    async def _stream_chat_answer(
        self, session_id: str, task_prompt: str, project_id: Optional[str] = None
    ) -> None:
        """Stream a live completion from the active BYOM chat model to the IDE.

        Injects short-term session memory + GraphRAG snippets. Persists the turn
        only on a successful, non-empty reply. Always finalizes with
        broadcast_stream_end; on failure it broadcasts an actionable message.
        """
        system_content = _CHAT_SYSTEM_PROMPT + await self._build_rag_context(
            task_prompt, project_id
        )
        history = _conversations.get(session_id, [])
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_content},
            *history,
            {"role": "user", "content": task_prompt},
        ]
        reply_parts: List[str] = []
        aborted = False
        try:
            # Phase 7.10.2 (ADR-702/G1): coalesce deltas into chunk_ms=40 frames so
            # the Webview never receives one WS frame per token. Concatenation is
            # preserved, so reply_parts still reconstructs the full answer.
            raw_stream = LLMGateway.astream_byom(
                messages, tier="medium", session_id=session_id
            )
            async for chunk in batch_tokens(raw_stream, chunk_ms=40):
                reply_parts.append(chunk)
                await vfs_manager.broadcast_token(session_id, chunk)
            reply = "".join(reply_parts)
            if reply:
                # Persist only completed turns so a failure never poisons memory.
                self._append_history(session_id, "user", task_prompt)
                self._append_history(session_id, "assistant", reply)
            else:
                await vfs_manager.broadcast_token(
                    session_id,
                    "No response was produced. Check that a BYOM preset is active and "
                    "its model is reachable (Dashboard → BYOM).",
                )
        except asyncio.CancelledError:
            # Phase 7.11.3 — user clicked Stop. Persist the partial answer (if
            # any) so the transcript reflects what arrived before the abort.
            aborted = True
            logger.info("[Session: %s] _stream_chat_answer aborted by user", session_id)
            partial = "".join(reply_parts).rstrip()
            if partial:
                self._append_history(session_id, "user", task_prompt)
                self._append_history(session_id, "assistant", partial + self._ABORT_MARKER.strip())
            try:
                await vfs_manager.broadcast_token(session_id, self._ABORT_MARKER)
            except Exception:  # noqa: BLE001 — best-effort during cancel
                pass
        except Exception as exc:  # noqa: BLE001 — a chat failure must never crash the task
            logger.warning("Live chat completion failed: %s", exc)
            await vfs_manager.broadcast_token(
                session_id,
                "I couldn't reach the configured model. Activate a BYOM preset "
                "(Dashboard → BYOM) and make sure its engine is running, then try again.",
            )
        finally:
            try:
                await vfs_manager.broadcast_stream_end(session_id)
            except Exception:  # noqa: BLE001
                pass
            if aborted:
                # Don't re-raise — abort path is user-visible-complete; the
                # runner returns normally so the registry cleanup callback fires.
                return

    async def stream_analyst_reply(
        self,
        session_id: str,
        text: str,
        paths: List[str],
        cursor: Optional[int] = None,
        project_id: Optional[str] = None,
        project_root: str = "",
    ) -> None:
        """Phase 7.10.3 — context-aware, streamed analyst reply (ADR-703).

        Orchestration only (the analyst stays read-only — Voice, not the Hand): assembles
        the budgeted + sandboxed context (active file via VFS + Codex + GraphRAG, reusing
        _build_rag_context), replays the namespaced analyst memory, streams chunk_ms=40
        batches to the Natt pane, emits the G2 context-version on stream end, then persists
        the turn. Analyst memory is namespaced (``natt:``) so it never mixes with main chat.
        """
        from agents.analyst import generate_analyst_reply_stream  # deferred — cycle guard
        from agents.analyst_context import assemble_analyst_context

        rag = await self._build_rag_context(text, project_id)
        try:
            context_block = await assemble_analyst_context(
                paths, project_id, session_id, cursor,
                rag_block=rag, project_root=project_root, vfs=self.vfs,
            )
        except Exception as exc:  # noqa: BLE001 — assembly must never crash the analyst
            logger.warning("Analyst context assembly failed (degrading to RAG only): %s", exc)
            context_block = rag

        mem_key = f"natt:{session_id}"
        history = list(_conversations.get(mem_key, []))

        parts: List[str] = []
        aborted = False
        try:
            async for chunk in generate_analyst_reply_stream(
                text, context_block, history, session_id
            ):
                parts.append(chunk)
                await vfs_manager.broadcast_natt_token(session_id, chunk)
        except asyncio.CancelledError:
            # Phase 7.11.3 — Stop button mid-analyst-stream.
            aborted = True
            logger.info("[Session: %s] stream_analyst_reply aborted by user", session_id)
            try:
                await vfs_manager.broadcast_natt_token(session_id, self._ABORT_MARKER)
            except Exception:  # noqa: BLE001
                pass

        context_version = hashlib.sha256(context_block.encode("utf-8")).hexdigest()[:12]
        try:
            await vfs_manager.broadcast_natt_stream_end(session_id, context_version=context_version)
        except Exception:  # noqa: BLE001 — best-effort during cancel
            pass

        reply = "".join(parts).strip()
        if reply:
            self._append_history(mem_key, "user", text)
            self._append_history(
                mem_key, "assistant",
                reply + (self._ABORT_MARKER.strip() if aborted else ""),
            )

    # ------------------------------------------------------------------
    # Phase 7.11.1 — Inline editor mutations (Cmd+K, ADR-706 §4.5a)
    # ------------------------------------------------------------------

    async def start_inline_edit(
        self,
        *,
        session_id: str,
        edit_id: str,
        file_path: str,
        file_content: str,
        range_start: int,
        range_end: int,
        prompt: str,
        language_id: Optional[str] = None,
    ) -> None:
        """Orchestrate one streaming inline-edit session.

        Emits server_inline_edit_start, then each typed delta from
        agents.inline_edit.stream_inline_edit, then a single
        server_inline_edit_end with the speculative final_content the host
        will hash to derive the commit base_hash. Honors cooperative
        cancellation via a per-edit asyncio.Event registered in
        self._inline_edits; cancel_inline_edit() sets the event AND
        Task.cancel() — belt-and-suspenders so a slow LLM yield can't
        outlive the user's Esc.
        """
        from agents.inline_edit import stream_inline_edit  # deferred — cycle guard

        cancel_event = asyncio.Event()
        current_task = asyncio.current_task()
        if current_task is not None:
            self._inline_edits[edit_id] = (cancel_event, current_task)

        await vfs_manager.broadcast_inline_edit_start(
            session_id, edit_id, file_path, range_start, range_end,
        )

        final_buf = file_content
        sel_start = max(0, min(range_start, len(file_content)))
        sel_end = max(sel_start, min(range_end, len(file_content)))
        insert_cursor = sel_start
        # The buffer evolves with each broadcast delta — kept in sync so the
        # final_content sent to the host matches what the editor will display.
        final_buf = file_content[:sel_start] + file_content[sel_end:]

        success = True
        error: Optional[str] = None
        try:
            async for delta in stream_inline_edit(
                prompt,
                file_path,
                file_content,
                (sel_start, sel_end),
                language_id,
                session_id=session_id,
                cancel_event=cancel_event,
            ):
                kind = delta["kind"]
                offset = int(delta.get("offset", 0))
                length = int(delta.get("length", 0))
                text = str(delta.get("text", ""))
                if kind == "INSERT":
                    final_buf = final_buf[:insert_cursor] + text + final_buf[insert_cursor:]
                    insert_cursor += len(text)
                # The initial DELETE was already applied to final_buf above;
                # treat any further DELETE as additive (length chars at offset).
                elif kind == "DELETE" and offset != sel_start:
                    end = min(offset + length, len(final_buf))
                    final_buf = final_buf[:offset] + final_buf[end:]
                elif kind == "ABORT":
                    success = False
                    error = text or "aborted"
                await vfs_manager.broadcast_inline_edit_delta(
                    session_id, edit_id, kind, offset, length, text,
                )
                if kind == "ABORT":
                    break
        except asyncio.CancelledError:
            success = False
            error = "cancelled"
            # Surface the cancellation as an ABORT so the manager can clean
            # up its decorations even if the task was killed mid-yield.
            try:
                await vfs_manager.broadcast_inline_edit_delta(
                    session_id, edit_id, "ABORT", 0, 0, "user_cancel",
                )
            except Exception:  # noqa: BLE001 — best-effort during cancel
                pass
            # Don't re-raise: this orchestrator owns the lifecycle and emits
            # the END event below. The cancel_event already broke the loop.
        except Exception as exc:  # noqa: BLE001 — orchestrator must never crash WS loop
            logger.warning("INLINE_EDIT orchestrator failed (edit_id=%s): %s", edit_id, exc)
            success = False
            error = str(exc)
        finally:
            self._inline_edits.pop(edit_id, None)
            try:
                await vfs_manager.broadcast_inline_edit_end(
                    session_id, edit_id, success,
                    final_content=final_buf if success else "",
                    error=error,
                )
            except Exception:  # noqa: BLE001 — END is best-effort on a dead socket
                pass

    def cancel_inline_edit(self, edit_id: str) -> bool:
        """Cooperative cancel — sets the agent's event AND cancels its Task.

        Returns True if a live edit was found and signaled; False if the
        edit_id was unknown (already completed or never started).
        """
        entry = self._inline_edits.get(edit_id)
        if entry is None:
            return False
        cancel_event, task = entry
        cancel_event.set()
        if not task.done():
            task.cancel()
        return True

    # ------------------------------------------------------------------
    # Phase 7.11.3 — Abort Controller Mesh (ADR-706 §4.5b)
    # ------------------------------------------------------------------

    def register_active_task(self, session_id: str, task: "asyncio.Task[Any]") -> None:
        """Track an in-flight session task so the abort mesh can cancel it.

        The caller MUST pass its OWN ``asyncio.current_task()`` from inside the
        runner closure (the `_runner` inside `submit_task`, the
        `_analyst_runner` inside the `client_analyst_query` handler) — NEVER
        the WS receive loop's task. The runner is a child task spawned via
        `asyncio.create_task(...)`; cancelling it propagates `CancelledError`
        into the generation coroutine without disturbing the WS connection.
        See plan W1 for the full invariant.

        Idempotent: a second call for the same session_id replaces the
        previous entry (only one in-flight generation per UI session). On
        task completion, the done-callback auto-removes the entry.
        """
        self._active_tasks[session_id] = task
        task.add_done_callback(lambda _t: self._active_tasks.pop(session_id, None))

    def abort_session(self, session_id: str) -> bool:
        """Cancel the in-flight task for ``session_id`` cooperatively.

        Returns True if a live task was signalled, False if the session has
        no registered task (already completed, never started, or never
        registered). Idempotent: calling on a done/cancelled task is a no-op.
        """
        task = self._active_tasks.get(session_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    # ------------------------------------------------------------------
    # Phase 7.11.6 (ADR-706 §4.5f) — Rich Tool Chips registry + retry
    # ------------------------------------------------------------------

    async def execute_tracked_tool(
        self,
        session_id: str,
        tool_name: str,
        args: Dict[str, Any],
        side_effect_free: bool = False,
    ) -> ToolCallSpec:
        """Run a tool through the *tracked* path so the IDE renders a chip.

        The shape is identical regardless of underlying executor:

        1. mint a UUID4 ``tool_call_id``;
        2. register a fresh :class:`ToolCallSpec` so a later
           ``retry_tool_call`` can replay verbatim;
        3. broadcast ``server_tool_start`` (pending status, args header);
        4. dispatch to the tool implementation (today only ``sandbox_bash`` is
           wired — future MCP / agent flows plug in via the same shape);
        5. stream output via ``server_tool_stream_chunk`` (one chunk for the
           one-shot sandbox adapter; many for future PTY-style streamers);
        6. always finalize with ``server_tool_result`` in a ``finally`` so a
           cancelled or exception-raising runner still closes the chip.

        Returns the populated :class:`ToolCallSpec` so callers (e.g., the
        ``client_invoke_tracked_bash`` smoke handler) can await it without
        re-reading the registry.
        """
        tool_call_id = uuid.uuid4().hex
        invoked_at = time.time()
        spec = ToolCallSpec(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=dict(args),  # defensive copy — agents won't mutate later
            side_effect_free=side_effect_free,
            invoked_at=invoked_at,
        )
        self._tool_call_registry[(session_id, tool_call_id)] = spec

        await vfs_manager.broadcast_tool_start(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=spec.args,
            side_effect_free=side_effect_free,
            invoked_at=invoked_at,
        )

        try:
            if tool_name == "sandbox_bash":
                from core.sandbox import get_active_adapter  # type: ignore[import-not-found]

                adapter = get_active_adapter()
                if adapter is None:
                    raise RuntimeError(
                        "Sandbox adapter not initialized — lifespan startup did "
                        "not resolve a core.sandbox.ACTIVE_ADAPTER."
                    )

                result = await adapter.execute(
                    args.get("command", ""),
                    timeout_s=float(args.get("timeout_sec", 30.0)),
                    cwd=str(args.get("working_dir") or ""),
                    env_whitelist=None,  # adapter's default whitelist
                )
                body = _truncate_tool_output(
                    (result.stdout or "") + (result.stderr or "")
                )
                if body:
                    await vfs_manager.broadcast_tool_stream_chunk(
                        session_id=session_id,
                        tool_call_id=tool_call_id,
                        chunk=body,
                        is_stderr=False,
                    )
                spec.output_buffer = body
                spec.exit_code = int(result.exit_code)
                spec.status = "success" if result.exit_code == 0 else "error"
            else:
                # Future tool integrations land here. Until then we surface a
                # clear error chip rather than silently swallowing the call.
                raise NotImplementedError(
                    f"Tracked execution of tool {tool_name!r} not wired yet "
                    "(Phase 7.11.6 only ships sandbox_bash; future tools will "
                    "register through this same method)."
                )
        except asyncio.CancelledError:
            spec.status = "error"
            spec.output_buffer = "[cancelled by user]"
            raise
        except Exception as exc:  # noqa: BLE001 — chip surfaces the error to UI
            logger.warning(
                "[Session: %s] execute_tracked_tool(%s) failed: %s",
                session_id, tool_name, exc,
            )
            spec.status = "error"
            err_text = f"[error] {exc}"
            spec.output_buffer = err_text
            # Best-effort: broadcast the error text as a stream chunk so the
            # chip body shows what went wrong even when no real output ran.
            try:
                await vfs_manager.broadcast_tool_stream_chunk(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    chunk=err_text,
                    is_stderr=True,
                )
            except Exception:  # noqa: BLE001 — never raise from the error path
                pass
        finally:
            spec.duration_ms = int((time.time() - invoked_at) * 1000)
            try:
                await vfs_manager.broadcast_tool_result(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    status=spec.status if spec.status in ("success", "error") else "error",
                    exit_code=spec.exit_code,
                    duration_ms=spec.duration_ms,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "broadcast_tool_result swallowed for %s: %s",
                    tool_call_id, exc,
                )

        return spec

    async def retry_tool_call(
        self, session_id: str, tool_call_id: str
    ) -> bool:
        """Exact-replay retry: re-invoke a previously tracked tool verbatim.

        Returns True if the spec was found and re-execution was started;
        False if the ``(session_id, tool_call_id)`` pair is unknown (e.g., the
        session was cleaned up or the id was forged). The replay produces a
        NEW chip with a fresh ``tool_call_id`` — the historical chip stays
        intact as a record of the previous attempt.
        """
        spec = self._tool_call_registry.get((session_id, tool_call_id))
        if spec is None:
            return False
        await self.execute_tracked_tool(
            session_id=session_id,
            tool_name=spec.tool_name,
            args=spec.args,
            side_effect_free=spec.side_effect_free,
        )
        return True

    def cleanup_session(self, session_id: str) -> int:
        """Purge every tool-call registry entry whose key starts with
        ``session_id``. Returns the count of entries removed.

        Called from ``api.websocket_manager.disconnect`` via the cleanup-hook
        bus so a crashed/disconnected session never leaks specs in the
        registry. Safe to call multiple times.
        """
        keys = [k for k in self._tool_call_registry if k[0] == session_id]
        for k in keys:
            del self._tool_call_registry[k]
        return len(keys)
