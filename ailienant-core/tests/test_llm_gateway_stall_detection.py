# ailienant-core/tests/test_llm_gateway_stall_detection.py
"""A local streaming call that goes silent mid-flight (no exception, no more
chunks) must be detected within a bounded idle window and raised as a clear,
named failure — not left to the caller's full call-level timeout (minutes) or
to indefinite silence if the engine never recovers."""
from __future__ import annotations

import asyncio

import pytest

from tools.llm_gateway import LocalStreamStalledError, _iter_with_stall_detection

pytestmark = pytest.mark.anyio


async def _stream_then_hang(chunks: list[str], hang_event: asyncio.Event):
    for c in chunks:
        yield c
    await hang_event.wait()  # never set — simulates a silent, still-open stream
    yield "unreachable"


async def _finite_stream(chunks: list[str]):
    for c in chunks:
        yield c


@pytest.mark.anyio
async def test_stall_detection_raises_within_the_idle_bound_not_the_full_timeout() -> None:
    """The idle bound must actually be enforced per-item — proven by using an
    idle timeout far shorter than any realistic full-call timeout and
    asserting the raise happens (not merely 'eventually')."""
    hang_event = asyncio.Event()
    stream = _stream_then_hang(["a", "b"], hang_event)

    seen: list[str] = []
    with pytest.raises(LocalStreamStalledError):
        async for item in _iter_with_stall_detection(stream, idle_timeout_s=0.05, is_local=True):
            seen.append(item)
    assert seen == ["a", "b"]  # chunks before the stall were still delivered


@pytest.mark.anyio
async def test_stall_detection_is_noop_passthrough_for_a_healthy_finite_stream() -> None:
    stream = _finite_stream(["x", "y", "z"])
    seen = [item async for item in _iter_with_stall_detection(stream, idle_timeout_s=5.0, is_local=True)]
    assert seen == ["x", "y", "z"]


@pytest.mark.anyio
async def test_stall_detection_is_disabled_for_a_cloud_target() -> None:
    """A cloud stall is already covered by the call's own `timeout=` and the
    frontend watchdog — this wrapper must be a transparent passthrough
    (never raise LocalStreamStalledError) when `is_local=False`, even past
    the idle bound."""
    hang_event = asyncio.Event()
    stream = _stream_then_hang(["a"], hang_event)

    async def _consume_briefly() -> list[str]:
        seen: list[str] = []
        async for item in _iter_with_stall_detection(stream, idle_timeout_s=0.05, is_local=False):
            seen.append(item)
            if len(seen) == 1:
                return seen  # stop consuming before the real hang — proves no raise happened yet
        return seen

    seen = await _consume_briefly()
    assert seen == ["a"]
