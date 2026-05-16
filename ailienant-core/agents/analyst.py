# ailienant-core/agents/analyst.py
#
# Phase 2.21 — Socratic "Grill Me" AnalystAgent.
#
# Implements the Matt Pocock "Grill Me" pattern:
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
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("ANALYST_AGENT")

DEBUG_MODE = True  # Phase 4: real LLM call; same pattern as planner.py

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
    "\n> Para sintetizar el plan, responde con 'OK', 'Proceed', o 'Dale'."
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
    """Phase 3.4.2: One-shot LLM call to summarise last N user intents (Pre-Dream Reflection)."""
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


async def run_analyst_node(state: dict) -> dict:
    """LangGraph node: Socratic Grill Me AnalystAgent (Phase 2.21).

    Each invocation asks ONE question, broadcasts it non-blockingly, and sets
    hitl_pending=True so the graph suspends. The next task_service.py invocation
    carries the user's answer as user_input; the _merge_messages reducer
    accumulates Q&A history across invocations.

    Phase 4: Replace DEBUG stub with real LLM call + VFS read_file tool binding.
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
    else:
        # Phase 4: real LLM call with VFS read_file tool.
        question = await _generate_question_llm(messages + new_messages, user_input)

    # Non-blocking broadcast — graph must not stall on WS I/O.
    from api.websocket_manager import vfs_manager  # deferred: avoids circular import
    _t = asyncio.create_task(vfs_manager.broadcast_token(task_id, question))
    _background_tasks.add(_t)
    _t.add_done_callback(_background_tasks.discard)

    new_messages.append({"role": "assistant", "content": question})

    return {
        "hitl_pending": True,
        "shared_understanding_reached": False,
        "messages": new_messages,
    }


async def _generate_question_llm(messages: List[Dict], user_input: str) -> str:
    """Phase 4 stub: real LLM call with VFS read_file tool bound via closure."""
    raise NotImplementedError(
        "Phase 4: wire LLMGateway with make_read_file_tool(vfs.read) via tool_registry."
    )


# ---------------------------------------------------------------------------
# Phase 3.4.3a — Nightmare Protocol
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
    """Parse the JSON body of a Nightmare/SupremeJudge response. Failsafe on bad input."""
    if raw_content is None:
        return _NIGHTMARE_FAILSAFE
    try:
        parsed = json.loads(raw_content)
        clamped_reward: float = max(0.0, min(1.0, float(parsed.get("reward", 0.0))))
        violated = parsed.get("violated_rules", [])
        if not isinstance(violated, list):
            violated = []
        return NightmareEvaluation(
            reward=clamped_reward,
            violated_rules=[str(v) for v in violated],
        )
    except (json.JSONDecodeError, TypeError, ValueError):
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
# Phase 3.4.8 — Supreme Judge (Tier.CLOUD reward evaluation)
# ---------------------------------------------------------------------------

async def supreme_judge_evaluate(
    code_delta: str,
    rules_json_path: str,
    session_id: str = "",
) -> NightmareEvaluation:
    """Phase 3.4.8 — Tier.CLOUD reward evaluation for MCTS rollouts.

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
# Phase 3.4.7 — Rule Distillation
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
