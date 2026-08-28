"""core/observability.py — Phoenix span tracing (13.1.5).

configure_langsmith()'s own tests live at tests/test_phase8_2_checkpoint_gate.py
(a phase-8 gate file); Phoenix tracing is a new backend, not a phase-8 concern,
so it gets its own sibling file rather than growing that gate.

Isolation: _phoenix_configured/_phoenix_tracer_provider are process-global
latches by design (a second lifespan cycle must not re-instrument), so tests
must reset them explicitly between runs — mirrors test_telemetry_log.py's
_isolate_sink fixture.
"""
from __future__ import annotations

from typing import Generator
from unittest.mock import MagicMock

import pytest

import core.observability as obs

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_phoenix_latch() -> Generator[None, None, None]:
    obs._phoenix_configured = False
    obs._phoenix_tracer_provider = None
    yield
    obs._phoenix_configured = False
    obs._phoenix_tracer_provider = None


def _clear_phoenix_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AILIENANT_ENABLE_PHOENIX_TRACING", raising=False)
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)


def test_phoenix_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_phoenix_env(monkeypatch)
    register_spy = MagicMock()
    monkeypatch.setattr("phoenix.otel.register", register_spy)

    assert obs.configure_phoenix_tracing() is False
    register_spy.assert_not_called()


def test_phoenix_deps_absent_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the runtime image, which never installs the dev-only client
    packages (requirements-dev.txt's DEBT-179 split) — an ImportError here is
    the normal Docker-runtime state, not a fault."""
    import sys

    _clear_phoenix_env(monkeypatch)
    monkeypatch.setenv("AILIENANT_ENABLE_PHOENIX_TRACING", "1")
    monkeypatch.setattr(obs, "_phoenix_endpoint_reachable", lambda endpoint: True)
    monkeypatch.setitem(sys.modules, "phoenix.otel", None)

    assert obs.configure_phoenix_tracing() is False


def test_phoenix_unreachable_endpoint_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """An opted-in flag with nothing listening must fail fast with one log
    line, not silently defer to a repeating background export error."""
    _clear_phoenix_env(monkeypatch)
    monkeypatch.setenv("AILIENANT_ENABLE_PHOENIX_TRACING", "1")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:1")  # reserved, never listens
    register_spy = MagicMock()
    monkeypatch.setattr("phoenix.otel.register", register_spy)

    assert obs.configure_phoenix_tracing() is False
    register_spy.assert_not_called()


def test_phoenix_idempotent_second_call_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already-configured must return True without touching env or re-registering
    — re-instrumenting an already-wrapped litellm function is unsafe."""
    _clear_phoenix_env(monkeypatch)
    obs._phoenix_configured = True
    register_spy = MagicMock()
    monkeypatch.setattr("phoenix.otel.register", register_spy)

    assert obs.configure_phoenix_tracing() is True
    register_spy.assert_not_called()


def test_shutdown_before_configure_is_noop() -> None:
    obs.shutdown_phoenix_tracing()  # must not raise


def test_shutdown_does_not_reset_the_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one counterintuitive invariant: unlike shutdown_telemetry_log(),
    shutdown must leave _phoenix_configured set so a later lifespan cycle in
    the same process never attempts to re-instrument."""
    flushed = MagicMock()
    obs._phoenix_configured = True
    obs._phoenix_tracer_provider = MagicMock(force_flush=flushed)

    obs.shutdown_phoenix_tracing()

    flushed.assert_called_once()
    assert obs._phoenix_configured is True


# ── The load-bearing test: does late-patching litellm actually work? ────────


async def test_litellm_acompletion_is_wrapped_and_produces_a_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLMInstrumentor patches via plain setattr(litellm, 'acompletion', ...)
    (verified directly against the installed package source) — an attribute
    lookup resolved at call time, matching every one of tools/llm_gateway.py's
    11 call sites (litellm.acompletion(...), never a bound/aliased import).
    So instrumenting from lifespan() — long after llm_gateway.py's own
    `import litellm` — must still wrap the same callable it calls. This proves
    that, rather than assuming it from the library's own "instrument before
    import" warning (which describes a different, narrower failure mode: an
    aliased `from litellm import acompletion` import, not present anywhere in
    this repo).
    """
    pytest.importorskip("openinference.instrumentation.litellm")
    from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from openinference.instrumentation.litellm import LiteLLMInstrumentor

    import litellm

    exporter = InMemorySpanExporter()
    tracer_provider = SDKTracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

    fake_response = MagicMock()
    fake_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    fake_response.choices = [MagicMock(finish_reason="stop")]
    fake_response.model = "gpt-4o"

    async def _fake_acompletion(*args: object, **kwargs: object) -> MagicMock:
        return fake_response

    # Assigned directly (not via monkeypatch.setattr) and restored explicitly
    # below, inside this function's own body — never left to fixture-teardown
    # ordering. tests/conftest.py's autouse _guard_litellm_patch_leakage
    # snapshots litellm.acompletion at its own setup and logs a false DEBT-201
    # "leak" error at its own teardown if the identity has changed by then;
    # relying on LiteLLMInstrumentor().uninstrument() alone would leave
    # litellm.acompletion pointing at `_fake_acompletion` (what _instrument()
    # captured as "original" here), not the real original, and whether that
    # gets fixed before the leak guard's check runs is an unstated ordering
    # assumption. Restoring it ourselves before this function returns removes
    # the ordering question entirely.
    real_acompletion = litellm.acompletion
    litellm.acompletion = _fake_acompletion

    _clear_phoenix_env(monkeypatch)
    monkeypatch.setenv("AILIENANT_ENABLE_PHOENIX_TRACING", "1")
    monkeypatch.setattr(obs, "_phoenix_endpoint_reachable", lambda endpoint: True)
    monkeypatch.setattr("phoenix.otel.register", lambda **kwargs: tracer_provider)

    try:
        assert obs.configure_phoenix_tracing() is True
        result = await litellm.acompletion(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
        assert result is fake_response  # the wrapper must still return the real result

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "acompletion"
    finally:
        LiteLLMInstrumentor().uninstrument()
        litellm.acompletion = real_acompletion
