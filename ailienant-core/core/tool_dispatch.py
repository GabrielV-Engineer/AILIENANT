"""Role-agnostic runtime tool-dispatch loop.

The registered role-gated tools are metadata-only schemas in the ToolRAGStore;
the executable callables are built separately by state-injecting factories.
Nothing connected the two — an LLM could see a tool but never call it. This
module is the missing seam: a generalized version of the agentic-cell pattern
that turns a model-emitted JSON envelope into gated, executed tool calls and
feeds the observations back so the model can reason over real results.

Why prompt-enforced JSON rather than native ``bind_tools``: the project gateway
returns plain text (litellm ``ModelResponse``), so — exactly as the coder parses
SEARCH/REPLACE and the agentic cell parses its envelope — tool intent is carried
in a small JSON object the model emits and we parse here.

Every dispatch is gated through the same pure ``evaluate_action`` matrix the rest
of the system uses, so a READ_ONLY tool runs friction-free while a mutating tier
is denied or escalated identically to every other call site. The loop is
self-correcting: malformed JSON or a call to an unknown tool is turned into a
feedback observation the model can recover from, never an exception that crashes
the host turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    MutableSequence,
    Optional,
    Sequence,
    Tuple,
)

from langchain_core.tools import BaseTool

from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    ToolPrivilegeTier,
    evaluate_action,
)
from shared.rbac import PermissionMode

logger = logging.getLogger("TOOL_DISPATCH")

# Hard ceiling on observation text fed back into the prompt — token hygiene: a
# verbose tool result must never balloon the next reasoning turn unbounded.
_MAX_OBSERVATION_CHARS: int = 4000


@dataclass(frozen=True)
class ToolCall:
    """A single model-proposed tool invocation."""

    name: str
    args: Dict[str, Any]


@dataclass(frozen=True)
class RegisteredTool:
    """An executable tool paired with the privilege metadata the gate needs.

    Co-locating the callable with its tier and ``allowed_roles`` keeps the
    dispatch decision authoritative at the call site instead of re-deriving it
    from the (metadata-only) RAG store.
    """

    tool: BaseTool
    tier: ToolPrivilegeTier
    allowed_roles: frozenset[str]


@dataclass
class DispatchResult:
    """Outcome of one dispatch: the observation text and whether code ran."""

    observation: str
    executed: bool


# A reasoner maps the running message history to the model's raw text reply; the
# loop owns parsing so it can distinguish "no tools" from "malformed output" and
# self-correct. Tests inject a deterministic reasoner to exercise control flow
# without a live model.
Reasoner = Callable[[Sequence[Dict[str, Any]]], Awaitable[str]]

# An approval callback decides whether a tool whose tier resolved to HITL may run.
# It receives the proposed call and its privilege metadata and returns True to
# admit, False to deny. Returning False (or no callback at all) degrades to a
# deny-with-report observation — an admission gate must never hang the turn.
ApprovalFn = Callable[["ToolCall", "RegisteredTool"], Awaitable[bool]]


def parse_tool_call_envelope(text: str) -> Tuple[List[ToolCall], Optional[str]]:
    """Extract tool calls from a model reply.

    Returns ``(calls, None)`` on success and ``([], error_message)`` when the
    text is not a parseable ``{"tool_calls": [...]}`` envelope, so the caller can
    feed the error back as a corrective observation. An envelope that parses but
    carries no calls is a valid "stop" signal — ``([], None)``.
    """
    if not text or not text.strip():
        return [], "empty response — emit a JSON tool-call envelope or {}"

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return [], "no JSON object found — respond with ONLY the tool-call envelope"

    try:
        envelope = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        return [], f"invalid JSON ({exc.msg}) — re-emit ONLY the tool-call envelope"

    if not isinstance(envelope, dict):
        return [], "envelope must be a JSON object with a 'tool_calls' array"

    raw_calls = envelope.get("tool_calls", [])
    if not isinstance(raw_calls, list):
        return [], "'tool_calls' must be an array"

    calls: List[ToolCall] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        args = raw.get("args", {})
        if isinstance(name, str) and isinstance(args, dict):
            calls.append(ToolCall(name=name, args=args))
    return calls, None


def build_schema_hint(tools: Mapping[str, RegisteredTool]) -> str:
    """Build the system instruction listing the callable tools and the envelope.

    Each tool is rendered with its name, description, and argument schema so the
    model can form a valid call; the required envelope shape is stated explicitly.
    """
    lines: List[str] = [
        "You may call tools to gather information before answering. Respond with "
        "ONLY a JSON object of the form "
        '{"tool_calls":[{"name":"<tool>","args":{...}}]}. '
        "Emit {} (no tool_calls) when you have enough information. Available tools:"
    ]
    for name, reg in tools.items():
        desc = (reg.tool.description or "").strip().split("\n", 1)[0]
        schema = reg.tool.args_schema
        arg_names: List[str] = []
        if isinstance(schema, dict):
            arg_names = list(schema.get("properties", {}).keys())
        elif schema is not None:
            try:
                arg_names = list(schema.model_json_schema().get("properties", {}).keys())
            except Exception:  # noqa: BLE001 — a schema introspection miss is non-fatal
                arg_names = []
        sig = ", ".join(arg_names)
        lines.append(f"- {name}({sig}): {desc}")
    return "\n".join(lines)


class ToolDispatcher:
    """Gate-and-execute a model's tool calls; drive a bounded ReAct loop.

    The dispatcher is constructed per turn against the active role's callable map
    and the live session policy. It performs no I/O of its own beyond invoking the
    tool callables, and never raises out of ``dispatch`` / ``run_loop`` — every
    failure mode degrades to a feedback observation so a faulty model output can
    never crash the host turn.
    """

    def __init__(
        self,
        tools: Mapping[str, RegisteredTool],
        *,
        active_role: str,
        session_mode: SessionPermissionMode,
        state: Mapping[str, Any],
        agent_permission: PermissionMode,
        approval_fn: Optional[ApprovalFn] = None,
    ) -> None:
        self._tools = tools
        self._active_role = active_role
        self._session_mode = session_mode
        self._state = state
        self._agent_permission = agent_permission
        # When a tier resolves to HITL the dispatcher consults this callback; with
        # no callback wired, a HITL tier degrades to deny-with-report (the safe
        # default for a non-interactive context). READ_ONLY consumers never reach
        # this branch, so omitting it is the friction-free path.
        self._approval_fn = approval_fn

    async def dispatch(self, call: ToolCall) -> DispatchResult:
        """Resolve, gate, and execute one tool call.

        Lookup miss, role mismatch, permission DENY/HITL, and execution failure
        all return a structured observation with ``executed=False`` so the loop
        can surface it to the model without aborting.
        """
        reg = self._tools.get(call.name)
        if reg is None:
            available = ", ".join(sorted(self._tools)) or "(none)"
            return DispatchResult(
                observation=(
                    f"[dispatch] tool '{call.name}' not found. "
                    f"Available tools: {available}."
                ),
                executed=False,
            )

        if self._active_role not in reg.allowed_roles:
            return DispatchResult(
                observation=(
                    f"[dispatch] DENIED — role '{self._active_role}' may not call "
                    f"'{call.name}'."
                ),
                executed=False,
            )

        decision = evaluate_action(
            self._session_mode, reg.tier, self._agent_permission
        )
        if decision is PermissionDecision.DENY:
            return DispatchResult(
                observation=(
                    f"[dispatch] DENIED — '{call.name}' ({reg.tier.value}) is not "
                    f"permitted under the current session policy."
                ),
                executed=False,
            )
        if decision is PermissionDecision.HITL:
            if self._approval_fn is None:
                # No interactive approval channel wired — degrade to deny-with-report
                # rather than hang. The model sees the denial and moves on.
                return DispatchResult(
                    observation=(
                        f"[dispatch] '{call.name}' ({reg.tier.value}) requires human "
                        f"approval, but no approval channel is available — denied."
                    ),
                    executed=False,
                )
            try:
                approved = await self._approval_fn(call, reg)
            except Exception as exc:  # noqa: BLE001 — an approval-channel fault must not crash the turn
                logger.warning(
                    "Approval channel failed for '%s': %s", call.name, exc, exc_info=True
                )
                return DispatchResult(
                    observation=f"[dispatch] '{call.name}' approval channel failed: {exc}",
                    executed=False,
                )
            if not approved:
                return DispatchResult(
                    observation=f"[dispatch] '{call.name}' was not approved — skipped.",
                    executed=False,
                )
            # Approved — fall through to execute below.

        try:
            result = await reg.tool._arun(**call.args)
            text = str(result)
            if len(text) > _MAX_OBSERVATION_CHARS:
                text = text[:_MAX_OBSERVATION_CHARS] + "\n…[truncated]"
            return DispatchResult(observation=text, executed=True)
        except (TypeError, ValueError) as exc:
            # Bad argument shape — recoverable: tell the model how it failed.
            logger.warning(
                "Tool '%s' rejected args: %s", call.name, exc, exc_info=True
            )
            return DispatchResult(
                observation=f"[dispatch] '{call.name}' argument error: {exc}",
                executed=False,
            )
        except Exception as exc:  # noqa: BLE001 — a tool fault must not crash the turn
            logger.warning(
                "Tool '%s' raised during dispatch: %s", call.name, exc, exc_info=True
            )
            return DispatchResult(
                observation=f"[dispatch] '{call.name}' failed: {exc}",
                executed=False,
            )

    async def run_loop(
        self,
        messages: MutableSequence[Dict[str, Any]],
        reasoner: Reasoner,
        *,
        max_iters: int,
        trace: MutableSequence[ToolCall],
    ) -> MutableSequence[ToolCall]:
        """Drive a bounded reason → call → observe loop.

        Each iteration asks the reasoner for an envelope. A parse error is fed
        back as a corrective observation (self-correction) and the iteration is
        consumed; an empty-but-valid envelope ends the loop; otherwise every call
        is dispatched, the observations are appended for the next turn, and each
        executed call is recorded on ``trace``. ``messages`` is mutated in place.
        """
        for _ in range(max(0, max_iters)):
            try:
                text = await reasoner(messages)
            except Exception as exc:  # noqa: BLE001 — reasoner failure is a soft stop
                logger.warning("Tool-dispatch reasoner failed: %s", exc, exc_info=True)
                break

            calls, error = parse_tool_call_envelope(text)
            if error is not None:
                messages.append(
                    {
                        "role": "system",
                        "content": f"[dispatch] {error}",
                    }
                )
                continue
            if not calls:
                break

            observations: List[str] = []
            for call in calls:
                result = await self.dispatch(call)
                if result.executed:
                    trace.append(call)
                observations.append(f"{call.name} → {result.observation}")

            messages.append(
                {
                    "role": "system",
                    "content": "[tool observations]\n" + "\n".join(observations),
                }
            )
        return trace


def make_gateway_reasoner(
    tools: Mapping[str, RegisteredTool],
    *,
    model: Optional[str] = None,
    session_id: str = "",
) -> Reasoner:
    """Build a gateway-backed reasoner that prepends the schema hint.

    Returns the model's raw text; the dispatcher's loop owns parsing. Best-effort
    — a transport failure surfaces as an empty string, which the loop treats as a
    graceful stop rather than a crash.
    """
    hint = build_schema_hint(tools)

    async def _reason(messages: Sequence[Dict[str, Any]]) -> str:
        from shared.config import MODEL_BIG
        from tools.llm_gateway import LLMGateway

        convo: List[Dict[str, Any]] = [
            {"role": "system", "content": hint},
            *messages,
        ]
        try:
            response = await LLMGateway.ainvoke(
                messages=convo,
                model=model or MODEL_BIG,
                temperature=0.0,
                session_id=session_id,
            )
            return response.choices[0].message.content or ""  # type: ignore[union-attr,index]
        except Exception as exc:  # noqa: BLE001 — a reasoning failure is a soft stop
            logger.warning("Gateway reasoner failed: %s", exc, exc_info=True)
            return ""

    return _reason


def make_websocket_approval_fn(session_id: str) -> ApprovalFn:
    """Build an ApprovalFn that routes a HITL tier through native Suspend & Resume.

    A tool the operator already approved this session is admitted without re-prompting
    (trust-once valve); otherwise the call suspends the graph via ``request_graph_approval``
    (LangGraph ``interrupt()``), freeing the runtime until the operator replies. An empty
    ``session_id`` (no live channel) denies without hanging. Dormant today (no mutating
    ``ToolDispatcher`` consumer — Analyst/Researcher are READ_ONLY); re-pointed so the
    first such consumer inherits the native path. A future consumer that interrupts
    mid-loop must adopt the cell's defer-then-interrupt-first pattern for replay safety.
    """

    async def _approve(call: "ToolCall", reg: "RegisteredTool") -> bool:
        if not session_id:
            return False
        # Lazy import — the api/transport layers import this module, so resolving at
        # call time avoids the construction-time cycle.
        from tools.mcp_adapter import _grant_session_trust, _is_session_trusted

        if _is_session_trusted(session_id, call.name):
            return True
        from core.hitl import request_graph_approval

        resp = request_graph_approval(
            session_id=session_id,
            action_description=f"TOOL_CALL: {call.name} ({reg.tier.value})",
            proposed_content=json.dumps(call.args, default=str)[:2000],
            request_kind="COMMAND_EXEC",
        )
        if resp.get("approved"):
            _grant_session_trust(session_id, call.name)
            return True
        return False

    return _approve
