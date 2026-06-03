# ailienant-core/agents/analyst.py
#
# — Socratic "Grill Me" AnalystAgent.
#
# Implements the "Grill Me" pattern:
#   - ONE question per turn, always with a recommended answer
#   - Reads codebase via read_file tool before asking (avoid asking what can be known)
#   - Sets hitl_pending=True to suspend the graph (non-blocking — no asyncio.wait)
#   - Detects agreement signals in user_input to trigger shared_understanding_reached=True
#
# Phase 4 upgrade: replace DEBUG stub with real LLM call +
#   tool_registry.bind_tools(llm, [make_read_file_tool(vfs.read)]).

import asyncio
import json
import logging
import os as _os
from typing import AsyncIterator, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

# SOUL.md persona reader. Analyst is the EXCLUSIVE consumer of
# brain.personality cognitive-isolation fence. Other agents
# (planner, coder, orchestrator, researcher) MUST NEVER import this module —
# Test D audits the four logic-agent files for foreign imports on every CI run.
from brain.personality import soul_manager

logger = logging.getLogger("ANALYST_AGENT")

# The live LLM path is the default; the synthetic [DEBUG Q1/Q2] script is the
# escape hatch retained for deterministic CI/UI smoke tests. Mirrors planner.py:
# set AILIENANT_ANALYST_DEBUG=1 to force the stub.
DEBUG_MODE: bool = _os.getenv("AILIENANT_ANALYST_DEBUG", "0") != "0"

_AGREEMENT_SIGNALS = frozenset([
    # English
    "looks good", "sounds good", "yes", "approved", "agreed",
    "let's go", "proceed", "i'm happy", "perfect", "solid",
    "ship it", "lgtm", "ok", "okay",
    # Spanish (user may respond in Spanish)
    "dale", "fuego", "proceder", "adelante", "de acuerdo",
    "perfecto", "bien", "listo", "lo apruebo", "seguimos",
])

# Strong reference set: prevents GC from destroying broadcast tasks mid-flight.
_background_tasks: set = set()

_CLOSE_HINT = (
    "\n> To summarize the plan, respond with 'OK', 'Proceed', or 'Go'."
)

# The "Grill Me" contract handed to the model on top of the SOUL persona. It
# enforces the one-question-per-turn Socratic rhythm and forbids the robotic
# failure modes the DEBUG stub exhibited (ignoring the user, repeating itself).
_GRILL_DIRECTIVE: str = (
    "You are running a Socratic 'Grill Me' planning session. Your job is to "
    "extract a precise, buildable plan from the user one question at a time.\n"
    "RULES:\n"
    "- Ask EXACTLY ONE focused question this turn. Never bundle multiple "
    "questions.\n"
    "- Build directly on what the user just said and on the workspace context "
    "below — reference their actual words, files, and code. Never ask something "
    "already answered, and never repeat a previous question.\n"
    "- Always end with a concrete recommended default answer so the user can "
    "agree in one word (format: 'Recommended: <your best guess>').\n"
    "- If the user defers ('you choose', 'give me your ideas'), MAKE the "
    "decision yourself, state it briefly, and ask the next question that moves "
    "the plan forward.\n"
    "- Be concise. No preamble, no restating the rules. Output only the question "
    "and its recommended answer."
)


def _has_prior_socratic_exchange(messages: List[Dict]) -> bool:
    """Return True if the analyst has already asked at least one question."""
    return any(m.get("role") == "assistant" for m in messages)


def _is_agreement(user_input: str) -> bool:
    """Detect if the user's latest message signals shared understanding."""
    text = user_input.strip().lower()
    return any(signal in text for signal in _AGREEMENT_SIGNALS)


_INTENT_SYSTEM_PROMPT: str = (
    "You are an AnalystAgent performing Pre-Dream Reflection. "
    "Given the last 3–5 user messages, produce ONE sentence (≤30 words) "
    "summarising the user's primary coding intent. "
    "Respond with only that sentence — no preamble, no punctuation beyond the sentence."
)


async def generate_intent_summary_llm(user_messages: List[str], task_id: str = "") -> str:
    """ One-shot LLM call to summarise last N user intents (Pre-Dream Reflection)."""
    from tools.llm_gateway import LLMGateway   # deferred — avoids circular import
    from shared.config import MINI_JUDGE_MODEL  # reuse the fast mini-judge model
    combined = "\n".join(f"- {m}" for m in user_messages)
    result = await LLMGateway.ainvoke(
        messages=[
            {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
            {"role": "user",   "content": combined},
        ],
        model=MINI_JUDGE_MODEL,
        temperature=0.0,
        max_tokens=60,
        session_id=task_id,
    )
    return str(result).strip()


async def run_analyst_node(
    state: dict, config: Optional[RunnableConfig] = None
) -> dict:
    """LangGraph node: Socratic Grill Me AnalystAgent.

    Each invocation asks ONE context-aware question, streams it to the chat, and
    sets hitl_pending=True so the graph suspends. The next task_service.py
    invocation carries the user's answer as user_input; the _merge_messages
    reducer accumulates Q&A history across invocations.

    The live path grounds each question in the workspace (active file + GraphRAG,
    via the same assembler the Natt pane uses) and mirrors the user's language, so
    questions adapt to what the user actually said and to their real code.
    """
    task_id: str = state.get("task_id", "")
    user_input: str = state.get("user_input", "")
    messages: List[Dict] = list(state.get("messages", []))

    has_prior = _has_prior_socratic_exchange(messages)

    # If this is a response to a prior Socratic question, check for agreement.
    if has_prior and _is_agreement(user_input):
        logger.info("AnalystAgent: agreement detected — shared understanding reached.")
        new_messages: List[Dict] = (
            [{"role": "user", "content": user_input}] if user_input else []
        )
        return {
            "shared_understanding_reached": True,
            "hitl_pending": False,
            "messages": new_messages,
        }

    # Accumulate the human's answer from the previous turn (if any).
    # Guard: only add user_input when has_prior=True (it's a Socratic response).
    # On the first turn, user_input is the original task brief — don't pollute history.
    new_messages = (
        [{"role": "user", "content": user_input}]
        if has_prior and user_input
        else []
    )

    # fetch the persona prompt as an EPHEMERAL local variable.
    # CRITICAL: soul_prompt is NEVER written to state.messages or returned in the
    # result dict (R1 — state-key contract). LLM call will receive
    # it as the system message body; for now it is held locally and only its
    # length + emoji flag are logged, so tests can audit integration without
    # leaking prompt content.
    soul_prompt: str = soul_manager.get_prompt()
    logger.info(
        "AnalystAgent: SOUL prompt loaded (%d chars, contains_emoji=%s).",
        len(soul_prompt),
        "🐜" in soul_prompt,
    )

    from api.websocket_manager import vfs_manager  # deferred: avoids circular import

    if DEBUG_MODE:
        if not has_prior:
            question = (
                f"[DEBUG Q1] Before writing any code, I need to understand the goal. "
                f"Task: '{user_input[:80]}'. "
                f"What is the primary deliverable, and what does 'done' look like? "
                f"Recommended: A working feature with all existing tests green + "
                f"new unit tests covering the changed behaviour."
                + _CLOSE_HINT
            )
        else:
            question = (
                "[DEBUG Q2] What are the non-functional constraints "
                "(performance budget, security surface, dependency restrictions)? "
                "Recommended: O(n) complexity max, no new external deps, "
                "all inputs sanitised at the boundary."
                + _CLOSE_HINT
            )
        logger.info("AnalystAgent (DEBUG): synthetic question generated.")
        # Non-blocking broadcast — graph must not stall on WS I/O.
        _t = asyncio.create_task(vfs_manager.broadcast_token(task_id, question))
        _background_tasks.add(_t)
        _t.add_done_callback(_background_tasks.discard)
    else:
        # Live path: stream a context-aware question token-by-token so it reads
        # like a real reply, then close the stream. The node MUST fully drain the
        # stream here (await) before returning hitl_pending=True — the graph
        # suspends on return, so a backgrounded stream would be truncated.
        context_block = await _assemble_socratic_context(state)
        parts: List[str] = []
        try:
            async for chunk in _stream_question_llm(
                messages + new_messages, soul_prompt, context_block, task_id
            ):
                parts.append(chunk)
                await vfs_manager.broadcast_token(task_id, chunk)
        except Exception as exc:  # noqa: BLE001 — analyst must never crash the graph
            logger.warning("AnalystAgent live question failed [%s: %s]",
                           type(exc).__name__, exc)
            if not parts:
                await vfs_manager.broadcast_token(task_id, _ANALYST_BYOM_DOWN)
                parts.append(_ANALYST_BYOM_DOWN)
        question = "".join(parts).strip() or _ANALYST_BYOM_DOWN
        try:
            await vfs_manager.broadcast_stream_end(task_id)
        except Exception:  # noqa: BLE001 — stream_end is best-effort on a dead socket
            pass

    new_messages.append({"role": "assistant", "content": question})

    return {
        "hitl_pending": True,
        "shared_understanding_reached": False,
        "messages": new_messages,
    }


async def _assemble_socratic_context(state: dict) -> str:
    """Build the read-only workspace context block for a Socratic question.

    Reuses the analyst context assembler (active file + workspace tree + GraphRAG)
    so the grilling references the user's real code. Never raises — a context
    failure degrades to an empty block, never crashes the graph node.
    """
    active_path: str = state.get("active_file_path") or ""
    paths: List[str] = [active_path] if active_path else []
    project_root: str = state.get("workspace_root") or ""
    project_id: Optional[str] = state.get("project_id") or None
    session_id: str = state.get("task_id", "")
    if not paths and not project_root:
        return ""
    try:
        from agents.analyst_context import assemble_analyst_context
        return await assemble_analyst_context(
            paths, project_id, session_id, project_root=project_root,
        )
    except Exception as exc:  # noqa: BLE001 — context assembly is best-effort
        logger.debug("Socratic context assembly failed (degrading): %s", exc)
        return ""


async def _stream_question_llm(
    messages: List[Dict],
    soul_prompt: str,
    context_block: str,
    session_id: str,
) -> AsyncIterator[str]:
    """Stream ONE Socratic question from the active BYOM model.

    System prompt = SOUL persona + the Grill-Me directive + language mirror +
    the sandboxed workspace context. Conversation history is replayed so the
    analyst builds on prior turns and never repeats itself. Tokens are coalesced
    into 40ms frames via the shared batcher (DOM-thrash neutralization).
    """
    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import
    from transport.token_batcher import batch_tokens
    from agents.roles import LANGUAGE_MIRROR_DIRECTIVE

    system_prompt = f"{soul_prompt}\n\n{_GRILL_DIRECTIVE}\n\n{LANGUAGE_MIRROR_DIRECTIVE}"
    if context_block:
        system_prompt = f"{system_prompt}\n\n{context_block}"

    llm_messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            llm_messages.append({"role": role, "content": str(content)})

    raw = LLMGateway.astream_byom(llm_messages, tier="medium", session_id=session_id)
    async for chunk in batch_tokens(raw, chunk_ms=40):
        yield chunk


_ANALYST_BYOM_DOWN: str = (
    "I can't reach the configured model right now. Activate a BYOM preset "
    "(Dashboard → BYOM) and make sure its engine is running, then ask me again."
)


async def generate_analyst_reply_stream(
    text: str,
    context_block: str = "",
    history: Optional[List[Dict[str, str]]] = None,
    session_id: str = "",
) -> AsyncIterator[str]:
    """ streaming analyst reply for the Natt pane.

    System prompt = SOUL persona (already identity) + the assembled,
    budgeted, sandboxed analyst context block. Conversation memory (history) is replayed
    so the analyst keeps continuity. Outbound tokens are coalesced into chunk_ms=40 frames
    via the shared batcher. Degrades to one actionable message if the BYOM engine is down —
    the analyst must never crash the WS loop. Read-only: nothing here mutates files.
    """
    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import
    from transport.token_batcher import batch_tokens

    system_prompt = soul_manager.get_prompt()
    if context_block:
        system_prompt = f"{system_prompt}\n\n{context_block}"

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})

    try:
        raw = LLMGateway.astream_byom(messages, tier="medium", session_id=session_id)
        produced = False
        async for chunk in batch_tokens(raw, chunk_ms=40):
            produced = True
            yield chunk
        if not produced:
            yield "(no response)"
    except Exception as exc:  # noqa: BLE001 — analyst must never crash the WS loop
        logger.warning("Analyst live reply failed [%s: %s]", type(exc).__name__, exc)
        yield _ANALYST_BYOM_DOWN


async def generate_analyst_reply(text: str, session_id: str = "") -> str:
    """full (non-streaming) analyst reply for the Natt pane.

    Backward-compatible single-string entry point, now backed by the streaming
    generator (no context wiring; callers wanting context use stream_analyst_reply).
    """
    parts: List[str] = []
    async for chunk in generate_analyst_reply_stream(text, session_id=session_id):
        parts.append(chunk)
    return "".join(parts).strip() or "(no response)"


# ---------------------------------------------------------------------------
# Nightmare Protocol
# ---------------------------------------------------------------------------

_NIGHTMARE_SYSTEM_PROMPT: str = (
    "You are the Nightmare Judge for AILIENANT. Given a code delta and a "
    "list of project rules, score the delta on a 0.0-1.0 scale where:\n"
    "  1.0 = no rules violated, clean diff\n"
    "  0.5 = stylistic concerns or weak adherence\n"
    "  0.0 = at least one hard rule is violated\n"
    "Respond with ONLY a JSON object of the form: "
    '{"reward": <float in [0.0, 1.0]>, "violated_rules": [<rule strings>]}'
    "\nNo prose, no markdown fences. If unsure, default to reward 0.0."
)


class NightmareEvaluation(BaseModel):
    """Pydantic result of one Nightmare Protocol evaluation."""

    reward: float = Field(ge=0.0, le=1.0)
    violated_rules: List[str] = Field(default_factory=list)


_NIGHTMARE_FAILSAFE: NightmareEvaluation = NightmareEvaluation(
    reward=0.0, violated_rules=["LLM_EVAL_FAILED"],
)


def _parse_nightmare_response(raw_content: Optional[str]) -> NightmareEvaluation:
    """Parse the JSON body of a Nightmare/SupremeJudge response. Failsafe on bad input.

    routes through the gateway's envelope unwrapper so a wrapped
    verdict ({"result": {…}}, fenced, or prose-prefixed) is still scored instead of
    failsafing to 0.0. Returns the failsafe only when the text is genuinely unparseable.
    """
    if raw_content is None:
        return _NIGHTMARE_FAILSAFE
    from tools.llm_gateway import LLMGateway  # deferred — avoids circular import
    parsed = LLMGateway._extract_nested_schema_target(raw_content, NightmareEvaluation)
    if not parsed:
        return _NIGHTMARE_FAILSAFE
    try:
        clamped_reward: float = max(0.0, min(1.0, float(parsed.get("reward", 0.0))))
        violated = parsed.get("violated_rules", [])
        if not isinstance(violated, list):
            violated = []
        return NightmareEvaluation(
            reward=clamped_reward,
            violated_rules=[str(v) for v in violated],
        )
    except (TypeError, ValueError):
        return _NIGHTMARE_FAILSAFE


async def evaluate_nightmare(
    code_delta: str,
    rules_json_path: str,
    session_id: str = "",
) -> NightmareEvaluation:
    """Score a code delta against the project's .ailienant.json rules.

    rules_json_path is the workspace directory containing .ailienant.json
    (matches RuleManager.get_combined_rules() contract). Failsafe on any
    error: returns reward=0.0 + violated_rules=["LLM_EVAL_FAILED"] so a
    broken judge never green-lights rule-violating code.
    """
    from tools.llm_gateway import LLMGateway   # deferred — avoids circular import
    from shared.config import MINI_JUDGE_MODEL  # reuse the fast mini-judge model
    from core.rules import RuleManager

    rules_text: str = RuleManager().get_combined_rules(rules_json_path)
    user_payload: str = (
        f"### Project Rules:\n{rules_text}\n\n"
        f"### Code Delta:\n{code_delta}"
    )
    try:
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _NIGHTMARE_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            model=MINI_JUDGE_MODEL,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=120,
            session_id=session_id,
        )
        raw_content = response.choices[0].message.content
        result = _parse_nightmare_response(raw_content)
        logger.info(
            "Nightmare: delta_len=%d reward=%.3f n_violations=%d",
            len(code_delta), result.reward, len(result.violated_rules),
        )
        return result
    except Exception as exc:
        logger.warning("Nightmare: LLM eval failed (failsafe reward=0.0): %s", exc)
        return _NIGHTMARE_FAILSAFE


# ---------------------------------------------------------------------------
# Supreme Judge (Tier.CLOUD reward evaluation)
# ---------------------------------------------------------------------------

async def supreme_judge_evaluate(
    code_delta: str,
    rules_json_path: str,
    session_id: str = "",
) -> NightmareEvaluation:
    """Tier.CLOUD reward evaluation for MCTS rollouts.

    Identical contract to evaluate_nightmare() but routes via Tier.CLOUD
    (MODEL_BIG) for higher-quality reasoning. Called only after the local
    Micro-Isolate pipeline passes (see agents/mcts_coder.py).
    """
    from tools.llm_gateway import LLMGateway, Tier
    from core.rules import RuleManager

    rules_text: str = RuleManager().get_combined_rules(rules_json_path)
    user_payload: str = (
        f"### Project Rules:\n{rules_text}\n\n"
        f"### Code Delta:\n{code_delta}"
    )
    try:
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _NIGHTMARE_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            tier=Tier.CLOUD,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=120,
            session_id=session_id,
        )
        raw_content = response.choices[0].message.content
        result = _parse_nightmare_response(raw_content)
        logger.info(
            "SupremeJudge: delta_len=%d reward=%.3f n_violations=%d",
            len(code_delta), result.reward, len(result.violated_rules),
        )
        return result
    except Exception as exc:
        logger.warning("SupremeJudge: LLM eval failed (failsafe reward=0.0): %s", exc)
        return _NIGHTMARE_FAILSAFE


# ---------------------------------------------------------------------------
# Rule Distillation
# ---------------------------------------------------------------------------

_RULE_DISTILLER_SYSTEM_PROMPT: str = (
    "You are the AnalystAgent performing Rule Distillation. "
    "You will receive CODE_A (what the AI wrote) and CODE_B (what the human "
    "corrected it to). Deduce ONE concise project rule (<=20 words) that, if "
    "the AI had followed it, would have made it write CODE_B in the first place. "
    "Focus on the underlying coding preference, not the literal edit.\n"
    "Examples: 'Prefer list comprehensions over for-loop accumulation'; "
    "'Use single quotes for string literals'; 'Type-annotate all public functions'.\n"
    "If the diff is purely cosmetic (whitespace, trivial naming) or no clear "
    "rule emerges, respond {\"rule\": null}. "
    'Respond ONLY with a JSON object: {"rule": "<rule>"} or {"rule": null}.'
)


async def distill_rejection_to_rule(
    original_code: str,
    user_code: str,
    session_id: str = "",
) -> Optional[str]:
    """Diff AI vs human; ask the mini-judge to extract one coding rule.

    Returns the rule string or None (LLM declined / trivial diff / failure).
    Never raises — telemetry must not block the user.
    """
    if not original_code.strip() or not user_code.strip():
        return None
    if original_code == user_code:
        return None
    from tools.llm_gateway import LLMGateway   # deferred — circular guard
    from shared.config import MINI_JUDGE_MODEL
    user_payload: str = (
        f"### CODE_A (AI wrote):\n{original_code}\n\n"
        f"### CODE_B (human corrected):\n{user_code}"
    )
    try:
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _RULE_DISTILLER_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            model=MINI_JUDGE_MODEL,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=80,
            session_id=session_id,
        )
        raw = response.choices[0].message.content
        if raw is None:
            return None
        parsed = json.loads(raw)
        rule = parsed.get("rule")
        if rule is None:
            return None
        rule_str: str = str(rule).strip()
        if not rule_str or rule_str.lower() == "none":
            return None
        logger.info("RuleDistiller: extracted rule (%d chars)", len(rule_str))
        return rule_str
    except Exception as exc:
        logger.warning("RuleDistiller: LLM failed (skipping): %s", exc)
        return None
