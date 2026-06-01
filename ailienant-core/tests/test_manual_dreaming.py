"""Manual Dreaming — on-demand consolidation, FinOps gate, OCC race guard.

The daemon never wakes on a timer: a pass runs only when ``run_consolidation`` is
awaited. These tests pin the contract — focus injection, the lock wrapping ONLY
the final write, a mid-run save aborting without a write, clean cancellation, and
the over-budget refusal — with the LLM / semantic store / overview faked.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import brain.daemon as daemon_mod
from brain.daemon import ConsolidationResult, OvernightDaemon, overnight_daemon

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------- fakes ----------

def _resp(text: str) -> SimpleNamespace:
    """Minimal litellm-shaped response: ``.choices[0].message.content``."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class _SpyLock:
    """Async context manager that records whether it is currently held."""

    def __init__(self, state: Dict[str, bool]) -> None:
        self._state = state

    async def __aenter__(self) -> "_SpyLock":
        self._state["held"] = True
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        self._state["held"] = False
        return False


class _FakeSemantic:
    """Records each upsert and whether the lock was held at call time."""

    def __init__(self, lock_state: Dict[str, bool]) -> None:
        self._lock_state = lock_state
        self.calls: List[Dict[str, Any]] = []

    async def semantic_upsert(self, file_path: str, content: str, workspace_hash: str) -> bool:
        self.calls.append(
            {
                "file_path": file_path,
                "content": content,
                "workspace_hash": workspace_hash,
                "held": self._lock_state.get("held", False),
            }
        )
        return True


def _make_daemon(
    monkeypatch: pytest.MonkeyPatch,
    *,
    overview: str = "Workspace root: demo\nsrc/app.py",
    invested: float = 0.0,
    invoke: Optional[Any] = None,
):
    """Build a daemon with all seams faked + a spy graph_write_lock installed."""
    lock_state: Dict[str, bool] = {"held": False}
    monkeypatch.setattr(daemon_mod, "graph_write_lock", lambda pid="": _SpyLock(lock_state))
    semantic = _FakeSemantic(lock_state)

    captured: Dict[str, Any] = {"messages": None, "called": False}

    async def _default_invoke(messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        captured["called"] = True
        captured["messages"] = messages
        return _resp("Consolidated insight about the workspace architecture.")

    daemon = OvernightDaemon(
        semantic=semantic,
        overview_fn=lambda root: overview,
        budget_fn=lambda: {"estimated_invested_usd": invested},
        llm_invoke=invoke or _default_invoke,
    )
    return daemon, semantic, captured, lock_state


# ---------- 1. fires only on action (no timer / loop) ----------

def test_daemon_has_no_idle_loop() -> None:
    """The consolidation daemon must expose no heartbeat/idle loop."""
    assert not hasattr(OvernightDaemon, "_loop")
    assert overnight_daemon._running is False


# ---------- 2. focus_area is injected into the prompt ----------

async def test_focus_area_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, _semantic, captured, _ = _make_daemon(monkeypatch)
    await daemon.run_consolidation(
        "proj", "Bug Fixes", workspace_root="/ws", session_id="dream:c1"
    )
    blob = " ".join(m["content"] for m in captured["messages"])
    assert "Bug Fixes" in blob


async def test_auto_focus_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, _semantic, captured, _ = _make_daemon(monkeypatch)
    await daemon.run_consolidation(
        "proj", None, workspace_root="/ws", session_id="dream:c1"
    )
    blob = " ".join(m["content"] for m in captured["messages"])
    assert "No specific focus" in blob


# ---------- 3. lock wraps ONLY the final write ----------

async def test_write_happens_under_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, semantic, _captured, lock_state = _make_daemon(monkeypatch)
    result = await daemon.run_consolidation(
        "proj", "Architecture and Patterns",
        workspace_root="/ws", session_id="dream:c1",
    )
    assert result.status == "written"
    assert len(semantic.calls) == 1
    assert semantic.calls[0]["held"] is True          # upsert ran inside the lock
    assert semantic.calls[0]["workspace_hash"] == "proj"
    assert lock_state["held"] is False                # released afterward


# ---------- 4. save-mid-run aborts without writing ----------

async def test_stale_snapshot_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, semantic, captured, _ = _make_daemon(monkeypatch)
    result = await daemon.run_consolidation(
        "proj", None, workspace_root="/ws", session_id="dream:c1",
        stale_check=lambda: True,
    )
    assert result.status == "aborted_stale"
    assert captured["called"] is True   # LLM ran, but the commit was skipped
    assert semantic.calls == []


# ---------- 5. cancellation mid-LLM never writes ----------

async def test_cancel_mid_llm_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _cancelling_invoke(messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    daemon, semantic, _captured, _ = _make_daemon(monkeypatch, invoke=_cancelling_invoke)
    with pytest.raises(asyncio.CancelledError):
        await daemon.run_consolidation(
            "proj", None, workspace_root="/ws", session_id="dream:c1"
        )
    assert semantic.calls == []


# ---------- 6. over-budget refuses before any LLM call ----------

async def test_over_budget_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, semantic, captured, _ = _make_daemon(monkeypatch, invested=999.0)
    result = await daemon.run_consolidation(
        "proj", "Refactoring and Technical Debt",
        workspace_root="/ws", session_id="dream:c1",
    )
    assert result.status == "refused_budget"
    assert captured["called"] is False
    assert semantic.calls == []


# ---------- 7. empty overview skips cheaply ----------

async def test_empty_overview_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, semantic, captured, _ = _make_daemon(monkeypatch, overview="")
    result = await daemon.run_consolidation(
        "proj", None, workspace_root="/ws", session_id="dream:c1"
    )
    assert result.status == "skipped_empty"
    assert captured["called"] is False
    assert semantic.calls == []


# ---------- 8. lifecycle start/stop is a clean no-op ----------

async def test_start_stop_clean() -> None:
    d = OvernightDaemon()
    d.start()
    assert d._running is True
    await d.stop()
    assert d._running is False


def test_result_is_frozen() -> None:
    r = ConsolidationResult("written", 42, "Bug Fixes")
    with pytest.raises(Exception):
        r.status = "mutated"  # type: ignore[misc]
