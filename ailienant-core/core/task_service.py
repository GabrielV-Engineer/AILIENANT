import json
import os
import re
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from .vfs_middleware import VFSMiddleware, DirtyBuffer
from brain.state import ManualAttachment
from api.websocket_manager import vfs_manager
from tools.llm_gateway import LLMGateway
import logging

logger = logging.getLogger(__name__)


# Phase 7.9.B.13 — persona for the live main-chat completion (direct BYOM call).
_CHAT_SYSTEM_PROMPT: str = (
    "You are AILIENANT, an expert AI coding assistant embedded in the user's IDE. "
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

    async def _run_coding_task(
        self, session_id: str, payload: TaskPayload, execution_mode: str
    ) -> None:
        """Drive the planner + coder agents to propose patches (review-only, no disk write)."""
        from agents.planner import run_planner_node  # deferred — avoids import cycle
        from agents.coder import run_coder_node

        state = self._build_initial_state(session_id, payload, execution_mode)

        await vfs_manager.broadcast_pipeline_step(session_id, "planner_agent")
        try:
            plan = await run_planner_node(state)
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
        for step in list(mission.tasks)[:_MAX_CODER_STEPS]:
            await vfs_manager.broadcast_pipeline_step(
                session_id, "coder_agent", step_id=step.step_number
            )
            try:
                cres = await run_coder_node(
                    {**state, "mission_spec": mission, "current_step_id": step.step_number}
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"step #{step.step_number}: {exc}")
                continue
            patches.update(cres.get("pending_patches") or {})
            contents.update(cres.get("pending_contents") or {})
            base_hashes.update(cres.get("pending_base_hash") or {})
            errors.extend(cres.get("errors") or [])

        # 1) Stream the plan + proposed diffs as a reviewable message.
        summary = self._format_coding_summary(mission, patches, errors)
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
        )
        if not decision or not decision.get("approved"):
            await vfs_manager.broadcast_token(
                session_id, "Changes discarded — no files were modified."
            )
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
        await vfs_manager.broadcast_token(session_id, result_msg)
        await vfs_manager.broadcast_stream_end(session_id)
        self._append_history(session_id, "assistant", result_msg)

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
        try:
            async for delta in LLMGateway.astream_byom(
                messages, tier="medium", session_id=session_id
            ):
                reply_parts.append(delta)
                await vfs_manager.broadcast_token(session_id, delta)
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
        except Exception as exc:  # noqa: BLE001 — a chat failure must never crash the task
            logger.warning("Live chat completion failed: %s", exc)
            await vfs_manager.broadcast_token(
                session_id,
                "I couldn't reach the configured model. Activate a BYOM preset "
                "(Dashboard → BYOM) and make sure its engine is running, then try again.",
            )
        finally:
            await vfs_manager.broadcast_stream_end(session_id)
