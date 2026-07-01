# tests/test_phase8_13_checkpoint_gate.py
"""Polyglot Devcontainer Execution Layer — Division Checkpoint Gate.

Test-only certification that Division 8.13's cross-cutting invariants hold
against their shipped entry points. It imports and invokes production code
(``core.sandbox``, ``api.devcontainer_bridge``, ``api.ws_contracts``), asserting
one load-bearing invariant per row; it modifies no production logic and follows
the sibling-gate convention. Sub-phase unit tests already cover each piece in
isolation (``test_devcontainer_adapter.py``, ``test_devcontainer_bridge.py``,
``test_devcontainer_ws_contract.py``); this gate re-certifies the invariants
from the division's vantage point — the guarantees that must hold *together*.

Rows certified here:
  ORACLE1     constructing the trusted tier never mutates the oracle's
              ACTIVE_TIER / ACTIVE_ADAPTER globals
  ORACLE2     untrusted or session-less execution never resolves to the
              devcontainer tier
  TRUST1      trusted execution with a live session resolves to the
              devcontainer tier, whose fallback targets the Native tier
              (never the untrusted-code cage)
  FALLBACK1   every pre-execution failure (bridge unavailable / provision
              failed / provision timeout) delegates to the fallback
  FALLBACK2   a mid-execution failure degrades in place — it never delegates
              (idempotency: the command may already have run)
  NEVERHANG1  a hanging bridge is bounded by asyncio.wait_for — the adapter
              always returns, it never hangs the caller
  CONTRACT1   every devcontainer WS event round-trips through the inbound
              discriminated-union validator and tolerates an unknown field
  SECRET1     the exec-request wire payload carries env-var NAMES only,
              never values
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

import core.sandbox as sandbox
from api.devcontainer_bridge import WebSocketHostBridge
from api.websocket_manager import ws_adapter
from core.sandbox import (
    DevcontainerSandboxAdapter,
    NativeHITLSandboxAdapter,
    SandboxResult,
)
from core.sandbox import get_trusted_adapter as _real_get_trusted_adapter


# ── Test doubles (mirror test_devcontainer_adapter.py / test_devcontainer_bridge.py) ──


class _FakeBridge:
    def __init__(self) -> None:
        self.provision_calls = 0
        self.exec_calls = 0

    async def ensure_provisioned(self, *, session_id: str, cwd: str) -> bool:
        self.provision_calls += 1
        return True

    async def exec_command(
        self, *, session_id: str, command: str, cwd: str,
        env_whitelist: Dict[str, str], timeout_s: float,
    ) -> SandboxResult:
        self.exec_calls += 1
        return SandboxResult(exit_code=0, stdout="ok", stderr="")

    async def open_host_session(self, **_kw: Any) -> Any:
        raise NotImplementedError


class _DecliningBridge(_FakeBridge):
    async def ensure_provisioned(self, *, session_id: str, cwd: str) -> bool:
        self.provision_calls += 1
        return False


class _HangingProvisionBridge(_FakeBridge):
    async def ensure_provisioned(self, *, session_id: str, cwd: str) -> bool:
        self.provision_calls += 1
        await asyncio.sleep(30)  # cancelled by the adapter's wait_for
        return True  # pragma: no cover


class _RaisingBridge(_FakeBridge):
    async def exec_command(self, **_kw: Any) -> SandboxResult:
        self.exec_calls += 1
        raise RuntimeError("bridge boom")


class _RecordingFallback:
    def __init__(self) -> None:
        self.calls: List[str] = []

    async def execute(
        self, command: str, *, timeout_s: float, cwd: str,
        env_whitelist: Dict[str, str], session_id: Optional[str] = None,
    ) -> SandboxResult:
        self.calls.append(command)
        return SandboxResult(exit_code=0, stdout="fallback-ran", stderr="")


class _FakeManager:
    """Records emitted exec requests; the bridge test double from 8.13.4/8.13.5."""

    def __init__(self) -> None:
        self.exec_requests: List[Dict[str, Any]] = []

    async def emit_devcontainer_exec_request(
        self, *, session_id: str, request_id: str, command: str, cwd: str,
        env_keys: List[str],
    ) -> None:
        self.exec_requests.append({
            "session_id": session_id, "request_id": request_id,
            "command": command, "cwd": cwd, "env_keys": env_keys,
        })

    async def wait_devcontainer_exec(self, **_kw: Any) -> Optional[Dict[str, Any]]:
        return {"stdout": "ok", "stderr": "", "exit_code": 0}


# ── ORACLE1 ───────────────────────────────────────────────────────────────────


def test_oracle1_construction_never_mutates_oracle_globals() -> None:
    before_tier = sandbox.ACTIVE_TIER
    before_adapter = sandbox.ACTIVE_ADAPTER
    _ = DevcontainerSandboxAdapter(bridge=_FakeBridge())
    sandbox.resolve_execution_adapter(session_id="s1", trusted=True)
    assert sandbox.ACTIVE_TIER is before_tier
    assert sandbox.ACTIVE_ADAPTER is before_adapter


# ── ORACLE2 ───────────────────────────────────────────────────────────────────


def test_oracle2_untrusted_or_sessionless_never_reaches_devcontainer() -> None:
    no_session = sandbox.resolve_execution_adapter(session_id=None, trusted=True)
    untrusted = sandbox.resolve_execution_adapter(session_id="s1", trusted=False)
    assert not isinstance(no_session, DevcontainerSandboxAdapter)
    assert not isinstance(untrusted, DevcontainerSandboxAdapter)


# ── TRUST1 ────────────────────────────────────────────────────────────────────


def test_trust1_trusted_session_routes_to_devcontainer_with_native_fallback() -> None:
    # ``_real_get_trusted_adapter`` is captured at import time, so the conftest's
    # test-isolation patch on ``sandbox.get_trusted_adapter`` (which delegates to
    # the oracle to avoid a live HITL wait in unrelated tests) does not apply —
    # this row certifies the real constructor's wiring.
    sandbox.reset_trusted_adapter()
    trusted = _real_get_trusted_adapter()
    assert isinstance(trusted, DevcontainerSandboxAdapter)
    # The fallback is the HITL-gated Native tier — never the untrusted-code cage.
    assert isinstance(trusted._fallback, NativeHITLSandboxAdapter)  # type: ignore[attr-defined]
    sandbox.reset_trusted_adapter()


# ── FALLBACK1 ─────────────────────────────────────────────────────────────────


def test_fallback1_every_pre_execution_failure_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run(bridge: Any) -> SandboxResult:
        fb = _RecordingFallback()
        adapter = DevcontainerSandboxAdapter(bridge=bridge, fallback=fb)  # type: ignore[arg-type]
        result = await adapter.execute(
            "t", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1",
        )
        assert fb.calls == ["t"], "pre-execution failure must delegate to the fallback"
        return result

    # bridge unavailable
    r1 = asyncio.run(_run(None))
    assert r1.stdout == "fallback-ran"

    # provision declined
    r2 = asyncio.run(_run(_DecliningBridge()))
    assert r2.stdout == "fallback-ran"

    # provision timeout
    monkeypatch.setattr(sandbox, "_PROVISION_TIMEOUT_S", 0.01)
    r3 = asyncio.run(_run(_HangingProvisionBridge()))
    assert r3.stdout == "fallback-ran"


# ── FALLBACK2 ─────────────────────────────────────────────────────────────────


def test_fallback2_mid_execution_failure_never_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_BRIDGE_GRACE_S", 0.0)
    fb = _RecordingFallback()
    adapter = DevcontainerSandboxAdapter(bridge=_RaisingBridge(), fallback=fb)  # type: ignore[arg-type]
    result = asyncio.run(
        adapter.execute("t", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1")
    )
    assert result.exit_code == -1
    assert result.stderr == "[devcontainer_bridge_error]"
    assert fb.calls == [], "mid-execution failure must NOT re-run on the fallback"


# ── NEVERHANG1 ────────────────────────────────────────────────────────────────


def test_neverhang1_hanging_bridge_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_PROVISION_TIMEOUT_S", 0.01)
    adapter = DevcontainerSandboxAdapter(bridge=_HangingProvisionBridge())

    async def _scenario() -> SandboxResult:
        # Wrapped in an outer deadline as a second safety net: if the adapter's
        # own bound ever regressed, this test fails fast instead of hanging
        # the suite.
        return await asyncio.wait_for(
            adapter.execute(
                "t", timeout_s=5, cwd="/w", env_whitelist={}, session_id="s1",
            ),
            timeout=2.0,
        )

    result = asyncio.run(_scenario())
    assert result.exit_code == -1
    assert result.stderr == "[devcontainer_provision_timeout]"


# ── CONTRACT1 ─────────────────────────────────────────────────────────────────


def test_contract1_devcontainer_events_are_additive_and_validate() -> None:
    events: List[tuple[str, Dict[str, Any]]] = [
        ("server_devcontainer_provision_request", {"session_id": "s", "request_id": "r", "cwd": "/w"}),
        ("client_devcontainer_provision_status", {"session_id": "s", "request_id": "r", "state": "ready"}),
        ("server_devcontainer_exec_request", {"session_id": "s", "request_id": "r", "command": "x", "cwd": "/w", "env_keys": []}),
        ("client_devcontainer_exec_stream", {"session_id": "s", "request_id": "r", "stream": "stdout", "chunk": "hi"}),
        ("client_devcontainer_exec_exit", {"session_id": "s", "request_id": "r", "exit_code": 0}),
    ]
    for event_type, data in events:
        # Baseline validates.
        result = ws_adapter.validate_json(json.dumps({"event_type": event_type, "data": data}))
        assert result.event_type == event_type
        # An unknown extra field is tolerated (additive-only, §10).
        tolerant_data = {**data, "future_field": "unexpected"}
        tolerant = ws_adapter.validate_json(json.dumps({"event_type": event_type, "data": tolerant_data}))
        assert tolerant.event_type == event_type


# ── SECRET1 ───────────────────────────────────────────────────────────────────


def test_secret1_exec_request_carries_env_names_only() -> None:
    mgr = _FakeManager()
    bridge = WebSocketHostBridge(manager=mgr)  # type: ignore[arg-type]
    asyncio.run(bridge.exec_command(
        session_id="s", command="x", cwd="/w",
        env_whitelist={"CI": "1", "API_KEY": "super-secret-value"},
        timeout_s=5.0,
    ))
    sent = mgr.exec_requests[0]
    assert sorted(sent["env_keys"]) == ["API_KEY", "CI"]
    payload_str = json.dumps(sent)
    assert "super-secret-value" not in payload_str
