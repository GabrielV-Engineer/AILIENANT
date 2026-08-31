# core/memory/context_auditor.py
"""Context Auditor: Mini-Judge classifier and routing-tier derivation.

audit_task_complexity        — async, cheap LLM 3-state semantic risk verdict.
compute_task_complexity_index — pure structural TCI score (no LLM, no I/O).
derive_routing_decision      — pure-function tier mapping (no I/O).
resolve_model_alias_for_routing — routing decision → model alias, with a per-role floor.
hardware_reroute             — pure hardware-aware graceful-degradation override.
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

    Failure semantics are deliberately asymmetric. Empty input is genuinely
    trivial, so it returns NONE. An unreachable classifier is NOT evidence of
    a simple task — it is absence of evidence, and this is the only semantic
    escalation gate in the routing spine, so it fails toward MEDIUM instead.
    One notch, never straight to CLOUD: a local-engine outage must not silently
    redirect every turn to a paid remote API.

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
    except Exception as exc:  # noqa: BLE001 — a judge fault must not crash the turn
        logger.warning(
            "MiniJudge: LLM call failed (non-fatal, escalating to MEDIUM): %s",
            exc, exc_info=True,
        )
        return RiskLevel.MEDIUM


# ── Shared lexical vocabulary ────────────────────────────────────────────────
# Word-lists and shape limits used by the ambiguity gate and the structural TCI
# score below. Every scan here runs over untrusted user input, so the patterns
# stay linear-time: literal character classes and `str.split()`, never a nested
# quantifier that could backtrack.
_SHORT_PROMPT_MAX_CHARS: int = 120
_SHORT_PROMPT_MAX_WORDS: int = 14

# Hard ceiling on how much of a prompt any scorer will scan. A pasted megabyte
# must cost a bounded slice, not a proportional CPU stall on the event loop.
_TCI_SCAN_CAP: int = 8000

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


# ── Task Complexity Index ────────────────────────────────────────────────────
# Saturation points: the count at which a sub-signal is considered maximally
# complex. Anchored to observable shape, not tuned to hit a verdict — a request
# touching ~8 distinct files is already broad enough that no larger number
# should route differently, and ~6 separate requirements is past the point where
# a small model reliably holds the whole contract in one draft.
_TCI_BREADTH_SATURATION: float = 8.0
_TCI_REQUIREMENT_SATURATION: float = 6.0
_TCI_WORDS_SATURATION: float = 120.0

# Sub-signal weights, mirroring the shape of the sibling CSS formula in
# agents/researcher.py. Breadth leads because "how much surface does this touch"
# is the most reliable observable; the greenfield premium is the smallest because
# it is a binary flag rather than a graded measurement. The final weight in each
# group is DERIVED from the others rather than written out, so the set can never
# stop summing to 1.0 and silently rescale TCI off the 0-100 range the routing
# bands are calibrated against.
_TCI_W_BREADTH: float = 0.45
_TCI_W_LOAD: float = 0.35
_TCI_W_GREENFIELD: float = 1.0 - _TCI_W_BREADTH - _TCI_W_LOAD

# Within the instruction-load term: how many distinct deliverables were asked for
# dominates how verbosely they were asked for.
_TCI_W_REQUIREMENTS: float = 0.7
_TCI_W_VERBOSITY: float = 1.0 - _TCI_W_REQUIREMENTS

# Any prompt carries at least one deliverable before markers are counted.
_TCI_BASE_REQUIREMENTS: float = 1.0

# Percent scale — the 0-100 range derive_routing_decision's bands are defined on.
_TCI_SCALE: float = 100.0

# Markers that separate one requirement from the next in a single prompt.
_REQUIREMENT_MARKERS = frozenset({"also", "then", "plus", "additionally", "and"})


def compute_task_complexity_index(
    *,
    user_input: str,
    explicit_mentions: int = 0,
    dirty_buffers: int = 0,
    retrieved_files: int = 0,
    corpus_empty: bool = False,
) -> float:
    """Score how capable a model this turn needs, on the documented 0-100 scale.

    Pure, bounded, no I/O and no LLM call — this runs on every turn, so it counts
    what is already in memory and nothing else. Three observable sub-signals,
    weighted like the sibling CSS formula in agents/researcher.py:

        breadth 45%  — distinct files the turn will touch.
        load    35%  — how much instruction the model must satisfy at once.
        green   20%  — creating from an empty corpus is maximal work, not
                       minimal; scoring it as trivial routes a whole-project
                       build to the cheapest model, which is exactly backwards.

    Structural only, by design. The Mini-Judge's semantic verdict is applied as a
    FLOOR over this score by the routing cascade that owns both signals, so the
    escalation rule lives in exactly one place rather than being duplicated here.
    The result is consumed by ``derive_routing_decision``, whose band boundaries
    own the actual tier choice — this places a turn within those bands and never
    redefines them.
    """
    text = (user_input or "").strip()[:_TCI_SCAN_CAP]
    lowered = text.lower()
    words = lowered.split()
    word_list = re.findall(r"[a-z']+", lowered)
    tokens = set(word_list)

    breadth_count = float(retrieved_files + explicit_mentions + dirty_buffers)
    breadth = min(1.0, breadth_count / _TCI_BREADTH_SATURATION)

    # One requirement is implicit in any prompt; each action verb or conjunction
    # marker beyond that signals another deliverable in the same ask. Counted by
    # OCCURRENCE, not by distinct word: "refactor A, then refactor B, then
    # refactor C" is three deliverables, and set-cardinality scored it as one —
    # systematically under-routing exactly the repetitive multi-target asks a
    # small model is least able to hold in a single draft.
    requirements = _TCI_BASE_REQUIREMENTS + float(
        sum(1 for w in word_list if w in _ACTION_VERBS or w in _REQUIREMENT_MARKERS)
    )
    verbosity = min(1.0, len(words) / _TCI_WORDS_SATURATION)
    load = (
        min(1.0, requirements / _TCI_REQUIREMENT_SATURATION) * _TCI_W_REQUIREMENTS
        + verbosity * _TCI_W_VERBOSITY
    )

    greenfield = 1.0 if (corpus_empty and (tokens & _ACTION_VERBS)) else 0.0

    structural = (
        _TCI_W_BREADTH * breadth
        + _TCI_W_LOAD * load
        + _TCI_W_GREENFIELD * greenfield
    ) * _TCI_SCALE
    return min(_TCI_SCALE, max(0.0, structural))


# ── Ambiguity gate: a pre-RAG underspecified-imperative probe ───────────────
# A short imperative that names an action verb and leans on a deictic pronoun
# ("fix this", "make it better") but gives the researcher no concrete anchor —
# no @-mention, no active file, no path/symbol in the text itself — wastes a
# full retrieval + skeleton pass on a guess. Reuses the shared word-lists above
# for consistency. Deliberately conservative: any concrete anchor always
# disqualifies, so a false negative just runs retrieval as before (harmless)
# while a false positive would interrupt a clear request.


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
    if not text or len(text) > _SHORT_PROMPT_MAX_CHARS:
        return False
    if _CODE_SIGNAL_RE.search(text):
        return False
    lowered = text.lower()
    if len(lowered.split()) > _SHORT_PROMPT_MAX_WORDS:
        return False
    tokens = set(re.findall(r"[a-z']+", lowered))
    return bool(tokens & _ACTION_VERBS) and bool(tokens & _CONTEXT_DEICTIC)


# Band boundaries — the committed TCI→tier contract. Named because both the
# forward mapping (`derive_routing_decision`) and its inverse (`tci_floor_for_tier`)
# read them: two literal copies of 30/50/75 would be two sources of truth that
# drift the moment one band is retuned.
_TCI_BAND_LOCAL_MEDIUM: float = 30.0
_TCI_BAND_LOCAL_BIG: float = 50.0
_TCI_BAND_CLOUD: float = 75.0

# Inverse of the band table: the minimum TCI that lands on each tier. LOCAL_SMALL
# floors at 0.0 — it is the bottom band, so nothing needs raising to reach it.
_TCI_TIER_FLOORS: dict[str, float] = {
    "LOCAL_SMALL": 0.0,
    "LOCAL_MEDIUM": _TCI_BAND_LOCAL_MEDIUM,
    "LOCAL_BIG": _TCI_BAND_LOCAL_BIG,
    "CLOUD": _TCI_BAND_CLOUD,
}


def derive_routing_decision(
    tci: float, css: float, corpus_empty: bool = False
) -> str:
    """Map TCI + CSS to a ContextMeter routing_decision tier string — the
    single function deciding which model size actually fits a turn's
    real context sufficiency and task complexity.

        css < 40           → CLOUD       (red-alert: maximum context needed)
        tci < 30           → LOCAL_SMALL (simple task, privacy-first)
        30 ≤ tci < 50      → LOCAL_MEDIUM (moderate complexity)
        50 ≤ tci < 75      → LOCAL_BIG    (substantial complexity)
        tci ≥ 75           → CLOUD        (cognitively demanding)

    These band boundaries are the committed contract: ``compute_task_complexity_index``
    places a turn within them and must never redefine them.

    corpus_empty distinguishes "no corpus to retrieve from" from "rich corpus but
    low coverage": both yield a low CSS, but only the latter warrants escalating to
    CLOUD. When the corpus is empty the red-alert floor is skipped and routing falls
    to the TCI bands alone — which is exactly why TCI carries a greenfield premium:
    an empty workspace must not read as a cheap task simply because there is
    nothing to retrieve. CSS stays truthful in telemetry — only this escalation
    decision changes.
    """
    if css < 40.0 and not corpus_empty:
        return "CLOUD"
    if tci < _TCI_BAND_LOCAL_MEDIUM:
        return "LOCAL_SMALL"
    if tci < _TCI_BAND_LOCAL_BIG:
        return "LOCAL_MEDIUM"
    if tci < _TCI_BAND_CLOUD:
        return "LOCAL_BIG"
    return "CLOUD"


def tci_floor_for_tier(tier: str) -> float:
    """Lowest TCI that ``derive_routing_decision`` maps onto ``tier``.

    The inverse of the band table above, derived from the same constants so the
    two can never disagree. A semantic escalation raises the *tier*; the score
    that justifies it must be raised to match, or the persisted meter reports a
    complexity its own bands would have routed somewhere else — a contradiction
    the reviewable route card surfaces directly to the operator.

    An unknown tier yields 0.0: a floor that cannot be resolved must never
    silently inflate a turn's recorded complexity.
    """
    return _TCI_TIER_FLOORS.get(tier, 0.0)


# Maps a computed routing_decision onto the litellm proxy alias (agents/*.py's
# actual call target) that decision is meant to reach. Each of the four maps onto
# the tier it names, CLOUD included — the preset's own cloud model is a distinct
# target from big, and collapsing them made the top of the escalation ladder
# unreachable from every agent path. A caller with no computed decision yet (a
# cache-hit turn, a benchmark stub, or the Researcher's own deliberately-fixed
# grounding call) keeps using its own ``default``.
def resolve_model_alias_for_routing(
    routing_decision: Optional[str], default: str, floor: Optional[str] = None
) -> str:
    """Resolve the litellm proxy alias a computed routing decision should
    reach, falling back to ``default`` when the decision is absent or not one
    ``derive_routing_decision`` ever actually returns.

    This is what makes the routing decision — computed, persisted, and
    rendered in the Context Meter widget — actually influence which model
    tier an agent requests, instead of every agent hardcoding a fixed alias
    regardless of what the decision said (N9).

    ``floor`` lets a role declare the least capable tier it can function on; a
    decision below it is raised to it, and everything at or above passes through
    untouched so the router still owns the escalation. The planner uses this: its
    output *shape* gates the whole turn, so a plan drafted by the smallest model
    costs every downstream node, whereas a coder step is per-file and has
    validate_output plus the acceptance checks behind it.
    """
    from shared.config import MODEL_BIG, MODEL_CLOUD, MODEL_MEDIUM, MODEL_SMALL

    # Declared in ascending capability order, so insertion order IS the ladder the
    # floor compares against. Deriving the ladder from the mapping keeps one source
    # of truth: a second hand-maintained tuple would silently disagree the first
    # time a tier is added or reordered.
    mapping = {
        "LOCAL_SMALL": MODEL_SMALL,
        "LOCAL_MEDIUM": MODEL_MEDIUM,
        "LOCAL_BIG": MODEL_BIG,
        "CLOUD": MODEL_CLOUD,
    }
    ladder: Tuple[str, ...] = tuple(mapping)
    decision = routing_decision or ""
    if floor and decision in ladder and floor in ladder:
        if ladder.index(decision) < ladder.index(floor):
            decision = floor
    return mapping.get(decision, default)


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
