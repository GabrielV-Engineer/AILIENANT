# ailienant-core/brain/nodes/subagent_worker_node.py
"""The subagent_worker graph node — one dispatched subagent invocation.

A thin, narrow-contract node (deliberately NOT a reuse of the coder node, which
returns a VFS patch): it reads its own ``_dispatch_task`` slice, runs a bounded,
role-gated tool loop via the existing ``ToolDispatcher``, synthesises a final answer
constrained to the task's ``response_schema``, and returns exactly one
``SubagentResultEnvelope`` on the reducer-guarded ``_dispatch_results`` fan-in
channel. It never raises — a fault becomes a ``status="error"`` envelope so a single
faulty subagent can never crash the host graph.

Tool arsenals: the READ_ONLY critic role maps to the analyst tool set (analyst
tools register under ``allowed_roles={"analyst"}`` in the Tool RAG catalog — a
name disjoint from the ``analyst_readonly`` dispatch-role identity, so it stays on
its own dedicated builder rather than the shared catalog path below). The 8
developer roles resolve through the same ``select_tools -> resolve_tools ->
ToolDispatcher`` substrate the agentic cell and the one-shot coder's grounding
pre-pass use, filtered to schemas whose ``allowed_roles`` include the dispatched
role — no separate, hand-maintained dev-role arsenal. An unknown role stays
tool-less (fail-safe). Both the tool reasoner and the final-answer synthesiser are
injectable through ``config.configurable`` so the node is exercisable without a
live gateway.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, MutableMapping, Optional

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


async def _resolve_tools(
    role: str,
    state: MutableMapping[str, Any],
    *,
    intent: str,
    session_mode: Any,
) -> Dict[str, Any]:
    """Role → executable ``RegisteredTool`` map for the ToolDispatcher.

    The READ_ONLY critic role maps to the analyst arsenal directly (see the
    module docstring for why it cannot share the catalog path below). Every
    other known dispatch role (the 8 developer roles) resolves through
    ``core.tool_rag.tool_rag_store.select_tools`` + ``core.tool_registry.
    resolve_tools`` — the identical substrate the agentic cell's registry
    fallback and the coder's grounding pre-pass already use, so this closes
    DEBT-106 with no new arsenal builder. An unrecognized role (not in
    ``DISPATCH_ROLE_PERMISSIONS``) stays tool-less — fail-safe, unchanged.
    Never raises — a resolution failure of any kind degrades to a tool-less
    subagent.
    """
    if role == "analyst_readonly":
        try:
            from tools.analyst_tools import build_analyst_tools
            return build_analyst_tools(state)
        except Exception as exc:  # noqa: BLE001 — degrade to tool-less, never crash the node
            logger.warning("analyst tool resolution failed; running tool-less: %s", exc)
            return {}

    from shared.rbac import DISPATCH_ROLE_PERMISSIONS

    if role not in DISPATCH_ROLE_PERMISSIONS:
        return {}

    try:
        from brain.agent_context import resolve_context_budget
        from core.deferred_tool_loader import DeferredToolLoader
        from core.tool_rag import TOOL_RAG_TOP_K, tool_rag_store
        from core.tool_registry import resolve_tools

        # Eager-vs-deferred rather than an unconditional top-k: a subagent whose
        # role slice fits the context budget gets its whole arsenal with no
        # embedding round-trip, and otherwise keeps tool_search as its way out of
        # a bad ranking. Constructed locally, never the module singleton, whose
        # store is bound at class-definition time and would bypass the
        # tool_rag_store monkeypatch seam the tests rely on.
        decision = await DeferredToolLoader(tool_rag_store).resolve(
            intent,
            active_role=role,
            session_mode=session_mode,
            context_window=resolve_context_budget(state),
            # +1 so the tool_search slot the deferred branch reserves does not
            # cost this subagent one of its usable tools (see agentic_cell.py).
            k=TOOL_RAG_TOP_K + 1,
        )
        return resolve_tools(decision.schemas, state)
    except Exception as exc:  # noqa: BLE001 — degrade to tool-less, never crash the node
        logger.warning(
            "dev-role tool resolution failed for '%s'; running tool-less: %s", role, exc,
            exc_info=True,
        )
        return {}


def _make_default_answer(system_prompt: str) -> AnswerFn:
    """Build the gateway-backed final-answer synthesiser (used when none is injected).

    A closure over ``system_prompt`` — the resolved role directive (DEBT-127) —
    rather than a signature change to ``AnswerFn``: an injected
    ``dispatch_answer_fn`` (the hermetic-test seam) must keep working
    unmodified. The structured-output instruction stays in the user message,
    untouched, so the response-schema contract is not put at risk by the
    addition.
    """

    async def _answer(task: SubagentTask, observations: List[str]) -> Dict[str, Any]:
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
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        raw = await LLMGateway.acomplete_with_thinking(
            messages=messages,
            response_format={"type": "json_object"},
            session_id=task.task_id,
        )
        try:
            parsed = json.loads(LLMGateway._sanitize_json_response(raw))
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}

    return _answer


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
        from agents.roles import build_subagent_system_prompt
        from core.permissions import session_mode_from_channel
        from core.tool_dispatch import ToolDispatcher, make_gateway_reasoner
        from shared.rbac import resolve_dispatch_permission

        session_mode = session_mode_from_channel(state.get("session_permission_mode"))
        tools = await _resolve_tools(
            task.subagent_role, state, intent=task.description, session_mode=session_mode,
        )
        # Per-role RBAC identity: dev roles resolve to the write/execute-capable floor,
        # the analyst_readonly critic stays READ_ONLY, an unknown role gets the READ_ONLY
        # floor. The (mode, tier, identity) matrix in evaluate_action() then denies any
        # tool the identity is not entitled to — so analyst_readonly can never reach a
        # WRITE/EXECUTE tool under any session mode, and a dev role's WRITE/EXECUTE tool
        # is still gated per the current session policy like any other dispatch path.
        dispatcher = ToolDispatcher(
            tools,
            active_role=task.subagent_role,
            session_mode=session_mode,
            state=state,
            agent_permission=resolve_dispatch_permission(task.subagent_role),
        )
        # Per-role prompt override, same channel agents/coder.py reads — resolved
        # once here and threaded to both the tool-loop reasoning and the final
        # answer synthesis below, so a saved directive actually reaches a
        # dispatched subagent's own role.
        _role_overrides = state.get("agent_role_overrides") or {}
        system_prompt = build_subagent_system_prompt(
            task.subagent_role, override=_role_overrides.get(task.subagent_role or "")
        )
        seed = (
            f"You are the '{task.subagent_role}' subagent. Task:\n{task.description}\n\n"
            "You MAY call the available tools to ground your answer; emit {} to skip."
        )
        loop_messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
        loop_messages.append({"role": "user", "content": seed})
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
        answer_fn: AnswerFn = configurable.get("dispatch_answer_fn") or _make_default_answer(
            system_prompt
        )
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
