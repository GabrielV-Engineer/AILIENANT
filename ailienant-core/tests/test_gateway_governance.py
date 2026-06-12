# ailienant-core/tests/test_gateway_governance.py
#
# Tier-governance tests for the External Capability Gateway: caller identity,
# the symmetric permission gate, the no-self-escalation posture, and the durable
# per-caller rate + budget DoS guard (clock-skew hardened, fail-closed on lock
# contention). In-process, isolated via tmp_path + monkeypatch.

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from filelock import Timeout

from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    ToolPrivilegeTier,
    classify_tool_privilege,
    register_privilege_overrides,
)
from gateway import catalog, governance, ledger, server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def iso_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the ledger at an isolated tmp file and reset env knobs to defaults."""
    path = tmp_path / "gateway_ledger.json"
    monkeypatch.setattr(ledger, "LEDGER_PATH", path)
    for var in (
        "AILIENANT_GATEWAY_RATE_CAP",
        "AILIENANT_GATEWAY_RATE_REFILL_PER_S",
        "AILIENANT_GATEWAY_BUDGET",
        "AILIENANT_GATEWAY_CALLER_ID",
        "AILIENANT_GATEWAY_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return path


# ---------------------------------------------------------------------------
# Caller identity (D2)
# ---------------------------------------------------------------------------


def test_resolve_caller_id_prefers_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_CALLER_ID", "  claude-code-1 ")
    assert governance.resolve_caller_id() == "claude-code-1"


def test_resolve_caller_id_hashes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AILIENANT_GATEWAY_CALLER_ID", raising=False)
    monkeypatch.setenv("AILIENANT_GATEWAY_TOKEN", "secret-A")
    id_a = governance.resolve_caller_id()
    monkeypatch.setenv("AILIENANT_GATEWAY_TOKEN", "secret-B")
    id_b = governance.resolve_caller_id()
    assert id_a != id_b
    assert len(id_a) == 16 and all(c in "0123456789abcdef" for c in id_a)


def test_resolve_caller_id_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AILIENANT_GATEWAY_CALLER_ID", raising=False)
    monkeypatch.delenv("AILIENANT_GATEWAY_TOKEN", raising=False)
    assert governance.resolve_caller_id() == "anonymous"


# ---------------------------------------------------------------------------
# No self-escalation (DoD) + posture (D4)
# ---------------------------------------------------------------------------


def test_internal_task_mode_is_never_auto() -> None:
    assert governance.INTERNAL_TASK_MODE is SessionPermissionMode.DEFAULT
    assert governance.INTERNAL_TASK_MODE is not SessionPermissionMode.AUTO


def test_resolve_internal_task_mode_ignores_escalation() -> None:
    escalation = {
        "execution_mode": "auto",
        "mode": "AUTO",
        "session_permission_mode": "auto",
        "permission_mode": "edit_execute_rbw",
        "prompt": "do a thing",
    }
    assert (
        governance.resolve_internal_task_mode(escalation)
        is SessionPermissionMode.DEFAULT
    )


# ---------------------------------------------------------------------------
# Symmetric permission gate (D8)
# ---------------------------------------------------------------------------


def test_gateway_verbs_classify_via_shared_catalog() -> None:
    # governance registers its overrides at import; classification is deterministic.
    assert classify_tool_privilege("run_task") is ToolPrivilegeTier.EXECUTE
    assert classify_tool_privilege("check_task_status") is ToolPrivilegeTier.READ_ONLY


def test_authorize_invocation_allows_curated_verbs() -> None:
    for cap in catalog.CATALOG:
        assert governance.authorize_invocation(cap) is PermissionDecision.ALLOW


def test_authorize_invocation_gates_a_dangerous_verb() -> None:
    register_privilege_overrides({"danger_verb": ToolPrivilegeTier.DANGEROUS})
    dangerous = catalog.Capability(
        name="danger_verb",
        description="hypothetical irreversible verb",
        tier=ToolPrivilegeTier.DANGEROUS,
        input_schema={"type": "object"},
        is_async=False,
    )
    assert governance.authorize_invocation(dangerous) is PermissionDecision.HITL


# ---------------------------------------------------------------------------
# Durable rate ceiling (D3, DoD) + metering
# ---------------------------------------------------------------------------


def test_rate_ceiling_rejects_beyond_cap(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_CAP", "3")
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_REFILL_PER_S", "0")  # freeze refill

    async def _drain() -> list[bool]:
        return [await ledger.check_and_consume_rate("caller-x") for _ in range(4)]

    results = asyncio.run(_drain())
    assert results == [True, True, True, False]


def test_dispatch_returns_rate_exceeded(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_CAP", "0")
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_REFILL_PER_S", "0")
    result = asyncio.run(server.dispatch_call("query_memory", {"query": "x"}))
    assert json.loads(result[0].text) == {
        "status": "denied",
        "reason": "rate_exceeded",
        "capability": "query_memory",
    }


def test_read_only_verb_is_metered(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_CAP", "1")
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_REFILL_PER_S", "0")

    async def _two() -> list[dict[str, Any]]:
        first = await server.dispatch_call("query_memory", {"query": "x"})
        second = await server.dispatch_call("query_memory", {"query": "x"})
        return [json.loads(first[0].text), json.loads(second[0].text)]

    first, second = asyncio.run(_two())
    assert first["status"] == "not_implemented"      # allowed, consumed the one token
    assert second["reason"] == "rate_exceeded"        # READ_ONLY call was metered


# ---------------------------------------------------------------------------
# Durable budget ceiling (D3)
# ---------------------------------------------------------------------------


def test_budget_ceiling(iso_ledger: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_BUDGET", "10")

    async def _scenario() -> bool:
        await ledger.consume_budget("caller-b", 12.0)
        return await ledger.budget_exceeded("caller-b")

    assert asyncio.run(_scenario()) is True


def test_dispatch_returns_budget_exceeded(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_BUDGET", "10")

    async def _scenario() -> dict[str, Any]:
        await ledger.consume_budget("anonymous", 50.0)  # default caller id
        result = await server.dispatch_call("query_memory", {"query": "x"})
        return json.loads(result[0].text)

    assert asyncio.run(_scenario())["reason"] == "budget_exceeded"


# ---------------------------------------------------------------------------
# Durability across restart (D3)
# ---------------------------------------------------------------------------


def test_ledger_state_survives_restart(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_CAP", "5")
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_REFILL_PER_S", "0")

    async def _consume_three() -> None:
        for _ in range(3):
            await ledger.check_and_consume_rate("caller-d")
        await ledger.consume_budget("caller-d", 7.0)

    asyncio.run(_consume_three())
    # Simulate a fresh process: re-read the same file via a new _load call.
    persisted = ledger._load()["caller-d"]
    assert persisted["bucket_tokens"] == pytest.approx(2.0)   # 5 - 3
    assert persisted["budget_consumed"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Clock-skew safety + lock isolation / fail-closed
# ---------------------------------------------------------------------------


def test_clock_skew_does_not_spuriously_deny(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_CAP", "5")
    # Seed a refill timestamp far in the future (a backward clock step).
    iso_ledger.write_text(
        json.dumps(
            {"caller-c": {"bucket_tokens": 5.0, "refill_at": 9_999_999_999.0,
                          "budget_consumed": 0.0}}
        ),
        encoding="utf-8",
    )
    granted = asyncio.run(ledger.check_and_consume_rate("caller-c"))
    assert granted is True  # max(0, now - future) == 0, no underflow, not denied
    rec = ledger._load()["caller-c"]
    assert rec["bucket_tokens"] <= 5.0  # never overflows the cap


def test_lock_path_is_dedicated_and_distinct(iso_ledger: Any) -> None:
    lock = ledger._lock_path()
    assert lock != ledger.LEDGER_PATH
    assert str(lock).endswith(".lock")


def test_rate_check_fails_closed_on_lock_timeout(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _AlwaysBusyLock:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...
        def acquire(self) -> None:
            raise Timeout("held by another process")
        def release(self) -> None: ...

    monkeypatch.setattr(ledger, "FileLock", _AlwaysBusyLock)
    # Fail closed: a held lock denies rather than silently allowing.
    assert asyncio.run(ledger.check_and_consume_rate("caller-e")) is False
