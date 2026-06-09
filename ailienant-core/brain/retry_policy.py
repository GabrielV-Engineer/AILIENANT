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

# Output validation self-correction loop (validate_output → coder_agent).
GUARDRAIL_MAX_RETRIES: int = 2

# Planner structured-output validation retries before conceding a malformed plan.
PLANNER_MAX_RETRIES: int = 2

# Consecutive local-model failures that escalate a step to the Cloud Surgeon.
CIRCUIT_BREAKER_THRESHOLD: int = 3

# In-turn self-healing: how many times the ErrorCorrectionAgent may read a
# traceback, propose a fix, and retry the failed node before conceding to the DLQ.
CORRECTION_MAX_ATTEMPTS: int = 3

# Cross-turn breaker: how many times the SAME normalized failure signature may
# recur (across graph invocations, in-process) before the reflexion loop stops
# spending LLM calls on a known-unfixable error and routes straight to the DLQ.
FAILURE_SIGNATURE_THRESHOLD: int = 3

# Autonomous ReAct cell: maximum run-read-edit-rerun iterations in a single turn
# before the loop concedes gracefully (no infinite loop). A single-axis bound — the
# full multi-axis governor (steps AND tokens AND elapsed time) supersedes this with a
# richer envelope; until then this ceiling guarantees in-turn termination.
AGENTIC_CELL_MAX_ITERATIONS: int = 6

# Transport-layer retries handed to litellm for a single LLM call (connection
# blips / transient 5xx). Distinct from the cognitive retry budgets above — this
# is the network envelope, applied uniformly across every gateway invocation.
LLM_MAX_TRANSPORT_RETRIES: int = 2

# SQLite WAL checkpoint backoff: attempts before conceding a deferred checkpoint
# when a concurrent writer keeps the WAL busy.
WAL_CHECKPOINT_MAX_RETRIES: int = 3
