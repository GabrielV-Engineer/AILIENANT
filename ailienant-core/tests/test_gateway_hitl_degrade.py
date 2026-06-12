# ailienant-core/tests/test_gateway_hitl_degrade.py
#
# HITL-degrade tests for the External Capability Gateway: a verb whose permission
# verdict is HITL (a DANGEROUS action) must return an immediate, structured
# deny-report and must NEVER block waiting for an approval that can never arrive
# (no human is in an external caller's loop). In-process, isolated via tmp_path +
# monkeypatch.

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from core.permissions import ToolPrivilegeTier, register_privilege_overrides
from gateway import catalog, ledger, server


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


def _inject_dangerous_verb(monkeypatch: pytest.MonkeyPatch) -> catalog.Capability:
    """Register and inject a synthetic DANGEROUS capability into the resolver.

    Patches ``catalog.get_capability`` (the resolver ``dispatch_call`` actually
    invokes) rather than the CATALOG tuple, so the test is agnostic to whether the
    resolver is a linear scan or a cached map. The original is captured first to
    avoid self-recursion in the replacement.
    """
    register_privilege_overrides({"danger_verb": ToolPrivilegeTier.DANGEROUS})
    danger = catalog.Capability(
        name="danger_verb",
        description="hypothetical irreversible verb",
        tier=ToolPrivilegeTier.DANGEROUS,
        input_schema={"type": "object"},
        is_async=False,
    )
    original = catalog.get_capability
    monkeypatch.setattr(
        catalog,
        "get_capability",
        lambda n: danger if n == "danger_verb" else original(n),
    )
    return danger


# ---------------------------------------------------------------------------
# DoD: DANGEROUS verb degrades to a deny-report, never hangs
# ---------------------------------------------------------------------------


def test_dangerous_verb_returns_deny_report_without_hanging(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_dangerous_verb(monkeypatch)

    async def _drive() -> dict[str, Any]:
        # A hard deadline turns "never hangs" into a falsifiable assertion: if the
        # branch ever awaited a human approval, this would raise TimeoutError.
        result = await asyncio.wait_for(
            server.dispatch_call("danger_verb", {}), timeout=2.0
        )
        return json.loads(result[0].text)

    payload = asyncio.run(_drive())
    assert payload["status"] == "denied"
    assert payload["reason"] == "requires_human_approval"
    assert payload["would_have_required"] == "human_approval"
    assert payload["tier"] == "dangerous"
    assert payload["capability"] == "danger_verb"
    assert isinstance(payload["message"], str) and payload["message"]


def test_gateway_never_binds_human_approval_primitive() -> None:
    # Structural guarantee: the gateway never imports the approval primitive, so it
    # cannot accidentally block on one. A future edit that reintroduces it trips here.
    assert not hasattr(server, "request_human_approval")


# ---------------------------------------------------------------------------
# Regression: bare denials keep their minimal three-key shape
# ---------------------------------------------------------------------------


def test_rate_exceeded_envelope_shape_is_unchanged(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_CAP", "0")
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_REFILL_PER_S", "0")
    result = asyncio.run(server.dispatch_call("query_memory", {"query": "x"}))
    payload = json.loads(result[0].text)
    assert payload == {
        "status": "denied",
        "reason": "rate_exceeded",
        "capability": "query_memory",
    }
