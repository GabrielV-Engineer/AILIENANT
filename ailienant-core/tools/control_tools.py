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
from typing import Any, Dict, FrozenSet, List, Literal, MutableMapping, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
# ALL_ROLES is re-exported (redundant-alias form): shared.rbac owns the role
# universe now, but this module has been its import site since the universal
# tools were written, and rewriting those call sites would be churn for no gain.
from shared.rbac import ALL_ROLES as ALL_ROLES
from shared.rbac import DEV_ROLES

logger = logging.getLogger("CONTROL_TOOLS")


# =====================================================================
# Shared constants & helpers
# =====================================================================

_CONTROL_ROLES: FrozenSet[str] = DEV_ROLES
"""All 8 canonical developer roles. Any agent may request HITL or self-mode its
session. Aliased rather than restated so a new dev role reaches these CONTROL tools
the moment it is added to the canonical set."""


_CONTROL_ROLES_WITH_ORCHESTRATOR: FrozenSet[str] = _CONTROL_ROLES | frozenset({"orchestrator"})
"""The 8 canonical roles plus the orchestrator — the orchestrator may also self-mode
its session and surface questions to the operator through these CONTROL tools."""


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
# AskUserQuestionTool
# =====================================================================


class AskUserQuestionOptionInput(BaseModel):
    label: str = Field(description="Short option text shown to the operator.")
    description: Optional[str] = Field(
        default=None,
        description="One sentence of rationale or trade-off for this option.",
    )
    recommended: bool = Field(
        default=False,
        description="Set true on exactly one option per question — the one you would pick.",
    )


class AskUserQuestionItem(BaseModel):
    header: str = Field(
        description="Very short label (<=3 words) identifying this question in a tab."
    )
    question: str = Field(description="The full question to ask.")
    context: Optional[str] = Field(
        default=None, description="Optional background to inform the answer."
    )
    options: List[AskUserQuestionOptionInput] = Field(
        description=(
            "2 to 4 concrete, mutually exclusive answers. Always populate this "
            "when the answer space is enumerable — never leave it empty just to "
            "fall back on free text."
        )
    )
    multi_select: bool = Field(
        default=False,
        description="True if the operator may pick more than one option for this question.",
    )


class AskUserQuestionInput(BaseModel):
    question: Optional[str] = Field(
        default=None,
        description=(
            "Legacy single free-form question. Prefer `questions` below whenever "
            "the answer space is enumerable; only use this bare form for a truly "
            "open-ended question with no sensible fixed options."
        ),
    )
    context: Optional[str] = Field(
        default=None,
        description="Optional context block to inform the operator's answer (single-question mode).",
    )
    suggested_options: Optional[List[str]] = Field(
        default=None,
        description="Optional structured choices the operator can pick from (single-question mode).",
    )
    questions: Optional[List[AskUserQuestionItem]] = Field(
        default=None,
        description=(
            "One to four related questions to ask in a single pause, each with its "
            "own concrete options. Batch questions the operator needs to answer "
            "together instead of calling this tool repeatedly."
        ),
    )


class GrillQuestionBatch(BaseModel):
    """A batch of structured questions an interviewing agent currently needs
    answered, reusing AskUserQuestionItem/AskUserQuestionOptionInput's shape so
    the ideation "Grill Me" flow (agents/analyst.py) and ask_user_question share
    one question/option model. An empty `questions` list is the completion
    signal — the agent has enough shared understanding to proceed without
    asking anything further this round."""

    questions: List[AskUserQuestionItem] = Field(default_factory=list)


def questions_to_pending_dicts(questions: List[AskUserQuestionItem]) -> List[Dict[str, Any]]:
    """Serialize a batch of AskUserQuestionItem into the plain-dict shape
    `pending_hitl_request`/`request_graph_clarification` expect, assigning each
    a stable `q{i}` correlation id. Shared by AskUserQuestionTool._arun and the
    ideation Grill Me flow so the id-assignment/serialization logic exists once."""
    return [
        {
            "id": f"q{i}",
            "header": item.header,
            "question": item.question,
            "context": item.context,
            "options": [opt.model_dump() for opt in item.options],
            "multi_select": item.multi_select,
        }
        for i, item in enumerate(questions)
    ]


class AskUserQuestionTool(BaseTool):
    """Pause the agent and surface a question to the operator.

    Behaviour: writes a structured payload into state['pending_hitl_request'].
    The agentic cell's fallback-dispatch loop detects the populated channel
    right after this call dispatches, defers (stops processing further tool
    calls this super-step) rather than suspending inline, and a dedicated
    clarification-resume phase on the NEXT iteration is what actually calls
    interrupt() — never this tool itself, since interrupt() cannot run safely
    mid-dispatch-loop. See brain/agentic_cell.py's clarification-resume phase.
    """

    name: str = "ask_user_question"
    description: str = (
        "Pause the agent and surface a question to the human operator. "
        "Sets state['pending_hitl_request']; the caller's dispatch loop "
        "detects the populated channel and defers to a suspend-and-resume "
        "phase on the next iteration. Cleared once the operator answers."
    )
    args_schema: Type[BaseModel] = AskUserQuestionInput  # pyright: ignore[reportIncompatibleVariableOverride]

    _state: MutableMapping[str, Any] = PrivateAttr()

    def __init__(self, *, state: MutableMapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state = state

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("AskUserQuestionTool is async-only — use _arun().")

    async def _arun(
        self,
        question: Optional[str] = None,
        context: Optional[str] = None,
        suggested_options: Optional[List[str]] = None,
        questions: Optional[List[AskUserQuestionItem]] = None,
    ) -> str:
        if not questions and not question:
            raise ValueError(
                "ask_user_question requires either `question` (single-question "
                "mode) or `questions` (batch mode)."
            )
        request_id = uuid.uuid4().hex
        request: dict[str, Any] = {
            "request_id": request_id,
            "kind": "ASK_USER_QUESTION",
            "requested_at": _now_iso(),
        }
        if questions:
            request["questions"] = questions_to_pending_dicts(questions)
            log_summary = f"{len(questions)} question(s)"
        else:
            request["question"] = question
            request["context"] = context
            request["suggested_options"] = list(suggested_options) if suggested_options else []
            log_summary = repr(question)
        self._state["pending_hitl_request"] = request
        logger.info("ask_user_question: HITL requested id=%s question=%s", request_id, log_summary)
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
    args_schema: Type[BaseModel] = TogglePlanModeInput  # pyright: ignore[reportIncompatibleVariableOverride]

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
