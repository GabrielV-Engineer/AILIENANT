"""Centralized retry/correction budgets.

Single source of truth for the bounded-attempt constants that were previously
scattered across the graph (output guardrail, planner validation loop, the
local→cloud circuit breaker) plus the new self-healing reflexion loop. Co-locating
them makes the resilience envelope auditable at a glance and prevents the budgets
from silently diverging.

Consumers alias these into their own module-level names so existing call sites stay
byte-stable (notably the agents/ package, which is held byte-identical by the
cognitive-isolation fence). The local backoff abstraction inside the LLM gateway is
intentionally out of scope here and is consolidated separately.
"""
from __future__ import annotations

from typing import Dict, Optional

# Output validation self-correction loop (validate_output → coder_agent).
GUARDRAIL_MAX_RETRIES: int = 2

# Planner structured-output validation retries before conceding a malformed plan.
PLANNER_MAX_RETRIES: int = 2

# Retries specifically for a plan that PARSED and VALIDATED cleanly but carried no
# WBS steps. Deliberately tighter than PLANNER_MAX_RETRIES: a schema error names a
# concrete thing a corrective can fix, whereas a model that has already emitted a
# stepless plan twice is unlikely to decompose the request on a third attempt — and
# on local hardware each attempt costs the user minutes of wall-clock.
PLANNER_EMPTY_WBS_MAX_RETRIES: int = 1

# Consecutive local-model failures that escalate a step to the Cloud Surgeon.
CIRCUIT_BREAKER_THRESHOLD: int = 3

# In-turn self-healing: how many times the ErrorCorrectionAgent may read a
# traceback, propose a fix, and retry the failed node before conceding to the DLQ.
CORRECTION_MAX_ATTEMPTS: int = 3

# Cross-turn breaker: how many times the SAME normalized failure signature may
# recur (across graph invocations, in-process) before the reflexion loop stops
# spending LLM calls on a known-unfixable error and routes straight to the DLQ.
FAILURE_SIGNATURE_THRESHOLD: int = 3

# Autonomous ReAct cell — three-axis circuit-breaker constants (brain/iteration_governor.py).
# AGENTIC_CELL_MAX_ITERATIONS is the steps axis and is kept by its original name so existing
# call sites remain byte-stable.
AGENTIC_CELL_MAX_ITERATIONS: int = 6        # max run-read-edit-rerun steps per turn
AGENTIC_CELL_MAX_COST_USD: float = 2.0      # per-turn token-spend ceiling (USD)
AGENTIC_CELL_MAX_ELAPSED_S: float = 300.0   # per-turn wall-clock ceiling (5 min)

# Transport-layer retries handed to litellm for a single LLM call (connection
# blips / transient 5xx). Distinct from the cognitive retry budgets above — this
# is the network envelope, applied uniformly across every gateway invocation.
LLM_MAX_TRANSPORT_RETRIES: int = 2

# SQLite WAL checkpoint backoff: attempts before conceding a deferred checkpoint
# when a concurrent writer keeps the WAL busy.
WAL_CHECKPOINT_MAX_RETRIES: int = 3

# Incremental apply gate (brain/apply_gate.py, 13.0.9): how many "request
# changes" rounds one WBS step may go through before a further revision
# request degrades to a plain reject. Without this bound, an operator
# repeatedly requesting changes on the same step could re-dispatch it
# indefinitely — LangGraph's own recursion_limit would eventually stop it, but
# that surfaces as an opaque graph error, not an honest step-failure message.
APPLY_REJECT_MAX_ATTEMPTS: int = 2

# ── Effort Budget ────────────────────────────────────────────────────────────
# Replaces the old SEQUENTIAL/MICRO_SWARM/FULL_SWARM execution-mode selector,
# which never controlled anything the main graph actually runs — the topology
# channel it wrote was persisted and read back on resume, but no routing
# decision in brain/engine.py ever branched on it. Effort genuinely controls
# three things a turn on slow local hardware can meaningfully trade off:
# whether the lint/LSP gate runs on top of the always-on syntax gate (that one
# is a correctness floor in every tier, never a tier feature), how many
# self-heal attempts a failure gets, and whether the plan's own acceptance
# checks execute at turn end.
EffortLevel = str  # "light" | "balanced" | "deep" — see brain/state.py's Literal

_EFFORT_LEVELS: frozenset[str] = frozenset({"light", "balanced", "deep"})
DEFAULT_EFFORT_LEVEL: str = "balanced"

# Light: a syntax/exec failure fails the step outright rather than paying a
# local self-heal round-trip — the fast, cheap path exists precisely so a
# trivial turn does not pay CORRECTION_MAX_ATTEMPTS's full cost on hardware
# where each attempt is expensive. Balanced/Deep keep the existing ceiling.
_EFFORT_CORRECTION_CEILING: Dict[str, int] = {
    "light": 0,
    "balanced": CORRECTION_MAX_ATTEMPTS,
    "deep": CORRECTION_MAX_ATTEMPTS,
}


def resolve_correction_ceiling(effort_level: Optional[str]) -> int:
    """Self-heal attempt ceiling for ``effort_level``.

    An unrecognized or absent value falls back to the flat
    ``CORRECTION_MAX_ATTEMPTS`` ceiling — today's exact behaviour — so an older
    checkpoint or a caller that never set the field is unaffected.
    """
    return _EFFORT_CORRECTION_CEILING.get(effort_level or "", CORRECTION_MAX_ATTEMPTS)


def normalize_effort_level(value: Optional[str]) -> str:
    """Coerce an arbitrary ``effort_level`` value to a known level, defaulting
    to :data:`DEFAULT_EFFORT_LEVEL` for anything unrecognized (never raises)."""
    lowered = (value or "").lower()
    return lowered if lowered in _EFFORT_LEVELS else DEFAULT_EFFORT_LEVEL
