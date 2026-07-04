# ailienant-core/brain/nodes/subagent_worker_node.py
"""The subagent_worker graph node — one dispatched subagent invocation.

A thin, narrow-contract node (deliberately NOT a reuse of the coder node, which
returns a VFS patch): it reads its own ``_dispatch_task`` slice, runs a bounded,
role-gated tool loop via the existing ``ToolDispatcher``, synthesises a final answer
constrained to the task's ``response_schema``, and returns exactly one
``SubagentResultEnvelope`` on the reducer-guarded ``_dispatch_results`` fan-in
channel. It never raises — a fault becomes a ``status="error"`` envelope so a single
faulty subagent can never crash the host graph.

Tool arsenals: the READ_ONLY critic role maps to the analyst tool set; every other
role currently runs tool-less (a pure-reasoning subagent), since per-role executable
tool maps for the developer roles do not yet exist. Both the tool reasoner and the
final-answer synthesiser are injectable through ``config.configurable`` so the node
is exercisable without a live gateway.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from langchain_core.runnables import RunnableConfig

from shared.config import MAX_OBSERVATION_CHARS
from brain.subagent_contracts import SubagentResultEnvelope, SubagentTask

logger = logging.getLogger("SUBAGENT_WORKER")

# Runtime type predicates for the closed response-field vocabulary. Explicit,
# auditable checks — deliberately not pydantic.create_model metaprogramming.
_TYPE_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "list_str": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
}

# A final-answer synthesiser: given the task and the tool observations, return the
# structured result dict. Injectable via config for hermetic tests.
AnswerFn = Callable[[SubagentTask, List[str]], Awaitable[Dict[str, Any]]]


def _validate_against_schema(result: Any, task: SubagentTask) -> Optional[str]:
    """Return None when ``result`` satisfies the task's response schema, else a reason."""
    if not isinstance(result, dict):
        return "structured result is not an object"
    for field in task.response_schema.fields:
        if field.name not in result:
            return f"missing field '{field.name}'"
        check = _TYPE_CHECKS.get(field.type)
        if check is not None and not check(result[field.name]):
            return f"field '{field.name}' is not of type {field.type}"
    return None


def _resolve_tools(role: str, state: Mapping[str, Any]) -> Dict[str, Any]:
    """Role → executable tool map for the ToolDispatcher.

    Only the READ_ONLY critic role has an executable tool set today (the analyst
    arsenal); the developer roles run tool-less until their arsenals land. Never
    raises — a resolution failure degrades to a tool-less (pure-reasoning) subagent.
    """
    if role == "analyst_readonly":
        try:
            from tools.analyst_tools import build_analyst_tools
            return build_analyst_tools(state)
        except Exception as exc:  # noqa: BLE001 — degrade to tool-less, never crash the node
            logger.warning("analyst tool resolution failed; running tool-less: %s", exc)
    return {}


async def _default_answer(task: SubagentTask, observations: List[str]) -> Dict[str, Any]:
    """Gateway-backed final-answer synthesiser (used when none is injected)."""
    import json

    from tools.llm_gateway import LLMGateway

    field_lines = "\n".join(
        f"- {f.name} ({f.type}): {f.description}" for f in task.response_schema.fields
    )
    context = "\n\n".join(observations) if observations else "(no tool observations)"
    prompt = (
        f"Task: {task.description}\n\n"
        f"Diagnostics gathered:\n{context}\n\n"
        "Return ONLY a JSON object with exactly these fields:\n"
        f"{field_lines}"
    )
    raw = await LLMGateway.acomplete_with_thinking(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        session_id=task.task_id,
    )
    try:
        parsed = json.loads(LLMGateway._sanitize_json_response(raw))
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


async def subagent_worker(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """Run one dispatched subagent and emit its result envelope."""
    configurable = (config or {}).get("configurable", {})
    raw_task = state.get("_dispatch_task")

    # Task admission — a malformed slice is reported, never raised.
    try:
        task = SubagentTask.model_validate(raw_task)
    except Exception as exc:  # noqa: BLE001 — a bad task slice must not crash the graph
        logger.warning("subagent_worker received an invalid _dispatch_task: %s", exc)
        envelope = SubagentResultEnvelope(
            task_id=str((raw_task or {}).get("task_id", "") if isinstance(raw_task, dict) else ""),
            status="error",
            raw_digest="",
            error_message=f"invalid dispatch task: {exc}",
        )
        return {
            "_dispatch_results": [envelope.model_dump()],
            "subagent_dispatch_trace": [{"task_id": envelope.task_id, "status": "error"}],
        }

    observations: List[str] = []
    trace_len = 0
    status = "ok"
    structured_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    # Initialized before the try so the post-try cost estimate is crash-safe on an early
    # failure (a bad reasoner/tool resolution) — the node must never raise. `trace` stays
    # `List[Any]` to keep `ToolCall`'s import deferred inside the try (no module-top cycle).
    loop_messages: List[Dict[str, Any]] = []
    trace: List[Any] = []

    try:
        from core.permissions import session_mode_from_channel
        from core.tool_dispatch import ToolCall, ToolDispatcher, make_gateway_reasoner
        from shared.rbac import PermissionMode

        tools = _resolve_tools(task.subagent_role, state)
        dispatcher = ToolDispatcher(
            tools,
            active_role=task.subagent_role,
            session_mode=session_mode_from_channel(state.get("session_permission_mode")),
            state=state,
            agent_permission=PermissionMode.READ_ONLY,
        )
        seed = (
            f"You are the '{task.subagent_role}' subagent. Task:\n{task.description}\n\n"
            "You MAY call the available READ_ONLY tools to ground your answer; emit {} to skip."
        )
        loop_messages = [{"role": "user", "content": seed}]
        trace = []
        reasoner = configurable.get("dispatch_tool_reasoner") or make_gateway_reasoner(
            tools, session_id=task.task_id
        )
        if tools:
            await dispatcher.run_loop(
                loop_messages, reasoner, max_iters=task.max_iterations, trace=trace
            )
        trace_len = len(trace)
        observations = [
            str(m.get("content", ""))
            for m in loop_messages
            if m.get("role") == "system"
            and str(m.get("content", "")).startswith("[tool observations]")
        ]

        # Final structured answer, constrained to response_schema.
        answer_fn: AnswerFn = configurable.get("dispatch_answer_fn") or _default_answer
        structured_result = await answer_fn(task, observations)
        reason = _validate_against_schema(structured_result, task)
        if reason is not None:
            status = "error"
            error_message = f"response_schema violation: {reason}"
    except Exception as exc:  # noqa: BLE001 — a subagent fault must not crash the host graph
        logger.warning(
            "subagent_worker '%s' failed [%s: %s]", task.task_id, type(exc).__name__, exc,
            exc_info=True,
        )
        status = "error"
        error_message = f"{type(exc).__name__}: {exc}"

    raw_digest = "\n\n".join(observations)
    if len(raw_digest) > MAX_OBSERVATION_CHARS:
        raw_digest = raw_digest[:MAX_OBSERVATION_CHARS] + "\n…[truncated]"

    # Real per-invocation cost of the tool loop (context + tool calls), the "actual" the
    # dispatch ledger reconciles against. The answer_fn synthesis call is not separately
    # metered (DEBT-105). Estimation must not crash the node, so a failure degrades to 0.0.
    from brain.iteration_governor import estimate_iteration_cost

    try:
        cost_usd = estimate_iteration_cost(loop_messages, trace)
    except Exception as exc:  # noqa: BLE001 — cost accounting must never sink the envelope
        logger.warning("subagent_worker cost estimate failed; recording 0.0: %s", exc)
        cost_usd = 0.0

    envelope = SubagentResultEnvelope(
        task_id=task.task_id,
        status=status,  # type: ignore[arg-type]  # narrowed to the Literal by construction
        structured_result=structured_result if status == "ok" else None,
        raw_digest=raw_digest,
        cost_usd=cost_usd,
        iterations_used=trace_len,
        error_message=error_message,
    )
    return {
        "_dispatch_results": [envelope.model_dump()],
        "subagent_dispatch_trace": [{"task_id": task.task_id, "status": status}],
    }
