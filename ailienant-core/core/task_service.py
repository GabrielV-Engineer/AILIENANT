import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Literal, Optional, Tuple, cast, TYPE_CHECKING
from .vfs_middleware import VFSMiddleware, DirtyBuffer
from brain.state import ManualAttachment
from api.websocket_manager import vfs_manager
from tools.llm_gateway import LLMGateway
import logging

if TYPE_CHECKING:
    from api.ws_contracts import PlanDocumentPayload
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
    # Per-submit idempotency key. Lets the HTTP submit endpoint dedup a resubmit
    # (e.g. driven by a WS reconnect) so the same request never spawns two
    # generations. Optional: an omitting client behaves exactly as before (the
    # server mints/ignores it), so the wire stays backward compatible.
    request_id: Optional[str] = None
    planner_mode_active: bool = False  # Phase 2.19: Planner-Mode toggle forwarded from WS registry
    # The frontend three-way mode selector (automatic | ask_before_edits | plan_mode).
    # Maps to the session permission policy that gates writes at the apply edge.
    # Optional: an omitting client keeps the per-session settings-file default.
    execution_mode: Optional[str] = None
    workspace_root: Optional[str] = None  # Passed from _workspace_registry at HTTP layer
    # Phase 7.12.9 (Fix 3) — the focused editor tab (may be SAVED, so absent from
    # dirty_buffers). Content is hard-capped client-side (ACTIVE_FILE_CHAR_CAP).
    active_file_path: Optional[str] = None
    active_file_content: Optional[str] = None
    # Phase 7.11.8 (ADR-706 §4.5g) — when the session was created via a
    # ``BRANCH_FROM_CHECKPOINT`` flow, this field carries the source
    # checkpoint_id so future graph runs can pin LangGraph's RunnableConfig
    # to the exact rewound state. Optional/default-None preserves the
    # pre-7.11.8 wire shape.
    from_checkpoint_id: Optional[str] = None
    # Phase 9 (ADR-707) — Native Thinking. When true (default), the live-chat
    # streamer uses the thinking-aware gateway path and emits reasoning deltas
    # to the Thought Box for capable models; incapable models silently fall back
    # to flat text. ``thinking_budget_tokens`` is the API-level circuit breaker
    # (Anthropic stops reasoning at the cap). Both default so an omitted field
    # from a pre-Phase-9 client keeps the prior behaviour for non-thinking
    # models while enabling thinking for capable ones.
    enable_native_thinking: bool = True
    thinking_budget_tokens: int = 4096


class _ThinkingStreamer:
    """Coalesces reasoning deltas from the coding graph into the Thought Box.

    The planner/coder nodes can't import the transport layer (cognitive-isolation
    fence), so they push reasoning text through a callback handed to them on the
    run config. This object IS that callback's home: it buffers deltas and flushes
    them to ``broadcast_thinking_chunk`` on the same 60 ms / 4096-char window the
    live-chat streamer uses, so a token flood never becomes one WS frame per token.
    """

    _WINDOW_S = 0.060
    _MAX_BUF = 4096

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._buf: List[str] = []
        self._buf_chars = 0
        self._chars_total = 0
        self._window_start = asyncio.get_running_loop().time()

    async def feed(self, text: str) -> None:
        if not text:
            return
        self._buf.append(text)
        self._buf_chars += len(text)
        self._chars_total += len(text)
        elapsed = asyncio.get_running_loop().time() - self._window_start
        if elapsed >= self._WINDOW_S or self._buf_chars >= self._MAX_BUF:
            await self.flush()
            self._window_start = asyncio.get_running_loop().time()

    async def flush(self) -> None:
        if not self._buf:
            return
        chunk = "".join(self._buf)
        self._buf = []
        self._buf_chars = 0
        # ~4 chars/token heuristic for the live "N tokens" telemetry; the billed
        # count flows through the gateway's usage accounting (display-only here).
        await vfs_manager.broadcast_thinking_chunk(
            self._session_id, chunk, max(1, self._chars_total // 4)
        )


class TaskService:
    """
    Capa de orquestación intermedia.
    Aísla la lógica de LangGraph y VFS de la capa de transporte HTTP.
    """

    def __init__(self) -> None:
        # Dependency injection (singleton).
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
        # Phase 7.11.8 (ADR-706 §4.5g) — when the session was minted by a
        # branch op, the L1 (MemorySaver) state has already been seeded by
        # `HybridCheckpointer.branch_from`. LangGraph's RunnableConfig binds
        # to the thread_id (== session_id), so the next graph node picks up
        # exactly where the source checkpoint left off. We just log the
        # provenance for diagnosability; no special routing needed today.
        if payload.from_checkpoint_id:
            logger.info(
                "[Session: %s] Resuming from branched checkpoint %s",
                session_id, payload.from_checkpoint_id,
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
                session_id,
                payload.task_prompt,
                payload.project_id,
                enable_native_thinking=payload.enable_native_thinking,
                thinking_budget_tokens=payload.thinking_budget_tokens,
            )

        return {"status": "success", "message": "Task completed.", "session_id": session_id}

    def _build_initial_state(
        self, session_id: str, payload: TaskPayload, execution_mode: str
    ) -> dict[str, Any]:
        """Construct the AIlienantGraphState seed consumed by run_planner_node."""
        initial_state: dict[str, Any] = {
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
            # Phase 7.12.9 (Fix 3) — transient planner inputs threaded via the
            # initial-state dict (NOT declared on AIlienantGraphState; the TypedDict
            # contract is unchanged). run_planner_node reads these to inject the
            # active tab prominently; downstream nodes ignore the unknown keys.
            "active_file_path": payload.active_file_path or "",
            "active_file_content": payload.active_file_content or "",
            "hitl_pending": False,
            "hitl_response": None,
            "shared_understanding_reached": False,
            # Transient handoff flag: synthesis_node sets it True after distilling
            # the Socratic dialogue so route_after_ideation forwards the brief to the
            # autonomous planner. Not on the AIlienantGraphState TypedDict — the
            # router reads it off the loose state dict, like active_file_path.
            "ideation_synthesized": False,
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
        # The per-task execution-mode selector takes precedence over the global
        # settings-file preference: the selector reflects the user's intent for
        # THIS turn, while the settings file is only a session-wide default. The
        # channel stores the uppercase Literal["DEFAULT","PLAN","AUTO"]; readers
        # lowercase before constructing the SessionPermissionMode enum (whose
        # values are lowercase), guarded by a ValueError → DEFAULT fallback.
        from core.permissions import session_mode_from_frontend
        _selector = session_mode_from_frontend(payload.execution_mode)
        if _selector is not None:
            initial_state["session_permission_mode"] = _selector.value.upper()
        else:
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
        """Finalize an aborted stream: broadcast the marker + stream_end + persist.

        Phase 7.11.8 — when the abort lands AFTER LangGraph has already run at
        least one node, an L1 checkpoint exists carrying the
        ``termination_reason="user_abort"`` marker. We pass that checkpoint_id
        through to the frontend so the resulting message renders the ⏹
        abort-savepoint icon variant of the branch button.
        """
        try:
            await vfs_manager.broadcast_token(session_id, self._ABORT_MARKER)
        except Exception:  # noqa: BLE001 — never block cancel on broadcast failure
            pass
        await self._finalize_stream(session_id)
        if history_key:
            try:
                self._append_history(history_key, "assistant", self._ABORT_MARKER.strip())
            except Exception:  # noqa: BLE001 — history persistence is best-effort here
                pass

    # ------------------------------------------------------------------
    # Phase 7.11.8 (ADR-706 §4.5g) — stream finalization + time-travel API
    # ------------------------------------------------------------------

    async def _finalize_stream(self, session_id: str) -> None:
        """Promote the current L1 checkpoint to L2 (so it becomes branchable),
        capture its ``checkpoint_id``, and emit ``server_stream_end`` carrying
        the id. Best-effort throughout: a missing L1 row (pure chat with no
        graph run), a closed DB connection, or a half-dead WS connection all
        degrade gracefully — the frontend simply gets ``checkpoint_id=None``
        and suppresses the per-message branch button on that turn.
        """
        cid: Optional[str] = None
        try:
            from brain.checkpoint import checkpoint_manager
            from langchain_core.runnables import RunnableConfig
            cfg: RunnableConfig = {"configurable": {"thread_id": session_id}}
            ct = checkpoint_manager.get_tuple(cfg)
            if ct is not None:
                cid = ct.config["configurable"].get("checkpoint_id")
                # Persist L1 → L2 so the branch_from() flow can find this
                # snapshot after a backend restart.
                try:
                    checkpoint_manager.promote(session_id)
                except Exception as exc:  # noqa: BLE001 — promote is best-effort
                    logger.debug(
                        "_finalize_stream: promote failed for %s: %s",
                        session_id, exc,
                    )
        except Exception as exc:  # noqa: BLE001 — fall through to broadcast w/o id
            logger.debug("_finalize_stream: get_tuple failed for %s: %s",
                         session_id, exc)
        try:
            await vfs_manager.broadcast_stream_end(session_id, checkpoint_id=cid)
        except Exception:  # noqa: BLE001 — never block on broadcast failure
            pass

    async def branch_session(
        self,
        parent_session_id: str,
        from_checkpoint_id: str,
        new_session_id: str,
    ) -> bool:
        """Fork: copy a parent session's checkpoint into a brand-new session_id.

        Calls ``HybridCheckpointer.branch_from`` to write the new L2 row +
        seed L1 for the new thread, then broadcasts
        ``server_session_branched`` so the IDE host can mint a new
        ``Session`` in the sidebar and open it. Returns ``False`` (without
        broadcasting) if the source checkpoint cannot be found — the WS
        handler in main.py logs that and the client toast surfaces it.
        """
        from brain.checkpoint import checkpoint_manager
        ok = checkpoint_manager.branch_from(
            from_thread_id=parent_session_id,
            from_checkpoint_id=from_checkpoint_id,
            new_thread_id=new_session_id,
        )
        if not ok:
            logger.warning(
                "branch_session: checkpoint %s not found for thread %s",
                from_checkpoint_id, parent_session_id,
            )
            return False
        try:
            await vfs_manager.broadcast_session_branched(
                parent_session_id=parent_session_id,
                new_session_id=new_session_id,
                from_checkpoint_id=from_checkpoint_id,
            )
        except Exception as exc:  # noqa: BLE001 — never block on broadcast failure
            logger.warning(
                "branch_session: broadcast failed for %s (L2 row written OK): %s",
                new_session_id, exc,
            )
        return True

    async def _run_coding_task(
        self, session_id: str, payload: TaskPayload, execution_mode: str
    ) -> None:
        """Drive the compiled LangGraph engine to propose patches, then apply via the HITL bridge.

        Entering the compiled graph (instead of calling the planner/coder nodes
        directly) is what arms the mode router (autonomous planner vs Socratic
        ideation loop), the in-graph self-healing path, and the checkpointer —
        the graph runs on ``thread_id=session_id`` so a checkpoint is written
        and the Rewind affordance becomes available. The graph proposes patches
        but does not touch disk (its apply node is inert); the actual write stays
        here behind the HITL approval card so the transport/permission boundary
        is never pushed into a cognitive node.
        """
        from brain.engine import alienant_app  # deferred — avoids import cycle
        from brain.state import AIlienantGraphState
        from langchain_core.runnables import RunnableConfig

        state = self._build_initial_state(session_id, payload, execution_mode)

        # Phase 7.10.2 (ADR-702): granular sub-step narration over server_pipeline_step.
        # A NarrationGate keeps narration <= 15% of streamed volume once the answer is
        # live; pre-answer phases (answer_bytes == 0) are never suppressed. The emitter
        # rides on the run config (RunnableConfig.configurable), NOT graph state: a
        # callable is not msgpack-serializable, and the checkpointer freezes the whole
        # state after every node. `configurable` is never checkpointed and never part
        # of a Send({**state}) fan-out payload, so the closure can never reach the
        # serializer while the planner still narrates without importing the transport
        # layer (cognitive-isolation fence stays intact).
        gate = NarrationGate()

        async def _narrate(node_name: str, step_id: Optional[int] = None) -> None:
            if gate.allow(len(node_name.encode())):
                await vfs_manager.broadcast_pipeline_step(session_id, node_name, step_id)

        # Stream the planner/coder native reasoning to the Thought Box while they
        # generate, so the long structured-output call no longer reads as a freeze.
        # Rides the same `configurable` seam as `narrate` (off graph state); the
        # nodes hand `feed` to the gateway as a best-effort reasoning sink. Thinking
        # uses its own `server_thinking_chunk` channel, so it never touches the
        # NarrationGate budget (which governs `server_pipeline_step` only).
        thinking_streamer = _ThinkingStreamer(session_id)

        # Phase 7.11.3 (ADR-706 §4.5b) — Abort Controller Mesh. CancelledError
        # may surface from ANY await in this coroutine (planner, coder steps,
        # HITL approval, write pipeline). The single outer except catches it,
        # tags the state with the savepoint marker so a future checkpointer
        # promote() carries it through cold-serializable, and emits the
        # standard abort response (broadcast marker + stream_end + persist).
        try:
            await _narrate("context_gather")
            cfg: RunnableConfig = {
                "configurable": {
                    "thread_id": session_id,
                    "narrate": _narrate,
                    "stream_thinking": thinking_streamer.feed,
                    "enable_native_thinking": payload.enable_native_thinking,
                    "thinking_budget_tokens": payload.thinking_budget_tokens,
                }
            }
            final_state: Dict[str, Any] = {}
            try:
                # stream_mode="values" yields the full accumulated state after
                # each node; the last snapshot is the complete final state. The
                # graph snapshot does NOT carry LLM tokens — node sub-step progress
                # comes from the narrate emitter, and the planner/coder reasoning
                # streams to the Thought Box via the stream_thinking sink, both on
                # config.configurable rather than from this loop.
                # cast satisfies the astream() overload (the seed carries a few
                # transient keys beyond the AIlienantGraphState TypedDict — the
                # graph drops them, the same way ainvoke() is cast at resume).
                async for snapshot in alienant_app.astream(
                    cast(AIlienantGraphState, state), config=cfg, stream_mode="values"
                ):
                    final_state = snapshot
                # Drain any reasoning still buffered from the final node.
                await thinking_streamer.flush()
            except asyncio.CancelledError:
                raise  # propagate to the outer abort handler — don't swallow here
            except Exception as exc:  # noqa: BLE001 — a graph failure must not crash the task
                logger.warning("Graph run failed: %s", exc)
                await vfs_manager.broadcast_token(
                    session_id,
                    "I couldn't run the planning engine — make sure a BYOM preset is "
                    f"active and its engine is running. ({exc})",
                )
                await self._finalize_stream(session_id)
                return

            mission = final_state.get("mission_spec")
            hitl_pending = bool(final_state.get("hitl_pending"))

            # Socratic suspend: in planner mode the ideation loop asked a question
            # (the analyst broadcasts it itself) and produced no plan yet. Finalize
            # so the checkpoint is written (Rewind works) and return — the next
            # user turn resumes on the same thread_id via the messages accumulator.
            if mission is None and hitl_pending:
                await self._finalize_stream(session_id)
                self._append_history(session_id, "user", payload.task_prompt)
                return

            # Genuine planner failure (no plan, and not awaiting the user).
            if mission is None:
                errs = final_state.get("errors") or ["the planner did not return a plan"]
                await vfs_manager.broadcast_token(
                    session_id, "I couldn't draft a plan: " + "; ".join(errs)
                )
                await self._finalize_stream(session_id)
                return

            # The graph's reducers (operator.or_ / operator.add) already merged
            # every coder step — across SWARM fan-out and the RELAY/validation
            # loop — into the final state, plus any in-graph self-healing fix.
            patches: Dict[str, str] = dict(final_state.get("pending_patches") or {})
            contents: Dict[str, str] = dict(final_state.get("pending_contents") or {})
            base_hashes: Dict[str, str] = dict(final_state.get("pending_base_hash") or {})
            errors: List[str] = list(final_state.get("errors") or [])

            # 1) Surface the plan. The structured document and its one-line chat
            # pointer ride in a single message so the bubble and the rich Plan
            # panel land on one frontend state transition — two sequential
            # broadcasts could arrive out of order and flash the pointer against
            # an empty panel. The proposed diffs keep their own DiffBlock render
            # path on apply, so they are not re-flattened into chat prose here.
            summary = self._format_coding_summary(mission, patches, errors)
            gate.record_answer(len(summary.encode()))  # flips the gate into 15% enforcement
            await vfs_manager.broadcast_plan_document(
                session_id, self._build_plan_payload(mission, summary)
            )
            await self._finalize_stream(session_id)
            self._append_history(session_id, "user", payload.task_prompt)
            self._append_history(session_id, "assistant", summary)

            # 2) No concrete edits → nothing to apply.
            if not contents:
                return

            # 3) Permission gate. The session mode (driven by the user's mode
            # selector) composes with the WRITE tier and the coder's identity
            # floor into a single verdict: DENY blocks the write outright (Plan
            # mode), HITL routes through the approval card (Ask), ALLOW applies
            # without interruption (Auto). The channel stores uppercase; the
            # enum is lowercase, so lowercase before constructing it.
            from core.permissions import (
                PermissionDecision,
                SessionPermissionMode,
                ToolPrivilegeTier,
                evaluate_action,
            )
            from shared.rbac import PermissionMode

            raw_mode = str(final_state.get("session_permission_mode") or "DEFAULT").lower()
            try:
                session_mode = SessionPermissionMode(raw_mode)
            except ValueError:
                session_mode = SessionPermissionMode.DEFAULT

            verdict = evaluate_action(
                session_mode, ToolPrivilegeTier.WRITE, PermissionMode.EDIT_EXECUTE_RBW
            )

            if verdict is PermissionDecision.DENY:
                blocked = (
                    "Plan mode is read-only — no files were changed. "
                    "Switch to Ask or Auto to apply edits."
                )
                gate.record_answer(len(blocked.encode()))
                await vfs_manager.broadcast_token(session_id, blocked)
                await self._finalize_stream(session_id)
                return

            # Decouple the payload from the UI so the actuation reads one variable
            # in every path: HITL may overwrite a single-file entry with the
            # operator's edited text; ALLOW applies the coder's proposal as-is.
            patches_to_apply: Dict[str, str] = dict(contents)

            if verdict is PermissionDecision.HITL:
                combined_diff = "\n".join(patches[p] for p in patches)
                approval = await vfs_manager.request_human_approval(
                    session_id=session_id,
                    action_description=(
                        f"Apply {len(patches_to_apply)} file change(s): "
                        + ", ".join(patches_to_apply)
                    ),
                    proposed_content=combined_diff,
                    request_kind="FILE_WRITE",
                )
                if not approval or not approval.get("approved"):
                    discarded = "Changes discarded — no files were modified."
                    gate.record_answer(len(discarded.encode()))
                    await vfs_manager.broadcast_token(session_id, discarded)
                    await self._finalize_stream(session_id)
                    return
                # Single-file edit-before-apply: honor the card's edited payload.
                modified = approval.get("modified_content")
                if modified and len(patches_to_apply) == 1:
                    only_path = next(iter(patches_to_apply))
                    patches_to_apply[only_path] = modified
            else:
                # ALLOW (Auto): announce the write BEFORE touching disk so the live
                # action log never shows a silent mutation — apply_patch_set's I/O
                # may take noticeable time behind VFS locks.
                notice = "⚡ Auto-applying approved changes directly to disk…"
                gate.record_answer(len(notice.encode()))
                await vfs_manager.broadcast_token(session_id, notice)

            # 4) Actuate via the VS Code applyEdit bridge (Python never writes to disk).
            from core.write_pipeline import apply_patch_set
            res = await apply_patch_set(session_id, patches_to_apply, base_hashes)
            if res.get("ok"):
                applied = res.get("applied_files") or list(patches_to_apply)
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
            await self._finalize_stream(session_id)
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
        """Render the one-line chat pointer to the rich Plan surface. The full
        outcome, scope, WBS and proposed diffs live in the Plan panel; the chat
        bubble only orients the user toward it (and carries any planner notes)."""
        lines: List[str] = []
        if patches:
            lines.append(
                f"Drafted a plan with {len(patches)} proposed file change(s) — "
                "see the Plan panel for the full breakdown and diffs."
            )
        else:
            lines.append(
                "Drafted a plan but produced no concrete edits for this request — "
                "see the Plan panel."
            )
        if errors:
            lines.append("_Notes:_ " + "; ".join(errors[:5]))
        return "\n".join(lines)

    @staticmethod
    def _build_plan_payload(mission: Any, summary: str) -> "PlanDocumentPayload":
        """Project a MissionSpecification onto the wire payload, carrying the chat
        pointer alongside the structure. Imported lazily to keep the contract
        module off this hot path's import graph."""
        from api.ws_contracts import PlanDocumentPayload

        dump = mission.model_dump() if hasattr(mission, "model_dump") else {}
        return PlanDocumentPayload(
            summary=summary,
            outcome=str(dump.get("outcome", "") or ""),
            scope=list(dump.get("scope") or []),
            constraints=list(dump.get("constraints") or []),
            decisions=list(dump.get("decisions") or []),
            tasks=list(dump.get("tasks") or []),
            checks=list(dump.get("checks") or []),
            ubiquitous_language=dict(dump.get("ubiquitous_language") or {}),
        )

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

    async def _stream_with_thinking(
        self,
        session_id: str,
        messages: List[Dict[str, str]],
        reply_parts: List[str],
        thinking_budget_tokens: int,
    ) -> str:
        """Phase 9 (ADR-707) — demux the thinking-aware gateway stream.

        Routes reasoning deltas → the Thought Box (``broadcast_thinking_chunk``,
        coalesced in a 60 ms window) and answer deltas → the chat bubble
        (``broadcast_token``, 40 ms window). Single-pass, inline time-window
        coalescing using the same monotonic-clock shape as
        ``transport.token_batcher.batch_tokens`` so a stalled upstream never
        blocks the loop and the two channels keep independent windows.

        Reasoning tokens are display-only: appended to neither ``reply_parts``
        nor history (cognitive-isolation invariant). Answer chunks ARE appended
        to ``reply_parts`` (shared with the caller's abort handler, so a partial
        answer is still persisted on Stop). Returns the joined answer text.
        """
        loop = asyncio.get_running_loop()
        _THINK_WINDOW_S = 0.060
        _TEXT_WINDOW_S = 0.040
        _MAX_BUF = 4096

        think_buf: List[str] = []
        text_buf: List[str] = []
        think_chars_total = 0
        think_buf_chars = 0
        text_buf_chars = 0
        think_window_start = loop.time()
        text_window_start = loop.time()

        async def _flush_think() -> None:
            nonlocal think_buf, think_buf_chars
            if think_buf:
                # ~4 chars/token heuristic for the live "N tokens" telemetry;
                # the authoritative billed count flows through the gateway's
                # usage accounting, this is display-only.
                await vfs_manager.broadcast_thinking_chunk(
                    session_id, "".join(think_buf), max(1, think_chars_total // 4)
                )
                think_buf = []
                think_buf_chars = 0

        async def _flush_text() -> None:
            nonlocal text_buf, text_buf_chars
            if text_buf:
                chunk = "".join(text_buf)
                reply_parts.append(chunk)
                await vfs_manager.broadcast_token(session_id, chunk)
                text_buf = []
                text_buf_chars = 0

        raw = LLMGateway.astream_byom_thinking(
            messages,
            tier="medium",
            session_id=session_id,
            enable_thinking=True,
            thinking_budget_tokens=thinking_budget_tokens,
        )
        async for d in raw:
            if d.kind == "thinking":
                think_buf.append(d.text)
                think_buf_chars += len(d.text)
                think_chars_total += len(d.text)
                if (loop.time() - think_window_start) >= _THINK_WINDOW_S or think_buf_chars >= _MAX_BUF:
                    await _flush_think()
                    think_window_start = loop.time()
            else:  # "text" — the answer channel
                # First answer delta after reasoning: flush the remaining
                # thinking so the box is complete before the answer renders.
                if think_buf:
                    await _flush_think()
                text_buf.append(d.text)
                text_buf_chars += len(d.text)
                if (loop.time() - text_window_start) >= _TEXT_WINDOW_S or text_buf_chars >= _MAX_BUF:
                    await _flush_text()
                    text_window_start = loop.time()

        # Drain trailing buffers (completion path; on abort the CancelledError
        # propagates out of the async-for and these never run — partial deltas
        # already went out, which is the desired behaviour).
        await _flush_think()
        await _flush_text()
        return "".join(reply_parts)

    async def _stream_chat_answer(
        self,
        session_id: str,
        task_prompt: str,
        project_id: Optional[str] = None,
        *,
        enable_native_thinking: bool = True,
        thinking_budget_tokens: int = 4096,
    ) -> None:
        """Stream a live completion from the active BYOM chat model to the IDE.

        Injects short-term session memory + GraphRAG snippets. Persists the turn
        only on a successful, non-empty reply. Always finalizes with
        broadcast_stream_end; on failure it broadcasts an actionable message.

        Phase 9 (ADR-707) — when ``enable_native_thinking`` is true the
        thinking-aware gateway path is used: reasoning deltas are coalesced in a
        wider chunk_ms=60 window and routed to ``broadcast_thinking_chunk`` (the
        Thought Box), while answer deltas keep the chunk_ms=40 path. Incapable
        models simply never emit thinking deltas. When false, the legacy
        flat-text ``astream_byom`` path runs unchanged (true zero-regression).
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
            if enable_native_thinking:
                reply = await self._stream_with_thinking(
                    session_id, messages, reply_parts, thinking_budget_tokens
                )
            else:
                # Legacy flat-text path — Phase 7.10.2 (ADR-702/G1): coalesce
                # deltas into chunk_ms=40 frames so the Webview never receives
                # one WS frame per token. Concatenation is preserved, so
                # reply_parts still reconstructs the full answer.
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
                await self._finalize_stream(session_id)
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
                from core.sandbox import get_active_adapter

                adapter = get_active_adapter()
                if adapter is None:
                    raise RuntimeError(
                        "Sandbox adapter not initialized — lifespan startup did "
                        "not resolve a core.sandbox.ACTIVE_ADAPTER."
                    )

                from tools.execution_tools import _sandbox_env

                result = await adapter.execute(
                    args.get("command", ""),
                    timeout_s=float(args.get("timeout_sec", 30.0)),
                    cwd=str(args.get("working_dir") or ""),
                    env_whitelist=_sandbox_env(),  # whitelisted host env only
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
                    status=cast(Literal["success", "error"], spec.status if spec.status in ("success", "error") else "error"),
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
