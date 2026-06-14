"""Wave 3b planner arsenal — deterministic pre-commit verification tools.

Provides two READ_ONLY tools the Planner invokes deterministically after Pydantic
validation succeeds and before the draft is committed to graph state. Both tools
read from the injected state mapping; neither touches disk or the token budget.

Every tool follows the orchestrator_tools.py / analyst_tools.py convention:
  - async-only (_arun); _run raises NotImplementedError
  - shared mutable graph state injected via PrivateAttr (never LLM-visible)
  - bounded output: caps enforced before return (§5.5)
  - pure reads — no state mutation

Tools registered here (all READ_ONLY, allowed_roles={"planner"}):
  validate_wbs_dependencies — forward-reference + out-of-scope + redundant-write detection
  estimate_plan_budget      — heuristic execution cost vs remaining session budget
"""

from __future__ import annotations

import itertools
import json
import logging
import posixpath
from pathlib import PurePosixPath
from typing import Any, Dict, FrozenSet, List, MutableMapping, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema

logger = logging.getLogger("PLANNER_TOOLS")

# ── Output / safety caps ───────────────────────────────────────────────────────
_WBS_MAX_STEPS: int = 200        # cap to prevent context flooding (§5.5)
_MAX_ISSUES_RETURNED: int = 20   # cap on issue list in output

# ── Cost model (conservative — cloud rate, matches token_ledger._USD_PER_K_CLOUD) ──
_CLOUD_USD_PER_K: float = 0.030

# ── Base token estimates by action type ───────────────────────────────────────
_ACTION_BASE_TOKENS: Dict[str, int] = {
    "write_file": 1000,
    "edit_file": 800,
    "read_file": 200,
    "run_command": 100,
}

# ── Role assignment ────────────────────────────────────────────────────────────
_PLANNER_ROLES: FrozenSet[str] = frozenset({"planner"})


def _normalize_scope_path(p: str) -> str:
    """Normalize a path string for hierarchical comparison.

    Strips leading slashes and applies posixpath.normpath so that ./src/,
    /src/, and src/ all collapse to the same canonical form before
    PurePosixPath.is_relative_to() is called.
    """
    return posixpath.normpath(p.lstrip("/"))


# =====================================================================
# ValidateWBSDependenciesTool
# =====================================================================


class ValidateWBSDependenciesInput(BaseModel):
    include_details: bool = Field(
        default=True,
        description="When True, include per-issue detail in the output (step, file, producer).",
    )


class ValidateWBSDependenciesTool(BaseTool):
    """Validate the draft MissionSpecification for structural ordering issues.

    Detects three classes of defect before the plan is committed:

    forward_reference  — a consumer step reads/runs a file that the plan itself
                         creates (write_file) at a later step number. Only flagged
                         for files the plan explicitly creates; pre-existing workspace
                         files are not tracked (no false positives).

    out_of_scope       — a step targets a file that does not fall under any
                         path-like scope boundary declared in mission_spec.scope.
                         Scope entries without '/' are skipped (human-readable
                         constraints, not path boundaries).

    redundant_write    — the same file is written twice with no consumer in between
                         (advisory warning only — valid stays True).

    Returns structured JSON so the caller can inject precise, actionable feedback
    into the Planner's retry prompt.
    """

    name: str = "validate_wbs_dependencies"
    description: str = (
        "Validate the draft mission plan for structural ordering issues: "
        "forward references, out-of-scope files, and redundant writes. "
        "Returns {valid, issues, summary}. Call before committing the plan."
    )
    args_schema: Type[BaseModel] = ValidateWBSDependenciesInput

    _state: MutableMapping[str, Any] = PrivateAttr()

    def __init__(self, *, state: MutableMapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state = state

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("ValidateWBSDependenciesTool is async-only — use _arun().")

    async def _arun(self, include_details: bool = True) -> str:  # noqa: ARG002
        mission = self._state.get("mission_spec")
        if mission is None:
            return json.dumps({"valid": True, "issues": [], "summary": "no mission to validate"})

        # ── Materialize the capped task list (Fix 6: never exhaust an iterator
        # across multiple passes — all passes share this plain list). ──────────
        raw_tasks = getattr(mission, "tasks", None) or []
        try:
            iter(raw_tasks)
        except TypeError:
            raw_tasks = []

        tasks_to_check: List[Any] = list(itertools.islice(raw_tasks, _WBS_MAX_STEPS))
        try:
            truncated: bool = len(raw_tasks) > _WBS_MAX_STEPS
        except TypeError:
            truncated = len(tasks_to_check) == _WBS_MAX_STEPS

        if not tasks_to_check:
            return json.dumps({"valid": True, "issues": [], "summary": "no steps to validate"})

        scope: List[str] = getattr(mission, "scope", None) or []

        # ── Pass 1: Map file operations ────────────────────────────────────────
        # write_steps: files explicitly written/created by the plan.
        # consumer_steps: files read/edited/run by the plan.
        write_steps: Dict[str, List[int]] = {}
        consumer_steps: Dict[str, List[int]] = {}

        for step in tasks_to_check:
            action = getattr(step, "action", "")
            target = getattr(step, "target_file", "")
            num = getattr(step, "step_number", 0)
            if not target:
                continue
            if action == "write_file":
                write_steps.setdefault(target, []).append(num)
            if action in {"read_file", "run_command", "edit_file"}:
                consumer_steps.setdefault(target, []).append(num)

        issues: List[Dict[str, Any]] = []

        # ── Pass 2: Forward-reference detection ───────────────────────────────
        # Only check files the plan explicitly creates (write_file steps present).
        # Pre-existing workspace files (no write_file) are not tracked.
        for file, producers in write_steps.items():
            first_producer = min(producers)
            for consumer_step in consumer_steps.get(file, []):
                if consumer_step < first_producer:
                    issues.append(
                        {
                            "type": "forward_reference",
                            "step_number": consumer_step,
                            "target_file": file,
                            "first_producer": first_producer,
                        }
                    )

        # ── Pass 3: Out-of-scope detection ────────────────────────────────────
        # Path-like scope entries (contain '/') define directory boundaries.
        # Normalize both sides with posixpath.normpath before PurePosixPath
        # comparison to eliminate ./  ../ and trailing-slash mismatches (Fix 7).
        path_like_scopes = [s for s in scope if "/" in s]
        skip_scope_check = not path_like_scopes

        if not skip_scope_check:
            norm_scopes = [_normalize_scope_path(s) for s in path_like_scopes]
            for step in tasks_to_check:
                target = getattr(step, "target_file", "")
                num = getattr(step, "step_number", 0)
                if not target:
                    continue
                norm_target = _normalize_scope_path(target)
                in_scope = any(
                    PurePosixPath(norm_target).is_relative_to(PurePosixPath(ns))
                    for ns in norm_scopes
                )
                if not in_scope:
                    issues.append(
                        {
                            "type": "out_of_scope",
                            "step_number": num,
                            "target_file": target,
                        }
                    )

        # ── Pass 4: Redundant-write detection (advisory — does not block) ─────
        last_write: Dict[str, int] = {}
        last_consumer: Dict[str, int] = {}
        ordered = sorted(tasks_to_check, key=lambda s: getattr(s, "step_number", 0))
        for step in ordered:
            action = getattr(step, "action", "")
            target = getattr(step, "target_file", "")
            num = getattr(step, "step_number", 0)
            if not target:
                continue
            if action == "write_file":
                prev_write = last_write.get(target)
                prev_consume = last_consumer.get(target, -1)
                if prev_write is not None and prev_consume < prev_write:
                    issues.append(
                        {
                            "type": "redundant_write",
                            "step_number": num,
                            "target_file": target,
                            "previous_write_at": prev_write,
                        }
                    )
                last_write[target] = num
            elif action in {"read_file", "run_command", "edit_file"}:
                last_consumer[target] = num

        # ── Cap, classify, summarise ──────────────────────────────────────────
        issues = issues[:_MAX_ISSUES_RETURNED]
        blocking_types = {"forward_reference", "out_of_scope"}
        valid = not any(i["type"] in blocking_types for i in issues)

        parts = []
        if truncated:
            parts.append(f"first {_WBS_MAX_STEPS} of {len(raw_tasks)} steps checked")
        if skip_scope_check:
            parts.append("scope_format_not_path_checkable")
        n_blocking = sum(1 for i in issues if i["type"] in blocking_types)
        if n_blocking:
            parts.append(f"{n_blocking} blocking issue(s) found")
        elif issues:
            parts.append(f"{len(issues)} advisory issue(s) found (plan is valid)")
        else:
            parts.append("plan is structurally valid")

        summary = "; ".join(parts)
        return json.dumps({"valid": valid, "issues": issues, "summary": summary})


# =====================================================================
# BudgetEstimatorTool
# =====================================================================


class BudgetEstimatorInput(BaseModel):
    include_breakdown: bool = Field(
        default=False,
        description="When True, include a per-step cost breakdown in the output.",
    )


class BudgetEstimatorTool(BaseTool):
    """Estimate the execution token cost of the draft plan vs remaining session budget.

    Provides a heuristic pre-commit cost check so the Planner can detect budget
    overruns before execution (shift-left of the oom_fallback mechanism). Cost is
    estimated using fixed tokens-per-action-type at the cloud rate; confidence is
    therefore always 'low' — a calibrated model is DEBT-045.

    Advisory only: a budget overage does not cause this tool to raise or to return
    valid=False. The caller decides whether to hard-reject or warn.
    """

    name: str = "estimate_plan_budget"
    description: str = (
        "Estimate the execution token cost of the draft mission plan and compare it "
        "against the remaining session budget. Returns {estimated_cost_usd, "
        "remaining_budget_usd, fits_within_budget, confidence, margin_usd, step_count}."
    )
    args_schema: Type[BaseModel] = BudgetEstimatorInput

    _state: MutableMapping[str, Any] = PrivateAttr()

    def __init__(self, *, state: MutableMapping[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state = state

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("BudgetEstimatorTool is async-only — use _arun().")

    async def _arun(self, include_breakdown: bool = False) -> str:
        budget: float = float(self._state.get("session_max_budget_usd") or 5.0)
        spent: float = float(self._state.get("accumulated_session_cost") or 0.0)
        remaining: float = budget - spent

        mission = self._state.get("mission_spec")
        raw_tasks = getattr(mission, "tasks", None) or []
        try:
            iter(raw_tasks)
        except TypeError:
            raw_tasks = []

        tasks_to_check: List[Any] = list(itertools.islice(raw_tasks, _WBS_MAX_STEPS))

        total_tokens: int = 0
        breakdown: List[Dict[str, Any]] = []

        for step in tasks_to_check:
            action = getattr(step, "action", "read_file")
            description = getattr(step, "description", "") or ""
            num = getattr(step, "step_number", 0)

            base = _ACTION_BASE_TOKENS.get(action, 200)
            desc_tokens = len(description) // 4
            step_tokens = base + desc_tokens
            total_tokens += step_tokens

            if include_breakdown:
                step_cost = step_tokens / 1000 * _CLOUD_USD_PER_K
                breakdown.append(
                    {
                        "step_number": num,
                        "action": action,
                        "estimated_tokens": step_tokens,
                        "estimated_cost_usd": round(step_cost, 6),
                    }
                )

        estimated_cost = total_tokens / 1000 * _CLOUD_USD_PER_K
        margin = remaining - estimated_cost

        payload: Dict[str, Any] = {
            "estimated_cost_usd": round(estimated_cost, 6),
            "remaining_budget_usd": round(remaining, 6),
            "fits_within_budget": estimated_cost <= remaining,
            "confidence": "low",
            "margin_usd": round(margin, 6),
            "step_count": len(tasks_to_check),
        }
        if include_breakdown:
            payload["breakdown"] = breakdown

        return json.dumps(payload)


# =====================================================================
# Schema registration helper + register_planner_tools
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
        allowed_roles=_PLANNER_ROLES,
    )


async def register_planner_tools(store: ToolRAGStore) -> int:
    """Register the 2 planner-scoped schemas in the given store. Returns count."""
    schemas: List[ToolSchema] = [
        _tool_schema(
            "validate_wbs_dependencies",
            "Validate the draft mission plan for structural ordering issues.",
            ValidateWBSDependenciesInput,
        ),
        _tool_schema(
            "estimate_plan_budget",
            "Estimate the execution token cost of the draft mission plan vs remaining budget.",
            BudgetEstimatorInput,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
