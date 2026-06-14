"""Phase 5.6 — Cognitive Control Tools (matrix-bypass / CONTROL classification).

Two BaseTool subclasses that let the agent re-mode itself or pause for a human:

    AskUserQuestionTool   — sets state["pending_hitl_request"]; orchestrator
                            graph node detects the populated channel and
                            suspends the turn. Cleared when the WebView posts
                            a structured hitl_response.
    TogglePlanModeTool    — self-mutates state["session_permission_mode"] with
                            one of {DEFAULT, PLAN, AUTO}. The Permission Engine
                            consults this channel on every tool dispatch.

Per the PHASE_5_BLUEPRINT.md §4 line 277 design intent ("CONTROL — policy-neutral
across the matrix"), both tools are registered with `ToolPrivilegeTier.READ_ONLY`
so the session-mode filter (PLAN → READ_ONLY only) admits them in every session
mode. core/permissions.py is NEVER modified by this module; the enum is only
imported.

Also exports DANGEROUS_COMMANDS_REGEX — the canonical attack-pattern list
consumed by tools.execution_tools.SandboxBashTool's HITL interceptor. The list
is the asymmetric-friction primitive: matches block the subprocess spawn and
redirect the agent to AskUserQuestionTool for explicit human approval.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, FrozenSet, List, Literal, MutableMapping, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema

logger = logging.getLogger("CONTROL_TOOLS")


# =====================================================================
# Shared constants & helpers
# =====================================================================

_CONTROL_ROLES: FrozenSet[str] = frozenset(
    {
        "core_dev",
        "architect_refactor",
        "qa_tester",
        "secops",
        "doc_manager",
        "data_ml_engineer",
        "devops_infra",
        "vcs_manager",
    }
)
"""All 8 canonical roles. Any agent may request HITL or self-mode its session."""


_CONTROL_ROLES_WITH_ORCHESTRATOR: FrozenSet[str] = _CONTROL_ROLES | frozenset({"orchestrator"})
"""The 8 canonical roles plus the orchestrator — the orchestrator may also self-mode
its session and surface questions to the operator through these CONTROL tools."""


ALL_ROLES: FrozenSet[str] = _CONTROL_ROLES | frozenset(
    {"researcher", "analyst", "planner", "orchestrator"}
)
"""The authoritative role universe: the 8 canonical coder roles plus the four cognitive
graph-node roles (researcher, analyst, planner, orchestrator). Universal tools — tool
discovery and the TODO scratchpad — are visible to every one of them, so this is the
`allowed_roles` they register with."""


DANGEROUS_COMMANDS_REGEX: List[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf?\b", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+(table|database|schema)\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=.*of=/dev/", re.IGNORECASE),
    re.compile(r":\(\)\s*\{.*:&\s*\};:"),
    re.compile(r"\bmkfs(\.|\s)", re.IGNORECASE),
    re.compile(r"\bchmod\s+-R\s+777\b", re.IGNORECASE),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"\b(curl|wget)\s+.*\|\s*(sudo\s+)?(bash|sh|zsh)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push.*--force\b", re.IGNORECASE),
]
"""Asymmetric-friction pattern list. Imported by execution_tools.SandboxBashTool."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# Task D — AskUserQuestionTool
# =====================================================================


class AskUserQuestionInput(BaseModel):
    question: str = Field(
        description="Natural-language question to surface to the human operator."
    )
    context: Optional[str] = Field(
        default=None,
        description="Optional context block to inform the operator's answer.",
    )
    suggested_options: Optional[List[str]] = Field(
        default=None,
        description="Optional structured choices the operator can pick from.",
    )


class AskUserQuestionTool(BaseTool):
    """Pause the agent and surface a question to the operator.

    Behaviour: writes a structured payload into state['pending_hitl_request']
    and returns a sentinel string carrying the request_id. The orchestrator's
    graph node detects the populated channel and suspends the turn until a
    matching hitl_response arrives from the WebView.
    """

    name: str = "ask_user_question"
    description: str = (
        "Pause the agent and surface a question to the human operator. "
        "Sets state['pending_hitl_request']; the orchestrator graph node "
        "detects the populated channel and suspends the turn. Cleared when "
        "the WebView posts a structured hitl_response."
    )
    args_schema: Type[BaseModel] = AskUserQuestionInput

    _state: MutableMapping[str, Any] = PrivateAttr()

    def __init__(self, *, state: MutableMapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state = state

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("AskUserQuestionTool is async-only — use _arun().")

    async def _arun(
        self,
        question: str,
        context: Optional[str] = None,
        suggested_options: Optional[List[str]] = None,
    ) -> str:
        request_id = uuid.uuid4().hex
        self._state["pending_hitl_request"] = {
            "request_id": request_id,
            "kind": "ASK_USER_QUESTION",
            "question": question,
            "context": context,
            "suggested_options": list(suggested_options) if suggested_options else [],
            "requested_at": _now_iso(),
        }
        logger.info(
            "ask_user_question: HITL requested id=%s question=%r", request_id, question
        )
        return f"[ask_user_question] HITL_PENDING:{request_id}"


# =====================================================================
# Task E — TogglePlanModeTool
# =====================================================================


class TogglePlanModeInput(BaseModel):
    mode: Literal["DEFAULT", "PLAN", "AUTO"] = Field(description="Target mode.")


class TogglePlanModeTool(BaseTool):
    """Self-mutate the session permission mode.

    Use PLAN to de-escalate (READ_ONLY-only turn), AUTO to self-escalate for
    routine work, DEFAULT to reset. The Permission Engine consults
    state['session_permission_mode'] on every tool dispatch.
    """

    name: str = "toggle_plan_mode"
    description: str = (
        "Self-mutate the session permission mode (DEFAULT / PLAN / AUTO). "
        "Use PLAN to de-escalate (READ_ONLY-only turn), AUTO to self-escalate "
        "for routine work, DEFAULT to reset. The Permission Engine consults "
        "this channel on every tool dispatch."
    )
    args_schema: Type[BaseModel] = TogglePlanModeInput

    _state: MutableMapping[str, Any] = PrivateAttr()

    def __init__(self, *, state: MutableMapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state = state

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("TogglePlanModeTool is async-only — use _arun().")

    async def _arun(self, mode: Literal["DEFAULT", "PLAN", "AUTO"]) -> str:
        previous = self._state.get("session_permission_mode", "DEFAULT")
        self._state["session_permission_mode"] = mode
        logger.info("toggle_plan_mode: %s -> %s", previous, mode)
        return f"[toggle_plan_mode] {previous} -> {mode}"


# =====================================================================
# Task H — Schema registration
# =====================================================================


def _control_schema(
    name: str,
    description: str,
    input_model: Type[BaseModel],
    *,
    allowed_roles: FrozenSet[str] = _CONTROL_ROLES,
) -> ToolSchema:
    """Build a ToolSchema for a CONTROL-classified tool.

    The privilege_tier is READ_ONLY — the simplest way to satisfy the
    "policy-neutral across the matrix" requirement without extending the
    ToolPrivilegeTier enum. ``allowed_roles`` defaults to the 8 canonical roles;
    callers pass a widened set to additively admit another role (e.g. orchestrator).
    """
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(input_model.model_json_schema(), default=str),
        privilege_tier=ToolPrivilegeTier.READ_ONLY,
        allowed_roles=allowed_roles,
    )


async def register_control_tools(store: ToolRAGStore) -> int:
    """Register the 2 CONTROL-classified schemas in the given store. Returns count."""
    schemas: List[ToolSchema] = [
        _control_schema(
            "ask_user_question",
            "Pause the agent and surface a structured question to the human operator.",
            AskUserQuestionInput,
            allowed_roles=_CONTROL_ROLES_WITH_ORCHESTRATOR,
        ),
        _control_schema(
            "toggle_plan_mode",
            "Self-mutate session_permission_mode (DEFAULT / PLAN / AUTO).",
            TogglePlanModeInput,
            allowed_roles=_CONTROL_ROLES_WITH_ORCHESTRATOR,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
