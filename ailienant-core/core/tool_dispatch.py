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
import time
import uuid
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
    Set,
    Tuple,
)

from langchain_core.tools import BaseTool

from core.activity_context import bind_agent_role, current_activity_sink, reset_agent_role
from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    ToolPrivilegeTier,
    evaluate_action,
)
from core.redaction import mask_secrets, truncate_middle
from core.telemetry import log_tool_invocation
from shared.config import MAX_JSON_PARSE_CHARS, MAX_OBSERVATION_CHARS
from shared.rbac import PermissionMode

logger = logging.getLogger("TOOL_DISPATCH")

# Hard ceiling on observation text fed back into the prompt — token hygiene: a
# verbose tool result must never balloon the next reasoning turn unbounded.
# Single-sourced in shared.config so the dispatch-result envelope schema and this
# loop enforce an identical ceiling that can never drift.
_MAX_OBSERVATION_CHARS: int = MAX_OBSERVATION_CHARS

# Event-loop safety ceiling checked before promote_tool_state ever calls
# json.loads — see shared.config.MAX_JSON_PARSE_CHARS for the full rationale.
_MAX_JSON_PARSE_CHARS: int = MAX_JSON_PARSE_CHARS

# Glass-Box Timeline detail-body budgets — independent of _MAX_OBSERVATION_CHARS
# (which bounds what re-enters the model's own prompt). Mirrors
# core/exec_log.py's _OUTPUT_CAP/_COMMAND_CAP so a tool-call row and a
# command row read with the same generosity on the wire.
_ACTIVITY_ARGS_CAP: int = 500
_ACTIVITY_OBSERVATION_CAP: int = 2_000

# =====================================================================
# State-channel promotion — allowlisted, additive
# =====================================================================
#
# A handful of tools return a JSON envelope whose payload is meant to land on
# a specific AIlienantGraphState channel (e.g. todo_write's "agent_todos"),
# not just ride along as prompt text. This is deliberately an allowlist, not
# a generic "any JSON key becomes a channel write" rule — a tool that isn't
# listed here can never mutate graph state through this path (zero-trust,
# CLAUDE.md §6.2). Only the caller (currently brain/agentic_cell.py) decides
# whether/how to fold the delta into its return value; this module never
# touches state directly.
_STATE_PROMOTERS: Dict[str, str] = {"todo_write": "agent_todos"}


def promote_tool_state(tool_name: str, raw: str) -> Optional[Dict[str, Any]]:
    """Decode an allowlisted tool's observation into a graph-state delta.

    Returns ``None`` for a non-allowlisted tool name, oversized text, or any
    decode/shape failure — this function never raises. The size check runs
    first and on the *untruncated* text, before any ``json.loads`` call, so an
    adversarial or malfunctioning model cannot force a synchronous O(L) parse
    of an unbounded string onto the event loop (see MAX_JSON_PARSE_CHARS).
    Items are re-validated through ``TodoItem`` rather than trusted as raw
    dicts, so the promoter trusts the shape it declares, not the tool's text.
    """
    channel = _STATE_PROMOTERS.get(tool_name)
    if channel is None:
        return None
    if len(raw) > _MAX_JSON_PARSE_CHARS:
        logger.warning(
            "promote_tool_state: '%s' observation (%d chars) exceeds the %d-char "
            "parse ceiling — dropped without parsing.",
            tool_name, len(raw), _MAX_JSON_PARSE_CHARS,
        )
        return None
    try:
        payload = json.loads(raw)
        raw_items = payload[channel]
        if not isinstance(raw_items, list):
            raise TypeError(f"{channel!r} must be a list, got {type(raw_items).__name__}")
        from tools.universal_tools import TodoItem  # deferred — avoids a module-load cycle

        items = [TodoItem.model_validate(item).model_dump() for item in raw_items]
        return {channel: items}
    except Exception as exc:  # noqa: BLE001 — a malformed promotion must not crash the turn
        logger.warning(
            "promote_tool_state: '%s' payload could not be promoted to '%s': %s",
            tool_name, channel, exc, exc_info=True,
        )
        return None


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
    """Outcome of one dispatch: the observation text and whether code ran.

    ``state_delta`` is additive (default ``None``) — populated only when
    ``call.name`` is in the ``_STATE_PROMOTERS`` allowlist and the observation
    decodes cleanly; every existing consumer of this dataclass is unaffected.
    """

    observation: str
    executed: bool
    state_delta: Optional[Dict[str, Any]] = None


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


def _resolve_ref(ref: str, defs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve a JSON-schema ``$ref`` (e.g. ``#/$defs/Foo``) against ``$defs``."""
    def_name = ref.rsplit("/", 1)[-1]
    resolved = defs.get(def_name)
    return resolved if isinstance(resolved, dict) else None


def _find_ref(prop_schema: Dict[str, Any]) -> Optional[str]:
    """Find a nested-model ``$ref`` on a property, whether direct, inside an
    array's ``items``, or inside an ``anyOf`` (Optional[Model] renders this way)."""
    if "$ref" in prop_schema:
        ref = prop_schema["$ref"]
        return ref if isinstance(ref, str) else None
    items = prop_schema.get("items")
    if isinstance(items, dict) and "$ref" in items:
        ref = items["$ref"]
        return ref if isinstance(ref, str) else None
    for option in prop_schema.get("anyOf", []) or []:
        if not isinstance(option, dict):
            continue
        ref = _find_ref(option)
        if ref:
            return ref
    return None


def _describe_schema_properties(
    schema_dict: Dict[str, Any],
    defs: Dict[str, Any],
    seen: Optional[Set[str]] = None,
    indent: str = "  ",
) -> List[str]:
    """Render one line per property with its own ``description``, recursing into
    ``$ref``-linked nested models so a tool's detailed per-field guidance (e.g. an
    LLM-facing instruction on a nested batch item) actually reaches the model —
    ``model_json_schema()`` never inlines nested models, only ``$ref``s them under
    ``$defs``. ``seen`` guards against a pathological self-referential schema."""
    seen = seen if seen is not None else set()
    lines: List[str] = []
    for prop_name, prop_schema in schema_dict.get("properties", {}).items():
        if not isinstance(prop_schema, dict):
            continue
        desc = prop_schema.get("description")
        line = f"{indent}{prop_name}"
        if desc:
            line += f": {desc}"
        lines.append(line)
        ref = _find_ref(prop_schema)
        if not ref or ref in seen:
            continue
        nested = _resolve_ref(ref, defs)
        if nested is None:
            continue
        lines.extend(_describe_schema_properties(nested, defs, seen | {ref}, indent + "  "))
    return lines


def build_schema_hint(tools: Mapping[str, RegisteredTool]) -> str:
    """Build the system instruction listing the callable tools and the envelope.

    Each tool is rendered with its name, description, and argument schema so the
    model can form a valid call; the required envelope shape is stated explicitly.
    Per-field ``description``s (including on nested batch/option models) are
    rendered too — without this, a tool author's field-level guidance never
    reaches the model through this prompt-JSON dispatch path.
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
        detail_lines: List[str] = []
        if isinstance(schema, dict):
            arg_names = list(schema.get("properties", {}).keys())
        elif schema is not None:
            try:
                # langchain-core types args_schema as possibly a pydantic.v1 model
                # for legacy-tool compat; every tool registered here is pydantic v2,
                # and the except below already degrades gracefully if that ever changes.
                full_schema = schema.model_json_schema()  # type: ignore[union-attr]
                arg_names = list(full_schema.get("properties", {}).keys())
                detail_lines = _describe_schema_properties(
                    full_schema, full_schema.get("$defs", {})
                )
            except Exception:  # noqa: BLE001 — a schema introspection miss is non-fatal
                arg_names = []
        sig = ", ".join(arg_names)
        lines.append(f"- {name}({sig}): {desc}")
        lines.extend(detail_lines)
    return "\n".join(lines)


def _format_call_args(args: Dict[str, Any]) -> str:
    """Best-effort one-line rendering of a tool call's arguments for display.

    Never raises — an unserializable value (e.g. a bound method smuggled into
    args by a malformed call) falls back to ``str(args)`` rather than crashing
    the Glass-Box Timeline detail emission.
    """
    try:
        return json.dumps(args, default=str)
    except Exception:  # noqa: BLE001 — display-only rendering must never fault
        return str(args)


def _build_activity_stdout(name: str, args: Dict[str, Any], observation: str) -> Tuple[str, bool]:
    """Mask and cap a tool call's args + observation into one detail-body string.

    Args and observation are capped independently (mirrors
    ``core/exec_log.py::record_exec``'s separate stdout/stderr budgets) so a
    huge observation can never crowd out the args preview. Cap BEFORE mask —
    ``mask_secrets`` truncates head-only past its own internal ceiling, so
    pre-truncating here with ``truncate_middle`` is what preserves the tail.
    """
    raw_args = _format_call_args(args)
    safe_args = mask_secrets(truncate_middle(raw_args, _ACTIVITY_ARGS_CAP)) or ""
    safe_observation = mask_secrets(truncate_middle(observation, _ACTIVITY_OBSERVATION_CAP)) or ""
    truncated = len(raw_args) > _ACTIVITY_ARGS_CAP or len(observation) > _ACTIVITY_OBSERVATION_CAP
    return f"args: {safe_args}\n\n{safe_observation}", truncated


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
        activity_role: Optional[str] = None,
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
        # Glass-Box Timeline lane attribution (13.1.9) — usually the SAME string
        # as `active_role` (researcher/analyst/a dispatched subagent all want
        # their RBAC identity to also be their lane identity). The one place it
        # genuinely diverges is the coder: `active_role` there is the per-WBS-
        # step target_role ("core_dev", "qa_tester", …) for RBAC, which is real
        # and meaningful for permissions but would fragment one coding turn
        # into a new lane every time the step's target_role changes, even
        # though it is still "the coder" acting throughout. `agents/coder.py`
        # passes `activity_role="coder"` to keep that turn as one lane while
        # leaving the RBAC gate untouched.
        self._activity_role = activity_role if activity_role is not None else active_role

    def classify(
        self, call: ToolCall
    ) -> Tuple[Optional[RegisteredTool], PermissionDecision, Optional[str]]:
        """Pure verdict for one call: (registered tool or None, decision, reason).

        No I/O, no execution, no approval-channel consultation — this is the
        single gate implementation (lookup miss → role check → ``evaluate_action``)
        that both ``dispatch()`` and a caller needing to know the verdict *before*
        committing to an approval flow (e.g. the agentic cell's HITL defer) share.
        ``reason`` is only populated for a non-ALLOW outcome and is the exact text
        ``dispatch()`` used to embed inline, kept here so both callers report
        identically.
        """
        reg = self._tools.get(call.name)
        if reg is None:
            available = ", ".join(sorted(self._tools)) or "(none)"
            return None, PermissionDecision.DENY, (
                f"tool '{call.name}' not found. Available tools: {available}."
            )

        if self._active_role not in reg.allowed_roles:
            return reg, PermissionDecision.DENY, (
                f"DENIED — role '{self._active_role}' may not call '{call.name}'."
            )

        decision = evaluate_action(
            self._session_mode, reg.tier, self._agent_permission
        )
        if decision is PermissionDecision.DENY:
            return reg, decision, (
                f"DENIED — '{call.name}' ({reg.tier.value}) is not permitted under "
                f"the current session policy."
            )
        if decision is PermissionDecision.HITL:
            return reg, decision, (
                f"'{call.name}' ({reg.tier.value}) requires human approval."
            )
        return reg, decision, None

    async def dispatch(
        self, call: ToolCall, *, activity_ref: Optional[str] = None,
    ) -> DispatchResult:
        """Bind the dispatcher's own lane attribution for the life of this one
        call, then delegate to ``_dispatch_impl``.

        ``self._activity_role`` is a narrower, more precise attribution than
        whatever the enclosing graph node bound as its own default (e.g. for a
        dispatched subagent, the node is ``subagent_worker`` but the role is
        the subagent's own, like ``core_dev``) — see
        ``core/activity_context.py``'s module docstring for the two-precedence
        contract, and this class's ``__init__`` for why it can differ from
        ``self._active_role`` (the RBAC identity `classify`/`_dispatch_impl`
        still gate against, untouched here). Reset in ``finally`` (charter
        §5.1) so attribution reverts to the node's own role once this call
        completes, rather than leaking into whatever the node narrates
        afterward.
        """
        role_token = bind_agent_role(self._activity_role)
        try:
            return await self._dispatch_impl(call, activity_ref=activity_ref)
        finally:
            reset_agent_role(role_token)

    async def _dispatch_impl(
        self, call: ToolCall, *, activity_ref: Optional[str] = None,
    ) -> DispatchResult:
        """Resolve, gate, and execute one tool call.

        Lookup miss, role mismatch, permission DENY/HITL, and execution failure
        all return a structured observation with ``executed=False`` so the loop
        can surface it to the model without aborting.

        Glass-Box Timeline instrumentation (DEBT-133), mirroring the discipline
        ``core/exec_log.py::record_execution`` already applies to commands:
        best-effort on every branch, never affects the returned
        ``DispatchResult``. Two modes, selected by ``activity_ref``:

        - ``None`` (the default — every call site except the one below): this
          method owns the row's full lifecycle. A call that never reaches
          ``tool._arun`` (denied, unresolved, HITL with no/failed/declined
          approval) emits a single ref-less ``emit_blocked`` marker — mirroring
          ``record_execution``'s own "attempted but never reached an adapter"
          case — and no detail follows. A call that does execute mints its own
          ref, emits an opening marker, then a closing detail.
        - a caller-supplied ref: the caller (``brain/agentic_cell.py``'s
          ``pending_tool_call`` HITL-defer/resume path) already emitted the
          OPENING marker under this exact ref before an ``interrupt()`` —
          replayed on every resume attempt, so the ref must stay stable rather
          than being re-minted here. Every branch below then resolves that
          same ref with a terminal ``emit_detail`` (never `emit_blocked`,
          which would open an orphan second row) — including a denial, so the
          row can never hang on "running…" through an approval round-trip.
        """
        sink = current_activity_sink()
        task_id = self._state.get("task_id")

        def _record_invocation(
            *, executed: bool, error: Optional[str], duration_ms: Optional[float],
        ) -> None:
            # DEBT-176 emit-only ledger — best-effort, mirrors log_routing_decision's
            # own never-raise contract, so a telemetry write can never break dispatch.
            try:
                log_tool_invocation(
                    task_id=task_id,
                    role=self._active_role,
                    tool_name=call.name,
                    decision=str(decision),
                    executed=executed,
                    duration_ms=duration_ms,
                    error=error,
                )
            except Exception:  # noqa: BLE001 — observability must never break dispatch
                logger.debug("tool-invocation telemetry skipped (%s)", call.name, exc_info=True)

        async def _emit_blocked() -> None:
            if sink is None:
                return
            try:
                await sink.emit_blocked(target=call.name, kind="tool")
            except Exception:  # noqa: BLE001 — observability must never break dispatch
                logger.debug("tool-dispatch blocked marker skipped (%s)", call.name, exc_info=True)

        async def _emit_detail(
            ref: str, *, executed: bool, observation: Optional[str], error: Optional[str],
            duration_ms: Optional[float],
        ) -> None:
            if sink is None:
                return
            try:
                stdout: Optional[str] = None
                truncated = False
                if observation is not None:
                    stdout, truncated = _build_activity_stdout(call.name, call.args, observation)
                await sink.emit_detail(
                    ref=ref,
                    source="unknown",
                    cwd=None,
                    initiator=self._active_role,
                    stdout=stdout,
                    stderr=None,
                    exit_code=(None if error is not None else (0 if executed else 1)),
                    duration_ms=duration_ms,
                    truncated=truncated,
                    error=error,
                )
            except Exception:  # noqa: BLE001 — observability must never break dispatch
                logger.debug("tool-dispatch detail emit skipped (%s)", call.name, exc_info=True)

        async def _not_executed(observation: Optional[str], error: Optional[str] = None) -> None:
            _record_invocation(executed=False, error=error, duration_ms=None)
            # A caller-supplied ref already has an open span (emitted before an
            # interrupt()) that MUST be resolved — a fresh emit_blocked would
            # orphan it as a second, never-resolving row. No ref means nothing
            # ever opened a span for this attempt, so emit_blocked is correct.
            if activity_ref is not None:
                await _emit_detail(
                    activity_ref, executed=False, observation=observation,
                    error=error, duration_ms=None,
                )
            else:
                await _emit_blocked()

        reg, decision, reason = self.classify(call)
        if reg is None:
            await _not_executed(reason)
            return DispatchResult(observation=f"[dispatch] {reason}", executed=False)

        if decision is PermissionDecision.DENY:
            await _not_executed(reason)
            return DispatchResult(observation=f"[dispatch] {reason}", executed=False)

        if decision is PermissionDecision.HITL:
            if self._approval_fn is None:
                # No interactive approval channel wired — degrade to deny-with-report
                # rather than hang. The model sees the denial and moves on.
                observation = f"[dispatch] {reason}, but no approval channel is available — denied."
                await _not_executed(observation)
                return DispatchResult(observation=observation, executed=False)
            try:
                approved = await self._approval_fn(call, reg)
            except Exception as exc:  # noqa: BLE001 — an approval-channel fault must not crash the turn
                logger.warning(
                    "Approval channel failed for '%s': %s", call.name, exc, exc_info=True
                )
                await _not_executed(None, error=str(exc))
                return DispatchResult(
                    observation=f"[dispatch] '{call.name}' approval channel failed: {exc}",
                    executed=False,
                )
            if not approved:
                observation = f"[dispatch] '{call.name}' was not approved — skipped."
                await _not_executed(observation)
                return DispatchResult(observation=observation, executed=False)
            # Approved — fall through to execute below.

        # About to actually invoke the tool. Only the ref=None path mints a
        # fresh id and opens the span here — the ref-supplied path's span was
        # already opened by the caller before its interrupt().
        exec_ref = activity_ref
        if exec_ref is None and sink is not None:
            exec_ref = uuid.uuid4().hex
            try:
                await sink.emit_marker(ref=exec_ref, target=call.name, kind="tool")
            except Exception:  # noqa: BLE001 — observability must never break dispatch
                logger.debug("tool-dispatch marker emit skipped (%s)", call.name, exc_info=True)

        t0 = time.perf_counter()
        try:
            result = await reg.tool._arun(**call.args)
            text = str(result)
            # Promote from the untruncated text — the observation clamp below can
            # split a JSON payload mid-object, which would corrupt (not just crop)
            # a state-channel write. promote_tool_state applies its own, larger
            # size ceiling first, so this ordering never feeds an unbounded parse.
            state_delta = promote_tool_state(call.name, text)
            duration_ms = (time.perf_counter() - t0) * 1000.0
            _record_invocation(executed=True, error=None, duration_ms=duration_ms)
            if exec_ref is not None:
                await _emit_detail(
                    exec_ref, executed=True, observation=text, error=None,
                    duration_ms=duration_ms,
                )
            if len(text) > _MAX_OBSERVATION_CHARS:
                text = text[:_MAX_OBSERVATION_CHARS] + "\n…[truncated]"
            return DispatchResult(observation=text, executed=True, state_delta=state_delta)
        except (TypeError, ValueError) as exc:
            # Bad argument shape — recoverable: tell the model how it failed.
            logger.warning(
                "Tool '%s' rejected args: %s", call.name, exc, exc_info=True
            )
            duration_ms = (time.perf_counter() - t0) * 1000.0
            _record_invocation(executed=False, error=str(exc), duration_ms=duration_ms)
            if exec_ref is not None:
                await _emit_detail(
                    exec_ref, executed=False, observation=None, error=str(exc),
                    duration_ms=duration_ms,
                )
            return DispatchResult(
                observation=f"[dispatch] '{call.name}' argument error: {exc}",
                executed=False,
            )
        except Exception as exc:  # noqa: BLE001 — a tool fault must not crash the turn
            logger.warning(
                "Tool '%s' raised during dispatch: %s", call.name, exc, exc_info=True
            )
            duration_ms = (time.perf_counter() - t0) * 1000.0
            _record_invocation(executed=False, error=str(exc), duration_ms=duration_ms)
            if exec_ref is not None:
                await _emit_detail(
                    exec_ref, executed=False, observation=None, error=str(exc),
                    duration_ms=duration_ms,
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
            return response.choices[0].message.content or ""
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
