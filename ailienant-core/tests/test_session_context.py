"""Unit tests for the live context-window occupancy helper.

Covers the contract that backs the workspace context-budget meter:
  * a cold thread (no checkpoint) reads 0 tokens against the default window
  * a populated window reports the real tokenized length and a clamped pct
  * occupancy reflects the CURRENT window, so a shrunk message list reads lower
  * any read failure degrades to zeros instead of raising (telemetry safety)
"""
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

import pytest

from api import sessions
from api.sessions import compute_context_occupancy

InstallFn = Callable[[Optional["_FakeTuple"]], None]


class _FakeTuple:
    def __init__(self, channel_values: Dict[str, Any]) -> None:
        self.checkpoint: Dict[str, Any] = {"channel_values": channel_values}


@pytest.fixture
def patch_get_tuple(monkeypatch: pytest.MonkeyPatch) -> InstallFn:
    """Swap checkpoint_manager.get_tuple with a controllable stub."""
    def _install(result: Optional[_FakeTuple]) -> None:
        monkeypatch.setattr(
            sessions.checkpoint_manager, "get_tuple",
            lambda _config: result, raising=True,
        )
    return _install


def test_cold_thread_reads_zero(patch_get_tuple: InstallFn) -> None:
    """No checkpoint for the thread → 0 tokens, default window, 0%."""
    patch_get_tuple(None)
    occ = compute_context_occupancy("unknown-thread")
    assert occ.context_used_tokens == 0
    assert occ.context_window == sessions._DEFAULT_CONTEXT_WINDOW
    assert occ.context_pct == 0.0


def test_empty_message_window_reads_zero(patch_get_tuple: InstallFn) -> None:
    """A checkpoint that exists but carries no messages must not throw."""
    patch_get_tuple(_FakeTuple({"messages": [], "active_llm_profile": {"context_window": 8192}}))
    occ = compute_context_occupancy("warm-thread")
    assert occ.context_used_tokens == 0
    assert occ.context_window == 8192
    assert occ.context_pct == 0.0


def test_populated_window_counts_real_tokens(patch_get_tuple: InstallFn) -> None:
    """Real messages produce a positive token count and a clamped fraction."""
    msgs = [
        {"role": "user", "content": "hello world " * 50},
        {"role": "assistant", "content": "a response with several tokens " * 50},
    ]
    patch_get_tuple(_FakeTuple({"messages": msgs, "active_llm_profile": {"context_window": 200_000}}))
    occ = compute_context_occupancy("busy-thread")
    assert occ.context_used_tokens > 0
    assert occ.context_window == 200_000
    assert 0.0 < occ.context_pct <= 1.0


def test_occupancy_tracks_current_window_not_history(patch_get_tuple: InstallFn) -> None:
    """A shorter (pruned) window must read LOWER than a longer one — the meter
    reflects live occupancy, never a monotonic lifetime total."""
    long_msgs = [{"role": "user", "content": "token " * 500}]
    short_msgs = [{"role": "user", "content": "token " * 10}]
    profile = {"context_window": 200_000}

    patch_get_tuple(_FakeTuple({"messages": long_msgs, "active_llm_profile": profile}))
    long_occ = compute_context_occupancy("t")
    patch_get_tuple(_FakeTuple({"messages": short_msgs, "active_llm_profile": profile}))
    short_occ = compute_context_occupancy("t")

    assert short_occ.context_used_tokens < long_occ.context_used_tokens


def test_pydantic_profile_shape_is_read(patch_get_tuple: InstallFn) -> None:
    """active_llm_profile may round-trip as an object, not a dict."""
    profile = SimpleNamespace(context_window=4096)
    patch_get_tuple(_FakeTuple({"messages": [{"content": "hi"}], "active_llm_profile": profile}))
    occ = compute_context_occupancy("t")
    assert occ.context_window == 4096


def test_missing_profile_falls_back_to_default(patch_get_tuple: InstallFn) -> None:
    """No profile in state → default window, still counts message tokens."""
    patch_get_tuple(_FakeTuple({"messages": [{"content": "some text here"}]}))
    occ = compute_context_occupancy("t")
    assert occ.context_window == sessions._DEFAULT_CONTEXT_WINDOW
    assert occ.context_used_tokens > 0


def test_read_failure_degrades_to_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising get_tuple must yield a safe zeroed result, not a 500."""
    def _boom(_config: Any) -> Any:
        raise RuntimeError("L2 connection exploded")
    monkeypatch.setattr(sessions.checkpoint_manager, "get_tuple", _boom, raising=True)
    occ = compute_context_occupancy("t")
    assert occ.context_used_tokens == 0
    assert occ.context_pct == 0.0
