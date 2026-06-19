# core/observability.py
"""LangSmith tracing bootstrap — env-gated, opt-in, zero new sink.

LangChain/LangGraph natively export traces to LangSmith when the standard env
vars are set, so this module adds no per-node instrumentation and no new local
log sink. It only confirms, once at startup, whether tracing is actually live —
returning a clear boolean for observability of the observability — and never
emits the API key to logs or transcripts (zero-trust secrets hygiene).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("OBSERVABILITY")

# Truthy values accepted for the LangChain tracing toggle.
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})

# Either env name carries the LangSmith credential, depending on SDK generation.
_KEY_VARS: tuple[str, ...] = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")


def configure_langsmith() -> bool:
    """Return True iff LangSmith tracing is enabled by the environment.

    Off by default: tracing is live only when ``LANGCHAIN_TRACING_V2`` is truthy
    AND a credential is present. A no-op otherwise — no sink, no network egress.
    The key itself is never logged.
    """
    tracing_on = (os.getenv("LANGCHAIN_TRACING_V2") or "").strip().lower() in _TRUTHY
    has_key = any(os.getenv(name) for name in _KEY_VARS)

    if not (tracing_on and has_key):
        logger.info("LangSmith tracing disabled (no env opt-in); telemetry channel unchanged.")
        return False

    project = os.getenv("LANGCHAIN_PROJECT") or os.getenv("LANGSMITH_PROJECT") or "default"
    logger.info("LangSmith tracing enabled via environment (project=%s).", project)
    return True
