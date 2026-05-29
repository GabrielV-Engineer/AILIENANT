"""Phase 6.10 — Checkpoint Gate Fase 6 (Adversarial E2E).

Twelve adversarial scenarios proving the Phase 6.1–6.9 systemic guarantees hold
under attack: sandbox tier resolution + the Wasm Scope Guard, the FinOps
Supervisor hard-kill and token-spike HITL, the OOM Cascade, the HITL
audit-chain integrity + tamper detection, the secrets scrubber, and the DLQ
resume lifecycle.

Test-only — no production code under ``core/``, ``tools/``, ``shared/``,
``brain/`` or ``main.py`` is modified; the suite only IMPORTS and INVOKES
shipped entry points. Async cases run via ``asyncio.run`` so the suite needs no
``pytest-asyncio`` dependency — mirroring the sibling Phase-6 suites
``test_audit_chain.py`` / ``test_dead_letter.py`` / ``test_oom_cascade.py``.
"""
import asyncio
import hashlib
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import litellm
import pytest
from fastapi.testclient import TestClient
from litellm import ModelResponse
from litellm.exceptions import ContextWindowExceededError

from brain.state import AIlienantGraphState

from core.audit import (
    AuditChainBrokenError,
    init_audit_table,
    log_audit_event,
    verify_chain,
)
from shared.config import MODEL_MEDIUM
from shared.logging_filters import SecretsScrubberFilter
from tools.llm_gateway import LLMGateway


# ── Shared helpers ───────────────────────────────────────────────────────────


class _Acompletion:
    """Stateful ``litellm.acompletion`` stub (copied from test_oom_cascade.py).

    ``raise_seq`` is consumed one entry per call: an exception entry is raised,
    a ``None`` entry returns a fresh ``ModelResponse``. Calls beyond the
    sequence also return a ``ModelResponse``.
    """

    def __init__(self, raise_seq: List[Optional[BaseException]]) -> None:
        self.raise_seq = list(raise_seq)
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> ModelResponse:
        idx = self.calls
        self.calls += 1
        exc = self.raise_seq[idx] if idx < len(self.raise_seq) else None
        if exc is not None:
            raise exc
        return ModelResponse()


def _ctx_err() -> ContextWindowExceededError:
    """A context-window OOM as litellm surfaces it."""
    return ContextWindowExceededError(
        message="context window exceeded",
        model=MODEL_MEDIUM,
        llm_provider="ollama",
    )


def _min_env() -> Dict[str, str]:
    """Minimal env-whitelist so the host shell can still start.

    The NativeHITL adapter passes ``env_whitelist`` as the command's *entire*
    environment. On Windows ``cmd.exe`` cannot initialise without ``SystemRoot``;
    this keeps the no-host-env guarantee (no secrets) while letting the shell
    boot. On POSIX an empty env is fine.
    """
    if os.name == "nt":
        root = os.environ.get("SystemRoot", "")
        return {"SystemRoot": root} if root else {}
    return {}


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point ``core.dead_letter`` at a throwaway catalog DB; return its path."""
    from core import dead_letter

    db = str(tmp_path / "catalog_test.sqlite")
    monkeypatch.setattr(dead_letter, "DB_CATALOG_PATH", db)
    return db


# ══════════════════════════════════════════════════════════════════════════════
# A — Sandbox tier resolution & isolation
# ══════════════════════════════════════════════════════════════════════════════


def test_A1_docker_tier_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon reachable → resolver binds the DOCKER tier (host-isolated)."""
    from core import sandbox

    saved = (sandbox.ACTIVE_TIER, sandbox.ACTIVE_ADAPTER)
    try:
        client = MagicMock()
        client.ping.return_value = True
        monkeypatch.setattr(sandbox.docker, "from_env", lambda: client)

        asyncio.run(sandbox.resolve_default_adapter())

        assert sandbox.get_active_tier() == "DOCKER"
        adapter = sandbox.get_active_adapter()
        # A safe execution routes through Docker, not the host-native tier.
        assert isinstance(adapter, sandbox.DockerSandboxAdapter)
        assert not isinstance(adapter, sandbox.NativeHITLSandboxAdapter)
    finally:
        sandbox.ACTIVE_TIER, sandbox.ACTIVE_ADAPTER = saved


def test_A2_docker_daemon_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker AND Wasm down → resolver degrades to NATIVE_HITL; HITL-gated exec.

    The shipped resolver degrades Docker → Wasm → NativeHITL, so reaching the
    host-native tier requires *both* upper tiers to fail — a total
    sandbox-degradation scenario. Execution then suspends on a human approval.
    """
    from core import sandbox
    from core.sandbox import NativeHITLSandboxAdapter

    saved = (sandbox.ACTIVE_TIER, sandbox.ACTIVE_ADAPTER)
    try:
        dead_client = MagicMock()
        dead_client.ping.side_effect = RuntimeError("docker daemon offline")
        monkeypatch.setattr(sandbox.docker, "from_env", lambda: dead_client)

        def _wasm_boom() -> NativeHITLSandboxAdapter:
            raise RuntimeError("wasmtime runtime unavailable")

        monkeypatch.setattr(sandbox, "WasmSandboxAdapter", _wasm_boom)

        asyncio.run(sandbox.resolve_default_adapter())
        assert sandbox.get_active_tier() == "NATIVE_HITL"

        # HITL approves → the suppressed host command is allowed to run.
        import api.websocket_manager as wsm

        approval = AsyncMock(return_value={"approved": True})
        monkeypatch.setattr(wsm.vfs_manager, "request_human_approval", approval)

        adapter = NativeHITLSandboxAdapter()
        result = asyncio.run(
            adapter.execute(
                "echo hello",
                timeout_s=10.0,
                cwd="",
                env_whitelist=_min_env(),
                session_id="t-a2",
            )
        )

        approval.assert_awaited_once()
        assert result.exit_code == 0
        assert "hello" in result.stdout
    finally:
        sandbox.ACTIVE_TIER, sandbox.ACTIVE_ADAPTER = saved


def test_B1_wasm_scope_guard(tmp_path: Any) -> None:
    """A .wasm payload importing a non-WASI host module trips the Scope Guard.

    ``WasmSandboxAdapter.execute`` catches ``WasmScopeError`` and returns a
    ``SandboxResult``; the exception itself is raised by ``_inspect_module_scope``
    — the seam its own docstring names as the Phase 6.10 B1 caller.
    """
    import wasmtime

    from core.sandbox import WasmSandboxAdapter, WasmScopeError

    wat = tmp_path / "evil.wat"
    # Imports a function from "env" — outside the wasi_snapshot_preview1 allow-list.
    wat.write_text('(module (import "env" "evil" (func)))', encoding="utf-8")

    adapter = WasmSandboxAdapter()
    module = wasmtime.Module.from_file(adapter._engine, str(wat))

    with pytest.raises(WasmScopeError):
        adapter._inspect_module_scope(module)


# ══════════════════════════════════════════════════════════════════════════════
# C — FinOps Supervisor (cost circuit breaker)
# ══════════════════════════════════════════════════════════════════════════════


def test_C1_budget_hard_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spend above 1.10× the ceiling → hard kill flag + route to END.

    The Supervisor reads spend from ``token_ledger.snapshot()``; with budget
    $10.00 the hard-kill threshold is $11.00, so the breach is set to $12.00.
    """
    from core import supervisor
    from core.supervisor import route_after_supervisor, run_supervisor_node

    snap = {
        "estimated_invested_usd": 12.0,
        "local_tokens": 0.0,
        "cloud_tokens": 0.0,
    }
    monkeypatch.setattr(supervisor.token_ledger, "snapshot", lambda: snap)
    monkeypatch.setattr(supervisor, "get_chain_head", AsyncMock(return_value=None))
    monkeypatch.setattr(supervisor, "_force_dlq", AsyncMock())

    state = cast(AIlienantGraphState, {"task_id": "c1", "session_max_budget_usd": 10.0})
    patch_out = asyncio.run(run_supervisor_node(state))

    assert patch_out["security_flags"] == ["SESSION_BUDGET_HARD_KILL"]
    route = route_after_supervisor(
        {"task_id": "c1", "security_flags": patch_out["security_flags"]}
    )
    assert route == "__end__"


def test_C2_token_spike_hitl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-turn token delta above the per-turn ceiling raises a HITL gate."""
    from core import supervisor
    from core.supervisor import run_supervisor_node

    # Within budget (no hard kill / soft gate); 100k tokens this turn > 64k limit.
    snap = {
        "estimated_invested_usd": 0.0,
        "local_tokens": 100_000.0,
        "cloud_tokens": 0.0,
    }
    monkeypatch.setattr(supervisor.token_ledger, "snapshot", lambda: snap)
    monkeypatch.setattr(supervisor, "get_chain_head", AsyncMock(return_value=None))
    supervisor._LAST_TURN_TOKENS.pop("c2", None)

    import api.websocket_manager as wsm

    approval = AsyncMock(return_value={"approved": True})
    monkeypatch.setattr(wsm.vfs_manager, "request_human_approval", approval)

    state = cast(AIlienantGraphState, {"task_id": "c2", "session_max_budget_usd": 10.0})
    asyncio.run(run_supervisor_node(state))

    approval.assert_awaited_once()
    assert approval.await_args is not None
    assert approval.await_args.kwargs["action_description"] == "TOKEN_SPIKE"


# ══════════════════════════════════════════════════════════════════════════════
# D — OOM Cascade & inference resilience
# ══════════════════════════════════════════════════════════════════════════════


def test_D1_oom_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    """ContextWindowExceededError → cascade re-emits to cloud, state marked."""
    stub = _Acompletion([_ctx_err(), None])
    monkeypatch.setattr(litellm, "acompletion", stub)
    state: Dict[str, Any] = {"task_id": "d1"}

    resp = asyncio.run(
        LLMGateway.ainvoke([{"role": "user", "content": "hi"}], state=state)
    )

    assert isinstance(resp, ModelResponse)
    assert stub.calls == 2  # local OOM + cloud rescue
    assert state["oom_fallback_active"] is True


def test_D2_double_oom(monkeypatch: pytest.MonkeyPatch) -> None:
    """OOM on BOTH the local and the cloud call → the exception bubbles up.

    The cascade is sequential, not recursive: a second OOM leaves ``ainvoke``
    uncaught — the ``dead_letter_decorator`` (Phase 6.4) is the intended catcher.
    """
    stub = _Acompletion([_ctx_err(), _ctx_err()])
    monkeypatch.setattr(litellm, "acompletion", stub)

    with pytest.raises(ContextWindowExceededError):
        asyncio.run(
            LLMGateway.ainvoke(
                [{"role": "user", "content": "hi"}], state={"task_id": "d2"}
            )
        )
    assert stub.calls == 2


# ══════════════════════════════════════════════════════════════════════════════
# E — HITL audit chain (cryptographic ledger)
# ══════════════════════════════════════════════════════════════════════════════


def test_E1_audit_chain_integrity(tmp_path: Any) -> None:
    """Three sequential audit events form a verifiable blake2b chain."""
    db = str(tmp_path / "audit_e1.sqlite")

    async def _run() -> bool:
        await init_audit_table(db_path=db)
        await log_audit_event(
            session_id="s1", action_description="BUDGET_OVERFLOW breach",
            proposed_content="cost report", resolution="approved", db_path=db,
        )
        await log_audit_event(
            session_id="s1", action_description="TOKEN_SPIKE turn",
            proposed_content="spike report", resolution="rejected", db_path=db,
        )
        await log_audit_event(
            session_id="s1", action_description="plain event",
            proposed_content=None, resolution="timeout", db_path=db,
        )
        return await verify_chain("s1", db_path=db)

    assert asyncio.run(_run()) is True


def test_E2_audit_tamper_detection(tmp_path: Any) -> None:
    """An out-of-band UPDATE of a historical row is caught by verify_chain."""
    db = str(tmp_path / "audit_e2.sqlite")

    async def _seed() -> None:
        await init_audit_table(db_path=db)
        await log_audit_event(
            session_id="s1", action_description="BUDGET_OVERFLOW",
            proposed_content="x", resolution="approved", db_path=db,
        )
        await log_audit_event(
            session_id="s1", action_description="TOKEN_SPIKE",
            proposed_content="y", resolution="rejected", db_path=db,
        )

    asyncio.run(_seed())

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE hitl_audit_log SET action_description = 'HACKED' WHERE rowid = 2"
    )
    conn.commit()
    conn.close()

    with pytest.raises(AuditChainBrokenError):
        asyncio.run(verify_chain("s1", db_path=db))


# ══════════════════════════════════════════════════════════════════════════════
# F — Secrets scrubber (DLP)
# ══════════════════════════════════════════════════════════════════════════════


def test_F1_secrets_scrubber() -> None:
    """An API key in a log record is replaced with ``REDACTED:<hash8>``.

    The marker is ``REDACTED:`` + the first 8 hex chars of blake2b(key) — no
    asterisks (a locked Phase 6.7 formatting decision).
    """
    key = "sk-ant-AAAAAAAAAAAAAAAAAAAA"
    h8 = hashlib.blake2b(key.encode("utf-8")).hexdigest()[:8]
    expected = f"REDACTED:{h8}"

    record = logging.LogRecord(
        name="dummy", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f"connecting to provider with {key}", args=None, exc_info=None,
    )
    SecretsScrubberFilter().filter(record)
    scrubbed = record.getMessage()

    assert expected in scrubbed
    assert "sk-ant-" not in scrubbed   # the raw key is gone
    assert "*" not in scrubbed         # exact REDACTED:<hash8> form, no asterisks


# ══════════════════════════════════════════════════════════════════════════════
# G — Dead Letter Queue & Resume API
# ══════════════════════════════════════════════════════════════════════════════


def test_G1_dlq_and_resume(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed node's DLQ episode is recovered by POST /api/v1/task/resume."""
    import main
    from core import dead_letter

    _isolate_catalog(tmp_path, monkeypatch)

    async def _seed() -> str:
        await dead_letter.init_dlq_table()
        return await dead_letter.save_dead_letter(
            task_id="g1", thread_id="g1", failed_node="apply_patch",
            exc=RuntimeError("node crashed mid-graph"), state={"task_id": "g1"},
        )

    episode_id = asyncio.run(_seed())

    # Mock the recovery side-effects so the test does not hang on a real graph.
    monkeypatch.setattr(main.checkpoint_manager, "recover", MagicMock())
    monkeypatch.setattr(main.alienant_app, "ainvoke", AsyncMock(return_value={}))

    client = TestClient(main.app)
    resp = client.post("/api/v1/task/resume/g1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["resumed"] is True
    assert body["from_episode"] == episode_id
    assert body["node_resumed_at"] == "apply_patch"


def test_G2_resume_idempotency(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resuming a task whose episode is already resolved is a 200 OK no-op."""
    import main
    from core import dead_letter

    _isolate_catalog(tmp_path, monkeypatch)

    async def _seed_and_resolve() -> None:
        await dead_letter.init_dlq_table()
        episode_id = await dead_letter.save_dead_letter(
            task_id="g2", thread_id="g2", failed_node="apply_patch",
            exc=RuntimeError("node crashed mid-graph"), state={"task_id": "g2"},
        )
        await dead_letter.mark_dlq_resolved(episode_id)  # already recovered once

    asyncio.run(_seed_and_resolve())

    monkeypatch.setattr(main.checkpoint_manager, "recover", MagicMock())
    monkeypatch.setattr(main.alienant_app, "ainvoke", AsyncMock(return_value={}))

    client = TestClient(main.app)
    resp = client.post("/api/v1/task/resume/g2")

    assert resp.status_code == 200
    body = resp.json()
    assert body["resumed"] is False
    assert body["reason"] == "no_dlq_episode"
