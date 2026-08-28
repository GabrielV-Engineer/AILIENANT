# core/observability.py
"""Tracing bootstraps — env-gated, opt-in. Two independent backends:

- LangSmith: LangChain/LangGraph natively export to it when the standard env
  vars are set, so no per-node instrumentation and no new local sink.
- Phoenix (self-hosted): a distinct artifact class from core/telemetry_log.py's
  flat event log — nested, timed spans, queryable as a waterfall — not a second
  sink for the same data (Section 12).

Both only confirm, once at startup, whether tracing is actually live —
returning a clear boolean for observability of the observability — and never
emit credentials to logs or transcripts (zero-trust secrets hygiene).
"""
from __future__ import annotations

import logging
import os
import socket
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger("OBSERVABILITY")

# Truthy values accepted by every boolean tracing toggle in this module.
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


# ── Phoenix (self-hosted OTel tracing) ──────────────────────────────────────

_PHOENIX_ENDPOINT_VAR = "PHOENIX_COLLECTOR_ENDPOINT"
_DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006"
_PHOENIX_PROBE_TIMEOUT_S = 1.5

# Re-registering an OTel provider or re-instrumenting an already-wrapped
# litellm/langchain function is unsafe, so a second lifespan cycle in one
# process (repeated TestClient(app) instantiation) must short-circuit here.
_phoenix_configured = False
_phoenix_tracer_provider: Optional[Any] = None


def _phoenix_endpoint_reachable(endpoint: str) -> bool:
    """One TCP connect, mirroring core/config/host_discovery.py::probe_host_alive
    — the cheapest deterministic proof a listener exists, so an opted-in but
    unstarted Phoenix container fails fast with one log line instead of a
    repeating export error on every batch flush.
    """
    parsed = urlparse(endpoint)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=_PHOENIX_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def configure_phoenix_tracing() -> bool:
    """Return True iff Phoenix span tracing was armed this call.

    Off by default (``AILIENANT_ENABLE_PHOENIX_TRACING``). The client packages
    are dev-only (never installed in the runtime image, per requirements-dev.txt's
    DEBT-179 split), so the imports are lazy and an ``ImportError`` is a normal,
    silent no-op rather than a fault. Instruments both LangChain (graph-node
    spans) and litellm (LLM-call spans, since every model call here is a raw
    ``litellm.acompletion`` — no agent uses a LangChain chat model).
    """
    global _phoenix_configured, _phoenix_tracer_provider
    if _phoenix_configured:
        return True
    if (os.getenv("AILIENANT_ENABLE_PHOENIX_TRACING") or "").strip().lower() not in _TRUTHY:
        logger.info("Phoenix tracing disabled (no env opt-in); no exporter armed.")
        return False

    endpoint = os.getenv(_PHOENIX_ENDPOINT_VAR) or _DEFAULT_PHOENIX_ENDPOINT
    if not _phoenix_endpoint_reachable(endpoint):
        logger.info(
            "Phoenix tracing opted in but %s is unreachable — skipping (endpoint=%s).",
            _PHOENIX_ENDPOINT_VAR, endpoint,
        )
        return False

    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from openinference.instrumentation.litellm import LiteLLMInstrumentor
        from phoenix.otel import register
    except ImportError:
        logger.info("Phoenix tracing opted in but the client packages are not installed.")
        return False

    try:
        # register() is called WITHOUT endpoint= on purpose — verified live
        # against a running Phoenix instance that passing endpoint= explicitly
        # skips the library's own "/v1/traces" suffix logic (a bare POST /
        # gets 405'd), which only runs on the PHOENIX_COLLECTOR_ENDPOINT-driven
        # resolution path. Setting the env var above and letting register()
        # read it itself is the only path that appends the suffix correctly.
        #
        # batch=True: SimpleSpanProcessor (the register() default) exports
        # synchronously on every span end, adding network latency inline to
        # every graph node and LLM call. set_global_tracer_provider=False plus
        # explicit tracer_provider= below avoids ever touching global OTel
        # state, so a second lifespan cycle can never hit "overriding the
        # current TracerProvider is not allowed".
        os.environ[_PHOENIX_ENDPOINT_VAR] = endpoint
        tracer_provider = register(
            protocol="http/protobuf", batch=True,
            set_global_tracer_provider=False, verbose=False,
        )
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
        LiteLLMInstrumentor().instrument(tracer_provider=tracer_provider)
    except Exception:  # noqa: BLE001 — tracing must never break boot
        logger.warning("Phoenix tracing setup failed; continuing without it.", exc_info=True)
        return False

    _phoenix_tracer_provider = tracer_provider
    _phoenix_configured = True
    logger.info("Phoenix tracing enabled via environment (endpoint=%s).", endpoint)
    return True


def shutdown_phoenix_tracing() -> None:
    """Flush in-flight batched spans. No-op if never configured, never raises.

    Deliberately does NOT reset the module latch — unlike the telemetry-log
    sink, an armed TracerProvider/instrumented-function pair cannot be safely
    re-registered within the same process, so a later lifespan cycle must keep
    treating this as already configured rather than retry.
    """
    if _phoenix_tracer_provider is None:
        return
    try:
        _phoenix_tracer_provider.force_flush()
    except Exception:  # noqa: BLE001 — shutdown must never raise
        logger.warning("Phoenix tracer flush failed during shutdown.", exc_info=True)
