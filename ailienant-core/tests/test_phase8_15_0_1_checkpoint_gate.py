# tests/test_phase8_15_0_1_checkpoint_gate.py
"""LLM Gateway Concurrency Throttle — Prerequisite Checkpoint Gate.

Test-only certification that the outbound-concurrency admission control on
``tools.llm_gateway`` holds against its shipped entry points. It imports and
invokes production code (``LLMGateway`` + the module-level ``_llm_semaphore``),
asserting one load-bearing invariant per row; it modifies no production logic and
follows the sibling-gate convention. House style mirrors ``test_single_flight.py``:
each async row drives an inner ``_run()`` via ``asyncio.run`` (no pytest-asyncio).

Rows certified here:
  THROTTLE1  peak in-flight calls never exceed the ceiling AND the ceiling is
             fully usable (no over-serialization) — proven with peak == ceiling
  THROTTLE2  the ceiling is floored at 1 and falls back to the default on a
             malformed override (the config contract behind the module constant)
  THROTTLE3  a failed call releases its slot — a subsequent call at ceiling 1
             acquires instead of dead-locking
  THROTTLE4  a delegating entry point (acomplete_with_thinking → ainvoke) does
             NOT re-acquire — it completes at ceiling 1 rather than self-locking
  THROTTLE5  a streaming call holds one slot across the whole stream and frees it
             on completion (a second stream at ceiling 1 also completes)
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from shared import config
from tools import llm_gateway
from tools.llm_gateway import LLMGateway


# ── helpers ───────────────────────────────────────────────────────────────────


_MSGS = [{"role": "user", "content": "hi"}]


def _reset_gate(monkeypatch: pytest.MonkeyPatch, ceiling: int) -> None:
    """Pin the ceiling and drop any per-loop semaphores from prior tests.

    The gate is keyed per event loop, so a fresh ``asyncio.run`` loop already gets
    a fresh semaphore; clearing the registry is belt-and-suspenders hygiene.
    """
    monkeypatch.setattr(llm_gateway, "LLM_MAX_CONCURRENCY", ceiling)
    llm_gateway._llm_semaphores.clear()


def _resp(content: str = "ok") -> SimpleNamespace:
    """A minimal ModelResponse stand-in: usage=None keeps token accounting a no-op."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=None,
    )


# ── THROTTLE1 ───────────────────────────────────────────────────────────────


def test_throttle1_peak_concurrency_equals_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_gate(monkeypatch, ceiling=2)
    tracker = {"entered": 0, "peak": 0}

    async def fake_acompletion(**kwargs: object) -> SimpleNamespace:
        tracker["entered"] += 1
        tracker["peak"] = max(tracker["peak"], tracker["entered"])
        await asyncio.sleep(0.02)  # widen the overlap window so peak is observable
        tracker["entered"] -= 1
        return SimpleNamespace(usage=None)

    monkeypatch.setattr(llm_gateway.litellm, "acompletion", fake_acompletion)

    async def _run() -> None:
        # 4 concurrent calls against a ceiling of 2 → at most 2 ever in-flight.
        await asyncio.gather(
            *[LLMGateway.ainvoke(_MSGS, model="test-model") for _ in range(4)]
        )

    asyncio.run(_run())
    assert tracker["peak"] == 2, f"gate must cap in-flight at the ceiling; saw peak={tracker['peak']}"


# ── THROTTLE2 ───────────────────────────────────────────────────────────────


def test_throttle2_ceiling_floored_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # The module constant is `max(1, _env_int("AILIENANT_LLM_MAX_CONCURRENCY", 8))`.
    monkeypatch.setenv("AILIENANT_LLM_MAX_CONCURRENCY", "0")
    assert max(1, config._env_int("AILIENANT_LLM_MAX_CONCURRENCY", 8)) == 1
    monkeypatch.setenv("AILIENANT_LLM_MAX_CONCURRENCY", "-5")
    assert max(1, config._env_int("AILIENANT_LLM_MAX_CONCURRENCY", 8)) == 1
    monkeypatch.setenv("AILIENANT_LLM_MAX_CONCURRENCY", "garbage")
    assert max(1, config._env_int("AILIENANT_LLM_MAX_CONCURRENCY", 8)) == 8
    # The shipped constants must themselves be a valid (>= 1) ceiling.
    assert config.LLM_MAX_CONCURRENCY >= 1
    assert llm_gateway.LLM_MAX_CONCURRENCY >= 1


# ── THROTTLE3 ───────────────────────────────────────────────────────────────


def test_throttle3_slot_released_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_gate(monkeypatch, ceiling=1)

    async def boom(**kwargs: object) -> SimpleNamespace:
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_gateway.litellm, "acompletion", boom)

    async def _run() -> None:
        with pytest.raises(RuntimeError):
            await LLMGateway.ainvoke(_MSGS, model="test-model")
        # If the failed call leaked its slot, this second call would block forever
        # at the ceiling-1 gate; wait_for converts a leak into a test failure.
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(
                LLMGateway.ainvoke(_MSGS, model="test-model"), timeout=1.0
            )

    asyncio.run(_run())


# ── THROTTLE4 ───────────────────────────────────────────────────────────────


def test_throttle4_no_double_acquire_via_delegator(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_gate(monkeypatch, ceiling=1)

    async def fake_acompletion(**kwargs: object) -> SimpleNamespace:
        return _resp("ok")

    monkeypatch.setattr(llm_gateway.litellm, "acompletion", fake_acompletion)

    async def _run() -> None:
        # The fallback branch (no sink, thinking disabled) delegates to ainvoke.
        # If acomplete_with_thinking wrongly re-acquired the gate, it would
        # self-deadlock at ceiling 1; wait_for turns that into a failure.
        out = await asyncio.wait_for(
            LLMGateway.acomplete_with_thinking(_MSGS, model="test-model"),
            timeout=1.0,
        )
        assert out == "ok"

    asyncio.run(_run())


# ── THROTTLE5 ───────────────────────────────────────────────────────────────


def test_throttle5_stream_holds_then_releases_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_gate(monkeypatch, ceiling=1)

    fake_target = SimpleNamespace(
        model="local-x", is_local=True, api_base="http://x", api_key=None
    )
    monkeypatch.setattr(
        "core.config.model_resolver.get_chat_target", lambda tier: fake_target
    )

    async def _chunks() -> object:
        yield SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(content="a"))],
        )
        # Final chunk carries usage; empty choices (include_usage shape).
        yield SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1), choices=[]
        )

    async def fake_acompletion(**kwargs: object) -> object:
        return _chunks()

    monkeypatch.setattr(llm_gateway.litellm, "acompletion", fake_acompletion)

    async def _run() -> None:
        deltas = [d async for d in LLMGateway.astream_byom(_MSGS, tier="medium")]
        assert deltas == ["a"]
        sem = llm_gateway._llm_semaphores.get(asyncio.get_running_loop())
        assert sem is not None and not sem.locked(), "stream must free its slot on completion"
        # A second stream at ceiling 1 must also complete (proves no slot leak).
        deltas2 = [d async for d in LLMGateway.astream_byom(_MSGS, tier="medium")]
        assert deltas2 == ["a"]

    asyncio.run(_run())
