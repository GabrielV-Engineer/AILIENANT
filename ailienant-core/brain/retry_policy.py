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
