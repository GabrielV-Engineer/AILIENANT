# ailienant-core/brain/subagent_contracts.py
"""Structured, schema-validated contracts for dynamic subagent dispatch.

These models replace the "LLM writes an executable script" pattern with "LLM
emits data validated by Pydantic". Every dispatch instruction a model authors is
parsed into one of these closed-vocabulary objects — exactly as a planner's output
is parsed into a MissionSpecification — so a hallucinated shape fails fast at the
boundary instead of executing as arbitrary code.

Kept as a dedicated leaf module (its only project import is the shared token-hygiene
constant) so the contracts stay independently testable and importable without
dragging the tool-dispatch or permission machinery along.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from shared.config import MAX_OBSERVATION_CHARS


class SubagentResponseField(BaseModel):
    """One field in a subagent's expected structured return shape."""

    name: str
    type: Literal["str", "int", "float", "bool", "list_str"]
    description: str


class SubagentResponseSchema(BaseModel):
    """The structured shape a subagent must return.

    A closed field-type vocabulary — deliberately NOT a free-form JSON-schema
    dict. Modelling the return shape as a bounded list of typed field records
    keeps validation and per-result truncation tractable, and prevents an
    LLM-authored schema from smuggling in an arbitrary nested structure.
    """

    fields: List[SubagentResponseField] = Field(min_length=1, max_length=8)


class SubagentTask(BaseModel):
    """One unit of dispatch work — the structured analog of a
    ``task(description, subagent_role, response_schema)`` call.

    There is deliberately no ``session_permission_mode`` field: a subagent can
    never request a more permissive mode than its parent turn, so no wire path to
    self-escalate is exposed. The dispatching node copies the permission mode from
    the parent's own state; it is never carried on the task.
    """

    task_id: str                      # uuid4 hex, caller-assigned — the idempotency key
    description: str = Field(max_length=4000)
    subagent_role: Literal[
        "core_dev", "architect_refactor", "devops_infra", "secops",
        "qa_tester", "doc_manager", "vcs_manager", "data_ml_engineer",
        "analyst_readonly",   # verification/critic role — floor-locked to READ_ONLY when wired
    ]
    response_schema: SubagentResponseSchema
    context_refs: List[str] = Field(default_factory=list, max_length=20)  # VFS paths, never raw content
    max_iterations: int = Field(default=1, ge=1, le=8)


class DispatchPlan(BaseModel):
    """A structured, schema-validated fan-out plan.

    The Pydantic bounds are the first line of defence against an over-scoped
    plan: ``dispatch_depth`` caps recursion and ``tasks`` caps fan-out width, so a
    runaway plan is rejected at construction rather than after it has spawned work.
    """

    pattern: Literal[
        "classify_and_act", "fanout_and_synthesize", "adversarial_verification",
        "generate_and_filter", "tournament", "loop_until_done",
    ]
    tasks: List[SubagentTask] = Field(min_length=1, max_length=32)  # hard fan-out width ceiling
    synthesis_instruction: str = Field(max_length=2000)
    dispatch_depth: int = Field(default=0, ge=0, le=2)              # recursion ceiling


class SubagentResultEnvelope(BaseModel):
    """Result of ONE subagent call.

    ``raw_digest`` is ALWAYS pre-truncated before construction — never a full
    transcript. Its cap is single-sourced from the same token-hygiene constant the
    tool-dispatch loop enforces, so the two ceilings can never drift apart.
    """

    task_id: str
    status: Literal["ok", "error", "budget_exhausted", "denied"]
    structured_result: Optional[Dict[str, Any]] = None  # validated against the task's response_schema
    raw_digest: str = Field(max_length=MAX_OBSERVATION_CHARS)
    cost_usd: float = 0.0
    iterations_used: int = 0
    error_message: Optional[str] = None


class DispatchBatchResult(BaseModel):
    """Aggregate of one dispatch — this, not N raw transcripts, folds into the
    graph state.

    ``pattern`` is an open ``str`` here (not the ``DispatchPlan`` Literal): the
    result side records what ran without re-imposing the plan-side vocabulary.
    """

    batch_id: str
    pattern: str
    results: List[SubagentResultEnvelope]
    total_cost_usd: float
    winner_task_id: Optional[str] = None  # populated for tournament / generate_and_filter
