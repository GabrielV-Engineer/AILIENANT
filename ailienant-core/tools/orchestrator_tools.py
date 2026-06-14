"""Wave 3 orchestrator arsenal — net-new READ_ONLY introspection / control tools.

Re-channels two operations the orchestrator graph node currently performs by direct
state mutation into audited, retrievable Tool RAG tools, so the same introspection is
invocable (and logged) from outside the graph — e.g. by the external MCP gateway.

Every tool follows the control_tools.py / analyst_tools.py convention:
  - async-only (_arun); _run raises NotImplementedError
  - the shared mutable graph state is injected via PrivateAttr + __init__ (never LLM-visible)
  - bounded output: caps enforced before return

Tools registered here (all READ_ONLY, allowed_roles={"orchestrator"}):
  get_wbs_status     — aggregate + per-step view of mission_spec.tasks (read-only)
  emit_hitl_request  — audited, idempotent HITL_APPROVAL_REQUIRED gate emission

CONTROL classification: both tools register READ_ONLY so the session-mode filter
(PLAN -> READ_ONLY only) admits them in every mode, exactly as ask_user_question /
toggle_plan_mode do. They coordinate; they never touch disk or budget.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
from datetime import datetime, timezone
from typing import Any, FrozenSet, List, MutableMapping, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema

logger = logging.getLogger("ORCHESTRATOR_TOOLS")

# ── Output caps (token hygiene §5.5) ──────────────────────────────────────────
_WBS_MAX_STEPS: int = 200   # bound the per-step rows so a pathological WBS can't flood context
_REASON_MAX_CHARS: int = 256

# ── Role assignment ────────────────────────────────────────────────────────────
_ORCHESTRATOR_ROLES: FrozenSet[str] = frozenset({"orchestrator"})

# Terminal WBS statuses, used to locate the first still-active step.
_TERMINAL_STATUSES: FrozenSet[str] = frozenset({"completed", "failed"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# GetWBSStatusTool
# =====================================================================


class WBSStatusInput(BaseModel):
    include_steps: bool = Field(
        default=True,
        description="When False, return only the aggregate status counts (cheaper).",
    )


class GetWBSStatusTool(BaseTool):
    """Report Work Breakdown Structure progress from the live mission specification.

    Reads state['mission_spec'].tasks and returns aggregate status counts plus the
    active (first non-terminal) step. Pure read — never mutates state, never raises
    into the LLM: a missing or malformed mission degrades to a structured payload.
    """

    name: str = "get_wbs_status"
    description: str = (
        "Report Work Breakdown Structure progress: per-status counts, the active "
        "step, and (optionally) per-step rows. Read-only view of the live mission."
    )
    args_schema: Type[BaseModel] = WBSStatusInput

    _state: MutableMapping[str, Any] = PrivateAttr()

    def __init__(self, *, state: MutableMapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state = state

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("GetWBSStatusTool is async-only — use _arun().")

    async def _arun(self, include_steps: bool = True) -> str:
        mission = self._state.get("mission_spec")
        # Type safety: never trust the internals of a dynamically-injected object. A
        # prior Planner fault could leave `tasks` as None or a non-iterable; iterating
        # that blindly would raise a synchronous TypeError and crash the node.
        tasks = getattr(mission, "tasks", None) or []
        try:
            iter(tasks)
        except TypeError:
            tasks = []

        if mission is None or not tasks:
            return json.dumps({"status": "no_mission", "tasks": []})

        counts = {"pending": 0, "in_progress": 0, "completed": 0, "failed": 0}
        active_step: Optional[int] = None
        for step in tasks:
            status = getattr(step, "status", "pending")
            if status in counts:
                counts[status] += 1
            if active_step is None and status not in _TERMINAL_STATUSES:
                active_step = getattr(step, "step_number", None)

        payload: dict[str, Any] = {
            "status": "ok",
            "total": len(tasks),
            "counts": counts,
            "active_step": active_step,
        }

        if include_steps:
            rows: List[dict[str, Any]] = []
            for step in itertools.islice(tasks, _WBS_MAX_STEPS):
                rows.append(
                    {
                        "step_number": getattr(step, "step_number", None),
                        "target_role": getattr(step, "target_role", None),
                        "action": getattr(step, "action", None),
                        "status": getattr(step, "status", None),
                        "target_file": getattr(step, "target_file", None),
                    }
                )
            payload["tasks"] = rows
            payload["truncated"] = len(tasks) > _WBS_MAX_STEPS

        return json.dumps(payload)


# =====================================================================
# EmitHITLRequestTool
# =====================================================================


class EmitHITLRequestInput(BaseModel):
    target_role: str = Field(
        description="The RBAC role whose action requires human approval (e.g. 'devops_infra')."
    )
    trigger: str = Field(
        description="The matched HITL trigger token (e.g. '.env', '--force')."
    )
    reason: Optional[str] = Field(
        default=None,
        description="Optional human-readable justification for the approval gate.",
    )


def _sanitize_flag_field(value: str) -> str:
    """Strip the colon and newline characters that delimit / corrupt the flag string.

    The emitted flag is colon-delimited and consumed downstream via split(':'). Both
    target_role and trigger are LLM/prompt-controlled, so an injected ':' or newline
    would desynchronise every static parser that reads the control channel.
    """
    return value.replace(":", "_").replace("\n", " ").replace("\r", " ").strip()


class EmitHITLRequestTool(BaseTool):
    """Raise an audited, idempotent human-in-the-loop approval gate.

    Emits the canonical ``HITL_APPROVAL_REQUIRED:<role>:<trigger>`` flag the graph
    already understands and records an audit entry in state['hitl_approval_requests'].
    The request_id is a deterministic hash of the sanitized flag, so re-issuing the
    same gate (LLM ReAct retries, network re-sends) is idempotent even if the audit
    channel is not persisted across a checkpointer turn.
    """

    name: str = "emit_hitl_request"
    description: str = (
        "Raise an audited human-in-the-loop approval gate for a role-specific "
        "trigger. Emits the canonical HITL_APPROVAL_REQUIRED flag and records an "
        "idempotent audit entry. Use before a sensitive action proceeds."
    )
    args_schema: Type[BaseModel] = EmitHITLRequestInput

    _state: MutableMapping[str, Any] = PrivateAttr()

    def __init__(self, *, state: MutableMapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state = state

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("EmitHITLRequestTool is async-only — use _arun().")

    async def _arun(
        self,
        target_role: str,
        trigger: str,
        reason: Optional[str] = None,
    ) -> str:
        role_s = _sanitize_flag_field(target_role)
        trigger_s = _sanitize_flag_field(trigger)

        # Empty-field guard: a field sanitized to empty would yield a malformed
        # "HITL_APPROVAL_REQUIRED::" flag that no parser can route — refuse to emit.
        if not role_s or not trigger_s:
            return json.dumps(
                {"error": "target_role and trigger are required and must be non-empty"}
            )

        # Reason hygiene: strip newlines and bound length before it reaches the audit
        # record / log line (no log injection, no context bloat).
        reason_s: Optional[str] = None
        if reason is not None:
            reason_s = reason.replace("\n", " ").replace("\r", " ").strip()[:_REASON_MAX_CHARS]

        flag = f"HITL_APPROVAL_REQUIRED:{role_s}:{trigger_s}"

        # Deterministic id: re-issuing the identical gate always yields the same id, so
        # idempotency survives a dropped (unpersisted) audit channel. blake2b matches the
        # agent_tools.py hashing idiom; determinism — not the algorithm — is the requirement.
        request_id = hashlib.blake2b(flag.encode("utf-8"), digest_size=4).hexdigest()

        # Best-effort audit append (within-turn dedup by request_id). The channel is an
        # audit aid; correctness rests on the deterministic id above, not its persistence.
        channel = self._state.setdefault("hitl_approval_requests", [])
        if not any(entry.get("request_id") == request_id for entry in channel):
            channel.append(
                {
                    "request_id": request_id,
                    "flag": flag,
                    "target_role": role_s,
                    "trigger": trigger_s,
                    "reason": reason_s,
                    "requested_at": _now_iso(),
                }
            )

        logger.info("emit_hitl_request: id=%s flag=%s", request_id, flag)
        return f"[emit_hitl_request] HITL_GATE:{request_id}"


# =====================================================================
# Schema registration helper + register_orchestrator_tools
# =====================================================================


def _tool_schema(
    name: str,
    description: str,
    json_schema_class: Type[BaseModel],
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(json_schema_class.model_json_schema(), default=str),
        privilege_tier=ToolPrivilegeTier.READ_ONLY,
        allowed_roles=_ORCHESTRATOR_ROLES,
    )


async def register_orchestrator_tools(store: ToolRAGStore) -> int:
    """Register the 2 orchestrator-scoped schemas in the given store. Returns count."""
    schemas: List[ToolSchema] = [
        _tool_schema(
            "get_wbs_status",
            "Report Work Breakdown Structure progress from the live mission specification.",
            WBSStatusInput,
        ),
        _tool_schema(
            "emit_hitl_request",
            "Raise an audited, idempotent human-in-the-loop approval gate.",
            EmitHITLRequestInput,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
