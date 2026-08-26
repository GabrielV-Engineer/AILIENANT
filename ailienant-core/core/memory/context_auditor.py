# core/memory/context_auditor.py
"""Context Auditor: Mini-Judge classifier and routing-tier derivation.

audit_task_complexity   — async, cheap LLM 3-state semantic risk verdict.
is_fast_track_eligible  — pure lexical trivial-query probe (no LLM, no I/O).
derive_routing_decision — pure-function tier mapping (no I/O).
hardware_reroute        — pure hardware-aware graceful-degradation override.
"""
from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any, List, Optional, Tuple

from shared.config import MINI_JUDGE_MODEL

logger = logging.getLogger("CONTEXT_AUDITOR")


class RiskLevel(str, Enum):
    """Semantic risk tiers emitted by the Mini-Judge.

    Ordering matters for the Veto Authority in agents/planner.py:
        NONE   — defer to mathematical routing.
        MEDIUM — force at least LOCAL_BIG.
        HIGH   — absolute veto → CLOUD.
    """
    NONE = "NONE"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_MINI_JUDGE_SYSTEM: str = (
    "Audit the user's coding task for semantic risk. Classify as one of:\n"
    "  HIGH   — multi-file refactoring impacts, deep AST mutations (class "
    "hierarchies, decorators, or core signatures), or logical complexity "
    "that outstrips local context availability.\n"
    "  MEDIUM — single-module refactor, non-trivial logic changes, or "
    "moderate scope touching more than a few functions.\n"
    "  NONE   — queries, explanations, regex fixes, or minor isolated edits.\n"
    "Respond with exactly one word: HIGH, MEDIUM, or NONE."
)


async def audit_task_complexity(user_input: str, session_id: str = "") -> RiskLevel:
    """Return the semantic RiskLevel for user_input.

    Uses MINI_JUDGE_MODEL for fast 3-state classification.
    Returns RiskLevel.NONE on empty input or any LLM failure
    (fail-safe default: don't escalate).
    LLMGateway is deferred to avoid circular imports at module load time.
    """
    if not user_input.strip():
        return RiskLevel.NONE
    try:
        from tools.llm_gateway import LLMGateway  # deferred — avoids circular at module level
        response = await LLMGateway.ainvoke(
            messages=[
                {"role": "system", "content": _MINI_JUDGE_SYSTEM},
                {"role": "user", "content": user_input},
            ],
            model=MINI_JUDGE_MODEL,
            temperature=0.0,
            max_tokens=8,
            session_id=session_id,
        )
        raw: Optional[str] = response.choices[0].message.content
        verdict = (raw or "").strip().upper()
        if verdict.startswith("HIGH"):
            risk = RiskLevel.HIGH
        elif verdict.startswith("MEDIUM"):
            risk = RiskLevel.MEDIUM
        else:
            risk = RiskLevel.NONE
        logger.info(
            "MiniJudge: input_len=%d verdict=%r risk=%s",
            len(user_input), verdict, risk.value,
        )
        return risk
    except Exception as exc:
        logger.warning("MiniJudge: LLM call failed (non-fatal, defaulting NONE): %s", exc)
        return RiskLevel.NONE


# ── Fast Track: a pre-RAG trivial-query probe ────────────────────────────────
# A self-contained trivial query (a greeting, a definition, a general "what is X"
# question) needs no workspace retrieval to answer. Detecting it lexically lets
# the planner skip the whole GraphRAG extraction — the most expensive turn on a
# cold workspace — and route LOCAL_SMALL directly. The probe is deliberately
# conservative: a false negative merely runs retrieval as before (harmless), while
# a false positive would skip context a real coding task needs, so anything that
# smells like code, an action, or a workspace reference disqualifies the query.
_FAST_TRACK_MAX_CHARS: int = 120
_FAST_TRACK_MAX_WORDS: int = 14

# Punctuation / syntax that betrays code, paths, or symbols rather than prose.
_CODE_SIGNAL_RE = re.compile(r"[/\\{}()\[\]<>;=]|`|::|->|\.\w{1,6}\b")

# Verbs that imply work the planner must contextualise, not a question to answer.
_ACTION_VERBS = frozenset({
    "refactor", "implement", "fix", "add", "create", "edit", "write", "delete",
    "remove", "rename", "migrate", "build", "generate", "update", "modify",
    "debug", "optimize", "optimise", "install", "run", "execute", "deploy",
    "patch", "merge", "test", "compile", "configure", "wire", "extract",
    "analyze", "analyse", "review", "inspect", "audit",
})

# Deictic references that bind the query to the open workspace → not self-contained.
_CONTEXT_DEICTIC = frozenset({
    "this", "that", "these", "those", "here", "current", "above", "below", "my",
})

# Nouns that name the workspace or its code structure → the query needs retrieval.
_CONTEXT_NOUNS = frozenset({
    "workspace", "codebase", "repo", "repository", "project", "directory",
    "folder", "file", "files", "function", "class", "module", "package",
})


def is_fast_track_eligible(user_input: str) -> bool:
    """True when user_input is a self-contained trivial query that needs no RAG.

    Pure and side-effect free. Eligibility requires a short, prose-shaped question
    with no code signals, no action verbs, and no workspace-deictic references.
    """
    text = (user_input or "").strip()
    if not text or len(text) > _FAST_TRACK_MAX_CHARS:
        return False
    lowered = text.lower()
    words = lowered.split()
    if len(words) > _FAST_TRACK_MAX_WORDS:
        return False
    if _CODE_SIGNAL_RE.search(text):
        return False
    tokens = set(re.findall(r"[a-z']+", lowered))
    if tokens & _ACTION_VERBS or tokens & _CONTEXT_DEICTIC or tokens & _CONTEXT_NOUNS:
        return False
    return True


# ── Ambiguity gate: a pre-RAG underspecified-imperative probe ───────────────
# A short imperative that names an action verb and leans on a deictic pronoun
# ("fix this", "make it better") but gives the researcher no concrete anchor —
# no @-mention, no active file, no path/symbol in the text itself — wastes a
# full retrieval + skeleton pass on a guess. Reuses is_fast_track_eligible's
# word-lists for consistency. Conservative like its sibling: any concrete
# anchor always disqualifies, so a false negative just runs retrieval as
# before (harmless) while a false positive would interrupt a clear request.


def is_underspecified(
    user_input: str,
    *,
    explicit_mentions: List[str],
    active_file_path: str = "",
) -> bool:
    """True when a short imperative prompt has no concrete target to act on.

    Pure and side-effect free. A concrete anchor — an @-mention, an open
    active file, or a code/path signal in the text itself — always
    disqualifies. Otherwise requires both an action verb and a deictic
    reference (the combination that signals "do X to this/that" with no
    named target) inside a short prompt.
    """
    if explicit_mentions or active_file_path:
        return False
    text = (user_input or "").strip()
    if not text or len(text) > _FAST_TRACK_MAX_CHARS:
        return False
    if _CODE_SIGNAL_RE.search(text):
        return False
    lowered = text.lower()
    if len(lowered.split()) > _FAST_TRACK_MAX_WORDS:
        return False
    tokens = set(re.findall(r"[a-z']+", lowered))
    return bool(tokens & _ACTION_VERBS) and bool(tokens & _CONTEXT_DEICTIC)


def derive_routing_decision(
    tci: float, css: float, fast_track: bool = False, corpus_empty: bool = False
) -> str:
    """Map TCI + CSS to a ContextMeter routing_decision tier string — the
    single function deciding which model size actually fits a turn's
    real context sufficiency and task complexity.

        fast_track        → LOCAL_SMALL  (trivial query, pre-RAG privacy-first path)
        css < 40           → CLOUD       (red-alert: maximum context needed)
        tci < 30           → LOCAL_SMALL (simple task, privacy-first)
        30 ≤ tci < 50      → LOCAL_MEDIUM (moderate complexity)
        50 ≤ tci < 75      → LOCAL_BIG    (substantial complexity)
        tci ≥ 75           → CLOUD        (cognitively demanding)

    LOCAL_MEDIUM's band was carved out of the middle of the old, single
    30-75 LOCAL_BIG band rather than added on top of it — every existing
    boundary (30, 75) and every existing verdict at those boundaries is
    unchanged; only the interior of that range now has a genuine middle
    destination instead of jumping straight from a simple task's model to
    its most capable local one.

    A fast-track query short-circuits to LOCAL_SMALL before the CSS floor: its
    context is sufficient by decree (nothing to retrieve), so the red-alert gate
    must not fire on the uncomputed CSS=0 of a skipped retrieval.

    corpus_empty distinguishes "no corpus to retrieve from" from "rich corpus but
    low coverage": both yield a low CSS, but only the latter warrants escalating to
    CLOUD. When the corpus is empty the red-alert floor is skipped and routing falls
    to the TCI bands alone, keeping a cold/tiny workspace local-first and cheap. CSS
    stays truthful in telemetry — only this escalation decision changes.
    """
    if fast_track:
        return "LOCAL_SMALL"
    if css < 40.0 and not corpus_empty:
        return "CLOUD"
    if tci < 30.0:
        return "LOCAL_SMALL"
    if tci < 50.0:
        return "LOCAL_MEDIUM"
    if tci < 75.0:
        return "LOCAL_BIG"
    return "CLOUD"


# Maps a computed routing_decision onto the litellm proxy alias
# (agents/*.py's actual call target) that decision is meant to reach.
# "CLOUD" maps to MODEL_BIG, not a dedicated cloud alias, matching the
# existing precedent in core/resource_manager.py's own SWITCH_TO_CLOUD branch
# (which constructs its escalation LLMProfile around MODEL_BIG too) — the
# `ailienant/*` proxy alias space has never had a fourth "cloud" alias
# distinct from "big"; introducing one is a larger contract change than this
# fix takes on. The other three map onto the tier they name directly,
# including LOCAL_MEDIUM now that derive_routing_decision genuinely selects
# it for moderate-complexity turns rather than jumping straight from SMALL
# to BIG — a caller with no computed decision yet (a cache-hit turn, a
# benchmark stub, or the Researcher's own deliberately-fixed grounding call)
# keeps using its own ``default``.
def resolve_model_alias_for_routing(routing_decision: Optional[str], default: str) -> str:
    """Resolve the litellm proxy alias a computed routing decision should
    reach, falling back to ``default`` when the decision is absent or not one
    ``derive_routing_decision`` ever actually returns.

    This is what makes the routing decision — computed, persisted, and
    rendered in the Context Meter widget — actually influence which model
    tier an agent requests, instead of every agent hardcoding a fixed alias
    regardless of what the decision said (N9).
    """
    from shared.config import MODEL_BIG, MODEL_MEDIUM, MODEL_SMALL

    mapping = {
        "LOCAL_SMALL": MODEL_SMALL,
        "LOCAL_MEDIUM": MODEL_MEDIUM,
        "LOCAL_BIG": MODEL_BIG,
        "CLOUD": MODEL_BIG,
    }
    return mapping.get(routing_decision or "", default)


def hardware_reroute(
    routing: str,
    provider: str,
    profile: Optional[Any] = None,
    *,
    cloud_available: bool,
    overflow_risk: bool = False,
) -> Tuple[str, str, Optional[str]]:
    """Hardware-aware graceful degradation applied after the routing cascade.

    Returns ``(routing, provider, warning)``. Only a LOCAL_* decision is eligible
    for an override; a CLOUD/HUMAN_REQUIRED decision and a missing profile are
    pass-throughs (no-op, warning ``None``).

    A LOCAL_* decision is rerouted when the host cannot run it safely — effective
    VRAM below the configured cloud floor, or a predicted context overflow against
    the candidate local window. When cloud is reachable the task moves to CLOUD;
    otherwise it degrades to LOCAL_SMALL with an explanatory warning (never blocks,
    never raises). The warning is the user-facing signal for the slowdown.
    """
    from shared.config import VRAM_CLOUD_FLOOR_GB
    from shared.hardware import HardwareProfile, effective_vram_gb

    if not routing.startswith("LOCAL") or profile is None:
        return routing, provider, None
    if not isinstance(profile, HardwareProfile):
        return routing, provider, None

    eff = effective_vram_gb(profile)
    vram_low = eff < VRAM_CLOUD_FLOOR_GB
    if not (vram_low or overflow_risk):
        return routing, provider, None

    if vram_low and overflow_risk:
        reason = (
            f"effective VRAM {eff:.1f}GB below the {VRAM_CLOUD_FLOOR_GB:.1f}GB floor "
            "and the request is predicted to overflow the local context window"
        )
    elif vram_low:
        reason = f"effective VRAM {eff:.1f}GB below the {VRAM_CLOUD_FLOOR_GB:.1f}GB cloud floor"
    else:
        reason = "request predicted to overflow the local context window"

    if cloud_available:
        warning = f"Hardware fallback: {reason}; routing to cloud."
        return "CLOUD", "CLOUD", warning

    warning = (
        f"Hardware fallback: {reason}, and no cloud provider is configured; "
        "staying on a small local model — responses may be slower or less capable."
    )
    return "LOCAL_SMALL", "LOCAL", warning
