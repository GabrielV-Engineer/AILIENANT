# ailienant-core/tests/test_devcontainer_adapter.py
"""Unit tests for the devcontainer trusted-tier sandbox adapter.

The adapter is a thin router over a ``HostExecutionBridge``: it owns
provisioning single-flight, timeout/DLQ degrade, and a never-crash contract.
These tests inject fake bridges and drive the coroutines via ``asyncio.run``
(this repo does not use pytest-asyncio; sync test bodies call ``asyncio.run``).

Loop note: ``asyncio.Lock`` binds to the first running loop, so each adapter
instance is driven by exactly one ``asyncio.run`` — the single-flight test runs
both calls inside one loop via ``asyncio.gather``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Dict, List, Optional

import pytest

import core.sandbox as sandbox
from core.pty_session import PreSpawnGuard, SandboxSession, SandboxSessionError
from core.sandbox import DevcontainerSandboxAdapter, SandboxResult


# ── Test doubles ─────────────────────────────────────────────────────────────


class _StubSession(SandboxSession):
    """Minimal no-op session returned by a fake bridge's open path."""

    async def start(self) -> None:
        return None

    async def run(self, command: str, *, timeout_s: float) -> int:
        return 0

    def stream(self) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            for _ in ():  # empty async generator
                yield b""
        return _gen()

    async def write_stdin(self, data: bytes) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def kill(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeBridge:
    """Records calls and returns canned results (satisfies HostExecutionBridge)."""

    def __init__(self) -> None:
        self.provision_calls = 0
        self.exec_calls = 0
        self.session = _StubSession()

    async def ensure_provisioned(self, *, session_id: str, cwd: str) -> bool:
        self.provision_calls += 1
        return True

    async def exec_command(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        env_whitelist: Dict[str, str],
        timeout_s: float,
    ) -> SandboxResult:
        self.exec_calls += 1
        return SandboxResult(exit_code=0, stdout=f"ran:{command}", stderr="")

    async def open_host_session(
        self,
        *,
        session_id: str,
        cwd: str,
        env_whitelist: Dict[str, str],
        pre_spawn_guard: Optional[PreSpawnGuard],
    ) -> SandboxSession:
        return self.session


class _DecliningBridge(_FakeBridge):
    async def ensure_provisioned(self, *, session_id: str, cwd: str) -> bool:
        self.provision_calls += 1
        return False


class _SlowProvisionBridge(_FakeBridge):
    async def ensure_provisioned(self, *, session_id: str, cwd: str) -> bool:
        self.provision_calls += 1
        await asyncio.sleep(0.05)  # force a real yield so the second caller hits the lock
        return True


class _HangingProvisionBridge(_FakeBridge):
    async def ensure_provisioned(self, *, session_id: str, cwd: str) -> bool:
        self.provision_calls += 1
        await asyncio.sleep(30)  # cancelled by the adapter's wait_for
        return True  # pragma: no cover


class _HangingExecBridge(_FakeBridge):
    async def exec_command(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        env_whitelist: Dict[str, str],
        timeout_s: float,
    ) -> SandboxResult:
        self.exec_calls += 1
        await asyncio.sleep(30)  # cancelled by the adapter's wait_for
        return SandboxResult(exit_code=0, stdout="", stderr="")  # pragma: no cover


class _RaisingBridge(_FakeBridge):
    async def exec_command(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        env_whitelist: Dict[str, str],
        timeout_s: float,
    ) -> SandboxResult:
        self.exec_calls += 1
        raise RuntimeError("bridge boom")


# ── execute() ────────────────────────────────────────────────────────────────


def test_execute_happy_path() -> None:
    bridge = _FakeBridge()
    adapter = DevcontainerSandboxAdapter(bridge=bridge)
    result = asyncio.run(
        adapter.execute(
            "echo hi", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1"
        )
    )
    assert result.exit_code == 0
    assert result.stdout == "ran:echo hi"
    assert bridge.provision_calls == 1
    assert bridge.exec_calls == 1


def test_execute_no_session_is_refused() -> None:
    bridge = _FakeBridge()
    adapter = DevcontainerSandboxAdapter(bridge=bridge)
    result = asyncio.run(
        adapter.execute(
            "echo hi", timeout_s=5, cwd="/w", env_whitelist={}, session_id=None
        )
    )
    assert result.exit_code == -1
    assert result.stderr == "[devcontainer_no_session]"
    assert bridge.provision_calls == 0
    assert bridge.exec_calls == 0


def test_execute_bridge_unavailable() -> None:
    # No injected bridge → _default_host_bridge() returns None (pre-wiring).
    adapter = DevcontainerSandboxAdapter(bridge=None)
    result = asyncio.run(
        adapter.execute(
            "echo hi", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1"
        )
    )
    assert result.exit_code == -1
    assert result.stderr == "[devcontainer_bridge_unavailable]"


def test_execute_provision_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sandbox, "_PROVISION_TIMEOUT_S", 0.01)
    bridge = _HangingProvisionBridge()
    adapter = DevcontainerSandboxAdapter(bridge=bridge)
    with caplog.at_level(logging.CRITICAL, logger="AILIENANT_SANDBOX"):
        result = asyncio.run(
            adapter.execute(
                "echo hi", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1"
            )
        )
    assert result.stderr == "[devcontainer_provision_timeout]"
    assert "[DLQ:Devcontainer]" in caplog.text


def test_execute_provision_declined_is_not_latched() -> None:
    bridge = _DecliningBridge()
    adapter = DevcontainerSandboxAdapter(bridge=bridge)

    async def _run() -> List[SandboxResult]:
        first = await adapter.execute(
            "c", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1"
        )
        second = await adapter.execute(
            "c", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1"
        )
        return [first, second]

    results = asyncio.run(_run())
    assert all(r.stderr == "[devcontainer_provision_failed]" for r in results)
    assert bridge.provision_calls == 2  # a failed provision is retried, not latched
    assert bridge.exec_calls == 0


def test_execute_exec_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sandbox, "_BRIDGE_GRACE_S", 0.0)
    bridge = _HangingExecBridge()
    adapter = DevcontainerSandboxAdapter(bridge=bridge)
    with caplog.at_level(logging.CRITICAL, logger="AILIENANT_SANDBOX"):
        result = asyncio.run(
            adapter.execute(
                "slow", timeout_s=0.01, cwd="/w", env_whitelist={}, session_id="s1"
            )
        )
    assert result.stderr == "[devcontainer_exec_timeout]"
    assert "[DLQ:Devcontainer]" in caplog.text


def test_execute_single_flight_provisioning() -> None:
    bridge = _SlowProvisionBridge()
    adapter = DevcontainerSandboxAdapter(bridge=bridge)

    async def _run() -> List[SandboxResult]:
        return list(
            await asyncio.gather(
                adapter.execute(
                    "a", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1"
                ),
                adapter.execute(
                    "b", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1"
                ),
            )
        )

    results = asyncio.run(_run())
    assert all(r.exit_code == 0 for r in results)
    assert bridge.provision_calls == 1  # single-flight: provisioned once for both
    assert bridge.exec_calls == 2


def test_execute_bridge_exception_degrades(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = _RaisingBridge()
    adapter = DevcontainerSandboxAdapter(bridge=bridge)
    with caplog.at_level(logging.ERROR, logger="AILIENANT_SANDBOX"):
        result = asyncio.run(
            adapter.execute(
                "boom", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1"
            )
        )
    assert result.exit_code == -1
    assert result.stderr == "[devcontainer_bridge_error]"
    assert "Devcontainer bridge exec failed" in caplog.text


# ── open_session() ───────────────────────────────────────────────────────────


def test_open_session_routes_to_bridge() -> None:
    bridge = _FakeBridge()
    adapter = DevcontainerSandboxAdapter(bridge=bridge)
    session = asyncio.run(
        adapter.open_session(cwd="/w", env_whitelist={}, session_id="s1")
    )
    assert session is bridge.session


def test_open_session_no_bridge_raises() -> None:
    adapter = DevcontainerSandboxAdapter(bridge=None)

    async def _run() -> SandboxSession:
        return await adapter.open_session(cwd="/w", env_whitelist={}, session_id="s1")

    with pytest.raises(SandboxSessionError):
        asyncio.run(_run())


def test_supports_sessions_flag() -> None:
    assert DevcontainerSandboxAdapter(bridge=_FakeBridge()).supports_sessions is True


# ── oracle-untouched invariant (hermetic) ────────────────────────────────────


def test_construction_does_not_mutate_resolution_globals() -> None:
    before_tier = sandbox.ACTIVE_TIER
    before_adapter = sandbox.ACTIVE_ADAPTER
    _ = DevcontainerSandboxAdapter(bridge=_FakeBridge())
    # The adapter is inert w.r.t. global tier resolution, so the oracle's
    # get_active_tier()=="DOCKER" guard is provably unaffected.
    assert sandbox.ACTIVE_TIER is before_tier
    assert sandbox.ACTIVE_ADAPTER is before_adapter
